"""0.52.0 机会板排序因子 (rank lab): 公式限制 / 选法合同 / 与 swing_candidates 同一判定 / 导入幂等 /
机会板出卡 / 研究投递密钥权限 / 信号表落键。全部在 tmp 数据目录里跑, 不读生产数据。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.services import rank_lab as rl
from app.services import swing_candidates as sc
from app.services import swing_desk as sd


def _days(n: int, start: date = date(2022, 6, 1)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _panel(syms: list[str], days: list[date], seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for s in syms:
        px = 50.0
        for d in days:
            px *= float(np.exp(rng.normal(0, 0.02)))
            rows.append({"symbol": s, "date": d, "open": px, "high": px * 1.01, "low": px * 0.99, "close": px,
                         "volume": float(rng.integers(100_000, 1_000_000)), "amount": px * 1e5})
    return pl.DataFrame(rows)


def _write_pool(tmp: Path, *, seed: int = 0, n_syms: int = 20) -> tuple[Path, list[str], list[date]]:
    """候选池三件套 (与 build_pool 同格式), 卡结局是随机数。"""
    rng = np.random.default_rng(seed)
    syms = [f"S{i:02d}" for i in range(n_syms)]
    days = _days(900)
    panel = rl.prepare_panel(_panel(syms, days, seed))
    win = [d for d in days if d >= date(2023, 1, 3)]
    rows = []
    for t, d in enumerate(win[:-8]):
        for j in rng.choice(n_syms, size=int(rng.integers(5, 16)), replace=False):
            filled = rng.random() < 0.6
            rows.append({"t": t, "date": d, "symbol": syms[j], "fill_t": t + 1 if filled else None,
                         "exit_t": t + 1 + int(rng.integers(0, 6)) if filled else None,
                         "r": float(rng.normal(0.05, 1)) if filled else None})
    lab = rl.lab_dir(tmp)
    lab.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema={"t": pl.Int32, "date": pl.Date, "symbol": pl.Utf8, "fill_t": pl.Int32,
                               "exit_t": pl.Int32, "r": pl.Float64}).write_parquet(lab / "pool.parquet")
    panel.write_parquet(lab / "panel.parquet")
    (lab / "meta.json").write_text(json.dumps({"contract": rl.CONTRACT, "days": [d.isoformat() for d in win],
                                               "symbols": syms, "yellow_t": [], "sectors": {}, "pool_sha": "p1",
                                               "data_end": win[-1].isoformat()}), encoding="utf-8")
    return tmp, syms, days


# ── 公式限制 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("formula,code", [
    ("", "R001"),
    ("x" * 601, "R002"),
    ("rsi_14 + 1", "R003"),               # 引用因子库 (扫描与候选池面板都没有)
    ("ts_mean(close,300)", "R004"),       # 预热超过 250 根
    ("ts_rsi(close,60)", "R007"),         # 指数平滑记忆太长
    ("close +", "E014"),
])
def test_check_formula_rejects(formula, code):
    with pytest.raises(rl.RankFormulaError) as e:
        rl.check_formula(formula)
    assert e.value.code == code


def test_check_formula_accepts_base_columns_and_cross_section():
    for f in ("ts_delta(close,20)/ts_delay(close,20)", "rank(ts_std(close/prev_close-1,20))", "ts_rsi(close,14)",
              "volume/ts_mean(volume,20)", "if_else(close>open, 1, -1)"):
        assert rl.check_formula(f).ok


def test_ranker_id_ignores_whitespace_but_not_direction():
    a = rl.ranker_id("ts_delta(close,5)/ts_delay(close,5)", "high")
    assert a == rl.ranker_id(" ts_delta(close, 5) / ts_delay(close,5) ", "high")
    assert a != rl.ranker_id("ts_delta(close,5)/ts_delay(close,5)", "low")
    assert a.startswith("rank_") and len(a) == 15


# ── 选法合同 ─────────────────────────────────────────────────────────

def test_picks_top5_finite_ties_by_symbol():
    pool = rl.Pool(days=[date(2024, 1, 2)], syms=["A", "B", "C", "D", "E", "F", "G"],
                   cells={(0, j): (1, 2, float(j)) for j in range(7)}, gate_js=[[0, 1, 2, 3, 4, 5, 6]],
                   yellow={}, sectors={}, panel=pl.DataFrame())
    vals = np.array([[1.0, 3.0, 3.0, np.nan, 2.0, 0.5, -1.0]])
    _ps, chosen = rl.picks(pool, vals)
    assert [s for _, s in chosen] == ["B", "C", "E", "A", "F"]          # NaN 的 D 不选; 同值 B 先于 C


def test_judge_segment_matches_swing_candidates_judge(tmp_path):
    data_dir, _syms, _ = _write_pool(tmp_path)
    pool = rl.load_pool(data_dir)
    vals = rl.value_matrix(pool, rl.factor_frame(pool.panel, "ts_delta(close,5)/ts_delay(close,5)", "high"))
    ps, _ = rl.picks(pool, vals)
    cand = rl._account(pool, ps)
    base = rl.baseline_runs(pool)
    ref = sc.judge(cand, base, len(pool.days) / 252)
    got = rl.judge_segment(cand, base, len(pool.days) / 252)
    for k in ("n", "mean_r", "excess", "excess_ci", "passed", "r_per_year", "base_r_per_year"):
        assert got[k] == ref[k]
    assert 0 <= got["p"] <= 1
    # 区间与 swing_candidates.week_boot_ci 逐位相同
    by_day: dict[int, list[float]] = {}
    for run in base:
        for x in run:
            by_day.setdefault(x["t"], []).append(x["r"])
    diff = [(x["t"], x["r"] - float(np.mean(by_day[x["t"]]))) for x in cand if x["t"] in by_day]
    assert rl.week_boot(diff)[0] == sc.week_boot_ci(diff)


def test_evaluate_segments_and_verdict(tmp_path):
    data_dir, _, _ = _write_pool(tmp_path)
    pool = rl.load_pool(data_dir)
    r = rl.evaluate_formula(pool, "ts_delta(close,5)/ts_delay(close,5)", "high")
    assert r["mining"]["window"][1] <= "2024-12-31" < r["holdout"]["window"][0]
    assert r["mining"]["n"] + r["holdout"]["n"] == r["full"]["n"]
    assert set(r["by_year"]) == {"2023", "2024", "2025"}
    assert r["pool_sha"] == "p1" and r["contract"] == rl.CONTRACT
    # 同一份池子重算逐位相同 (挖掘端与 ZenTrade 两边复核的前提)
    assert rl.evaluate_formula(rl.load_pool(data_dir), "ts_delta(close,5)/ts_delay(close,5)", "high") == r


def test_verdict_requires_holdout_ci_above_zero():
    """0.55.0: 留出段要自己的区间下限 > 0; 点估计为正但区间跨 0 (如 [-0.026, 0.197]) 不算候选。"""
    ok_full = {"passed": True, "excess": 0.1, "n": 500}
    hold = {"excess": 0.0846, "excess_ci": [-0.0262, 0.1966], "n": 604}
    assert not rl.verdict({"full": ok_full, "mining": {"excess": 0.2, "n": 300}, "holdout": hold})["passed"]
    assert rl.verdict({"full": ok_full, "mining": {"excess": 0.2, "n": 300},
                       "holdout": {**hold, "excess_ci": [0.004, 0.19]}})["passed"]
    assert not rl.verdict({"full": ok_full, "mining": {"excess": 0.2, "n": 300},
                           "holdout": {"excess": 0.3, "excess_ci": [0.1, 0.5], "n": 12}})["passed"]   # 笔数不足
    assert not rl.verdict({"full": {**ok_full, "passed": False}, "mining": {"excess": 0.2, "n": 300},
                           "holdout": {**hold, "excess_ci": [0.004, 0.19]}})["passed"]
    for bad in (None, [], [float("nan"), 1.0], ["0.1", 0.2], [0.1]):
        assert not rl.verdict({"full": ok_full, "holdout": {**hold, "excess_ci": bad}})["passed"]
    assert not rl.verdict(None)["passed"]
    # both_segments_positive 仍是点估计口径 (只作标注)
    assert rl.verdict({"full": ok_full, "mining": {"excess": 0.2, "n": 300}, "holdout": hold})["both_segments_positive"]


# ── 复权基准无关 (R008) 与覆盖率 (R009) ──────────────────────────────

@pytest.mark.parametrize("formula", [
    "close",                                              # 跨股票比价格水平 (前复权因子由未来拆股决定)
    "close*volume/max(amount,0.0001)",                    # 前复权价 x 拆股后量 ÷ 当日原成交额
    "ts_mean(raw_close*volume/max(amount,0.0001)-1,5)",   # 原价 x 拆股后量 ÷ 原成交额
    "amount/max(volume,0.0001)/max(prev_close,0.0001)-1",
    "high-low",                                           # 价格差 = 价格水平
    "rank(volume)",                                       # 跨股票比成交量水平 (拆股会改)
    "close/raw_close",
])
def test_check_formula_rejects_adjustment_dependent(formula):
    with pytest.raises(rl.RankFormulaError) as e:
        rl.check_formula(formula)
    assert e.value.code == "R008"


@pytest.mark.parametrize("formula", [
    "ts_delta(close,20)/ts_delay(close,20)", "close/ts_max(close,20)-1", "open/prev_close-1",
    "ts_mean(high-low,5)/ts_mean(high-low,40)", "ts_std(close/prev_close-1,20)", "volume/ts_mean(volume,20)",
    "rank(ts_delta(close,20)/ts_delay(close,20))", "ts_mean(amount,5)/ts_mean(amount,20)", "rank(ts_mean(amount,20))",
    "ts_corr(close,volume,20)", "(close-low)/max(high-low,0.0001)", "ts_rsi(close,14)", "ts_rank(close,20)",
    "ts_mean(turnover_rate,5)", "raw_close/ts_mean(raw_close,20)", "ts_min(low,10)/max(ts_min(low,60),0.0001)-1",
    "decay_linear(close/prev_close-1,10)", "if_else(close>open, 1, -1)",
    # 嵌套滚动的浮点残差不算复权相关
    "ts_corr(close/max(prev_close,0.0001)-1,volume/max(ts_mean(volume,20),0.0001),20)",
    "ts_mean(high-low,5)/max(ts_mean(high-low,40),0.0001)",
])
def test_check_formula_keeps_adjustment_free(formula):
    assert rl.check_formula(formula).ok


def test_warmup_rows_are_blank():
    """预热期 (前 warmup_bars - 1 根) 置空: max(ts_mean(volume,20), 0.0001) 在预热期返回 0.0001,
    新上市股票头几天的量比会成为 1e10 级垃圾值并天天排第一。"""
    days = _days(80)
    raw = _panel(["A", "B"], days)
    raw = raw.filter(~((pl.col("symbol") == "B") & (pl.col("date") < days[40])))   # B 晚上市 40 天, 只有 40 根
    panel = rl.prepare_panel(raw)
    f = "volume/max(ts_mean(volume,20),0.0001)"
    warm = rl.check_formula(f).warmup_bars - 1
    fr = rl.factor_frame(panel, f, "high")
    for s, n in (("A", 80), ("B", 40)):
        vals = fr.filter(pl.col("symbol") == s).sort("date")["value"].to_list()
        assert len(vals) == n
        assert all(v is None for v in vals[:warm]), s
        assert all(v is not None and 0 < v < 10 for v in vals[warm:]), s


def test_coverage_floor_rejects_sparse_formula(tmp_path):
    data_dir, _syms, _ = _write_pool(tmp_path)
    pool = rl.load_pool(data_dir)
    vals = rl.value_matrix(pool, rl.factor_frame(pool.panel, "ts_delta(close,5)/ts_delay(close,5)", "high"))
    assert rl.check_values(pool, vals) > 0.9
    sparse = vals.copy()
    sparse[:, 3:] = np.nan                                 # 只剩 3 只有值
    with pytest.raises(rl.RankFormulaError) as e:
        rl.check_values(pool, sparse)
    assert e.value.code == "R009"
    with pytest.raises(rl.RankFormulaError) as e:
        rl.check_values(pool, np.full_like(vals, np.nan))
    assert e.value.code == "R006"


# ── 导入 / 复核 / 移除 ───────────────────────────────────────────────

def test_import_idempotent_registers_factor_and_parity(tmp_path, monkeypatch):
    data_dir, _, _ = _write_pool(tmp_path)
    from app.factors import registry
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    pool = rl.load_pool(data_dir)
    mine = rl.evaluate_formula(pool, "ts_delta(close,5)/ts_delay(close,5)", "high")
    src = {"system": "external-miner", "pool_sha": "p1", **{k: {kk: mine[k][kk] for kk in ("n", "mean_r", "excess", "excess_ci")}
                                                          for k in ("mining", "holdout", "full")}}
    out = rl.import_ranker(data_dir, name="五日动量", formula="ts_delta(close,5)/ts_delay(close,5)", direction="high",
                           source=src)
    rid = out["ranker"]["id"]
    assert out["created"] and out["factor_id"] == "uf_rk_" + rid[5:]
    assert out["parity"] == {"checked": True, "match": True, "diff": []}
    assert (data_dir / "user_data" / "custom_factors" / f"{out['factor_id']}.json").exists()
    again = rl.import_ranker(data_dir, name="改名", formula=" ts_delta(close, 5)/ts_delay(close,5)", direction="high")
    assert again["ranker"]["id"] == rid and not again["created"]
    assert rl.read_rankers(data_dir)[rid]["name"] == "改名"
    # 挖掘端报错的数字 → 对不上要写出来
    bad = dict(src, full={**src["full"], "n": src["full"]["n"] + 1})
    assert rl.parity(bad, mine, "p1")["match"] is False
    # 池子已更新 → 不可比
    assert rl.parity(src, mine, "p2")["checked"] is False
    # 正在 3+3 里的不能移除
    with pytest.raises(ValueError):
        rl.remove_ranker(data_dir, rid, enabled_long=[rid])
    rl.remove_ranker(data_dir, rid, enabled_long=["macd_golden"])
    assert rid not in rl.read_rankers(data_dir)


def test_evaluate_without_pool_reports_no_pool(tmp_path):
    rl.lab_dir(tmp_path).mkdir(parents=True)
    out = rl.import_ranker(tmp_path, name="x", formula="close/ts_mean(close,20)", direction="low")
    assert out["result"]["error"] == "no_pool"
    assert rl.gear_view(out["result"])["passed"] is False


# ── 盘后扫描值 → 机会板 ──────────────────────────────────────────────

def test_scan_values_equal_pool_panel_values(tmp_path):
    data_dir, syms, days = _write_pool(tmp_path)
    raw = _panel(syms, days)
    rid = rl.import_ranker(data_dir, name="m", formula="ts_delta(close,5)/ts_delay(close,5)", direction="high")["ranker"]["id"]
    frames = {g["symbol"][0]: g for g in raw.partition_by("symbol")}
    vals = rl.scan_values(data_dir, frames, set(syms), days[-1])
    ref = rl.factor_frame(rl.prepare_panel(raw), "ts_delta(close,5)/ts_delay(close,5)", "high").filter(pl.col("date") == days[-1])
    want = {s: round(v, 10) for s, v in ref.select(["symbol", "value"]).iter_rows()}
    assert vals[rid]["values"] == want and vals[rid]["direction"] == "high"


def test_board_hits_and_compose_boards():
    rv = {"rank_aaaaaaaaaa": {"values": {"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0, "E": 1.0, "F": 0.5, "G": 9.0}}}
    hits = rl.board_hits(rv, ("rank_aaaaaaaaaa", "macd_golden"), {"A", "B", "C", "D", "E", "F"})
    assert [(h["symbol"], h["rank_pos"]) for h in hits] == [("A", 1), ("B", 2), ("C", 3), ("D", 4), ("E", 5)]
    closes = {s: 100.0 for s in "ABCDEFG"}
    atr = {s: 4.0 for s in "ABCDEFG"}
    h4 = {s: "long" for s in "ABCDEF"}                     # G 不在 4h 门内: 值最高也不出
    b = sd.compose_boards(signals=[], closes=closes, as_of=date(2026, 11, 5), long_rules=("rank_aaaaaaaaaa",),
                          short_rules=sd.DEFAULT_SHORT, atr=atr, h4_state=h4, short_ok=False, rank_values=rv)
    assert [c["symbol"] for c in b["long"]] == ["A", "B", "C", "D", "E"]
    assert [c["rank_pos"] for c in b["long"]] == [1, 2, 3, 4, 5]
    assert b["meta"]["rank_hits"] == 5
    # 没勾排序因子: 值不起作用, 板与原来一样
    b2 = sd.compose_boards(signals=[], closes=closes, as_of=date(2026, 11, 5), long_rules=sd.DEFAULT_LONG,
                           short_rules=sd.DEFAULT_SHORT, atr=atr, h4_state=h4, short_ok=False, rank_values=rv)
    assert b2["long"] == [] and b2["meta"]["rank_hits"] == 0


def test_save_signals_writes_rank_values_only_for_watchlist(tmp_path):
    from app.services import watchlist_v4 as wv
    wv.save_signals(tmp_path, date(2026, 11, 5), {}, watchlist={"A", "B"}, frames={},
                    rank_values={"rank_aaaaaaaaaa": {"name": "m", "direction": "high", "values": {"A": 1.0, "Z": 2.0}}})
    p = json.loads((wv.signals_dir(tmp_path) / "2026-11-05.json").read_text(encoding="utf-8"))
    assert p["rank_values"]["rank_aaaaaaaaaa"]["values"] == {"A": 1.0}


# ── 研究投递密钥 ─────────────────────────────────────────────────────

def test_research_scope_permissions():
    from app.services import api_keys as ak
    assert ak.permission_error("research", "POST", "/api/factor-lab/rankers/import") is None
    assert ak.permission_error("research", "GET", "/api/opportunities/rules") is None
    assert ak.permission_error("research", "POST", "/api/backtest/run") is None
    assert "研究投递" in ak.permission_error("research", "POST", "/api/opportunities/settings")
    assert "研究投递" in ak.permission_error("research", "POST", "/api/factor-lab/rankers/rank_aaaaaaaaaa/remove")
    assert ak.permission_error("read", "POST", "/api/factor-lab/rankers/import") is not None
    assert ak.permission_error("research", "POST", "/api/settings/api-keys") is not None
