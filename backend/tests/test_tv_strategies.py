"""TradingView 开源指标移植的逐根对照 (2026-10-09)。

答案来自 tests/tv_reference/*.py —— 由 Pine 源码逐行转写、用 PyneCore (对拍 TradingView 的 Pine 运行时) 逐根执行,
见 tests/tv_reference/README.md。这里把 app/backtest/tv_kernels.py 的内核在同样的序列上跑一遍, 要求每一根的买/卖完全相同。
另测: 引擎加载 / research_only 门禁 / 生产滑动窗口截断与全历史一致 / 停牌缺口不跨越。
"""
from __future__ import annotations

import gzip
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest import tv_kernels as tk

HERE = Path(__file__).parent
FIX = HERE / "tv_reference" / "fixtures.json.gz"
BUILTIN = Path(__file__).parents[1] / "app" / "strategy" / "builtin"
TV_IDS = ("tv_supertrend", "tv_ut_bot", "tv_chandelier_exit", "tv_halftrend", "tv_zlsma_cross", "tv_squeeze_momentum",
          "tv_wavetrend", "tv_smc_structure", "tv_phantom_shift", "tv_phantom_trail")


def _fixtures():
    with gzip.open(FIX, "rt") as f:
        return json.load(f)


def _kernel(name: str, inputs: dict, s: dict):
    h, lo, c, v = (np.asarray(s[k], dtype=np.float64) for k in ("high", "low", "close", "volume"))
    base = name.split("@")[0]
    if base in ("tv_supertrend", "tv_phantom_shift"):
        return tk.supertrend_1d(h, lo, c, int(inputs["Periods"]), float(inputs["Multiplier"]))
    if base == "tv_ut_bot":
        return tk.ut_bot_1d(c, float(inputs["a"]), int(inputs["c"]), h, lo)
    if base == "tv_chandelier_exit":
        return tk.chandelier_exit_1d(h, lo, c, v, int(inputs["length"]), float(inputs["mult"]), True, False, 1.2,
                                     False, 200, False, 20)
    if base == "tv_zlsma_cross":
        return tk.zlsma_1d(c, int(inputs["length"]), int(inputs["offset"]))
    if base == "tv_halftrend":
        return tk.halftrend_1d(h, lo, c, int(inputs["amplitude"]), int(inputs["channelDeviation"]), True)
    if base == "tv_squeeze_momentum":
        return tk.squeeze_momentum_1d(h, lo, c, int(inputs["length"]), float(inputs["mult"]), int(inputs["lengthKC"]),
                                      float(inputs["multKC"]), True)
    if base == "tv_wavetrend":
        return tk.wavetrend_1d(h, lo, c, int(inputs["n1"]), int(inputs["n2"]), int(inputs["s1"]), -56.5, 56.5)
    if base == "tv_smc_structure":
        return tk.smc_swing_1d(h, lo, c, int(inputs["size"]), bool(inputs["chochOnly"]))
    if base == "tv_phantom_trail":
        return tk.phantom_trail_1d(h, lo, c, int(inputs["trlLen"]), float(inputs["trlMult"]))
    raise KeyError(name)


def test_every_kernel_matches_pine_reference_bar_by_bar():
    fx = _fixtures()
    assert fx["results"], "fixtures 为空"
    seen, fired = set(), {}
    for key, res in fx["results"].items():
        name, series = key.split("|", 1)
        b, s = _kernel(name, res["inputs"], fx["series"][series])
        rb, rs = np.asarray(res["buy"], bool), np.asarray(res["sell"], bool)
        bad_b = np.flatnonzero(b != rb)
        bad_s = np.flatnonzero(s != rs)
        assert bad_b.size == 0, f"{key}: buy 差 {bad_b[:10].tolist()} (共 {bad_b.size})"
        assert bad_s.size == 0, f"{key}: sell 差 {bad_s[:10].tolist()} (共 {bad_s.size})"
        seen.add(name.split("@")[0])
        fired[name] = fired.get(name, 0) + int(rb.sum()) + int(rs.sum())
    assert seen == set(TV_IDS) - {"tv_phantom_shift"} | {"tv_phantom_shift"}
    assert all(v > 0 for v in fired.values()), f"有参照从未触发 (对照无效): {fired}"


