# ruff: noqa: RUF002, RUF003
"""0.52.1 机会页审查修复回归 (11-06 对抗审查 11 条)。

1/2 拆股口径: 今日多空存原始价; 页面按名单日后的拆股换到今天原始价; 结算按名单日原始价口径;
    整页 "对昨收涨跌" 的昨收取原始收盘并按除权比例换算 (盘前跳空记录 / 跳空榜 / 大盘宽度 / 共振影子回填)。
3   停牌记缺: 持有期走完仍缺上榜票 K 线 → 照常结算其余, 缺的票记 missing。
4   盘前判断记分: 18:00 按定版收盘补记, 收盘价不同则覆盖 16:10 的盘中快照记分。
6   开盘后 "盘前" 列换成开盘价对昨收 (与参考价同源)。
7   股数名义上限 = 权益 25%。
8   日历拉不到不出名单 (不按工作日近似写休市日)。
9/10 半日市: 隔夜基准 = 前一交易日实际收盘; 名额释放时刻只在收盘前给。
11  盘前跳空记录原子写, 最新一份坏了退回上一份。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import ls_rank as lr

ET = us_clock.tz
pytestmark = pytest.mark.real_corporate_actions          # 用测试自己写的公司行动缓存 (conftest 默认桩成空)


@pytest.fixture(autouse=True)
def _cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    lr._SPARK.clear()
    lr._SETTLE_CACHE.clear()
    from app.services import opp_page
    opp_page._PC_CACHE.clear()


def _actions(tmp_path: Path, *acts: dict) -> None:
    d = tmp_path / "reference" / "corporate_actions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "actions.json").write_text(json.dumps({"fetched_at": "2026-11-05T08:00:00-05:00", "symbols": ["SNDK"],
                                                "window": {"start": "2026-10-26", "end": "2026-12-05"}, "actions": list(acts)}))


SPLIT = {"id": "x", "type": "forward_split", "symbol": "SNDK", "ex_date": "2026-11-20", "factor": 8.0, "auto": True}


# ---------- 拆股比例 ----------

def test_split_factor_window_is_after_exclusive_through_inclusive():
    from app.services.corporate_actions import split_factor
    acts = [SPLIT, {**SPLIT, "id": "y", "type": "reverse_split", "ex_date": "2026-12-01", "factor": 0.5},
            {**SPLIT, "id": "z", "type": "cash_dividend", "factor": 3.0}, {**SPLIT, "id": "w", "symbol": "MU"}]
    assert split_factor(acts, "SNDK", date(2026, 11, 19), date(2026, 11, 20)) == 8.0
    assert split_factor(acts, "SNDK", date(2026, 11, 20), date(2026, 11, 25)) == 1.0       # 除权日当天收盘之后 = 已是拆后
    assert split_factor(acts, "SNDK", date(2026, 11, 19), date(2026, 12, 1)) == 4.0
    assert split_factor(acts, "SNDK", date(2026, 11, 19), date(2026, 11, 19)) == 1.0


# ---------- 1. 今日多空: 原始价口径 ----------

def _bars(days, base, raw_k=1.0):
    rows = []
    for i, d in enumerate(days):
        c = base * (1 + 0.01 * ((i % 5) - 2))
        rows.append({"date": d, "open": c * 0.995, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6,
                     "amount": 1e6 * c, "raw_close": c * raw_k})
    return rows


def _bdays(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def test_feature_rows_store_raw_close_and_raw_atr():
    days = _bdays(date(2026, 11, 4), 80)
    adj = pl.DataFrame([{"symbol": "X", **r} for r in _bars(days, 100.0, raw_k=8.0)]).with_columns(pl.col("date").cast(pl.Date))
    f = lr.feature_rows(adj, ["X"])["X"]
    g = lr.feature_rows(adj.drop("raw_close"), ["X"])["X"]
    assert f["close"] == pytest.approx(g["close"] * 8) and f["atr"] == pytest.approx(g["atr"] * 8)
    for k in lr.FEATURES:                                          # 特征 (比率) 不受口径影响
        assert f[k] == pytest.approx(g[k], nan_ok=True)


def _sndk_list(tmp_path: Path, day: date, as_of: date, close=800.0, atr=40.0) -> None:
    rows = [{"symbol": "SNDK", "rank": 1, "pct": 97.0, "side": "long", "young": False, "close": close, "atr": atr, "tags": [],
             "earnings": None, "prev_rank": 2, "is_new": False, "book": "long"},
            {"symbol": "MU", "rank": 2, "pct": 10.0, "side": "short", "young": False, "close": 200.0, "atr": 8.0, "tags": [],
             "earnings": None, "prev_rank": 2, "is_new": False, "book": "short"}]
    lr._write_json(lr.day_path(tmp_path, day), {"contract": lr.CONTRACT, "day": day.isoformat(), "as_of": as_of.isoformat(),
                                                "made_at": f"{as_of.isoformat()}T16:52:00-05:00",
                                                "model": {"quarter": "2026Q4", "trained_to": "2026-09-22"},
                                                "params": {"basis": "raw"}, "long": ["SNDK"], "short": ["MU"],
                                                "random": {"long": [], "short": []}, "rows": rows})
    p = tmp_path / "ledger" / "nav" / "daily.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": as_of.isoformat(), "nav": 100_000.0}) + "\n")
    from app.services import watchlist_v4 as wv
    wv.save_ruleset(tmp_path, [], {"risk_per_trade_pct": 0.5}, "t")


def test_view_on_ex_date_converts_list_prices_to_post_split(tmp_path, monkeypatch):
    """审查 C30c 场景 A: 名单 11-19 晚出 (拆前 800 / ATR 40), 11-20 除权 8 拆 1, 盘前 HL 101 (拆后)。"""
    _actions(tmp_path, SPLIT)
    day = date(2026, 11, 20)
    _sndk_list(tmp_path, day, date(2026, 11, 19))
    pg = tmp_path / "premarket_gap"
    pg.mkdir()
    (pg / f"{day}.json").write_text(json.dumps({"day": str(day), "at": f"{day}T09:00:00-05:00",
                                                "gaps": {"SNDK": 1.0}, "mids": {"SNDK": 101.0}}))
    now = datetime(2026, 11, 20, 9, 5, tzinfo=ET)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    v = lr.view(tmp_path, now=now)
    r = v["long"][0]
    assert r["close"] == 100.0 and r["atr"] == 5.0 and r["split"] == 8.0
    assert r["ref"] == 101.0 and r["stop"] == 91.0 and r["pre_pct"] == 1.0
    assert r["qty"] == 50                                               # $500 ÷ (2 × 5)
    mu = v["short"][0]
    assert mu["close"] == 200.0 and "split" not in mu                  # 没拆股的票不动


def test_view_before_ex_date_is_untouched(tmp_path):
    _actions(tmp_path, SPLIT)
    _sndk_list(tmp_path, date(2026, 11, 19), date(2026, 11, 18))
    v = lr.view(tmp_path, now=datetime(2026, 11, 18, 17, 0, tzinfo=ET))
    assert v["long"][0]["close"] == 800.0 and v["long"][0]["atr"] == 40.0


def _write_daily(data_dir: Path, rows: list[dict]) -> None:
    df = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    for c in ("raw_high", "raw_low", "turnover_rate"):
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    df = df.with_columns(pl.lit(None, dtype=pl.Int64).alias("quote_ts"))
    base = data_dir / "kline_daily_enriched"
    for (d,), g in df.group_by(["date"]):
        p = base / f"date={d.isoformat()}" / "part.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        g.drop("date").with_columns(pl.lit(d).alias("date")).write_parquet(p)


def test_settle_across_split_uses_list_day_raw_basis(tmp_path):
    """审查 C30c 场景 B: 名单 11-17 (as_of 11-16, 拆前 ATR 40), 11-20 除权; 结算时日K已整体回溯 /8。
    名单日原始价口径下: 入场 800, 第 4 天低点 760 未破 720 止损, 第 5 天收 768 → (768 − 800) / 40 = −0.8 ATR (− 成本)。"""
    days = [date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23)]
    raw = {days[0]: (800, 810, 790, 800), days[1]: (800, 812, 792, 804), days[2]: (804, 806, 790, 796),
           days[3]: (796, 800, 780, 790), days[4]: (98.0, 99.0, 95.0, 96.0), days[5]: (96.0, 97.0, 95.5, 96.0)}
    rows = []
    for d, (o, h, lo, c) in raw.items():
        k = 1.0 if d >= date(2026, 11, 20) else 1 / 8                  # 回溯复权: 拆前 /8, 原始价保留
        rows.append({"symbol": "SNDK", "date": d, "open": o * k, "high": h * k, "low": lo * k, "close": c * k,
                     "volume": 1e6, "amount": 1e8, "raw_close": float(c)})
        rows.append({"symbol": "MU", "date": d, "open": 200.0, "high": 201.0, "low": 199.0, "close": 200.0,
                     "volume": 1e6, "amount": 1e8, "raw_close": 200.0})
    _write_daily(tmp_path, rows)
    _sndk_list(tmp_path, date(2026, 11, 17), date(2026, 11, 16))
    assert lr.settle(tmp_path, date(2026, 11, 23))["settled"] == 1
    r = lr.load_day(tmp_path, date(2026, 11, 17))["settled"]["r"]["SNDK"]
    assert r["long_exit"] == "time"
    assert r["long"] == pytest.approx((96 * 8 - 800) / 40 - 0.001 * 800 / 40, abs=1e-4)


# ---------- 3. 停牌记缺 ----------

def test_settle_records_missing_listed_symbol_after_hold_ends(tmp_path):
    days = [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)]
    rows = []
    for d in days:
        rows.append({"symbol": "MU", "date": d, "open": 200.0, "high": 201.0, "low": 199.0, "close": 200.0,
                     "volume": 1e6, "amount": 1e8, "raw_close": 200.0})
        if d <= date(2026, 11, 6):                                     # SNDK 11-09 起停牌
            rows.append({"symbol": "SNDK", "date": d, "open": 800.0, "high": 801.0, "low": 799.0, "close": 800.0,
                         "volume": 1e6, "amount": 1e8, "raw_close": 800.0})
    _write_daily(tmp_path, rows)
    _sndk_list(tmp_path, date(2026, 11, 5), date(2026, 11, 4))
    assert lr.settle(tmp_path, date(2026, 11, 10))["settled"] == 0      # 持有期还没走完 (11-11 是第 5 天): 等
    assert lr.settle(tmp_path, date(2026, 11, 11))["settled"] == 1
    st = lr.load_day(tmp_path, date(2026, 11, 5))["settled"]
    assert st["missing"] == ["SNDK"] and set(st["r"]) == {"MU"} and st["end"] == "2026-11-11"
    v = lr.settle_view(tmp_path)
    assert v["last"]["missing"] == {"long": ["SNDK"], "short": []}
    assert v["last"]["long"] == [] and len(v["last"]["short"]) == 1
    assert lr.settle(tmp_path, date(2026, 11, 12))["settled"] == 0      # 幂等


def test_settle_waits_when_hold_end_bar_not_landed(tmp_path):
    """through 已过第 5 天但那天日K还没落盘 (其余票也没有) → 不判缺, 下次再试。"""
    days = [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10)]
    rows = [{"symbol": s, "date": d, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6, "amount": 1e8,
             "raw_close": 100.0} for d in days for s in ("MU", "SNDK")]
    _write_daily(tmp_path, rows)
    _sndk_list(tmp_path, date(2026, 11, 5), date(2026, 11, 4))
    assert lr.settle(tmp_path, date(2026, 11, 11))["settled"] == 0


# ---------- 6 / 7. 开盘后同源 + 名义上限 ----------

def test_after_open_change_column_is_open_vs_prev_close(tmp_path, monkeypatch):
    _sndk_list(tmp_path, date(2026, 11, 5), date(2026, 11, 4), close=100.0, atr=1.0)
    pg = tmp_path / "premarket_gap"
    pg.mkdir()
    (pg / "2026-11-05.json").write_text(json.dumps({"day": "2026-11-05", "at": "2026-11-05T09:00:00-05:00",
                                                    "gaps": {"SNDK": -5.0}, "mids": {"SNDK": 95.0}}))
    from app.services import swing_desk as sd
    monkeypatch.setattr(sd, "_today_bars", lambda repo, today, syms: {"SNDK": (103.0, 104.0, 102.0, 103.5)})
    v = lr.view(tmp_path, repo=object(), now=datetime(2026, 11, 5, 10, 0, tzinfo=ET))
    r = v["long"][0]
    assert r["ref"] == 103.0 and r["pre_pct"] == 3.0 and v["pre"]["open"] is True
    # 名义上限: $500 ÷ 2 = 250 股 × 103 = $25,750 > 权益 25% ($25,000) → 242 股
    assert r["qty"] == math.floor(100_000 * 0.25 / 103.0) == 242


# ---------- 8. 日历拉不到不出名单 ----------

def test_generate_skips_when_calendar_unavailable(tmp_path, monkeypatch):
    def boom(a, b):
        raise OSError("down")
    monkeypatch.setattr(us_clock, "_calendar_provider", boom)
    us_clock._cal_cache.clear()
    monkeypatch.setattr(lr, "train", lambda *a, **k: pytest.fail("不该训练"))
    assert lr.generate(tmp_path, date(2026, 11, 25), watch=["A"])["reason"] == "calendar_unavailable"
    assert lr.list_days(tmp_path) == []


# ---------- 4. 盘前判断补记分覆盖 ----------

class _Repo:
    def __init__(self, closes, settled=None):
        self.closes, self.settled = closes, settled

    def get_daily(self, symbol, start, end, columns=None):
        rows = [(d, c) for d, c in sorted(self.closes.items()) if start.isoformat() <= d <= end.isoformat()]
        return pl.DataFrame({"date": [r[0] for r in rows], "close": [r[1] for r in rows]})

    def latest_settled_daily_date(self):
        return self.settled


def test_mood_rescore_at_1800_overrides_intraday_snapshot(tmp_path):
    from app.services import market_mood as mm
    day = date(2026, 10, 27)
    jd = tmp_path / "market_mood" / "judgments"
    jd.mkdir(parents=True)
    (jd / f"{day}.json").write_text(json.dumps({"day": str(day), "main": "range", "stance": "range",
                                                "probs": {"up": 30, "down": 20, "range": 50}}))
    repo = _Repo({"2026-10-24": 100.0, "2026-10-27": 100.28})          # 16:10 盘中快照: +0.28% 震荡
    a = mm.score(tmp_path, repo, day=day)
    assert a["outcome"] == "range" and a["hit"] is True and a["final"] is False
    repo.closes["2026-10-27"], repo.settled = 100.31, day                # 18:00 官方收盘: +0.31% 上涨
    b = mm.score(tmp_path, repo, day=day)
    assert b["outcome"] == "up" and b["hit"] is False and b["final"] is True and b["rescored_from"] == 100.28
    assert mm.load_scores(tmp_path)[str(day)]["outcome"] == "up"        # 读时同日后写的生效
    assert mm.score(tmp_path, repo, day=day) == b                        # 定版后不再写
    assert len((tmp_path / "market_mood" / "scores.jsonl").read_text().splitlines()) == 2


def test_post_close_pipeline_rescores_mood():
    from app.jobs import daily_pipeline as dp
    src = Path(dp.__file__).read_text(encoding="utf-8")
    assert "market_mood.score(repo.store.data_dir, repo, day=day)" in src


def test_mood_rescore_same_close_marks_final_without_changing_outcome(tmp_path):
    from app.services import market_mood as mm
    day = date(2026, 10, 27)
    jd = tmp_path / "market_mood" / "judgments"
    jd.mkdir(parents=True)
    (jd / f"{day}.json").write_text(json.dumps({"day": str(day), "main": "up", "stance": "up", "probs": {"up": 50, "down": 20, "range": 30}}))
    repo = _Repo({"2026-10-24": 100.0, "2026-10-27": 101.0})
    a = mm.score(tmp_path, repo, day=day)
    assert mm.score(tmp_path, repo, day=day) == a                        # 没定版且价格没变: 不重复写
    repo.settled = day
    b = mm.score(tmp_path, repo, day=day)
    assert b["final"] is True and b["scored_at"] == a["scored_at"] and b["outcome"] == a["outcome"]


# ---------- 9. 半日市隔夜基准 ----------

def test_overnight_base_is_previous_session_close_on_half_day(monkeypatch):
    from app.services import market_mood as mm
    half = date(2026, 11, 27)
    monkeypatch.setattr(us_clock, "session_close", lambda d: time(13, 0) if d == half else time(16, 0))
    t13 = datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    ms = mm._ms
    candles = [{"t": ms(t13 - timedelta(minutes=5)), "T": ms(t13) - 1, "o": "99", "c": "100"},
               {"t": ms(t13), "T": ms(t13) + 299_999, "o": "100", "c": "100.5"},
               {"t": ms(t13 + timedelta(hours=3)), "T": ms(t13 + timedelta(hours=3)) + 299_999, "o": "102", "c": "102"},
               {"t": ms(datetime(2026, 11, 30, 8, 0, tzinfo=ET)), "T": ms(datetime(2026, 11, 30, 8, 5, tzinfo=ET)) - 1,
                "o": "102.5", "c": "102.5"}]
    monkeypatch.setattr(mm, "_post_json", lambda url, body: candles)
    out = mm.fetch_overnight(datetime(2026, 11, 30, 8, 30, tzinfo=ET))
    assert out["ndx"]["chg_pct"] == 2.5                                  # 13:00 基准 100 → 102.5


# ---------- 10. 半日市名额释放时刻 ----------

def test_release_at_none_after_half_day_close(monkeypatch):
    from app.services import opp_radar as orad
    half = date(2026, 11, 27)
    monkeypatch.setattr(us_clock, "session_close", lambda d: time(13, 0) if d == half else time(16, 0))
    monkeypatch.setattr(us_clock, "entry_close", lambda d: time(12, 30) if d == half else time(15, 30))
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_pipeline_schedule", lambda: {"hour": 16, "minute": 35})
    src = Path(orad.__file__).read_text(encoding="utf-8")
    assert 'now.time() < dtime(16, 0)' not in src
    for hm, want in (("12:00", "12:30"), ("14:00", None)):
        ck = orad.clock(datetime.combine(half, time.fromisoformat(hm), tzinfo=ET))
        rel = ck["entry_close"] if ck["trading_day"] and ck["close"] and hm < ck["close"] else None
        assert rel == want, hm


# ---------- 2. 整页对昨收: 除权日 ----------

def test_prev_close_uses_raw_and_split_on_ex_date(tmp_path):
    from app.services import opp_page
    _actions(tmp_path, SPLIT)
    rows = {"SNDK": (date(2026, 11, 19), 100.0, 800.0), "MU": (date(2026, 11, 19), 200.0, 200.0),
            "OLD": (date(2026, 11, 19), 50.0, None)}
    pc = opp_page.prev_close_today_basis(tmp_path, rows, date(2026, 11, 20))
    assert pc == {"SNDK": 100.0, "MU": 200.0, "OLD": 50.0}
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_daily_batch=lambda syms, a, b, columns=None: pl.DataFrame(
        {"symbol": ["SNDK", "SNDK"], "date": [date(2026, 11, 18), date(2026, 11, 19)], "close": [800.0, 800.0], "raw_close": [800.0, 800.0]}))
    assert opp_page._prev_closes(repo, ["SNDK"], date(2026, 11, 20)) == {"SNDK": 100.0}


def test_premarket_gap_prev_close_on_ex_date(tmp_path, monkeypatch):
    from app.services import premarket_gap
    _actions(tmp_path, SPLIT)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 11, 20, 9, 0, tzinfo=ET))
    monkeypatch.setattr("app.services.watchlist.list_symbols", lambda: [{"symbol": "SNDK"}])
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: SimpleNamespace(mids=lambda syms: {"SNDK": 101.0}))

    class Repo:
        def get_daily(self, s, a, b, columns=None):
            return pl.DataFrame({"date": [date(2026, 11, 19)], "close": [800.0], "raw_close": [800.0]})
    loop = SimpleNamespace(repo=Repo(), ledger=SimpleNamespace(open_positions=[]), execution_symbols=lambda: [])
    _, pc, _ = premarket_gap._inputs(tmp_path, loop)
    assert pc == {"SNDK": 100.0}
    assert premarket_gap.scan(scope={"SNDK": ["自选"]}, prev_close=pc, mids={"SNDK": 101.0})["gaps"] == {"SNDK": 1.0}


def test_shadow_settle_converts_split_back_to_record_day(tmp_path):
    from app.services import opp_desk as od
    _actions(tmp_path, SPLIT)
    now = datetime(2026, 11, 19, 13, 0, tzinfo=ET)
    od.record(tmp_path, now, [{"symbol": "SNDK", "side": "long", "n": 2, "engines": {"gap": {}, "open": {}}}], {"SNDK": 800.0})

    class Repo:
        def get_daily_batch(self, syms, start, end, columns=None):
            return pl.DataFrame({"symbol": ["SNDK"] * 3, "date": [date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23)],
                                 "close": [101.0, 102.0, 103.0], "raw_close": [808.0, 102.0, 103.0]})
    od.settle(tmp_path, Repo(), today=date(2026, 11, 23))           # 第 1 / 3 个交易日 = 11-19 / 11-23 (跨除权日 11-20)
    r = od.today_rows(tmp_path, now.date())["SNDK"]
    assert r["r1"] == 1.0 and r["r3"] == pytest.approx((103 * 8 / 800 - 1) * 100, abs=1e-3)


# ---------- 11. 跳空记录原子写 + 坏文件退回 ----------

def test_premarket_gap_latest_falls_back_to_previous_good_file(tmp_path):
    from app.services import premarket_gap
    d = tmp_path / "premarket_gap"
    d.mkdir()
    (d / "2026-11-05.json").write_text(json.dumps({"day": "2026-11-05", "gaps": {}}))
    (d / "2026-11-06.json").write_text('{"day": "2026-11-06", "gaps": {"A"')
    assert premarket_gap.latest(tmp_path)["day"] == "2026-11-05"
    src = Path(premarket_gap.__file__).read_text(encoding="utf-8")
    assert "atomic_write_text(out /" in src and '.json").write_text(' not in src
