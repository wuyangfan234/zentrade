"""公式挖掘 -> 机会板候补席: 自动写排序公式、按机会卡回放、送进规则前向记分, 由自动选策略决定进不进入选。

为什么不按回测成绩挑 (2026-11-18 实测, 1613 条已评公式, 只用 2025-06-30 以前的数据):
  - 同一把卡回放尺子上, 挖掘段前一半的超额对后一半的相关 = -0.10; 前一半最好的 5% (+0.16R) 后一半 -0.02R, 与全体无差别;
  - 全市场 1300 只流动股上 2021-22 的 5 日 Rank IC, 对同一条公式 2023 起在自选上的 IC 相关 = +0.02。
  回测排名不带来信息, 所以这里的回测只做三件事: 公式能不能算、够不够笔数、选出来的票和已有的是不是一回事。
  另有一道产品决策的底线: 挖掘段回放超额为负的不入席 (不提高命中率, 只是不让回放亏的公式占席位); 过了底线之后仍不按超额排序。
  席位给“行为不一样”的公式, 好坏只由上线后的前向成绩 (rule_forward) 判定。

流程 (每个交易日 17:40 ET 候选池重导之后):
  1. 退席: 候补里前向成绩垫底 (below) 或坐满 MAX_TENURE_DAYS 个交易日仍未过线的, 标记移除; 入选中的不动。
  2. 有空席才生成: 结构模板 + 窗口变异出 GEN_PER_RUN 条, 试过的 (含已退席的) 不再试。
  3. 评估 (子进程, 只看挖掘段): 可算 + 覆盖率 + 成交笔数 >= MIN_MINING_N + 回放超额不为负。
  4. 查重: 每日前 5 与在席 / 已退席公式的重合度, 超线不收; 剩下的按“最不像已有的”优先入席, 每轮至多 ADMIT_PER_RUN 条。
  5. 入席 = rank_lab.import_ranker (登记排序因子 + 因子库草稿 + 三段回放) 并补最近一份信号表; 当晚起进入前向记分。

借鉴: alphagen LinearAlphaPool (定容池, 新因子与池内互相关超线即拒, 满员挤掉最弱) / AlphaAgent FactorZoo 截面相关查重 /
WS-GP (只在已知有效的结构里搜, 不做无约束随机树) / gplearn point mutation (只改常数与同类算子)。

与外部研究系统共用排序因子登记: 候补只认自己登记的 (source.system = SYSTEM)。同一条公式之后被外部系统导入
(登记的出处被改写) -> 记为 released, 让出席位, 不再由这里退席。入席时它已被别的系统登记 (搜索期间导入的)
-> 不入席、不改写出处, 本轮记录列进 taken。

边界: 只写 research/formula_miner/ 与 rank_lab 的登记 (经 rank_lab 自己的函数); 不改入选策略、下单簿、台账、交易环。
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONTRACT = "formula-miner-v1"
SYSTEM = "zentrade-miner"
BENCH_CAP = 6               # 候补席位 (不含已入选的)
ADMIT_PER_RUN = 2           # 每轮至多入席
GEN_PER_RUN = 120           # 每轮生成条数 (去掉试过的之后再评估)
MAX_TENURE_DAYS = 60        # 坐满这么多个交易日仍未过线 -> 退席
MIN_MINING_N = 60           # 挖掘段成交笔数下限 (太少的公式攒不出前向样本)
OVERLAP_ALIVE_MAX = 0.6     # 与在席排序因子的每日前 5 重合度上限
OVERLAP_DEAD_MAX = 0.8      # 与已退席公式的重合度上限 (同一种行为不再试第二次)
DEAD_COMPARE = 40           # 查重时回看多少条已退席公式
KEEP_RUNS = 30
EVAL_BUDGET_S = 900
WINDOWS = (3, 5, 10, 14, 20, 40, 60, 120)
_RUN_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_RUNNING = [0]


# ── 文件 ────────────────────────────────────────────────────────────────────

def miner_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "formula_miner"


def _read(p: Path, default):
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return d if isinstance(d, type(default)) else default


def _write(p: Path, d) -> None:
    from app.services.fs_utils import atomic_write_text
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(d, ensure_ascii=False, indent=1))


def enabled(data_dir: Path) -> bool:
    """没有文件 = 开 (与自动选策略同一口径: 默认全自动, 可手动关)。"""
    v = _read(miner_dir(data_dir) / "settings.json", {}).get("enabled")
    return True if v is None else v is True


def set_enabled(data_dir: Path, on: bool) -> dict:
    d = {"enabled": bool(on), "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    _write(miner_dir(data_dir) / "settings.json", d)
    return d


def read_state(data_dir: Path) -> dict:
    d = _read(miner_dir(data_dir) / "state.json", {})
    return {"contract": CONTRACT, "seq": int(d.get("seq") or 0),
            "members": {k: v for k, v in (d.get("members") or {}).items() if isinstance(v, dict)},
            "runs": [x for x in d.get("runs") or [] if isinstance(x, dict)]}


def _save_state(data_dir: Path, st: dict) -> None:
    _write(miner_dir(data_dir) / "state.json", st)


def trials_path(data_dir: Path) -> Path:
    return miner_dir(data_dir) / "trials.jsonl"


def read_trials(data_dir: Path) -> list[dict]:
    p = trials_path(data_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            x = json.loads(line)
        except ValueError:
            continue
        if isinstance(x, dict) and x.get("id"):
            out.append(x)
    return out


def _append_trials(data_dir: Path, rows: list[dict]) -> None:
    if not rows:
        return
    p = trials_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── 生成 (纯函数, 给定种子可复现) ────────────────────────────────────────────
# 全部积木都是比值 / 收益率 (与复权基准无关, 过得了 rank_lab 的 R008)。

ATOMS = {
    "ret": ("(close/prev_close-1)", "日收益"),
    "gap": ("(open/prev_close-1)", "隔夜跳空"),
    "intra": ("(close/open-1)", "日内收益"),
    "range": ("((high-low)/close)", "振幅"),
    "clv": ("((close-low)/max(high-low,0.0001))", "收盘位置"),
    "upper": ("((high-max(open,close))/max(high-low,0.0001))", "上影占比"),
    "lower": ("((min(open,close)-low)/max(high-low,0.0001))", "下影占比"),
    "volr": ("(volume/max(ts_mean(volume,20),0.0001))", "量比"),
    "absret": ("abs(close/prev_close-1)", "日波动"),
}
SIGNED = ("ret", "gap", "intra")
LEVELS = {
    "dist_hi": ("(close/ts_max(high,{n})-1)", "距 {n} 日高点"),
    "dist_lo": ("(close/ts_min(low,{n})-1)", "距 {n} 日低点"),
    "bias": ("(close/ts_mean(close,{n})-1)", "{n} 日均线偏离"),
    "mom": ("(close/ts_delay(close,{n})-1)", "{n} 日涨幅"),
}
CONDS = {
    "up": ("close>prev_close", "上涨日"),
    "down": ("close<prev_close", "下跌日"),
    "gap_up": ("open>prev_close", "高开日"),
    "gap_down": ("open<prev_close", "低开日"),
    "heavy": ("volume>ts_mean(volume,20)", "放量日"),
    "light": ("volume<ts_mean(volume,20)", "缩量日"),
}
# 同一天不会同时成立的一对: 「A 且 B 占比」配上它们恒为 0, 当天前 5 只剩股票代码顺序
OPPOSITE = {"up": "down", "down": "up", "gap_up": "gap_down", "gap_down": "gap_up", "heavy": "light", "light": "heavy"}


def _level(rng: random.Random) -> tuple[str, str]:
    k = rng.choice(sorted(LEVELS))
    n = rng.choice(WINDOWS)
    return LEVELS[k][0].format(n=n), LEVELS[k][1].format(n=n)


def _simple(rng: random.Random) -> tuple[str, str]:
    """一个单行为的小表达式 (给组合模板当零件)。"""
    if rng.random() < 0.5:
        return _level(rng)
    k = rng.choice(sorted(ATOMS))
    n = rng.choice(WINDOWS[:6])
    return f"ts_mean({ATOMS[k][0]},{n})", f"{ATOMS[k][1]} {n} 日均值"


def _fresh(rng: random.Random) -> dict:
    t = rng.choice(("smooth", "std", "ratio", "level", "corr", "cond_share", "cond_count", "spread", "rank_sum",
                    "ts_rank", "rsi", "decay", "zscore"))
    a = rng.choice(sorted(ATOMS))
    ax, al = ATOMS[a]
    n = rng.choice(WINDOWS)
    if t == "smooth":
        f, name = f"ts_mean({ax},{n})", f"{al} {n} 日均值"
    elif t == "std":
        f, name = f"ts_std({ax},{max(n, 5)})", f"{al} {max(n, 5)} 日标准差"
    elif t == "ratio":
        s, lg = sorted(rng.sample(WINDOWS, 2))
        f, name = f"ts_mean({ax},{s})/max(ts_mean(abs({ax}),{lg}),0.0001)", f"{al} {s}/{lg} 日比"
    elif t == "level":
        f, name = _level(rng)
    elif t == "corr":
        b = rng.choice([k for k in sorted(ATOMS) if k != a])
        m = max(n, 10)
        f, name = f"ts_corr({ax},{ATOMS[b][0]},{m})", f"{al}与{ATOMS[b][1]} {m} 日相关"
    elif t == "cond_share":
        # 只用带符号的收益类积木、窗口 >= 10: 恒正的积木 (振幅 / 量比) 在短窗口里常整窗为 0, 一排并列名次
        # 由滚动求和的浮点残差决定, 过不了复权无关检验 (R008), 上板顺序也就没有意义
        s = rng.choice(SIGNED)
        c = rng.choice(sorted(CONDS))
        m = max(n, 10)
        f = f"ts_sum(if_else({CONDS[c][0]},{ATOMS[s][0]},0),{m})/max(ts_sum(abs({ATOMS[s][0]}),{m}),0.0001)"
        name = f"{CONDS[c][1]}的{ATOMS[s][1]}占比 {m} 日"
    elif t == "cond_count":
        c1 = rng.choice(sorted(CONDS))
        c2 = rng.choice([k for k in sorted(CONDS) if k not in (c1, OPPOSITE.get(c1))])
        m = max(n, 5)
        f = f"ts_sum(if_else(({CONDS[c1][0]}) and ({CONDS[c2][0]}),1,0),{m})/{m}"
        name = f"{CONDS[c1][1]}且{CONDS[c2][1]}占比 {m} 日"
    elif t == "spread":
        (f1, n1), (f2, n2) = _level(rng), _level(rng)
        while f2 == f1:  # 两项抽到同一项时公式是 x - x, 恒为 0, 当天前 5 只剩股票代码顺序; 第二项重抽
            f2, n2 = _level(rng)
        f, name = f"{f1}-{f2}", f"{n1} 减 {n2}"
    elif t == "rank_sum":
        (f1, n1), (f2, n2) = _simple(rng), _simple(rng)
        f, name = f"rank({f1})+rank({f2})", f"{n1} 加 {n2}"
    elif t == "ts_rank":
        m = max(n, 10)
        f, name = f"ts_rank({ax},{m})", f"{al} {m} 日分位"
    elif t == "rsi":
        m = rng.choice((3, 5, 10, 14, 20))
        f, name = f"ts_rsi(close,{m})", f"RSI {m}"
    elif t == "decay":
        f, name = f"decay_linear({ax},{max(n, 5)})", f"{max(n, 5)} 日加权{al}"
    else:
        m = max(n, 10)
        f, name = f"ts_zscore({ax},{m})", f"{al} {m} 日标准分"
    return {"formula": f, "name": name, "template": t}


_WIN_RE = re.compile(r",(\d+)\)")
_WIN_NOTE_RE = re.compile(r"\s*·\s*窗口 \d+→\d+$")


def _mutant_name(parent_name: str, old: str, new: str) -> str:
    """变异公式的名字 = 母公式的名字 + 实际改动的窗口 (名字上屏、进推送, 不能还写着母公式的窗口)。至多 40 字, 截母名不截窗口。"""
    wo, wn = _WIN_RE.findall(old), _WIN_RE.findall(new)
    a, b = next(((x, y) for x, y in zip(wo, wn, strict=False) if x != y), ("", ""))
    note = f" · 窗口 {a}→{b}"
    base = _WIN_NOTE_RE.sub("", parent_name).removesuffix(" 变体").strip() or "公式"
    return base[:40 - len(note)].rstrip() + note


def mutate(formula: str, rng: random.Random) -> str | None:
    """点变异: 把一个窗口换成梯子上相邻的一档 (结构不动)。公式里没有窗口 -> None。"""
    spots = [m for m in _WIN_RE.finditer(formula) if int(m.group(1)) in WINDOWS]
    if not spots:
        return None
    m = rng.choice(spots)
    i = WINDOWS.index(int(m.group(1)))
    j = rng.choice([k for k in (i - 1, i + 1) if 0 <= k < len(WINDOWS)])
    return formula[:m.start(1)] + str(WINDOWS[j]) + formula[m.end(1):]


def generate(seed: str, n: int, parents: list[dict] | None = None) -> list[dict]:
    """-> [{formula, direction, name, template, parent?}], 批内按 (公式, 方向) 去重。约四分之一来自在席公式的窗口变异。"""
    rng = random.Random(seed)
    parents = [p for p in (parents or []) if isinstance(p.get("formula"), str)]
    out, seen = [], set()
    for _ in range(n * 6):
        if len(out) >= n:
            break
        if parents and rng.random() < 0.25:
            p = rng.choice(parents)
            f = mutate(p["formula"], rng)
            if f is None:
                continue
            item = {"formula": f, "name": _mutant_name(str(p.get("name") or ""), p["formula"], f), "template": "mutate",
                    "parent": p.get("id"),
                    "direction": p.get("direction") if p.get("direction") in ("high", "low") else "high"}
        else:
            item = {**_fresh(rng), "direction": rng.choice(("high", "low"))}
            item["name"] = f"{item['name']} {'高' if item['direction'] == 'high' else '低'}"[:40]
        key = (re.sub(r"\s+", "", item["formula"]), item["direction"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# ── 评估与查重 (子进程里跑; 只看挖掘段) ──────────────────────────────────────

def overlap(a: dict[int, set], b: dict[int, set]) -> float:
    """两条公式每日上板名单的重合度: 两边都有名单的日子里, 交集 / 较大的那份, 再按日平均。没有共同日 -> 0。"""
    days = [t for t in a if t in b and a[t] and b[t]]
    if not days:
        return 0.0
    return sum(len(a[t] & b[t]) / max(len(a[t]), len(b[t])) for t in days) / len(days)


def _pick_sets(chosen: list[tuple[int, str]], cut: int) -> dict[int, set]:
    out: dict[int, set] = {}
    for t, s in chosen:
        if t < cut:
            out.setdefault(t, set()).add(s)
    return out


def evaluate(pool, item: dict) -> tuple[dict, dict[int, set] | None]:
    """一条公式 -> (试验记录, 挖掘段每日前 5)。不可用的公式记 error, 名单为 None。留出段的格子不参与任何数字。"""
    from app.services import rank_lab as rl
    rid = rl.ranker_id(item["formula"], item["direction"])
    rec: dict[str, Any] = {"id": rid, "formula": item["formula"], "direction": item["direction"], "name": item["name"],
                           "template": item.get("template"), "parent": item.get("parent")}
    cut = rl._segments(pool)["mining"][1]
    try:
        vals = rl.value_matrix(pool, rl.factor_frame(pool.panel, item["formula"], item["direction"]))
        rec["coverage"] = round(rl.check_values(pool, vals, cut), 4)
        ps, chosen = rl.picks(pool, vals)
        cand = [x for x in rl._account(pool, ps) if x["t"] < cut]
        base = [[x for x in run if x["t"] < cut] for run in rl.baseline_runs(pool)]
        seg = rl.judge_segment(cand, base, cut / 252)
    except rl.RankFormulaError as e:
        rec["error"] = e.code
        return rec, None
    except Exception as e:
        rec["error"] = type(e).__name__
        return rec, None
    rec["mining"] = {k: seg.get(k) for k in ("n", "mean_r", "excess", "excess_ci")}
    return rec, _pick_sets(chosen, cut)


def search(data_dir: Path, *, slots: int, seed: str, budget: int = GEN_PER_RUN, deadline_s: float = EVAL_BUDGET_S) -> dict:
    """生成 -> 评估 -> 查重 -> 选出至多 slots 条。试验逐条追加进 trials.jsonl (试过的永久记数)。不登记排序因子 (主进程做)。"""
    import time

    from app.services import rank_lab as rl
    data_dir = Path(data_dir)
    pool = rl.load_pool(data_dir)
    if pool is None:
        return {"skipped": "no_pool"}
    cut = rl._segments(pool)["mining"][1]
    st = read_state(data_dir)
    alive = rl.read_rankers(data_dir)
    every = rl.read_rankers(data_dir, include_removed=True)
    tried = {t["id"] for t in read_trials(data_dir)} | set(every)

    def sets_of(rk: dict) -> dict[int, set] | None:
        try:
            vals = rl.value_matrix(pool, rl.factor_frame(pool.panel, rk["formula"], rk["direction"]))
            return _pick_sets(rl.picks(pool, vals)[1], cut)
        except Exception as e:
            logger.info("formula miner: reference %s unreadable: %s", rk.get("id"), type(e).__name__)
            return None
    alive_sets = {rid: s for rid, rk in alive.items() if (s := sets_of(rk)) is not None}
    dead_ids = [rid for rid, m in sorted(st["members"].items(), key=lambda x: x[1].get("retired_day") or "", reverse=True)
                if m.get("status") == "retired" and rid in every and rid not in alive][:DEAD_COMPARE]
    dead_sets = {rid: s for rid in dead_ids if (s := sets_of(every[rid])) is not None}
    items = generate(seed, budget, [{**rk, "id": rid} for rid, rk in alive.items()])
    funnel = {"generated": len(items), "new": 0, "evaluated": 0, "errors": 0, "thin": 0, "negative": 0, "same_as_alive": 0,
              "same_as_retired": 0, "eligible": 0, "admitted": 0}
    t0, rows, ok = time.monotonic(), [], []
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for it in items:
        rid = rl.ranker_id(it["formula"], it["direction"])
        if rid in tried:
            continue
        tried.add(rid)
        funnel["new"] += 1
        if time.monotonic() - t0 > deadline_s:
            break
        rec, sets = evaluate(pool, it)
        rec.update(run=seed, at=now, pool_sha=pool.sha)
        funnel["evaluated"] += 1
        if sets is None:
            rec["outcome"] = "error"
            funnel["errors"] += 1
        elif (rec["mining"].get("n") or 0) < MIN_MINING_N:
            rec["outcome"] = "thin"
            funnel["thin"] += 1
        elif not isinstance(rec["mining"].get("excess"), (int, float)) or rec["mining"]["excess"] < 0:
            rec["outcome"] = "negative"
            funnel["negative"] += 1
        else:
            oa = max((overlap(sets, s) for s in alive_sets.values()), default=0.0)
            od = max((overlap(sets, s) for s in dead_sets.values()), default=0.0)
            rec["overlap"] = {"alive": round(oa, 3), "retired": round(od, 3)}
            if oa >= OVERLAP_ALIVE_MAX:
                rec["outcome"] = "same_as_alive"
                funnel["same_as_alive"] += 1
            elif od >= OVERLAP_DEAD_MAX:
                rec["outcome"] = "same_as_retired"
                funnel["same_as_retired"] += 1
            else:
                rec["outcome"] = "eligible"
                funnel["eligible"] += 1
                ok.append((max(oa, od), rid, rec, sets))
        rows.append(rec)
    # 席位给“最不像已有的”; 回测超额不参与排序 (实测对之后的成绩没有预测力)
    ok.sort(key=lambda x: (x[0], x[1]))
    chosen: list[tuple[dict, dict[int, set]]] = []
    for _score, _rid, rec, sets in ok:
        if len(chosen) >= max(0, slots):
            break
        if any(overlap(sets, s) >= OVERLAP_ALIVE_MAX for _r, s in chosen):
            continue
        rec["outcome"] = "admitted"
        chosen.append((rec, sets))
    funnel["admitted"] = len(chosen)
    _append_trials(data_dir, rows)
    return {"funnel": funnel, "pool_sha": pool.sha, "split": rl.split(),
            "chosen": [{k: r.get(k) for k in ("id", "formula", "direction", "name", "template", "parent", "mining", "overlap")}
                       for r, _s in chosen]}


def worker_run(data_dir: Path, repo, cfg: dict) -> dict:
    """子进程 (backtest/worker.py kind=formula_miner)。"""
    return search(Path(data_dir), slots=int(cfg.get("slots") or 0), seed=str(cfg["seed"]),
                  budget=int(cfg.get("budget") or GEN_PER_RUN))


# ── 候补席管理 (主进程) ─────────────────────────────────────────────────────

def _forward_rows(data_dir: Path) -> dict[str, dict]:
    from app.services import rule_forward as rf
    return rf.by_rule(rf.read_summary(data_dir) or {}, "all")


def _long_ids(data_dir: Path) -> list[str]:
    from app.services import swing_desk as sd
    return list(sd.strategies_from_ruleset(data_dir)[0])


def retire(data_dir: Path, *, today: date) -> list[dict]:
    """退席判定: 前向垫底 / 坐满期限仍未过线 / 已被人工移除。入选中的不动 (状态记 selected);
    被外部研究系统接手的记 released (不移除)。返回本次退席的。"""
    from app.market_clock import us_clock
    from app.services import rank_lab as rl
    data_dir = Path(data_dir)
    rows, long_ids, alive = _forward_rows(data_dir), _long_ids(data_dir), rl.read_rankers(data_dir)
    out = []
    with _STATE_LOCK:
        st = read_state(data_dir)
        for rid, m in st["members"].items():
            if m.get("status") in ("retired", "released"):
                continue
            reason = None
            if rid not in alive:
                reason = "removed"
            elif ((alive[rid].get("source") or {}).get("system") or SYSTEM) != SYSTEM:
                # 同一条公式被外部研究系统导入, 登记归它: 让出席位, 之后的去留不归这里管
                m.update(status="released", released_day=today.isoformat(),
                         released_to=str((alive[rid].get("source") or {}).get("system"))[:40])
                continue
            elif rid in long_ids:
                m["status"] = "selected"
                continue
            else:
                m["status"] = "bench"
                v = (rows.get(rid) or {}).get("verdict")
                try:
                    sat = len(us_clock.trading_days_between(us_clock.next_trading_day(date.fromisoformat(m["admitted_day"])), today))
                except (KeyError, ValueError):
                    sat = 0
                if v == "below":
                    reason = "below"
                elif sat >= MAX_TENURE_DAYS and v != "above":
                    reason = "timeout"
            if reason is None:
                continue
            if reason != "removed":
                try:
                    rl.remove_ranker(data_dir, rid, enabled_long=long_ids)
                except (KeyError, ValueError) as e:
                    logger.warning("formula miner: retire %s skipped: %s", rid, type(e).__name__)
                    continue
            fw = rows.get(rid) or {}
            m.update(status="retired", retired_day=today.isoformat(), reason=reason,
                     final={k: fw.get(k) for k in ("hits", "filled", "days", "excess", "ci", "verdict")})
            out.append({"id": rid, "name": m.get("name"), "reason": reason})
        _save_state(data_dir, st)
    return out


def bench_count(st: dict) -> int:
    return sum(1 for m in st["members"].values() if m.get("status") == "bench")


def admit(data_dir: Path, repo, chosen: list[dict], *, run_id: str, today: date, pool_sha: str | None, split: dict | None,
          taken: list[dict] | None = None) -> list[dict]:
    """把选中的公式登记成排序因子 (候补)。单条失败只跳过。
    搜索期间别的系统已登记了同一条公式 (含已移除): 不改写它的出处、不记成候补, 列进 taken (rank_lab 锁内判断)。"""
    from app.services import rank_lab as rl
    data_dir = Path(data_dir)
    done = []
    for c in chosen:
        try:
            res = rl.import_ranker(data_dir, name=c["name"], formula=c["formula"], direction=c["direction"],
                                   idea=f"公式挖掘 · {c['name']}",
                                   source={"system": SYSTEM, "id": c["id"], "run_id": run_id, "status": "challenger",
                                           "mining": c.get("mining"), "pool_sha": pool_sha, "split": split},
                                   only_new=True)
        except Exception as e:
            logger.warning("formula miner: admit %s failed: %s", c.get("id"), type(e).__name__)
            continue
        if res.get("skipped"):
            who = (res["ranker"].get("source") or {}).get("system")
            logger.info("formula miner: %s already registered by %s, not admitted", c.get("id"), who)
            if taken is not None:
                taken.append({"id": c.get("id"), "name": c.get("name"), "system": who})
            continue
        rid = res["ranker"]["id"]
        with _STATE_LOCK:
            st = read_state(data_dir)
            st["members"][rid] = {"id": rid, "name": c["name"], "formula": c["formula"], "direction": c["direction"],
                                  "template": c.get("template"), "parent": c.get("parent"), "run_id": run_id,
                                  "admitted_day": today.isoformat(), "status": "bench", "mining": c.get("mining"),
                                  "overlap": c.get("overlap")}
            _save_state(data_dir, st)
        done.append({"id": rid, "name": c["name"]})
    if done and repo is not None:
        try:
            rl.backfill_latest(data_dir, repo)
        except Exception as e:
            logger.warning("formula miner: backfill failed (soft): %s", type(e).__name__)
    return done


def is_running() -> bool:
    return _RUNNING[0] > 0


def run(data_dir: Path, repo, *, today: date | None = None, force: bool = False, searcher=None) -> dict:
    """一轮: 退席 -> (有空席) 生成评估 -> 入席。关着且非 force -> 不动。searcher 留给测试替换子进程。"""
    from app.market_clock import us_clock
    data_dir = Path(data_dir)
    today = today or us_clock.today()
    if not force and not enabled(data_dir):
        return {"skipped": "disabled"}
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "running"}
    _RUNNING[0] += 1
    try:
        retired = retire(data_dir, today=today)
        with _STATE_LOCK:
            st = read_state(data_dir)
            st["seq"] += 1
            seq = st["seq"]
            _save_state(data_dir, st)
        run_id = f"fm-{today.isoformat()}-{seq}"
        rec: dict[str, Any] = {"run_id": run_id, "day": today.isoformat(), "retired": retired,
                               "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        slots = min(ADMIT_PER_RUN, BENCH_CAP - bench_count(st))
        if slots <= 0:
            rec["skipped"] = "bench_full"
        else:
            cfg = {"slots": slots, "seed": run_id, "budget": GEN_PER_RUN}
            if searcher is not None:
                res = searcher(data_dir, cfg)
            else:
                from app.backtest.worker import make_worker_task, run_worker_task
                from app.services.heavy_job_limiter import shared_heavy_job_limiter
                with shared_heavy_job_limiter.slot("normal", label="公式挖掘"):
                    res = run_worker_task(make_worker_task("formula_miner", data_dir, cfg))
            if res.get("error"):
                rec["error"] = str(res["error"])[:200]
            elif res.get("skipped"):
                rec["skipped"] = res["skipped"]
            else:
                rec["funnel"] = res.get("funnel")
                taken: list[dict] = []
                rec["admitted"] = admit(data_dir, repo, res.get("chosen") or [], run_id=run_id, today=today,
                                        pool_sha=res.get("pool_sha"), split=res.get("split"), taken=taken)
                if taken:
                    rec["taken"] = taken
        with _STATE_LOCK:
            st = read_state(data_dir)
            st["runs"] = (st["runs"] + [rec])[-KEEP_RUNS:]
            _save_state(data_dir, st)
        return rec
    finally:
        _RUNNING[0] -= 1
        _RUN_LOCK.release()


def run_nightly(data_dir: Path, repo) -> dict | None:
    """候选池重导之后调用。盘中 (09:00-17:00 ET) 不跑。"""
    from app.services import swing_candidates as sc
    if not sc.can_run_now():
        return None
    return run(data_dir, repo)


# ── 页面视图 (只读) ─────────────────────────────────────────────────────────

def view(data_dir: Path, *, today: date | None = None) -> dict:
    from app.market_clock import us_clock
    from app.services import rank_lab as rl
    from app.services import rule_forward as rf
    data_dir = Path(data_dir)
    today = today or us_clock.today()
    st = read_state(data_dir)
    rows, long_ids = _forward_rows(data_dir), _long_ids(data_dir)
    results = rl.read_results(data_dir).get("rankers") or {}
    keep = ("hits", "filled", "days", "excess", "ci", "verdict")

    def sat(m: dict) -> int:
        try:
            end = date.fromisoformat(m["retired_day"]) if m.get("retired_day") else today
            return len(us_clock.trading_days_between(us_clock.next_trading_day(date.fromisoformat(m["admitted_day"])), end))
        except (KeyError, ValueError):
            return 0

    def row(rid: str, m: dict) -> dict:
        fw = m.get("final") if m.get("status") == "retired" else rows.get(rid)
        res = results.get(rid) if isinstance(results.get(rid), dict) else {}
        status = "selected" if (m.get("status") != "retired" and rid in long_ids) else m.get("status")
        return {"id": rid, "name": m.get("name"), "formula": m.get("formula"), "direction": m.get("direction"),
                "status": status, "reason": m.get("reason"), "admitted_day": m.get("admitted_day"),
                "retired_day": m.get("retired_day"), "sat_days": sat(m),
                "replay": {seg: {k: (res.get(seg) or {}).get(k) for k in ("n", "excess", "excess_ci")}
                           for seg in ("mining", "holdout") if isinstance(res.get(seg), dict)} or None,
                "forward": {**{k: (fw or {}).get(k) for k in keep}, "verdict": (fw or {}).get("verdict") or "thin"}}
    members = sorted(st["members"].items(), key=lambda x: x[1].get("admitted_day") or "", reverse=True)
    live = [row(r, m) for r, m in members if m.get("status") not in ("retired", "released")]
    gone = [row(r, m) for r, m in members if m.get("status") == "retired"]
    trials = read_trials(data_dir)
    outcomes: dict[str, int] = {}
    for t in trials:
        outcomes[t.get("outcome") or "error"] = outcomes.get(t.get("outcome") or "error", 0) + 1
    pool_ok = (rl.lab_dir(data_dir) / "meta.json").exists() and (rl.lab_dir(data_dir) / "pool.parquet").exists()
    return {"contract": CONTRACT, "enabled": enabled(data_dir), "running": is_running(), "pool_ready": pool_ok,
            "bench": [x for x in live if x["status"] == "bench"], "selected": [x for x in live if x["status"] == "selected"],
            "retired": gone[:20],
            "totals": {"tried": len(trials), "admitted": len(st["members"]), "retired": len(gone),
                       "selected": sum(1 for x in live if x["status"] == "selected"),
                       "released": sum(1 for m in st["members"].values() if m.get("status") == "released"),
                       "above": sum(1 for x in live if x["forward"]["verdict"] == "above"), "outcomes": outcomes},
            "caps": {"bench": BENCH_CAP, "per_run": ADMIT_PER_RUN, "tenure_days": MAX_TENURE_DAYS,
                     "need_days": rf.VERDICT_DAYS, "need_filled": rf.MIN_FILLED},
            "last_run": st["runs"][-1] if st["runs"] else None, "runs": list(reversed(st["runs"]))[:10]}
