"""0.34.0 开盘放量榜 (无限 10-22 选「机会页按时段换主角」): 不写台账、不进交易环。0.35.0 起出榜后按齿轮开关 / 门槛推一条飞书。

不变量: 规则与回放 orb.py 同式 (14 有效日相对量 / 资格 / 收阳且相对量>=1 前 3); 09:51 前不出榜; 当天已出榜不重算;
取数失败记进当天记录不抛; 收盘记录先读盘中强势的 5 分钟K 缓存; 页面视图只读; 补算行带 backfill 不进上线后战绩。
"""
import json
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import intraday_strength as ids
from app.services import open_radar as orr

D0 = date(2026, 10, 19)       # 周一


@pytest.fixture(autouse=True)
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    ids._DRIFT_DONE.clear()


def et(d, h, m=0):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=us_clock.tz)


def _day(price=100.0, vol=20_000.0, or_vol=None, or_up=True, rng=2.0):
    """78 根 5 分钟K; 第 0 根 (开盘区间) 量 = or_vol, 收阳 / 收阴; 全天振幅约 rng。"""
    a = {c: np.full(ids.SLOTS, price) for c in ("o", "h", "l", "c", "vw")}
    a["v"] = np.full(ids.SLOTS, vol)
    a["h"] = a["h"] + rng / 2
    a["l"] = a["l"] - rng / 2
    a["o"][0], a["c"][0] = (price, price + 0.5) if or_up else (price + 0.5, price)
    a["h"][0], a["l"][0] = price + 0.8, price - 0.3
    if or_vol is not None:
        a["v"][0] = or_vol
    return a


def _hist(n=20, **kw):
    return [_day(**kw) for _ in range(n)]


# ---------- 纯函数 ----------

def test_evaluate_rvol_and_eligibility():
    hist = _hist(or_vol=10_000.0)
    r = orr.evaluate(_day(or_vol=50_000.0), hist)
    assert r is not None and r["rvol"] == pytest.approx(5.0) and r["dir"] == 1
    assert r["or_high"] == pytest.approx(100.8) and r["or_low"] == pytest.approx(99.7)
    assert r["gap_pct"] == pytest.approx(0.0) and r["or_chg_pct"] == pytest.approx(0.5)
    assert r["atr"] == pytest.approx(2.0)


@pytest.mark.parametrize("why", ["cheap", "thin", "narrow", "few_days", "prev_invalid", "no_open_bar"])
def test_evaluate_rejects(why):
    kw = {"or_vol": 10_000.0}
    hist = _hist(**kw)
    today = _day(or_vol=50_000.0)
    if why == "cheap":
        hist, today = _hist(price=4.0, **kw), _day(price=4.0, or_vol=50_000.0)
    if why == "thin":
        hist = _hist(vol=10_000.0, **kw)               # 78 x 1 万 = 78 万 < 100 万股
    if why == "narrow":
        hist = _hist(rng=0.3, **kw)
        for a in hist:
            a["h"][0], a["l"][0] = 100.1, 99.9
    if why == "few_days":
        hist = hist[:13]
    if why == "prev_invalid":
        hist[-1] = None
    if why == "no_open_bar":
        today["v"][0] = np.nan
    assert orr.evaluate(today, hist) is None


def test_rank_picks_bullish_top3_only():
    rows = {"A": {"rvol": 6.0, "dir": -1}, "B": {"rvol": 5.0, "dir": 1}, "C": {"rvol": 3.0, "dir": 1},
            "D": {"rvol": 2.0, "dir": 1}, "E": {"rvol": 1.5, "dir": 1}, "F": {"rvol": 0.9, "dir": 1}}
    out = orr.rank(rows)
    assert [r["symbol"] for r in out] == ["A", "B", "C", "D", "E", "F"]
    assert [r["symbol"] for r in out if r["pick"]] == ["B", "C", "D"]          # 收阴的 A 不点名; 只取 3 只
    assert [r.get("rank") for r in out if r["pick"]] == [1, 2, 3]
    assert orr.rank({"X": {"rvol": 0.9, "dir": 1}})[0]["pick"] is False       # 相对量 < 1 不点名


