"""机会页全功能审计 (2026-11-10) 的修复回归。产品决策: 同分卡改固定散列排序; 回放改成 09:45-15:30 (不碰交易);
做空多条命中排序不动; 页面恢复逐卡结论与目标价; 其余按建议 (赔率表认规则 / 结算拆股口径 / 保存口校验 /
停算无用标注 / 被砍与被挡候选的影子记录 / 回放数字旁并排实际数)。
"""
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_swing_book_tight import SIG_DAY, _repo, _row, _setup, _snapshot

from app.market_clock import us_clock
from app.services import intraday_strength as ids
from app.services import kline_4h
from app.services import swing_desk as sd
from app.services import swing_pm_check as pmc
from app.services import swing_settlement as ss
from app.services import swing_verdict as sv
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv

D1 = date(2026, 10, 9)
D2 = date(2026, 10, 12)


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    pmc._LOAD_CACHE.clear()
    pmc._LAST_TRY.clear()


def _flat(px, n=13):
    return [(px, px, px, px)] * n


# ---------- 1. 赔率表认规则 ----------

def _table(long_rules):
    return {"rules": {"long": list(long_rules), "short": ["tv_halftrend"]},
            "cells": {"long|1|birth": {"n": 500, "fill": 0.6, "ev": 0.2, "ci": [0.1, 0.3], "level": "day"}}}


def test_odds_not_given_when_table_built_for_other_rules(cal):
    now = datetime(2026, 10, 8, 20, 0, tzinfo=us_clock.tz)
    card = {"side": "long", "signal_day": SIG_DAY.isoformat(), "entry_days": 2, "price_source": "close", "price": 100.0,
            "entry_zone": [99.0, 99.5], "atr": 2.0, "risk": 1.75}
    tab = _table(["ibs_mean_reversion", "tv_zlsma_cross", "trend_breakout"])
    same = sv.odds_for(card, now, equity=100_000, risk_pct=0.005, table=tab,
                       rules=["trend_breakout", "ibs_mean_reversion", "tv_zlsma_cross"])        # 顺序不同 = 同一套
    assert same["fill"] == 0.6 and same["ev_r"] == 0.2 and not same.get("stale")
    other = sv.odds_for(card, now, equity=100_000, risk_pct=0.005, table=tab,
                        rules=["volume_price_surge", "tv_smc_structure", "ibs_mean_reversion"])
    assert other["stale"] is True and other["fill"] is None and other["ev_r"] is None and other["ev_usd"] is None
    assert other["qty"] == same["qty"] and other["risk_usd"] == same["risk_usd"]               # 计划股数照给
    assert sv.odds_for(card, now, table=tab)["fill"] == 0.6                                     # 没给规则 = 不核对 (旧调用)


