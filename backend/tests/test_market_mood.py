"""盘前大盘情绪: 兜底规则、锁定、记分、是否今日/最新 (2026-10-12)。整点更新与分时点记分见文件后半。"""
import hashlib
import json
from datetime import date, datetime, time, timedelta

import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import market_mood as mm

ET = us_clock.tz


@pytest.fixture(autouse=True)
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def _items(**over):
    base = {
        "trend": {"QQQ": {"close": 751.27, "chg_pct": 0.4, "vs_ma20_pct": 1.86, "as_of": "2026-10-09"}, "as_of": "2026-10-09"},
        "overnight": {"ndx": {"last": 31016, "chg_pct": 0.2, "chg_30m_pct": 0.05, "as_of": "2026-10-12T08:34:00-04:00"}},
        "calendar": {"events": [], "as_of": "2026-10-12T08:35"},
        "rates": {"us10y": 5.24, "us10y_chg_bp": -2, "as_of": "2026-10-09"},
        "sectors": {"breadth": {"up_pct": 55.0, "n": 8000, "as_of": "2026-10-09"}, "as_of": "2026-10-09"},
        "panic": {"vix": 14.84, "vix3m": 17.77, "ratio": 0.835, "as_of": "2026-10-09"},
    }
    base.update(over)
    fetchers = {}
    for k, v in base.items():
        if isinstance(v, Exception):
            def f(e=v):
                raise e
        else:
            def f(v=v):
                return v
        fetchers[k] = f
    return fetchers


class Repo:
    def __init__(self, closes: dict[str, float]):
        self.closes = closes

    def get_daily(self, symbol, start, end, columns=None):
        rows = [(d, c) for d, c in sorted(self.closes.items()) if start.isoformat() <= d <= end.isoformat()]
        return pl.DataFrame({"date": [r[0] for r in rows], "close": [r[1] for r in rows]})


NOW = datetime(2026, 10, 12, 8, 35, tzinfo=ET)


def _llm(payload):
    return lambda messages: json.dumps(payload)


# ── 兜底规则 ──
def test_round5_sums_to_100_in_steps_of_5():
    for raw in ({"up": 33, "down": 33, "range": 34}, {"up": 1, "down": 1, "range": 1}, {"up": 72, "down": 9, "range": 19},
                {"up": "x", "down": 50, "range": 50}, {}):
        out = mm._round5(raw)
        assert sum(out.values()) == 100 and all(v % 5 == 0 and v >= 0 for v in out.values()), (raw, out)
    assert mm._round5({}) == mm.BASE_RATES


def test_tie_forces_range_and_caps_stars():
    items = mm.collect(None, None, ".", NOW, _items())
    g = mm.guard({"probs": {"up": 40, "down": 40, "range": 20}, "stars": 5}, items)
    assert g["main"] == "range" and g["stance"] == "range" and g["stars"] == 2
    assert "tie_to_range" in g["guards"]


def test_panic_only_downgrades_long_and_missing_never_triggers(tmp_path):
    hot = _items(panic={"vix": 22.0, "vix3m": 20.0, "ratio": 1.1, "as_of": "2026-10-09"})
    g = mm.guard({"probs": {"up": 60, "down": 15, "range": 25}, "stars": 4}, mm.collect(None, None, tmp_path, NOW, hot))
    assert g["panic"] == "tighten_long" and g["main"] == "up" and g["stance"] == "range"
    g = mm.guard({"probs": {"up": 15, "down": 60, "range": 25}}, mm.collect(None, None, tmp_path, NOW, hot))
    assert g["stance"] == "down"                          # 偏空不受影响
    gone = _items(panic=RuntimeError("cboe down"))
    g = mm.guard({"probs": {"up": 60, "down": 15, "range": 25}}, mm.collect(None, None, tmp_path, NOW, gone))
    assert g["panic"] == "unknown" and g["stance"] == "up" and g["leans"]["panic"]["lean"] is None


