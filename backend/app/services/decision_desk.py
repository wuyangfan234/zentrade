"""全系统决策台 (2026-10-02 产品决策)。

机会页 = 系统所有策略的综合决策, 而不是 S1/S2 两个形态的输出:
1. 候选来源: 策略库全部可运行策略 + S1 突破 + S2 回踩 (以后挖掘/AI 发布的策略经人工启用后加入)。
2. 统一成卡: 任何来源选中的股票都按同一纪律做成交易提案 —— 入场区 (最近收盘 ~ +1%)、
   失效点 (策略自带止损与 2×ATR 取较紧)、目标区 (≥2R 投影), 最长 7 个交易日。
3. 权重来自证据: 统一口径重放的平均 R 按入场日聚类 95% 置信下界;
   - 下界 ≤ 0 且样本 ≥ 30 → 权重 0 (只展示, 注明原因)
   - 样本 < 30 → 探索额度 (单笔风险 0.25%, 每天最多 1 张, 标“探索单”)
   - 下界 > 0 → 正常 1% 风险, 权重 = 下界 × 收缩系数 n/(n+100)
   每周更新, 单次变化不超过 ±50% (负证据立即归零)。
4. 综合分 = Σ 各来源权重 × 共振加成 (同日 ≥2 个独立来源选中); 因子只用于排序画像。
5. 人工管理: 新来源默认“只观察”, 由用户手动启用/停用/锁权重上限, 每次操作记审计。
6. 两个分页: 全股票池 / 自选股 (同一账户、同一风控)。
"""
from __future__ import annotations

import json
import math
import os
import threading
from datetime import date, datetime
from pathlib import Path

import numpy as np
import polars as pl

from app.opportunity.card import CardState, OpportunityCard

MAX_HOLDING_DAYS = 7
MIN_RR = 2.0
ENTRY_ABOVE_PCT = (0.0, 1.0)
ATR_MULT = 2.0
MIN_SAMPLES_FOR_JUDGEMENT = 30
SHRINK_K = 100
WEEKLY_MAX_CHANGE = 0.5
RESONANCE_BONUS = 1.25
EXPLORE_RISK_PCT = 0.0025
PROVEN_RISK_PCT = 0.01
EXPLORE_DAILY_CAP = 1
BUILTIN_SETUPS = ("breakout", "pullback")
# 做空来源 (2026-10-03): 来源 id 在此登记即按空头成卡; 其余来源一律做多
SHORT_SOURCES = frozenset({"phantom_4h_short"})


def side_of(source: str) -> str:
    return "short" if source in SHORT_SOURCES else "long"

_lock = threading.Lock()


# ── 统一成卡 ────────────────────────────────────────────────

def atr(b: pl.DataFrame, n: int = 14) -> float:
    if b.height < 6:
        return 0.0
    h, l, c = b["high"].to_numpy(), b["low"].to_numpy(), b["close"].to_numpy()
    pc = np.roll(c, 1)[1:]
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - pc), np.abs(l[1:] - pc)))
    return float(np.mean(tr[-n:]))