def test_board_card_odds_stale_until_table_matches_board_rules(tmp_path, monkeypatch, cal):
    """板上那天的出卡规则与赔率表不是一套 -> 卡不带回区率; 是同一套 -> 照给。"""
    _setup(tmp_path, monkeypatch)                                                    # 3+3 = macd_golden | tv_halftrend
    _snapshot(tmp_path, SIG_DAY, [_row("macd_golden", "LLY"), _row("phantom_4h", "LLY")], {"LLY"}, {"LLY": 100.0}, {"LLY": 2.0})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"LLY"}, pm=wv.permission(tmp_path))
    assert wb.save_day(tmp_path, built) == 1
    lg = SimpleNamespace(open_positions=[], read_events=lambda: [], nav=lambda marks: 100_000.0)
    from app.services import desk_overview as ov
    monkeypatch.setattr(ov, "_account", lambda loop, data_dir: (lg, None))
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: {})
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    now = datetime(2026, 10, 8, 20, 0, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    monkeypatch.setattr(sv, "odds_table", lambda: _table(["ibs_mean_reversion"]))
    o = sd.build_live(tmp_path, repo=object(), today=now.date())["long"][0]["odds"]
    assert o["stale"] is True and o["fill"] is None and o["qty"]
    monkeypatch.setattr(sv, "odds_table", lambda: _table(["macd_golden"]))
    o = sd.build_live(tmp_path, repo=object(), today=now.date())["long"][0]["odds"]
    assert o["fill"] == 0.6 and not o.get("stale")


# ---------- 2. 同分卡固定散列 ----------

def _tie_inputs(syms):
    sig = []
    for s in syms:
        sig += [{"rule": "tv_halftrend", "symbol": s, "side": "short"}, {"rule": "phantom_4h_short", "symbol": s, "side": "short"}]
    return dict(signals=sig, closes={s: 100.0 for s in syms}, as_of=SIG_DAY, long_rules=("macd_golden",),
                short_rules=("tv_halftrend",), atr={s: 2.0 for s in syms}, short_ok=True)


def test_tie_break_is_hash_not_alphabet_and_reproducible():
    syms = ["AMD", "AXTI", "HIMS", "MU", "NVDA", "SNDK", "TSM"]
    kw = _tie_inputs(syms)
    legacy = [c["symbol"] for c in sd.compose_boards(**kw, tiebreak="symbol")["short"]]
    assert legacy == ["AMD", "AXTI", "HIMS", "MU", "NVDA"]                          # 旧口径: 字母靠后的被砍
    board = sd.compose_boards(**kw)
    got = [c["symbol"] for c in board["short"]]
    want = sorted(syms, key=lambda s: sd.tie_key(SIG_DAY, s))[:5]
    assert board["tiebreak"] == sd.TIEBREAK == "hash-v1" and got == want and got != legacy
    assert [c["symbol"] for c in sd.compose_boards(**kw)["short"]] == got            # 同一天同一批候选结果固定
    other_day = sd.compose_boards(**{**kw, "as_of": SIG_DAY + timedelta(days=1)})["short"]
    assert [c["symbol"] for c in other_day] != got                                   # 换一天换一个顺序 (不固定偏向谁)


def test_tie_break_only_decides_between_equal_cards():
    """分数高的 / 4h 门是事件的照旧排前, 散列只在其余都相同时起作用。"""
    kw = _tie_inputs(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"])
    kw["signals"] = [s for s in kw["signals"] if not (s["rule"] == "phantom_4h_short" and s["symbol"] == "FFF")]
    kw["h4_state"] = {"FFF": "short"}                                                # FFF 只是方向同向 (state)
    kw["signals"].append({"rule": "tv_smc_structure", "symbol": "EEE", "side": "short"})
    kw["short_rules"] = ("tv_halftrend", "tv_smc_structure")
    cards = sd.compose_boards(**kw, max_per_side=99)["short"]
    assert cards[0]["symbol"] == "EEE" and cards[-1]["symbol"] == "FFF"


def test_board_for_day_keeps_frozen_tiebreak_of_saved_book(tmp_path, monkeypatch, cal):
    """旧簿没有 tiebreak 字段 = 当时的字母序: 重算那天的板仍是簿里那 5 张; 新落的簿冻结散列口径。"""
    syms = ["AMD", "AXTI", "HIMS", "MU", "NVDA", "SNDK", "TSM"]
    _setup(tmp_path, monkeypatch)
    rows = []
    for s in syms:
        rows += [_row("tv_halftrend", s, "short"), _row("phantom_4h_short", s, "short")]
    _snapshot(tmp_path, SIG_DAY, rows, set(syms), {s: 100.0 for s in syms}, {s: 2.0 for s in syms})
    repo = _repo(tmp_path)
    assert sd.tiebreak_for_day(tmp_path, SIG_DAY) == "hash-v1"                       # 还没落簿
    built = wb.build_day(tmp_path, repo, SIG_DAY, as_of_close=SIG_DAY, watch=set(syms), pm=wv.permission(tmp_path))
    assert built["tiebreak"] == "hash-v1" and wb.save_day(tmp_path, built) == 5
    booked = sorted(c["symbol"] for c in wb.load_day(tmp_path, SIG_DAY)["cards"])
    assert booked == sorted(c["symbol"] for c in sd.board_for_day(tmp_path, SIG_DAY, repo)["short"])
    # 把簿改成上线前的样子 (字母序那 5 张, 没有 tiebreak 字段)
    p = wb.book_path(tmp_path, SIG_DAY)
    raw = json.loads(p.read_text())
    raw.pop("tiebreak")
    p.write_text(json.dumps(raw))
    wb._LOAD_CACHE.clear()
    assert sd.tiebreak_for_day(tmp_path, SIG_DAY) == "symbol"
    assert [c["symbol"] for c in sd.board_for_day(tmp_path, SIG_DAY, repo)["short"]] == ["AMD", "AXTI", "HIMS", "MU", "NVDA"]


# ---------- 3. 回放入场时窗 = 纸面 (09:45 起, 收盘前 30 分钟止) ----------

ZONE, STOP, TARGET = (97.0, 98.5), 92.5, 110.5
FLAT = (100.0, 100.5, 99.5, 100.0)                     # 一直在挂单区上方
TOUCH = (98.0, 98.4, 97.2, 99.8)


def test_replay_fills_in_0945_bar_when_given():
    days = [[TOUCH] + [FLAT] * 12, [FLAT] * 13]
    assert ss.replay_card("long", ZONE, STOP, TARGET, days)["status"] == "no_fill"            # 没有 09:45 那根: 从 10:00 起判
    r = ss.replay_card("long", ZONE, STOP, TARGET, days + [[FLAT] * 13] * 5, open15=[TOUCH, None])
    assert r["status"] in ("closed", "holding") and r["fill_day"] == 0 and r["fill_bar"] == 0
    assert r["entry"] == round(98.0 * 1.0005, 4)
    # 第一根 30 分钟K 进过区, 但那是 09:30-09:45 的事 (09:45 那根没碰到): 纸面不成交, 回放也不成交
    assert ss.replay_card("long", ZONE, STOP, TARGET, days, open15=[FLAT, FLAT])["status"] == "no_fill"


def test_replay_never_fills_in_last_bar_of_the_day():
    last_only = [[FLAT] * 12 + [(100.0, 100.2, 97.5, 98.0)], [FLAT] * 13]
    assert ss.replay_card("long", ZONE, STOP, TARGET, last_only)["status"] == "no_fill"       # 15:30 后纸面不成交
    half = [[FLAT] * 6 + [(100.0, 100.2, 97.5, 98.0)], [FLAT] * 13]                          # 半日市 7 根: 12:30 那根同理
    assert ss.replay_card("long", ZONE, STOP, TARGET, half)["status"] == "no_fill"
    mid = [[FLAT] * 5 + [TOUCH] + [FLAT] * 7] + [[FLAT] * 13] * 6
    r = ss.replay_card("long", ZONE, STOP, TARGET, mid)
    assert r["status"] == "closed" and r["fill_bar"] == 5


def test_replay_window_matches_paper_entry_window(cal):
    d = date(2026, 11, 6)
    for hm, ok in (((9, 44), False), ((9, 45), True), ((15, 30), True), ((15, 31), False)):
        assert us_clock.in_entry_window(datetime(d.year, d.month, d.day, *hm, tzinfo=us_clock.tz)) is ok


def test_contract_bumped_to_v3_and_old_file_untouched(tmp_path):
    assert ss.CONTRACT == "swing-settle-v3"
    assert ss.outcomes_path(tmp_path).name == "outcomes.swing-settle-v3.jsonl"
    assert ss.outcomes_path(tmp_path, "swing-settle-v2").name == "outcomes.swing-settle-v2.jsonl"


# ---------- 4. 结算的拆股口径 ----------

SPLIT_DAYS = [date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20)]
EX_DAY = date(2026, 11, 16)
SIGNAL_DAY = date(2026, 11, 12)


def _split_store(root: Path) -> None:
    """合成票 ZZZ: 信号日 11-12 收 800; 11-13 第 3 根回踩到 784 (进挂单区 780-790); 11-16 起 8 拆 1 (原始价 ~99), 横盘。"""
    rows, subs = [], []
    for d in SPLIT_DAYS:
        for i in range(13):
            t = datetime(d.year, d.month, d.day, 14, 30, tzinfo=UTC) + timedelta(minutes=30 * i)
            if d < EX_DAY:
                o, h, lo, c = (795, 796, 784, 786) if i == 2 else (795, 797, 791, 792)
            else:
                o, h, lo, c = 99.0, 99.4, 98.8, 99.1
            rows.append(("ZZZ", t.strftime("%Y-%m-%dT%H:%M:%SZ"), float(o), float(h), float(lo), float(c), 1000.0))
        px = (795.0, 797.0, 791.0, 792.0) if d < EX_DAY else (99.0, 99.4, 98.8, 99.1)
        subs.append(("ZZZ", datetime(d.year, d.month, d.day, 14, 45, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     *px, 500.0, d.isoformat()))
    cols = ["symbol", "t", "open", "high", "low", "close", "volume"]
    kline_4h.write_month(root, "2026-11", pl.DataFrame(rows, schema=cols, orient="row"))
    kline_4h._merge_open15(root, pl.DataFrame(subs, schema=[*cols, "day"], orient="row"))


class _SplitRepo:
    """日K: adjusted=True = 除权后 (除权日前的 close 已回溯 ÷8, raw_close 仍是当日原始价)。"""
    def __init__(self, adjusted: bool):
        self.adjusted = adjusted

    def get_daily_batch(self, symbols, start, end, columns=None):
        out = []
        for d in [SIGNAL_DAY, *SPLIT_DAYS]:
            raw = 800.0 if d == SIGNAL_DAY else (792.0 if d < EX_DAY else 99.1)
            out.append(("ZZZ", d, raw / 8.0 if (self.adjusted and d < EX_DAY) else raw, raw))
        return pl.DataFrame(out, schema=["symbol", "date", "close", "raw_close"], orient="row").filter(
            (pl.col("date") >= start) & (pl.col("date") <= end))


SPLIT_ACTION = [{"id": "x", "type": "forward_split", "symbol": "ZZZ", "ex_date": EX_DAY.isoformat(), "factor": 8.0, "auto": True}]


def test_settlement_bars_are_on_signal_day_basis_across_a_split(tmp_path, monkeypatch, cal):
    from app.services import corporate_actions as ca
    _split_store(tmp_path)
    zone, stop, target = (780.0, 790.0), 750.0, 870.0                                # 信号日 11-12 的价位
    seen = []
    # 三种情形同一结局: 公司行动已知 + 日K未回溯 / 公司行动已知 + 日K已回溯 / 公司行动缓存缺这条 + 日K已回溯
    for actions, repo in ((SPLIT_ACTION, _SplitRepo(False)), (SPLIT_ACTION, _SplitRepo(True)), ([], _SplitRepo(True))):
        monkeypatch.setattr(ca.CorporateActionService, "actions", lambda self, a=actions: list(a))
        days, o15 = ss.card_days(tmp_path, repo, "ZZZ", SPLIT_DAYS, basis_day=SIGNAL_DAY)
        assert days[0][2][2] == pytest.approx(784.0) and days[1][0][0] == pytest.approx(792.0, rel=2e-3)
        assert o15[0][0] == pytest.approx(795.0) and o15[1][0] == pytest.approx(792.0, rel=2e-3)
        r = ss.replay_card("long", zone, stop, target, days, open15=o15)
        seen.append((r["status"], r.get("fill_day"), r.get("fill_bar"), r.get("why")))
        assert abs(r["r"]) < 0.2                                                       # 横盘到期, 不是 -19R
    assert seen[0] == seen[1] == seen[2] == ("closed", 0, 2, "time")                   # 哪种情形结算都是同一结局
    # 不换口径 (旧行为): 除权后真实成交消失
    old = ss._m30_days(tmp_path, _SplitRepo(True), "ZZZ", SPLIT_DAYS[0], SPLIT_DAYS[-1])
    assert ss.replay_card("long", zone, stop, target, [old[d] for d in SPLIT_DAYS])["status"] == "no_fill"


def test_dividend_sized_adjustment_is_not_rescaled(tmp_path, monkeypatch, cal):
    """日K复权系数只差 1% (现金分红) 且公司行动里没有拆股 -> 不换算, 结算看原始价 (纸面成交看的也是原始报价)。"""
    from app.services import corporate_actions as ca
    monkeypatch.setattr(ca.CorporateActionService, "actions", lambda self: [])
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: pl.DataFrame(
        [("ZZZ", SIGNAL_DAY, 99.0, 100.0), ("ZZZ", SPLIT_DAYS[0], 100.0, 100.0)],
        schema=["symbol", "date", "close", "raw_close"], orient="row").filter((pl.col("date") >= s) & (pl.col("date") <= e)))
    f = ss._basis_factors(tmp_path, repo, "ZZZ", SPLIT_DAYS[0], SPLIT_DAYS[0], SIGNAL_DAY)
    assert f(SPLIT_DAYS[0]) == 1.0
    assert ss._basis_factors(tmp_path, repo, "ZZZ", SPLIT_DAYS[0], SPLIT_DAYS[0], None)(SIGNAL_DAY) == pytest.approx(0.99)   # 研究口径 = 日K复权系数


def test_premarket_void_price_uses_signal_day_basis(tmp_path, monkeypatch, cal):
    from app.services import corporate_actions as ca
    monkeypatch.setattr(ca.CorporateActionService, "actions", lambda self: list(SPLIT_ACTION))
    cols = ["symbol", "t", "open", "high", "low", "close", "volume"]
    t = datetime(2026, 11, 16, 13, 30, tzinfo=UTC)                           # 08:30 ET 盘前, 拆后原始价 99
    kline_4h.write_month(tmp_path, "2026-11", pl.DataFrame([("ZZZ", t.strftime("%Y-%m-%dT%H:%M:%SZ"), 99.0, 99.0, 99.0, 99.0, 10.0)],
                                                          schema=cols, orient="row"))
    repo = _SplitRepo(True)
    assert pmc.premarket_close_m30(tmp_path, repo, "ZZZ", EX_DAY) == pytest.approx(99.0)
    assert pmc.premarket_close_m30(tmp_path, repo, "ZZZ", EX_DAY, basis_day=SIGNAL_DAY) == pytest.approx(792.0)   # 99 x 8


# ---------- 5. 保存口校验 ----------

def _client(tmp_path, engine=None):
    from app.api.opportunities import router
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.trading_loop = None
    if engine is not None:
        app.state.strategy_engine = engine
    return TestClient(app)


def _engine(ids_):
    return SimpleNamespace(list_strategies=lambda: [{"id": i, "name": i, "source": "builtin"} for i in ids_])


def test_settings_rejects_unknown_and_non_shortable_rules_without_writing(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    eng = _engine(["macd_golden", "ibs_mean_reversion", "trend_breakout", "tv_halftrend", "tv_zlsma_cross"])
    c = _client(tmp_path, eng)
    before = sd.swing_config_path(tmp_path).read_text()
    r = c.post("/api/opportunities/settings", json={"long": ["no_such_rule", "ibs_mean_reversion"], "short": ["tv_halftrend"]})
    assert r.status_code == 422 and "no_such_rule" in r.json()["detail"]
    r = c.post("/api/opportunities/settings", json={"long": ["ibs_mean_reversion"], "short": ["ibs_mean_reversion", "trend_breakout"]})
    assert r.status_code == 422 and "ibs_mean_reversion" in r.json()["detail"]
    assert sd.swing_config_path(tmp_path).read_text() == before                       # 拒绝时什么都不写
    r = c.post("/api/opportunities/settings", json={"long": ["ibs_mean_reversion", "trend_breakout"], "short": ["tv_halftrend"]})
    assert r.status_code == 200 and sd.strategies_from_ruleset(tmp_path) == (("ibs_mean_reversion", "trend_breakout"), ("tv_halftrend",))


def test_settings_long_check_skipped_when_catalog_unavailable(tmp_path, monkeypatch, cal):
    """策略目录读不到 (引擎没起来) 时不因目录故障拦住保存; 做空能力是静态表, 照查。"""
    _setup(tmp_path, monkeypatch)
    c = _client(tmp_path)
    assert c.post("/api/opportunities/settings", json={"long": ["whatever"], "short": ["tv_halftrend"]}).status_code == 200
    assert c.post("/api/opportunities/settings", json={"long": ["whatever"], "short": ["macd_golden"]}).status_code == 422


# ---------- 6. 影子记录: 被砍 / 被 4h 门挡的候选只结算不下单 ----------

def _shadow_day(tmp_path, monkeypatch):
    syms = ["AMD", "AXTI", "HIMS", "MU", "NVDA", "SNDK", "TSM"]
    _setup(tmp_path, monkeypatch)
    rows = [_row("macd_golden", "GGG")]                                              # 做多命中但 4h 门不同向
    for s in syms:
        rows += [_row("tv_halftrend", s, "short"), _row("phantom_4h_short", s, "short")]
    allsyms = set(syms) | {"GGG"}
    _snapshot(tmp_path, SIG_DAY, rows, allsyms, {s: 100.0 for s in allsyms}, {s: 2.0 for s in allsyms})
    repo = _repo(tmp_path)
    built = wb.build_day(tmp_path, repo, SIG_DAY, as_of_close=SIG_DAY, watch=allsyms, pm=wv.permission(tmp_path))
    assert wb.save_day(tmp_path, built) == 5
    return repo, syms


def test_compose_boards_shadow_lists_cut_and_gate_blocked_only_on_request():
    kw = _tie_inputs(["AMD", "AXTI", "HIMS", "MU", "NVDA", "SNDK", "TSM"])
    kw["signals"].append({"rule": "macd_golden", "symbol": "GGG", "side": "long"})
    kw["closes"]["GGG"], kw["atr"]["GGG"] = 100.0, 2.0
    assert "shadow" not in sd.compose_boards(**kw)                                    # 默认不带 (页面载荷不变)
    b = sd.compose_boards(**kw, shadow=True)
    kept = {c["symbol"] for c in b["short"]}
    cut = {c["symbol"] for c in b["shadow"] if c["shadow"] == "cut"}
    assert len(kept) == 5 and cut == set(kw["closes"]) - kept - {"GGG"} and len(cut) == 2
    blocked = [c for c in b["shadow"] if c["shadow"] == "gate_4h"]
    assert [(c["symbol"], c["side"], c["gate"]) for c in blocked] == [("GGG", "long", "none")]
    assert blocked[0]["entry_zone"] == [99.0, 99.5] and blocked[0]["stop"] == 97.5    # 与上板卡同一套价位
    assert all(c["symbol"] != "GGG" for c in b["long"])                                # 影子不上板


def test_shadow_recorded_once_after_book_and_settled_like_board_cards(tmp_path, monkeypatch, cal):
    repo, syms = _shadow_day(tmp_path, monkeypatch)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 9, 17, 0, tzinfo=us_clock.tz))
    rec = ss.record_shadow(tmp_path, repo, SIG_DAY)
    assert rec["contract"] == "swing-shadow-v1" and rec["late"] is False and rec["tiebreak"] == "hash-v1"
    by = {c["symbol"]: c for c in rec["cards"]}
    kept = {c["symbol"] for c in wb.load_day(tmp_path, SIG_DAY)["cards"]}
    assert set(by) == (set(syms) - kept) | {"GGG"} and by["GGG"]["reason"] == "gate_4h"
    assert {by[s]["reason"] for s in set(syms) - kept} == {"cut"}
    before = ss.shadow_path(tmp_path, SIG_DAY).read_text()
    assert ss.record_shadow(tmp_path, repo, SIG_DAY) is None                           # 已记过不改写
    assert ss.shadow_path(tmp_path, SIG_DAY).read_text() == before
    # 结算: 空头挂单区 100.5-101, 第 1 天 10:00 那根碰到 -> 成交; GGG 做多区 99-99.5 没碰到
    hit = [(100.0, 100.0, 100.0, 100.0), (100.6, 100.8, 100.4, 100.6), *_flat(100.6, 11)]
    bars = {D1: hit, D2: _flat(100.6)}
    monkeypatch.setattr(ss, "_m30_days", lambda data_dir, repo, symbol, s, e, **kw: {k: v for k, v in bars.items() if s <= k <= e})
    r = ss.settle_shadow(tmp_path, repo, as_of_close=D2)
    assert r["written"] == 1 and r["pending"] == 2                                     # GGG 未回区已定; 两张空头还在持有
    rows = ss.read_shadow_outcomes(tmp_path)
    assert rows[0]["symbol"] == "GGG" and rows[0]["status"] == "no_fill" and rows[0]["reason"] == "gate_4h"
    assert rows[0]["card_id"] == "shadow-2026-10-08-GGG-long" and rows[0]["contract"] == ss.CONTRACT
    assert ss.read_outcomes(tmp_path) == []                                            # 不写进板卡结局
    t = ss.shadow_table(tmp_path)
    assert t["groups"]["gate_4h:long"]["shadow"]["cards"] == 1 and t["n_total"] == 1


