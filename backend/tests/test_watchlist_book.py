"""(0.11.0 起单一口径, 原试用档测试改写) 自选机会卡下单簿 + 合约式核算 (2026-10-10 产品需求: “纸面账户先按小风险开仓, 但是现在机会卡是多空两种方向的,
你按照合约期权的方式进行核算, 每笔 0.25%（约 $250）, 前向确认继续独立统计, 通过后再升到 1%”)。"""
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.ledger.core import CONTRACT_MARGIN_RATE, Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.services import decision_scan_job as job
from app.services import entry_guard
from app.services import strategy_evaluation as se
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv
from app.trader.trader import SwingTrader

SIG_DAY = date(2026, 10, 8)        # 周四
TODAY = date(2026, 10, 9)          # 周五


def _mode(tmp_path):
    d = tmp_path / "decision_desk"
    d.mkdir(parents=True, exist_ok=True)
    (d / "evidence_mode.json").write_text(json.dumps({"mode": "v4"}))
    se._V4_CACHE.clear()
    wv._STATUS_CACHE.clear()
    wv._PM_CACHE.clear()
    wb._ENSURE_KEY.clear()
    wb._ENSURE_RETRY.clear()
    wb._LOAD_CACHE.clear()


def _fake_forward(tmp_path, rule, rule_r, months=4, per_month=40):
    rng = np.random.default_rng(3)
    rows = []
    for m in range(months):
        for k in range(per_month):
            day = (date(2026, 10, 6) + timedelta(days=31 * m + k % 20)).isoformat()
            sym = f"S{k % 5}"
            rows.append({"as_of": day, "rule": rule, "symbol": sym, "stop": -0.06,
                         "outcome": {"status": "closed", "r": float(rule_r + rng.normal(0, 1.2))}})
            rows.append({"as_of": day, "rule": wv.BASELINE_ID, "symbol": sym, "stop": -0.06,
                         "outcome": {"status": "closed", "r": float(rng.normal(0, 1.2))}})
    wv._save(tmp_path, rows)
    wv._STATUS_CACHE.clear()
    wv._PM_CACHE.clear()


# ── 许可: 单一口径 (0.11.0) ──

def test_single_tier_ruleset_rules_trade_at_ruleset_risk_and_short_never(tmp_path):
    _mode(tmp_path)
    assert wv.permission(tmp_path)["tradable"] == set()                       # 默认 R0 不下单
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 1.0})
    pm = wv.permission(tmp_path)
    assert pm["tradable"] == {"macd_golden"} and pm["risk_pct"] == 0.01 and pm["short_tradable"] == set()
    assert wv.rule_permission(pm, "macd_golden", "long") == (True, 0.01, "rule")
    assert wv.rule_permission(pm, "trend_breakout", "long")[0] is False
    assert wv.rule_permission(pm, "phantom_4h_short", "short")[0] is False
    assert not hasattr(wv, "save_trial") and not hasattr(wv, "trial_config")
    (wv.rules_dir(tmp_path) / "current.json").write_text("{")
    wv._PM_CACHE.clear()
    assert wv.permission(tmp_path)["tradable"] == set()                       # 规则集损坏 → fail-closed






# ── 下单簿 ──

