"""
@pyne
"""
# WaveTrend [LazyBear] vX by DGT — f_getWT + Strong cross 判定逐行转写
from pynecore.lib import script, ta, hlc3, plot, math, input


@script.indicator("WT ref")
def main(n1=input.int(10, "Channel"), n2=input.int(21, "Average"), s1=input.int(4, "Signal")):
    s = hlc3
    esa = ta.ema(s, n1)
    d = ta.ema(math.abs(s - esa), n1)
    ci = (s - esa) / (0.015 * d)
    wtOsc = ta.ema(ci, n2)
    wtSig = ta.sma(wtOsc, s1)
    lowerThreshold = math.avg(-60, -53)
    upperThreshold = math.avg(60, 53)
    up = ta.crossover(wtOsc, wtSig)
    dn = ta.crossunder(wtOsc, wtSig)
    plot(1 if (up and wtOsc < lowerThreshold) else 0, "buy")
    plot(1 if (dn and wtOsc > upperThreshold) else 0, "sell")
