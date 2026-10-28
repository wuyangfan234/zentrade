"""方向 A: 卡级结局结算 (2026-10-11 无限批复 A-D-impl-plan)。

结算对象 = 自选信号表每一行 (规则 x 票 x 方向 x 信号日), 含全部档位。回放复用 card_replay.replay_state(_short)
(与交易员/影子账本同一规则), 结算模块只读信号表与日K, 只写 data/research/settlement/。"""
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import card_replay as wc
from app.services import card_settlement as cs
from app.services import strategy_evaluation as se
from app.services import watchlist_v4 as wv

SIG = date(2026, 9, 1)          # 周二


@pytest.fixture(autouse=True)
def _cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)      # 工作日近似 (无节假日)


def _days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _frame(sym, rows, start=SIG):
    """rows[0] = 信号日; 之后逐个交易日。"""
    days = _days(start, len(rows))
    return pl.DataFrame({"symbol": sym, "date": days, "open": [float(r[0]) for r in rows],
                         "high": [float(r[1]) for r in rows], "low": [float(r[2]) for r in rows],
                         "close": [float(r[3]) for r in rows], "volume": [1e6] * len(rows)})


def _repo(*frames):
    full = pl.concat(frames)
    return SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: full.filter(
        pl.col("symbol").is_in(syms) & (pl.col("date") >= s) & (pl.col("date") <= e)))


LONG = {"rule": "macd_golden", "side": "long", "stop": -0.06, "entry_zone": [100.0, 101.0], "invalidation": 94.0,
        "target_zone": [112.0, 114.0], "risk": 6.5, "rr": 2.0}
SHORT = {"rule": "phantom_4h_short", "side": "short", "stop": 0.05, "entry_zone": [99.0, 100.0], "invalidation": 106.0,
         "target_zone": [86.0, 87.7], "risk": 6.5, "rr": 2.0}


def _write(tmp_path, day, rows, watch, **extra):
    sd = wv.signals_dir(tmp_path)
    sd.mkdir(parents=True, exist_ok=True)
    sig = [{**r, "card_id": f"opp-{day}-{r['symbol']}-{r['rule']}-{r.get('side', 'long')}"} for r in rows]
    (sd / f"{day.isoformat()}.json").write_text(json.dumps({"as_of": day.isoformat(), "watchlist": sorted(watch),
                                                            "closes": {}, "signals": sig,
                                                            "generated_at": f"{day.isoformat()}T20:00:00-04:00", **extra}))


def _arrays(frame, day=SIG, upto=None):
    after = frame.filter((pl.col("date") > day) & (pl.col("date") <= (upto or date(2100, 1, 1))))
    return tuple(after[k].to_numpy().astype(float) for k in ("open", "high", "low", "close"))


def _kw(x, **over):
    return dict(entry_zone=tuple(x["entry_zone"]), invalidation=x["invalidation"], target=tuple(x["target_zone"]),
                risk=x["risk"], **over)


FLAT = (95, 96, 94, 95)


def _hit_long_rows():
    # 信号日收 100; 第 1 天开在区内 100.5 成交; 第 2 天最高 113 (半仓目标); 第 3 天 115 (远端全平)
    return [(99, 100, 98, 100), (100.5, 103, 100, 102), (104, 113, 103, 110), (111, 115, 110, 114)] + [FLAT] * 10


def _by(rows):
    return {(r["symbol"], r["rule"], r["side"]): r for r in rows}


# ── 结局与 R ──

def test_hit_long_r_equals_trader_replay_and_settles_after_min_age(tmp_path):
    nv = _frame("NVDA", _hit_long_rows())
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    close = _days(SIG, 8)[-1]                             # 信号日之后第 7 个交易日收盘
    rows = cs.compute(tmp_path, _repo(nv), as_of_close=close)["rows"]
    (r,) = rows
    exp = wc.replay_state(*_arrays(nv, upto=close), **_kw(LONG))
    assert exp["status"] == "closed"
    assert r["outcome"] == "HIT" and r["final"] is True and r["r"] == exp["r"] and r["exit_reason"] == exp["exit_reason"]
    assert r["fill_date"] == _days(SIG, 2)[1].isoformat() and r["hold_days"] == exp["days"]
    assert r["exit_date"] == _days(SIG, 2 + exp["days"] - 1)[-1].isoformat()
    assert r["entry"] == round(100.5 * (1 + se.SLIP), 4)
    assert r["ret"] == pytest.approx(exp["r"] * LONG["risk"] / r["entry"], abs=1e-6)


