"""机会板排序因子 (rank lab, 0.52.0): 外部研究系统挖排序公式, 用机会板卡回放验收, ZenTrade 同码复核后导入。

一个排序因子 = 一条 DSL 公式 + 方向 (高好 / 低好)。它不另起一个选股器, 只回答机会板的一个问题:
「今天 4h 门内的自选 (平均约 15 只) 里, 先挂哪 5 张卡?」

验收合同 rank-lab-v2 (与 swing_candidates 同一套卡回放与账户约束, 只是入场集合换成“4h 门内全部”):
  候选池 = 每个信号日 4h 门 (多) 内、卡结局可判定的自选格子; 卡 = tight-v1 几何; 结局 = 30 分钟K 逐根回放 (R)。
  因子组 = 每天按因子值取前 5 (值非有限的不选; 同值按代码); 基线 = 同一池每天随机挑 5 (种子 1-5 取平均)。
  账户约束 = 每笔 0.5% (最多 12 仓, SPY<200 日线 6 仓, 每天新开 10 笔, 同板块 4 仓, 冷却 3 天)。
  判定 = 因子每笔 R 减同日随机基线每笔 R, 周聚类自助法 95% 区间 (swing_candidates.judge 原样)。
  分段 = 挖掘段 2023-01-03 ~ 2025-06-30 / 留出段 2025-07-01 起 / 全窗 (齿轮显示全窗, 与挖掘候选同口径)。

v1 → v2 (2026-11-10) 只改了分段: v1 是挖掘段到 2024-12-31、留出段 2025-01-02 起。挖掘端选公式用的数据若
延到 2025-06-30, ZenTrade 仍按 v1 复核, 2025-01 ~ 06 既用来选公式又用来“独立确认”, 确认就不独立了。
候选池文件 (pool / panel) 与分段无关, v1 导出的池子可以直接按 v2 评估。分段不进 pool_sha (它只认数据),
而是每份复核结果、meta、接口状态都带 split 字段 (见 split()); 对账先比 split, 分段不同的结果不可比、也不给“候选”。

数据交接 (两个系统读同一份文件、跑同一份代码, 数字逐位一致):
  research/rank_lab/pool.parquet   候选池格子 (t, date, symbol, fill_t, exit_t, r; 未成交 r 为空)
  research/rank_lab/panel.parquet  自选日K (算因子用; 与池同一次导出)
  research/rank_lab/meta.json      窗口 / 交易日 / 名单 / 大盘黄灯 / 板块 / 账户约束 / 分段 / pool_sha
  research/rank_lab/rankers.json   已导入的排序因子;  results.json = ZenTrade 自己对它们的复核结果
外部挖掘端 (研究系统) 只读挂载本目录与 backend 源码, 用本模块的纯函数打分; 导入走
POST /api/factor-lab/rankers/import (研究投递密钥), ZenTrade 用同一份池重算后才在齿轮显示数字。
导入 ≠ 启用: 只有在机会页齿轮把它勾进做多 3 条并保存, 盘后扫描才按它出卡 (纸面)。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.services import swing_candidates as sc

logger = logging.getLogger(__name__)

CONTRACT = "rank-lab-v2"
POOL_CONTRACTS = ("rank-lab-v1", CONTRACT)  # 能读的候选池: v1 与 v2 的池文件格式相同, 只是分段不同 (分段在代码里)
PER_SIDE = sc.PER_SIDE                      # 机会板每天最多 5 张做多卡
SEEDS = sc.SEEDS
MINING_END = date(2025, 6, 30)              # 挖掘段终点 (含); 之后 = 留出段。改它 = 改合同, 必须与挖掘端同时改
HOLDOUT_START = date(2025, 7, 1)
MAX_WARMUP = 250                            # 公式预热上限 (盘后扫描的日K约 275 个交易日)
PANEL_WARM_DAYS = 420                       # 面板比窗口多取的自然日 (≈ 290 交易日)
BASE_COLS = ("open", "high", "low", "close", "volume", "amount", "turnover_rate", "raw_close")
RANK_PREFIX = "rank_"
MIN_SEG_N = 30                              # 段内成交少于 30 笔不算过关
RSI_MAX = 30                                # ts_rsi 窗口上限 (见 check_formula)
MIN_COVERAGE = 0.8                          # 4h 门内格子里有值的比例下限 (见 check_values)
_ID_RE = re.compile(r"^rank_[a-f0-9]{10}$")


def lab_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "rank_lab"


def is_ranker(rule_id: Any) -> bool:
    return isinstance(rule_id, str) and rule_id.startswith(RANK_PREFIX)


def split() -> dict[str, str]:
    """现行分段。每份复核结果、meta、接口状态都带它: 两端 (或新旧两次) 分段不同时, 数字不可比, 一眼能看出来。"""
    return {"mining_end": MINING_END.isoformat(), "holdout_start": HOLDOUT_START.isoformat()}


def _norm_split(x: Any) -> dict[str, str] | None:
    """挖掘端报的分段 → 与 split() 同形; 认 {mining_end, holdout_start} 或 [挖掘段终点, 留出段起点]。读不懂 → None。"""
    if isinstance(x, dict):
        x = [x.get("mining_end"), x.get("holdout_start")]
    if isinstance(x, (list, tuple)) and len(x) == 2 and all(isinstance(v, str) for v in x):
        try:
            a, b = (date.fromisoformat(v[:10]).isoformat() for v in x)
        except ValueError:
            return None
        return {"mining_end": a, "holdout_start": b}
    return None


def account_caps() -> dict[str, int]:
    r = sc.RISK_PCT
    return {"max_pos": int(0.06 / r), "day_cap": int(0.05 / r), "sect_cap": int(0.02 / r), "yellow_cap": int(0.03 / r)}


# ── 文件锁 (导入请求与盘后子进程会写同一份 rankers / results) ─────────────────────

_TLOCK = threading.Lock()


@contextlib.contextmanager
def _locked(data_dir: Path):
    d = lab_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    with _TLOCK:
        fh = open(d / ".lock", "a+")          # noqa: SIM115 - 锁文件句柄跟随上下文关闭
        try:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except ImportError:               # Windows: 只有进程内锁
                pass
            yield
        finally:
            fh.close()


def _read_json(p: Path, default):
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(p: Path, obj) -> None:
    from app.services.fs_utils import atomic_write_text
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(obj, ensure_ascii=False, indent=1))


# ── 面板与公式 (纯函数; 挖掘端在自己的容器里调同一份) ──────────────────────────────

class RankFormulaError(ValueError):
    """公式不能当排序因子用。code = DSL 错误码或 R0xx (本模块的额外限制)。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def prepare_panel(df: pl.DataFrame) -> pl.DataFrame:
    """日K → 公式面板: 只留基础列 (缺的列补空), 转 Float64, 按 (symbol, date) 排序, 加 prev_close = 上一根收盘。
    盘后扫描 (frames) 与候选池导出 (panel.parquet) 都走这里, 两边同式。"""
    if df.is_empty():
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, **{c: pl.Float64 for c in BASE_COLS},
                                    "prev_close": pl.Float64})
    cols = [pl.col("symbol").cast(pl.Utf8), pl.col("date").cast(pl.Date)]
    for c in BASE_COLS:
        cols.append(pl.col(c).cast(pl.Float64) if c in df.columns else pl.lit(None, dtype=pl.Float64).alias(c))
    out = df.select(cols).unique(subset=["symbol", "date"], keep="last").sort(["symbol", "date"])
    return out.with_columns(pl.col("close").shift(1).over("symbol").alias("prev_close"))


