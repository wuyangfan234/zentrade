# ruff: noqa: RUF001
"""0.39.0 机会页雷达 (opp_radar): 只读投影 + 帧 + SSE。

不变量:
- 一股一态: 同一只票在 blips 里只出现一次; 持仓票的挂单卡不另出点; 观察层只作附注。
- 名额镜像与 entry_guard 同式 (book_caps / sector_side_count 只数机会板仓); 名额满时挂单显示为受限 (sector_side_cap)。
- 预警: 距区 <= 0.25 ATR 的候选多于空位才进入; 上一帧已预警时, 距区 <= 0.5 ATR 的候选仍多于空位就保持 (撤销带缓冲)。
- /page 请求不写任何文件 (帧只由定时任务写); 交易链 (trading_loop / entry_guard / watchlist_book / swing_verdict /
  notify_opportunity / premarket_gap / pm_rank / trader) 不引用 opp_radar。
- 帧: 状态不变不重复写 (心跳除外); 状态变了推 SSE; 坏行跳过; 文件轮转保留最近 KEEP_FILES 天。
"""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ledger.core import Account
from app.market_clock import us_clock
from app.services import opp_radar as orad

ET = us_clock.tz
TODAY = datetime(2026, 10, 23).date()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_pipeline_schedule", lambda: {"hour": 16, "minute": 35})
    orad._LAST.clear()
    orad._STATS.clear()
    yield


def _pos(sym, qty, avg, stop, card="swing", entry="2026-10-22"):
    return SimpleNamespace(symbol=sym, qty=qty, avg_cost=avg, stop_price=stop, entry_date=entry,
                           card_id=f"opp-{entry}-{sym}-book-{card}-{'short-' if qty < 0 else ''}abcd")


def _fill(seq, sym, side, qty, price, stop, day="2026-10-22", at=None, max_days=5):
    return {"seq": seq, "kind": "fill", "symbol": sym, "side": side, "qty": qty, "price": price, "stop_price": stop,
            "trade_date": day, "at": at or f"{day}T10:00:00-04:00", "max_holding_days": max_days,
            "card_id": f"opp-{day}-{sym}-book-swing-x", "target_zone": [1, 2]}


def _card(sym, side="short", price=100.0, zone=(101.0, 102.0), atr=4.0, **kw):
    return {"symbol": sym, "side": side, "price": price, "entry_zone": list(zone), "atr": atr, "stop": zone[1] + 5 if side == "short" else zone[0] - 5,
            "target": 90.0, "risk": 5.0, "state": "pending", "verdict": "order", "signal_day": "2026-10-22", "entry_days": 2,
            "entry_day_n": 1, "hold_days": 5, "card_id": f"c-{sym}", "price_source": "live", "strategies": [{"name": "R"}], **kw}


def _swing(*cards, risk=0.005, equity=100_000.0):
    return {"long": [c for c in cards if c["side"] == "long"], "short": [c for c in cards if c["side"] == "short"],
            "risk_pct": risk, "equity": equity, "entry_day": TODAY.isoformat()}


