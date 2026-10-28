"""0.40.0 盘面情报面板: 数据源解析、宏观实际值对期、宽度口径、财报反应、聚合只读与区块隔离、刷新节奏。

不变量: GET /api/intel/today 只读 (不写任何文件、不出网); 某块失败只空那一块;
宏观实际值只在 FRED 最新一期 = 本次公布对应期时给出; 不进交易链 (不 import 交易环/台账写口)。
"""
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_clock import us_clock
from app.services import intel_calc as calc
from app.services import intel_panel as panel
from app.services import intel_sources as src

ET = us_clock.tz
NOW = datetime(2026, 10, 29, 10, 0, tzinfo=ET)          # 周四 10:00 ET


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    panel._LIVE_CACHE.clear()
    from app.services import watchlist
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [{"symbol": "NVDA"}, {"symbol": "TSLA"}, {"symbol": "AMD"}])


# ── 数据源解析 ──────────────────────────────────────────────────────

def test_parse_cboe_quote_and_vol_ratio():
    body = {"timestamp": "2026-10-26 09:06:12", "data": {"current_price": 19.24, "prev_day_close": 19.45,
                                                         "price_change_percent": -1.0797, "last_trade_time": "2026-10-23T16:15:01"}}
    q = src.parse_cboe_quote(body)
    assert q == {"value": 19.24, "prev_close": 19.45, "chg_pct": -1.08, "as_of": "2026-10-23T16:15:01"}
    assert src.parse_cboe_quote({"data": {"current_price": 0}}) is None

    def http(u):
        return {"data": {"current_price": 20.0 if "VIX3M" in u else 22.0}}
    v = src.fetch_vol(http)
    assert v["ratio"] == 1.1 and v["vix"]["value"] == 22.0


def test_fetch_vol_raises_without_vix():
    def http(u):
        raise OSError("down")
    with pytest.raises(ValueError):
        src.fetch_vol(http)


def test_parse_snapshot_live_vs_previous_day():
    today = date(2026, 10, 26)
    snap = {"dailyBar": {"c": 183.24, "t": "2026-10-23T04:00:00Z"}, "prevDailyBar": {"c": 182.33, "t": "2026-10-22T04:00:00Z"},
            "latestTrade": {"p": 183.0, "t": "2026-10-23T19:59:59Z"}}
    r = src.parse_snapshot(snap, today)          # 周一盘前: 今天还没日线 → 给上周五的涨跌, 标那一天
    assert r["live"] is False and r["day"] == "2026-10-23" and r["chg_pct"] == round((183.24 / 182.33 - 1) * 100, 2)
    snap_live = {"dailyBar": {"c": 185.0, "t": "2026-10-26T04:00:00Z"}, "prevDailyBar": {"c": 183.24},
                 "latestTrade": {"p": 186.0, "t": "2026-10-26T14:00:00Z"}}
    r2 = src.parse_snapshot(snap_live, today)    # 盘中: 最新成交价对前一日收盘
    assert r2["live"] is True and r2["price"] == 186.0 and r2["chg_pct"] == round((186 / 183.24 - 1) * 100, 2)
    assert src.parse_snapshot({"dailyBar": {"c": 1}}, today) is None


def test_combine_quote_uses_official_prev_close():
    today = date(2026, 10, 26)
    daily = {"close": 704.4, "prev_close": 704.47, "day": "2026-10-23", "chg_pct": -0.01, "d5": -1.2}
    live = {"price": 710.0, "live": True, "as_of": "2026-10-26T14:00:00Z"}
    r = src.combine_quote(live, daily, today)
    assert r["live"] and r["chg_pct"] == round((710 / 704.4 - 1) * 100, 2) and r["base"] == "官方前收"
    stale = {"price": 704.1, "live": False, "chg_pct": -0.05}
    r2 = src.combine_quote(stale, daily, today)                     # 盘前: 只给官方日K涨跌, 不用 IEX 单所前收
    assert r2["live"] is False and r2["chg_pct"] == -0.01 and r2["day"] == "2026-10-23"
    assert src.combine_quote(stale, None, today)["base"] == "IEX 前收"
    assert src.combine_quote(None, None, today) is None


def test_update_pcr_backs_off_unpublished_day(tmp_path):
    calls = []

    def http(u):
        calls.append(u)
        if "2026-10-28" in u:
            raise OSError("403")
        return {"ratios": [{"name": "EQUITY PUT/CALL RATIO", "value": "0.7"}]}
    src.update_pcr(tmp_path, NOW, days=3, http_get=http)
    assert len(calls) == 3
    calls.clear()
    src.update_pcr(tmp_path, NOW + timedelta(minutes=30), days=3, http_get=http)
    assert calls == []
    src.update_pcr(tmp_path, NOW + timedelta(hours=3), days=3, http_get=http)
    assert len(calls) == 1 and "2026-10-28" in calls[0]


def test_parse_pcr_and_view_avg():
    body = {"ratios": [{"name": "TOTAL PUT/CALL RATIO", "value": "0.93"}, {"name": "EQUITY PUT/CALL RATIO", "value": "0.67"},
                       {"name": "INDEX PUT/CALL RATIO", "value": "1.10"}, {"name": "OEX PUT/CALL RATIO", "value": "3.00"}]}
    assert src.parse_pcr(body) == {"total": 0.93, "equity": 0.67, "index": 1.1}
    assert src.parse_pcr({"ratios": []}) is None
    cache = {"days": {f"2026-10-{d:02d}": {"equity": 0.5 + d / 100, "total": 0.9} for d in range(1, 23)}}
    v = src.pcr_view(cache)
    assert v["day"] == "2026-10-22" and v["n_hist"] == 20
    assert v["equity_avg20"] == round(sum(0.5 + d / 100 for d in range(2, 22)) / 20, 2)


