# ruff: noqa: RUF001
"""0.26.0 第①批 (10-18 产品决策): 直接修、不改交易规则。

1. 移出自选的卡不再从板和“已结束”里一起消失 -> 进“已结束”, 原因 = 已移出自选。
2. 休市时卡上“现价”= 最近一个已收盘交易日的收盘价 (不是信号日收盘)。
3. 持仓页胜率按口径分开: 机会板卡 / 旧规则卡。
4. 准入漏斗只统计机会板的卡 (旧决策台观察卡永远不下单, 不进漏斗)。
5. 规则面板“回测 N 笔”按现在这张卡 (tight-v1) 重算; 排序权重不动 (截断保持现状)。
"""
import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from test_swing_book_tight import SIG_DAY, _repo, _row, _setup, _snapshot

from app.market_clock import us_clock
from app.services import entry_funnel as ef
from app.services import swing_desk as sd
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    ef._SEEN.clear()


def _ledger_stub(monkeypatch, positions=(), events=()):
    lg = SimpleNamespace(open_positions=list(positions), read_events=lambda: list(events))
    from app.services import desk_overview as ov
    monkeypatch.setattr(ov, "_account", lambda loop, data_dir: (lg, None))


# ---------- 1. 移出自选的卡进“已结束” ----------

def test_removed_from_watchlist_card_goes_to_history(tmp_path, monkeypatch, cal):
    """10-15 的 ABBV: 落簿后被移出自选, 次日重扫的信号表不再含它 -> 旧实现板和历史都没有它。"""
    _setup(tmp_path, monkeypatch)
    rows = [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA"), _row("macd_golden", "ABBV"), _row("phantom_4h", "ABBV")]
    _snapshot(tmp_path, SIG_DAY, rows, {"AAA", "ABBV"}, {"AAA": 100.0, "ABBV": 50.0}, {"AAA": 2.0, "ABBV": 1.0})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"AAA", "ABBV"},
                         pm=wv.permission(tmp_path))
    assert wb.save_day(tmp_path, built) == 2
    # 用户移出 ABBV 后盘前重扫: 同一信号日的信号表只剩 AAA (生产 10-16 09:17 ET 就是这样)
    _snapshot(tmp_path, SIG_DAY, rows[:2], {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})
    from app.services import decision_scan_job as job
    monkeypatch.setattr(job, "_watchlist", lambda: {"AAA"})
    _ledger_stub(monkeypatch)
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: {})
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 9, 9, 40, tzinfo=us_clock.tz))
    out = sd.build_live(tmp_path, repo=object(), today=date(2026, 10, 9))
    assert [c["symbol"] for c in out["long"]] == ["AAA"]
    hist = {c["symbol"]: c for c in out["history"]}
    assert "ABBV" in hist, "落簿后移出自选的卡必须进已结束, 不能凭空消失"
    h = hist["ABBV"]
    assert h["state"] == "void" and h["void_reason"] == "not_in_watchlist"
    assert h["entry_zone"] == [49.5, 49.75] and h["card_id"] == wb.card_id("ABBV", "swing", "long", SIG_DAY)


def test_book_card_still_in_watchlist_stays_pending(tmp_path, monkeypatch, cal):
    """簿里有、重扫后的板上没有、但票仍在自选的卡: 交易环照样可能成交 -> 照常上板 (板 = 簿)。"""
    _setup(tmp_path, monkeypatch)
    rows = [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA")]
    _snapshot(tmp_path, SIG_DAY, rows, {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"AAA"}, pm=wv.permission(tmp_path))
    wb.save_day(tmp_path, built)
    _snapshot(tmp_path, SIG_DAY, [], {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})          # 重扫后规则不再命中
    from app.services import decision_scan_job as job
    monkeypatch.setattr(job, "_watchlist", lambda: {"AAA"})
    _ledger_stub(monkeypatch)
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: {})
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 9, 9, 40, tzinfo=us_clock.tz))
    out = sd.build_live(tmp_path, repo=object(), today=date(2026, 10, 9))
    assert [(c["symbol"], c["state"]) for c in out["long"]] == [("AAA", "pending")]
    c = out["long"][0]
    assert c["entry_zone"] == [99.0, 99.5] and c["stop"] == 97.5 and c["target"] == 103.5   # 簿里冻结的价位 (区上沿 -/+ 1/2 ATR)
    assert c["stop_pct"] < 0 < c["target_pct"]
    assert out["history"] == []