WL = [{"symbol": s, "group_ids": ["g1"]} for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III")]
GROUPS = [{"id": "g0", "name": "核心"}, {"id": "g1", "name": "芯片"}]
TECH = {s: "Technology" for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH")}


def _project(now, *, swing=None, positions=(), events=(), frames=(), sectors=None, intraday=None, radar=None, gaps=None,
             account=None, loop_blocks=None, gate="green", open_risk=0.0):
    return orad.project(now=now, swing=swing or _swing(), intraday=intraday, radar=radar, gaps=gaps, positions=list(positions),
                        events=list(events), account=account or Account(max_positions=6), sectors=TECH if sectors is None else sectors,
                        watch_rows=WL, groups=GROUPS, frames=list(frames), gate=gate, open_risk=open_risk, loop_blocks=loop_blocks)


def _b(view, sym):
    xs = [b for b in view["blips"] if b["symbol"] == sym]
    assert len(xs) == 1, (sym, xs)
    return xs[0]


# ---------- 环 / 一股一态 ----------

def test_rings_by_distance_and_one_state_per_symbol():
    now = datetime(2026, 10, 23, 10, 15, tzinfo=ET)
    sw = _swing(_card("AAA", price=101.5),                     # 在区 d<=0 → 执行
                _card("BBB", price=100.0),                     # 距区 (101-100)/4 = 0.25 → 临近
                _card("CCC", price=95.0),                      # 1.5 ATR → 等待
                _card("DDD", price=100.0, state="holding"))     # 持仓卡: 不出挂单点
    v = _project(now, swing=sw, positions=[_pos("DDD", -10, 100.0, 105.0)], events=[_fill(1, "DDD", "short", 10, 100.0, 105.0)],
                 intraday={"active": [{"symbol": "AAA", "r_now": 0.3}], "checks_today": [], "today": TODAY.isoformat()})
    syms = [b["symbol"] for b in v["blips"]]
    assert len(syms) == len(set(syms))                          # 一股一态
    assert (_b(v, "AAA")["ring"], _b(v, "AAA")["kind"]) == (0, "order")
    assert (_b(v, "BBB")["ring"], _b(v, "BBB")["d"]) == (1, 0.25)
    assert _b(v, "CCC")["ring"] == 2
    assert _b(v, "DDD")["kind"] in ("hold", "exit") and "order" not in _b(v, "DDD")
    assert [n["src"] for n in _b(v, "AAA")["also"]] == ["intraday"]   # 观察层只作附注
    assert _b(v, "EEE")["kind"] == "quiet" and _b(v, "EEE")["ring"] == 4


def test_hold_reads_ledger_stop_not_card_and_exit_ring():
    now = datetime(2026, 10, 23, 11, 0, tzinfo=ET)
    sw = _swing(_card("AAA", price=26.0, state="holding", atr=2.0, zone=(27.5, 28.0)))
    ev = [_fill(1, "AAA", "short", 198, 27.6862, 30.29, day="2026-10-19"), {"seq": 2, "kind": "stop_move", "symbol": "AAA", "stop_price": 27.6811,
                                                                            "at": "2026-10-22T10:32:00-04:00"}]
    v = _project(now, swing=sw, positions=[_pos("AAA", -198, 27.6862, 27.6811, entry="2026-10-19")], events=ev)
    h = _b(v, "AAA")["hold"]
    assert h["stop_now"] == 27.6811 and h["stop_initial"] == 30.29 and h["stop_moved"] is True
    assert h["exit_day"] == "2026-10-23" and _b(v, "AAA")["kind"] == "exit" and _b(v, "AAA")["ring"] == 0   # 第 5 天 = 今天 15:30 平仓
    assert h["exit_at"] == "15:30" and h["day_n"] == 5


def test_exit_intent_puts_holding_in_exec_ring():
    now = datetime(2026, 10, 23, 8, 0, tzinfo=ET)
    ev = [_fill(1, "AAA", "buy", 10, 100.0, 95.0), {"seq": 5, "kind": "exit_intent", "symbol": "AAA", "reason": "invalidation",
                                                     "trade_date": "2026-10-22", "at": "2026-10-22T16:00:00-04:00"}]
    v = _project(now, swing=_swing(_card("AAA", side="long", price=94.0, state="holding", zone=(99, 100))),
                 positions=[_pos("AAA", 10, 100.0, 95.0)], events=ev)
    b = _b(v, "AAA")
    assert b["kind"] == "exit" and b["hold"]["exit_code"] == "exit_next_open" and b["hold"]["exit_reason"] == "invalidation"


# ---------- 名额镜像 ----------

def test_sector_side_full_mirrors_entry_guard_and_blocks_orders():
    """0.5% 风险 → 同板块同向 4 个; 4 个机会板空头仓占满时, 科技空挂单显示为受限 (与 entry_guard 同一原因码)。"""
    from app.services import entry_guard
    now = datetime(2026, 10, 23, 10, 39, tzinfo=ET)
    pos = [_pos(s, -10, 100.0, 105.0) for s in ("AAA", "BBB", "CCC", "DDD")]
    ev = [_fill(i, s, "short", 10, 100.0, 105.0) for i, s in enumerate(("AAA", "BBB", "CCC", "DDD"), 1)]
    sw = _swing(_card("EEE", price=101.5), _card("FFF", price=99.0))
    v = _project(now, swing=sw, positions=pos, events=ev)
    slot = v["caps"]["slots"][0]
    ledger = SimpleNamespace(open_positions=pos)
    assert slot["cap"] == entry_guard.book_caps(0.005)[1] == 4
    assert slot["used"] == entry_guard.sector_side_count(ledger, TECH, "Technology", "short", evo_only=True, rule="swing") == 4
    assert slot["state"] == "full" and slot["waiting"] == ["EEE", "FFF"]
    for s in ("EEE", "FFF"):
        b = _b(v, s)
        assert b["kind"] == "blocked" and b["codes"] == ["sector_side_cap"] and b["ring"] >= 1   # 受限永不进执行环
    assert v["verdict"]["code"] == "cap_full" and v["verdict"]["blocked"] == ["EEE", "FFF"]


def test_legacy_positions_do_not_take_swing_slots():
    """名额只数机会板仓 (entry_guard evo_only=True, rule=swing): 旧规则仓不占名额。"""
    now = datetime(2026, 10, 23, 10, 39, tzinfo=ET)
    pos = [_pos(s, -10, 100.0, 105.0, card="phantom_4h") for s in ("AAA", "BBB", "CCC", "DDD")]
    v = _project(now, swing=_swing(_card("EEE", price=101.5)), positions=pos)
    assert _b(v, "EEE")["kind"] == "order"
    assert v["caps"]["slots"][0]["used"] == 0


def test_paper_reason_codes_are_split_and_unknown_sector_is_blocked():
    now = datetime(2026, 10, 23, 10, 39, tzinfo=ET)
    sw = _swing(_card("AAA", price=101.5, blocked="paper_watch", paper_reason="准入阻断：sector_side_cap；daily_open_cap"),
                _card("GGG", price=101.5))
    v = _project(now, swing=sw, sectors={"AAA": "Technology"})
    assert _b(v, "AAA")["codes"] == ["sector_side_cap", "daily_open_cap"]
    assert _b(v, "GGG")["codes"] == ["sector_unknown"]          # 板块查不到 = 不开 (entry_guard fail-closed)


def test_caution_enters_at_quarter_atr_and_holds_until_half_atr():
    now = datetime(2026, 10, 23, 10, 0, tzinfo=ET)
    pos = [_pos(s, -10, 100.0, 105.0) for s in ("AAA", "BBB", "CCC")]       # 3/4: 余 1
    near = _swing(_card("EEE", price=100.2), _card("FFF", price=100.4))     # d = 0.2 / 0.15 ATR → 2 个临近 > 1 空位
    v1 = _project(now, swing=near, positions=pos)
    assert v1["caps"]["slots"][0]["state"] == "caution" and v1["verdict"]["code"] == "cap_caution"
    frame = orad.frame_of(v1)
    # 距离退到 0.3 / 0.4 ATR (> 0.25 但 <= 0.5): 上一帧预警 → 保持
    mid = _swing(_card("EEE", price=99.8), _card("FFF", price=99.4))
    v2 = _project(now, swing=mid, positions=pos, frames=[frame])
    assert v2["caps"]["slots"][0]["state"] == "caution"
    # 没有上一帧预警时同样的距离只是偏紧
    v3 = _project(now, swing=mid, positions=pos)
    assert v3["caps"]["slots"][0]["state"] == "tight"
    # 退到 0.5 ATR 以外: 撤销
    far = _swing(_card("EEE", price=98.0), _card("FFF", price=97.0))
    v4 = _project(now, swing=far, positions=pos, frames=[orad.frame_of(v2)])
    assert v4["caps"]["slots"][0]["state"] == "tight"


def test_daily_cap_and_loop_global_block_are_mirrored_in_window_only():
    now = datetime(2026, 10, 23, 11, 0, tzinfo=ET)
    sw = _swing(_card("AAA", price=101.5), risk=0.01)                      # 1% → 单日 5 笔
    fills = [_fill(i, f"Z{i}", "buy", 1, 10.0, 9.0, day="2026-10-23", at="2026-10-23T09:50:00-04:00") for i in range(5)]
    v = _project(now, swing=sw, events=fills, sectors={**TECH})
    assert v["caps"]["opens"] == {"used": 5, "cap": 5, "day": "2026-10-23"}
    assert _b(v, "AAA")["codes"] == ["daily_open_cap"]
    v2 = _project(now, swing=_swing(_card("AAA", price=101.5)), loop_blocks={"*": ["circuit_breaker_day_halt"]})
    assert _b(v2, "AAA")["codes"] == ["circuit_breaker_day_halt"] and _b(v2, "AAA")["mirror"] is True
    pre = datetime(2026, 10, 23, 9, 0, tzinfo=ET)                          # 入场窗外: 交易环拦截不适用
    v3 = _project(pre, swing=_swing(_card("AAA", price=101.5)), loop_blocks={"*": ["circuit_breaker_day_halt"]})
    assert _b(v3, "AAA")["kind"] == "order"


def test_market_gate_unavailable_blocks_all_orders():
    now = datetime(2026, 10, 23, 11, 0, tzinfo=ET)
    v = _project(now, swing=_swing(_card("AAA", price=101.5)), gate="unavailable")
    assert _b(v, "AAA")["codes"] == ["market_gate"]


# ---------- 结论 / 时钟 ----------

def test_verdict_codes_by_time():
    sw = _swing(_card("AAA", price=101.5))
    assert _project(datetime(2026, 10, 25, 1, 44, tzinfo=ET), swing=sw)["verdict"]["code"] == "closed"     # 周日
    assert _project(datetime(2026, 10, 23, 9, 0, tzinfo=ET), swing=sw)["verdict"]["code"] == "pre_window"
    assert _project(datetime(2026, 10, 23, 11, 0, tzinfo=ET), swing=sw)["verdict"]["code"] == "orders"
    assert _project(datetime(2026, 10, 23, 15, 32, tzinfo=ET), swing=sw)["verdict"]["code"] == "cutoff"
    assert _project(datetime(2026, 10, 23, 16, 5, tzinfo=ET), swing=sw)["verdict"]["code"] == "closed"


def test_clock_next_event_crosses_weekend_and_has_beijing_ts():
    ck = orad.clock(datetime(2026, 10, 25, 1, 44, tzinfo=ET))
    assert ck["trading_day"] is False and ck["next"]["day"] == "2026-10-26" and ck["next"]["code"] == "board_first"
    assert ck["next"]["at"] == "08:00"                                      # 10-26 仍是夏令时: 北京 20:00 = 美东 08:00
    ck2 = orad.clock(datetime(2026, 11, 2, 9, 0, tzinfo=ET))
    assert ck2["board_first"] == "07:00"                                    # 冬令时: 北京 20:00 = 美东 07:00
    ck3 = orad.clock(datetime(2026, 10, 23, 10, 31, tzinfo=ET))
    assert ck3["next"] == {**ck3["next"], "at": "11:00", "code": "intraday_check"} and ck3["entry_window"] is True


# ---------- 帧 / SSE ----------

def test_write_frame_dedups_and_heartbeats(tmp_path):
    now = datetime(2026, 10, 23, 10, 0, tzinfo=ET)
    v = _project(now, swing=_swing(_card("AAA", price=101.5)))
    r1 = orad.write_frame(tmp_path, v)
    assert r1["changed"] is True and r1["seq"] == 1
    assert orad.write_frame(tmp_path, {**v, "at": "2026-10-23T10:00:15-04:00"}) is None             # 同状态 15 秒后: 不写
    r3 = orad.write_frame(tmp_path, {**v, "at": "2026-10-23T10:06:00-04:00"})                       # 过心跳: 写, 但 changed=False
    assert r3["changed"] is False and r3["seq"] == 2
    v2 = _project(datetime(2026, 10, 23, 10, 7, tzinfo=ET), swing=_swing(_card("AAA", price=99.0)))
    assert orad.write_frame(tmp_path, v2)["changed"] is True
    assert len(orad.load_frames(tmp_path, TODAY)) == 3


def test_frames_drive_changed_and_direction(tmp_path):
    t0 = datetime(2026, 10, 23, 10, 0, tzinfo=ET)
    v0 = _project(t0, swing=_swing(_card("AAA", price=95.0)))
    orad.write_frame(tmp_path, v0)
    t1 = datetime(2026, 10, 23, 10, 30, tzinfo=ET)
    v1 = _project(t1, swing=_swing(_card("AAA", price=100.4)), frames=orad.load_frames(tmp_path, TODAY))
    orad.write_frame(tmp_path, v1)
    t2 = datetime(2026, 10, 23, 10, 30, 5, tzinfo=ET)
    v2 = _project(t2, swing=_swing(_card("AAA", price=100.4)), frames=orad.load_frames(tmp_path, TODAY))
    b = _b(v2, "AAA")
    assert b["changed_at"] == "2026-10-23T10:30:00-04:00"                   # 等待 → 临近 发生在第二帧
    assert b["dir"] == "in"
    assert "trail" not in b and "timeline" not in v2 and "feed" not in v2 and "arcs" not in v2      # 0.41.0 雷达图退役


def test_bad_frame_lines_are_skipped_and_rotation_keeps_recent(tmp_path):
    d = orad.frames_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "2026-10-23.jsonl").write_text('{"at":"2026-10-23T10:00:00-04:00","b":[]}\n{bad\n{"at":1}\n')
    assert len(orad.load_frames(tmp_path, TODAY)) == 1
    for i in range(orad.KEEP_FILES + 3):
        (d / f"2026-09-{i + 1:02d}.jsonl").write_text("")
    orad._rotate(tmp_path)
    assert len(list(d.glob("20*.jsonl"))) == orad.KEEP_FILES


def test_tick_pushes_only_on_change_and_respects_window(tmp_path, monkeypatch):
    sent = []
    views = iter([_project(datetime(2026, 10, 23, 10, 0, tzinfo=ET), swing=_swing(_card("AAA", price=95.0))),
                  _project(datetime(2026, 10, 23, 10, 0, 20, tzinfo=ET), swing=_swing(_card("AAA", price=95.0))),
                  _project(datetime(2026, 10, 23, 10, 0, 40, tzinfo=ET), swing=_swing(_card("AAA", price=101.5)))])
    monkeypatch.setattr(orad, "live_view", lambda *a, **k: next(views))
    monkeypatch.setattr(orad, "_cheap_signature", lambda *a: object())     # 每次都不同 → 每次重算
    for s in (0, 20, 40):
        orad.tick(tmp_path, now=datetime(2026, 10, 23, 10, 0, s, tzinfo=ET), notify=sent.append)
    assert [x["seq"] for x in sent] == [1, 2]                               # 第二次状态没变: 不写不推
    assert orad.tick(tmp_path, now=datetime(2026, 10, 24, 10, 0, tzinfo=ET), notify=sent.append) == {"skipped": "window"}
    assert orad.status(tmp_path, datetime(2026, 10, 23, 10, 1, tzinfo=ET))["frames"] == 2


def test_quote_subscriber_carries_latest_radar_frame():
    from app.services.quote_service import QuoteSubscriber
    sub = QuoteSubscriber()
    sub.notify_opp_radar({"at": "a", "seq": 1})
    sub.notify_opp_radar({"at": "b", "seq": 2})
    out = sub.pop()
    assert out["opp_radar"] == {"at": "b", "seq": 2} and sub.pop()["opp_radar"] is None


def test_sse_stream_emits_opp_radar_event():
    import asyncio

    from app.api import intraday
    from app.services.quote_service import QuoteSubscriber

    sub = QuoteSubscriber()
    sub.notify_opp_radar({"at": "2026-10-23T10:00:00-04:00", "seq": 3})
    qs = SimpleNamespace(subscribe=lambda: sub, unsubscribe=lambda s: None, _symbol_count=0)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(quote_service=qs)))
    resp = asyncio.run(intraday.quote_stream(req))

    async def first():
        async for ev in resp.body_iterator:
            return ev
    ev = asyncio.run(first())
    assert ev["event"] == "opp_radar_updated" and json.loads(ev["data"])["seq"] == 3


