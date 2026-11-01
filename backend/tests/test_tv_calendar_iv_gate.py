"""① 宏观日历接入 TradingView 做多周前瞻 ② 非交易时段不展示期权 IV。

不变量:
- 本周事件以 ForexFactory 为准, TradingView 只补 FF 覆盖周之后的日子 (两源一致预期有小差, 不混);
  TradingView 挂了只是没有前瞻, 本周日历、FRED 实际值照旧; 页面请求不出网。
- IV 只在「现在是盘中」且「这份链也是盘中拉的」时给; 跨式预期波动、PCR、持仓墙不受影响 (只用报价与持仓量)。
- 开盘前拉的当天免费链, 盘中会被重拉一次; 重拉失败继续给旧链 (不含 IV), 不报不可用。
"""
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.market_clock import us_clock
from app.services import alpaca_options as ao
from app.services import intel_calc as calc
from app.services import intel_panel as panel
from app.services import intel_sources as src
from app.services import options_analytics as oa

ET = us_clock.tz
MON_1000 = datetime(2026, 11, 2, 10, 0, tzinfo=ET)          # 周一盘中
SUN_1400 = datetime(2026, 11, 1, 14, 0, tzinfo=ET)          # 周日


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    from app.services import watchlist
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [{"symbol": "NVDA"}])
    getattr(ao, "_REFETCH_FAILED", {}).clear()


# 11-01 生产实拉 TradingView 的真实条目 (截取), 字段原样
TV_RAW = {"status": "ok", "result": [
    {"id": "1", "title": "ISM Manufacturing PMI", "country": "US", "date": "2026-11-02T15:00:00.000Z", "importance": 1,
     "forecast": 54.6, "previous": 54.5, "actual": None, "unit": None, "scale": None},
    {"id": "2", "title": "Midterm Elections", "country": "US", "date": "2026-11-03T00:00:00.000Z", "importance": 1},
    {"id": "3", "title": "Non Farm Payrolls", "country": "US", "date": "2026-11-06T13:30:00.000Z", "importance": 1,
     "forecast": 65, "previous": 29, "unit": None, "scale": "K"},
    {"id": "4", "title": "Inflation Rate YoY", "country": "US", "date": "2026-11-10T13:30:00.000Z", "importance": 1,
     "forecast": None, "previous": 3.7, "unit": None, "scale": None},                      # 远期条目: 单位缺失
    {"id": "4b", "title": "Housing Starts", "country": "US", "date": "2026-11-18T13:30:00.000Z", "importance": 1,
     "previous": 1.221, "unit": None, "scale": None},
    {"id": "4c", "title": "ISM Services PMI", "country": "US", "date": "2026-11-04T15:00:00.000Z", "importance": 1,
     "previous": 54.9, "unit": None, "scale": None},                                       # 指数: 不补单位
    {"id": "5", "title": "Core PPI MoM", "country": "US", "date": "2026-11-13T13:30:00.000Z", "importance": 0,
     "previous": 0.3, "unit": "%"},
    {"id": "6", "title": "Initial Jobless Claims", "country": "US", "date": "2026-11-12T13:30:00.000Z", "importance": 0},
    {"id": "7", "title": "Veterans Day", "country": "US", "date": "2026-11-11T00:00:00.000Z", "importance": -1},
    {"id": "8", "title": "FOMC Minutes", "country": "US", "date": "2026-11-18T19:00:00.000Z", "importance": 1},
    {"id": "9", "title": "German ifo", "country": "DE", "date": "2026-11-10T09:00:00.000Z", "importance": 1},
    {"id": "10", "title": "Fed Logan Speech", "country": "US", "date": "2026-11-06T00:00:00.000Z", "importance": 0},
    {"id": "11", "title": "Far Away", "country": "US", "date": "2026-12-20T13:30:00.000Z", "importance": 1},
]}


# ── TradingView 解析 ──────────────────────────────────────────────