def test_update_pcr_only_fetches_missing_and_not_today_before_close(tmp_path):
    calls = []

    def http(u):
        calls.append(u)
        return {"ratios": [{"name": "EQUITY PUT/CALL RATIO", "value": "0.7"}]}
    src.update_pcr(tmp_path, NOW, days=5, http_get=http)
    assert len(calls) == 5 and not any("2026-10-29" in u for u in calls)      # 10:00 不拉今天
    calls.clear()
    src.update_pcr(tmp_path, NOW, days=5, http_get=http)
    assert calls == []                                                          # 已有的不重拉
    src.update_pcr(tmp_path, NOW.replace(hour=17), days=5, http_get=http)
    assert len(calls) == 1 and "2026-10-29" in calls[0]


def test_fetch_ff_week_usd_only_and_et_time():
    data = [{"title": "Advance GDP q/q", "country": "USD", "date": "2026-10-29T08:30:00-04:00", "impact": "High",
             "forecast": "3.2%", "previous": "1.5%"},
            {"title": "German ifo", "country": "EUR", "date": "2026-10-26T04:00:00-04:00", "impact": "High"},
            {"title": "Bank Holiday", "country": "USD", "date": "2026-10-26T00:00:00-04:00", "impact": "Holiday"}]
    ev = src.fetch_ff_week(lambda u: data)
    assert len(ev) == 1 and ev[0]["at"] == "2026-10-29T08:30-04:00" and ev[0]["impact"] == "high"


# ── 宏观实际值: 对期 + 计算 ─────────────────────────────────────────

def test_ref_ok_monthly_quarterly_weekly():
    released = date(2026, 10, 15)
    assert src.ref_ok({"freq": "M", "lag": 1}, date(2026, 9, 1), released)
    assert not src.ref_ok({"freq": "M", "lag": 1}, date(2026, 8, 1), released)      # FRED 还没上新: 不能拿上月当实际
    assert src.ref_ok({"freq": "M", "lag": 2}, date(2026, 8, 1), released)          # JOLTS 晚两个月
    assert src.ref_ok({"freq": "Q", "lag": 1}, date(2026, 7, 1), date(2026, 10, 29))  # 10 月底公布 Q3
    assert not src.ref_ok({"freq": "Q", "lag": 1}, date(2026, 4, 1), date(2026, 10, 29))
    assert src.ref_ok({"freq": "W"}, date(2026, 10, 24), date(2026, 10, 29))
    assert not src.ref_ok({"freq": "W"}, date(2026, 10, 17), date(2026, 10, 29))


def test_compute_actual_kinds():
    obs = [(date(2025, 9, 1), 324.0), (date(2026, 8, 1), 334.131), (date(2026, 9, 1), 336.298)]
    assert src.compute_actual({"kind": "pct"}, obs) == (0.6, "0.6%")
    assert src.compute_actual({"kind": "yoy"}, obs) == (3.8, "3.8%")
    assert src.compute_actual({"kind": "diff_k"}, [(date(2026, 8, 1), 159015), (date(2026, 9, 1), 159044)]) == (29, "29K")
    assert src.compute_actual({"kind": "k"}, [(date(2026, 10, 17), 194000)]) == (194, "194K")
    assert src.compute_actual({"kind": "m_from_k"}, [(date(2026, 9, 1), 1221)]) == (1.22, "1.22M")
    assert src.compute_actual({"kind": "level", "unit": "%", "dp": 1}, [(date(2026, 7, 1), 2.2)]) == (2.2, "2.2%")


def test_surprise_direction_by_display_precision():
    assert src.surprise("0.3%", "0.3%") == "inline"
    assert src.surprise("0.4%", "0.3%") == "above"
    assert src.surprise("0.4%", "0.3%", derived=True) == "near"          # 推算值差 0.1: 在取整误差内
    assert src.surprise("0.5%", "0.3%", derived=True) == "above"
    assert src.surprise("194K", "198K") == "below"
    assert src.surprise("1.22M", "1.30M") == "below"
    assert src.surprise("0.3%", None) is None


def test_update_actuals_matches_period_and_backs_off(tmp_path):
    ev = [{"at": "2026-10-29T08:30-04:00", "title": "Core PCE Price Index m/m", "impact": "high", "forecast": "0.3%", "previous": "0.2%"},
          {"at": "2026-10-29T08:30-04:00", "title": "Unemployment Claims", "impact": "medium", "forecast": "198K", "previous": "194K"},
          {"at": "2026-10-30T08:30-04:00", "title": "Employment Cost Index q/q", "impact": "medium", "forecast": "0.8%"}]
    calls = []
    stale = {"PCEPILFE": [(date(2026, 7, 1), 130.133), (date(2026, 8, 1), 130.455)],           # 9 月还没上 FRED
             "ICSA": [(date(2026, 10, 17), 194000), (date(2026, 10, 24), 201000)]}

    def fetch(s, since):
        calls.append(s)
        return stale[s]
    now = datetime(2026, 10, 29, 8, 40, tzinfo=ET)
    c = src.update_actuals(tmp_path, ev, now, fetch=fetch)
    assert c["2026-10-29T08:30|Unemployment Claims"]["actual"] == "201K"
    assert "2026-10-29T08:30|Core PCE Price Index m/m" not in c                     # 期数对不上: 不给
    assert "2026-10-30T08:30|Employment Cost Index q/q" not in c                     # 还没公布: 不查
    assert sorted(calls) == ["ICSA", "PCEPILFE"]
    calls.clear()
    src.update_actuals(tmp_path, ev, now + timedelta(minutes=2), fetch=fetch)
    assert calls == []                                                              # 5 分钟内不重试
    stale["PCEPILFE"].append((date(2026, 9, 1), 130.845))
    c = src.update_actuals(tmp_path, ev, now + timedelta(minutes=6), fetch=fetch)
    assert calls == ["PCEPILFE"] and c["2026-10-29T08:30|Core PCE Price Index m/m"]["actual"] == "0.3%"


