"""0.37.0 (2026-10-23 产品决策): 新内置策略「动量RSI2回调」(momentum_rsi2_pullback) + 引擎读模块级 TAKE_PROFIT。

规则 (原文): 过去 60 日涨幅排全池前 20%、收盘在 50 日线上方、RSI(2) < 10, 且 SPY 在 200 日线上方时次日开盘买入;
收盘高于前一日最高价、盈利满 10% 止盈或持满 5 个交易日先到先出。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix
from app.strategy import market_data
from app.strategy.builtin import momentum_rsi2_pullback as mod
from app.strategy.engine import StrategyEngine, _take_profit_const

BUILTIN = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
START = date(2023, 1, 2)


def _bars(n_days: int, *, spy_drift: float = 0.001, pullback: tuple[str, ...] = ("S29",), n_stocks: int = 30,
          with_spy: bool = True, volume: float = 1e6, seed: int = 0) -> pl.DataFrame:
    """n_stocks 只股票, 动量 = 下标越大越强; pullback 里的票最后两天各跌 7% (RSI(2) 打到个位数)。"""
    rng = np.random.default_rng(seed)
    rows = []
    syms = [f"S{i:02d}" for i in range(n_stocks)] + (["SPY"] if with_spy else [])
    for j, sym in enumerate(syms):
        px = 50.0
        for t in range(n_days):
            drift = spy_drift if sym == "SPY" else 0.0005 * j
            px *= 1 + drift + rng.normal(0, 0.002)
            if sym in pullback and t in (n_days - 3, n_days - 2):
                px *= 0.93
            rows.append(dict(symbol=sym, date=START + timedelta(days=t), open=px * 0.999, high=px * 1.004,
                             low=px * 0.996, close=px, volume=volume))
    return pl.DataFrame(rows)


def _entries(panel: pl.DataFrame, params: dict | None = None):
    m = build_market_data_matrix(panel)
    sig = mod.MATRIX_STRATEGY.compute_signals(m, params or {})
    return m, np.asarray(sig.entry, bool), sig


class _EmptyRepo:
    def resolve_asset_type(self, s):
        return "stock"

    def get_daily(self, *a, **k):
        return pl.DataFrame()

    def _scan_daily_symbol(self, *a, **k):
        return pl.DataFrame()

    def get_instruments_asset(self, asset_type):
        return pl.DataFrame()


@pytest.fixture(autouse=True)
def _no_store(monkeypatch, tmp_path):
    """策略里两处只读外部数据 (instruments 元数据、矩阵缺 SPY 时的 SPY 日K) 默认读不到, 各测试按需注入。"""
    mod._REGIME_STORE_CACHE.clear()
    market_data._set_repo(_EmptyRepo())
    yield
    market_data._reset_repo()


# ---------- 加载与元数据 ----------

def test_loads_with_take_profit_hold_and_no_stop():
    e = StrategyEngine(strategy_dirs=[BUILTIN])
    assert e.load_errors() == []
    s = e.get("momentum_rsi2_pullback")
    assert s.execution_backend == "matrix_native" and s.meta["research_only"] is False
    assert s.take_profit == 0.10 and s.max_hold_days == 5 and s.stop_loss is None
    assert s.meta["cross_sectional"] is True
    assert s.entry_signals == ["signal_momentum_rsi2_buy"] and s.exit_signals == ["signal_momentum_rsi2_exit"]
    # 引擎默认流动性门槛 ($5 / $2000 万), 不是 A 股口径
    assert s.basic_filter["price_min"] == 5 and s.basic_filter["amount_min"] == 2e7
    # 预热覆盖 SPY 200 日线
    assert s.matrix_strategy.required_warmup_bars({}) >= 200


@pytest.mark.parametrize("value, expect", [
    (0.10, 0.10), (1, 1.0), (None, None), (True, None), ("0.1", None), (0, None), (-0.1, None),
    (float("nan"), None), (float("inf"), None), (6, None),
])
def test_take_profit_constant_is_validated(value, expect):
    assert _take_profit_const(value) == expect


def test_other_builtins_have_no_take_profit():
    """只有新策略声明了 TAKE_PROFIT; 其余内置策略的回测/证据口径不受这次引擎改动影响。"""
    e = StrategyEngine(strategy_dirs=[BUILTIN])
    with_tp = sorted(m["id"] for m in e.list_strategies(include_research=True) if e.get(m["id"]).take_profit)
    assert with_tp == ["momentum_rsi2_pullback"]


@pytest.mark.full_edition
def test_strategy_detail_exposes_take_profit():
    from app.api.strategy import _strategy_detail
    e = StrategyEngine(strategy_dirs=[BUILTIN])
    s = e.get("momentum_rsi2_pullback")
    assert _strategy_detail(s)["take_profit"] == 0.10
    assert _strategy_detail(s, {"take_profit": 0.2})["take_profit"] == 0.2
    assert _strategy_detail(e.get("ibs_mean_reversion"))["take_profit"] is None


# ---------- 买点 ----------

def test_buys_only_strong_name_after_pullback_in_bull_market():
    m, ent, _ = _entries(_bars(320))
    t = m.shape[0] - 2                                     # 回调第二天收盘
    assert [m.symbols[j] for j in np.flatnonzero(ent[t])] == ["S29"]
    assert not ent[:, m.symbols.index("SPY")].any()        # SPY 本身不参与


def test_weak_name_pullback_is_not_top_20pct():
    _, ent, _ = _entries(_bars(320, pullback=("S02",)))
    assert not ent[-2].any()


def test_spy_below_200d_blocks_entries():
    _, ent, _ = _entries(_bars(320, spy_drift=-0.002))
    assert not ent.any()
    _, ent2, _ = _entries(_bars(320, spy_drift=-0.002), {"require_regime": False})
    assert ent2[-2].any()


def test_missing_spy_and_no_store_is_fail_closed():
    _, ent, _ = _entries(_bars(320, with_spy=False))
    assert not ent.any()


def test_missing_spy_reads_store_via_market_data():
    full = _bars(320)
    spy = full.filter(pl.col("symbol") == "SPY").select(["date", "close"])

    class _Repo(_EmptyRepo):
        calls = 0

        def get_daily(self, *a, **k):                      # 会触发整份指标重算的那条路, 策略不许走
            raise AssertionError("strategy must not call get_daily (heavy enriched refresh)")

        def _scan_daily_symbol(self, symbol, start, end, columns=None):
            _Repo.calls += 1
            return spy.filter((pl.col("date") >= start) & (pl.col("date") <= end)) if symbol == "SPY" else pl.DataFrame()

    market_data._set_repo(_Repo())
    stocks = full.filter(pl.col("symbol") != "SPY")
    m, ent, _ = _entries(stocks)
    assert [m.symbols[j] for j in np.flatnonzero(ent[-2])] == ["S29"]
    _entries(stocks)
    assert _Repo.calls == 1                                # 5 分钟内复用, 不每次读盘


def test_short_history_spy_is_fail_closed():
    """SPY 只有 150 根: 200 日线算不出, 不能当作多头。"""
    _, ent, _ = _entries(_bars(150))
    assert not ent.any()


def test_illiquid_names_are_out_of_the_pool():
    _, ent, _ = _entries(_bars(320, volume=1000.0))       # 日成交额 ~$5 万 << $2000 万
    assert not ent.any()
    _, ent2, _ = _entries(_bars(320, volume=1000.0), {"pool_adv_min": 0})
    assert ent2[-2].any()


def test_etfs_are_out_of_the_pool():
    """instruments 标成 ETF (或 is_etf) 的票不进排名池、不出买点; 维表读不到 = 不剔除。"""
    class _Repo(_EmptyRepo):
        def get_instruments_asset(self, asset_type):
            return pl.DataFrame({"symbol": ["S29", "S28", "S27"], "instrument_type": ["etf", "common_stock", None],
                                 "is_etf": [False, True, None]})

    market_data._set_repo(_Repo())
    assert market_data.non_stock_symbols() == frozenset({"S29", "S28"})
    _, ent, _ = _entries(_bars(320))
    assert not ent.any()


def test_pool_too_small_gives_no_ranking():
    _, ent, _ = _entries(_bars(320, n_stocks=8, pullback=("S07",)))
    assert not ent.any()


def test_ranking_uses_whole_pool_not_the_traded_subset():
    """截面: 同一只票在 30 只的池里是前 20%, 在只含弱票的池里也许不是 —— 回放/对账要在全市场上算。"""
    full = _bars(320, pullback=("S20",))
    _, ent, _ = _entries(full)
    assert not ent[-2].any()                               # S20 在 30 只里不在前 20%
    sub = full.filter(pl.col("symbol").is_in([f"S{i:02d}" for i in range(11)] + ["S20", "SPY"]))
    m2, ent2, _ = _entries(sub)
    assert [m2.symbols[j] for j in np.flatnonzero(ent2[-2])] == ["S20"]   # 在弱池里变成前 20%


def test_signals_are_finite_and_shaped():
    m, ent, sig = _entries(_bars(320))
    assert np.isfinite(np.asarray(sig.score)).all()
    assert np.asarray(sig.score)[ent].min() > 0
    assert sig.entry.shape == m.shape and sig.exit.shape == m.shape


# ---------- 卖点 ----------

def test_exit_is_close_above_previous_high():
    rows = []
    for t, (h, c) in enumerate([(10, 9.5), (10.2, 10.1), (10.5, 10.3), (11, 10.4)]):
        rows.append(dict(symbol="A", date=START + timedelta(days=t), open=c, high=h, low=c - 0.5, close=c, volume=1e6))
    m = build_market_data_matrix(pl.DataFrame(rows))
    ex = np.asarray(mod.MATRIX_STRATEGY.compute_signals(m, {"require_regime": False}).exit, bool)[:, 0]
    assert ex.tolist() == [False, True, True, False]     # 10.1>10, 10.3>10.2, 10.4<10.5


def test_backtest_uses_take_profit_and_hold_from_module(tmp_path):
    """回测服务从策略定义取止盈/持有天数 (回测页不传覆盖时)。"""
    from types import SimpleNamespace

    from app.backtest.strategy import StrategyBacktestService
    e = StrategyEngine(strategy_dirs=[BUILTIN])
    s = e.get("momentum_rsi2_pullback")
    svc = StrategyBacktestService(SimpleNamespace(repo=None), e)
    assert svc._normalize_pct(svc._override_value({}, "take_profit", getattr(s, "take_profit", None)), 0.01, 5.0) == 0.10
    assert svc._override_value({}, "max_hold_days", s.max_hold_days) == 5


# ---------- 对账: 截面策略在全市场算 ----------

@pytest.mark.full_edition
def test_reconcile_replay_runs_cross_sectional_rules_on_full_market(monkeypatch, tmp_path):
    from app.services import strategy_evaluation_runner as runner
    from app.services import swing_settlement as ss
    calls = []
    e = StrategyEngine(strategy_dirs=[BUILTIN])
    day = START + timedelta(days=319)
    panel = _bars(320)
    md = build_market_data_matrix(panel)

    def fake_signal_matrix(sid, svc, engine, start, end, *, symbols=None, warm_days=None, disk_cache=None):
        calls.append((sid, symbols, warm_days))
        sig = engine.get(sid).matrix_strategy.compute_signals(md, {})
        return md, sig, None

    monkeypatch.setattr(runner, "_signal_matrix", fake_signal_matrix)
    out = ss.replay_rule_hits(tmp_path, object(), day - timedelta(days=1), ["S29", "S05"],
                              {"long": ("momentum_rsi2_pullback", "ibs_mean_reversion"), "short": ()}, engine=e)
    by = {c[0]: c for c in calls}
    assert by["momentum_rsi2_pullback"][1] is None and by["momentum_rsi2_pullback"][2] is None
    assert by["ibs_mean_reversion"][1] == ["S05", "S29"] and by["ibs_mean_reversion"][2] == ss.SIGNAL_WARM_DAYS
    assert out["momentum_rsi2_pullback"] == {"S29"}