def test_closed_trade_still_unsettled_inside_seven_trading_day_floor(tmp_path):
    nv = _frame("NVDA", _hit_long_rows())
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    close = _days(SIG, 7)[-1]                             # 只过了 6 个交易日
    (r,) = cs.compute(tmp_path, _repo(nv), as_of_close=close)["rows"]
    assert r["state"] == "closed" and r["outcome"] == "UNSETTLED" and r["final"] is False


def test_trigger_on_last_window_day_is_unsettled_until_exit_even_after_floor(tmp_path):
    rows = [(99, 100, 98, 100)] + [FLAT] * 4 + [(100.5, 102, 100, 101)] + [(101, 102, 100, 101)] * 3
    nv = _frame("NVDA", rows)
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    close = _days(SIG, 9)[-1]                             # 8 个交易日后: 第 5 天触发, 仍在持有
    (r,) = cs.compute(tmp_path, _repo(nv), as_of_close=close)["rows"]
    assert r["state"] == "triggered" and r["outcome"] == "UNSETTLED" and r["final"] is False


def test_expired_window_is_exp_and_not_in_win_rate_denominator(tmp_path):
    nv = _frame("NVDA", [(99, 100, 98, 100)] + [FLAT] * 9)
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    rows = cs.compute(tmp_path, _repo(nv), as_of_close=_days(SIG, 10)[-1])["rows"]
    assert rows[0]["outcome"] == "EXP" and rows[0]["final"] is True and rows[0].get("r") is None
    cell = cs.table(rows, kind="fill", scope="all")["cells"][0]
    assert cell["n"] == 0 and cell["exp"] == 1 and cell["settled"] == 1 and cell["trigger_rate"] == 0.0
    assert cell["win_rate"] is None


def test_gap_pass_settles_same_day_as_void_and_signal_table_uses_gap_open_with_actual_risk(tmp_path):
    # 第 1 天开 104 > 101 x 1.02 → 作废 (PASS); 之后一路上涨到远端目标
    rows = [(99, 100, 98, 100), (104, 106, 103, 105), (106, 113, 105, 112), (113, 116, 112, 115)] + [FLAT] * 6
    nv = _frame("NVDA", rows)
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    # 当天 (只有 1 根信号日后K) 就结算为作废
    (r0,) = cs.compute(tmp_path, _repo(nv), as_of_close=_days(SIG, 2)[-1])["rows"]
    assert r0["outcome"] == "PASS" and r0["final_fill"] is True and r0["signal_final"] is False
    close = _days(SIG, 10)[-1]
    (r,) = cs.compute(tmp_path, _repo(nv), as_of_close=close)["rows"]
    o, h, lo, c = _arrays(nv, upto=close)
    entry_v = 104 * (1 + se.SLIP)
    exp = wc.replay_state(o, h, lo, c, **_kw(LONG, ) | {"entry_zone": (100.0, 104.0), "risk": entry_v - 94.0})
    assert r["outcome"] == "PASS" and r.get("r") is None
    assert r["signal_r"] == exp["r"] and r["signal_risk"] == pytest.approx(entry_v - 94.0)
    assert r["signal_r_orig"] == pytest.approx(exp["r"] * (entry_v - 94.0) / LONG["risk"], abs=1e-4)
    fill = cs.table([r], kind="fill", scope="all")["cells"][0]
    sig = cs.table([r], kind="signal", scope="all")["cells"][0]
    assert fill["n"] == 0 and fill["pass"] == 1 and sig["n"] == 1


def test_missing_bar_is_counted_not_treated_as_no_trigger(tmp_path):
    nv = _frame("NVDA", _hit_long_rows()).filter(pl.col("date") != _days(SIG, 3)[-1])
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    rows = cs.compute(tmp_path, _repo(nv), as_of_close=_days(SIG, 10)[-1])["rows"]
    assert rows[0]["outcome"] == "MISSING"
    cell = cs.table(rows, kind="fill", scope="all")["cells"][0]
    assert cell["missing"] == 1 and cell["settled"] == 0