def card_from_pick(source: str, symbol: str, b: pl.DataFrame, *, as_of: date, stop_loss: float | None,
                   revision: int = 1, extra: dict | None = None, side: str = "long") -> OpportunityCard | None:
    """策略选中的一只股票 → 7 日纪律交易提案。b 为截至 as_of 的不复权/复权一致日K (升序)。

    side="short": 镜像 — 入场区 收盘 −1% ~ 收盘; 失效点 = min(策略止损(正比例), 中值 + 2ATR);
    目标 = min(60 日最低, 中值 − 2R)。stop_loss 对空头为正数比例 (如 +0.03 = 高于入场 3%)。
    """
    if b.is_empty() or b.height < 20:
        return None
    last = float(b["close"][-1])
    if not np.isfinite(last) or last <= 0:
        return None
    a = atr(b)
    if a <= 0:
        return None
    if side == "short":
        return _short_card(source, symbol, b, last, a, as_of=as_of, stop_loss=stop_loss, revision=revision, extra=extra)
    lo, hi = round(last * (1 + ENTRY_ABOVE_PCT[0] / 100), 4), round(last * (1 + ENTRY_ABOVE_PCT[1] / 100), 4)
    mid = (lo + hi) / 2
    inval_atr = mid - ATR_MULT * a
    inval_strategy = mid * (1 + stop_loss) if stop_loss is not None and -0.5 < stop_loss < 0 else None
    invalidation = round(max(x for x in (inval_atr, inval_strategy) if x is not None), 4)
    if invalidation <= 0 or invalidation >= lo:
        return None
    risk = mid - invalidation
    recent_high = float(b["high"].tail(60).max())
    target = max(recent_high, mid + MIN_RR * risk)
    rr = (target - mid) / risk
    if rr < MIN_RR or risk / mid < 0.005:
        return None
    setup = f"strategy:{source}"
    card = OpportunityCard(
        id=OpportunityCard.make_id(symbol, setup.replace(":", "-"), as_of, revision),
        symbol=symbol, setup_type=setup, created_date=as_of,
        entry_zone=(lo, hi), invalidation=invalidation,
        target_zone=(round(target * 0.98, 4), round(target, 4)), max_holding_days=MAX_HOLDING_DAYS,
        risk_per_share=round(risk, 4), reward_risk=round(rr, 2),
        evidence={"source": source, "last_close": last, "atr14": round(a, 4), "stop_loss": stop_loss,
                  "recent_high_60d": recent_high, "unified_rule": "entry 0~+1%, inval=max(strategy stop, 2ATR), target>=2R, 7d",
                  **(extra or {})},
    )
    return None if card.validate(MIN_RR) else card


def _short_card(source: str, symbol: str, b: pl.DataFrame, last: float, a: float, *, as_of: date,
                stop_loss: float | None, revision: int, extra: dict | None) -> OpportunityCard | None:
    lo, hi = round(last * (1 - ENTRY_ABOVE_PCT[1] / 100), 4), round(last * (1 - ENTRY_ABOVE_PCT[0] / 100), 4)
    mid = (lo + hi) / 2
    inval_atr = mid + ATR_MULT * a
    inval_strategy = mid * (1 + stop_loss) if stop_loss is not None and 0 < stop_loss < 0.5 else None
    invalidation = round(min(x for x in (inval_atr, inval_strategy) if x is not None), 4)
    if invalidation <= hi:
        return None
    risk = invalidation - mid
    recent_low = float(b["low"].tail(60).min())
    target = min(recent_low, mid - MIN_RR * risk)
    if target <= 0:
        return None
    rr = (mid - target) / risk
    if rr < MIN_RR or risk / mid < 0.005:
        return None
    setup = f"strategy:{source}"
    card = OpportunityCard(
        id=OpportunityCard.make_id(symbol, setup.replace(":", "-") + "-short", as_of, revision),
        symbol=symbol, setup_type=setup, created_date=as_of, side="short",
        entry_zone=(lo, hi), invalidation=invalidation,
        target_zone=(round(target, 4), round(target * 1.02, 4)), max_holding_days=MAX_HOLDING_DAYS,
        risk_per_share=round(risk, 4), reward_risk=round(rr, 2),
        evidence={"source": source, "side": "short", "last_close": last, "atr14": round(a, 4), "stop_loss": stop_loss,
                  "recent_low_60d": recent_low,
                  "unified_rule": "short: entry -1%~0, inval=min(strategy stop, 2ATR), target<=-2R (60d low), 7d",
                  **(extra or {})},
    )
    return None if card.validate(MIN_RR) else card


# ── 证据 → 权重 ─────────────────────────────────────────────

