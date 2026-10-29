"""0.28.0 盘中强势卡 (2026-10-20 产品决策: 只上机会板 + 自动结算记战绩, 不下单; 波段出场 2R / 5 天)。"""
import json
from datetime import date, datetime, time
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import intraday_strength as ids

D0 = date(2026, 10, 19)       # 周一


@pytest.fixture(autouse=True)
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def et(d, h, m=0):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=us_clock.tz)


def _flat(price=100.0, vol=1000.0):
    a = {c: np.full(ids.SLOTS, price) for c in ("o", "h", "l", "c", "vw")}
    a["v"] = np.full(ids.SLOTS, vol)
    return a


def _strong(prev=100.0, vol=3000.0):
    """开盘 +1%, 每根 +0.1%, 一路在均价线上 (效率高, 回撤 0)。"""
    a = {c: np.full(ids.SLOTS, np.nan) for c in ("o", "h", "l", "c", "vw", "v")}
    p = prev * 1.01
    for k in range(ids.SLOTS):
        o = p
        c = p * 1.003
        a["o"][k], a["c"][k] = o, c
        a["h"][k], a["l"][k] = c * 1.0005, o * 0.9995
        a["vw"][k] = (o + c) / 2
        a["v"][k] = vol
        p = c
    return a


# ---------- 纯函数 ----------

def test_completed_slots_respects_20min_delay():
    assert ids.completed_slots(time(10, 0)) == 2          # 09:30, 09:35 两根 (09:40 那根 09:45 才结束)
    assert ids.completed_slots(time(15, 0)) == 62
    assert len(ids.CHECKS) == 11 and ids.CHECKS[0] == time(10, 0) and ids.CHECKS[-1] == time(15, 0)


def test_strong_day_passes_and_card_geometry():
    hist = [_flat() for _ in range(25)]
    today = _strong()
    out = ids.evaluate("NVDA", today, hist, hist[-1], time(11, 0), live=today["c"][15] * 1.001)
    assert out is not None
    f = out["features"]
    assert f["chg"] >= 0.02 and f["rvol"] == pytest.approx(3.0) and f["above"] >= 0.7 and f["pull"] <= 0.33
    assert out["entry"] == pytest.approx(out["entry_ref"] * 1.0005, rel=1e-6)
    r = out["entry"] - out["stop"]
    assert 0.005 * out["entry"] - 1e-6 <= r <= 0.04 * out["entry"] + 1e-6
    assert out["target"] == pytest.approx(out["entry"] + 2 * r, abs=1e-3)


@pytest.mark.parametrize("why", ["low_vol", "flat", "few_days", "live_below_vwap", "no_live"])
def test_rule_rejects(why):
    hist = [_flat() for _ in range(25)]
    today = _strong(vol=1000.0 if why == "low_vol" else 3000.0)
    live = today["c"][15]
    if why == "flat":
        today = _flat(100.5, 3000.0)
    if why == "few_days":
        hist = hist[:10]
    if why == "live_below_vwap":
        live = 50.0
    if why == "no_live":
        live = None
    assert ids.evaluate("NVDA", today, hist, hist[-1], time(11, 0), live=live) is None


def test_suspect_split_gap_skipped():
    hist = [_flat(200.0) for _ in range(25)]
    today = _strong(prev=100.0)           # 对昨收 200 跌一半 = 疑似拆股
    assert ids.evaluate("X", today, hist, hist[-1], time(11, 0), live=today["c"][15]) is None


def test_geometry_clamps():
    g = ids.geometry(100.0, vwap=99.9, low_n=99.95)        # 风险太小 -> 0.5%
    assert g["stop"] == pytest.approx(100.05 * 0.995, rel=1e-6)
    g = ids.geometry(100.0, vwap=80.0, low_n=85.0)         # 风险太大 -> 4%
    assert g["stop"] == pytest.approx(100.05 * 0.96, rel=1e-6)
    assert ids.geometry(100.0, vwap=101.0, low_n=99.0) is not None   # min(vwap, low) = 99
    assert ids.geometry(98.0, vwap=101.0, low_n=99.0) is None         # 实时价不在止损上方


# ---------- 结算 ----------

def _card(entry=100.0, stop=98.0, slot=10, day=D0):
    return {"entry": entry, "stop": stop, "target": entry + 2 * (entry - stop), "risk": entry - stop, "slot": slot,
            "day": day.isoformat()}