def test_parse_tv_events_shape_impact_units_and_all_day():
    ev = src.parse_tv_events(TV_RAW, after=date(2026, 11, 1), until=date(2026, 11, 22))
    by = {e["title"]: e for e in ev}
    assert "German ifo" not in by and "Far Away" not in by                      # 只要美国、只要窗口内
    nfp = by["Non Farm Payrolls"]
    assert nfp["at"] == "2026-11-06T08:30-05:00" and nfp["impact"] == "high"   # UTC → 美东 (11 月已是 EST)
    assert nfp["forecast"] == "65K" and nfp["previous"] == "29K" and nfp["source"] == "tradingview"
    assert by["Inflation Rate YoY"]["previous"] == "3.7%" and by["Inflation Rate YoY"]["forecast"] is None
    assert by["Housing Starts"]["previous"] == "1.221M" and by["ISM Services PMI"]["previous"] == "54.9"
    assert by["Inflation Rate YoY"]["title_zh"] == "CPI 同比"
    assert by["Core PPI MoM"]["impact"] == "high"                               # TV 标 0, FF 标高: 升一档
    assert by["Initial Jobless Claims"]["impact"] == "medium"                   # FF 也是中: 不升
    assert by["Veterans Day"]["impact"] == "low"
    # UTC 00:00 = 只给日期的全天事件: 留在当天, 不被换算成前一天 19:00
    assert by["Midterm Elections"]["all_day"] is True and by["Midterm Elections"]["at"] == "2026-11-03T00:00"
    # 讲话是有时刻的: 00:00Z = 美东前一天 19:00, 不能当成第二天全天
    assert by["Fed Logan Speech"]["at"] == "2026-11-05T19:00-05:00" and by["Fed Logan Speech"]["all_day"] is False


def test_fetch_tv_lookahead_sends_window_and_rejects_bad_payload():
    seen = []
    ev = src.fetch_tv_lookahead(SUN_1400, http_get=lambda u: seen.append(u) or TV_RAW)
    assert "countries=US" in seen[0] and "from=2026-11-01T00:00:00.000Z" in seen[0]
    assert "to=2026-11-29T05:00:00.000Z" in seen[0]                             # 本周六 (11-07) + 21 天: 周日拉也满 3 周
    assert {e["title"] for e in ev} >= {"Non Farm Payrolls", "FOMC Minutes"}
    with pytest.raises(ValueError):
        src.fetch_tv_lookahead(SUN_1400, http_get=lambda u: {"status": "error"})
    with pytest.raises(ValueError):
        src.fetch_tv_lookahead(SUN_1400, http_get=lambda u: [])


def test_default_request_carries_origin_header(monkeypatch):
    """实测: 不带 Origin 403, 只带 Referer 也 403。"""
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(TV_RAW).encode()

    def fake_urlopen(req, timeout=None):
        captured.update({k.lower(): v for k, v in req.header_items()})
        return Resp()
    monkeypatch.setattr(src.urllib.request, "urlopen", fake_urlopen)
    src.fetch_tv_lookahead(SUN_1400)
    assert captured["origin"] == "https://www.tradingview.com"


# ── 日历视图: 本周 FF 为准, 之后 TradingView ───────────────────────

def _seed_calendar(d: Path, *, ff=True, tv=True):
    root = d / "intel"
    root.mkdir(parents=True, exist_ok=True)
    if ff:   # FF 本周 (周日 11-01 到 周六 11-07)
        (root / "macro_calendar.json").write_text(json.dumps({"fetched_at": "2026-11-01T13:16:31-05:00", "events": [
            {"at": "2026-11-02T10:00-05:00", "title": "ISM Manufacturing PMI", "impact": "medium", "forecast": "54.7"},
            {"at": "2026-11-06T08:30-05:00", "title": "Non-Farm Employment Change", "impact": "high", "forecast": "63K"}]}))
    if tv:
        ev = src.parse_tv_events(TV_RAW, after=date(2026, 10, 31), until=date(2026, 11, 22))
        (root / "macro_lookahead.json").write_text(json.dumps({"fetched_at": "2026-11-01T13:20:00-05:00", "events": ev}))


def test_calendar_uses_ff_this_week_and_tv_after(tmp_path):
    _seed_calendar(tmp_path)
    cal = panel.calendar_view(tmp_path, SUN_1400, {}, {"macro_impact": "high_medium"})
    macro = [e for e in cal["events"] if e["kind"] == "macro"]
    this_week = [e for e in macro if e["day"] <= "2026-11-07"]
    assert all(e.get("source") is None for e in this_week)                       # 本周全是 FF
    assert [e["forecast"] for e in this_week if "Non" in e["title"]] == ["63K"]    # 不混进 TV 的 65K
    later = [e for e in macro if e["day"] > "2026-11-07"]
    assert {e["title"] for e in later} >= {"Inflation Rate YoY", "Core PPI MoM", "FOMC Minutes"}
    assert all(e["source"] == "tradingview" and e["actual"] is None for e in later)
    assert "Veterans Day" not in {e["title"] for e in later}                      # 高+中档: 低的不显示
    assert cal["lookahead_from"] == "2026-11-10" and "TradingView" in cal["source"]
    assert cal["ff_until"] == "2026-11-07"


