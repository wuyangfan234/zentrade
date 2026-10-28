"""用户偏好设置持久化。

存储位置: data/user_data/preferences.json
沿用 secrets_store 的 merge-write 模式,但不做 chmod 0600 (非敏感数据)。
"""
from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# 进程内缓存: 行情轮询线程一轮会调用 8~12 次 getter, 每次读盘+parse 是纯重复;
# 文件仅在用户改设置时变化, 以 (mtime_ns, size) 签名判断是否重读。
_cache: dict | None = None
_cache_sig: tuple[int, int] | None = None


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "preferences.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _invalidate_cache() -> None:
    global _cache, _cache_sig
    _cache = None
    _cache_sig = None


def load() -> dict:
    """读取 preferences.json (带 mtime 签名缓存)。返回深拷贝, 调用方可自由修改。"""
    global _cache, _cache_sig
    p = _path()
    try:
        sig = (p.stat().st_mtime_ns, p.stat().st_size)
    except OSError:
        return {}
    if _cache is not None and sig == _cache_sig:
        return copy.deepcopy(_cache)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("preferences.json malformed: %s", e)
        return {}
    _cache = data
    _cache_sig = sig
    return copy.deepcopy(_cache)


_SAVE_LOCK = threading.Lock()


def save(updates: dict) -> dict:
    """合并写入。返回新内容。

    锁内 read-modify-write: FastAPI 同步端点跑线程池, 并行 PUT 各自基于旧快照
    写盘会互相覆盖 (实测: 压缩总开关并行写分时/日K两键, 后写者把先写者覆盖)。
    """
    with _SAVE_LOCK:
        current = load()
        current.update(updates)
        atomic_write_text(
            _path(), json.dumps(current, indent=2, ensure_ascii=False),
        )
        _invalidate_cache()
    return current


def get_realtime_quotes_enabled() -> bool:
    return load().get("realtime_quotes_enabled", False)


def get_watchlist_groups_in_nav() -> bool:
    """自选分组是否显示在侧边栏（可展开二级子菜单）。默认 False。"""
    return load().get("watchlist_groups_in_nav", False)


def get_realtime_quote_interval() -> float:
    return load().get("realtime_quote_interval", 6.0)


def set_realtime_quote_interval(interval: float) -> float:
    """保存行情轮询间隔（不在此做 min/max 校验，由调用方按档位限制）。

    走 save() 而不是自己 load + write_text: 锁外的 read-modify-write 会用旧快照
    整体覆盖文件, 把并发写入的另一个偏好丢掉 (见 save 的 docstring)。
    """
    save({"realtime_quote_interval": interval})
    return interval


def get_minute_sync_enabled() -> bool:
    return load().get("minute_sync_enabled", False)


def get_minute_intraday_refresh() -> bool:
    """自选列表分时图是否跟随实时行情刷新。

    默认值随权限: 有实时行情权限 (Pro+) 的用户默认开启, 否则关闭。
    用户主动设置过的 (key 存在) 以用户选择为准, 即使是 False 也尊重。
    """
    data = load()
    if "minute_intraday_refresh" in data:
        return bool(data["minute_intraday_refresh"])
    # 未设置过: 有权限默认开, 无权限默认关。
    try:
        from app.services.quote_service import QuoteService
        return QuoteService.is_realtime_allowed()
    except Exception:
        return False


# 分时图实时刷新间隔允许范围 (秒)。下限 3s, 上限 60s。
_INTRADAY_REFRESH_INTERVAL_MIN = 3
_INTRADAY_REFRESH_INTERVAL_MAX = 60


def get_minute_intraday_refresh_interval() -> int:
    """分时图实时刷新轮询间隔 (秒)。默认 6s, 范围 [3, 60]。"""
    return max(_INTRADAY_REFRESH_INTERVAL_MIN,
               min(_INTRADAY_REFRESH_INTERVAL_MAX,
                   int(load().get("minute_intraday_refresh_interval", 6))))