def test_removed_card_with_position_stays_on_board(tmp_path, monkeypatch, cal):
    """已成交的卡即使移出自选也照常显示为持仓 (持仓管理不受自选影响)。"""
    _setup(tmp_path, monkeypatch)
    rows = [_row("macd_golden", "ABBV"), _row("phantom_4h", "ABBV")]
    _snapshot(tmp_path, SIG_DAY, rows, {"ABBV"}, {"ABBV": 50.0}, {"ABBV": 1.0})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"ABBV"}, pm=wv.permission(tmp_path))
    wb.save_day(tmp_path, built)
    _snapshot(tmp_path, SIG_DAY, [], set(), {}, {})
    cid = wb.card_id("ABBV", "swing", "long", SIG_DAY)
    pos = SimpleNamespace(symbol="ABBV", card_id=cid, avg_cost=49.6, qty=10, entry_date=date(2026, 10, 9))
    _ledger_stub(monkeypatch, positions=[pos])
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: {})
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 9, 11, 0, tzinfo=us_clock.tz))
    out = sd.build_live(tmp_path, repo=object(), today=date(2026, 10, 9))
    assert [(c["symbol"], c["state"]) for c in out["long"]] == [("ABBV", "holding")]
    assert not any(c["symbol"] == "ABBV" for c in out["history"])


def test_removed_holding_card_flagged_watch_removed(tmp_path, monkeypatch, cal):
    """0.33.0: 持仓票已不在自选 -> 卡带 watch_removed (前端提示次日开盘平仓); 在自选 / 读不到自选 -> 不标。"""
    _setup(tmp_path, monkeypatch)
    rows = [_row("macd_golden", "ABBV"), _row("phantom_4h", "ABBV")]
    _snapshot(tmp_path, SIG_DAY, rows, {"ABBV"}, {"ABBV": 50.0}, {"ABBV": 1.0})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"ABBV"}, pm=wv.permission(tmp_path))
    wb.save_day(tmp_path, built)
    cid = wb.card_id("ABBV", "swing", "long", SIG_DAY)
    pos = SimpleNamespace(symbol="ABBV", card_id=cid, avg_cost=49.6, qty=10, entry_date=date(2026, 10, 9))
    _ledger_stub(monkeypatch, positions=[pos])
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: {})
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 9, 11, 0, tzinfo=us_clock.tz))
    for watch, flagged in (({"NVDA"}, True), ({"ABBV"}, False), (set(), False)):
        monkeypatch.setattr(sd, "_watchlist_now", lambda w=watch: set(w))
        out = sd.build_live(tmp_path, repo=object(), today=date(2026, 10, 9))
        c = next(x for x in out["long"] if x["symbol"] == "ABBV")
        assert bool(c.get("watch_removed")) is flagged, watch


# ---------- 2. 休市时现价 = 最近收盘 ----------

