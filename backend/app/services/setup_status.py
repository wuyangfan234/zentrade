"""Setup 状态: 四周负期望自动降级为只观察 (停新开仓), 仅手动恢复。

用户拍板口径 (2026-10-01): 以评估日为终点、往前 28 个自然日 (按回合 exit_date, 美东日期) 内的
闭合回合, 有效 R (首填冻结风险可算) ≥ 10 笔且平均 R < 0 → 该 setup 降级。
R 为 None 的回合不计样本也不计平均; 部分平仓不是闭合回合 (由 review.build_round_trips 保证)。

状态文件 data_dir/review/setup_status.json (原子写), 变更审计 setup_status_audit.jsonl (append-only)。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, timedelta
from pathlib import Path

WINDOW_DAYS = 28
MIN_SAMPLES = 10
SETUPS = ("breakout", "pullback")
_LOCK = threading.Lock()


def evaluate(trips, setup: str, as_of: date) -> dict:
    start = as_of - timedelta(days=WINDOW_DAYS - 1)
    # 回补成交 (backfill) 是事后重放补记, 不是实时决策的结果, 不进降级证据 (无限 2026-10-12)
    rs = [t.r_multiple for t in trips
          if t.setup_type == setup and start <= t.exit_date <= as_of and t.r_multiple is not None
          and not getattr(t, "backfill", False)]
    avg = sum(rs) / len(rs) if rs else None
    return {"setup": setup, "samples": len(rs), "avg_r": round(avg, 4) if avg is not None else None,
            "window_start": start.isoformat(), "window_end": as_of.isoformat(),
            "degrade": len(rs) >= MIN_SAMPLES and avg is not None and avg < 0}


class SetupStatusStore:
    def __init__(self, data_dir: Path | str):
        self.dir = Path(data_dir) / "review"
        self.path = self.dir / "setup_status.json"
        self.audit_path = self.dir / "setup_status_audit.jsonl"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def is_degraded(self, setup: str) -> bool:
        if setup in EXEMPT:
            return False
        return self.load().get(setup, {}).get("status") == "degraded"

    def audit(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _write(self, state: dict, entry: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def degrade(self, setup: str, evidence: dict, *, at: str) -> None:
        with _LOCK:
            state = self.load()
            state[setup] = {"status": "degraded", "degraded_at": at, "evidence": evidence}
            self._write(state, {"action": "degrade", "setup": setup, "at": at, "evidence": evidence})

    def restore(self, setup: str, *, note: str = "", at: str) -> None:
        with _LOCK:
            state = self.load()
            previous = state.get(setup, {})
            state[setup] = {"status": "active", "restored_at": at, "note": note,
                            "previous": {k: previous.get(k) for k in ("degraded_at", "evidence")}}
            self._write(state, {"action": "restore", "setup": setup, "at": at, "note": note})

    def known(self) -> list[str]:
        """内置 S1/S2 + 状态文件里出现过的 setup (含 strategy:<规则>)。"""
        return list(dict.fromkeys([*SETUPS, *self.load()]))

    def summary(self) -> list[dict]:
        state = self.load()
        return [{"setup": s, **({"status": "active"} | state.get(s, {}))} for s in self.known()]


def setups_in(trips) -> list[str]:
    """评估对象 = 内置 S1/S2 + 回合里实际出现的 setup_type。

    审计 #1 (2026-10-12): 现行下单簿的回合记作 strategy:<规则>, 旧实现只循环 breakout/pullback,
    负期望规则永远不会被降级。
    0.15.0: 自进化卡 (strategy:evo) 不进周降级 —— 它每天按已离场历史重算权重, 纠错在权重里; “四周负 → 降级 → 只能手动恢复”
    是一个要人等的死锁 (无限 10-13: 系统不允许建任何要等的东西)。2023-26 回放里这条规则会把 evo 停掉 189 周中的 71 周。"""
    seen = sorted({t.setup_type for t in trips if getattr(t, "setup_type", None)} - set(EXEMPT))
    return list(dict.fromkeys([*SETUPS, *seen]))


EXEMPT = ("strategy:evo", "strategy:swing")   # tight-v1 机会板卡: 不进周降级 (降级只能手动恢复, 会让机会板整周停摆)


def run_weekly_evaluation(store: SetupStatusStore, trips, as_of: date, *, at: str | None = None) -> list[str]:
    """只降级不恢复; 已降级的 setup 保持原状。返回本次新降级的 setup。"""
    changed = []
    trips = list(trips)
    for setup in setups_in(trips):
        if store.is_degraded(setup):
            continue
        decision = evaluate(trips, setup, as_of)
        if decision["degrade"]:
            store.degrade(setup, {k: decision[k] for k in ("samples", "avg_r", "window_start", "window_end")},
                          at=at or as_of.isoformat())
            changed.append(setup)
    return changed
