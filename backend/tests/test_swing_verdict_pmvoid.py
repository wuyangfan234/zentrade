"""0.27.0 机会卡下单裁决 + 做多第 1 天盘前越止损作废 (2026-10-19 产品决策 B “折中”; 期望写美元; 盘前量/新闻角标保留)。"""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from test_swing_book_tight import SIG_DAY, _repo, _row, _setup, _snapshot

from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.services import entry_guard
from app.services import swing_desk as sd
from app.services import swing_pm_check as pmc
from app.services import swing_settlement as ss
from app.services import swing_verdict as sv
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv

D1 = date(2026, 10, 9)        # SIG_DAY (周四 10-08) 后第 1 个交易日 (周五)
D2 = date(2026, 10, 12)       # 第 2 个交易日 (周一)


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    pmc._LOAD_CACHE.clear()
    pmc._LAST_TRY.clear()


def et(d, h, m=0):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=us_clock.tz)


def _book_card(tmp_path, monkeypatch, sym="LLY", close=100.0, atr=2.0, side="long"):
    _setup(tmp_path, monkeypatch)
    if side == "long":
        rows = [_row("macd_golden", sym), _row("phantom_4h", sym)]
    else:
        rows = [_row("tv_halftrend", sym, "short"), _row("phantom_4h_short", sym, "short")]
    _snapshot(tmp_path, SIG_DAY, rows, {sym}, {sym: close}, {sym: atr})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={sym}, pm=wv.permission(tmp_path))
    assert wb.save_day(tmp_path, built) == 1
    return OpportunityCard.from_dict(built["cards"][0])


# ---------- 1. 盘前越止损检查 (纯规则) ----------

def test_long_day1_premarket_below_stop_voids_and_is_written_once(tmp_path, monkeypatch, cal):
    card = _book_card(tmp_path, monkeypatch)                        # 区 99.00-99.50, 止损 97.50
    calls = []

    def sip(syms, day):
        calls.append((tuple(syms), day))
        return {"LLY": (97.2, "08:30")}
    res = pmc.run(tmp_path, [card], et(D1, 9, 20), sip=sip)
    assert res["void"] == [card.id] and res["checked"] == 1
    rec = pmc.load(tmp_path, D1)[card.id]
    assert rec["void"] is True and rec["price"] == 97.2 and rec["source"] == "sip" and rec["stop"] == 97.5
    # 再跑: 已查过 -> 不再取价, 记录不改写
    before = pmc.check_path(tmp_path, D1).read_bytes()
    assert pmc.run(tmp_path, [card], et(D1, 9, 31), sip=sip)["checked"] == 0
    assert len(calls) == 1 and pmc.check_path(tmp_path, D1).read_bytes() == before


def test_above_stop_not_void_and_short_never_checked(tmp_path, monkeypatch, cal):
    card = _book_card(tmp_path, monkeypatch)
    assert pmc.run(tmp_path, [card], et(D1, 9, 20), sip=lambda s, d: {"LLY": (97.6, "08:30")})["void"] == []
    assert pmc.load(tmp_path, D1)[card.id]["void"] is False
    short = _book_card(tmp_path / "s", monkeypatch, sym="AMD", side="short")
    assert short.is_short
    assert pmc.run(tmp_path / "s", [short], et(D1, 9, 20), sip=lambda s, d: {"AMD": (999.0, "08:30")})["checked"] == 0


def test_only_first_entry_day_and_not_before_sip_ready(tmp_path, monkeypatch, cal):
    card = _book_card(tmp_path, monkeypatch)
    sip = lambda s, d: {"LLY": (90.0, "08:30")}                     # noqa: E731
    assert pmc.run(tmp_path, [card], et(D1, 9, 10), sip=sip)["skipped"] == "not_ready"     # 09:16 前拿不到 08:30 那根
    assert pmc.run(tmp_path, [card], et(D2, 9, 20), sip=sip)["checked"] == 0               # 第 2 天不查 (那一格期望为正)
    assert not pmc.check_path(tmp_path, D2).exists()