def _normalize_ext_field(raw) -> dict | None:
    """规范化单个 ext 字段配置, 兼容旧字符串格式 ("id.field") 和新对象格式。

    新格式: {"field": "id.field", "maxTags": N, "hiddenIndices": [...]}
    maxTags=0 或缺省=不限制; hiddenIndices 指定要隐藏的位置 (0-based)。
    """
    if raw is None:
        return None
    # 旧格式: 纯字符串 "configId.fieldName"
    if isinstance(raw, str):
        return {"field": raw}
    if isinstance(raw, dict):
        field = raw.get("field")
        if not field:
            return None
        return {
            "field": field,
            "maxTags": int(raw["maxTags"]) if raw.get("maxTags") else 0,
            "hiddenIndices": [int(i) for i in raw["hiddenIndices"]] if raw.get("hiddenIndices") else [],
        }
    return None


def get_monitor_ext_fields() -> dict:
    """监控中心个股通知要展示的 ext 字段 (concept/industry)。

    返回 {"concept": {"field", "maxTags", "hiddenIndices"} | None, ...}。
    后端只需读 .field 构建 ext_columns; maxTags/hiddenIndices 供前端渲染裁剪。
    兼容旧字符串格式 ("id.field") 自动升级。
    """
    data = load()
    raw = data.get("monitor_ext_fields")
    if raw is None:
        # 未配置 = 不展示标签 (原默认指向上游内置预设, 已随预设退役)
        return {"concept": None, "industry": None}
    return {
        "concept": _normalize_ext_field(raw.get("concept")),
        "industry": _normalize_ext_field(raw.get("industry")),
    }


def get_minute_sync_days() -> int:
    return max(1, min(30, load().get("minute_sync_days", 5)))


def get_minute_sync_segment_days() -> int:
    """分钟 K 拉取的单段大小(交易日)。默认 20,范围 [5, 30]。

    每段拉完后立即落盘(流式),避免全量攒内存导致 OOM。
    段越小内存峰值越低但总耗时越长(限速 sleep 随段数线性增加);
    物理上限 ~41 交易日(TickFlow 单次 10000 根 / 一天 241 根 ≈ 41 天),max=30 留出余量。
    """
    return max(5, min(30, load().get("minute_sync_segment_days", 20)))

# ===== 盘中分钟增量刷新 (Expert 专有) =====

# 稳态轮为 intraday.universe 单请求增量, 无脉冲并发, 间隔可低至 3s;
# 全天修复轮 (intraday.batch 28 块爆发) 的 rpm 安全与间隔无关, 由轮次
# 调度 max(间隔, 单轮完成) 天然防重叠。
_MINUTE_REFRESH_INTERVAL_MIN = 3
_MINUTE_REFRESH_INTERVAL_MAX = 120


def get_minute_refresh_enabled() -> bool:
    """盘中分钟K增量落盘开关。默认关闭; 能力门控 (Expert) 在服务层判断。"""
    return bool(load().get("minute_refresh_enabled", False))


def get_minute_refresh_interval() -> int:
    """盘中分钟增量刷新间隔(秒)。默认 6,范围 [3, 120]。"""
    return max(
        _MINUTE_REFRESH_INTERVAL_MIN,
        min(_MINUTE_REFRESH_INTERVAL_MAX, int(load().get("minute_refresh_interval", 6))),
    )


# ===== 数据源选择 (厂商内置源已退役 P1-1b2；只认插件/自定义源；空串=未配置) =====

_ALLOWED_DATA_PROVIDERS: set[str] = set()
DATA_SOURCE_JOB_TIMEOUT_MIN_S = 60


def get_data_source_job_timeout_s() -> int:
    """返回普通数据后台任务的卡死判定时间(秒)。"""
    from app.services.pipeline_jobs import DEFAULT_JOB_TIMEOUT_S
    raw = load().get("data_source_job_timeout_s", DEFAULT_JOB_TIMEOUT_S)
    try:
        timeout_s = int(raw)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_JOB_TIMEOUT_S
    return max(DATA_SOURCE_JOB_TIMEOUT_MIN_S, timeout_s)


