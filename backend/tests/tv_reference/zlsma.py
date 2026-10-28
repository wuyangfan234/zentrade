"""
@pyne
"""
# ZLSMA (veryfid) — 逐行转写 + 作者说明的穿越信号
from pynecore.lib import script, ta, close, plot, input


@script.indicator("ZLSMA ref")
def main(length=input.int(32, "Length"), offset=input.int(0, "Offset")):
    lsma = ta.linreg(close, length, offset)
    lsma2 = ta.linreg(lsma, length, offset)
    eq = lsma - lsma2
    zlsma = lsma + eq
    plot(1 if ta.crossover(close, zlsma) else 0, "buy")
    plot(1 if ta.crossunder(close, zlsma) else 0, "sell")