def test_settle_stop_first_when_both_in_one_bar():
    a = _flat(100.0)
    a["l"][12], a["h"][12] = 97.0, 105.0
    r = ids.settle(_card(), [(D0, a)], window_over=False)
    assert r["exit_reason"] == "stop" and r["exit"] == 98.0 and r["r"] == pytest.approx((98 * 0.9995 - 100) / 2, abs=1e-4)


def test_settle_target_and_gap_open_next_day():
    a = _flat(100.0)
    b = _flat(103.0)
    b["o"][0] = 106.0                      # 次日跳空过目标 104 -> 按开盘 106
    r = ids.settle(_card(), [(D0, a), (date(2026, 10, 20), b)], window_over=False)
    assert r["exit_reason"] == "target" and r["exit"] == 106.0
    c = _flat(100.0)
    c["o"][0] = 97.0                       # 次日跳空破止损 -> 按开盘 97
    r = ids.settle(_card(), [(D0, a), (date(2026, 10, 20), c)], window_over=False)
    assert r["exit_reason"] == "stop" and r["exit"] == 97.0


def test_settle_starts_at_fire_slot_and_time_exit():
    a = _flat(100.0)
    a["l"][5] = 90.0                       # 出卡槽 10 之前的低点不算
    assert ids.settle(_card(), [(D0, a)], window_over=False) is None
    days = [date(2026, 10, 19 + i) for i in range(5)]
    bars = [(d, _flat(101.0)) for d in days]
    r = ids.settle(_card(), bars, window_over=True)
    assert r["exit_reason"] == "time" and r["exit"] == 101.0 and r["exit_at"].startswith("2026-10-23T15:55")


def test_settle_entry_bar_mismatch_voids():
    """出卡那一根 (现拉, 拆股后复权) 与卡上入场参考价差 > 15% -> 作废; 持有期大跳空见 test_holding_gap_is_real_pnl_not_void。"""
    a = _flat(25.0)
    r = ids.settle({**_card(), "entry_ref": 100.0}, [(D0, a), (date(2026, 10, 20), _flat(25.0))], window_over=False)
    assert r["exit_reason"] == "corp_action_suspect" and r["r"] is None


# ---------- 编排 run() ----------