def test_matches_replay_rule_on_synthetic_universe():
    """与回放 orb.py 的资格 + 排名逐式一致 (14 有效日均量 / ATR / 开盘根均量; 收阳且 >=1 取前 3)。"""
    rng = np.random.default_rng(7)
    rows = {}
    for i in range(12):
        base = float(rng.uniform(5_000, 20_000))
        hist = _hist(or_vol=base)
        today = _day(or_vol=base * float(rng.uniform(0.5, 6)), or_up=bool(i % 3))
        r = orr.evaluate(today, hist)
        assert r is not None
        rows[f"S{i:02d}"] = r
    out = orr.rank(rows)
    want = sorted([s for s, r in rows.items() if r["dir"] == 1 and r["rvol"] >= 1], key=lambda s: (-rows[s]["rvol"], s))[:3]
    assert [r["symbol"] for r in out if r["pick"]] == want


# ---------- 编排 ----------

def _frame(symbol, d, a):
    rows = []
    for k in range(ids.SLOTS):
        if np.isnan(a["c"][k]):
            continue
        t = datetime.combine(d, time(9, 30), tzinfo=us_clock.tz) + timedelta(minutes=k * ids.BAR_MIN)
        rows.append((symbol, t.astimezone(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     *(float(a[c][k]) for c in ids.COLS)))
    return rows


def _fake_fetch(arrays, fail=False):
    calls = []

    def fetch(symbols, start, end, until=None):
        calls.append((tuple(symbols), start, end, until))
        if fail:
            raise RuntimeError("boom")
        rows = []
        for (s, d), a in arrays.items():
            if s in symbols and start <= d <= end:
                for r in _frame(s, d, a):
                    if until is None or datetime.fromisoformat(r[1].replace("Z", "+00:00")) < until:
                        rows.append(r)
        return pl.DataFrame(rows, schema=["symbol", "t", *ids.COLS], orient="row")
    return fetch, calls


def _history_days(n=30):
    out, d = [], D0
    while len(out) < n:
        d = d.fromordinal(d.toordinal() - 1)
        if d.weekday() < 5:
            out.append(d)
    return sorted(out)


def _arrays():
    arr = {}
    for s, mult, up in (("NVDA", 4.0, True), ("AMD", 2.0, True), ("TSLA", 6.0, False), ("MU", 0.5, True)):
        for d in _history_days():
            arr[(s, d)] = _day(or_vol=10_000.0)
        arr[(s, D0)] = _day(or_vol=10_000.0 * mult, or_up=up)
    return arr


def test_run_before_ready_does_nothing(tmp_path):
    fetch, calls = _fake_fetch(_arrays())
    assert orr.run(tmp_path, et(D0, 9, 45), symbols=["NVDA"], fetch=fetch)["skipped"] == "too_early"
    assert orr.run(tmp_path, et(date(2026, 10, 18), 10, 0), symbols=["NVDA"], fetch=fetch)["skipped"] == "not_trading_day"
    assert calls == [] and not orr.day_path(tmp_path, D0).exists()


def test_run_writes_board_once_and_never_touches_ledger(tmp_path):
    fetch, calls = _fake_fetch(_arrays())
    res = orr.run(tmp_path, et(D0, 9, 51), symbols=["NVDA", "AMD", "TSLA", "MU"], fetch=fetch)
    assert res["picks"] == ["NVDA", "AMD"]                                     # TSLA 收阴, MU 相对量 < 1
    rec = orr.load_day(tmp_path, D0)
    assert rec["contract"] == orr.CONTRACT and [r["symbol"] for r in rec["rows"]] == ["TSLA", "NVDA", "AMD", "MU"]
    # 当天 SIP 只取到 now-16 分钟 (09:35 前): 只用开盘那一根
    today_calls = [c for c in calls if c[1] == D0]
    assert today_calls and today_calls[-1][3] == et(D0, 9, 35)
    n = len(calls)
    assert orr.run(tmp_path, et(D0, 9, 56), symbols=["NVDA"], fetch=fetch)["skipped"] == "exists"
    assert len(calls) == n                                                      # 09:56 不再出网
    assert not (tmp_path / "ledger").exists() and not (tmp_path / "notifications").exists()
    # 历史缓存与盘中强势共用
    assert ids.cache_path(tmp_path, _history_days()[-1]).exists()


def test_run_fetch_failure_recorded_not_raised(tmp_path):
    fetch, _ = _fake_fetch({}, fail=True)
    res = orr.run(tmp_path, et(D0, 9, 51), symbols=["NVDA"], fetch=fetch)
    assert res == {"error": "RuntimeError"}
    rec = orr.load_day(tmp_path, D0)
    assert rec["rows"] == [] and rec["errors"][0]["error"] == "RuntimeError"
    fetch2, _ = _fake_fetch(_arrays())
    assert orr.run(tmp_path, et(D0, 9, 56), symbols=["NVDA", "AMD"], fetch=fetch2)["picks"] == ["NVDA", "AMD"]   # 09:56 重试成功
    assert orr.load_day(tmp_path, D0)["errors"]                                # 失败记录保留


def test_run_busy_lock_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(orr, "LOCK_WAIT_S", 0.2)                              # 生产等 60 秒; 测试只等 0.2 秒
    fh = ids._acquire(tmp_path, wait=False)
    try:
        assert orr.run(tmp_path, et(D0, 9, 51), symbols=["NVDA"], fetch=_fake_fetch(_arrays())[0])["skipped"] == "busy"
    finally:
        ids._release(fh)


def test_settle_reads_intraday_cache_first(tmp_path):
    arr = _arrays()
    fetch, _ = _fake_fetch(arr)
    orr.run(tmp_path, et(D0, 9, 51), symbols=["NVDA", "AMD"], fetch=fetch)
    assert orr.settle(tmp_path, et(D0, 15, 0))["skipped"] == "not_closed"
    full = arr[("NVDA", D0)].copy()
    full = {c: v.copy() for c, v in arr[("NVDA", D0)].items()}
    full["c"][-1] = 110.0
    full["h"][40] = 112.0
    df = pl.DataFrame(_frame("NVDA", D0, full), schema=["symbol", "t", *ids.COLS], orient="row")
    ids._write_cache(tmp_path, D0, df, {"NVDA"})                               # 盘中强势 16:17 后写的当天缓存
    fetch2, calls2 = _fake_fetch(arr)
    res = orr.settle(tmp_path, et(D0, 16, 40), fetch=fetch2)
    assert res["settled"] == 2
    assert [c[0] for c in calls2] == [("AMD",)]                                # 缓存里有的 NVDA 不再出网
    rows = {r["symbol"]: r for r in orr.load_day(tmp_path, D0)["rows"]}
    pc = rows["NVDA"]["prev_close"]
    assert rows["NVDA"]["chg_close_pct"] == pytest.approx((110 / pc - 1) * 100, abs=1e-3)
    assert rows["NVDA"]["high_pct"] == pytest.approx((112 / pc - 1) * 100, abs=1e-3)
    assert orr.settle(tmp_path, et(D0, 17, 0), fetch=fetch2)["skipped"] == "done"


def test_view_today_and_previous_and_stats(tmp_path, monkeypatch):
    fetch, _ = _fake_fetch(_arrays())
    orr.run(tmp_path, et(D0, 9, 51), symbols=["NVDA", "AMD", "TSLA"], fetch=fetch)
    from app.services import swing_desk as sd
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now: {"NVDA": (104.0, "live")})
    v = orr.view(tmp_path, now=et(D0, 10, 0))
    assert v["is_today"] and [r["symbol"] for r in v["picks"]] == ["NVDA", "AMD"]
    nv = v["picks"][0]
    assert nv["price"] == 104.0 and nv["chg_now_pct"] == pytest.approx((104 / nv["prev_close"] - 1) * 100, abs=1e-3)
    assert [r["symbol"] for r in v["others"]] == ["TSLA"]                      # 收阴但相对量 >= 2: 只列「也放量」
    # 第二天 09:40 还没出榜: 给上一交易日的榜, is_today=False, pending=True
    d1 = date(2026, 10, 20)
    v1 = orr.view(tmp_path, now=et(d1, 9, 40))
    assert v1["day"] == D0.isoformat() and not v1["is_today"] and v1["pending"]
    assert "price" not in v1["picks"][0]                                       # 不给旧榜现价
    # 战绩: 只算上线后 (非补算) 且已记收盘的点名票
    rec = orr.load_day(tmp_path, D0)
    for r in rec["rows"]:
        r["chg_close_pct"] = 6.0 if r["symbol"] == "NVDA" else 1.0
    orr._save_day(tmp_path, rec)
    assert orr.view(tmp_path, now=et(d1, 9, 40))["stats"] == {"n": 2, "hit": 1}