def test_closed_market_price_is_latest_close_not_signal_close(tmp_path, monkeypatch, cal):
    """10-17 周六: AAPL 卡 (信号日 10-15, 收盘 342.00) 以前显示 342.00; 最新收盘 10-16 是 343.46。"""
    _setup(tmp_path, monkeypatch)
    _snapshot(tmp_path, SIG_DAY, [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA")], {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})
    _ledger_stub(monkeypatch)
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    import app.api.portfolio as pf
    monkeypatch.setattr(pf, "_live_marks", lambda syms: {})
    bars = pl.DataFrame({"symbol": ["AAA", "AAA"], "date": [SIG_DAY, date(2026, 10, 9)], "close": [100.0, 101.3]})
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: bars.filter(
        pl.col("symbol").is_in(syms) & (pl.col("date") >= s) & (pl.col("date") <= e)))
    sat = datetime(2026, 10, 10, 11, 0, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: sat)
    c = sd.build_live(tmp_path, repo=repo, today=date(2026, 10, 10))["long"][0]
    assert c["price"] == 101.3 and c["price_source"] == "close"
    assert c["price_asof"] == "2026-10-09"
    # 挂单区 [99.0, 99.5] (收盘 100 回踩 0.25~0.5 ATR=2): 距区按 101.3 算
    assert c["dist_pct"] == round((99.5 / 101.3 - 1) * 100, 2)


def test_closed_market_price_falls_back_to_signal_close_without_bars(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    _snapshot(tmp_path, SIG_DAY, [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA")], {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})
    _ledger_stub(monkeypatch)
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    import app.api.portfolio as pf
    monkeypatch.setattr(pf, "_live_marks", lambda syms: {})
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 10, 11, 0, tzinfo=us_clock.tz))
    c = sd.build_live(tmp_path, repo=object(), today=date(2026, 10, 10))["long"][0]   # repo 无日K接口
    assert c["price"] == 100.0 and c["price_source"] == "close" and c.get("price_asof") == SIG_DAY.isoformat()


# ---------- 3. 持仓页胜率分口径 ----------

def test_portfolio_summary_splits_board_and_legacy(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.portfolio import router
    from app.ledger.core import Account, Ledger
    now = datetime(2026, 10, 20, 11, 0, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    monkeypatch.setattr(us_clock, "today", lambda: now.date())
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    led = Ledger(tmp_path / "ledger")
    led.init_account(Account())
    rounds = [("AAA", "opp-2026-10-08-AAA-book-tv_ut_bot-1", 12.0), ("BBB", "opp-2026-10-08-BBB-book-phantom_4h-1", 9.5),
              ("CCC", wb.card_id("CCC", "swing", "long", date(2026, 10, 15)), 10.8)]
    for i, (sym, cid, px) in enumerate(rounds):
        at = f"2026-10-1{4 + i}T10:00:00-04:00"
        led.append("fill", {"symbol": sym, "side": "buy", "qty": 100, "price": 10.0, "fee": 1.0, "trade_date": at[:10],
                            "card_id": cid, "setup_type": "strategy:x", "stop_price": 9.0, "invalidation": 9.0,
                            "target_zone": [12.0, 12.0], "risk_per_share": 1.0, "max_holding_days": 5, "quote_ts": at}, at=at)
        led.append("close", {"symbol": sym, "qty": 100, "price": px, "fee": 1.0, "reason": "time_stop", "card_id": cid,
                             "trade_date": at[:10]}, at=at.replace("10:00", "15:00"))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_daily=lambda *a, **k: pl.DataFrame())
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {})
    sm = TestClient(app).get("/api/portfolio/live").json()["summary"]
    assert sm["closed_trades"] == 3                                     # 合计口径不变
    by = {g["key"]: g for g in sm["by_scope"]}
    assert by["swing"]["closed_trades"] == 1 and by["swing"]["win_rate"] == 1.0
    assert by["legacy"]["closed_trades"] == 2 and by["legacy"]["win_rate"] == 0.5
    assert by["swing"]["label"] == "机会板" and by["legacy"]["label"] == "旧规则"


# ---------- 4. 准入漏斗只统计机会板卡 ----------

def _ev(tmp_path, rows):
    d = tmp_path / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_funnel_counts_only_board_cards(tmp_path, cal):
    day = date(2026, 10, 16)
    board_id = wb.card_id("AAPL", "swing", "long", date(2026, 10, 15))
    gone_id = wb.card_id("ABBV", "swing", "long", date(2026, 10, 15))
    _ev(tmp_path, [
        {"kind": "opportunity", "state": "active", "card_id": board_id, "symbol": "AAPL", "trade_date": "2026-10-16"},
        {"kind": "opportunity", "state": "watch", "card_id": gone_id, "symbol": "ABBV", "trade_date": "2026-10-16",
         "reason": "准入阻断：not_in_watchlist"},
        {"kind": "opportunity", "state": "active", "card_id": "opp-2026-10-15-NVDA-desk-1", "symbol": "NVDA", "trade_date": "2026-10-16"},
        {"kind": "opportunity", "state": "expired", "card_id": "opp-2026-10-15-MU-desk-1", "symbol": "MU", "trade_date": "2026-10-16"},
    ])
    now = datetime(2026, 10, 16, 9, 30, 11, tzinfo=us_clock.tz)
    # 交易环一轮里的拦截: 旧决策台卡 (v4_book_only 等) 与机会板卡混在一起, 落盘时带上 card_id
    ef.record_blockers(tmp_path, now, [
        {"symbol": "NVDA", "reason": "v4_book_only", "card_id": "opp-2026-10-15-NVDA-desk-1"},
        {"symbol": "NVDA", "reason": "source_not_tradable", "card_id": "opp-2026-10-15-NVDA-desk-1"},
        {"symbol": "ABBV", "reason": "not_in_watchlist", "card_id": gone_id},
        {"symbol": "AMD", "reason": "regular_open_unavailable"},
        {"symbol": "*", "reason": "circuit_breaker_day_halt"},
    ])
    d = ef.summarize(tmp_path, days=1, end=day)["days"][0]
    assert d["active"] == 1                                             # 只数机会板卡
    assert {r["reason"]: r["n"] for r in d["reasons"]} == {"not_in_watchlist": 1}
    assert d["blocked"] == 1
    assert [g["reason"] for g in d["global"]] == ["circuit_breaker_day_halt"]      # 全局拦截照旧单列
    assert d["scope"] == "swing"


def test_funnel_legacy_rows_without_card_id_are_dropped(tmp_path, cal):
    """0.26 之前落盘的行没有 card_id: 无法判断是不是机会板卡, 不计入 (旧数据 3967 行全是这类)。"""
    day = date(2026, 10, 16)
    p = tmp_path / "funnel"
    p.mkdir(parents=True)
    (p / "2026-10-16.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"symbol": "NVDA", "reason": "v4_book_only", "at": "2026-10-16T09:30:11-04:00"},
        {"symbol": "*", "reason": "earnings_calendar_unavailable", "at": "2026-10-16T09:30:11-04:00"}]), encoding="utf-8")
    _ev(tmp_path, [])
    d = ef.summarize(tmp_path, days=1, end=day)["days"][0]
    assert d["blocked"] == 0 and d["reasons"] == []
    assert [g["reason"] for g in d["global"]] == ["earnings_calendar_unavailable"]