def _bars(sym, closes, end=TODAY):
    days = []
    d = end
    while len(days) < len(closes):
        if us_clock.is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)
    days = days[::-1]
    c = np.array(closes, float)
    return pl.DataFrame({"symbol": sym, "date": days, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": np.full(len(c), 2e6)})


def _sig(sym, rule, side="long", px=100.0):
    if side == "short":
        return {"rule": rule, "symbol": sym, "side": "short", "stop": 0.05, "entry_zone": [px * 0.99, px],
                "invalidation": px * 1.06, "target_zone": [px * 0.86, px * 0.877], "risk": px * 0.065, "rr": 2.0,
                "card_id": "y"}
    return {"rule": rule, "symbol": sym, "stop": -0.06, "entry_zone": [px, px * 1.01], "invalidation": px * 0.94,
            "target_zone": [px * 1.12, px * 1.14], "risk": px * 0.065, "rr": 2.0, "card_id": "x"}


def _signals(tmp_path, day, rows, watch):
    sd = wv.signals_dir(tmp_path)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"{day.isoformat()}.json").write_text(json.dumps({"as_of": day.isoformat(), "watchlist": sorted(watch),
                                                            "closes": {}, "signals": rows}))


def _repo(frames, data_dir):
    full = pl.concat(frames)
    return SimpleNamespace(store=SimpleNamespace(data_dir=data_dir),
                           get_daily_batch=lambda syms, s, e, columns=None: full.filter(
                               pl.col("symbol").is_in(syms) & (pl.col("date") >= s) & (pl.col("date") <= e)))


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def test_book_builds_only_permitted_pending_cards_both_sides(tmp_path, monkeypatch, cal):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    watch = {"NVDA", "MU", "AMD", "PLTR", "AAPL"}
    rows = [_sig("NVDA", "macd_golden"), _sig("MU", "phantom_4h_short", "short"),
            _sig("AMD", "trend_breakout"),                                          # 未勾选 → 不进簿
            _sig("PLTR", "macd_golden"), _sig("PLTR", "tv_ut_bot", "short"),       # 同日多空 → 冲突
            _sig("AAPL", "macd_golden"), _sig("XYZ", "macd_golden")]               # XYZ 不在自选
    _signals(tmp_path, SIG_DAY, rows, watch)
    pm = wv.permission(tmp_path)
    built = wb.build_day(tmp_path, None, SIG_DAY, as_of_close=SIG_DAY, watch=watch, pm=pm)
    by = {(c["symbol"], c["side"]): c for c in built["cards"]}
    assert set(by) == {("NVDA", "long"), ("PLTR", "long"), ("AAPL", "long")}          # 0.11.0: 做空不开
    dec = by[("NVDA", "long")]["evidence"]["decision"]
    assert dec["book"] is True and dec["tier"] == "rule" and dec["risk_pct"] == 0.01 and dec["tab"] == "watchlist"
    assert "gate" not in dec                                                          # 0.12.0: 无 MA50 门槛
    assert not [s for s in built["skipped"] if s["reason"] == "direction_conflict"]
    assert by[("NVDA", "long")]["state"] == "open"


def test_book_skips_signals_already_triggered_or_expired_by_last_close(tmp_path, cal):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    sig_day = date(2026, 10, 6)
    watch = {"NVDA", "MU"}
    _signals(tmp_path, sig_day, [_sig("NVDA", "macd_golden"), _sig("MU", "macd_golden")], watch)
    # 10-06 收 100; NVDA 之后一直 96 (未入区 → 仍待触发); MU 10-07 开在 100.5 (已入场 → 不再补开)
    nv = _bars("NVDA", [100] * 30 + [96, 96], end=date(2026, 10, 8))
    mu = _bars("MU", [100] * 30 + [100.5, 101], end=date(2026, 10, 8))
    repo = _repo([nv, mu], tmp_path)
    built = wb.build_day(tmp_path, repo, sig_day, as_of_close=date(2026, 10, 8), watch=watch, pm=wv.permission(tmp_path))
    assert [c["symbol"] for c in built["cards"]] == ["NVDA"]
    assert {s["symbol"]: s["reason"] for s in built["skipped"]}["MU"].startswith("not_pending")


def test_book_missing_bars_does_not_persist(tmp_path, cal):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    sig_day = date(2026, 10, 6)
    _signals(tmp_path, sig_day, [_sig("NVDA", "macd_golden")], {"NVDA"})
    nv = _bars("NVDA", [100] * 30, end=sig_day)                       # 缺 10-07/10-08 收盘
    built = wb.build_day(tmp_path, _repo([nv], tmp_path), sig_day, as_of_close=date(2026, 10, 8), watch={"NVDA"},
                         pm=wv.permission(tmp_path))
    assert built.get("incomplete") is True and wb.save_day(tmp_path, built) == 0 and built["cards"] == []


def test_book_is_immutable_and_only_appends_new_symbols(tmp_path, cal):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    watch = {"NVDA", "MU"}
    _signals(tmp_path, SIG_DAY, [_sig("NVDA", "macd_golden"), _sig("MU", "ma_golden_cross")], watch)
    b1 = wb.build_day(tmp_path, None, SIG_DAY, as_of_close=SIG_DAY, watch=watch, pm=wv.permission(tmp_path))
    assert wb.save_day(tmp_path, b1) == 1
    first = json.loads(wb.book_path(tmp_path, SIG_DAY).read_text())["cards"][0]
    wv.save_ruleset(tmp_path, ["macd_golden", "ma_golden_cross"], {})       # 新勾选
    b2 = wb.build_day(tmp_path, None, SIG_DAY, as_of_close=SIG_DAY, watch=watch, pm=wv.permission(tmp_path))
    assert wb.save_day(tmp_path, b2) == 1
    cards = json.loads(wb.book_path(tmp_path, SIG_DAY).read_text())["cards"]
    assert cards[0] == first and [c["symbol"] for c in cards] == ["NVDA", "MU"]
    assert wb.save_day(tmp_path, b2) == 0                                  # 幂等


def test_window_days_cover_five_sessions_after_signal(tmp_path, cal):
    for d in (date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 8)):
        _signals(tmp_path, d, [], set())
    # 10-09: 10-01 之后已过 10-02,05,06,07,08,09 = 6 天 → 窗口外; 10-02 之后 5 天 → 仍在窗口
    assert wb.window_days(tmp_path, TODAY, date(2026, 10, 8)) == [date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 8)]


# ── 准入 ──

def _book_card(sym="NVDA", side="long", rule="macd_golden", state=CardState.ACTIVE, day=SIG_DAY, px=100.0):
    s = _sig(sym, rule, side, px)
    return OpportunityCard(id=wb.card_id(sym, rule, side, day), symbol=sym, setup_type=f"strategy:{rule}",
                           created_date=day, side=side, entry_zone=tuple(s["entry_zone"]), invalidation=s["invalidation"],
                           target_zone=tuple(s["target_zone"]), risk_per_share=s["risk"], reward_risk=2.0,
                           evidence={"book": True, "decision": {"book": True, "tab": "watchlist", "tier": "rule",
                                                                "risk_pct": 0.01, "sides": [side],
                                                                "tradable_sources": [rule]}},
                           state=state)