def test_sip_error_retries_then_falls_back_to_hl_0900_then_none(tmp_path, monkeypatch, cal):
    card = _book_card(tmp_path, monkeypatch)

    def boom(s, d):
        raise ConnectionError("x")
    r = pmc.run(tmp_path, [card], et(D1, 9, 20), sip=boom, fallback=lambda s, d: {"LLY": 97.0})
    assert r["skipped"].startswith("sip_error") and not pmc.check_path(tmp_path, D1).exists()   # 09:44 前不写, 等重试
    pmc._LAST_TRY.clear()
    r = pmc.run(tmp_path, [card], et(D1, 9, 44), sip=boom, fallback=lambda s, d: {"LLY": 97.0})
    assert r["void"] == [card.id] and pmc.load(tmp_path, D1)[card.id]["source"] == "hl_0900"
    # 两边都没有 -> 记 none, 不作废 (不因缺数据作废, 也不让卡一直等)
    card2 = _book_card(tmp_path / "b", monkeypatch)
    r = pmc.run(tmp_path / "b", [card2], et(D1, 9, 44), sip=boom, fallback=lambda s, d: {})
    rec = pmc.load(tmp_path / "b", D1)[card2.id]
    assert r["void"] == [] and rec["source"] == "none" and rec["void"] is False


def test_sip_ok_but_symbol_missing_uses_gap_file_mid(tmp_path, monkeypatch, cal):
    card = _book_card(tmp_path, monkeypatch)
    (tmp_path / "premarket_gap").mkdir()
    (tmp_path / "premarket_gap" / f"{D1}.json").write_text(json.dumps({"mids": {"LLY": 97.1, "BAD": "x"}}))
    r = pmc.run(tmp_path, [card], et(D1, 9, 20), sip=lambda s, d: {})
    assert r["void"] == [card.id] and pmc.load(tmp_path, D1)[card.id]["source"] == "hl_0900"


def test_bad_record_file_is_kept_not_overwritten(tmp_path, monkeypatch, cal):
    card = _book_card(tmp_path, monkeypatch)
    p = pmc.check_path(tmp_path, D1)
    p.parent.mkdir(parents=True)
    p.write_text("{garbage")
    pmc.run(tmp_path, [card], et(D1, 9, 20), sip=lambda s, d: {"LLY": (98.0, "08:30")})
    assert list(p.parent.glob(f"{D1}.bad-*")) and pmc.load(tmp_path, D1)[card.id]["void"] is False


# ---------- 2. 准入 (第二道闸, fail-closed) ----------

def test_guard_blocks_void_and_unchecked_day1_only(tmp_path, monkeypatch, cal):
    card = _book_card(tmp_path, monkeypatch)
    monkeypatch.setattr("app.services.decision_scan_job._watchlist", lambda: {"LLY"})
    # 第 1 天还没查: 09:45 前不拦 (纸面本来不成交, 免得把卡打回观察), 09:45 起先不开
    assert entry_guard.evaluate(card, data_dir=tmp_path, now=et(D1, 9, 40)).allowed
    a = entry_guard.evaluate(card, data_dir=tmp_path, now=et(D1, 10, 0))
    assert pmc.PENDING in a.blockers
    # 查过、没越止损 -> 放行
    pmc.run(tmp_path, [card], et(D1, 9, 20), sip=lambda s, d: {"LLY": (98.0, "08:30")})
    assert entry_guard.evaluate(card, data_dir=tmp_path, now=et(D1, 10, 0)).allowed
    # 越止损 -> 第 1 天、第 2 天都拦
    t2 = tmp_path / "v"
    card2 = _book_card(t2, monkeypatch)
    pmc.run(t2, [card2], et(D1, 9, 20), sip=lambda s, d: {"LLY": (97.0, "08:30")})
    assert pmc.REASON in entry_guard.evaluate(card2, data_dir=t2, now=et(D1, 10, 0)).blockers
    assert pmc.REASON in entry_guard.evaluate(card2, data_dir=t2, now=et(D2, 10, 0)).blockers
    # 第 2 天没有第 1 天记录 (系统第 1 天没运行) -> 不拦 (没有依据)
    t3 = tmp_path / "n"
    card3 = _book_card(t3, monkeypatch)
    assert entry_guard.evaluate(card3, data_dir=t3, now=et(D2, 10, 0)).allowed