def _frame(symbol, d, a):
    rows = []
    for k in range(ids.SLOTS):
        if np.isnan(a["c"][k]):
            continue
        t = datetime.combine(d, time(9, 30), tzinfo=us_clock.tz) + (k * ids.BAR_MIN) * __import__("datetime").timedelta(minutes=1)
        rows.append((symbol, t.astimezone(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     *(float(a[c][k]) for c in ids.COLS)))
    return rows


def _fake_fetch(days_arrays):
    calls = []

    def fetch(symbols, start, end, until=None):
        calls.append((tuple(symbols), start, end, until))
        rows = []
        for (s, d), a in days_arrays.items():
            if s in symbols and start <= d <= end:
                for r in _frame(s, d, a):
                    if until is None or datetime.fromisoformat(r[1].replace("Z", "+00:00")) < until:
                        rows.append(r)
        return pl.DataFrame(rows, schema=["symbol", "t", *ids.COLS], orient="row")
    return fetch, calls


def _history_days(n=30):
    out = []
    d = D0
    while len(out) < n:
        d = d.fromordinal(d.toordinal() - 1)
        if d.weekday() < 5:
            out.append(d)
    return sorted(out)


def test_run_fires_once_per_day_and_never_touches_ledger(tmp_path):
    arrays = {("NVDA", d): _flat() for d in _history_days()}
    arrays[("NVDA", D0)] = _strong()
    fetch, calls = _fake_fetch(arrays)
    res = ids.run(tmp_path, et(D0, 11, 0), symbols=["NVDA"], fetch=fetch, quote=lambda ss: {s: 104.0 for s in ss})
    assert res["fired"] == ["NVDA"] and res["check"] == "11:00"
    rec = ids.load_day(tmp_path, D0)
    c = rec["cards"][0]
    assert c["card_id"] == "ids-2026-10-19-NVDA" and c["status"] in ("open", "closed") and c["entry_ref"] == 104.0
    # 同日第二个检查点不再出
    res2 = ids.run(tmp_path, et(D0, 11, 30), symbols=["NVDA"], fetch=fetch, quote=lambda ss: {s: 104.0 for s in ss})
    assert res2["fired"] == []
    assert len(ids.load_day(tmp_path, D0)["cards"]) == 1
    assert not (tmp_path / "ledger").exists()
    # 历史缓存已写, 第二次不再拉历史
    hist_calls = [c for c in calls if c[1] != D0]
    assert len(hist_calls) == 1


def test_run_late_or_off_checkpoint_does_not_fire(tmp_path):
    arrays = {("NVDA", d): _flat() for d in _history_days()}
    arrays[("NVDA", D0)] = _strong()
    fetch, calls = _fake_fetch(arrays)
    res = ids.run(tmp_path, et(D0, 11, 15), symbols=["NVDA"], fetch=fetch, quote=lambda ss: {s: 104.0 for s in ss})
    assert res.get("skipped") == "nothing_to_do" and calls == []          # 不在检查点又无在途卡: 不出网


def test_run_settles_on_later_day(tmp_path):
    arrays = {("NVDA", d): _flat() for d in _history_days()}
    arrays[("NVDA", D0)] = _strong()
    fetch, _ = _fake_fetch(arrays)
    res0 = ids.run(tmp_path, et(D0, 10, 30), symbols=["NVDA"], fetch=fetch, quote=lambda ss: {s: 104.0 for s in ss})
    assert res0["fired"] == ["NVDA"]                 # 10:00 只看得到 2 根 (+1.6%), 10:30 看得到 8 根 (+3.5%)
    c = ids.load_day(tmp_path, D0)["cards"][0]
    d1 = date(2026, 10, 20)
    crash = _flat(c["stop"] * 0.98)
    arrays[("NVDA", d1)] = crash
    fetch2, _ = _fake_fetch(arrays)
    res = ids.run(tmp_path, et(d1, 10, 30), symbols=["NVDA"], fetch=fetch2, quote=lambda ss: {})
    c2 = ids.load_day(tmp_path, D0)["cards"][0]
    if c["status"] == "open":
        assert c["card_id"] in res["settled"]
    assert c2["status"] == "closed" and c2["exit_reason"] in ("stop", "target")


def test_run_skips_non_trading_day_and_holds_lock(tmp_path):
    assert ids.run(tmp_path, et(date(2026, 10, 18), 11, 0), symbols=["X"])["skipped"] == "not_trading_day"
    fh = ids._acquire(tmp_path, wait=False)          # 另一个进程 (补算) 正拿着文件锁
    try:
        assert ids.run(tmp_path, et(D0, 11, 0), symbols=["X"])["skipped"] == "busy"
    finally:
        ids._release(fh)


# ---------- 接口 / 调度 / 状态 ----------

def test_build_live_shape_and_stats_exclude_backfill(tmp_path, monkeypatch):
    rec = {"day": "2026-10-16", "checks": [], "cards": [
        {"card_id": "ids-2026-10-16-A", "symbol": "A", "day": "2026-10-16", "status": "closed", "r": 1.0, "entry": 10, "risk": 0.2},
        {"card_id": "ids-2026-10-16-B", "symbol": "B", "day": "2026-10-16", "status": "closed", "r": -1.0, "backfill": True,
         "entry": 10, "risk": 0.2},
        {"card_id": "ids-2026-10-16-C", "symbol": "C", "day": "2026-10-16", "status": "open", "entry": 10.0, "stop": 9.8,
         "target": 10.4, "risk": 0.2, "fired_at": "2026-10-16T11:00:00-04:00"}]}
    p = ids.day_path(tmp_path, date(2026, 10, 16))
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(rec))
    from app.services import swing_desk as sd
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now: {"C": (10.2, "live")})
    monkeypatch.setattr(sd, "_equity_and_risk", lambda data_dir, lg: (100_000.0, 0.005))
    out = ids.build_live(tmp_path, now=et(D0, 12, 0))
    assert [c["symbol"] for c in out["active"]] == ["C"]
    c = out["active"][0]
    assert c["price_source"] == "live" and c["r_now"] == pytest.approx((10.2 * 0.9995 - 10) / 0.2, abs=0.01)
    assert c["odds"]["qty"] == int(min(100_000 * 0.005 / 0.2, 25_000 / 10)) and c["odds"]["ev_usd"] is not None
    assert out["stats"] == {"n": 1, "win": 1.0, "mean_r": 1.0, "sum_r": 1.0}      # 回填卡不进自然战绩
    assert out["next_check"] == "12:30" and out["replay"]["sig"] == 0


