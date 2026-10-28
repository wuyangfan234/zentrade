"""
@pyne
"""
# Phantom Flow Core SECTION 4 (Phantom Trail) — 逐行转写
from pynecore import Series, Persistent
from pynecore.lib import script, ta, close, high, low, nz, plot, math, na, input


@script.indicator("Phantom Trail ref")
def main(trlLen=input.int(22, "len"), trlMult=input.float(3.0, "mult")):
    trlAtr = ta.atr(14)
    trlLongRaw = ta.highest(high, trlLen) - trlMult * trlAtr
    trlShortRaw = ta.lowest(low, trlLen) + trlMult * trlAtr
    trlLongStop: Series[float] = na(float)
    trlShortStop: Series[float] = na(float)
    trlLongStop = math.max(trlLongRaw, nz(trlLongStop[1], trlLongRaw)) if close[1] > nz(trlLongStop[1], trlLongRaw) else trlLongRaw
    trlShortStop = math.min(trlShortRaw, nz(trlShortStop[1], trlShortRaw)) if close[1] < nz(trlShortStop[1], trlShortRaw) else trlShortRaw
    trlDir: Persistent[int] = 1
    trlDir = -1 if (trlDir == 1 and close < trlLongStop) else (1 if (trlDir == -1 and close > trlShortStop) else trlDir)
    d: Series[int] = trlDir
    plot(1 if (d == 1 and d[1] == -1) else 0, "buy")
    plot(1 if (d == -1 and d[1] == 1) else 0, "sell")