def test_backfill_marks_rows_and_excluded_from_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: et(date(2026, 10, 21), 9, 0))
    fetch, _ = _fake_fetch(_arrays())
    out = orr.backfill_days(tmp_path, [D0], symbols=["NVDA", "AMD"], fetch=fetch)
    assert out[D0.isoformat()] == ["NVDA", "AMD"]
    rec = orr.load_day(tmp_path, D0)
    assert rec["backfill"] and all(r["backfill"] for r in rec["rows"]) and rec.get("settled_at")
    assert orr._stats(tmp_path, date(2026, 10, 21)) is None
    assert orr.backfill_days(tmp_path, [D0], symbols=["NVDA"], fetch=fetch)[D0.isoformat()] == "exists"


def test_scheduler_registers_open_radar(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    from app.services import ops_watch as ow
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    j, s = sched.get_job("zen_open_radar"), sched.get_job("zen_open_radar_settle")
    assert j is not None and s is not None
    t0 = datetime(2026, 10, 19, 0, 0, tzinfo=us_clock.tz)
    f1 = j.trigger.get_next_fire_time(None, t0).astimezone(us_clock.tz)
    f2 = j.trigger.get_next_fire_time(f1, f1 + timedelta(seconds=1)).astimezone(us_clock.tz)
    assert (f1.strftime("%H:%M"), f2.strftime("%H:%M")) == ("09:51", "09:56")
    s1 = s.trigger.get_next_fire_time(None, t0).astimezone(us_clock.tz)
    s2 = s.trigger.get_next_fire_time(s1, s1 + timedelta(seconds=1)).astimezone(us_clock.tz)
    assert (s1.strftime("%H:%M"), s2.strftime("%H:%M")) == ("16:40", "17:40")          # 0.34.1: 17:40 补缺
    for jid in ("zen_open_radar", "zen_open_radar_settle"):
        assert jid in ow.WATCHED and jid in ow.CATCHUP
    assert ow.CATCHUP["zen_open_radar"] == time(10, 30)


def test_not_in_trading_chain():
    """开盘放量榜不被交易环 / 准入 / 下单簿 / 推送引用。"""
    import pathlib
    root = pathlib.Path(orr.__file__).parent
    for name in ("trading_loop.py", "entry_guard.py", "watchlist_book.py", "swing_verdict.py", "notify_opportunity.py",
                 "premarket_gap.py", "pm_rank.py"):
        p = root / name
        if p.exists():
            assert "open_radar" not in p.read_text(encoding="utf-8"), name


def test_account_reset_keeps_radar_records():
    """开盘放量榜记录 (opportunities/radar) 不随账户重置归档。0.39.0 起雷达帧 (opportunities/radar_frames) 记的是
    旧台账的持仓 / 名额状态, 随台账归档 —— 两者分开断言, 不用子串 "radar" 一把抓。"""
    from app.services import account_reset
    assert "opportunities/radar" not in account_reset.ARCHIVE_ITEMS
    assert not any(x == "opportunities/radar" or x.startswith("opportunities/radar/") for x in account_reset.ARCHIVE_ITEMS)
    assert "opportunities/radar_frames" in account_reset.ARCHIVE_ITEMS


def test_bad_record_reads_as_none(tmp_path):
    p = orr.day_path(tmp_path, D0)
    p.parent.mkdir(parents=True)
    p.write_text("{nope")
    assert orr.load_day(tmp_path, D0) is None
    p.write_text(json.dumps({"day": D0.isoformat(), "rows": [1, {"x": 1}, {"symbol": "A", "rvol": 1, "pick": True}]}))
    assert [r["symbol"] for r in orr.load_day(tmp_path, D0)["rows"]] == ["A"]


def test_view_today_ran_but_empty_does_not_fall_back(tmp_path):
    """评审 P2: 今天跑过但没有一只有资格 -> 显示今天的空榜, 不退回上一交易日; 状态页 ran=True。"""
    fetch, _ = _fake_fetch(_arrays())
    orr.run(tmp_path, et(D0, 9, 51), symbols=["NVDA", "AMD"], fetch=fetch)
    d1 = date(2026, 10, 20)
    orr._save_day(tmp_path, {"contract": orr.CONTRACT, "day": d1.isoformat(), "at": et(d1, 9, 51).isoformat(),
                             "symbols": 2, "rows": [], "errors": []})
    v = orr.view(tmp_path, now=et(d1, 10, 0))
    assert v["day"] == d1.isoformat() and v["is_today"] and v["picks"] == [] and v["eligible"] == 0
    from unittest.mock import patch
    with patch.object(us_clock, "today", lambda: d1):
        st = orr.status(tmp_path)
    assert st["ran"] and not st["ready"]
    # 只有取数失败记录 (没有 at) 的今天 = 还没出榜, 照旧给上一交易日
    d2 = date(2026, 10, 21)
    orr._save_day(tmp_path, {"contract": orr.CONTRACT, "day": d2.isoformat(), "rows": [],
                             "errors": [{"at": et(d2, 9, 51).isoformat(), "error": "X"}]})
    v2 = orr.view(tmp_path, now=et(d2, 10, 0))
    assert v2["day"] == D0.isoformat() and not v2["is_today"]                  # 10-20 空榜不当「上一交易日的榜」, 给最近一个有票的


def test_lock_wait_s_waits_then_gets_lock(tmp_path):
    """评审 P2: 开盘放量榜与盘中强势共用一把锁; 定时任务改为最多等 wait_s 秒, 不再一碰就跳过。"""
    import threading
    import time as _t
    fh = ids._acquire(tmp_path, wait=False)
    threading.Timer(0.5, lambda: ids._release(fh)).start()
    t0 = _t.monotonic()
    fh2 = ids._acquire(tmp_path, wait=False, wait_s=5)
    assert fh2 is not None and 0.3 < _t.monotonic() - t0 < 4
    t1 = _t.monotonic()
    assert ids._acquire(tmp_path, wait=False, wait_s=1.2) is None            # 一直被占: 等满 1.2 秒放弃
    assert 1.0 < _t.monotonic() - t1 < 3
    ids._release(fh2)
    assert ids.LOCK_WAIT_S < ids.MAX_LATE_S and orr.LOCK_WAIT_S <= ids.LOCK_WAIT_S



# ---------- 0.34.1 审查修复 ----------

def test_rank_uses_unrounded_rvol_like_replay():
    """排序 / 过阈值用未舍入的相对量 (回放 orb.py 同式); 舍入只用于展示, 不落盘原值。"""
    rows = {"A": {"rvol": 1.0, "rvol_raw": 0.9996, "dir": 1}, "B": {"rvol": 2.0, "rvol_raw": 2.00049, "dir": 1},
            "C": {"rvol": 2.0, "rvol_raw": 2.00011, "dir": 1}}
    out = orr.rank(rows)
    assert [r["symbol"] for r in out] == ["B", "C", "A"]                    # 舍入后 B / C 同为 2.0, 原值 B 更大
    assert [r["symbol"] for r in out if r["pick"]] == ["B", "C"]            # A 原值 < 1 不点名 (舍入后 1.0)
    assert all("rvol_raw" not in r for r in out)


def test_evaluate_drops_non_finite_rows():
    hist = _hist(or_vol=10_000.0)
    for a in hist:
        a["h"][:] = np.nan                                                   # 缺高价 -> ATR 非有限
    assert orr.evaluate(_day(or_vol=50_000.0), hist) is None


def test_run_never_fails_to_save_on_nan(tmp_path, monkeypatch):
    arr = _arrays()
    for d in _history_days():
        arr[("AMD", d)]["h"][:] = np.nan
    fetch, _ = _fake_fetch(arr)
    res = orr.run(tmp_path, et(D0, 9, 51), symbols=["NVDA", "AMD"], fetch=fetch)
    assert res["picks"] == ["NVDA"]
    assert [r["symbol"] for r in orr.load_day(tmp_path, D0)["rows"]] == ["NVDA"]


def test_settle_missing_does_not_mark_settled_and_retries(tmp_path):
    arr = _arrays()
    fetch, _ = _fake_fetch(arr)
    orr.run(tmp_path, et(D0, 9, 51), symbols=["NVDA", "AMD"], fetch=fetch)
    bad, _ = _fake_fetch({}, fail=True)
    res = orr.settle(tmp_path, et(D0, 16, 40), fetch=bad)
    rec = orr.load_day(tmp_path, D0)
    assert res["missing"] == 2 and not rec.get("settled_at")
    assert rec["errors"][-1]["error"] == "settle_missing"
    assert orr.view(tmp_path, now=et(D0, 16, 45))["settled"] is False
    res2 = orr.settle(tmp_path, et(D0, 17, 40), fetch=fetch)                # 17:40 再补
    assert res2["settled"] == 2 and orr.load_day(tmp_path, D0).get("settled_at")


def test_drift_mark_only_after_refetch_succeeds(tmp_path, monkeypatch):
    """开盘榜先查出拆股、随后取数失败: 不能把今天标成已查, 否则 10:00 盘中强势会继续用拆股前的历史缓存。"""
    arr = _arrays()
    fetch, _ = _fake_fetch(arr)
    orr.run(tmp_path, et(D0, 9, 51), symbols=["NVDA", "AMD"], fetch=fetch)  # 冷启动写好缓存
    ids._DRIFT_DONE.clear()
    d1 = date(2026, 10, 20)
    ref = D0
    split = {k: ({c: (v / 2 if c in ("o", "h", "l", "c", "vw") else v * 2) for c, v in a.items()} if k[0] == "NVDA" else a)
             for k, a in arr.items()}                                        # NVDA 1 拆 2: 现拉的复权价减半
    calls = {"n": 0}

    def flaky(symbols, start, end, until=None):
        calls["n"] += 1
        if start == end == ref:
            return _fake_fetch(split)[0](symbols, start, end, until)       # 复权检查: 现拉 10-19
        raise RuntimeError("alpaca down")                                    # 重拉历史失败
    res = orr.run(tmp_path, et(d1, 9, 51), symbols=["NVDA", "AMD"], fetch=flaky)
    assert res == {"error": "RuntimeError"}
    assert ids._DRIFT_DONE.get(str(tmp_path)) != d1                          # 今天没标已查
    assert ids._drift_once(tmp_path, d1, ["NVDA", "AMD"], _fake_fetch(split)[0]) == {"NVDA"}   # 下一个任务照样查出



# ---------- 0.35.0 飞书推送 (无限 10-22「开盘榜推送到飞书, 开关我手动决定」) ----------

def _q():
    sent = []

    def enqueue(data_dir, key, payload):
        sent.append((key, payload))
        return True
    return sent, enqueue


def _board(tmp_path):
    """NVDA 相对量 4.0 / AMD 2.0 点名, TSLA 收阴不点名。"""
    fetch, _ = _fake_fetch(_arrays())
    orr.run(tmp_path, et(D0, 9, 51), symbols=["NVDA", "AMD", "TSLA", "MU"], fetch=fetch)


def _nolive(monkeypatch):
    from app.services import opp_page
    from app.services import swing_desk as sd
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now: {"NVDA": (104.0, "live")})
    monkeypatch.setattr(opp_page, "card_context", lambda data_dir, syms, prices, today: (
        {"NVDA": {"earnings": {"date": "2026-10-21", "timing": "amc"}, "news": {"count_24h": 3}}}, {}))


