"""0.29.0 机会页聚合 /api/opportunities/page 与齿轮唯一保存口 /api/opportunities/settings。

不变量: GET 只读 (除期权链预热缓存); 一块失败只空一块; 板卡标注只读缓存不出网; 标注不改板卡任何字段 (不进裁决);
保存: 3+3 照写 swing.json; 风险变了才新建规则集版本且勾选原样沿用; 风险越界 422 且什么都不写。
"""
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_clock import us_clock
from app.services import opp_page
from app.services import swing_desk as sd
from app.services import watchlist_v4 as wv

NOW = datetime(2026, 10, 20, 10, 5, tzinfo=us_clock.tz)
TODAY = NOW.date()
_REAL_WARM = opp_page._warm_options                                     # 模块导入时取原函数 (autouse 夹具会替换成桩)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    opp_page._CTX_CACHE.clear()
    opp_page._WARM_TRIED.clear()
    wv._PM_CACHE.clear()
    warmed = []
    monkeypatch.setattr(opp_page, "_warm_options", lambda data_dir, sym, today: warmed.append(sym))
    yield warmed


def _card(sym, side="long", price=100.0):
    return {"symbol": sym, "side": side, "price": price, "close": price - 1, "entry_zone": [97.0, 98.0], "stop": 95.0,
            "target": 104.0, "verdict": "order", "state": "pending", "strategies": [], "score": 1.0}


def _swing(*cards):
    return {"contract": sd.CONTRACT, "available": True, "as_of": "2026-10-19", "long_rules": [], "short_rules": [],
            "long": [c for c in cards if c["side"] == "long"], "short": [c for c in cards if c["side"] == "short"],
            "history": [], "refresh": {"count": 0, "last_at": None, "changes": []}}


def _stub_sections(monkeypatch, swing, intraday=None, mood=None):
    from app.services import intraday_strength, market_mood
    monkeypatch.setattr(sd, "build_live", lambda data_dir, repo=None, loop=None, today=None: json.loads(json.dumps(swing)))
    monkeypatch.setattr(intraday_strength, "build_live",
                        lambda data_dir, repo=None, loop=None, now=None: intraday or {"active": [], "history": []})
    monkeypatch.setattr(market_mood, "view", lambda data_dir, now=None: mood or {"judgment": None})


def _chain_rows(spot=100.0, exp="2026-10-23", put_vol=300, call_vol=600):
    rows = []
    for k in (95.0, 100.0, 105.0):
        for typ, vol in (("call", call_vol), ("put", put_vol)):
            rows.append({"contractID": f"X{typ}{k}", "expiration": exp, "strike": k, "type": typ, "bid": 1.0, "ask": 1.2,
                         "mark": 1.1, "volume": vol // 3, "open_interest": 10, "implied_volatility": 0.4,
                         "delta": 0.5 if typ == "call" else -0.5, "date": TODAY.isoformat()})
    return rows


def _free_cache(d: Path, sym, day=TODAY, rows=None):
    p = d / "options_free" / sym / f"{day.isoformat()}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"available": True, "rows": rows or _chain_rows(), "source": "alpaca"}))


def _client(tmp_path):
    from app.api.opportunities import router
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.trading_loop = None
    return TestClient(app)


def _tree_hash(d: Path) -> dict:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


# ---------- GET /page ----------

def test_page_aggregates_sections_and_context_without_touching_cards(tmp_path, monkeypatch):
    swing = _swing(_card("NVDA"), _card("AMD", "short", 150.0))
    _stub_sections(monkeypatch, swing, intraday={"active": [{"symbol": "AXTI"}], "history": []}, mood={"judgment": {"day": "x"}})
    _free_cache(tmp_path, "NVDA")
    (tmp_path / "premarket_gap").mkdir()
    (tmp_path / "premarket_gap" / f"{TODAY}.json").write_text(json.dumps({"day": TODAY.isoformat(), "gaps": {"NVDA": 1.234}}))
    from app.services import earnings_calendar, news_store
    monkeypatch.setattr(earnings_calendar.EarningsCalendar, "event_for",
                        lambda self, s: {"date": "2026-10-23", "timing": "amc"} if s == "AMD" else None)
    monkeypatch.setattr(news_store, "batch_summary", lambda data_dir, syms: {s: {"count_24h": 2, "tone": {"bearish": 1, "bullish": 0}}
                                                                           for s in syms})
    d = _client(tmp_path).get("/api/opportunities/page").json()
    assert d["contract"] == opp_page.CONTRACT and d["errors"] == {}
    assert d["phase"]["phase"] == "session"
    assert d["intraday"]["active"][0]["symbol"] == "AXTI" and d["mood"]["judgment"]["day"] == "x"
    assert d["swing"] == swing                                          # 标注不改板卡任何字段 (不进裁决)
    nv, amd = d["ctx"]["NVDA"], d["ctx"]["AMD"]
    assert nv["premarket_pct"] == 1.234 and nv["news"]["tone"]["bearish"] == 1
    assert nv["pcr"] == 0.5 and "earnings" not in nv                    # 近月 put 300 / call 600
    assert amd["earnings"] == {"date": "2026-10-23", "timing": "amc"}
    assert "pcr" not in amd and "premarket_pct" not in amd               # 没缓存 / 没盘前 -> 不给键


