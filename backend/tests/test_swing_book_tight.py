"""tight-v1 (0.24.0): 机会板卡 = 纸面卡。许可切换、下单簿几何、2 日入场窗、限价回踩免开盘越区作废、5 日持有。"""
import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_clock import us_clock
from app.opportunity.card import CardState
from app.services import entry_guard
from app.services import strategy_evaluation as se
from app.services import swing_desk as sd
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv

SIG_DAY = date(2026, 10, 8)        # 周四


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


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


def _row(rule, sym, side="long"):
    return {"rule": rule, "symbol": sym, "side": side, "entry_zone": [100.0, 101.0], "invalidation": 80.0,
            "target_zone": [150.0, 160.0], "risk": 20.0, "rr": 2.0}


def _snapshot(tmp_path, day, rows, watch, closes, atr, h4=None):
    d = wv.signals_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day.isoformat()}.json").write_text(json.dumps({
        "as_of": day.isoformat(), "watchlist": sorted(watch), "closes": closes, "signals": rows,
        "atr": atr, "h4_state": h4 or {}}))


def _bars(sym, rows):
    """rows: [(date, high, low)]"""
    return pl.DataFrame({"symbol": sym, "date": [r[0] for r in rows], "open": [float(r[2]) for r in rows],
                         "high": [float(r[1]) for r in rows], "low": [float(r[2]) for r in rows],
                         "close": [float(r[2]) for r in rows]})


def _repo(tmp_path, frames=(), spy_ok=True):
    full = pl.concat(frames) if frames else None

    def batch(syms, s, e, columns=None):
        if full is None:
            return pl.DataFrame()
        return full.filter(pl.col("symbol").is_in(syms) & (pl.col("date") >= s) & (pl.col("date") <= e))
    return SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_daily_batch=batch, _spy_ok=spy_ok)


def _setup(tmp_path, monkeypatch, spy_ok=True):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 1.0})
    sd.save_strategies(tmp_path, ["macd_golden"], ["tv_halftrend"])
    wv._PM_CACHE.clear()
    monkeypatch.setattr(wv, "short_gate", lambda repo, day: {"day": day.isoformat(), "ok": spy_ok,
                                                             "close": 700.0, "ma50": 710.0})


def test_permission_swing_mode_trades_only_board_cards(tmp_path, monkeypatch):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 1.0})
    pm = wv.permission(tmp_path)
    assert pm["tradable"] == {"macd_golden"}                        # 旧模式: 规则集勾选
    sd.save_strategies(tmp_path, ["macd_golden"], ["tv_halftrend"])
    wv._PM_CACHE.clear()
    pm = wv.permission(tmp_path)
    assert pm["tradable"] == {"swing"} and pm["short_tradable"] == {"swing"} and pm["risk_pct"] == 0.01
    assert wv.rule_permission(pm, "macd_golden", "long")[0] is False


def test_swing_book_cards_equal_board_geometry(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    watch = {"LLY", "AMD", "NONE"}
    rows = [_row("macd_golden", "LLY"), _row("phantom_4h", "LLY"),
            _row("tv_halftrend", "AMD", "short"), _row("phantom_4h_short", "AMD", "short"),
            _row("macd_golden", "NONE")]                                    # 无 4h 门
    _snapshot(tmp_path, SIG_DAY, rows, watch, {"LLY": 100.0, "AMD": 200.0, "NONE": 50.0},
              {"LLY": 2.0, "AMD": 4.0, "NONE": 1.0})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    by = {c["symbol"]: c for c in built["cards"]}
    assert set(by) == {"LLY", "AMD"}
    lly = by["LLY"]
    assert lly["setup_type"] == "strategy:swing" and list(lly["entry_zone"]) == [99.0, 99.5]
    assert lly["invalidation"] == 97.5 and list(lly["target_zone"]) == [103.5, 103.5]
    assert lly["max_holding_days"] == 5
    dec = lly["evidence"]["decision"]
    assert dec["entry_days"] == 2 and dec["limit_pullback"] is True and dec["book"] is True
    assert dec["rules"] == ["macd_golden"] and dec["gate_4h"] == "event"
    board = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    b = {c["symbol"]: c for c in board["long"] + board["short"]}
    for sym in ("LLY", "AMD"):
        assert list(by[sym]["entry_zone"]) == b[sym]["entry_zone"] and by[sym]["invalidation"] == b[sym]["stop"]
    assert by["AMD"]["side"] == "short" and by["AMD"]["evidence"]["decision"]["short_gate"]["ma50"] == 710.0


def test_swing_book_no_short_when_spy_above_ma50(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch, spy_ok=False)
    rows = [_row("tv_halftrend", "AMD", "short"), _row("phantom_4h_short", "AMD", "short")]
    _snapshot(tmp_path, SIG_DAY, rows, {"AMD"}, {"AMD": 200.0}, {"AMD": 4.0})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"AMD"},
                         pm=wv.permission(tmp_path))
    assert built["cards"] == []