# ---------- 只读 / 不进交易链 ----------

def test_collect_does_not_write(tmp_path, monkeypatch):
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "account.json").write_text(json.dumps({"initial_equity": 100000.0}))
    (tmp_path / "ledger" / "events.jsonl").write_text(json.dumps(_fill(1, "AAA", "short", 10, 100.0, 105.0)) + "\n")
    (tmp_path / "opportunities" / "cards").mkdir(parents=True)
    (tmp_path / "opportunities" / "cards" / "2026-10-22.json").write_text(json.dumps({"gate": "green", "cards": []}))
    monkeypatch.setattr(orad, "_watch_rows_and_groups", lambda: (WL, GROUPS))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    v = orad.collect(tmp_path, now=datetime(2026, 10, 23, 10, 0, tzinfo=ET), swing=_swing(_card("BBB", price=101.5)))
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not orad.frames_dir(tmp_path).exists()
    assert _b(v, "AAA")["kind"] in ("hold", "exit") and _b(v, "BBB")["kind"] == "order"
    (tmp_path / "opportunities" / "cards" / "2026-10-22.json").unlink()                 # 无卡册 = 交易环大盘闸 unavailable
    orad._GATE_CACHE.clear()
    v2 = orad.collect(tmp_path, now=datetime(2026, 10, 23, 10, 0, tzinfo=ET), swing=_swing(_card("BBB", price=101.5)))
    assert _b(v2, "BBB")["codes"] == ["market_gate"]


