"""卡片回放内核 (card_replay, 0.29.1 从旧 watchlist_cards 迁出): 推进规则与影子账本结算逐行一致 (随机路径对拍);
盘中未收盘K 只决定入场。另保留自选信号表 (下单簿与盘前推送的输入) 的记录口径测试。"""
import json
from datetime import date
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import card_replay as cr
from app.services import decision_scan_job as job
from app.services import strategy_evaluation as se
from app.services import watchlist_v4 as wv


@pytest.mark.parametrize("seed", range(400))
def test_replay_state_matches_settlement_rules(seed):
    """同一条路径: 结算给 closed 的, 卡片也给 closed 且 R 相同; 结算给 no_fill/gap_expired 的, 卡片消失;
    结算给 open_at_end (未满期) 的, 卡片显示为 pending/triggered。"""
    rng = np.random.default_rng(seed)
    n = int(rng.integers(0, 14))
    c = 100 * np.cumprod(1 + rng.normal(0, 0.03, n))
    o = c * (1 + rng.normal(0, 0.01, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.015, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.015, n)))
    kw = dict(entry_zone=(100.0, 101.0), invalidation=94.0, target=(112.0, 114.0), risk=6.5)
    a = se.simulate_native_daily(o, h, lo, c, **kw)
    b = cr.replay_state(o, h, lo, c, **kw)
    if a["status"] == "closed":
        assert b["status"] == "closed" and b["r"] == a["r"] and b["exit_reason"] == a["exit_reason"]
    elif a["status"] == "gap_expired":
        assert b["status"] == "gap_expired"
    elif a["status"] == "no_fill":
        assert b["status"] == ("expired" if n >= se.ENTRY_WINDOW else "pending")
    else:
        assert a["status"] == "open_at_end" and b["status"] == "triggered"


@pytest.mark.parametrize("seed", range(400))
def test_short_replay_state_matches_short_settlement(seed):
    """看空卡 (2026-10-09): 与空头 v2 日线结算 simulate_native_daily_short 同一条路径同一结果 (closed 的 R/原因相同;
    未成交 → 卡消失或待触发; 未满期 → 已触发)。BH-03 起两段回补, 不再对齐一段式的 v1 _simulate_short。"""
    rng = np.random.default_rng(10_000 + seed)
    n = int(rng.integers(0, 14))
    c = 100 * np.cumprod(1 + rng.normal(0, 0.03, n))
    o = c * (1 + rng.normal(0, 0.01, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.015, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.015, n)))
    kw = dict(entry_zone=(99.0, 100.0), invalidation=106.0, target=(86.0, 87.7), risk=6.5)
    a = se.simulate_native_daily_short(o, h, lo, c, **kw)
    b = cr.replay_state_short(o, h, lo, c, **kw)
    if a["status"] == "closed":
        assert b["status"] == "closed" and b["r"] == a["r"] and b["exit_reason"] == a["exit_reason"]
    elif a["status"] == "gap_expired":
        assert b["status"] == "gap_expired"
    elif a["status"] == "no_fill":
        assert b["status"] == ("expired" if n >= se.ENTRY_WINDOW else "pending")
    else:
        assert a["status"] == "open_at_end" and b["status"] == "triggered"


def test_pending_then_triggered_then_gone():
    kw = dict(entry_zone=(100.0, 101.0), invalidation=94.0, target=(112.0, 114.0), risk=6.5)
    # 两天没碰到入场区 → 待触发 第 3 天
    o, h, lo, c = (np.array(x, float) for x in ([96, 97], [98, 99], [95, 96], [97, 98]))
    assert cr.replay_state(o, h, lo, c, **kw) == {"status": "pending", "elapsed": 2}
    # 第三天开在入场区 → 已触发
    o, h, lo, c = (np.array(x, float) for x in ([96, 97, 100.5], [98, 99, 103], [95, 96, 99.8], [97, 98, 102]))
    st = cr.replay_state(o, h, lo, c, **kw)
    assert st["status"] == "triggered" and st["hold_day"] == 1 and st["stop"] == 94.0
    # 第四天收盘跌破失效 → 次日开盘离场 (待离场), 有次日 → closed (卡消失)
    o2, h2, l2, c2 = (np.append(x, y) for x, y in zip((o, h, lo, c), (101, 101.5, 92, 93), strict=True))
    assert cr.replay_state(o2, h2, l2, c2, **kw)["exit_next_open"] == "invalidation"
    o3, h3, l3, c3 = (np.append(x, y) for x, y in zip((o2, h2, l2, c2), (92.5, 93, 91, 92), strict=True))
    assert cr.replay_state(o3, h3, l3, c3, **kw)["status"] == "closed"
    # 5 天都没触发 → expired
    o, h, lo, c = (np.full(5, v, float) for v in (96, 98, 95, 97))
    assert cr.replay_state(o, h, lo, c, **kw)["status"] == "expired"


