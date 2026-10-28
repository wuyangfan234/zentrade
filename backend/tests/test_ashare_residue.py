"""A 股残留守卫（2026-10-11 用户：老的 A 股信息全部删掉；五档盘口删除；财务数据保留；2026-10-12 起路由 SEC EDGAR）。

扫 backend/app 与 frontend/src 的非测试源码，A 股专有词一律不许再出现（少数美股合规说明在白名单里，逐条写明理由）。
策略文件本身（strategy/builtin/*.py）字节参与证据指纹，不扫也不改。
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = re.compile(
    r"A股|A 股|沪深|沪主板|深主板|创业板|科创板|北交所|涨停|跌停|炸板|连板|封板|封单|五档|depth5|DEPTH5|"
    r"akshare|tushare|baostock|东方财富|东财|同花顺|通达信|大智慧|limit_up|limit_down|price_limit|PriceLimit|"
    r"TickFlowStockPanel|tick-stock-panel|tickflow-stock-panel|整手|印花税|上证|深成"
)
# (文件, 片段) —— 只放行美股合规说明：告诉 AI/用户「美股没有这些概念」
ALLOW = {
    ("backend/app/services/stock_analyzer.py", '不要使用"涨停/跌停/连板/封板"等说法'),
    ("backend/app/strategy/prompts/strategy-builder-step1.md", "连续封板"),
    ("backend/app/strategy/prompts/strategy-builder-step2.md", "连续封板"),
    ("backend/app/strategy/ai_generator.py", "不做整手倍乘"),
    # 旧规则兼容: 识别历史规则里残留的旧字段并标注「美股不可用」, 不是功能入口
    ("frontend/src/components/monitor/RuleEditor.tsx", "'consecutive_limit_ups', 'consecutive_limit_downs', 'limit_up_price', 'limit_down_price',"),
    ("frontend/src/components/monitor/RuleEditor.tsx", "'signal_limit_up', 'signal_limit_down', 'signal_limit_down_recovery', 'signal_broken_limit_up',"),
    ("backend/app/strategy/prompts/strategy-guide.md", "不乘整手因子"),
}
SKIP_PREFIX = ("backend/app/strategy/builtin/",)


def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "backend/app", "frontend/src"],
                             check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("不在 git 仓库里（容器内）")
    files = [f for f in out.splitlines() if f]
    if not files:
        pytest.skip("此布局没有 frontend/src")
    return [f for f in files
            if not f.startswith(SKIP_PREFIX) and ".test." not in f and "/__tests__/" not in f
            and f.endswith((".py", ".ts", ".tsx", ".md", ".yaml", ".yml", ".json", ".css"))]


def test_no_ashare_terms_in_sources():
    hits = []
    for rel in _tracked():
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if FORBIDDEN.search(line) and not any(rel == f and frag in line for f, frag in ALLOW):
                hits.append(f"{rel}:{no}: {line.strip()[:120]}")
    assert not hits, "A 股残留:\n" + "\n".join(hits)


def test_depth5_capability_removed():
    from app.data_providers import capabilities as caps
    from app.data_providers.base import ProviderCapabilities
    from app.data_providers.models import Cap
    from app.services import preferences

    assert "depth5" not in {c["id"] for c in caps.CAPABILITY_REGISTRY}
    assert "depth5" not in caps._DATASET_TO_CAPS
    assert not any(c.value.startswith("depth5") for c in Cap)
    assert not hasattr(preferences, "get_depth5_data_provider")
    assert "depth5" not in ProviderCapabilities.__dataclass_fields__


def test_settings_api_has_no_depth5_field():
    from app.api.settings import DataProvidersIn
    assert "depth5_data_provider" not in DataProvidersIn.model_fields
    assert "financial_data_provider" in DataProvidersIn.model_fields   # 财务保留


def test_financial_capability_kept_and_routed_to_sec_edgar():
    from app.data_providers import capabilities as caps
    fin = next(c for c in caps.CAPABILITY_REGISTRY if c["id"] == "financial")
    assert fin["field"] == "financial_data_provider"
    assert "美股暂无可用源" not in fin["desc"] and fin["default"] == "sec_edgar"


def test_storage_has_no_depth5_dir_or_view(tmp_path):
    from app.storage.repository import DataStore
    store = DataStore(tmp_path)
    assert not (tmp_path / "depth5").exists()
    assert (tmp_path / "financials" / "metrics").exists()     # 财务目录保留
    names = {r[0] for r in store.query("SELECT table_name FROM information_schema.tables").fetchall()} \
        if hasattr(store, "query") else set()
    assert "depth5" not in names


def test_board_filter_removed_and_ignored():
    """板块过滤 (沪主板/创业板… 代码前缀) 已删; 旧配置里残留的 boards 键被忽略, 不清零美股。"""
    import polars as pl
    from app.strategy.engine import DEFAULT_BASIC_FILTER, StrategyEngine

    assert "boards" not in DEFAULT_BASIC_FILTER
    df = pl.DataFrame({"symbol": ["AAPL", "600000.SH"], "close": [100.0, 10.0], "amount": [1e9, 1e9]})
    expr = StrategyEngine._build_basic_filter_expr({"boards": ["沪主板"]}) \
        if hasattr(StrategyEngine, "_build_basic_filter_expr") else None
    if expr is not None:
        assert df.filter(expr)["symbol"].to_list() == ["AAPL", "600000.SH"]
    from app.backtest import matrix
    assert not hasattr(matrix, "_symbol_in_boards")


def test_instrument_rows_have_no_limit_columns():
    from app.services import instrument_sync
    rows = instrument_sync._flatten([{"symbol": "AAPL", "name": "Apple", "ext": {"limit_up": 1, "limit_down": 0}}]) \
        if hasattr(instrument_sync, "_flatten") else None
    if rows is not None:
        assert "limit_up" not in rows[0] and "limit_down" not in rows[0]