def test_halftrend_production_gate_only_differs_in_atr100_warmup():
    """生产用 atr_gate=False (窗口只有约 75 根, ATR(100) 算不出); 除前 99 根外与原版逐根相同。"""
    fx = _fixtures()
    for key, res in fx["results"].items():
        if not key.startswith("tv_halftrend|"):
            continue
        s = fx["series"][key.split("|", 1)[1]]
        h, lo, c = (np.asarray(s[k], dtype=np.float64) for k in ("high", "low", "close"))
        b, x = tk.halftrend_1d(h, lo, c, 2, 2, False)
        assert (b[100:] == np.asarray(res["buy"], bool)[100:]).all()
        assert (x[100:] == np.asarray(res["sell"], bool)[100:]).all()


def _engine():
    from app.strategy.engine import StrategyEngine
    return StrategyEngine(strategy_dirs=[BUILTIN])


def test_tv_strategies_are_public_matrix_strategies():
    """2026-10-09 产品需求: “把这些策略给我上线系统, 不要停留在研究态”。公开 = 进策略页/选股/回测/决策台候选/规则目录;
    是否下单另由规则集勾选 + 前向确认决定 (见 test_tv_live_cannot_trade_without_ruleset)。"""
    e = _engine()
    assert e.load_errors() == []
    for sid in TV_IDS:
        s = e.get(sid)
        assert s.execution_backend == "matrix_native"
        assert not s.meta.get("research_only"), f"{sid} 已上线, 不得回到研究态"
        assert s.matrix_strategy.required_warmup_bars({}) <= 74, "不得放大生产共享历史窗口"
    public = {m["id"] for m in e.list_strategies()}
    assert set(TV_IDS) <= public


def test_tke_stays_on_the_hourly_view_and_out_of_the_daily_book(tmp_path):
    """TKE 只在策略页按 1 小时查看。日线扫描、默认机会卡、可下单集合都不能收它。"""
    from app.services import watchlist_table as wt
    from app.services import watchlist_v4 as wv

    e = _engine()
    s = e.get("tv_tke")
    assert s.meta["timeframes"] == ["1h"]
    assert s.execution_backend == "matrix_native"
    daily = [m["id"] for m in e.list_strategies()
             if not m.get("research_only") and "1d" in m.get("timeframes", ["1d"])]
    assert "tv_tke" not in daily
    assert "tv_tke" not in wv.TV_RULES
    assert "tv_tke" not in wv.display_rules(tmp_path)["long"]
    assert "tv_tke" not in wv.display_rules(tmp_path)["short"]
    assert "tv_tke" not in {row["id"] for row in wt.rule_catalog(e) if row.get("timeframe") == "1d"}
    assert wv.permission(tmp_path)["tradable"] == set()


def test_tv_live_cannot_trade_without_ruleset(tmp_path):
    """上线 ≠ 下单: 规则目录里有它们、机会卡默认显示它们; 但规则集 (R{n}) 没勾选、前向未过 → 可下单集合为空。"""
    from app.services import watchlist_table as wt
    from app.services import watchlist_v4 as wv
    catalog = {r["id"] for r in wt.rule_catalog(_engine())}
    assert set(TV_IDS) <= catalog
    disp = wv.display_rules(tmp_path)
    assert set(TV_IDS) <= disp["long"]
    assert "tv_squeeze_momentum" not in disp["short"]          # 挤压的“卖”是动能转弱, 默认不出看空
    assert (set(TV_IDS) - {"tv_squeeze_momentum"}) <= disp["short"]
    assert wv.permission(tmp_path)["tradable"] == set()
    saved = wv.save_display(tmp_path, ["tv_supertrend"], ["tv_supertrend", "macd_golden"])
    assert saved["short"] == ["tv_supertrend"]                  # 内置策略卖出 ≠ 做空: 存不进看空显示
    assert wv.display_rules(tmp_path)["long"] == {"tv_supertrend"}


def _panel(n=400, symbols=8, gap=None, seed=3):
    rng = np.random.default_rng(seed)
    rows = []
    d0 = date(2023, 1, 2)
    for a in range(symbols):
        c = 40 * np.exp(np.cumsum(rng.normal(0.0004, 0.025, n)))
        o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.006, n))
        h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.012, n)))
        lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.012, n)))
        for t in range(n):
            if gap and a == gap[0] and gap[1] <= t < gap[2]:
                continue
            rows.append((f"S{a}", d0 + timedelta(days=t), o[t], h[t], lo[t], c[t], 1e6))
    return pl.DataFrame(rows, schema=["symbol", "date", "open", "high", "low", "close", "volume"], orient="row")