def _setup(tmp_path, monkeypatch, today, signals_by_day, bars, watch=("NVDA", "MU"), rules=("macd_golden",)):
    d = tmp_path / "decision_desk"
    d.mkdir(parents=True, exist_ok=True)
    (d / "evidence_mode.json").write_text(json.dumps({"mode": "v4"}))
    wv.save_ruleset(tmp_path, list(rules), {})
    sd = wv.signals_dir(tmp_path)
    sd.mkdir(parents=True)
    for day, sig in signals_by_day.items():
        (sd / f"{day}.json").write_text(json.dumps({"as_of": day, "watchlist": list(watch), "closes": {}, "signals": sig,
                                                    "backfill": True}))
    monkeypatch.setattr(us_clock, "today", lambda: today)
    monkeypatch.setattr(job, "_watchlist", lambda: set(watch))
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda syms: {})
    return SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: bars.filter(pl.col("symbol").is_in(syms)
                                                                                       & (pl.col("date") <= e)))


def _sig(sym, rule="macd_golden"):
    return {"rule": rule, "symbol": sym, "stop": -0.06, "entry_zone": [100.0, 101.0], "invalidation": 94.0,
            "target_zone": [112.0, 114.0], "risk": 6.5, "rr": 2.0, "rank": 1, "total": 3, "card_id": "x"}


def _short_sig(sym, rule="phantom_4h_short"):
    return {"rule": rule, "symbol": sym, "side": "short", "stop": 0.05, "entry_zone": [99.0, 100.0], "invalidation": 106.0,
            "target_zone": [86.0, 87.7], "risk": 6.5, "rr": 2.0, "rank": 1, "total": 3, "card_id": "y"}


def test_backfill_signals_do_not_reach_table_or_preopen_push(tmp_path, monkeypatch):
    from app.services import watchlist_table as wt
    us_clock._calendar_provider = None
    _setup(tmp_path, monkeypatch, date(2026, 10, 6), {"2026-10-05": [_sig("NVDA")]}, pl.DataFrame())
    assert wt._signal_days(tmp_path, date(2026, 10, 6), 5) == []


def test_short_watchlist_signals_go_to_table_not_shadow_ledger(tmp_path):
    from app.opportunity.engine import ScanResult
    d0 = date(2026, 10, 5)
    n = 80
    dd_ = pl.date_range(date(2026, 6, 1), d0, eager=True)[-n:]
    c = np.linspace(120, 100, n)
    frames = {s: pl.DataFrame({"symbol": s, "date": dd_, "open": c, "high": c * 1.02, "low": c * 0.98, "close": c,
                               "volume": 1e6}) for s in ("NVDA", "XYZ")}
    hits = {"phantom_4h_short": [{"symbol": "NVDA", "rank": 1}, {"symbol": "XYZ", "rank": 2}],
            "macd_golden": [{"symbol": "NVDA", "rank": 1}]}
    short = job.watchlist_short_signals(d0, watchlist={"NVDA"}, frames=frames, hits=hits,
                                        stops_by_symbol={"phantom_4h_short": {"NVDA": 0.04}})
    assert list(short) == ["phantom_4h_short"] and [x["symbol"] for x in short["phantom_4h_short"]] == ["NVDA"]
    assert short["phantom_4h_short"][0]["card"].is_short
    out = SimpleNamespace(summary={})
    job._watchlist_v4_snapshot(tmp_path, d0, watchlist={"NVDA"}, frames=frames, hits=hits, stops={"macd_golden": -0.06},
                               stops_by_symbol={"phantom_4h_short": {"NVDA": 0.04}}, s1s2=ScanResult(d0), out=out)
    table = json.loads((wv.signals_dir(tmp_path) / "2026-10-05.json").read_text())
    sides = {(x["rule"], x["side"]) for x in table["signals"]}
    assert ("phantom_4h_short", "short") in sides
    ledger_rules = {r["rule"] for r in wv.load(tmp_path)}
    assert "phantom_4h_short" not in ledger_rules                          # 影子账本里记为 <id>:short
    assert "phantom_4h_short:short" in ledger_rules and wv.SHORT_BASELINE_ID in ledger_rules
    assert out.summary["watchlist_v4"]["short_signals"] == 1