def check_formula(formula: str):
    """编译 + 排序因子的额外限制。返回 CompiledFormula; 不合格抛 RankFormulaError。"""
    from app.factors.dsl import BASE_COLUMNS, compile_formula
    text = (formula or "").strip()
    if not text:
        raise RankFormulaError("R001", "公式为空")
    if len(text) > 600:
        raise RankFormulaError("R002", "公式过长 (> 600 字符)")
    compiled = compile_formula(text)
    if not compiled.ok:
        e = compiled.errors[0]
        raise RankFormulaError(e.code, e.message)
    if compiled.referenced_factors:
        raise RankFormulaError("R003", "排序因子只能用基础行情列 (open/high/low/close/volume/amount/turnover_rate/"
                               f"prev_close/raw_close), 不能引用因子库: {sorted(compiled.referenced_factors)}")
    extra = set(compiled.dependencies) - set(BASE_COLUMNS)
    if extra:
        raise RankFormulaError("R003", f"用到了基础列以外的字段: {sorted(extra)}")
    if compiled.warmup_bars > MAX_WARMUP:
        raise RankFormulaError("R004", f"预热 {compiled.warmup_bars} 根超过上限 {MAX_WARMUP} (窗口嵌套太长)")
    for m in re.finditer(r"ts_rsi\s*\(", text):
        tail = text[m.end():]
        depth, i = 1, 0
        while i < len(tail) and depth:
            depth += {"(": 1, ")": -1}.get(tail[i], 0)
            i += 1
        last = tail[:i - 1].rsplit(",", 1)[-1].strip()
        try:
            n = float(last)
        except ValueError:
            continue
        if n > RSI_MAX:
            raise RankFormulaError("R007", f"ts_rsi 的窗口不能超过 {RSI_MAX} (指数平滑记忆太长, 盘后扫描只有约 275 根日K, 与回放算不一致)")
    bad = _adjustment_leak(text, compiled)
    if bad:
        raise RankFormulaError("R008", "公式值随复权基准变化 (价格列是导出日回溯的前复权价, volume 按拆股调整, "
                               f"amount / raw_close 是当日原值): {bad}。价格与量只能用比值、收益率或与自身历史比较, "
                               "不能跨股票比价格水平, 也不能把价格列与 amount / raw_close 混算")
    return compiled


# ── 复权基准无关检验 (R008) ──────────────────────────────────────────────────
# 回放面板的 open/high/low/close/prev_close = 当日原价 x 导出日之前全部拆股/分红的累计因子 (前复权),
# volume = 原量 ÷ 拆股因子; amount、raw_close、turnover_rate 不复权。累计因子由信号日之后的公司行动决定,
# 回放时公式若依赖它的水平, 就提前看到了未来的拆股 / 分红 (实测: close*volume/amount 样本内
# p=0.002, 剔除复权因子后 p=0.23, 留出段 -0.10R)。检验: 每只股票把价格列乘一个常数、volume 乘另一个常数
# (模拟不同的未来复权), 预热期之后公式值必须不变。

_PIT_C = (0.1, 0.5, 2.0, 7.0, 13.0, 0.03)      # 每只的价格缩放 (分红 / 拆股)
_PIT_V = (10.0, 0.2, 3.0, 0.5, 1 / 7, 40.0)     # 每只的成交量缩放 (拆股)
_PIT_PANELS: list[pl.DataFrame] = []


def _pit_panels() -> tuple[pl.DataFrame, pl.DataFrame]:
    """确定性的探针面板 (6 只 x 320 天): 原样一份, 按 _PIT_C / _PIT_V 缩放一份。"""
    if not _PIT_PANELS:
        rng = np.random.default_rng(20261107)
        start = date(2022, 1, 3)
        days = [start + timedelta(days=i) for i in range(470) if (start + timedelta(days=i)).weekday() < 5][:320]
        rows: list[list[dict]] = [[], []]
        for i in range(len(_PIT_C)):
            px, k = 40.0 + 25 * i, (1.0, 1.4, 0.8, 2.0, 1.1, 0.6)[i]
            for d in days:
                c = px * (1 + rng.normal(0.0005, 0.02))
                o = px * (1 + rng.normal(0, 0.006))
                h = max(o, c) * (1 + abs(rng.normal(0, 0.008)))
                lo = min(o, c) * (1 - abs(rng.normal(0, 0.008)))
                vol = float(np.exp(rng.normal(14, 0.5)))
                base = {"symbol": f"P{i}", "date": d, "amount": (h + lo + c) / 3 * vol * k,
                        "turnover_rate": float(rng.uniform(0.005, 0.05)), "raw_close": c * k}
                for s, (cs, vs) in enumerate(((1.0, 1.0), (_PIT_C[i], _PIT_V[i]))):
                    rows[s].append({**base, "open": o * cs, "high": h * cs, "low": lo * cs, "close": c * cs,
                                    "volume": vol * vs})
                px = c
        _PIT_PANELS.extend(prepare_panel(pl.DataFrame(r)) for r in rows)
    return _PIT_PANELS[0], _PIT_PANELS[1]


_PIT_CACHE: dict[str, str] = {}


