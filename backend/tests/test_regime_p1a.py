# ruff: noqa: RUF001, RUF002, RUF003
"""P1-A: regime three states (slow + hourly fast + hysteresis), multi_scale_v1 gate, short only in defense,
observe-grade cards stay out of the paper book, snapshot keys append-only, config rollback."""
import json
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.market_clock import us_clock
from app.opportunity import direction_gate as dg
from app.opportunity import multi_scale_gate as ms
from app.opportunity import regime as rg
from app.services import strategy_evaluation as se
from app.services import swing_desk as sd
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv

A, R, D = rg.ATTACK, rg.RANGE, rg.DEFENSE
SIG_DAY = date(2026, 10, 8)


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


# ── hysteresis state machine ───────────────────────────────────────────

def test_hourly_downgrade_needs_single_fast_trigger():
    s = rg.step(A, mode="hourly", slow=A, fast=D, day="2026-11-02", slot="10:00")
    assert s.changed and s.state == D and s.trigger == "fast"


def test_hourly_upgrade_needs_two_consecutive_confirmations_without_slow_agreement():
    s1 = rg.step(D, mode="hourly", slow=D, fast=A, day="2026-11-02", slot="10:00")
    assert not s1.changed and s1.state == D and s1.streak["count"] == 1
    s2 = rg.step(D, mode="hourly", slow=D, fast=R, streak=s1.streak, day="2026-11-02", slot="11:00")
    assert s2.changed and s2.state == R and s2.trigger == "fast_confirm"     # 两次确认取较低的目标


def test_hourly_upgrade_streak_breaks_on_flat_reading_and_on_new_day():
    s1 = rg.step(D, mode="hourly", slow=D, fast=R, day="2026-11-02", slot="14:00")
    s2 = rg.step(D, mode="hourly", slow=D, fast=D, streak=s1.streak, day="2026-11-02", slot="15:00")
    assert not s2.changed and s2.streak["count"] == 0
    s3 = rg.step(D, mode="hourly", slow=D, fast=R, streak=s1.streak, day="2026-11-03", slot="09:25")
    assert not s3.changed and s3.streak["count"] == 1                     # 跨日不连


def test_hourly_upgrade_immediate_when_slow_agrees():
    s = rg.step(R, mode="hourly", slow=A, fast=A, day="2026-11-02", slot="10:00")
    assert s.changed and s.state == A and s.note == "slow_agrees"


def test_close_upgrade_capped_by_close_fast_reading_and_downgrade_by_either_layer():
    up = rg.step(D, mode="close", slow=A, fast=R, day="2026-11-02")
    assert up.changed and up.state == R and up.trigger == "slow"
    dn = rg.step(A, mode="close", slow=R, fast=A, day="2026-11-02")
    assert dn.changed and dn.state == R and dn.trigger == "slow"
    dn2 = rg.step(A, mode="close", slow=A, fast=D, day="2026-11-02")
    assert dn2.changed and dn2.state == D and dn2.trigger == "fast"


def test_close_without_slow_layer_never_upgrades():
    assert rg.step(R, mode="close", slow=None, fast=A, day="d").state == R
    assert rg.step(R, mode="close", slow=None, fast=D, day="d").state == D


def test_slow_and_fast_targets():
    up = {"above_ma50": True, "ma50_slope_up": True, "breadth": 0.7}
    assert rg.slow_target(up)[0] == A
    assert rg.slow_target({**up, "breadth": 0.5})[0] == R
    assert rg.slow_target({"above_ma50": False, "ma50_slope_up": False, "breadth": 0.6})[0] == D
    assert rg.slow_target({"above_ma50": False, "ma50_slope_up": True, "breadth": 0.3})[0] == D
    assert rg.slow_target(None) == (None, [])
    base = {"above_ema10": True, "break_up": False, "break_down": False, "vol_ratio": 1.0}
    assert rg.fast_target(base)[0] == A
    assert rg.fast_target({**base, "vol_ratio": 1.3})[0] == R
    assert rg.fast_target({**base, "break_down": True})[0] == D
    assert rg.fast_target({**base, "above_ema10": False, "vol_ratio": 1.6})[0] == D