def get_data_source_long_job_timeout_s() -> int:
    """返回分钟 K 全市场等长任务的卡死判定时间(秒)。"""
    from app.services.pipeline_jobs import LONG_JOB_TIMEOUT_S
    raw = load().get(
        "data_source_long_job_timeout_s",
        LONG_JOB_TIMEOUT_S,
    )
    try:
        timeout_s = int(raw)
    except (TypeError, ValueError):
        timeout_s = LONG_JOB_TIMEOUT_S
    return max(DATA_SOURCE_JOB_TIMEOUT_MIN_S, timeout_s)


def get_minute_batch_compress() -> bool:
    """分时详情与批量响应是否启用 gzip 传输压缩。默认开启 (公网部署传输是大头);
    本机/内网可关闭省服务端 CPU。每次请求即时读取, 开关保存后立即生效。
    """
    raw = load().get("minute_batch_compress", True)
    return bool(raw)


def get_daily_batch_compress() -> bool:
    """日K详情与批量响应是否启用 gzip 传输压缩 (与分时各自独立配置)。默认开启。"""
    raw = load().get("daily_batch_compress", True)
    return bool(raw)


def _allowed_data_providers() -> set[str]:
    try:
        from app.data_providers import custom as custom_sources
        return _ALLOWED_DATA_PROVIDERS | custom_sources.names()
    except Exception:  # noqa: BLE001
        return set(_ALLOWED_DATA_PROVIDERS)


def get_daily_data_provider() -> str:
    provider = str(load().get("daily_data_provider", "") or "").lower()
    return provider if provider in _allowed_data_providers() else ""


def get_adj_factor_provider() -> str:
    # 「跟随日K」(same_as_daily) 特殊值已下线: 存量配置里的旧值按非法值回退未配置
    provider = str(load().get("adj_factor_provider", "") or "").lower()
    return provider if provider in _allowed_data_providers() else ""


def get_minute_data_provider() -> str:
    provider = str(load().get("minute_data_provider", "") or "").lower()
    return provider if provider in _allowed_data_providers() else ""


def get_full_minute_data_provider() -> str:
    provider = str(load().get("full_minute_data_provider", "") or "").lower()
    return provider if provider in _allowed_data_providers() else ""


def get_realtime_data_provider() -> str:
    provider = str(load().get("realtime_data_provider", "") or "").lower()
    return provider if provider in _allowed_data_providers() else ""


def get_financial_provider() -> str:
    allowed = _allowed_data_providers()
    provider = str(load().get("financial_data_provider", "") or "").lower()
    if provider in allowed:
        return provider
    return "sec_edgar" if "sec_edgar" in allowed else ""   # 未配置时默认 EDGAR (2026-10-12)


# ===== 盘后管道拉取内容开关 (股票 / ETF / 指数 独立控制; 键名 pull_a_share 为历史兼容) =====

def get_pipeline_pull_a_share() -> bool:
    """是否拉取股票日K。默认 True。"""
    return load().get("pipeline_pull_a_share", True)


def get_pipeline_pull_etf() -> bool:
    """是否拉取 ETF 日K。默认 False(标的多,首次较慢)。"""
    return load().get("pipeline_pull_etf", False)


def get_pipeline_pull_index() -> bool:
    """是否拉取指数日K。默认 True。"""
    return load().get("pipeline_pull_index", True)





# (旧市场「主线/情绪」过滤配置 get/set_mainline_* / sentiment_exclude_st 已随主线功能删除;
#  9-30 清理: 引用的边界常量早已不存在, 属死代码且会 NameError。)


_PIPELINE_PULL_KEYS = ("pipeline_pull_a_share", "pipeline_pull_etf", "pipeline_pull_index")


