"""BH-03: 空头卡结算与交易员规则一致 —— 最低触及目标近端 (target[1]) 先回补一半, 止损下移到保本;
余仓到远端 (target[0]) 回补, 或按止损/第 7 日离场。与多头 replay_state 两段逐条镜像
(trader.py manage_intraday: 空头 far_hit = price <= target[0] 全平, near_hit = price <= target[1] 减半)。
与 replay_state_short 的随机路径锁定见 test_card_replay.py::test_short_replay_state_matches_short_settlement。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.services import card_replay as wc
from app.services import card_settlement as cs
from app.services import strategy_evaluation as se

KW = dict(entry_zone=(99.0, 100.0), invalidation=104.0, target=(90.0, 91.8), risk=4.5)


def arr(*x):
    return np.array(x, dtype=float)


def test_near_target_only_covers_half_then_far_closes_rest():
    # 第 0 天开在区内 99.5 成交; 第 1 天最低 91.5 (≤近端 91.8, >远端 90); 第 2 天最低 85 (≤远端 90)
    o, h, lo, c = arr(99.5, 95.0, 92.0), arr(99.8, 95.5, 92.5), arr(99.3, 91.5, 85.0), arr(99.4, 92.0, 86.0)
    st = wc.replay_state_short(o, h, lo, c, **KW)
    entry = 99.5 * (1 - se.SLIP)
    legs = [(0.5, 91.8 * (1 + se.SLIP)), (0.5, 90.0 * (1 + se.SLIP))]
    exp_r = round(sum(w * (entry - p) for w, p in legs) / 4.5 - se.FEE_R(4.5, entry) * 1.5, 4)
    assert st["status"] == "closed" and st["exit_reason"] == "target_zone_upper" and st["days"] == 3
    assert st["r"] == pytest.approx(exp_r, abs=2e-4)


def test_after_half_cover_remainder_is_held_with_breakeven_stop():
    o, h, lo, c = arr(99.5, 95.0), arr(99.8, 95.5), arr(99.3, 91.5), arr(99.4, 92.0)
    st = wc.replay_state_short(o, h, lo, c, **KW)
    assert st["status"] == "triggered" and st["half"] is True
    assert st["stop"] <= round(99.5 * (1 - se.SLIP), 4)       # 止损至少下移到保本


def test_half_then_breakeven_stop_exit_reason():
    # 半仓回补后反弹, 收盘站上保本价 → 次日开盘按 breakeven_after_partial 离场
    o, h, lo, c = arr(99.5, 95.0, 99.0, 100.0), arr(99.8, 95.5, 100.5, 100.5), arr(99.3, 91.5, 98.5, 99.5), arr(99.4, 92.0, 100.0, 100.2)
    st = wc.replay_state_short(o, h, lo, c, **KW)
    assert st["status"] == "closed" and st["exit_reason"] == "breakeven_after_partial"


def test_bench_exit_at_open_short_far_target():
    """基准取价类型: 空头余仓开盘就越过远端 → 按开盘价离场 (与多头 target_zone_upper 镜像)。"""
    assert cs.bench_exit_at_open("target_zone_upper", "short", exit_open=89.0, target=(90.0, 91.8), same_bar_as_fill=False)
    assert not cs.bench_exit_at_open("target_zone_upper", "short", exit_open=90.5, target=(90.0, 91.8), same_bar_as_fill=False)