def test_stale_and_missing_flags(tmp_path):
    f = _items(rates={"us10y": 5.2, "as_of": "2026-10-01"},
               overnight={"ndx": {"last": 1, "chg_pct": 0, "as_of": "2026-10-12T06:00:00-04:00"}},
               calendar=ValueError("feed"))
    st = {i["key"]: i["status"] for i in mm.collect(None, None, tmp_path, NOW, f)}
    assert st["rates"] == "stale" and st["overnight"] == "stale" and st["calendar"] == "missing" and st["trend"] == "ok"


# ── 生成与锁定 ──
def test_generate_locks_and_never_rewrites(tmp_path):
    a = mm.generate(tmp_path, None, None, now=NOW, fetchers=_items(),
                    llm=_llm({"probs": {"up": 20, "down": 40, "range": 40}, "stars": 3}))
    assert a["day"] == "2026-10-12" and a["mode"] == "llm" and sum(a["probs"].values()) == 100
    b = mm.generate(tmp_path, None, None, now=NOW + timedelta(hours=2), fetchers=_items(),
                    llm=_llm({"probs": {"up": 90, "down": 5, "range": 5}, "stars": 5}))
    assert b == a


def test_generate_falls_back_when_llm_fails(tmp_path):
    def boom(_):
        raise TimeoutError
    j = mm.generate(tmp_path, None, None, now=NOW, fetchers=_items(), llm=boom)
    assert j["mode"] == "fallback" and j["probs"] == mm.BASE_RATES and j["error"] == "TimeoutError"


def test_generate_skips_non_trading_day(tmp_path):
    assert mm.generate(tmp_path, None, None, now=datetime(2026, 10, 11, 8, 35, tzinfo=ET), fetchers=_items(),
                       llm=_llm({})) is None


# ── 记分 ──
def test_score_uses_band_and_is_idempotent(tmp_path):
    mm.generate(tmp_path, None, None, now=NOW, fetchers=_items(),
                llm=_llm({"probs": {"up": 20, "down": 50, "range": 30}}))
    repo = Repo({"2026-10-09": 751.27})
    assert mm.score(tmp_path, repo, day=date(2026, 10, 12)) is None          # 当日日K未到
    repo.closes["2026-10-12"] = 751.27 * 0.995                                 # -0.5%
    row = mm.score(tmp_path, repo, day=date(2026, 10, 12))
    assert row["outcome"] == "down" and row["hit"] is True and row["p_outcome"] == 50
    assert mm.score(tmp_path, repo, day=date(2026, 10, 12)) == row
    assert len((tmp_path / "market_mood" / "scores.jsonl").read_text().splitlines()) == 1
    assert [mm.outcome(x) for x in (0.31, 0.3, -0.3, -0.31)] == ["up", "range", "range", "down"]


# ── 是否今日 / 是否最新 ──
def test_view_today_and_latest_flags(tmp_path):
    mm.generate(tmp_path, None, None, now=NOW, fetchers=_items(), llm=_llm({"probs": {"up": 50, "down": 25, "range": 25}}))
    v = mm.view(tmp_path, now=NOW + timedelta(hours=1))
    assert v["is_today"] and v["is_latest"]
    v = mm.view(tmp_path, now=datetime(2026, 10, 13, 8, 0, tzinfo=ET))       # 次日 08:35 前: 仍是最新, 但不是今天
    assert not v["is_today"] and v["is_latest"] and v["next_at"].startswith("2026-10-13T08:35")
    v = mm.view(tmp_path, now=datetime(2026, 10, 13, 9, 0, tzinfo=ET))       # 次日 08:35 后没生成: 不是最新
    assert not v["is_today"] and not v["is_latest"] and v["expected_day"] == "2026-10-13"
    assert mm.view(tmp_path / "empty", now=NOW)["judgment"] is None


def test_message_has_no_explanatory_copy(tmp_path):
    j = mm.generate(tmp_path, None, None, now=NOW, fetchers=_items(), llm=_llm({"probs": {"up": 40, "down": 40, "range": 20}}))
    body = mm.message(j)["body"]
    assert body.startswith("**震荡 20 · 跌 40 · 涨 40  →  观望")
    assert "命中" not in body and "平局" not in body


