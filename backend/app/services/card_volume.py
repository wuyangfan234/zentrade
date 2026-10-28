"""盘前量比 + 盘前实时价 + 美东时段 (0.29.1 起只剩机会板与机会页用的这三样; 旧机会卡的共振评分 / 首选 / 盘中确认已退役)。
只读, 不写交易状态。

- phase(): 美东时段 closed / night / premarket / open_wait / session / after。
- premarket(): 今日 04:00 起到现在的盘前成交量 / 过去 10 个交易日同一时刻的盘前累计均量 (Alpaca SIP 15 分钟 K,
  免费档 16 分钟延迟); 非阻塞, 过期后台单飞刷新。机会板卡「盘前量」角标用, 只展示, 不进下单裁决。
- premarket_prices(): Hyperliquid 永续中间价 (盘前实时, 15 秒缓存); 机会板盘前现价兜底。
量能等级: >=2.0 爆量 / >=1.2 放量 / 0.8-1.2 平量 / <0.8 缩量。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from datetime import time as dtime

from app.market_clock import us_clock

logger = logging.getLogger(__name__)

PRE_START = dtime(4, 0)
OPEN = dtime(9, 30)
LEVELS = ((2.0, "surge", "爆量"), (1.2, "up", "放量"), (0.8, "flat", "平量"), (0.0, "down", "缩量"))
_lock = threading.Lock()
_cache: dict = {}
_inflight: set = set()


def _refresh_async(key, fn) -> None:
    """单飞后台刷新: 同一 key 同时只跑一个; 接口请求不等它 (盘前首次取数约 30 秒, 不能卡住页面)。"""
    with _lock:
        if key in _inflight:
            return
        _inflight.add(key)

    def run():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            logger.warning("card volume refresh %s failed: %s", key[0], type(e).__name__)
        finally:
            with _lock:
                _inflight.discard(key)
    threading.Thread(target=run, daemon=True, name=f"card-volume-{key[0]}").start()
PREMARKET_TTL_S = 60           # 盘前量: 1 分钟取一次 (SIP 1 分钟K 增量, 免费档扣留最近 15 分钟)
HL_TTL_S = 15                  # 盘前价: Hyperliquid 永续中间价 15 秒取一次 (实时, 与 SIP 同根收盘偏差中位 5bp)
BASE_TTL_S = 6 * 3600          # 基线 (过去 10/20 日): 当天取一次


def level(ratio: float | None) -> dict | None:
    if ratio is None or math.isnan(ratio) or ratio < 0:
        return None
    for th, code, label in LEVELS:
        if ratio >= th:
            return {"ratio": round(float(ratio), 2), "level": code, "label": label}
    return None


def phase(now: datetime | None = None) -> dict:
    """当前时段 (美东): closed 休市 / premarket 盘前 04:00-09:30 / open_wait 开盘观察 09:30-09:45 /
    session 盘中 09:45-16:00 (纸面入场窗 09:45-15:30) / after 盘后。"""
    now = us_clock.market_datetime(now or us_clock.now())
    d, t = now.date(), now.time()
    if not us_clock.is_trading_day(d):
        return {"phase": "closed", "label": "休市", "at": now.isoformat(timespec="minutes")}
    if t < PRE_START:
        code, label = "night", "盘前未开"
    elif t < OPEN:
        code, label = "premarket", "盘前"
    elif t < dtime(9, 45):
        code, label = "open_wait", "开盘观察"
    elif t < us_clock.session_close(d):      # 半日市 13:00 收盘
        code, label = "session", "盘中"
    else:
        code, label = "after", "已收盘"
    return {"phase": code, "label": label, "at": now.isoformat(timespec="minutes")}


_HL: list = []        # 惰性单例 (HyperliquidProvider 带 listed 缓存)


def _hl_mids(symbols: list[str]) -> dict[str, float]:
    if not _HL:
        from app.plugins.hyperliquid.provider import HyperliquidProvider
        _HL.append(HyperliquidProvider())
    return _HL[0].mids(symbols)


def premarket_prices(symbols: list[str], *, fetch=_hl_mids, block: bool = False) -> dict[str, dict]:
    """{sym: {price, at (HH:MM:SS ET), source: "HL"}}。15 秒缓存; 非阻塞 (首次无缓存时同步取一次, 约 0.5 秒)。"""
    if not symbols:
        return {}
    key = ("hl_mid", tuple(sorted(symbols)))

    def load():
        mids = fetch(list(symbols))
        at = us_clock.market_datetime(us_clock.now()).strftime("%H:%M:%S")
        out = {s: {"price": float(v), "at": at, "source": "HL"} for s, v in mids.items() if v and v > 0}
        with _lock:
            _cache[key] = (time.monotonic(), out)
    with _lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < HL_TTL_S:
        return hit[1]
    if hit and not block:
        _refresh_async(key, load)
        return hit[1]
    try:
        load()
    except Exception as e:
        logger.warning("premarket HL mids unavailable: %s", type(e).__name__)
        return hit[1] if hit else {}
    with _lock:
        return _cache[key][1]


def premarket_prices_cached(symbols: list[str], *, fetch=None) -> dict[str, dict] | None:
    """与 premarket_prices 同一缓存, 但请求里绝不同步出网 (0.34.1 审查): 有缓存给缓存 (过期则后台刷新);
    没有缓存 -> 后台单飞取一次, 本次返回 None (调用方显示“取数中”, 下一轮刷新拿到)。"""
    if not symbols:
        return {}
    key = ("hl_mid", tuple(sorted(symbols)))
    with _lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < HL_TTL_S:
        return hit[1]

    get = fetch or _hl_mids

    def load():
        mids = get(list(symbols))
        at = us_clock.market_datetime(us_clock.now()).strftime("%H:%M:%S")
        out = {s: {"price": float(v), "at": at, "source": "HL"} for s, v in mids.items() if v and v > 0}
        with _lock:
            _cache[key] = (time.monotonic(), out)
    _refresh_async(key, load)
    return hit[1] if hit else None


# ── 盘前量 (Alpaca SIP: 今日 1 分钟K 增量, 基线 15 分钟K) ─────────────

def _premarket_bars(symbols: list[str], start: datetime, end: datetime, tf: str = "15Min") -> dict[str, list[dict]]:
    from app.plugins.alpaca import provider as ap
    p = ap.AlpacaProvider()
    if not p.key or not p.secret:
        return {}
    out: dict[str, list[dict]] = defaultdict(list)
    for sym, b in ap._iter_bars(p.key, p.secret, symbols, tf, start, end, "raw", "sip"):
        out[sym].append(b)
    return out


def _bar_time(b: dict) -> datetime:
    return datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00")).astimezone(us_clock.tz)


def _pre_cum(bars: list[dict], day: date, upto: dtime, width_min: int = 15) -> tuple[float, float | None]:
    """(04:00 → upto 的盘前累计量, 最后一根收盘价)。bars = width_min 分钟K (t = 起点)。
    upto 落在某根K中间时按时间比例计入该根 (基线用 15 分钟K, 今日用 1 分钟K 时比例恒为 0/1)。"""
    s, last, last_t = 0.0, None, None
    up = datetime.combine(day, min(upto, OPEN))
    for b in bars:
        t = _bar_time(b)
        if t.date() != day or not (PRE_START <= t.time() < OPEN):
            continue
        start = datetime.combine(day, t.time())
        if start >= up:
            continue
        frac = min(1.0, (up - start).total_seconds() / (width_min * 60))
        s += float(b.get("v") or 0) * frac
        if frac >= 1.0 and (last_t is None or t > last_t):
            last_t, last = t, b.get("c")
    return s, (float(last) if isinstance(last, (int, float)) and last > 0 else None)


def premarket(symbols: list[str], now: datetime | None = None, *, fetch=_premarket_bars, block: bool = False) -> dict[str, dict]:
    """{sym: {ratio, level, label, volume, base, last, last_at, through}}。只在交易日 04:00 之后有值; 开盘后冻结为整段盘前。
    基线 = 过去 10 个交易日 04:00 → 同一分钟 (开盘后为 09:30) 的盘前累计量均值。失败 → {} (卡上不显示, 不猜)。
    block=False (接口用): 返回已有缓存 (可能稍旧或为空), 过期则后台刷新; 下一次轮询拿到新值。

    为什么量能不用 HL 实时: 2026-10-09 实测 (自选 x 16 日 x 4 个时刻, 对照 SIP 真值的量比):
      SIP 延迟 15 分钟的量比 与 真值 Spearman 0.97-0.996, 等级一致 89-97%, 放量命中 90-98%;
      HL 永续自己的量比      Spearman 0.45-0.50, 等级一致 43-48%, 放量命中 50-56% (一半是误报);
      SIP + HL 拼最后 15 分钟 Spearman 0.94-0.99, 不比直接用延迟的好。
    盘前量是累计量, 15 分钟内很少换等级; 永续合约的成交与股票成交不是一回事, 不能代替。"""
    now = us_clock.market_datetime(now or us_clock.now())
    today = now.date()
    if not symbols or not us_clock.is_trading_day(today) or now.time() < PRE_START:
        return {}
    # SIP 免费档扣留最近 15 分钟: 统计到 now-16min 整分为止 (标出截至时刻, 不冒充实时)
    through = min((now - timedelta(minutes=16)).replace(second=0, microsecond=0),
                  datetime.combine(today, OPEN, tzinfo=us_clock.tz))
    if through.time() <= PRE_START:
        return {}
    key = ("pre", today, tuple(sorted(symbols)))
    with _lock:
        hit = _cache.get(key)
    fresh = hit and (time.monotonic() - hit[0] < PREMARKET_TTL_S or hit[2] >= datetime.combine(today, OPEN, tzinfo=us_clock.tz))
    if fresh:
        return hit[1]
    if not block:
        _refresh_async(key, lambda: _compute_premarket(symbols, today, through, key, fetch))
        return hit[1] if hit else {}
    return _compute_premarket(symbols, today, through, key, fetch)


def _compute_premarket(symbols, today, through, key, fetch) -> dict[str, dict]:
    try:
        days, d = [], today
        while len(days) < 10:
            d = us_clock.previous_trading_day(d)
            days.append(d)
        bkey = ("pre_base", today, tuple(sorted(symbols)))
        with _lock:
            base_hit = _cache.get(bkey)
        if base_hit and time.monotonic() - base_hit[0] < BASE_TTL_S:
            base_bars = base_hit[1]
        else:
            base_bars = defaultdict(list)
            for d in days:      # 每日一个窗口, 只拉盘前 22 根
                got = fetch(symbols, datetime.combine(d, PRE_START, tzinfo=us_clock.tz),
                            datetime.combine(d, OPEN, tzinfo=us_clock.tz))
                for s, bs in got.items():
                    base_bars[s].extend(bs)
            with _lock:
                _cache[bkey] = (time.monotonic(), base_bars)
        # 今日: 1 分钟K 增量 (只取上次截至之后的部分, 每分钟一次约 1 个请求)
        tkey = ("pre_today", today, tuple(sorted(symbols)))
        with _lock:
            th = _cache.get(tkey)
        bars_today, got_to = (th[1], th[2]) if th else ({}, datetime.combine(today, PRE_START, tzinfo=us_clock.tz))
        if through > got_to:
            new = fetch(symbols, got_to - timedelta(minutes=2), through, "1Min")
            for sym, bs in new.items():
                d_ = bars_today.setdefault(sym, {})
                for b in bs:
                    d_[str(b["t"])] = b
            with _lock:
                _cache[tkey] = (time.monotonic(), bars_today, through)
        upto = through.time()
        out = {}
        for s in symbols:
            vol, last = _pre_cum(list((bars_today.get(s) or {}).values()), today, upto, width_min=1)
            base = [_pre_cum(base_bars.get(s, []), d, upto)[0] for d in days]
            base = [x for x in base if x > 0]
            if len(base) < 5:
                continue
            m = sum(base) / len(base)
            lv = level(vol / m) if m > 0 else None
            if lv:
                out[s] = {**lv, "volume": int(vol), "base": int(m), "last": last, "through": through.strftime("%H:%M")}
        with _lock:
            _cache[key] = (time.monotonic(), out, through)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("premarket volume unavailable: %s", type(e).__name__)
        return {}


# ── 盘中量 (TickFlow 累计量 vs 过去 20 日同一时刻的 1 分钟K 累计) ─────────