def get_pipeline_pull_types() -> dict:
    """返回三个拉取开关的当前值。"""
    return {
        "pipeline_pull_a_share": get_pipeline_pull_a_share(),
        "pipeline_pull_etf": get_pipeline_pull_etf(),
        "pipeline_pull_index": get_pipeline_pull_index(),
    }


def set_pipeline_pull_types(cfg: dict) -> dict:
    """批量保存拉取开关。只接受白名单内的布尔字段。"""
    updates = {
        k: bool(v) for k, v in cfg.items()
        if k in _PIPELINE_PULL_KEYS and v is not None
    }
    save(updates)
    return get_pipeline_pull_types()


def get_pipeline_index_symbols() -> str:
    """指数自定义拉取代码(逗号/换行/空格分隔)。空串表示全量。"""
    return str(load().get("pipeline_index_symbols", "") or "").strip()


def get_pipeline_schedule() -> dict:
    """返回盘后管道调度时间 {"hour": 16, "minute": 35}。

    默认 16:35 (美东): 美股 16:00 收盘, 供应商聚合官方日K需要缓冲 ——
    收盘即拉可能写入未含尾盘成交的日线。留 35 分钟缓冲 (P1-2b)。
    """
    d = load().get("pipeline_schedule", {"hour": 16, "minute": 35})
    return {"hour": d.get("hour", 16), "minute": d.get("minute", 35)}


def set_pipeline_schedule(hour: int, minute: int) -> dict:
    h = max(0, min(23, hour))
    m = max(0, min(59, minute))
    # 盘后管道不早于 16:35: 美股 16:00 收盘, 供应商官方日线定稿需要缓冲 (P1-2b)
    if h * 60 + m < 16 * 60 + 35:
        h, m = 16, 35
    save({"pipeline_schedule": {"hour": h, "minute": m}})
    return {"hour": h, "minute": m}


def get_instruments_schedule() -> dict:
    """返回盘前标的维表调度时间 {"hour": 9, "minute": 10}。"""
    d = load().get("instruments_schedule", {"hour": 9, "minute": 10})
    return {"hour": d.get("hour", 9), "minute": d.get("minute", 10)}


def set_instruments_schedule(hour: int, minute: int) -> dict:
    h = max(0, min(23, hour))
    m = max(0, min(59, minute))
    # 盘前不晚于 09:15 (美东, 开盘 9:30 前)
    if h * 60 + m > 9 * 60 + 15:
        h, m = 9, 15
    save({"instruments_schedule": {"hour": h, "minute": m}})
    return {"hour": h, "minute": m}


def get_enriched_batch_size() -> int:
    """返回 enriched 全量计算每批 symbol 数量。"""
    return max(1, min(10000, load().get("enriched_batch_size", 1000)))


def set_enriched_batch_size(size: int) -> int:
    """保存 enriched 全量计算批次大小。"""
    size = max(10, min(6000, size))
    save({"enriched_batch_size": size})
    return size


def get_index_daily_batch_size() -> int:
    """返回指数日 K 同步每批 symbol 数量。"""
    return max(1, min(10000, load().get("index_daily_batch_size", 100)))


def set_index_daily_batch_size(size: int) -> int:
    """保存指数日 K 同步批次大小。"""
    size = max(1, min(10000, size))
    save({"index_daily_batch_size": size})
    return size


# 监控外部推送渠道白名单。
# 多选: 不推送 = 空数组, 而非 'none'
PUSH_CHANNELS = {"feishu", "wecom", "custom", "email"}


MINING_BUDGET_PROFILES = frozenset({"balanced", "strict"})


def get_mining_schedule() -> dict:
    """返回周度自动 mining 配置。历史配置缺字段时默认关闭。"""
    data = load()
    weekday = data.get("mining_schedule_weekday", 4)
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 4:
        weekday = 4
    profile = data.get("mining_budget_profile", "balanced")
    if not isinstance(profile, str) or profile not in MINING_BUDGET_PROFILES:
        profile = "balanced"
    enabled = data.get("mining_schedule_enabled", False)
    if not isinstance(enabled, bool):
        enabled = False
    return {
        "mining_schedule_enabled": enabled,
        "mining_schedule_weekday": weekday,
        "mining_budget_profile": profile,
    }