def test_fast_values_uses_prior_days_only():
    c = np.linspace(100, 130, 80)
    v = rg.fast_values(c, c + 1, c - 1, 90.0)
    assert v["break_down"] is True and v["above_ema10"] is False
    assert rg.fast_target(v)[0] == D
    assert rg.fast_values(c[:10], c[:10], c[:10], 100.0) is None


# ── persistence: state file + append-only audit ────────────────────────

def _spy_repo(closes, end: date, tmp_path=None):
    days = []
    d = end
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    df = pl.DataFrame({"symbol": "SPY", "date": days, "high": [x + 1 for x in closes],
                       "low": [x - 1 for x in closes], "close": [float(x) for x in closes]})

    def batch(syms, s, e, columns=None):
        return df.filter(pl.col("symbol").is_in(syms) & (pl.col("date") >= s) & (pl.col("date") <= e))
    return SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_daily_batch=batch)


def _frames_up(n_syms=6, n=40):
    out = {}
    for i in range(n_syms):
        c = np.linspace(50, 60 + i, n)
        out[f"S{i}"] = pl.DataFrame({"date": [date(2026, 9, 1) + timedelta(days=k) for k in range(n)], "close": c})
    return out


def test_evaluate_close_writes_state_and_append_only_audit(tmp_path):
    end = date(2026, 11, 2)
    up = list(np.linspace(500, 700, 120))
    repo = _spy_repo(up, end, tmp_path)
    frames = _frames_up()
    f1 = rg.evaluate_close(tmp_path, repo, end, frames=frames, watchlist=set(frames))
    assert f1["regime_state"] == A and f1["regime_contract"] == "regime-v1" and f1["regime_reasons"]
    audit = (tmp_path / "rules" / rg.AUDIT_FILE).read_bytes()
    assert len(rg.load_audit(tmp_path)) == 1 and rg.load_audit(tmp_path)[0]["from"] is None
    # 同态再评估: 不追加
    rg.evaluate_close(tmp_path, repo, end, frames=frames, watchlist=set(frames))
    assert (tmp_path / "rules" / rg.AUDIT_FILE).read_bytes() == audit
    # 断崖下跌 → 降级, 追加一行, 旧行字节不变
    crash = up[:-1] + [520.0]
    f2 = rg.evaluate_close(tmp_path, _spy_repo(crash, end, tmp_path), end, frames=frames, watchlist=set(frames))
    assert f2["regime_state"] == D
    after = (tmp_path / "rules" / rg.AUDIT_FILE).read_bytes()
    assert after.startswith(audit) and len(rg.load_audit(tmp_path)) == 2
    row = rg.load_audit(tmp_path)[-1]
    assert row["from"] == A and row["to"] == D and row["conditions"] and row["values"]["fast"]["break_down"] is True
    assert row["at"] and row["trigger"] in ("fast", "slow")


def test_evaluate_close_without_spy_close_returns_none(tmp_path):
    repo = _spy_repo(list(np.linspace(500, 700, 120)), date(2026, 10, 30), tmp_path)
    assert rg.evaluate_close(tmp_path, repo, date(2026, 11, 2), frames={}, watchlist=set()) is None
    assert not (tmp_path / "rules" / rg.STATE_FILE).exists()


