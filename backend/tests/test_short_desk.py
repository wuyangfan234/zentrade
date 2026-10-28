"""做空接入决策台 / 来源 / 镜像 (2026-10-03)。"""
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from app.opportunity.engine import ScanResult
from app.services import alpaca_mirror, decision_scan, phantom_4h, phantom_4h_source, shortable
from app.services import decision_desk as dd


def _daily(sym, n=80, up=True):
    d = pl.date_range(date(2026, 5, 1), date(2026, 10, 2), eager=True)[-n:]
    c = np.linspace(80, 100, n) if up else np.linspace(120, 100, n)
    return pl.DataFrame({"symbol": sym, "date": d, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6})


_PARAMS = {"revision": 1, "portfolio": {"daily_new_cards_max": 5, "max_portfolio_risk_pct": 6, "sector_max_active": 2},
           "market_gate": {"caution_card_max": 2, "caution_risk_pct": 3}}


def _build(picks, tmp_path, shortable_fn=None, enabled=("phantom_4h", "phantom_4h_short")):
    reg = dd.SourceRegistry(tmp_path)
    for s in enabled:
        reg.set_status(s, "enabled")
    ev = {"sources": {s: {"n": 0} for s in enabled}}  # 无证据 → 探索额度 (可交易)
    frames = {s: _daily(s, up=(s != "SHT")) for s in {x for v in picks.values() for x in v}}
    s1s2 = ScanResult(as_of=date(2026, 10, 2), gate="green", params_revision=1, scanned=len(frames), cards=[], watch=[], rejected=[])
    return decision_scan.build(as_of=date(2026, 10, 2), strategy_picks=picks, s1s2=s1s2, frames=frames, evaluation=ev,
                               registry=reg, watchlist=set(), stop_losses={}, sector_map={s: f"S{s}" for s in frames},
                               portfolio_risk_used_pct=0.0, active_symbols=set(), params=_PARAMS, shortable=shortable_fn)


def test_short_source_builds_short_card(tmp_path):
    out = _build({"phantom_4h_short": ["SHT"]}, tmp_path, shortable_fn=lambda s: True)
    cards = out.result.cards + out.result.watch
    assert len(cards) == 1 and cards[0].side == "short" and cards[0].invalidation > cards[0].entry_zone[1]
    assert cards[0].id.endswith(cards[0].id.split("-")[-1]) and "short" in cards[0].id


def test_not_shortable_goes_to_watch(tmp_path):
    out = _build({"phantom_4h_short": ["SHT"]}, tmp_path, shortable_fn=lambda s: False)
    assert not out.result.cards and "不可借券" in out.result.watch[0].state_reason


def test_long_and_short_sources_on_same_symbol_conflict(tmp_path):
    out = _build({"phantom_4h": ["SHT"], "phantom_4h_short": ["SHT"]}, tmp_path, shortable_fn=lambda s: True)
    assert not out.result.cards and "多空来源冲突" in out.result.watch[0].state_reason


def test_shortable_checker_caches_and_fails_closed(tmp_path):
    calls = []

    def q(sym):
        calls.append(sym)
        if sym == "ERR":
            raise RuntimeError("down")
        return {"shortable": sym != "HTB", "easy_to_borrow": True}
    ok = shortable.checker(tmp_path, query=q)
    assert ok("AAA") is True and ok("AAA") is True and calls.count("AAA") == 1
    assert ok("HTB") is False and ok("ERR") is False
    assert shortable.checker(tmp_path, query=lambda s: (_ for _ in ()).throw(AssertionError()))("AAA") is True  # 当日缓存


def test_phantom_signals_by_side_uses_short_trail():
    rng = np.random.default_rng(2)
    n = 400
    idx = pd.date_range("2026-01-05 09:30", periods=n, freq="4h")
    c = 100 * np.exp(np.cumsum(np.where(np.arange(n) % 160 < 80, -0.004, 0.002) + rng.normal(0, 0.008, n)))
    o = c * (1 + rng.normal(0, 0.003, n))
    df = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.004, "low": np.minimum(o, c) * 0.996, "close": c, "volume": 1e5}, index=idx)
    s = phantom_4h.scan_series(df)
    short_bars = s.index[s["action"] == "空头条件齐备"]
    assert len(short_bars) > 0
    ts = short_bars[len(short_bars) // 2]
    day = ts.date() if (ts.hour, ts.minute) < (13, 30) else (ts + pd.Timedelta(days=1)).date()
    prev = sorted({t.date() for t in s.index if t.date() < day})[-1]
    sig = phantom_4h_source.signals_by_side({"AAA": df}, day, prev)["short"]
    assert sig and sig[0]["side"] == "short"
    if sig[0]["stop_loss"] is not None:
        assert sig[0]["stop_loss"] > 0 and sig[0]["chandelier"] > sig[0]["close_4h"]


def test_mirror_maps_short_open_to_sell_and_cover_to_buy(tmp_path, monkeypatch):
    m = alpaca_mirror.AlpacaMirror(tmp_path, http=lambda *a, **k: {})
    m._state["started_seq"] = 0
    events = [
        {"seq": 1, "kind": "fill", "symbol": "XYZ", "side": "short", "qty": 10, "price": 100.0, "at": "2026-10-05T10:00:00-04:00"},
        {"seq": 2, "kind": "partial_close", "symbol": "XYZ", "position_side": "short", "qty": 5, "price": 95.0, "at": "2026-10-05T11:00:00-04:00"},
        {"seq": 3, "kind": "fill", "symbol": "AAA", "side": "buy", "qty": 3, "price": 50.0, "at": "2026-10-05T10:00:00-04:00"},
        {"seq": 4, "kind": "close", "symbol": "AAA", "qty": 3, "price": 51.0, "at": "2026-10-05T12:00:00-04:00"},
    ]
    m.sync(events)
    o = {x["seq"]: (x["side"], x["intent"]) for x in m.orders()}
    assert o == {1: ("sell", "open_short"), 2: ("buy", "cover"), 3: ("buy", "open_long"), 4: ("sell", "close_long")}
    # 已成交的卖空 → 镜像持仓为负; 回补不超过镜像空头
    m._state["orders"]["zt-1"].update(status="filled", filled_qty=10)
    assert m.positions() == {"XYZ": -10.0}