def set_mining_schedule(enabled: bool, weekday: int, profile: str) -> dict:
    """校验并一次写入周度自动 mining 的整组配置。"""
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 4:
        raise ValueError("mining schedule weekday must be between 0 and 4")
    if profile not in MINING_BUDGET_PROFILES:
        raise ValueError("mining budget profile must be balanced or strict")
    result = {
        "mining_schedule_enabled": bool(enabled),
        "mining_schedule_weekday": weekday,
        "mining_budget_profile": profile,
    }
    save(result)
    return result


SSE_REFRESH_PAGES_DEFAULT = {
    "watchlist": True,
    "opportunities": True,     # 0.39.0 机会页雷达: 状态变化即刷新 (关 = 只按 30/60 秒轮询)
}


# ===== 盘中实时行情范围 (独立于盘后管道范围) =====
# 指数不在其中: 展示层固定核心四只 (app.services.index_const), 不开放配置。


def get_realtime_pull_stock() -> bool:
    return load().get("realtime_pull_stock", True)


def get_realtime_pull_etf() -> bool:
    # 老用户兼容: ETF 实时默认关闭，避免升级后请求量/写盘量突然增加。
    return load().get("realtime_pull_etf", False)


def set_realtime_quote_scope(cfg: dict) -> dict:
    updates = {}
    for key in ("realtime_pull_stock", "realtime_pull_etf"):
        if key in cfg and cfg[key] is not None:
            updates[key] = bool(cfg[key])
    if updates:
        save(updates)
    return get_realtime_quote_scope()


def get_realtime_quote_scope() -> dict:
    return {
        "realtime_pull_stock": get_realtime_pull_stock(),
        "realtime_pull_etf": get_realtime_pull_etf(),
    }


def get_sse_refresh_pages() -> dict[str, bool]:
    """返回每个页面的 SSE 刷新开关。"""
    stored = load().get("sse_refresh_pages", {})
    # 合并默认值 (新增页面自动出现)
    result = dict(SSE_REFRESH_PAGES_DEFAULT)
    # 只认当前仍存在的页面 (已退役页面的历史开关不再下发)
    result.update({k: v for k, v in stored.items() if k in SSE_REFRESH_PAGES_DEFAULT})
    return result


def get_strategy_monitor_enabled() -> bool:
    """策略告警评估总开关。"""
    return load().get("strategy_monitor_enabled", False)


def get_system_notify_enabled() -> bool:
    """系统通知开关 — 开启后监控告警同时推送到操作系统通知中心。"""
    return load().get("system_notify_enabled", False)


def set_system_notify_enabled(enabled: bool) -> bool:
    """保存系统通知开关。"""
    save({"system_notify_enabled": bool(enabled)})
    return bool(enabled)


def get_feishu_app_chat_id() -> str:
    """飞书应用机器人推送目标群 (oc_ 开头的 chat_id)。凭据在环境变量, 不在偏好里。"""
    return str(load().get("feishu_app_chat_id", "") or "").strip()


def set_feishu_app_chat_id(chat_id: str) -> str:
    """保存应用机器人推送目标群; 传空串表示清空。"""
    save({"feishu_app_chat_id": str(chat_id or "").strip()})
    return get_feishu_app_chat_id()


def get_feishu_webhook_url() -> str:
    """飞书自定义机器人 Webhook 地址 — 全局共用一处, 所有启用推送的规则都推到这一个群。"""
    return load().get("feishu_webhook_url", "")


def get_feishu_webhook_secret() -> str:
    """飞书自定义机器人签名密钥 — 机器人启用「签名校验」时必填, 留空表示不验签。"""
    return load().get("feishu_webhook_secret", "")


