# ruff: noqa: RUF001, RUF002
"""机会页状态投影 (0.39.0 起; 0.41.0 雷达图退役, 本模块只供状态栏 + 持仓 + 名额 + 帧推送)。

只做展示层投影, 不改交易环 / 准入 / 许可 / 下单簿 / 台账 / 价位:
- view(): /page 的 radar_view 段。纯读: 机会板 / 盘中强势 / 开盘榜 / 跳空 四段 + 台账快照 + 当天雷达帧。
  一只自选票只给一个主状态 (平仓 > 持仓 > 挂单 > 受限 > 观察 > 静默), 环 = 离动作多近:
  0 执行 (在挂单区 / 本时段平仓) · 1 临近 (<= 0.5 ATR / 下一交易日平仓 / 距止损 <= 0.5 ATR) · 2 等待 · 3 观察 · 4 静默。
- 名额镜像与 entry_guard 同式 (book_caps / sector_side_count / opens_today / position_cap); 只展示, 不比交易环宽。
- 名额预警 (参照 openscope AircraftConflict: 间隔 + 缓冲即预警, 分离后撤销): 同一板块同方向名额,
  距挂单区 <= 0.25 ATR 的候选多于空位即「预警」; 连续两次评估不再多于空位才撤销。
- 帧 (write_frame): 定时任务 (zen_radar_frame, 15 秒) 发现状态变化时追加一行到
  data/opportunities/radar_frames/<美东日>.jsonl, 并经已有的 /api/intraday/stream 推 opp_radar_updated。
  帧用于: 状态刚变化的时刻 / 名额满的时段 / 页面立即刷新 (SSE)。
文案不在后端拼: 只给代码与数字, 页面 (opportunities/terms.ts) 统一用词。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time as _time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONTRACT = "opp-radar-v1"
RING_EXEC, RING_NEAR, RING_WAIT, RING_WATCH, RING_QUIET = 0, 1, 2, 3, 4
NEAR_ATR = 0.5                 # 环 1 的距离上限 (= swing_state_odds 的 0.5 档)
CAUTION_ATR = 0.25             # 名额预警: 距挂单区 <= 0.25 ATR 的候选
FRAMES_SUBDIR = ("opportunities", "radar_frames")
KEEP_FILES = 20                # 帧文件保留最近 20 个美东日
HEARTBEAT_S = 300              # 状态未变也每 5 分钟落一帧 (尾迹 / 色带时刻)
SCALE_GAP = 7                  # 12 点方向留 7 格写环名
WATCH_ORDER = ("intraday", "open_pick", "gap", "open_vol", "heat", "near")
KIND_RANK = {"exit": 0, "hold": 1, "order": 2, "blocked": 3, "watch": 4, "quiet": 5}
CAP_CODES = frozenset({"sector_side_cap", "daily_open_cap", "max_positions", "portfolio_risk"})
FRESH_FRAME_S = 120            # 最近一帧超过 2 分钟 (帧任务没在跑) 时, 不把“与上一帧不同”标成刚变化
_GATE_CACHE: dict[str, tuple[int, str | None]] = {}


# ── 帧文件 ─────────────────────────────────────────────────────────

def frames_dir(data_dir: Path) -> Path:
    return Path(data_dir).joinpath(*FRAMES_SUBDIR)


def frame_path(data_dir: Path, day: date) -> Path:
    return frames_dir(data_dir) / f"{day.isoformat()}.jsonl"


def load_frames(data_dir: Path, day: date) -> list[dict]:
    """当天帧 (只读)。坏行 / 写到一半的末行跳过; 文件不存在 = 空。"""
    p = frame_path(data_dir, day)
    out: list[dict] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            f = json.loads(ln)
        except ValueError:
            continue
        if isinstance(f, dict) and isinstance(f.get("at"), str) and isinstance(f.get("b"), list):
            out.append(f)
    return out


def _frame_state(frame: dict) -> dict[str, tuple]:
    """帧里每只票的 (kind, ring, r, d)。"""
    out = {}
    for row in frame.get("b") or []:
        if isinstance(row, list) and len(row) >= 5 and isinstance(row[0], str):
            out[row[0]] = (row[1], row[2], row[3], row[4])
    return out


# ── 小工具 ─────────────────────────────────────────────────────────

def _num(v) -> float | None:
    import math
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _hm(iso: str | None) -> str | None:
    return iso[11:16] if isinstance(iso, str) and len(iso) >= 16 else None


def _et(now: datetime | None):
    from app.market_clock import us_clock
    return us_clock.market_datetime(now or us_clock.now())


def _at_et(iso: Any) -> datetime | None:
    from app.market_clock import us_clock
    try:
        return us_clock.market_datetime(str(iso))
    except (TypeError, ValueError):
        return None


def zone_pos(side: str, price, zone, atr) -> tuple[str | None, float | None]:
    """现价相对挂单区: ("in" | "wait" | "past", 区外距离 ATR)。与交易环成交条件同式 (lo <= 价 <= hi)。

    审计 2026-10-26 H-4: 旧口径只看 dist_atr <= 0 就画成「在挂单区 · 执行」, 但价格穿过整个区
    (多头跌破区下沿 / 空头涨破区上沿) 后交易环不会成交。"past" 的距离 = 离区内侧边的 ATR 数 (回区要走多远)。
    """
    p, a = _num(price), _num(atr)
    try:
        lo, hi = (_num(x) for x in zone)
    except (TypeError, ValueError):
        return None, None
    if p is None or lo is None or hi is None:
        return None, None
    if lo <= p <= hi:
        return "in", 0.0
    wait = p > hi if side == "long" else p < lo
    if a is None or a <= 0:
        return ("wait" if wait else "past"), None
    gap = (p - hi) / a if p > hi else (lo - p) / a
    return ("wait" if wait else "past"), round(gap, 2)


def r_order(d: float, past_gap: float | None = None) -> tuple[int, float]:
    """挂单卡: (环, 半径比例)。在区越深入越靠圆心; 越过挂单区 (past_gap 非 None) 不进执行环,
    按回区距离与等待卡同口径: <= 0.5 ATR 临近, 更远放等待。"""
    if past_gap is not None:
        g = max(past_gap, 0.0)
        if g <= NEAR_ATR:
            return RING_NEAR, round(0.40 + 0.18 * (g / NEAR_ATR), 3)
        return RING_WAIT, round(0.64 + 0.12 * min((g - NEAR_ATR) / 1.5, 1), 3)
    if d <= 0:
        return RING_EXEC, round(0.31 - 0.18 * min(-d / 0.25, 1), 3)
    if d <= NEAR_ATR:
        return RING_NEAR, round(0.38 + 0.20 * (d / NEAR_ATR), 3)
    return RING_WAIT, round(0.64 + 0.12 * min((d - NEAR_ATR) / 1.5, 1), 3)


def r_hold(ring: int, stop_dist: float | None) -> float:
    if ring == RING_EXEC:
        return 0.22
    if ring == RING_NEAR:
        if stop_dist is not None and stop_dist <= NEAR_ATR:
            return round(0.38 + 0.20 * min(max(stop_dist / NEAR_ATR, 0), 1), 3)
        return 0.50
    sd = stop_dist if stop_dist is not None else 3.0
    return round(0.64 + 0.12 * min(max((sd - NEAR_ATR) / 2.5, 0), 1), 3)


def blocked_codes(card: dict) -> list[str]:
    """卡为何此刻不下单: blocked / verdict_reason + 交易环准入原因 (paper_reason = '准入阻断：a；b')。"""
    codes: list[str] = []
    b = card.get("blocked")
    if b and b != "paper_watch":
        codes.append(str(b))
    if b == "paper_watch":
        txt = str(card.get("paper_reason") or "")
        if "：" in txt:
            txt = txt.split("：", 1)[1]
        parts = [p.strip() for p in txt.replace(";", "；").split("；") if p.strip()]
        codes.extend(parts or ["paper_watch"])
    if not b and card.get("verdict") == "skip" and card.get("verdict_reason"):
        codes.append(str(card["verdict_reason"]))
    return codes


# ── 方位: 自选分组固定扇区 ─────────────────────────────────────────

def bearings(rows: list[dict], groups: list[dict], extra: list[str] = ()) -> tuple[dict[str, float], dict[str, str], list[dict]]:
    """({票: 方位角}, {票: 分组名}, [{name, a0, a1}])。每只票取第一个非「核心」分组 (按分组表顺序),
    只在「核心」的票归「核心」; 不在自选里却有持仓 / 挂单的票归「其他」。12 点方向留 SCALE_GAP 格。"""
    order = [g.get("name") for g in groups if g.get("name")]
    gid = {g.get("id"): g.get("name") for g in groups}
    prim: dict[str, str] = {}
    for r in rows:
        s = r.get("symbol")
        if not isinstance(s, str):
            continue
        names = [gid.get(i) for i in (r.get("group_ids") or []) if gid.get(i)]
        rest = sorted((n for n in names if n != "核心"), key=lambda n: order.index(n) if n in order else 999)
        prim[s] = rest[0] if rest else (names[0] if names else "核心")
    for s in extra:
        prim.setdefault(s, "其他")
    seq = [n for n in order if n != "核心"] + ["核心", "其他"]
    seq += sorted({g for g in prim.values() if g not in seq})
    members = {g: sorted(s for s, gg in prim.items() if gg == g) for g in seq}
    slots = [(g, max(len(members[g]), 2)) for g in seq if members[g]]
    if not slots:
        return {}, {}, []
    step = 360 / (sum(n for _, n in slots) + len(slots) - 1 + SCALE_GAP + 1)
    cur = (SCALE_GAP + 1) / 2 * step
    out, arcs = {}, []
    for g, n in slots:
        ms = members[g]
        for i, s in enumerate(ms):
            out[s] = round(cur + (i + 0.5) * n / len(ms) * step, 2)
        arcs.append({"name": g, "a0": round(cur, 2), "a1": round(cur + n * step, 2)})
        cur += (n + 1) * step
    return out, prim, arcs


# ── 时钟 / 日程 ────────────────────────────────────────────────────

def _entry_close(d: date) -> dtime:
    """入场截止 = 时间平仓时刻: 正常日 15:30, 半日市收盘前 30 分钟 (market_clock.entry_close)。"""
    from app.market_clock import us_clock
    return us_clock.entry_close(d)


def schedule(d: date) -> list[tuple[str, str]]:
    """一个交易日的事件 (美东 HH:MM, 代码), 升序。"""
    from zoneinfo import ZoneInfo

    from app.market_clock import us_clock
    from app.market_clock.clock import US_ENTRY_OPEN
    from app.services import intraday_strength as ids
    from app.services import open_radar
    from app.services import swing_desk as sd
    start = datetime.combine(d, dtime(*sd.REFRESH_START_CN), tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(us_clock.tz)
    close = us_clock.session_close(d)
    ec = _entry_close(d)
    ev = [(start.strftime("%H:%M"), "board_first"), ("09:00", "gap"), ("09:30", "open"),
          (US_ENTRY_OPEN.strftime("%H:%M"), "entry_open"), (open_radar.READY_AT.strftime("%H:%M"), "open_radar")]
    ev += [(t.strftime("%H:%M"), "intraday_check") for t in ids.CHECKS if t < close]
    ev += [(ec.strftime("%H:%M"), "entry_close"), (close.strftime("%H:%M"), "close")]
    try:
        from app.services.preferences import get_pipeline_schedule
        ps = get_pipeline_schedule()
        ev.append((f"{int(ps['hour']):02d}:{int(ps['minute']):02d}", "pipeline"))
    except Exception:
        ev.append(("16:35", "pipeline"))
    return sorted(ev)


def clock(now: datetime) -> dict:
    """时钟段: 当前时段 + 下一件事 (跨交易日)。北京时间由页面按时区换算。"""
    from app.market_clock import us_clock
    from app.services import card_volume as cv
    d = now.date()
    trading = us_clock.is_trading_day(d)
    hm = now.strftime("%H:%M")
    nxt = None
    if trading:
        nxt = next(({"day": d.isoformat(), "at": t, "code": c} for t, c in schedule(d) if t > hm), None)
    if nxt is None:
        nd = us_clock.next_trading_day(d)
        t, c = schedule(nd)[0]
        nxt = {"day": nd.isoformat(), "at": t, "code": c}
    nxt["ts"] = datetime.combine(date.fromisoformat(nxt["day"]), dtime.fromisoformat(nxt["at"]),
                                 tzinfo=us_clock.tz).isoformat()
    ec = _entry_close(d) if trading else None
    window = bool(trading and dtime(9, 45) <= now.time() <= ec) if ec else False
    session_day = d if trading and now.time() < (ec or dtime(0)) else us_clock.next_trading_day(d)
    return {"et": now.isoformat(timespec="seconds"), "trading_day": trading, "phase": cv.phase(now)["phase"],
            "close": us_clock.session_close(d).strftime("%H:%M") if trading else None,
            "board_first": schedule(d if trading else us_clock.next_trading_day(d))[0][0],
            "entry_window": window, "entry_close": ec.strftime("%H:%M") if ec else None,
            "session_day": session_day.isoformat(), "next": nxt}


# ── 台账只读 ───────────────────────────────────────────────────────

def _ledger(loop, data_dir: Path):
    from app.services import desk_overview as ov
    return ov._account(loop, data_dir)


GATE_READ_ERROR = "_read_error"


def _gate(data_dir: Path) -> str | None:
    """最新卡册的大盘闸 = 交易环开新仓与组合风险上限的依据 (trading_loop: gate 不是 green / yellow 就不开新仓)。
    无卡册 = unavailable; 卡册缺 gate 键 = None (交易环同样不开); 读文件出错 = GATE_READ_ERROR (不镜像拦截)。按 mtime 缓存。"""
    d = Path(data_dir) / "opportunities" / "cards"
    try:
        files = sorted(d.glob("20*.json"))
    except OSError:
        return GATE_READ_ERROR
    if not files:
        return "unavailable"
    p = files[-1]
    try:
        m = p.stat().st_mtime_ns
    except OSError:
        return GATE_READ_ERROR
    hit = _GATE_CACHE.get(str(p))
    if hit and hit[0] == m:
        return hit[1]
    try:
        g = json.loads(p.read_text(encoding="utf-8")).get("gate")
    except (OSError, ValueError, AttributeError):
        return GATE_READ_ERROR
    _GATE_CACHE.clear()
    _GATE_CACHE[str(p)] = (m, g)
    return g


def _round_fills(events: list[dict]) -> dict[str, dict]:
    """每只票当前回合的开仓成交 (最近一次开仓 fill 且之后没有 close)。"""
    out: dict[str, dict] = {}
    for e in events:
        k, sym = e.get("kind"), e.get("symbol")
        if not sym:
            continue
        if k == "fill" and e.get("side") in ("buy", "short"):
            out.setdefault(sym, e)          # 加仓不换回合: 保留本回合第一笔开仓 (原止损 / 计划持有天数)
        elif k == "close":
            out.pop(sym, None)
    return out


def _pending_exits(events: list[dict], fills: dict[str, dict]) -> dict[str, dict]:
    """本回合建仓之后、尚未执行的离场意向 (次日开盘平仓)。"""
    out: dict[str, dict] = {}
    for e in events:
        s = e.get("symbol")
        if e.get("kind") == "exit_intent" and s in fills and (e.get("seq") or 0) > (fills[s].get("seq") or 0):
            out[s] = e
        elif e.get("kind") in ("close",) and s in out:
            out.pop(s, None)
    return out


def _exit_day(entry: date, max_days: int) -> date | None:
    from app.market_clock import us_clock
    days = us_clock.trading_days_between(entry, entry + timedelta(days=max_days * 2 + 14))
    return days[max_days - 1] if len(days) >= max_days else None


# ── 主投影 ─────────────────────────────────────────────────────────

def project(*, now: datetime, swing: dict | None, intraday: dict | None, radar: dict | None, gaps: dict | None,
            positions: list, events: list[dict], account, sectors: dict[str, str] | None, watch_rows: list[dict],
            groups: list[dict], frames: list[dict], gate: str | None, earnings: dict[str, dict] | None = None,
            open_risk: float | None = None, max_days_default: int = 5, board_ticks: list[str] | None = None,
            missing: list[str] | None = None, loop_blocks: dict[str, list[str]] | None = None) -> dict:
    """纯函数 (不读写文件): 由各段数据 + 台账快照 + 当天帧算出雷达视图。
    sectors = None: 板块表不可用 (不做板块名额镜像); {} 或缺键: 与 entry_guard 一致按 sector_unknown 拦截。"""
    from app.market_clock import us_clock
    from app.services import earnings_guard, entry_guard
    from app.services import swing_desk as sd
    from app.services import swing_verdict as sv
    today = now.date()
    ck = clock(now)
    sec_map = sectors or {}
    sw = swing or {}
    cards = [c for c in (sw.get("long") or []) + (sw.get("short") or []) if isinstance(c, dict) and c.get("symbol")]
    risk = _num(sw.get("risk_pct"))
    equity = _num(sw.get("equity"))
    earnings = earnings or {}
    fills = _round_fills(events)
    pend_exit = _pending_exits(events, fills)
    pos_by = {p.symbol: p for p in positions}
    blips: dict[str, dict] = {}

    # ── 持仓 (台账为准: 当前止损读 stop_price, 原止损读本回合开仓 fill) ──
    ref_day = date.fromisoformat(ck["session_day"])
    held_cards = {c["symbol"]: c for c in cards if c.get("state") == "holding"}
    for s, p in pos_by.items():
        c = held_cards.get(s) or {}
        f = fills.get(s) or {}
        side = "short" if p.qty < 0 else "long"
        price = _num(c.get("price")) or _num(p.avg_cost)
        atr = _num(c.get("atr"))
        stop = _num(p.stop_price) if p.stop_price is not None else _num(f.get("stop_price"))
        sd_atr = None
        if price is not None and stop is not None and atr:
            sd_atr = round(((price - stop) if side == "long" else (stop - price)) / atr, 2)
        try:
            entry = date.fromisoformat(str(p.entry_date or f.get("trade_date"))[:10])
        except ValueError:
            entry = today
        max_days = min(int(f.get("max_holding_days") or c.get("hold_days") or max_days_default),
                       int(getattr(account, "max_holding_days", 7) or 7))
        ex_day = _exit_day(entry, max_days)
        intent = pend_exit.get(s)
        if intent is not None:
            ring, exit_code = RING_EXEC, "exit_next_open"
        elif ex_day is not None and ex_day <= ref_day:
            ring, exit_code = RING_EXEC, "time_exit"
        elif ex_day is not None and ex_day == us_clock.next_trading_day(ref_day):
            ring, exit_code = RING_NEAR, "time_exit"
        else:
            ring, exit_code = RING_WAIT, "time_exit"
        if ring == RING_WAIT and sd_atr is not None and sd_atr <= NEAR_ATR:
            ring = RING_NEAR
        if c.get("watch_removed") and intent is None:
            ring = RING_EXEC
            exit_code = "exit_watch_removed"
        # 次开盘平仓的执行日: 离场意向 = 意向交易日的下一交易日; 移出自选 (意向在收盘定版时才写) = 下一个收盘定版日的下一交易日
        open_day = None
        if intent is not None:
            try:
                open_day = us_clock.next_trading_day(date.fromisoformat(str(intent.get("trade_date"))[:10]))
            except ValueError:
                open_day = us_clock.next_trading_day(today)
        elif exit_code == "exit_watch_removed":
            decide = today if us_clock.is_trading_day(today) else us_clock.next_trading_day(today)
            open_day = us_clock.next_trading_day(decide)
        qty = abs(int(p.qty))
        sg = 1 if side == "long" else -1
        pnl = round(sg * (price - float(p.avg_cost)) * qty, 2) if price is not None else None
        r_now = _num(c.get("r_now"))
        rps = _num(c.get("risk")) or _num(f.get("risk_per_share"))
        if r_now is None and price is not None and rps:      # swing_desk 同式: (现价 - 成交价) / 卡面计划每股风险
            r_now = round(sg * (price - float(p.avg_cost)) / rps, 2)
        tz = f.get("target_zone") or c.get("target_zone")
        target = _num(c.get("target"))
        if target is None and isinstance(tz, list) and len(tz) == 2:
            target = _num(tz[1] if side == "short" else tz[0])
        earn = earnings.get(s)
        enote = None
        if earn and earnings_guard.reduction_needed(earn, entry, max_days):
            enote = {"date": str(earn.get("date"))[:10], "timing": earn.get("timing"),
                     "reduce_day": earnings_guard.reduce_day(earn).isoformat()}
        blips[s] = {
            "symbol": s, "side": side, "kind": "exit" if ring == RING_EXEC else "hold", "ring": ring,
            "r": r_hold(ring, sd_atr), "d": None,
            "hold": {"qty": qty, "avg": round(float(p.avg_cost), 4), "price": price,
                     "price_source": c.get("price_source") or "cost", "price_asof": c.get("price_asof"),
                     "stop_now": stop, "stop_initial": _num(f.get("stop_price")),
                     "stop_moved": stop is not None and _num(f.get("stop_price")) is not None
                     and abs(stop - float(f["stop_price"])) > 1e-6,
                     "target": target, "entry_date": entry.isoformat(), "day_n": us_clock.days_held(entry, max(today, entry)),
                     "max_days": max_days, "exit_day": ex_day.isoformat() if ex_day else None,
                     "exit_at": ck["entry_close"] if ex_day == today else _entry_close(ex_day).strftime("%H:%M") if ex_day else None,
                     "exit_code": exit_code, "exit_reason": (intent or {}).get("reason"),
                     "exit_open_day": open_day.isoformat() if open_day else None,
                     "stop_dist_atr": sd_atr, "r_now": r_now, "pnl_usd": pnl,
                     "card_id": p.card_id or f.get("card_id"), "earnings": enote},
        }

    # ── 机会板挂单 (一股一态: 已持仓的票不再单独出挂单点; 同票多张卡取最近的一张) ──
    for c in cards:
        s = c["symbol"]
        if c.get("state") == "holding" or (s in blips and blips[s]["kind"] in ("exit", "hold")):
            continue
        d = sv.dist_atr(c.get("side") or "long", c.get("price"), c.get("entry_zone") or (), c.get("atr"))
        zpos, zgap = zone_pos(c.get("side") or "long", c.get("price"), c.get("entry_zone") or (), c.get("atr"))
        codes = blocked_codes(c)
        if not codes and sectors is not None and not sectors.get(s):
            codes = ["sector_unknown"]              # entry_guard: 板块查不到 = 不开 (fail-closed)
        kind = "blocked" if codes else "order"
        if zpos == "past":
            ring, rr = r_order(d if d is not None else 0.0, zgap if zgap is not None else 0.0)
        else:
            ring, rr = r_order(d) if d is not None else (RING_WAIT, 0.76)
        if kind == "blocked":
            ring, rr = max(ring, RING_NEAR), max(rr, 0.40)
        prev = blips.get(s)
        # 同票多卡取离成交最近的一张: 第三项用区外距离 (越区卡的 d 是大负数, 不能压过只差 0.3 ATR 的等待卡)
        pg = _gap(prev) if prev is not None else None
        cg = _gap({"zone_pos": zpos, "zone_gap": zgap, "d": d})
        if prev is not None and (KIND_RANK[prev["kind"]], prev["ring"], pg if pg is not None else 9) <= \
                (KIND_RANK[kind], ring, cg if cg is not None else 9):
            prev.setdefault("also_cards", []).append(c.get("card_id"))
            continue
        o = c.get("odds") or {}
        try:
            sday = date.fromisoformat(str(c.get("signal_day"))[:10])
            last = sd._last_entry_close(sday).date()
            valid = f"{last.isoformat()}T{_entry_close(last).strftime('%H:%M')}"
        except (TypeError, ValueError):
            valid = None
        earn = earnings.get(s)
        enote = None
        try:
            ed = date.fromisoformat(str(sw.get("entry_day") or today)[:10])
        except ValueError:
            ed = today
        if earn and earnings_guard.reduction_needed(earn, ed, int(c.get("hold_days") or max_days_default)):
            enote = {"date": str(earn.get("date"))[:10], "timing": earn.get("timing"),
                     "reduce_day": earnings_guard.reduce_day(earn).isoformat()}
        lo_hi = c.get("entry_zone") or [None, None]
        qty = o.get("qty")
        if qty is None:
            try:
                qty = sv.plan_qty(equity, risk, c.get("risk"), (float(lo_hi[0]) + float(lo_hi[1])) / 2)
            except (TypeError, ValueError, IndexError):
                qty = None
        risk_usd = o.get("risk_usd")
        if risk_usd is None and qty and _num(c.get("risk")):
            risk_usd = round(qty * float(c["risk"]))
        blips[s] = {
            "symbol": s, "side": c.get("side") or "long", "kind": kind, "ring": ring, "r": round(rr, 3), "d": d,
            "zone_pos": zpos, "zone_gap": zgap, "codes": codes,
            "order": {"card_id": c.get("card_id"), "zone": lo_hi, "stop": _num(c.get("stop")), "target": _num(c.get("target")),
                      "qty": qty, "risk_usd": risk_usd, "price": _num(c.get("price")), "price_source": c.get("price_source"),
                      "price_asof": c.get("price_asof"), "dist_pct": _num(c.get("dist_pct")), "atr": _num(c.get("atr")),
                      "signal_day": c.get("signal_day"), "day_n": c.get("entry_day_n"), "entry_days": c.get("entry_days"),
                      "valid_until": valid, "rules": [x.get("name") for x in c.get("strategies") or [] if isinstance(x, dict)],
                      "gate": c.get("gate"), "beyond_stop": bool(c.get("beyond_stop")),
                      "odds": {k: o.get(k) for k in ("fill", "ev_r", "ev_usd", "ci_r", "ci_usd", "sig", "n")} if o else None,
                      "sector": sec_map.get(s), "earnings": enote},
        }

    # ── 观察层 (只展示, 不下单): 盘中强势只记录卡 / 开盘点名 / 跳空 / 开盘放量 / 放量榜 / 差一步 ──
    notes: dict[str, list[dict]] = {}

    def note(sym, src, **kw):
        if isinstance(sym, str):
            notes.setdefault(sym, []).append({"src": src, **kw})
    for c in (intraday or {}).get("active") or []:
        if isinstance(c, dict):
            note(c.get("symbol"), "intraday", r_now=_num(c.get("r_now")), day_n=c.get("day_n"), hold_days=c.get("hold_days"))
    rd = radar or {}
    if rd.get("is_today"):
        for r in rd.get("picks") or []:
            note(r.get("symbol"), "open_pick", rvol=_num(r.get("rvol")), rank=r.get("rank"), chg=_num(r.get("or_chg_pct")))
        for r in rd.get("others") or []:
            note(r.get("symbol"), "open_vol", rvol=_num(r.get("rvol")), chg=_num(r.get("or_chg_pct")))
    for r in (gaps or {}).get("rows") or []:
        note(r.get("symbol"), "gap", pct=_num(r.get("gap_pct")))
    checks = [x for x in (intraday or {}).get("checks_today") or [] if isinstance(x, dict)]
    if checks and str((intraday or {}).get("today") or today.isoformat()) == today.isoformat():
        for h in (checks[-1].get("heat") or []):
            note(h.get("symbol"), "heat", rvol=_num(h.get("rvol")), chg=_num(h.get("chg")))
        for x in checks:
            for n in x.get("near") or []:
                note(n.get("symbol"), "near", why=n.get("why"), check=x.get("check"))
    for s, ns in notes.items():
        ns.sort(key=lambda n: WATCH_ORDER.index(n["src"]))
        uniq, seen = [], set()
        for n in ns:
            if n["src"] not in seen:
                uniq.append(n)
                seen.add(n["src"])
        if s in blips:
            blips[s]["also"] = uniq
        else:
            blips[s] = {"symbol": s, "side": None, "kind": "watch", "ring": RING_WATCH, "r": 0.84, "d": None, "also": uniq}

    # ── 方位 + 静默 ──
    wl = {r.get("symbol") for r in watch_rows if isinstance(r.get("symbol"), str)}
    extra = sorted(s for s, b in blips.items() if s not in wl and b["kind"] != "watch")
    bear, prim, _arcs = bearings(watch_rows, groups, extra)
    for s in list(blips):
        if s not in bear:                       # 不在自选的观察票不上图 (只在读数的观察组)
            blips[s]["off_scope"] = True
    for s in wl:
        if s not in blips:
            blips[s] = {"symbol": s, "side": None, "kind": "quiet", "ring": RING_QUIET, "r": 0.94, "d": None}
    for s, b in blips.items():
        b["bearing"], b["group"], b["sector"] = bear.get(s), prim.get(s), sec_map.get(s)

    # ── 帧对比: 刚变化 / 接近远离 / 尾迹 ──
    last_at = _at_et(frames[-1]["at"]) if frames else None
    frames_fresh = last_at is not None and (now - last_at).total_seconds() <= FRESH_FRAME_S
    hist: dict[str, list[tuple]] = {}
    for f in frames:
        st_all = _frame_state(f)
        for s in set(hist) | set(st_all):
            st = st_all.get(s) or ("quiet", RING_QUIET, 0.94, None)
            hist.setdefault(s, []).append((f["at"], *st))
    for s, b in blips.items():
        if b["kind"] == "quiet":
            continue
        h = hist.get(s) or []
        changed = None
        cur = (b["kind"], b["ring"])
        if not frames:
            changed = None                      # 当天还没有帧 (休市 / 首刷前): 不标刚变化
        elif not h or (h[-1][1], h[-1][2]) != cur:
            changed = now.isoformat(timespec="seconds") if frames_fresh else None
        else:
            for i in range(len(h) - 1, -1, -1):
                if (h[i][1], h[i][2]) != cur:
                    changed = h[i + 1][0]
                    break
            else:
                changed = None                  # 当天首帧起就是这个状态: 没有观察到的变化
        b["changed_at"] = changed
        if b.get("d") is not None:
            prev_d = next((row[4] for row in reversed(h) if row[4] is not None and abs(row[4] - b["d"]) > 0.04), None)
            b["dir"] = None if prev_d is None else ("in" if b["d"] < prev_d else "out")

    # ── 名额镜像 (entry_guard 同式) ──
    day_cap, sector_cap = entry_guard.book_caps(risk)
    swing_pos = [p for p in positions if entry_guard.is_evo_position(p, sd.RULE_ID)]
    slots: dict[str, dict] = {}
    for p in swing_pos:
        sec = sec_map.get(p.symbol)
        if not sec:
            continue
        k = f"{sec}:{'short' if p.qty < 0 else 'long'}"
        slots.setdefault(k, {"key": k, "sector": sec, "side": "short" if p.qty < 0 else "long", "holders": [], "waiting": []})
        slots[k]["holders"].append(p.symbol)
    for b in blips.values():
        # 名额候选: 挂单, 或只因名额类原因受限的卡 (冷却 / 财报 / 冲突受限的卡今天拿不到名额, 不算候选)
        if not (b["kind"] == "order" or (b["kind"] == "blocked" and set(b.get("codes") or ()) <= CAP_CODES)):
            continue
        sec = sec_map.get(b["symbol"])
        if not sec:
            continue
        k = f"{sec}:{b['side']}"
        slots.setdefault(k, {"key": k, "sector": sec, "side": b["side"], "holders": [], "waiting": []})
        slots[k]["waiting"].append(b["symbol"])
    prev_caps = {c.get("key"): c for c in (frames[-1].get("caps") or [])} if frames else {}
    for k, sl in slots.items():
        sl["holders"].sort()
        sl["waiting"].sort(key=lambda s: (_gap(blips[s]) if _gap(blips[s]) is not None else 9, s))
        sl["used"], sl["cap"] = len(sl["holders"]), sector_cap
        free = max(sector_cap - sl["used"], 0)
        near = [s for s in sl["waiting"] if _gap(blips[s]) is not None and _gap(blips[s]) <= CAUTION_ATR]
        sl["near"], sl["free"] = near, free
        pc = prev_caps.get(k) or {}
        # 预警进入: 距区 <= 0.25 ATR 的候选多于空位; 撤销带缓冲: 上一帧已预警时, 距区 <= 0.5 ATR 的候选仍多于空位就保持
        hold_wide = [s for s in sl["waiting"] if _gap(blips[s]) is not None and _gap(blips[s]) <= NEAR_ATR]
        if sl["used"] >= sector_cap and sl["waiting"]:
            state = "full"
        elif free > 0 and (len(near) > free or (pc.get("state") == "caution" and len(hold_wide) > free)):
            state = "caution"
        elif len(sl["waiting"]) > free:
            state = "tight"
        else:
            state = "ok"
        sl["state"] = state
        rel = sorted({h for h in sl["holders"] if blips.get(h, {}).get("kind") == "exit"})
        sl["release_by"] = rel
        sl["release_at"] = ck["entry_close"] if rel and ck["trading_day"] and now.time() < dtime(16, 0) else None
        for s in sl["waiting"]:
            blips[s]["slot"] = k
            if state == "full" and blips[s]["kind"] == "order":      # 名额已满: 交易环此刻同样会以 sector_side_cap 拒开
                _mirror_block(blips[s], "sector_side_cap")
    sev = {"full": 0, "caution": 1, "tight": 2, "ok": 3}
    slot_list = sorted(slots.values(), key=lambda x: (sev[x["state"]], -x["used"], x["key"]))
    try:
        from app.services.entry_guard import Admission
        adm = Admission((), risk or 0.0, False, tier="rule", book=True, evo=True, segregated_rule=sd.RULE_ID)
        port_cap = (min(account.max_portfolio_risk_pct, 0.03) if gate == "yellow" else account.max_portfolio_risk_pct) \
            if gate in ("green", "yellow") else None

        from types import SimpleNamespace
        pos_cap = entry_guard.position_cap(account, adm, port_cap or account.max_portfolio_risk_pct,
                                           SimpleNamespace(open_positions=positions))
    except Exception as e:
        logger.warning("opp radar: position cap unavailable: %s", type(e).__name__)
        port_cap, pos_cap = None, None
    opens = 0
    if ck["trading_day"] and ck["session_day"] == today.isoformat():
        opens = sum(1 for e in events if e.get("kind") == "fill" and e.get("side") in ("buy", "short")
                    and (_at_et(e.get("at")) or now).date() == today)
    caps = {"slots": slot_list, "sector_cap": sector_cap, "risk_pct": risk,
            "opens": {"used": opens, "cap": day_cap, "day": ck["session_day"]},
            "positions": {"used": len(positions), "cap": pos_cap},
            "portfolio_risk": {"used": round(open_risk / equity, 4) if open_risk is not None and equity else None,
                               "cap": port_cap, "gate": gate}}

    # 交易环本轮的拦截 (入场窗内才有意义): '*' = 全局 (熔断 / 保护数据不可用 / 估值不全), 其余按票
    lb = (loop_blocks or {}) if ck["entry_window"] else {}
    glob = [c for c in lb.get("*", []) if c]
    gate_closed = gate not in ("green", "yellow", GATE_READ_ERROR)
    for b in blips.values():
        if b["kind"] != "order":
            continue
        own = [c for c in lb.get(b["symbol"], []) if c]
        if gate_closed:
            _mirror_block(b, "market_gate")
        elif glob:
            _mirror_block(b, glob[0])
        elif own:
            _mirror_block(b, own[0])
    for b in blips.values():
        if b["kind"] == "order":
            if day_cap and opens >= day_cap:
                _mirror_block(b, "daily_open_cap")
            elif pos_cap and len(positions) >= pos_cap:
                _mirror_block(b, "max_positions")
            elif port_cap and equity and open_risk is not None and _num((b.get("order") or {}).get("risk_usd")) \
                    and open_risk + float(b["order"]["risk_usd"]) > equity * port_cap + 1e-6:
                # trader.try_fill_entry 同式: 现有组合风险 + 本笔计划风险 > 权益 x 组合上限 (黄灯 3%) -> portfolio_risk 拒开
                _mirror_block(b, "portfolio_risk")

    # ── 一句话结论 (代码 + 参数; 用词由页面统一) ──
    verdict = _verdict(now, ck, blips, slot_list)

    # ── 泳道 (四个引擎) ──
    lanes = _lanes(now, ck, sw, intraday, radar, gaps, events, frames, board_ticks or [],
                   any(x["state"] == "full" for x in slot_list))
    price_src: dict[str, int] = {}
    for b in blips.values():
        src = (b.get("order") or b.get("hold") or {}).get("price_source")
        if src:
            price_src[src] = price_src.get(src, 0) + 1
    stale_prices = bool(ck["entry_window"] and price_src and not price_src.get("live") and not price_src.get("snapshot"))
    out_blips = sorted(blips.values(), key=lambda b: (b["ring"], KIND_RANK[b["kind"]], _gap(b) if _gap(b) is not None else 9, b["symbol"]))
    return {"contract": CONTRACT, "at": now.isoformat(timespec="seconds"), "clock": ck, "verdict": verdict, "caps": caps,
            "lanes": lanes, "blips": out_blips,
            "price_sources": price_src, "stale_prices": stale_prices, "frames": len(frames),
            "last_frame_at": frames[-1]["at"] if frames else None, "missing": sorted(missing or [])}


def _in_zone(b: dict) -> bool:
    """挂单点此刻是否在挂单区内 (交易环会成交的位置)。旧帧/无 zone_pos 时回退 d <= 0。"""
    zp = b.get("zone_pos")
    if zp is not None:
        return zp == "in"
    return b.get("d") is not None and b["d"] <= 0


def _gap(b: dict) -> float | None:
    """离挂单区多远 (ATR, 区内 = 0): 等待回踩取 d, 越过挂单区取回区距离。用于临近/名额预警排序。"""
    zp = b.get("zone_pos")
    if zp == "in":
        return 0.0
    if zp == "past":
        return b.get("zone_gap")
    d = b.get("d")
    return None if d is None else max(d, 0.0)


def _mirror_block(b: dict, code: str) -> None:
    """名额镜像拦截: 挂单此刻进区也会被交易环以同一原因拒开 (entry_guard / trader 同式)。只改展示状态。"""
    b["kind"] = "blocked"
    b["codes"] = [code]
    b["mirror"] = True
    if b["ring"] == RING_EXEC:
        b["ring"], b["r"] = RING_NEAR, 0.40


def _verdict(now: datetime, ck: dict, blips: dict[str, dict], slots: list[dict]) -> dict:
    from app.market_clock import us_clock
    orders = [b for b in blips.values() if b["kind"] == "order"]
    blocked = [b for b in blips.values() if b["kind"] == "blocked"]
    exits = sorted(b["symbol"] for b in blips.values() if b["kind"] == "exit")
    holds = [b for b in blips.values() if b["kind"] in ("hold", "exit")]
    full = next((s for s in slots if s["state"] == "full"), None)
    caution = next((s for s in slots if s["state"] == "caution"), None)
    nearest = min((b for b in orders if _gap(b) is not None), key=lambda b: (_gap(b), b["d"] if b.get("d") is not None else 0), default=None)
    base = {"orders": len(orders), "blocked": sorted(b["symbol"] for b in blocked), "holds": len(holds), "exits": exits,
            "exit_at": ck["entry_close"],
            "nearest": {"symbol": nearest["symbol"], "d": nearest["d"], "zone_pos": nearest.get("zone_pos"),
                        "zone_gap": nearest.get("zone_gap")} if nearest else None}
    in_zone_blocked = sorted(b["symbol"] for b in blocked if _in_zone(b))
    trading, d = ck["trading_day"], now.date()
    ec = ck["entry_close"]
    if not trading or (trading and now.time() >= us_clock.session_close(d)):
        tight = next((s for s in slots if s["state"] in ("full", "caution", "tight")), None)
        outside = [b["symbol"] for b in orders if not tight or b["symbol"] not in tight["waiting"]]
        max_fill = (min(tight["free"], len(tight["waiting"])) + len(outside)) if tight else len(orders)
        max_fill = min(max_fill, len(orders))          # 受限挂单 (镜像拦截) 不计入可成交笔数
        # 审计 M-1: 下一挂单日 = 下一交易日 (clock.session_day), 不是「下一事件日」(收盘到 16:35 之间下一事件是今天的盘后管道)
        nd = ck["session_day"]
        exits_next = sorted(b["symbol"] for b in holds if (b.get("hold") or {}).get("exit_day") == nd
                            or (b.get("hold") or {}).get("exit_code") in ("exit_next_open", "exit_watch_removed"))
        return {"kind": "idle", "code": "closed", **base, "next_day": nd, "max_fills": max_fill,
                "slot": _slot_brief(tight), "outside": sorted(outside), "exits_next": exits_next}
    if ec and now.time() >= dtime.fromisoformat(ec):
        nd = us_clock.next_trading_day(d).isoformat()
        return {"kind": "idle", "code": "cutoff", **base, "next_day": nd, "slot": _slot_brief(full or caution)}
    if full:
        return {"kind": "warn", "code": "cap_full", **base, "slot": _slot_brief(full), "in_zone": in_zone_blocked}
    if caution:
        return {"kind": "warn", "code": "cap_caution", **base, "slot": _slot_brief(caution)}
    if orders:
        code = "pre_window" if not ck["entry_window"] and now.time() < dtime(9, 45) else "orders"
        return {"kind": "go", "code": code, **base}
    return {"kind": "idle", "code": "no_orders", **base}


def _slot_brief(s: dict | None) -> dict | None:
    if not s:
        return None
    return {k: s.get(k) for k in ("key", "sector", "side", "used", "cap", "free", "holders", "waiting", "near",
                                   "state", "release_by", "release_at")}


def _lanes(now, ck, sw, intraday, radar, gaps, events, frames, ticks, full_now: bool = False) -> dict:
    from app.services import intraday_strength as ids
    from app.services import open_radar
    today = now.date()
    fills = [{"at": _hm(e.get("at")), "symbol": e.get("symbol"), "side": "short" if e.get("side") == "short" else "long"}
             for e in events if e.get("kind") == "fill" and e.get("side") in ("buy", "short")
             and (_at_et(e.get("at")) or now).date() == today]
    full: list[list] = []
    seq = [(_hm(f.get("at")), any(c.get("state") == "full" for c in f.get("caps") or [])) for f in frames
           if (_at_et(f.get("at")) or now).date() == today]
    seq.append((now.strftime("%H:%M"), full_now))          # 当前状态 (还没落帧) 也算一点: 名额恢复时立即收段
    for hm, is_full in seq:
        if is_full and (not full or full[-1][1] is not None):
            full.append([hm, None])
        elif not is_full and full and full[-1][1] is None:
            full[-1][1] = hm
    gp = gaps or {}
    gap_today = bool(gp) and str(gp.get("at", ""))[:10] == today.isoformat()
    rd = radar or {}
    it = intraday or {}
    chk = [c for c in it.get("checks_today") or [] if isinstance(c, dict)]
    chk_today = [c for c in chk if str(c.get("at", ""))[:10] == today.isoformat()]
    return {
        "board": {"ticks": ticks, "window": ["09:45", ck["entry_close"] or "15:30"], "fills": fills, "full": full,
                  "orders": sum(1 for c in (sw.get("long") or []) + (sw.get("short") or []) if c.get("state") != "holding"),
                  "refresh_last": _hm((sw.get("refresh") or {}).get("last_at"))},
        "gap": {"at": _hm(gp.get("at")) if gap_today else None, "live": bool(gp.get("live")), "warming": bool(gp.get("warming")),
                "n": len(gp.get("rows") or []) if gp else 0, "threshold": gp.get("threshold_pct")},
        "open": {"at": _hm(rd.get("at")) if rd.get("is_today") else None, "ready_at": rd.get("ready_at") or open_radar.READY_AT.strftime("%H:%M"),
                 "picks": [r.get("symbol") for r in rd.get("picks") or []] if rd.get("is_today") else [],
                 "eligible": rd.get("eligible") if rd.get("is_today") else None, "push": rd.get("push")},
        "intraday": {"ticks": [c.get("check") for c in chk_today], "schedule": [t.strftime("%H:%M") for t in ids.CHECKS],
                     "fired": sum(len(c.get("fired") or []) for c in chk_today), "active": len(it.get("active") or []),
                     "next": it.get("next_check")},
    }


def _opp_codes(state: str | None, reason: str) -> list[str]:
    """台账 opportunity 事件原因 → 代码 (页面统一用词, 不把后端句子原样上屏)。"""
    if reason.startswith("准入阻断："):
        return [p.strip() for p in reason.split("：", 1)[1].replace(";", "；").split("；") if p.strip()]
    if state == "active":
        if "容量恢复" in reason:
            return ["capacity_restored"]
        if "开盘确认" in reason:
            return ["open_confirmed"]
        return ["activated"]
    if state == "expired":
        return ["entry_window_over"] if "入场窗" in reason else ["expired"]
    return []


# ── 组装: /page 与帧任务共用 ───────────────────────────────────────

def _watch_rows_and_groups() -> tuple[list[dict], list[dict]]:
    from app.services import watchlist
    return watchlist.list_symbols(), watchlist.list_groups()


def collect(data_dir: Path, *, repo=None, loop=None, now: datetime | None = None, swing=None, intraday=None,
            radar=None, gaps=None, earnings: dict | None = None, missing: list[str] | None = None) -> dict:
    """读台账 / 自选 / 板块 / 当天帧, 调 project()。只读。"""
    data_dir = Path(data_dir)
    now = _et(now)
    lg, account = _ledger(loop, data_dir)
    if lg is None:
        from app.ledger.core import Account
        positions, events, account, open_risk = [], [], account or Account(), None
    else:
        positions, events = list(lg.open_positions), lg.read_events()
        open_risk = lg.open_risk()
    try:
        rows, groups = _watch_rows_and_groups()
    except Exception as e:
        logger.warning("opp radar: watchlist unavailable: %s", type(e).__name__)
        rows, groups = [], []
    sectors: dict[str, str] | None = None
    if repo is not None and hasattr(repo, "get_instruments"):
        try:
            from app.services.opportunity_service import OpportunityService
            sectors = OpportunityService(repo).sector_map()      # 交易环同一来源; {} = 读不到 → 与准入一样按未识别处理
        except Exception as e:
            logger.warning("opp radar: sectors unavailable: %s", type(e).__name__)
            sectors = {}
    if earnings is None:
        earnings = {}
        try:
            from app.services.earnings_calendar import EarningsCalendar
            cal = EarningsCalendar(data_dir)
            evs = (cal._read(cal.cache_path) or {}).get("events") or {}          # 只读一次 (event_for 每次都读文件)
            syms = {p.symbol for p in positions} | {c.get("symbol") for c in (swing or {}).get("long", []) + (swing or {}).get("short", [])}
            for s in syms:
                ev = evs.get(s.upper()) if isinstance(s, str) else None
                if isinstance(ev, dict) and ev.get("date"):
                    earnings[s] = ev
        except Exception as e:
            logger.warning("opp radar: earnings unavailable: %s", type(e).__name__)
    ticks: list[str] = []
    try:
        from app.services import swing_desk as sd
        raw = json.loads((sd.live_dir(data_dir) / f"{now.date().isoformat()}.json").read_text(encoding="utf-8"))
        ticks = [str(x.get("at", ""))[11:16] for x in (raw.get("snapshots") or []) if isinstance(x, dict)]
    except (OSError, ValueError, AttributeError):
        ticks = []
    loop_blocks: dict[str, list[str]] = {}
    try:
        if loop is not None and getattr(loop, "_boot", False):
            for x in list(getattr(loop, "entry_blockers", None) or []):
                if isinstance(x, dict) and x.get("reason"):
                    loop_blocks.setdefault(str(x.get("symbol") or "*"), []).append(str(x["reason"]))
            bs = getattr(loop, "breaker_status", None) or {}
            if bs.get("block_new_entries"):
                loop_blocks.setdefault("*", []).insert(0, "circuit_breaker_unavailable" if bs.get("unavailable") else "circuit_breaker_day_halt")
    except Exception as e:
        logger.warning("opp radar: loop blockers unavailable: %s", type(e).__name__)
    return project(now=now, swing=swing, intraday=intraday, radar=radar, gaps=gaps, positions=positions, events=events,
                   account=account, sectors=sectors, watch_rows=rows, groups=groups,
                   frames=load_frames(data_dir, now.date()), gate=_gate(data_dir), earnings=earnings,
                   open_risk=open_risk, board_ticks=ticks, missing=missing, loop_blocks=loop_blocks)


def live_view(data_dir: Path, *, repo=None, loop=None, now: datetime | None = None) -> dict:
    """帧任务用: 自己取四段 (与 /page 同一组函数), 不取新闻 / 期权标注。"""
    from app.services import intraday_strength as ids
    from app.services import open_radar, opp_page
    from app.services import swing_desk as sd
    now = _et(now)
    errors: dict[str, str] = {}
    swing = opp_page._section("swing", lambda: sd.build_live(data_dir, repo=repo, loop=loop), errors)
    intraday = opp_page._section("intraday", lambda: ids.build_live(data_dir, repo=repo, loop=loop), errors)
    stage = opp_page._section("stage", lambda: opp_page.stage_of(now), errors)
    radar = opp_page._section("radar", lambda: open_radar.view(data_dir, repo=repo, now=now), errors)
    gaps = opp_page._section("gaps", lambda: opp_page.gap_board(data_dir, repo, now, swing, (stage or {}).get("key")), errors)
    if swing is None:
        raise RuntimeError(f"swing unavailable: {errors.get('swing')}")
    return collect(data_dir, repo=repo, loop=loop, now=now, swing=swing, intraday=intraday, radar=radar, gaps=gaps,
                   missing=[k for k in ("intraday", "radar", "gaps") if k in errors])


# ── 帧写入 (定时任务专用; 页面请求永不写) ──────────────────────────

def frame_of(view: dict) -> dict:
    b = [[x["symbol"], x["kind"], x["ring"], x["r"], x.get("d")] for x in view.get("blips") or [] if x["kind"] != "quiet"]
    caps = [{"key": s["key"], "used": s["used"], "cap": s["cap"], "free": s["free"], "near": len(s.get("near") or []),
             "state": s["state"]} for s in (view.get("caps") or {}).get("slots") or []]
    lanes = view.get("lanes") or {}
    zp = sorted((x["symbol"], x.get("zone_pos")) for x in view.get("blips") or [] if x.get("zone_pos"))
    sig = {"b": [row[:3] for row in b], "zp": zp, "codes": sorted((x["symbol"], tuple(x.get("codes") or ())) for x in view.get("blips") or []
                                                       if x.get("codes")),
           "caps": [(c["key"], c["used"], c["state"]) for c in caps],
           "lanes": [len((lanes.get("board") or {}).get("fills") or []), (lanes.get("gap") or {}).get("at"),
                     (lanes.get("open") or {}).get("at"), len((lanes.get("intraday") or {}).get("ticks") or [])]}
    digest = hashlib.sha1(json.dumps(sig, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {"at": view["at"], "digest": digest, "b": b, "caps": caps}


def _rotate(data_dir: Path) -> int:
    files = sorted(frames_dir(data_dir).glob("20*.jsonl"))
    n = 0
    for p in files[:-KEEP_FILES]:
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


_WRITE_LOCK = threading.Lock()
_LAST: dict[str, Any] = {}


def write_frame(data_dir: Path, view: dict, *, heartbeat_s: int = HEARTBEAT_S) -> dict | None:
    """状态有变化 (digest 不同) 或距上一帧超过 heartbeat_s 才追加一行。返回 {frame, changed} 或 None (未写)。"""
    data_dir = Path(data_dir)
    fr = frame_of(view)
    day = date.fromisoformat(fr["at"][:10])
    with _WRITE_LOCK:
        prev = load_frames(data_dir, day)
        last = prev[-1] if prev else None
        changed = last is None or last.get("digest") != fr["digest"]
        if not changed:
            try:
                age = (datetime.fromisoformat(fr["at"]) - datetime.fromisoformat(last["at"])).total_seconds()
            except (TypeError, ValueError):
                age = heartbeat_s
            if age < heartbeat_s:
                return None
        p = frame_path(data_dir, day)
        p.parent.mkdir(parents=True, exist_ok=True)
        new_file = not p.exists()
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(fr, ensure_ascii=False, separators=(",", ":")) + "\n")
            fh.flush()
        if new_file:
            _rotate(data_dir)
    return {"frame": fr, "changed": changed, "seq": len(prev) + 1}


def in_frame_window(now: datetime) -> bool:
    """美东交易日, 机会板首刷前 5 分钟到收盘后 10 分钟。"""
    from zoneinfo import ZoneInfo

    from app.market_clock import us_clock
    from app.services import swing_desk as sd
    now = _et(now)
    d = now.date()
    if not us_clock.is_trading_day(d):
        return False
    start = datetime.combine(d, dtime(*sd.REFRESH_START_CN), tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(us_clock.tz)
    end = datetime.combine(d, us_clock.session_close(d), tzinfo=us_clock.tz) + timedelta(minutes=10)
    return start - timedelta(minutes=5) <= now <= end


def _cheap_signature(data_dir: Path, loop, now: datetime) -> tuple:
    """不重算整板的变化探测: 台账水位 + 四个引擎落盘文件的 mtime + 自选 mtime + 上一帧挂单/持仓按热报价算出的环。"""
    day = now.date().isoformat()
    paths = [Path(data_dir) / "opportunities" / "swing_live" / f"{day}.json",
             Path(data_dir) / "opportunities" / "intraday" / f"{day}.json",
             Path(data_dir) / "opportunities" / "radar" / f"{day}.json",
             Path(data_dir) / "premarket_gap" / f"{day}.json",
             Path(data_dir) / "user_data" / "watchlist.parquet"]
    mt = []
    for p in paths:
        try:
            mt.append(p.stat().st_mtime_ns)
        except OSError:
            mt.append(None)
    # 台账: 运行中的交易环在内存里给持仓 (股数 / 当前止损) 与本轮准入拦截; 未启动时退回事件文件 mtime
    led = None
    try:
        if loop is not None and getattr(loop, "_boot", False):
            led = (tuple(sorted((p.symbol, p.qty, p.stop_price) for p in loop.ledger.open_positions)),
                   tuple(sorted({(str(b.get("symbol")), str(b.get("reason"))) for b in (loop.entry_blockers or [])})))
        else:
            led = (Path(data_dir) / "ledger" / "events.jsonl").stat().st_mtime_ns
    except (OSError, AttributeError, TypeError):
        led = None
    rings = []
    geo = _LAST.get("geo") or []
    if geo:
        try:
            from app.api.portfolio import _live_marks
            from app.services import swing_verdict as sv
            marks = _live_marks(sorted({g[0] for g in geo}))
            for s, side, zone, atr in geo:
                m = marks.get(s)
                if m is None:
                    continue
                d = sv.dist_atr(side, float(m[0]), zone, atr)
                zp, zg = zone_pos(side, float(m[0]), zone, atr)
                rings.append((s, None if d is None else (r_order(d, zg if zp == "past" else None)[0], zp)))
        except Exception:
            rings = []
    # 复审: 半日市的入场截止 (12:30) / 收盘 (13:00) 也是状态切换时刻
    hm = now.strftime("%H:%M")
    try:
        from app.market_clock import us_clock
        extra = {us_clock.entry_close(now.date()).strftime("%H:%M"), us_clock.session_close(now.date()).strftime("%H:%M")}
    except Exception:
        extra = set()
    return (led, tuple(mt), tuple(rings), hm in _EVENT_MINUTES or hm in extra)


def tick(data_dir: Path, *, repo=None, loop=None, now: datetime | None = None, notify=None) -> dict:
    """zen_radar_frame (15 秒): 窗口外不做; 便宜签名没变且未到心跳 → 不做; 否则重算一帧, 状态变了就推 SSE。不抛。"""
    try:
        now = _et(now)
        if not in_frame_window(now):
            return {"skipped": "window"}
    except Exception as e:                          # 交易日历不可用: 本轮不落帧, 不向调度器抛
        logger.warning("opp radar frame: window check failed: %s", type(e).__name__)
        return {"error": type(e).__name__}
    try:
        sig = _cheap_signature(data_dir, loop, now)
    except Exception:
        sig = None
    last_t = _LAST.get("t") or 0.0
    if sig is not None and sig == _LAST.get("sig") and _time.monotonic() - last_t < HEARTBEAT_S:
        return {"skipped": "same"}
    try:
        view = live_view(data_dir, repo=repo, loop=loop, now=now)
    except Exception as e:
        logger.warning("opp radar frame: view failed: %s", type(e).__name__)
        return {"error": type(e).__name__}
    _LAST["sig"], _LAST["t"] = sig, _time.monotonic()
    _LAST["geo"] = [(b["symbol"], b["side"], (b.get("order") or {}).get("zone"), (b.get("order") or {}).get("atr"))
                    for b in view["blips"] if b["kind"] in ("order", "blocked") and (b.get("order") or {}).get("atr")]
    try:
        res = write_frame(data_dir, view)
    except Exception as e:
        logger.warning("opp radar frame: write failed: %s", type(e).__name__)
        return {"error": type(e).__name__}
    if res is None:
        return {"written": False}
    _STATS.update(day=now.date().isoformat(), last_at=res["frame"]["at"], frames=res["seq"])
    if res["changed"] and notify is not None:
        try:
            notify({"at": res["frame"]["at"], "seq": res["seq"], "digest": res["frame"]["digest"]})
            _STATS["pushed"] = _STATS.get("pushed", 0) + 1
        except Exception as e:
            logger.warning("opp radar frame: notify failed: %s", type(e).__name__)
    return {"written": True, "changed": res["changed"], "seq": res["seq"]}


_STATS: dict[str, Any] = {}
# 时段边界 (阶段 / 入场窗 / 截止) 那一分钟强制重算一帧, 状态栏与结论按点切换
_EVENT_MINUTES = frozenset({"09:00", "09:30", "09:45", "09:51", "10:30", "15:30", "16:00"})


def status(data_dir: Path, now: datetime | None = None) -> dict:
    """系统状态页一行: 今天帧数 / 最近一帧 / 本进程推送次数。"""
    now = _et(now)
    fr = load_frames(data_dir, now.date())
    return {"day": now.date().isoformat(), "frames": len(fr), "last_at": fr[-1]["at"] if fr else None,
            "window": in_frame_window(now), "pushed": _STATS.get("pushed", 0) if _STATS.get("day") == now.date().isoformat() else 0}
