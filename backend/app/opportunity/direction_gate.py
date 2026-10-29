"""机会板方向门 (可插拔)。

机会板 (swing_desk tight-v1) 只在「方向门」同向时出卡。方向门在盘后扫描时为自选股给出方向:
``long`` / ``short`` / 不给 (= 不放行)。扫描把结果写进当日自选信号快照的 ``h4_state`` 字段
(字段名沿用历史, 内容与具体门无关), 机会板和纸面下单簿只读快照, 不重算门。

内置门 ``daily_ma50`` (原创, MIT): 信号日收盘在 50 日简单均线之上 = long, 之下 = short;
样本不足或数据缺失 = 不放行 (fail-closed)。

可选插件门可以调用 :func:`register` 注册自己 (例如私有部署里的 4h 门)。选择顺序:
``rules/swing.json`` 的 ``"gate"`` 字段 (已注册才生效) → 插件注册时声明的 ``preferred`` 门 → ``daily_ma50``。
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_GATE_ID = "daily_ma50"
MA_WINDOW = 50

# states(ctx) -> {symbol: "long" | "short"}; ctx 见 GateContext
StatesFn = Callable[["GateContext"], dict[str, str]]
# matrix(data_dir, repo, symbols, days) -> bool (T, N): 研究回放用, True = 该日该票做多方向放行
MatrixFn = Callable[[Path, Any, list[str], list[date]], np.ndarray]


@dataclass(frozen=True)
class GateContext:
    """方向门计算所需的扫描上下文 (只读)。"""

    data_dir: Path
    repo: Any
    as_of: date
    symbols: tuple[str, ...]
    frames: dict = field(default_factory=dict)
    # 扫描中已经算好的插件结果 (例如插件门在自己的来源扫描里顺手给出的方向); 门可以直接复用
    precomputed: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DirectionGate:
    id: str
    name: str
    states: StatesFn
    matrix: MatrixFn | None = None
    preferred: bool = False


_REGISTRY: dict[str, DirectionGate] = {}
_LOCK = threading.Lock()


def register(gate: DirectionGate) -> None:
    with _LOCK:
        _REGISTRY[gate.id] = gate


def get(gate_id: str) -> DirectionGate | None:
    with _LOCK:
        return _REGISTRY.get(gate_id)


def available() -> list[dict]:
    with _LOCK:
        return [{"id": g.id, "name": g.name, "preferred": g.preferred} for g in _REGISTRY.values()]


def _configured_id(data_dir: Path | None) -> str | None:
    if data_dir is None:
        return None
    try:
        raw = json.loads((Path(data_dir) / "rules" / "swing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    gid = raw.get("gate") if isinstance(raw, dict) else None
    return gid if isinstance(gid, str) and gid else None


def active(data_dir: Path | None = None) -> DirectionGate:
    """当前生效的方向门。配置的门未注册时回退, 不报错 (插件缺失不影响机会板运行)。"""
    with _LOCK:
        gid = _configured_id(data_dir)
        if gid and gid in _REGISTRY:
            return _REGISTRY[gid]
        for g in _REGISTRY.values():
            if g.preferred:
                return g
        return _REGISTRY[DEFAULT_GATE_ID]


# ── 内置: 日线 50 日均线门 ─────────────────────────────────────────────

def _ma_side(closes: np.ndarray, window: int = MA_WINDOW) -> str | None:
    c = np.asarray(closes, dtype=float)
    c = c[np.isfinite(c)]
    if c.size < window:
        return None
    ma = float(c[-window:].mean())
    last = float(c[-1])
    if last > ma:
        return "long"
    if last < ma:
        return "short"
    return None


def _close_series(frame) -> np.ndarray | None:
    """扫描帧 (polars DataFrame, 含 date/close) → 截至信号日的收盘序列。"""
    try:
        if frame is None or frame.is_empty() or "close" not in frame.columns:
            return None
        if "date" in frame.columns:
            frame = frame.sort("date")
        return frame["close"].to_numpy()
    except Exception:
        return None


def _daily_ma50_states(ctx: GateContext) -> dict[str, str]:
    out: dict[str, str] = {}
    missing: list[str] = []
    for sym in ctx.symbols:
        frame = (ctx.frames or {}).get(sym)
        closes = _close_series(frame)
        if closes is None:
            missing.append(sym)
            continue
        if frame is not None and "date" in frame.columns:
            try:
                import polars as pl
                closes = frame.filter(pl.col("date") <= ctx.as_of).sort("date")["close"].to_numpy()
            except Exception:
                pass
        side = _ma_side(closes)
        if side:
            out[sym] = side
    if missing and ctx.repo is not None:
        out.update(_states_from_repo(ctx.repo, missing, ctx.as_of))
    return out


def _states_from_repo(repo, symbols: list[str], as_of: date) -> dict[str, str]:
    """扫描帧里没有的自选 (不在流动性池内) 从日K库补算; 读失败 = 不放行。"""
    from datetime import timedelta
    try:
        batch = repo.get_daily_batch(sorted(symbols), as_of - timedelta(days=MA_WINDOW * 2 + 30), as_of,
                                     columns=["symbol", "date", "close"])
    except Exception as e:
        logger.warning("direction gate: daily bars unavailable: %s", type(e).__name__)
        return {}
    if batch is None or batch.is_empty():
        return {}
    out = {}
    for g in batch.partition_by("symbol"):
        side = _ma_side(g.sort("date")["close"].to_numpy())
        if side:
            out[str(g["symbol"][0])] = side
    return out


def _daily_ma50_matrix(data_dir: Path, repo, symbols: list[str], days: list[date]) -> np.ndarray:
    """研究回放用 (T, N): 前一交易日收盘 > MA50 才放行做多 (信号日开盘前可知, 无前视)。"""
    from datetime import timedelta
    n_days, n_syms = len(days), len(symbols)
    gate = np.zeros((n_days, n_syms), bool)
    if not days or not symbols:
        return gate
    batch = repo.get_daily_batch(sorted(symbols), days[0] - timedelta(days=MA_WINDOW * 2 + 30), days[-1],
                                 columns=["symbol", "date", "close"])
    if batch is None or batch.is_empty():
        return gate
    col = {s: j for j, s in enumerate(symbols)}
    day_idx = np.array([np.datetime64(d) for d in days])
    for g in batch.partition_by("symbol"):
        j = col.get(str(g["symbol"][0]))
        if j is None:
            continue
        g = g.sort("date")
        c = g["close"].to_numpy().astype(float)
        dts = np.array([np.datetime64(d) for d in g["date"].to_list()])
        if c.size < MA_WINDOW:
            continue
        ma = np.convolve(c, np.ones(MA_WINDOW) / MA_WINDOW, mode="valid")
        above = np.zeros(c.size, bool)
        above[MA_WINDOW - 1:] = c[MA_WINDOW - 1:] > ma
        # 每个信号日用「严格早于该日」的最后一个收盘判断
        pos = np.searchsorted(dts, day_idx, side="left") - 1
        ok = pos >= MA_WINDOW - 1
        gate[ok, j] = above[pos[ok]]
    return gate


register(DirectionGate(id=DEFAULT_GATE_ID, name="日线趋势门 (收盘相对 50 日均线)",
                       states=_daily_ma50_states, matrix=_daily_ma50_matrix))


PLUGINS = ("app.services.phantom_4h_gate",)


def load_plugins() -> None:
    """导入可选的门插件。插件文件不存在 (例如开源核心版) 时跳过; 存在但导入失败只记日志, 回落到内置门。"""
    from app import optional
    for mod in PLUGINS:
        try:
            optional.load(mod)
        except Exception as e:
            logger.warning("direction gate plugin %s failed to import: %s", mod, type(e).__name__)


load_plugins()
