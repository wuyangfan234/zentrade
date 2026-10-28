"""BH-02: 空头「开盘低于入场区、盘中反弹到下沿才卖空成交」时, 成交日的最低价可能发生在成交之前,
不能算作成交后的浮盈/目标 —— 与多头 gap_down_fill (成交日最高 = max(入场, 收盘)) 严格镜像:
成交日最低 = min(入场, 收盘)。_simulate_short (空头证据) 与 replay_state_short (看空卡/卡结算) 两处一致。
用例取自 bug 排查复现脚本 repro/short_vs_long.py 的用例 1 / 用例 4。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.services import card_replay as wc
from app.services import strategy_evaluation as se


def arr(*x):
    return np.array(x, dtype=float)


# 用例 1: 开 98 (< 下沿 99, 未超 2%), 先跌到 91 (成交前), 再反弹到 99 卖空成交, 收 98.5
CASE1 = dict(arrs=(arr(98, 98.6), arr(99.2, 99.0), arr(91.0, 98.0), arr(98.5, 98.4)),
             kw=dict(entry_zone=(99.0, 100.0), invalidation=104.0, target=(90.0, 91.8), risk=4.5))
# 用例 4: 小 R; 开 98.2, 先探 97.3 (成交前), 再拉到 99 卖空成交, 收 99.2
CASE4 = dict(arrs=(arr(98.2, 99.5), arr(99.3, 99.8), arr(97.3, 98.9), arr(99.2, 99.6)),
             kw=dict(entry_zone=(99.0, 100.0), invalidation=100.2, target=(96.0, 97.0), risk=0.7))


@pytest.mark.parametrize("case", [CASE1, CASE4], ids=["case1_pre_fill_low_not_target", "case4_pre_fill_low_not_trailing"])
def test_simulate_short_ignores_low_before_gap_up_fill(case):
    res = se.simulate_7d(*case["arrs"], side="short", **case["kw"])
    # 修前: 用例 1 记成当日止盈 (target_zone_lower), 用例 4 记成 trailing_stop
    assert res["status"] == "open_at_end"
    assert res["entry_price"] == pytest.approx(99.0 * (1 - se.SLIP))


@pytest.mark.parametrize("case", [CASE1, CASE4], ids=["case1_pre_fill_low_not_target", "case4_pre_fill_low_not_trailing"])
def test_replay_state_short_ignores_low_before_gap_up_fill(case):
    st = wc.replay_state_short(*case["arrs"], **case["kw"])
    assert st["status"] == "triggered"
    assert st["stop"] == case["kw"]["invalidation"]   # 止损没被成交前的低点提前下移
    assert st["exit_next_open"] is None


def test_gap_up_fill_still_counts_close_below_entry():
    """成交日收盘低于入场: 收盘本身发生在成交之后, 照常计入 (与多头 max(入场, 收盘) 镜像)。"""
    kw = dict(entry_zone=(99.0, 100.0), invalidation=104.0, target=(90.0, 91.8), risk=4.5)
    o, h, lo, c = arr(98.0), arr(99.2), arr(85.0), arr(91.0)   # 收盘 91 已低于目标近端
    assert se.simulate_7d(o, h, lo, c, side="short", **kw)["exit_reason"] == "target_zone_lower"
    st = wc.replay_state_short(o, h, lo, c, **kw)   # 卡回放是两段 (BH-03): 近端只回补一半
    assert st["status"] == "triggered" and st["half"] is True