def test_calendar_without_ff_falls_back_to_tv_from_today(tmp_path):
    _seed_calendar(tmp_path, ff=False)
    cal = panel.calendar_view(tmp_path, SUN_1400, {}, {"macro_impact": "high"})
    titles = [e["title"] for e in cal["events"] if e["kind"] == "macro"]
    assert "Non Farm Payrolls" in titles and "Midterm Elections" in titles        # FF 缺: 本周也用 TV 顶上
    mid = next(e for e in cal["events"] if e["title"] == "Midterm Elections")
    assert mid["time"] == "" and mid["day"] == "2026-11-03"
    assert cal["ff_until"] is None and "ForexFactory 暂缺" in cal["source"]        # 前端据此不把本周收进「前瞻」


def test_calendar_without_tv_is_unchanged(tmp_path):
    _seed_calendar(tmp_path, tv=False)
    cal = panel.calendar_view(tmp_path, SUN_1400, {}, {"macro_impact": "high_medium"})
    assert {e["title"] for e in cal["events"] if e["kind"] == "macro"} == {"ISM Manufacturing PMI", "Non-Farm Employment Change"}
    assert cal["lookahead_from"] is None and cal["source"] == "ForexFactory 本周日历 · 实际值 FRED"


def test_tick_runs_lookahead_and_failure_keeps_old_cache(tmp_path, monkeypatch):
    _seed_calendar(tmp_path)
    before = (tmp_path / "intel" / "macro_lookahead.json").read_text()
    monkeypatch.setattr(src, "fetch_tv_lookahead", lambda now: (_ for _ in ()).throw(OSError("403")))
    r = panel.tick(tmp_path, None, None, now=MON_1000, only={"macro_lookahead"})
    assert str(r["macro_lookahead"]).startswith("error")
    assert (tmp_path / "intel" / "macro_lookahead.json").read_text() == before    # 失败不覆盖上一份
    st = json.loads((tmp_path / "intel" / "state.json").read_text())
    assert "OSError" in st["macro_lookahead"]["error"]
    cal = panel.calendar_view(tmp_path, MON_1000, {}, {"macro_impact": "high_medium"})
    assert cal["lookahead_error"] and cal["error"] is None                        # 只标前瞻, 不连累本周日历
    monkeypatch.setattr(src, "fetch_tv_lookahead", lambda now: [{"at": "2026-11-10T08:30-05:00", "title": "X", "impact": "high",
                                                                 "source": "tradingview"}])
    panel.tick(tmp_path, None, None, now=MON_1000 + timedelta(hours=7), only={"macro_lookahead"})
    assert json.loads((tmp_path / "intel" / "macro_lookahead.json").read_text())["events"][0]["title"] == "X"


def test_lookahead_is_manual_refreshable_and_slow_cadence():
    assert "macro_lookahead" in panel.MANUAL_SOURCES
    assert panel.DUE["macro_lookahead"] == (6 * 3600, 6 * 3600)


# ── IV 时段门 ─────────────────────────────────────────────────────

def test_iv_visible_only_in_session_with_session_fetched_chain():
    assert oa.iv_visible("2026-11-02T15:01:00Z", MON_1000) is True               # 10:01 ET 拉, 10:00 看 (同盘中)
    assert oa.iv_visible("2026-11-02T13:00:00Z", MON_1000) is False              # 08:00 ET 盘前拉的
    assert oa.iv_visible("2026-10-30T19:00:00Z", SUN_1400) is False              # 周日: 不管哪份链
    assert oa.iv_visible("2026-11-02T15:01:00Z", datetime(2026, 11, 2, 17, 0, tzinfo=ET)) is False   # 收盘后
    assert oa.iv_visible(None, MON_1000) is False and oa.iv_visible("garbage", MON_1000) is False
    assert oa.iv_visible(1793640000, MON_1000) is False                                # 不是时刻字符串: 不给
    # 上周五盘中拉的链, 周一盘中看: 数据已隔了交易日, 不给 (付费缓存回退会走到这里)
    assert oa.iv_visible("2026-10-30T19:00:00Z", MON_1000) is False
    assert oa.iv_visible(datetime(2026, 11, 2, 11, 0, tzinfo=UTC).astimezone(ET).replace(hour=11), MON_1000) is True


