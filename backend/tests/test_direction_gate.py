"""机会板方向门 (可插拔): 内置日线 MA50 门、插件注册与选择顺序、扫描接线。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.opportunity import direction_gate as dg


def _frame(closes: list[float], end: date) -> pl.DataFrame:
    days = [end - timedelta(days=len(closes) - 1 - i) for i in range(len(closes))]
    return pl.DataFrame({"date": days, "close": closes})


def _ctx(frames: dict, as_of: date, repo=None, pre=None) -> dg.GateContext:
    return dg.GateContext(data_dir=Path("."), repo=repo, as_of=as_of, symbols=tuple(sorted(frames)),
                          frames=frames, precomputed=pre or {})


D = date(2026, 10, 28)


def test_daily_ma50_long_short_and_fail_closed():
    up = _frame([100 + i for i in range(60)], D)            # 末值远高于均线
    down = _frame([200 - i for i in range(60)], D)          # 末值远低于均线
    short_hist = _frame([100 + i for i in range(30)], D)    # 样本不足 → 不放行
    flat = _frame([100.0] * 60, D)                          # 等于均线 → 不放行
    gate = dg.get(dg.DEFAULT_GATE_ID)
    out = gate.states(_ctx({"UP": up, "DN": down, "NEW": short_hist, "FLAT": flat}, D))
    assert out == {"UP": "long", "DN": "short"}


def test_daily_ma50_ignores_bars_after_as_of():
    """信号日之后的K线 (例如回放时帧里多带了未来) 不得影响当日方向。"""
    closes = [100 + i for i in range(60)] + [0.01] * 5      # 未来 5 天暴跌
    frame = _frame(closes, D + timedelta(days=5))
    out = dg.get(dg.DEFAULT_GATE_ID).states(_ctx({"X": frame}, D))
    assert out == {"X": "long"}


def test_daily_ma50_falls_back_to_repo_for_symbols_outside_frames():
    calls = []

    def get_daily_batch(symbols, start, end, columns):
        calls.append((tuple(symbols), end))
        return pl.concat([_frame([100 + i for i in range(60)], end).with_columns(pl.lit("WL").alias("symbol"))])

    repo = SimpleNamespace(get_daily_batch=get_daily_batch)
    ctx = dg.GateContext(data_dir=Path("."), repo=repo, as_of=D, symbols=("WL",), frames={})
    assert dg.get(dg.DEFAULT_GATE_ID).states(ctx) == {"WL": "long"}
    assert calls and calls[0][0] == ("WL",) and calls[0][1] == D


def test_daily_ma50_matrix_uses_previous_close_only():
    days_all = [date(2026, 1, 1) + timedelta(days=i) for i in range(80)]
    closes = [100.0] * 60 + [200.0] * 20                      # 第 60 天起上穿
    batch = pl.DataFrame({"symbol": ["A"] * 80, "date": days_all, "close": closes})
    repo = SimpleNamespace(get_daily_batch=lambda symbols, start, end, columns: batch.filter(pl.col("date") <= end))
    days = days_all[55:70]
    m = dg.get(dg.DEFAULT_GATE_ID).matrix(Path("."), repo, ["A"], days)
    assert m.shape == (15, 1)
    first_up = days_all.index(days_all[60])
    for t, d in enumerate(days):
        # 只看严格早于 d 的收盘: d=第 60 天时前一日仍是 100 → 不放行; 第 61 天起放行
        assert bool(m[t, 0]) == (days_all.index(d) > first_up), d


def test_registry_selection_order(tmp_path, monkeypatch):
    monkeypatch.setattr(dg, "_REGISTRY", dict(dg._REGISTRY))
    base = dg.active(tmp_path)
    # 没有插件 (或插件未声明 preferred) → 内置门
    for gid, g in list(dg._REGISTRY.items()):
        if g.preferred:
            dg._REGISTRY.pop(gid)
    assert dg.active(tmp_path).id == dg.DEFAULT_GATE_ID
    # 插件声明 preferred → 插件优先
    plugin = dg.DirectionGate(id="plug", name="插件门", states=lambda ctx: {}, preferred=True)
    dg.register(plugin)
    assert dg.active(tmp_path).id == "plug"
    # swing.json 显式选择 (已注册) 优先于插件; 选了未注册的门 → 忽略
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "swing.json").write_text(json.dumps({"gate": dg.DEFAULT_GATE_ID}))
    assert dg.active(tmp_path).id == dg.DEFAULT_GATE_ID
    (tmp_path / "rules" / "swing.json").write_text(json.dumps({"gate": "missing"}))
    assert dg.active(tmp_path).id == "plug"
    assert base.id in {g["id"] for g in dg.available()}


def test_scan_direction_states_uses_active_gate_and_records_meta(tmp_path, monkeypatch):
    from app.services import decision_scan_job as job
    monkeypatch.setattr(dg, "_REGISTRY", dict(dg._REGISTRY))
    seen = {}

    def states(ctx):
        seen["pre"] = ctx.precomputed
        seen["symbols"] = ctx.symbols
        return {"AAA": "long", "BBB": "short", "CCC": "sideways"}

    dg.register(dg.DirectionGate(id="probe", name="探针门", states=states, preferred=True))
    monkeypatch.setattr(dg, "active", lambda data_dir=None: dg.get("probe"))
    meta = {"phantom_4h_state": {"AAA": "long"}}
    out = job._direction_states(None, tmp_path, D, {}, {"AAA", "BBB", "CCC"}, meta)
    assert out == {"AAA": "long", "BBB": "short"}                      # 非法值丢弃
    assert seen["pre"] == {"phantom_4h_state": {"AAA": "long"}}        # 插件来源结果作为 precomputed 交给门
    assert seen["symbols"] == ("AAA", "BBB", "CCC")
    assert "phantom_4h_state" not in meta and meta["direction_gate"] == {"id": "probe", "long": 1, "short": 1}


def test_scan_direction_states_fails_closed_on_gate_error(tmp_path, monkeypatch):
    from app.services import decision_scan_job as job

    def boom(ctx):
        raise RuntimeError("x")

    monkeypatch.setattr(dg, "active", lambda data_dir=None: dg.DirectionGate(id="bad", name="坏门", states=boom))
    meta = {}
    assert job._direction_states(None, tmp_path, D, {}, {"AAA"}, meta) == {}
    assert meta["direction_gate"]["id"] == "bad"


@pytest.mark.full_edition
def test_full_edition_prefers_phantom_gate_and_reuses_scan_state(tmp_path):
    """完整版: Phantom 4h 门插件存在且优先; 扫描期直接复用来源扫描算好的方向 (行为与改造前一致)。"""
    gate = dg.active(tmp_path)
    assert gate.id == "phantom_4h" and gate.matrix is not None
    ctx = _ctx({}, D, pre={"phantom_4h_state": {"NVDA": "long", "TSLA": "short", "X": "?"}})
    assert gate.states(ctx) == {"NVDA": "long", "TSLA": "short"}


def test_swing_candidates_gate_matrix_delegates_to_active_gate(tmp_path, monkeypatch):
    from app.services import swing_candidates as sc
    want = np.array([[True], [False]])
    monkeypatch.setattr(dg, "active", lambda data_dir=None: dg.DirectionGate(
        id="m", name="m", states=lambda ctx: {}, matrix=lambda d, r, s, days: want))
    assert (sc._gate_matrix(tmp_path, None, ["A"], [D, D]) == want).all()
    monkeypatch.setattr(dg, "active", lambda data_dir=None: dg.DirectionGate(id="n", name="n", states=lambda ctx: {}))
    assert not sc._gate_matrix(tmp_path, None, ["A", "B"], [D]).any()       # 无矩阵实现 = 全不放行