def test_not_in_trading_chain():
    root = Path(orad.__file__).parent
    for name in ("trading_loop.py", "entry_guard.py", "watchlist_book.py", "swing_verdict.py", "notify_opportunity.py",
                 "premarket_gap.py", "pm_rank.py", "opportunity_service.py", "swing_desk.py"):
        assert "opp_radar" not in (root / name).read_text(encoding="utf-8"), name
    assert "opp_radar" not in (root.parent / "trader" / "trader.py").read_text(encoding="utf-8")


def test_account_reset_archives_frames():
    from app.services import account_reset
    assert "opportunities/radar_frames" in account_reset.ARCHIVE_ITEMS


def test_scheduler_registers_frame_job():
    """zen_radar_frame: 15 秒, 单实例, 落帧后经 quote_service 推 SSE (源码断言; 调度注册函数需完整 app 状态)。"""
    from app.jobs import daily_pipeline
    src = Path(daily_pipeline.__file__).read_text(encoding="utf-8")
    i = src.index("def _radar_frame():")
    block = src[i:i + 900]
    assert 'id="zen_radar_frame"' in block and "IntervalTrigger(seconds=15)" in block and "max_instances=1" in block
    assert "notify_opp_radar_updated" in block and "opp_radar.tick(" in block


def test_sse_page_toggle_default_on():
    from app.services import preferences
    assert preferences.SSE_REFRESH_PAGES_DEFAULT["opportunities"] is True


