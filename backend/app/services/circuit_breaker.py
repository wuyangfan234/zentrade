"""组合熔断 (无限 2026-10-01 批准参数, 10-02 批准恢复条件; 10-03 无限要求放宽当日阈值 2% → 3%)。

- 当日熔断: 盘中净值较上一交易日定版净值下跌 ≥3% → 当天停止新开仓; 下一交易日自动恢复。
  阈值依据 (近 2 年日K回算, 现持仓权重): 2% 时按收盘口径约 6.7% 的交易日触发、盘中最低口径约 28%,
  对 6 仓×1% 风险的波段账户过敏; 3% = 组合风险上限 6% 的一半, 收盘口径约 2.7%、盘中最低口径约 11%。
- 回撤减半: 净值较历史最高定版净值回撤 ≥10% → 单笔风险减半; 回撤收窄到 5% 以内才恢复 (滞回, 防抖)。
- 只限制新开仓, 绝不强平已有持仓; 缺少基准 (无上一日净值/峰值) 时不触发。
- 每次状态切换写入 data/circuit_breaker/events.jsonl, 当前状态写 state.json, 可审计、可重放。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path

DAY_HALT_PCT = 0.03
HALF_RISK_DD_PCT = 0.10
HALF_RISK_RECOVER_PCT = 0.05

_lock = threading.Lock()


def evaluate(*, day: date, nav_now: float | None, prev_close_nav: float | None, peak_nav: float | None,
             state: dict) -> dict:
    """纯函数: 由当前净值与基准算出熔断状态。state 是上一次的持久状态。"""
    state = dict(state or {})
    day_s = day.isoformat()
    day_halt = bool(state.get("day_halt_date") == day_s)
    day_change = None
    if nav_now and prev_close_nav and prev_close_nav > 0:
        day_change = nav_now / prev_close_nav - 1
        if day_change <= -DAY_HALT_PCT:
            day_halt = True
    if day_halt:
        state["day_halt_date"] = day_s
    drawdown = None
    half = bool(state.get("half_risk"))
    if nav_now and peak_nav and peak_nav > 0:
        drawdown = nav_now / peak_nav - 1
        if drawdown <= -HALF_RISK_DD_PCT:
            half = True
        elif half and drawdown > -HALF_RISK_RECOVER_PCT:
            half = False
    state["half_risk"] = half
    return {
        "day": day_s,
        "day_halt": day_halt,
        "half_risk": half,
        "block_new_entries": day_halt,
        "risk_multiplier": 0.5 if half else 1.0,
        "day_change_pct": round(day_change * 100, 3) if day_change is not None else None,
        "drawdown_pct": round(drawdown * 100, 3) if drawdown is not None else None,
        "rules": {"day_halt": f"当日净值较上一交易日定版下跌≥{DAY_HALT_PCT:.0%}：当天停新开仓，下一交易日恢复",
                  "half_risk": "较最高净值回撤≥10%：单笔风险减半；回撤收窄到5%以内恢复",
                  "scope": "只限制新开仓，不强平持仓"},
        "state": state,
    }


class CircuitBreaker:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "circuit_breaker"
        self.state_path = self.dir / "state.json"
        self.log_path = self.dir / "events.jsonl"

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def current(self, day: date) -> dict:
        st = self._load()
        return evaluate(day=day, nav_now=None, prev_close_nav=None, peak_nav=None, state=st.get("state", {}))

    def update(self, *, day: date, nav_now: float | None, prev_close_nav: float | None,
               peak_nav: float | None) -> dict:
        with _lock:
            prev = self._load()
            res = evaluate(day=day, nav_now=nav_now, prev_close_nav=prev_close_nav, peak_nav=peak_nav,
                           state=prev.get("state", {}))
            transitions = []
            prev_day_halt = prev.get("state", {}).get("day_halt_date") == res["day"]
            if res["day_halt"] and not prev_day_halt:
                transitions.append("day_halt_on")
            if res["half_risk"] != bool(prev.get("state", {}).get("half_risk")):
                transitions.append("half_risk_on" if res["half_risk"] else "half_risk_off")
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"state": res["state"], "last": {k: res[k] for k in
                                       ("day", "day_change_pct", "drawdown_pct", "block_new_entries", "risk_multiplier")}},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
            for t in transitions:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"event": t, "day": res["day"], "nav": nav_now, "prev_close_nav": prev_close_nav,
                                        "peak_nav": peak_nav, "day_change_pct": res["day_change_pct"],
                                        "drawdown_pct": res["drawdown_pct"]}, ensure_ascii=False) + "\n")
            res["transitions"] = transitions
            return res
