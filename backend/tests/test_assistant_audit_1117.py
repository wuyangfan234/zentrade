"""AI 助手体检修复 (2026-11-17) 的回归测试。

每条对应一次实测故障 (0.56.0 + 真实数据副本):
1. get_indices: 实时缓存为空 (重启后到开盘前) 时返回 0 条 → 回退本地指数日K收盘
2. 参考 ETF (SPY/QQQ/DIA/IWM): 盘中不在股票缓存, ETF/指数名单为空被判成股票 → 查不到
3. list_data_capabilities: 读已删字段 tf_tier, 每次 KeyError
4. get_factor_values: 只读物化列, 80 个因子 17 个可查 → 走引擎同一现算路径
5. 问「我的持仓」只有 get_lots (持仓提醒, 常为 0) → 模型答「持仓为空」; 新增 get_portfolio
6. run_strategy: 1 小时策略 / 研究模板直接抛英文异常; 单次返回 70 列 x 30 行约 5 万字符
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import ClassVar

import polars as pl
import pytest

from app.custom.assistant import tools as assistant_tools
from app.custom.assistant import tools_extra
from app.ledger.core import Account, Ledger
from app.services import tool_catalog

CORE = ("SPY", "QQQ", "DIA", "IWM")


class _EmptyIndexQuotes:
    """重启后、开盘前的行情服务: 指数缓存为空, 股票缓存只有个股。"""

    def __init__(self, stocks: pl.DataFrame | None = None) -> None:
        self._stocks = stocks if stocks is not None else pl.DataFrame()

    def get_index_quotes(self, symbols=None) -> pl.DataFrame:
        return pl.DataFrame()

    def get_quotes_compat(self) -> pl.DataFrame:
        return self._stocks


def _index_daily(day: date) -> pl.DataFrame:
    rows = []
    closes = {"SPY": (754.37, 739.95), "QQQ": (679.89, 661.75), "DIA": (523.15, 515.68), "IWM": (277.54, 272.94)}
    for symbol, (prev, last) in closes.items():
        rows.append({"symbol": symbol, "date": day - timedelta(days=3), "open": prev, "high": prev, "low": prev,
                     "close": prev, "volume": 1.0, "amount": 1.0})
        rows.append({"symbol": symbol, "date": day, "open": prev, "high": prev, "low": last - 1,
                     "close": last, "volume": 1.0, "amount": 5.0e10})
    return pl.DataFrame(rows)


class _Repo:
    """最小仓库桩: 股票日K缺当日参考 ETF (盘中实况), 指数日K有当日。名单为空 → resolve 全判 stock。"""

    def __init__(self, day: date, data_dir: Path | None = None) -> None:
        self.day = day
        self.index = _index_daily(day)
        self.stock = self.index.filter(pl.col("date") < day)
        self.store = type("S", (), {"data_dir": data_dir})()

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        src = self.index if asset_type == "index" else self.stock
        return src.filter((pl.col("symbol") == symbol) & (pl.col("date") >= start) & (pl.col("date") <= end))

    def get_enriched_latest_asset(self, asset_type, refresh=True):
        if asset_type == "index":
            last = self.index.filter(pl.col("date") == self.day).with_columns(
                pl.lit(-0.0191).alias("change_pct"))
            return last, self.day
        return pl.DataFrame(), None

    def get_name_map(self, symbols=None):
        return {}


@pytest.fixture
def today(monkeypatch) -> date:
    day = date(2026, 11, 16)
    monkeypatch.setattr(tools_extra.us_clock, "today", lambda: day)
    monkeypatch.setattr(assistant_tools, "cn_today", lambda: day)
    return day


# ---------------------------------------------------------------- 1. 指数 0 条

async def test_indices_fall_back_to_index_daily_close_when_realtime_cache_empty(today) -> None:
    ctx = assistant_tools.ToolContext.build(repo=_Repo(today), quote_service=_EmptyIndexQuotes())
    payload = await assistant_tools.execute_assistant_tool("get_indices", {}, ctx)
    result = payload["result"]
    assert result["count"] == 4
    assert result["source"] == "close"
    spy = next(r for r in result["rows"] if r["symbol"] == "SPY")
    assert spy["last_price"] == 739.95
    assert spy["prev_close"] == 754.37
    assert spy["change_pct"] == pytest.approx((739.95 / 754.37 - 1) * 100, abs=1e-3)  # 百分数口径
    assert spy["date"] == today.isoformat()
    assert "收盘" in result["note"]
    assert "收盘" in assistant_tools.summarize_tool_result("get_indices", payload)


async def test_indices_prefer_realtime_cache_when_present(today) -> None:
    class _Live(_EmptyIndexQuotes):
        def get_index_quotes(self, symbols=None):
            return pl.DataFrame([{"symbol": "SPY", "last_price": 741.0, "prev_close": 754.37,
                                  "change_pct": -1.77, "amplitude": 1.2}])

    ctx = assistant_tools.ToolContext.build(repo=_Repo(today), quote_service=_Live())
    result = (await assistant_tools.execute_assistant_tool("get_indices", {}, ctx))["result"]
    assert result["source"] == "realtime"
    assert result["rows"][0]["last_price"] == 741.0
    assert result["rows"][0]["change_pct"] == -1.77


async def test_indices_report_unavailable_instead_of_silent_zero(today) -> None:
    class _NoData(_Repo):
        def get_daily_asset(self, *a, **k):
            return pl.DataFrame()

    ctx = assistant_tools.ToolContext.build(repo=_NoData(today), quote_service=_EmptyIndexQuotes())
    result = (await assistant_tools.execute_assistant_tool("get_indices", {}, ctx))["result"]
    assert result["count"] == 0 and result["source"] == "unavailable"
    assert "检查" in result["note"]


# ---------------------------------------------------------------- 2. 参考 ETF 被当股票

async def test_reference_etf_quote_routes_to_index_table_when_lists_empty(today) -> None:
    ctx = assistant_tools.ToolContext.build(repo=_Repo(today), quote_service=_EmptyIndexQuotes())
    result = (await assistant_tools.execute_assistant_tool("get_stock_quote", {"symbols": list(CORE)}, ctx))["result"]
    assert sorted(r["symbol"] for r in result["rows"]) == sorted(CORE)


async def test_reference_etf_daily_uses_newer_index_table(today) -> None:
    ctx = assistant_tools.ToolContext.build(repo=_Repo(today), quote_service=_EmptyIndexQuotes())
    result = (await assistant_tools.execute_assistant_tool("get_stock_daily", {"symbol": "SPY", "days": 10}, ctx))["result"]
    assert result["rows"][-1]["date"] == today.isoformat()


# ---------------------------------------------------------------- 3. 能力目录 KeyError

def test_list_data_capabilities_uses_real_registry_without_retired_fields() -> None:
    """不打桩: 真实注册表里已没有 tf_tier, 旧实现在这里必然 KeyError。"""
    result = tool_catalog.list_data_capabilities()
    ids = [c["id"] for c in result["capabilities"]]
    assert {"daily", "realtime", "minute", "financial"} <= set(ids)
    assert all(c["label"] for c in result["capabilities"])
    assert all("tf_tier" not in c for c in result["capabilities"])


async def test_list_data_capabilities_tool_succeeds_end_to_end() -> None:
    payload = await assistant_tools.execute_assistant_tool("list_data_capabilities", {}, assistant_tools.ToolContext.build())
    assert payload["ok"] is True, payload
    assert "项能力" in assistant_tools.summarize_tool_result("list_data_capabilities", payload)


# ---------------------------------------------------------------- 4. 因子只能查物化列

class _FactorRepo:
    def __init__(self, day: date, latest: pl.DataFrame, hist: pl.DataFrame | None = None, data_dir=None) -> None:
        self.day, self.latest, self.hist = day, latest, hist
        self.store = type("S", (), {"data_dir": data_dir})()

    def get_enriched_latest(self):
        return self.latest, self.day

    def get_enriched_history(self, target, lookback):
        return self.hist

    def get_name_map(self, symbols=None):
        return {}


def _cross_section(day: date, n: int = 6) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [f"S{i}" for i in range(n)], "date": [day] * n,
        "close": [10.0 + i for i in range(n)], "ma20": [10.0] * n, "change_pct": [0.01 * i for i in range(n)],
    })


async def test_factor_not_materialized_is_computed_with_engine_path(today) -> None:
    repo = _FactorRepo(today, _cross_section(today))
    ctx = assistant_tools.ToolContext.build(repo=repo)
    payload = await assistant_tools.execute_assistant_tool("get_factor_values", {"factor": "ma20_bias", "top": 5}, ctx)
    assert payload["ok"] is True, payload
    result = payload["result"]
    assert result["method"] == "现算"
    assert result["rows"][0]["symbol"] == "S5"  # close 15 / ma20 10 偏离最大
    assert result["valid"] == 6


async def test_factor_change_pct_no_longer_duplicates_column(today) -> None:
    repo = _FactorRepo(today, _cross_section(today))
    payload = await assistant_tools.execute_assistant_tool(
        "get_factor_values", {"factor": "change_pct", "top": 5}, assistant_tools.ToolContext.build(repo=repo))
    assert payload["ok"] is True, payload
    assert payload["result"]["rows"][0]["symbol"] == "S5"


async def test_unknown_factor_error_is_plain_language(today) -> None:
    repo = _FactorRepo(today, _cross_section(today))
    payload = await assistant_tools.execute_assistant_tool(
        "get_factor_values", {"factor": "no_such_factor"}, assistant_tools.ToolContext.build(repo=repo))
    assert payload["ok"] is False
    assert "没有 id 为 no_such_factor 的因子" in payload["error"]
    assert "物化列" not in payload["error"]


async def test_fundamental_factor_is_gated_by_announce_date(today, tmp_path) -> None:
    metrics = pl.DataFrame({
        "symbol": ["S1", "S2", "S3"], "period_end": ["2026-06-30", "2026-06-30", "2026-09-30"],
        "period_type": ["Q", "Q", "Q"],
        "announce_date": ["2026-08-01", "2026-08-02", (today + timedelta(days=5)).isoformat()],  # S3 还没公告
        "roe": [0.10, 0.30, 0.90], "gross_margin": [0.5] * 3, "net_margin": [0.1] * 3, "revenue_yoy": [0.1] * 3,
        "net_income_yoy": [0.1] * 3, "debt_to_asset_ratio": [0.3] * 3, "bps": [5.0] * 3, "source": ["sec_edgar"] * 3,
    })
    (tmp_path / "financials" / "metrics").mkdir(parents=True)
    metrics.write_parquet(tmp_path / "financials" / "metrics" / "part.parquet")
    repo = _FactorRepo(today, _cross_section(today), data_dir=tmp_path)
    payload = await assistant_tools.execute_assistant_tool(
        "get_factor_values", {"factor": "roe_latest", "top": 5}, assistant_tools.ToolContext.build(repo=repo))
    assert payload["ok"] is True, payload
    rows = payload["result"]["rows"]
    assert [r["symbol"] for r in rows] == ["S2", "S1"]  # 未公告的 S3 不得提前出现
    assert payload["result"]["method"] == "财务公告门控"


# ---------------------------------------------------------------- 5. 持仓被答成「为空」

def _ledger(tmp_path: Path) -> Path:
    ledger_dir = tmp_path / "ledger"
    lg = Ledger(data_dir=ledger_dir)
    lg.init_account(Account(initial_equity=100000.0, slippage_bps=0.0, commission_per_share=0.0, min_commission=0.0))
    lg.append("fill", {"symbol": "AAA", "side": "buy", "qty": 100, "price": 50.00, "fee": 0.0,
                       "trade_date": "2026-11-13", "stop_price": 47.00, "card_id": "c1"}, at="2026-11-13T09:45:00-05:00")
    lg.append("fill", {"symbol": "BBB", "side": "buy", "qty": 40, "price": 200.00, "fee": 0.0,
                       "trade_date": "2026-11-13", "stop_price": 190.00, "card_id": "c2"}, at="2026-11-13T10:00:00-05:00")
    lg.append("exit_intent", {"symbol": "AAA", "card_id": "c1", "entry_seq": 1, "reason": "invalidation",
                              "signal_price": 46.80, "trade_date": "2026-11-16"}, at="2026-11-16T16:00:00-05:00")
    return ledger_dir


class _CloseRepo:
    closes: ClassVar[dict[str, float]] = {"AAA": 46.80, "BBB": 196.00}

    def get_daily(self, symbol, start, end, columns=None):
        return pl.DataFrame({"symbol": [symbol], "date": [date(2026, 11, 16)], "close": [self.closes[symbol]]})

    def get_name_map(self, symbols=None):
        return {"AAA": "Alpha Corp", "BBB": "Beta Inc"}


async def test_portfolio_tool_reads_paper_ledger_and_pending_exit(today, tmp_path) -> None:
    _ledger(tmp_path)
    ctx = assistant_tools.ToolContext.build(repo=_CloseRepo(), data_dir=tmp_path)
    payload = await assistant_tools.execute_assistant_tool("get_portfolio", {}, ctx)
    assert payload["ok"] is True, payload
    result = payload["result"]
    assert result["count"] == 2
    first = result["rows"][0]
    assert first["symbol"] == "AAA"  # 待离场排最前
    assert first["below_stop"] is True
    assert first["pending_exit"]["reason_label"] == "跌破止损"
    assert first["pending_exit"]["trade_date"] == "2026-11-16"
    second = result["rows"][1]
    assert second["pending_exit"] is None
    assert second["stop_distance_pct"] == pytest.approx((196.00 - 190.00) / 196.00 * 100, abs=0.01)
    assert result["unrealized_pnl"] == pytest.approx((46.80 - 50.00) * 100 + (196.00 - 200.00) * 40, abs=0.01)
    assert result["open_risk"] == pytest.approx(100 * (50.00 - 47.00) + 40 * (200.00 - 190.00), abs=0.01)
    assert result["as_of"] == "2026-11-16"
    assert "待离场 1" in assistant_tools.summarize_tool_result("get_portfolio", payload)


async def test_portfolio_tool_is_read_only(today, tmp_path) -> None:
    ledger_dir = _ledger(tmp_path)
    before = {p.name: p.read_bytes() for p in ledger_dir.rglob("*") if p.is_file()}
    ctx = assistant_tools.ToolContext.build(repo=_CloseRepo(), data_dir=tmp_path)
    await assistant_tools.execute_assistant_tool("get_portfolio", {}, ctx)
    after = {p.name: p.read_bytes() for p in ledger_dir.rglob("*") if p.is_file()}
    assert before == after


def test_lots_and_portfolio_are_described_as_different_things() -> None:
    schemas = {s["function"]["name"]: s["function"]["description"] for s in assistant_tools.build_tool_schemas()}
    assert "不是模拟账户持仓" in schemas["get_lots"]
    assert "我的持仓" in schemas["get_portfolio"]
    from app.custom.assistant.prompt import build_system_prompt

    text = build_system_prompt(None)
    assert "get_portfolio" in text and "它为空不代表没有持仓" in text


async def test_empty_lots_note_points_to_portfolio(tmp_path) -> None:
    payload = await assistant_tools.execute_assistant_tool(
        "get_lots", {}, assistant_tools.ToolContext.build(data_dir=tmp_path))
    assert "get_portfolio" in payload["result"]["note"]


# ---------------------------------------------------------------- 6. 策略运行

class _MetaEngine:
    def list_strategies(self, include_research=False):
        return [
            {"id": "tv_tke", "name": "TKE 上穿 20", "timeframes": ["1h"]},
            {"id": "factor_rank_research", "name": "因子排名研究", "timeframes": ["1d"], "research_only": True, "scoring": {}},
            {"id": "ok_daily", "name": "日线", "timeframes": ["1d"]},
        ]


def test_strategy_catalog_marks_strategies_that_cannot_run_on_daily() -> None:
    result = tool_catalog.list_strategies(_MetaEngine())
    by = {s["id"]: s for s in result["strategies"]}
    assert "1 小时" in by["tv_tke"]["unavailable"]
    assert "评分字段" in by["factor_rank_research"]["unavailable"]
    assert by["ok_daily"]["unavailable"] is None


async def test_run_strategy_rejects_unavailable_with_plain_reason(today) -> None:
    ctx = assistant_tools.ToolContext.build(repo=_FactorRepo(today, _cross_section(today)), engine=_MetaEngine())
    payload = await assistant_tools.execute_assistant_tool("run_strategy", {"strategy_id": "tv_tke"}, ctx)
    assert payload["ok"] is False
    assert "1 小时" in payload["error"] and "TKE" in payload["error"]


def test_strategy_rows_are_slimmed() -> None:
    row = {f"col{i}": float(i) for i in range(60)}
    row.update({"symbol": "HALO", "close": 96.5, "score": 99.7, "signal_n_day_low": True, "reason": "3 连跌"})
    slim = tools_extra.slim_strategy_row(row)
    assert slim["symbol"] == "HALO" and slim["score"] == 99.7 and slim["reason"] == "3 连跌"
    assert "col5" not in slim
    assert len(json.dumps(slim)) < 400


# ---------------------------------------------------------------- 7. 新工具都已注册且有摘要

def test_new_tools_are_registered_with_schemas() -> None:
    names = {s["function"]["name"] for s in assistant_tools.build_tool_schemas()}
    assert {"get_portfolio", "get_earnings", "get_financials", "get_news", "get_opportunities"} <= names
    assert len(names) == 19


async def test_financials_tool_only_returns_announced_periods(today, tmp_path) -> None:
    for table, extra in (("metrics", {"roe": [0.5, 0.6], "gross_margin": [0.7, 0.75], "net_margin": [0.5, 0.6],
                                      "revenue_yoy": [1.0, 1.1], "net_income_yoy": [1.2, 1.3],
                                      "debt_to_asset_ratio": [0.3, 0.28], "bps": [9.0, 9.5]}),
                         ("income", {"revenue": [9.0e10, 9.6e10], "gross_profit": [6.0e10, 7.2e10],
                                     "operating_income": [5.0e10, 6.3e10], "net_income": [5.0e10, 5.9e10],
                                     "eps_diluted": [2.1, 2.46]})):
        df = pl.DataFrame({"symbol": ["NVDA", "NVDA"], "period_end": ["2026-07-26", "2026-10-26"],
                           "period_type": ["Q", "Q"],
                           "announce_date": ["2026-08-26", (today + timedelta(days=2)).isoformat()],
                           "source": ["sec_edgar", "sec_edgar"], **extra})
        (tmp_path / "financials" / table).mkdir(parents=True)
        df.write_parquet(tmp_path / "financials" / table / "part.parquet")
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)
    result = (await assistant_tools.execute_assistant_tool("get_financials", {"symbol": "nvda"}, ctx))["result"]
    assert [p["period_end"] for p in result["periods"]] == ["2026-07-26"]  # 未公告的下一季不提前出现
    p = result["periods"][0]
    assert p["gross_margin_pct"] == 70.0 and p["revenue_usd_bn"] == 90.0
    assert result["latest_announce"] == "2026-08-26"


def test_prompt_keeps_internal_identifiers_out_of_answers() -> None:
    """实测: 回答里出现 get_portfolio / as_of / reason=invalidation / top_gainers 等英文标识。"""
    from app.custom.assistant import prompt

    text = prompt.build_system_prompt(None)
    assert "正文不出现工具名、英文字段名和代码式取值" in text
    assert "「11-16 收盘」" in text