# ---------- 审查回归 (0.39.0 复审) ----------

def test_portfolio_risk_mirror_matches_trader_formula():
    """trader.try_fill_entry: 现有组合风险 + 本笔风险 > 权益 x 组合上限 -> portfolio_risk。黄灯上限 3%: 2,800 + 500 > 3,000。"""
    now = datetime(2026, 10, 23, 11, 0, tzinfo=ET)
    c = _card("AAA", side="long", price=101.0, zone=(100.0, 100.5), atr=2.0, odds={"qty": 100, "risk_usd": 500})
    v = _project(now, swing=_swing(c), gate="yellow", open_risk=2800.0)
    b = _b(v, "AAA")
    assert b["kind"] == "blocked" and b["codes"] == ["portfolio_risk"] and b.get("mirror") is True
    assert v["caps"]["portfolio_risk"]["cap"] == 0.03
    # 绿灯 6%: 同一组合风险不拦
    assert _b(_project(now, swing=_swing(c), gate="green", open_risk=2800.0), "AAA")["kind"] == "order"


def test_pending_exit_open_day_is_intent_next_trading_day():
    """离场意向在周四收盘后写: 周四晚 / 周五盘前都应显示「周五开盘平仓」(执行日 = 意向日的下一交易日), 不是「次日」。"""
    ev = [_fill(1, "AAA", "buy", 10, 100.0, 95.0, day="2026-10-21"),
          {"seq": 2, "kind": "exit_intent", "symbol": "AAA", "reason": "invalidation", "trade_date": "2026-10-22",
           "at": "2026-10-22T16:05:00-04:00"}]
    for now in (datetime(2026, 10, 22, 20, 0, tzinfo=ET), datetime(2026, 10, 23, 8, 30, tzinfo=ET)):
        h = _b(_project(now, positions=[_pos("AAA", 10, 100.0, 95.0, entry="2026-10-21")], events=ev), "AAA")["hold"]
        assert h["exit_code"] == "exit_next_open" and h["exit_open_day"] == "2026-10-23"
    # 周五收盘后写的意向 -> 下周一
    ev[1] = {**ev[1], "trade_date": "2026-10-23", "at": "2026-10-23T16:05:00-04:00"}
    h = _b(_project(datetime(2026, 10, 24, 12, 0, tzinfo=ET), positions=[_pos("AAA", 10, 100.0, 95.0, entry="2026-10-21")],
                    events=ev), "AAA")["hold"]
    assert h["exit_open_day"] == "2026-10-26"