def test_bad_record_file_reads_as_empty(tmp_path):
    p = ids.day_path(tmp_path, D0)
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert ids.load_day(tmp_path, D0)["cards"] == []
    p.write_text(json.dumps({"cards": [1, None, {"no": "symbol"}]}))
    assert ids.load_day(tmp_path, D0)["cards"] == []


def test_scheduler_registers_intraday_jobs(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    from app.services import ops_watch as ow
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    j = sched.get_job("zen_intraday_strength")
    s = sched.get_job("zen_intraday_strength_settle")
    assert j is not None and s is not None
    t0 = datetime(2026, 10, 19, 0, 0, tzinfo=us_clock.tz)
    fires = []
    f = j.trigger.get_next_fire_time(None, t0)
    while f is not None and f.astimezone(us_clock.tz).date() == D0:
        fires.append(f.astimezone(us_clock.tz).strftime("%H:%M"))
        f = j.trigger.get_next_fire_time(f, f + __import__("datetime").timedelta(seconds=1))
    assert fires[0] == "10:00" and fires[-1] == "15:30" and len(fires) == 12
    assert s.trigger.get_next_fire_time(None, t0).astimezone(us_clock.tz).strftime("%H:%M") == "16:25"
    for jid in ("zen_intraday_strength", "zen_intraday_strength_settle"):
        assert jid in ow.WATCHED and jid in ow.CATCHUP


def test_account_reset_keeps_intraday_records():
    from app.services import account_reset
    assert not any("intraday" in x for x in account_reset.ARCHIVE_ITEMS)


# ---------- 0.28.0 审查修复 ----------

def test_sparse_pre_listing_days_not_counted_as_base():
    """SPCX 型: 上市前每天 1-7 根零星成交, 不能当量比基线 (旧口径第 20 天量比虚高)。"""
    sparse = _flat()
    for c in ("o", "h", "l", "c", "vw", "v"):
        sparse[c][5:] = np.nan
    hist = [sparse for _ in range(10)] + [_flat() for _ in range(15)]
    today = _strong()
    assert ids.evaluate("SPCX", today, hist, hist[-1], time(11, 0), live=today["c"][15]) is None
    hist = [sparse for _ in range(5)] + [_flat() for _ in range(20)]
    assert ids.evaluate("SPCX", today, hist, hist[-1], time(11, 0), live=today["c"][15]) is not None


def test_late_fire_partial_slot_ignores_pre_fire_extremes():
    """定时任务晚到 (出卡在 10:07, 槽 10:05 已走 2 分钟): 那一根出卡前的低点不算止损, 只看收盘。"""
    a = _flat(100.0)
    a["l"][7] = 97.0                       # 10:05 那根里 (出卡前) 有过 97
    card = {**_card(), "slot": 7, "slot_partial": True}
    assert ids.settle(card, [(D0, a)], window_over=False) is None
    card.pop("slot_partial")
    assert ids.settle(card, [(D0, a)], window_over=False)["exit_reason"] == "stop"
    a["c"][7] = 97.5                       # 收盘已破止损 -> 按收盘出
    r = ids.settle({**card, "slot_partial": True}, [(D0, a)], window_over=False)
    assert r["exit_reason"] == "stop" and r["exit"] == 97.5


def test_holding_gap_is_real_pnl_not_void():
    """持有期真实跳空 (财报 -45%) 按开盘止损计亏, 不作废; 出卡那一根对不上入场参考价 (拆股后复权) 才作废。"""
    a = _flat(100.0)
    b = _flat(55.0)
    card = {**_card(), "entry_ref": 100.0}
    r = ids.settle(card, [(D0, a), (date(2026, 10, 20), b)], window_over=False)
    assert r["exit_reason"] == "stop" and r["exit"] == 55.0 and r["r"] < -20
    split = _flat(25.0)                    # 4:1 拆股后现拉的出卡日K线 = 25, 卡上入场参考价 100
    r = ids.settle(card, [(D0, split)], window_over=False)
    assert r["exit_reason"] == "corp_action_suspect" and r["r"] is None


def test_fetch_error_recorded_not_raised(tmp_path):
    arrays = {("NVDA", d): _flat() for d in _history_days()}
    arrays[("NVDA", D0)] = _strong()

    def boom(*a, **k):
        raise TimeoutError("alpaca")
    res = ids.run(tmp_path, et(D0, 11, 0), symbols=["NVDA"], fetch=boom, quote=lambda ss: {})
    assert res["error"] == "TimeoutError"
    rec = ids.load_day(tmp_path, D0)
    assert rec["errors"][0]["error"] == "TimeoutError" and rec["errors"][0]["check"] == "11:00"
    st = ids.status(tmp_path) if us_clock.today() == D0 else None
    if st:
        assert st["errors"] == 1


def test_today_bars_after_until_cut(tmp_path):
    """SIP 右端之后还没走完的那根不进结算 (今天的 5 分钟K 被截到 now-16min)。"""
    arrays = {("NVDA", d): _flat() for d in _history_days()}
    arrays[("NVDA", D0)] = _strong()
    fetch, calls = _fake_fetch(arrays)
    ids.run(tmp_path, et(D0, 11, 0), symbols=["NVDA"], fetch=fetch, quote=lambda ss: {s: 104.0 for s in ss})
    today_call = [c for c in calls if c[1] == D0 and c[2] == D0][-1]
    assert today_call[3] == et(D0, 10, 44)


def test_new_symbol_only_fetches_itself(tmp_path):
    """缓存已有 NVDA 30 天; 新加 AMD -> 只拉 AMD 的 30 天, 不连带 NVDA 重拉。"""
    arrays = {(s, d): _flat() for d in _history_days() for s in ("NVDA", "AMD")}
    fetch, calls = _fake_fetch(arrays)
    ids.ensure_history(tmp_path, ["NVDA"], D0, fetch=fetch)
    calls.clear()
    ids.ensure_history(tmp_path, ["NVDA", "AMD"], D0, fetch=fetch)
    assert [c[0] for c in calls] == [("AMD",)]
    calls.clear()
    ids.ensure_history(tmp_path, ["NVDA", "AMD"], D0, fetch=fetch)
    assert calls == []


def test_split_drift_refetches_symbol_history(tmp_path):
    arrays = {("NVDA", d): _flat(100.0) for d in _history_days()}
    fetch, _ = _fake_fetch(arrays)
    ids.ensure_history(tmp_path, ["NVDA"], D0, fetch=fetch)
    last = _history_days()[-1]
    split = {k: _flat(25.0) for k in arrays}                # 拆股后 SIP split 复权 = 全部历史 /4
    fetch2, _ = _fake_fetch(split)
    ids._DRIFT_DONE.clear()
    drift = ids._drift_once(tmp_path, D0, ["NVDA"], fetch2)
    assert drift == {"NVDA"}
    hist = ids.ensure_history(tmp_path, ["NVDA"], D0, fetch=fetch2, force=drift)
    arr = ids.to_arrays(hist[last])
    assert ids.last_close(arr[("NVDA", last)]) == 25.0
    ids._DRIFT_DONE.clear()


def test_backfill_marks_cards_and_skips_existing(tmp_path, monkeypatch):
    arrays = {("NVDA", d): _flat() for d in _history_days()}
    arrays[("NVDA", D0)] = _strong()
    fetch, _ = _fake_fetch(arrays)
    monkeypatch.setattr(us_clock, "now", lambda: et(D0, 15, 0))           # 当天还没收盘 -> 不补
    assert ids.backfill_days(tmp_path, [D0], symbols=["NVDA"], fetch=fetch) == {"2026-10-19": "not_closed"}
    assert not ids.day_path(tmp_path, D0).exists()
    monkeypatch.setattr(us_clock, "now", lambda: et(date(2026, 10, 20), 1, 0))
    out = ids.backfill_days(tmp_path, [D0], symbols=["NVDA"], fetch=fetch)
    assert out == {"2026-10-19": 1}
    rec = ids.load_day(tmp_path, D0)
    assert rec["cards"][0]["backfill"] is True and all(c.get("backfill") for c in rec["checks"])
    assert ids.backfill_days(tmp_path, [D0], symbols=["NVDA"], fetch=fetch) == {"2026-10-19": "exists"}


def test_bad_symbols_filtered():
    assert ids._SYM_RE.fullmatch("BRK.B") and ids._SYM_RE.fullmatch("NVDA")
    assert not ids._SYM_RE.fullmatch("600519.SH") and not ids._SYM_RE.fullmatch("nvda")


def test_not_in_trading_chain():
    """只记录不下单: 交易环 / 准入 / 下单簿都不引用本模块。"""
    import pathlib
    root = pathlib.Path(ids.__file__).parent
    for name in ("trading_loop.py", "entry_guard.py", "watchlist_book.py"):
        assert "intraday_strength" not in (root / name).read_text(encoding="utf-8")


# ---------- 0.34.0 放量前 5 / 差一步 (只展示) ----------

def test_check_records_heat_top5_and_near_miss(tmp_path):
    """每个检查点记: 放量前 5 (与出卡同一个量比, 不分涨跌) + 过了前 6 道门却没出卡的票及原因; 不改出卡规则。"""
    arrays = {}
    for s in ("NVDA", "AMD", "TSLA", "MU", "AAPL", "META", "ORCL"):
        for d in _history_days():
            arrays[(s, d)] = _flat()
    arrays[("NVDA", D0)] = _strong(vol=3000.0)                 # 过形态, 实时价在均价线上 -> 出卡
    arrays[("AMD", D0)] = _strong(vol=4000.0)                  # 过形态, 实时价跌到均价线下 -> 差一步
    for i, s in enumerate(("TSLA", "MU", "AAPL", "META", "ORCL")):
        arrays[(s, D0)] = _flat(100.0, 1000.0 * (i + 1))      # 平盘, 量比 1..5
    fetch, _ = _fake_fetch(arrays)
    res = ids.run(tmp_path, et(D0, 11, 0), symbols=sorted({s for s, _ in arrays}), fetch=fetch,
                  quote=lambda ss: {s: (104.0 if s == "NVDA" else 50.0) for s in ss})
    assert res["fired"] == ["NVDA"]
    chk = ids.load_day(tmp_path, D0)["checks"][-1]
    assert [h["symbol"] for h in chk["heat"]] == ["ORCL", "AMD", "META", "AAPL", "NVDA"]       # 量比 5 / 4 / 4 / 3 / 3 (同量比按代码)
    assert chk["heat"][0]["rvol"] == pytest.approx(5.0)
    assert [n["symbol"] for n in chk["near"]] == ["AMD"] and chk["near"][0]["why"] == "below_vwap"
    assert chk["near"][0]["live"] == 50.0
    # 已出过卡的票第二个检查点仍进放量前 5, 但不再算候选 / 差一步
    res2 = ids.run(tmp_path, et(D0, 11, 30), symbols=sorted({s for s, _ in arrays}), fetch=fetch,
                   quote=lambda ss: {s: 109.0 for s in ss})          # 11:30 合成数据均价线约 105, 109 在线上
    chk2 = ids.load_day(tmp_path, D0)["checks"][-1]
    assert "NVDA" in [h["symbol"] for h in chk2["heat"]] and chk2["near"] == []
    assert res2["fired"] == ["AMD"] and len(ids.load_day(tmp_path, D0)["cards"]) == 2   # 差一步的票之后照常能出卡


def test_heat_rows_drops_nan_and_caps():
    feats = {f"S{i}": {"rvol": float(i), "chg": 0.01, "P": 10.0, "vwap": 9.9} for i in range(8)}
    feats["BAD"] = {"rvol": float("nan"), "chg": 0.0, "P": 1.0, "vwap": 1.0}
    out = ids.heat_rows(feats)
    assert [r["symbol"] for r in out] == ["S7", "S6", "S5", "S4", "S3"] and all(r["rvol"] is not None for r in out)
    json.dumps(out, allow_nan=False)


def test_heat_or_near_failure_never_loses_cards(tmp_path, monkeypatch):
    """放量前 5 / 差一步只展示: 它们抛异常时, 出卡与落盘照常 (对抗探针)。"""
    arrays = {(s, d): _flat() for s in ("NVDA", "AMD") for d in _history_days()}
    arrays[("NVDA", D0)] = _strong()
    arrays[("AMD", D0)] = _strong(vol=4000.0)
    fetch, _ = _fake_fetch(arrays)

    def boom(*a, **k):
        raise ValueError("x")
    monkeypatch.setattr(ids, "heat_rows", boom)
    monkeypatch.setattr(ids, "_near_row", boom)
    res = ids.run(tmp_path, et(D0, 11, 0), symbols=["NVDA", "AMD"], fetch=fetch,
                  quote=lambda ss: {s: (104.0 if s == "NVDA" else 50.0) for s in ss})
    assert res["fired"] == ["NVDA"]
    rec = ids.load_day(tmp_path, D0)
    assert [c["symbol"] for c in rec["cards"]] == ["NVDA"]
    assert rec["checks"][-1]["heat"] == [] and rec["checks"][-1]["near"] == []


def test_feature_error_on_already_carded_symbol_does_not_kill_check(tmp_path, monkeypatch):
    """评审 未证实->加固: 0.34.0 起已出过卡的票也算特征 (只为放量前 5)。它算坏了只少这一行, 不让整个检查点失败;
    出卡候选算坏了照旧失败 (与 0.33.0 一致)。"""
    arrays = {(s, d): _flat() for s in ("NVDA", "AMD") for d in _history_days()}
    arrays[("NVDA", D0)] = _strong(vol=3000.0)
    arrays[("AMD", D0)] = _strong(vol=4000.0)
    fetch, _ = _fake_fetch(arrays)
    q = lambda ss: {s: 109.0 for s in ss}  # noqa: E731
    assert ids.run(tmp_path, et(D0, 11, 0), symbols=["NVDA"], fetch=fetch,
                   quote=lambda ss: {s: 104.0 for s in ss})["fired"] == ["NVDA"]
    real = ids.features
    hits = []

    def nvda_breaks(a, n, pc, base):                 # 当天量 3000 的那只 = NVDA (已出过卡)
        if float(np.nanmax(a["v"][:n])) == 3000.0:
            hits.append("NVDA")
            raise ValueError("x")
        return real(a, n, pc, base)
    monkeypatch.setattr(ids, "features", nvda_breaks)
    res = ids.run(tmp_path, et(D0, 11, 30), symbols=["NVDA", "AMD"], fetch=fetch, quote=q)
    rec = ids.load_day(tmp_path, D0)
    assert hits == ["NVDA"] and rec["checks"][-1]["check"] == "11:30"
    assert [h["symbol"] for h in rec["checks"][-1]["heat"]] == ["AMD"]
    assert res["fired"] == ["AMD"] and [c["symbol"] for c in rec["cards"]] == ["NVDA", "AMD"]
    # 出卡候选 (还没出过卡) 算坏了: 与 0.33.0 一样直接抛出 (由调度层记失败), 不落半截卡
    tmp2 = tmp_path / "b"
    with pytest.raises(ValueError):
        ids.run(tmp2, et(D0, 11, 0), symbols=["NVDA"], fetch=fetch, quote=lambda ss: {s: 104.0 for s in ss})
    assert ids.load_day(tmp2, D0)["cards"] == []

def test_firing_unchanged_vs_pre_0340_logic(tmp_path):
    """同一输入: 新 _fire 出卡集合 / 价位与 0.33.0 逻辑 (先跳过已出卡票, 只对候选算) 逐张一致。"""
    rng = np.random.default_rng(3)
    syms = [f"S{i}" for i in range(12)]
    arrays = {(s, d): _flat(vol=1000.0) for s in syms for d in _history_days()}
    for i, s in enumerate(syms):
        arrays[(s, D0)] = _strong(vol=float(rng.uniform(800, 5000))) if i % 2 == 0 else _flat(100.0 + i, float(rng.uniform(500, 5000)))
    fetch, _ = _fake_fetch(arrays)
    quote = lambda ss: {s: 104.0 + (hash(s) % 3) for s in ss}  # noqa: E731
    got = {}
    for chk in (time(10, 30), time(11, 0), time(11, 30)):
        ids.run(tmp_path, et(D0, chk.hour, chk.minute), symbols=syms, fetch=fetch, quote=quote)
    for c in ids.load_day(tmp_path, D0)["cards"]:
        got[c["symbol"]] = (c["check"], c["entry"], c["stop"], c["target"])
    # 0.33.0 口径直接用纯函数复算
    hist = [[arrays.get((s, d)) for d in _history_days()] for s in syms]
    want, done = {}, set()
    for chk in (time(10, 30), time(11, 0), time(11, 30)):
        for s, h in zip(syms, hist, strict=True):
            if s in done:
                continue
            r = ids.evaluate(s, arrays[(s, D0)], h, h[-1], chk, quote([s]).get(s))
            if r is not None:
                want[s] = (chk.strftime("%H:%M"), r["entry"], r["stop"], r["target"])
                done.add(s)
    assert got == want and got
