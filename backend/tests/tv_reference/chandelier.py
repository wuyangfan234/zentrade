"""
@pyne
"""
# Chandelier Exit (everget v8) — 逐行转写 (useClose=true)
from pynecore import Series, Persistent
from pynecore.lib import script, ta, close, nz, plot, math, input


@script.indicator("CE ref")
def main(length=input.int(22, "ATR Period"), mult=input.float(3.0, "ATR Multiplier")):
    atr = mult * ta.atr(length)
    longStop: Series[float] = ta.highest(close, length) - atr
    longStopPrev = nz(longStop[1], longStop)
    longStop = math.max(longStop, longStopPrev) if close[1] > longStopPrev else longStop
    shortStop: Series[float] = ta.lowest(close, length) + atr
    shortStopPrev = nz(shortStop[1], shortStop)
    shortStop = math.min(shortStop, shortStopPrev) if close[1] < shortStopPrev else shortStop
    d: Persistent[int] = 1
    d = 1 if close > shortStopPrev else (-1 if close < longStopPrev else d)
    dd: Series[int] = d
    plot(1 if (dd == 1 and dd[1] == -1) else 0, "buy")
    plot(1 if (dd == -1 and dd[1] == 1) else 0, "sell")
