"""Phantom 4h 来源接入决策台: 逐股止损进卡、扫描时刻可见的 4h 根窗口、数据层合成口径、失败降级。"""
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import polars as pl

from app.services import decision_desk as dd
from app.services import decision_scan, kline_4h, phantom_4h, phantom_4h_source
from app.opportunity.engine import ScanResult


def _daily(sym="AAA", n=80, last=100.0):
    d = pl.date_range(date(2026, 5, 1), date(2026, 10, 2), eager=True)[-n:]
    c = np.linspace(last * 0.8, last, n)
    return pl.DataFrame({"symbol": sym, "date": d, "open": c, "high": c * 1.02, "low": c * 0.98, "close": c, "volume": 1e6})


def test_per_symbol_stop_overrides_strategy_stop():
    frames = {"AAA": _daily("AAA"), "BBB": _daily("BBB")}
    s1s2 = ScanResult(as_of=date(2026, 10, 2), gate="green", params_revision=1, scanned=2, cards=[], watch=[], rejected=[])
    params = {"revision": 1, "portfolio": {"daily_new_cards_max": 5, "max_portfolio_risk_pct": 6, "sector_max_active": 2},
              "market_gate": {"caution_card_max": 2, "caution_risk_pct": 3}}
    out = decision_scan.build(as_of=date(2026, 10, 2), strategy_picks={"phantom_4h": ["AAA", "BBB"]}, s1s2=s1s2, frames=frames,
                              evaluation=None, registry=dd.SourceRegistry(__import__("pathlib").Path("/tmp/_p4h_reg_test")),
                              watchlist=set(), stop_losses={}, sector_map={"AAA": "Tech", "BBB": "Tech"},
                              portfolio_risk_used_pct=0.0, active_symbols=set(), params=params,
                              stop_losses_by_symbol={"phantom_4h": {"AAA": -0.03, "BBB": None}})
    cards = {c.symbol: c for c in out.result.cards + out.result.watch}
    a, b = cards["AAA"], cards["BBB"]
    assert a.evidence["stop_loss"] == -0.03 and b.evidence["stop_loss"] is None
    # AAA: 吊灯止损 3% 比 2ATR 紧 → 失效位更高
    assert a.invalidation > b.invalidation


def _bars_4h(seed=1, n=400):
    rng = np.random.default_rng(seed)
    idx = []
    d = pd.Timestamp("2026-03-02")
    while len(idx) < n:
        if d.dayofweek < 5:
            for hm in ("01:30", "05:30", "09:30", "13:30", "17:30", "21:30"):
                idx.append(pd.Timestamp(f"{d.date()} {hm}"))
        d += pd.Timedelta(days=1)
    idx = idx[:n]
    c = 50 * np.exp(np.cumsum(np.where(np.arange(n) % 150 < 90, 0.003, -0.002) + rng.normal(0, 0.008, n)))
    o = c * (1 + rng.normal(0, 0.003, n))
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.004, "low": np.minimum(o, c) * 0.996, "close": c, "volume": 1e5},
                        index=pd.DatetimeIndex(idx))