def _rows(iv=0.4):
    rows = []
    for exp in ("2026-11-06", "2026-11-20"):
        for k in (95.0, 100.0, 105.0):
            for typ in ("call", "put"):
                d = (0.25 if k == 105 else 0.5) if typ == "call" else (-0.25 if k == 95 else -0.5)
                rows.append({"contractID": f"N{exp}{typ}{k}", "expiration": exp, "strike": k, "type": typ, "bid": 2.0,
                             "ask": 2.2, "mark": 2.1, "volume": 3000, "open_interest": 100, "implied_volatility": iv,
                             "delta": d})
    return rows


def test_strip_iv_keeps_price_based_fields():
    a = oa.analyze(_rows(), 100.0, today=date(2026, 11, 2), earnings_date=date(2026, 11, 5))
    assert a["skew"] and a["term"][0]["atm_iv"] == 0.4 and a["unusual"][0]["iv"] == 0.4
    s = oa.strip_iv(a)
    assert s["iv_hidden"] is True and s["skew"] is None
    assert all(t["atm_iv"] is None for t in s["term"]) and all(u["iv"] is None for u in s["unusual"])
    assert s["earnings_move"]["atm_iv"] is None
    assert s["earnings_move"]["expected_move_pct"] == a["earnings_move"]["expected_move_pct"]   # 跨式照给
    assert s["pcr"] == a["pcr"] and s["oi_walls"] == a["oi_walls"] and s["heatmap"] == a["heatmap"]
    assert [t["straddle"] for t in s["term"]] == [t["straddle"] for t in a["term"]]
    assert oa.strip_iv({"ok": False, "reason": "x"}) == {"ok": False, "reason": "x"}


def _free(d: Path, sym: str, day: date, fetched_at: str, rows=None):
    p = ao._dir(d, sym) / f"{day.isoformat()}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"available": True, "rows": rows or _rows(), "source": "alpaca", "fetched_at": fetched_at}))
    return p


def test_opp_card_iv_hidden_off_hours_and_for_preopen_chain(tmp_path, monkeypatch):
    from app.services import opp_page
    warmed = []
    monkeypatch.setattr(opp_page, "_warm_options", lambda d, s, t: warmed.append(s))
    _free(tmp_path, "NVDA", MON_1000.date(), "2026-11-02T15:01:00Z")             # 盘中拉的
    monkeypatch.setattr(us_clock, "now", lambda: MON_1000.replace(hour=11))
    o = opp_page._options(tmp_path, "NVDA", 100.0, MON_1000.date(), date(2026, 11, 5))
    assert o["iv"] == 0.4 and warmed == []
    monkeypatch.setattr(us_clock, "now", lambda: MON_1000.replace(hour=17))      # 收盘后: 同一份链也不给
    o = opp_page._options(tmp_path, "NVDA", 100.0, MON_1000.date(), date(2026, 11, 5))
    assert o["iv"] is None and o["pcr"] == 1.0
    _free(tmp_path, "NVDA", MON_1000.date(), "2026-11-02T12:30:00Z")             # 07:30 ET 盘前拉的
    monkeypatch.setattr(us_clock, "now", lambda: MON_1000)
    o = opp_page._options(tmp_path, "NVDA", 100.0, MON_1000.date(), date(2026, 11, 5))
    assert o["iv"] is None and warmed == ["NVDA"]                                # 盘中: 后台重拉, 本次不给 IV


def test_intel_implied_move_keeps_straddle_drops_iv_off_hours(tmp_path):
    _free(tmp_path, "NVDA", SUN_1400.date(), "2026-11-01T17:00:00Z")
    r = calc.implied_move(tmp_path, "NVDA", 100.0, date(2026, 11, 5), SUN_1400.date(), now=SUN_1400)
    assert r["expected_move_pct"] > 0 and r["atm_iv"] is None
    _free(tmp_path, "NVDA", MON_1000.date(), "2026-11-02T14:45:00Z")             # 09:45 ET 拉
    r = calc.implied_move(tmp_path, "NVDA", 100.0, date(2026, 11, 5), MON_1000.date(), now=MON_1000)
    assert r["atm_iv"] == 0.4


