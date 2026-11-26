"""P1-B1 策略账本: 格子 = (策略, 方向, regime), 四档 在用 / 观察 / 冷冻 / 复活; 收缩超额打分。

直接生效 (无影子): swing_desk.board_for_day 每次出板都读本账本 —
  - 打分 = Σ_命中且参与打分的规则 excess × n / (n + k)   (k 默认 100, data/rules/swing.json 的 "ledger_k" 可改)
    这个分就是机会板每侧排序的主键 (取代旧「命中条数 + 固定 EXCESS_WEIGHT」公式);
  - 冷冻档规则不算命中、不参与打分; 卡上全部参与规则都是观察档 → ledger_tier="observe", 纸面下单簿不收;
  - 回滚开关: swing.json "ledger": false → 不按档位过滤 (收缩打分仍用产物里的 excess/n); 彻底回滚 = git revert。

数据来源只读既有产物 (backend/config/*.json), 不回放、不写影子文件:
  swing_rule_history.json   tight-v1 每条规则 (多头 = id, 空头 = "id:short") 的 n / excess / p (主来源)
  watchlist_rule_history.json 旧宽卡历史, 只在 tight-v1 历史里没有该规则时兜底 (source 字段标明)
  swing_base_rates.json      只取窗口与数据截止日写进审计 (产物版本)
产物只有全窗口汇总 (2023-01 起), 没有逐笔样本、也没有按 regime 拆分:
  - CI 由产物 p 值反推 (产物 p = 超额均值 / 周聚类自助标准差的单侧正态近似, 与 rank_lab.judge_segment 同口径);
    有逐笔 (t, 超额) 时用 ci_from_pairs (直接复用 rank_lab.week_boot, 区间与 swing_candidates.week_boot_ci 逐位相同)。
  - 三个 regime 格子暂用同一份汇总 (regime_split=False 写进审计); 产物将来带 regime 拆分时按格子读。
  - 「滚动 252 交易日」受产物窗口限制, 现为产物全窗口; 窗口写进审计。

月度复核: 每个自然月第一次出板时落一条只追加审计 data/rules/ledger_audit.jsonl (不进结算台账)。
同月内档位只认这条记录 (同一天同一批候选结果可复现); freeze_streak = 连续几次月度复核点估计 < 0。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CONTRACT = "strategy-ledger-v1"
ACTIVE, OBSERVE, FROZEN, REVIVED = "在用", "观察", "冷冻", "复活"
TIER_CODE = {ACTIVE: "active", OBSERVE: "observe", FROZEN: "frozen", REVIVED: "revived"}
SCORING_TIERS = (ACTIVE, OBSERVE, REVIVED)       # 复活 = 新样本单独过 CI, 与在用同等 (可进纸面)
PAPER_TIERS = (ACTIVE, REVIVED)
REGIMES = ("进攻", "震荡", "防守")
SIDES = ("long", "short")
MIN_N = 30                                        # 同 rank_lab.MIN_SEG_N
FREEZE_STREAK = 2
DEFAULT_K = 100.0
AUDIT_FILE = "ledger_audit.jsonl"
CONFIG_DIR = Path(__file__).parents[2] / "config"
_Z975 = 1.959963984540054


# ── 纯函数 ─────────────────────────────────────────────────────────────────────

def classify(n: int | None, excess: float | None, ci_low: float | None, ci_high: float | None,
             prev_tier: str | None = None, freeze_streak: int = 0) -> str:
    """四档判定。

    冷冻后: 只有新样本单独 n ≥ 30 且 ci_low > 0 才「复活」, 否则仍「冷冻」(调用方传入冷冻后的新样本统计)。
    其余: 连续 2 次月度复核点估计 < 0 或 ci_high < 0 → 冷冻; ci_low > 0 且 n ≥ 30 → 在用;
    excess > 0 且 n ≥ 30 且 ci_low ≤ 0 ≤ ci_high → 观察; 证据不足 (n < 30 / 无 CI / 点估计 ≤ 0) → 冷冻 (fail-closed, 不参与打分)。
    """
    n = int(n or 0)
    ok_ci = ci_low is not None and ci_high is not None and math.isfinite(ci_low) and math.isfinite(ci_high)
    if prev_tier == FROZEN:
        return REVIVED if (ok_ci and n >= MIN_N and ci_low > 0) else FROZEN
    if freeze_streak >= FREEZE_STREAK or (ok_ci and ci_high < 0):
        return FROZEN
    if ok_ci and ci_low > 0 and n >= MIN_N:
        return ACTIVE
    if (ok_ci and excess is not None and excess > 0 and n >= MIN_N and ci_low <= 0 <= ci_high):
        return OBSERVE
    return FROZEN


def shrink(excess: float | None, n: int | None, k: float = DEFAULT_K) -> float:
    """收缩超额 = excess × n / (n + k)。k = 0 → 直接等于 excess。"""
    if excess is None or not math.isfinite(float(excess)):
        return 0.0
    n = max(int(n or 0), 0)
    k = max(float(k), 0.0)
    if n + k <= 0:
        return 0.0
    return float(excess) * n / (n + k)


def card_score(cells: Iterable[tuple[float | None, int | None]], k: float = DEFAULT_K) -> float:
    """卡分 = Σ 命中规则的收缩超额。cells = [(excess, n), ...]。"""
    return sum(shrink(e, n, k) for e, n in cells)


def ci_from_pairs(pairs: list[tuple[int, float]]) -> tuple[float | None, float | None]:
    """逐笔 (交易日序号, 超额 R) → 周聚类自助 95% CI (复用 rank_lab.week_boot)。样本 < 10 → (None, None)。"""
    from app.services import rank_lab
    ci, _ = rank_lab.week_boot(pairs)
    return (ci[0], ci[1]) if ci else (None, None)


def ci_from_summary(excess: float | None, p: float | None) -> tuple[float | None, float | None]:
    """产物只有 (excess, 单侧 p): 反推自助标准差 se = excess / z(1-p), CI = excess ± 1.96 se。
    p 恰为 0.5 (z = 0) / 缺 → (None, None)。p ≈ 0 → 下限按 p = 1e-6 截。"""
    if excess is None or p is None or not (0 <= p <= 1):
        return None, None
    from statistics import NormalDist
    pp = min(max(float(p), 1e-6), 1 - 1e-6)
    z = NormalDist().inv_cdf(1 - pp)
    if abs(z) < 1e-9 or abs(excess) < 1e-12:
        return None, None
    se = abs(float(excess) / z)
    return round(excess - _Z975 * se, 4), round(excess + _Z975 * se, 4)


def cell_key(rule: str, side: str) -> str:
    return f"{rule}|{side}"


# ── 读既有产物 ─────────────────────────────────────────────────────────────────

_ART_CACHE: dict[str, Any] = {}


def _read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def artifact_stats(config_dir: Path | None = None) -> dict[str, Any]:
    """产物汇总 → {"cells": {"rule|side": {n, excess, p, ci_low, ci_high, source}}, "window", "data_end"}。进程内缓存。"""
    cdir = Path(config_dir or CONFIG_DIR)
    ck = str(cdir)
    if ck in _ART_CACHE:
        return _ART_CACHE[ck]
    tight = _read_json(cdir / "swing_rule_history.json")
    wide = _read_json(cdir / "watchlist_rule_history.json")
    base = _read_json(cdir / "swing_base_rates.json")
    cells: dict[str, dict] = {}

    def put(rules: dict, source: str) -> None:
        for rid, v in (rules or {}).items():
            if not isinstance(v, dict):
                continue
            rule, side = (rid[:-6], "short") if rid.endswith(":short") else (rid, "long")
            key = cell_key(rule, side)
            if key in cells:
                continue
            ex, p = v.get("excess"), v.get("p")
            ex = float(ex) if isinstance(ex, (int, float)) else None
            p = float(p) if isinstance(p, (int, float)) else None
            lo, hi = ci_from_summary(ex, p)
            cells[key] = {"n": int(v.get("n") or 0), "excess": ex, "p": p, "ci_low": lo, "ci_high": hi,
                          "source": source}

    put(tight.get("rules") or {}, "swing_rule_history")
    put(wide.get("rules") or {}, "watchlist_rule_history")
    out = {"cells": cells, "window": tight.get("window") or base.get("window"),
           "data_end": tight.get("data_end") or base.get("data_end")}
    _ART_CACHE[ck] = out
    return out


# ── 配置 / 审计 ────────────────────────────────────────────────────────────────

def _swing_cfg(data_dir: Path) -> dict:
    return _read_json(Path(data_dir) / "rules" / "swing.json")


def ledger_k(data_dir: Path) -> float:
    v = _swing_cfg(data_dir).get("ledger_k")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else DEFAULT_K


def enabled(data_dir: Path) -> bool:
    """回滚开关: swing.json "ledger": false → 不按档位过滤。"""
    return _swing_cfg(data_dir).get("ledger") is not False


def audit_path(data_dir: Path) -> Path:
    return Path(data_dir) / "rules" / AUDIT_FILE


def _audit_records(data_dir: Path) -> list[dict]:
    p = audit_path(data_dir)
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and isinstance(r.get("month"), str) and isinstance(r.get("cells"), dict):
                out.append(r)
    except OSError:
        pass
    return out


def review_cells(stats: dict[str, dict], prev: dict[str, dict] | None) -> dict[str, dict]:
    """月度复核 (纯函数): 产物统计 × 上次复核 → 本次各格 (含 tier / freeze_streak)。格子 key = "rule|side|regime"。"""
    prev = prev or {}
    out: dict[str, dict] = {}
    for key, s in sorted(stats.items()):
        for reg in REGIMES:
            ck = f"{key}|{reg}"
            pv = prev.get(ck) or {}
            ex = s.get("excess")
            streak = (int(pv.get("freeze_streak") or 0) + 1) if (ex is not None and ex < 0) else 0
            tier = classify(s.get("n"), ex, s.get("ci_low"), s.get("ci_high"),
                            prev_tier=pv.get("tier"), freeze_streak=streak)
            out[ck] = {"n": s.get("n"), "excess": ex, "ci_low": s.get("ci_low"), "ci_high": s.get("ci_high"),
                       "tier": tier, "freeze_streak": streak}
    return out


def monthly_review(data_dir: Path, day: date, *, config_dir: Path | None = None) -> dict:
    """day 所在自然月的复核记录: 已有 → 原样返回; 没有 → 用上一次复核 + 当前产物算一条并只追加写入审计。"""
    month = day.strftime("%Y-%m")
    recs = _audit_records(data_dir)
    for r in reversed(recs):
        if r["month"] == month:
            return r
    earlier = [r for r in recs if r["month"] < month]
    prev = max(earlier, key=lambda r: r["month"])["cells"] if earlier else None
    art = artifact_stats(config_dir)
    rec = {"contract": CONTRACT, "month": month,
           "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "window": art.get("window"), "data_end": art.get("data_end"), "regime_split": False,
           "cells": review_cells(art["cells"], prev)}
    p = audit_path(data_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass                     # 写不了审计不挡出板 (档位仍按本次复核)
    return rec


def ledger_view(rec: dict, regime_state: str | None, k: float) -> dict:
    """复核记录 → 出板用的切片: {"contract", "month", "regime", "k", "cells": {"rule|side": {n, excess, tier}}}。
    regime 缺 (P1-A 回滚) → 用「震荡」格 (现产物三格同值)。"""
    reg = regime_state if regime_state in REGIMES else "震荡"
    cells = {}
    for ck, c in (rec.get("cells") or {}).items():
        rule_side, _, r = ck.rpartition("|")
        if r == reg:
            cells[rule_side] = {"n": c.get("n"), "excess": c.get("excess"), "tier": c.get("tier")}
    return {"contract": CONTRACT, "month": rec.get("month"), "regime": reg, "k": k, "cells": cells}


def ledger_for(data_dir: Path, day: date, regime_state: str | None) -> dict | None:
    """出板读点: 回滚开关关 → None; 否则本月复核 (必要时落审计) 在当前 regime 的切片。"""
    if not enabled(data_dir):
        return None
    return ledger_view(monthly_review(data_dir, day), regime_state, ledger_k(data_dir))


def lookup(ledger: dict | None, rule: str, side: str) -> tuple[str, float | None, int]:
    """(tier, excess, n)。ledger=None (开关关) → 档位一律在用, 分数用产物汇总。
    rank_* 排序因子不在账本里 (由 rank-lab-v2 确认合同管), 视为在用、分数 0。账本里没有的其他规则 → 冷冻。"""
    if rule.startswith("rank_"):
        return ACTIVE, 0.0, 0
    if ledger is None:
        c = artifact_stats()["cells"].get(cell_key(rule, side)) or {}
        return ACTIVE, c.get("excess"), int(c.get("n") or 0)
    c = (ledger.get("cells") or {}).get(cell_key(rule, side))
    if not c:
        return FROZEN, None, 0
    return c.get("tier") or FROZEN, c.get("excess"), int(c.get("n") or 0)
