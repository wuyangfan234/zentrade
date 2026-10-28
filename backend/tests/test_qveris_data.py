"""QVeris 业务层: 缓存新鲜度 (期权盘中 15 分钟/收盘后到下次开盘; 预期同一美东日; 电话会永久)、
只读不花钱、财报预期口径、电话会与提炼、每日自动目标 (10 个交易日内有财报的持仓+候选, 持仓优先)。"""
import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import secrets_store
from app.market_clock import us_clock
from app.plugins.qveris import provider as qp
from app.services import qveris_data as qd
from app.services import qveris_gate as qg

FX = json.loads((Path(__file__).parent / "fixtures" / "qveris_earnings_20261002.json").read_text())
CHAIN = json.loads((Path(__file__).parent / "fixtures" / "qveris_nvda_chain_20261001.json").read_text())


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    store = {"qveris_api_key": "sk-test"}
    monkeypatch.setattr(secrets_store, "load", lambda: store)
    monkeypatch.setattr(us_clock, "_calendar_provider", None)  # 工作日即交易日
    return store


def _ts(y, m, d, hh, mm):  # ET 墙钟 → UTC 时间戳
    return datetime(y, m, d, hh, mm, tzinfo=us_clock.tz).timestamp()


def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=us_clock.tz)


def test_options_freshness_in_session_15min_and_after_close_until_next_open():
    e = {"fetched_ts": _ts(2026, 10, 2, 10, 0)}
    assert qd.options_fresh(e, now=_et(2026, 10, 2, 10, 14))
    assert not qd.options_fresh(e, now=_et(2026, 10, 2, 10, 16))
    after_close = {"fetched_ts": _ts(2026, 10, 2, 16, 30)}
    assert qd.options_fresh(after_close, now=_et(2026, 10, 3, 12, 0))      # 周六: 仍是周五收盘后的链
    assert qd.options_fresh(after_close, now=_et(2026, 10, 5, 9, 0))       # 周一开盘前
    assert not qd.options_fresh(after_close, now=_et(2026, 10, 5, 9, 45))  # 周一盘中 → 过期
    pre_open = {"fetched_ts": _ts(2026, 10, 2, 8, 0)}
    assert not qd.options_fresh(pre_open, now=_et(2026, 10, 2, 16, 30))     # 中间经历了开盘


def test_estimates_fresh_same_eastern_day_only():
    e = {"fetched_ts": _ts(2026, 10, 2, 9, 0)}
    assert qd.estimates_fresh(e, now=_et(2026, 10, 2, 23, 59))
    assert not qd.estimates_fresh(e, now=_et(2026, 10, 3, 0, 1))


def test_read_only_paths_never_spend(tmp_path):
    def boom(*a, **k):
        raise AssertionError("must not call")
    assert qd.estimates(tmp_path, "LITE", refresh=False, execute=boom)["available"] is False
    assert qd.transcript(tmp_path, "MU", "2026Q3", refresh=False, execute=boom)["available"] is False
    repo = SimpleNamespace(get_daily=lambda *a, **k: None)
    assert qd.options_summary(tmp_path, repo, "NVDA", refresh=False, execute=boom)["available"] is False
    assert qg.ledger(tmp_path) == []


