"""审计 1026 第三批 b: 策略页/监控/因子类问题回归 (M-7 M-8 M-10 M-11 M-16 L-12 L-13 L-14)."""
from __future__ import annotations

import errno
import json
import pathlib
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import screener as screener_api
from app.market_clock import us_clock
from app.services import strategy_cache
from app.services.screener import regular_session_hours
from app.strategy import monitor as mon
from app.strategy import monitor_rules
from app.strategy.engine import StrategyResult

# ── M-7: 小时线组装 ─────────────────────────────────────────


def _minute_bars(day: date, minutes: int, symbol: str = "NVDA") -> list[tuple]:
    start = datetime.combine(day, time(9, 30))
    rows = []
    for i in range(minutes):
        ts = start + timedelta(minutes=i)
        px = 100.0 + i * 0.01
        rows.append((symbol, ts, px, px + 0.05, px - 0.05, px + 0.01, 100.0))
    return rows


def _frame(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(
        rows, schema=["symbol", "datetime", "open", "high", "low", "close", "volume"], orient="row",
    )


def test_m7_naive_one_minute_bars_build_seven_hour_buckets(monkeypatch):
    monkeypatch.setattr(us_clock, "session_close", lambda d: time(16, 0))
    day = date(2026, 10, 23)
    hours = regular_session_hours(_frame(_minute_bars(day, 390)))

    assert hours.height == 7
    labels = [ts.strftime("%H:%M") for ts in hours["datetime"].to_list()]
    assert labels == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    assert hours["volume"].to_list() == [6000.0] * 6 + [3000.0]
    first = hours.row(0, named=True)
    assert first["open"] == pytest.approx(100.0)
    assert first["close"] == pytest.approx(100.0 + 59 * 0.01 + 0.01)
    assert hours["date"].to_list() == [day] * 7


def test_m7_thirty_minute_input_still_supported(monkeypatch):
    monkeypatch.setattr(us_clock, "session_close", lambda d: time(16, 0))
    day = date(2026, 10, 23)
    rows = [r for r in _minute_bars(day, 390) if r[1].minute in (0, 30)]
    hours = regular_session_hours(_frame(rows))
    assert hours.height == 7
    assert hours["volume"].to_list() == [200.0] * 6 + [100.0]


def test_m7_half_day_stops_at_session_close(monkeypatch):
    half = date(2026, 11, 27)
    monkeypatch.setattr(
        us_clock, "session_close", lambda d: time(13, 0) if d == half else time(16, 0),
    )
    # 数据源在 13:00 后仍有零星盘后 K, 不得混进常规时段
    hours = regular_session_hours(_frame(_minute_bars(half, 300)))
    labels = [ts.strftime("%H:%M") for ts in hours["datetime"].to_list()]
    assert labels == ["09:30", "10:30", "11:30", "12:30"]
    assert hours["volume"].to_list() == [6000.0] * 3 + [3000.0]


# ── M-8: scope=symbols 监控只覆盖自己的股票 ─────────────────


class _MonitorEngine:
    def __init__(self, results):
        self.results = results

    def latest_strategy_results(self):
        return self.results


def _request(data_dir, monitor):
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=data_dir)), monitor_engine=monitor)
    return SimpleNamespace(app=SimpleNamespace(state=state))


class _FakeStrategyEngine:
    def __init__(self, hits):
        self.hits = hits

    def get(self, sid):
        return SimpleNamespace(
            execution_backend="polars_expr", filter_history_fn=None, lookback_days=1,
            meta={"id": sid, "name": "fake"},
        )

    def required_history_bars(self, ids, **kw):
        return 1

    def run(self, sid, ctx, pool=None, overrides=None, params=None):
        present = set(ctx.current["symbol"].to_list())
        syms = [s for s in self.hits if s in present]
        return StrategyResult(
            as_of=date(2026, 10, 26), strategy_id=sid,
            rows=[{"symbol": s, "close": 10.0} for s in syms], total=len(syms),
            scores={s: 50.0 for s in syms},
            entry_signal_hits=[{"symbol": s, "signals": ["sig"]} for s in syms],
        )


def _df(n):
    return pl.DataFrame({
        "symbol": [f"S{i:02d}" for i in range(n)],
        "close": [10.0] * n,
        "change_pct": [0.01] * n,
    })


