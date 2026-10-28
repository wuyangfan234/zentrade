"""0.35.2 交易环瘦身 (无限 10-23「交易环要不要瘦身, 如果不影响其他系统可以瘦身」): v4 下交易环只处理下单簿卡 + 有持仓的卡。

10-22 生产: 2685 张卡里 1291 张是旧决策台观察卡 (v4 下准入恒 v4_book_only, 永不开仓), 交易环每 15 秒为它们
复验日K (5-8 秒, 一只票缺日K 让缓存永远不命中)、跑准入 (1291 次读许可 3.2 秒)、取 31 只报价; 一轮中位 11.7 秒, 跳过 34 轮。

不变量:
  1. v4: 复验 / 生命周期 / 执行池 / 卡册 只含下单簿卡 + 持仓卡; 旧卡状态不在交易环里改 (到期交给收盘后的 advance_lifecycle)。
  2. 非 v4 (v1/v2/v3): 调用形状与 0.35.1 相同, 全量处理 (测试桩不收 book_only 也照跑)。
  3. 旧卡上有持仓 (历史持仓) 照旧管理: 卡册里有它, 止损 / 目标 / 时间止损按卡执行。
  4. 收盘后 advance_lifecycle (不带 book_only) 仍处理旧卡到期; 新闻覆盖 (news_job.universe) 读 all_cards, 不受影响。
  5. 公司行动早上覆盖 = 执行池 + 自选 (执行池收窄后, 自选里盘中才出卡的票不必等补拉)。
"""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.ledger.core import Account
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult
from app.services import decision_scan_job as job
from app.services import watchlist_book as wb
from app.services.trading_loop import TradingLoop

SIG_DAY = date(2026, 10, 8)
TODAY = date(2026, 10, 9)


@pytest.fixture(autouse=True)
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def _mode(tmp_path, mode="v4"):
    import json
    d = tmp_path / "decision_desk"
    d.mkdir(parents=True, exist_ok=True)
    (d / "evidence_mode.json").write_text(json.dumps({"mode": mode}))


def _old_card(sym, state=CardState.WATCH, day=SIG_DAY):
    """旧决策台卡 (opportunities/cards 的 watch 列表): 没有 book 标记。"""
    return OpportunityCard(id=f"opp-{day.isoformat()}-{sym}-strategy-ibs_mean_reversion-x", symbol=sym,
                           setup_type="strategy:ibs_mean_reversion", created_date=day, entry_zone=(100.0, 101.0),
                           invalidation=95.0, target_zone=(110.0, 112.0), risk_per_share=5.5, reward_risk=2.0,
                           evidence={"sector": "Technology", "decision": {"tab": "watchlist", "sources": ["ibs_mean_reversion"]}},
                           state=state)


def _book_card(sym, day=SIG_DAY):
    return OpportunityCard(id=wb.card_id(sym, "swing", "long", day), symbol=sym, setup_type="strategy:swing",
                           created_date=day, entry_zone=(97.0, 98.0), invalidation=95.0, target_zone=(104.0, 104.0),
                           risk_per_share=2.5, reward_risk=2.4,
                           evidence={"book": True, "decision": {"book": True, "tab": "watchlist", "tier": "rule",
                                                                "sides": ["long"], "entry_days": 2, "limit_pullback": True}},
                           state=CardState.OPEN)


def _repo(tmp_path, syms):
    days, d = [], SIG_DAY                                         # 截至上一交易日 (复验只用已收盘日K) 的 150 个工作日
    while len(days) < 150:
        if d.weekday() < 5:
            days.append(d)
        d = date.fromordinal(d.toordinal() - 1)
    rows = [{"symbol": s, "date": x, "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 5e6, "amount": 5e8}
            for s in syms for x in sorted(days)]
    full = pl.DataFrame(rows)
    calls = []

    def batch(symbols, start, end, columns=None):
        calls.append(tuple(sorted(symbols)))
        return full.filter(pl.col("symbol").is_in(list(symbols)) & (pl.col("date") <= end))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=lambda: pl.DataFrame({"symbol": syms, "sector": ["Technology"] * len(syms)}),
                           get_daily=lambda s, a, b, columns=None: full.filter(pl.col("symbol") == s),
                           get_daily_batch=batch)
    return repo, calls


