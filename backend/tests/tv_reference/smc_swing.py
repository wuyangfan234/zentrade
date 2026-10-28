"""
@pyne
"""
# Smart Money Concepts [LuxAlgo] — leg / getCurrentStructure / displayStructure 的波段 (swing) 部分逐行转写
from pynecore import Series, Persistent
from pynecore.lib import script, ta, close, high, low, plot, na, input

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


@script.indicator("SMC swing ref")
def main(size=input.int(50, "Swing length"), chochOnly=input.bool(True, "CHoCH only")):
    currentLeg = leg(size)
    newPivot = ta.change(currentLeg) != 0
    pivotLow = ta.change(currentLeg) == 1
    swHigh: Persistent[float] = na(float)
    swLow: Persistent[float] = na(float)
    hiCrossed: Persistent[bool] = False
    loCrossed: Persistent[bool] = False
    bias: Persistent[int] = 0
    if newPivot:
        if pivotLow:
            swLow = low[size]
            loCrossed = False
        else:
            swHigh = high[size]
            hiCrossed = False
    buy = 0
    sell = 0
    if ta.crossover(close, swHigh) and not hiCrossed:
        if bias == BEARISH or not chochOnly:
            buy = 1
        hiCrossed = True
        bias = BULLISH
    if ta.crossunder(close, swLow) and not loCrossed:
        sell = 1
        loCrossed = True
        bias = BEARISH
    plot(buy, "buy")
    plot(sell, "sell")