def test_short_r_sign_matches_short_replay(tmp_path):
    rows = [(100, 101, 99, 100), (99.5, 100, 95, 96), (95, 96, 85, 86)] + [FLAT] * 8
    mu = _frame("MU", rows)
    _write(tmp_path, SIG, [{**SHORT, "symbol": "MU"}], {"MU"})
    close = _days(SIG, 9)[-1]
    (r,) = cs.compute(tmp_path, _repo(mu), as_of_close=close)["rows"]
    exp = wc.replay_state_short(*_arrays(mu, upto=close), **_kw(SHORT))
    assert exp["status"] == "closed" and exp["r"] > 0
    assert r["outcome"] == "HIT" and r["r"] == exp["r"] and r["side"] == "short" and r["ret"] > 0


def test_same_bar_target_and_stop_touch_is_flagged_without_changing_r(tmp_path):
    rows = [(99, 100, 98, 100), (100.5, 113, 93, 95), (95, 96, 94, 95)] + [FLAT] * 8
    nv = _frame("NVDA", rows)
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    close = _days(SIG, 10)[-1]
    (r,) = cs.compute(tmp_path, _repo(nv), as_of_close=close)["rows"]
    assert r["same_bar"] is True and r["r"] == wc.replay_state(*_arrays(nv, upto=close), **_kw(LONG))["r"]


# ── 同日随机入场基准 ──