def _adjustment_leak(text: str, compiled) -> str:
    """'' = 与复权基准无关; 否则返回说明。只比较预热期之后的行 (预热期的空值兜底不算)。按公式文本缓存。"""
    key = re.sub(r"\s+", "", text)
    if key in _PIT_CACHE:
        return _PIT_CACHE[key]
    from app.factors.dsl import FACTOR_COLUMN
    p0, p1 = _pit_panels()
    try:
        a = compiled.frame_transform(p0)
        b = compiled.frame_transform(p1)
    except Exception as e:                       # 运行期失败交给评估报错, 这里不判
        logger.debug("rank lab pit probe skipped: %s", type(e).__name__)
        return ""
    if a is None or b is None:
        return ""
    # 只看预热期之后, 且比的是每天 6 只的截面名次 (机会板只用名次挑前 5): 嵌套滚动算子会把预热期的大数
    # 带进 polars 的滚动累加器, 留下 1e-6 级的浮点残差, 按数值逐位比会误报 (实测 ts_corr(收益, 量比))。
    keep = pl.int_range(pl.len()).over("symbol") >= compiled.warmup_bars + 1
    v = pl.col(FACTOR_COLUMN).cast(pl.Float64)

    def ranks(df: pl.DataFrame) -> pl.DataFrame:
        return (df.select("symbol", "date", pl.when(v.is_finite()).then(v).otherwise(None).alias("v")).filter(keep)
                .with_columns(pl.col("v").rank("average").over("date").alias("r")).sort(["date", "symbol"]))
    ra, rb = ranks(a), ranks(b)
    bad_rows = ra.select("date", (pl.col("r").ne_missing(rb["r"])).alias("bad")).filter(pl.col("bad"))
    n_days = ra["date"].n_unique()
    bad_days = bad_rows["date"].n_unique()
    out = ""
    if bad_days > max(2, int(0.01 * n_days)):
        out = f"{bad_days}/{n_days} 天的截面名次改变"
    if len(_PIT_CACHE) > 512:
        _PIT_CACHE.clear()
    _PIT_CACHE[key] = out
    return out


def factor_frame(panel: pl.DataFrame, formula: str, direction: str) -> pl.DataFrame:
    """面板 → [symbol, date, value]; value 已按方向定向 (越大越该先挂)。非有限值保留为 null。
    0.55.0: 每只股票前 warmup_bars - 1 根 (公式还没攒够历史) 一律置空。DSL 的 max / min 会跳过空值,
    预热期 max(ts_mean(volume,20), 0.0001) 返回 0.0001, 新上市股票头几天的量比就成了 1e10 级, 天天排第一。"""
    if direction not in ("high", "low"):
        raise RankFormulaError("R005", "方向只能是 high (值大优先) 或 low (值小优先)")
    from app.factors.dsl import FACTOR_COLUMN
    compiled = check_formula(formula)
    out = compiled.frame_transform(panel)
    if out is None:
        raise RankFormulaError("E013", "面板缺少公式所需列")
    v = pl.col(FACTOR_COLUMN).cast(pl.Float64)
    v = v if direction == "high" else -v
    warm = pl.int_range(pl.len()).over("symbol") >= max(compiled.warmup_bars - 1, 0)
    return out.select("symbol", "date", pl.when(warm & v.is_finite()).then(v).otherwise(None).alias("value"))


def formula_sha(formula: str, direction: str) -> str:
    norm = re.sub(r"\s+", "", formula or "")
    return hashlib.sha256(f"{norm}|{direction}".encode()).hexdigest()


def ranker_id(formula: str, direction: str) -> str:
    return RANK_PREFIX + formula_sha(formula, direction)[:10]


# ── 候选池 (纯数据结构) ───────────────────────────────────────────────────────

@dataclass
class Pool:
    days: list[date]
    syms: list[str]
    cells: dict[tuple[int, int], tuple[int, int, float] | None]
    gate_js: list[list[int]]                    # 每个 t: 池内格子的 j (升序, 与基线随机抽样同序)
    yellow: dict[int, bool]
    sectors: dict[str, str]
    panel: pl.DataFrame
    meta: dict = field(default_factory=dict)
    _base: dict | None = None

    @property
    def sha(self) -> str | None:
        return self.meta.get("pool_sha")


def load_pool(data_dir_or_lab: Path) -> Pool | None:
    """读 research/rank_lab/ (传 data_dir 或该目录本身)。缺文件 / 合同不符 → None。"""
    d = Path(data_dir_or_lab)
    if not (d / "meta.json").exists():
        d = lab_dir(d)
    meta = _read_json(d / "meta.json", {})
    if meta.get("contract") not in POOL_CONTRACTS:
        return None
    try:
        pool = pl.read_parquet(d / "pool.parquet")
        panel = pl.read_parquet(d / "panel.parquet")
    except Exception as e:
        logger.warning("rank lab: pool unreadable: %s", type(e).__name__)
        return None
    days = [date.fromisoformat(x) for x in meta.get("days") or []]
    syms = list(meta.get("symbols") or [])
    if not days or not syms:
        return None
    si = {s: j for j, s in enumerate(syms)}
    cells: dict[tuple[int, int], tuple[int, int, float] | None] = {}
    gate_js: list[list[int]] = [[] for _ in days]
    for t, sym, fi, xi, r in pool.select(["t", "symbol", "fill_t", "exit_t", "r"]).iter_rows():
        j = si.get(sym)
        if j is None or not (0 <= t < len(days)):
            continue
        cells[(int(t), j)] = None if r is None else (int(fi), int(xi), float(r))
        gate_js[int(t)].append(j)
    gate_js = [sorted(set(x)) for x in gate_js]
    yellow = {int(t): True for t in meta.get("yellow_t") or []}
    return Pool(days=days, syms=syms, cells=cells, gate_js=gate_js, yellow=yellow,
                sectors=dict(meta.get("sectors") or {}), panel=panel, meta=meta)


def value_matrix(pool: Pool, frame: pl.DataFrame) -> np.ndarray:
    """[symbol, date, value] → (T, N), 缺 = NaN。"""
    di = {d: i for i, d in enumerate(pool.days)}
    si = {s: j for j, s in enumerate(pool.syms)}
    out = np.full((len(pool.days), len(pool.syms)), np.nan)
    frame = frame.filter((pl.col("date") >= pool.days[0]) & (pl.col("date") <= pool.days[-1]) & pl.col("value").is_not_null())
    for sym, d, v in frame.select(["symbol", "date", "value"]).iter_rows():
        t, j = di.get(d), si.get(sym)
        if t is not None and j is not None and v is not None:
            out[t, j] = float(v)
    return out


