"""排序因子出值处的浮点残差: 条件触发型公式 (if_else(条件, 值, 0) 外套 ts_mean / ts_sum) 在全零窗口里
本该得 0, 滚动累加却留下 1e-17 级的残差。残差会把本该并列的名次打乱, 于是:
  - R008 (与复权基准无关) 的名次检验把数学上与复权无关的公式判成"名次改变"而拒收;
  - 机会板每日前 5 在并列时不按代码顺序, 而按残差的正负与大小挑。
修法: factor_frame 出值与 R008 检验共用同一处清理 (12 位有效数字, 绝对值低于 1e-12 记 0)。"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from app.services import rank_lab as rl

# 数学上与复权基准无关 (只有比值 / 与自身历史比较), 探针面板上缩放前后数值最大只差 1e-12 级
CONDITIONAL = [
    "ts_sum(if_else(open<ts_delay(low,1),(close-open)/max(ts_delay(low,1)-open,0.0001),0),10)/10",
    "ts_mean(if_else((open<prev_close) and (close>prev_close),(close-open)/max(high-low,0.0001),0),10)",
    "ts_sum(if_else((open<prev_close) and (close>prev_close),volume/max(ts_mean(volume,20),0.0001),0),10)",
    "ts_mean(if_else((close<open) and (close>ts_mean(close,20)),volume,0),5)/max(ts_mean(volume,20),0.0001)",
    "ts_sum(if_else(close<ts_mean(close,10),volume,0),10)/max(ts_sum(volume,10),0.0001)",
]


@pytest.mark.parametrize("formula", CONDITIONAL)
def test_factor_frame_has_no_residual_between_zero_and_real_values(formula):
    """出值里不能有"既不是 0、又小到不可能是真值"的数: 它们只会是累加残差, 并且会参与排名。"""
    p0, _p1 = rl._pit_panels()
    v = rl.factor_frame(p0, formula, "high")["value"].drop_nulls().to_numpy()
    tiny = v[(np.abs(v) > 0) & (np.abs(v) < 1e-12)]
    assert tiny.size == 0, f"{tiny.size} 个残差值, 例如 {tiny[:3]}"
    assert (v == 0).any()                                   # 全零窗口确实存在, 且是精确的 0


@pytest.mark.parametrize("formula", CONDITIONAL)
def test_check_formula_accepts_ratio_formulas_with_tied_zero_windows(formula):
    rl._PIT_CACHE.clear()
    rl.check_formula(formula)                               # 不抛 R008


@pytest.mark.parametrize("formula", [
    "close", "close*volume/max(amount,0.0001)", "ts_mean(raw_close*volume/max(amount,0.0001)-1,5)", "high-low",
    "rank(volume)", "close/raw_close",
    # 预热期 max(.., 0.0001) 兜底把 1e15 级的数带进外层滚动累加, 残差大到与真值同量级: 值随成交量复权而变, 照拒
    "ts_mean(((min(open,close)-low)/max(ts_mean(high-low,14),0.0001))*(volume/max(ts_mean(volume,20),0.0001)),5)",
])
def test_real_adjustment_dependence_is_still_rejected(formula):
    rl._PIT_CACHE.clear()
    with pytest.raises(rl.RankFormulaError) as e:
        rl.check_formula(formula)
    assert e.value.code == "R008"


def test_clean_keeps_real_values_and_direction():
    df = pl.DataFrame({"x": [0.0, 2.2e-17, -4.0e-13, 1.0e-9, -0.25, 3.141592653589793, 1.0e12 + 0.3, None]})
    got = df.select(rl.clean_values(pl.col("x")).alias("x"))["x"].to_list()
    assert got[0] == 0.0 and got[1] == 0.0 and got[2] == 0.0
    assert got[3] == 1.0e-9 and got[4] == -0.25
    assert got[5] == pytest.approx(3.141592653589793, rel=1e-11)
    assert got[6] == pytest.approx(1.0e12, rel=1e-11)
    assert got[7] is None
    p0, _p1 = rl._pit_panels()
    f = CONDITIONAL[1]
    hi = rl.factor_frame(p0, f, "high")["value"].to_numpy()
    lo = rl.factor_frame(p0, f, "low")["value"].to_numpy()
    ok = np.isfinite(hi)
    assert np.array_equal(hi[ok], -lo[ok])                  # low = high 取负, 清理不破坏方向对称
