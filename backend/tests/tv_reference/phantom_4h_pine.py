"""
@pyne
"""
# Phantom Flow Core 中文优化版 4h 关键件逐行转写 —— Pine 答案:
# SECTION 1 Shift (16-43) / SMC leg+getCurrentStructure+displayStructure (358-605, 761-771) /
# SECTION 3 Oscillator maBase/osc (875-880) / SECTION 4 Trail (935-958) / 仪表盘 L1-L4 (1134-1138)。
# L5 (日线 EMA20>EMA50, request.security) 不在这里: 由调用方用日K单独算并对齐。
from pynecore import Series, Persistent
from pynecore.lib import script, ta, close, high, low, open, hl2, nz, plot, math, na, input

BULLISH_LEG = 1
BEARISH_LEG = 0
BULLISH = 1
BEARISH = -1


def leg(size: int):
    lg: Persistent[int] = 0
    newLegHigh = high[size] > ta.highest(size)
    newLegLow = low[size] < ta.lowest(size)
    if newLegHigh:
        lg = BEARISH_LEG
    elif newLegLow:
        lg = BULLISH_LEG
    return lg


@script.indicator("Phantom 4h pine ref")
def main():
    # ── SECTION 1 Shift ──
    atrShift = ta.atr(14)
    upShift: Series[float] = hl2 - 3.2 * atrShift
    upShiftPrev = nz(upShift[1], upShift)
    upShift = math.max(upShift, upShiftPrev) if close[1] > upShiftPrev else upShift
    dnShift: Series[float] = hl2 + 3.2 * atrShift
    dnShiftPrev = nz(dnShift[1], dnShift)
    dnShift = math.min(dnShift, dnShiftPrev) if close[1] < dnShiftPrev else dnShift
    shiftTrend: Persistent[int] = 1
    shiftTrend = 1 if (shiftTrend == -1 and close > dnShiftPrev) else (-1 if (shiftTrend == 1 and close < upShiftPrev) else shiftTrend)

    # ── SMC: swing (34) 与 internal (5) 两套 leg / pivot ──
    swLeg = leg(34)
    swNew = ta.change(swLeg) != 0
    swPivLow = ta.change(swLeg) == 1
    inLeg = leg(5)
    inNew = ta.change(inLeg) != 0
    inPivLow = ta.change(inLeg) == 1
    swHigh: Persistent[float] = na(float)
    swLow: Persistent[float] = na(float)
    swHiX: Persistent[bool] = False
    swLoX: Persistent[bool] = False
    inHigh: Persistent[float] = na(float)
    inLow: Persistent[float] = na(float)
    inHiX: Persistent[bool] = False
    inLoX: Persistent[bool] = False
    # getCurrentStructure(34) 先于 (5) 调用 (第 761-762 行)
    if swNew:
        if swPivLow:
            swLow = low[34]
            swLoX = False
        else:
            swHigh = high[34]
            swHiX = False
    if inNew:
        if inPivLow:
            inLow = low[5]
            inLoX = False
        else:
            inHigh = high[5]
            inHiX = False
    # displayStructure(true) (internal, 共振过滤默认开) 先于 displayStructure() (swing)
    upperWick = high - math.max(close, open)
    bullishBar = upperWick > math.min(close, open - low)
    bearishBar = upperWick < math.min(close, open - low)
    inBias: Persistent[int] = 0
    swBias: Persistent[int] = 0
    inBull = 0
    inBear = 0
    if ta.crossover(close, inHigh) and not inHiX and (inHigh != swHigh and bullishBar):
        inHiX = True
        inBias = BULLISH
        inBull = 1
    if ta.crossunder(close, inLow) and not inLoX and (inLow != swLow and bearishBar):
        inLoX = True
        inBias = BEARISH
        inBear = 1
    if ta.crossover(close, swHigh) and not swHiX:
        swHiX = True
        swBias = BULLISH
    if ta.crossunder(close, swLow) and not swLoX:
        swLoX = True
        swBias = BEARISH

    # ── SECTION 3 Oscillator ──
    maBase = ta.ema(hl2, 50)
    oscDiff = hl2 - maBase
    oscRange = ta.percentile_linear_interpolation(oscDiff, 1000, 99)
    osc = ta.hma(ta.change(oscDiff / oscRange, 21), 10)

    # ── SECTION 4 Trail ──
    trlAtr = ta.atr(14)
    trlLongRaw = ta.highest(high, 22) - 3.0 * trlAtr
    trlShortRaw = ta.lowest(low, 22) + 3.0 * trlAtr
    trlLongStop: Series[float] = na(float)
    trlShortStop: Series[float] = na(float)
    trlLongStop = math.max(trlLongRaw, nz(trlLongStop[1], trlLongRaw)) if close[1] > nz(trlLongStop[1], trlLongRaw) else trlLongRaw
    trlShortStop = math.min(trlShortRaw, nz(trlShortStop[1], trlShortRaw)) if close[1] < nz(trlShortStop[1], trlShortRaw) else trlShortRaw
    trlDir: Persistent[int] = 1
    trlDir = -1 if (trlDir == 1 and close < trlLongStop) else (1 if (trlDir == -1 and close > trlShortStop) else trlDir)

    plot(shiftTrend, "shift")
    plot(swBias, "sw_bias")
    plot(inBias, "in_bias")
    plot(inBull, "in_bull")
    plot(inBear, "in_bear")
    plot(1 if osc > 0 else 0, "osc_pos")
    plot(1 if close > maBase else 0, "above_ma")
    plot(trlDir, "trl_dir")
    plot(trlLongStop, "trl_long")
    plot(trlShortStop, "trl_short")