def set_feishu_webhook_url(url: str) -> str:
    """保存飞书 Webhook 地址。传入空串表示清空配置。"""
    save({"feishu_webhook_url": str(url or "").strip()})
    return get_feishu_webhook_url()


def set_feishu_webhook_secret(secret: str) -> str:
    """保存飞书签名密钥。传入空串表示不验签。"""
    save({"feishu_webhook_secret": str(secret or "").strip()})
    return get_feishu_webhook_secret()


def get_wecom_webhook_url() -> str:
    """企业微信群推送 Webhook 地址 — 与飞书并列的第二推送通道。

    存储完整 URL (https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx);
    用户也可只填 key, 由 webhook_adapter.normalize_wecom_url 自动补全。
    """
    return load().get("wecom_webhook_url", "")


def set_wecom_webhook_url(url: str) -> str:
    """保存企业微信 Webhook 地址。传入空串表示清空配置。

    存储时统一补全为完整 URL, 避免后续每次推送都要再判一次。
    """
    from app.services.webhook_adapter import normalize_wecom_url
    save({"wecom_webhook_url": normalize_wecom_url(url)})
    return get_wecom_webhook_url()


def get_custom_webhook_url() -> str:
    """Generic third-party JSON Webhook URL shared by enabled rules and reviews."""
    return str(load().get("custom_webhook_url") or "")


def set_custom_webhook_url(url: str) -> str:
    """Persist or clear the generic third-party JSON Webhook URL."""
    value = str(url or "").strip()
    save({"custom_webhook_url": value})
    return value


_EMAIL_SMTP_DEFAULTS = {
    "host": "",
    "port": 465,
    "security": "ssl",
    "username": "",
    "from_address": "",
    "to_addresses": [],
}


def get_email_smtp_config() -> dict:
    """Return non-secret SMTP settings for the email notification channel."""
    raw = load().get("email_smtp_config")
    if not isinstance(raw, dict):
        raw = {}
    security = raw.get("security", _EMAIL_SMTP_DEFAULTS["security"])
    if security not in {"ssl", "starttls", "none"}:
        security = _EMAIL_SMTP_DEFAULTS["security"]
    try:
        port = int(raw.get("port", _EMAIL_SMTP_DEFAULTS["port"]))
    except (TypeError, ValueError):
        port = _EMAIL_SMTP_DEFAULTS["port"]
    if not 1 <= port <= 65535:
        port = _EMAIL_SMTP_DEFAULTS["port"]
    recipients = raw.get("to_addresses")
    if not isinstance(recipients, list):
        recipients = []
    return {
        "host": str(raw.get("host") or "").strip(),
        "port": port,
        "security": security,
        "username": str(raw.get("username") or "").strip(),
        "from_address": str(raw.get("from_address") or "").strip(),
        "to_addresses": [str(item).strip() for item in recipients if str(item).strip()],
    }


def set_email_smtp_config(config: dict) -> dict:
    """Atomically persist the non-secret SMTP configuration group."""
    normalized = {
        "host": str(config.get("host") or "").strip(),
        "port": int(config.get("port", 465)),
        "security": str(config.get("security") or "ssl"),
        "username": str(config.get("username") or "").strip(),
        "from_address": str(config.get("from_address") or "").strip(),
        "to_addresses": [
            str(item).strip() for item in config.get("to_addresses", []) if str(item).strip()
        ],
    }
    save({"email_smtp_config": normalized})
    return get_email_smtp_config()


# ===== 企业微信智能机器人 (API 模式 / 长连接) =====


def get_wecom_bot_id() -> str:
    """企业微信智能机器人 BotID — 机器人的唯一标识。"""
    return load().get("wecom_bot_id", "")


def set_wecom_bot_id(bot_id: str) -> str:
    """保存智能机器人 BotID。传入空串表示清空。"""
    save({"wecom_bot_id": (bot_id or "").strip()})
    return get_wecom_bot_id()