def test_market_mood_jobs_registered(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    for jid in ("zen_market_mood", "zen_market_mood_score", "zen_market_mood_score_retry",
                "zen_market_mood_preopen", "zen_market_mood_intraday"):
        assert sched.get_job(jid) is not None
    pre, intra = (str(sched.get_job(j).trigger) for j in ("zen_market_mood_preopen", "zen_market_mood_intraday"))
    assert "hour='9', minute='25'" in pre and "day_of_week='mon-fri'" in pre
    assert "hour='10-15', minute='0'" in intra and "day_of_week='mon-fri'" in intra
    from app.services import ops_watch as ow
    for jid in ("zen_market_mood_preopen", "zen_market_mood_intraday"):
        assert jid in ow.WATCHED and jid not in ow.CATCHUP      # a late hourly update is pointless: no catch-up
    # the job wrapper is soft: an update failure never raises into the scheduler
    monkeypatch.setattr(us_clock, "today", lambda: date(2026, 10, 12))

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(mm, "update", boom)
    assert sched.get_job("zen_market_mood_intraday").func() is None


# ── 整点更新 (09:25 与 10:00-15:00 每个整点; 08:35 原判不改写) ──
LIVE = {"price": 760.1, "chg_pct": 1.18, "session": "regular", "time_et": "10:00", "as_of": "2026-10-12T10:00:00-04:00"}


def _counting_llm(payload, calls):
    def f(messages):
        calls.append(messages)
        return json.dumps(payload)
    return f


class SettleRepo(Repo):
    settled = None

    def latest_settled_daily_date(self):
        return self.settled


def _kline_part(data_dir, day, closes, snap=(), with_ts=True):
    p = data_dir / "kline_daily" / f"date={day}" / "part.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    syms = list(closes)
    cols = {"symbol": syms, "close": [float(closes[s]) for s in syms]}
    if with_ts:
        cols["quote_ts"] = pl.Series([1_760_000_000_000 if s in snap else None for s in syms], dtype=pl.Int64)
    pl.DataFrame(cols).write_parquet(p)


def test_sector_breadth_reads_previous_official_close_only(tmp_path):
    # production 11-19: the session day's partition is written during the day (all snapshot rows)
    # and the previous day still holds ~160 unsettled snapshot rows. Breadth must ignore both.
    _kline_part(tmp_path, "2026-10-08", {"A": 10, "B": 10, "C": 10, "D": 10}, with_ts=False)     # old partition: no quote_ts column
    _kline_part(tmp_path, "2026-10-09", {"A": 11, "B": 11, "C": 9, "D": 12}, snap={"D"})
    _kline_part(tmp_path, "2026-10-12", {"A": 1, "B": 1, "C": 1, "D": 1}, snap={"A", "B", "C", "D"})
    out = mm.fetch_sectors(None, tmp_path, datetime(2026, 10, 12, 10, 0, tzinfo=ET))
    assert out["breadth"] == {"up_pct": 66.7, "n": 3, "as_of": "2026-10-09"}
    assert out["as_of"] == "2026-10-09"


def test_update_slots_and_due_window(monkeypatch):
    d = date(2026, 10, 12)

    def at(h, m, s=0):
        return datetime(2026, 10, 12, h, m, s, tzinfo=ET)
    assert [s.strftime("%H:%M") for s in mm.update_slots(d)] == ["09:25", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00"]
    assert mm.due_slot(at(9, 25)) == time(9, 25) and mm.due_slot(at(9, 39, 59)) == time(9, 25)
    assert mm.due_slot(at(9, 24, 59)) is None and mm.due_slot(at(9, 40)) is None
    assert mm.due_slot(at(15, 14)) == time(15, 0) and mm.due_slot(at(16, 0)) is None
    assert mm.update_slots(date(2026, 10, 11)) == [] and mm.due_slot(datetime(2026, 10, 11, 10, 0, tzinfo=ET)) is None
    monkeypatch.setattr(us_clock, "session_close", lambda day: time(13, 0))                 # half day
    assert [s.strftime("%H:%M") for s in mm.update_slots(d)][-1] == "12:00"
    assert mm.due_slot(at(12, 5)) == time(12, 0) and mm.due_slot(at(13, 0)) is None


def test_update_appends_one_snapshot_per_slot_and_never_rewrites(tmp_path):
    mm.generate(tmp_path, None, None, now=NOW, fetchers=_items(), llm=_llm({"probs": {"up": 20, "down": 50, "range": 30}}))
    jf = tmp_path / "market_mood" / "judgments" / "2026-10-12.json"
    before = hashlib.sha256(jf.read_bytes()).hexdigest()
    calls: list = []
    at10 = datetime(2026, 10, 12, 10, 0, 3, tzinfo=ET)
    u = mm.update(tmp_path, None, None, now=at10, fetchers=_items(live=LIVE),
                  llm=_counting_llm({"probs": {"up": 55, "down": 20, "range": 25}, "stars": 4,
                                     "items": {"live": {"lean": "up", "note": "QQQ +1.18%"}}}, calls))
    assert u["slot"] == "10:00" and u["probs"] == {"up": 55, "down": 20, "range": 25} and u["stance"] == "up"
    assert u["prompt_version"] == mm.UPDATE_PROMPT_VERSION and u["generated_at"] == "2026-10-12T10:00:03-04:00"
    live = next(i for i in u["items"] if i["key"] == "live")
    assert live["status"] == "ok" and live["lean"] == "up" and live["label"] == "QQQ 现价"
    again = mm.update(tmp_path, None, None, now=at10 + timedelta(minutes=5), fetchers=_items(live=LIVE),
                      llm=_counting_llm({"probs": {"up": 5, "down": 90, "range": 5}}, calls))
    assert again == u and len(calls) == 1                                    # same slot: no model call, no rewrite
    mm.update(tmp_path, None, None, now=datetime(2026, 10, 12, 11, 0, 1, tzinfo=ET), fetchers=_items(live=LIVE),
              llm=_counting_llm({"probs": {"up": 30, "down": 40, "range": 30}}, calls))
    lines = (tmp_path / "market_mood" / "updates" / "2026-10-12.jsonl").read_text().splitlines()
    assert [json.loads(x)["slot"] for x in lines] == ["10:00", "11:00"]
    assert [x["slot"] for x in mm.load_updates(tmp_path, date(2026, 10, 12))] == ["10:00", "11:00"]
    assert hashlib.sha256(jf.read_bytes()).hexdigest() == before           # 08:35 original untouched
    system, user = calls[0][0]["content"], calls[0][1]["content"]
    assert "QQQ 现价" in user and "10:00 ET" in user and "距收盘 360 分钟" in user
    assert "参考起点" not in system and "原判" not in system + user          # no base-rate anchor, original not fed back


def test_update_writes_nothing_without_original_outside_window_or_on_model_failure(tmp_path):
    at10 = datetime(2026, 10, 12, 10, 0, tzinfo=ET)
    ok = _llm({"probs": {"up": 50, "down": 25, "range": 25}})
    assert mm.update(tmp_path, None, None, now=at10, fetchers=_items(live=LIVE), llm=ok) is None    # no 08:35 original
    mm.generate(tmp_path, None, None, now=NOW, fetchers=_items(), llm=_llm({"probs": {"up": 20, "down": 50, "range": 30}}))
    for now in (datetime(2026, 10, 12, 10, 20, tzinfo=ET), datetime(2026, 10, 11, 10, 0, tzinfo=ET)):
        assert mm.update(tmp_path, None, None, now=now, fetchers=_items(live=LIVE), llm=ok) is None

    def boom(_):
        raise TimeoutError
    for llm in (boom, _llm({"summary": "x"}), _llm({"probs": {"up": 0, "down": 0, "range": 0}}), lambda _: "not json"):
        assert mm.update(tmp_path, None, None, now=at10, fetchers=_items(live=LIVE), llm=llm) is None
    assert not (tmp_path / "market_mood" / "updates").exists()
    assert mm.load_updates(tmp_path, date(2026, 10, 12)) == []


def _intel_cache(data_dir, *, fetched_at, qqq, daily_day="2026-10-09", prev_close=751.27):
    d = data_dir / "intel"
    d.mkdir(parents=True, exist_ok=True)
    (d / "etfs.json").write_text(json.dumps({"rows": {"QQQ": qqq}, "source": "x", "fetched_at": fetched_at}))
    (d / "etf_daily.json").write_text(json.dumps({"day": daily_day, "rows": {"QQQ": {
        "close": prev_close, "prev_close": 748.3, "day": daily_day, "chg_pct": 0.4, "d5": 1.2}}}))


def test_live_item_reads_intel_tape_cache_and_flags_stale_or_missing(tmp_path):
    at = datetime(2026, 10, 12, 10, 0, 5, tzinfo=ET)
    row = {"price": 760.29, "chg_pct": 1.1, "as_of": "2026-10-12T14:00:01.123456789Z", "day": "2026-10-12",
           "live": True, "session": "regular", "time_et": "10:00"}
    _intel_cache(tmp_path, fetched_at="2026-10-12T10:00:02-04:00", qqq=row)
    v = mm.fetch_live(tmp_path, at)
    assert v["price"] == 760.29 and v["chg_pct"] == round((760.29 / 751.27 - 1) * 100, 2)   # against the official prior close
    assert v["as_of"].startswith("2026-10-12T10:00:01") and v["time_et"] == "10:00"
    items = {i["key"]: i for i in mm.collect(None, None, tmp_path, at, _items(), live=True)}
    assert items["live"]["status"] == "ok" and "760.29" in items["live"]["text"] and items["live"]["label"] == "QQQ 现价"
    items = {i["key"]: i for i in mm.collect(None, None, tmp_path, at + timedelta(minutes=11), _items(), live=True)}
    assert items["live"]["status"] == "stale"                                # last trade > 10 minutes old
    _intel_cache(tmp_path, fetched_at="2026-10-09T15:59:00-04:00",
                 qqq={**row, "day": "2026-10-09", "as_of": "2026-10-09T19:59:00Z"})
    items = {i["key"]: i for i in mm.collect(None, None, tmp_path, at, _items(), live=True)}
    assert items["live"]["status"] == "missing" and items["live"]["raw"] is None   # yesterday's tape is not a live price
    assert [i["key"] for i in mm.collect(None, None, tmp_path, at, _items())] == [k for k, _ in mm.ITEMS]


def test_guard_extends_to_live_item_without_changing_the_six_item_result(tmp_path):
    raw = {"probs": {"up": 50, "down": 25, "range": 25}, "stars": 4,
           "items": {"trend": {"lean": "up", "note": "a"}, "live": {"lean": "down", "note": "b"}}}
    g6 = mm.guard(raw, mm.collect(None, None, tmp_path, NOW, _items()))
    assert list(g6["leans"]) == [k for k, _ in mm.ITEMS]
    g7 = mm.guard(raw, mm.collect(None, None, tmp_path, NOW, _items(live=LIVE), live=True))
    assert {k: v for k, v in g7["leans"].items() if k != "live"} == g6["leans"]
    assert g7["leans"]["live"] == {"lean": "down", "note": "b"}
    keys = ("probs", "main", "stance", "stars", "panic", "guards")
    assert {k: g7[k] for k in keys} == {k: g6[k] for k in keys}


# ── 分时点记分 ──
def test_tie_never_counts_as_hit(tmp_path):
    mm.generate(tmp_path, None, None, now=NOW, fetchers=_items(), llm=_llm({"probs": {"up": 40, "down": 40, "range": 20}}))
    row = mm.score(tmp_path, Repo({"2026-10-09": 100.0, "2026-10-12": 100.1}), day=date(2026, 10, 12))     # +0.1% = range
    assert row["main"] == "range" and row["outcome"] == "range" and row["hit"] is False
    assert row["probs"] == {"up": 40, "down": 40, "range": 20} and row["brier"] == mm.brier(row["probs"], "range") == 0.96
    assert mm.brier(mm.BASE_RATES, "up") == 0.545 and mm.brier({"up": 100, "down": 0, "range": 0}, "up") == 0
    assert mm.brier({"up": "x"}, "up") is None


def test_score_scores_each_update_slot_and_follows_final_rules(tmp_path):
    day = date(2026, 10, 12)
    mm.generate(tmp_path, None, None, now=NOW, fetchers=_items(), llm=_llm({"probs": {"up": 20, "down": 50, "range": 30}}))
    for h, probs in ((10, {"up": 60, "down": 15, "range": 25}), (11, {"up": 35, "down": 35, "range": 30})):
        mm.update(tmp_path, None, None, now=datetime(2026, 10, 12, h, 0, tzinfo=ET), fetchers=_items(live=LIVE),
                  llm=_llm({"probs": probs}))
    repo = SettleRepo({"2026-10-09": 100.0, "2026-10-12": 100.5})                # 16:10 snapshot close: +0.5% = up
    row = mm.score(tmp_path, repo, day=day)
    assert row["outcome"] == "up" and row["hit"] is False and row["final"] is False and "slot" not in row
    us = mm.load_update_scores(tmp_path)
    assert {k: (r["hit"], r["final"], r["brier"]) for k, r in us.items()} == {
        "2026-10-12 10:00": (True, False, mm.brier({"up": 60, "down": 15, "range": 25}, "up")),
        "2026-10-12 11:00": (False, False, mm.brier({"up": 35, "down": 35, "range": 30}, "up"))}
    uf = tmp_path / "market_mood" / "update_scores.jsonl"
    assert mm.score(tmp_path, repo, day=day) == row and len(uf.read_text().splitlines()) == 2   # same close: no writes
    repo.closes["2026-10-12"], repo.settled = 99.9, day                        # 18:00 official close: -0.1% = range
    b = mm.score(tmp_path, repo, day=day)
    assert b["outcome"] == "range" and b["final"] is True and b["rescored_from"] == 100.5
    us = mm.load_update_scores(tmp_path)
    assert all(r["final"] and r["outcome"] == "range" and r["rescored_from"] == 100.5 for r in us.values())
    assert us["2026-10-12 11:00"]["hit"] is False                              # tie: not a hit even though the day ranged
    assert mm.score(tmp_path, repo, day=day) == b and len(uf.read_text().splitlines()) == 4    # final: frozen
    assert len((tmp_path / "market_mood" / "scores.jsonl").read_text().splitlines()) == 2


def test_score_still_scores_updates_when_original_row_is_already_final(tmp_path):
    day = date(2026, 10, 12)
    mm.generate(tmp_path, None, None, now=NOW, fetchers=_items(), llm=_llm({"probs": {"up": 20, "down": 50, "range": 30}}))
    mm.update(tmp_path, None, None, now=datetime(2026, 10, 12, 10, 0, tzinfo=ET), fetchers=_items(live=LIVE),
              llm=_llm({"probs": {"up": 60, "down": 15, "range": 25}}))
    old = {"day": "2026-10-12", "close": 101.0, "chg_pct": 1.0, "outcome": "up", "main": "down", "stance": "down",
           "hit": False, "p_outcome": 20, "scored_at": "2026-10-12T16:46:00-04:00", "final": True}
    (tmp_path / "market_mood" / "scores.jsonl").write_text(json.dumps(old) + "\n")
    repo = SettleRepo({"2026-10-09": 100.0, "2026-10-12": 101.0})
    repo.settled = day
    assert mm.score(tmp_path, repo, day=day) == old                               # existing rows are not rewritten
    assert len((tmp_path / "market_mood" / "scores.jsonl").read_text().splitlines()) == 1
    r = mm.load_update_scores(tmp_path)["2026-10-12 10:00"]
    assert r["final"] is True and r["hit"] is True and r["outcome"] == "up"


def test_weekly_stats_brier_against_fixed_distribution_and_per_slot(tmp_path):
    jd = tmp_path / "market_mood" / "judgments"
    jd.mkdir(parents=True)
    (jd / "2026-10-09.json").write_text(json.dumps({"day": "2026-10-09", "main": "down", "probs": {"up": 20, "down": 50, "range": 30}}))
    rows = [{"day": "2026-10-09", "outcome": "down", "main": "down", "hit": True},       # older row: no probs -> judgment
            {"day": "2026-10-12", "outcome": "up", "main": "down", "hit": False,
             "probs": {"up": 25, "down": 50, "range": 25}, "brier": 0.875}]
    (tmp_path / "market_mood" / "scores.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "market_mood" / "update_scores.jsonl").write_text(json.dumps(
        {"day": "2026-10-12", "slot": "10:00", "outcome": "up", "main": "up", "hit": True,
         "probs": {"up": 60, "down": 15, "range": 25}, "brier": 0.245, "final": True}) + "\n")
    st = mm.weekly_stats(tmp_path)
    b1, b2 = mm.brier({"up": 20, "down": 50, "range": 30}, "down"), mm.brier({"up": 25, "down": 50, "range": 25}, "up")
    assert st["n"] == 2 and st["hit"] == 1 and st["brier"] == round((b1 + b2) / 2, 3)
    assert st["brier_base"] == round((mm.brier(mm.BASE_RATES, "down") + mm.brier(mm.BASE_RATES, "up")) / 2, 3)
    assert st["slots"] == [{"slot": "10:00", "n": 1, "hit": 1, "brier": 0.245, "brier_base": mm.brier(mm.BASE_RATES, "up")}]
    assert mm.weekly_stats(tmp_path / "empty")["slots"] == [] and mm.weekly_stats(tmp_path / "empty")["brier"] is None


def test_view_latest_snapshot_next_update_and_missing_mark(tmp_path):
    mm.generate(tmp_path, None, None, now=NOW, fetchers=_items(), llm=_llm({"probs": {"up": 20, "down": 50, "range": 30}}))
    for h, m in ((9, 25), (10, 0)):
        mm.update(tmp_path, None, None, now=datetime(2026, 10, 12, h, m, tzinfo=ET), fetchers=_items(live=LIVE),
                  llm=_llm({"probs": {"up": 55, "down": 20, "range": 25}}))
    v = mm.view(tmp_path, now=datetime(2026, 10, 12, 10, 5, tzinfo=ET))
    assert v["judgment"]["day"] == "2026-10-12" and v["judgment"]["stance"] == "down"            # original unchanged
    assert v["latest"]["slot"] == "10:00" and v["latest"]["stance"] == "up"
    assert any(i["key"] == "live" for i in v["latest"]["items"])
    assert [u["slot"] for u in v["updates"]] == ["09:25", "10:00"] and "items" not in v["updates"][0]
    assert v["next_update_at"].startswith("2026-10-12T11:00") and v["update_missing"] is None
    assert mm.view(tmp_path, now=datetime(2026, 10, 12, 11, 29, tzinfo=ET))["update_missing"] is None
    assert mm.view(tmp_path, now=datetime(2026, 10, 12, 11, 30, tzinfo=ET))["update_missing"] == "11:00"
    late = mm.view(tmp_path, now=datetime(2026, 10, 12, 17, 0, tzinfo=ET))
    assert late["update_missing"] == "15:00" and late["next_update_at"].startswith("2026-10-13T09:25")
    nxt = mm.view(tmp_path, now=datetime(2026, 10, 13, 8, 0, tzinfo=ET))       # next day before 08:35
    assert not nxt["is_today"] and nxt["latest"]["slot"] == "10:00" and nxt["update_missing"] is None
    assert mm.view(tmp_path / "empty", now=NOW)["latest"] is None