@pytest.mark.parametrize("sid", TV_IDS)
def test_matrix_strategy_equals_per_symbol_kernel_and_skips_gaps(sid):
    """矩阵版 = 每只股票“有效 K 线”单独跑内核; 停牌缺口不当作 K 线 (与 TradingView 图表一致)。"""
    from app.backtest.matrix import build_market_data_matrix
    e = _engine()
    s = e.get(sid)
    panel = _panel(gap=(2, 150, 160))
    m = build_market_data_matrix(panel)
    sig = s.matrix_strategy.compute_signals(m, e.resolve_params(s))
    a = m.symbols.index("S2")
    rows = np.flatnonzero(np.isfinite(m.close[:, a]))
    assert rows.size == 390
    sub = panel.filter(pl.col("symbol") == "S2").sort("date")
    one = build_market_data_matrix(sub)
    sig1 = s.matrix_strategy.compute_signals(one, e.resolve_params(s))
    assert (sig.entry[rows, a] == sig1.entry[:, 0]).all()
    assert (sig.exit[rows, a] == sig1.exit[:, 0]).all()
    assert not sig.entry[np.flatnonzero(~np.isfinite(m.close[:, a])), a].any()


def _tail_agreement(sid, window):
    """全历史计算 vs 只给最近 window 根 (生产 run_all 共享窗口), 最后一天信号是否相同。"""
    from app.backtest.matrix import build_market_data_matrix
    e = _engine()
    s = e.get(sid)
    p = e.resolve_params(s)
    panel = _panel(n=600, symbols=40, seed=11)
    days = sorted(panel["date"].unique().to_list())
    agree = total = 0
    for end in days[-60:]:
        full = panel.filter(pl.col("date") <= end)
        cut = full.filter(pl.col("date") >= days[days.index(end) - window + 1])
        a = s.matrix_strategy.compute_signals(build_market_data_matrix(full), p).entry[-1]
        b = s.matrix_strategy.compute_signals(build_market_data_matrix(cut), p).entry[-1]
        agree += int((a == b).sum())
        total += a.size
    return agree / total


@pytest.mark.parametrize("sid", ["tv_supertrend", "tv_ut_bot", "tv_chandelier_exit", "tv_halftrend", "tv_zlsma_cross",
                                 "tv_squeeze_momentum", "tv_phantom_shift", "tv_phantom_trail"])
def test_production_window_matches_full_history(sid):
    """递推型指标 (ATR/EMA 种子、棘轮、趋势状态) 在 75 根窗口里要收敛到与全历史相同的当日信号。
    阈值按实测定 (见提交说明); 不收敛的策略 (WaveTrend/SMC) 单独测更宽的窗口。"""
    assert _tail_agreement(sid, 75) >= 0.995


def test_smc_production_window_recalls_full_history_hits():
    """SMC 在盘后扫描的自选长窗口 (decision_scan_job.LONG_MEMORY_BARS) 下, 对全历史命中的召回 >= 0.9
    (共享 212 根窗口约 0.6, 75 根约 0.03)。
    全格一致率对稀疏信号是空守卫 (全 False 也有 0.998), 这里按命中计。"""
    from app.backtest.matrix import build_market_data_matrix
    e = _engine()
    s = e.get("tv_smc_structure")
    p = e.resolve_params(s)
    from app.services.decision_scan_job import LONG_MEMORY_BARS
    window = LONG_MEMORY_BARS["tv_smc_structure"]
    panel = _panel(n=window + 160, symbols=48, seed=7)
    days = sorted(panel["date"].unique().to_list())
    full = s.matrix_strategy.compute_signals(build_market_data_matrix(panel), p)
    hit = got = 0
    for k in range(window, len(days)):
        a = full.entry[k].astype(bool) | full.exit[k].astype(bool)
        if not a.any():
            continue
        cut = panel.filter((pl.col("date") <= days[k]) & (pl.col("date") >= days[k - window + 1]))
        sg = s.matrix_strategy.compute_signals(build_market_data_matrix(cut), p)
        b = sg.entry[-1].astype(bool) | sg.exit[-1].astype(bool)
        hit += int(a.sum())
        got += int((a & b).sum())
    assert hit >= 8
    assert got / hit >= 0.9


@pytest.mark.parametrize("sid,window", [("tv_wavetrend", 75), ("tv_smc_structure", 75)])
def test_long_memory_strategies_window_agreement_recorded(sid, window):
    """WaveTrend (三层 EMA) 与 SMC (结构状态记忆) 截断后一致率较低: 记录实测值, 防止静默退化。"""
    assert _tail_agreement(sid, window) >= 0.97