def test_swing_book_skips_touched_and_expires_after_two_days(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    watch = {"LLY", "MU"}
    rows = [_row("macd_golden", "LLY"), _row("phantom_4h", "LLY"), _row("macd_golden", "MU"), _row("phantom_4h", "MU")]
    _snapshot(tmp_path, SIG_DAY, rows, watch, {"LLY": 100.0, "MU": 100.0}, {"LLY": 2.0, "MU": 2.0})
    d1 = date(2026, 10, 9)
    # LLY 10-09 低点 99.8 未进 [99.0, 99.5]; MU 低点 99.2 已触及
    repo = _repo(tmp_path, [_bars("LLY", [(SIG_DAY, 101, 100), (d1, 102, 99.8)]),
                            _bars("MU", [(SIG_DAY, 101, 100), (d1, 101, 99.2)])])
    built = wb.build_day(tmp_path, repo, SIG_DAY, as_of_close=d1, watch=watch, pm=wv.permission(tmp_path))
    assert [c["symbol"] for c in built["cards"]] == ["LLY"]
    assert {s.get("symbol"): s["reason"] for s in built["skipped"]}["MU"] == "not_pending:touched"
    d2 = date(2026, 10, 12)       # 信号日后第 2 个已收盘交易日 → 入场窗结束
    built2 = wb.build_day(tmp_path, repo, SIG_DAY, as_of_close=d2, watch=watch, pm=wv.permission(tmp_path))
    assert built2["cards"] == [] and built2["skipped"][0]["reason"] == "entry_window_over"


def test_swing_book_requires_board_geometry_inputs(tmp_path, monkeypatch, cal):
    """旧信号表 (无 atr) → 不出卡, 不用策略宽卡兜底。"""
    _setup(tmp_path, monkeypatch)
    d = wv.signals_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{SIG_DAY.isoformat()}.json").write_text(json.dumps({
        "as_of": SIG_DAY.isoformat(), "watchlist": ["LLY"], "closes": {"LLY": 100.0},
        "signals": [_row("macd_golden", "LLY"), _row("phantom_4h", "LLY")]}))
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"LLY"},
                         pm=wv.permission(tmp_path))
    assert built["cards"] == []


def _svc(tmp_path, cards):
    from app.services.opportunity_service import OpportunityService
    svc = OpportunityService.__new__(OpportunityService)
    svc.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    svc.ledger = SimpleNamespace(open_positions=[], open_risk=lambda: 0.0, nav=lambda marks: 100000.0,
                                 read_events=lambda: [], append=lambda *a, **k: None)
    import threading
    svc._lock = threading.RLock()
    svc.entry_blockers = []
    trans = []
    svc._transition = lambda c, st, why, now: trans.append((c.symbol, st, why))
    svc.all_cards = lambda as_of=None: cards
    svc.sector_map = lambda: {c.symbol: "Tech" for c in cards}
    svc.cooldown_symbols = lambda d: set()
    svc.list_card_days = lambda: []
    svc._raw_cards = lambda d: None
    return svc, trans


def _swing_card(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    rows = [_row("macd_golden", "LLY"), _row("phantom_4h", "LLY")]
    _snapshot(tmp_path, SIG_DAY, rows, {"LLY"}, {"LLY": 100.0}, {"LLY": 2.0})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"LLY"},
                         pm=wv.permission(tmp_path))
    from app.opportunity.card import OpportunityCard
    return OpportunityCard.from_dict(built["cards"][0])


def test_lifecycle_two_day_window_and_gap_exempt(tmp_path, monkeypatch, cal):
    card = _swing_card(tmp_path, monkeypatch)
    assert entry_guard.decision(card)["limit_pullback"] is True
    svc, trans = _svc(tmp_path, [card])
    monkeypatch.setattr(entry_guard, "evaluate", lambda *a, **k: entry_guard.Admission((), 0.01, False, tier="rule",
                                                                                      book=True))
    monkeypatch.setattr(entry_guard, "missing_valuation", lambda *a: [])
    # 第 1 天 10:00: 开盘 103 (远高于区上沿 99.5 的 2%) → 限价回踩卡不作废, 激活
    now = datetime(2026, 10, 9, 10, 0, tzinfo=us_clock.tz)
    svc.advance_lifecycle(now, {"LLY": {"price": 103.0, "open": 103.0}}, quotes_ready=True)
    assert trans and trans[-1][1] == CardState.ACTIVE
    # 第 2 天收盘后 → 入场窗结束
    trans.clear()
    svc.advance_lifecycle(datetime(2026, 10, 12, 16, 1, tzinfo=us_clock.tz), {}, quotes_ready=True)
    assert trans[-1][1] == CardState.EXPIRED and trans[-1][2].startswith("2个交易日")