def test_book_admission_uses_current_permission_and_skips_borrow(tmp_path, monkeypatch):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    now = datetime(2026, 10, 9, 10, 30, tzinfo=us_clock.tz)
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA", "MU"})
    long_card, short_card = _book_card(), _book_card("MU", "short", "phantom_4h_short")
    from app.services import shortable
    monkeypatch.setattr(shortable, "_query", lambda s: (_ for _ in ()).throw(AssertionError("合约式不查借券")))
    a = entry_guard.evaluate(long_card, data_dir=tmp_path, now=now, check_borrow=True, refresh_borrow=True)
    b = entry_guard.evaluate(short_card, data_dir=tmp_path, now=now, check_borrow=True, refresh_borrow=True)
    assert a.allowed and a.risk_pct == 0.01 and a.tier == "rule" and a.book
    assert not b.allowed and any(x.startswith("source_not_tradable") for x in b.blockers)     # 0.11.0 做空不开
    monkeypatch.setattr(job, "_watchlist", lambda: {"MU"})                    # 移出自选 → 停止新开
    assert "not_in_watchlist" in entry_guard.evaluate(long_card, data_dir=tmp_path, now=now).blockers
    # 同票另有未成交的反方向卡 → 分歧
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA"})
    other = _book_card("NVDA", "short", "tv_ut_bot", day=date(2026, 10, 7))
    assert "direction_conflict" in entry_guard.evaluate(long_card, data_dir=tmp_path, now=now,
                                                        cards=[long_card, other]).blockers
    # 对方被拦成观察仍算分歧 (否则双方都回 WATCH 后先入区的一方会被放行)
    watched = other.with_state(CardState.WATCH, "x")
    assert "direction_conflict" in entry_guard.evaluate(long_card, data_dir=tmp_path, now=now,
                                                        cards=[long_card, watched]).blockers
    gone = other.with_state(CardState.EXPIRED, "x")
    assert entry_guard.evaluate(long_card, data_dir=tmp_path, now=now, cards=[long_card, gone]).allowed


def test_v4_desk_cards_no_longer_open_positions(tmp_path, monkeypatch):
    """v4 下自选开仓只走机会卡下单簿; 决策台卡即使前向通过也不重复开仓。"""
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    _fake_forward(tmp_path, "macd_golden", 0.8)
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA"})
    desk = OpportunityCard(id="c", symbol="NVDA", setup_type="strategy:macd_golden", created_date=SIG_DAY,
                           entry_zone=(100.0, 101.0), invalidation=95.0, target_zone=(112.0, 114.0), risk_per_share=5.5,
                           reward_risk=2.1, evidence={"decision": {"tier": "proven", "risk_pct": 0.01, "tab": "watchlist",
                                                                   "tradable_sources": ["macd_golden"]}})
    adm = entry_guard.evaluate(desk, data_dir=tmp_path, now=datetime(2026, 10, 9, 11, tzinfo=us_clock.tz))
    assert "v4_book_only" in adm.blockers
    book = _book_card()
    adm = entry_guard.evaluate(book, data_dir=tmp_path, now=datetime(2026, 10, 9, 11, tzinfo=us_clock.tz))
    assert adm.allowed and adm.risk_pct == pytest.approx(0.01) and adm.tier == "rule"


def test_position_cap_scales_with_risk_for_book_cards():
    acc = Account()
    trial = entry_guard.Admission((), 0.0025, False, tier="trial", book=True)
    proven = entry_guard.Admission((), 0.01, False, tier="proven", book=True)
    desk = entry_guard.Admission((), 0.0025, True, tier="explore")
    assert entry_guard.position_cap(acc, trial, 0.06) == 24
    assert entry_guard.position_cap(acc, trial, 0.03) == 12
    assert entry_guard.position_cap(acc, proven, 0.06) == 6
    assert entry_guard.position_cap(acc, desk, 0.06) == 6


# ── 合约式核算 ──

def _ledger(tmp_path):
    lg = Ledger(tmp_path / "ledger")
    lg.init_account(Account())
    lg.replay(Account())
    return lg


@pytest.mark.parametrize("side", ["buy", "short"])
def test_contract_nav_equals_stock_nav_at_every_step(tmp_path, side):
    """同一串成交: 合约式与股票式的净值在每一步都相同; 现金不按名义进出。"""
    a, b = _ledger(tmp_path / "a"), _ledger(tmp_path / "b")
    fills = [("fill", dict(symbol="X", side=side, qty=100, price=50.0, fee=1.0, stop_price=47.0 if side == "buy" else 53.0)),
             ("partial_close", dict(symbol="X", qty=40, price=52.0 if side == "buy" else 48.0, fee=1.0)),
             ("close", dict(symbol="X", qty=60, price=49.0 if side == "buy" else 51.0, fee=1.0))]
    marks_seq = [{"X": 51.0}, {"X": 47.5}, {}]
    for (kind, ev), marks in zip(fills, marks_seq):
        a.append(kind, ev, at="2026-10-09T10:00:00-04:00")
        b.append(kind, {**ev, **({"accounting": "contract"} if kind == "fill" else {})}, at="2026-10-09T10:00:00-04:00")
        assert b.nav(marks) == pytest.approx(a.nav(marks))
        if kind == "fill":
            assert b.cash == pytest.approx(100_000)        # 合约开仓: 现金不动
    assert b.cash == pytest.approx(a.cash) and b.open_positions == []


def test_contract_buying_power_symmetric_and_margin_based(tmp_path):
    lg = _ledger(tmp_path)
    lg.append("fill", dict(symbol="L", side="buy", qty=100, price=200.0, fee=1.0, stop_price=190.0, accounting="contract"),
              at="2026-10-09T10:00:00-04:00")
    lg.append("fill", dict(symbol="S", side="short", qty=100, price=200.0, fee=1.0, stop_price=210.0,
                           accounting="contract"), at="2026-10-09T10:00:00-04:00")
    marks = {"L": 200.0, "S": 200.0}
    assert lg.contract_margin(marks) == pytest.approx(2 * 100 * 200 * CONTRACT_MARGIN_RATE)
    assert lg.buying_power(marks) == pytest.approx(lg.nav(marks) - 2 * 100 * 200 * CONTRACT_MARGIN_RATE)
    assert lg.short_exposure(marks) == 0.0
    # 重放一致
    lg2 = Ledger(tmp_path / "ledger", read_only=True)
    lg2.replay(Account(), persist=False)
    assert lg2.nav(marks) == pytest.approx(lg.nav(marks)) and {p.symbol: p.contract for p in lg2.open_positions} == \
        {"L": True, "S": True}


