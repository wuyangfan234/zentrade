"""QVeris 成本闸: 缓存免费、每日上限 (美东日)、在途去重、不重试、失败也记账、余额缓存、Key 探测。"""
import threading
import time
from datetime import datetime, timezone

import pytest

from app import secrets_store
from app.plugins.qveris import provider as qp
from app.services import qveris_gate as qg


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    store = {"qveris_api_key": "sk-test"}
    monkeypatch.setattr(secrets_store, "load", lambda: store)
    monkeypatch.setattr(secrets_store, "save", lambda u: store.update({k: v for k, v in u.items() if v is not None}) or store)
    qg._balance_cache.update(at=0.0, value=None, error=None)
    return store


def _exec(counter, charged: float | None = 1.0, data=None):
    def f(kind, params):
        counter.append((kind, params))
        return {"data": data or {"ok": kind}, "charged": charged, "execution_id": f"x{len(counter)}", "elapsed_ms": 5}
    return f


NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc).timestamp()  # 11:00 ET


def test_cache_hit_is_free_and_not_called(tmp_path):
    calls = []
    a = qg.fetch(tmp_path, "earnings_estimates", "lite", {"symbol": "LITE"}, fresh=lambda e: True, execute=_exec(calls), now=NOW)
    b = qg.fetch(tmp_path, "earnings_estimates", "LITE", {"symbol": "LITE"}, fresh=lambda e: True, execute=_exec(calls), now=NOW)
    assert len(calls) == 1 and not a["cached"] and a["charged"] == 1.0
    assert b["cached"] and b["charged"] == 0.0 and b["data"] == a["data"]
    assert qg.spent_today(tmp_path, day="2026-10-02") == 1.0


def test_stale_cache_triggers_a_new_paid_call(tmp_path):
    calls = []
    qg.fetch(tmp_path, "options_chain", "NVDA", {}, fresh=lambda e: False, execute=_exec(calls), now=NOW)
    qg.fetch(tmp_path, "options_chain", "NVDA", {}, fresh=lambda e: False, execute=_exec(calls), now=NOW)
    assert len(calls) == 2 and qg.spent_today(tmp_path, day="2026-10-02") == 2.0


def test_daily_limit_blocks_before_calling(tmp_path, _secrets):
    _secrets["qveris_daily_limit"] = 2
    calls = []
    for s in ("A", "B"):
        qg.fetch(tmp_path, "options_chain", s, {}, fresh=lambda e: False, execute=_exec(calls), now=NOW)
    with pytest.raises(qg.QuotaExceeded) as ei:
        qg.fetch(tmp_path, "options_chain", "C", {}, fresh=lambda e: False, execute=_exec(calls), now=NOW)
    assert len(calls) == 2 and "2/2" in str(ei.value)
    # 缓存命中不受上限影响
    assert qg.fetch(tmp_path, "options_chain", "A", {}, fresh=lambda e: True, execute=_exec(calls), now=NOW)["cached"]


def test_daily_limit_resets_on_the_us_eastern_day(tmp_path, _secrets):
    _secrets["qveris_daily_limit"] = 1
    calls = []
    late = datetime(2026, 10, 3, 3, 30, tzinfo=timezone.utc).timestamp()      # 10-02 23:30 ET
    next_day = datetime(2026, 10, 3, 4, 30, tzinfo=timezone.utc).timestamp()  # 10-03 00:30 ET
    qg.fetch(tmp_path, "options_chain", "A", {}, fresh=lambda e: False, execute=_exec(calls), now=late)
    with pytest.raises(qg.QuotaExceeded):
        qg.fetch(tmp_path, "options_chain", "B", {}, fresh=lambda e: False, execute=_exec(calls), now=late)
    qg.fetch(tmp_path, "options_chain", "B", {}, fresh=lambda e: False, execute=_exec(calls), now=next_day)
    assert len(calls) == 2


def test_failure_is_recorded_not_retried_and_not_cached(tmp_path):
    calls = []

    def boom(kind, params):
        calls.append(1)
        raise qp.QverisError("HTTP 502", status=502, charged=0.0)
    with pytest.raises(qp.QverisError):
        qg.fetch(tmp_path, "options_chain", "NVDA", {}, fresh=lambda e: True, execute=boom, now=NOW)
    assert calls == [1]                                   # 不重试
    rows = qg.ledger(tmp_path)
    assert rows[-1]["ok"] is False and rows[-1]["charged"] == 0.0 and "502" in rows[-1]["error"]
    assert qg.cache_get(tmp_path, "options_chain", "NVDA") is None