def test_prev_matches_guards_definition_mismatch(tmp_path):
    """10-26 生产对拍: FRED 实际 GDP 上一期 2.2 vs ForexFactory 前值 1.5 → 口径不一致, 不给实际值。"""
    gdp = src.FRED_MAP["Advance GDP q/q"]
    obs = [(date(2026, 4, 1), 2.2), (date(2026, 7, 1), 3.1)]
    assert src.prev_matches(gdp, obs, "1.5%") is False
    assert src.prev_matches(gdp, obs, "2.2%") is True
    assert src.prev_matches(gdp, obs, None) is None
    claims = src.FRED_MAP["Unemployment Claims"]
    assert src.prev_matches(claims, [(date(2026, 10, 17), 194000), (date(2026, 10, 24), 201000)], "194K") is True
    ev = [{"at": "2026-10-29T08:30-04:00", "title": "Advance GDP q/q", "impact": "high", "forecast": "3.2%", "previous": "1.5%"}]
    c = src.update_actuals(tmp_path, ev, datetime(2026, 10, 29, 8, 40, tzinfo=ET), fetch=lambda s_, since: obs)
    assert c["2026-10-29T08:30|Advance GDP q/q"]["status"] == "mismatch" and "actual" not in c["2026-10-29T08:30|Advance GDP q/q"]


def test_fred_map_titles_have_chinese_names():
    assert set(src.FRED_MAP) <= set(src.TITLE_ZH)


# ── 宽度 ───────────────────────────────────────────────────────────

def _write_daily(data_dir: Path, days: list[date], prices: dict[str, list[float]], vol=1_000_000):
    for i, d in enumerate(days):
        rows = [{"symbol": s, "date": d, "close": p[i], "high": p[i] * 1.01, "low": p[i] * 0.99, "volume": vol,
                 "raw_close": p[i]} for s, p in prices.items()]
        out = data_dir / "kline_daily_enriched" / f"date={d.isoformat()}"
        out.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(rows).write_parquet(out / "part.parquet")


def test_compute_breadth_universe_filters_and_counts(tmp_path):
    days = us_clock.trading_days_between(date(2026, 6, 1), date(2026, 10, 23))
    n = len(days)
    prices = {"UP": [10 + i * 0.1 for i in range(n)], "DOWN": [50 - i * 0.1 for i in range(n)],
              "PENNY": [1.0] * n, "ETFX": [20 + i * 0.1 for i in range(n)]}
    _write_daily(tmp_path, days, prices)
    inst = tmp_path / "instruments"
    inst.mkdir()
    pl.DataFrame({"symbol": ["UP", "DOWN", "PENNY", "ETFX"],
                  "instrument_type": ["common_stock", "common_stock", "common_stock", "etf"]}).write_parquet(inst / "instruments.parquet")
    b = calc.compute_breadth(tmp_path)
    assert b["as_of"] == "2026-10-23" and b["universe"] == 2              # 仙股、ETF 不进
    assert (b["adv"], b["dec"]) == (1, 1) and b["up_pct"] == 50.0
    assert b["above_ma50_pct"] == 50.0 and len(b["series"]) == 20
    assert set(b["last_close"]) == {"UP", "DOWN"}