def test_contract_and_stock_cannot_mix_on_one_position(tmp_path):
    lg = _ledger(tmp_path)
    lg.append("fill", dict(symbol="L", side="buy", qty=10, price=100.0, fee=1.0, accounting="contract"),
              at="2026-10-09T10:00:00-04:00")
    with pytest.raises(ValueError):
        lg.append("fill", dict(symbol="L", side="buy", qty=10, price=100.0, fee=1.0), at="2026-10-09T10:01:00-04:00")
    with pytest.raises(ValueError):
        lg.append("fill", dict(symbol="Q", side="buy", qty=10, price=100.0, accounting="futures"),
                  at="2026-10-09T10:01:00-04:00")


def test_trader_fills_book_card_contract_style_at_trial_risk(tmp_path):
    lg = _ledger(tmp_path)
    acc = replace(Account(), risk_per_trade_pct=0.0025)
    tr = SwingTrader(lg, acc)
    card = _book_card("MU", "short", "phantom_4h_short")
    t1, t2 = "2026-10-09T10:00:00-04:00", "2026-10-09T10:01:00-04:00"
    assert tr.try_fill_entry(card, price=99.5, trade_date=TODAY, at=t1, quote_ts=t1) is None
    fill = tr.try_fill_entry(card, price=99.6, trade_date=TODAY, at=t2, quote_ts=t2)
    assert fill["side"] == "short" and fill["accounting"] == "contract" and fill["risk_pct"] == 0.0025
    assert fill["qty"] == int(100_000 * 0.0025 / card.risk_per_share)          # $250 / 6.5 = 38 股
    assert lg.cash == pytest.approx(100_000) and lg.open_positions[0].contract


# ── 交易环端到端: 下单簿 → 生命周期 → 意向 → 成交 ──

def test_loop_trades_book_cards_end_to_end(tmp_path, monkeypatch, cal):
    from app.services import shortable
    from app.services.trading_loop import TradingLoop
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    watch = {"NVDA", "MU"}
    monkeypatch.setattr(job, "_watchlist", lambda: set(watch))
    monkeypatch.setattr(shortable, "_query", lambda s: (_ for _ in ()).throw(AssertionError("no borrow check")))
    _signals(tmp_path, SIG_DAY, [_sig("NVDA", "macd_golden"), _sig("MU", "phantom_4h_short", "short")], watch)
    frames = [_bars("NVDA", [100] * 140, end=SIG_DAY), _bars("MU", [100] * 140, end=SIG_DAY)]
    full = pl.concat(frames)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=lambda: pl.DataFrame({"symbol": ["NVDA", "MU"], "sector": ["Tech", "Tech"]}),
                           get_daily=lambda s, a, b, columns=None: full.filter(pl.col("symbol") == s),
                           get_daily_batch=lambda syms, s, e, columns=None: full.filter(
                               pl.col("symbol").is_in(syms) & (pl.col("date") <= e)))
    clock = [datetime(2026, 10, 9, 8, 0, tzinfo=us_clock.tz)]
    monkeypatch.setattr(us_clock, "now", lambda: clock[0])
    monkeypatch.setattr(us_clock, "today", lambda: clock[0].date())
    loop = TradingLoop(repo, Account())
    monkeypatch.setattr(loop, "_enqueue_events", lambda: None)
    monkeypatch.setattr(loop.earnings, "usable", lambda now=None: True)
    monkeypatch.setattr(loop.earnings, "event_for", lambda s: None)
    monkeypatch.setattr(loop.corp_actions, "usable", lambda now: True)
    monkeypatch.setattr(loop.corp_actions, "covers", lambda s: True)
    monkeypatch.setattr(loop.corp_actions, "actions", lambda: [])
    loop.boot()
    from app.opportunity.engine import ScanResult
    loop.opp_svc.save_scan(ScanResult(as_of=SIG_DAY, gate="green", params_revision=3, scanned=2))   # 当日大盘闸门 (决策台卡全观察)
    assert loop.ensure_watchlist_book(clock[0]) == {SIG_DAY.isoformat(): 1}
    ids = {c.symbol: c for c in loop.opp_svc.all_cards(TODAY) if (c.evidence or {}).get("book")}
    assert set(ids) == {"NVDA"}                                            # 0.11.0: 做空不开

    def q(px, t):
        at = t.isoformat()
        o = t.replace(hour=9, minute=30, second=0).isoformat()
        return {s: {"symbol": s, "price": p, "feed": "tickflow", "timestamp": at, "open": 100.2, "open_timestamp": o}
                for s, p in px.items()}

    loop.opp_svc.revalidate_unfilled = lambda now: set(watch)
    for minute, px in ((50, {"NVDA": 100.4, "MU": 99.6}), (51, {"NVDA": 100.5, "MU": 99.5})):
        clock[0] = datetime(2026, 10, 9, 9, minute, tzinfo=us_clock.tz)
        loop.intraday_fill_check(q(px, clock[0]), entry_symbols=set(watch))
    fills = [e for e in loop.ledger.read_events() if e["kind"] == "fill"]
    assert {(f["symbol"], f["side"]) for f in fills} == {("NVDA", "buy")}
    assert all(f["accounting"] == "contract" and f["entry_tier"] == "rule" for f in fills)
    assert all(f["risk_pct"] == 0.01 for f in fills), [(f["qty"], f.get("risk_pct")) for f in fills]
    assert loop.ledger.cash == pytest.approx(100_000)          # 合约式: 开仓现金不动 (费用计入均价)
    # 影子账本 (前向确认数据) 没有被交易写过
    assert not wv.path(tmp_path).exists()


