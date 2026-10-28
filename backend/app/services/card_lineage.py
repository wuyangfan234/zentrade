"""机会卡谱系 (方向 A, 2026-10-11 无限批复 A-D-impl-plan)。只读: 不写任何文件、不改许可。

出卡时 (盘后扫描写自选信号表那一刻) 给每一行打复现印记, 结算时据此分清“规则变了还是数据变了”:
  ruleset_version   规则集版本 (rules/current.json → R{n})
  params_hash       规则集参数 + 该行止损档 + 结算常量 (入场窗/持有/跳空/滑点) 的 sha256 前 16 位
  data_asof         扫描所用最后一根已收盘日K的日期 (= 信号日)
  strategy_fingerprint  引擎策略 = strategy_evaluation.current_fingerprints (源码字节 + 有效覆盖 + 口径);
                    不在引擎里的 Phantom 4h / 内置 S1 S2 = 模块文件 sha256 前 12 位 (标 module_sha); 算不出 = None
  mode              档位角标: T 试用 / O 下单 (前向确认档) / W 观察 / L 记账 —— 与机会卡 tradable/tier/watch_only 同一判定
历史信号表 (没有印记) 用 reconstruct() 按当时生效的规则集版本与试用开关重建, 标 lineage_src="reconstructed",
策略指纹一律留空 (代码已变, 无法还原)。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CONTRACT = "zt-settle-v1"
SGT = timezone(timedelta(hours=8))

# 家族 (A/B 共用一份): 趋势延续 / 突破 / 回踩 / 反转 / 量价事件。暂定, B 落地时再与无限确认
_FAMILY = {
    "breakout": ("breakout", "breakout_new_high_60d", "boll_breakout", "ma_convergence_breakout",
                 "platform_consolidation_breakout", "trend_breakout"),
    "trend": ("bullish_alignment", "ma_golden_cross", "macd_golden", "low_volatility_leader", "tv_supertrend", "tv_ut_bot",
              "tv_chandelier_exit", "tv_halftrend", "tv_zlsma_cross", "tv_phantom_shift", "tv_phantom_trail",
              "tv_smc_structure", "tv_wavetrend", "tv_squeeze_momentum", "phantom_4h", "phantom_4h_pine", "phantom_4h_short"),
    "pullback": ("pullback", "pullback_ma20_bounce", "pullback_to_support", "rsi_midline_pullback"),
    "reversal": ("long_lower_shadow_reversal", "n_day_low_reversal", "oversold_bounce", "oversold_reversal",
                 "macd_below_zero_revival"),
    "volume_event": ("volume_price_surge", "strong_open"),
}
_RULE_FAMILY = {r: f for f, rules in _FAMILY.items() for r in rules}


def rule_family(rule: str) -> str:
    period = "4h" if str(rule).startswith("phantom_4h") else "1d"
    return f"zt:{_RULE_FAMILY.get(rule, 'other')}:{period}"


def settle_constants() -> dict:
    from app.services import strategy_evaluation as se
    return {"entry_window": se.ENTRY_WINDOW, "max_hold": se.MAX_HOLD, "gap_limit": se.GAP_LIMIT, "slip": se.SLIP}


def params_hash(params: dict | None, stop, per_symbol_stop: bool) -> str:
    blob = json.dumps({"params": params or {}, "stop": stop, "per_symbol_stop": bool(per_symbol_stop),
                       "settle": settle_constants()}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def mode_code(pm: dict, rule: str, side: str, enabled: set) -> str:
    """与旧机会卡 (0.29.1 退役) 的 tradable / tier / watch_only 同一判定 (历史谱系重建用)。"""
    from app.services import watchlist_v4 as wv
    if side == "long" and rule in (pm.get("trial_long") or set()) and rule not in (pm.get("tradable") or set()):
        return "T"          # 只用于重建 0.11.0 之前的历史行 (当时的试用档)
    if side == "short" and rule in (pm.get("short_tradable") or set()):
        return "T"
    ok, _, _tier = wv.rule_permission(pm, rule, side)
    if ok:
        return "O"
    return "W" if (side == "short" or rule not in enabled) else "L"


_SERVICES = Path(__file__).resolve().parent


def _module_sha(rule: str) -> str | None:
    from app.services import decision_desk as dd
    if rule.startswith("phantom_4h"):
        f = _SERVICES / ("phantom_4h_pine.py" if "pine" in rule else "phantom_4h.py")
    elif rule in dd.BUILTIN_SETUPS:
        f = _SERVICES / "decision_desk.py"
    else:
        return None
    try:
        return hashlib.sha256(f.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def fingerprints(data_dir: Path, rules: list[str]) -> dict[str, dict | None]:
    from app.services import strategy_evaluation as se
    try:
        eng = se.current_fingerprints(data_dir, sorted(set(rules)))
    except Exception:  # noqa: BLE001
        eng = {}
    out = {}
    for r in sorted(set(rules)):
        if eng.get(r):
            out[r] = {"kind": "engine", "hash": eng[r]}
        else:
            m = _module_sha(r)
            out[r] = {"kind": "module_sha", "hash": m} if m else None
    return out


def stamp(data_dir: Path, as_of: date, *, rules: list[str]):
    """(lineage, row_extra) 供 watchlist_v4.save_signals 写入; 任何失败 → (None, None), 不影响落表。"""
    try:
        from app.services import strategy_evaluation
        from app.services import watchlist_v4 as wv
        pm = wv.permission(data_dir)
        rs = pm.get("ruleset") or {}
        v4 = strategy_evaluation.evidence_mode(data_dir) == "v4"
        enabled = {x for x in rs.get("enabled") or [] if isinstance(x, str)}
        params = rs.get("params") or {}
        lineage = {"contract": CONTRACT, "ruleset_version": rs.get("version"), "data_asof": as_of.isoformat(),
                   "strategy_fingerprint": fingerprints(data_dir, rules), "settle": settle_constants(),
                   "stamped_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        empty = {"tradable": set(), "trial_long": set(), "short_tradable": set()}

        def extra(row: dict) -> dict:
            side = "short" if row.get("side") == "short" else "long"
            return {"mode": mode_code(pm if v4 else empty, row.get("rule"), side, enabled),
                    "params_hash": params_hash(params, row.get("stop"), bool(row.get("per_symbol_stop")))}
        return lineage, extra
    except Exception as e:  # noqa: BLE001
        logger.warning("card lineage stamp failed (signals saved without it): %s", type(e).__name__)
        return None, None


def _ts(s) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=SGT)


def _cutoff(payload: dict) -> datetime:
    """该信号表生效的时刻: 实时文件 = generated_at; 补算文件 = 信号日次日 06:00 SGT (盘后扫描约 04:50 SGT)。"""
    day = date.fromisoformat(str(payload.get("as_of"))[:10])
    if not payload.get("backfill"):
        t = _ts(payload.get("generated_at"))
        if t is not None:
            return t
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), SGT) + timedelta(hours=6)


def reconstruct(data_dir: Path, payload: dict) -> dict:
    """无印记的历史信号表: 按当时生效的规则集版本与试用开关重建档位与参数哈希 (标 reconstructed)。
    前向确认档在 2026-10-11 前从未通过 (影子账本无已结算行), 重建时只有试用 / 记账 / 观察三档。"""
    from app.services import watchlist_v4 as wv
    cut = _cutoff(payload)
    rd = wv.rules_dir(data_dir)
    version, rs = 0, {"enabled": list(wv.DEFAULT_ENABLED), "params": dict(wv.DEFAULT_PARAMS)}
    for v in wv.versions(data_dir):
        r = wv._read(rd / f"R{v}.json")
        t = _ts((r or {}).get("created_at"))
        if isinstance(r, dict) and t is not None and t <= cut:
            version, rs = v, r
    trial = None
    p = rd / "trial_audit.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(line)
            except ValueError:
                continue
            t = _ts(x.get("at"))
            if isinstance(x, dict) and t is not None and t <= cut:
                trial = x if x.get("enabled") is True else None
    enabled = {x for x in rs.get("enabled") or [] if isinstance(x, str)}
    trial_short = {x for x in trial.get("short") or [] if isinstance(x, str)} & wv._short_capable_ids() if trial else set()
    pm = {"tradable": set(), "trial_long": set(enabled) if (trial and version >= 1) else set(),
          "short_tradable": trial_short if version >= 1 else set(),
          "risk_pct": 0.0}   # 历史重建: 0.11.0 之前的试用档 (风险 0.25%) 只用于角标 T, 不进许可
    params = rs.get("params") or {}

    def mode(row: dict) -> str:
        side = "short" if row.get("side") == "short" else "long"
        return mode_code(pm, row.get("rule"), side, enabled)

    def ph(row: dict) -> str:
        return params_hash(params, row.get("stop"), bool(row.get("per_symbol_stop")))
    return {"ruleset_version": version, "mode": mode, "params_hash": ph, "strategy_fingerprint": {},
            "lineage_src": "reconstructed", "cutoff": cut.isoformat()}
