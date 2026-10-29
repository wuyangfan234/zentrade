"""Phantom 4h 方向门插件 (私有完整版专用; 开源核心版不含本文件)。

把原有机会板 4h 门原样包装成 direction_gate 插件, 行为不变:
- 扫描期 (states): 直接复用 decision_scan_job 里 Phantom 4h 来源扫描已经算好的自选方向状态
  (meta["phantom_4h_state"]), 不重复计算;
- 研究回放 (matrix): 30 分钟K → 4h Phantom, 可见窗 [前日 13:30, 当日 09:30] 内“多头条件齐备”或最后方向 = 多。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np

from app.opportunity import direction_gate

GATE_ID = "phantom_4h"


def _states(ctx: direction_gate.GateContext) -> dict[str, str]:
    pre = (ctx.precomputed or {}).get("phantom_4h_state") or {}
    return {s: v for s, v in pre.items() if v in ("long", "short")}


def _matrix(data_dir: Path, repo, symbols: list[str], days: list[date]) -> np.ndarray:
    import pandas as pd

    from app.services import kline_4h, phantom_4h
    from app.services.phantom_4h_source import DIRECTION_SIDE
    n_days, n_syms = len(days), len(symbols)
    gate = np.zeros((n_days, n_syms), bool)
    bars = kline_4h.bars_4h(data_dir, repo, symbols, days[0] - timedelta(days=400), days[-1] + timedelta(days=1))
    for j, sym in enumerate(symbols):
        df = bars.get(sym)
        if df is None or len(df) < phantom_4h.MIN_BARS:
            continue
        s = phantom_4h.scan_series(df)
        idx = s.index.values
        act = s["action"].values
        dr = s["direction"].values
        for t in range(1, n_days):
            lo = np.datetime64(pd.Timestamp(days[t - 1]) + pd.Timedelta(hours=13, minutes=30))
            hi = np.datetime64(pd.Timestamp(days[t]) + pd.Timedelta(hours=9, minutes=30))
            a = np.searchsorted(idx, lo, "left")
            b = np.searchsorted(idx, hi, "right")
            if b == 0:
                continue
            gate[t, j] = bool((act[a:b] == "多头条件齐备").any()) or DIRECTION_SIDE.get(str(dr[b - 1])) == "long"
    return gate


direction_gate.register(direction_gate.DirectionGate(
    id=GATE_ID, name="4h Phantom 方向门", states=_states, matrix=_matrix, preferred=True))