def test_push_settings_default_on_3x_and_bad_file_falls_back(tmp_path):
    assert orr.push_settings(tmp_path) == {"enabled": True, "min_rvol": 3.0}
    p = orr.push_path(tmp_path)
    p.parent.mkdir(parents=True)
    for bad in ("{nope", "[]", json.dumps({"enabled": "no", "min_rvol": 4}), json.dumps({"enabled": 1, "min_rvol": True})):
        p.write_text(bad)
        assert orr.push_settings(tmp_path) == {"enabled": True, "min_rvol": 3.0}, bad
    assert orr.save_push_settings(tmp_path, enabled=False, min_rvol=5) == {"enabled": False, "min_rvol": 5.0}
    assert orr.push_settings(tmp_path) == {"enabled": False, "min_rvol": 5.0}
    with pytest.raises(ValueError):
        orr.save_push_settings(tmp_path, enabled=True, min_rvol=4)
    assert set(orr.PUSH_REPLAY) == set(orr.PUSH_LEVELS)


def test_push_only_picks_at_or_above_threshold_once_per_day(tmp_path, monkeypatch):
    _board(tmp_path)
    _nolive(monkeypatch)
    sent, enq = _q()
    res = orr.push(tmp_path, now=et(D0, 9, 51), enqueue=enq)
    assert res == {"pushed": ["NVDA"], "min_rvol": 3.0, "queued": True}             # AMD 2.0x < 3x; TSLA 收阴不在点名里
    key, msg = sent[0]
    assert key == f"open_radar:{D0.isoformat()}" and msg["title"] == "ZenTrade · 开盘放量"
    body = msg["body"]
    assert "**NVDA** 相对量 4.0x" in body and "现在 +" in body and "财报 10-21盘后" in body and "新闻 3" in body
    assert "AMD" not in body and "TSLA" not in body
    assert not any(w in body for w in ("止损", "目标", "入场", "挂单"))                    # 只点名, 不给价位
    assert orr.load_day(tmp_path, D0)["push"]["symbols"] == ["NVDA"]
    # 09:56 再跑: 不再推
    assert orr.push(tmp_path, now=et(D0, 9, 56), enqueue=enq)["skipped"] == "sent" and len(sent) == 1