def source_weight(ev: dict | None) -> dict:
    """ev = {n, mean, ci_low} (统一口径重放, 入场日聚类 95% CI)。"""
    invalid = {"weight": 0.0, "tier": "negative", "risk_pct": 0.0, "daily_cap": 0,
               "reason": "统一口径证据缺失或损坏：只展示不开仓"}
    if isinstance(ev, dict) and ev.get("stale_version"):
        return {**invalid, "reason": "策略在评估之后改过（源码或保存的参数），这份证据已不对应当前策略：只展示不开仓，等下次评估"}
    if isinstance(ev, dict) and ev.get("v4"):
        return _v4_weight(ev)
    if isinstance(ev, dict) and ev.get("v3"):
        return _v3_weight(ev)
    if not isinstance(ev, dict) or type(ev.get("n")) not in (int, float):
        return invalid
    n = ev["n"]
    if not math.isfinite(n) or n < 0 or int(n) != n:
        return invalid
    def _finite(k):
        return type(ev.get(k)) in (int, float) and math.isfinite(ev[k])
    # 样本少 (<30) 时按入场日聚类的 CI 可能算不出 (单日样本) —— 那是“样本薄”, 不是“证据损坏”;
    # 只有达到判定样本量后 CI 仍缺失/非法才按损坏处理 (2026-10-05)。
    if n and not _finite("mean"):
        return invalid
    if n >= MIN_SAMPLES_FOR_JUDGEMENT and not _finite("ci_low"):
        return invalid
    if n == 0:
        return {"weight": 0.0, "tier": "explore", "risk_pct": EXPLORE_RISK_PCT, "daily_cap": EXPLORE_DAILY_CAP,
                "reason": "尚无统一口径评估：按探索额度（单笔 0.25%，每天最多 1 张）"}
    n, ci_low, mean = int(ev["n"]), ev.get("ci_low"), ev.get("mean")
    if n < MIN_SAMPLES_FOR_JUDGEMENT:
        return {"weight": round(max(mean or 0, 0) * n / (n + SHRINK_K), 4), "tier": "explore",
                "risk_pct": EXPLORE_RISK_PCT, "daily_cap": EXPLORE_DAILY_CAP,
                "reason": f"样本 {n} 笔 <{MIN_SAMPLES_FOR_JUDGEMENT}：按探索额度（单笔 0.25%，每天最多 1 张）"}
    if ci_low is None or ci_low <= 0:
        return {"weight": 0.0, "tier": "negative", "risk_pct": 0.0, "daily_cap": 0,
                "reason": f"{n} 笔平均 {mean:+.3f}R，置信下界 {ci_low:+.3f}R ≤ 0：只展示不开仓"}
    return {"weight": round(ci_low * n / (n + SHRINK_K) * 10, 4), "tier": "proven", "risk_pct": PROVEN_RISK_PCT,
            "daily_cap": None, "reason": f"{n} 笔平均 {mean:+.3f}R，置信下界 {ci_low:+.3f}R > 0"}


def _v4_weight(ev: dict) -> dict:
    """v4 (2026-10-06): 规则集勾选 + 自选前向确认通过才可下单 (basis=v4; 来源登记表的启用/观察不再参与)。"""
    risk = ev.get("risk_pct")
    ok = ev.get("tradable") is True and type(risk) in (int, float) and math.isfinite(risk) and 0 < risk <= 0.02
    fwd = ev.get("forward") if isinstance(ev.get("forward"), dict) else {}
    ex = fwd.get("excess") if type(fwd.get("excess")) in (int, float) else 0.0
    n = fwd.get("n") if type(fwd.get("n")) in (int, float) else 0
    if ok:
        return {"weight": round(max(ex, 0.0001) * n / (n + SHRINK_K) * 10, 4) or 0.0001, "tier": "proven",
                "risk_pct": float(risk), "daily_cap": None, "reason": "v4", "basis": "v4"}
    return {"weight": 0.0, "tier": "negative", "risk_pct": 0.0, "daily_cap": 0, "reason": "v4", "basis": "v4"}