def _loop(tmp_path, monkeypatch, *, mode="v4", watch=("NVDA", "MU", "AMD")):
    _mode(tmp_path, mode)
    monkeypatch.setattr(job, "_watchlist", lambda: set(watch))
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 9, 10, 0, tzinfo=us_clock.tz))
    monkeypatch.setattr(us_clock, "today", lambda: TODAY)
    repo, calls = _repo(tmp_path, ["NVDA", "MU", "AMD", "KOD"])
    loop = TradingLoop(repo, Account())
    monkeypatch.setattr(loop, "_enqueue_events", lambda: None)
    loop.boot()
    # 旧决策台扫描: 3 张观察卡 (MU / AMD 在自选, KOD 不在)
    loop.opp_svc.save_scan(ScanResult(as_of=SIG_DAY, gate="green", params_revision=3, scanned=3,
                                      watch=[_old_card("MU"), _old_card("AMD"), _old_card("KOD")]))
    # 机会板下单簿: 1 张 NVDA
    book = {"as_of": SIG_DAY.isoformat(), "as_of_close": SIG_DAY.isoformat(), "cards": [_book_card("NVDA").to_dict()],
            "skipped": [], "contract": "swing-desk-tight-v1"}
    wb.save_day(tmp_path, book)
    return loop, calls


def test_v4_loop_only_handles_book_cards_and_positions(tmp_path, monkeypatch):
    loop, calls = _loop(tmp_path, monkeypatch)
    assert {c.symbol for c in loop.opp_svc.all_cards(TODAY)} == {"NVDA", "MU", "AMD", "KOD"}      # 卡本身都还在
    assert loop.execution_symbols() == ["NVDA"]                                                     # 旧观察卡不取报价
    loop._sync_card_book()
    assert {c.symbol for c in loop.trader._cards.values()} == {"NVDA"}
    elig = loop.opp_svc.revalidate_unfilled(us_clock.now(), book_only=True)
    assert elig == {"NVDA"} and calls[-1] == ("NVDA",)                                             # 只为下单簿卡读日K
    before = len(loop.ledger.read_events())
    loop.opp_svc.advance_lifecycle(us_clock.now(), {}, quotes_ready=False, book_only=True)
    olds = [e for e in loop.ledger.read_events()[before:] if e.get("symbol") in ("MU", "AMD", "KOD")]
    assert olds == []                                                                               # 旧卡状态不在交易环里改
    assert not any(b["symbol"] in ("MU", "AMD", "KOD") for b in loop.opp_svc.entry_blockers)       # 也不再刷 6000 条拦截


def test_run_intraday_passes_book_only_only_in_v4(tmp_path, monkeypatch):
    seen = {}
    for mode in ("v4", "v3"):
        loop, _ = _loop(tmp_path / mode, monkeypatch, mode=mode)
        monkeypatch.setattr(us_clock, "in_session", lambda now: True)
        monkeypatch.setattr(loop, "ensure_watchlist_book", lambda now: None)
        monkeypatch.setattr(loop, "_cover_new_symbols", lambda now: None)
        monkeypatch.setattr(loop, "premarket_void_check", lambda *a, **k: None)
        got: list = []
        seen[mode] = got

        def life(*a, _got=got, **k):
            _got.append(("life", k.get("book_only")))

        def reval(now, _got=got, **k):
            _got.append(("reval", k.get("book_only")))
            return set()
        monkeypatch.setattr(loop.opp_svc, "advance_lifecycle", life)
        monkeypatch.setattr(loop.opp_svc, "revalidate_unfilled", reval)
        monkeypatch.setattr(loop, "_execution_quotes", lambda p, s: ([], []))
        monkeypatch.setattr(loop, "intraday_fill_check", lambda q, entry_symbols=None: [])
        loop.run_intraday(provider=SimpleNamespace(name="tickflow", get_execution_quotes=lambda s: []))
    assert seen["v4"] == [("life", True), ("reval", True)]
    assert seen["v3"] == [("life", None), ("reval", None)]                                          # 非 v4: 与 0.35.1 同形状