def test_guard_ignores_short_and_old_rules(tmp_path, monkeypatch, cal):
    short = _book_card(tmp_path, monkeypatch, sym="AMD", side="short")
    monkeypatch.setattr("app.services.decision_scan_job._watchlist", lambda: {"AMD"})
    assert not ({pmc.PENDING, pmc.REASON} & set(entry_guard.evaluate(short, data_dir=tmp_path, now=et(D1, 10)).blockers))


# ---------- 3. 交易环: 作废写台账 expired, 撤 pending 意向; 幂等 ----------

class _Ledger:
    def __init__(self):
        self.events = []
        import threading
        self._lock = threading.RLock()

    def read_events(self):
        return list(self.events)

    def append(self, kind, payload, *, at):
        e = {"seq": len(self.events) + 1, "kind": kind, "at": at, **payload}
        self.events.append(e)
        return e


def test_trading_loop_voids_card_in_ledger_once(tmp_path, monkeypatch, cal):
    from app.services.trading_loop import TradingLoop
    card = _book_card(tmp_path, monkeypatch)
    lg = _Ledger()
    lg.events.append({"seq": 1, "kind": "entry_intent", "card_id": card.id, "status": "pending", "symbol": "LLY",
                      "at": "x"})
    loop = TradingLoop.__new__(TradingLoop)
    loop.ledger = lg
    loop.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    trans = []
    loop.opp_svc = SimpleNamespace(_transition=lambda c, st, why, now: trans.append((c.id, st, why)),
                                   all_cards=lambda d: [card])
    loop._sync_card_book = lambda: None
    monkeypatch.setattr(pmc, "sip_last_premarket", lambda s, d: {"LLY": (97.0, "08:30")})
    loop.premarket_void_check(et(D1, 9, 20))
    assert trans == [(card.id, CardState.EXPIRED, "盘前越过止损作废")]
    assert lg.events[-1]["kind"] == "entry_intent" and lg.events[-1]["status"] == "blocked" \
        and lg.events[-1]["reason"] == pmc.REASON
    # 卡已转 EXPIRED 后再跑: 不重复写
    expired = OpportunityCard.from_dict({**card.to_dict(), "state": "expired"})
    loop.opp_svc.all_cards = lambda d: [expired]
    n = len(lg.events)
    loop.premarket_void_check(et(D1, 9, 31))
    assert len(trans) == 1 and len(lg.events) == n


def test_trading_loop_retries_ledger_write_from_record(tmp_path, monkeypatch, cal):
    """记录已写 (上一轮改台账失败) -> 下一轮按记录补作废, 不需要再取价。"""
    from app.services.trading_loop import TradingLoop
    card = _book_card(tmp_path, monkeypatch)
    pmc.run(tmp_path, [card], et(D1, 9, 20), sip=lambda s, d: {"LLY": (97.0, "08:30")})
    loop = TradingLoop.__new__(TradingLoop)
    loop.ledger = _Ledger()
    loop.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    trans = []
    loop.opp_svc = SimpleNamespace(_transition=lambda c, st, why, now: trans.append(st), all_cards=lambda d: [card])
    loop._sync_card_book = lambda: None
    monkeypatch.setattr(pmc, "sip_last_premarket", lambda s, d: pytest.fail("should not refetch"))
    loop.premarket_void_check(et(D1, 9, 40))
    assert trans == [CardState.EXPIRED]


# ---------- 4. 机会板: 作废卡进灰色历史; 裁决 + 美元期望 ----------

def _ledger_stub(monkeypatch, nav=100_000.0, positions=(), events=()):
    lg = SimpleNamespace(open_positions=list(positions), read_events=lambda: list(events), nav=lambda marks: nav)
    from app.services import desk_overview as ov
    monkeypatch.setattr(ov, "_account", lambda loop, data_dir: (lg, None))