# ── 审查修复 (S1-S5 + 名额隔离) ──

def test_s5_build_does_not_write_swing_json(tmp_path):
    sd.build(tmp_path)
    assert not sd.swing_config_path(tmp_path).exists()


def test_s4_window_over_on_weekend(cal):
    sat = date(2026, 10, 17)
    now = datetime(2026, 10, 17, 11, 0, tzinfo=us_clock.tz)
    assert sd.window_over(2, sat, now) is True
    assert sd.window_over(1, sat, now) is False


def test_s3_short_gate_missing_does_not_persist_and_conflict_flows(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch, spy_ok=None)
    rows = [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA"),
            _row("tv_halftrend", "AAA", "short"), _row("phantom_4h_short", "AAA", "short")]
    _snapshot(tmp_path, SIG_DAY, rows, {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})
    pm = wv.permission(tmp_path)
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"AAA"}, pm=pm)
    assert built["cards"] == [] and wb.save_day(tmp_path, built) == 0
    monkeypatch.setattr(wv, "short_gate", lambda repo, day: {"day": day.isoformat(), "ok": True, "close": 1, "ma50": 2})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"AAA"}, pm=pm)
    assert built["cards"] == [] and {"symbol": "AAA", "reason": "direction_conflict"} in built["skipped"]


def test_s2_board_uses_rules_frozen_in_book(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    rows = [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA"), _row("volume_price_surge", "BBB"),
            _row("phantom_4h", "BBB")]
    _snapshot(tmp_path, SIG_DAY, rows, {"AAA", "BBB"}, {"AAA": 100.0, "BBB": 50.0}, {"AAA": 2.0, "BBB": 1.0})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"AAA", "BBB"},
                         pm=wv.permission(tmp_path))
    assert wb.save_day(tmp_path, built) == 1 and built["swing_rules"]["long"] == ["macd_golden"]
    sd.save_strategies(tmp_path, ["volume_price_surge"], ["tv_halftrend"])          # 用户中途改 3+3
    board = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    assert [c["symbol"] for c in board["long"]] == ["AAA"]                           # 已落簿的日子: 板 = 簿


def test_s1_live_board_keeps_held_card_over_newer_signal(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    d0, d1 = date(2026, 10, 7), SIG_DAY
    for d, c in ((d0, 100.0), (d1, 110.0)):
        _snapshot(tmp_path, d, [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA")], {"AAA"}, {"AAA": c}, {"AAA": 2.0})
    held = wb.card_id("AAA", "swing", "long", d0)
    pos = SimpleNamespace(symbol="AAA", card_id=held, avg_cost=99.2, qty=10, entry_date=d1)
    lg = SimpleNamespace(open_positions=[pos], read_events=lambda: [])
    from app.services import desk_overview as ov
    monkeypatch.setattr(ov, "_account", lambda loop, data_dir: (lg, None))
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: {"AAA": (101.0, "live")})
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 9, 11, 0, tzinfo=us_clock.tz))
    out = sd.build_live(tmp_path, repo=object(), today=date(2026, 10, 9))
    assert len(out["long"]) == 2                      # 持仓卡在前, 同票新信号仍显示并标“本卡不开”
    c = out["long"][0]
    assert c["state"] == "holding" and c["card_id"] == held and c["signal_day"] == d0.isoformat()
    assert c["r_now"] == pytest.approx((101.0 - 99.2) / c["risk"], abs=0.01)
    newer = out["long"][1]
    assert newer["signal_day"] == d1.isoformat() and newer["blocked"] == "holding_other"


def test_swing_position_cap_ignores_legacy_positions(tmp_path, monkeypatch):
    """旧 19 仓不占机会板卡名额 (风险仍计入组合风险上限)。"""
    from app.ledger.core import Account
    legacy = [SimpleNamespace(symbol=f"S{i}", card_id=f"opp-2026-10-08-S{i}-book-tv_ut_bot-ab", qty=1) for i in range(19)]
    swing = [SimpleNamespace(symbol="X", card_id=wb.card_id("X", "swing", "long", SIG_DAY), qty=1)]
    lg = SimpleNamespace(open_positions=legacy + swing)
    adm = entry_guard.Admission((), 0.01, False, tier="rule", book=True, evo=True, segregated_rule="swing")
    cap = entry_guard.position_cap(Account(), adm, 0.06, lg)
    assert cap - len(legacy) == 6 and len(lg.open_positions) < cap
