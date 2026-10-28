"""影子前向账本 (2026-10-05, 优先级②): 不占资金、不下单, 每天盘后把**全部来源**当日前 10 名 + 同日同池随机基线
做成与生产一致的 7 日卡, 冻结快照; 满 7 日纪律后按同一回放规则结算。目的: 用远快于真实成交的速度积累
“前向” (信号产生时未来未知) 样本, 并与历史回放并排比较, 发现回放与现实是否背离。

- 快照只用扫描当时已有的日K (盘后官方收盘已确认之后), 一旦写入不再改动 (只追加 outcome)。
- 随机基线: 当日流动性池中按日期固定种子抽 BASELINE_N 只, 每只按每个出现的止损比例各成一张卡 (与 v3 同口径)。
- 结算: 信号日后 ≥14 个自然日、日K 足够走完 5 日入场窗 + 7 日持仓 → 用 strategy_evaluation.simulate_native_daily
  (v2/v3 同一回放); 数据没走完保持待结算, 下次再试。
- 汇总: 按来源给出 前向 n / 平均 R / 同日随机基线 / 超额 R, 与 v3 历史回放并排展示 (机会页“来源体检”)。
文件: data/decision_desk/shadow_forward.jsonl (一行一张影子卡)。
"""
from __future__ import annotations

import json
import logging
import math
import threading
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

BASELINE_N = 40
BASELINE_ID = "__random__"
SETTLE_AFTER_DAYS = 14
STALE_DAYS = 10


_LOCK = threading.Lock()   # record (盘后扫描) 与 settle (周六) / 手动 rescan 互斥, 避免整文件读改写互相覆盖


def path(data_dir: Path) -> Path:
    """只算路径, 不建目录 (读路径不产生副作用; 写入时由 _save 建目录)。"""
    return Path(data_dir) / "decision_desk" / "shadow_forward.jsonl"


