"""机会 API 与服务单测 (P3)。合成存储 + 服务 + 端点契约。"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import opportunities as opp_api
from app.opportunity.engine import load_params
from app.services.opportunity_service import OpportunityService

PARAMS = load_params()
AS_OF = date(2026, 9, 25)


def _bars(n: int = 160) -> pl.DataFrame:
    rows = []
    base = 100.0
    # 末日贴近 AS_OF: 粗筛看近 30 日 ADV, 数据停在两个月前会被窗口筛掉 (9-29 修复)
    d = AS_OF - timedelta(days=n)
    for i in range(n):
        base *= 1.002
        rows.append({"date": d, "open": base, "high": base * 1.01, "low": base * 0.99,
                     "close": base, "volume": 1_000_000.0, "amount": base * 1e6})
        d += timedelta(days=1)
    return pl.DataFrame(rows)


class _FakeRepo:
    def __init__(self):
        self.store = SimpleNamespace(data_dir=None)

    def get_instruments(self) -> pl.DataFrame:
        return pl.DataFrame({"symbol": ["TEST"], "name": ["Test"]})

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        if symbol == "SPY":
            rows = [{"date": AS_OF - timedelta(days=i), "close": 500.0, "volume": 1.0}
                    for i in range(210)]
            return pl.DataFrame(rows).sort("date")
        return _bars()

    def get_daily_batch(self, symbols, start, end, columns=None):
        frames = [_bars().with_columns(pl.lit("TEST").alias("symbol")),
                  pl.DataFrame([{"date": AS_OF - timedelta(days=i), "open": 499.0, "high": 501.0,
                                 "low": 498.0, "close": 500.0, "volume": 1_000_000_000.0,
                                 "amount": 5e11} for i in range(210)]).with_columns(pl.lit("SPY").alias("symbol"))]
        return pl.concat(frames).sort(["symbol", "date"])


def _client(svc) -> TestClient:
    app = FastAPI()
    app.include_router(opp_api.router)
    app.state.opportunity_service = svc
    return TestClient(app)


def test_scan_persists_and_reads_back(tmp_path):
    repo = _FakeRepo()
    repo.store.data_dir = tmp_path
    svc = OpportunityService(repo)
    result = svc.run_daily_scan(AS_OF)
    assert result.scanned >= 1
    payload = svc.load_cards(AS_OF)
    assert payload is not None
    assert payload["as_of"] == AS_OF.isoformat()
    assert payload["gate"] in ("green", "yellow")


def test_today_endpoint_falls_back_to_latest(tmp_path):
    repo = _FakeRepo()
    repo.store.data_dir = tmp_path
    svc = OpportunityService(repo)
    svc.run_daily_scan(AS_OF)
    client = _client(svc)
    r = client.get("/api/opportunities/today")
    assert r.status_code == 200
    assert r.json()["as_of"] == AS_OF.isoformat()


def test_today_empty_before_any_scan(tmp_path):
    repo = _FakeRepo()
    repo.store.data_dir = tmp_path
    svc = OpportunityService(repo)
    client = _client(svc)
    r = client.get("/api/opportunities/today")
    assert r.status_code == 200
    assert r.json()["cards"] == []


def test_history_listing_and_fetch(tmp_path):
    repo = _FakeRepo()
    repo.store.data_dir = tmp_path
    svc = OpportunityService(repo)
    svc.run_daily_scan(AS_OF)
    client = _client(svc)
    days = client.get("/api/opportunities/history").json()["days"]
    assert AS_OF.isoformat() in days
    r = client.get("/api/opportunities/history", params={"day": AS_OF.isoformat()})
    assert r.status_code == 200
    r404 = client.get("/api/opportunities/history", params={"day": "2020-01-01"})
    assert r404.status_code == 404


def test_rules_save_validates_short_capable_and_round_trips(tmp_path):
    repo = _FakeRepo()
    repo.store.data_dir = tmp_path
    app = FastAPI()
    app.include_router(opp_api.router)
    app.state.repo = repo
    client = TestClient(app)
    r = client.post("/api/opportunities/rules", json={"enabled": ["breakout"], "params": {}, "short": ["breakout"]})
    assert r.status_code == 422 and "不能做空" in r.json()["detail"]
    r = client.post("/api/opportunities/rules",
                    json={"enabled": ["breakout"], "params": {"risk_per_trade_pct": 1}, "short": ["phantom_4h_short"]})
    assert r.status_code == 200
    g = client.get("/api/opportunities/rules").json()
    assert g["ruleset"]["short"] == ["phantom_4h_short"]
    assert g["trading"]["short"] == ["phantom_4h_short"] and g["trading"]["long"] == ["breakout"]


def test_state_endpoint(tmp_path):
    repo = _FakeRepo()
    repo.store.data_dir = tmp_path
    svc = OpportunityService(repo)
    svc.run_daily_scan(AS_OF)
    client = _client(svc)
    st = client.get("/api/opportunities/state").json()
    assert st["latest_card_day"] == AS_OF.isoformat()


def test_save_scan_same_day_merges_existing_cards(tmp_path):
    """同日重扫合并语义 (9-29 审计回归): 覆盖写曾把同日活跃卡抹掉 (实证 2→0)。"""
    from app.opportunity.card import OpportunityCard, CardState
    from app.opportunity.engine import ScanResult

    repo = _FakeRepo()
    repo.store.data_dir = tmp_path
    svc = OpportunityService(repo)

    def _card(sym: str) -> OpportunityCard:
        return OpportunityCard(
            id=f"c_{sym}", symbol=sym, setup_type="breakout",
            created_date=AS_OF, entry_zone=(10.0, 10.5), invalidation=9.5,
            target_zone=(11.0, 12.0), state=CardState.CANDIDATE,
        )

    r1 = ScanResult(as_of=AS_OF, params_revision=1)
    r1.gate = "green"
    r1.cards = [_card("ARX"), _card("HZO")]
    svc.save_scan(r1)

    # 同日重扫: 新结果只含 MSFT —— 旧两卡必须保留
    r2 = ScanResult(as_of=AS_OF, params_revision=1)
    r2.gate = "green"
    r2.cards = [_card("MSFT")]
    svc.save_scan(r2)

    payload = svc.load_cards(AS_OF)
    syms = sorted(c["symbol"] for c in payload["cards"])
    assert syms == ["ARX", "HZO", "MSFT"]

    # 第三次重扫同票去重: MSFT 再出不翻倍
    r3 = ScanResult(as_of=AS_OF, params_revision=1)
    r3.gate = "green"
    r3.cards = [_card("MSFT")]
    svc.save_scan(r3)
    payload = svc.load_cards(AS_OF)
    assert sorted(c["symbol"] for c in payload["cards"]) == ["ARX", "HZO", "MSFT"]


def test_settlement_endpoint_is_read_only_and_reports_unavailable_without_file(tmp_path):
    """方向 A: GET /settlement 只读 table.json; 没有就报 available=false, 不建目录、不触发计算。"""
    import json as _json

    from app.services import card_settlement as cs
    app = FastAPI()
    app.include_router(opp_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    c = TestClient(app)
    path = [r.path for r in opp_api.router.routes if r.path.endswith("/settlement")][0]
    j = c.get(path).json()
    assert j["available"] is False and j["thresholds"]["gray_below"] == 20
    assert not cs.settle_dir(tmp_path).exists()
    cs.settle_dir(tmp_path).mkdir(parents=True)
    (cs.settle_dir(tmp_path) / "table.json").write_text(_json.dumps({"contract": cs.CONTRACT, "tables": {"fill": {}}}))
    before = sorted(p.name for p in cs.settle_dir(tmp_path).iterdir())
    j = c.get(path).json()
    assert j["available"] is True and j["tables"] == {"fill": {}}
    assert sorted(p.name for p in cs.settle_dir(tmp_path).iterdir()) == before
