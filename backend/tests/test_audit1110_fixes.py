"""2026-11-10 只读审计确认的 9 条缺陷的复现测试 (先红后修, 修复提交不改本文件)。

 1. 交易环取卡: 每个历史卡文件都把整份台账重新解析一遍, 且每次都重读早已全部了结的旧卡文件 (单轮耗时随运行天数平方增长)。
 2. IEX 交叉核对没有时间上限, 主源报价已到手仍被拖住整轮。
 3. 主源正常返回但一条可用报价都没有时不回退 IEX。
 4. 持仓重大利空提醒: 冷却过后总是取窗口内最早一条, 已推过 → 入队被忽略却照样计数并刷新冷却。
 5. 被撮合层按容量拦下的卡仍是 ACTIVE, 价在区内时每两帧往台账写一对 待成交 / 已拦截 意向。
 6. 簿里冻结的 3+3 在做空为空时不生效 (开源核心版做空默认就是空)。
 7. 内置日线方向门: 研究回放矩阵用前一日收盘, 盘后扫描用信号日收盘。
 8. 日历拉取失败不做退避, 每次判断交易日都重试并打一条告警。
 9. 发布不到 3 分钟的新闻 (age_hours 取整为 0.0) 被当成「没有时间」。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.market_clock.clock import US_SESSION_CLOSE, US_SESSION_OPEN, US_TZ, MarketClock, Session
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult
from app.services import decision_scan_job as job
from app.services import entry_guard, news_job
from app.services import news_ingest as ni
from app.services import news_store as ns
from app.services import strategy_evaluation as se
from app.services import swing_desk as sd
from app.services import trading_loop as tl
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv
from app.services.opportunity_service import OpportunityService
from app.services.trading_loop import TradingLoop

SIG_DAY = date(2026, 10, 8)        # 周四
TODAY = date(2026, 10, 9)          # 周五


@pytest.fixture(autouse=True)
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def _mode(tmp_path, mode="v4"):
    d = tmp_path / "decision_desk"
    d.mkdir(parents=True, exist_ok=True)
    (d / "evidence_mode.json").write_text(json.dumps({"mode": mode}))
    se._V4_CACHE.clear()
    wv._STATUS_CACHE.clear()
    wv._PM_CACHE.clear()
    wb._ENSURE_KEY.clear()
    wb._ENSURE_RETRY.clear()
    wb._LOAD_CACHE.clear()


def _desk_card(sym, day, state=CardState.WATCH):
    return OpportunityCard(id=OpportunityCard.make_id(sym, "strategy:trend_breakout", day, 1), symbol=sym,
                           setup_type="strategy:trend_breakout", created_date=day, entry_zone=(100.0, 101.0),
                           invalidation=95.0, target_zone=(110.0, 112.0), risk_per_share=5.5, reward_risk=2.0,
                           evidence={"sector": "Technology", "decision": {"tab": "watchlist", "sources": ["trend_breakout"]}},
                           state=state)


def _book_card(sym="NVDA", day=SIG_DAY, state=CardState.OPEN, rule="macd_golden"):
    return OpportunityCard(id=wb.card_id(sym, rule, "long", day), symbol=sym, setup_type=f"strategy:{rule}",
                           created_date=day, entry_zone=(99.5, 100.5), invalidation=97.0, target_zone=(106.0, 106.0),
                           risk_per_share=3.0, reward_risk=2.0,
                           evidence={"book": True, "decision": {"book": True, "tab": "watchlist", "tier": "rule",
                                                                "risk_pct": 0.01, "sides": ["long"],
                                                                "tradable_sources": [rule]}},
                           state=state)


# ── 1. 取卡: 台账只解析一次, 早已终结的旧卡文件不读 ─────────────────────────────────

def _svc(tmp_path):
    svc = OpportunityService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    lg = Ledger(tmp_path / "ledger")
    lg.init_account(Account())
    lg.replay(Account())
    svc.bind_ledger(lg)
    return svc, lg


def _count(monkeypatch, obj, name):
    calls = []
    real = getattr(obj, name)

    def wrapper(*a, **k):
        calls.append(a)
        return real(*a, **k)
    monkeypatch.setattr(obj, name, wrapper)
    return calls


def test_all_cards_parses_ledger_once_and_skips_long_dead_card_files(tmp_path, monkeypatch):
    svc, lg = _svc(tmp_path)
    as_of = date(2026, 11, 9)
    recent = [date(2026, 11, d) for d in (2, 3, 4, 5, 6)]
    old = [date(2026, 8, 3), date(2026, 8, 4)]                      # 距今 > 40 个自然日, 且卡已全部了结
    for i, d in enumerate(recent + old):
        svc.save_scan(ScanResult(as_of=d, gate="green", params_revision=3, scanned=1, watch=[_desk_card(f"S{i}", d)]))
    for i, d in enumerate(old, start=len(recent)):                   # 旧卡早已到期 (与生产同形)
        c = _desk_card(f"S{i}", d)
        lg.append("opportunity", {"card_id": c.id, "symbol": c.symbol, "state": "expired", "reason": "5个交易日入场窗结束",
                                  "trade_date": (d + timedelta(days=7)).isoformat()}, at=f"{d + timedelta(days=7)}T16:00:00-04:00")
    c0 = _desk_card("S0", recent[0])
    lg.append("opportunity", {"card_id": c0.id, "symbol": "S0", "state": "expired", "reason": "5个交易日入场窗结束",
                              "trade_date": "2026-11-09"}, at="2026-11-09T16:00:00-05:00")
    wb.save_day(tmp_path, {"as_of": "2026-11-06", "as_of_close": "2026-11-06", "cards": [_book_card("NVDA", date(2026, 11, 6)).to_dict()],
                           "skipped": [], "contract": "swing-desk-tight-v1"})
    svc.all_cards(as_of)                                             # 第一次: 旧文件各看一眼, 记下里面的卡已全部了结
    parses = _count(monkeypatch, lg, "read_events")
    reads = _count(monkeypatch, svc, "_raw_cards")
    cards = svc.all_cards(as_of)
    assert len(parses) == 1                                          # 旧实现: 每个卡文件 + 下单簿各解析一遍 = 8 次
    assert sorted(a[0] for a in reads) == recent                     # 全部了结的旧卡文件不再读
    by = {c.symbol: c for c in cards}
    assert set(by) == {"S0", "S1", "S2", "S3", "S4", "NVDA"}
    assert by["S0"].state == CardState.EXPIRED and by["S1"].state == CardState.WATCH       # 台账状态照常叠加


def test_all_cards_still_loads_an_old_card_file_that_backs_an_open_position(tmp_path):
    """旧文件里还有没了结的卡 (这里是仍在持有的) 就照旧读: 卡册里必须有它 (止损 / 目标 / 时间止损读卡)。"""
    svc, lg = _svc(tmp_path)
    old_day = date(2026, 8, 3)
    held = _desk_card("MU", old_day, state=CardState.OPEN)
    svc.save_scan(ScanResult(as_of=old_day, gate="green", params_revision=3, scanned=2, cards=[held],
                             watch=[_desk_card("AMD", old_day)]))
    svc.save_scan(ScanResult(as_of=date(2026, 11, 6), gate="green", params_revision=3, scanned=1,
                             watch=[_desk_card("NVDA", date(2026, 11, 6))]))
    lg.append("fill", {"symbol": "MU", "side": "buy", "qty": 10, "price": 100.0, "fee": 1.0, "card_id": held.id,
                       "stop_price": 95.0, "trade_date": "2026-08-04"}, at="2026-08-04T10:00:00-04:00")
    by = {c.id: c for c in svc.all_cards(date(2026, 11, 9))}
    assert by[held.id].state == CardState.FILLED
    assert {c.symbol for c in by.values()} == {"MU", "AMD", "NVDA"}


# ── 2 / 3. 执行报价: 交叉核对有时间上限; 主源为空回退 IEX ───────────────────────────

def _bare_loop(tmp_path):
    return TradingLoop(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), Account())


def _quote(sym, feed, price=100.0):
    now = us_clock.now()
    return {"symbol": sym, "price": price, "feed": feed, "timestamp": now.isoformat(), "open": 100.0,
            "open_timestamp": now.isoformat()}


def test_iex_cross_check_has_a_time_budget_and_does_not_queue_behind_a_stuck_request(tmp_path, monkeypatch):
    loop = _bare_loop(tmp_path)
    monkeypatch.setattr(tl, "CROSS_CHECK_BUDGET_S", 0.3, raising=False)
    release, calls = threading.Event(), []

    def slow_iex(symbols):
        calls.append(tuple(symbols))
        release.wait(4)
        return [_quote(s, "iex") for s in symbols]
    monkeypatch.setattr(loop, "_iex_provider", lambda: SimpleNamespace(get_execution_quotes=slow_iex))
    primary = SimpleNamespace(name="tickflow", get_execution_quotes=lambda s: [_quote(x, "tickflow") for x in s])
    try:
        t0 = time.monotonic()
        quotes, rejected = loop._execution_quotes(primary, ["NVDA"])
        assert time.monotonic() - t0 < 2.0                           # 旧实现: 等 IEX 返回 (4 秒)
        assert [q["symbol"] for q in quotes] == ["NVDA"] and rejected == []
        t1 = time.monotonic()
        quotes, _ = loop._execution_quotes(primary, ["NVDA"])        # 上一轮的 IEX 请求还没回来
        assert time.monotonic() - t1 < 1.0 and len(quotes) == 1
        assert len(calls) == 1                                       # 不在单线程池里再排一个
    finally:
        release.set()


def test_primary_returning_no_usable_quotes_falls_back_to_iex(tmp_path, monkeypatch):
    loop = _bare_loop(tmp_path)
    monkeypatch.setattr(loop, "_iex_provider",
                        lambda: SimpleNamespace(get_execution_quotes=lambda s: [_quote(x, "iex", 111.0) for x in s]))
    primary = SimpleNamespace(name="tickflow", get_execution_quotes=lambda s: [])     # 200 但报价全部过期被滤掉
    quotes, rejected = loop._execution_quotes(primary, ["HOLD"])
    assert [(q["symbol"], q["feed"], q["price"]) for q in quotes] == [("HOLD", "iex", 111.0)] and rejected == []
    assert loop.execution_source["active"] == "iex_fallback" and loop.trader.quote_feed == "iex"


def test_primary_and_iex_both_empty_stays_empty(tmp_path, monkeypatch):
    loop = _bare_loop(tmp_path)
    monkeypatch.setattr(loop, "_iex_provider", lambda: SimpleNamespace(get_execution_quotes=lambda s: []))
    primary = SimpleNamespace(name="tickflow", get_execution_quotes=lambda s: [])
    assert loop._execution_quotes(primary, ["HOLD"]) == ([], [])


# ── 4 / 9. 新闻 ────────────────────────────────────────────────────────────────

T0 = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)


def _news(rid, published, sym="NVDA"):
    return {"id": rid, "created_at": published.strftime("%Y-%m-%dT%H:%M:%SZ"), "headline": f"H {rid}", "zh_headline": f"标题 {rid}",
            "zh_summary": "", "symbols": [sym], "mentions": {sym: "headline"}, "sources": ["alpaca"], "refs": [], "scope": "focused",
            "tags": {sym: {"relevance": "primary", "event_type": "regulatory", "direction": "bearish", "importance": "high",
                           "rehash": False}},
            "tagged_by": {"mode": "llm", "prompt_version": "news-tag-v1"}}


def _save_news(tmp_path, rows):
    ns._SHARD_INDEX.clear()
    ni.save_shard(tmp_path, T0, {r["id"]: r for r in rows})


def _outbox_news_keys(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "notifications" / "outbox.sqlite3")
    try:
        return sorted(k for (k,) in db.execute("select key from outbox where key like 'news:%'"))
    finally:
        db.close()


def _cooldown_stamp(tmp_path):
    return json.loads((tmp_path / "news" / "push_state.json").read_text())["NVDA"]


def test_news_alert_pushes_the_newest_unpushed_event_after_cooldown(tmp_path):
    a = _news("evt-A", T0)
    _save_news(tmp_path, [a])
    assert news_job.notify_holdings(tmp_path, {"NVDA"}, now=T0 + timedelta(minutes=10)) == 1
    assert _outbox_news_keys(tmp_path) == ["news:evt-A:NVDA"]
    stamp = _cooldown_stamp(tmp_path)
    # 冷却期内又出了两个不同事件 B、C: 冷却过后推最新的一条 (C), 而不是再去「推」已经推过的 A
    b, c = _news("evt-B", T0 + timedelta(hours=5)), _news("evt-C", T0 + timedelta(hours=11))
    _save_news(tmp_path, [a, b, c])
    assert news_job.notify_holdings(tmp_path, {"NVDA"}, now=T0 + timedelta(hours=11, minutes=30)) == 0    # 12 小时内最多一次
    assert _cooldown_stamp(tmp_path) == stamp
    assert news_job.notify_holdings(tmp_path, {"NVDA"}, now=T0 + timedelta(hours=12, minutes=30)) == 1
    assert _outbox_news_keys(tmp_path) == ["news:evt-A:NVDA", "news:evt-C:NVDA"]


def test_news_alert_without_a_new_event_neither_counts_nor_refreshes_cooldown(tmp_path):
    a = _news("evt-A", T0)
    _save_news(tmp_path, [a])
    assert news_job.notify_holdings(tmp_path, {"NVDA"}, now=T0 + timedelta(minutes=10)) == 1
    stamp = _cooldown_stamp(tmp_path)
    # 旧实现: 冷却一过就把 A 再「推」一次 —— 入队被忽略, 却返回 1 并把冷却再刷 12 小时, 之后的新事件被它挡住
    assert news_job.notify_holdings(tmp_path, {"NVDA"}, now=T0 + timedelta(hours=13)) == 0
    assert _cooldown_stamp(tmp_path) == stamp and _outbox_news_keys(tmp_path) == ["news:evt-A:NVDA"]
    b = _news("evt-B", T0 + timedelta(hours=20))
    _save_news(tmp_path, [a, b])
    assert news_job.notify_holdings(tmp_path, {"NVDA"}, now=T0 + timedelta(hours=20, minutes=12)) == 1
    assert _outbox_news_keys(tmp_path) == ["news:evt-A:NVDA", "news:evt-B:NVDA"]


def test_news_alert_does_not_back_push_reports_published_before_the_last_push(tmp_path):
    """同一事件多家媒体: 推送时已在窗口里的其它报道 (已计入「同类报道共 N 条」) 冷却后不补推。"""
    rows = [_news("evt-1", T0), _news("evt-2", T0 + timedelta(minutes=20)), _news("evt-3", T0 + timedelta(minutes=40))]
    _save_news(tmp_path, rows)
    assert news_job.notify_holdings(tmp_path, {"NVDA"}, now=T0 + timedelta(hours=1)) == 1
    assert _outbox_news_keys(tmp_path) == ["news:evt-1:NVDA"]         # 首次仍推最早一条 (事件首发)
    assert news_job.notify_holdings(tmp_path, {"NVDA"}, now=T0 + timedelta(hours=14)) == 0
    assert news_job.notify_holdings(tmp_path, {"NVDA"}, now=T0 + timedelta(hours=27)) == 0
    assert _outbox_news_keys(tmp_path) == ["news:evt-1:NVDA"]


def test_news_published_within_minutes_counts_in_summary_and_push(tmp_path):
    _save_news(tmp_path, [_news("evt-fresh", T0)])
    now = T0 + timedelta(minutes=1)
    v = ns.for_symbol(tmp_path, "NVDA", now=now)[0]
    assert v["age_hours"] == 0.0
    s = ns.summarize([v])
    assert s["high_bearish_48h"] is True and [x["id"] for x in s["top"]] == ["evt-fresh"]
    assert news_job.notify_holdings(tmp_path, {"NVDA"}, now=now) == 1


def test_summary_ranks_a_minutes_old_item_ahead_of_an_older_one(tmp_path):
    _save_news(tmp_path, [_news("evt-old", T0 - timedelta(hours=30)), _news("evt-fresh", T0)])
    s = ns.summarize(ns.for_symbol(tmp_path, "NVDA", now=T0 + timedelta(minutes=1)))
    assert [x["id"] for x in s["top"]] == ["evt-fresh", "evt-old"]    # 同重要度同相关度按新旧; 旧实现把 0.0 小时排到最后


# ── 5. 撮合层容量拦截 → 卡转回观察 ──────────────────────────────────────────────

HELD = ["XOM", "JNJ", "JPM", "PG", "CAT", "NEE"]
SECTORS = {"NVDA": "Technology", "XOM": "Energy", "JNJ": "Health Care", "JPM": "Financials", "PG": "Consumer Staples",
           "CAT": "Industrials", "NEE": "Utilities"}
START = datetime(2026, 10, 9, 10, 0, tzinfo=us_clock.tz)


def _book_loop(tmp_path, monkeypatch):
    """v4 下单簿里一张 NVDA 做多卡 (1% 风险 → 持仓上限 6), 账户已有 6 个昨日开的仓 (各在不同板块)。"""
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA"})
    clock = [START]
    monkeypatch.setattr(us_clock, "now", lambda: clock[0])
    monkeypatch.setattr(us_clock, "today", lambda: clock[0].date())
    syms = list(SECTORS)
    days, d = [], SIG_DAY
    while len(days) < 150:
        if d.weekday() < 5:
            days.append(d)
        d = date.fromordinal(d.toordinal() - 1)
    full = pl.DataFrame([{"symbol": s, "date": x, "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 5e6,
                          "amount": 5e8} for s in syms for x in sorted(days)])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=lambda: pl.DataFrame({"symbol": syms, "sector": [SECTORS[s] for s in syms]}),
                           get_daily=lambda s, a, b, columns=None: full.filter(pl.col("symbol") == s),
                           get_daily_batch=lambda ss, a, b, columns=None: full.filter(
                               pl.col("symbol").is_in(list(ss)) & (pl.col("date") <= b)))
    loop = TradingLoop(repo, Account())
    monkeypatch.setattr(loop, "_enqueue_events", lambda: None)
    loop.boot()
    loop.opp_svc.save_scan(ScanResult(as_of=SIG_DAY, gate="green", params_revision=3, scanned=1))
    wb.save_day(tmp_path, {"as_of": SIG_DAY.isoformat(), "as_of_close": SIG_DAY.isoformat(), "cards": [_book_card().to_dict()],
                           "skipped": [], "contract": "swing-desk-tight-v1"})
    for s in HELD:
        loop.ledger.append("fill", {"symbol": s, "side": "buy", "qty": 10, "price": 100.0, "fee": 1.0, "stop_price": 99.0,
                                    "trade_date": SIG_DAY.isoformat()}, at=f"{SIG_DAY.isoformat()}T10:00:00-04:00")

    def frame(i, px=100.0):
        clock[0] = START + timedelta(seconds=15 * i)
        o = clock[0].replace(hour=9, minute=30, second=0).isoformat()
        q = {"NVDA": {"symbol": "NVDA", "price": px, "feed": "tickflow", "timestamp": clock[0].isoformat(), "open": 100.2,
                      "open_timestamp": o}}
        return loop.intraday_fill_check(q, entry_symbols={"NVDA"})
    return loop, frame, clock


def _card_events(loop):
    cid = _book_card().id
    return [e for e in loop.ledger.read_events() if e.get("card_id") == cid]


def test_capacity_blocked_card_returns_to_watch_and_stops_writing_intents(tmp_path, monkeypatch):
    loop, frame, clock = _book_loop(tmp_path, monkeypatch)
    frame(0)                                                                         # 入区: 登记待成交意向
    frame(1)                                                                         # 撮合: 持仓数已满 → 拦下
    assert any(b.get("reason") == "max_positions" and b.get("card_id") == _book_card().id for b in loop.entry_blockers)
    for i in range(2, 8):
        frame(i)
    evs = _card_events(loop)
    intents = [(e["status"], e.get("reason")) for e in evs if e["kind"] == "entry_intent"]
    assert intents == [("pending", None), ("blocked", "max_positions")]              # 旧实现: 每两帧一对, 8 帧 4 对
    states = [(e["state"], e["reason"]) for e in evs if e["kind"] == "opportunity"]
    assert states[-1] == ("watch", "准入阻断：max_positions")  # noqa: RUF001
    n = len(loop.ledger.read_events())
    for i in range(8, 20):
        frame(i)
    assert len(loop.ledger.read_events()) == n                                       # 同一拦截状态不再落任何事件
    # 平掉一只 → 名额恢复; 价仍在区内 → 重新激活, 下一帧成交
    loop.ledger.append("close", {"symbol": "XOM", "qty": 10, "price": 100.0, "fee": 1.0, "reason": "manual",
                                 "trade_date": TODAY.isoformat()}, at=clock[0].isoformat())
    for i in range(20, 24):
        frame(i)
    evs = _card_events(loop)
    assert ("active", "观察卡入区且组合容量恢复") in [(e.get("state"), e.get("reason")) for e in evs if e["kind"] == "opportunity"]
    assert [e["symbol"] for e in evs if e["kind"] == "fill"] == ["NVDA"]


def test_watch_card_blocked_by_fill_layer_waits_for_a_capacity_release_event(tmp_path, monkeypatch):
    """生命周期的「容量恢复」只看持仓数与组合风险, 不看现金 / 股数为 0 —— 这类拦截必须等到真有释放容量的事件
    (平仓 / 减仓 / 移动止损) 才重新激活, 否则转回观察后下一帧又被激活、再被拦, 事件比修复前还多。"""
    loop, _, clock = _book_loop(tmp_path, monkeypatch)
    card = _book_card()
    now = START + timedelta(minutes=1)
    clock[0] = now
    lg = loop.ledger
    lg.append("close", {"symbol": "XOM", "qty": 10, "price": 100.0, "fee": 1.0, "reason": "manual",
                        "trade_date": TODAY.isoformat()}, at=START.isoformat())          # 名额有空 (5 < 6)
    lg.append("opportunity", {"card_id": card.id, "symbol": "NVDA", "state": "active", "reason": "次交易日开盘确认有效",
                              "trade_date": TODAY.isoformat()}, at=START.isoformat())
    intent = {"symbol": "NVDA", "card_id": card.id, "price": 100.0, "quote_ts": START.isoformat(),
              "trade_date": TODAY.isoformat(), "proposal": card.to_dict()}
    lg.append("entry_intent", {**intent, "status": "pending"}, at=START.isoformat())
    lg.append("entry_intent", {**intent, "status": "blocked", "reason": "cash"}, at=(START + timedelta(seconds=15)).isoformat())
    lg.append("opportunity", {"card_id": card.id, "symbol": "NVDA", "state": "watch", "reason": "准入阻断：cash",  # noqa: RUF001
                              "trade_date": TODAY.isoformat()}, at=(START + timedelta(seconds=15)).isoformat())
    o = now.replace(hour=9, minute=30, second=0).isoformat()
    quotes = {"NVDA": {"symbol": "NVDA", "price": 100.0, "feed": "tickflow", "timestamp": now.isoformat(), "open": 100.2,
                       "open_timestamp": o}}
    n = len(lg.read_events())
    loop.opp_svc.advance_lifecycle(now, quotes, book_only=True)
    assert len(lg.read_events()) == n                                                # 旧实现: 立刻「观察卡入区且组合容量恢复」
    lg.append("stop_move", {"symbol": "JNJ", "stop_price": 99.5, "trade_date": TODAY.isoformat()}, at=now.isoformat())
    loop.opp_svc.advance_lifecycle(now, quotes, book_only=True)
    last = lg.read_events()[-1]
    assert (last["kind"], last.get("card_id"), last.get("state")) == ("opportunity", card.id, "active")


def test_intent_index_capacity_block_only_covers_same_day_fill_layer_reasons(tmp_path):
    lg = Ledger(tmp_path / "ledger")
    lg.init_account(Account())
    base = {"symbol": "NVDA", "price": 100.0, "quote_ts": START.isoformat(), "trade_date": TODAY.isoformat(), "proposal": {}}
    lg.append("entry_intent", {**base, "card_id": "cap", "status": "blocked", "reason": "portfolio_risk"}, at=START.isoformat())
    lg.append("entry_intent", {**base, "card_id": "adm", "status": "blocked", "reason": "direction_conflict"}, at=START.isoformat())
    lg.append("entry_intent", {**base, "card_id": "pend", "status": "pending"}, at=START.isoformat())
    idx = entry_guard.IntentIndex(lg)
    assert idx.capacity_blocked("cap", TODAY) is True
    assert idx.capacity_blocked("adm", TODAY) is False and idx.capacity_blocked("pend", TODAY) is False
    assert idx.capacity_blocked("cap", TODAY + timedelta(days=3)) is False            # 换了交易日: 重新按容量检查
    assert idx.capacity_blocked("none", TODAY) is False
    lg.append("fill", {"symbol": "XOM", "side": "buy", "qty": 10, "price": 100.0, "fee": 1.0, "stop_price": 99.0}, at=START.isoformat())
    lg.append("partial_close", {"symbol": "XOM", "qty": 5, "price": 100.0, "fee": 1.0}, at=START.isoformat())
    assert entry_guard.IntentIndex(lg).capacity_blocked("cap", TODAY) is False        # 之后有减仓 = 容量可能已恢复


# ── 6. 簿里冻结的 3+3 ───────────────────────────────────────────────────────────

def test_rules_for_day_uses_frozen_rules_when_frozen_short_side_is_empty(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "swing.json").write_text(json.dumps({"long": ["macd_golden"], "short": []}))
    wb.book_dir(tmp_path).mkdir(parents=True)
    wb.book_path(tmp_path, SIG_DAY).write_text(json.dumps(
        {"as_of": SIG_DAY.isoformat(), "cards": [], "swing_rules": {"long": ["volume_price_surge"], "short": []}}))
    assert sd.rules_for_day(tmp_path, SIG_DAY) == (("volume_price_surge",), ())       # 旧实现: 退回当前 swing.json
    assert sd.rules_for_day(tmp_path, TODAY)[0] == ("macd_golden",)                   # 还没落簿的日子仍读当前配置


# ── 7. 内置日线方向门: 回放矩阵 = 盘后扫描 ──────────────────────────────────────────

def test_daily_ma50_matrix_matches_the_live_state_on_the_signal_day():
    from app.opportunity import direction_gate as dg
    days, d = [], date(2026, 1, 5)
    while len(days) < 80:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    closes = [100.0] * 78 + [90.0, 120.0]              # 倒数第 2 天跌破均线, 信号日 T 收盘站回均线上方
    frame = pl.DataFrame({"symbol": ["X"] * 80, "date": days, "close": closes})
    repo = SimpleNamespace(get_daily_batch=lambda syms, a, b, columns=None: frame.filter(
        (pl.col("date") >= a) & (pl.col("date") <= b)))
    t = days[-1]
    live = dg._daily_ma50_states(dg.GateContext(data_dir=None, repo=None, as_of=t, symbols=("X",), frames={"X": frame}))
    mat = dg._daily_ma50_matrix(None, repo, ["X"], days[-3:])
    assert live == {"X": "long"}
    assert bool(mat[-1, 0]) is True                    # 旧实现: 用 T-1 收盘 (90 < MA50) → 不放行, 与扫描差一天
    assert bool(mat[-2, 0]) is False                   # T-1 那一行: 当天收盘 90 在均线下
    assert mat.dtype == np.bool_


# ── 8. 日历拉取失败: 退避, 不逐次重试与告警 ────────────────────────────────────────

def test_calendar_failure_is_backed_off_instead_of_retried_on_every_call(caplog):
    calls = []

    def failing(start, end):
        calls.append((start, end))
        raise RuntimeError("alpaca credentials not configured")
    clock = MarketClock(tz=US_TZ, sessions=[Session(US_SESSION_OPEN, US_SESSION_CLOSE)], calendar_provider=failing)
    with caplog.at_level(logging.WARNING, logger="app.market_clock.clock"):
        results = [clock.is_trading_day(date(2026, 11, 9)) for _ in range(200)]
        between = clock.trading_days_between(date(2026, 11, 9), date(2026, 11, 13))
    assert all(results) and len(between) == 5                         # 仍按工作日放行 (fail-open 不变)
    assert len(calls) == 1                                            # 旧实现: 201 次
    assert len([r for r in caplog.records if "market calendar fetch failed" in r.getMessage()]) == 1


def test_calendar_recovers_after_the_backoff_window(monkeypatch):
    import app.market_clock.clock as clockmod
    state = {"fail": True, "calls": 0}

    def provider(start, end):
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("network down")
        return [date(2026, 11, 9)]
    clock = MarketClock(tz=US_TZ, sessions=[Session(US_SESSION_OPEN, US_SESSION_CLOSE)], calendar_provider=provider)
    assert clock.is_trading_day(date(2026, 11, 10)) is True          # 失败 → 工作日放行
    state["fail"] = False
    assert clock.is_trading_day(date(2026, 11, 10)) is True and state["calls"] == 1      # 退避期内不重试
    monkeypatch.setattr(clockmod, "CALENDAR_FAIL_BACKOFF_S", 0.0, raising=False)
    assert clock.is_trading_day(date(2026, 11, 10)) is False and state["calls"] == 2     # 退避过后恢复真实日历