def test_push_threshold_levels_and_off_switch(tmp_path, monkeypatch):
    _board(tmp_path)
    _nolive(monkeypatch)
    sent, enq = _q()
    orr.save_push_settings(tmp_path, enabled=False, min_rvol=1)
    assert orr.push(tmp_path, now=et(D0, 9, 51), enqueue=enq) == {"skipped": "off"} and not sent
    assert "push" not in orr.load_day(tmp_path, D0)                                      # 关着: 不记推送
    orr.save_push_settings(tmp_path, enabled=True, min_rvol=1)
    assert orr.push(tmp_path, now=et(D0, 9, 52), enqueue=enq)["pushed"] == ["NVDA", "AMD"]


def test_push_none_above_threshold_records_and_lowering_threshold_retries(tmp_path, monkeypatch):
    _board(tmp_path)
    _nolive(monkeypatch)
    sent, enq = _q()
    orr.save_push_settings(tmp_path, enabled=True, min_rvol=5)
    assert orr.push(tmp_path, now=et(D0, 9, 51), enqueue=enq) == {"pushed": [], "min_rvol": 5.0} and not sent
    assert orr.load_day(tmp_path, D0)["push"] == {"at": et(D0, 9, 51).isoformat(timespec="seconds"), "min_rvol": 5.0, "symbols": []}
    orr.save_push_settings(tmp_path, enabled=True, min_rvol=3)                            # 09:51 后调低门槛: 09:56 那次再看
    assert orr.push(tmp_path, now=et(D0, 9, 56), enqueue=enq)["pushed"] == ["NVDA"] and len(sent) == 1