def test_backfill_events_are_not_pushed_or_mirrored(tmp_path):
    from app.services import alpaca_mirror, notify_opportunity
    ev = {"kind": "fill", "seq": 7, "symbol": "PLTR", "side": "buy", "qty": 10, "price": 200.0, "fee": 1.0,
          "trade_date": "2026-10-09", "card_id": "c", "backfill": True, "at": "2026-10-09T10:00:00-04:00"}
    assert notify_opportunity.enqueue_trade_event(tmp_path, ev) is True
    assert notify_opportunity.notification_status(tmp_path).get("counts", {}).get("pending", 0) in (0, None)
    m = alpaca_mirror.AlpacaMirror(tmp_path, http=lambda *a, **k: {})
    m.start([{"seq": 0}])
    assert m.sync([ev]) == 0


def test_table_marks_ruleset_rows_tradable_at_ruleset_size(tmp_path, monkeypatch, cal):
    from app.services import watchlist_table as wt
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 1.0})
    monkeypatch.setattr(us_clock, "today", lambda: TODAY)
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA"})
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda syms: {})
    _signals(tmp_path, SIG_DAY, [_sig("NVDA", "macd_golden")], {"NVDA"})
    led = Ledger(tmp_path / "ledger")
    led.init_account(Account())
    nv = _bars("NVDA", [100] * 30, end=SIG_DAY)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_daily=lambda s, a, b, columns=None: nv,
                           get_daily_batch=lambda syms, s, e, columns=None: nv)
    row = next(r for r in wt.build(data_dir=tmp_path, repo=repo)["rows"] if r["symbol"] == "NVDA")
    assert row["tradable"] is True and row["tier"] == "rule" and row["qty"] == int(1000 / 6.5)   # 单一口径 1%



def test_ensure_retries_missing_bars_at_most_every_five_minutes(tmp_path, monkeypatch, cal):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    sig_day = date(2026, 10, 6)
    _signals(tmp_path, sig_day, [_sig("NVDA", "macd_golden")], {"NVDA"})
    calls = []
    nv = _bars("NVDA", [100] * 30, end=sig_day)                       # 缺 10-07/10-08
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: (calls.append(1), nv)[1])
    t = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: t[0])
    for _ in range(3):
        assert wb.ensure(tmp_path, repo, today=TODAY, as_of_close=date(2026, 10, 8), watch={"NVDA"}) == {}
    assert len(calls) == 1
    t[0] += wb.RETRY_AFTER_S + 1
    wb.ensure(tmp_path, repo, today=TODAY, as_of_close=date(2026, 10, 8), watch={"NVDA"})
    assert len(calls) == 2



# ── 独立审查回归 (2026-10-10) ──




@pytest.mark.real_corporate_actions
def test_intraday_corp_merge_failure_keeps_morning_cache_usable(tmp_path):
    """P2: 盘中补拉失败不让全账户停开仓; 新票仍未覆盖。"""
    from app.services.corporate_actions import CorporateActionService
    now = datetime(2026, 10, 9, 8, 0, tzinfo=us_clock.tz)
    svc = CorporateActionService(tmp_path)
    svc.refresh(["NVDA"], now, http_get=lambda url: {"corporate_actions": {}})
    assert svc.usable(now) and svc.covers("NVDA")
    later = datetime(2026, 10, 9, 11, 0, tzinfo=us_clock.tz)
    def boom(url):
        raise RuntimeError("x")
    res = svc.refresh_merge(["NVDA", "MU"], later, http_get=boom)
    assert res["merged"] == 0 and svc.usable(later) and svc.covers("NVDA") and not svc.covers("MU")
    res = svc.refresh_merge(["NVDA", "MU"], later, http_get=lambda url: {"corporate_actions": {}})
    assert res["merged"] == 1 and svc.covers("MU") and svc.usable(later)


def test_builtin_setups_keep_their_name_for_degradation_checks(tmp_path, cal):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["breakout"], {})
    _signals(tmp_path, SIG_DAY, [_sig("NVDA", "breakout")], {"NVDA"})
    built = wb.build_day(tmp_path, None, SIG_DAY, as_of_close=SIG_DAY, watch={"NVDA"}, pm=wv.permission(tmp_path))
    assert built["cards"][0]["setup_type"] == "breakout"


def test_contract_short_does_not_freeze_entries_when_a_quote_is_missing(tmp_path):
    lg = _ledger(tmp_path)
    lg.append("fill", dict(symbol="S", side="short", qty=10, price=100.0, fee=1.0, stop_price=106.0,
                           accounting="contract"), at="2026-10-09T10:00:00-04:00")
    lg.append("fill", dict(symbol="L", side="buy", qty=10, price=100.0, fee=1.0, stop_price=94.0,
                           accounting="contract"), at="2026-10-09T10:00:00-04:00")
    assert entry_guard.missing_valuation(lg, {"S": {"price": 99.0}}) == []
    lg.append("fill", dict(symbol="X", side="short", qty=10, price=50.0, fee=1.0, stop_price=53.0),
              at="2026-10-09T10:01:00-04:00")                          # 股票式空头: 仍要求全部报价
    assert entry_guard.missing_valuation(lg, {"S": {"price": 99.0}, "X": {"price": 50.0}}) == ["L"]


