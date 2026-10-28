"""
@pyne
"""
# UT Bot Alerts (QuantNomad v1.0) — 逐行转写 (h=false → src=close)
from pynecore import Series
from pynecore.lib import script, ta, close, nz, plot, math, input


@script.indicator("UT Bot ref")
def main(a=input.float(1.0, "Key"), c=input.int(10, "ATR Period")):
    xATR = ta.atr(c)
    nLoss = a * xATR
    src: Series[float] = close
    stop: Series[float] = 0.0
    p = nz(stop[1], 0)
    if src > p and src[1] > p:
        stop = math.max(nz(stop[1]), src - nLoss)
    elif src < p and src[1] < p:
        stop = math.min(nz(stop[1]), src + nLoss)
    elif src > p:
        stop = src - nLoss
    else:
        stop = src + nLoss
    e = ta.ema(src, 1)
    above = ta.crossover(e, stop)
    below = ta.crossover(stop, e)
    plot(1 if (src > stop and above) else 0, "buy")
    plot(1 if (src < stop and below) else 0, "sell")