def _board(tmp_path, monkeypatch, now, prices):
    _ledger_stub(monkeypatch)
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: prices)
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    monkeypatch.setattr(us_clock, "now", lambda: now)
    return sd.build_live(tmp_path, repo=object(), today=now.date())


def test_board_voided_card_goes_to_history(tmp_path, monkeypatch, cal):
    card = _book_card(tmp_path, monkeypatch)
    pmc.run(tmp_path, [card], et(D1, 9, 20), sip=lambda s, d: {"LLY": (97.0, "08:30")})
    out = _board(tmp_path, monkeypatch, et(D1, 9, 25), {"LLY": (97.0, "premarket")})
    assert out["long"] == []
    h = out["history"][0]
    assert h["symbol"] == "LLY" and h["state"] == "paper_expired" and h["void_reason"] == pmc.REASON and h["pm_price"] == 97.0


def test_board_verdict_order_with_usd_odds(tmp_path, monkeypatch, cal):
    _book_card(tmp_path, monkeypatch)                                    # 区 99.00-99.50 止损 97.50 ATR 2
    out = _board(tmp_path, monkeypatch, et(D1, 8, 0), {"LLY": (100.3, "premarket")})
    c = out["long"][0]
    assert c["verdict"] == "order" and c["verdict_reason"] is None
    o = c["odds"]
    assert o["day"] == 1 and o["phase"] == "pre" and o["dist_atr"] == 0.4 and o["bin"] == "0.25-0.5"    # (100.3 - 99.5) / 2
    cell = sv.odds_table()["cells"]["long|1|pre|0.25-0.5"]
    assert o["fill"] == cell["fill"] and o["ev_r"] == cell["ev"]
    # 计划股数 = min(100000 * 1% / 1.75 = 571, 100000 * 25% / 99.25 = 251) = 251 (名义封顶); 最多亏 251 * 1.75 = $439
    assert o["qty"] == 251 and o["risk_usd"] == 439 and o["ev_usd"] == round(cell["ev"] * 251 * 1.75)
    assert out["risk_pct"] == 0.01


def test_board_premarket_beyond_stop_before_check_is_skip(tmp_path, monkeypatch, cal):
    _book_card(tmp_path, monkeypatch)
    out = _board(tmp_path, monkeypatch, et(D1, 8, 30), {"LLY": (97.0, "premarket")})
    c = out["long"][0]
    assert c["verdict"] == "skip" and c["verdict_reason"] == "premarket_beyond_stop"
    assert c["odds"]["bin"] == "beyond_stop" and c["odds"]["ev_r"] < 0


def test_board_intraday_beyond_stop_still_order(tmp_path, monkeypatch, cal):
    """盘中越止损不作废 (那一格历史期望为正), 检查过没越止损的卡照常下单。"""
    card = _book_card(tmp_path, monkeypatch)
    pmc.run(tmp_path, [card], et(D1, 9, 20), sip=lambda s, d: {"LLY": (98.0, "08:30")})
    out = _board(tmp_path, monkeypatch, et(D1, 11, 0), {"LLY": (97.0, "live")})
    c = out["long"][0]
    assert c["verdict"] == "order" and c["odds"]["phase"] == "am" and c["odds"]["bin"] == "beyond_stop"


def test_board_blocked_is_skip_without_numbers(tmp_path, monkeypatch, cal):
    _book_card(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "_cooldown", lambda events, day: {"LLY"})
    out = _board(tmp_path, monkeypatch, et(D1, 8, 0), {"LLY": (100.0, "premarket")})
    c = out["long"][0]
    assert c["blocked"] == "cooldown" and c["verdict"] == "skip" and c["verdict_reason"] == "cooldown"


def test_board_birth_cell_before_any_new_price(tmp_path, monkeypatch, cal):
    """信号日盘后到第 1 天盘前 04:00 前, 现价还是信号日收盘 -> birth 格 (第 1 天全部卡的结局)。"""
    _book_card(tmp_path, monkeypatch)
    out = _board(tmp_path, monkeypatch, et(SIG_DAY, 20, 0), {})
    o = out["long"][0]["odds"]
    assert o["bin"] == "birth" and o["day"] == 1 and o["ev_r"] == sv.odds_table()["cells"]["long|1|birth"]["ev"]