def test_unreported_charge_is_booked_at_the_quote(tmp_path):
    calls = []
    out = qg.fetch(tmp_path, "options_chain", "NVDA", {}, fresh=lambda e: False, execute=_exec(calls, charged=None), now=NOW)
    assert out["charged"] == 1.0 and qg.spent_today(tmp_path, day="2026-10-02") == 1.0


def test_concurrent_identical_requests_are_deduplicated(tmp_path):
    calls = []
    gate = threading.Event()

    def slow(kind, params):
        calls.append(1)
        gate.wait(2)
        return {"data": {"v": 1}, "charged": 1.0, "execution_id": "x"}
    results = []

    def worker():
        results.append(qg.fetch(tmp_path, "options_chain", "NVDA", {"s": 1}, fresh=lambda e: False, execute=slow, now=NOW))
    ts = [threading.Thread(target=worker) for _ in range(3)]
    for t in ts:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in ts:
        t.join(5)
    assert calls == [1] and len(results) == 3
    assert sum(1 for r in results if not r["cached"]) == 1
    assert qg.spent_today(tmp_path, day="2026-10-02") == 1.0


def test_set_daily_limit_validates(_secrets):
    assert qg.set_daily_limit(100) == 100.0 and _secrets["qveris_daily_limit"] == 100.0
    with pytest.raises(ValueError):
        qg.set_daily_limit(-1)


def test_balance_is_cached_and_errors_are_reported(monkeypatch):
    n = []
    monkeypatch.setattr(qp, "credits", lambda key=None: n.append(1) or {"remaining": 9737.23, "purchased_total": 65000.0,
                                                                        "daily_free_remaining": 0.0})
    assert qg.balance()["remaining"] == 9737.23
    assert qg.balance()["remaining"] == 9737.23 and n == [1]       # 60 秒内不重复查
    assert qg.balance(force=True)["remaining"] == 9737.23 and n == [1, 1]

    def down(key=None):
        raise qp.QverisError("HTTP 401 Key 无效")
    monkeypatch.setattr(qp, "credits", down)
    b = qg.balance(force=True)
    assert "401" in b["error"]


def test_usage_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(qp, "credits", lambda key=None: {"remaining": 100.0, "purchased_total": 0, "daily_free_remaining": 0})
    monkeypatch.setattr(qg, "_et_day", lambda ts=None: "2026-10-02")
    calls = []
    qg.fetch(tmp_path, "options_chain", "NVDA", {}, fresh=lambda e: False, execute=_exec(calls), now=NOW)
    qg.fetch(tmp_path, "earnings_estimates", "LITE", {}, fresh=lambda e: False, execute=_exec(calls), now=NOW, trigger="auto")
    u = qg.usage(tmp_path)
    assert u["spent_today"] == 2.0 and u["daily_limit"] == 100.0 and u["balance"]["remaining"] == 100.0
    assert u["by_kind"]["options_chain"]["calls"] == 1 and u["recent"][0]["trigger"] == "auto"
    assert "sk-test" not in str(u)


def test_execute_parses_billing_and_rejects_alpha_vantage_notes(monkeypatch):
    monkeypatch.setattr(qp, "_request", lambda *a, **k: {"success": True, "execution_id": "e1",
        "billing": {"recorded_amount_credits": 1.0}, "result": {"data": {"estimates": [1]}}, "elapsed_time_ms": 10})
    out = qp.execute("earnings_estimates", {"symbol": "X"}, key="k")
    assert out["charged"] == 1.0 and out["data"] == {"estimates": [1]}
    monkeypatch.setattr(qp, "_request", lambda *a, **k: {"success": True, "billing": {"final_amount_credits": 0},
        "result": {"data": {"Information": "Please slow down"}}})
    with pytest.raises(qp.QverisError) as ei:
        qp.execute("earnings_estimates", {"symbol": "X"}, key="k")
    assert ei.value.charged == 0
    monkeypatch.setattr(qp, "_request", lambda *a, **k: {"success": False, "error_message": "denied",
                                                         "billing": {"final_amount_credits": 0}})
    with pytest.raises(qp.QverisError):
        qp.execute("options_chain", {"symbol": "X"}, key="k")
    with pytest.raises(qp.QverisError):
        qp.execute("price_target", {}, key="k")   # 不在白名单的工具一律拒绝


def test_probe_api_key_uses_free_balance(monkeypatch):
    monkeypatch.setattr(qp, "credits", lambda key=None: {"remaining": 12.5, "purchased_total": 0, "daily_free_remaining": 0})
    ok, msg = qp.probe_api_key("sk-x")
    assert ok and "12.50" in msg
    assert qp.probe_api_key("  ")[0] is False
