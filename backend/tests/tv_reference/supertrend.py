"""
@pyne
"""
# Supertrend (KivancOzbilgic v3.0) / Phantom Flow SECTION 1 — 逐行转写
from pynecore import Series
from pynecore.lib import script, ta, close, hl2, nz, plot, math, input


@script.indicator("Supertrend ref")
def main(Periods=input.int(10, "ATR Period"), Multiplier=input.float(3.0, "ATR Multiplier")):
    src = hl2
    atr = ta.atr(Periods)
    up: Series[float] = src - (Multiplier * atr)
    up1 = nz(up[1], up)
    up = math.max(up, up1) if close[1] > up1 else up
    dn: Series[float] = src + (Multiplier * atr)
    dn1 = nz(dn[1], dn)
    dn = math.min(dn, dn1) if close[1] < dn1 else dn
    trend: Series[int] = 1
    trend = nz(trend[1], trend)
    trend = 1 if (trend == -1 and close > dn1) else (-1 if (trend == 1 and close < up1) else trend)
    plot(1 if (trend == 1 and trend[1] == -1) else 0, "buy")
    plot(1 if (trend == -1 and trend[1] == 1) else 0, "sell")