def test_page_is_read_only_except_options_warm(tmp_path, monkeypatch, _env):
    _stub_sections(monkeypatch, _swing(_card("NVDA"), _card("MU")))
    _free_cache(tmp_path, "NVDA")
    before = _tree_hash(tmp_path)
    for _ in range(2):
        _client(tmp_path).get("/api/opportunities/page")
    assert _tree_hash(tmp_path) == before
    assert not (tmp_path / "qveris").exists()                           # 不像 qveris_gate.cache_get 那样顺手建目录
    assert _env == ["MU"]                                               # 只给缺当天缓存的票预热; 2 分钟内复用标注不重复预热


def test_one_section_failure_only_blanks_that_section(tmp_path, monkeypatch):
    _stub_sections(monkeypatch, _swing(_card("NVDA")))
    from app.services import intraday_strength, news_store

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(intraday_strength, "build_live", boom)
    monkeypatch.setattr(news_store, "batch_summary", boom)
    d = _client(tmp_path).get("/api/opportunities/page").json()
    assert d["intraday"] is None and d["errors"] == {"intraday": "RuntimeError", "ctx_news": "RuntimeError"}
    assert d["swing"]["long"][0]["symbol"] == "NVDA" and "NVDA" in d["ctx"]
    opp_page._CTX_CACHE.clear()
    monkeypatch.setattr(sd, "build_live", boom)
    d = _client(tmp_path).get("/api/opportunities/page").json()
    assert d["swing"] is None and d["ctx"] == {} and "swing" in d["errors"]


def test_context_failure_is_not_cached(tmp_path, monkeypatch):
    _stub_sections(monkeypatch, _swing(_card("NVDA")))
    from app.services import news_store
    calls = []

    def flaky(data_dir, syms):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("x")
        return {"NVDA": {"count_24h": 1, "tone": None}}
    monkeypatch.setattr(news_store, "batch_summary", flaky)
    assert _client(tmp_path).get("/api/opportunities/page").json()["errors"] == {"ctx_news": "RuntimeError"}
    d = _client(tmp_path).get("/api/opportunities/page").json()
    assert d["errors"] == {} and d["ctx"]["NVDA"]["news"]["count_24h"] == 1
    _client(tmp_path).get("/api/opportunities/page")
    assert len(calls) == 2                                              # 成功那次缓存住了


def test_options_order_paid_today_then_free_today_then_old_paid(tmp_path, _env):
    def paid(fetched: datetime, put_vol):
        p = tmp_path / "qveris" / "cache" / "options_chain" / "NVDA.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"fetched_ts": fetched.timestamp(), "data": {"data": _chain_rows(put_vol=put_vol)}}))
    _free_cache(tmp_path, "NVDA", rows=_chain_rows(put_vol=600))                       # 免费今天: pcr 1.0
    paid(datetime(2026, 10, 19, 15, 0, tzinfo=UTC), 150)                             # 付费昨天: pcr 0.25
    assert opp_page._options(tmp_path, "NVDA", 100.0, TODAY, None)["pcr"] == 1.0
    paid(datetime(2026, 10, 20, 14, 0, tzinfo=UTC), 150)                             # 付费今天优先
    assert opp_page._options(tmp_path, "NVDA", 100.0, TODAY, None)["pcr"] == 0.25
    (tmp_path / "options_free" / "NVDA" / f"{TODAY}.json").unlink()
    paid(datetime(2026, 10, 19, 15, 0, tzinfo=UTC), 900)                             # 没免费今天 -> 旧付费, 并预热免费链
    assert opp_page._options(tmp_path, "NVDA", 100.0, TODAY, None)["pcr"] == 1.5
    assert _env == ["NVDA"]                                                           # 审查 #4: 旧付费不能挡住预热
    assert opp_page._options(tmp_path, "NVDA", None, TODAY, None) is None              # 没现价不算