def test_estimates_normalized_next_quarter_and_years(tmp_path):
    ex = lambda kind, params: {"data": FX["estimates"], "charged": 1.0, "execution_id": "e"}  # noqa: E731
    r = qd.estimates(tmp_path, "lite", refresh=True, execute=ex)
    est = r["estimates"]
    assert r["symbol"] == "LITE" and r["charged"] == 1.0 and not r["cached"]
    assert est["next"]["horizon"] == "fiscal quarter" and est["next"]["eps_avg"] is not None
    assert all("year" in y["horizon"] for y in est["years"])
    assert all("quarter" in q["horizon"] for q in est["quarters"])
    # 同日再查走缓存, 不再扣费
    again = qd.estimates(tmp_path, "LITE", refresh=True, execute=lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert again["cached"] and again["charged"] == 0.0


def test_options_summary_uses_cache_and_spot_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(qd, "spot_price", lambda repo, s: (CHAIN["spot"], "2026-10-01 收盘"))
    calls = []

    def ex(kind, params):
        calls.append(params)
        return {"data": {"data": CHAIN["data"]}, "charged": 1.0, "execution_id": "e"}
    r = qd.options_summary(tmp_path, None, "NVDA", refresh=True, execute=ex)
    assert calls[0]["require_greeks"] == "true" and r["analysis"]["ok"] and r["spot_source"] == "2026-10-01 收盘"
    ro = qd.options_summary(tmp_path, None, "NVDA", refresh=False)
    assert ro["available"] and ro["cached"] and ro["analysis"]["contracts"] == r["analysis"]["contracts"]


def test_transcript_per_quarter_cache_and_digest(tmp_path):
    ex = lambda kind, params: {"data": FX["transcript"], "charged": 1.0, "execution_id": "e"}  # noqa: E731
    t = qd.transcript(tmp_path, "MU", "2026q3", refresh=True, execute=ex)
    assert t["available"] and t["quarter"] == "2026Q3" and len(t["segments"]) == 4 and t["avg_sentiment"] is not None
    # 另一个季度是另一份缓存
    assert qd.transcript(tmp_path, "MU", "2026Q2", refresh=False)["available"] is False

    seen = {}

    async def ai(messages, **kw):
        seen.update(kw)
        return "要点如下:\n- 营收同比增长 379%\n- 下季度指引 615 亿美元\n无关行"
    d = asyncio.run(qd.digest_transcript(tmp_path, "MU", "2026Q3", ai_call=ai))
    assert d["points"] == ["营收同比增长 379%", "下季度指引 615 亿美元"] and seen["task"] == "news_translate"

    async def no_call(messages, **kw):
        raise AssertionError("digest must be cached")
    assert asyncio.run(qd.digest_transcript(tmp_path, "MU", "2026Q3", ai_call=no_call))["points"][0].startswith("营收")
    assert qd.transcript(tmp_path, "MU", "2026Q3", refresh=False)["digest"]["points"]


def test_empty_transcript_reports_reason_and_is_not_considered_fresh(tmp_path):
    ex = lambda kind, params: {"data": {"symbol": "MU", "quarter": "2027Q1", "transcript": []}, "charged": 1.0}  # noqa: E731
    t = qd.transcript(tmp_path, "MU", "2027Q1", refresh=True, execute=ex)
    assert not t["available"] and "暂无电话会" in t["reason"]
    assert not qd.transcript_fresh({"data": {"transcript": []}})


def test_latest_quarter():
    assert qd.latest_quarter(date(2026, 10, 2)) == "2026Q3"
    assert qd.latest_quarter(date(2026, 2, 1)) == "2025Q4"


def _cal(tmp_path, events):
    d = tmp_path / "reference" / "earnings"
    d.mkdir(parents=True)
    (d / "calendar.json").write_text(json.dumps({"fetched_at": "2026-10-02T08:30:00-04:00", "events": events}))


@pytest.mark.real_earnings_calendar  # conftest 默认把财报日历打桩为空, 本测试需要真实读取
def test_auto_targets_positions_plus_swing_cards_in_watchlist(tmp_path, monkeypatch):
    """0.22.4: 候选 = 机会板活卡 ∩ 自选; 旧版非终态卡 (all_cards) 不再进名单。"""
    from app.opportunity.card import CardState
    _cal(tmp_path, {"SSL": {"date": "2026-10-15", "timing": "bmo"}, "TH": {"date": "2026-10-06", "timing": "amc"},
                    "FAR": {"date": "2026-10-30", "timing": "bmo"}, "OLD": {"date": "2026-10-01", "timing": "bmo"},
                    "LEG": {"date": "2026-10-07", "timing": "bmo"}, "NWL": {"date": "2026-10-07", "timing": "bmo"}})
    from app.services import swing_desk
    monkeypatch.setattr(swing_desk, "build", lambda d, repo=None: {"long": [{"symbol": "TH"}, {"symbol": "FAR"}],
                                                       "short": [{"symbol": "OLD"}, {"symbol": "NWL"}]})
    monkeypatch.setattr(qd, "_watchlist_symbols", lambda: {"SSL", "TH", "FAR", "OLD"})  # NWL 不在自选
    loop = SimpleNamespace(
        ledger=SimpleNamespace(open_positions=[SimpleNamespace(symbol="SSL")]),
        opp_svc=SimpleNamespace(all_cards=lambda d: [SimpleNamespace(symbol="LEG", state=CardState.OPEN)]))
    t = qd.auto_targets(tmp_path, loop, today=date(2026, 10, 2))
    assert [(x["symbol"], x["role"]) for x in t] == [("SSL", "持仓"), ("TH", "候选")]


def test_estimates_cache_valid_until_earnings_date():
    f = qd.estimates_fresh_until(date(2026, 10, 9))
    e = {"fetched_ts": _et(2026, 10, 2, 9, 0).timestamp()}
    assert f(e, now=_et(2026, 10, 8, 9, 0))
    assert f(e, now=_et(2026, 10, 9, 20, 0))
    assert not f(e, now=_et(2026, 10, 10, 9, 0))
    assert not qd.estimates_fresh_until(date(2026, 12, 1))(e, now=_et(2026, 10, 8, 9, 0))  # 上一窗口买的不算


def test_auto_refresh_stops_at_quota_and_skips_cached(tmp_path, _env, monkeypatch):
    _env["qveris_daily_limit"] = 1
    monkeypatch.setattr(qd, "auto_targets", lambda d, loop, today=None: [
        {"symbol": "AAA", "role": "持仓"}, {"symbol": "BBB", "role": "候选"}, {"symbol": "CCC", "role": "候选"}])
    calls = []

    def ex(kind, params):
        calls.append(params["symbol"])
        return {"data": FX["estimates"], "charged": 1.0}
    r = qd.auto_refresh_estimates(tmp_path, None, execute=ex)
    assert calls == ["AAA"] and r["fetched"] == ["AAA"] and "已拒绝" in r["blocked"]
    assert qg.ledger(tmp_path)[-1]["trigger"] == "auto"
    assert qd.auto_state(tmp_path)["targets"] == 3


def test_auto_refresh_without_key_is_a_noop(tmp_path, _env, monkeypatch):
    _env.pop("qveris_api_key")
    monkeypatch.delenv("QVERIS_API_KEY", raising=False)

    def boom(*a, **k):
        raise AssertionError("must not call without key")
    assert qd.auto_refresh_estimates(tmp_path, None, execute=boom) == {"skipped": "未配置 QVeris Key"}
    assert qg.ledger(tmp_path) == []