def test_evaluate_hourly_runs_only_in_mood_slots_and_once_per_slot(tmp_path, monkeypatch, cal):
    end = date(2026, 11, 2)
    closes = list(np.linspace(500, 700, 120))
    repo = _spy_repo(closes, end - timedelta(days=3), tmp_path)    # 只到上周五
    rg._save_state(tmp_path, {"state": A, "as_of": "2026-10-30", "slow": {"target": A}})
    now = datetime.combine(end, time(10, 5), tzinfo=us_clock.tz)
    res = rg.evaluate_hourly(tmp_path, repo, now=now, price=600.0)          # 跌破前 20 日低
    assert res["state"] == D and res["slot"] == "10:00" and res["mode"] == "hourly"
    again = rg.evaluate_hourly(tmp_path, repo, now=now, price=999.0)
    assert again["state"] == D and len(rg.load_audit(tmp_path)) == 1      # 同一 slot 不再评估
    off = datetime.combine(end, time(10, 40), tzinfo=us_clock.tz)
    assert rg.evaluate_hourly(tmp_path, repo, now=off, price=999.0) is None
    # 无现价 = 不评估
    later = datetime.combine(end, time(11, 2), tzinfo=us_clock.tz)
    monkeypatch.setattr(rg, "_live_price", lambda *a, **k: None)
    assert rg.evaluate_hourly(tmp_path, repo, now=later) is None


def test_hourly_hook_lives_in_market_mood_update_job():
    """快层复用已有的大盘整点重判任务 (不另起定时器)。"""
    import inspect

    from app.jobs import daily_pipeline
    src = inspect.getsource(daily_pipeline)
    i = src.index("def _market_mood_update")
    body = src[i:src.index("scheduler.add_job", i)]
    assert "regime.evaluate_hourly" in body
    assert src.count("regime.evaluate_hourly") == 1


# ── multi_scale_v1 gate ────────────────────────────────────────────────

def test_vote_four_combinations_and_fail_closed():
    assert ms.vote("long", "long", None) == ("long", ms.FULL)           # 慢中同意
    assert ms.vote("long", "short", "long") == ("long", ms.OBSERVE)     # 冲突 → 快层定, 只观察级
    assert ms.vote("long", None, "long") == ("long", ms.OBSERVE)        # 中性也算冲突
    assert ms.vote("long", "short", None) == (None, None)               # 冲突且快层缺 → 不放行
    assert ms.vote(None, "long", "long") == (None, None)                # 慢层数据不足 → 不放行
    assert ms.vote("short", "short", "long") == ("short", ms.FULL)


def _frame(closes):
    return pl.DataFrame({"date": [date(2026, 1, 1) + timedelta(days=i) for i in range(len(closes))],
                         "close": [float(x) for x in closes]})


def test_multi_scale_states_grades_and_fail_closed():
    up = np.linspace(50, 100, 80)
    mixed = np.concatenate([np.linspace(50, 100, 70), np.linspace(100, 93, 10)])   # 仍在 MA50 上, MA20 下且下行
    frames = {"UP": _frame(up), "MIX": _frame(mixed), "MIX2": _frame(mixed), "SHORT": _frame(up[:30])}
    pre = {"phantom_4h_state": {"MIX": "long"}}
    ctx = dg.GateContext(data_dir=None, repo=None, as_of=date(2026, 12, 31), symbols=tuple(frames),
                         frames=frames, precomputed=pre)
    st = ms._states(ctx)
    assert st == {"UP": "long", "MIX": "long"}
    assert pre["gate_grades"] == {"MIX": ms.OBSERVE}


def test_gate_switch_and_rollback_via_swing_json(tmp_path):
    (tmp_path / "rules").mkdir()
    p = tmp_path / "rules" / "swing.json"
    p.write_text(json.dumps({"long": ["macd_golden"], "short": [], "gate": "multi_scale_v1"}))
    assert dg.active(tmp_path).id == "multi_scale_v1"
    p.write_text(json.dumps({"long": ["macd_golden"], "short": [], "gate": "daily_ma50"}))
    assert dg.active(tmp_path).id == "daily_ma50"
    assert dg.get("multi_scale_v1").preferred is False                  # 不配置不会被选中