def test_earnings_iv_uses_earnings_expiry(tmp_path, monkeypatch):
    _free_cache(tmp_path, "NVDA", rows=_chain_rows(exp="2026-10-23") + [
        {**r, "contractID": r["contractID"] + "b", "implied_volatility": 0.8} for r in _chain_rows(exp="2026-10-30")])
    o = opp_page._options(tmp_path, "NVDA", 100.0, TODAY, date(2026, 10, 27))
    assert o["iv"] == 0.8                                               # 财报后第一个到期 = 10-30
    assert opp_page._options(tmp_path, "NVDA", 100.0, TODAY, None)["iv"] is None


def test_warm_options_single_flight_and_retry_window(tmp_path, monkeypatch):
    import threading
    import time as _t

    from app.services import alpaca_options
    gate, calls = threading.Event(), []

    def chain(data_dir, sym, today):
        calls.append(sym)
        gate.wait(2)
        return {"available": False}
    monkeypatch.setattr(alpaca_options, "chain", chain)
    _REAL_WARM(tmp_path, "NVDA", TODAY)
    _REAL_WARM(tmp_path, "NVDA", TODAY)                                 # 在途: 不重复
    gate.set()
    for _ in range(100):
        if not opp_page._WARM_INFLIGHT:
            break
        _t.sleep(0.02)
    _REAL_WARM(tmp_path, "NVDA", TODAY)                                 # 10 分钟内不重试
    assert calls == ["NVDA"] and not opp_page._WARM_INFLIGHT


# ---------- POST /settings ----------

def _mode(tmp_path):
    (tmp_path / "decision_desk").mkdir(parents=True, exist_ok=True)
    (tmp_path / "decision_desk" / "evidence_mode.json").write_text(json.dumps({"mode": "v4"}))


def test_settings_risk_change_creates_version_keeping_enabled(tmp_path):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden", "tv_smc_structure"], {"risk_per_trade_pct": 0.5}, short=["tv_smc_structure"])
    c = _client(tmp_path)
    r = c.post("/api/opportunities/settings", json={"long": ["volume_price_surge"], "short": ["tv_halftrend"],
                                                    "risk_per_trade_pct": 0.75}).json()
    assert r["ruleset"] == {"version": 2, "risk_per_trade_pct": 0.75}
    rs = wv.current_ruleset(tmp_path)
    assert rs["enabled"] == ["macd_golden", "tv_smc_structure"] and rs["short"] == ["tv_smc_structure"] and rs["evo"] is False
    assert json.loads(sd.swing_config_path(tmp_path).read_text())["long"] == ["volume_price_surge"]
    r = c.post("/api/opportunities/settings", json={"long": ["macd_golden"], "short": ["tv_halftrend"],
                                                    "risk_per_trade_pct": 0.75}).json()
    assert r["ruleset"]["version"] == 2 and wv.versions(tmp_path) == [1, 2]          # 风险没变: 不建版本
    assert sd.strategies_from_ruleset(tmp_path)[0] == ("macd_golden",)


def test_page_settings_are_current_swing_json_not_board_frozen_rules(tmp_path, monkeypatch):
    """板上的 long_rules 是簿里冻结的那套; 齿轮必须读当前 swing.json, 否则存完 3+3 再打开会显示旧勾选。"""
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 0.5})
    sd.save_strategies(tmp_path, ["volume_price_surge"], ["tv_halftrend"])
    swing = {**_swing(_card("NVDA")), "long_rules": ["tv_smc_structure"], "short_rules": ["tv_smc_structure"]}
    _stub_sections(monkeypatch, swing)
    d = _client(tmp_path).get("/api/opportunities/page").json()
    push = d["settings"].pop("radar_push")
    assert d["settings"] == {"long": ["volume_price_surge"], "short": ["tv_halftrend"], "risk_per_trade_pct": 0.5,
                             "ruleset_version": 1, "evo": False}
    assert push["enabled"] is True and push["min_rvol"] == 3.0 and [x["v"] for x in push["levels"]] == [1.0, 2.0, 3.0, 5.0]
    assert d["swing"]["long_rules"] == ["tv_smc_structure"]


