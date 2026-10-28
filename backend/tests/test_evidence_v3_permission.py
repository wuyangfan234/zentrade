"""v3 许可链 (2026-10-05): evidence_mode=v3 时只认 v3.1 结论 + 前向确认; 缺文件 fail-closed; 指纹变动只展示;
前向确认按预登记第 12 节自动晋级/回落; 切换 API 需要已完成的 v3 评估。"""
import json
from datetime import date, timedelta

import pytest

from app.services import decision_desk as dd
from app.services import decision_scan_job as job
from app.services import forward_confirmation as fc
from app.services import shadow_forward as sf
from app.services import strategy_evaluation as se


def _v3(**sources):
    return {"contract": "v3.1-calibrated-2026-10-05", "generated_at": "2026-10-05T23:00:00+0800", "evaluated": list(sources),
            "sources": sources}


def _src(tier="negative", ex=0.05, hold=0.05, n=3000, reason="r"):
    return {"n": n, "mean": 0.1, "excess": {"n": n, "mean": ex, "p": 0.2}, "holdout": {"n": 1000, "mean": hold},
            "tier_v3": tier, "reason_v3": reason, "kind": "strategy"}


def _setup(tmp_path, payload, mode="v3"):
    d = tmp_path / "decision_desk"
    d.mkdir(parents=True, exist_ok=True)
    (d / "evaluation_v3.json").write_text(json.dumps(payload))
    (d / "evaluation.json").write_text(json.dumps({"sources": {"good": {"n": 1800, "mean": 0.13, "ci_low": 0.02}}}))
    (d / "evidence_mode.json").write_text(json.dumps({"mode": mode}))
    reg = dd.SourceRegistry(tmp_path)
    for s in ("good", "llsr", "bad"):
        reg.set_status(s, "enabled")
    return reg


def test_v3_mode_uses_only_v3_verdicts_and_fails_closed_for_unevaluated_sources(tmp_path):
    _setup(tmp_path, _v3(llsr=_src("negative", reason="同时检验多个来源后…只展示不开仓"), bad=_src("proven", ex=0.08)))
    assert job.source_permission(tmp_path, "bad")["tradable"] is True
    assert job.source_permission(tmp_path, "bad")["risk_pct"] == dd.PROVEN_RISK_PCT
    p = job.source_permission(tmp_path, "llsr")
    assert p["tradable"] is False and "只展示" in p["reason"]
    # v1 里是“证据为正”的 good, 在 v3 模式下没有 v3 证据 → 只展示 (不回退旧尺子)
    assert job.source_permission(tmp_path, "good")["tradable"] is False


def test_v3_mode_with_missing_file_blocks_everything_instead_of_falling_back_to_v1(tmp_path):
    _setup(tmp_path, _v3())
    (tmp_path / "decision_desk" / "evaluation_v3.json").unlink()
    ev = se.load_evaluation(tmp_path)
    assert ev["sources"] == {} and ev["v3_missing"] is True
    assert job.source_permission(tmp_path, "good")["tradable"] is False


def test_v3_stale_fingerprint_blocks(tmp_path, monkeypatch):
    src = _src("proven", ex=0.08)
    src["fingerprint"] = {"hash": "aaa"}
    _setup(tmp_path, _v3(bad=src))
    monkeypatch.setattr(se, "current_fingerprints", lambda d, sids, contract=None: {s: "bbb" for s in sids})
    p = job.source_permission(tmp_path, "bad")
    assert p["tradable"] is False and "改过" in p["reason"]