def test_options_panel_free_chain_iv_gate_and_preopen_refetch(tmp_path, monkeypatch):
    from app.services import qveris_data as qd
    monkeypatch.setattr(qd, "spot_price", lambda repo, s: (100.0, "测试"))
    monkeypatch.setattr(us_clock, "now", lambda: SUN_1400)
    _free(tmp_path, "NVDA", SUN_1400.date(), "2026-11-01T17:00:00Z")
    r = qd.options_summary(tmp_path, None, "NVDA", refresh=False)
    assert r["source"] == "alpaca" and r["analysis"]["iv_hidden"] is True and r["analysis"]["skew"] is None
    # 周一盘中: 开盘前拉的当天缓存要重拉; 重拉拿到盘中链 → 给 IV
    _free(tmp_path, "NVDA", MON_1000.date(), "2026-11-02T12:30:00Z")
    monkeypatch.setattr(us_clock, "now", lambda: MON_1000)
    seen = {}

    def chain(data_dir, sym, today, min_fetched=None):
        seen["cut"] = min_fetched
        return {"available": True, "rows": _rows(0.33), "source": "alpaca", "fetched_at": "2026-11-02T15:00:30Z"}
    monkeypatch.setattr(ao, "chain", chain)
    r = qd.options_summary(tmp_path, None, "NVDA", refresh=False)
    assert seen["cut"] == datetime(2026, 11, 2, 9, 30, tzinfo=ET)
    assert r["analysis"].get("iv_hidden") is None and r["analysis"]["term"][0]["atm_iv"] == 0.33


def test_options_panel_paid_chain_same_rule(tmp_path, monkeypatch):
    from app.services import qveris_data as qd
    from app.services import qveris_gate as gate
    monkeypatch.setattr(qd, "spot_price", lambda repo, s: (100.0, "测试"))
    monkeypatch.setattr(qd, "_free_options", lambda *a, **k: None)              # 免费源不可用 → 退回付费缓存
    fetched = datetime(2026, 10, 30, 15, 0, tzinfo=ET)                           # 周五盘中付费查的
    monkeypatch.setattr(gate, "cache_get", lambda d, kind, sym, *a: {"data": {"data": _rows(0.5)}, "fetched_at": "x",
                                                                    "fetched_ts": fetched.timestamp()})
    monkeypatch.setattr(us_clock, "now", lambda: SUN_1400)
    r = qd.options_summary(tmp_path, None, "NVDA", refresh=False)
    assert r["analysis"]["iv_hidden"] is True                                   # 周日看: 不给
    monkeypatch.setattr(us_clock, "now", lambda: fetched + timedelta(minutes=5))
    r = qd.options_summary(tmp_path, None, "NVDA", refresh=False)
    assert r["analysis"]["term"][0]["atm_iv"] == 0.5                            # 同一盘中: 给


# ── 免费链缓存: 盘前拉的当天缓存可被替换 ───────────────────────────

def _fetch_ok(symbol, today):
    contracts = {"N1": {"underlying_symbol": symbol, "expiration_date": "2026-11-06", "strike_price": "100", "type": "call",
                        "open_interest": "10"}}
    snaps = {"N1": {"latestQuote": {"bp": 1.0, "ap": 1.2}, "impliedVolatility": 0.3}}
    return contracts, snaps


def test_chain_min_fetched_refetches_and_keeps_old_on_failure(tmp_path):
    day = MON_1000.date()
    cut = datetime(2026, 11, 2, 9, 30, tzinfo=ET)
    p = _free(tmp_path, "NVDA", day, "2026-11-02T12:30:00Z")
    assert ao.chain(tmp_path, "NVDA", today=day, fetch=_fetch_ok)["cached"] is True          # 不给 cutoff: 照旧读缓存
    calls = []
    r = ao.chain(tmp_path, "NVDA", today=day, fetch=lambda s, t: calls.append(s) or _fetch_ok(s, t), min_fetched=cut)
    assert calls == ["NVDA"] and r["cached"] is False and json.loads(p.read_text())["rows"][0]["contractID"] == "N1"
    _free(tmp_path, "NVDA", day, "2026-11-02T12:30:00Z")

    def boom(s, t):
        raise OSError("down")
    r = ao.chain(tmp_path, "NVDA", today=day, fetch=boom, min_fetched=cut)
    assert r["available"] is True and r["cached"] is True and r["fetched_at"] == "2026-11-02T12:30:00Z"
    fresh = _free(tmp_path, "NVDA", day, "2026-11-02T15:00:00Z")
    calls.clear()
    r = ao.chain(tmp_path, "NVDA", today=day, fetch=lambda s, t: calls.append(s) or _fetch_ok(s, t), min_fetched=cut)
    assert calls == [] and r["cached"] is True and fresh.exists()                             # 盘中拉的: 不重拉