def test_push_skips_late_no_board_backfill_and_failed_enqueue_retries(tmp_path, monkeypatch):
    sent, enq = _q()
    assert orr.push(tmp_path, now=et(D0, 9, 51), enqueue=enq) == {"skipped": "no_board"}
    _board(tmp_path)
    _nolive(monkeypatch)
    assert orr.push(tmp_path, now=et(D0, 10, 30), enqueue=enq) == {"skipped": "late"} and not sent
    # 入队失败 (磁盘): 不记已推, 09:56 再试
    res = orr.push(tmp_path, now=et(D0, 9, 51), enqueue=lambda *a: False)
    assert res["queued"] is False and "push" not in orr.load_day(tmp_path, D0)
    assert orr.push(tmp_path, now=et(D0, 9, 56), enqueue=enq)["pushed"] == ["NVDA"]
    # 补算的日子不推
    rec = orr.load_day(tmp_path, D0)
    rec.pop("push")
    rec["backfill"] = True
    orr._save_day(tmp_path, rec)
    assert orr.push(tmp_path, now=et(D0, 9, 57), enqueue=enq) == {"skipped": "no_board"}


def test_push_failures_in_prices_or_context_still_push(tmp_path, monkeypatch):
    _board(tmp_path)
    from app.services import opp_page
    from app.services import swing_desk as sd

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(sd, "_live_prices", boom)
    monkeypatch.setattr(opp_page, "card_context", boom)
    sent, enq = _q()
    assert orr.push(tmp_path, now=et(D0, 9, 51), enqueue=enq)["pushed"] == ["NVDA"]
    assert "现在" not in sent[0][1]["body"] and "**NVDA** 相对量 4.0x" in sent[0][1]["body"]