def _shadow(tmp_path, source, n, months, r=0.3, base=0.0, stop=-0.06):
    rows = []
    d0 = fc.FORWARD_START
    for i in range(n):
        day = (d0 + timedelta(days=int(i * months * 30 / n))).isoformat()
        rows.append({"as_of": day, "source": source, "symbol": f"S{i}", "stop_loss": stop,
                     "outcome": {"status": "closed", "r": r + (0.5 if i % 2 else -0.5)}})
        rows.append({"as_of": day, "source": sf.BASELINE_ID, "symbol": f"B{i}", "stop_loss": stop,
                     "outcome": {"status": "closed", "r": base}})
    p = sf.path(tmp_path)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_forward_confirmation_promotes_only_preregistered_candidates_meeting_all_bars(tmp_path):
    v3 = _v3(llsr=_src("negative", ex=0.07, hold=0.10), bad=_src("negative", ex=-0.02, hold=0.05))
    _setup(tmp_path, v3)
    assert fc.candidates(v3) == ["llsr"]
    _shadow(tmp_path, "llsr", 200, 7)
    st = fc.status(tmp_path, v3)["llsr"]
    assert st["candidate"] and not st["promoted"] and st["n"] == 200          # 样本不够
    _shadow(tmp_path, "llsr", 400, 4)
    assert not fc.status(tmp_path, v3)["llsr"]["promoted"]                    # 月份不够
    _shadow(tmp_path, "llsr", 400, 7)
    st = fc.status(tmp_path, v3)["llsr"]
    assert st["promoted"] and st["p"] < st["threshold"]
    p = job.source_permission(tmp_path, "llsr")
    assert p["tradable"] is True and p["basis"] == "forward" and "前向确认通过" in p["reason"]
    # 不在候选集合里的来源, 前向再好也不晋级
    _shadow(tmp_path, "bad", 400, 7)
    assert not fc.status(tmp_path, v3)["bad"]["promoted"]


def test_forward_ignores_rows_before_start_and_without_same_day_baseline(tmp_path):
    v3 = _v3(llsr=_src("negative", ex=0.07, hold=0.10))
    _setup(tmp_path, v3)
    early = (fc.FORWARD_START - timedelta(days=3)).isoformat()
    rows = [{"as_of": early, "source": "llsr", "symbol": "A", "stop_loss": -0.06, "outcome": {"status": "closed", "r": 2.0}},
            {"as_of": early, "source": sf.BASELINE_ID, "symbol": "B", "stop_loss": -0.06, "outcome": {"status": "closed", "r": 0}},
            {"as_of": fc.FORWARD_START.isoformat(), "source": "llsr", "symbol": "C", "stop_loss": -0.08,
             "outcome": {"status": "closed", "r": 1.0}}]
    sf.path(tmp_path).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert fc.status(tmp_path, v3)["llsr"]["n"] == 0


def test_v1_and_v2_modes_unchanged(tmp_path):
    _setup(tmp_path, _v3(good=_src("negative")), mode="v1")
    assert job.source_permission(tmp_path, "good")["tradable"] is True    # v1 证据为正
    _setup(tmp_path, _v3(good=_src("negative")), mode="bogus")
    assert se.evidence_mode(tmp_path) == "v1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from app.api import opportunities as api
    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.opportunity_service = object()
    return TestClient(app)


