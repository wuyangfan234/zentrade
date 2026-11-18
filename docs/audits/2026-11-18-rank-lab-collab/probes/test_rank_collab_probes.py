"""排序因子协作链路 (ZenTrade x oasis-rank) 只读审计探针, 2026-11-18。

全部在 pytest tmp 数据目录里跑: 不读生产数据、不出网、不写仓库。每条测试断言的是“现行代码的实际行为”
(即复现缺陷 / 口径), 所以在现行代码上应当全部通过; 修复后对应断言会翻转。

用法 (把本文件复制进某棵代码树的 backend/tests/, 借用该树的 conftest: 冻结日历、禁出网):
    cd <tree>/backend
    PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m pytest tests/test_rank_collab_probes.py -q -s -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.services import formula_miner as fm
from app.services import rank_lab as rl
from app.services import rule_auto_select as ras
from app.services import rule_forward as rf
from app.services import swing_desk as sd

FA, DA = "ts_delta(close,5)/ts_delay(close,5)", "high"      # 任意两条合格公式
FB, DB = "ts_std(close/prev_close-1,20)", "low"
FRI = date(2026, 12, 4)                                       # 自动选策略评定日 (周五)


# ── 夹具: 与 tests/test_rank_lab.py::_write_pool 同格式, pool_sha 按 build_pool 同式现算 ──────────────

def _days(n: int, start: date = date(2022, 6, 1)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _panel(syms: list[str], days: list[date], seed: int) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for s in syms:
        px = 50.0
        for d in days:
            px *= float(np.exp(rng.normal(0, 0.02)))
            rows.append({"symbol": s, "date": d, "open": px, "high": px * 1.01, "low": px * 0.99, "close": px,
                         "volume": float(rng.integers(100_000, 1_000_000)), "amount": px * 1e5})
    return pl.DataFrame(rows)


SYMS = [f"S{i:02d}" for i in range(20)]
DAYS = _days(901)
PANEL = rl.prepare_panel(_panel(SYMS, DAYS, 0))
WIN_ALL = [d for d in DAYS if d >= date(2023, 1, 3)]


def _pool_df(win: list[date], seed: int, r_shift_last: int = 0) -> pl.DataFrame:
    """候选池格子。r_shift_last > 0: 最后这么多个交易日的结局换一组随机数 (模拟次日重导时“近期卡结局变了”)。"""
    rng = np.random.default_rng(seed)
    rng2 = np.random.default_rng(seed + 99)
    rows = []
    for t, d in enumerate(win[:-8]):
        for j in rng.choice(len(SYMS), size=int(rng.integers(5, 16)), replace=False):
            filled = rng.random() < 0.6
            r = float(rng.normal(0.05, 1)) if filled else None
            if filled and r_shift_last and t >= len(win) - 8 - r_shift_last:
                r = float(rng2.normal(0.05, 1))
            rows.append({"t": t, "date": d, "symbol": SYMS[j], "fill_t": t + 1 if filled else None,
                         "exit_t": t + 1 + int(rng.integers(0, 6)) if filled else None, "r": r})
    return pl.DataFrame(rows, schema={"t": pl.Int32, "date": pl.Date, "symbol": pl.Utf8, "fill_t": pl.Int32,
                                      "exit_t": pl.Int32, "r": pl.Float64})


def _sha(lab: Path) -> str:
    """build_pool 的 pool_sha 同式: sha256(pool.parquet 字节 + panel.parquet 字节)[:16]。"""
    h = hashlib.sha256()
    for name in ("pool.parquet", "panel.parquet"):
        h.update((lab / name).read_bytes())
    return h.hexdigest()[:16]


def _write_gen(data_dir: Path, win: list[date], *, seed: int = 0, r_shift_last: int = 0, meta: bool = True) -> str:
    lab = rl.lab_dir(data_dir)
    lab.mkdir(parents=True, exist_ok=True)
    _pool_df(win, seed, r_shift_last).write_parquet(lab / "pool.parquet")
    PANEL.filter(pl.col("date") <= win[-1]).write_parquet(lab / "panel.parquet")
    sha = _sha(lab)
    if meta:
        (lab / "meta.json").write_text(json.dumps({
            "contract": rl.CONTRACT, "days": [d.isoformat() for d in win], "symbols": SYMS, "yellow_t": [],
            "sectors": {}, "pool_sha": sha, "data_end": win[-1].isoformat(), "split": rl.split()}), encoding="utf-8")
    return sha


@pytest.fixture
def data_dir(tmp_path):
    _write_gen(tmp_path, WIN_ALL[:-1])
    return tmp_path


def _oasis_src(**kw) -> dict:
    return {"system": "oasis-rank", "id": "rank_probe", "status": "confirmed", **kw}


# ── F1 夜间整份重算抹掉并发导入的复核结果 ────────────────────────────────────────

def test_f1_full_rescore_drops_result_of_ranker_imported_meanwhile(data_dir, monkeypatch):
    """rank_lab.evaluate_and_store(ids=None) 先读登记、锁外算很久, 写回时 allres = {} 整份覆盖:
    计算期间经 /rankers/import 进来的排序因子, 导入当刻写好的复核结果被抹掉 (登记还在)。"""
    a = rl.import_ranker(data_dir, name="A", formula=FA, direction=DA, source=_oasis_src())["ranker"]["id"]
    gate, entered = threading.Event(), threading.Event()
    real = rl.evaluate_formula

    def slow(pool, formula, direction):
        if threading.current_thread().name == "nightly":
            entered.set()
            assert gate.wait(30)
        return real(pool, formula, direction)
    monkeypatch.setattr(rl, "evaluate_formula", slow)
    t = threading.Thread(target=rl.evaluate_and_store, args=(data_dir,), name="nightly")
    t.start()
    assert entered.wait(30)
    b = rl.import_ranker(data_dir, name="B", formula=FB, direction=DB, source=_oasis_src())["ranker"]["id"]
    before = set(rl.read_results(data_dir)["rankers"])
    gate.set()
    t.join(30)
    after = rl.read_results(data_dir)["rankers"]
    print(f"\n[F1] 登记={sorted(rl.read_rankers(data_dir))} 导入 B 后复核结果={sorted(before)} 夜间写回后={sorted(after)}")
    assert {a, b} <= before                                    # 导入当刻 B 有复核结果
    assert b in rl.read_rankers(data_dir)                      # 登记还在
    assert b not in after and a in after                       # B 的复核结果被整份覆盖抹掉
    assert rl.gear_view(after.get(b)) is None                  # 齿轮右侧数字为空, 要等下一个交易日 17:40 ET 重导


# ── F2 三件套没有“代”的原子性: 读到新 pool + 旧 meta 时无报错, 打旧 pool_sha 的标签 ─────────────────

def test_f2_mixed_generation_read_is_silent_and_fakes_a_parity_mismatch(tmp_path):
    """build_pool 依次原子替换 pool.parquet -> panel.parquet -> meta.json (生产 11-17 实测三者 mtime 相差约 36 ms)。
    读者 (ZenTrade 导入时的复核 / oasis-rank 的 Lab) 都走 load_pool: 先读 meta 再读两份 parquet, 不核 pool_sha。
    落在窗口里 = 新池子 + 旧 meta: 不报错, 结果却按旧 pool_sha 记账; 拿去与挖掘端在真旧池上算的数对账 -> 假的“数字对不上”。"""
    old_win, new_win = WIN_ALL[:-1], WIN_ALL
    sha_old = _write_gen(tmp_path, old_win)
    pool_old = rl.load_pool(tmp_path)
    src = rl.evaluate_formula(pool_old, FA, DA)                # 挖掘端在完整的旧池上算 (它报 pool_sha = 旧)
    lab = rl.lab_dir(tmp_path)
    _pool_df(new_win, 0, r_shift_last=40).write_parquet(lab / "pool.parquet")   # 重导: 只换了 pool, meta 还没换
    torn = rl.load_pool(tmp_path)
    assert torn is not None and torn.sha == sha_old            # 无报错, 仍标旧 sha
    real_sha = _sha(lab)
    mine = rl.evaluate_formula(torn, FA, DA)
    par = rl.parity({"pool_sha": sha_old, "split": rl.split(), **{k: src[k] for k in ("mining", "holdout", "full")}},
                    mine, torn.sha)
    print(f"\n[F2] meta.pool_sha={sha_old} 实际字节 sha={real_sha} parity={par}")
    assert real_sha != sha_old                                  # 只要按 build_pool 同式重算一次 sha 就能识别
    assert par["checked"] is True and par["match"] is False    # 假的“对不上”, 且 parity 是导入当刻快照, 之后不再更新


# ── F3 “导入 != 启用”已不成立: 自动选策略可把导入的排序因子换进做多 3 条 ──────────────────────────

class _Engine:
    def list_strategies(self):
        return [{"id": i, "name": i} for i in ("a", "b", "c", "d", "e")]


def _row(rule, side, excess, verdict, *, filled=40, days=25, hits=60):
    return {"rule": rule, "side": side, "hits": hits, "filled": filled, "days": days, "fill": 0.6, "mean_r": 0.1,
            "win": 0.5, "excess": excess, "ci": [excess - 0.1, excess + 0.1], "verdict": verdict}


def test_f3_auto_select_enables_an_imported_ranker_without_the_gear(data_dir, monkeypatch):
    rid = rl.import_ranker(data_dir, name="外部", formula=FA, direction=DA, source=_oasis_src())["ranker"]["id"]
    sd.save_strategies(data_dir, ["a", "b", "c"], ["x", "y", "z"])
    rows = [_row("a", "long", 0.2, "above"), _row("b", "long", 0.02, "unclear"), _row("c", "long", -0.15, "below"),
            _row(rid, "long", 0.30, "above")]
    p = rf.summary_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"contract": rf.CONTRACT, "days": 30, "cells": 2000, "as_of": FRI.isoformat(),
                             "scopes": {"all": {"compared": len(rows), "rules": rows}, "gate": {"compared": 0, "rules": []}}}))
    from app.services import notify_opportunity as no
    monkeypatch.setattr(no, "enqueue_global", lambda *a, **k: True)
    assert ras.enabled(data_dir) is True                        # 没有开关文件 = 开 (生产现状: rules/auto_select.json 不存在)
    assert rid in ras.eligible(data_dir, _Engine())["long"]      # 已导入 (未移除) 的排序因子全部是候补
    rec = ras.run_weekly(data_dir, _Engine(), day=FRI)
    long_now = sd.strategies_from_ruleset(data_dir)[0]
    print(f"\n[F3] 换入记录={rec['sides']['long']} 做多 3 条={long_now}")
    assert rec["sides"]["long"]["applied"] is True and rid in long_now


# ── F4 公式挖掘在 search 窗口里与外部导入撞同一 id: 出处被改回公式挖掘, 之后可被它退席移除 ───────────────

def test_f4_miner_admit_takes_over_an_oasis_import_and_can_retire_it(data_dir, monkeypatch):
    f, d = "ts_rsi(close,14)", "low"                           # oasis-rank 种子「RSI 低」原文; 公式挖掘 rsi 模板也会生成
    assert any(x["formula"] == f and x["direction"] == d for s in range(400) for x in fm.generate(f"seed-{s}", 120))
    rid = rl.import_ranker(data_dir, name="RSI 低", formula=f, direction=d, source=_oasis_src())["ranker"]["id"]
    chosen = [{"id": rid, "formula": f, "direction": d, "name": "RSI 14 低", "template": "rsi", "mining": {}}]
    fm.admit(data_dir, None, chosen, run_id="fm-probe", today=date(2026, 11, 18), pool_sha=None, split=rl.split())
    src = rl.read_rankers(data_dir)[rid]["source"]
    assert src["system"] == "zentrade-miner" and fm.read_state(data_dir)["members"][rid]["status"] == "bench"
    monkeypatch.setattr(fm, "_forward_rows", lambda dd: {rid: {"verdict": "below"}})
    monkeypatch.setattr(fm, "_long_ids", lambda dd: [])
    out = fm.retire(data_dir, today=date(2026, 11, 19))
    gone = rl.read_rankers(data_dir, include_removed=True)[rid]
    print(f"\n[F4] 出处={src.get('system')} 退席={out} removed_at={gone.get('removed_at')}")
    assert out and gone["removed_at"]                           # oasis-rank 的导入被公式挖掘退席移除


def test_f4_control_without_race_the_miner_skips_known_ids(data_dir):
    """对照: search() 开始前已登记的 id (含已移除) 都进 tried, 不会再被选中 (formula_miner.py search)。"""
    f, d = "ts_rsi(close,14)", "low"
    rid = rl.import_ranker(data_dir, name="RSI 低", formula=f, direction=d, source=_oasis_src())["ranker"]["id"]
    res = fm.search(data_dir, slots=6, seed="probe", budget=60)
    assert rid not in {c["id"] for c in res.get("chosen") or []}
    assert all(t["id"] != rid for t in fm.read_trials(data_dir))


# ── F5 已移除的排序因子: 名字回落成原始编号 (卡、回看、换入记录按当下登记现取名字) ─────────────────────

def test_f5_removed_ranker_name_falls_back_to_raw_id(data_dir, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", data_dir)
    rid = rl.import_ranker(data_dir, name="外部", formula=FA, direction=DA, source=_oasis_src())["ranker"]["id"]
    before = sd.strategy_name(rid)
    rl.remove_ranker(data_dir, rid, enabled_long=[])
    after = sd.strategy_name(rid)
    print(f"\n[F5] 移除前={before} 移除后={after}")
    assert before == "排序 · 外部" and after == rid


# ── F6 移除后再导入 = 静默复活 (removed_at 清空, created=False, 无提示) ────────────────────────────

def test_f6_reimport_silently_revives_a_removed_ranker(data_dir):
    rid = rl.import_ranker(data_dir, name="外部", formula=FA, direction=DA, source=_oasis_src())["ranker"]["id"]
    rl.remove_ranker(data_dir, rid, enabled_long=[])
    assert rid not in rl.read_rankers(data_dir)
    out = rl.import_ranker(data_dir, name="外部", formula=FA, direction=DA, source=_oasis_src())
    print(f"\n[F6] created={out['created']} removed_at={out['ranker']['removed_at']}")
    assert out["created"] is False and out["ranker"]["removed_at"] is None and rid in rl.read_rankers(data_dir)
