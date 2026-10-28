"""HL pre-market gap alert (approved: ±3% threshold; Beijing 21:00 summer / 22:00 winter = 09:00 ET;
scope = HL-listed positions + active cards + watchlist; one merged message; nothing sent when nothing fires;
never triggers a trade)."""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.market_clock import us_clock
from app.plugins.hyperliquid import provider as hl
from app.services import premarket_gap


def test_hl_symbols_and_mids_parse(monkeypatch):
    calls = []

    def post(body, timeout=20):
        calls.append(body["type"])
        if body["type"] == "meta":
            return {"universe": [{"name": "xyz:NVDA"}, {"name": "xyz:LITE"}, {"name": "xyz:OLD", "isDelisted": True}]}
        return {"xyz:NVDA": "230.5", "xyz:LITE": "1090.0", "xyz:OLD": "1"}

    monkeypatch.setattr(hl, "_post", post)
    p = hl.HyperliquidProvider()
    assert p.listed() == {"NVDA", "LITE"}
    assert p.mids(["NVDA", "LITE", "MISSING"]) == {"NVDA": 230.5, "LITE": 1090.0}


def test_plugin_never_declares_trading_datasets():
    import yaml
    from pathlib import Path
    manifest = yaml.safe_load((Path(hl.__file__).parent / "plugin.yaml").read_text())
    assert manifest["datasets"] == []  # reference only: cannot be selected as realtime/daily/execution source


def test_scan_flags_gaps_beyond_threshold_and_skips_unlisted():
    res = premarket_gap.scan(
        scope={"NVDA": ["自选"], "LITE": ["持仓"], "SSL": ["持仓"], "AAPL": ["自选"]},
        prev_close={"NVDA": 225.0, "LITE": 1050.0, "SSL": 13.9, "AAPL": 330.0},
        mids={"NVDA": 233.0, "LITE": 1015.0, "AAPL": 331.0},
        threshold_pct=3.0,
    )
    # holdings first, then cards, then watchlist
    assert [(r["symbol"], r["direction"]) for r in res["alerts"]] == [("LITE", "down"), ("NVDA", "up")]
    assert res["alerts"][0]["roles"] == ["持仓"] and res["alerts"][0]["gap_pct"] == pytest.approx(-3.333, abs=0.01)
    assert res["not_on_hl"] == ["SSL"] and res["checked"] == 3


def test_message_is_merged_and_says_no_auto_action():
    res = {"alerts": [{"symbol": "LITE", "gap_pct": -3.33, "hl_price": 1015.0, "prev_close": 1050.0, "roles": ["持仓"], "direction": "down"},
                      {"symbol": "NVDA", "gap_pct": 3.56, "hl_price": 233.0, "prev_close": 225.0, "roles": ["自选"], "direction": "up"}],
           "checked": 3, "not_on_hl": ["SSL"]}
    payload = premarket_gap.message(res, date(2026, 10, 2), datetime(2026, 10, 2, 9, 0, tzinfo=us_clock.tz))
    assert payload["title"] == "ZenTrade · 盘前跳空预警"
    body = payload["body"]
    assert body.index("LITE") < body.index("NVDA")  # holdings first
    assert "-3.33%" in body and "+3.56%" in body and "观察，不触发交易" in body
    assert "SSL" in body  # tells which holdings HL cannot see


def test_run_enqueues_once_per_day_and_stays_silent_when_nothing_fires(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(premarket_gap.notify, "_enqueue", lambda d, key, payload: sent.append((key, payload)) or True)
    monkeypatch.setattr(premarket_gap, "_inputs", lambda data_dir, loop: ({"NVDA": ["自选"]}, {"NVDA": 225.0}, {"NVDA": 226.0}))
    out = premarket_gap.run(tmp_path, None, now=datetime(2026, 10, 2, 9, 0, tzinfo=us_clock.tz))
    assert out["alerts"] == [] and sent == []
    monkeypatch.setattr(premarket_gap, "_inputs", lambda data_dir, loop: ({"NVDA": ["自选"]}, {"NVDA": 225.0}, {"NVDA": 240.0}))
    premarket_gap.run(tmp_path, None, now=datetime(2026, 10, 2, 9, 0, tzinfo=us_clock.tz))
    assert [k for k, _ in sent] == ["premarket_gap:2026-10-02"]
    saved = (tmp_path / "premarket_gap" / "2026-10-02.json").read_text()
    assert "NVDA" in saved


def test_schedule_is_0900_et_so_beijing_follows_dst():
    assert premarket_gap.SCHEDULE_ET == (9, 0)