def test_switch_to_v3_requires_completed_v3_and_is_audited(client, tmp_path):
    (tmp_path / "decision_desk").mkdir()
    r = client.post("/api/opportunities/desk/evidence/mode", json={"mode": "v3"})
    assert r.status_code == 409
    (tmp_path / "decision_desk" / "evaluation_v3.json").write_text(json.dumps(_v3(llsr=_src())))
    r = client.post("/api/opportunities/desk/evidence/mode", json={"mode": "v3", "note": "全权委托"})
    assert r.status_code == 200 and r.json()["mode"] == "v3"
    audit = [json.loads(x) for x in (tmp_path / "decision_desk" / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["action"] == "evidence_mode" and audit[-1]["value"] == "v3" and audit[-1]["previous"] == "v1"
    body = client.get("/api/opportunities/desk/evidence").json()
    assert body["mode"] == "v3" and body["v3"]["sources"][0]["source"] == "llsr"
    assert body["v3"]["sources"][0]["forward"]["candidate"] is True


@pytest.mark.parametrize("bad", [{"n": "3000"}, {"n": float("nan")}, {"n": True}, {"excess": {"mean": "0.08"}},
                                 {"excess": {"mean": -0.01}}, {"excess": None}, {"n": 0}])
def test_v3_malformed_numbers_never_grant_permission(tmp_path, bad):
    src = {**_src("proven", ex=0.08), **bad}
    _setup(tmp_path, _v3(bad_src=src))
    assert job.source_permission(tmp_path, "bad_src")["tradable"] is False


def test_v3_forward_confirmation_exception_is_fail_closed(tmp_path, monkeypatch):
    _setup(tmp_path, _v3(llsr=_src("negative", ex=0.07, hold=0.1), bad=_src("proven", ex=0.08)))
    monkeypatch.setattr(fc, "status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert job.source_permission(tmp_path, "llsr")["tradable"] is False
    assert job.source_permission(tmp_path, "bad")["tradable"] is True          # 历史证据通过的不受前向故障影响


def test_forged_forward_promotion_without_positive_numbers_is_ignored(tmp_path):
    src = {**_src("negative"), "forward": {"promoted": True, "n": "400", "excess": 0.1}}
    from app.services import decision_desk as _dd
    assert _dd.source_weight({**src, "v3": True})["tier"] == "negative"


def test_legacy_s1s2_default_does_not_apply_in_v3_mode(tmp_path):
    """v1/注册表/权重/运行记录都丢失时, 旧版 S1/S2 兼容默认 (1% 风险) 只在 v1 模式成立; v3 模式下 S1/S2 只展示。"""
    d = tmp_path / "decision_desk"
    d.mkdir()
    (d / "evaluation_v3.json").write_text(json.dumps(_v3(llsr=_src())))
    (d / "evidence_mode.json").write_text(json.dumps({"mode": "v3"}))
    assert job.source_permission(tmp_path, "breakout")["tradable"] is False
    (d / "evidence_mode.json").write_text(json.dumps({"mode": "v1"}))
    (d / "evaluation_v3.json").unlink()
    assert job.source_permission(tmp_path, "breakout")["tradable"] is True      # v1 旧兼容不变


def test_history_proven_without_delisted_names_is_not_granted(tmp_path):
    payload = _v3(bad=_src("proven", ex=0.08))
    payload["with_delisted"] = False
    _setup(tmp_path, payload)
    p = job.source_permission(tmp_path, "bad")
    assert p["tradable"] is False and "退市" in p["reason"]


def test_non_dict_fingerprint_does_not_raise(tmp_path):
    src = {**_src("proven", ex=0.08), "fingerprint": "garbage"}
    _setup(tmp_path, _v3(bad=src))
    assert job.source_permission(tmp_path, "bad")["tradable"] is True       # 无法校验版本 → 沿用现有“算不出不拦”语义


def test_v3_evidence_is_cached_but_reflects_file_changes_immediately(tmp_path, monkeypatch):
    """盘中每 15 秒每张卡都问许可: v3 证据按文件签名缓存; 证据文件或影子账本一变立即重算; 返回值改动不污染缓存。"""
    calls = []
    real = se._load_v3_uncached
    monkeypatch.setattr(se, "_load_v3_uncached", lambda d: calls.append(1) or real(d))
    se._V3_CACHE.clear()
    _setup(tmp_path, _v3(bad=_src("proven", ex=0.08)))
    for _ in range(5):
        assert job.source_permission(tmp_path, "bad")["tradable"] is True
    assert len(calls) == 1
    ev = se.load_evaluation(tmp_path)
    ev["sources"]["bad"]["tier_v3"] = "negative"                 # 调用方改返回值
    assert job.source_permission(tmp_path, "bad")["tradable"] is True
    import time as _t
    _t.sleep(0.01)
    (tmp_path / "decision_desk" / "evaluation_v3.json").write_text(json.dumps(_v3(bad=_src("negative"))))
    assert job.source_permission(tmp_path, "bad")["tradable"] is False   # 文件变化立即生效
    assert len(calls) == 2
