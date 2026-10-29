"""Phantom 4h Pine 原版仪表盘 (phantom_4h_pine): 与 Pine 原文 (PyneCore 逐行转写) 逐根一致、生产滑动窗与长历史一致、
扫描只用盘后可见根、日线趋势灯只用前一交易日、来源失败降级、不进下单许可。
对照答案: tests/tv_reference/phantom_4h_pine_fixture.json.gz (gen_phantom4h_fixture.py 生成, 真实 4h K 线 2 只 x 2600 根)。
"""
import gzip
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import polars as pl
import pytest

from app.services import decision_desk as dd
from app.services import kline_4h
from app.services import phantom_4h_pine as pp
from app.services import phantom_4h_source as p4

FIX = Path(__file__).parent / "tv_reference" / "phantom_4h_pine_fixture.json.gz"
WARM = 1100      # 跳过振荡器 1000 根分位窗的预热段 (Pine 同样是 na)


def _series():
    with gzip.open(FIX, "rt") as fh:
        d = json.load(fh)
    out = {}
    for sym, v in d["series"].items():
        df = pd.DataFrame({k: v[k] for k in ("open", "high", "low", "close", "volume")},
                          index=pd.DatetimeIndex(pd.to_datetime(v["ts"])))
        out[sym] = (df, v)
    return out


@pytest.mark.parametrize("sym", ["NVDA", "COIN"])
def test_dashboard_lights_and_trail_match_pine_bar_by_bar(sym):
    df, ref = _series()[sym]
    o = pp.dashboard_series(df)
    w = slice(WARM, len(df))
    np.testing.assert_array_equal(o["l1"].to_numpy()[w], (np.array(ref["shift"]) == 1)[w])
    np.testing.assert_array_equal(o["l2"].to_numpy()[w], (np.array(ref["sw_bias"]) == 1)[w])
    np.testing.assert_array_equal(o["l3"].to_numpy()[w], (np.array(ref["osc_pos"]) == 1)[w])
    np.testing.assert_array_equal(o["l4"].to_numpy()[w], (np.array(ref["above_ma"]) == 1)[w])
    np.testing.assert_array_equal(o["trail_dir"].to_numpy()[w], np.array(ref["trl_dir"])[w])
    np.testing.assert_allclose(o["long_trail"].to_numpy()[w], np.array(ref["trl_long"], dtype=float)[w], rtol=1e-4)


def test_vendored_structure_is_not_pine_structure():
    """记录差异 (不是缺陷断言): 简化扫描版内部结构与 Pine 波段结构不是同一个东西 → Pine 版必须独立成源。"""
    from app.vendor.oasis_phantom.phantom_state_machine import leg_pivots, structure_bias
    df, ref = _series()["NVDA"]
    iph, ipl = leg_pivots(df, 5)
    vb, _, _ = structure_bias(df, iph, ipl)
    agree = float(np.mean(vb.to_numpy()[WARM:] == np.array(ref["sw_bias"])[WARM:]))
    assert agree < 0.9


def test_sliding_window_matches_long_history():
    """生产只加载 PINE_HISTORY_DAYS 的 4h 历史: 截止根的分数/进场事件须与全历史计算一致。"""
    df, _ = _series()["COIN"]
    full = pp.dashboard_series(df)
    bars_per_day = 5                                                   # 真实数据: 01:30/05:30/09:30/13:30/17:30
    n = int(p4.PINE_HISTORY_DAYS * 5 / 7 * bars_per_day * 0.9)        # 留 10% 余量 (节假日/缺根)
    assert n > pp.MIN_BARS
    for end in range(len(df) - 400, len(df), 37):
        sub = pp.dashboard_series(df.iloc[max(0, end - n):end + 1])
        assert sub["score"].iloc[-1] == full["score"].iloc[end]
        assert bool(sub["enter"].iloc[-1]) == bool(full["enter"].iloc[end])


def test_daily_light_uses_previous_session_only():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-07 09:30"), pd.Timestamp("2026-10-08 09:30"), pd.Timestamp("2026-10-08 13:30")])
    df = pd.DataFrame({"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1.0}, index=idx)
    ok = pd.Series([False, True, False], index=pd.to_datetime(["2026-10-06", "2026-10-07", "2026-10-08"]).date)
    o = pp.dashboard_series(df, ok)
    # 10-07 的根看 10-06 收盘后 (False); 10-08 的根看 10-07 收盘后 (True), 不看 10-08 当日 (False)
    assert o["l5"].tolist() == [False, True, True]


def test_scan_window_and_signal_fields(monkeypatch):
    df, _ = _series()["NVDA"]
    full = pp.dashboard_series(df)
    days = sorted({t.date() for t in df.index})
    ent_days = sorted({t.date() for t in full.index[full["enter"]]})
    assert ent_days, "真实序列里应出现“积极做多”"
    day = ent_days[len(ent_days) // 2]
    prev = days[days.index(day) - 1]
    sig, short = p4.pine_signals({"NVDA": df[df.index <= pd.Timestamp(f"{day} 09:30")]}, day, prev, {})
    lo = datetime.combine(prev, datetime.min.time()).replace(hour=13, minute=30)
    hi = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
    w = full[(full.index >= lo) & (full.index <= hi) & full["enter"]]
    if w.empty:
        assert sig == []
    else:
        assert sig[0]["bar"] == w.index[-1].isoformat() and pd.Timestamp(sig[0]["bar"]) <= hi
        assert sig[0]["lights"] >= 4
        assert sig[0]["stop_loss"] is None or -1 < sig[0]["stop_loss"] < 0
    assert short == 0


def test_pine_failure_does_not_break_phantom_4h(monkeypatch, tmp_path):
    frames = {f"P{i:03d}": pl.DataFrame({"date": [date(2026, 10, 2)], "close": [1.0], "volume": [1.0]}) for i in range(3)}
    monkeypatch.setattr(p4, "pool", lambda repo, as_of, fr: ["P000", "P001"])
    monkeypatch.setattr(kline_4h, "trading_days", lambda *a: [date(2026, 10, 1), date(2026, 10, 2)])
    monkeypatch.setattr(kline_4h, "bars_4h", lambda *a, **k: {})
    monkeypatch.setattr(p4, "signals_by_side", lambda *a: {"long": [{"symbol": "P000", "score": 80.0, "stop_loss": -0.04}],
                                                          "short": []})
    monkeypatch.setattr(p4, "pine_signals", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    res = p4.run(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), None, date(2026, 10, 2), frames)
    assert res["picks"] == ["P000"] and "pine_picks" not in res and "boom" in res["meta"]["pine"]["error"]


def test_pine_source_is_in_rule_catalog_but_not_tradable_without_evidence(tmp_path):
    """2026-10-09 产品决策: Pine 版上线到策略与机会卡。进规则目录 (可勾选/可看), 默认机会卡显示;
    没有证据 → 权重 0; 下单仍只认规则集 + 前向确认。"""
    w = dd.source_weight(None)
    assert w["weight"] == 0.0 and w["risk_pct"] == 0.0
    from app.services import watchlist_table as wt
    from app.services import watchlist_v4 as wv
    assert p4.PINE_SOURCE_ID in {r["id"] for r in wt.rule_catalog(None)}
    assert p4.PINE_SOURCE_ID in wv.display_rules(tmp_path)["long"]
    assert wv.permission(tmp_path)["tradable"] == set()
