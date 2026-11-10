"""2026-11-10 审计 #1 的补充回归: v4 交易环取卡 (loop_cards book_only) 不读注定被丢掉的决策台卡文件, 结果不变。

这是对修复方式的等价性守卫 (在未修复的代码上「结果相同」那一半也成立), 所以不放进先红后修的 test_audit1110_fixes.py。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.ledger.core import Account, Ledger
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult
from app.services import entry_guard
from app.services import watchlist_book as wb
from app.services.opportunity_service import OpportunityService

AS_OF = date(2026, 11, 9)
DAYS = [date(2026, 11, d) for d in (3, 4, 5, 6)]


def _desk(sym, day, state=CardState.WATCH):
    return OpportunityCard(id=OpportunityCard.make_id(sym, "strategy:trend_breakout", day, 1), symbol=sym,
                           setup_type="strategy:trend_breakout", created_date=day, entry_zone=(100.0, 101.0),
                           invalidation=95.0, target_zone=(110.0, 112.0), risk_per_share=5.5, reward_risk=2.0,
                           evidence={"sector": "Technology", "decision": {"tab": "watchlist"}}, state=state)


def _book(sym, day):
    return OpportunityCard(id=wb.card_id(sym, "swing", "long", day), symbol=sym, setup_type="strategy:swing",
                           created_date=day, entry_zone=(97.0, 98.0), invalidation=95.0, target_zone=(104.0, 104.0),
                           risk_per_share=2.5, reward_risk=2.4,
                           evidence={"book": True, "decision": {"book": True, "tab": "watchlist", "tier": "rule"}},
                           state=CardState.OPEN)


def _env(tmp_path):
    svc = OpportunityService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    lg = Ledger(tmp_path / "ledger")
    lg.init_account(Account())
    lg.replay(Account())
    svc.bind_ledger(lg)
    for i, d in enumerate(DAYS):
        svc.save_scan(ScanResult(as_of=d, gate="green", params_revision=3, scanned=2,
                                 watch=[_desk(f"W{i}", d), _desk(f"X{i}", d)]))
    wb._LOAD_CACHE.clear()
    wb.save_day(tmp_path, {"as_of": DAYS[-1].isoformat(), "as_of_close": DAYS[-1].isoformat(),
                           "cards": [_book("NVDA", DAYS[-1]).to_dict()], "skipped": [], "contract": "swing-desk-tight-v1"})
    return svc, lg


def _reads(monkeypatch, svc):
    seen = []
    real = svc._raw_cards

    def spy(day):
        seen.append(day)
        return real(day)
    monkeypatch.setattr(svc, "_raw_cards", spy)
    return seen


def _expected(svc, lg):
    """旧口径: 先全读, 再筛「下单簿卡 + 有持仓的卡」。"""
    held = {p.card_id for p in lg.open_positions if p.card_id}
    return sorted(c.id for c in svc.all_cards(AS_OF) if entry_guard.is_book(c) or c.id in held)


def test_book_only_reads_no_desk_file_when_nothing_is_held(tmp_path, monkeypatch):
    svc, lg = _env(tmp_path)
    want = _expected(svc, lg)
    seen = _reads(monkeypatch, svc)
    got = svc.loop_cards(AS_OF, book_only=True)
    assert sorted(c.id for c in got) == want == [_book("NVDA", DAYS[-1]).id]
    assert seen == []


def test_book_only_reads_only_the_day_of_a_held_desk_card(tmp_path, monkeypatch):
    svc, lg = _env(tmp_path)
    held = _desk("W1", DAYS[1])
    lg.append("fill", {"symbol": "W1", "side": "buy", "qty": 10, "price": 100.0, "fee": 1.0, "card_id": held.id,
                       "stop_price": 95.0, "trade_date": "2026-11-05"}, at="2026-11-05T10:00:00-05:00")
    want = _expected(svc, lg)
    seen = _reads(monkeypatch, svc)
    got = svc.loop_cards(AS_OF, book_only=True)
    assert sorted(c.id for c in got) == want
    assert {c.id: c.state for c in got}[held.id] == CardState.FILLED
    assert seen == [DAYS[1]]


def test_book_only_falls_back_to_full_read_when_a_held_card_id_has_no_date(tmp_path, monkeypatch):
    svc, lg = _env(tmp_path)
    lg.append("fill", {"symbol": "ODD", "side": "buy", "qty": 10, "price": 100.0, "fee": 1.0, "card_id": "legacy-card",
                       "stop_price": 95.0, "trade_date": "2026-11-05"}, at="2026-11-05T10:00:00-05:00")
    want = _expected(svc, lg)
    seen = _reads(monkeypatch, svc)
    assert sorted(c.id for c in svc.loop_cards(AS_OF, book_only=True)) == want
    assert sorted(seen) == DAYS                       # 认不出生成日: 不收窄, 照旧全读后再筛


def test_old_file_with_an_unfinished_card_is_still_read(tmp_path, monkeypatch):
    """旧文件只有「卡全部了结」才跳过: 还有没了结的卡 (例如停机多日后没来得及判到期的) 照旧读出来,
    生命周期才能把它正常判到期; 不会因为卡龄大就从交易环里消失。"""
    svc, lg = _env(tmp_path)
    old = date(2026, 8, 3)
    done, pending = _desk("OLD1", old), _desk("OLD2", old, state=CardState.OPEN)
    svc.save_scan(ScanResult(as_of=old, gate="green", params_revision=3, scanned=2, cards=[pending], watch=[done]))
    lg.append("opportunity", {"card_id": done.id, "symbol": "OLD1", "state": "expired", "reason": "5个交易日入场窗结束",
                              "trade_date": "2026-08-10"}, at="2026-08-10T16:00:00-04:00")
    for _ in range(2):                                # 第二次也一样 (不靠首次解析)
        by = {c.symbol: c.state for c in svc.all_cards(AS_OF)}
        assert by["OLD2"] == CardState.OPEN and by["OLD1"] == CardState.EXPIRED
    lg.append("opportunity", {"card_id": pending.id, "symbol": "OLD2", "state": "expired", "reason": "5个交易日入场窗结束",
                              "trade_date": "2026-11-09"}, at="2026-11-09T16:00:00-05:00")
    seen = _reads(monkeypatch, svc)
    assert "OLD2" not in {c.symbol for c in svc.all_cards(AS_OF)}      # 全部了结后才不再读
    assert old not in seen


def test_full_mode_is_unchanged(tmp_path):
    svc, _ = _env(tmp_path)
    assert sorted(c.id for c in svc.loop_cards(AS_OF)) == sorted(c.id for c in svc.all_cards(AS_OF))
    assert len(svc.loop_cards(AS_OF)) == 9