def test_settings_turns_off_legacy_evo_and_rejects_bad_risk_without_writing(tmp_path):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 1.0}, evo=True)
    c = _client(tmp_path)
    before = _tree_hash(tmp_path)
    for bad in (0.05, 2.5, float("nan")):
        body = json.dumps({"long": ["a"], "short": ["b"], "risk_per_trade_pct": bad}).replace("NaN", "\"NaN\"")
        r = c.post("/api/opportunities/settings", content=body, headers={"content-type": "application/json"})
        assert r.status_code == 422
    assert _tree_hash(tmp_path) == before                               # 越界什么都不写 (含 swing.json)
    r = c.post("/api/opportunities/settings", json={"long": ["macd_golden"], "short": ["tv_halftrend"]}).json()
    assert r["ruleset"]["version"] == 2 and wv.current_ruleset(tmp_path)["evo"] is False
    assert wv.current_ruleset(tmp_path)["params"]["risk_per_trade_pct"] == 1.0


@pytest.mark.parametrize("bad", [True, False, "0.5", "1", "", 0, [], {}])
def test_settings_risk_must_be_a_json_number(tmp_path, bad):
    """审查 #5: 宽松模式会把 true -> 1.0、"0.5" -> 0.5 并建新版本; 风险只收 JSON 数字 (0.1-2) 或 null。"""
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 0.5})
    before = _tree_hash(tmp_path)
    r = _client(tmp_path).post("/api/opportunities/settings", json={"long": ["a"], "short": ["b"], "risk_per_trade_pct": bad})
    assert r.status_code == 422, (bad, r.status_code)
    assert _tree_hash(tmp_path) == before and wv.versions(tmp_path) == [1]


def test_page_glue_failure_still_returns_sections(tmp_path, monkeypatch):
    """审查 #3: 板卡列表里混入坏元素时, 只丢标注, 不让整页 500。"""
    swing = _swing(_card("NVDA"))
    swing["long"].append("not-a-card")
    _stub_sections(monkeypatch, swing)
    r = _client(tmp_path).get("/api/opportunities/page")
    assert r.status_code == 200
    d = r.json()
    assert d["swing"]["long"][0]["symbol"] == "NVDA" and d["ctx"] == {} and "ctx" in d["errors"]


# ---------- 0.34.0 按时段换主角 ----------

@pytest.mark.parametrize("hm,key", [((3, 0), "pre"), ((8, 5), "pre"), ((9, 29), "pre"), ((9, 30), "open"), ((10, 29), "open"),
                                    ((10, 30), "session"), ((15, 59), "session"), ((16, 0), "board"), ((20, 0), "board")])
def test_stage_follows_et_clock(hm, key):
    assert opp_page.stage_of(datetime(2026, 10, 20, *hm, tzinfo=us_clock.tz))["key"] == key


def test_stage_weekend_is_board():
    assert opp_page.stage_of(datetime(2026, 10, 18, 10, 0, tzinfo=us_clock.tz))["key"] == "board"


def test_stage_half_day_close(monkeypatch):
    from datetime import time as dtime
    monkeypatch.setattr(us_clock, "session_close", lambda d: dtime(13, 0))
    assert opp_page.stage_of(datetime(2026, 11, 27, 13, 5, tzinfo=us_clock.tz))["key"] in ("board",)