def test_contract_rounds_are_never_mirrored_to_broker(tmp_path):
    from app.services import alpaca_mirror
    m = alpaca_mirror.AlpacaMirror(tmp_path, http=lambda *a, **k: {})
    m.start([{"seq": 0}])
    evs = [{"kind": "fill", "seq": 1, "symbol": "MU", "side": "short", "qty": 3, "price": 100.0, "accounting": "contract",
            "at": "x"},
           {"kind": "close", "seq": 2, "symbol": "MU", "qty": 3, "price": 98.0, "position_side": "short", "entry_seq": 1,
            "at": "x"},
           {"kind": "fill", "seq": 3, "symbol": "PLTR", "side": "buy", "qty": 5, "price": 200.0, "backfill": True, "at": "x"},
           {"kind": "close", "seq": 4, "symbol": "PLTR", "qty": 5, "price": 201.0, "entry_seq": 3, "at": "x"},
           {"kind": "fill", "seq": 5, "symbol": "NVDA", "side": "buy", "qty": 2, "price": 100.0, "at": "x"}]
    assert m.sync(evs) == 1 and [o["symbol"] for o in m._state["orders"].values()] == ["NVDA"]


def test_opposite_short_signal_never_enters_book(tmp_path, monkeypatch, cal):
    """0.11.0: 做空不开 —— 同日看空信号不进簿, 也不再把看多卡判成多空冲突。"""
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    wv.save_display(tmp_path, [], ["phantom_4h_short", "tv_ut_bot"])
    watch = {"NVDA"}
    _signals(tmp_path, SIG_DAY, [_sig("NVDA", "macd_golden"), _sig("NVDA", "tv_ut_bot", "short")], watch)
    b1 = wb.build_day(tmp_path, None, SIG_DAY, as_of_close=SIG_DAY, watch=watch, pm=wv.permission(tmp_path))
    assert [(c["symbol"], c["side"]) for c in b1["cards"]] == [("NVDA", "long")]
    assert not any(x["reason"] == "direction_conflict" for x in b1["skipped"])


def test_single_symbol_missing_bars_only_skips_that_symbol(tmp_path, cal):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    sig_day = date(2026, 10, 6)
    _signals(tmp_path, sig_day, [_sig("NVDA", "macd_golden"), _sig("MU", "macd_golden")], {"NVDA", "MU"})
    nv = _bars("NVDA", [100] * 30 + [96, 96], end=date(2026, 10, 8))
    mu = _bars("MU", [100] * 30, end=sig_day)                              # MU 缺 10-07/08
    built = wb.build_day(tmp_path, _repo([nv, mu], tmp_path), sig_day, as_of_close=date(2026, 10, 8),
                         watch={"NVDA", "MU"}, pm=wv.permission(tmp_path))
    assert wb.save_day(tmp_path, built) == 1
    assert [c["symbol"] for c in json.loads(wb.book_path(tmp_path, sig_day).read_text())["cards"]] == ["NVDA"]


@pytest.mark.real_corporate_actions
def test_loop_new_symbol_merge_failure_does_not_halt_covered_symbols(tmp_path, monkeypatch, cal):
    """P2 端到端: 早盘缓存覆盖 NVDA; 盘中新进簿的 MU 补拉失败 → NVDA 照常成交, MU 被 not_covered 拦下, 5 分钟后重试。"""
    from app.services import corporate_actions as ca
    from app.services.trading_loop import TradingLoop
    from app.opportunity.engine import ScanResult
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    watch = {"NVDA", "MU"}
    monkeypatch.setattr(job, "_watchlist", lambda: set(watch))
    _signals(tmp_path, SIG_DAY, [_sig("NVDA", "macd_golden"), _sig("MU", "macd_golden")], watch)
    full = pl.concat([_bars("NVDA", [100] * 140, end=SIG_DAY), _bars("MU", [100] * 140, end=SIG_DAY)])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=lambda: pl.DataFrame({"symbol": ["NVDA", "MU"], "sector": ["Tech", "Tech"]}),
                           get_daily=lambda s, a, b, columns=None: full.filter(pl.col("symbol") == s),
                           get_daily_batch=lambda syms, s, e, columns=None: full.filter(
                               pl.col("symbol").is_in(syms) & (pl.col("date") <= e)))
    clock = [datetime(2026, 10, 9, 8, 0, tzinfo=us_clock.tz)]
    monkeypatch.setattr(us_clock, "now", lambda: clock[0])
    monkeypatch.setattr(us_clock, "today", lambda: clock[0].date())
    loop = TradingLoop(repo, Account())
    monkeypatch.setattr(loop, "_enqueue_events", lambda: None)
    loop.boot()
    loop.opp_svc.save_scan(ScanResult(as_of=SIG_DAY, gate="green", params_revision=3, scanned=2))
    loop.corp_actions.refresh(["NVDA"], clock[0], http_get=lambda url: {"corporate_actions": {}})   # 早盘只覆盖 NVDA
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("source down")
    monkeypatch.setattr(ca, "fetch_actions", boom)
    loop.ensure_watchlist_book(clock[0])

    def q(px, t):
        o = t.replace(hour=9, minute=30, second=0).isoformat()
        return {s: {"symbol": s, "price": p, "feed": "tickflow", "timestamp": t.isoformat(), "open": 100.2,
                    "open_timestamp": o} for s, p in px.items()}

    loop.opp_svc.revalidate_unfilled = lambda now: set(watch)
    for minute in (50, 51):
        clock[0] = datetime(2026, 10, 9, 9, minute, tzinfo=us_clock.tz)
        loop._cover_new_symbols(clock[0])
        loop.intraday_fill_check(q({"NVDA": 100.4 + minute / 100, "MU": 99.5}, clock[0]), entry_symbols=set(watch))
    fills = {e["symbol"] for e in loop.ledger.read_events() if e["kind"] == "fill"}
    assert fills == {"NVDA"}
    assert any(x["symbol"] == "MU" and x["reason"] == "corporate_actions_not_covered" and x.get("card_id")
               for x in loop.entry_blockers)                               # 0.26.0: 逐卡拦截带 card_id (漏斗按它归属)
    assert not any(b["reason"] == "corporate_actions_unavailable" for b in loop.entry_blockers)
    assert len(calls) == 1                                                 # 5 分钟内不重试
    clock[0] = datetime(2026, 10, 9, 9, 57, tzinfo=us_clock.tz)
    loop._cover_new_symbols(clock[0])
    assert len(calls) == 2