# ---------- 5. 赔率表与裁决纯函数 ----------

def test_odds_table_covers_every_state():
    cells = sv.odds_table()["cells"]
    for side in ("long", "short"):
        for day in (1, 2):
            for ph in ("pre", "open", "am", "pm"):
                for b in ("gt1", "0.5-1", "0.25-0.5", "0-0.25", "in_zone", "below_zone", "beyond_stop"):
                    c = cells.get(f"{side}|{day}|{ph}|{b}")
                    assert c is not None, (side, day, ph, b)
                    assert 0 <= c["fill"] <= 1 and c["n"] >= 20 and isinstance(c["ev"], float)
    assert cells["long|1|pre|beyond_stop"]["ci"][1] < 0                 # 唯一两段同号的负格
    assert cells["long|1|birth"]["n"] > 2000


def test_dist_bin_and_rounding():
    assert sv.dist_atr("long", 100.0, (99.0, 99.5), 2.0) == 0.25 and sv.dist_bin(0.25) == "0-0.25"
    assert sv.dist_bin(0.26) == "0.25-0.5" and sv.dist_bin(1.01) == "gt1" and sv.dist_bin(0.0) == "in_zone"
    assert sv.dist_bin(-0.25) == "in_zone" and sv.dist_bin(-0.26) == "below_zone" and sv.dist_bin(-1.01) == "beyond_stop"
    assert sv.dist_atr("short", 98.0, (100.5, 101.0), 2.0) == 1.25
    assert sv.dist_atr("long", None, (1, 2), 1) is None and sv.dist_atr("long", 1, (1, 2), 0) is None


@pytest.mark.parametrize("now,day,phase", [
    (datetime(2026, 10, 8, 17, 0), 1, "pre"),     # 信号日盘后 -> 下一交易日第 1 天
    (datetime(2026, 10, 9, 9, 29), 1, "pre"),
    (datetime(2026, 10, 9, 9, 30), 1, "open"),
    (datetime(2026, 10, 9, 10, 30), 1, "am"),
    (datetime(2026, 10, 9, 12, 30), 1, "pm"),
    (datetime(2026, 10, 9, 15, 31), 2, "pre"),    # 15:30 后今天不能成交 -> 下一交易日 (周一) 第 2 天
    (datetime(2026, 10, 10, 11, 0), 2, "pre"),    # 周六
])
def test_state_key(now, day, phase, cal):
    k = sv.state_key("long", SIG_DAY, now.replace(tzinfo=us_clock.tz), price_source="live")
    assert (k["day"], k["phase"]) == (day, phase)


def test_state_key_window_over(cal):
    assert sv.state_key("long", SIG_DAY, datetime(2026, 10, 12, 15, 31, tzinfo=us_clock.tz)) is None


def test_plan_qty_notional_cap_and_bad_inputs():
    assert sv.plan_qty(100_000, 0.005, 0.1, 50.0) == 500             # 风险算 5000 股, 25% 名义封顶 500 股
    for bad in (None, float("nan"), 0, -1, True):
        assert sv.plan_qty(bad, 0.01, 1.0, 10.0) is None
        assert sv.plan_qty(100_000, bad, 1.0, 10.0) is None


def test_annotate_survives_missing_table(monkeypatch):
    monkeypatch.setattr(sv, "odds_table", lambda: {})
    c = {"symbol": "X", "side": "long", "signal_day": "2026-10-08", "state": "pending", "entry_zone": [1, 2], "atr": 1,
         "price": 3, "risk": 0.5, "stop": 0.5}
    sv.annotate([c], datetime(2026, 10, 9, 11, tzinfo=us_clock.tz))
    assert c["verdict"] == "order" and c["odds"] is None


# ---------- 6. 结算 v2 与盘前推送 ----------

def _flat(px, n=13):
    return [(px, px, px, px)] * n


