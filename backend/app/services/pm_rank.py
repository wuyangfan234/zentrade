"""盘前定稿 + 同向放量排前 (无限 2026-10-21 19:40 SGT 授权, 交易类: 只改已出卡的优先级)。

规则 (与 offline premarket evaluation 回放同口径):
  对入场窗第 1 天的机会板卡 (信号日 = 前一交易日), 09:00 ET 时点:
    盘前量比 = 当日 04:00-09:00 盘前累计量 (SIP 30 分钟K, 起点 04:00..08:30) / 前 20 个交易日同窗均量 (有效日 >= 10)
    盘前缺口 = 方向 x (09:00 前最后一根 30 分钟K 收盘 - 信号日收盘) / 信号日 ATR14   (顺向为正)
    排前 = 量比 > 2 且 缺口 > 0。
  只重排: 机会板同组内排前; 交易环同一轮内先尝试排前的卡 (每日开仓名额被占满时它们先拿到)。
  不新增、不作废、不改入场区/止损/目标/仓位/结算; 数据缺失 = 原序。
  展示: 卡面一行 (缺口、量比、隔夜新闻条数) + 顶栏「盘前定稿 09:20」(含盘前 SPY 变化)。
回放 (70/30, 验证段 2025-08-25 起): 现行 54.9 R/年 -> 63.0; 差值 训练 +3.3 [-6.1, +13.1] / 验证 +8.1 [-2.2, +22.9] (区间跨 0)。
记录 data/opportunities/pm_rank/<美东日>.json, 定稿后不改写。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

logger = logging.getLogger(__name__)

CONTRACT = "pm-rank-v1"
PRE_START = dtime(4, 0)
PRE_END = dtime(9, 0)            # 盘前窗口终点 (最后一根 = 08:30 起的 30 分钟K)
SHOW_AT = dtime(9, 20)           # 顶栏「盘前定稿 09:20」(SIP 免费档扣留 15 分钟: 08:30 那根 09:16 后才全)
BASE_DAYS = 20
MIN_BASE = 10
PVR_MIN = 2.0


def path(data_dir: Path, day: date) -> Path:
    return Path(data_dir) / "opportunities" / "pm_rank" / f"{day.isoformat()}.json"


def load(data_dir: Path, day: date) -> dict | None:
    try:
        raw = json.loads(path(data_dir, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get("cards"), dict) else None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def vol_ratio(today_vol, base_vols: list) -> float | None:
    tv = _num(today_vol)
    base = [b for b in (base_vols or [])[-BASE_DAYS:] if (_num(b) or 0) > 0]
    if not tv or tv <= 0 or len(base) < MIN_BASE:
        return None
    return tv / (sum(base) / len(base))


def gap_atr(side: str, pre_price, close, atr) -> float | None:
    p, c, a = _num(pre_price), _num(close), _num(atr)
    if p is None or c is None or a is None or a <= 0 or p <= 0 or c <= 0:
        return None
    return (1 if side == "long" else -1) * (p - c) / a


def is_priority(pvr, gap) -> bool:
    return pvr is not None and gap is not None and pvr > PVR_MIN and gap > 0


def compute(book_cards: list[dict], *, pre: dict, base: dict, spy_prev_close=None, news: dict | None = None) -> dict:
    """纯函数。book_cards = 下单簿卡 (id/symbol/side/evidence.ref_close/atr14); pre = {sym: (09:00 前最后收盘, 盘前累计量)}。"""
    news = news or {}
    out = {}
    for c in book_cards:
        sym, side, cid = c.get("symbol"), c.get("side"), c.get("id")
        if not (isinstance(sym, str) and side in ("long", "short") and isinstance(cid, str)):
            continue
        ev = c.get("evidence") or {}
        px, vol = (pre.get(sym) or (None, None))
        g = gap_atr(side, px, ev.get("ref_close"), ev.get("atr14"))
        r = vol_ratio(vol, base.get(sym) or [])
        out[cid] = {"symbol": sym, "side": side, "gap_atr": None if g is None else round(g, 2),
                    "pvr": None if r is None else round(r, 2), "news_n": int(news.get(sym) or 0),
                    "priority": is_priority(r, g)}
    spy = (pre.get("SPY") or (None, None))[0]
    sp, pc = _num(spy), _num(spy_prev_close)
    return {"contract": CONTRACT, "cards": out, "spy_chg": (sp / pc - 1) if sp and pc else None}


def save(data_dir: Path, day: date, rec: dict) -> bool:
    """定稿只写一次 (已有文件不改写)。"""
    p = path(data_dir, day)
    if p.exists():
        return False
    from app.services.fs_utils import atomic_write_text
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps({"day": day.isoformat(), **rec}, ensure_ascii=False, indent=1))
    return True


def priority_ids(data_dir: Path, day: date) -> set[str]:
    rec = load(data_dir, day)
    if not rec:
        return set()
    return {cid for cid, v in rec["cards"].items() if isinstance(v, dict) and v.get("priority") is True}


def reorder(cards: list, prio: set[str], key=lambda c: c.get("card_id")) -> list:
    """稳定重排: 排前的卡在前, 其余原序。只是置换, 不增减、不改卡。"""
    if not prio:
        return list(cards)
    return sorted(cards, key=lambda c: 0 if key(c) in prio else 1)


def entry_order(cards, data_dir: Path, day: date) -> list:
    """交易环同一轮的尝试顺序 (OpportunityCard 对象)。读不到 = 原序。"""
    try:
        prio = priority_ids(data_dir, day)
    except Exception:  # noqa: BLE001 — 排序失败只回到原序, 不影响开仓
        prio = set()
    return reorder(list(cards), prio, key=lambda c: getattr(c, "id", None))


# ── 取数 (定时任务里调用; 只读本地 30 分钟K 历史 + SIP 当日盘前 + 本地新闻) ─────────────────────
def _base_volumes(data_dir: Path, symbols: list[str], day: date) -> dict[str, list[float]]:
    import polars as pl

    from app.market_clock import us_clock
    from app.services import kline_4h
    days, d = [], day
    while len(days) < BASE_DAYS:
        d = us_clock.previous_trading_day(d)
        days.append(d)
    days = sorted(days)
    raw = kline_4h.load_m30(data_dir, symbols, days[0], days[-1])
    if raw.is_empty():
        return {}
    raw = raw.with_columns(pl.col("t").str.to_datetime(time_zone="UTC").dt.convert_time_zone("America/New_York").alias("ts"))
    raw = raw.with_columns(pl.col("ts").dt.date().alias("date"),
                           (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("mm"))
    raw = raw.filter((pl.col("mm") >= 240) & (pl.col("mm") < 540) & pl.col("date").is_in(days))
    agg = raw.group_by(["symbol", "date"]).agg(pl.col("volume").sum())
    m: dict[tuple, float] = {(s, dd): float(v) for s, dd, v in agg.iter_rows()}
    return {s: [m.get((s, dd), 0.0) for dd in days] for s in symbols}


def _sip_premarket(symbols: list[str], day: date) -> dict[str, tuple[float, float]]:
    from app.market_clock import us_clock
    from app.plugins.alpaca import provider as ap
    from app.services.swing_pm_check import _bar_time
    p = ap.AlpacaProvider()
    if not p.key or not p.secret:
        raise RuntimeError("alpaca credentials missing")
    start = datetime.combine(day, PRE_START, tzinfo=us_clock.tz)
    end = datetime.combine(day, PRE_END, tzinfo=us_clock.tz)
    last: dict[str, tuple] = {}
    vol: dict[str, float] = {}
    for sym, b in ap._iter_bars(p.key, p.secret, list(symbols), "30Min", start, end, "raw", "sip"):
        t = _bar_time(b)
        if t.date() != day or not (PRE_START <= t.time() < PRE_END):
            continue
        vol[sym] = vol.get(sym, 0.0) + float(b.get("v") or 0)
        c = _num(b.get("c"))
        if c and (sym not in last or t > last[sym][0]):
            last[sym] = (t, c)
    return {s: (last[s][1] if s in last else None, vol.get(s, 0.0)) for s in set(last) | set(vol)}


def _overnight_news(data_dir: Path, symbols: list[str], day: date) -> dict[str, int]:
    from app.market_clock import us_clock
    from app.services import news_store
    a = datetime.combine(us_clock.previous_trading_day(day), dtime(16, 0), tzinfo=us_clock.tz)
    b = datetime.combine(day, PRE_END, tzinfo=us_clock.tz)
    out: dict[str, int] = {}
    want = set(symbols)
    for row in news_store.load_recent(data_dir, days=5, now=b):
        dt = news_store._parse(row.get("created_at"))
        if not dt or not (a <= dt < b):
            continue
        for s in set(row.get("symbols") or []) & want:
            out[s] = out.get(s, 0) + 1
    return out


def first_day_cards(data_dir: Path, day: date) -> list[dict]:
    """入场窗第 1 天的下单簿卡 (信号日 = day 的前一交易日)。"""
    from app.market_clock import us_clock
    from app.services import watchlist_book as wb
    sday = us_clock.previous_trading_day(day)
    try:
        raw = json.loads(wb.book_path(data_dir, sday).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [c for c in (raw.get("cards") or []) if isinstance(c, dict) and "book-swing" in str(c.get("id"))]


def run(data_dir: Path, repo, now: datetime) -> dict:
    """09:20 / 09:30 / 09:40 ET 定时任务调用 (幂等; 已定稿直接返回)。失败不抛, 不影响任何交易流程。"""
    data_dir = Path(data_dir)
    day = now.date()
    if path(data_dir, day).exists():
        return {"status": "exists"}
    if now.time() < dtime(9, 16):
        return {"status": "too_early"}
    cards = first_day_cards(data_dir, day)
    syms = sorted({c["symbol"] for c in cards if isinstance(c.get("symbol"), str)})
    try:
        pre = _sip_premarket(syms + ["SPY"], day)
    except Exception as e:  # noqa: BLE001
        logger.warning("pm_rank: sip failed: %s", type(e).__name__)
        return {"status": "sip_failed"}
    try:
        base = _base_volumes(data_dir, syms, day) if syms else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("pm_rank: base volumes failed: %s", type(e).__name__)
        base = {}
    try:
        news = _overnight_news(data_dir, syms, day) if syms else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("pm_rank: news failed: %s", type(e).__name__)
        news = {}
    spy_prev = None
    try:
        from app.market_clock import us_clock
        prev = us_clock.previous_trading_day(day)
        df = repo.get_daily("SPY", prev, prev, columns=["date", "close"]) if repo is not None else None
        if df is not None and df.height:
            spy_prev = float(df["close"][-1])
    except Exception as e:  # noqa: BLE001
        logger.warning("pm_rank: spy prev close failed: %s", type(e).__name__)
    rec = compute(cards, pre=pre, base=base, spy_prev_close=spy_prev, news=news)
    rec["at"] = now.isoformat(timespec="minutes")
    save(data_dir, day, rec)
    return {"status": "saved", "cards": len(rec["cards"]), "priority": sum(v["priority"] for v in rec["cards"].values())}


def view(data_dir: Path, now: datetime) -> dict | None:
    """机会页顶栏 + 卡面一行 (09:20 ET 起, 当日已定稿才给)。"""
    if now.time() < SHOW_AT:
        return None
    rec = load(data_dir, now.date())
    if not rec:
        return None
    return {"at": "09:20", "spy_chg": rec.get("spy_chg"), "cards": rec["cards"]}