def test_shadow_not_recorded_before_book_is_saved(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    _snapshot(tmp_path, SIG_DAY, [_row("macd_golden", "GGG")], {"GGG"}, {"GGG": 100.0}, {"GGG": 2.0})
    assert ss.record_shadow(tmp_path, _repo(tmp_path), SIG_DAY) is None and not ss.shadow_dir(tmp_path).exists()


def test_shadow_marks_late_backfill(tmp_path, monkeypatch, cal):
    repo, _syms = _shadow_day(tmp_path, monkeypatch)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 20, 17, 0, tzinfo=us_clock.tz))
    assert ss.record_shadow(tmp_path, repo, SIG_DAY)["late"] is True


def test_shadow_is_outside_the_trading_chain():
    """影子只在结算模块里: 交易环 / 准入 / 下单簿 / 卡服务 / 推送不引用它。"""
    root = Path(ss.__file__).parents[1]
    for rel in ("trader/trader.py", "services/trading_loop.py", "services/entry_guard.py", "services/watchlist_book.py",
                "services/opportunity_service.py", "services/notify_opportunity.py", "services/opp_radar.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "shadow_path" not in src and "record_shadow" not in src and "opportunities/shadow" not in src, rel
        assert "shadow=True" not in src, rel


# ---------- 7. 回放数字旁并排上线后的实际数 ----------

def test_live_vs_replay_flags_divergence_only_with_enough_days():
    few = {f"d{i}": [-1.0] for i in range(7)}
    v = ss.live_vs_replay(few, 0.1)
    assert v["n"] == 7 and v["ci"] is None and v["diverged"] is False                 # 出卡日不足 8 个: 不给区间, 不判背离
    many = {f"d{i}": [-1.0, -0.8] for i in range(10)}
    v = ss.live_vs_replay(many, 0.1)
    assert v["n"] == 20 and v["days"] == 10 and v["mean_r"] == -0.9 and v["ci"][1] < 0.1 and v["diverged"] is True
    mixed = {f"d{i}": [1.5 if i % 2 else -1.2] for i in range(12)}
    v = ss.live_vs_replay(mixed, 0.1)
    assert v["ci"][0] < 0.1 < v["ci"][1] and v["diverged"] is False
    assert ss.live_vs_replay(many, None)["diverged"] is False                          # 没有回放值: 不判


def test_live_line_marks_stale_replay_and_counts_cards(tmp_path, monkeypatch):
    rows = [{"card_id": f"c{i}", "side": "long", "signal_day": f"2026-10-{10 + i:02d}", "status": "closed", "r": -1.0} for i in range(3)]
    rows += [{"card_id": "n1", "side": "long", "signal_day": "2026-10-20", "status": "no_fill"},
             {"card_id": "v1", "side": "long", "signal_day": "2026-10-21", "status": "void"}]
    p = ss.outcomes_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    built = (ss.base_rates().get("rules") or {}).get("long") or []
    line = ss.live_line(tmp_path, built, None)["long"]
    assert line["cards"] == 4 and line["n"] == 3 and line["fill"] == 0.75 and line["mean_r"] == -1.0   # void 不计
    assert line["replay_stale"] is False and line["replay_mean_r"] == ss.base_rates()["long"]["mean_r"]["v"]
    stale = ss.live_line(tmp_path, ["some_other_rule"], None)["long"]
    assert stale["replay_stale"] is True and stale["replay_mean_r"] is None and stale["replay_fill"] is None


def test_intraday_view_carries_live_vs_replay(tmp_path, monkeypatch, cal):
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 11, 9, 12, 0, tzinfo=us_clock.tz))
    for i in range(9):
        d = date(2026, 10, 19) + timedelta(days=i)
        ids.day_path(tmp_path, d).parent.mkdir(parents=True, exist_ok=True)
        ids.day_path(tmp_path, d).write_text(json.dumps({"day": d.isoformat(), "cards": [
            {"card_id": f"ids-{d}-X", "symbol": "X", "day": d.isoformat(), "status": "closed", "r": -1.0, "exit_at": f"{d}T10:00"},
            {"card_id": f"ids-{d}-Y", "symbol": "Y", "day": d.isoformat(), "status": "closed", "r": -1.0, "backfill": True}],
            "checks": [], "errors": []}))
    v = ids.build_live(tmp_path)
    assert v["stats"]["n"] == 9 and v["live"]["n"] == 9 and v["live"]["days"] == 9     # 回补的不计
    assert v["live"]["replay_mean_r"] == ids.ODDS["mean_r"] and v["live"]["diverged"] is True