# ── 开仓节奏上限 / 单笔风险上限 / 回补剔除 (2026-10-12 产品决策) ──

def _open(lg, sym, side, at="2026-10-09T10:00:00-04:00", **kw):
    lg.append("fill", dict(symbol=sym, side=side, qty=10, price=100.0, fee=1.0,
                           stop_price=94.0 if side == "buy" else 106.0, accounting="contract",
                           card_id=f"c-{sym}", **kw), at=at)


def test_book_daily_open_cap_blocks_sixth_open_of_the_day(tmp_path, monkeypatch):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA"})
    now = datetime(2026, 10, 9, 10, 30, tzinfo=us_clock.tz)
    lg = _ledger(tmp_path)
    _open(lg, "OLD", "buy", at="2026-10-08T10:00:00-04:00")          # 昨天的开仓不占今天额度
    for i in range(entry_guard.BOOK_DAILY_OPEN_CAP - 1):
        _open(lg, f"S{i}", "buy")
    card = _book_card()
    assert entry_guard.evaluate(card, data_dir=tmp_path, now=now, ledger=lg).allowed     # 第 5 笔可开
    _open(lg, "S9", "buy")
    lg.append("close", dict(symbol="S9", qty=10, price=101.0, fee=1.0, card_id="c-S9"), at="2026-10-09T10:10:00-04:00")
    a = entry_guard.evaluate(card, data_dir=tmp_path, now=now, ledger=lg)
    assert not a.allowed and "daily_open_cap" in a.blockers                             # 平仓不退额度
    nxt = datetime(2026, 10, 12, 10, 30, tzinfo=us_clock.tz)
    assert "daily_open_cap" not in entry_guard.evaluate(card, data_dir=tmp_path, now=nxt, ledger=lg).blockers


def test_book_sector_side_cap_two_per_sector_and_direction(tmp_path, monkeypatch):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA", "MU", "LLY", "ZZZ"})
    now = datetime(2026, 10, 9, 10, 30, tzinfo=us_clock.tz)
    sectors = {"AMD": "Technology", "INTC": "Technology", "NVDA": "Technology", "MU": "Technology",
               "LLY": "Health Care"}
    lg = _ledger(tmp_path)
    _open(lg, "AMD", "buy")
    _open(lg, "INTC", "buy")
    long_mu = _book_card("MU")
    a = entry_guard.evaluate(long_mu, data_dir=tmp_path, now=now, ledger=lg, sectors=sectors)
    assert not a.allowed and a.blockers == ("sector_side_cap",)                       # 科技多头已 2 仓
    assert entry_guard.evaluate(_book_card("LLY"), data_dir=tmp_path, now=now, ledger=lg,
                                sectors=sectors).allowed                              # 其他板块多头不受影响
    lg.append("close", dict(symbol="AMD", qty=10, price=101.0, fee=1.0, card_id="c-AMD"),
              at="2026-10-09T10:20:00-04:00")
    assert entry_guard.evaluate(long_mu, data_dir=tmp_path, now=now, ledger=lg, sectors=sectors).allowed
    unknown = entry_guard.evaluate(_book_card("ZZZ"), data_dir=tmp_path, now=now, ledger=lg, sectors=sectors)
    assert "sector_unknown" in unknown.blockers                                         # 板块未知 = 不开


def test_contract_fill_caps_actual_risk_at_budget(tmp_path):
    lg = _ledger(tmp_path)
    acc = replace(Account(), risk_per_trade_pct=0.0025)
    tr = SwingTrader(lg, acc)
    card = _book_card("NVDA", "long")                                 # 区间 100~101, 失效 94, 计划每股风险 6.5
    t1, t2 = "2026-10-09T10:00:00-04:00", "2026-10-09T10:01:00-04:00"
    assert tr.try_fill_entry(card, price=100.9, trade_date=TODAY, at=t1, quote_ts=t1) is None
    fill = tr.try_fill_entry(card, price=101.0, trade_date=TODAY, at=t2, quote_ts=t2)
    rps = fill["price"] - card.invalidation                            # 101.0505 - 94 = 7.0505
    assert fill["qty"] == int(250 / rps) == 35
    assert fill["qty"] * rps <= 250 and fill["risk_cap"]["planned_qty"] == int(250 / 6.5)