def test_m8_symbols_scope_realtime_merges_into_full_market_cache(tmp_path, monkeypatch):
    today = str(mon.cn_today())
    full_rows = [{"symbol": f"S{i:02d}", "close": 1.0} for i in range(60)]
    strategy_cache.write_cache(tmp_path, today, {"sid": {"total": 60, "as_of": today, "rows": full_rows}})

    engine = mon.MonitorRuleEngine()
    # 监控子集里 S00..S02 仍命中, S03/S04 实时掉出
    engine.set_strategy_engine(_FakeStrategyEngine(["S00", "S01", "S02"]))
    rule = monitor_rules.normalize({
        "id": "mr_m8", "name": "m8", "type": "strategy", "scope": "symbols",
        "symbols": ["S00", "S01", "S02", "S03", "S04"], "strategy_id": "sid",
        "notify_events": ["buy_signal"],
    })
    engine.set_rules([rule])
    engine.evaluate(_df(60), asset_type="stock")

    req = _request(tmp_path, engine)
    summary = screener_api.get_cached_summary(req)
    assert summary["results"]["sid"]["total"] == 58
    one = screener_api.get_cached_result("sid", req, ext_columns=None)
    symbols = {row["symbol"] for row in one["result"]["rows"]}
    assert "S03" not in symbols and "S04" not in symbols
    assert {"S00", "S01", "S02", "S59"} <= symbols
    expired = set(one["today_ever_rows"]) - symbols
    assert expired == {"S03", "S04"}


def test_m8_all_scope_realtime_still_replaces(tmp_path):
    today = str(mon.cn_today())
    strategy_cache.write_cache(tmp_path, today, {"sid": {
        "total": 2, "as_of": today, "rows": [{"symbol": "A"}, {"symbol": "B"}],
    }})
    realtime = {"sid": {"total": 1, "as_of": today, "rows": [{"symbol": "C"}], "covered_symbols": None}}
    summary = screener_api.get_cached_summary(_request(tmp_path, _MonitorEngine(realtime)))
    assert summary["results"]["sid"]["total"] == 1


# ── M-10: 冷却期内被压下的股票冷却后还能推出 ───────────────────


def test_m10_batch_cooldown_does_not_swallow_new_symbols():
    fake = _FakeStrategyEngine([])
    engine = mon.MonitorRuleEngine()
    engine.set_strategy_engine(fake)
    rule = monitor_rules.normalize({
        "id": "mr_m10", "name": "m10", "type": "strategy", "scope": "all", "strategy_id": "x",
        "notify_events": ["buy_signal"], "cooldown_seconds": 3600,
    })
    engine.set_rules([rule])
    df = _df(40)
    t0 = 1_800_000_000.0
    assert engine._evaluate_rule(df, rule, t0) == []
    fake.hits = [f"S{i:02d}" for i in range(6)]
    first = engine._evaluate_rule(df, rule, t0 + 600)
    assert len(first) == 1 and first[0]["symbol"] == ""
    fake.hits = [f"S{i:02d}" for i in range(13)]
    assert engine._evaluate_rule(df, rule, t0 + 1800) == []  # 冷却中
    later = engine._evaluate_rule(df, rule, t0 + 4300)  # 冷却已过
    assert len(later) == 1
    message = later[0]["message"]
    assert "7 只" in message
    assert all(f"S{i:02d}" in message for i in range(6, 13))
    assert "S00" not in message
    # 推过一次后同一根 K 线不再重复
    assert engine._evaluate_rule(df, rule, t0 + 8000) == []


def test_m10_single_symbol_cooldown_keeps_signal_pending():
    fake = _FakeStrategyEngine([])
    engine = mon.MonitorRuleEngine()
    engine.set_strategy_engine(fake)
    rule = monitor_rules.normalize({
        "id": "mr_m10b", "name": "m10b", "type": "strategy", "scope": "all", "strategy_id": "x",
        "notify_events": ["buy_signal"], "cooldown_seconds": 3600,
    })
    engine.set_rules([rule])
    df = _df(5)
    t0 = 1_800_000_000.0
    engine._evaluate_rule(df, rule, t0)
    fake.hits = ["S00"]
    assert [e["symbol"] for e in engine._evaluate_rule(df, rule, t0 + 60)] == ["S00"]
    fake.hits = ["S00", "S01"]
    assert [e["symbol"] for e in engine._evaluate_rule(df, rule, t0 + 120)] == ["S01"]
    assert engine._evaluate_rule(df, rule, t0 + 180) == []


