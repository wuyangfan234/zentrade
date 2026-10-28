"""
@pyne
"""
# HalfTrend (everget v3) — 逐行转写
from pynecore import Series, Persistent
from pynecore.lib import script, ta, close, high, low, nz, plot, math, na, input


@script.indicator("HalfTrend ref")
def main(amplitude=input.int(2, "Amplitude"), channelDeviation=input.int(2, "Channel Deviation")):
    trend: Persistent[int] = 0
    nextTrend: Persistent[int] = 0
    maxLowPrice: Persistent[float] = nz(low[1], low)
    minHighPrice: Persistent[float] = nz(high[1], high)
    up: Persistent[float] = 0.0
    down: Persistent[float] = 0.0
    arrowUp = na(float)
    arrowDown = na(float)
    atr2 = ta.atr(100) / 2
    highPrice = high[math.abs(ta.highestbars(amplitude))]
    lowPrice = low[math.abs(ta.lowestbars(amplitude))]
    highma = ta.sma(high, amplitude)
    lowma = ta.sma(low, amplitude)
    if nextTrend == 1:
        maxLowPrice = math.max(lowPrice, maxLowPrice)
        if highma < maxLowPrice and close < nz(low[1], low):
            trend = 1
            nextTrend = 0
            minHighPrice = highPrice
    else:
        minHighPrice = math.min(highPrice, minHighPrice)
        if lowma > minHighPrice and close > nz(high[1], high):
            trend = 0
            nextTrend = 1
            maxLowPrice = lowPrice
    trendS: Series[int] = trend
    upS: Series[float] = up
    downS: Series[float] = down
    if trend == 0:
        if not na(trendS[1]) and trendS[1] != 0:
            up = down if na(downS[1]) else downS[1]
            arrowUp = up - atr2
        else:
            up = maxLowPrice if na(upS[1]) else math.max(maxLowPrice, upS[1])
    else:
        if not na(trendS[1]) and trendS[1] != 1:
            down = up if na(upS[1]) else upS[1]
            arrowDown = down + atr2
        else:
            down = minHighPrice if na(downS[1]) else math.min(minHighPrice, downS[1])
    upS = up
    downS = down
    plot(1 if (not na(arrowUp) and trend == 0 and trendS[1] == 1) else 0, "buy")
    plot(1 if (not na(arrowDown) and trend == 1 and trendS[1] == 0) else 0, "sell")