def test_non_v4_keeps_full_card_set(tmp_path, monkeypatch):
    loop, _ = _loop(tmp_path, monkeypatch, mode="v3")
    assert set(loop.execution_symbols()) == {"NVDA", "MU", "AMD", "KOD"}
    loop._sync_card_book()
    assert {c.symbol for c in loop.trader._cards.values()} == {"NVDA", "MU", "AMD", "KOD"}


def test_old_card_with_open_position_is_still_managed(tmp_path, monkeypatch):
    """历史上从旧决策台卡开的仓: 卡册里仍有它 (止损 / 目标 / 时间止损要读卡)。"""
    loop, _ = _loop(tmp_path, monkeypatch)
    old = _old_card("MU", state=CardState.FILLED)
    loop.opp_svc.save_scan(ScanResult(as_of=date(2026, 10, 7), gate="green", params_revision=3, scanned=1, cards=[old]))
    loop.ledger.append("fill", {"symbol": "MU", "side": "buy", "qty": 10, "price": 100.0, "fee": 1.0, "card_id": old.id,
                                "stop_price": 95.0, "trade_date": "2026-10-08"}, at="2026-10-08T10:00:00-04:00")
    loop.ledger.replay(loop.account)
    assert "MU" in loop.execution_symbols()
    loop._sync_card_book()
    assert old.id in loop.trader._cards and loop.trader._card_of(loop.ledger.open_positions[0]).id == old.id


def test_post_close_lifecycle_still_expires_old_cards(tmp_path, monkeypatch):
    """收盘后 advance_lifecycle 不带 book_only: 旧卡照旧按入场窗到期 (不会因瘦身变成永远的 watch)。"""
    loop, _ = _loop(tmp_path, monkeypatch)
    late = datetime(2026, 10, 19, 16, 5, tzinfo=us_clock.tz)                                        # 入场窗早已结束
    monkeypatch.setattr(us_clock, "today", lambda: late.date())
    loop.opp_svc.advance_lifecycle(late, {})
    states = {c.symbol: c.state for c in loop.opp_svc.all_cards(late.date())}
    assert states["MU"] == CardState.EXPIRED and states["AMD"] == CardState.EXPIRED


def test_news_universe_still_covers_old_watch_cards(tmp_path, monkeypatch):
    from app.services import news_job
    loop, _ = _loop(tmp_path, monkeypatch)
    monkeypatch.setattr("app.services.watchlist.list_symbols", lambda: [])
    assert {"MU", "AMD", "KOD", "NVDA"} <= set(news_job.universe(loop))


def test_corporate_action_coverage_is_pool_plus_watchlist(tmp_path, monkeypatch):
    loop, _ = _loop(tmp_path, monkeypatch, watch=("NVDA", "MU", "AMD", "TSLA"))
    assert loop.corporate_action_symbols() == ["AMD", "MU", "NVDA", "TSLA"]
    got = {}
    monkeypatch.setattr(loop.corp_actions, "refresh", lambda symbols, now: got.setdefault("s", symbols) and {"status": "x"})
    monkeypatch.setattr(loop, "ensure_watchlist_book", lambda now: None)
    loop.refresh_corporate_actions()
    assert got["s"] == ["AMD", "MU", "NVDA", "TSLA"]


def test_premarket_gap_no_longer_labels_watchlist_as_card(tmp_path, monkeypatch):
    """跳空推送的角色 (持仓 > 机会卡 > 自选) 读 loop.execution_symbols(): 旧观察卡不再把自选票标成「机会卡」。"""
    from app.services import premarket_gap
    loop, _ = _loop(tmp_path, monkeypatch)
    monkeypatch.setattr("app.services.watchlist.list_symbols", lambda: [{"symbol": s} for s in ("NVDA", "MU", "AMD")])
    hl = SimpleNamespace(mids=lambda syms: {})
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: hl)
    scope, _, _ = premarket_gap._inputs(tmp_path, loop)
    assert scope["NVDA"] == ["机会卡", "自选"] and scope["MU"] == ["自选"] and scope["AMD"] == ["自选"]