def test_save_strategies_keeps_gate_and_regime_switches(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "swing.json").write_text(json.dumps({"long": ["a"], "short": [], "gate": "daily_ma50",
                                                                "regime": False}))
    out = sd.save_strategies(tmp_path, ["macd_golden"], [])
    raw = json.loads((tmp_path / "rules" / "swing.json").read_text())
    assert raw["gate"] == "daily_ma50" and raw["regime"] is False and out["gate"] == "daily_ma50"
    assert dg.active(tmp_path).id == "daily_ma50" and rg.enabled(tmp_path) is False


def test_direction_states_passes_observe_grades_to_meta(tmp_path, monkeypatch):
    from app.services import decision_scan_job as job
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "swing.json").write_text(json.dumps({"long": ["x"], "gate": "multi_scale_v1"}))
    mixed = np.concatenate([np.linspace(50, 100, 70), np.linspace(100, 93, 10)])
    frames = {"MIX": _frame(mixed), "UP": _frame(np.linspace(50, 100, 80))}
    meta = {"phantom_4h_state": {"MIX": "long"}}
    st = job._direction_states(None, tmp_path, date(2026, 12, 31), frames, set(frames), meta)
    assert st == {"MIX": "long", "UP": "long"}
    assert meta["gate_grades"] == {"MIX": "observe"} and meta["direction_gate"]["id"] == "multi_scale_v1"


# ── snapshot: append only ──────────────────────────────────────────────

def test_save_signals_regime_keys_are_append_only(tmp_path):
    frames = {"AAA": pl.DataFrame({"date": [SIG_DAY], "close": [10.0]})}
    wv.save_signals(tmp_path, SIG_DAY, {}, watchlist={"AAA"}, frames=frames, h4_state={"AAA": "long"})
    old = json.loads((wv.signals_dir(tmp_path) / f"{SIG_DAY}.json").read_text())
    reg = {"regime_state": D, "regime_as_of": "2026-10-08T16:40:00-04:00", "regime_reasons": ["SPY < MA50"],
           "regime_contract": "regime-v1"}
    wv.save_signals(tmp_path, SIG_DAY, {}, watchlist={"AAA"}, frames=frames, h4_state={"AAA": "long"},
                    regime=reg, gate_grade={"AAA": "observe", "ZZZ": "observe"})
    new = json.loads((wv.signals_dir(tmp_path) / f"{SIG_DAY}.json").read_text())
    for k, v in old.items():
        if k != "generated_at":
            assert new[k] == v
    assert set(new) - set(old) == {"regime_state", "regime_as_of", "regime_reasons", "regime_contract", "gate_grade"}
    assert new["gate_grade"] == {"AAA": "observe"}


# ── board + paper book: short only in defense, observe grade not booked ──

def _mode(tmp_path):
    d = tmp_path / "decision_desk"
    d.mkdir(parents=True, exist_ok=True)
    (d / "evidence_mode.json").write_text(json.dumps({"mode": "v4"}))
    se._V4_CACHE.clear()
    wv._STATUS_CACHE.clear()
    wv._PM_CACHE.clear()
    wb._ENSURE_KEY.clear()
    wb._ENSURE_RETRY.clear()
    wb._LOAD_CACHE.clear()


def _row(rule, sym, side="long"):
    return {"rule": rule, "symbol": sym, "side": side, "entry_zone": [100.0, 101.0], "invalidation": 80.0,
            "target_zone": [150.0, 160.0], "risk": 20.0, "rr": 2.0}


