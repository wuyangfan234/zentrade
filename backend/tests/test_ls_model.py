# ruff: noqa: RUF003
"""0.57.0 模型状态 (services/ls_model.py): 冠军登记 / 挑战者影子名单 / 同窗裁决 / 特征漂移 / 页面段 / 历史日口径。"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import ls_model as lm
from app.services import ls_rank as lr


@pytest.fixture(autouse=True)
def _cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    lr._SPARK.clear()
    lr._SETTLE_CACHE.clear()


def _bdays(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def _bars(days: list[date], seed: int, base: float = 100.0) -> list[dict]:
    rng = np.random.default_rng(seed)
    c = base
    rows = []
    for d in days:
        o = c * (1 + rng.normal(0, 0.005))
        c = max(1.0, o * (1 + rng.normal(0, 0.02)))
        h, lo = max(o, c) * 1.004, min(o, c) * 0.996
        rows.append({"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": 1e6, "amount": 1e6 * c, "raw_close": c})
    return rows


def _write_daily(data_dir: Path, frames: dict[str, list[dict]]) -> None:
    rows = [{"symbol": s, **r} for s, rs in frames.items() for r in rs]
    df = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    for c in ("raw_high", "raw_low", "turnover_rate"):
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    df = df.with_columns(pl.lit(None, dtype=pl.Int64).alias("quote_ts"))
    base = data_dir / "kline_daily_enriched"
    for (d,), g in df.group_by(["date"]):
        p = base / f"date={d.isoformat()}" / "part.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        g.drop("date").with_columns(pl.lit(d).alias("date")).write_parquet(p)


class _Est:
    def __init__(self, sign: float = 1.0):
        self.sign = sign

    def predict(self, x):
        return self.sign * np.nan_to_num(np.asarray(x)[:, 0], nan=0.0)


def _trained_to(q: str) -> str:
    return "2026-09-22" if q == "2026Q4" else "2026-12-23"


def _model(q: str, sign: float = 1.0) -> dict:
    return {"est": _Est(sign), "meta": {"quarter": q, "trained_to": _trained_to(q), "rows": 1000, "version": lr.MODEL_VERSION}}


SYMS = [f"S{i:02d}" for i in range(20)]


def _universe(tmp_path: Path, end: date, n_days: int = 200) -> None:
    days = _bdays(end, n_days)
    _write_daily(tmp_path, {s: _bars(days, i, base=50 + i) for i, s in enumerate(SYMS)})


def _rec(tmp_path: Path, day: date, q: str, long_r: float, *, sub: str | None = None, late: bool = False,
         settled_end: date | None = None, settle: bool = True) -> None:
    """手写一份名单: 看多 5 只每只 long_r, 其余 0; 看空 5 只 short = 0.1。"""
    longs, shorts = SYMS[:5], SYMS[-5:]
    end = (settled_end or day + timedelta(days=6)).isoformat()
    r = {s: {"long": long_r if s in longs else 0.0, "long_exit": "time", "short": 0.1 if s in shorts else 0.0,
             "short_exit": "time", "end": end} for s in SYMS}
    rec = {"contract": lr.CONTRACT, "day": day.isoformat(), "as_of": (day - timedelta(days=1)).isoformat(),
           "model": {"version": lr.MODEL_VERSION, "quarter": q, "trained_to": _trained_to(q), "rows": 1000},
           "n": len(SYMS), "missing": [], "stale": [], "long": longs, "short": shorts,
           "random": {"long": SYMS[5:10], "short": SYMS[10:15]},
           "rows": [{"symbol": s, "rank": i + 1, "atr": 2.0, "close": 100.0, "young": i == 7} for i, s in enumerate(SYMS)],
           **({"late": True} if late else {}),
           **({"settled": {"at": "x", "end": end, "r": r}} if settle else {})}
    lr._write_json(lr.day_path(tmp_path, day, sub), rec)


def _tree(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


# ── 冠军登记 ──────────────────────────────────────────────────────

def test_fresh_install_champion_is_list_day_quarter(tmp_path):
    assert lm.champion_quarter(tmp_path, date(2026, 11, 5)) == "2026Q4"
    assert not lm.registry_path(tmp_path).exists()                     # 只读调用不落盘


def test_new_quarter_list_still_comes_from_champion(tmp_path, monkeypatch):
    """换季后正式名单仍由冠军出 (改动前会直接换成新季度模型)。"""
    as_of, nd = date(2027, 1, 4), date(2027, 1, 5)
    _universe(tmp_path, as_of)
    _rec(tmp_path, date(2026, 12, 31), "2026Q4", 0.1)
    asked = []

    def fake_train(data_dir, day, **kw):
        asked.append(lr.quarter_of(day))
        return _model(lr.quarter_of(day))
    monkeypatch.setattr(lr, "train", fake_train)
    res = lr.generate(tmp_path, as_of, next_day=nd, watch=SYMS)
    assert res["status"] == "ok"
    assert asked == ["2026Q4"]
    assert lr.load_day(tmp_path, nd)["model"]["quarter"] == "2026Q4"
    assert lm.load_registry(tmp_path)["champion"] == "2026Q4"


def test_shadow_list_needs_explicit_model(tmp_path):
    with pytest.raises(ValueError):
        lr.generate(tmp_path, date(2027, 1, 4), next_day=date(2027, 1, 5), watch=SYMS, sub="2027Q1")


# ── 挑战者 ────────────────────────────────────────────────────────

def test_challenger_starts_at_new_quarter_and_writes_shadow_only(tmp_path):
    as_of, nd = date(2027, 1, 4), date(2027, 1, 5)
    _universe(tmp_path, as_of)
    _rec(tmp_path, date(2026, 12, 31), "2026Q4", 0.1)
    main_before = lr.list_days(tmp_path)
    out = lm.run_shadow(tmp_path, as_of, next_day=nd, watch=SYMS, model=_model("2027Q1", -1.0))
    assert out["challenger"] == "2027Q1" and out["decide"]["state"] == "running" and out["generate"]["status"] == "ok"
    assert lr.list_days(tmp_path) == main_before                        # 正式名单目录不多文件
    sh = lr.load_day(tmp_path, nd, "2027Q1")
    assert sh["model"]["quarter"] == "2027Q1" and len(sh["long"]) == 5
    reg = lm.load_registry(tmp_path)
    assert reg["champion"] == "2026Q4" and reg["challenger"] == "2027Q1"
    assert lm.run_shadow(tmp_path, as_of, next_day=nd, watch=SYMS, model=_model("2027Q1"))["generate"]["status"] == "exists"


def test_no_challenger_inside_champion_quarter(tmp_path):
    _universe(tmp_path, date(2026, 11, 4))
    _rec(tmp_path, date(2026, 11, 4), "2026Q4", 0.1)
    assert lm.run_shadow(tmp_path, date(2026, 11, 4), next_day=date(2026, 11, 5), watch=SYMS) == {"status": "none"}
    assert not lr.root(tmp_path, "2026Q4").exists()


def _pairs(tmp_path: Path, n: int, champ_r: float, chal_r: float, start: date = date(2027, 1, 5)) -> list[date]:
    days = [d for d in _bdays(start + timedelta(days=3 * n), 3 * n) if d >= start][:n]
    assert len(days) == n
    for d in days:
        _rec(tmp_path, d, "2026Q4", champ_r)
        _rec(tmp_path, d, "2027Q1", chal_r, sub="2027Q1")
    return days


def _start_challenger(tmp_path: Path) -> None:
    lm._save_registry(tmp_path, {"champion": "2026Q4", "challenger": "2027Q1",
                                 "history": [{"quarter": "2027Q1", "event": "challenger", "day": "2027-01-05", "champion": "2026Q4"}]})


def test_decide_waits_until_gate_n_pairs(tmp_path):
    _start_challenger(tmp_path)
    _pairs(tmp_path, lm.GATE_N - 1, 0.1, 0.5)
    assert lm.decide(tmp_path, date(2027, 2, 10)) == {"state": "running", "n": lm.GATE_N - 1}
    assert lm.load_registry(tmp_path)["challenger"] == "2027Q1"


def test_challenger_not_worse_is_promoted_and_next_list_uses_it(tmp_path, monkeypatch):
    _start_challenger(tmp_path)
    days = _pairs(tmp_path, lm.GATE_N, 0.1, 0.1)                        # 持平也算不低于
    res = lm.decide(tmp_path, days[-1] + timedelta(days=7))
    assert res["state"] == "promoted" and res["n"] == lm.GATE_N and res["chal"] == res["champ"]
    reg = lm.load_registry(tmp_path)
    assert reg["champion"] == "2027Q1" and reg["challenger"] is None
    assert reg["history"][-1]["event"] == "promoted" and reg["history"][-1]["champion"] == "2026Q4"
    asked = []

    def fake_train(data_dir, day, **kw):
        asked.append(lr.quarter_of(day))
        return _model(lr.quarter_of(day))
    monkeypatch.setattr(lr, "train", fake_train)
    assert lm.champion_model(tmp_path, days[-1] + timedelta(days=8))["meta"]["quarter"] == "2027Q1"
    assert asked == ["2027Q1"]


def test_worse_challenger_is_rejected_once_and_shadow_stops(tmp_path):
    _start_challenger(tmp_path)
    days = _pairs(tmp_path, lm.GATE_N, 0.3, 0.1)
    end = days[-1] + timedelta(days=7)
    _universe(tmp_path, end)
    out = lm.run_shadow(tmp_path, end, next_day=end + timedelta(days=1), watch=SYMS, model=_model("2027Q1"))
    assert out["decide"]["state"] == "rejected" and "generate" not in out
    reg = lm.load_registry(tmp_path)
    assert reg["champion"] == "2026Q4" and reg["challenger"] is None
    n_shadow = len(lr.list_days(tmp_path, "2027Q1"))
    assert lm.run_shadow(tmp_path, end, next_day=end + timedelta(days=1), watch=SYMS) == {"status": "none"}   # 同季度不再立
    assert len(lr.list_days(tmp_path, "2027Q1")) == n_shadow


def test_gate_ignores_late_unsettled_and_uses_first_n(tmp_path):
    _start_challenger(tmp_path)
    days = _pairs(tmp_path, lm.GATE_N, 0.1, 0.5)
    extra = days[-1] + timedelta(days=7)
    _rec(tmp_path, extra, "2026Q4", 9.0, late=True)                     # 开盘后补出的不配对
    _rec(tmp_path, extra, "2027Q1", -9.0, sub="2027Q1")
    more = extra + timedelta(days=1)
    _rec(tmp_path, more, "2026Q4", 9.0, settle=False)                   # 没结算的不配对
    _rec(tmp_path, more, "2027Q1", -9.0, sub="2027Q1")
    a, b = lm.same_window(tmp_path, "2027Q1")
    assert len(a) == len(b) == lm.GATE_N
    assert lm.decide(tmp_path, more)["state"] == "promoted"


# ── 特征漂移 ──────────────────────────────────────────────────────

def test_psi_zero_for_same_distribution_large_for_shift_none_when_thin():
    rng = np.random.default_rng(1)
    ref = rng.normal(0, 1, 5000)
    assert lm.psi(ref, rng.normal(0, 1, 2000)) < 0.03
    assert lm.psi(ref, rng.normal(1.0, 1, 2000)) > lm.PSI_WARN
    assert lm.psi(ref, rng.normal(0, 1, lm.MIN_CUR - 1)) is None
    assert lm.psi(ref[: lm.MIN_REF - 1], ref) is None
    assert lm.psi(np.r_[ref, np.nan, np.inf], np.r_[ref[:500], np.nan]) is not None      # 缺值 / 无穷不参与


def test_psi_survives_ties_and_empty_bins():
    ref = np.r_[np.zeros(900), np.ones(100)]
    v = lm.psi(ref, np.ones(200))
    assert v is not None and np.isfinite(v) and v > lm.PSI_WARN


def test_compute_drift_writes_eight_features_and_status_reads_it(tmp_path):
    as_of = date(2026, 11, 16)
    _universe(tmp_path, as_of, n_days=420)
    _rec(tmp_path, date(2026, 11, 17), "2026Q4", 0.1, settle=False)
    d = lm.compute_drift(tmp_path, as_of, watch=SYMS)
    assert [r["key"] for r in d["rows"]] == list(lr.FEATURES)
    assert d["ref"]["to"] <= "2026-09-22" < d["cur"]["from"] and d["cur"]["to"] == as_of.isoformat()
    assert all(0 <= r["psi"] < 5 for r in d["rows"])
    # 注意线按特征各算: 不低于 PSI_WARN, 且不低于参照窗里任何一个 20 日小窗的 PSI; 超过它才记注意
    assert d["windows"] >= lm.MIN_BASE_WINDOWS
    for r in d["rows"]:
        assert r["line"] == max(lm.PSI_WARN, r["base"]) and r["warn"] == (r["psi"] >= r["line"])
    st = lm.status(tmp_path, today=date(2026, 11, 17))
    assert st["drift"]["rows"] == d["rows"] and st["drift"]["warn"] == lm.PSI_WARN


def test_drift_line_is_the_features_own_yearly_high_not_a_fixed_number(tmp_path, monkeypatch):
    """滚动均值类特征一个月对全年的 PSI 本来就高: 固定 0.25 会常年报注意。"""
    as_of = date(2026, 11, 16)
    _universe(tmp_path, as_of, n_days=420)
    _rec(tmp_path, date(2026, 11, 17), "2026Q4", 0.1, settle=False)
    real = lm.psi
    cycle = lm.compute_drift(tmp_path, as_of, watch=SYMS)["windows"] + 1      # 每个特征: 1 次当前窗 + 各基线小窗
    calls = {"n": 0}

    def fake(ref, cur, bins=lm.PSI_BINS):
        calls["n"] += 1
        # 每个特征先算当前窗 (第 1 次), 再算各基线小窗: 当前 0.5, 基线最高 0.9
        k = (calls["n"] - 1) % cycle
        return 0.5 if k == 0 else (0.9 if k == 3 else 0.1)
    monkeypatch.setattr(lm, "psi", fake)
    d = lm.compute_drift(tmp_path, as_of, watch=SYMS)
    assert d["windows"] == cycle - 1 >= lm.MIN_BASE_WINDOWS and calls["n"] == cycle * len(lr.FEATURES)
    assert all(r == {"key": r["key"], "psi": 0.5, "base": 0.9, "line": 0.9, "warn": False} for r in d["rows"])   # 0.5 > 0.25 但没超过自己的年内高点
    calls["n"] = 0
    monkeypatch.setattr(lm, "psi", lambda ref, cur, bins=lm.PSI_BINS: (calls.__setitem__("n", calls["n"] + 1), 0.3 if (calls["n"] - 1) % cycle == 0 else 0.05)[1])
    d = lm.compute_drift(tmp_path, as_of, watch=SYMS)
    assert all(r["base"] == 0.05 and r["line"] == lm.PSI_WARN and r["warn"] is True for r in d["rows"])          # 基线很低时仍以 0.25 为底
    assert real is not lm.psi


def test_compute_drift_none_without_lists(tmp_path):
    assert lm.compute_drift(tmp_path, date(2026, 11, 16), watch=SYMS) is None
    assert not lm.drift_path(tmp_path).exists()


# ── 页面段 ────────────────────────────────────────────────────────

def test_status_none_without_lists_and_read_only(tmp_path):
    assert lm.status(tmp_path, today=date(2026, 11, 17)) is None
    _rec(tmp_path, date(2026, 11, 6), "2026Q4", 0.2)
    _rec(tmp_path, date(2026, 11, 17), "2026Q4", 0.2, settle=False)
    before = _tree(tmp_path)
    st = lm.status(tmp_path, today=date(2026, 11, 17))
    assert _tree(tmp_path) == before
    assert st["champion"] == {"quarter": "2026Q4", "name": "GBM 2026Q4", "trained_to": "2026-09-22", "rows": 1000, "since": "2026-11-06"}
    assert st["forward"]["days"] == 1 and st["forward"]["failed"] is None and st["forward"]["check_days"] == lr.CHECK_DAYS
    assert st["forward"]["total"]["long_minus_all"] == 0.15            # 0.2 − 全体均值 0.05
    assert st["coverage"] == {"ranked": 20, "young": 1, "missing": 0, "stale": 0, "total": 20}
    assert st["next_retrain"] == {"quarter": "2027Q1", "train_end": "2026-12-24"}
    assert st["challenger"] is None and st["drift"] is None and st["post"] is None
    assert [(r["quarter"], r["role"], r["days"]) for r in st["registry"]] == [("2026Q4", "champion", 1)]


def test_status_fail_criterion_only_after_check_days(tmp_path):
    days = _bdays(date(2027, 3, 1), lr.CHECK_DAYS)
    for d in days[:-1]:
        _rec(tmp_path, d, "2026Q4", -0.2)
    st = lm.status(tmp_path, today=days[-1] + timedelta(days=9))
    assert st["forward"]["days"] == lr.CHECK_DAYS - 1 and st["forward"]["failed"] is None
    _rec(tmp_path, days[-1], "2026Q4", -0.2)
    st = lm.status(tmp_path, today=days[-1] + timedelta(days=9))
    assert st["forward"]["days"] == lr.CHECK_DAYS and st["forward"]["failed"] is True


def test_status_asof_only_counts_what_was_settled_by_then(tmp_path):
    d1, d2, d3 = date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10)
    _rec(tmp_path, d1, "2026Q4", 0.2, settled_end=date(2026, 11, 12))
    _rec(tmp_path, d2, "2026Q4", 0.4, settled_end=date(2026, 11, 13))
    _rec(tmp_path, d3, "2026Q4", 0.6, settled_end=date(2026, 11, 16))
    _rec(tmp_path, date(2026, 11, 17), "2026Q4", 0.0, settle=False)
    now = lm.status(tmp_path, today=date(2026, 11, 17))
    assert now["forward"]["days"] == 3 and now["list_day"] == "2026-11-17" and now["post"] is None
    then = lm.status(tmp_path, today=date(2026, 11, 17), asof=d3)
    assert then["list_day"] == "2026-11-10" and then["asof"] == "2026-11-10"
    assert then["forward"]["days"] == 0 and then["forward"]["total"] is None      # 11-10 时一份都还没到期
    assert then["post"] == {"day": "2026-11-10", "end": "2026-11-16", "long": 0.6, "short": 0.1}
    mid = lm.status(tmp_path, today=date(2026, 11, 17), asof=date(2026, 11, 12))
    assert mid["forward"]["days"] == 1 and mid["list_day"] == "2026-11-10" and mid["post"] is None   # 11-12 没有写给当天的名单


def test_status_challenger_states_and_asof_hides_later_verdict(tmp_path):
    _start_challenger(tmp_path)
    days = _pairs(tmp_path, lm.GATE_N, 0.3, 0.1)
    end = days[-1] + timedelta(days=7)
    run = lm.status(tmp_path, today=end)
    c = run["challenger"]
    assert c["state"] == "running" and c["n"] == lm.GATE_N and c["gate_n"] == lm.GATE_N and c["lists"] == lm.GATE_N
    assert c["champ"]["long_minus_all"] == 0.225 and c["chal"]["long_minus_all"] == 0.075
    assert [r["role"] for r in run["registry"]] == ["champion", "challenger"]
    lm.decide(tmp_path, end)
    done = lm.status(tmp_path, today=end + timedelta(days=1))
    assert done["challenger"]["state"] == "rejected" and done["challenger"]["decided"] == end.isoformat()
    assert done["champion"]["quarter"] == "2026Q4" and done["registry"][-1]["role"] == "rejected"
    early = lm.status(tmp_path, today=end + timedelta(days=1), asof=days[9])
    assert early["challenger"]["state"] == "running" and early["challenger"]["decided"] is None
    assert early["challenger"]["n"] < lm.GATE_N


def test_run_post_close_isolates_failures(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(lm, "run_shadow", boom)
    monkeypatch.setattr(lm, "compute_drift", lambda *a, **k: None)
    assert lm.run_post_close(tmp_path, date(2026, 11, 16)) == {"shadow": {"error": "RuntimeError"}, "drift": None}


def test_ls_rank_post_close_calls_model_step_softly(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "settle", lambda *a, **k: {"settled": 0})
    monkeypatch.setattr(lr, "generate", lambda *a, **k: {"status": "exists"})
    monkeypatch.setattr(lm, "run_post_close", lambda data_dir, day: {"shadow": {"status": "none"}, "drift": None})
    assert lr.run_post_close(tmp_path, date(2026, 11, 16))["model"] == {"shadow": {"status": "none"}, "drift": None}

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(lm, "run_post_close", boom)
    out = lr.run_post_close(tmp_path, date(2026, 11, 16))
    assert out["model"] == {"error": "RuntimeError"} and out["generate"] == {"status": "exists"}


def test_not_in_trading_chain():
    src_txt = Path(lm.__file__).read_text(encoding="utf-8")
    for bad in ("app.ledger", "notify", "trading_loop", "paper_order", "alpaca"):
        assert bad not in src_txt
    assert json.dumps(lm.load_registry(Path("/nonexistent"))) == json.dumps({"champion": None, "challenger": None, "history": []})
