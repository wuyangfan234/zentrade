"""能力注册表与能力路由矩阵 — 数据集维度的单一权威定义。

能力 (capability) = 一个标准化数据集 (CONTRIBUTING「数据源插件化要求」):
daily / adj_factor / realtime / minute / financial / full_minute (注册表顺序即设置页卡片顺序)。
注册表集中声明每个能力的展示元数据与路由偏好字段, 前端设置页不再各自硬编码。
财务数据: SEC EDGAR 主源 (内置插件 sec_edgar, 默认路由) + Finnhub 备源; 生效源为空时 usable=False。

build_capability_matrix 把注册表、插件/自定义源的能力声明 (datasets) 和当前
路由偏好合并为一个矩阵, 供设置页一次拉全。当前偏好由 API 层注入
(preferences getters 自带合法源校验), 本模块不反向依赖 services 层。

候选契约: 每个能力的 candidates 只包含「当前确实可提供该能力」的源 —
未就绪的插件/自定义源 (依赖未装/Key 未配) 放入 pending 并携带原因,
供前端置灰提示。其他页面可以把 candidates 直接当作可用提供方名单。

usable 契约: 每个能力额外给出 usable = 生效源当前能否真正提供该能力
(生效源在 candidates 中)。各页面的能力门控 (缺能力提示 → 数据源配置)
统一以 usable 为准 — 路由到可用插件时可用, 未配置任何源时不可用。

厂商内置源 (TickFlow) 已于 P1-1b2 退役: 候选源 = 插件 + 自定义 YAML 源。
"""

from __future__ import annotations

from app.data_providers import custom as custom_sources
from app.data_providers.models import Cap, CapabilityLimits, CapabilitySet

# 数据集 → 运行时能力映射 (厂商退役后能力集推导的唯一映射表)
_DATASET_TO_CAPS: dict[str, tuple[Cap, ...]] = {
    "daily": (Cap.KLINE_DAILY_BATCH, Cap.KLINE_DAILY_BY_SYMBOL),
    "minute": (Cap.KLINE_MINUTE_BATCH, Cap.KLINE_MINUTE_BY_SYMBOL),
    "adj_factor": (Cap.ADJ_FACTOR,),
    "realtime": (Cap.QUOTE_BATCH, Cap.QUOTE_BY_SYMBOL),
    "financial": (Cap.FINANCIAL,),
    "full_minute": (Cap.INTRADAY_UNIVERSE, Cap.INTRADAY_BATCH),
}

_CAPABILITY_FIELD: dict[str, str] = {
    "daily": "daily_data_provider",
    "minute": "minute_data_provider",
    "adj_factor": "adj_factor_provider",
    "realtime": "realtime_data_provider",
    "financial": "financial_data_provider",
    "full_minute": "full_minute_data_provider",
}


def current_capability_set(current: dict[str, str]) -> CapabilitySet:
    """厂商内置源退役后的运行时能力集 (P1-1b2)。

    从各数据集的生效自定义源声明 (datasets + 每数据集 rpm/batch) 推导:
    生效源声明了某数据集 → 授予对应能力, 限速取该数据集的声明值。
    current 为 {偏好字段: 当前值} (与 build_capability_matrix 同一注入)。
    未配置/未声明 → 不授予, capset.has() 为 False (fail-closed)。
    """
    caps: dict[Cap, CapabilityLimits] = {}
    for dataset, cap_ids in _DATASET_TO_CAPS.items():
        provider_name = (current.get(_CAPABILITY_FIELD[dataset]) or "").strip()
        if not provider_name:
            continue
        try:
            if not custom_sources.provider_has_dataset(provider_name, dataset):
                continue
            provider = custom_sources.get_provider(provider_name)
        except Exception:  # noqa: BLE001
            continue
        cfg = provider.config.datasets.get(dataset)
        limits = CapabilityLimits(
            rpm=getattr(cfg, "rpm", None),
            batch=getattr(cfg, "batch", None),
        )
        for cap_id in cap_ids:
            if cap_id not in caps:
                caps[cap_id] = limits
    return CapabilitySet(caps)

