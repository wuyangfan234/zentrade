"""Weekly mining universe: bounded themed pool (user 2026-10-01: cap 200, themes, all watchlist names)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import mining_pool, mining_schedule, preferences
from app.services.mining_jobs import MiningRunStore

LATEST = date(2026, 8, 14)


def _trading_days(n: int) -> list[date]:
    days, d = [], LATEST
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def _write_enriched(root: Path, amounts: dict[str, float], *, days: int = 70, short: dict[str, int] | None = None,
                    closes: dict[str, float] | None = None) -> None:
    short = short or {}
    closes = closes or {}
    dates = _trading_days(days)
    for i, d in enumerate(dates):
        rows = [s for s in amounts if i >= len(dates) - short.get(s, days)]
        part = root / "kline_daily_enriched" / f"date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": rows, "date": [d] * len(rows),
            "close": [closes.get(s, 50.0) for s in rows],
            "amount": [amounts[s] for s in rows],
        }).write_parquet(part / "part.parquet")


def _write_instruments(root: Path, types: dict[str, str]) -> None:
    out = root / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": list(types), "instrument_type": list(types.values())}).write_parquet(out)


def _write_config(path: Path, themes: dict[str, list[str]], cap: int) -> Path:
    path.write_text(json.dumps({
        "version": 1, "max_symbols": cap,
        "liquidity_fill": {"window_days": 60, "min_close": 5.0, "min_history_days": 55,
                           "instrument_types": ["common_stock", "adr", "unknown"]},
        "themes": {name: {"label": name, "symbols": syms} for name, syms in themes.items()},
    }))
    return path


@pytest.fixture
def universe(tmp_path):
    amounts = {f"T{i:02d}": 1e9 - i for i in range(10)}            # themed
    amounts.update({f"F{i:02d}": 5e8 - i for i in range(10)})       # liquidity fill candidates
    amounts.update({"W1": 1.0, "W2": 2.0, "NEW": 9e9, "PENNY": 8e9, "WARR": 7e9})
    _write_enriched(tmp_path, amounts, short={"NEW": 20}, closes={"PENNY": 1.0})
    types = {s: "common_stock" for s in amounts}
    types["WARR"] = "warrant"
    _write_instruments(tmp_path, types)
    themes = {"ai": [f"T{i:02d}" for i in range(10)] + ["NEW", "GONE"]}
    cfg = _write_config(tmp_path / "pool.json", themes, cap=15)
    return tmp_path, cfg


def test_pool_keeps_watchlist_first_then_themes_then_liquidity_fill_up_to_cap(universe):
    data_dir, cfg = universe
    pool = mining_pool.build_mining_pool(data_dir, ["W1", "W2"], config_path=cfg)
    assert len(pool.symbols) == 15
    assert pool.symbols[:2] == ["W1", "W2"]                          # 自选必在, 即使成交额最低
    assert set(pool.symbols[2:12]) == {f"T{i:02d}" for i in range(10)}
    assert pool.symbols[12:] == ["F00", "F01", "F02"]                # 按近 60 日成交额补足
    assert pool.by_source == {"watchlist": 2, "theme": 10, "liquidity": 3}


def test_pool_drops_symbols_without_enough_history_and_ineligible_fill(universe):
    data_dir, cfg = universe
    pool = mining_pool.build_mining_pool(data_dir, ["W1"], config_path=cfg)
    assert "NEW" not in pool.symbols          # 只有 20 天日K, 不足 55 天
    assert "GONE" not in pool.symbols         # 本地无数据
    assert "PENNY" not in pool.symbols        # 股价 < $5 不进流动性补足
    assert "WARR" not in pool.symbols         # 权证不进流动性补足
    assert {"NEW", "GONE"} <= set(pool.dropped)


def test_cap_trims_themes_but_never_the_watchlist(tmp_path):
    amounts = {f"T{i:02d}": 1e9 - i for i in range(10)}
    amounts.update({f"W{i}": 1.0 for i in range(4)})
    _write_enriched(tmp_path, amounts)
    _write_instruments(tmp_path, {s: "common_stock" for s in amounts})
    cfg = _write_config(tmp_path / "pool.json", {"ai": [f"T{i:02d}" for i in range(10)]}, cap=6)
    pool = mining_pool.build_mining_pool(tmp_path, [f"W{i}" for i in range(4)], config_path=cfg)
    assert pool.symbols == ["W0", "W1", "W2", "W3", "T00", "T01"]   # 主题按成交额截断
    assert pool.by_source == {"watchlist": 4, "theme": 2, "liquidity": 0}


def test_shipped_config_is_capped_at_200_and_contains_every_current_watchlist_name():
    cfg = json.loads(mining_pool.DEFAULT_CONFIG_PATH.read_text())
    assert cfg["max_symbols"] == 200
    themed = {s for theme in cfg["themes"].values() for s in theme["symbols"]}
    watchlist = {"AAOI", "AAPL", "AMD", "AMZN", "ARM", "AVGO", "COIN", "CRCL", "DELL", "INTC", "LITE", "META",
                 "MRVL", "MSFT", "MSTR", "MU", "NVDA", "ORCL", "PLTR", "RKLB", "SNDK", "SPCX", "TSLA", "TSM"}
    assert watchlist <= themed
    for theme in ("semiconductors", "memory_storage", "servers_compute", "ai_software", "commercial_space", "biotech_pharma"):
        assert cfg["themes"][theme]["symbols"]


class _Repo:
    def __init__(self, data_dir):
        self.store = SimpleNamespace(data_dir=data_dir)

    def latest_enriched_date(self, asset_type="stock"):
        return LATEST

    def get_matrix_data_generation(self, asset_type="stock"):
        return "g1"

    def get_instruments_asset(self, asset_type="stock"):
        return pl.read_parquet(self.store.data_dir / "instruments" / "instruments.parquet")


class _Manager:
    def __init__(self, data_dir):
        self.store = MiningRunStore(data_dir)
        self.calls = []

    def start(self, request, fingerprint, *, force, source, run_id):
        self.calls.append(request)
        return {"run_id": self.store.create(request, fingerprint, run_id=run_id)["run_id"]}


def test_weekly_request_uses_the_bounded_pool_instead_of_the_full_market(universe, monkeypatch):
    data_dir, cfg = universe
    monkeypatch.setattr(mining_pool, "DEFAULT_CONFIG_PATH", cfg)
    monkeypatch.setattr(mining_pool, "_watchlist_symbols", lambda: ["W1", "W2"])
    monkeypatch.setattr(preferences, "get_mining_schedule", lambda: {
        "mining_schedule_enabled": True, "mining_schedule_weekday": 4, "mining_budget_profile": "balanced"})
    # enough history for the balanced profile gate
    for d in _trading_days(900):
        part = data_dir / "kline_daily_enriched" / f"date={d.isoformat()}"
        if not part.exists():
            part.mkdir(parents=True)
            (part / "part.parquet").write_bytes(b"x")
    manager = _Manager(data_dir)
    state = SimpleNamespace(repo=_Repo(data_dir), mining_manager=manager, strategy_engine=None)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    result = mining_schedule.run_weekly_mining(state, now=datetime(2026, 8, 14, 18, tzinfo=ZoneInfo("America/New_York")))
    assert result["status"] == "enqueued"
    request = manager.calls[0]
    assert request["symbols"] is not None and len(request["symbols"]) == 15
    assert request["symbols"][:2] == ["W1", "W2"]
    assert request["universe"]["max_symbols"] == 15