def test_ai_context_has_no_iv_when_hidden():
    from app.services import stock_analyzer as sa
    a = oa.strip_iv(oa.analyze(_rows(), 100.0, today=date(2026, 11, 2), earnings_date=None))
    text = sa._paid_part({"options": {"analysis": a}})
    assert "隐含波动率" not in text.replace("隐含波动率非交易时段不提供", "") and "IV 差" not in text
    assert "跨式预期波动" in text


def test_ff_stuck_on_old_week_lets_tv_cover_this_week(tmp_path):
    _seed_calendar(tmp_path)
    nxt = SUN_1400 + timedelta(days=7)                                             # FF 还停在上周 (11-01 那周)
    cal = panel.calendar_view(tmp_path, nxt, {}, {"macro_impact": "high"})
    assert cal["ff_until"] is None and "ForexFactory 暂缺" in cal["source"]


def test_fetch_window_reaches_three_weeks_past_ff_week_any_weekday():
    for now in (SUN_1400, SUN_1400 + timedelta(days=5)):                          # 周日 / 周五拉
        seen: list[str] = []

        def get(u, seen=seen):
            seen.append(u)
            return {"status": "ok", "result": []}
        src.fetch_tv_lookahead(now, http_get=get)
        to = date.fromisoformat(seen[0].split("to=")[1][:10])
        assert (to - timedelta(days=1)) - src.week_end(now.date()) == timedelta(days=src.TV_DAYS)


def test_opp_context_cache_not_reused_across_close(tmp_path, monkeypatch):
    from app.services import opp_page
    monkeypatch.setattr(opp_page, "card_context", lambda d, syms, prices, today: ({"NVDA": {"iv": 0.4 if us_clock.in_session() else None}}, {}))
    opp_page._CTX_CACHE.clear()
    monkeypatch.setattr(us_clock, "now", lambda: MON_1000.replace(hour=15, minute=59))
    assert opp_page._cached_context(tmp_path, ["NVDA"], {}, MON_1000.date())[0]["NVDA"]["iv"] == 0.4
    monkeypatch.setattr(us_clock, "now", lambda: MON_1000.replace(hour=16, minute=1))
    assert opp_page._cached_context(tmp_path, ["NVDA"], {}, MON_1000.date())[0]["NVDA"]["iv"] is None   # 收盘后不复用盘中 IV
    opp_page._CTX_CACHE.clear()


def test_preopen_refetch_failure_backs_off(tmp_path):
    day = MON_1000.date()
    cut = datetime(2026, 11, 2, 9, 30, tzinfo=ET)
    _free(tmp_path, "NVDA", day, "2026-11-02T12:30:00Z")
    calls = []

    def boom(s, t):
        calls.append(s)
        raise OSError("down")
    for _ in range(3):                                                            # 上游挂时连开 3 次弹窗
        r = ao.chain(tmp_path, "NVDA", today=day, fetch=boom, min_fetched=cut)
        assert r["available"] is True and r["cached"] is True
    assert calls == ["NVDA"]                                                      # 只同步试一次, 之后先给旧链
    ao._REFETCH_FAILED[(str(tmp_path), "NVDA", day.isoformat())] -= ao.REFETCH_RETRY_S + 1
    r = ao.chain(tmp_path, "NVDA", today=day, fetch=lambda s, t: calls.append(s) or _fetch_ok(s, t), min_fetched=cut)
    assert calls == ["NVDA", "NVDA"] and r["cached"] is False                     # 冷却过后再试, 成功就换新链
    assert (str(tmp_path), "NVDA", day.isoformat()) not in ao._REFETCH_FAILED