# ---------- 审计 2026-10-26 H-4: 越过挂单区 ≠ 在区 ----------

def test_zone_pos_matches_trader_fill_condition():
    # 多 [97, 98] ATR 4: 区内 / 未到区 / 越过区 (交易环 lo <= 价 <= hi 才成交)
    assert orad.zone_pos("long", 97.5, [97, 98], 4) == ("in", 0.0)
    assert orad.zone_pos("long", 99.0, [97, 98], 4) == ("wait", 0.25)
    assert orad.zone_pos("long", 96.5, [97, 98], 4) == ("past", 0.12)
    assert orad.zone_pos("long", 95.0, [97, 98], 4) == ("past", 0.5)
    # 空 [187.90, 189.77]: 现价 195.74 已越过上沿 (生产 10-19 11:30 PLTR)
    assert orad.zone_pos("short", 195.74, [187.90, 189.77], 5.69)[0] == "past"
    assert orad.zone_pos("short", 186.0, [187.90, 189.77], 5.69)[0] == "wait"


def test_beyond_zone_is_not_exec_ring_or_in_zone():
    now = datetime(2026, 10, 23, 10, 15, tzinfo=ET)
    sw = _swing(_card("AAA", price=101.5),                              # 区内 → 执行
                _card("BBB", price=104.0),                              # 空单区 [101,102], 现价 104 越过 0.5 ATR
                _card("CCC", side="long", price=95.0, zone=(97.0, 98.0)))  # 多单越过区下沿 0.5 ATR
    v = _project(now, swing=sw)
    a, b, c = _b(v, "AAA"), _b(v, "BBB"), _b(v, "CCC")
    assert (a["ring"], a["zone_pos"]) == (0, "in")
    assert b["zone_pos"] == "past" and b["ring"] == 1 and b["d"] < -0.25
    assert c["zone_pos"] == "past" and c["ring"] == 1
    # 结论「最近」取真正离成交最近的 (区内 AAA), 不是 d 最负的越区票
    assert v["verdict"]["nearest"]["symbol"] == "AAA"
    only_past = _project(now, swing=_swing(_card("BBB", price=104.0)))
    assert only_past["verdict"]["nearest"]["zone_pos"] == "past"