def get_wecom_bot_secret() -> str:
    """企业微信智能机器人 Secret — 长连接专用密钥。"""
    return load().get("wecom_bot_secret", "")


def set_wecom_bot_secret(secret: str) -> str:
    """保存智能机器人 Secret。传入空串表示清空。"""
    save({"wecom_bot_secret": (secret or "").strip()})
    return get_wecom_bot_secret()


def get_wecom_bot_enabled() -> bool:
    """智能机器人长连接是否启用。默认 False(需用户配置凭证后手动开启)。"""
    return load().get("wecom_bot_enabled", False)


def set_wecom_bot_enabled(enabled: bool) -> bool:
    """保存智能机器人启用状态。"""
    save({"wecom_bot_enabled": bool(enabled)})
    return get_wecom_bot_enabled()



def get_webhook_enabled_default() -> bool:
    """新建监控规则时是否默认勾选推送 (老布尔, 已由 webhook_default_channels 取代)。

    保留向后兼容: 读取 webhook_default_channels 非空时返回 True。
    """
    return bool(get_webhook_default_channels())


def set_webhook_enabled_default(enabled: bool) -> bool:
    """保存推送默认勾选态 (老布尔兼容入口)。

    新数据模型为渠道数组; 此处把老布尔转译: True→['feishu','wecom'], False→[]。
    """
    set_webhook_default_channels(["feishu", "wecom"] if enabled else [])
    return get_webhook_enabled_default()


def get_webhook_default_channels() -> list[str]:
    """新建监控规则的默认渠道 + 机会页开盘放量提醒渠道 (多选)。

    监控规则只把它作为新建时的预填, 单条规则仍可独立修改; 开盘放量提醒在发送时直接跟随这里的当前勾选。
    空列表 = 新建规则默认不推送, 且开盘放量不进入外部推送队列。

    向后兼容: 老版本只有布尔 webhook_enabled_default (勾选即飞书+企业微信双推),
    这里把 True 迁移为 ['feishu','wecom'], 还原当时的实际行为。
    """
    d = load()
    raw = d.get("webhook_default_channels")
    if isinstance(raw, list):
        return [c for c in raw if c in PUSH_CHANNELS]
    # 兼容老布尔开关 (勾选即双推)
    if d.get("webhook_enabled_default") is True:
        return ["feishu", "wecom"]
    return []


def set_webhook_default_channels(channels: list[str]) -> list[str]:
    """保存新建规则默认推送渠道 (多选)。过滤白名单外、去重、保序。空列表 = 不推送。"""
    seen: set[str] = set()
    cleaned: list[str] = []
    for c in channels or []:
        if c in PUSH_CHANNELS and c not in seen:
            seen.add(c)
            cleaned.append(c)
    save({"webhook_default_channels": cleaned})
    return cleaned


def get_screener_auto_run() -> bool:
    """选股页进入时是否自动运行所有策略 (获取命中数)。默认开。"""
    return load().get("screener_auto_run", True)


def get_strategy_monitor_ids() -> list[str]:
    """返回监控池中的策略 ID。"""
    return load().get("strategy_monitor_ids", [])


