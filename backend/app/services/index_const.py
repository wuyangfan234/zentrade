"""实时指数核心清单 — 产品级固定契约 (单一权威)。

美股 (P1-4b): 侧栏指数条 / 市场总览固定四只美股核心指数, 不开放配置。
后端消费方 (quote_service / overview) 与前端 Layout 统一引用此处。

监控规则的指数标的不受此限 — quote_service 会把启用规则的指数并入显式拉取。
"""

CORE_INDEX_NAMES: dict[str, str] = {
    "SPY": "标普500",
    "QQQ": "纳斯达克100",
    "DIA": "道琼斯工业",
    "IWM": "罗素2000",
}

CORE_INDEX_SYMBOLS: tuple[str, ...] = tuple(CORE_INDEX_NAMES.keys())