def test_signals_only_use_bars_closed_by_post_close_scan():
    df = _bars_4h()
    s = phantom_4h.scan_series(df)
    ready_days = sorted({ts.date() for ts in s.index[s["action"] == "多头条件齐备"]})
    assert ready_days, "合成数据里应有“多头条件齐备”"
    day = ready_days[len(ready_days) // 2]
    days = sorted({ts.date() for ts in df.index})
    prev = days[days.index(day) - 1]
    sig = phantom_4h_source.signals_for_day({"AAA": df}, day, prev)
    lo, hi = datetime.combine(prev, datetime.min.time()).replace(hour=13, minute=30), datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
    w = s[(s.index >= lo) & (s.index <= hi) & (s["action"] == "多头条件齐备")]
    if w.empty:
        assert sig == []
    else:
        assert sig[0]["bar"] == w.index[-1].isoformat()
        assert pd.Timestamp(sig[0]["bar"]) <= hi  # 当日 13:30 根 (收于 17:30) 不可见
        if sig[0]["stop_loss"] is not None:
            assert -1 < sig[0]["stop_loss"] < 0


def test_bars_4h_wall_clock_bins_and_adjustment(tmp_path):
    # 两个交易日 (含夏令时切换后的 11-03) 的 30 分钟线 → 4h 分箱恒为 09:30/13:30 (+ 盘前 05:30/01:30, 盘后 17:30)
    rows = []
    for day, utc_off in (("2025-10-31", 4), ("2025-11-03", 5)):
        for k in range(32):  # 04:00 .. 19:30 ET
            et = pd.Timestamp(f"{day} 04:00") + pd.Timedelta(minutes=30 * k)
            t = (et + pd.Timedelta(hours=utc_off)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append(("AAA", t, 10.0, 10.5, 9.5, 10.0, 100.0))
    src = pl.DataFrame(rows, schema=["symbol", "t", "open", "high", "low", "close", "volume"], orient="row")
    kline_4h.write_month(tmp_path, "2025-10", src.filter(pl.col("t").str.slice(0, 7) == "2025-10"))
    kline_4h.write_month(tmp_path, "2025-11", src.filter(pl.col("t").str.slice(0, 7) == "2025-11"))
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: pl.DataFrame(
        {"symbol": ["AAA", "AAA"], "date": [date(2025, 10, 31), date(2025, 11, 3)], "close": [5.0, 5.0], "raw_close": [10.0, 10.0]}))
    b = kline_4h.bars_4h(tmp_path, repo, ["AAA"], date(2025, 10, 1), date(2025, 11, 30))["AAA"]
    bins = sorted({t.strftime("%H:%M") for t in b.index})
    assert bins == ["01:30", "05:30", "09:30", "13:30", "17:30"]
    assert b["close"].iloc[0] == 5.0 and b["volume"].iloc[0] > 0  # 复权系数 0.5 已乘上


def test_source_failure_degrades(monkeypatch, tmp_path):
    from app.services import decision_scan_job
    monkeypatch.setattr(phantom_4h_source, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    picks, meta = {"other": ["X"]}, {}
    stops = decision_scan_job._phantom_4h_picks(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), date(2026, 10, 2), {}, picks, meta)
    assert stops == {} and picks == {"other": ["X"]} and "boom" in meta["phantom_4h"]["error"]


def test_merge_int_and_float_partitions(tmp_path):
    old = pl.DataFrame({"symbol": ["A"], "t": ["2026-09-01T13:30:00Z"], "open": [10], "high": [11], "low": [9], "close": [10],
                        "volume": [100]})  # 回填文件里常见的整数列
    p = kline_4h._month_path(tmp_path, "2026-09")
    p.parent.mkdir(parents=True)
    old.write_parquet(p)
    new = pl.DataFrame({"symbol": ["A", "A"], "t": ["2026-09-01T13:30:00Z", "2026-09-02T13:30:00Z"], "open": [10.5, 11.0],
                        "high": [11.5, 12.0], "low": [9.5, 10.0], "close": [10.5, 11.0], "volume": [150.0, 200.0],
                        "day": ["2026-09-01", "2026-09-02"]})
    assert kline_4h._merge_into_months(tmp_path, new) == 2
    got = kline_4h.load_m30(tmp_path, ["A"], date(2026, 9, 1), date(2026, 9, 30))
    assert got.height == 2 and got["close"].to_list() == [10.5, 11.0]  # 同一时刻以新数据为准
    assert got.schema["volume"] == pl.Float64


def test_watchlist_include_extras_never_enter_pool_top10_but_get_hits_and_stops(monkeypatch, tmp_path):
    """自选独立选股 (2026-10-05): include 额外计算的自选不参与全池前 10 竞争, 但要有命中名次与吊灯止损。"""
    frames = {f"P{i:03d}": _daily(f"P{i:03d}") for i in range(205)}
    frames["WL"] = _daily("WL")
    monkeypatch.setattr(phantom_4h_source, "pool", lambda repo, as_of, fr: [f"P{i:03d}" for i in range(200)])
    monkeypatch.setattr(kline_4h, "trading_days", lambda *a: [date(2026, 10, 1), date(2026, 10, 2)])
    monkeypatch.setattr(kline_4h, "bars_4h", lambda *a, **k: {})
    sig = {"long": [{"symbol": "WL", "score": 99.0, "stop_loss": -0.04}, {"symbol": "P150", "score": 50.0, "stop_loss": -0.03}]
                   + [{"symbol": f"P{i:03d}", "score": 10.0 - i * 0.01, "stop_loss": -0.05} for i in range(12)],
           "short": []}
    monkeypatch.setattr(phantom_4h_source, "signals_by_side", lambda *a: sig)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    res = phantom_4h_source.run(repo, None, date(2026, 10, 2), frames, include={"WL", "P011"})
    assert "WL" not in res["picks"] and res["picks"][0] == "P150" and len(res["picks"]) == 10
    assert res["stops"]["WL"] == -0.04            # 池外自选: 有止损
    assert res["stops"]["P011"] == -0.05          # 池内但排第 10 名之后的自选: 也有止损
    assert res["hits"][0] == {"symbol": "WL", "score": 99.0, "rank": 1}
    assert res["meta"]["extra"] == 1