def load(data_dir: Path) -> list[dict]:
    p = path(data_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _save(data_dir: Path, rows: list[dict]) -> None:
    from app.services.fs_utils import atomic_write_text
    path(data_dir).parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    atomic_write_text(path(data_dir), text + ("\n" if text else ""))


def _card_row(source: str, symbol: str, card, as_of: date, *, rank: int | None, stop, now: str,
              ref_close: float | None = None) -> dict:
    """ref_close = 快照时 (信号日) 的收盘价, 与卡上价位同一复权基准; 结算时用它把后来的复权日K换算回快照基准
    (信号日之后发生拆股/并股, 复权日K整段被重新缩放, 冻结价位不能直接与之比较)。"""
    return {"as_of": as_of.isoformat(), "source": source, "symbol": symbol, "rank": rank, "stop_loss": stop,
            "entry_zone": list(card.entry_zone), "invalidation": float(card.invalidation),
            "target_zone": list(card.target_zone), "risk": float(card.risk_per_share), "snapshot_at": now,
            "ref_close": ref_close, "outcome": None}


def _last_close(frames: dict, symbol: str) -> float | None:
    b = frames.get(symbol)
    try:
        v = float(b["close"][-1]) if b is not None and b.height else None
    except Exception:  # noqa: BLE001
        return None
    return v if v is not None and math.isfinite(v) and v > 0 else None


def record(data_dir: Path, as_of: date, *, frames: dict, hits: dict[str, list[dict]], stops: dict,
           stops_by_symbol: dict | None = None, pool: list[str] | None = None, top_n: int = 10,
           native_cards: list | None = None, now: datetime | None = None) -> dict:
    """写入当日影子卡 (来源前 top_n + S1/S2 等原生卡 + 随机基线)。同一 (as_of, source, symbol, 止损) 只记一次。失败不抛。
    原生卡 (S1 突破/S2 回踩) 用自己的入场/失效/目标几何, 没有可比的同止损随机基线 → 只算绝对 R, 不算超额。"""
    now_s = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _LOCK:
            return _record_locked(data_dir, as_of, frames=frames, hits=hits, stops=stops,
                                  stops_by_symbol=stops_by_symbol, pool=pool, top_n=top_n,
                                  native_cards=native_cards, now_s=now_s)
    except Exception as e:  # noqa: BLE001
        logger.warning("shadow forward record failed (ignored): %s", type(e).__name__)
        return {}


def _record_locked(data_dir, as_of, *, frames, hits, stops, stops_by_symbol, pool, top_n, native_cards, now_s) -> dict:
    from app.services import decision_desk as dd
    if True:
        rows = load(data_dir)
        seen = {(r["as_of"], r["source"], r["symbol"], r.get("stop_loss")) for r in rows}
        added = defaultdict(int)
        used_stops = set()
        for sid, hs in (hits or {}).items():
            if dd.side_of(sid) != "long":
                continue  # 空头影子另立口径, 本轮只做多头
            per = (stops_by_symbol or {}).get(sid)
            for h in hs[:top_n]:
                sym = h["symbol"]
                b = frames.get(sym)
                stop = per.get(sym) if per is not None else stops.get(sid)
                if b is None or (as_of.isoformat(), sid, sym, stop) in seen:
                    continue
                card = dd.card_from_pick(sid, sym, b, as_of=as_of, stop_loss=stop)
                if card is None:
                    continue
                rows.append(_card_row(sid, sym, card, as_of, rank=h.get("rank"), stop=stop, now=now_s,
                                      ref_close=_last_close(frames, sym)))
                seen.add((as_of.isoformat(), sid, sym, stop))
                added[sid] += 1
                if per is None:
                    used_stops.add(stop)
        for setup, card in native_cards or []:
            sid = dd.source_of(setup)
            key = (as_of.isoformat(), sid, card.symbol, "native")
            if key in seen:
                continue
            rows.append(_card_row(sid, card.symbol, card, as_of, rank=None, stop="native", now=now_s,
                                  ref_close=_last_close(frames, card.symbol)))
            seen.add(key)
            added[sid] += 1
        universe = sorted(pool if pool is not None else frames)
        if universe:
            rng = np.random.default_rng(int(as_of.strftime("%Y%m%d")))
            sample = rng.choice(len(universe), size=min(BASELINE_N, len(universe)), replace=False)
            for stop in sorted(used_stops or {-0.06}, key=lambda x: (x is None, x)):
                for k in sample:
                    sym = universe[int(k)]
                    key = (as_of.isoformat(), BASELINE_ID, sym, stop)
                    if key in seen:
                        continue
                    card = dd.card_from_pick(BASELINE_ID, sym, frames[sym], as_of=as_of, stop_loss=stop)
                    if card is None:
                        continue
                    rows.append(_card_row(BASELINE_ID, sym, card, as_of, rank=None, stop=stop, now=now_s,
                                          ref_close=_last_close(frames, sym)))
                    seen.add(key)
                    added[BASELINE_ID] += 1
        if added:
            _save(data_dir, rows)
        return dict(added)


def settle(data_dir: Path, repo, *, today: date) -> int:
    """结算满期影子卡。返回本次结算条数。"""
    with _LOCK:
        return _settle_locked(data_dir, repo, today=today)


def _settle_locked(data_dir: Path, repo, *, today: date) -> int:
    rows = load(data_dir)
    n = settle_rows(rows, repo, today=today)
    if n:
        _save(data_dir, rows)
    return n


TERMINAL = frozenset({"closed", "no_fill", "gap_expired"})


def settle_rows(rows: list[dict], repo, *, today: date) -> int:
    """就地给满期影子卡填 outcome (纯内存, 调用方负责落盘与加锁)。行需含 as_of/symbol/entry_zone/invalidation/
    target_zone/risk/ref_close。与自选影子账本 (watchlist_v4) 共用, 两本账同一结算口径。"""
    from app.services.strategy_evaluation import simulate_native_daily, simulate_native_daily_short
    pending = [r for r in rows if (r.get("outcome") or {}).get("status") not in TERMINAL
               and (today - date.fromisoformat(r["as_of"])).days >= SETTLE_AFTER_DAYS]
    if not pending:
        return 0
    syms = sorted({r["symbol"] for r in pending})
    start = min(date.fromisoformat(r["as_of"]) for r in pending) - timedelta(days=7)
    bars = repo.get_daily_batch(syms, start, today, columns=["symbol", "date", "open", "high", "low", "close"])
    if bars is None or bars.is_empty():
        return 0
    by_sym = {g["symbol"][0]: g.sort("date") for g in bars.partition_by("symbol")}
    # 停止交易判定: 全市场最新交易日之后 ≥ STALE_DAYS 个自然日仍没有该股日K → 视为已退市/停牌,
    # 持仓按最后一根收盘平仓 (delisted_exit), 不能永远挂在待结算里 (否则前向样本只剩“活下来的”, 2026-10-06 审查)。
    latest_any = bars["date"].max()
    n = 0
    for r in pending:
        as_of = date.fromisoformat(r["as_of"])
        b = by_sym.get(r["symbol"])
        if b is None:
            continue
        upto = b.filter(b["date"] <= as_of)
        after = b.filter(b["date"] > as_of)
        if after.height == 0:
            continue
        scale = 1.0
        ref = r.get("ref_close")
        if isinstance(ref, (int, float)) and ref > 0 and upto.height:
            now_close = float(upto["close"][-1])
            if now_close > 0 and abs(now_close / ref - 1) > 0.02:
                scale = ref / now_close        # 信号日后发生拆并股: 把后来的复权日K换回快照基准
        stopped = (latest_any is not None and (latest_any - after["date"].max()).days >= STALE_DAYS)
        if after.height < 10 and not stopped:
            continue
        o, h, lo, c = (after[k].to_numpy().astype(float) * scale for k in ("open", "high", "low", "close"))
        sim = simulate_native_daily_short if r.get("side") == "short" else simulate_native_daily
        res = sim(o, h, lo, c, entry_zone=tuple(r["entry_zone"]), invalidation=r["invalidation"],
                  target=tuple(r["target_zone"]), risk=r["risk"],
                  last_index=(after.height - 1) if stopped else None)
        if res.get("status") not in TERMINAL:
            continue
        r["outcome"] = {"status": res["status"], "r": res.get("r"), "exit_reason": res.get("exit_reason"),
                        "resolved_on": today.isoformat(), **({"price_rescaled": round(scale, 6)} if scale != 1.0 else {})}
        n += 1
    return n


def summary(data_dir: Path) -> dict:
    """按来源: 前向已结算 n / 平均 R / 同日同止损随机基线 / 超额 R; 以及待结算数与起始日。"""
    rows = load(data_dir)
    closed = [r for r in rows if (r.get("outcome") or {}).get("status") == "closed" and r["outcome"].get("r") is not None]
    base: dict[tuple, list[float]] = defaultdict(list)
    for r in closed:
        if r["source"] == BASELINE_ID:
            base[(r["as_of"], r.get("stop_loss"))].append(r["outcome"]["r"])
    out: dict[str, dict] = {}
    by_src: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_src[r["source"]].append(r)
    for sid, rs in by_src.items():
        cl = [r for r in rs if (r.get("outcome") or {}).get("status") == "closed" and r["outcome"].get("r") is not None]
        ex = []
        for r in cl:
            b = base.get((r["as_of"], r.get("stop_loss")))
            if b and sid != BASELINE_ID:
                ex.append(r["outcome"]["r"] - statistics.fmean(b))
        out[sid] = {"snapshots": len(rs), "closed": len(cl),
                    "pending": sum(1 for r in rs if (r.get("outcome") or {}).get("status") not in ("closed", "no_fill", "gap_expired")),
                    "mean_r": round(statistics.fmean(r["outcome"]["r"] for r in cl), 4) if cl else None,
                    "excess": round(statistics.fmean(ex), 4) if ex else None, "excess_n": len(ex),
                    "since": min(r["as_of"] for r in rs)}
    return {"sources": out, "rows": len(rows), "since": min((r["as_of"] for r in rows), default=None),
            "baseline_id": BASELINE_ID}


def finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