# ── M-11: 评分分量缺失不再整分清零 ───────────────────────────


def test_m11_missing_turnover_does_not_zero_whole_score():
    from app.backtest.matrix import build_market_data_matrix, build_matrix_score

    symbols = ["AAA", "BBB", "CCC", "ZZZ"]
    momentum = {"AAA": 0.0, "BBB": 1.0, "CCC": 2.0, "ZZZ": 3.0}
    turnover = {"AAA": None, "BBB": None, "CCC": 5.0, "ZZZ": None}
    rows = [
        {
            "symbol": s, "date": date(2026, 10, 23), "open": 10.0, "high": 10.0, "low": 10.0,
            "close": 10.0, "volume": 1000.0, "feat_mom": momentum[s], "turnover_rate": turnover[s],
        }
        for s in symbols
    ]
    market = build_market_data_matrix(pl.DataFrame(rows), field_columns={"feat_mom", "turnover_rate"})
    universe = np.ones(market.shape, dtype=bool)
    score = build_matrix_score(
        market, universe, {"feat_mom": 1.0, "turnover_rate": 1.0}, "score", True,
        fallback=np.zeros(market.shape, dtype=np.float32),
    )[0]
    by_symbol = dict(zip(market.symbols, score.tolist(), strict=True))
    # 缺失换手率的票按动量排开, 而不是全部 0 分后按字母序
    assert by_symbol["ZZZ"] > by_symbol["BBB"] > by_symbol["AAA"]
    assert by_symbol["AAA"] > 0.0


# ── M-16: 删除因子扫策略覆盖与策略源码 ────────────────────────