def _v3_weight(ev: dict) -> dict:
    """v3.1 (2026-10-05): 历史证据 (随机基线 + 留出期 + 退市股 + 多重检验) 或前向确认 (影子账本) 任一通过才开仓。
    权重 = 超额 R (相对随机挑选) × 收缩系数; 只有 proven 一档, 探索档经安慰剂校准后停用。"""
    def num(x):  # 只认真正的有限数值; 字符串/布尔/NaN 一律视为缺失 (fail-closed)
        return float(x) if type(x) in (int, float) and math.isfinite(x) else None

    fwd = ev.get("forward") if isinstance(ev.get("forward"), dict) else {}
    f_n, f_ex = num(fwd.get("n")), num(fwd.get("excess"))
    if fwd.get("promoted") is True and f_n and f_n > 0 and f_ex is not None and f_ex > 0:
        return {"weight": round(f_ex * f_n / (f_n + SHRINK_K) * 10, 4), "tier": "proven", "risk_pct": PROVEN_RISK_PCT,
                "daily_cap": None, "reason": fwd.get("reason") or "前向确认通过", "basis": "forward"}
    ex_block = ev.get("excess") if isinstance(ev.get("excess"), dict) else {}
    n, ex = num(ev.get("n")), num(ex_block.get("mean"))
    if ev.get("tier_v3") == "proven" and n and n > 0 and ex is not None and ex > 0:
        return {"weight": round(ex * n / (n + SHRINK_K) * 10, 4), "tier": "proven", "risk_pct": PROVEN_RISK_PCT,
                "daily_cap": None, "reason": ev.get("reason_v3") or "v3 证据通过", "basis": "history"}
    reason = ev.get("reason_v3") or "v3 证据未通过：只展示不开仓"
    if fwd.get("candidate"):
        reason += f"；前向确认进行中：{fwd.get('progress_text') or ''}"
    return {"weight": 0.0, "tier": "negative", "risk_pct": 0.0, "daily_cap": 0, "reason": reason, "basis": "none"}


def capped_update(*, previous: float | None, target: float) -> float:
    if previous is None or previous <= 0 or target <= 0:
        return max(target, 0.0)
    lo, hi = previous * (1 - WEEKLY_MAX_CHANGE), previous * (1 + WEEKLY_MAX_CHANGE)
    return round(min(max(target, lo), hi), 4)


def weekly_weight(*, previous: float | None, target: float, due: bool) -> float:
    """周更语义 (2026-10-05 修正): 到期 → 从上周权重最多移动 ±50%; 未到期 → 沿用上周权重, 不重新定档。
    目标为 0 (负证据/缺证据) 立即归零; 上周没有正权重 (新转正) 直接取目标。"""
    if target <= 0:
        return 0.0
    if previous is None or previous <= 0:
        return round(target, 4)
    return capped_update(previous=previous, target=target) if due else round(previous, 4)


# ── 人工来源管理 ────────────────────────────────────────────