def _setup(tmp_path, monkeypatch, *, regime_state, regime_switch=None, gate_grade=None, spy_ok=True):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 1.0})
    sd.save_strategies(tmp_path, ["macd_golden"], ["tv_halftrend"])
    if regime_switch is not None:
        p = tmp_path / "rules" / "swing.json"
        raw = json.loads(p.read_text())
        raw["regime"] = regime_switch
        p.write_text(json.dumps(raw))
    wv._PM_CACHE.clear()
    monkeypatch.setattr(wv, "short_gate", lambda repo, day: {"day": day.isoformat(), "ok": spy_ok,
                                                             "close": 700.0, "ma50": 710.0})
    syms = ["L1", "L2", "L3", "AMD"]
    rows = [_row("macd_golden", s) for s in ("L1", "L2", "L3")] + [_row("tv_halftrend", "AMD", "short")]
    snap = {"as_of": SIG_DAY.isoformat(), "watchlist": syms, "signals": rows,
            "closes": {"L1": 100.0, "L2": 100.0, "L3": 100.0, "AMD": 200.0},
            "atr": {"L1": 2.0, "L2": 2.0, "L3": 2.0, "AMD": 4.0},
            "h4_state": {"L1": "long", "L2": "long", "L3": "long", "AMD": "short"}}
    if regime_state:
        snap.update(regime_state=regime_state, regime_as_of="x", regime_reasons=["r"], regime_contract="regime-v1")
    if gate_grade:
        snap["gate_grade"] = gate_grade
    d = wv.signals_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{SIG_DAY.isoformat()}.json").write_text(json.dumps(snap))
    return set(syms)


def _empty_repo(tmp_path):
    return SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_daily_batch=lambda *a, **k: pl.DataFrame())


def test_defense_opens_short_as_observe_only_and_closes_long(tmp_path, monkeypatch, cal):
    watch = _setup(tmp_path, monkeypatch, regime_state=D, spy_ok=False)
    board = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    assert board["long"] == [] and [c["symbol"] for c in board["short"]] == ["AMD"]
    assert board["short"][0]["grade"] == "observe" and board["meta"]["regime"]["state"] == D
    assert board["meta"]["short_gate"] is True                               # SPY 在 MA50 上也开 (由 regime 定)
    built = wb.build_day(tmp_path, _empty_repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    assert built["cards"] == []
    assert {"symbol": "AMD", "side": "short", "reason": "observe_only"} in built["skipped"]


def test_attack_and_range_no_short_and_long_caps(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch, regime_state=A, spy_ok=True)
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    assert b["short"] == [] and len(b["long"]) == 3 and b["meta"]["short_gate"] is False
    _setup(tmp_path, monkeypatch, regime_state=R, spy_ok=True)
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    assert b["short"] == [] and len(b["long"]) == 2


def test_regime_switch_off_rolls_back_to_tight_v1(tmp_path, monkeypatch, cal):
    watch = _setup(tmp_path, monkeypatch, regime_state=R, regime_switch=False, spy_ok=True)
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    assert len(b["long"]) == 3 and [c["symbol"] for c in b["short"]] == ["AMD"] and "regime" not in b["meta"]
    assert all("grade" not in c for c in b["long"] + b["short"])
    built = wb.build_day(tmp_path, _empty_repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    assert {c["symbol"] for c in built["cards"]} == {"L1", "L2", "L3", "AMD"}


def test_old_snapshot_without_regime_keeps_tight_v1(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch, regime_state=None, spy_ok=False)
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    assert len(b["long"]) == 3 and b["short"] == [] and b["meta"]["short_gate"] is False


def test_multi_scale_observe_grade_long_not_booked(tmp_path, monkeypatch, cal):
    watch = _setup(tmp_path, monkeypatch, regime_state=A, gate_grade={"L2": "observe"})
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    assert {c["symbol"]: c.get("grade") for c in b["long"]} == {"L1": None, "L2": "observe", "L3": None}
    built = wb.build_day(tmp_path, _empty_repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    assert {c["symbol"] for c in built["cards"]} == {"L1", "L3"}
    assert {"symbol": "L2", "side": "long", "reason": "observe_only"} in built["skipped"]


def test_short_regime_does_not_downweight_halftrend():
    """空头样本少不是降权理由: regime 只决定开不开, 不改 3+3 空头规则名单或权重。"""
    assert "tv_halftrend" in sd.DEFAULT_SHORT
    assert rg.SHORT_CAP[D] == sd.MAX_PER_SIDE