def test_swing_settlement_table_has_shadow_block(tmp_path):
    t = ss.table(tmp_path)
    assert t["contract"] == ss.CONTRACT and set(t["shadow"]["groups"]) == {"cut:long", "cut:short", "gate_4h:long", "gate_4h:short"}


# ---------- 8. 09:45 那根 15 分钟K 的数据层 ----------

def test_open15_store_roundtrip_and_refresh(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(provider, symbols, days, threads=4):
        calls.append((tuple(symbols), tuple(days)))
        rows = [(s, datetime(d.year, d.month, d.day, 13, 45, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),   # 10 月 = 夏令时
                 10.0, 11.0, 9.0, 10.5, 100.0, d.isoformat()) for s in symbols for d in days]
        return pl.DataFrame(rows, schema=["symbol", "t", "open", "high", "low", "close", "volume", "day"], orient="row")
    monkeypatch.setattr(kline_4h, "fetch_open15", fake_fetch)
    hist = [date(2026, 10, 1) + timedelta(days=i) for i in range(20)]
    monkeypatch.setattr(kline_4h, "trading_days", lambda repo, s, e: [d for d in hist if s <= d <= e])
    r = kline_4h.refresh_open15(tmp_path, None, None, ["AAA", "BBB"], hist[-1], days_back=3, min_days=10)
    assert calls[0] == (("AAA", "BBB"), tuple(hist[-3:])) and calls[1] == (("AAA", "BBB"), tuple(hist[-10:]))   # 最近 3 日 + 不足的补 10 日
    assert r["backfilled"] == 2
    df = kline_4h.load_open15(tmp_path, ["AAA"], hist[0], hist[-1])
    assert df.height == 10 and df["symbol"].unique().to_list() == ["AAA"]
    calls.clear()
    kline_4h.refresh_open15(tmp_path, None, None, ["AAA", "BBB"], hist[-1], days_back=3, min_days=10)
    assert len(calls) == 1 and kline_4h.load_open15(tmp_path, None, hist[0], hist[-1]).height == 20    # 幂等, 够数不再补
    assert kline_4h.refresh_open15(tmp_path, None, None, [], hist[-1]) == {"symbols": 0}
    repo = SimpleNamespace(get_daily_batch=lambda *a, **k: pl.DataFrame())
    assert ss._open15_days(tmp_path, repo, "AAA", hist[-2], hist[-1])[hist[-1]] == (10.0, 11.0, 9.0, 10.5)


def test_core_m30_refresh_also_refreshes_open15_and_survives_its_failure(tmp_path, monkeypatch):
    """没有 4h 来源时 (开源核心版) 30 分钟K 由 _refresh_m30 单独刷新: 09:45 那根要一起刷; 它失败不能丢掉 30 分钟K 的结果。"""
    import app.data_providers.custom as custom
    from app.services import decision_scan_job as job
    seen = []
    monkeypatch.setattr(custom, "get_provider", lambda name: "PROVIDER")
    monkeypatch.setattr(kline_4h, "refresh_recent", lambda *a, **k: {"bars": 1})
    monkeypatch.setattr(kline_4h, "ensure_history", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(kline_4h, "refresh_open15", lambda dd, repo, prov, syms, day: seen.append((prov, tuple(syms), day)) or {"bars": 2})
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    meta: dict = {}
    job._refresh_m30(repo, date(2026, 11, 9), {"BBB", "AAA"}, meta)
    assert seen == [("PROVIDER", ("AAA", "BBB"), date(2026, 11, 9))]
    assert meta["m30"] == {"refresh": {"bars": 1}, "history": {"ok": True}, "open15": {"bars": 2}}

    def boom(*a, **k):
        raise RuntimeError("no 15 min bars")
    monkeypatch.setattr(kline_4h, "refresh_open15", boom)
    meta = {}
    job._refresh_m30(repo, date(2026, 11, 9), {"AAA"}, meta)
    assert meta["m30"]["refresh"] == {"bars": 1} and meta["m30"]["open15_error"].startswith("RuntimeError")
    assert "error" not in meta["m30"]