def test_table_and_push_ignore_short_rows(tmp_path, monkeypatch):
    from app.services import watchlist_table as wt
    us_clock._calendar_provider = None
    repo = _setup(tmp_path, monkeypatch, date(2026, 10, 6), {}, pl.DataFrame())
    wv.save_ruleset(tmp_path, ["macd_golden", "phantom_4h_short"], {})
    p = wv.signals_dir(tmp_path) / "2026-10-05.json"
    p.write_text(json.dumps({"as_of": "2026-10-05", "watchlist": ["NVDA", "MU"], "closes": {"NVDA": 99.0},
                             "signals": [_short_sig("NVDA"), {**_sig("MU"), "side": "long"}]}))
    monkeypatch.setattr("app.opportunity.engine.load_params", lambda: {"portfolio": {"watchlist_promotion_days": 5}})
    rows = {r["symbol"]: r for r in wt.build(data_dir=tmp_path, repo=repo)["rows"]}
    assert rows["NVDA"]["signal"] is None and rows["MU"]["signal"] is not None


def test_sell_hits_of_tv_rules_become_short_signals_builtin_sells_do_not(tmp_path):
    d0 = date(2026, 10, 5)
    n = 80
    dd_ = pl.date_range(date(2026, 6, 1), d0, eager=True)[-n:]
    c = np.linspace(120, 100, n)
    frames = {"NVDA": pl.DataFrame({"symbol": "NVDA", "date": dd_, "open": c, "high": c * 1.02, "low": c * 0.98, "close": c,
                                    "volume": 1e6})}
    short = job.watchlist_short_signals(d0, watchlist={"NVDA"}, frames=frames, hits={}, stops_by_symbol={},
                                        sell_hits={"tv_ut_bot": [{"symbol": "NVDA", "rank": None}],
                                                   "macd_golden": [{"symbol": "NVDA", "rank": None}]},
                                        stops={"tv_ut_bot": None, "macd_golden": -0.06})
    assert list(short) == ["tv_ut_bot"] and short["tv_ut_bot"][0]["card"].is_short
    assert short["tv_ut_bot"][0]["per_symbol_stop"] is False


@pytest.mark.parametrize("seed", range(300))
def test_partial_today_bar_only_decides_entry(seed):
    """盘中接上的今日未收盘K (收盘 = NaN): 只能把 pending 变成 今天入场 / 今天作废, 永不在今天离场;
    今天入场的价格 = 把这根K 当作正式日K 时结算给出的入场价 (同一入场规则)。"""
    rng = np.random.default_rng(50_000 + seed)
    n = int(rng.integers(0, 5))
    for short in (False, True):
        c = 100 * np.cumprod(1 + rng.normal(0, 0.03, n + 1))
        o = c * (1 + rng.normal(0, 0.01, n + 1))
        h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.015, n + 1)))
        lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.015, n + 1)))
        if short:
            kw, fn = dict(entry_zone=(99.0, 100.0), invalidation=106.0, target=(86.0, 87.7), risk=6.5), cr.replay_state_short
        else:
            kw, fn = dict(entry_zone=(100.0, 101.0), invalidation=94.0, target=(112.0, 114.0), risk=6.5), cr.replay_state
        before = fn(o[:n], h[:n], lo[:n], c[:n], **kw)
        if before["status"] != "pending":
            continue
        part = fn(o, h, lo, np.append(c[:n], np.nan), **kw)
        full = fn(o, h, lo, c, **kw)
        assert part["status"] in ("pending", "triggered", "gap_expired", "expired")
        if part["status"] == "expired":
            # 入场窗最后一天 (第 5 天) 盘中还没碰到: build 不采纳 expired, 卡保持“待触发 5/5”到收盘
            assert n == se.ENTRY_WINDOW - 1
        if part["status"] == "triggered":
            assert part["fill_index"] == n and part["exit_next_open"] is None and part["hold_day"] == 0
            assert full["status"] in ("triggered", "closed")
            if full["status"] == "triggered":
                assert full["entry"] == part["entry"]
        if part["status"] == "gap_expired":
            assert full["status"] == "gap_expired"