def picks(pool: Pool, vals: np.ndarray) -> tuple[list[tuple], list[tuple[int, str]]]:
    """每天池内按值取前 5 (只取有限值; 同值按代码)。返回 (成交候选 picks, 上板 (t, 代码))。"""
    out, chosen = [], []
    for t, js in enumerate(pool.gate_js):
        cand = [j for j in js if math.isfinite(vals[t, j])]
        cand.sort(key=lambda j: (-vals[t, j], pool.syms[j]))
        for j in cand[:PER_SIDE]:
            chosen.append((t, pool.syms[j]))
            v = pool.cells[(t, j)]
            if v is not None:
                out.append((v[0], v[1], pool.syms[j], t, v[2], float(vals[t, j])))
    return out, chosen


def _account(pool: Pool, ps: list[tuple]) -> list[dict]:
    return sc.account(ps, yellow=pool.yellow, sectors=pool.sectors, **account_caps())


def baseline_runs(pool: Pool) -> list[list[dict]]:
    """随机基线 (种子 1-5): 每天从同一池随机挑 5, 同一账户约束 (swing_candidates.evaluate 同式)。缓存在 pool 上。"""
    if pool._base is None:
        runs = []
        for seed in SEEDS:
            rg = np.random.default_rng(seed)
            bp = []
            for t, js in enumerate(pool.gate_js):
                if not js:
                    continue
                for j in rg.permutation(js)[:PER_SIDE]:
                    v = pool.cells[(t, int(j))]
                    if v is not None:
                        bp.append((v[0], v[1], pool.syms[int(j)], t, v[2], 0.0))
            runs.append(_account(pool, bp))
        pool._base = {"runs": runs}
    return pool._base["runs"]