def test_benchmark_same_direction_same_holding_fixed_seed_and_excludes_own_symbol(tmp_path):
    nv = _frame("NVDA", _hit_long_rows())
    up = [_frame(s, [(100 + i, 100 + i, 100 + i, 100 + i) for i in range(14)]) for s in ("A1", "A2", "A3")]
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}, {**SHORT, "symbol": "A1", "rule": "tv_ut_bot",
                                                        "entry_zone": [101.0, 102.0], "invalidation": 140.0,
                                                        "target_zone": [60.0, 70.0]}],
           {"NVDA", "A1", "A2", "A3"})
    close = _days(SIG, 10)[-1]
    a = _by(cs.compute(tmp_path, _repo(nv, *up), as_of_close=close)["rows"])
    b = _by(cs.compute(tmp_path, _repo(nv, *up), as_of_close=close)["rows"])
    lr = a[("NVDA", "macd_golden", "long")]
    assert lr["bench_ret"] is not None and lr["bench_ret"] > 0          # 名单其余票一路涨: 看多基准为正
    assert lr["bench_ret"] == b[("NVDA", "macd_golden", "long")]["bench_ret"]
    assert lr["excess_r"] == pytest.approx((lr["ret"] - lr["bench_ret"]) * lr["entry"] / LONG["risk"], abs=1e-4)
    assert 0.0 <= lr["bench_pctl"] <= 1.0
    sr = a[("A1", "tv_ut_bot", "short")]
    if sr["outcome"] == "HIT":
        assert sr["bench_ret"] < 0                                     # 同样的上涨, 看空基准为负
    # 宇宙里只有本票 → 无基准, 不伪造
    _write(tmp_path / "solo", SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    (solo,) = cs.compute(tmp_path / "solo", _repo(nv), as_of_close=close)["rows"]
    assert solo["bench_ret"] is None and solo["excess_r"] is None


# ── 表 ──

def _fake(n, ex, rule="r1", side="long", day0=0):
    return [{"key": f"{rule}|{side}|{i}", "rule": rule, "side": side, "symbol": f"S{i}", "outcome": "HIT", "final": True,
             "final_fill": True, "provenance": "live", "signal_day": (SIG + timedelta(days=(day0 + i) % 25)).isoformat(),
             "r": e + 0.1, "excess_r": e, "same_bar": False} for i, e in enumerate(ex[:n])]


def test_table_gray_below_20_plain_below_30_and_color_only_when_ci_excludes_zero():
    rng = np.random.default_rng(1)
    t = cs.table(_fake(19, [1.0] * 19), kind="fill", scope="all")["cells"][0]
    assert t["tone"] == "gray" and t["mean_r"] is None and t["win_rate"] is None and t["n"] == 19
    t = cs.table(_fake(25, [1.0] * 25), kind="fill", scope="all")["cells"][0]
    assert t["tone"] == "plain" and t["mean_r"] == pytest.approx(1.1)
    t = cs.table(_fake(40, list(1.0 + rng.normal(0, 0.1, 40))), kind="fill", scope="all")["cells"][0]
    assert t["tone"] == "pos" and t["ci"][0] > 0
    t = cs.table(_fake(40, list(-1.0 + rng.normal(0, 0.1, 40))), kind="fill", scope="all")["cells"][0]
    assert t["tone"] == "neg"
    t = cs.table(_fake(40, list(rng.normal(0, 2.0, 40))), kind="fill", scope="all")["cells"][0]
    assert t["tone"] == "plain"


def test_table_rows_split_by_rule_and_side_and_live_scope_excludes_backfill():
    rows = _fake(3, [1, 1, 1], "r1", "long") + _fake(2, [1, 1], "r1", "short")
    rows += [{**x, "provenance": "backfill", "key": x["key"] + "b"} for x in _fake(4, [1] * 4, "r2", "long")]
    keys = {(c["rule"], c["side"]) for c in cs.table(rows, kind="fill", scope="all")["cells"]}
    assert keys == {("r1", "long"), ("r1", "short"), ("r2", "long")}
    live = cs.table(rows, kind="fill", scope="live")
    assert {(c["rule"], c["side"]) for c in live["cells"]} == {("r1", "long"), ("r1", "short")}
    assert live["m"] == 2 and live["all"]["n"] == 5


# ── 落盘: 只写一次, 只写研究目录 ──

def test_run_appends_final_rows_once_and_writes_only_research_dir(tmp_path):
    nv = _frame("NVDA", _hit_long_rows())
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    cs.settle_dir(tmp_path).mkdir(parents=True)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    close = _days(SIG, 10)[-1]
    s1 = cs.run(tmp_path, _repo(nv), as_of_close=close)
    s2 = cs.run(tmp_path, _repo(nv), as_of_close=close)
    assert s1["appended"] == 1 and s2["appended"] == 0
    lines = cs.cards_path(tmp_path).read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["contract"] == cs.CONTRACT
    assert json.loads(lines[0])["record_kind"] == "settled"
    assert cs.CONTRACT in cs.cards_path(tmp_path).name
    tab = json.loads((cs.settle_dir(tmp_path) / "table.json").read_text())
    assert set(tab["tables"]) == {"fill", "signal"} and set(tab["tables"]["fill"]) == {"live", "all"}
    assert len((cs.settle_dir(tmp_path) / "runs.jsonl").read_text().splitlines()) == 2
    for p, b in before.items():
        assert p.read_bytes() == b                                   # 信号表等原文件逐字节不变
    changed = {p for p in tmp_path.rglob("*") if p.is_file()} - set(before)
    assert all(cs.settle_dir(tmp_path) in p.parents for p in changed)


def test_final_row_is_never_rewritten_even_if_bars_change_later(tmp_path):
    nv = _frame("NVDA", _hit_long_rows())
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    cs.settle_dir(tmp_path).mkdir(parents=True)
    close = _days(SIG, 10)[-1]
    cs.run(tmp_path, _repo(nv), as_of_close=close)
    first = cs.cards_path(tmp_path).read_text()
    nv2 = nv.with_columns(pl.col("high") * 0.5)                         # 日K 事后被修
    cs.run(tmp_path, _repo(nv2), as_of_close=close)
    assert cs.cards_path(tmp_path).read_text() == first
    tab = json.loads((cs.settle_dir(tmp_path) / "table.json").read_text())
    assert tab["tables"]["fill"]["all"]["all"]["n"] == 1


def test_pipeline_mode_does_nothing_until_settlement_dir_is_enabled(tmp_path):
    nv = _frame("NVDA", _hit_long_rows())
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    assert cs.run_if_enabled(tmp_path, _repo(nv), as_of_close=_days(SIG, 10)[-1]) is None
    assert not cs.settle_dir(tmp_path).exists()


def test_provenance_backfill_flags():
    p = {"backfill": True}
    assert cs.provenance(p, {"rule": "macd_golden", "side": "long"}) == "backfill"
    p = {"tv_backfill_at": "x", "short_backfill_at": "y"}
    assert cs.provenance(p, {"rule": "tv_ut_bot", "side": "long"}) == "backfill"
    assert cs.provenance(p, {"rule": "phantom_4h_short", "side": "short"}) == "backfill"
    assert cs.provenance(p, {"rule": "macd_golden", "side": "long"}) == "live"
    assert cs.provenance({}, {"rule": "tv_ut_bot", "side": "short"}) == "live"


def test_settlement_module_never_imports_trading_chain():
    src = Path(cs.__file__).read_text(encoding="utf-8")
    for bad in ("entry_guard", "watchlist_book", "app.trader", "trading_loop", "save_ruleset", "save_trial", "ledger"):
        assert bad not in src


def test_pass_writes_void_record_same_day_then_signal_record_after_virtual_trade_and_floor(tmp_path):
    rows = [(99, 100, 98, 100), (104, 106, 103, 105), (106, 113, 105, 112), (113, 116, 112, 115)] + [FLAT] * 6
    nv = _frame("NVDA", rows)
    _write(tmp_path, SIG, [{**LONG, "symbol": "NVDA"}], {"NVDA"})
    cs.settle_dir(tmp_path).mkdir(parents=True)
    s1 = cs.run(tmp_path, _repo(nv), as_of_close=_days(SIG, 2)[-1])         # 跳空当天
    recs = [json.loads(x) for x in cs.cards_path(tmp_path).read_text().splitlines()]
    assert s1["appended"] == 1 and [(r["record_kind"], r["outcome"]) for r in recs] == [("pass_void", "PASS")]
    s2 = cs.run(tmp_path, _repo(nv), as_of_close=_days(SIG, 4)[-1])         # 虚拟交易已平但未过保护期
    assert s2["appended"] == 0
    s3 = cs.run(tmp_path, _repo(nv), as_of_close=_days(SIG, 10)[-1])
    recs = [json.loads(x) for x in cs.cards_path(tmp_path).read_text().splitlines()]
    assert s3["appended"] == 1 and [r["record_kind"] for r in recs] == ["pass_void", "pass_signal"]
    assert recs[0]["card_id"] == recs[1]["card_id"] and recs[1]["signal_r"] is not None
    tab = json.loads((cs.settle_dir(tmp_path) / "table.json").read_text())["tables"]
    assert tab["fill"]["all"]["all"]["n"] == 0 and tab["fill"]["all"]["all"]["pass"] == 1
    assert tab["signal"]["all"]["all"]["n"] == 1


def test_benchmark_exit_price_type_follows_card_exit():
    """卡开盘离场 (收盘破止损次日开盘出 / 开盘跳过目标) → 基准取同一根K开盘; 收盘离场 (第 7 日) 与盘中按目标价离场 → 收盘。"""
    tgt = (112.0, 114.0)
    for r in ("invalidation", "trailing_stop", "breakeven_after_partial"):
        assert cs.bench_exit_at_open(r, "long", exit_open=90.0, target=tgt, same_bar_as_fill=False)
    assert not cs.bench_exit_at_open("time_stop", "long", exit_open=100.0, target=tgt, same_bar_as_fill=False)
    assert not cs.bench_exit_at_open("target_zone_upper", "long", exit_open=110.0, target=tgt, same_bar_as_fill=False)
    assert cs.bench_exit_at_open("target_zone_upper", "long", exit_open=115.0, target=tgt, same_bar_as_fill=False)
    assert not cs.bench_exit_at_open("target_zone_upper", "long", exit_open=115.0, target=tgt, same_bar_as_fill=True)
    st = (86.0, 87.7)
    # 空头两段 (BH-03): 最终离场是远端 target[0] (target_zone_upper), 开盘已越过远端才按开盘价
    assert cs.bench_exit_at_open("target_zone_upper", "short", exit_open=85.0, target=st, same_bar_as_fill=False)
    assert not cs.bench_exit_at_open("target_zone_upper", "short", exit_open=87.0, target=st, same_bar_as_fill=False)


def test_ci_needs_enough_signal_days_before_coloring():
    """2026-10-11 试算发现: 只有 2 个信号日时按日分块自助法的区间极窄 (两个日均值之间), 会误着色。
    信号日 (块) 少于 MIN_BLOCKS → 不给区间、不着色, 只显数。"""
    from app.services import card_settlement as cs
    rows = []
    for i in range(40):
        day = "2026-09-29" if i % 2 else "2026-09-30"
        rows.append({"outcome": "HIT", "signal_day": day, "r": 0.3 + (i % 5) * 0.1, "excess_r": 0.2 + (i % 5) * 0.1,
                     "rule": "x", "side": "long", "provenance": "live"})
    c = cs._cell(rows, "fill")
    assert c["n"] == 40 and c["ci"] is None and c["tone"] == "plain"
    many = [{**r, "signal_day": f"2026-09-{1 + i % 20:02d}"} for i, r in enumerate(rows)]
    c2 = cs._cell(many, "fill")
    assert cs.MIN_BLOCKS <= 20 and c2["ci"] is not None and c2["tone"] == "pos"
