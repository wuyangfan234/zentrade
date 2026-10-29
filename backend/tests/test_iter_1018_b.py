"""0.26.0 第③批 (10-18 产品决策): 机会页折中方案 + 每日自动对账。

- 每张上过板的卡自动出结局 (成交、没成交都算), 规则与纸面账户同一套 (30 分钟K, 2 日入场窗, 1R 保本 2R 锁 1R, 第 5 天 15:30)。
- 随时有效的置信区间: 不设样本门槛, 任何时候都能看。
- 卡上加一行基础率数字; 改版不清零 (结局按 contract 分文件)。
- 每日对账: 生产信号表重算的板 vs 下单簿, 不一致写 reconcile.jsonl 并推送。
"""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from test_swing_book_tight import SIG_DAY, _repo, _row, _setup, _snapshot

from app.market_clock import us_clock
from app.services import swing_desk as sd
from app.services import swing_settlement as ss
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def _day(o, h, lo, c, n=13):
    """一天 13 根 30 分钟K: 第一根 (09:30) 给 (o,h,lo,c), 其余平盘在收盘价。"""
    return [(o, h, lo, c)] + [(c, c, c, c)] * (n - 1)


def _flat(px, n=13):
    return [(px, px, px, px)] * n


# ---------- 回放纯函数 ----------

def test_replay_no_fill_when_zone_never_touched():
    days = [_flat(105.0), _flat(104.0)]
    assert ss.replay_card("long", (99.0, 100.0), 98.0, 104.0, days)["status"] == "no_fill"


def test_replay_skips_first_bar_of_each_day():
    """09:30-10:00 那根碰到挂单区不算成交 (生产 09:45 起才成交)。"""
    d1 = [(105.0, 105.0, 99.5, 104.0), *_flat(104.0, 12)]
    d2 = _flat(104.0)
    assert ss.replay_card("long", (99.0, 100.0), 98.0, 104.0 + 10, [d1, d2])["status"] == "no_fill"


def test_replay_target_and_r():
    zone, stop, tgt = (99.0, 100.0), 98.5, 103.0               # 中值 99.5, 每股风险 1.0
    d1 = [_flat(101.0)[0], (100.2, 100.4, 99.8, 100.0), *_flat(100.0, 11)]    # 第二根碰区: 成交价 = 开盘 100.2 夹到 100.0
    d2 = [(100.5, 103.2, 100.4, 103.0), *_flat(103.0, 12)]      # 第二天第一根盘中触目标
    r = ss.replay_card("long", zone, stop, tgt, [d1, d2])
    assert r["status"] == "closed" and r["why"] == "target" and r["fill_day"] == 0 and r["exit_day"] == 1
    entry = 100.0 * (1 + ss.SLIP)
    assert r["r"] == pytest.approx((103.0 * (1 - ss.SLIP) - entry) / 1.0, abs=1e-4)


def test_replay_close_below_stop_exits_next_open():
    zone, stop, tgt = (99.0, 100.0), 98.0, 110.0
    d1 = [_flat(101.0)[0], (100.0, 100.0, 99.5, 99.6), *_flat(97.5, 11)]    # 收盘 97.5 < 止损 98
    d2 = [(97.0, 97.2, 96.8, 97.0), *_flat(97.0, 12)]
    r = ss.replay_card("long", zone, stop, tgt, [d1, d2])
    assert r["why"] == "stop" and r["exit_day"] == 1 and r["exit"] == pytest.approx(97.0 * (1 - ss.SLIP), abs=1e-4)


def test_replay_breakeven_after_1r_and_time_exit_on_day5():
    zone, stop, tgt = (99.0, 100.0), 98.5, 104.0              # 风险 1.0
    d1 = [_flat(101.0)[0], (100.0, 100.0, 99.9, 100.0), *_flat(100.0, 11)]
    d2 = [(100.5, 101.2, 100.4, 101.0), *_flat(101.0, 12)]      # 浮盈 > 1R -> 止损移到保本
    d3 = [*_flat(100.0)[:-1], (100.0, 100.0, 99.0, 99.2)]      # 收盘 99.2 < 保本 ~100.05 -> 次日开盘离场
    d4 = [(99.3, 99.3, 99.3, 99.3), *_flat(99.3, 12)]
    r = ss.replay_card("long", zone, stop, tgt, [d1, d2, d3, d4])
    assert r["why"] == "stop" and r["exit_day"] == 3            # 保本止损, 不是原止损 98.5
    days = [d1] + [_flat(100.5)] * 5
    t = ss.replay_card("long", zone, stop, tgt, days)
    assert t["why"] == "time" and t["exit_day"] == 4            # 成交日算第 1 天, 第 5 天离场