def test_page_has_stage_radar_gaps_and_side_context(tmp_path, monkeypatch):
    swing = _swing(_card("NVDA"))
    _stub_sections(monkeypatch, swing, intraday={"active": [], "history": [], "checks_today": [
        {"check": "10:00", "heat": [{"symbol": "AXTI", "rvol": 3.0}], "near": [{"symbol": "SMCI", "why": "below_vwap"}]}]})
    from app.services import earnings_calendar, news_store, open_radar
    monkeypatch.setattr(open_radar, "view", lambda data_dir, repo=None, now=None: {
        "picks": [{"symbol": "SPCX", "rvol": 2.8}], "others": [], "is_today": True})
    (tmp_path / "premarket_gap").mkdir()
    (tmp_path / "premarket_gap" / f"{TODAY}.json").write_text(json.dumps(
        {"day": TODAY.isoformat(), "at": "2026-10-20T09:00:01-04:00", "gaps": {"DELL": 6.9, "NVDA": -3.1, "MU": 1.0},
         "mids": {"DELL": 493.4, "NVDA": 180.0, "MU": 900.0}}))
    monkeypatch.setattr(earnings_calendar.EarningsCalendar, "event_for", lambda self, s: None)
    seen = []
    monkeypatch.setattr(news_store, "batch_summary", lambda data_dir, syms: seen.extend(syms) or {})
    d = _client(tmp_path).get("/api/opportunities/page").json()
    assert d["errors"] == {} and d["stage"] == {"key": "open", "label": "开盘"}          # NOW = 10:05 ET
    assert d["radar"]["picks"][0]["symbol"] == "SPCX"
    g = d["gaps"]
    assert g["live"] is False and g["threshold_pct"] == 3.0 and g["checked"] == 3
    assert [(r["symbol"], r.get("role")) for r in g["rows"]] == [("DELL", None), ("NVDA", "挂单")]   # |跳空| >= 3%, 按幅度
    # 榜上的票也给新闻标注 (但不预热期权链)
    assert set(seen) >= {"NVDA", "SPCX", "AXTI", "SMCI", "DELL"}
    assert d["swing"] == swing


def test_gap_board_live_only_in_premarket_and_frozen_after_0900(tmp_path, monkeypatch):
    from app.services import card_volume as cv
    calls = []
    monkeypatch.setattr(cv, "premarket_prices_cached", lambda syms, **k: calls.append(list(syms)) or {"NVDA": {"price": 110.0}})
    from app.services import decision_scan_job
    monkeypatch.setattr(decision_scan_job, "_watchlist", lambda: {"NVDA", "MU"})
    repo = SimpleNamespace(get_daily_batch=lambda syms, a, b, columns=None: __import__("polars").DataFrame(
        {"symbol": ["NVDA"], "date": [date(2026, 10, 19)], "close": [100.0]}))
    pre = datetime(2026, 10, 20, 8, 0, tzinfo=us_clock.tz)
    g = opp_page.gap_board(tmp_path, repo, pre, None, "pre")
    assert g["live"] and g["rows"] == [{"symbol": "NVDA", "gap_pct": 10.0, "price": 110.0}]
    assert opp_page.gap_board(tmp_path, repo, datetime(2026, 10, 20, 11, 0, tzinfo=us_clock.tz), None, "session") is None
    assert len(calls) == 1                                                    # 盘中不现取 HL
    assert opp_page.gap_board(tmp_path, repo, datetime(2026, 10, 18, 8, 0, tzinfo=us_clock.tz), None, "pre") is None   # 周末


def test_page_stage_sections_are_read_only(tmp_path, monkeypatch, _env):
    _stub_sections(monkeypatch, _swing(_card("NVDA")))
    from app.services import open_radar
    (open_radar.root(tmp_path)).mkdir(parents=True)
    open_radar.day_path(tmp_path, TODAY).write_text(json.dumps({"day": TODAY.isoformat(), "rows": [
        {"symbol": "SPCX", "rvol": 2.8, "dir": 1, "pick": True, "rank": 1, "prev_close": 150.0}]}))
    _free_cache(tmp_path, "NVDA")
    before = _tree_hash(tmp_path)
    d = _client(tmp_path).get("/api/opportunities/page").json()
    assert d["radar"]["is_today"] and d["radar"]["picks"][0]["symbol"] == "SPCX"
    assert _tree_hash(tmp_path) == before