def week_boot(pairs: list[tuple[int, float]], *, n_boot: int = sc.N_BOOT, seed: int = 7):
    """swing_candidates.week_boot_ci 同一抽样 (区间逐位相同), 另给自助均值的标准差。样本 < 10 → (None, None)。"""
    if len(pairs) < 10:
        return None, None
    from collections import defaultdict
    s, n = defaultdict(float), defaultdict(int)
    for t, x in pairs:
        s[t // 5] += x
        n[t // 5] += 1
    ks = list(s)
    sums, cnts = np.array([s[k] for k in ks]), np.array([n[k] for k in ks])
    pick = np.random.default_rng(seed).integers(0, len(ks), (n_boot, len(ks)))
    m = sums[pick].sum(1) / np.maximum(cnts[pick].sum(1), 1)
    ci = [round(float(np.percentile(m, 2.5)), 4), round(float(np.percentile(m, 97.5)), 4)]
    return ci, float(np.std(m, ddof=1))


def judge_segment(cand: list[dict], base_runs: list[list[dict]], years: float) -> dict:
    """swing_candidates.judge + 单侧 p (正态近似: 超额均值 / 自助标准差), 供挖掘端做多重检验校正。"""
    res = sc.judge(cand, base_runs, years)
    from collections import defaultdict
    by_day = defaultdict(list)
    for run in base_runs:
        for x in run:
            by_day[x["t"]].append(x["r"])
    day_mean = {t: float(np.mean(v)) for t, v in by_day.items()}
    glob = float(np.mean([x["r"] for run in base_runs for x in run])) if any(base_runs) else 0.0
    diff = [(x["t"], x["r"] - day_mean.get(x["t"], glob)) for x in cand]
    _ci, sd = week_boot(diff)
    p = None
    if sd and sd > 0 and res.get("excess") is not None:
        z = float(np.mean([d for _, d in diff])) / sd
        p = round(0.5 * math.erfc(z / math.sqrt(2)), 6)
    res["p"] = p
    res["win_rate"] = round(float(np.mean([x["r"] > 0 for x in cand])), 4) if cand else None
    return res


def _by_year(pool: Pool, cand: list[dict], base_runs: list[list[dict]]) -> dict[str, dict]:
    """分年: 笔数 / 每笔 R / 比同日随机多的 R (与 judge 同一减法)。看是不是只靠某一年。"""
    from collections import defaultdict
    by_day = defaultdict(list)
    for run in base_runs:
        for x in run:
            by_day[x["t"]].append(x["r"])
    day_mean = {t: float(np.mean(v)) for t, v in by_day.items()}
    glob = float(np.mean([x["r"] for run in base_runs for x in run])) if any(base_runs) else 0.0
    acc: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for x in cand:
        acc[str(pool.days[x["t"]].year)].append((x["r"], x["r"] - day_mean.get(x["t"], glob)))
    return {y: {"n": len(v), "mean_r": round(float(np.mean([a for a, _ in v])), 4),
                "excess": round(float(np.mean([b for _, b in v])), 4)} for y, v in sorted(acc.items())}


def _segments(pool: Pool) -> dict[str, tuple[int, int]]:
    """段名 → [t0, t1) 。"""
    n = len(pool.days)
    cut = next((i for i, d in enumerate(pool.days) if d > MINING_END), n)
    return {"mining": (0, cut), "holdout": (cut, n), "full": (0, n)}


def evaluate_values(pool: Pool, vals: np.ndarray) -> dict:
    """已定向的值矩阵 → 三段判定 + 上板摘要。"""
    ps, chosen = picks(pool, vals)
    cand = _account(pool, ps)
    base = baseline_runs(pool)
    out: dict[str, Any] = {}
    for seg, (t0, t1) in _segments(pool).items():
        if t1 <= t0:
            out[seg] = None
            continue
        c = [x for x in cand if t0 <= x["t"] < t1]
        b = [[x for x in run if t0 <= x["t"] < t1] for run in base]
        out[seg] = {**judge_segment(c, b, (t1 - t0) / 252),
                    "window": [pool.days[t0].isoformat(), pool.days[t1 - 1].isoformat()]}
    out["by_year"] = _by_year(pool, cand, base)
    finite = np.isfinite(vals)
    pool_cells = sum(len(js) for js in pool.gate_js)
    covered = sum(1 for t, js in enumerate(pool.gate_js) for j in js if finite[t, j])
    out["coverage"] = round(covered / pool_cells, 4) if pool_cells else 0.0
    out["picks_sha"] = hashlib.sha256(json.dumps(sorted(chosen)).encode()).hexdigest()[:16]
    out["board_days"] = len({t for t, _ in chosen})
    out["pool_sha"] = pool.sha
    out["contract"] = CONTRACT
    out["split"] = split()
    return out


def evaluate_formula(pool: Pool, formula: str, direction: str) -> dict:
    """公式 → 三段判定。公式问题抛 RankFormulaError。"""
    frame = factor_frame(pool.panel, formula, direction)
    vals = value_matrix(pool, frame)
    check_values(pool, vals, _segments(pool)["mining"][1])
    return evaluate_values(pool, vals)


def check_values(pool: Pool, vals: np.ndarray, t_end: int | None = None) -> float:
    """值矩阵的可用性: 全空 → R006; 4h 门内格子 (t < t_end) 有值比例 < MIN_COVERAGE → R009。返回覆盖率。
    覆盖率低的公式 (如只有少数股票有 turnover_rate) 只在有数据的那几只里排, 等于换了一个候选池。
    覆盖率只按挖掘段算 (evaluate_formula 与挖掘端同口径), 不看留出段。"""
    if not np.isfinite(vals).any():
        raise RankFormulaError("R006", "公式在候选池里没有有效数值")
    t_end = len(pool.gate_js) if t_end is None else t_end
    finite = np.isfinite(vals)
    cells = sum(len(js) for js in pool.gate_js[:t_end])
    covered = sum(1 for t, js in enumerate(pool.gate_js[:t_end]) for j in js if finite[t, j])
    cov = covered / cells if cells else 0.0
    if cov < MIN_COVERAGE:
        raise RankFormulaError("R009", f"4h 门内只有 {cov:.0%} 的格子有值 (下限 {MIN_COVERAGE:.0%}), 数据太稀疏")
    return cov


def daily_pick_sets(pool: Pool, formula: str, direction: str) -> dict[int, list[str]]:
    """查重用: {t: 当天上板的 5 只}。"""
    vals = value_matrix(pool, factor_frame(pool.panel, formula, direction))
    _ps, chosen = picks(pool, vals)
    out: dict[int, list[str]] = {}
    for t, s in chosen:
        out.setdefault(t, []).append(s)
    return out


def verdict(res: dict | None) -> dict:
    """ZenTrade 侧“候选”标记: 全窗 (2023 起) 周聚类区间下限 > 0 (与挖掘候选同口径), 且留出段 (HOLDOUT_START 起,
    挖掘端选公式时不看) 自己的区间下限也 > 0、笔数 ≥ 30。
    0.55.0 起留出段要区间下限 > 0, 不再只看点估计: 全窗一半样本是挑公式用的挖掘段, 挖掘段刚过多重检验时,
    全窗下限 > 0 只要求留出段 z ≥ -0.2 左右, 留出段纯噪声的公式约一半会被判候选。
    结果不是按现行分段算的 (旧结果没有 split, 或分段已改) → 不给候选 (stale_split): 它的“留出段”
    不是现行留出段, 可能含挖掘端已经用来选公式的月份; 重导 / 重算后恢复。"""
    if not isinstance(res, dict):
        return {"passed": False, "both_segments_positive": False}
    if res.get("split") != split():
        return {"passed": False, "both_segments_positive": False, "stale_split": True}
    full, mine, hold = res.get("full") or {}, res.get("mining") or {}, res.get("holdout") or {}

    def pos(x: dict) -> bool:
        return isinstance(x.get("excess"), (int, float)) and x["excess"] > 0 and (x.get("n") or 0) >= MIN_SEG_N

    def sig(x: dict) -> bool:
        ci = x.get("excess_ci")
        return (isinstance(ci, (list, tuple)) and len(ci) == 2 and isinstance(ci[0], (int, float))
                and math.isfinite(ci[0]) and ci[0] > 0 and (x.get("n") or 0) >= MIN_SEG_N)
    return {"passed": bool(full.get("passed")) and sig(hold), "both_segments_positive": pos(mine) and pos(hold)}


# ── 候选池导出 (ZenTrade 侧, 要 30 分钟K / 4h 门 / 交易日历) ───────────────────────

def build_pool(data_dir: Path, repo, *, end: date, watchlist: set[str], out_dir: Path | None = None) -> dict:
    """在自选上算 4h 门与每格卡结局, 连同日K面板写进 research/rank_lab/ (原子写; out_dir 给了就写那里)。返回 meta。"""
    from app.services import kline_4h
    days = kline_4h.trading_days(repo, sc.WINDOW_START, end)
    if len(days) < 60:
        raise ValueError(f"trading days too few: {len(days)}")
    want = sorted(watchlist)
    raw = repo.get_daily_batch(want, panel_start(), end, columns=["symbol", "date", *BASE_COLS])
    if raw is None or raw.is_empty():
        raise ValueError("no daily bars for watchlist")
    panel = prepare_panel(raw.filter(pl.col("date") <= end))
    syms = sorted(set(panel["symbol"].to_list()))
    di, si = {d: i for i, d in enumerate(days)}, {s: j for j, s in enumerate(syms)}
    n_days, n_syms = len(days), len(syms)
    close, hi, lo = (np.full((n_days, n_syms), np.nan) for _ in range(3))
    for s, d, c, h, low in panel.select(["symbol", "date", "close", "high", "low"]).iter_rows():
        t = di.get(d)
        if t is not None and c is not None:
            close[t, si[s]], hi[t, si[s]], lo[t, si[s]] = c, h, low
    # ATR14 与 swing_candidates.evaluate 同式 (窗口内滚动, 前 13 天为空)
    pc = np.vstack([close[:1], close[:-1]])
    tr = np.fmax(hi - lo, np.fmax(np.abs(hi - pc), np.abs(lo - pc)))
    atr = np.full_like(close, np.nan)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for t in range(13, n_days):
            atr[t] = np.nanmean(tr[t - 13:t + 1], axis=0)
    gate = sc._gate_matrix(data_dir, repo, syms, days)
    cells = sc._cells(data_dir, repo, syms, days, close, atr, gate)
    yellow = sc._spy_below_ma200(repo, days)
    sectors = sc._sectors(repo)
    rows = []
    for (t, j), v in sorted(cells.items()):
        rows.append({"t": t, "date": days[t], "symbol": syms[j], "fill_t": None if v is None else v[0],
                     "exit_t": None if v is None else v[1], "r": None if v is None else float(v[2])})
    pool_df = pl.DataFrame(rows, schema={"t": pl.Int32, "date": pl.Date, "symbol": pl.Utf8, "fill_t": pl.Int32,
                                         "exit_t": pl.Int32, "r": pl.Float64})
    d = Path(out_dir) if out_dir is not None else lab_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    from app.services.fs_utils import atomic_write_parquet
    atomic_write_parquet(pool_df, d / "pool.parquet")
    atomic_write_parquet(panel, d / "panel.parquet")
    h = hashlib.sha256()
    for name in ("pool.parquet", "panel.parquet"):
        h.update((d / name).read_bytes())
    filled = sum(1 for v in cells.values() if v is not None)
    meta = {
        "contract": CONTRACT, "card_contract": "swing-desk-tight-v1", "baseline": "same pool random 5, seeds 1-5",
        "window": [days[0].isoformat(), days[-1].isoformat()], "data_end": end.isoformat(),
        "mining": [days[0].isoformat(), MINING_END.isoformat()], "holdout_start": HOLDOUT_START.isoformat(),
        "split": split(),
        "days": [x.isoformat() for x in days], "symbols": syms, "watchlist_n": len(want),
        "yellow_t": sorted(t for t, v in yellow.items() if v), "sectors": {s: sectors[s] for s in syms if s in sectors},
        "per_side": PER_SIDE, "risk_pct": sc.RISK_PCT, "caps": account_caps(), "max_warmup": MAX_WARMUP,
        "counts": {"cells": len(cells), "filled": filled, "gate_per_day": round(len(cells) / n_days, 2)},
        "pool_sha": h.hexdigest()[:16],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_json(d / "meta.json", meta)
    return meta


# ── 已导入的排序因子 + ZenTrade 自己的复核结果 ──────────────────────────────────

def read_rankers(data_dir: Path, *, include_removed: bool = False) -> dict[str, dict]:
    d = _read_json(lab_dir(data_dir) / "rankers.json", {})
    out = {k: v for k, v in (d.get("rankers") or {}).items() if _ID_RE.match(str(k)) and isinstance(v, dict)}
    return out if include_removed else {k: v for k, v in out.items() if not v.get("removed_at")}


def read_results(data_dir: Path) -> dict:
    return _read_json(lab_dir(data_dir) / "results.json", {})


def ranker_names(data_dir: Path) -> dict[str, str]:
    return {k: f"排序 · {v.get('name') or k}" for k, v in read_rankers(data_dir).items()}


def import_ranker(data_dir: Path, *, name: str, formula: str, direction: str, idea: str = "",
                  source: dict | None = None) -> dict:
    """导入一条排序因子 (幂等: 同公式同方向 = 同一个 id)。公式不合格抛 RankFormulaError。
    做三件事: ① 登记 rankers.json ② 写进因子库 (自定义因子 uf_rk_*, 草稿) ③ 用当前候选池立即复核一次。"""
    formula = (formula or "").strip()
    check_formula(formula)
    if direction not in ("high", "low"):
        raise RankFormulaError("R005", "方向只能是 high 或 low")
    name = re.sub(r"\s+", " ", (name or "").strip())[:40] or "未命名排序因子"
    rid = ranker_id(formula, direction)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    src = {k: v for k, v in (source or {}).items() if k in (
        "system", "id", "run_id", "mining", "holdout", "full", "family_size", "fdr_pass", "pool_sha", "split",
        "status", "url", "rank")}
    fid = "uf_rk_" + rid[len(RANK_PREFIX):]
    with _locked(data_dir):
        p = lab_dir(data_dir) / "rankers.json"
        doc = _read_json(p, {})
        allr = doc.get("rankers") if isinstance(doc.get("rankers"), dict) else {}
        prev = allr.get(rid) or {}
        entry = {"id": rid, "name": name, "formula": formula, "direction": direction, "idea": (idea or "")[:400],
                 "factor_id": fid, "source": src or prev.get("source") or {},
                 "imported_at": prev.get("imported_at") or now, "updated_at": now, "removed_at": None}
        allr[rid] = entry
        _write_json(p, {"contract": CONTRACT, "rankers": allr})
    factor_ok = _register_library_factor(data_dir, fid, name, formula, direction, idea)
    res = evaluate_and_store(data_dir, [rid])
    mine = (res.get("rankers") or {}).get(rid)
    par = parity(src, mine, res.get("pool_sha"))
    with _locked(data_dir):
        p = lab_dir(data_dir) / "rankers.json"
        doc = _read_json(p, {})
        allr = doc.get("rankers") if isinstance(doc.get("rankers"), dict) else {}
        if rid in allr:
            allr[rid]["parity"] = par
            _write_json(p, {"contract": CONTRACT, "rankers": allr})
            entry = allr[rid]
    return {"ranker": entry, "factor_id": fid if factor_ok else None, "result": mine,
            "pool_sha": res.get("pool_sha"), "created": not prev, "parity": par}


def parity(source: dict | None, mine: dict | None, pool_sha: str | None) -> dict:
    """挖掘端报的成绩 vs ZenTrade 用同一份候选池重算的成绩。同一 pool_sha、同一分段下三段的笔数 / 每笔 R / 超额 / 区间
    必须逐位相同; pool_sha 不同 (池子已重导) 或分段不同 = 不可比, 以 ZenTrade 这边为准。
    pool_sha 只认池文件, 不含分段: 挖掘端要另报 split (与 split() 同形, 或 [挖掘段终点, 留出段起点])。报了且与这边
    不同 (或读不懂) → 不比数字 (否则会把口径差报成“数字对不上”); 没报 → 照旧比数字, 对不上时注明可能是分段不同。"""
    src = source or {}
    if not src.get("pool_sha"):
        return {"checked": False, "reason": "挖掘端没报 pool_sha"}
    if src.get("pool_sha") != pool_sha:
        return {"checked": False, "reason": "候选池已更新, 以 ZenTrade 复核为准", "source_pool": src.get("pool_sha"),
                "zentrade_pool": pool_sha}
    if not isinstance(mine, dict) or mine.get("error"):
        return {"checked": False, "reason": "ZenTrade 复核失败"}
    declared = src.get("split")
    if declared is not None:
        want = _norm_split(declared)
        if want is None:
            return {"checked": False, "reason": "挖掘端报的分段读不懂, 数字不可比, 以 ZenTrade 复核为准",
                    "source_split": declared, "zentrade_split": mine.get("split")}
        if want != mine.get("split"):
            return {"checked": False, "reason": "分段口径不同, 数字不可比, 以 ZenTrade 复核为准", "source_split": declared,
                    "zentrade_split": mine.get("split")}
    diff = []
    for seg in ("mining", "holdout", "full"):
        a, b = src.get(seg) or {}, mine.get(seg) or {}
        for k in ("n", "mean_r", "excess", "excess_ci"):
            if k in a and a.get(k) != b.get(k):
                diff.append(f"{seg}.{k}: 挖掘 {a.get(k)} / ZenTrade {b.get(k)}")
    # split_checked: 分段是否显式核对过 (挖掘端报了且与这边相同)。没报时 match 只说明报来的数字一致;
    # 只报全窗的话, 全窗与分段无关, 一致也不代表两边分段相同。
    out = {"checked": True, "match": not diff, "diff": diff[:6], "split_checked": declared is not None}
    if diff and declared is None:
        out["note"] = "挖掘端没报分段 (split), 对不上可能是分段口径不同"
    return out


def _register_library_factor(data_dir: Path, fid: str, name: str, formula: str, direction: str, idea: str) -> bool:
    """写进因子库 (自定义因子, 草稿), 便于在因子页做检验。已存在则不动。失败只记日志 (排序因子照常导入)。"""
    try:
        from app.factors import store
        if any(str(x.get("id")) == fid for x in store.load_all(Path(data_dir))):
            return True
        definition = {"id": fid, "kind": "custom", "version": 1, "label": f"排序·{name}"[:40], "group": "排序因子",
                      "formula": formula, "description": (idea or "外部研究系统导入的机会板排序因子")[:300],
                      "direction": direction, "status": "draft", "created_at": store._now(), "updated_at": store._now()}
        store.to_spec(definition)
        store.register_definition(definition)
        store.save_one(Path(data_dir), definition)
        return True
    except Exception as e:
        logger.warning("rank lab: library factor %s not saved: %s", fid, type(e).__name__)
        return False


def remove_ranker(data_dir: Path, rid: str, *, enabled_long: list[str]) -> dict:
    """标记移除 (保留记录与因子库条目)。正在 3+3 里 → ValueError (先在齿轮取消勾选)。"""
    if rid in enabled_long:
        raise ValueError("该排序因子正在机会板做多 3 条里, 先在齿轮取消勾选并保存")
    with _locked(data_dir):
        p = lab_dir(data_dir) / "rankers.json"
        doc = _read_json(p, {})
        allr = doc.get("rankers") if isinstance(doc.get("rankers"), dict) else {}
        if rid not in allr:
            raise KeyError(rid)
        allr[rid]["removed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _write_json(p, {"contract": CONTRACT, "rankers": allr})
        return allr[rid]


def evaluate_and_store(data_dir: Path, ids: list[str] | None = None) -> dict:
    """用当前候选池复核 (ids=None = 全部未移除的), 合并写 results.json。没有池 → 记 no_pool。"""
    pool = load_pool(data_dir)
    rankers = read_rankers(data_dir)
    todo = [r for r in (ids or list(rankers)) if r in rankers]
    fresh: dict[str, dict] = {}
    for rid in todo:
        rk = rankers[rid]
        if pool is None:
            fresh[rid] = {"error": "no_pool", "detail": "候选池还没导出 (每个交易日 17:40 ET 自动导出)"}
            continue
        try:
            res = evaluate_formula(pool, rk["formula"], rk["direction"])
            fresh[rid] = {**res, **verdict(res), "evaluated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        except RankFormulaError as e:
            fresh[rid] = {"error": e.code, "detail": e.message}
        except Exception as e:
            logger.warning("rank lab: evaluate %s failed: %s", rid, type(e).__name__)
            fresh[rid] = {"error": type(e).__name__, "detail": str(e)[:200]}
    with _locked(data_dir):
        p = lab_dir(data_dir) / "results.json"
        doc = _read_json(p, {})
        allres = doc.get("rankers") if isinstance(doc.get("rankers"), dict) else {}
        if ids is None:
            allres = {}
        allres.update(fresh)
        out = {"contract": CONTRACT, "split": split(), "pool_sha": pool.sha if pool else None,
               "data_end": pool.meta.get("data_end") if pool else None,
               "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "rankers": allres}
        _write_json(p, out)
    return out


def gear_view(res: dict | None) -> dict | None:
    """齿轮右侧数字 (与 opportunities._candidate_view 同字段): 全窗。"""
    if not isinstance(res, dict):
        return None
    if res.get("error"):
        return {"passed": False, "error": str(res["error"]), "detail": str(res.get("detail") or "")[:200]}
    full = res.get("full") or {}
    return {"passed": bool(verdict(res)["passed"]), "excess": full.get("excess"), "excess_ci": full.get("excess_ci"),
            "n": full.get("n"), "mean_r": full.get("mean_r"), "r_per_year": full.get("r_per_year"),
            "base_r_per_year": full.get("base_r_per_year"), "first_passed": None,
            # window = 这一段实际的起止日 (齿轮悬停的“挖掘段 / 留出段”月份读它, 不写死)
            "segments": {k: {kk: (res.get(k) or {}).get(kk)
                             for kk in ("n", "mean_r", "excess", "excess_ci", "passed", "window")}
                         for k in ("mining", "holdout")},
            "pool_sha": res.get("pool_sha")}


# ── 盘后扫描: 每条排序因子在自选上的当日值 (落进自选信号表, 机会板读) ────────────────

def panel_start() -> date:
    """公式面板的起点 (候选池导出与盘后扫描同一个): 起点相同, 指数平滑类算子 (ts_rsi) 的递推也逐位相同。"""
    return sc.WINDOW_START - timedelta(days=PANEL_WARM_DAYS)


def scan_panel(repo, frames: dict | None, watchlist: set[str], as_of: date) -> pl.DataFrame:
    """盘后扫描时的公式面板: 全部有日K的自选 (与候选池面板同一截面, 截面算子 rank/zscore 的分母一致),
    与候选池同一起点 (panel_start)。repo 读失败退回扫描帧 (只含流动性池里的自选, 历史较短)。"""
    raw = None
    if repo is not None:
        try:
            raw = repo.get_daily_batch(sorted(watchlist), panel_start(), as_of,
                                       columns=["symbol", "date", *BASE_COLS])
        except Exception as e:
            logger.warning("rank lab scan: daily bars unavailable, using scan frames: %s", type(e).__name__)
            raw = None
    if raw is None or raw.is_empty():
        parts = []
        for s in sorted(watchlist):
            b = (frames or {}).get(s)
            if b is None or b.is_empty():
                continue
            parts.append(b.select([c for c in ("symbol", "date", *BASE_COLS) if c in b.columns]))
        if not parts:
            return prepare_panel(pl.DataFrame())
        raw = pl.concat(parts, how="diagonal_relaxed")
    return prepare_panel(raw.filter(pl.col("date") <= as_of))


def scan_values(data_dir: Path, frames: dict | None, watchlist: set[str], as_of: date, repo=None) -> dict[str, dict]:
    """{rid: {name, direction, formula_sha, values: {代码: 定向值}}}; 没有排序因子 → {}。单条失败只跳过。"""
    rankers = read_rankers(data_dir)
    if not rankers:
        return {}
    panel = scan_panel(repo, frames, watchlist, as_of)
    if panel.is_empty():
        return {}
    out = {}
    for rid, rk in sorted(rankers.items()):
        try:
            fr = factor_frame(panel, rk["formula"], rk["direction"]).filter(pl.col("date") == as_of)
        except Exception as e:
            logger.warning("rank lab scan %s skipped: %s", rid, type(e).__name__)
            continue
        vals = {s: round(float(v), 10) for s, v in fr.select(["symbol", "value"]).iter_rows()
                if v is not None and math.isfinite(v)}
        out[rid] = {"name": rk.get("name") or rid, "direction": rk["direction"],
                    "formula_sha": formula_sha(rk["formula"], rk["direction"])[:16], "values": vals}
    return out


def backfill_latest(data_dir: Path, repo) -> dict:
    """导入后补算最近一份自选信号表里缺的排序值 (只加 rank_values 里没有的因子, 已有的不动; 数据截至该信号日收盘,
    不看未来)。该日下单簿已落盘 → 板按簿里冻结的 3+3 出, 补了也不改簿; 还没落簿 → 勾上后当天预览就能出卡。"""
    from app.services import watchlist_v4 as wv
    files = wv.signal_files(Path(data_dir))
    if not files:
        return {"skipped": "no_signal_file"}
    day, f = files[-1]
    payload = wv._read(f)
    if not isinstance(payload, dict) or payload.get("backfill"):
        return {"skipped": "bad_signal_file"}
    have = payload.get("rank_values") if isinstance(payload.get("rank_values"), dict) else {}
    missing = [rid for rid in read_rankers(data_dir) if rid not in have]
    if not missing:
        return {"day": day.isoformat(), "added": []}
    watch = {s for s in payload.get("watchlist") or [] if isinstance(s, str)}
    vals = scan_values(data_dir, None, watch, day, repo=repo)
    add = {rid: v for rid, v in vals.items() if rid in missing}
    if not add:
        return {"day": day.isoformat(), "added": []}
    from app.services.fs_utils import atomic_write_text
    with _locked(data_dir):
        payload = wv._read(f)                    # 锁内重读, 只合并新键
        if not isinstance(payload, dict):
            return {"skipped": "bad_signal_file"}
        cur = payload.get("rank_values") if isinstance(payload.get("rank_values"), dict) else {}
        cur.update({k: {**v, "values": {s: x for s, x in v["values"].items() if s in watch}, "backfilled": True}
                    for k, v in add.items() if k not in cur})
        payload["rank_values"] = cur
        atomic_write_text(f, json.dumps(payload, ensure_ascii=False))
    return {"day": day.isoformat(), "added": sorted(add)}


def board_hits(rank_values: dict | None, rule_ids: tuple[str, ...], eligible: set[str]) -> list[dict]:
    """机会板做多 (swing_desk.compose_boards 调用): 每条勾选的排序因子在 eligible (4h 门同向 + 能出紧几何卡的自选)
    里按值取前 5 → 合成命中行 (rank_pos 从 1 起)。与验收合同同一选法: 值大优先, 同值按代码, 非有限值不选。
    当天信号表没有该因子的值 (导入晚于扫描) → 不出。"""
    out = []
    for rid in rule_ids:
        if not is_ranker(rid):
            continue
        vals = ((rank_values or {}).get(rid) or {}).get("values") or {}
        cand = [(float(v), s) for s, v in vals.items()
                if s in eligible and isinstance(v, (int, float)) and math.isfinite(v)]
        cand.sort(key=lambda x: (-x[0], x[1]))
        for pos, (v, s) in enumerate(cand[:PER_SIDE], start=1):
            out.append({"rule": rid, "symbol": s, "side": "long", "rank_pos": pos, "rank_value": v})
    return out


# ── 盘后任务 / 子进程入口 ──────────────────────────────────────────────────────

def worker_run(data_dir: Path, repo, cfg: dict) -> dict:
    """子进程 (backtest/worker.py kind=rank_lab): 导出候选池 + 复核全部排序因子。"""
    from app.config import settings
    settings.backtest_matrix_disk_cache_enabled = False
    repo._enriched_warming = True
    meta = build_pool(Path(data_dir), repo, end=date.fromisoformat(cfg["end"]), watchlist=set(cfg["watchlist"]))
    res = evaluate_and_store(Path(data_dir))
    return {"pool_sha": meta["pool_sha"], "cells": meta["counts"]["cells"], "rankers": len(res.get("rankers") or {})}


_RUNNING = [0]


def is_running() -> bool:
    return _RUNNING[0] > 0


def run_nightly(data_dir: Path, repo) -> dict | None:
    """17:40 ET (交易日): 上一交易日为窗口终点 (最后几天的卡结局本来就未定)。盘中 (09:00-17:00 ET) 不跑。"""
    from app.market_clock import us_clock
    from app.services import decision_scan_job as job
    if not sc.can_run_now():
        return None
    watch = sorted(job._watchlist())
    if not watch:
        return {"skipped": "empty_watchlist"}
    from app.backtest.worker import make_worker_task, run_worker_task
    from app.services.heavy_job_limiter import shared_heavy_job_limiter
    cfg = {"end": us_clock.previous_trading_day(us_clock.today()).isoformat(), "watchlist": watch}
    _RUNNING[0] += 1
    try:
        with shared_heavy_job_limiter.slot("normal"):
            res = run_worker_task(make_worker_task("rank_lab", Path(data_dir), cfg))
    finally:
        _RUNNING[0] -= 1
    if res.get("error"):
        raise RuntimeError(f"rank lab worker failed: {res['error']}")
    return res


def status(data_dir: Path) -> dict:
    meta = _read_json(lab_dir(data_dir) / "meta.json", {})
    res = read_results(data_dir)
    # split = 现行分段; results_split = 已存复核结果所用的分段 (不同 = 结果是旧分段算的, 等重导 / 重算)
    return {"contract": CONTRACT, "split": split(), "pool_sha": meta.get("pool_sha"), "data_end": meta.get("data_end"),
            "window": meta.get("window"), "generated_at": meta.get("generated_at"), "counts": meta.get("counts"),
            "symbols": len(meta.get("symbols") or []), "results_at": res.get("generated_at"),
            "results_pool_sha": res.get("pool_sha"), "results_split": res.get("split"), "running": is_running()}
