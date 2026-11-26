"""P1-B1 策略账本: 格子 = (策略, 方向, regime), 档位五级: 在用 / 观察 / 待定 / 冷冻 / 复活; 收缩超额打分。

直接生效 (无影子): swing_desk.board_for_day 每次出板都读本账本 —
  - 打分 = Σ_命中且参与打分的规则 excess × n / (n + k)   (k 默认 100, data/rules/swing.json 的 "ledger_k" 可改)
    这个分就是机会板每侧排序的主键 (取代旧「命中条数 + 固定 EXCESS_WEIGHT」公式);
  - 待定 / 冷冻档规则不算命中、不参与打分 (只命中它们的票不出卡);
    卡上参与打分的非 rank_* 规则全是观察档 → ledger_tier="observe", 纸面下单簿不收;
    rank_* 排序因子不进账本 (rank-lab-v2 确认合同管), 不参与卡档位判定; 只有 rank_* 命中的卡 ledger_tier=None;
  - 回滚开关: swing.json "ledger": false → 不按档位过滤 (收缩打分仍用产物里的 excess/n); 彻底回滚 = git revert。
    开关关时出板读点给的是 status="disabled" 的空切片 (不是 None), 落簿冻结的也是它, 板与簿照样同源。
  - 账本不可用 (P1-B2, fail-closed): 历史/未来月份没有复核记录 → status="unavailable" 的空切片,
    所有规则 (含 rank_*) 一律按待定处理 → 不打分、不出卡、不进簿。不再把「没有账本」当成「全部在用」。

数据来源只读既有产物 (backend/config/*.json), 不回放、不写影子文件:
  swing_rule_history.json   tight-v1 每条规则 (多头 = id, 空头 = "id:short") 的 n / excess / p (主来源)
  watchlist_rule_history.json 旧宽卡历史, 只在 tight-v1 历史里没有该规则时兜底 (source 字段标明)
  swing_base_rates.json      只取窗口与数据截止日写进审计 (产物版本)
产物只有全窗口汇总 (2023-01 起), 没有逐笔样本、也没有按 regime 拆分:
  - CI 由产物 p 值反推 (产物 p = 超额均值 / 周聚类自助标准差的单侧正态近似, 与 rank_lab.judge_segment 同口径);
    有逐笔 (t, 超额) 时用 ci_from_pairs (直接复用 rank_lab.week_boot, 区间与 swing_candidates.week_boot_ci 逐位相同)。
  - 三个 regime 格子暂用同一份汇总 (regime_split=False 写进审计); 产物将来带 regime 拆分时按格子读。
  - 「滚动 252 交易日」受产物窗口限制, 现为产物全窗口; 窗口写进审计。
  - 复活的实际口径限制: 规格要求「冷冻后新样本单独 n ≥ 30 且 ci_low > 0」, 但产物只有全窗口汇总, 没有冷冻日之后的
    单独样本 → 现在复活判的是**全样本** n / CI (classify 的口径不变, 是调用方喂进去的统计量不是新样本)。
    产物将来带逐笔 / 分期样本时, review_cells 改为喂冷冻后样本即可。

月度复核: 当月 (美东) 第一次出板时落一条只追加审计 data/rules/ledger_audit.jsonl (不进结算台账)。
只写当月: 看历史/未来月份且该月没有记录 → 只读不写, monthly_review 返回 None, ledger_for 给 unavailable 切片
(fail-closed, 见上), 不会用当前产物给过去月份补写
而打乱 freeze_streak 的连续计数。同月内档位只认这条记录 (同一天同一批候选结果可复现);
freeze_streak = 连续几次月度复核点估计 < 0。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

CONTRACT = "strategy-ledger-v1"
ACTIVE, OBSERVE, PENDING, FROZEN, REVIVED = "在用", "观察", "待定", "冷冻", "复活"
TIER_CODE = {ACTIVE: "active", OBSERVE: "observe", PENDING: "pending", FROZEN: "frozen", REVIVED: "revived"}
SCORING_TIERS = (ACTIVE, OBSERVE, REVIVED)       # 复活 = 过 CI, 与在用同等 (可进纸面); 待定/冷冻不打分
PAPER_TIERS = (ACTIVE, REVIVED)
REGIMES = ("进攻", "震荡", "防守")
SIDES = ("long", "short")
MIN_N = 30                                        # 同 rank_lab.MIN_SEG_N
FREEZE_STREAK = 2
DEFAULT_K = 100.0
AUDIT_FILE = "ledger_audit.jsonl"
STATUS_OK, STATUS_DISABLED, STATUS_UNAVAILABLE = "ok", "disabled", "unavailable"
CONFIG_DIR = Path(__file__).parents[2] / "config"
_Z975 = 1.959963984540054


# ── 纯函数 ─────────────────────────────────────────────────────────────────────

def classify(n: int | None, excess: float | None, ci_low: float | None, ci_high: float | None,
             prev_tier: str | None = None, freeze_streak: int = 0) -> str:
    """档位判定 (五级)。优先级: 冷冻条件 > 复活 > 在用 > 观察 > 待定。

    冷冻: freeze_streak ≥ 2 (连续 2 次月度复核点估计 < 0) 或 ci_high < 0。
    复活: 上次是冷冻, 且 n ≥ 30 且 ci_low > 0 (否则保持冷冻)。口径限制见模块 docstring (现为全样本统计)。
    在用: ci_low > 0 且 n ≥ 30。
    观察: excess > 0 且 n ≥ 30 且 ci_low ≤ 0 ≤ ci_high。
    待定: 其余 = 证据不足 (n < 30 / CI 缺) 或 点估计 ≤ 0 但 streak < 2 且 ci_high ≥ 0。
          不打分、不出卡、不进簿 (效果同冷冻), 但不是冷冻: 下个月证据够了直接回到观察/在用, 不必走复活。
    """
    n = int(n or 0)
    ok_ci = (ci_low is not None and ci_high is not None
             and math.isfinite(ci_low) and math.isfinite(ci_high))
    if freeze_streak >= FREEZE_STREAK or (ok_ci and ci_high < 0):
        return FROZEN
    if prev_tier == FROZEN:
        return REVIVED if (ok_ci and n >= MIN_N and ci_low > 0) else FROZEN
    if ok_ci and ci_low > 0 and n >= MIN_N:
        return ACTIVE
    if ok_ci and excess is not None and excess > 0 and n >= MIN_N and ci_low <= 0 <= ci_high:
        return OBSERVE
    return PENDING


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


def _today() -> date:
    """当前美东日期 (月度复核按美东自然月)。"""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).date()


def monthly_review(data_dir: Path, day: date, *, config_dir: Path | None = None,
                   today: date | None = None) -> dict | None:
    """day 所在自然月的复核记录: 已有 → 原样返回; 没有且 day 在当月 → 用上一次复核 + 当前产物算一条并只追加写入审计;
    没有且 day 不在当月 (历史/未来) → None, 只读不写 (不补写过去月份, 保 freeze_streak 连续计数)。"""
    month = day.strftime("%Y-%m")
    recs = _audit_records(data_dir)
    for r in reversed(recs):
        if r["month"] == month:
            return r
    if month != (today or _today()).strftime("%Y-%m"):
        return None
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
    except OSError as e:         # 写不了审计不挡出板 (档位仍按本次复核), 但必须留痕
        logger.warning("strategy ledger: monthly audit write failed (%s): %s", rec.get("month"), type(e).__name__)
    return rec


def _cell_view(c: dict) -> dict:
    """单格切片 (ledger_view 与空切片共用同一形状): 旧三键 n / excess / tier 语义不变, 追加 ci_low / ci_high (可 None)。"""
    return {"n": c.get("n"), "excess": c.get("excess"), "tier": c.get("tier"),
            "ci_low": c.get("ci_low"), "ci_high": c.get("ci_high")}


def ledger_view(rec: dict, regime_state: str | None, k: float) -> dict:
    """复核记录 → 出板用的切片: {"contract", "month", "regime", "k", "cells": {"rule|side": {n, excess, tier, ci_low, ci_high}}}。
    regime 缺 (P1-A 回滚) → 用「震荡」格 (现产物三格同值)。ci_low / ci_high = 定档所用 95% 区间, 只供展示, 缺则 None。"""
    reg = regime_state if regime_state in REGIMES else "震荡"
    cells = {}
    for ck, c in (rec.get("cells") or {}).items():
        rule_side, _, r = ck.rpartition("|")
        if r == reg:
            cells[rule_side] = _cell_view(c)
    return {"contract": CONTRACT, "status": STATUS_OK, "month": rec.get("month"), "regime": reg, "k": k,
            "cells": cells}


def _empty_view(status: str, day: date, regime_state: str | None, k: float) -> dict:
    """disabled / unavailable 切片: cells 为空 (不改 lookup 语义); 若有格子, 形状同 _cell_view (含 ci_low / ci_high)。"""
    reg = regime_state if regime_state in REGIMES else "震荡"
    return {"contract": CONTRACT, "status": status, "month": day.strftime("%Y-%m"), "regime": reg, "k": k,
            "cells": {}}


def ledger_for(data_dir: Path, day: date, regime_state: str | None) -> dict:
    """出板读点, 总返回切片 (不返回 None, 落簿不会冻结 None):
      回滚开关关            → status="disabled": 不按档位过滤 (lookup 走产物汇总, 档位一律在用);
      该月没有复核记录      → status="unavailable": fail-closed, 所有规则按待定 (不出卡, 见模块 docstring);
      否则                  → status="ok": 该月复核 (当月必要时落审计) 在当前 regime 的切片。"""
    k = ledger_k(data_dir)
    if not enabled(data_dir):
        return _empty_view(STATUS_DISABLED, day, regime_state, k)
    rec = monthly_review(data_dir, day)
    if rec is None:
        return _empty_view(STATUS_UNAVAILABLE, day, regime_state, k)
    return ledger_view(rec, regime_state, k)


def lookup(ledger: dict | None, rule: str, side: str) -> tuple[str, float | None, int]:
    """(tier, excess, n)。
    status="unavailable" (该月无复核) → 一律待定, rank_* 也不例外 (fail-closed: 账本不可用时不出任何卡)。
    ledger=None (P1-B1 之前落的旧簿) / status="disabled" (开关关) → 档位一律在用, 分数用产物汇总。
    rank_* 排序因子不在账本里 (由 rank-lab-v2 确认合同管): 算命中、分数 0; 卡档位判定不计它 (见 swing_desk._card)。
    账本里没有的其他规则 → 待定 (无证据, 不打分)。"""
    status = ledger.get("status") if isinstance(ledger, dict) else None
    if status == STATUS_UNAVAILABLE:
        return PENDING, None, 0
    if rule.startswith("rank_"):
        return ACTIVE, 0.0, 0
    if ledger is None or status == STATUS_DISABLED:
        c = artifact_stats()["cells"].get(cell_key(rule, side)) or {}
        return ACTIVE, c.get("excess"), int(c.get("n") or 0)
    c = (ledger.get("cells") or {}).get(cell_key(rule, side))
    if not c:
        return PENDING, None, 0
    return c.get("tier") or PENDING, c.get("excess"), int(c.get("n") or 0)