def test_gap_board_never_blocks_request_on_hl(tmp_path, monkeypatch):
    """0.34.1 审查: 盘前跳空榜在请求里不同步出网; 没缓存 -> 后台单飞取, 本次返回“取数中”, 下一轮拿到。"""
    import threading

    from app.services import card_volume as cv
    from app.services import decision_scan_job
    monkeypatch.setattr(decision_scan_job, "_watchlist", lambda: {"NVDA"})
    cv._cache.clear()
    gate, started = threading.Event(), threading.Event()

    def slow_fetch(syms):
        started.set()
        gate.wait(5)
        return {"NVDA": 110.0}
    monkeypatch.setattr(cv, "_hl_mids", slow_fetch)
    repo = SimpleNamespace(get_daily_batch=lambda syms, a, b, columns=None: __import__("polars").DataFrame(
        {"symbol": ["NVDA"], "date": [date(2026, 10, 19)], "close": [100.0]}))
    pre = datetime(2026, 10, 20, 8, 0, tzinfo=us_clock.tz)
    import time as _t
    t0 = _t.monotonic()
    g = opp_page.gap_board(tmp_path, repo, pre, None, "pre")
    assert _t.monotonic() - t0 < 1.0                                        # 没等 HL
    assert g["warming"] and g["rows"] == []
    assert started.wait(2)
    gate.set()
    for _ in range(50):
        if cv._cache:
            break
        _t.sleep(0.02)
    g2 = opp_page.gap_board(tmp_path, repo, pre, None, "pre")
    assert not g2.get("warming") and g2["rows"] == [{"symbol": "NVDA", "gap_pct": 10.0, "price": 110.0}]
    cv._cache.clear()


# ---------- 0.35.0 开盘榜推送开关 (齿轮同一个写口) ----------

def test_settings_saves_radar_push_without_new_ruleset_version(tmp_path):
    from app.services import open_radar
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 0.5})
    c = _client(tmp_path)
    r = c.post("/api/opportunities/settings", json={"long": ["macd_golden"], "short": ["tv_halftrend"],
                                                    "radar_push": {"enabled": False, "min_rvol": 5}}).json()
    assert r["radar_push"] == {"enabled": False, "min_rvol": 5.0}
    assert open_radar.push_settings(tmp_path) == {"enabled": False, "min_rvol": 5.0}
    assert wv.versions(tmp_path) == [1]                                   # 推送开关不建规则集版本
    # 不带 radar_push: 原样不动
    r = c.post("/api/opportunities/settings", json={"long": ["macd_golden"], "short": ["tv_halftrend"]}).json()
    assert r["radar_push"] == {"enabled": False, "min_rvol": 5.0}


@pytest.mark.parametrize("bad", [{"enabled": True, "min_rvol": 4}, {"enabled": "true", "min_rvol": 3}, {"enabled": True, "min_rvol": "3"},
                                 {"enabled": True}, {"min_rvol": 3}, {"enabled": 1, "min_rvol": 3}, {"enabled": True, "min_rvol": True}])
def test_settings_radar_push_rejects_bad_values_without_writing(tmp_path, bad):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 0.5})
    before = _tree_hash(tmp_path)
    r = _client(tmp_path).post("/api/opportunities/settings", json={"long": ["a"], "short": ["b"], "radar_push": bad})
    assert r.status_code == 422, (bad, r.status_code)
    assert _tree_hash(tmp_path) == before


# ---------- 0.39.0 radar_view ----------

def test_page_radar_view_is_read_only_and_isolated(tmp_path, monkeypatch):
    """雷达段: /page 只读 (不落帧); 雷达算失败只空这一段; 机会板失败时雷达不给 (不把读不到画成没挂单)。"""
    from app.services import opp_radar
    _stub_sections(monkeypatch, _swing(_card("NVDA"), _card("AMD", "short", 150.0)))
    monkeypatch.setattr(opp_radar, "_watch_rows_and_groups", lambda: ([{"symbol": "NVDA", "group_ids": []}], []))
    before = _tree_hash(tmp_path)
    d = _client(tmp_path).get("/api/opportunities/page").json()
    assert _tree_hash(tmp_path) == before and not opp_radar.frames_dir(tmp_path).exists()
    rv = d["radar_view"]
    assert rv["contract"] == opp_radar.CONTRACT and {b["symbol"] for b in rv["blips"]} >= {"NVDA", "AMD"}
    assert d["swing"] == _swing(_card("NVDA"), _card("AMD", "short", 150.0))     # 雷达不改板卡任何字段
    monkeypatch.setattr(opp_radar, "project", lambda **k: 1 / 0)
    d2 = _client(tmp_path).get("/api/opportunities/page").json()
    assert d2["radar_view"] is None and d2["errors"]["radar_view"] == "ZeroDivisionError" and d2["swing"]["long"]
    monkeypatch.setattr(sd, "build_live", lambda *a, **k: 1 / 0)
    d3 = _client(tmp_path).get("/api/opportunities/page").json()
    assert d3["radar_view"] is None and "radar_view" not in d3["errors"] and d3["errors"]["swing"] == "ZeroDivisionError"