def test_m16_factor_delete_scans_overrides_and_strategy_sources(tmp_path, monkeypatch):
    from app.api import factors as factors_api

    factor_id = "uf_b3b_ref"
    factor_dir = tmp_path / "user_data" / "custom_factors"
    factor_dir.mkdir(parents=True)
    (factor_dir / f"{factor_id}.json").write_text(json.dumps({
        "id": factor_id, "kind": "custom", "label": "x", "formula": "close", "version": 1, "status": "draft",
    }), encoding="utf-8")
    overrides = tmp_path / "user_data" / "strategy_overrides"
    overrides.mkdir(parents=True)
    (overrides / "ma_golden_cross.json").write_text(json.dumps({"scoring": {factor_id: 1.0}}), encoding="utf-8")
    (overrides / "other.json").write_text(json.dumps({"scoring": {f"{factor_id}_2": 1.0}}), encoding="utf-8")
    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "mined_factor_x.py").write_text(f"SCORING = {{'{factor_id}': 1.0}}\n", encoding="utf-8")

    app = FastAPI()
    app.include_router(factors_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    response = TestClient(app).delete(f"/api/factors/custom/{factor_id}")

    assert response.status_code == 409
    assert sorted(response.json()["detail"]["references"]) == [
        "strategies/custom/mined_factor_x.py",
        "user_data/strategy_overrides/ma_golden_cross.json",
    ]
    assert (factor_dir / f"{factor_id}.json").exists()


# ── L-12: NaN/inf 输入 400 且不落盘 ─────────────────────────


class _Repo:
    def __init__(self, data_dir):
        self.store = SimpleNamespace(data_dir=data_dir)

    def resolve_asset_type(self, symbol):
        return "stock"


def _nan_client(tmp_path):
    from app.api import lots as lots_api
    from app.api import monitor_rules as mr_api

    app = FastAPI()
    app.include_router(mr_api.router)
    app.include_router(lots_api.router)
    app.state.repo = _Repo(tmp_path)
    app.state.monitor_engine = None
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_l12_monitor_rule_rejects_non_finite_value(tmp_path, literal):
    client = _nan_client(tmp_path)
    body = (
        '{"id":"mr_nan","name":"nan","type":"price","scope":"symbols","symbols":["AAPL"],'
        f'"conditions":[{{"field":"close","op":">","value":{literal}}}]}}'
    )
    response = client.post("/api/monitor-rules", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert not (tmp_path / "user_data" / "monitor_rules" / "mr_nan.json").exists()
    assert client.get("/api/monitor-rules"). status_code == 200


@pytest.mark.parametrize("field", ["cost_price", "target_pct", "qty"])
def test_l12_lot_rejects_nan(tmp_path, field):
    client = _nan_client(tmp_path)
    payload = {"symbol": "AAPL", "cost_price": 10, "target_pct": 10}
    text = json.dumps(payload)[:-1] + f', "{field}": NaN}}'
    response = client.post("/api/lots", content=text, headers={"content-type": "application/json"})
    assert response.status_code == 400
    lots_dir = tmp_path / "user_data" / "lots"
    assert not list(lots_dir.glob("*.json")) if lots_dir.exists() else True
    assert client.get("/api/lots"). status_code == 200
    assert client.get("/api/monitor-rules"). status_code == 200


def test_l12_save_one_refuses_nan(tmp_path):
    rule = monitor_rules.normalize({
        "id": "mr_direct", "name": "x", "type": "price", "scope": "symbols", "symbols": ["A"],
        "conditions": [{"field": "close", "op": ">", "value": float("nan")}],
    })
    with pytest.raises(ValueError):
        monitor_rules.save_one(tmp_path, rule)
    assert not (tmp_path / "user_data" / "monitor_rules" / "mr_direct.json").exists()


# ── L-13: AI 草稿原子写 ─────────────────────────────────────


def test_l13_ai_draft_write_failure_leaves_no_partial_py(tmp_path, monkeypatch):
    from app.strategy.ai_iterator import AIStrategyIterator

    ai_dir = tmp_path / "strategies" / "ai"
    ai_dir.mkdir(parents=True)
    real_write_text = pathlib.Path.write_text

    def half_then_enospc(self, data, *args, **kwargs):
        if self.parent == ai_dir:
            real_write_text(self, data[: len(data) // 2], *args, **kwargs)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", half_then_enospc)
    reloads = []
    engine = SimpleNamespace(reload=lambda: reloads.append(1))
    code = 'META = {"id": "x", "name": "x"}\n' + "# filler\n" * 200
    with pytest.raises(OSError):
        AIStrategyIterator._save_draft(engine, str(tmp_path), "ai_probe01", code, {"name": "probe"})
    monkeypatch.setattr(pathlib.Path, "write_text", real_write_text)
    assert sorted(p.name for p in ai_dir.iterdir()) == []
    assert reloads == []


# ── L-14: 查看历史日期不清掉今天的缓存 ───────────────────────


def _cache_rows(as_of, *symbols):
    return {"total": len(symbols), "as_of": as_of, "rows": [{"symbol": s, "close": 1.0} for s in symbols]}


def test_l14_past_date_view_keeps_today_cache(tmp_path):
    today, past = "2026-10-23", "2026-09-15"
    strategy_cache.write_cache(tmp_path, today, {"s1": _cache_rows(today, "AAA", "BBB"), "s2": _cache_rows(today, "CCC")})
    strategy_cache.write_cache(tmp_path, today, {"s1": _cache_rows(today, "AAA")})

    strategy_cache.write_cache(tmp_path, past, {"s1": _cache_rows(past, "ZZZ")})
    viewing = strategy_cache.read_cache(tmp_path)
    assert viewing["as_of"] == past
    assert sorted(viewing["today_ever_rows"]["s1"]) == ["ZZZ"]
    assert "live_stash" not in viewing

    strategy_cache.write_cache(tmp_path, today, {"s1": _cache_rows(today, "AAA")})
    back = strategy_cache.read_cache(tmp_path)
    assert back["as_of"] == today
    assert set(back["results"]) == {"s1", "s2"}
    assert sorted(back["today_ever_rows"]["s1"]) == ["AAA", "BBB"]
    assert sorted(back["today_ever_rows"]["s2"]) == ["CCC"]


def test_l14_new_trading_day_still_resets(tmp_path):
    day1, day2 = "2026-10-22", "2026-10-23"
    strategy_cache.write_cache(tmp_path, day1, {"s1": _cache_rows(day1, "AAA")})
    strategy_cache.write_cache(tmp_path, day2, {"s1": _cache_rows(day2, "BBB")})
    cached = strategy_cache.read_cache(tmp_path)
    assert cached["as_of"] == day2
    assert sorted(cached["today_ever_rows"]["s1"]) == ["BBB"]
