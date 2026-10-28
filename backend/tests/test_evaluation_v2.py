"""拍板 3 (2026-10-05): v2 新口径证据与现行 v1 并行, 确认后再切换。"""
import json

import numpy as np
import pytest

from app.services import decision_desk as dd
from app.services import strategy_evaluation as se
from app.services import strategy_evaluation_runner as runner

ZONE, INVAL, TARGET, RISK = (100.0, 101.0), 95.0, (112.0, 114.0), 5.5


def arr(*rows):
    o, h, l, c = (np.array([r[k] for r in rows], dtype=float) for k in range(4))
    return o, h, l, c


def test_near_target_then_invalidation_is_half_profit_half_breakeven_not_full_win():
    # 成交 → 次日触近端 (112) → 之后收盘跌破保本 → 次日开盘离场
    o, h, l, c = arr((100.5, 101, 99.5, 100.6), (101, 112.5, 100.8, 110), (108, 109, 99, 99.5), (99, 99.5, 97, 98))
    v1 = se.simulate_7d(o, h, l, c, entry_zone=ZONE, invalidation=INVAL, target=TARGET, risk=RISK)
    v2 = se.simulate_native_daily(o, h, l, c, entry_zone=ZONE, invalidation=INVAL, target=TARGET, risk=RISK)
    assert v1["exit_reason"] == "target_zone_lower" and v1["r"] > 1.9      # 旧口径: 近端全平记大赚
    assert v2["status"] == "closed" and v2["partial"] is True
    # 峰值已过 2R → 止损抬到 +1R (与生产 _move_stop 同); 收盘跌破 → 次日开盘卖剩下一半
    assert v2["exit_reason"] == "trailing_stop"
    assert 0.5 < v2["r"] < v1["r"]                                           # 一半在 112, 一半在 99 开盘
    assert [x["reason"] for x in v2["legs"]] == ["target_zone_lower", "trailing_stop"]


def test_far_target_exits_rest_and_full_two_leg_r():
    o, h, l, c = arr((100.5, 101, 99.5, 100.6), (101, 114.5, 100.8, 113.5))
    v2 = se.simulate_native_daily(o, h, l, c, entry_zone=ZONE, invalidation=INVAL, target=TARGET, risk=RISK)
    assert v2["exit_reason"] == "target_zone_upper" and v2["partial"] is True
    expected = (0.5 * (112 * (1 - se.SLIP)) + 0.5 * (114 * (1 - se.SLIP)) - 100.5 * (1 + se.SLIP)) / RISK
    assert v2["r"] == pytest.approx(expected - se.FEE_R(RISK, 100.5 * (1 + se.SLIP)) * 1.5, abs=1e-3)


def test_gap_down_fill_day_high_before_fill_is_not_a_target_hit():
    # 开盘 102.5 (高于入场区, 未超 2%) 冲到 113 后回落到 101 成交: 日内高点发生在成交前, 不能算触及目标
    o, h, l, c = arr((102.5, 113, 100.8, 101.2), (101.2, 102, 100.5, 101.5))
    v1 = se.simulate_7d(o, h, l, c, entry_zone=ZONE, invalidation=INVAL, target=TARGET, risk=RISK)
    v2 = se.simulate_native_daily(o, h, l, c, entry_zone=ZONE, invalidation=INVAL, target=TARGET, risk=RISK)
    assert v1["status"] == "closed" and v1["exit_reason"] == "target_zone_lower"   # 旧口径的已知偏差
    assert v2["status"] == "open_at_end"                                           # 两天数据不够走完, 不虚构结局


def test_v1_and_v2_agree_on_plain_invalidation():
    o, h, l, c = arr((100.5, 101, 99.5, 100.6), (100, 100.5, 93, 94), (93.5, 94, 92, 93))
    v1 = se.simulate_7d(o, h, l, c, entry_zone=ZONE, invalidation=INVAL, target=TARGET, risk=RISK)
    v2 = se.simulate_native_daily(o, h, l, c, entry_zone=ZONE, invalidation=INVAL, target=TARGET, risk=RISK)
    assert v1["r"] == pytest.approx(v2["r"]) and v2["exit_reason"] == "invalidation"


def _write(tmp_path, name, payload):
    p = tmp_path / "decision_desk" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload))


def test_default_mode_is_v1_and_v2_file_never_changes_permissions(tmp_path):
    _write(tmp_path, "evaluation.json", {"sources": {"a": {"n": 900, "mean": 0.1, "ci_low": 0.02}}})
    _write(tmp_path, "evaluation_v2.json", {"sources": {"a": {"n": 900, "mean": -0.1, "ci_low": -0.2}}})
    assert se.evidence_mode(tmp_path) == "v1"
    assert se.load_evaluation(tmp_path)["sources"]["a"]["ci_low"] == 0.02


def test_v2_mode_uses_v2_and_carries_uncovered_sources_from_v1(tmp_path):
    _write(tmp_path, "evaluation.json", {"sources": {"a": {"n": 900, "mean": 0.1, "ci_low": 0.02},
                                                     "breakout": {"n": 1395, "mean": -0.077, "ci_low": -0.152}}})
    _write(tmp_path, "evaluation_v2.json", {"sources": {"a": {"n": 900, "mean": -0.1, "ci_low": -0.2}}})
    _write(tmp_path, "evidence_mode.json", {"mode": "v2"})
    ev = se.load_evaluation(tmp_path)
    assert ev["sources"]["a"]["ci_low"] == -0.2
    assert ev["sources"]["breakout"]["carried_from"] == "v1"