def test_trading_loop_blockers_carry_card_id():
    """交易环/生命周期写 entry_blockers 的每一处逐卡拦截都要带 card_id (漏斗靠它区分机会板卡)。"""
    import inspect

    from app.services import opportunity_service, trading_loop
    src = inspect.getsource(trading_loop.TradingLoop.intraday_fill_check) + inspect.getsource(
        opportunity_service.OpportunityService.advance_lifecycle)
    import re
    lines = [ln for ln in src.splitlines() if re.search(r"entry_blockers\.(append|extend)\(\{'symbol': (card|c)\.symbol", ln)]
    assert len(lines) >= 7, lines
    assert all("'card_id'" in ln for ln in lines), [ln.strip() for ln in lines if "'card_id'" not in ln]


# ---------- 5. 规则面板历史 = tight-v1 ----------

@pytest.mark.full_edition
def test_rule_history_is_tight_card():
    from app.services import swing_desk as _sd
    h = _sd.rule_history()
    assert h.get("contract") == "swing-desk-tight-v1"
    rules = h.get("rules") or {}
    for r in ("tv_smc_structure", "volume_price_surge", "ibs_mean_reversion", "tv_smc_structure:short"):
        assert r in rules, r
        x = rules[r]
        assert {"n", "fill", "mean_r", "excess", "p"} <= set(x)
        assert x["n"] > 0 and 0 < x["fill"] <= 1