def test_breadth_ignores_live_intraday_partition(tmp_path):
    """盘中实时行情会写当天分区; 宽度只用已定版的分区 (quote_official_close.json)。"""
    days = us_clock.trading_days_between(date(2026, 6, 1), date(2026, 10, 29))
    n = len(days)
    _write_daily(tmp_path, days, {"UP": [10 + i * 0.1 for i in range(n)], "DOWN": [50 - i * 0.1 for i in range(n)]})
    (tmp_path / "quote_official_close.json").write_text(json.dumps(["2026-10-26", "2026-10-27"]))
    # 10-28 不在清单 (盘后定版失败) 且在清单覆盖期内 → 排除; 10-29 = 今天盘中 → 排除
    assert calc.latest_partition_day(tmp_path, date(2026, 10, 29)) == "2026-10-27"
    assert calc.compute_breadth(tmp_path, today=date(2026, 10, 29))["as_of"] == "2026-10-27"
    (tmp_path / "quote_official_close.json").write_text(json.dumps(["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]))
    assert calc.latest_partition_day(tmp_path, date(2026, 10, 29)) == "2026-10-29"


def test_fetch_upcoming_keeps_previous_rows_for_failed_days():
    def http(url, headers=None):
        if url.endswith("2026-10-30"):
            raise OSError("down")
        if url.endswith("2026-10-29"):
            return {"data": {"rows": [{"symbol": "AMZN", "time": "time-after-hours", "epsForecast": "$1.50", "noOfEsts": "20"}]}}
        return {"data": {"rows": []}}
    prev = {"rows": {"TSLA": {"date": "2026-10-30", "timing": "bmo"}, "OLD": {"date": "2026-10-20"}}}
    r = calc.fetch_upcoming(NOW.date(), http_get=http, previous=prev)
    assert r["rows"]["AMZN"]["eps_forecast"] == 1.5 and r["rows"]["TSLA"]["timing"] == "bmo"
    assert "OLD" not in r["rows"] and r["failed_days"] == ["2026-10-30"]


def test_live_breadth_uses_snapshot_against_official_close():
    base = {f"S{i}": 10.0 for i in range(300)}
    breadth = {"as_of": "2026-10-28", "last_close": base}
    px = {f"S{i}": (11.0 if i < 200 else 9.0) for i in range(300)}
    repo = SimpleNamespace(_enriched_cache=object(), get_enriched_latest=lambda: (pl.DataFrame({"symbol": list(px), "close": list(px.values())}), NOW.date()))
    r = panel.live_breadth(repo, breadth, NOW.date())
    assert r["n"] == 300 and r["adv"] == 200 and r["up_pct"] == 66.7
    panel._LIVE_CACHE.clear()
    repo2 = SimpleNamespace(_enriched_cache=object(), get_enriched_latest=lambda: (pl.DataFrame({"symbol": list(px), "close": list(px.values())}), date(2026, 10, 28)))
    assert panel.live_breadth(SimpleNamespace(_enriched_cache=None), breadth, NOW.date()) is None   # 缓存冷: 不在请求里重算
    assert panel.live_breadth(repo2, breadth, NOW.date()) is None              # 快照不是今天: 不给盘中近似


# ── 财报历史反应 ───────────────────────────────────────────────────

def test_reaction_moves_bmo_vs_amc():
    closes = {date(2026, 7, 21): 100.0, date(2026, 7, 22): 110.0, date(2026, 7, 23): 99.0}
    rows = [{"date": "2026-07-22", "timing": "bmo"}, {"date": "2026-07-22", "timing": "amc"},
            {"date": "2026-07-22", "timing": "unknown"}, {"date": "2026-01-10", "timing": "amc"}]
    r = calc.reaction_moves(rows, closes)
    assert r[0]["move_pct"] == 10.0 and r[0]["react_day"] == "2026-07-22"     # 盘前: 当天对前一日
    assert r[1]["move_pct"] == -10.0 and r[1]["react_day"] == "2026-07-23"    # 盘后: 次日对当天
    assert r[2]["move_pct"] == -10.0                                          # 未注明按盘后
    assert r[3]["move_pct"] is None                                           # 无日K覆盖
    s = calc.summarize_history(r)
    assert s["n"] == 3 and s["median_abs_move"] == 10.0


def test_earnings_history_rows_reads_event_files(tmp_path):
    ed = tmp_path / "ev"
    ed.mkdir()
    (ed / "2026-07-22.json").write_text(json.dumps({"date": "2026-07-22", "rows": [
        {"symbol": "TSLA", "time": "time-after-hours", "eps": "$0.40", "epsForecast": "$0.44", "surprise": "-9.1"},
        {"symbol": "IBM", "time": "time-after-hours"}]}))
    (ed / "2026-10-29.json").write_text(json.dumps({"date": "2026-10-29", "rows": [{"symbol": "TSLA"}]}))
    (ed / "._junk.json").write_text("x")
    r = calc.earnings_history_rows(ed, {"TSLA"}, before=date(2026, 10, 29))
    assert list(r) == ["TSLA"] and len(r["TSLA"]) == 1
    assert r["TSLA"][0] == {"date": "2026-07-22", "timing": "amc", "eps": 0.4, "forecast": 0.44, "surprise_pct": -9.1}


def test_upcoming_for_prefers_nasdaq_rows_and_window():
    up = {"rows": {"NVDA": {"date": "2026-11-05", "timing": "amc", "eps_forecast": 1.2}}}
    cal = {"events": {"TSLA": {"date": "2026-10-30", "timing": "bmo"}, "AMD": {"date": "2026-12-20", "timing": "amc"},
                      "NVDA": {"date": "2026-11-06", "timing": "bmo"}}}
    r = calc.upcoming_for(up, cal, ["NVDA", "TSLA", "AMD"], NOW.date())
    assert r["NVDA"]["date"] == "2026-11-05" and r["NVDA"]["eps_forecast"] == 1.2
    assert r["TSLA"]["timing"] == "bmo" and "AMD" not in r                     # 超出 10 个交易日不列


def test_opex_third_friday():
    assert calc.opex_day(2026, 10) == date(2026, 10, 16)
    assert calc.opex_day(2026, 11) == date(2026, 11, 20)


# ── 聚合: 只读 + 区块隔离 ─────────────────────────────────────────

def _seed(data_dir: Path):
    d = data_dir / "intel"
    d.mkdir(parents=True)
    (d / "macro_calendar.json").write_text(json.dumps({"fetched_at": "2026-10-29T09:45-04:00", "events": [
        {"at": "2026-10-29T08:30-04:00", "title": "Advance GDP q/q", "impact": "high", "forecast": "3.2%", "previous": "1.5%"},
        {"at": "2026-10-29T08:30-04:00", "title": "Personal Income m/m", "impact": "low", "forecast": "0.3%"},
        {"at": "2026-10-29T10:30-04:00", "title": "CB Consumer Confidence", "impact": "medium", "forecast": "80.5"},
        {"at": "2026-10-28T14:00-04:00", "title": "FOMC Statement", "impact": "high"}]}))
    (d / "macro_actuals.json").write_text(json.dumps({"2026-10-29T08:30|Advance GDP q/q": {"actual": "3.5%", "source": "FRED A191RL1Q225SBEA"}}))
    (d / "earnings_upcoming.json").write_text(json.dumps({"fetched_at": "2026-10-29T06:00-04:00", "rows": {
        "TSLA": {"date": "2026-10-29", "timing": "amc", "eps_forecast": 0.45, "n_est": 12}}}))
    (d / "earnings_history.json").write_text(json.dumps({"symbols": {"TSLA": {"n": 8, "median_abs_move": 7.1, "reports": []}}}))
    (d / "etfs.json").write_text(json.dumps({"fetched_at": "2026-10-29T09:59:00-04:00", "rows": {
        "QQQ": {"price": 700, "chg_pct": -0.5, "live": True}, "XLK": {"price": 180, "chg_pct": 0.4, "live": True}}}))


def test_build_is_read_only_and_assembles(tmp_path):
    _seed(tmp_path)

    def digest():
        h = hashlib.sha256()
        for f in sorted(tmp_path.rglob("*")):
            if f.is_file():
                h.update(str(f).encode() + f.read_bytes())
        return h.hexdigest(), sorted(str(p) for p in tmp_path.rglob("*"))
    before = digest()
    loop = SimpleNamespace(ledger=SimpleNamespace(open_positions=[SimpleNamespace(symbol="TSLA", qty=10)]))
    out = panel.build(tmp_path, repo=None, loop=loop, now=NOW)
    assert digest() == before                                          # 不写、不建目录
    assert out["contract"] == "intel-v1"
    cal = out["calendar"]
    titles = [e["title"] for e in cal["events"]]
    assert "Personal Income m/m" not in titles                         # 默认 高+中, 低不显示
    gdp = next(e for e in cal["events"] if e["title"] == "Advance GDP q/q")
    assert gdp["actual"] == "3.5%" and gdp["surprise"] == "above" and gdp["past"] is True
    cb = next(e for e in cal["events"] if e["title"] == "CB Consumer Confidence")
    assert cb["past"] is False and cb["actual_status"] is None
    fomc = next(e for e in cal["events"] if e["title"] == "FOMC Statement")
    assert fomc["actual_status"] == "unmapped"
    earn = next(e for e in cal["events"] if e["kind"] == "earnings")
    assert earn["symbol"] == "TSLA" and earn["role"] == "持仓" and earn["time"] == "16:05"
    assert out["earnings"]["rows"][0]["symbol"] == "TSLA" and out["earnings"]["rows"][0]["hist"]["median_abs_move"] == 7.1
    assert out["earnings"]["rows"][0]["days"] == 0
    assert out["tape"]["indexes"][0]["symbol"] == "QQQ"
    assert out["errors"] == {}


def test_build_isolates_failing_section(tmp_path, monkeypatch):
    _seed(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(panel, "earnings_view", boom)
    out = panel.build(tmp_path, now=NOW)
    assert out["earnings"] is None and out["errors"] == {"earnings": "RuntimeError"}
    assert out["calendar"] is not None


def test_settings_levels(tmp_path):
    _seed(tmp_path)
    panel.save_settings(tmp_path, macro_impact="high")
    out = panel.build(tmp_path, now=NOW)
    assert {e["impact"] for e in out["calendar"]["events"] if e["kind"] == "macro"} == {"high"}
    panel.save_settings(tmp_path, macro_impact="all")
    out = panel.build(tmp_path, now=NOW)
    assert "Personal Income m/m" in [e["title"] for e in out["calendar"]["events"]]
    with pytest.raises(ValueError):
        panel.save_settings(tmp_path, macro_impact="x")


def test_news_view_watch_main_and_macro(tmp_path, monkeypatch):
    from app.services import news_store
    rows = [
        {"id": "a", "created_at": "2026-10-29T13:00:00Z", "headline": "NVDA wins", "zh_headline": "英伟达中标", "symbols": ["NVDA"],
         "mentions": {"NVDA": "headline"}, "tags": {"NVDA": {"relevance": "primary", "event_type": "contract", "direction": "bullish",
                                                             "importance": "high"}}, "tagged_by": {"mode": "llm"}, "sources": ["alpaca"]},
        {"id": "b", "created_at": "2026-10-29T12:00:00Z", "headline": "AAPL x", "symbols": ["AAPL"], "mentions": {"AAPL": "headline"},
         "tags": {"AAPL": {"relevance": "primary", "event_type": "product", "importance": "high"}}, "tagged_by": {"mode": "llm"}},
        {"id": "c", "created_at": "2026-10-29T11:00:00Z", "headline": "Fed", "symbols": ["SPY"], "mentions": {"SPY": "body"},
         "tags": {"SPY": {"relevance": "primary", "event_type": "macro", "importance": "high", "direction": "bearish"}},
         "tagged_by": {"mode": "llm"}},
        {"id": "d", "created_at": "2026-10-26T11:00:00Z", "headline": "old", "symbols": ["NVDA"], "mentions": {"NVDA": "headline"}},
    ]
    monkeypatch.setattr(news_store, "load_recent", lambda data_dir, days, now: rows)
    v = panel.news_view(tmp_path, NOW, {"NVDA": "自选"})
    assert [i["id"] for i in v["items"]] == ["a", "c"]                   # 非自选的 AAPL 不进; 过 36 小时的不进
    assert v["tone"] == {"NVDA": {"bullish": 1, "bearish": 0}}
    assert v["items"][1]["macro"] is True


def test_corp_actions_window(tmp_path):
    p = tmp_path / "reference" / "corporate_actions"
    p.mkdir(parents=True)
    (p / "actions.json").write_text(json.dumps({"actions": [
        {"symbol": "NVDA", "type": "cash_dividend", "ex_date": "2026-11-05", "rate": 0.01},
        {"symbol": "NVDA", "type": "cash_dividend", "ex_date": "2026-10-01", "rate": 0.01},
        {"symbol": "XYZ", "type": "forward_split", "ex_date": "2026-11-01"}]}))
    r = panel.corp_actions_view(tmp_path, NOW, {"NVDA": "自选"})
    assert r == [{"symbol": "NVDA", "role": "自选", "type": "cash_dividend", "date": "2026-11-05", "rate": 0.01, "special": None}]


# ── 刷新节奏 ───────────────────────────────────────────────────────

def test_tick_runs_due_sources_and_isolates_failures(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(src, "fetch_etfs", lambda p, now: calls.append("etfs") or {"rows": {"QQQ": {"price": 1}}})
    monkeypatch.setattr(src, "fetch_vol", lambda **k: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(panel, "_alpaca", lambda: object())
    r = panel.tick(tmp_path, None, None, now=NOW, only={"etfs", "vol"})
    assert r["etfs"] != "" and str(r["vol"]).startswith("error")
    st = json.loads((tmp_path / "intel" / "state.json").read_text())
    assert st["etfs"]["error"] is None and "OSError" in st["vol"]["error"]
    assert (tmp_path / "intel" / "etfs.json").exists() and not (tmp_path / "intel" / "vol.json").exists()
    calls.clear()
    assert panel.tick(tmp_path, None, None, now=NOW + timedelta(seconds=30), only={"etfs"}) == {}   # 未到期不跑
    assert calls == []
    panel.tick(tmp_path, None, None, now=NOW + timedelta(seconds=61), only={"etfs"})
    assert calls == ["etfs"]


def test_tick_off_hours_slower(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "fetch_etfs", lambda p, now: {"rows": {}})
    monkeypatch.setattr(panel, "_alpaca", lambda: object())
    sat = datetime(2026, 10, 31, 10, 0, tzinfo=ET)
    panel.tick(tmp_path, None, None, now=sat, only={"etfs"})
    assert panel.tick(tmp_path, None, None, now=sat + timedelta(minutes=10), only={"etfs"}) == {}


def test_tick_single_flight(tmp_path):
    assert panel._TICK_LOCK.acquire(blocking=False)
    try:
        assert panel.tick(tmp_path, None, None, now=NOW) is None
    finally:
        panel._TICK_LOCK.release()


# ── API ────────────────────────────────────────────────────────────

def test_api_today_and_settings(tmp_path):
    from app.api import intel
    _seed(tmp_path)
    app = FastAPI()
    app.include_router(intel.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.trading_loop = None
    c = TestClient(app)
    r = c.get("/api/intel/today")
    assert r.status_code == 200 and r.json()["contract"] == "intel-v1"
    assert c.post("/api/intel/settings", json={"macro_impact": "bogus"}).status_code == 422
    assert c.post("/api/intel/settings", json={"macro_impact": "high"}).json()["macro_impact"] == "high"


def test_not_in_trading_chain():
    """情报模块不 import 交易环、台账写口、规则保存口 (只展示)。"""
    root = Path(__file__).resolve().parents[1] / "app" / "services"
    for name in ("intel_panel.py", "intel_sources.py", "intel_calc.py"):
        text = (root / name).read_text(encoding="utf-8")
        for bad in ("trading_loop import", "ledger.core", "save_ruleset", "save_strategies", "_enqueue", "place_order"):
            assert bad not in text, (name, bad)


def test_scheduler_registers_intel_job():
    text = (Path(__file__).resolve().parents[1] / "app" / "jobs" / "daily_pipeline.py").read_text(encoding="utf-8")
    assert 'id="zen_intel"' in text


# ── 准确性复核 (10-26 盘中对拍后补) ─────────────────────────────────

def test_infer_timing_from_open_gap():
    """Nasdaq 历史库 99% 不给时段; 原来一律按盘后, GLW (盘前公布) 7-28 的反应被算成次日 -1.56%, 实际当天 -12.1%。"""
    d = date(2026, 7, 28)
    bars = {date(2026, 7, 27): (100.0, 100.0), d: (88.0, 87.9), date(2026, 7, 29): (87.5, 86.5)}
    assert calc.infer_timing(d, bars, None) == ("bmo", "gap")
    bars_amc = {date(2026, 7, 27): (100.0, 100.0), d: (100.2, 100.5), date(2026, 7, 29): (115.0, 116.0)}
    assert calc.infer_timing(d, bars_amc, "bmo") == ("amc", "gap")          # 跳空分明时以跳空为准
    flat = {date(2026, 7, 27): (100.0, 100.0), d: (100.5, 100.4), date(2026, 7, 29): (100.6, 100.0)}
    assert calc.infer_timing(d, flat, "bmo") == ("bmo", "next")               # 不分明: 沿用下次时段
    assert calc.infer_timing(d, flat, None) == ("unknown", "none")
    rows = [{"date": "2026-07-28", "timing": "unknown"}]
    closes = {k: v[1] for k, v in bars.items()}
    opens = {k: v[0] for k, v in bars.items()}
    r = calc.reaction_moves(rows, closes, opens, None)[0]
    assert r["timing"] == "bmo" and r["move_pct"] == round((87.9 / 100 - 1) * 100, 2) and r["timing_basis"] == "gap"


def test_parity_spot_matches_chain_time():
    rows = []
    for k, c, p in [(515, (18.58, 19.87), (9.13, 9.94)), (525, (13.57, 13.85), (13.70, 14.48)), (535, (9.03, 9.48), (18.75, 19.72))]:
        rows.append({"strike": k, "expiration": "2026-10-30", "type": "call", "bid": c[0], "ask": c[1]})
        rows.append({"strike": k, "expiration": "2026-10-30", "type": "put", "bid": p[0], "ask": p[1]})
    s = calc.parity_spot(rows, date(2026, 10, 28), date(2026, 10, 26))
    assert s == pytest.approx(525 + 13.71 - 14.09, abs=1e-6)
    wide = [{**r, "ask": r["bid"] * 2} for r in rows]                         # 价差太大的合约不用
    assert calc.parity_spot(wide, date(2026, 10, 28), date(2026, 10, 26)) is None


def test_eps_consensus_prefers_adjusted_sources():
    """Nasdaq 对 ARM 给 0.22 (GAAP 口径), QVeris/Finnhub 给 0.47/0.48 (调整后)。"""
    row = {"eps_forecast": 0.22, "finnhub": {"eps": 0.4795, "date": "2026-11-05"}}
    qv = {"eps_avg": 0.4734, "period": "2026-09-30"}
    assert calc.eps_consensus(row, qv, date(2026, 11, 4)) == {"eps": 0.4734, "source": "QVeris"}
    assert calc.eps_consensus(row, {"eps_avg": 1.0, "period": "2026-03-31"}, date(2026, 11, 4))["source"] == "Finnhub"  # 财季对不上
    assert calc.eps_consensus(row, None, date(2026, 11, 4))["source"] == "Finnhub"
    assert calc.eps_consensus({"eps_forecast": 0.22}, None, date(2026, 11, 4)) == {"eps": 0.22, "source": "Nasdaq"}


def test_fetch_finnhub_upcoming_per_symbol():
    calls = []

    def http(url, headers=None):
        calls.append(url)
        if "symbol=BAD" in url:
            raise OSError("x")
        return {"earningsCalendar": [{"symbol": "ARM", "date": "2026-11-05", "hour": "", "epsEstimate": 0.4795}]}
    r = calc.fetch_finnhub_upcoming(["ARM", "BAD"], NOW.date(), "tok", http_get=http, pause=0)
    assert r == {"ARM": {"date": "2026-11-05", "timing": "unknown", "eps": 0.4795}} and len(calls) == 2
    assert calc.fetch_finnhub_upcoming(["ARM"], NOW.date(), None, http_get=http) == {}


def test_options_warm_soonest_first_in_background(tmp_path, monkeypatch):
    """原实现按字母序取前 15 只 (MSFT 排第 16 被漏), 且在 tick 锁里串行跑 4 分钟。"""
    from app.services import alpaca_options
    _seed(tmp_path)
    up = {"rows": {f"S{i:02d}": {"date": "2026-11-0" + str(1 + i % 5), "timing": "amc"} for i in range(30)}}
    up["rows"]["MSFT"] = {"date": "2026-10-29", "timing": "amc"}
    (tmp_path / "intel" / "earnings_upcoming.json").write_text(json.dumps(up))
    from app.services import watchlist
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [{"symbol": s} for s in up["rows"]])
    stale = alpaca_options._dir(tmp_path, "MSFT") / "2026-10-29.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({"available": True, "rows": [], "fetched_at": "2026-10-29T04:00:53Z"}))   # 00:00 ET
    seen = []
    monkeypatch.setattr(alpaca_options, "chain", lambda d, sym, today: seen.append((sym, stale.exists() if sym == "MSFT" else None)))
    started = []
    monkeypatch.setattr(panel.threading, "Thread", lambda target, args, **k: SimpleNamespace(start=lambda: (started.append(1), target(*args))))
    panel.tick(tmp_path, None, None, now=NOW, only={"options_warm"})
    assert started == [1]
    assert seen[0] == ("MSFT", False) and len(seen) == panel.WARM_MAX       # 最近的先跑; 开盘前的缓存被清掉重拉
    assert not panel._WARM_LOCK.locked()


# ── 0.42.4 情报页 bug 清剿 (10-28 生产实证后补) ─────────────────────────

def _snap(daily_t, daily_c, prev_c, trade_t, trade_p):
    return {"dailyBar": {"c": daily_c, "t": daily_t}, "prevDailyBar": {"c": prev_c},
            "latestTrade": {"p": trade_p, "t": trade_t}}


def test_parse_snapshot_premarket_trade_is_live():
    """10-27 生产: SPY 12:04Z (08:04 ET) 已有 IEX 盘前成交, 原实现到 09:30 前一直显示 10-24 收盘「非今日」。"""
    today = date(2026, 10, 27)
    s = _snap("2026-10-26T04:00:00Z", 765.2, 760.0, "2026-10-27T12:04:00Z", 767.47)
    r = src.parse_snapshot(s, today)
    assert r["live"] is True and r["day"] == "2026-10-27" and r["price"] == 767.47
    assert r["chg_pct"] == round((767.47 / 765.2 - 1) * 100, 2)            # 盘前价对最近一日收盘, 不是对前前日
    assert r["session"] == "pre" and r["time_et"] == "08:04"
    night = _snap("2026-10-26T04:00:00Z", 765.2, 760.0, "2026-10-26T23:50:00Z", 765.0)   # 夜里: 最后成交是昨天盘后
    r2 = src.parse_snapshot(night, today)
    assert r2["live"] is False and r2["day"] == "2026-10-26" and r2["chg_pct"] == round((765.2 / 760 - 1) * 100, 2)


def test_parse_snapshot_marks_regular_and_post_session():
    today = date(2026, 10, 27)
    reg = src.parse_snapshot(_snap("2026-10-27T04:00:00Z", 769.0, 765.2, "2026-10-27T15:00:00Z", 769.5), today)
    assert reg["session"] == "regular" and reg["time_et"] == "11:00"
    post = src.parse_snapshot(_snap("2026-10-27T04:00:00Z", 769.7, 765.2, "2026-10-27T20:22:33Z", 769.9), today)
    assert post["session"] == "post" and post["live"] is True


def test_combine_quote_premarket_uses_official_close_and_keeps_session():
    today = date(2026, 10, 27)
    daily = {"close": 765.37, "prev_close": 759.9, "day": "2026-10-26", "chg_pct": 0.71, "d5": 0.4}
    snap = src.parse_snapshot(_snap("2026-10-26T04:00:00Z", 765.2, 760.0, "2026-10-27T12:04:00Z", 767.47), today)
    r = src.combine_quote(snap, daily, today)
    assert r["live"] and r["base"] == "官方前收" and r["chg_pct"] == round((767.47 / 765.37 - 1) * 100, 2)
    assert r["session"] == "pre" and r["time_et"] == "08:04"


def test_tape_view_premarket_index_is_live(tmp_path):
    d = tmp_path / "intel"
    d.mkdir(parents=True)
    now = datetime(2026, 10, 27, 8, 10, tzinfo=ET)
    snap = src.parse_snapshot(_snap("2026-10-26T04:00:00Z", 765.2, 760.0, "2026-10-27T12:04:00Z", 767.47), now.date())
    (d / "etfs.json").write_text(json.dumps({"fetched_at": "2026-10-27T08:09:55-04:00", "rows": {"SPY": snap}}))
    (d / "etf_daily.json").write_text(json.dumps({"day": "2026-10-26", "rows": {
        "SPY": {"close": 765.37, "prev_close": 759.9, "day": "2026-10-26", "chg_pct": 0.71, "d5": 0.4}}}))
    spy = panel.tape_view(tmp_path, now)["indexes"][0]
    assert spy["symbol"] == "SPY" and spy["live"] is True and spy["price"] == 767.47 and spy["session"] == "pre"


def test_vix_change_uses_official_prev_close():
    """10-27 生产: VIX 收 17.52, CBOE 官方前收 18.39 (-4.73%), 延迟报价给 -4.97%; 收盘后 prev_day_close 又滚成 17.52。"""
    def http(u):
        if "VIX3M" in u:
            return {"data": {"current_price": 19.57, "prev_day_close": 19.57, "price_change_percent": 0.0, "last_trade_time": "2026-10-27T16:15:01"}}
        if "VIX9D" in u:
            return {"data": {"current_price": 16.65}}
        return {"data": {"current_price": 17.52, "prev_day_close": 17.52, "price_change_percent": -4.97, "last_trade_time": "2026-10-27T16:15:01"}}
    csv_text = "DATE,OPEN,HIGH,LOW,CLOSE\n10/23/2026,19.18,20.66,18.87,19.45\n10/26/2026,19.66,19.84,17.79,18.39\n10/27/2026,18.43,18.55,17.43,17.52\n"
    pulls = []
    t0 = datetime(2026, 10, 27, 17, 0, tzinfo=ET)
    v = src.fetch_vol(http, closes={}, now=t0, http_text=lambda u: pulls.append(u) or csv_text)
    assert v["vix"]["prev_close"] == 18.39 and v["vix"]["chg_pct"] == -4.73 and v["vix"]["chg_basis"] == "官方前收"
    assert pulls == [src.VIX_HISTORY_URL] and v["vix_closes"]["2026-10-26"] == 18.39
    v2 = src.fetch_vol(http, closes=v["vix_closes"], history_at=v["vix_history_at"], now=t0 + timedelta(minutes=2),
                       http_text=lambda u: pulls.append(u) or csv_text)
    assert v2["vix"]["chg_pct"] == -4.73 and len(pulls) == 1                # 有缓存就不再拉 CSV
    v3 = src.fetch_vol(http, closes={}, now=t0, http_text=lambda u: (_ for _ in ()).throw(OSError("down")))
    assert v3["vix"]["chg_pct"] == -4.97 and "chg_basis" not in v3["vix"]  # CSV 挂了沿用报价, 不抛


def test_vix_overnight_gth_compares_with_quote_day_close():
    """10-28 04:10 ET 生产干跑: 夜盘 VIX 17.58, last_trade_time 仍是 10-27 16:15; 应对 10-27 收盘 17.52 (+0.34%),
    不能对 10-26 收盘 18.39 (-4.40%)。"""
    def http(u):
        return {"data": {"current_price": 17.58 if u.endswith("/_VIX.json") else 19.57,
                         "price_change_percent": 0.34, "last_trade_time": "2026-10-27T16:15:01"}}
    closes = {"2026-10-26": 18.39, "2026-10-27": 17.52}
    v = src.fetch_vol(http, closes=closes, now=datetime(2026, 10, 28, 4, 10, tzinfo=ET), http_text=lambda u: 1 / 0)
    assert v["vix"]["value"] == 17.58 and v["vix"]["prev_close"] == 17.52 and v["vix"]["chg_pct"] == 0.34
    assert src.vix_base(17.52, date(2026, 10, 27), closes) == 18.39            # 收盘后到夜盘前: 现值 = 收盘, 对前一日


def test_vol_view_hides_close_cache(tmp_path):
    d = tmp_path / "intel"
    d.mkdir(parents=True)
    (d / "vol.json").write_text(json.dumps({"vix": {"value": 17.5}, "vix_closes": {"2026-10-26": 18.39}, "fetched_at": "2026-10-29T09:58:00-04:00"}))
    v = panel.vol_view(tmp_path, NOW)
    assert "vix_closes" not in v and v["age_min"] == 2.0


def test_earnings_days_counts_from_next_session_on_holiday():
    """周六看下周一的财报: 原实现显示「今天」(days=0)。"""
    sat, mon = date(2026, 10, 31), date(2026, 11, 2)
    assert panel.trading_days_ahead(sat, mon) == 1
    assert panel.trading_days_ahead(date(2026, 10, 30), mon) == 1
    assert panel.trading_days_ahead(date(2026, 11, 2), mon) == 0
    assert panel.trading_days_ahead(date(2026, 10, 28), date(2026, 11, 4)) == 5


def test_news_view_keeps_hold_items_beyond_overall_limit(tmp_path, monkeypatch):
    """10-28 生产: 持仓新闻 38 条全排在前 40 条之后, 页面「持仓」筛选为空。"""
    from app.services import news_store
    rows = []
    for i in range(60):           # 60 条自选新闻比持仓新闻更新
        rows.append({"id": f"w{i}", "created_at": f"2026-10-29T13:{i:02d}:00Z", "headline": f"w{i}", "symbols": ["NVDA"],
                     "mentions": {"NVDA": "headline"}, "tags": {"NVDA": {"relevance": "primary", "event_type": "product",
                                                                           "direction": "bullish", "importance": "medium"}},
                     "tagged_by": {"mode": "llm"}})
    rows.append({"id": "h1", "created_at": "2026-10-29T10:00:00Z", "headline": "tsla", "symbols": ["TSLA"],
                 "mentions": {"TSLA": "headline"}, "tags": {"TSLA": {"relevance": "primary", "event_type": "contract",
                                                                       "direction": "neutral", "importance": "medium"}},
                 "tagged_by": {"mode": "llm"}})
    rows.append({"id": "both", "created_at": "2026-10-29T09:00:00Z", "headline": "x", "symbols": ["NVDA", "TSLA"],
                 "mentions": {"NVDA": "headline", "TSLA": "headline"},
                 "tags": {"NVDA": {"relevance": "primary", "event_type": "product", "importance": "medium"},
                          "TSLA": {"relevance": "primary", "event_type": "product", "importance": "medium"}},
                 "tagged_by": {"mode": "llm"}})
    monkeypatch.setattr(news_store, "load_recent", lambda data_dir, days, now: rows)
    v = panel.news_view(tmp_path, NOW, {"NVDA": "自选", "TSLA": "持仓"})
    hold = [i["id"] for i in v["items"] if i["role"] == "持仓"]
    assert hold == ["h1", "both"]                                          # 同时提到自选和持仓的, 记在持仓名下
    assert v["counts"]["hold"] == 2 and v["counts"]["all"] == 62 and v["limit"] == panel.NEWS_LIMIT
    assert len([i for i in v["items"] if i["role"] == "自选"]) == panel.NEWS_LIMIT


def test_manual_refresh_skips_slow_earnings_sources(tmp_path, monkeypatch):
    """10-28 生产: 立即刷新 162 秒, 其中 earnings_upcoming 145 秒 (占住 tick 锁, ETF 1 分钟刷新停摆)。"""
    from app.api import intel
    seen = {}
    monkeypatch.setattr(panel, "tick", lambda *a, **k: seen.update(k) or {})
    app = FastAPI()
    app.include_router(intel.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.trading_loop = None
    assert TestClient(app).post("/api/intel/refresh").status_code == 200
    assert seen["force"] is True and seen["only"] == set(panel.MANUAL_SOURCES)
    assert "etfs" in seen["only"] and not {"earnings_upcoming", "earnings_history", "options_warm"} & seen["only"]
