"""自选页 enriched 端点的 LEFT JOIN 回归测试.

核心契约 (修复 inner-filter bug 后):
  自选列表里的每一只标的都必须出现在返回结果中, 即使它不在 enriched 缓存里
  (新股 / 冷门股 / 新用户未同步). 缺失标的的指标字段为 null, 前端渲染为 "—".

旧 bug: `df_e.filter(is_in(stock_symbols))` 以 enriched 为主表, 会把不在缓存
universe 里的自选股静默丢弃.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import watchlist as wl_api


class _FakeRepo:
    """最小化 repo mock: 只实现 watchlist_enriched 调用到的方法."""

    def __init__(self, enriched_df, enriched_date, etf_df=None, etf_date=None,
                 instruments_df=None, name_map=None, etf_set=None,
                 index_df=None, index_date=None, index_set=None,
                 history_span=None, hist_df=None, history_raises=False):
        self._enriched = enriched_df
        self._enriched_date = enriched_date
        self._etf = etf_df
        self._etf_date = etf_date
        self._index = index_df
        self._index_date = index_date
        self._instruments = instruments_df or pl.DataFrame()
        self._name_map = name_map or {}
        self._etf_set = etf_set or set()
        self._index_set = index_set or set()
        # 加入后涨跌幅基准价所需的内存历史缓存 (未注入时端点按 null 降级)
        self._history_span = history_span
        self._hist = hist_df if hist_df is not None else pl.DataFrame()
        self._history_raises = history_raises

    def get_enriched_history_span(self):
        return self._history_span

    def get_enriched_range(self, start, end, symbols=None, columns=None):
        if self._history_raises:
            raise RuntimeError("history cache exploded")
        if self._hist.is_empty():
            return self._hist
        # 与真实实现同语义: 按日期区间 + symbol 过滤
        return self._hist.filter(
            (pl.col("date") >= start) & (pl.col("date") <= end)
            & pl.col("symbol").is_in(list(symbols or []))
        )

    def get_enriched_latest(self):
        return self._enriched, self._enriched_date

    def get_enriched_latest_asset(self, asset):
        if asset == "etf":
            etf = self._etf if self._etf is not None else pl.DataFrame()
            return etf, self._etf_date
        if asset == "index":
            idx = self._index if self._index is not None else pl.DataFrame()
            return idx, self._index_date
        return pl.DataFrame(), None

    def get_etf_symbol_set(self):
        return self._etf_set

    def get_index_symbol_set(self):
        return self._index_set

    def get_instruments(self):
        return self._instruments

    def get_name_map(self, symbols):
        return {s: n for s, n in self._name_map.items() if s in (symbols or [])}


def _make_request(repo):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def _enriched_df(symbols_data):
    """symbols_data: [(symbol, close, change_pct, amount), ...]"""
    return pl.DataFrame(
        [{"symbol": s, "close": c, "change_pct": p, "amount": a, "turnover_rate": 1.0}
         for s, c, p, a in symbols_data],
        schema_overrides={
            "close": pl.Float64, "change_pct": pl.Float64,
            "amount": pl.Float64, "turnover_rate": pl.Float64,
        },
    )


def test_watchlist_symbol_not_in_enriched_still_returned(monkeypatch):
    """核心回归: 自选里有但 enriched 缓存里没有的标的, 必须仍返回一行 (指标 null)."""
    # enriched 缓存只覆盖 600519, 不覆盖 999999 (新加的冷门股)
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": "600519"}, {"symbol": "999999"}])
    repo = _FakeRepo(
        enriched_df=_enriched_df([("600519", 1800.0, 1.2, 1e9)]),
        enriched_date="2026-07-08",
        name_map={"600519": "贵州茅台", "999999": "未知股"},
    )

    # ext_columns 显式传 None 绕过 FastAPI Query 默认值
    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    syms = [r["symbol"] for r in res["rows"]]
    assert "600519" in syms, "缓存里有的标的必须返回"
    assert "999999" in syms, "缓存里没有的自选标的也必须返回 (修复的核心)"

    # 缺失标的指标应为 null
    row_999 = next(r for r in res["rows"] if r["symbol"] == "999999")
    assert row_999["close"] is None, f"缺失指标应为 null, 实际: {row_999['close']}"
    assert row_999["name"] == "未知股", "name 走 get_name_map, 应正常返回"

    # 命中标的指标正常
    row_519 = next(r for r in res["rows"] if r["symbol"] == "600519")
    assert row_519["close"] == 1800.0


def test_all_watchlist_missing_from_enriched(monkeypatch):
    """股票 enriched 缓存未就绪时, 自选仍返回占位行."""
    syms = ["000001", "000002"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in syms])
    repo = _FakeRepo(
        enriched_df=pl.DataFrame(schema={"symbol": pl.Utf8}),
        enriched_date=None,
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    assert [r["symbol"] for r in res["rows"]] == syms
    assert all(r.get("close") is None for r in res["rows"])
    assert res["as_of"] is None


def test_partial_coverage_preserves_count(monkeypatch):
    """多只自选, 部分覆盖: 返回行数必须 == 自选股票数."""
    syms = ["600519", "000001", "999888", "888999"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in syms])
    repo = _FakeRepo(
        enriched_df=_enriched_df([
            ("600519", 1800.0, 1.2, 1e9),
            ("000001", 15.0, 0.3, 2e9),
        ]),
        enriched_date="2026-07-08",
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    assert len(res["rows"]) == len(syms), \
        f"返回行数应等于自选数 {len(syms)}, 实际 {len(res['rows'])}"

    returned = {r["symbol"] for r in res["rows"]}
    assert returned == set(syms)


def test_etf_not_in_enriched_still_returned(monkeypatch):
    """ETF 同样: 自选了但 ETF enriched 缓存没有的, 也应返回 (指标 null)."""
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": "510300"}, {"symbol": "599999"}])
    repo = _FakeRepo(
        enriched_df=pl.DataFrame(schema={"symbol": pl.Utf8}),  # 无股票自选
        enriched_date=None,
        etf_df=_enriched_df([("510300", 4.0, 0.5, 1e8)]),
        etf_date="2026-07-08",
        etf_set={"510300", "599999"},
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    syms = [r["symbol"] for r in res["rows"]]
    assert "510300" in syms
    assert "599999" in syms, "ETF enriched 缺失的自选标的也必须返回"

    row_missing = next(r for r in res["rows"] if r["symbol"] == "599999")
    assert row_missing["close"] is None


def test_all_etf_watchlist_missing_from_enriched(monkeypatch):
    """ETF enriched 缓存未就绪时, 自选仍返回占位行."""
    syms = ["510300", "599999"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in syms])
    repo = _FakeRepo(
        enriched_df=pl.DataFrame(schema={"symbol": pl.Utf8}),
        enriched_date=None,
        etf_df=pl.DataFrame(schema={"symbol": pl.Utf8}),
        etf_date=None,
        etf_set=set(syms),
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    assert [r["symbol"] for r in res["rows"]] == syms
    assert all(r.get("close") is None for r in res["rows"])
    assert res["as_of"] is None


def test_mixed_watchlist_keeps_pending_stock_rows(monkeypatch):
    """股票缓存未就绪不应影响 ETF 行, 且保持自选原始顺序."""
    syms = ["510300", "000001", "510500"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in syms])
    repo = _FakeRepo(
        enriched_df=pl.DataFrame(schema={"symbol": pl.Utf8}),
        enriched_date=None,
        etf_df=_enriched_df([("510300", 4.0, 0.5, 1e8), ("510500", 6.0, -0.2, 2e8)]),
        etf_date="2026-07-08",
        etf_set={"510300", "510500"},
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    assert [r["symbol"] for r in res["rows"]] == syms
    assert next(r for r in res["rows"] if r["symbol"] == "000001").get("close") is None
    assert next(r for r in res["rows"] if r["symbol"] == "510300")["close"] == 4.0
    assert res["as_of"] == "2026-07-08"


def test_mixed_watchlist_keeps_pending_etf_rows(monkeypatch):
    """ETF 缓存未就绪不应影响股票行, 且保持自选原始顺序."""
    syms = ["510300", "000001", "510500"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": s} for s in syms])
    repo = _FakeRepo(
        enriched_df=_enriched_df([("000001", 15.0, 0.3, 2e9)]),
        enriched_date="2026-07-08",
        etf_df=pl.DataFrame(schema={"symbol": pl.Utf8}),
        etf_date=None,
        etf_set={"510300", "510500"},
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    assert [r["symbol"] for r in res["rows"]] == syms
    assert next(r for r in res["rows"] if r["symbol"] == "000001")["close"] == 15.0
    assert all(next(r for r in res["rows"] if r["symbol"] == symbol).get("close") is None
               for symbol in ("510300", "510500"))
    assert res["as_of"] == "2026-07-08"


def test_watchlist_enriched_index_branch(monkeypatch):
    """自选含指数: 行走 index enriched, asset_type=index, 名称回填, 股票/ETF 行不受影响。

    断言 (计划 Task 4 Step 1):
      - 指数行存在, close == 3000.0, asset_type == "index", name == "上证指数"
      - 指数行 turnover_rate 为 None (列不存在或 null, 不报错)
      - 股票行 asset_type == "stock"; ETF 行 == "etf"
      - as_of == min(股票日期, etf日期, index日期)
    """
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: [{"symbol": "JPM"}, {"symbol": "VOO"},
                                 {"symbol": "SPX"}])
    repo = _FakeRepo(
        enriched_df=_enriched_df([("JPM", 10.0, 0.3, 2e9)]),
        enriched_date="2026-07-23",
        etf_df=_enriched_df([("VOO", 4.0, 0.5, 1e8)]),
        etf_date="2026-07-24",
        etf_set={"VOO"},
        index_df=pl.DataFrame([{"symbol": "SPX", "close": 3000.0, "change_pct": 0.01,
                                "amount": 1e9, "ma5": 2990.0}]),
        index_date="2026-07-24",
        index_set={"SPX"},
        name_map={"JPM": "浦发银行", "VOO": "沪深300ETF", "SPX": "上证指数"},
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    rows = {r["symbol"]: r for r in res["rows"]}
    # 指数行
    idx = rows["SPX"]
    assert idx["close"] == 3000.0
    assert idx["asset_type"] == "index"
    assert idx["name"] == "上证指数"
    # 指数无换手率: 列缺失或 null, 不报错
    assert idx.get("turnover_rate") is None
    # 股票行 / ETF 行 asset_type
    assert rows["JPM"]["asset_type"] == "stock"
    assert rows["VOO"]["asset_type"] == "etf"
    # as_of == min(三类缓存日期)
    assert res["as_of"] == "2026-07-23"


def _quote_df(rows):
    """rows: [(symbol, close, prev_close, volume, open, high, low), ...]"""
    return pl.DataFrame(
        [{"symbol": s, "close": c, "prev_close": pc, "volume": v,
          "open": o, "high": h, "low": lo, "amount": 1e9, "change_pct": 1.0}
         for s, c, pc, v, o, h, lo in rows],
        schema_overrides={k: pl.Float64 for k in
                          ("close", "prev_close", "volume", "open", "high", "low",
                           "amount", "change_pct")},
    )



def _hist_df(rows):
    """rows: [(symbol, date, close), ...]"""
    return pl.DataFrame(
        [{"symbol": s, "date": d, "close": c} for s, d, c in rows],
        schema={"symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64},
    )


def _entries(*pairs):
    """(symbol, added_at) 对 → list_symbols 返回值"""
    return [{"symbol": s, "added_at": a} for s, a in pairs]


def _span():
    return (date(2026, 9, 1), date(2026, 9, 18))


def test_pct_since_added_baseline_is_added_day_close(monkeypatch):
    """正常路径: 基准 = 加入日收盘 (<= 语义), 结果为小数口径, added_at 原样透传。"""
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: _entries(("MCD", "2026-09-10T03:21:07")))
    repo = _FakeRepo(
        enriched_df=_enriched_df([("MCD", 11.0, 1.0, 1e9)]),
        enriched_date="2026-09-18",
        history_span=_span(),
        hist_df=_hist_df([("MCD", date(2026, 9, 9), 9.0),
                          ("MCD", date(2026, 9, 10), 10.0),
                          ("MCD", date(2026, 9, 11), 10.5)]),
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    row = res["rows"][0]

    # 美股语义 (P1-2): added_at 03:21 UTC = 美东 09-09 23:21 → 基准取 09-09 收盘 9.0
    assert row["pct_since_added"] == pytest.approx(11.0 / 9.0 - 1.0)
    # 小数口径: 存 0.222 而非 22.2 (与 momentum_* 一致, 前端 fmtPct 期望小数)
    assert row["pct_since_added"] < 1, "必须是小数口径, 不能写成百分数"
    # added_at 原样透传 (前端负责 UTC→北京换算)
    assert row["added_at"] == "2026-09-10T03:21:07"
    # 中间量不得泄漏到响应
    assert "base_close" not in row


def test_pct_since_added_snaps_back_to_previous_trading_day(monkeypatch):
    """周末/节假日加入: 基准回落到 <= 加入日的最近交易日 (周五), 而非无值。"""
    # 2026-09-19 是周六 (北京时间), 无 bar
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: _entries(("MCD", "2026-09-19T02:00:00")))
    repo = _FakeRepo(
        enriched_df=_enriched_df([("MCD", 13.2, 1.0, 1e9)]),
        enriched_date="2026-09-18",
        history_span=_span(),
        hist_df=_hist_df([("MCD", date(2026, 9, 17), 10.0),
                          ("MCD", date(2026, 9, 18), 12.0)]),
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    assert res["rows"][0]["pct_since_added"] == pytest.approx(13.2 / 12.0 - 1.0)


def test_pct_since_added_uses_market_date_not_utc(monkeypatch):
    """added_at 是 naive UTC: 美东时间 09-18 16:30 加入的, 基准必须取 09-18 收盘。

    该字符串的 UTC 日期是 09-18; 若按北京时间口径会错取 09-19 (结果 0.1),
    美东正确结果是 1.2。两者可区分, 故本用例能捕捉口径回归 (P1-2)。
    """
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: _entries(("MCD", "2026-09-18T20:30:00")))
    repo = _FakeRepo(
        enriched_df=_enriched_df([("MCD", 22.0, 1.0, 1e9)]),
        enriched_date="2026-09-19",
        history_span=(date(2026, 9, 1), date(2026, 9, 19)),
        hist_df=_hist_df([("MCD", date(2026, 9, 18), 10.0),
                          ("MCD", date(2026, 9, 19), 20.0)]),
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    pct = res["rows"][0]["pct_since_added"]
    assert pct == pytest.approx(22.0 / 10.0 - 1.0), \
        f"基准应取美东日 09-18 的收盘 10.0; 按北京口径会得到 0.1, 实际 {pct}"


@pytest.mark.parametrize("history_span,history_raises,keeps_added_at", [
    (None, False, True),      # 历史缓存不可用 (冷启动/预热中): 只降级涨跌幅
    (_span(), True, False),   # 取数抛异常: 整个装饰块降级
])
def test_pct_since_added_degrades_without_breaking_rows(
    monkeypatch, history_span, history_raises, keeps_added_at,
):
    """装饰列取不到时不得 fail 整个请求: 返回完整 rows, 新列为 null。"""
    syms = ["MCD", "BAC"]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: _entries(("MCD", "2026-09-10T03:00:00"),
                                         ("BAC", "2026-09-11T03:00:00")))
    repo = _FakeRepo(
        enriched_df=_enriched_df([("MCD", 11.0, 1.0, 1e9),
                                  ("BAC", 15.0, 0.3, 2e9)]),
        enriched_date="2026-09-18",
        history_span=history_span,
        history_raises=history_raises,
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)

    assert [r["symbol"] for r in res["rows"]] == syms
    assert all(r["pct_since_added"] is None for r in res["rows"])
    assert all(bool(r["added_at"]) is keeps_added_at for r in res["rows"])
    # 中间量不得残留在响应里
    assert all("base_close" not in r for r in res["rows"])
    # 主表数据完好
    assert res["rows"][0]["close"] == 11.0


def test_pct_since_added_null_when_added_before_history_span(monkeypatch):
    """加入日早于可查询历史范围 (约 300 天) 的标的显式降级, 同表其它标的不受影响。"""
    # 600519: added_at 10:00 UTC = 美东 09-10 06:00 → 基准日 09-10 (美股口径 P1-2)
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: _entries(("MCD", "2026-09-10T10:00:00"),
                                         ("BAC", "2025-01-05T03:00:00")))
    repo = _FakeRepo(
        enriched_df=_enriched_df([("MCD", 11.0, 1.0, 1e9),
                                  ("BAC", 15.0, 0.3, 2e9)]),
        enriched_date="2026-09-18",
        history_span=_span(),       # 只覆盖 2026-09-01 起
        hist_df=_hist_df([("MCD", date(2026, 9, 10), 10.0)]),
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    rows = {r["symbol"]: r for r in res["rows"]}

    assert rows["BAC"]["pct_since_added"] is None, "超窗应降级为 null"
    assert rows["BAC"]["added_at"] == "2025-01-05T03:00:00", "加入日期仍应返回"
    assert rows["MCD"]["pct_since_added"] == pytest.approx(0.1), \
        "一只超窗不得拖垮同表其它标的"


def test_added_at_empty_string_normalized_to_null(monkeypatch):
    """老 schema 迁移补的空串归一为 null, 不抛异常也不渲染成空日期。"""
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: _entries(("MCD", "")))
    repo = _FakeRepo(
        enriched_df=_enriched_df([("MCD", 11.0, 1.0, 1e9)]),
        enriched_date="2026-09-18",
        history_span=_span(),
        hist_df=_hist_df([("MCD", date(2026, 9, 10), 10.0)]),
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    row = res["rows"][0]
    assert row["added_at"] is None
    assert row["pct_since_added"] is None


def test_pct_since_added_null_for_etf_row(monkeypatch):
    """ETF 显式降级: 加入日期正常返回, 加入后涨跌幅为 null。"""
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: _entries(("VOO", "2026-09-10T03:00:00")))
    repo = _FakeRepo(
        enriched_df=pl.DataFrame(schema={"symbol": pl.Utf8}),
        enriched_date=None,
        etf_df=_enriched_df([("VOO", 4.0, 0.5, 1e8)]),
        etf_date="2026-09-18",
        etf_set={"VOO"},
        history_span=_span(),
        hist_df=_hist_df([("VOO", date(2026, 9, 10), 3.8)]),
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    row = res["rows"][0]
    assert row["added_at"] == "2026-09-10T03:00:00"
    assert row["pct_since_added"] is None, "ETF 无历史缓存, 显式降级为 —"


def test_pct_since_added_null_for_invalid_baseline(monkeypatch):
    """基准价非正/NaN 时返回 null, 不得产出 inf 或 NaN 这类会污染前端排序的值。"""
    monkeypatch.setattr(wl_api.watchlist, "list_symbols",
                        lambda: _entries(("MCD", "2026-09-10T03:00:00"),
                                         ("BAC", "2026-09-10T03:00:00")))
    repo = _FakeRepo(
        enriched_df=_enriched_df([("MCD", 11.0, 1.0, 1e9),
                                  ("BAC", 15.0, 0.3, 2e9)]),
        enriched_date="2026-09-18",
        history_span=_span(),
        hist_df=_hist_df([("MCD", date(2026, 9, 10), 0.0),
                          ("BAC", date(2026, 9, 10), float("nan"))]),
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    for row in res["rows"]:
        assert row["pct_since_added"] is None, f"{row['symbol']} 应降级为 null"


def test_empty_watchlist_returns_no_rows(monkeypatch):
    """空自选: 保持既有早退契约, 不因新字段改变响应结构。"""
    monkeypatch.setattr(wl_api.watchlist, "list_symbols", lambda: [])
    repo = _FakeRepo(
        enriched_df=_enriched_df([("MCD", 11.0, 1.0, 1e9)]),
        enriched_date="2026-09-18",
        history_span=_span(),
    )

    res = wl_api.watchlist_enriched(_make_request(repo), ext_columns=None)
    assert res == {"rows": [], "as_of": None, "elapsed_ms": 0}