def test_rules_api_history_uses_tight_history(tmp_path, monkeypatch):
    """GET /api/opportunities/rules 的 catalog.history 来自 tight-v1 历史, 不再是旧宽卡 watchlist_rule_history。"""
    from app.api import opportunities as api_opp
    tight = {"contract": "swing-desk-tight-v1", "rules": {"macd_golden": {"n": 7, "fill": 0.5, "mean_r": 0.1, "excess": 0.02, "p": 0.4},
                                                          "tv_halftrend:short": {"n": 3, "fill": 0.6, "mean_r": -0.1, "excess": -0.05, "p": 0.7}}}
    monkeypatch.setattr(sd, "rule_history", lambda: tight)
    hist = api_opp._rule_history_for_panel()
    assert hist["macd_golden"]["n"] == 7 and hist["tv_halftrend:short"]["n"] == 3


def test_swing_sort_weights_unchanged():
    """截断保持现状 (10-18 产品决策第 7 条): 同分排序权重一个字都不动。"""
    assert sd.EXCESS_WEIGHT == {"tv_smc_structure": 0.40, "volume_price_surge": 0.25, "macd_golden": 0.19,
                                "tv_chandelier_exit": 0.06, "tv_halftrend": 0.06}
    assert sd.MAX_PER_SIDE == 5


# ---------- 第②批: 每笔 0.5%、最多 12 仓; 单日 / 同板块上限按金额折算 (10-18 产品决策 A) ----------

def test_book_caps_scale_by_amount():
    from app.services import entry_guard as eg
    assert eg.book_caps(0.01) == (5, 2)            # 10-12 原值 (每笔 1%)
    assert eg.book_caps(0.005) == (10, 4)          # 10-18: 每笔 0.5%
    assert eg.book_caps(0.0025) == (20, 8)
    assert eg.book_caps(None) == (5, 2) and eg.book_caps(0.0) == (5, 2)


def test_position_cap_half_percent_is_12_and_6_when_yellow():
    from app.ledger.core import Account
    from app.services import entry_guard as eg
    adm = eg.Admission((), 0.005, False, tier="rule", book=True, evo=True, segregated_rule="swing")
    lg = SimpleNamespace(open_positions=[])
    assert eg.position_cap(Account(), adm, 0.06, lg) == 12          # 6% / 0.5%
    assert eg.position_cap(Account(), adm, 0.03, lg) == 6           # SPY < 200 日线: 3% / 0.5%


def test_book_admission_half_percent_allows_tenth_open_and_fourth_sector(tmp_path, monkeypatch):
    """规则集单笔风险 0.5%: 当天第 10 笔可开、第 11 笔被拦; 同板块同方向第 4 仓可开、第 5 仓被拦。"""
    from test_watchlist_book import SIG_DAY as WB_DAY
    from test_watchlist_book import _book_card, _ledger, _mode, _open

    from app.services import decision_scan_job as job
    from app.services import entry_guard as eg
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 0.5})
    wv._PM_CACHE.clear()
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA", "MU"})
    now = datetime(2026, 10, 9, 10, 30, tzinfo=us_clock.tz)
    lg = _ledger(tmp_path)
    for i in range(9):
        _open(lg, f"S{i}", "buy")
    card = _book_card()
    a = eg.evaluate(card, data_dir=tmp_path, now=now, ledger=lg)
    assert a.allowed and a.risk_pct == 0.005                          # 第 10 笔
    _open(lg, "S9", "buy")
    assert "daily_open_cap" in eg.evaluate(card, data_dir=tmp_path, now=now, ledger=lg).blockers   # 第 11 笔
    # 同板块同方向: 只数机会板 (swing) 仓; 这里规则是 macd_golden (非 swing) -> 数全部同向同板块仓
    lg2 = _ledger(tmp_path / "b")
    sectors = {f"T{i}": "Technology" for i in range(5)} | {"MU": "Technology"}
    for i in range(3):
        _open(lg2, f"T{i}", "buy", at="2026-10-08T10:00:00-04:00")
    mu = _book_card("MU")
    assert eg.evaluate(mu, data_dir=tmp_path, now=now, ledger=lg2, sectors=sectors).allowed       # 第 4 仓
    _open(lg2, "T3", "buy", at="2026-10-08T10:00:00-04:00")
    assert eg.evaluate(mu, data_dir=tmp_path, now=now, ledger=lg2, sectors=sectors).blockers == ("sector_side_cap",)
    assert WB_DAY