def test_push_real_outbox_dedupes_by_day_and_channel(tmp_path, monkeypatch):
    """真 outbox: 全局选择飞书时按 日+渠道 唯一; 未配置 = unconfigured 留存。"""
    from app.services import notify_opportunity as notify
    _board(tmp_path)
    _nolive(monkeypatch)
    monkeypatch.setattr(notify.preferences, "get_webhook_default_channels", lambda: ["feishu"])
    monkeypatch.setattr(notify, "_configured", lambda: False)
    assert orr.push(tmp_path, now=et(D0, 9, 51))["queued"] is True
    st = notify.notification_status(tmp_path)
    assert st["counts"]["unconfigured"] == 1
    assert notify._queue(tmp_path).status(f"open_radar:{D0.isoformat()}:feishu")["status"] == "unconfigured"


def test_push_without_global_channels_does_not_mark_sent(tmp_path, monkeypatch):
    from app.services import notify_opportunity as notify
    _board(tmp_path)
    _nolive(monkeypatch)
    monkeypatch.setattr(notify.preferences, "get_webhook_default_channels", lambda: [])

    assert orr.push(tmp_path, now=et(D0, 9, 51)) == {"skipped": "no_channels"}
    rec = orr.load_day(tmp_path, D0)
    assert rec is not None
    assert "push" not in rec
    assert orr.push(tmp_path, now=et(D0, 9, 56)) == {"skipped": "no_channels"}