def set_realtime_monitor_config(cfg: dict) -> dict:
    """批量更新实时监控配置。"""
    updates = {}
    if "sse_refresh_pages" in cfg:
        updates["sse_refresh_pages"] = cfg["sse_refresh_pages"]
    if "strategy_monitor_enabled" in cfg:
        updates["strategy_monitor_enabled"] = cfg["strategy_monitor_enabled"]
    if "strategy_monitor_ids" in cfg:
        updates["strategy_monitor_ids"] = cfg["strategy_monitor_ids"]
    if "screener_auto_run" in cfg:
        updates["screener_auto_run"] = bool(cfg["screener_auto_run"])
    if "minute_intraday_refresh" in cfg:
        updates["minute_intraday_refresh"] = bool(cfg["minute_intraday_refresh"])
    if "minute_intraday_refresh_interval" in cfg:
        # clamp 到 [5, 60], 与 getter 一致, 防前端传越界值
        updates["minute_intraday_refresh_interval"] = max(
            _INTRADAY_REFRESH_INTERVAL_MIN,
            min(_INTRADAY_REFRESH_INTERVAL_MAX, int(cfg["minute_intraday_refresh_interval"])))
    if "minute_refresh_enabled" in cfg:
        updates["minute_refresh_enabled"] = bool(cfg["minute_refresh_enabled"])
    if "minute_refresh_interval" in cfg:
        # clamp 到 [3, 120], 与 getter 一致, 防前端传越界值
        updates["minute_refresh_interval"] = max(
            _MINUTE_REFRESH_INTERVAL_MIN,
            min(_MINUTE_REFRESH_INTERVAL_MAX, int(cfg["minute_refresh_interval"])))
    if "monitor_ext_fields" in cfg:
        raw = cfg["monitor_ext_fields"] or {}
        updates["monitor_ext_fields"] = {
            "concept": _normalize_ext_field(raw.get("concept")),
            "industry": _normalize_ext_field(raw.get("industry")),
        }
    if updates:
        save(updates)
    return get_realtime_monitor_config()


def get_realtime_monitor_config() -> dict:
    """返回完整的实时监控配置。"""
    return {
        "sse_refresh_pages": get_sse_refresh_pages(),
        "strategy_monitor_enabled": get_strategy_monitor_enabled(),
        "strategy_monitor_ids": get_strategy_monitor_ids(),
        "screener_auto_run": get_screener_auto_run(),
        "minute_intraday_refresh": get_minute_intraday_refresh(),
        "minute_intraday_refresh_interval": get_minute_intraday_refresh_interval(),
        "minute_refresh_enabled": get_minute_refresh_enabled(),
        "minute_refresh_interval": get_minute_refresh_interval(),
        "monitor_ext_fields": get_monitor_ext_fields(),
    }


def get_nav_order() -> list[str]:
    """返回左侧菜单的自定义排序（内置页面 path + 扩展分析菜单 id）。"""
    return load().get("nav_order", [])


def set_nav_order(order: list[str]) -> list[str]:
    """保存左侧菜单排序。"""
    save({"nav_order": order})
    return get_nav_order()


def get_nav_hidden() -> list[str]:
    """返回左侧菜单中隐藏的项 id 列表。"""
    return load().get("nav_hidden", [])


def set_nav_hidden(hidden: list[str]) -> list[str]:
    """保存左侧菜单隐藏项。"""
    save({"nav_hidden": hidden})
    return get_nav_hidden()


def get_watchlist_columns() -> list[dict] | None:
    """返回自选列表列配置。"""
    return load().get("watchlist_columns")


def set_watchlist_columns(columns: list[dict]) -> list[dict]:
    """保存自选列表列配置。"""
    save({"watchlist_columns": columns})
    return columns


def get_screener_result_columns() -> list[dict] | None:
    """返回策略结果列表列配置。"""
    return load().get("screener_result_columns")


def set_screener_result_columns(columns: list[dict]) -> list[dict]:
    """保存策略结果列表列配置。"""
    save({"screener_result_columns": columns})
    return columns


# ===== 首次使用引导 =====

def get_onboarding_completed() -> bool:
    """是否已完成首次使用向导。默认 False（新用户）。"""
    return bool(load().get("onboarding_completed", False))


def set_onboarding_completed(done: bool = True) -> bool:
    """标记首次使用向导完成状态。"""
    save({"onboarding_completed": bool(done)})
    return bool(done)


# ===== 财务数据同步时间(持久化,重启不丢失) =====
# 结构: { "metrics": "2026-06-25T10:00:00+08:00", "income": ..., ... }

def get_financial_sync_times() -> dict[str, str]:
    """返回各财务表的最后同步时间(ISO 字符串)。未同步过的表不在返回值中。"""
    return load().get("financial_sync_times", {}) or {}
