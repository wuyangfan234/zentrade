"""监控规则 API 路由 — HTTP 请求 → 调用 monitor_rules 模块 → 同步引擎内存态。

只做胶水: 校验 → 持久化 → 失效引擎内存态。不含评估逻辑。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.strategy import monitor_rules
from app.strategy.intraday_signals import INTRADAY_SIGNAL_LABELS, uses_intraday_signals

router = APIRouter(prefix="/api/monitor-rules", tags=["monitor-rules"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _reconcile_index_asset_type(rule: dict, repo) -> dict:
    """纠正误存为 stock 的指数/ETF 规则 (asset_type → index/etf)。

    个股弹窗加监控 / 点位提醒等入口未传 asset_type, 指数/ETF symbol 的规则被存成
    stock, 导致监控中心显示「个股」、引擎在股票轮评估 (指数/ETF symbol 永不命中)。
    仅当规则全部 symbols 都 resolve 为同一非 stock 类型时纠正 (混合池不动)。
    """
    if rule.get("asset_type", "stock") != "stock" or rule.get("scope") != "symbols":
        return rule
    symbols = [s for s in rule.get("symbols", []) if s]
    if not symbols:
        return rule
    try:
        types = {repo.resolve_asset_type(s) for s in symbols}
    except Exception:
        return rule
    if len(types) == 1:
        only = next(iter(types))
        if only in ("index", "etf"):
            rule["asset_type"] = only
    return rule


def _sync_engine(request: Request) -> None:
    """保存/删除后,把最新规则集 reload 到引擎内存态。"""
    engine = getattr(request.app.state, "monitor_engine", None)
    if engine is not None:
        repo = request.app.state.repo
        rules = [
            _reconcile_index_asset_type(r, repo)
            for r in monitor_rules.load_all(_data_dir(request))
        ]
        engine.set_rules(rules)


# ── Pydantic 模型 ───────────────────────────────────────
class ConditionModel(BaseModel):
    field: str
    op: str            # truth | > >= < <= == !=
    value: float | None = None   # op 非 truth 时必填


class RuleModel(BaseModel):
    id: str
    name: str
    enabled: bool = True
    type: str          # strategy | signal | price | market | volume_delta | date
    asset_type: str = "stock"   # stock | etf (etf: strategy 型走 ETF 历史加载器)
    scope: str = "symbols"   # symbols | all | watchlist_group
    symbols: list[str] = []
    # watchlist_group 作用域: 绑定的自选分组 id (成员动态解析, 增删自选自动生效)
    group_id: str | None = None
    threshold_pct: float = 1.0
    window_minutes: int = 5
    strategy_id: str | None = None
    direction: str = "entry"  # entry | exit | both
    notify_events: list[str] | None = None
    score_min: float | None = None
    score_max: float | None = None
    conditions: list[ConditionModel] = []
    logic: str = "and"        # and | or
    cooldown_seconds: int = 3600
    # date 类型 (日期提醒): 纯日历窗口, 无 conditions
    remind_date: str | None = None   # YYYY-MM-DD
    lead_days: int = 0               # 提前 N 天进入提醒窗口
    severity: str = "info"    # info | warn | critical
    webhook_url: str = ""     # Webhook 推送地址 (推送到 QMT 等外部软件, 待定)
    webhook_enabled: bool = False  # 兼容老规则 (已由 webhook_channels 取代, 仅做向后兼容读)
    webhook_channels: list[str] = []  # 合法值: feishu | wecom | custom | email
    message: str = ""
    # volume_delta 专属: metric=volume 按股数 / amount 按成交额 (USD)
    metric: str = "volume"
    # volume_delta 专属 (轮询放量监控): 相邻两次全市场快照的成交量增量
    threshold_volume: float = 500_000  # 单轮增量 >= 此值(股)时报警 (美股量为股, 9-29)
    threshold_amount: float = 1e6    # metric=amount 时: 单轮增量 >= 此值(USD)时报警
    # 基础过滤 (与策略 basic_filter 语义对齐): 值为 null 表示不过滤
    basic_filter: dict = {}


# ── 字段选项 ─────────────────────────────────────────────
@router.get("/options")
def get_options(request: Request):
    """返回可选字段、信号列、运算符、枚举,供前端表单使用。"""
    from app.indicators.pipeline import ENRICHED_COLUMNS
    from app.services.kline_sync import intraday_monitor_support
    from app.strategy.custom_signals import ALLOWED_FIELDS, load_all as load_csg

    # 阈值字段 (带中文标签)
    threshold_fields = [
        {"key": f, "label": ENRICHED_COLUMNS.get(f, f)}
        for f in sorted(ALLOWED_FIELDS)
    ]
    # 内置信号列 (布尔, 用于 op=truth)
    builtin_signals = [
        {"key": k, "label": v}
        for k, v in ENRICHED_COLUMNS.items()
        if k.startswith("signal_")
    ]
    builtin_signals.extend(
        {"key": key, "label": label}
        for key, label in INTRADAY_SIGNAL_LABELS.items()
    )
    # 自定义信号列 (csg_)
    custom_sigs = []
    try:
        for cs in load_csg(_data_dir(request)):
            if cs.get("enabled") is not False:
                custom_sigs.append({
                    "key": f"csg_{cs['id']}",
                    "label": cs.get("name", cs["id"]),
                })
    except Exception:
        pass

    return {
        "threshold_fields": threshold_fields,
        "builtin_signals": builtin_signals,
        "custom_signals": custom_sigs,
        "operators": [">", ">=", "<", "<=", "==", "!="],
        "types": [
            {"key": "signal", "label": "信号"},
            {"key": "price", "label": "价格/涨跌"},
            {"key": "market", "label": "市场异动"},
            {"key": "strategy", "label": "策略监控"},
            {"key": "volume_delta", "label": "轮询放量"},
            {"key": "date", "label": "日期提醒"},
        ],
        "scopes": [
            {"key": "symbols", "label": "指定标的"},
            {"key": "watchlist_group", "label": "自选分组"},
            {"key": "all", "label": "全市场"},
        ],
        "logics": [
            {"key": "and", "label": "全部满足 (AND)"},
            {"key": "or", "label": "任一满足 (OR)"},
        ],
        "severities": [
            {"key": "info", "label": "普通"},
            {"key": "warn", "label": "警告"},
            {"key": "critical", "label": "重要"},
        ],
        "directions": [
            {"key": "entry", "label": "入场"},
            {"key": "exit", "label": "出场"},
            {"key": "both", "label": "出入都报"},
        ],
        "intraday_signal_support": intraday_monitor_support(
            getattr(request.app.state, "capabilities", None),
        ),
    }


# ── 列表 ───────────────────────────────────────────────
@router.get("")
def list_rules(request: Request):
    repo = request.app.state.repo
    rules = [
        _reconcile_index_asset_type(r, repo)
        for r in monitor_rules.load_all(_data_dir(request))
    ]
    from app.services.kline_sync import intraday_monitor_support

    support = intraday_monitor_support(getattr(request.app.state, "capabilities", None))
    intraday_rules = [
        rule for rule in rules
        if rule.get("enabled", True) and uses_intraday_signals(rule)
    ]
    pooled_symbols = {
        str(symbol)
        for rule in intraday_rules
        for symbol in rule.get("symbols", [])
        if symbol
    }
    runtime_warning = ""
    if intraday_rules and not support["available"]:
        runtime_warning = str(support["reason"])
    elif len(pooled_symbols) > int(support["max_symbols"]):
        runtime_warning = (
            f"分时监听标的池已超限: {len(pooled_symbols)}/{support['max_symbols']}"
        )
    if runtime_warning:
        for rule in intraday_rules:
            rule["runtime_warning"] = runtime_warning
    # 分组作用域规则: 绑定的分组被删除 → 标注运行时警告 (引擎侧已 fail-closed 跳过)
    group_rules = [rule for rule in rules if rule.get("scope") == "watchlist_group"]
    if group_rules:
        from app.services import watchlist as watchlist_service

        try:
            existing_ids = {g["id"] for g in watchlist_service.list_groups()}
            for rule in group_rules:
                if rule.get("group_id") not in existing_ids:
                    rule["runtime_warning"] = "绑定的自选分组已删除, 规则已暂停监控, 编辑可重新选择"
        except Exception:  # noqa: BLE001
            pass
    # 按 created_at 倒序
    rules.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return {"rules": rules}


# ── 新建 / 更新 ────────────────────────────────────────
@router.post("")
def save_rule(req: RuleModel, request: Request):
    rule = monitor_rules.normalize(req.model_dump())
    rule = _reconcile_index_asset_type(rule, request.app.state.repo)
    if rule.get("type") == "strategy":
        from app.strategy.engine import StrategyDataContext

        strategy_engine = getattr(request.app.state, "strategy_engine", None)
        if strategy_engine is None:
            raise HTTPException(status_code=503, detail="策略引擎未初始化")
        try:
            strategy = strategy_engine.get(str(rule.get("strategy_id")))
            strategy_engine.validate_context(
                strategy,
                StrategyDataContext(
                    asset_type=str(rule.get("asset_type") or "stock"),
                    timeframe="1d",
                    as_of=date.today(),
                ),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    # 编辑现有规则时, 保留原 created_at (避免按时间排序时位置跳动)
    existing = monitor_rules.load_one(_data_dir(request), rule["id"])
    # 批次派生规则由「持仓提醒」页托管, 监控中心只读 (启停/改/删均回持仓页)
    if existing and existing.get("lot_id"):
        raise HTTPException(status_code=409, detail="该规则由「持仓提醒」页托管, 请在持仓提醒页修改")
    if existing and existing.get("created_at"):
        rule["created_at"] = existing["created_at"]
    try:
        monitor_rules.validate(rule)
        # Prefixes alone do not establish that a signal exists. Reject at the
        # authoring boundary, without changing historical rule evaluation.
        from app.indicators.pipeline import SIGNAL_DEPENDENCIES
        from app.strategy import custom_signals

        known_signals = set(SIGNAL_DEPENDENCIES) | set(INTRADAY_SIGNAL_LABELS)
        for sig in custom_signals.load_all(_data_dir(request)):
            if sig.get("enabled") is False:
                continue
            prefix = (custom_signals.INTRADAY_PREFIX if sig.get("timeframe") == "intraday"
                      else custom_signals.PREFIX)
            known_signals.add(f"{prefix}{sig['id']}")
        for condition in rule.get("conditions", []):
            field = condition.get("field")
            if condition.get("op") == "truth" and field not in known_signals:
                raise ValueError(f"未知或已停用的信号: {field}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if rule.get("scope") == "watchlist_group":
        # 绑定的分组必须存在 (strategy 层校验形状, 存在性在本层校验)
        from app.services import watchlist as watchlist_service

        group_id = str(rule.get("group_id") or "")
        try:
            group_ids = {g["id"] for g in watchlist_service.list_groups()}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"自选分组读取失败: {e}") from e
        if group_id not in group_ids:
            raise HTTPException(status_code=400, detail="自选分组不存在或已被删除, 请重新选择")
    if rule.get("enabled", True) and uses_intraday_signals(rule):
        from app.services.kline_sync import intraday_monitor_support

        support = intraday_monitor_support(getattr(request.app.state, "capabilities", None))
        if not support["available"]:
            raise HTTPException(status_code=403, detail=str(support["reason"]))
        symbols = set(str(symbol) for symbol in rule.get("symbols", []) if symbol)
        for saved in monitor_rules.load_all(_data_dir(request)):
            if (
                saved.get("id") != rule.get("id")
                and saved.get("enabled", True)
                and uses_intraday_signals(saved)
            ):
                symbols.update(str(symbol) for symbol in saved.get("symbols", []) if symbol)
        max_symbols = int(support["max_symbols"])
        if len(symbols) > max_symbols:
            raise HTTPException(
                status_code=400,
                detail=f"当前分时数据能力最多监听 {max_symbols} 只标的,当前规则合计 {len(symbols)} 只",
            )
    monitor_rules.save_one(_data_dir(request), rule)
    _sync_engine(request)
    return {"ok": True, "rule": rule}


# ── 删除 ───────────────────────────────────────────────
@router.delete("/{rule_id}")
def delete_rule(rule_id: str, request: Request):
    if not monitor_rules.ID_RE.match(rule_id):
        raise HTTPException(status_code=400, detail="规则 id 非法")
    # 批次派生规则由「持仓提醒」页托管, 删除需在持仓页操作 (级联清理派生规则)
    existing = monitor_rules.load_one(_data_dir(request), rule_id)
    if existing and existing.get("lot_id"):
        raise HTTPException(status_code=409, detail="该规则由「持仓提醒」页托管, 请在持仓提醒页删除批次")
    deleted = monitor_rules.delete_one(_data_dir(request), rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="规则不存在")
    _sync_engine(request)
    return {"ok": True}


# ── 演示数据生成 (仅 Dev 页用) ─────────────────────────

import time as _time


def _demo_rule(rule_id: str, name: str, rtype: str, scope: str, symbols: list[str],
               conditions: list[dict], logic: str = "or", cooldown: int = 3600,
               severity: str = "info", message: str = "",
               strategy_id: str | None = None, direction: str = "entry") -> dict:
    rule = monitor_rules.normalize({
        "id": rule_id,
        "name": name,
        "type": rtype,
        "scope": scope,
        "symbols": symbols,
        "conditions": conditions,
        "logic": logic,
        "cooldown_seconds": cooldown,
        "severity": severity,
        "message": message,
        "enabled": True,
    })
    if rtype == "strategy":
        rule["strategy_id"] = strategy_id
        rule["direction"] = direction
    return rule


_DEMO_RULES_TEMPLATE = [
    ("个股信号 · NVDA 放量突破", "signal", "symbols", ["NVDA"],
     [{"field": "signal_volume_surge", "op": "truth"},
      {"field": "signal_n_day_high", "op": "truth"}], "or", "info"),
    ("个股信号 · AAPL 金叉", "signal", "symbols", ["AAPL"],
     [{"field": "signal_ma_golden_5_20", "op": "truth"}], "or", "info"),
    ("价格 · TSLA 跌幅监控", "price", "symbols", ["TSLA"],
     [{"field": "change_pct", "op": "<", "value": -0.03}], "or", "warn", "warn"),
    ("价格 · AMD RSI超卖", "price", "symbols", ["AMD"],
     [{"field": "rsi_14", "op": "<", "value": 30}], "and", "warn", "warn"),
    ("市场异动 · 全市场涨幅超10%", "market", "all", [],
     [{"field": "change_pct", "op": ">", "value": 0.10}], "or", "critical", "critical"),
    ("市场异动 · 全市场创60日新高", "market", "all", [],
     [{"field": "signal_n_day_high", "op": "truth"}], "or", "warn", "warn"),
    ("市场异动 · 跌幅超5%", "market", "all", [],
     [{"field": "change_pct", "op": "<", "value": -0.05}], "or", "warn", "warn"),
    ("个股信号 · MSFT 跌破MA20", "signal", "symbols", ["MSFT"],
     [{"field": "signal_ma20_breakdown", "op": "truth"}], "or", "info"),
]

# 策略类型单独声明 (格式不同: 含 strategy_id + direction)
_DEMO_STRATEGY_RULES: list[dict] = [
    {"name": "策略监控 · 趋势突破", "strategy_id": "trend_breakout", "direction": "entry"},
    {"name": "策略监控 · MACD金叉", "strategy_id": "macd_golden", "direction": "both"},
]


@router.post("/seed")
def seed_demo_rules(request: Request):
    """生成演示监控规则 (Dev 页用)。覆盖 signal/price/market/strategy 四类。"""
    ts = int(_time.time() * 1000)
    created = []
    i = 0
    for (name, rtype, scope, symbols, conditions, logic, severity, sev) in _DEMO_RULES_TEMPLATE:
        rule_id = f"demo_{ts}_{i}"
        rule = _demo_rule(rule_id, name, rtype, scope, symbols, conditions, logic, 3600, sev)
        monitor_rules.save_one(_data_dir(request), rule)
        created.append(rule_id)
        i += 1
    # 策略类型规则
    for sr in _DEMO_STRATEGY_RULES:
        rule_id = f"demo_{ts}_{i}"
        rule = _demo_rule(
            rule_id, sr["name"], "strategy", "all", [], [], "and", 3600, "info",
            strategy_id=sr["strategy_id"], direction=sr.get("direction", "entry"),
        )
        monitor_rules.save_one(_data_dir(request), rule)
        created.append(rule_id)
        i += 1
    _sync_engine(request)
    return {"ok": True, "generated": len(created), "ids": created}