def test_push_rechecks_new_global_channels_on_retry(tmp_path, monkeypatch):
    from app.services import notify_opportunity as notify
    _board(tmp_path)
    _nolive(monkeypatch)
    selected = [["feishu"]]
    monkeypatch.setattr(notify.preferences, "get_webhook_default_channels", lambda: selected[0])
    calls = []

    def route(data_dir, key, payload, **kwargs):
        channels = list(kwargs.get("channels") or selected[0])
        calls.append((key, channels))
        return {"selected": channels, "queued": channels, "ok": True}

    monkeypatch.setattr(notify, "enqueue_global", route)
    assert orr.push(tmp_path, now=et(D0, 9, 51))["channels"] == ["feishu"]
    selected[0] = ["wecom"]
    assert orr.push(tmp_path, now=et(D0, 9, 56))["channels"] == ["wecom"]
    assert len(calls) == 2
    rec = orr.load_day(tmp_path, D0)
    assert rec is not None
    assert rec["push"]["channels"] == ["feishu", "wecom"]
    assert orr.push(tmp_path, now=et(D0, 9, 57))["skipped"] == "sent"
    assert len(calls) == 2


def test_view_and_status_show_push_state(tmp_path, monkeypatch):
    _board(tmp_path)
    _nolive(monkeypatch)
    v = orr.view(tmp_path, now=et(D0, 9, 52))
    assert v["push"] == {"enabled": True, "min_rvol": 3.0, "today": None}
    orr.push(tmp_path, now=et(D0, 9, 52), enqueue=_q()[1])
    v = orr.view(tmp_path, now=et(D0, 9, 53))
    assert v["push"]["today"]["symbols"] == ["NVDA"]
    monkeypatch.setattr(us_clock, "today", lambda: D0)
    st = orr.status(tmp_path)
    assert st["pushed"] == ["NVDA"] and st["push"] == {"enabled": True, "min_rvol": 3.0}


def test_scheduler_job_pushes_after_board(tmp_path, monkeypatch):
    """调度任务: 出榜 -> 推送 -> 立刻 flush; 推送抛错不影响出榜结果。"""
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    from app.services import notify_opportunity as notify
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    calls = []
    monkeypatch.setattr(orr, "run", lambda data_dir: calls.append("run") or {"picks": ["NVDA"]})
    monkeypatch.setattr(orr, "push", lambda data_dir, repo=None: calls.append("push") or {"pushed": ["NVDA"], "queued": True})
    monkeypatch.setattr(notify, "flush_notifications", lambda data_dir, limit=10: calls.append("flush") or {})
    sched = dp.start_scheduler(state.repo, MagicMock())
    job = sched.get_job("zen_open_radar")
    res = job.func()
    assert calls == ["run", "push", "flush"] and res["push"]["pushed"] == ["NVDA"]

    def boom(data_dir, repo=None):
        raise RuntimeError("x")
    monkeypatch.setattr(orr, "push", boom)
    assert job.func() == {"picks": ["NVDA"]}