def test_legacy_trial_card_blocked_after_gate_removal(tmp_path, monkeypatch):
    """0.12.0 去掉 MA50 门槛后, 0.11.0 前的试用档旧卡 (tier=trial, 无 gate) 仍不能开仓, 只等入场窗过期。"""
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    now = datetime(2026, 10, 9, 10, 30, tzinfo=us_clock.tz)
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA"})
    card = _book_card()
    old = card.__class__(**{**card.__dict__, "evidence": {"book": True, "decision": {
        **card.evidence["decision"], "tier": "trial", "risk_pct": 0.0025}}})
    a = entry_guard.evaluate(old, data_dir=tmp_path, now=now)
    assert not a.allowed and "legacy_card" in a.blockers
    assert entry_guard.evaluate(card, data_dir=tmp_path, now=now).allowed      # 新卡 (tier=rule, 无 gate 字段) 照开


def test_book_has_no_ma50_gate(tmp_path):
    """0.12.0: 大盘/个股在 MA50 下方也照常落簿 (repo=None 也不再因门槛数据缺失整天不落盘)。"""
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    watch = {"NVDA"}
    _signals(tmp_path, SIG_DAY, [_sig("NVDA", "macd_golden")], watch)
    built = wb.build_day(tmp_path, None, SIG_DAY, as_of_close=SIG_DAY, watch=watch, pm=wv.permission(tmp_path))
    assert [c["symbol"] for c in built["cards"]] == ["NVDA"]
    assert not [s for s in built["skipped"] if s.get("reason") in ("gate", "bars_unavailable")]


# ── 做空: 规则集 short + SPY<50日线 门槛 ──

def _spy_bars(closes, end):
    n = len(closes)
    d0 = end - timedelta(days=n - 1)
    return pl.DataFrame({"symbol": "SPY", "date": [d0 + timedelta(days=i) for i in range(n)],
                         "open": closes, "high": [x * 1.01 for x in closes], "low": [x * 0.99 for x in closes],
                         "close": closes, "volume": np.full(n, 1e9)})


def test_book_short_card_when_ruleset_short_and_spy_below_ma50(tmp_path, cal):
    _mode(tmp_path)
    wv._SHORT_GATE_CACHE.clear()
    wv.save_ruleset(tmp_path, ["macd_golden"], {}, short=["phantom_4h_short"])
    watch = {"NVDA", "MU"}
    _signals(tmp_path, SIG_DAY, [_sig("MU", "phantom_4h_short", "short"), _sig("NVDA", "macd_golden")], watch)
    spy = _spy_bars([100.0] * 60 + [90.0], SIG_DAY)                 # 收盘 90 < 50 根均值 ≈99.8 → 门槛过
    repo = _repo([_bars("MU", [100] * 30, end=SIG_DAY), spy], tmp_path)
    pm = wv.permission(tmp_path)
    assert pm["short_tradable"] == {"phantom_4h_short"}
    built = wb.build_day(tmp_path, repo, SIG_DAY, as_of_close=SIG_DAY, watch=watch, pm=pm)
    by = {(c["symbol"], c["side"]): c for c in built["cards"]}
    assert set(by) == {("MU", "short"), ("NVDA", "long")}
    dec = by[("MU", "short")]["evidence"]["decision"]
    assert dec["tier"] == "rule" and dec["risk_pct"] == 0.01
    assert dec["short_gate"] == {"day": SIG_DAY.isoformat(), "spy_close": 90.0, "spy_ma50": pytest.approx(99.8)}
    assert built["permission"]["proven"] == ["macd_golden"] and built["permission"]["short"] == ["phantom_4h_short"]


def test_book_gate_false_skips_short_without_direction_conflict(tmp_path, cal):
    _mode(tmp_path)
    wv._SHORT_GATE_CACHE.clear()
    wv.save_ruleset(tmp_path, ["macd_golden"], {}, short=["phantom_4h_short"])
    _signals(tmp_path, SIG_DAY, [_sig("MU", "phantom_4h_short", "short"), _sig("MU", "macd_golden")], {"MU"})
    spy = _spy_bars([90.0] * 60 + [110.0], SIG_DAY)                 # 收盘 110 > 均值 → 门槛不过
    built = wb.build_day(tmp_path, _repo([_bars("MU", [100] * 30, end=SIG_DAY), spy], tmp_path), SIG_DAY,
                         as_of_close=SIG_DAY, watch={"MU"}, pm=wv.permission(tmp_path))
    assert {(c["symbol"], c["side"]) for c in built["cards"]} == {("MU", "long")}   # 被拦的看空不与看多冲突
    assert {s["reason"] for s in built["skipped"]} == {"short_gate"}


def test_book_gate_missing_marks_incomplete_and_does_not_persist(tmp_path, cal):
    _mode(tmp_path)
    wv._SHORT_GATE_CACHE.clear()
    wv.save_ruleset(tmp_path, ["macd_golden"], {}, short=["phantom_4h_short"])
    _signals(tmp_path, SIG_DAY, [_sig("MU", "phantom_4h_short", "short")], {"MU"})
    repo = _repo([_bars("MU", [100] * 30, end=SIG_DAY)], tmp_path)  # 没有 SPY 日K
    built = wb.build_day(tmp_path, repo, SIG_DAY, as_of_close=SIG_DAY, watch={"MU"}, pm=wv.permission(tmp_path))
    assert built["incomplete"] is True and built["cards"] == []
    assert built["skipped"][0]["reason"] == "short_gate_missing"