def test_settle_v2_void_from_record_and_from_m30_replay(tmp_path, monkeypatch, cal):
    assert ss.CONTRACT == "swing-settle-v2"
    card = _book_card(tmp_path, monkeypatch)
    pmc.run(tmp_path, [card], et(D1, 9, 20), sip=lambda s, d: {"LLY": (97.0, "08:30")})
    monkeypatch.setattr(ss, "_m30_days", lambda *a, **k: {D1: _flat(99.2), D2: _flat(99.2)})
    assert ss.settle(tmp_path, None, as_of_close=D1)["written"] == 1         # 作废当天就定结局
    row = ss.read_outcomes(tmp_path)[0]
    assert row["status"] == "void" and row["why"] == pmc.REASON and row["pm_source"] == "sip" and row["void_date"] == str(D1)
    # 没有生产记录 (上线前的卡) -> 用已存 30 分钟K 盘前最后一根重放
    t2 = tmp_path / "old"
    _book_card(t2, monkeypatch)
    monkeypatch.setattr(pmc, "premarket_close_m30", lambda data_dir, repo, sym, day: 97.4)
    ss.settle(t2, None, as_of_close=D1)
    assert ss.read_outcomes(t2)[0]["pm_source"] == "m30_replay"


def test_summarize_rows_excludes_void():
    s = ss.summarize_rows([{"status": "void"}, {"status": "no_fill"}, {"status": "closed", "why": "target", "r": 2.0}])
    assert s["cards"] == 2 and s["filled"] == 1


def test_base_rates_rebuilt_with_void_rule():
    b = ss.base_rates()
    assert b["settle_contract"] == "swing-settle-v2" and b["void_cards"]["long"] == 24 and b["void_cards"]["short"] == 0
    assert b["long"]["cards"] == 2326 - 24


def test_preopen_message_has_verdict_and_odds():
    from app.services import premarket_gap as pg
    board = {"long": [
        {"symbol": "AAA", "side": "long", "state": "pending", "entry_zone": [99.0, 99.5], "stop": 97.5, "target": 103.5,
         "verdict": "order", "odds": {"fill": 0.73, "ev_r": 0.17, "ev_usd": 170}},
        {"symbol": "BBB", "side": "long", "state": "pending", "entry_zone": [9.0, 9.5], "stop": 8.5, "target": 11.0,
         "verdict": "skip", "verdict_reason": "premarket_beyond_stop", "odds": None},
        {"symbol": "CCC", "side": "long", "state": "pending", "entry_zone": [9.0, 9.5], "stop": 8.5, "target": 11.0,
         "blocked": "cooldown", "verdict": "skip", "verdict_reason": "cooldown"}], "short": []}
    body = pg.swing_preopen_message(board, {"gaps": {"AAA": -1.2}}, D1, et(D1, 9, 0))["body"]
    assert "AAA 多 下单 挂单 99.00-99.50 止损 97.50 目标 103.50 盘前 -1.2% · 回区 73% 期望 +$170" in body
    assert "BBB 多 不下单 盘前越止损 挂单" in body and "CCC 多 不下单 平仓冷却 挂单" in body


