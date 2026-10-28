"""QVeris API: 只读不花钱、refresh 才调用、无 Key 400、超上限 429、数据源失败 502、上限设置、密钥不出现在响应、
插件清单被设置页识别为可配置 Key 的数据源。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import secrets_store
from app.api import qveris as qapi
from app.plugins.qveris import provider as qp
from app.services import qveris_data as qd
from app.services import qveris_gate as qg

CHAIN = json.loads((Path(__file__).parent / "fixtures" / "qveris_nvda_chain_20261001.json").read_text())
FX = json.loads((Path(__file__).parent / "fixtures" / "qveris_earnings_20261002.json").read_text())


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = {"qveris_api_key": "sk-secret-value-123456"}
    monkeypatch.setattr(secrets_store, "load", lambda: store)
    monkeypatch.setattr(secrets_store, "save", lambda u: store.update({k: v for k, v in u.items() if v is not None}) or store)
    monkeypatch.setattr(qp, "credits", lambda key=None: {"remaining": 9737.23, "purchased_total": 65000.0, "daily_free_remaining": 0.0})
    qg._balance_cache.update(at=0.0, value=None, error=None)
    monkeypatch.setattr(qd, "spot_price", lambda repo, s: (CHAIN["spot"], "2026-10-01 收盘"))
    calls = []

    def fake_exec(kind, params):
        calls.append((kind, params))
        data = {"options_chain": {"data": CHAIN["data"]}, "earnings_estimates": FX["estimates"],
                "earnings_transcript": FX["transcript"]}[kind]
        return {"data": data, "charged": 1.0, "execution_id": f"e{len(calls)}", "elapsed_ms": 10}
    monkeypatch.setattr(qp, "execute", fake_exec)
    app = FastAPI()
    app.include_router(qapi.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app), calls, store


def test_read_only_requests_do_not_spend(env):
    c, calls, _ = env
    for path in ("/api/qveris/options?symbol=NVDA", "/api/qveris/estimates?symbol=LITE", "/api/qveris/transcript?symbol=MU&quarter=2026Q3"):
        r = c.get(path)
        assert r.status_code == 200 and r.json()["available"] is False and r.json()["cost"] == 1.0
    assert calls == []


def test_refresh_spends_once_then_cache_is_free(env, monkeypatch):
    c, calls, _ = env
    # 固定为非盘中 (周六): 收盘后的链直到下次开盘都有效 → 第二次必走缓存
    monkeypatch.setattr(qd, "options_fresh", lambda entry, now=None: True)
    r = c.get("/api/qveris/options", params={"symbol": "nvda", "refresh": 1}).json()
    assert r["available"] and r["charged"] == 1.0 and not r["cached"] and r["analysis"]["ok"]
    assert len(calls) == 1 and calls[0][1]["require_greeks"] == "true"
    r2 = c.get("/api/qveris/options", params={"symbol": "NVDA", "refresh": 1}).json()
    assert r2["cached"] and r2["charged"] == 0.0 and len(calls) == 1
    u = c.get("/api/qveris/usage").json()
    assert u["balance"]["remaining"] == 9737.23 and u["daily_limit"] == 100.0 and u["spent_today"] == 1.0


def test_quota_maps_to_429_and_does_not_call(env):
    c, calls, store = env
    assert c.post("/api/qveris/limit", json={"daily_limit": 1}).json()["daily_limit"] == 1.0
    assert c.get("/api/qveris/estimates", params={"symbol": "LITE", "refresh": 1}).status_code == 200
    r = c.get("/api/qveris/estimates", params={"symbol": "NVDA", "refresh": 1})
    assert r.status_code == 429 and "已拒绝" in r.json()["detail"] and len(calls) == 1


def test_missing_key_is_400_and_source_failure_is_502(env, monkeypatch):
    c, calls, store = env
    store.pop("qveris_api_key")
    monkeypatch.delenv("QVERIS_API_KEY", raising=False)
    assert c.get("/api/qveris/options", params={"symbol": "NVDA", "refresh": 1}).status_code == 400
    store["qveris_api_key"] = "sk-x"

    def down(kind, params):
        raise qp.QverisError("HTTP 503", status=503, charged=0.0)
    monkeypatch.setattr(qp, "execute", down)
    r = c.get("/api/qveris/estimates", params={"symbol": "LITE", "refresh": 1})
    assert r.status_code == 502 and "QVeris 调用失败" in r.json()["detail"]


def test_validation_and_quarter_default(env):
    c, _, _ = env
    assert c.get("/api/qveris/options", params={"symbol": "BAD SYM"}).status_code == 422
    assert c.get("/api/qveris/transcript", params={"symbol": "MU", "quarter": "2026Q5"}).status_code == 422
    r = c.get("/api/qveris/transcript", params={"symbol": "MU"}).json()
    assert len(r["quarter"]) == 6 and r["quarter"][4] == "Q"
    assert c.post("/api/qveris/limit", json={"daily_limit": -5}).status_code == 422


def test_transcript_and_digest_flow(env, monkeypatch):
    c, calls, _ = env
    r = c.get("/api/qveris/transcript", params={"symbol": "MU", "quarter": "2026Q3", "refresh": 1}).json()
    assert r["available"] and len(r["segments"]) == 4
    assert c.post("/api/qveris/transcript/digest", json={"symbol": "MU", "quarter": "2026Q2"}).status_code == 404

    async def fake_ai(messages, **kw):
        return "- 要点一\n- 要点二"
    import app.services.ai_provider as ap
    monkeypatch.setattr(ap, "generate_ai_text", fake_ai)
    d = c.post("/api/qveris/transcript/digest", json={"symbol": "MU", "quarter": "2026Q3"}).json()
    assert d["digest"]["points"] == ["要点一", "要点二"]
    assert len(calls) == 1  # 提炼不花 QVeris 积分


def test_secret_never_in_responses(env):
    c, _, _ = env
    c.get("/api/qveris/estimates", params={"symbol": "LITE", "refresh": 1})
    for path in ("/api/qveris/usage", "/api/qveris/estimates?symbol=LITE", "/api/qveris/auto"):
        assert "sk-secret-value-123456" not in c.get(path).text


def test_plugin_manifest_declares_key_and_no_market_datasets():
    from app.data_providers.custom import loader
    m = loader.plugin_manifest("qveris")
    assert m["api_key_env"] == "QVERIS_API_KEY" and m["datasets"] == [] and m["hidden"] is False
    assert loader.probe_plugin_key("qveris", "") == (False, "Key 为空")
