# ruff: noqa: RUF003
"""0.51.0 今日多空 (services/ls_rank.py): 名单 / 结算 / 页面投影 / 调度 / 只读边界。"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import ls_rank as lr


@pytest.fixture(autouse=True)
def _cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    lr._SPARK.clear()
    lr._SETTLE_CACHE.clear()


def _bdays(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def _bars(days: list[date], seed: int, base: float = 100.0, vol: float = 1e6) -> list[dict]:
    rng = np.random.default_rng(seed)
    c = base
    rows = []
    for d in days:
        o = c * (1 + rng.normal(0, 0.005))
        c = max(1.0, o * (1 + rng.normal(0, 0.02)))
        h, lo = max(o, c) * (1 + abs(rng.normal(0, 0.006))), min(o, c) * (1 - abs(rng.normal(0, 0.006)))
        v = vol * (1 + abs(rng.normal(0, 0.3)))
        rows.append({"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": v, "amount": v * c, "raw_close": c})
    return rows


def _write_daily(data_dir: Path, frames: dict[str, list[dict]]) -> None:
    rows = [{"symbol": s, **r} for s, rs in frames.items() for r in rs]
    df = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    for c in ("raw_high", "raw_low", "turnover_rate"):
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    df = df.with_columns(pl.lit(None, dtype=pl.Int64).alias("quote_ts"))
    base = data_dir / "kline_daily_enriched"
    for (d,), g in df.group_by(["date"]):
        p = base / f"date={d.isoformat()}" / "part.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        g.drop("date").with_columns(pl.lit(d).alias("date")).write_parquet(p)


class _Model:
    """预测 = rev1 (昨日跌得多的分高): 排序可手算。"""

    def predict(self, x):
        return np.nan_to_num(np.asarray(x)[:, 0], nan=0.0)


MODEL = {"est": _Model(), "meta": {"quarter": "2026Q4", "trained_to": "2026-09-22", "rows": 1, "version": lr.MODEL_VERSION}}
AS_OF = date(2026, 11, 4)      # 周三
NEXT = date(2026, 11, 5)


def _universe(tmp_path: Path, n: int = 20, young: str | None = "YNG") -> list[str]:
    days = _bdays(AS_OF, 200)
    frames = {f"S{i:02d}": _bars(days, i, base=50 + i) for i in range(n)}
    if young:
        frames[young] = _bars(days[-100:], 99)
    _write_daily(tmp_path, frames)
    return sorted(frames)


# ---------- 结算口径 ----------

def test_exit_r_stop_gap_and_time_exit():
    a = 2.0
    flat = [(100.0, 101.0, 99.0, 100.0)] * 5
    r, why = lr.exit_r(flat, "long", a, cost=0.0)
    assert (round(r, 6), why) == (0.0, "time")
    hit = [(100.0, 101.0, 99.0, 100.0), (100.0, 100.5, 95.5, 97.0), *flat[2:]]          # 第 2 天盘中破 96 → −2 ATR
    assert lr.exit_r(hit, "long", a, cost=0.0) == (-2.0, "stop")
    gap = [(100.0, 101.0, 99.0, 100.0), (94.0, 95.0, 93.0, 94.5), *flat[2:]]            # 第 2 天开盘跳空穿止损 → 按开盘价
    assert lr.exit_r(gap, "long", a, cost=0.0) == (-3.0, "stop")
    up = [(100.0, 101.0, 99.0, 100.0)] * 4 + [(100.0, 106.0, 99.0, 105.0)]
    assert lr.exit_r(up, "long", a, cost=0.0) == (2.5, "time")
    assert lr.exit_r(up, "short", a, cost=0.0) == (-2.0, "stop")                         # 空头: 盘中涨破 104
    r, _ = lr.exit_r(flat, "long", a)
    assert r == pytest.approx(-0.001 * 100 / a)                                           # 10bp 往返成本按 ATR 折算
    assert lr.exit_r(flat[:4], "long", a) is None


# ---------- 特征: 训练与出名单同一口径 ----------

def test_feature_rows_equal_panel_features(tmp_path):
    days = _bdays(AS_OF, 120)
    rows = _bars(days, 7)
    nxt = {**rows[-1], "date": NEXT}                     # 多一根假的 "名单日" K 线: 面板该行的特征只用到 AS_OF
    df = pl.DataFrame([{"symbol": "X", **r} for r in [*rows, nxt]]).with_columns(pl.col("date").cast(pl.Date))
    panel = lr.build_panel(df).filter(pl.col("date") == NEXT).to_dicts()[0]
    fr = lr.feature_rows(df.filter(pl.col("date") <= AS_OF), ["X"])["X"]
    for k in lr.FEATURES:
        assert fr[k] == pytest.approx(panel[k], rel=1e-9, abs=1e-12), k
    assert fr["last"] == AS_OF and fr["bars"] == 120


# ---------- 出名单 ----------

def test_generate_fixed_five_long_five_short_and_young_not_listed(tmp_path):
    syms = _universe(tmp_path)
    res = lr.generate(tmp_path, AS_OF, next_day=NEXT, watch=syms, model=MODEL)
    assert res["status"] == "ok" and res["day"] == NEXT.isoformat()
    rec = lr.load_day(tmp_path, NEXT)
    assert len(rec["long"]) == 5 and len(rec["short"]) == 5 and not set(rec["long"]) & set(rec["short"])
    assert [r["rank"] for r in rec["rows"]] == list(range(1, len(rec["rows"]) + 1))
    by = {r["symbol"]: r for r in rec["rows"]}
    assert by["YNG"]["young"] and by["YNG"]["side"] is None                              # 上市不足 130 日: 排名但不上榜
    feats = lr.feature_rows(lr.load_daily(tmp_path, AS_OF - timedelta(days=400), AS_OF), syms)
    elig = sorted((s for s in feats if feats[s]["bars"] >= lr.MIN_HISTORY), key=lambda s: (-feats[s]["rev1"], s))
    assert rec["long"] == elig[:5] and rec["short"] == list(reversed(elig))[:5]          # 按模型分数排 (假模型 = rev1)
    assert len(rec["random"]["long"]) == 5 and len(rec["random"]["short"]) == 5
    assert rec["model"]["quarter"] == "2026Q4" and rec["as_of"] == AS_OF.isoformat()
    assert all(by[s]["book"] == "long" for s in rec["long"]) and all(by[s]["book"] == "short" for s in rec["short"])
    assert lr.generate(tmp_path, AS_OF, next_day=NEXT, watch=syms, model=MODEL)["status"] == "exists"


def test_list_made_after_open_is_late_and_not_counted(tmp_path, monkeypatch):
    syms = _universe(tmp_path)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 11, 5, 10, 0, tzinfo=us_clock.tz))     # 名单日开盘后才生成
    lr.generate(tmp_path, AS_OF, next_day=NEXT, watch=syms, model=MODEL)
    assert lr.load_day(tmp_path, NEXT)["late"] is True
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 11, 4, 17, 0, tzinfo=us_clock.tz))     # 前一天收盘后: 正常
    lr.generate(tmp_path, AS_OF, next_day=NEXT, watch=syms, model=MODEL, force=True)
    assert "late" not in lr.load_day(tmp_path, NEXT)


def test_settle_view_excludes_late_lists(tmp_path):
    days = _bdays(date(2026, 11, 13), 7)
    up = [{"date": d, "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.0 + i, "volume": 1e6, "amount": 1e8, "raw_close": 100.0}
          for i, d in enumerate(days)]
    _write_daily(tmp_path, {"A": up, "B": up, "C": up, "D": up})
    _listed(tmp_path, date(2026, 11, 5), ["A", "B", "C", "D"])
    rec = lr.load_day(tmp_path, date(2026, 11, 5))
    lr._write_json(lr.day_path(tmp_path, date(2026, 11, 5)), {**rec, "late": True})
    assert lr.settle(tmp_path, date(2026, 11, 11))["settled"] == 1                       # 照常结算
    v = lr.settle_view(tmp_path)
    assert v["days"] == 0 and v["total"] is None and v["late"] == 1                       # 但不计入前向成绩


def test_generate_skips_when_too_few_symbols_and_never_trains(tmp_path, monkeypatch):
    syms = _universe(tmp_path, n=8, young=None)
    monkeypatch.setattr(lr, "train", lambda *a, **k: pytest.fail("不该训练"))
    assert lr.generate(tmp_path, AS_OF, next_day=NEXT, watch=syms)["reason"] == "too_few_symbols"
    assert not lr.day_path(tmp_path, NEXT).exists()


def test_generate_marks_new_entries_against_previous_list(tmp_path):
    syms = _universe(tmp_path)
    lr.generate(tmp_path, AS_OF, next_day=NEXT, watch=syms, model=MODEL)
    rec = lr.load_day(tmp_path, NEXT)
    prev = {**rec, "day": AS_OF.isoformat(), "long": [*rec["long"][:4], "S19"], "short": rec["short"]}
    lr.day_path(tmp_path, AS_OF).write_text(json.dumps(prev))
    lr.generate(tmp_path, AS_OF, next_day=NEXT, watch=syms, model=MODEL, force=True)
    rec2 = lr.load_day(tmp_path, NEXT)
    new = {r["symbol"] for r in rec2["rows"] if r["is_new"]}
    assert new == {rec["long"][4]} - {"S19"}
    assert all(r["prev_rank"] is not None for r in rec2["rows"])


# ---------- 训练 ----------

def test_train_writes_quarter_model_and_reuses_it(tmp_path, monkeypatch):
    days = _bdays(date(2026, 9, 30), 420)
    frames = {f"P{i:02d}": _bars(days, i, base=20 + i, vol=2e6 + i * 1e5) for i in range(30)}
    _write_daily(tmp_path, frames)
    monkeypatch.setattr(lr, "TRAIN_START", days[0] + timedelta(days=100))
    monkeypatch.setattr(lr, "MIN_TRAIN_ROWS", 500)
    monkeypatch.setattr(lr, "GBM", {**lr.GBM, "max_iter": 20, "min_samples_leaf": 20})
    m = lr.train(tmp_path, NEXT, watch=["P00", "P01"], excluded=set())
    assert m["meta"]["quarter"] == "2026Q4" and m["meta"]["trained_to"] < lr.train_end(NEXT).isoformat()
    assert lr.model_path(tmp_path, "2026Q4").exists()
    monkeypatch.setattr(lr, "load_daily", lambda *a, **k: pytest.fail("已有本季度模型, 不该再读数据"))
    assert lr.train(tmp_path, date(2026, 12, 1), watch=["P00"])["meta"]["trained_at"] == m["meta"]["trained_at"]


def test_train_end_is_eight_days_before_quarter():
    assert lr.train_end(date(2026, 11, 5)) == date(2026, 9, 23)
    assert lr.train_end(date(2027, 1, 4)) == date(2026, 12, 24)
    assert lr.quarter_of(date(2026, 12, 31)) == "2026Q4"


def test_pool_is_point_in_time_top_liquidity_plus_watchlist():
    days = _bdays(date(2026, 6, 30), 120)
    rows = []
    for i, s in enumerate(["BIG", "MID", "TINY", "ETFX", "WL"]):
        for r in _bars(days, i, base=50, vol=[9e6, 5e6, 1e3, 8e6, 1e3][i]):
            rows.append({"symbol": s, **r})
    narrow = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    import app.services.ls_rank as m
    old = (m.POOL_TOP, m.TRAIN_START)
    try:
        m.POOL_TOP, m.TRAIN_START = 2, days[70]
        mem = m.pool_members(narrow, {"WL"}, {"ETFX"})
    finally:
        m.POOL_TOP, m.TRAIN_START = old
    got = set(mem.filter(pl.col("date") == days[-1])["symbol"].to_list())
    assert got == {"BIG", "MID", "WL"}                                                    # ETF 排除, 小票不进, 自选总在


# ---------- 结算 ----------

def _listed(tmp_path: Path, day: date, syms: list[str], atr: float = 2.0) -> None:
    rows = [{"symbol": s, "rank": i + 1, "pct": 50.0, "side": None, "atr": atr, "close": 100.0} for i, s in enumerate(syms)]
    rec = {"contract": lr.CONTRACT, "day": day.isoformat(), "as_of": (day - timedelta(days=1)).isoformat(),
           "model": {"quarter": "2026Q4", "trained_to": "2026-09-22"}, "long": syms[:1], "short": syms[1:2],
           "random": {"long": syms[2:3], "short": syms[3:4]}, "rows": rows}
    lr._write_json(lr.day_path(tmp_path, day), rec)


def test_settle_after_five_bars_is_idempotent_and_series_adds_up(tmp_path):
    days = _bdays(date(2026, 11, 13), 7)                 # 11-05 起 5 根 + 余量
    d0 = date(2026, 11, 5)
    up = [{"date": d, "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.0 + i, "volume": 1e6, "amount": 1e8, "raw_close": 100.0}
          for i, d in enumerate(days)]
    down = [{**r, "close": 100.0 - (r["close"] - 100.0) * 0.5} for r in up]
    _write_daily(tmp_path, {"A": up, "B": down, "C": up, "D": down})
    _listed(tmp_path, d0, ["A", "B", "C", "D"])
    assert lr.settle(tmp_path, date(2026, 11, 10))["settled"] == 0                       # 只有 4 根: 不结
    assert lr.settle(tmp_path, date(2026, 11, 11))["settled"] == 1
    rec = lr.load_day(tmp_path, d0)
    r = rec["settled"]["r"]
    assert r["A"]["long"] == pytest.approx((104 - 100) / 2 - 0.001 * 100 / 2)
    assert r["B"]["short"] == pytest.approx((100 - 98) / 2 - 0.001 * 100 / 2)
    assert lr.settle(tmp_path, date(2026, 11, 12))["settled"] == 0                       # 幂等
    s = lr.series([rec])
    assert s["pair"] == pytest.approx(round(s["long"] + s["short"], 3))
    assert s["long_minus_all"] == pytest.approx(round(s["long"] - s["all"], 3))
    v = lr.settle_view(tmp_path)
    assert v["days"] == 1 and v["last"]["made"] == d0.isoformat() and v["last"]["long"][0]["symbol"] == "A"


# ---------- 页面投影 ----------

def _nav(tmp_path: Path, nav: float = 100_000.0) -> None:
    p = tmp_path / "ledger" / "nav" / "daily.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": "2026-11-04", "nav": nav}) + "\n")


def _ruleset(tmp_path: Path, risk: float = 0.5) -> None:
    from app.services import watchlist_v4 as wv
    wv.save_ruleset(tmp_path, [], {"risk_per_trade_pct": risk}, "t")


def test_view_premarket_reference_stop_and_qty(tmp_path, monkeypatch):
    syms = _universe(tmp_path)
    lr.generate(tmp_path, AS_OF, next_day=NEXT, watch=syms, model=MODEL)
    _nav(tmp_path)
    _ruleset(tmp_path)
    rec = lr.load_day(tmp_path, NEXT)
    s = rec["long"][0]
    atr = next(r["atr"] for r in rec["rows"] if r["symbol"] == s)
    close = next(r["close"] for r in rec["rows"] if r["symbol"] == s)
    pg = tmp_path / "premarket_gap"
    pg.mkdir()
    (pg / f"{NEXT.isoformat()}.json").write_text(json.dumps({"day": NEXT.isoformat(), "at": "2026-11-05T09:00:00-05:00",
                                                           "gaps": {s: -2.5}, "mids": {s: close * 0.975}}))
    now = datetime(2026, 11, 5, 9, 10, tzinfo=us_clock.tz)
    v = lr.view(tmp_path, now=now)
    row = next(r for r in v["long"] if r["symbol"] == s)
    assert row["ref"] == round(close * 0.975, 2) and row["pre_pct"] == -2.5
    assert row["stop"] == round(close * 0.975 - 2 * atr, 2)
    assert row["qty"] == math.floor(500.0 / (2 * atr))                                    # 100,000 × 0.5% = $500
    assert v["risk_usd"] == 500.0 and v["is_today"] and v["pre"]["at"].startswith("2026-11-05T09:00")
    assert len(v["long"]) == 5 and len(v["short"]) == 5 and len(v["all"]) == len(rec["rows"])
    assert v["hold_until"] == "2026-11-11" and v["settle"]["days"] == 0


def test_view_after_close_shows_next_day_list_without_live_prices(tmp_path):
    syms = _universe(tmp_path)
    lr.generate(tmp_path, AS_OF, next_day=NEXT, watch=syms, model=MODEL)
    v = lr.view(tmp_path, now=datetime(2026, 11, 4, 17, 0, tzinfo=us_clock.tz))
    assert v["day"] == NEXT.isoformat() and not v["is_today"] and v["pre"] is None
    row = v["long"][0]
    assert row["ref"] == round(row["close"], 2) and row["pre_pct"] is None
    assert row["qty"] is None                                                             # 没有净值 / 规则集: 不给股数


def test_view_none_without_any_list(tmp_path):
    assert lr.view(tmp_path, now=datetime(2026, 11, 5, 8, 0, tzinfo=us_clock.tz)) is None


def test_view_is_read_only(tmp_path):
    import hashlib
    syms = _universe(tmp_path)
    lr.generate(tmp_path, AS_OF, next_day=NEXT, watch=syms, model=MODEL)

    def tree():
        h = hashlib.sha256()
        for p in sorted(tmp_path.rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(tmp_path)).encode())
                h.update(p.read_bytes())
        return h.hexdigest()
    before = tree()
    for _ in range(2):
        lr.view(tmp_path, now=datetime(2026, 11, 5, 11, 0, tzinfo=us_clock.tz))
    assert tree() == before


def test_page_includes_ls_rank_section(tmp_path, monkeypatch):
    from app.services import opp_page
    syms = _universe(tmp_path)
    lr.generate(tmp_path, AS_OF, next_day=NEXT, watch=syms, model=MODEL)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 11, 4, 18, 0, tzinfo=us_clock.tz))
    from app.services import intraday_strength, market_mood
    from app.services import swing_desk as sd
    monkeypatch.setattr(sd, "build_live", lambda data_dir, repo=None, loop=None, today=None: None)
    monkeypatch.setattr(intraday_strength, "build_live", lambda data_dir, repo=None, loop=None, now=None: None)
    monkeypatch.setattr(market_mood, "view", lambda data_dir, now=None: {"judgment": None})
    page = opp_page.build(tmp_path, repo=SimpleNamespace(get_daily_batch=lambda *a, **k: pl.DataFrame()))
    assert page["ls_rank"]["day"] == NEXT.isoformat() and "ls_rank" not in page["errors"]


# ---------- 边界 ----------

def test_not_in_trading_chain():
    """今日多空不被交易环 / 准入 / 下单簿 / 裁决 / 推送引用, 自己也不碰台账写口与规则保存口。"""
    root = Path(lr.__file__).parent
    for name in ("trading_loop.py", "entry_guard.py", "watchlist_book.py", "swing_verdict.py", "notify_opportunity.py",
                 "premarket_gap.py", "pm_rank.py"):
        p = root / name
        if p.exists():
            assert "ls_rank" not in p.read_text(encoding="utf-8"), name
    src = Path(lr.__file__).read_text(encoding="utf-8")
    for bad in ("trading_loop import", "ledger.core", "save_ruleset", "save_strategies", "_enqueue", "place_order"):
        assert bad not in src, bad


def test_account_reset_keeps_ls_rank_records():
    from app.services import account_reset
    assert not any(x.startswith("opportunities/ls_rank") for x in account_reset.ARCHIVE_ITEMS)


def test_scheduler_registers_ls_rank_backup_job(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    from app.services import ops_watch as ow
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    j = sched.get_job("zen_ls_rank")
    assert j is not None
    t0 = datetime(2026, 11, 2, 0, 0, tzinfo=us_clock.tz)
    f1 = j.trigger.get_next_fire_time(None, t0).astimezone(us_clock.tz)
    f2 = j.trigger.get_next_fire_time(f1, f1 + timedelta(seconds=1)).astimezone(us_clock.tz)
    assert (f1.strftime("%H:%M"), f2.strftime("%H:%M")) == ("08:20", "17:20")
    assert "zen_ls_rank" in ow.WATCHED and "zen_ls_rank" in ow.CATCHUP
    src = Path(dp.__file__).read_text(encoding="utf-8")
    assert "ls_rank.run_post_close(repo.store.data_dir, day)" in src                       # 16:35 盘后管道出名单


def test_ensure_uses_latest_settled_day(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(lr, "run_post_close", lambda data_dir, day: calls.append(day) or {"ok": 1})
    assert lr.ensure(tmp_path, SimpleNamespace(latest_settled_daily_date=lambda: AS_OF)) == {"as_of": "2026-11-04", "ok": 1}
    assert calls == [AS_OF]
    assert lr.ensure(tmp_path, SimpleNamespace(latest_settled_daily_date=lambda: None))["status"] == "skipped"


def test_bad_record_reads_as_none(tmp_path):
    p = lr.day_path(tmp_path, NEXT)
    p.parent.mkdir(parents=True)
    p.write_text("{nope")
    assert lr.load_day(tmp_path, NEXT) is None
    p.write_text(json.dumps({"day": "2026-11-04", "rows": []}))
    assert lr.load_day(tmp_path, NEXT) is None                                           # 日期对不上 = 坏记录