class SourceRegistry:
    """data/decision_desk/sources.json + audit.jsonl。状态: enabled / observe / disabled。"""

    def __init__(self, data_dir: Path, builtin: set[str] | None = None):
        self.dir = Path(data_dir) / "decision_desk"
        self.path = self.dir / "sources.json"
        self.audit_path = self.dir / "audit.jsonl"
        self.builtin = set(builtin or ())

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"_invalid": True}
        except (OSError, ValueError):
            return {"_invalid": True} if self.path.exists() else {}

    def _save(self, data: dict, entry: dict) -> None:
        """先落审计 (fsync) 再原子替换登记文件: 审计写不进去就不改授权; 替换失败补记 failed。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat()
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({**entry, "at": stamp}, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({**entry, "at": stamp, "failed": True}, ensure_ascii=False) + "\n")
            raise

    def status(self, source: str) -> str:
        return self.row(source)["status"]

    def set_status(self, source: str, status: str, note: str = "") -> dict:
        if status not in ("enabled", "observe", "disabled"):
            raise ValueError("status must be enabled/observe/disabled")
        with _lock:
            data = self._load()
            data.setdefault(source, {})["status"] = status
            self._save(data, {"action": "status", "source": source, "value": status, "note": note})
        return data[source]

    def set_cap(self, source: str, cap: float | None, note: str = "") -> dict:
        with _lock:
            data = self._load()
            row = data.setdefault(source, {})
            if cap is None:
                row.pop("cap", None)
            else:
                row["cap"] = float(cap)
            self._save(data, {"action": "cap", "source": source, "value": cap, "note": note})
        return row

    def seed(self, sources: set[str], note: str = "上线时已存在的策略默认启用") -> bool:
        """上线时一次性登记: 当时已存在的来源设为启用; 之后新增的来源不在登记里 → 默认只观察。"""
        with _lock:
            data = self._load()
            if data.get("_seeded"):
                return False
            # 登记文件丢失/损坏但审计历史还在 = 缺档, 不是首次上线: 不能把全部来源重新启用 (2026-10-05)。
            if self.audit_path.exists() and self.audit_path.stat().st_size > 0:
                return False
            for s in sorted(sources):
                data.setdefault(s, {}).setdefault("status", "enabled")
            data["_seeded"] = {"sources": sorted(sources), "at": datetime.now().astimezone().isoformat()}
            self._save(data, {"action": "seed", "source": "*", "value": sorted(sources), "note": note})
            return True

    def row(self, source: str) -> dict:
        data = self._load()
        row = data.get(source, {})
        if data.get("_invalid") or not isinstance(row, dict):
            return {"status": "observe"}
        return {"status": "enabled" if source in self.builtin else "observe", **row}

    def effective(self, source: str, w: dict) -> dict:
        row = self.row(source)  # one fresh snapshot for status AND cap
        st = row["status"]
        out = dict(w)
        if w.get("basis") == "v4":
            # v4: 规则集 (data/rules) 取代来源登记表的人工启用; 是否可下单只看规则集 + 前向确认
            out["status"] = "ruleset"
            out["tradable"] = out.get("tier") == "proven" and out.get("weight", 0) > 0
            return out
        out["status"] = st
        if st != "enabled":
            out["weight"] = 0.0
            out["tradable"] = False
            out["reason"] = ("已手动停用" if st == "disabled" else "只观察：等待人工启用") + "；" + w.get("reason", "")
            return out
        cap = row.get("cap")
        if cap is not None:
            if type(cap) not in (int, float) or not math.isfinite(cap) or cap <= 0:
                out.update(weight=0.0, tradable=False, risk_pct=0.0, reason="人工权重上限为零或无效：禁止开仓")
                return out
            out["weight"] = min(out.get("weight", 0.0), cap)
            out["capped"] = cap
        out["tradable"] = (out.get("tier") == "proven" and out.get("weight", 0) > 0) or out.get("tier") == "explore"
        return out

    def audit(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [json.loads(x) for x in self.audit_path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ── 综合排序 ────────────────────────────────────────────────

_TIER_ORDER = {"proven": 0, "explore": 1, "negative": 2}


def rank(picks: dict[str, list[str]], weights: dict[str, dict], *, watchlist: set[str]) -> list[dict]:
    out = []
    for sym, sources in picks.items():
        srcs = sorted(set(sources))
        tradable = [s for s in srcs if weights.get(s, {}).get("tier") in ("proven", "explore")
                    and weights.get(s, {}).get("tradable", True)]
        base = sum(weights.get(s, {}).get("weight", 0.0) for s in tradable)
        sides = sorted({side_of(s) for s in srcs})
        direction_conflict = len(sides) > 1
        resonance = 0 if direction_conflict else len(srcs)
        score = base * (RESONANCE_BONUS if resonance >= 2 else 1.0)
        tiers = sorted((weights.get(s, {}).get("tier", "explore") for s in tradable), key=lambda t: _TIER_ORDER.get(t, 9))
        tier = tiers[0] if tiers else "negative"
        risk = max((weights.get(s, {}).get("risk_pct", 0.0) for s in tradable), default=0.0)
        out.append({"symbol": sym, "sources": srcs, "tradable_sources": tradable, "resonance": resonance,
                    "sides": sides, "direction_conflict": direction_conflict,
                    "score": round(score, 4), "tier": tier, "risk_pct": risk, "tradable": bool(tradable),
                    "tab": "watchlist" if sym in watchlist else "pool"})
    out.sort(key=lambda r: (not r["tradable"], _TIER_ORDER.get(r["tier"], 9), -r["score"], -r["resonance"], r["symbol"]))
    return out


def apply_explore_quota(ranked: list[dict], cap: int = EXPLORE_DAILY_CAP) -> tuple[list[dict], list[dict]]:
    kept, held, used = [], [], 0
    for r in ranked:
        if r.get("tradable") and not r.get("direction_conflict") and r.get("tier") == "explore":
            if used >= cap:
                held.append(r)
                continue
            used += 1
        kept.append(r)
    return kept, held


def is_strategy_setup(setup_type: str) -> bool:
    return str(setup_type).startswith("strategy:")


def source_of(setup_type: str) -> str:
    return str(setup_type).split(":", 1)[1] if is_strategy_setup(setup_type) else str(setup_type)


__all__ = ["CardState", "atr", "card_from_pick", "source_weight", "capped_update", "SourceRegistry", "rank",
           "apply_explore_quota", "is_strategy_setup", "source_of"]
