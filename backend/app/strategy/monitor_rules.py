"""监控规则 — 统一的 MonitorRule 模型,覆盖策略/个股信号/个股价格/市场异动四类。

职责:
  - 从 data/user_data/monitor_rules/*.json 加载规则定义
  - 校验规则字段合法性
  - 提供 CRUD (load_all / save_one / delete_one)

不知道: 行情评估引擎、API、告警落盘。纯函数 + 文件存储。

设计 (镜像 custom_signals.py 的写法):
  - 一对象一文件 + glob 全扫 + 全量重写
  - 字段白名单复用 custom_signals.ALLOWED_FIELDS (阈值条件) + 信号列清单 (布尔条件)
  - id 正则与 custom_signals 一致,保证可纳入同一索引体系
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.services.fs_utils import atomic_write_text
from app.strategy.custom_signals import ALLOWED_FIELDS
from app.strategy.intraday_signals import uses_intraday_signals

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────
ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
RULE_TYPES = {"strategy", "signal", "price", "market", "volume_delta", "date"}
# 旧市场规则类型 (梯队 / 板块 / 异动边缘) 已退役: 历史文件加载时跳过, 不进评估引擎。
RETIRED_RULE_TYPES = {"ladder", "sector", "abnormal"}
SCOPES = {"symbols", "all", "watchlist_group"}
LOGICS = {"and", "or"}
DIRECTIONS = {"entry", "exit", "both"}
STRATEGY_NOTIFY_EVENTS = {"buy_signal", "sell_signal", "pool_entry", "pool_exit"}
SEVERITIES = {"info", "warn", "critical"}
OPS = {">", ">=", "<", "<=", "==", "!="}
# volume_delta 规则 (轮询放量): 阈值口径 (成交量股数 / 成交额 USD)
VD_METRICS = {"volume", "amount"}
# volume_delta 基础过滤默认值 (与策略 DEFAULT_BASIC_FILTER 核心子集对齐,
# 美股口径 9-29 审计: 价格 >=$5, 成交额 >=$20M; 股本门槛无数据源置 None)
VD_BASIC_FILTER_DEFAULTS: dict = {
    "price_min": 5,
    "price_max": None,
    "market_cap_min": None,
    "float_cap_min": None,
    "float_cap_max": None,
    "amount_min": 2e7,
}

# 布尔信号列前缀 (op=truth 时 field 取这些)
_SIGNAL_PREFIXES = ("signal_", "csg_")


# ── 持久化 (镜像 custom_signals.py) ─────────────────────
def _dir(data_dir: Path) -> Path:
    d = data_dir / "user_data" / "monitor_rules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(data_dir: Path, rule_id: str) -> Path:
    return _dir(data_dir) / f"{rule_id}.json"


def load_all(data_dir: Path) -> list[dict]:
    """读取全部监控规则。损坏的文件被跳过。"""
    d = _dir(data_dir)
    out: list[dict] = []
    for f in sorted(d.glob("*.json")):
        try:
            rule = normalize(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning("monitor rule load failed %s: %s", f.name, e)
            continue
        if rule.get("type") in RETIRED_RULE_TYPES:
            logger.warning("monitor rule %s skipped: type %s retired", f.name, rule.get("type"))
            continue
        out.append(rule)
    return out


def load_one(data_dir: Path, rule_id: str) -> dict | None:
    p = _path(data_dir, rule_id)
    if not p.exists():
        return None
    try:
        rule = normalize(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:
        logger.warning("monitor rule load failed %s: %s", rule_id, e)
        return None
    return None if rule.get("type") in RETIRED_RULE_TYPES else rule


def save_one(data_dir: Path, rule: dict) -> None:
    p = _path(data_dir, rule["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    text = _dumps_rule(rule)
    atomic_write_text(p, text)


def delete_one(data_dir: Path, rule_id: str) -> bool:
    p = _path(data_dir, rule_id)
    if p.exists():
        p.unlink()
        return True
    return False


# ── 校验 ────────────────────────────────────────────────
def _is_signal_field(field: str) -> bool:
    """判断 field 是否为布尔信号列 (signal_ / csg_ 前缀)。"""
    return any(field.startswith(p) for p in _SIGNAL_PREFIXES)


def date_rule_in_window(remind_date: str, lead_days: int, today: str) -> bool:
    """提醒窗口 [remind_date - lead_days, remind_date] 是否包含 today (均 YYYY-MM-DD)。

    只判自然日历窗口; 是否在交易时段由调用方决定。到期落在休市/节假日不会顺延,
    需 lead_days 覆盖 (交易日历口径待 issue 定夺)。非法输入一律返回 False (fail-safe)。
    """
    try:
        remind = date.fromisoformat(remind_date)
        today_d = date.fromisoformat(today)
        lead = max(0, int(lead_days or 0))
    except (ValueError, TypeError):
        return False
    start = remind - timedelta(days=lead)
    return start <= today_d <= remind


def validate(rule: dict) -> None:
    """校验一条监控规则,非法则抛 ValueError (含中文信息)。"""
    rid = rule.get("id", "")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        raise ValueError(f"规则 id 非法 (仅小写字母数字下划线, 1-40字符): {rid!r}")
    if not isinstance(rule.get("name"), str) or not rule["name"].strip():
        raise ValueError("规则 name 不能为空")
    if rule.get("type") not in RULE_TYPES:
        raise ValueError(f"type 必须是 {RULE_TYPES} 之一")

    # 指数规则: 仅 signal/price + symbols 作用域 + 不含分时信号
    # (指数无策略语义; 无本地分钟K, 分时信号会静默不触发)
    if rule.get("asset_type") == "index":
        if rule.get("type") not in ("signal", "price"):
            raise ValueError("指数监控仅支持 signal/price 类型")
        if rule.get("scope") != "symbols":
            raise ValueError("指数监控仅支持指定标的 (scope=symbols)")
        if uses_intraday_signals(rule):
            raise ValueError("指数无本地分钟K数据, 不支持分时信号条件")

    # 策略类型: 需要 strategy_id + direction,conditions 可空
    if rule.get("type") == "strategy":
        if not rule.get("strategy_id"):
            raise ValueError("策略类型规则必须指定 strategy_id")
        if rule.get("direction", "entry") not in DIRECTIONS:
            raise ValueError(f"direction 必须是 {DIRECTIONS} 之一")
        notify_events = rule.get("notify_events")
        if not isinstance(notify_events, list) or not notify_events:
            raise ValueError("策略类型规则至少选择一个通知事件")
        invalid_events = set(notify_events) - STRATEGY_NOTIFY_EVENTS
        if invalid_events:
            raise ValueError(f"notify_events 包含非法事件: {sorted(invalid_events)}")
        score_min = rule.get("score_min")
        score_max = rule.get("score_max")
        for label, value in (("评分下限", score_min), ("评分上限", score_max)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{label}必须是 0 到 100 之间的数字")
            if value < 0 or value > 100:
                raise ValueError(f"{label}必须是 0 到 100 之间的数字")
        if score_min is not None and score_max is not None and score_min > score_max:
            raise ValueError("评分下限不能大于评分上限")
    elif rule.get("type") == "volume_delta":
        # 轮询放量监控: 相邻两次全市场快照的成交量/成交额差值, 不用 conditions
        if rule.get("asset_type", "stock") != "stock":
            raise ValueError("轮询放量监控仅支持个股 (依赖全市场股票快照)")
        if rule.get("metric", "volume") not in VD_METRICS:
            raise ValueError(f"metric 必须是 {VD_METRICS} 之一 (volume=成交量股数, amount=成交额 USD)")
        if rule.get("metric", "volume") == "amount":
            thr = rule.get("threshold_amount")
            if isinstance(thr, bool) or not isinstance(thr, (int, float)) or not math.isfinite(thr) or thr < 1:
                raise ValueError("threshold_amount 必须是 >=1 的数字 (单轮成交额增量, 单位 USD)")
        else:
            thr = rule.get("threshold_volume")
            if isinstance(thr, bool) or not isinstance(thr, (int, float)) or not math.isfinite(thr) or thr < 1:
                raise ValueError("threshold_volume 必须是 >=1 的数字 (单轮成交量增量, 单位股)")
        bf = rule.get("basic_filter")
        if bf is not None:
            if not isinstance(bf, dict):
                raise ValueError("basic_filter 必须是对象")
            for key, value in bf.items():
                if key == "exclude_st":
                    continue   # 旧市场的 ST 过滤已停用: 旧规则文件里的该键读到即忽略
                elif key in ("price_min", "price_max", "market_cap_min", "float_cap_min",
                             "float_cap_max", "amount_min"):
                    if value is not None and (
                        isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value <= 0
                    ):
                        raise ValueError(f"basic_filter.{key} 必须是正数字或 null")
                else:
                    raise ValueError(f"basic_filter 不支持字段: {key}")
    elif rule.get("type") == "date":
        # 日期提醒: 纯日历, 锚定标的 (scope=symbols) 避免无对象的空提醒
        remind = rule.get("remind_date")
        if not isinstance(remind, str) or not remind.strip():
            raise ValueError("日期提醒规则必须指定 remind_date")
        try:
            date.fromisoformat(remind.strip())
        except ValueError:
            raise ValueError(f"remind_date 必须是 YYYY-MM-DD 日期: {remind!r}") from None
        lead = rule.get("lead_days", 0)
        if isinstance(lead, bool) or not isinstance(lead, int) or lead < 0:
            raise ValueError("lead_days 必须是非负整数 (提前提醒天数)")
        if rule.get("conditions"):
            raise ValueError("日期提醒规则不支持行情 conditions")
    else:
        # 信号/价格/市场类型: 需要 conditions
        conds = rule.get("conditions")
        if not isinstance(conds, list) or len(conds) == 0:
            raise ValueError("conditions 不能为空")
        if len(conds) > 8:
            raise ValueError("conditions 最多 8 条")
        if rule.get("logic", "and") not in LOGICS:
            raise ValueError(f"logic 必须是 {LOGICS} 之一")
        for i, c in enumerate(conds):
            if not isinstance(c, dict):
                raise ValueError(f"第 {i+1} 个条件格式错误")
            field = c.get("field", "")
            op = c.get("op", "")
            if op == "truth":
                # 布尔信号: field 必须是 signal_/csg_ 前缀
                if not _is_signal_field(field):
                    raise ValueError(f"第 {i+1} 个条件: op=truth 时 field 必须是信号列 (signal_/csg_ 前缀): {field!r}")
            elif op in OPS:
                # 阈值比较: field 必须在白名单, 需要 value
                if field not in ALLOWED_FIELDS:
                    raise ValueError(f"第 {i+1} 个条件: 阈值字段 {field!r} 不在白名单")
                value = c.get("value")
                if (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise ValueError(f"第 {i+1} 个条件: value 必须是有限数字")
            else:
                raise ValueError(f"第 {i+1} 个条件: op {op!r} 非法 (应为 truth 或 {OPS})")

    # scope 校验
    if rule.get("scope", "symbols") not in SCOPES:
        raise ValueError(f"scope 必须是 {SCOPES} 之一")
    if rule.get("scope") == "symbols":
        syms = rule.get("symbols")
        if not isinstance(syms, list) or len(syms) == 0:
            raise ValueError("scope=symbols 时 symbols 不能为空")
    if rule.get("scope") == "watchlist_group":
        # 动态绑定自选分组: 评估时实时解析成员 (分组后续增删自动生效)。
        # 分组存在性由 API 层在保存时校验 (strategy 层不依赖 services)。
        gid = rule.get("group_id")
        if not isinstance(gid, str) or not gid.strip():
            raise ValueError("scope=watchlist_group 时必须选择自选分组")
        if rule.get("asset_type", "stock") != "stock":
            raise ValueError("自选分组作用域仅支持个股")
    if uses_intraday_signals(rule) and rule.get("scope") != "symbols":
        raise ValueError("分时穿越信号仅支持指定标的")

    # 其余枚举
    if rule.get("severity", "info") not in SEVERITIES:
        raise ValueError(f"severity 必须是 {SEVERITIES} 之一")
    cd = rule.get("cooldown_seconds", 3600)
    if isinstance(cd, bool) or not isinstance(cd, int) or cd < 0:
        raise ValueError("cooldown_seconds 必须是非负整数")
    for key in ("threshold_pct", "window_minutes"):
        value = rule.get(key)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{key} 必须是有限数字")


def _dumps_rule(rule: dict) -> str:
    """allow_nan=False: NaN/inf 不能落盘 (落盘后 GET 列表永久 500, 审计 L-12)."""
    return json.dumps(rule, ensure_ascii=False, indent=2, allow_nan=False)


def normalize(rule: dict) -> dict:
    """补全默认字段,返回规范化后的规则 (不校验)。"""
    r = dict(rule)
    r.setdefault("enabled", True)
    r.setdefault("asset_type", "stock")
    # volume_delta 默认全市场, 其余默认指定标的
    r.setdefault("scope", "all" if r.get("type") == "volume_delta" else "symbols")
    r.setdefault("symbols", [])
    r.setdefault("group_id", None)
    # watchlist_group 作用域: 成员动态来自分组, symbols 不参与; 其他作用域清掉残留 group_id
    if r.get("scope") == "watchlist_group":
        r["symbols"] = []
    else:
        r["group_id"] = None
    r.setdefault("threshold_pct", 1.0)
    r.setdefault("window_minutes", 5)
    r.setdefault("strategy_id", None)
    r.setdefault("direction", "entry")
    if r.get("type") == "strategy":
        r.setdefault("score_min", None)
        r.setdefault("score_max", None)
        if r.get("notify_events") is None:
            # 兼容统一监控上线后的旧规则: 当时实际行为是同时通知进入和移出。
            r["notify_events"] = ["pool_entry", "pool_exit"]
        else:
            r["notify_events"] = list(dict.fromkeys(r["notify_events"]))
    else:
        r.pop("notify_events", None)
        r.pop("score_min", None)
        r.pop("score_max", None)
    r.setdefault("conditions", [])
    # volume_delta 专属默认字段 (轮询放量): 冷却期默认 300s 而非 3600s --
    # 持续放量会连续多轮达标, 1 小时只提醒一次太迟钝。
    if r.get("type") == "volume_delta":
        if r.get("cooldown_seconds") is None:
            r["cooldown_seconds"] = 300
        r["metric"] = r.get("metric") if r.get("metric") in VD_METRICS else "volume"
        r.setdefault("threshold_volume", 500_000)  # 股 (美股量为股, 9-29)
        r.setdefault("threshold_amount", 1e6)
        bf = {k: v for k, v in (r.get("basic_filter") or {}).items() if k != "exclude_st"}   # 旧 ST 键已停用
        r["basic_filter"] = {**VD_BASIC_FILTER_DEFAULTS, **bf}
    # date 专属默认字段 (日期提醒): 纯日历窗口, 无行情条件, 每天至多一次
    if r.get("type") == "date":
        r["conditions"] = []
        r.setdefault("remind_date", None)
        r["lead_days"] = int(r.get("lead_days") or 0)
        r["cooldown_seconds"] = 86400
    r.setdefault("logic", "and")
    r.setdefault("cooldown_seconds", 3600)
    r.setdefault("severity", "info")
    r.setdefault("message", "")
    r.setdefault("webhook_url", "")
    r.setdefault("webhook_enabled", False)
    # webhook_channels: 命中时推送的外部渠道。
    # 向后兼容: 老规则只有 webhook_enabled 布尔 (当时勾选即飞书+企业微信双推),
    # 这里把 webhook_enabled=True 但未带 webhook_channels 的老规则迁移为 ['feishu','wecom'],
    # 还原其当时的实际行为, 用户无感知。
    if r.get("webhook_channels") is None:
        r["webhook_channels"] = ["feishu", "wecom"] if r.get("webhook_enabled") else []
    else:
        # 防御性过滤, 只保留合法渠道
        r["webhook_channels"] = [
            c for c in r["webhook_channels"] if c in ("feishu", "wecom", "custom", "email")
        ]
    r.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    return r


# 策略监控自动迁移的规则 id 前缀 (固定, 保证幂等)
STRATEGY_RULE_PREFIX = "mr_strategy_"


def strategy_rule_id(strategy_id: str) -> str:
    """策略监控规则 id = mr_strategy_{strategy_id}。"""
    return f"{STRATEGY_RULE_PREFIX}{strategy_id}"


def migrate_strategy_monitors(data_dir: Path, strategy_ids: list[str], strategy_names: dict[str, str]) -> list[dict]:
    """把 preferences.strategy_monitor_ids 里的策略,同步生成/更新 type=strategy 规则。

    幂等: 已存在的策略规则会被更新 (方向/名称),不会重复创建。
    已从 strategy_ids 移除的策略, 其规则会被停用 (enabled=False) 而非删除 (保留历史触发记录的关联)。

    Args:
        data_dir: 数据目录
        strategy_ids: 当前监控池中的策略 id 列表
        strategy_names: {strategy_id: 策略名} 用于规则显示名
    Returns:
        本次生成/更新的规则列表
    """
    desired = set(strategy_ids)
    existing = load_all(data_dir)
    # 已存在的策略规则 {strategy_id: rule}
    existing_strategy_rules: dict[str, dict] = {}
    for r in existing:
        rid = r.get("id", "")
        if rid.startswith(STRATEGY_RULE_PREFIX):
            sid = rid[len(STRATEGY_RULE_PREFIX):]
            if sid:
                existing_strategy_rules[sid] = r

    touched: list[dict] = []
    # 1. 为当前监控池的策略 upsert 规则
    for sid in desired:
        rule_id = strategy_rule_id(sid)
        name = strategy_names.get(sid, sid)
        rule = existing_strategy_rules.get(sid)
        if rule is None:
            rule = normalize({
                "id": rule_id,
                "name": f"策略监控 · {name}",
                "type": "strategy",
                "scope": "all",
                "strategy_id": sid,
                "direction": "entry",
                "notify_events": ["pool_entry", "pool_exit"],
                "conditions": [],
                "cooldown_seconds": 3600,
                "enabled": True,
            })
        else:
            rule = dict(rule)
            rule["enabled"] = True
            rule["strategy_id"] = sid
            rule["name"] = f"策略监控 · {name}"
            rule.setdefault("scope", "all")
            rule.setdefault("direction", "entry")
        save_one(data_dir, rule)
        touched.append(rule)

    # 2. 不在监控池的策略 → 停用其规则 (不删除)
    for sid, rule in existing_strategy_rules.items():
        if sid not in desired and rule.get("enabled") is not False:
            rule = dict(rule)
            rule["enabled"] = False
            save_one(data_dir, rule)

    return touched