def test_scheduler_registers_pm_check_job(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    from app.services import ops_watch as ow
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    job = sched.get_job("zen_swing_pm_check")
    assert job is not None and "zen_swing_pm_check" in ow.WATCHED and "zen_swing_pm_check" in ow.CATCHUP
    fire = job.trigger.get_next_fire_time(None, datetime(2026, 10, 9, 0, 0, tzinfo=us_clock.tz))
    assert fire.astimezone(us_clock.tz).strftime("%H:%M") == "09:20"


def test_account_reset_archives_pm_check():
    from app.services import account_reset
    assert "opportunities/pm_check" in account_reset.ARCHIVE_ITEMS


def test_trading_loop_check_never_raises(tmp_path, monkeypatch, cal):
    """检查出错不抛 (交易环其它步骤照跑); 还没查过的第 1 天做多卡由准入 pending 单独拦。"""
    from app.services.trading_loop import TradingLoop
    loop = TradingLoop.__new__(TradingLoop)
    loop.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    loop.opp_svc = SimpleNamespace(all_cards=lambda d: (_ for _ in ()).throw(RuntimeError("x")))
    assert loop.premarket_void_check(et(D1, 9, 20)) == {"error": "RuntimeError"}


def test_funnel_labels_new_reasons():
    from app.services import entry_funnel
    assert entry_funnel.label(pmc.REASON) == "盘前越过止损已取消"
    assert entry_funnel.label(pmc.PENDING) == "盘前越止损检查未完成"


# ---------- 7. 审查 B1/R2: 交易环不出网、不等锁; SIP 慢返回按返回时刻兜底; 08:30 那根没到先不写 ----------

def test_trader_path_never_calls_sip(tmp_path, monkeypatch, cal):
    card = _book_card(tmp_path, monkeypatch)
    boom = lambda s, d: pytest.fail("trader path must not call SIP")         # noqa: E731
    assert pmc.run(tmp_path, [card], et(D1, 9, 35), sip=boom, allow_sip=False)["skipped"] == "scheduled_only"
    r = pmc.run(tmp_path, [card], et(D1, 9, 44), sip=boom, fallback=lambda s, d: {"LLY": 97.0}, allow_sip=False)
    assert r["void"] == [card.id] and pmc.load(tmp_path, D1)[card.id]["source"] == "hl_0900"


def test_trader_path_skips_when_lock_busy(tmp_path, monkeypatch, cal):
    card = _book_card(tmp_path, monkeypatch)
    pmc._LOCK.acquire()
    try:
        r = pmc.run(tmp_path, [card], et(D1, 9, 50), fallback=lambda s, d: {"LLY": 97.0}, allow_sip=False)
    finally:
        pmc._LOCK.release()
    assert r["skipped"] == "busy" and not pmc.check_path(tmp_path, D1).exists()


def test_slow_sip_error_uses_time_after_return(tmp_path, monkeypatch, cal):
    """09:43:30 发起, SIP 超时到 09:45 才报错 -> 按返回时刻已过 09:44, 直接兜底写记录, 不再等下一轮。"""
    card = _book_card(tmp_path, monkeypatch)
    clock = {"now": et(D1, 9, 43)}
    monkeypatch.setattr(us_clock, "now", lambda: clock["now"])

    def slow(s, d):
        clock["now"] = et(D1, 9, 45)
        raise TimeoutError("x")
    r = pmc.run(tmp_path, [card], et(D1, 9, 43), sip=slow, fallback=lambda s, d: {"LLY": 98.0})
    assert r["checked"] == 1 and pmc.load(tmp_path, D1)[card.id]["source"] == "hl_0900"


def test_last_bar_before_0830_waits_until_0940(tmp_path, monkeypatch, cal):
    card = _book_card(tmp_path, monkeypatch)
    monkeypatch.setattr(us_clock, "now", lambda: et(D1, 9, 20))
    r = pmc.run(tmp_path, [card], et(D1, 9, 20), sip=lambda s, d: {"LLY": (97.0, "08:00")})
    assert r["checked"] == 0 and r["pending"] == 1 and not pmc.check_path(tmp_path, D1).exists()
    monkeypatch.setattr(us_clock, "now", lambda: et(D1, 9, 40))
    r = pmc.run(tmp_path, [card], et(D1, 9, 40), sip=lambda s, d: {"LLY": (97.0, "08:00")})
    assert r["void"] == [card.id] and pmc.load(tmp_path, D1)[card.id]["bar_at"] == "08:00"


def test_scheduler_runs_pm_check_three_times(tmp_path, monkeypatch):
    from datetime import timedelta
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    job = sched.get_job("zen_swing_pm_check")
    t, fires = datetime(2026, 10, 9, 0, 0, tzinfo=us_clock.tz), []
    for _ in range(3):
        f = job.trigger.get_next_fire_time(None, t)
        fires.append(f.astimezone(us_clock.tz).strftime("%H:%M"))
        t = f + timedelta(seconds=1)
    assert fires == ["09:20", "09:30", "09:40"]
