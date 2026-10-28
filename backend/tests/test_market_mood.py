"""盘前大盘情绪: 兜底规则、锁定、记分、是否今日/最新 (2026-10-12)。"""
import json
from datetime import date, datetime, timedelta

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
    for jid in ("zen_market_mood", "zen_market_mood_score", "zen_market_mood_score_retry"):
        assert sched.get_job(jid) is not None
