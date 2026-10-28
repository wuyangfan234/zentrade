"""机会板做多卡第 1 天盘前越止损作废 (0.27.0, 无限 2026-10-19 选 B “折中”)。

规则: 做多卡在入场窗第 1 天 (信号日后第一个交易日), 09:00 ET 时点的盘前价 (04:00-09:00 最后一根已完成 30 分钟K
收盘, Alpaca SIP 原始价) 已低于止损 -> 整张卡作废 (第 2 天也不挂)。做空不适用 (2023-26 回放只出现过 1 次)。
证据 (frozen offline replay (2026-10)): 这一状态继续挂单的期望 -0.25R [-0.46, -0.08], 训练 2023-24 -0.26 /
留出 2025-26 -0.24 两段同号; 24 张卡里后来成交 6 张, 6 张全亏; 整卡作废账户层 +0.8R/年 [-0.9, +2.5] (两段都不为负)。
第 2 天盘前越止损那一格期望为正 (+0.06R), 所以只看第 1 天。

时点: SIP 免费档扣留最近 15 分钟, 08:30-09:00 那根要 09:16 后才拿得到 -> 定时任务 09:20 / 09:30 / 09:40 ET 取 SIP
(北京夏令时 21:20 起 / 冬令时 22:20 起; 只查还没查过的卡)。09:40 前 SIP 最后一根早于 08:30 的票先不写 (可能还没聚合)。
交易环每轮也补查, 但不出网: 09:44 前不做事, 09:44 起只用本地 09:00 HL 价兜底, 锁被定时任务占着就跳过 (不拖持仓管理)。
纸面 09:45 才开始成交。
取价: SIP 拿到就用; SIP 成功但某票盘前没有成交 -> 用 09:00 盘前跳空任务存下的 Hyperliquid 中间价
(premarket_gap/<日>.json 的 mids, 同一时点); SIP 请求失败 -> 09:44 前不写记录、每分钟最多重试一次, 09:44 起改用
同一份 Hyperliquid 09:00 价; 两边都没有 -> 记 source=none 不作废 (与上线前行为相同, 不因为缺数据把卡作废, 也不让卡一直等)。
记录 data/opportunities/pm_check/<美东日>.json (只追加, 每张卡一条), 结算与机会页读同一份。
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import threading
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

logger = logging.getLogger(__name__)

CHECK_AT = dtime(9, 0)          # 盘前价时点: 09:00 前最后一根 30 分钟K 收盘
READY_AT = dtime(9, 16)         # SIP 延迟 15 分钟: 08:30 那根 09:16 后可取
FALLBACK_AT = dtime(9, 44)      # 09:44 起 SIP 仍拿不到 -> HL; 纸面 09:45 开始成交
LAST_BAR_AT = "08:30"            # 09:00 前最后一根 30 分钟K 的起点
LAST_BAR_GRACE = dtime(9, 40)   # 09:40 前没拿到 08:30 那根 = 可能还没聚合, 等下一次定时任务
PRE_START = dtime(4, 0)
REASON = "premarket_beyond_stop"
PENDING = "premarket_check_pending"
_LOCK = threading.Lock()


def check_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "pm_check"


def check_path(data_dir: Path, day: date) -> Path:
    return check_dir(data_dir) / f"{day.isoformat()}.json"


_LOAD_CACHE: dict = {}


def load(data_dir: Path, day: date) -> dict[str, dict]:
    """{card_id: 记录}。读失败 = 空 (结算/机会页按“没查过”处理, 与上线前相同)。
    准入每帧每卡都会问 -> 按文件 (mtime, size) 缓存, 返回副本。"""
    p = check_path(data_dir, day)
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (st.st_ino, st.st_mtime_ns, st.st_size)       # 原子替换 = 新 inode
    hit = _LOAD_CACHE.get(str(p))
    if hit and hit[0] == key:
        return dict(hit[1])
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    got = raw.get("checked") if isinstance(raw, dict) else None
    out = {k: v for k, v in got.items() if isinstance(v, dict)} if isinstance(got, dict) else {}
    _LOAD_CACHE[str(p)] = (key, out)
    return dict(out)


def first_entry_day(signal_day: date) -> date:
    from app.market_clock import us_clock
    return us_clock.next_trading_day(signal_day)


def guard_state(data_dir: Path, card_id: str, signal_day: date, now: datetime) -> str:
    """准入用: 'void' (已作废) / 'pending' (今天是第 1 天、已到纸面开始成交的 09:45、仍没查过) / 'ok'。
    09:45 前不报 pending: 纸面本来就不成交, 检查最晚 09:44 写完 (SIP 失败也会改用 HL 或记 none);
    提前拦会把卡打回观察、再要等价格回区才重新激活。
    第 2 天起没有第 1 天的记录 (第 1 天系统没运行) -> 'ok' (没有依据, 不作废)。日期算不出 -> 'ok'。"""
    from app.market_clock import us_clock
    from app.market_clock.clock import US_ENTRY_OPEN
    try:
        first = first_entry_day(signal_day)
    except ValueError:
        return "ok"
    rec = load(data_dir, first).get(card_id)
    if rec is not None:
        return "void" if rec.get("void") is True else "ok"
    t = us_clock.market_datetime(now)
    return "pending" if t.date() == first and t.time() >= US_ENTRY_OPEN else "ok"


def due_cards(cards, today: date, done: set[str]) -> list:
    """今天要查的卡: 机会板做多下单簿卡, 今天是它的入场窗第 1 天, 还没成交/结束, 今天还没查过。"""
    from app.opportunity.card import CardState
    from app.services import entry_guard
    out = []
    for c in cards:
        if c.id in done or c.side != "long" or not entry_guard.is_book(c):
            continue
        if not str(c.setup_type or "").endswith(":swing"):
            continue
        if c.state not in (CardState.OPEN, CardState.ACTIVE, CardState.WATCH):
            continue
        try:
            if first_entry_day(c.created_date) != today:
                continue
        except ValueError:
            continue
        out.append(c)
    return out


def _bar_time(b: dict):
    from app.market_clock import us_clock
    return datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00")).astimezone(us_clock.tz)


def sip_last_premarket(symbols: list[str], day: date) -> dict[str, tuple[float, str]]:
    """{sym: (09:00 前最后一根已完成 30 分钟K 的收盘, 该根起点 HH:MM)}。网络/鉴权失败抛异常 (调用方决定重试)。"""
    from app.market_clock import us_clock
    from app.plugins.alpaca import provider as ap
    p = ap.AlpacaProvider()
    if not p.key or not p.secret:
        raise RuntimeError("alpaca credentials missing")
    start = datetime.combine(day, PRE_START, tzinfo=us_clock.tz)
    end = datetime.combine(day, CHECK_AT, tzinfo=us_clock.tz)
    best: dict[str, tuple] = {}
    for sym, b in ap._iter_bars(p.key, p.secret, list(symbols), "30Min", start, end, "raw", "sip"):
        t = _bar_time(b)
        if t.date() != day or not (PRE_START <= t.time() < CHECK_AT):
            continue
        c = b.get("c")
        if not isinstance(c, (int, float)) or not math.isfinite(c) or c <= 0:
            continue
        if sym not in best or t > best[sym][0]:
            best[sym] = (t, float(c))
    return {s: (v[1], v[0].strftime("%H:%M")) for s, v in best.items()}


def gap_mids(data_dir: Path, day: date) -> dict[str, float]:
    """09:00 ET 盘前跳空任务落盘的 Hyperliquid 中间价 (premarket_gap/<日>.json 的 mids)。读不到 = 空。"""
    try:
        raw = json.loads((Path(data_dir) / "premarket_gap" / f"{day.isoformat()}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    mids = raw.get("mids") if isinstance(raw, dict) else None
    if not isinstance(mids, dict):
        return {}
    return {s: float(v) for s, v in mids.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0}


RETRY_S = 60
_LAST_TRY: dict = {}


def us_clock_now_time(fallback_now: datetime):
    """当前美东时刻 (SIP 返回之后再取); 取不到用调用时的 now。测试冻结 us_clock.now 时两者一致。"""
    from app.market_clock import us_clock
    try:
        t = us_clock.market_datetime(us_clock.now())
        return max(t, fallback_now).time() if t.date() == fallback_now.date() else fallback_now.time()
    except Exception:
        return fallback_now.time()


def run(data_dir: Path, cards, now: datetime, *, sip=None, fallback=None, allow_sip: bool = True) -> dict:
    """查 now (美东) 当天到期的卡, 写记录, 返回 {checked, void: [card_id], pending, source}。不碰台账 (调用方作废卡)。
    cards = 当前全部卡 (opp_svc.all_cards)。sip / fallback = 取价函数 (测试替换; None = 模块里的真实取价)。
    allow_sip=False (交易环每轮): 不出网 —— 09:44 前什么都不做 (由 09:20/09:30/09:40 定时任务取 SIP);
    09:44 起只用本地 09:00 HL 价兜底写记录; 锁被定时任务占着 (它可能正卡在 SIP 上) 就本轮跳过, 绝不等。"""
    import time as _time

    from app.market_clock import us_clock
    sip = sip or sip_last_premarket
    fallback = fallback or (lambda syms, day: gap_mids(data_dir, day))
    now = us_clock.market_datetime(now)
    today = now.date()
    if not us_clock.is_trading_day(today) or now.time() < READY_AT:
        return {"checked": 0, "void": [], "pending": 0, "skipped": "not_ready"}
    if not allow_sip and now.time() < FALLBACK_AT:
        return {"checked": 0, "void": [], "pending": 0, "skipped": "scheduled_only"}
    if not _LOCK.acquire(blocking=allow_sip):
        return {"checked": 0, "void": [], "pending": 0, "skipped": "busy"}
    try:
        return _run_locked(data_dir, cards, now, today, sip=sip, fallback=fallback, allow_sip=allow_sip, _time=_time)
    finally:
        _LOCK.release()


def _run_locked(data_dir: Path, cards, now: datetime, today: date, *, sip, fallback, allow_sip: bool, _time) -> dict:
    """run() 持锁后的主体 (见 run)。"""
    done = set(load(data_dir, today))
    due = due_cards(cards, today, done)
    if not due:
        return {"checked": 0, "void": [], "pending": 0}
    syms = sorted({c.symbol for c in due})
    tkey = (str(Path(data_dir).resolve()), today)
    last = _LAST_TRY.get(tkey)
    if allow_sip and last is not None and _time.monotonic() - last < RETRY_S and now.time() < FALLBACK_AT:
        return {"checked": 0, "void": [], "pending": len(due), "skipped": "retry_wait"}
    prices: dict[str, tuple[float, str, str]] = {}
    sip_err = None
    if allow_sip:
        try:
            for s, (px, at) in sip(syms, today).items():
                prices[s] = (px, "sip", at)
        except Exception as e:
            sip_err = type(e).__name__
            logger.warning("swing pm check: SIP premarket unavailable: %s", sip_err)
    t_after = us_clock_now_time(now)          # SIP 可能卡很久 (超时重试): 用返回后的时刻判断
    # 09:40 前: SIP 最后一根早于 08:30 (可能还没聚合完) 的票本轮不写, 等下一次定时任务; 09:40 起接受
    late: set[str] = set()
    if allow_sip and t_after < LAST_BAR_GRACE:
        late = {s for s, v in prices.items() if v[1] == "sip" and v[2] < LAST_BAR_AT}
        for s in late:
            prices.pop(s)
    missing = [s for s in syms if s not in prices and s not in late]
    if missing and sip_err is not None and t_after < FALLBACK_AT:
        _LAST_TRY[tkey] = _time.monotonic()
        return {"checked": 0, "void": [], "pending": len(due), "skipped": f"sip_error:{sip_err}"}
    if missing:
        try:
            fb = fallback(missing, today)
        except Exception as e:
            logger.warning("swing pm check: HL 09:00 fallback unavailable: %s", type(e).__name__)
            fb = {}
        for s in missing:
            px = fb.get(s)
            if isinstance(px, (int, float)) and not isinstance(px, bool) and math.isfinite(px) and px > 0:
                prices[s] = (float(px), "hl_0900", "09:00")
    recs, void = {}, []
    for c in due:
        if c.symbol in late:
            continue
        px = prices.get(c.symbol)
        rec = {"symbol": c.symbol, "side": c.side, "signal_day": c.created_date.isoformat(),
               "stop": float(c.invalidation), "at": now.isoformat(timespec="seconds")}
        if px is None:
            rec.update(price=None, source="none", void=False)
        else:
            rec.update(price=round(px[0], 4), source=px[1], bar_at=px[2], void=bool(px[0] < float(c.invalidation)))
        if rec["void"]:
            void.append(c.id)
        recs[c.id] = rec
    if recs:
        _append(data_dir, today, recs)
    return {"checked": len(recs), "void": void, "pending": len(due) - len(recs),
            "source": sorted({r["source"] for r in recs.values()})}


def _append(data_dir: Path, day: date, recs: dict) -> None:
    from app.services.fs_utils import atomic_write_text
    p = check_path(data_dir, day)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw = raw if isinstance(raw, dict) and isinstance(raw.get("checked"), dict) else None
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError):
        raw = None
    if raw is None:                       # 坏文件: 改名留证, 不覆盖已有记录
        with contextlib.suppress(OSError):
            p.rename(p.with_suffix(f".bad-{datetime.now().strftime('%H%M%S')}"))
        raw = {}
    checked = dict(raw.get("checked") or {})
    for k, v in recs.items():
        checked.setdefault(k, v)          # 只追加: 已查过的卡不改写
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps({"day": day.isoformat(), "rule": REASON, "checked": checked}, ensure_ascii=False))


def void_ids(data_dir: Path, day: date) -> set[str]:
    return {k for k, v in load(data_dir, day).items() if v.get("void") is True}


def premarket_close_m30(data_dir: Path, repo, symbol: str, day: date) -> float | None:
    """回放/结算用: 系统已存的 30 分钟K (kline_4h 数据层, 04:00-20:00) 里 day 当天 04:00-09:00 最后一根收盘,
    复权到系统日K口径 (与 swing_settlement._m30_days 同式)。缺数据 -> None (不作废, 与生产 source=none 相同)。"""
    import polars as pl

    from app.services import kline_4h
    raw = kline_4h.load_m30(data_dir, [symbol], day, day)
    if raw.is_empty():
        return None
    raw = raw.with_columns(pl.col("t").str.to_datetime(time_zone="UTC").dt.convert_time_zone("America/New_York").alias("ts"))
    raw = raw.with_columns(pl.col("ts").dt.date().alias("date"),
                           (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("mm"))
    raw = raw.filter((pl.col("date") == day) & (pl.col("mm") >= 240) & (pl.col("mm") < 540)).sort("ts")
    if raw.is_empty():
        return None
    c = raw["close"][-1]
    if not isinstance(c, (int, float)) or not math.isfinite(c) or c <= 0:
        return None
    adj = kline_4h._adj_factors(repo, [symbol], day, day)
    a = 1.0
    if adj.height:
        v = adj.filter(pl.col("date") == day)["adj"]
        if len(v) and isinstance(v[0], (int, float)) and math.isfinite(v[0]) and v[0] > 0:
            a = float(v[0])
    return float(c) * a