def test_v2_mode_with_missing_v2_file_falls_back_to_v1(tmp_path):
    _write(tmp_path, "evaluation.json", {"sources": {"a": {"n": 900, "mean": 0.1, "ci_low": 0.02}}})
    _write(tmp_path, "evidence_mode.json", {"mode": "v2"})
    assert se.load_evaluation(tmp_path)["sources"]["a"]["ci_low"] == 0.02


def test_compare_versions_flags_permission_change():
    cmp = runner.compare_versions({"sources": {"a": {"n": 900, "mean": 0.1, "ci_low": 0.02}, "b": {"n": 900, "mean": 0, "ci_low": -0.1}}},
                                  {"sources": {"a": {"n": 900, "mean": -0.1, "ci_low": -0.2}, "b": {"n": 900, "mean": 0, "ci_low": -0.05}}})
    assert cmp["a"]["changes_permission"] is True and cmp["a"]["tier_v1"] == "proven" and cmp["a"]["tier_v2"] == "negative"
    assert cmp["b"]["changes_permission"] is False


def test_fingerprint_changes_with_saved_params(tmp_path):
    from types import SimpleNamespace
    code = tmp_path / "s.py"
    code.write_text("X = 1")
    ov = {"params": {"cutoff": 10}}
    eng = SimpleNamespace(get=lambda sid: SimpleNamespace(file_path=code), _override_loader=lambda sid: ov)
    a = runner.strategy_fingerprint(eng, "s")["hash"]
    ov["params"]["cutoff"] = 11
    b = runner.strategy_fingerprint(eng, "s")["hash"]
    ov["display_limit"] = 5                       # 只影响展示条数, 不算策略变化
    c = runner.strategy_fingerprint(eng, "s")["hash"]
    code.write_text("X = 2")
    d = runner.strategy_fingerprint(eng, "s")["hash"]
    assert a != b and b == c and c != d


def test_v2_uses_saved_params_in_signal_path(monkeypatch):
    from types import SimpleNamespace
    seen = {}

    class Svc:
        def _normalize_params(self, params, s):
            seen["params"] = params
            raise RuntimeError("stop here")
    eng = SimpleNamespace(get=lambda sid: SimpleNamespace(), _override_loader=lambda sid: {"params": {"cutoff": 110}})
    with pytest.raises(RuntimeError):
        runner._signals_for("s", Svc(), eng, None, None)
    assert seen["params"] == {"cutoff": 110}


def test_stale_fingerprint_in_v2_mode_blocks_source(tmp_path, monkeypatch):
    _write(tmp_path, "evaluation.json", {"sources": {}})
    _write(tmp_path, "evaluation_v2.json", {"sources": {"a": {"n": 900, "mean": 0.2, "ci_low": 0.05,
                                                              "fingerprint": {"hash": "old"}}}})
    _write(tmp_path, "evidence_mode.json", {"mode": "v2"})
    monkeypatch.setattr(se, "current_fingerprints", lambda d, sids: {"a": "new"})
    ev = se.load_evaluation(tmp_path)["sources"]["a"]
    w = dd.source_weight(ev)
    assert ev["stale_version"] == {"evaluated": "old", "current": "new"}
    assert w["tier"] == "negative" and "改过" in w["reason"]
    monkeypatch.setattr(se, "current_fingerprints", lambda d, sids: {"a": "old"})
    assert dd.source_weight(se.load_evaluation(tmp_path)["sources"]["a"])["tier"] == "proven"
    monkeypatch.setattr(se, "current_fingerprints", lambda d, sids: {"a": None})   # 算不出指纹: 不据此拦截
    assert dd.source_weight(se.load_evaluation(tmp_path)["sources"]["a"])["tier"] == "proven"


def _client(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import opportunities as api
    from app.market_clock import us_clock
    monkeypatch.setattr(us_clock, "in_session", lambda *a: False)
    monkeypatch.setattr(se, "current_fingerprints", lambda d, sids: {s: "h1" for s in sids})
    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def test_evidence_api_compares_and_switch_requires_finished_v2(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    _write(tmp_path, "evaluation.json", {"generated_at": "t1", "sources": {"a": {"n": 900, "mean": 0.1, "ci_low": 0.02}}})
    r = c.get("/api/opportunities/desk/evidence").json()
    assert r["mode"] == "v1" and r["v2"] is None and r["comparison"] == {}
    assert c.post("/api/opportunities/desk/evidence/mode", json={"mode": "v2"}).status_code == 409
    assert se.evidence_mode(tmp_path) == "v1"
    _write(tmp_path, "evaluation_v2.json", {"generated_at": "t2", "evaluated": ["a"],
                                            "sources": {"a": {"n": 900, "mean": -0.1, "ci_low": -0.2, "fingerprint": {"hash": "h1"}}}})
    r = c.get("/api/opportunities/desk/evidence").json()
    assert r["permission_changes"] == ["a"] and r["comparison"]["a"]["version"] == "match"
    ok = c.post("/api/opportunities/desk/evidence/mode", json={"mode": "v2", "note": "核对完成"})
    assert ok.status_code == 200 and ok.json() == {"ok": True, "mode": "v2", "previous": "v1"}
    audit = [json.loads(x) for x in (tmp_path / "decision_desk" / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["action"] == "evidence_mode" and audit[-1]["value"] == "v2" and audit[-1]["previous"] == "v1"
    assert c.post("/api/opportunities/desk/evidence/mode", json={"mode": "v1"}).json()["mode"] == "v1"
    assert c.post("/api/opportunities/desk/evidence/mode", json={"mode": "v9"}).status_code == 422
    assert c.post("/api/opportunities/desk/evidence/mode", json={"mode": "v3"}).status_code == 409  # 尚无 v3 评估
