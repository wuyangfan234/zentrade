"""
@pyne
"""
# Squeeze Momentum [LazyBear] v2 (pastebin UCpcX8d7) — 逐行转写; 买卖点 = 作者说明的 Carter 用法
from pynecore import Series, Persistent
from pynecore.lib import script, ta, close, high, low, nz, plot, math, input


@script.indicator("SQZMOM ref")
def main(length=input.int(20, "BB Length"), mult=input.float(2.0, "BB MultFactor"),
         lengthKC=input.int(20, "KC Length"), multKC=input.float(1.5, "KC MultFactor")):
    source = close
    basis = ta.sma(source, length)
    dev = mult * ta.stdev(source, length)
    upperBB = basis + dev
    lowerBB = basis - dev
    ma = ta.sma(source, lengthKC)
    rng = ta.tr
    rangema = ta.sma(rng, lengthKC)
    upperKC = ma + rangema * multKC
    lowerKC = ma - rangema * multKC
    sqzOn = (lowerBB > lowerKC) and (upperBB < upperKC)
    sqzOff = (lowerBB < lowerKC) and (upperBB > upperKC)
    val: Series[float] = ta.linreg(source - math.avg(math.avg(ta.highest(high, lengthKC), ta.lowest(low, lengthKC)),
                                                     ta.sma(close, lengthKC)), lengthKC, 0)
    armed: Persistent[bool] = False
    buy = 0
    if sqzOn:
        armed = True
    if sqzOff and armed:
        if val > 0:
            buy = 1
        armed = False
    plot(buy, "buy")
    plot(1 if val < nz(val[1]) else 0, "sell")
