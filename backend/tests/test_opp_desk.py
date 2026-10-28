"""0.41.0 一页作战台 (opp_desk): 大盘状态 / 跨引擎共振 / 共振影子账本。

不变量:
- 大盘状态与 10-22 回测同口径: SPY >= +1% 或自选 >= 50% 涨 >= 1% = 普涨, 对称普跌, 其余平常; 无数据 = None。
- 共振 = 同票 >= 2 个引擎同向; 反向冲突不算; 已结束的盘中卡不计; 跳空 / 开盘榜只认今天。
- 影子账本同票同日只记一次; 回填按方向取正负; 不进交易链 (交易环 / 准入 / 下单簿 / 推送不引用 opp_desk)。
"""
import json
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import opp_desk as od

ET = us_clock.tz
NOW = datetime(2026, 10, 27, 13, 0, tzinfo=ET)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


@pytest.mark.parametrize(("spy", "ups", "downs", "n", "key"), [
    (0.54, 34, 2, 44, "up"),           # 10-27: SPY 不到 1%, 但自选 77% 涨 >= 1%
    (1.2, 5, 5, 44, "up"),
    (-1.1, 3, 10, 44, "down"),
    (0.2, 10, 8, 44, "normal"),
    (None, 0, 0, 0, None),
    (None, 30, 0, 44, "up"),
])
def test_classify(spy, ups, downs, n, key):
    assert od.classify(spy, ups, downs, n) == key


def _sw(*cards):
    return {"long": [c for c in cards if c["side"] == "long"], "short": [c for c in cards if c["side"] == "short"]}


def test_resonance_needs_two_engines_same_side():
    gaps = {"at": "2026-10-27T09:00:00-04:00", "rows": [{"symbol": "BE", "gap_pct": 3.7}, {"symbol": "AVGO", "gap_pct": 3.5},
                                                       {"symbol": "XX", "gap_pct": -4.0}]}
    radar = {"is_today": True, "picks": [{"symbol": "BE", "dir": 1, "rank": 2, "rvol": 1.4}, {"symbol": "TSLA", "dir": 1, "rank": 3, "rvol": 1.2},
                                         {"symbol": "AVGO", "dir": 1, "rank": 1, "rvol": 1.5}]}
    intraday = {"active": [{"symbol": "TSLA", "side": "long", "status": "open", "day": "2026-10-27", "r_now": 0.5},
                           {"symbol": "TSLA", "side": "long", "status": "open", "day": "2026-10-26", "r_now": 1.4}]}
    swing = _sw({"symbol": "TSLA", "side": "long", "state": "pending"}, {"symbol": "XX", "side": "long", "state": "pending"},
                {"symbol": "ARM", "side": "short", "state": "holding"})
    res = od.resonance(now=NOW, swing=swing, intraday=intraday, radar=radar, gaps=gaps)
    got = {r["symbol"]: (r["side"], r["n"]) for r in res}
    assert got == {"TSLA": ("long", 3), "BE": ("long", 2), "AVGO": ("long", 2)}           # XX 跳空空 vs 挂单多 = 冲突不算; 持仓不算引擎
    tsla = next(r for r in res if r["symbol"] == "TSLA")
    assert [c["day"] for c in tsla["engines"]["intraday"]["cards"]] == ["2026-10-27", "2026-10-26"]
    assert list(tsla["engines"]) == ["open", "intraday", "board"]


def test_resonance_ignores_stale_engines():
    gaps = {"at": "2026-10-26T09:00:00-04:00", "rows": [{"symbol": "BE", "gap_pct": 3.7}]}
    radar = {"is_today": False, "picks": [{"symbol": "BE", "dir": 1}]}
    assert od.resonance(now=NOW, swing=_sw(), intraday={"active": []}, radar=radar, gaps=gaps) == []


def test_shadow_records_once_and_settles_by_side(tmp_path):
    res = [{"symbol": "BE", "side": "long", "n": 3, "engines": {"gap": {}, "open": {}, "intraday": {}}},
           {"symbol": "ZZ", "side": "short", "n": 2, "engines": {"gap": {}, "open": {}}}]
    assert od.record(tmp_path, NOW, res, {"BE": 220.0, "ZZ": 100.0}) == 2
    assert od.record(tmp_path, NOW.replace(minute=5), res, {"BE": 230.0, "ZZ": 90.0}) == 0
    rows = od.today_rows(tmp_path, NOW.date())
    assert rows["BE"]["entry"] == 220.0 and rows["BE"]["at"] == "2026-10-27T13:00:00-04:00"

    class Repo:
        def get_daily_batch(self, syms, start, end, columns=None):
            out = []
            for i, d in enumerate(("2026-10-27", "2026-10-28", "2026-10-29")):
                out += [{"symbol": "BE", "date": date.fromisoformat(d), "close": 220.0 * (1 + 0.01 * (i + 1))},
                        {"symbol": "ZZ", "date": date.fromisoformat(d), "close": 100.0 * (1 + 0.02 * (i + 1))}]
            return pl.DataFrame(out)
    n = od.settle(tmp_path, Repo(), today=date(2026, 10, 29))
    rows = od.today_rows(tmp_path, NOW.date())
    assert n == 4 and rows["BE"]["r1"] == 1.0 and rows["BE"]["r3"] == 3.0 and "r5" not in rows["BE"]
    assert rows["ZZ"]["r1"] == -2.0                                                         # 空头取反
    st = od.shadow_stats(tmp_path)
    assert st["n"] == 2 and st["settled"] == 0 and st["since"] == "2026-10-27"


def test_shadow_window():
    assert od.in_window(NOW) and not od.in_window(NOW.replace(hour=9, minute=25)) and not od.in_window(NOW.replace(hour=15, minute=31))
    assert not od.in_window(datetime(2026, 10, 25, 11, 0, tzinfo=ET))


def test_not_in_trading_chain():
    root = Path(od.__file__).parent
    for name in ("trading_loop.py", "entry_guard.py", "watchlist_book.py", "swing_verdict.py", "notify_opportunity.py", "swing_desk.py"):
        assert "opp_desk" not in (root / name).read_text(encoding="utf-8"), name
    assert "opp_desk" not in (root.parent / "trader" / "trader.py").read_text(encoding="utf-8")


def test_scheduler_registers_shadow_jobs():
    from app.jobs import daily_pipeline
    src = Path(daily_pipeline.__file__).read_text(encoding="utf-8")
    assert 'id="zen_resonance_shadow"' in src and 'id="zen_resonance_settle"' in src and "opp_desk.tick(" in src


def test_page_has_desk_section_and_is_isolated(tmp_path, monkeypatch):
    from app.services import opp_page
    monkeypatch.setattr(od, "regime", lambda *a, **k: {"key": "up", "spy": 0.5, "qqq": 1.2, "n": 2, "up": 2, "down": 0, "up3": 1,
                                                        "chg": {"BE": 8.0}, "px": {"BE": 220.5}, "at": NOW.isoformat()})
    d = od.view(tmp_path, now=NOW, swing=_sw({"symbol": "BE", "side": "long", "state": "pending"}),
                intraday={"active": [{"symbol": "BE", "side": "long", "status": "open", "day": "2026-10-27", "r_now": 0.1}]},
                radar=None, gaps=None, symbols=["BE"])
    assert d["regime"]["key"] == "up" and d["resonance"][0]["chg"] == 8.0 and d["resonance"][0]["price"] == 220.5
    assert "px" not in d["regime"]
    assert not od.shadow_dir(tmp_path).exists()                                                # /page 只读
    json.dumps(d)
    assert opp_page  # imported