def test_replay_short_mirror():
    zone, stop, tgt = (100.0, 101.0), 102.0, 97.0
    d1 = [_flat(99.0)[0], (100.5, 100.8, 100.2, 100.5), *_flat(100.5, 11)]
    d2 = [(99.0, 99.2, 96.5, 97.0), *_flat(97.0, 12)]
    r = ss.replay_card("short", zone, stop, tgt, [d1, d2])
    assert r["status"] == "closed" and r["why"] == "target" and r["r"] > 0


def test_replay_pending_and_holding_until_enough_bars():
    assert ss.replay_card("long", (99.0, 100.0), 98.0, 104.0, [_flat(105.0)])["status"] == "pending"
    d1 = [_flat(101.0)[0], (100.0, 100.0, 99.9, 100.0), *_flat(100.0, 11)]
    assert ss.replay_card("long", (99.0, 100.0), 98.5, 104.0, [d1, _flat(100.2)])["status"] == "holding"


# ---------- 随时有效区间 ----------

def test_cs_proportion_properties():
    assert ss.cs_proportion(0, 0) == (0.0, 1.0)
    lo, hi = ss.cs_proportion(50, 100)
    assert lo < 0.5 < hi
    w = [ss.cs_proportion(n // 2, n) for n in (30, 100, 300, 1000)]
    widths = [b - a for a, b in w]
    assert widths == sorted(widths, reverse=True)               # 样本越多越窄
    assert ss.cs_proportion(0, 40)[0] >= 0 and ss.cs_proportion(40, 40)[1] <= 1


def test_cs_is_anytime_valid_simulation():
    """蒙特卡洛: 真实比例 0.3, 200 条路径各 400 步, 任一时刻越界的路径比例 <= 5% (随时有效)。"""
    import random
    rg = random.Random(3)
    bad = 0
    for _ in range(200):
        k = 0
        crossed = False
        for n in range(1, 401):
            k += rg.random() < 0.3
            if n % 10 == 0:
                lo, hi = ss.cs_proportion(k, n)
                if not (lo <= 0.3 <= hi):
                    crossed = True
                    break
        bad += crossed
    assert bad / 200 <= 0.05


def test_summarize_rows_and_overlap():
    rows = [{"status": "no_fill"}] * 4 + [{"status": "closed", "why": "target", "r": 2.0}] * 3 + \
           [{"status": "closed", "why": "stop", "r": -1.0}] * 3
    s = ss.summarize_rows(rows)
    assert s["cards"] == 10 and s["filled"] == 6 and s["fill"]["p"] == 0.6 and s["win"]["p"] == 0.5
    assert s["target"]["k"] == 3 and s["stop"]["k"] == 3 and s["time"]["k"] == 0
    assert s["mean_r"]["v"] == 0.5 and s["mean_r"]["ci"][0] < 0.5 < s["mean_r"]["ci"][1]
    assert ss.overlap([0.1, 0.2], [0.3, 0.4]) is False and ss.overlap([0.1, 0.35], [0.3, 0.4]) is True
    assert ss.overlap(None, [0, 1]) is None


# ---------- 生产结算 (只写 research/swing_settle/) ----------

def _book_with_card(tmp_path, monkeypatch, sym="AAA", close=100.0, atr=2.0):
    _setup(tmp_path, monkeypatch)
    _snapshot(tmp_path, SIG_DAY, [_row("macd_golden", sym), _row("phantom_4h", sym)], {sym}, {sym: close}, {sym: atr})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={sym}, pm=wv.permission(tmp_path))
    assert wb.save_day(tmp_path, built) == 1
    return built["cards"][0]


def test_settle_writes_once_and_never_rewrites(tmp_path, monkeypatch, cal):
    card = _book_with_card(tmp_path, monkeypatch)
    lo, hi = card["entry_zone"]
    d1 = [_flat(101.0)[0], (hi, hi, lo, hi), *_flat(hi, 11)]
    tgt = card["target_zone"][0]
    d2 = [(hi, tgt + 0.5, hi, tgt), *_flat(tgt, 12)]
    bars = {date(2026, 10, 9): d1, date(2026, 10, 12): d2}
    monkeypatch.setattr(ss, "_m30_days", lambda data_dir, repo, symbol, s, e: {k: v for k, v in bars.items() if s <= k <= e})
    r1 = ss.settle(tmp_path, None, as_of_close=date(2026, 10, 12))
    assert r1["written"] == 1
    rows = ss.read_outcomes(tmp_path)
    assert rows[0]["card_id"] == card["id"] and rows[0]["why"] == "target" and rows[0]["fill_date"] == "2026-10-09"
    assert rows[0]["exit_date"] == "2026-10-12" and rows[0]["contract"] == ss.CONTRACT
    before = ss.outcomes_path(tmp_path).read_text()
    assert ss.settle(tmp_path, None, as_of_close=date(2026, 10, 13))["written"] == 0     # 已写的行不再写
    assert ss.outcomes_path(tmp_path).read_text() == before


def test_settle_no_fill_after_two_days(tmp_path, monkeypatch, cal):
    _book_with_card(tmp_path, monkeypatch)
    bars = {date(2026, 10, 9): _flat(110.0), date(2026, 10, 12): _flat(111.0)}
    monkeypatch.setattr(ss, "_m30_days", lambda data_dir, repo, symbol, s, e: {k: v for k, v in bars.items() if s <= k <= e})
    assert ss.settle(tmp_path, None, as_of_close=date(2026, 10, 9))["pending"] == 1      # 第 1 天: 入场窗还没走完
    assert ss.settle(tmp_path, None, as_of_close=date(2026, 10, 12))["written"] == 1
    assert ss.read_outcomes(tmp_path)[0]["status"] == "no_fill"


def test_settle_missing_bars_not_treated_as_no_fill(tmp_path, monkeypatch, cal):
    _book_with_card(tmp_path, monkeypatch)
    monkeypatch.setattr(ss, "_m30_days", lambda *a, **k: {})                    # 30 分钟K 缺
    r = ss.settle(tmp_path, None, as_of_close=date(2026, 10, 12))
    assert r["written"] == 0 and r["missing"] == 1 and ss.read_outcomes(tmp_path) == []


def test_contract_bump_keeps_old_outcomes(tmp_path, monkeypatch, cal):
    """改版不清零: 新 contract 写新文件, 旧文件原样保留。"""
    _book_with_card(tmp_path, monkeypatch)
    bars = {date(2026, 10, 9): _flat(110.0), date(2026, 10, 12): _flat(111.0)}
    monkeypatch.setattr(ss, "_m30_days", lambda data_dir, repo, symbol, s, e: {k: v for k, v in bars.items() if s <= k <= e})
    ss.settle(tmp_path, None, as_of_close=date(2026, 10, 12))
    cur = ss.CONTRACT
    old = ss.outcomes_path(tmp_path).read_text()
    monkeypatch.setattr(ss, "CONTRACT", "swing-settle-v9")
    ss.settle(tmp_path, None, as_of_close=date(2026, 10, 12))
    assert ss.outcomes_path(tmp_path, cur).read_text() == old
    assert ss.outcomes_path(tmp_path, "swing-settle-v9").exists()


# ---------- 卡面一行 + 回测页卡结局 ----------

@pytest.mark.full_edition
def test_card_line_from_base_rates():
    line = ss.card_line("long")
    assert line is not None and set(line) >= {"fill", "win", "target", "stop", "time", "contract"}
    assert 0 < line["fill"] < 1 and line["contract"] == "swing-desk-tight-v1"
    assert abs(line["target"] + line["stop"] + line["time"] - 1) < 0.01
    assert ss.card_line("short") is not None


@pytest.mark.full_edition
def test_card_line_marks_stale_when_current_rules_differ():
    """基础率是 10-15 那套 3+3 的回放成绩; 当前 3+3 改了 -> stale (卡面标“旧 3+3”), 顺序不同不算改。"""
    built = ss.base_rates()["rules"]["long"]
    assert ss.card_line("long", tuple(reversed(built)))["stale"] is False
    assert ss.card_line("long", ("macd_golden", "ibs_mean_reversion"))["stale"] is True
    assert ss.card_line("long")["stale"] is False                      # 不给当前 3+3 -> 不判


@pytest.mark.full_edition
def test_live_board_cards_carry_base_line(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    _snapshot(tmp_path, SIG_DAY, [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA")], {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})
    lg = SimpleNamespace(open_positions=[], read_events=lambda: [])
    from app.services import desk_overview as ov
    monkeypatch.setattr(ov, "_account", lambda loop, data_dir: (lg, None))
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: {})
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 9, 9, 40, tzinfo=us_clock.tz))
    out = sd.build_live(tmp_path, repo=object(), today=date(2026, 10, 9))
    assert out["base_rates"]["long"]["fill"] == ss.card_line("long")["fill"]
    assert out["base_rates"]["short"] is not None
    assert out["base_rates"]["long"]["stale"] is True                  # _setup 的 3+3 = macd_golden, 不是基础率那套


@pytest.mark.full_edition
def test_table_flags_mismatch_when_intervals_do_not_overlap(tmp_path, monkeypatch):
    rows = [{"card_id": f"c{i}", "side": "long", "status": "no_fill", "signal_day": "2026-10-09", "symbol": "A"}
            for i in range(300)]
    p = ss.outcomes_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("\n".join(json.dumps(r) for r in rows))
    t = ss.table(tmp_path)
    long = t["sides"]["long"]
    assert long["actual"]["cards"] == 300 and long["actual"]["fill"]["p"] == 0.0
    assert "fill" in long["mismatch"]                           # 回放回区 59%, 实际 0% -> 区间不重叠 = 撮合模型要修
    assert t["sides"]["short"]["actual"]["cards"] == 0 and t["sides"]["short"]["mismatch"] == []


@pytest.mark.full_edition
def test_settlement_api_returns_swing_table(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.opportunities import router
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    d = TestClient(app).get("/api/opportunities/swing-settlement").json()
    assert d["contract"] == ss.CONTRACT and set(d["sides"]) == {"long", "short"}
    assert d["sides"]["long"]["replay"]["cards"] > 0


# ---------- 每日对账 ----------

def _same_as_prod(day, syms, rules):
    """回放 = 生产 (信号层一致): 直接按信号表里的命中返回。"""
    snap = sd.load_watchlist_snapshot(_same_as_prod.dir, day) or {}
    out = {}
    for side, ids in rules.items():
        for r in ids:
            key = r if side == "long" else f"{r}:short"
            out[key] = {x["symbol"] for x in snap.get("signals") or [] if x.get("rule") == r and (x.get("side") or "long") == side}
    return out


def test_reconcile_ok_when_book_matches(tmp_path, monkeypatch, cal):
    _book_with_card(tmp_path, monkeypatch)
    sent = []
    from app.services import notify_opportunity as no
    monkeypatch.setattr(no, "_enqueue", lambda d, k, p: sent.append((k, p)) or True)
    _same_as_prod.dir = tmp_path
    out = ss.reconcile(tmp_path, object(), day=SIG_DAY, replay=_same_as_prod)
    assert out["ok"] is True and out["issues"] == [] and out["booked"] == 1 and out["recomputed"] == 1
    assert out["signal_check"]["macd_golden"] == {"prod": 1, "replay": 1}
    assert sent == []
    assert ss.read_reconcile(tmp_path)[0]["day"] == SIG_DAY.isoformat()


def test_reconcile_signal_layer_flags_production_vs_replay(tmp_path, monkeypatch, cal):
    """Q2 原意: 生产实际出的信号 vs 回放用同样数据算出的信号。0.25.0 前 connors_rsi2 生产 0 命中就是这一类:
    回放有、生产没出 -> signal_missing (先按生产同一成卡函数确认“出得了卡”); 生产有、回放无 -> signal_extra。"""
    _book_with_card(tmp_path, monkeypatch)
    sent = []
    from app.services import notify_opportunity as no
    monkeypatch.setattr(no, "_enqueue", lambda d, k, p: sent.append((k, p)) or True)
    monkeypatch.setattr(ss, "_cardable", lambda data_dir, repo, day, syms, side, stop=None: set(syms) - {"NOCARD"})
    rep = lambda day, syms, rules: {"macd_golden": {"AAA", "BBB", "NOCARD"}, "tv_halftrend:short": set()}  # noqa: E731
    out = ss.reconcile(tmp_path, object(), day=SIG_DAY, replay=rep)
    kinds = {(x["kind"], x.get("symbol")) for x in out["issues"]}
    assert ("signal_missing", "BBB") in kinds and ("signal_missing", "NOCARD") not in kinds
    assert out["ok"] is False and sent and "回放有、生产没出 1 处" in sent[0][1]["body"]
    rep2 = lambda day, syms, rules: {"macd_golden": set()}  # noqa: E731
    out2 = ss.reconcile(tmp_path, object(), day=SIG_DAY, replay=rep2, notify=False)
    assert [x["kind"] for x in out2["issues"]] == ["signal_extra"] and out2["issues"][0]["symbol"] == "AAA"


def test_reconcile_signal_replay_unavailable_is_not_an_issue(tmp_path, monkeypatch, cal):
    _book_with_card(tmp_path, monkeypatch)

    def boom(day, syms, rules):
        raise RuntimeError("no matrix")
    out = ss.reconcile(tmp_path, object(), day=SIG_DAY, replay=boom, notify=False)
    assert out["signal_check"] == "unavailable" and out["ok"] is True


def test_reconcile_flags_book_card_not_recomputed_and_notifies(tmp_path, monkeypatch, cal):
    _book_with_card(tmp_path, monkeypatch)
    # 信号表被改写 (票还在自选, 但重算后规则不再命中) -> 簿卡对不上
    _snapshot(tmp_path, SIG_DAY, [], {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})
    sent = []
    from app.services import notify_opportunity as no
    monkeypatch.setattr(no, "_enqueue", lambda d, k, p: sent.append((k, p)) or True)
    out = ss.reconcile(tmp_path, object(), day=SIG_DAY, replay=lambda d, syms, rules: {})
    assert out["ok"] is False and out["issues"][0]["kind"] == "book_card_not_recomputed"
    assert sent and sent[0][0] == f"swing_reconcile:{SIG_DAY.isoformat()}" and "机会板对账" in sent[0][1]["title"]


def test_reconcile_board_layer_uses_latest_saved_book(tmp_path, monkeypatch, cal):
    """生产时序: 当天收盘对账时, 当天的下单簿还没落 (下一交易日北京 20:00 才落) -> 核已落盘的最近一本簿。"""
    _book_with_card(tmp_path, monkeypatch)
    nxt = date(2026, 10, 9)
    _snapshot(tmp_path, nxt, [], {"AAA"}, {"AAA": 101.0}, {"AAA": 2.0})
    out = ss.reconcile(tmp_path, object(), day=nxt, notify=False, replay=lambda d, syms, rules: {})
    assert out["board_day"] == SIG_DAY.isoformat() and out["booked"] == 1 and out["ok"] is True


def test_reconcile_ignores_cards_removed_from_watchlist(tmp_path, monkeypatch, cal):
    _book_with_card(tmp_path, monkeypatch)
    _snapshot(tmp_path, SIG_DAY, [], set(), {}, {})              # 移出自选: 已知差异
    out = ss.reconcile(tmp_path, object(), day=SIG_DAY, notify=False, replay=lambda d, syms, rules: {})
    assert out["ok"] is True


@pytest.mark.full_edition
def test_reconcile_lists_zero_hit_only_beyond_history_max(tmp_path, monkeypatch, cal):
    """连续 0 命中只在超过回放里的历史最长时列出: SMC 结构历史上最长 64 天不命中, 连续 14 天不该列;
    macd_golden 历史最长 23 天, 连续 25 天才列。"""
    _setup(tmp_path, monkeypatch)
    from datetime import timedelta
    d = SIG_DAY
    hist = ss.rule_hit_rates()
    assert hist["tv_smc_structure"]["max_zero_run"] == 64 and hist["macd_golden"]["max_zero_run"] == 23
    for i in range(25):
        day = d - timedelta(days=i)
        _snapshot(tmp_path, day, [_row("phantom_4h", "AAA")], {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})   # macd_golden 0 命中
    out = ss.reconcile(tmp_path, object(), day=d, notify=False, replay=lambda dd, syms, rules: {})
    assert out["rule_hits"]["macd_golden"] == 0
    assert out["zero_hit_rules"]["macd_golden"] == {"days": 25, "history_max": 23}
    out14 = ss.reconcile(tmp_path, object(), day=d - timedelta(days=11), notify=False, replay=lambda dd, syms, rules: {})
    assert "macd_golden" not in (out14.get("zero_hit_rules") or {})   # 14 天 < 历史最长 23 天
    assert out14["zero_hit_rules"]["tv_halftrend:short"] == {"days": 14, "history_max": 10}   # 空头 halftrend 历史最长 10 天