def test_beyond_zone_blocked_not_listed_as_in_zone_and_not_near():
    now = datetime(2026, 10, 23, 10, 0, tzinfo=ET)
    pos = [_pos(s, -10, 100.0, 105.0) for s in ("AAA", "BBB", "CCC", "DDD")]   # 4/4 满
    sw = _swing(_card("EEE", price=101.5), _card("FFF", price=110.0))           # EEE 区内, FFF 越区 2 ATR
    v = _project(now, swing=sw, positions=pos)
    assert v["verdict"]["code"] == "cap_full"
    assert v["verdict"]["in_zone"] == ["EEE"]
    # 名额预警的「临近」不算越区很远的票
    pos3 = pos[:3]
    v2 = _project(now, swing=_swing(_card("EEE", price=110.0), _card("FFF", price=111.0)), positions=pos3)
    assert v2["caps"]["slots"][0]["near"] == []


def test_far_past_zone_goes_wait_ring_and_multi_card_picks_nearest():
    """复审: 越区很远放等待环; 同票多卡按离成交距离选主卡, 不被越区卡的大负 d 抢走。"""
    now = datetime(2026, 10, 23, 10, 15, tzinfo=ET)
    far = _project(now, swing=_swing(_card("AAA", price=110.0)))                 # 空单越区 2 ATR
    assert (_b(far, "AAA")["zone_pos"], _b(far, "AAA")["ring"]) == ("past", 2)
    sw = _swing(_card("AAA", price=120.0, card_id="c-past"),                       # 空 [101,102] 越区 4.5 ATR
                _card("AAA", side="long", price=100.0, zone=(98.0, 98.8), card_id="c-wait"))  # 多 等回踩 0.3 ATR
    v = _project(now, swing=sw)
    b = _b(v, "AAA")
    assert b["order"]["card_id"] == "c-wait" and b["zone_pos"] == "wait"
    assert "c-past" in (b.get("also_cards") or [])


def test_frame_digest_changes_when_zone_pos_flips():
    now = datetime(2026, 10, 23, 10, 15, tzinfo=ET)
    va = _project(now, swing=_swing(_card("AAA", price=100.0)))                   # 空单未到区 0.25 ATR → 临近环
    vb = _project(now, swing=_swing(_card("AAA", price=102.5)))                   # 越区 0.125 ATR → 同为临近环
    assert _b(va, "AAA")["ring"] == _b(vb, "AAA")["ring"] == 1
    assert orad.frame_of(va)["digest"] != orad.frame_of(vb)["digest"]           # 文案变了要推新帧
