"""把 Alpaca raw/all 两套日K换算成「除权事件」(adj_factor event basis v2)。

背景 (2026-09-30 审计): adjustment=all 的价格是相对「现在」复权的, 因此
L(d) = raw_close(d) / all_close(d) 是一个「水平值」: 只在除权日跳变, 其余日子不变。
旧实现把每一天的 L(d) 直接当作 ex_factor 落盘, 而 enriched 管道按
「每行 = 一次事件的前/后价格比」做累乘 —— 两套口径错配, 结果是拆股没被复权、
分红复权错位一天。

本模块只做纯计算:
- ex_factor = L(前一交易日) / L(除权日), 即事件前后价格比 (10:1 拆股 = 10);
- vol_factor = V(除权日) / V(前一交易日), 其中 V(d) = all_volume / raw_volume,
  只有拆股/合股会改变成交量口径 (10:1 拆股 = 10, 1:20 合股 = 0.05)。
两个价格都四舍五入到分, 所以相邻日 L 的比值本身带舍入噪声; 只保留超过
「分位舍入上界」的变化, 其余视为噪声丢弃, 不伪造事件。
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date

ADJ_BASIS = "event_v2"
_CENT = 0.005          # 单个价格的最大舍入误差 (四舍五入到分)
_SAFETY = 1.5          # 舍入上界的安全倍数
_MIN_REL = 2e-5        # 极高价股的浮点噪声下限
_SPLIT_DEFAULT = 1.5   # 成交量证据不可靠时: 价格因子 <1 (合股) 或 ≥1.5 (常见拆股) 视为拆合股

Bar = tuple[float, float]  # (close, volume)


def _noise_bound(raw_prev: float, level_prev: float, raw_cur: float, level_cur: float) -> float:
    """相邻两日 L 比值的相对舍入误差上界。all = raw / L, 两者都舍入到分。"""
    bound = _CENT * ((1 + level_prev) / raw_prev + (1 + level_cur) / raw_cur)
    return max(bound * _SAFETY, _MIN_REL)


def events_from_bars(raw: Mapping[date, Bar], adjusted: Mapping[date, Bar]) -> list[dict]:
    """单个标的: {日期: (收盘, 成交量)} 两套 → 事件行 (不含 symbol)。

    只使用两套都有且价格为正的日期; 停牌缺口跨越时用缺口两侧的水平比较,
    事件日记在缺口后第一个有数据的交易日 (= 实际按新口径交易的首日)。
    """
    if raw.keys() != adjusted.keys():
        raise ValueError("adjustment coverage mismatch: raw/all dates differ")
    for day in raw:
        for close, volume in (raw[day], adjusted[day]):
            if not math.isfinite(close) or close <= 0:
                raise ValueError("adjustment price must be finite and positive")
            if not math.isfinite(volume) or volume < 0:
                raise ValueError("adjustment volume must be finite and nonnegative")
    days = sorted(raw)
    events: list[dict] = []
    # (raw_close, L, 最近一次可用的 (V, 调整后成交量)); 零量/缺量日不更新 V
    prev: tuple[float, float, tuple[float, float] | None] | None = None
    for d in days:
        raw_close, raw_vol = raw[d]
        adj_close, adj_vol = adjusted[d]
        level = raw_close / adj_close
        vol_obs = (adj_vol / raw_vol, adj_vol) if raw_vol > 0 and adj_vol > 0 else None
        if prev is not None:
            prev_raw, prev_level, prev_vol = prev
            price_factor = prev_level / level
            # 拆合股必然改变价格口径; 只有成交量水平跳变而价格口径不变, 只能是调整后
            # 成交量取整 (累计合股后历史量被压到个位数) 的噪声, 不是公司行动。
            price_noise = _noise_bound(prev_raw, prev_level, raw_close, level)
            if abs(price_factor - 1) > price_noise:
                events.append({
                    "trade_date": d,
                    "ex_factor": price_factor,
                    "vol_factor": _volume_factor(price_factor, price_noise, prev_vol, vol_obs),
                })
        prev = (raw_close, level, vol_obs if vol_obs is not None else (prev[2] if prev else None))
    return events


def _volume_factor(price_factor: float, price_noise: float, prev: tuple[float, float] | None,
                   cur: tuple[float, float] | None) -> float:
    """价格事件的成交量倍数: 拆合股 = 价格因子, 现金分红 = 1。

    成交量水平只用来判别事件类型, 不直接当因子 (整数取整会让小量样本抖动数个百分点)。
    判别看对数距离: 实测量比更接近 1 → 分红; 更接近价格因子 → 拆合股。
    量太小、取整误差盖过两种假设的差距时, 按价格因子形态兜底。
    """
    separation = abs(math.log(price_factor))
    if prev is not None and cur is not None:
        candidate = prev[0] / cur[0]
        vol_noise = (0.5 / prev[1] + 0.5 / cur[1]) * _SAFETY   # 调整后成交量四舍五入到整股
        if vol_noise < separation / 2:
            if abs(math.log(candidate / price_factor)) >= abs(math.log(candidate)):
                return 1.0   # 量口径未随价格变化 → 现金分红
            # 拆合股: 两个估计取噪声更小者 (大成交量时量比精确, 小成交量时价比精确)
            return candidate if vol_noise < price_noise else price_factor
    return price_factor if price_factor < 1 or price_factor >= _SPLIT_DEFAULT else 1.0


def events_for_symbols(
    raw_bars: Iterable[tuple[str, dict]],
    adjusted_bars: Iterable[tuple[str, dict]],
) -> list[dict]:
    """Alpaca bar 流 (symbol, bar) × 2 → 全部事件行 (含 symbol)。"""
    def collect(stream):
        out: dict[str, dict[date, Bar]] = {}
        for sym, bar in stream:
            day = date.fromisoformat(str(bar["t"])[:10])
            close = bar.get("c")
            if close is None or not math.isfinite(float(close)):
                raise ValueError(f"{sym}: adjustment price must be finite and positive")
            out.setdefault(sym, {})[day] = (float(close), float(bar.get("v") or 0.0))
        return out

    raw = collect(raw_bars)
    adjusted = collect(adjusted_bars)
    if raw.keys() != adjusted.keys():
        diff = sorted(raw.keys() ^ adjusted.keys())[:10]
        raise ValueError(f"adjustment coverage mismatch: raw/all symbols differ: {diff}")
    rows: list[dict] = []
    for sym in sorted(raw):
        try:
            events = events_from_bars(raw[sym], adjusted[sym])
        except ValueError as exc:
            raise ValueError(f"{sym}: {exc}") from exc
        rows.extend({"symbol": sym, **ev} for ev in events)
    return rows