CAPABILITY_REGISTRY: list[dict] = [
    {
        "id": "daily",
        "label": "日K",
        "desc": "历史K线与实时覆写",
        "field": "daily_data_provider",
        "default": "",
    },
    {
        "id": "adj_factor",
        "label": "除权因子",
        "desc": "前复权计算基准",
        "field": "adj_factor_provider",
        "default": "",
        # 独立路由 (曾经的「跟随日K」特殊值已下线: 每个能力单独配置,
        # 复权口径一致性改由未来的一致性警示保障, 不做路由耦合)
    },
    {
        "id": "realtime",
        "label": "实时行情",
        "desc": "全市场实时快照",
        "field": "realtime_data_provider",
        "default": "",
    },
    {
        "id": "minute",
        "label": "分钟K",
        "desc": "分时图与分钟回测",
        "field": "minute_data_provider",
        "default": "",
    },
    {
        "id": "financial",
        "label": "财务数据",
        "desc": "财务指标与三大报表（SEC EDGAR 主源 · Finnhub 备源）",
        "field": "financial_data_provider",
        "default": "sec_edgar",
    },
    {
        "id": "full_minute",
        "label": "全量分钟",
        "desc": "盘中全市场当日分钟落盘 (冷启动全天 + 标的池增量)",
        "field": "full_minute_data_provider",
        "default": "",
        # 插件/自定义源声明 full_minute 数据集即可提供
        # (插件实现 get_intraday_batch / 可选 get_intraday_latest, YAML 仅修复轮)
    },
]


def _declared_sources() -> list[dict]:
    """插件 + 自定义源 → 统一能力声明视图。未注册 (hidden/加载失败) 的源不会出现。"""
    rows: list[dict] = []
    for plugin in custom_sources.list_plugins():
        rows.append({
            "name": plugin["name"],
            "display": plugin.get("display_name") or plugin["name"],
            "datasets": set(plugin.get("datasets") or []),
            "available": bool(plugin.get("available")),
            "status": str(plugin.get("status") or ""),
            "kind": "plugin",
        })
    for source in custom_sources.list_sources():
        rows.append({
            "name": source["name"],
            "display": source.get("display_name") or source["name"],
            "datasets": set(source.get("datasets") or []),
            # 自定义源注册即已通过加载校验, 视为可用
            "available": True,
            "status": "ok",
            "kind": "custom",
        })
    return rows


def _display_of(sources: list[dict], name: str) -> str:
    for s in sources:
        if s["name"] == name:
            return s["display"]
    return name


def build_capability_matrix(current: dict[str, str]) -> dict:
    """注册表 + 源能力声明 + 当前偏好 → 能力路由矩阵。

    current 为 {偏好字段: 当前值}, 由 API 层经 preferences getters 注入;
    getters 已把非法值 (未注册源) 回退为默认, 这里直接信任。effective
    即当前值本身 (每个能力独立路由, 无跟随/派生特殊值)。
    空字符串 = 未配置任何源 (usable=False, 前端引导配置)。
    """
    sources = _declared_sources()

    capabilities = []
    for cap in CAPABILITY_REGISTRY:
        effective = current.get(cap["field"], cap["default"]) if cap["field"] else cap["default"]
        candidates: list[dict] = []
        pending: list[dict] = []
        for s in sources:
            if cap["id"] not in s["datasets"]:
                continue
            entry = {
                "name": s["name"],
                "display": s["display"],
                "kind": s["kind"],
                "available": s["available"],
                "status": s["status"],
                "note": None if s["available"] else (s["status"] or "不可用"),
            }
            (candidates if s["available"] else pending).append(entry)
        usable = bool(effective) and any(c["name"] == effective for c in candidates)
        capabilities.append({
            "id": cap["id"],
            "label": cap["label"],
            "desc": cap["desc"],
            "field": cap["field"],
            "default": cap["default"],
            "usable": usable,
            "current": effective,
            "current_display": _display_of(sources, effective or ""),
            "effective": effective,
            "effective_display": _display_of(sources, effective or ""),
            "candidates": candidates,
            "pending": pending,
        })
    return {"capabilities": capabilities}
