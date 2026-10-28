"""Read Alpaca history, replay the minute service in a temporary store, verify real API responses.

No production writes, no orders. Times are deliberately historical replay clocks, not realtime.
Run from backend: .venv/bin/python scripts/verify_minute_chain.py --out <absolute-directory>
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline
from app.market_clock import us_clock
from app.plugins.alpaca.provider import AlpacaProvider
from app.services import kline_sync, minute_refresh, preferences
from app.storage.repository import DataStore, KlineRepository


def summarize(df):
    return [{
        "symbol": g["symbol"][0], "rows": g.height,
        "first": str(g["datetime"][0]), "last": str(g["datetime"][-1]),
        "volume_shares": g["volume"].sum(),
        "vwap_usd": g["amount"].sum() / g["volume"].sum(),
        "last_close_usd": g["close"][-1],
    } for g in df.sort(["symbol", "datetime"]).partition_by("symbol")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    provider = AlpacaProvider()
    symbols = ["AAPL", "MU", "NVDA"]
    report = {"kind": "historical-network-replay", "source": "Alpaca SIP", "days": {}, "service_rounds": [], "api_checks": []}
    for day in [date(2026, 9, 25), date(2026, 9, 28)]:
        print(f"network history {day}", flush=True)
        bars = provider.get_minute(symbols, datetime.combine(day, us_clock.sessions[0].open, tzinfo=us_clock.tz), datetime.combine(day, us_clock.sessions[-1].close, tzinfo=us_clock.tz))
        summary = summarize(bars)
        assert len(summary) == len(symbols)
        assert all(x["rows"] == 390 and x["first"].endswith("09:30:00") and x["last"].endswith("15:59:00") for x in summary)
        bars.write_parquet(out / f"sip-{day}.parquet")
        report["days"][str(day)] = summary

    with tempfile.TemporaryDirectory(prefix="zt-minute-replay-") as tmp:
        data_dir = Path(tmp)
        print("network instruments", flush=True)
        inst = provider.instruments_df().filter(pl.col("symbol").is_in(symbols))
        (data_dir / "instruments").mkdir()
        inst.write_parquet(data_dir / "instruments/instruments.parquet")
        repo = KlineRepository(DataStore(data_dir))
        service = minute_refresh.MinuteRefreshService(repo)
        service._resolve_custom = lambda: (provider, "alpaca")
        state = SimpleNamespace(repo=repo, capabilities=SimpleNamespace(has=lambda c: True))
        app = FastAPI()
        app.include_router(kline.router)
        app.state.repo, app.state.capabilities = state.repo, state.capabilities
        service.set_app_state(state)
        with patch.object(us_clock, "_calendar_provider", None), patch.object(preferences, "get_minute_data_provider", return_value="alpaca"), patch.object(kline_sync, "_resolve_minute_provider", return_value=(provider, False, None)), patch.object(preferences, "get_minute_refresh_enabled", return_value=True):
            for hour, minute, mode in [(12, 0, "full"), (12, 1, "increment"), (16, 18, "increment")]:
                now = datetime(2026, 9, 25, hour, minute, tzinfo=us_clock.tz)
                with patch.object(us_clock, "now", return_value=now):
                    print(f"network service replay {now.isoformat()}", flush=True)
                    service._run_round()
                    s = service.status()
                    assert s["last_error"] is None, s
                    assert s["last_mode"] == mode, s
                    s["replay_as_of"] = now.isoformat()
                    report["service_rounds"].append(s)
            with patch.object(us_clock, "now", return_value=datetime(2026, 9, 25, 16, 20, tzinfo=us_clock.tz)):
                client = TestClient(app)
                for symbol in symbols:
                    print(f"local API replay {symbol}", flush=True)
                    response = client.get("/api/kline/minute", params={"symbol": symbol, "date": "2026-09-25", "live": "true"})
                    response.raise_for_status()
                    body = response.json()
                    assert len(body["rows"]) == 390
                    assert body["rows"][0]["datetime"].endswith("09:30:00")
                    assert body["rows"][-1]["datetime"].endswith("15:59:00")
                    assert body["data_delay_minutes"] == 16
                    report["api_checks"].append({"symbol": symbol, "http": response.status_code, "rows": len(body["rows"]), "source": body["source"], "as_of": body["as_of"], "data_delay_minutes": body["data_delay_minutes"]})
    (out / "live-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Evidence:", out / "live-verification.json")


if __name__ == "__main__":
    main()
