"""机会页聚合 (0.29.0): 一次请求给出整页 = 机会板 + 盘中强势 + 大盘条 + 雷达 / 共振 / 今日多空。

- 各区块互相隔离: 某一块算失败, 该块给 null 并在 errors 里写原因, 其余照常返回 (页面分块显示读取失败)。
- 只读: 不写台账、簿、规则文件。
- 板卡标注 (新闻 / 财报 / 盘前 / 期权, card_context) 页面已不显示, /page 不再计算 (ctx 恒为空);
  card_context 只剩开盘放量推送在用 (不取期权, 不预热期权链)。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONTRACT = "opp-page-v1"
EARNINGS_DAYS = 7               # 财报角标: 今天起 7 个自然日内 (与旧自选表同一窗口)
WARM_RETRY_S = 600
_WARM_LOCK = threading.Lock()
_WARM_INFLIGHT: set[str] = set()
_WARM_TRIED: dict[tuple[str, str], float] = {}


def _section(name: str, fn, errors: dict) -> Any:
    try:
        return fn()
    except Exception as e:  # 一块失败不拖垮整页
        logger.warning("opp page: %s failed: %s", name, type(e).__name__)
        errors[name] = type(e).__name__
        return None


def _board_cards(swing: dict | None) -> list[dict]:
    return [*((swing or {}).get("long") or []), *((swing or {}).get("short") or [])]


def _earnings_dates(data_dir: Path, syms: list[str]) -> dict[str, tuple[date, Any]]:
    """财报日历里每只票的下一次财报 (日期, 盘前/盘后); 没有或读不出 → 不给。"""
    from app.services.earnings_calendar import EarningsCalendar
    cal = EarningsCalendar(data_dir)
    out = {}
    for s in syms:
        ev = cal.event_for(s)
        if not ev:
            continue
        try:
            out[s] = (date.fromisoformat(str(ev.get("date"))[:10]), ev.get("timing"))
        except (TypeError, ValueError):
            continue
    return out


def _premarket(data_dir: Path, today: date) -> dict[str, float]:
    """当天 09:00 ET 盘前跳空 (HL 永续价 vs 昨收, premarket_gap 落盘); 不是今天的不给。"""
    from app.services import premarket_gap
    g = premarket_gap.latest(data_dir) or {}
    if str(g.get("day")) != today.isoformat():
        return {}
    out: dict[str, float] = {k: float(v) for k, v in (g.get("gaps") or {}).items() if isinstance(v, (int, float))}
    for a in g.get("alerts") or []:
        if isinstance(a.get("gap_pct"), (int, float)) and isinstance(a.get("symbol"), str):
            out.setdefault(a["symbol"], float(a["gap_pct"]))
    return out


def _warm_options(data_dir: Path, sym: str, today: date) -> None:
    """后台单飞拉一次免费期权链 (写当天缓存)。测试里替换为桩。"""
    key = (sym, today.isoformat())
    with _WARM_LOCK:
        last = _WARM_TRIED.get(key)
        if sym in _WARM_INFLIGHT or (last is not None and time.monotonic() - last < WARM_RETRY_S):
            return
        _WARM_INFLIGHT.add(sym)
        _WARM_TRIED[key] = time.monotonic()
        if len(_WARM_TRIED) > 512:
            for k in [k for k in _WARM_TRIED if k[1] != today.isoformat()]:
                _WARM_TRIED.pop(k, None)

    def run():
        try:
            from app.services import alpaca_options
            alpaca_options.chain(data_dir, sym, today=today, min_fetched=session_open_utc(today))
        except Exception as e:
            logger.warning("opp page: options warm %s failed: %s", sym, type(e).__name__)
        finally:
            with _WARM_LOCK:
                _WARM_INFLIGHT.discard(sym)
    threading.Thread(target=run, daemon=True, name=f"opp-options-{sym}").start()


def session_open_utc(day: date):
    """当天开盘时刻 (带时区)。盘中预热时, 早于它拉的当天缓存 = 开盘前的残值, 要重拉。"""
    from datetime import datetime as _dt

    from app.market_clock import us_clock
    return _dt.combine(day, us_clock.sessions[0].open, tzinfo=us_clock.tz)


def _paid_cache(data_dir: Path, sym: str) -> dict | None:
    """QVeris 付费期权缓存 (qveris_gate._cache_path 同一路径); 只读, 不像 cache_get 那样顺手建目录。"""
    safe = "".join(c for c in sym.upper() if c.isalnum() or c in ".-")[:12]
    p = Path(data_dir) / "qveris" / "cache" / "options_chain" / f"{safe}.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _option_rows(data_dir: Path, sym: str, today: date) -> tuple[list | None, str | None]:
    """与 qveris_data.options_summary(refresh=False) 同一顺序, 但不在请求里出网:
    今天的付费缓存 → 今天的免费缓存 → (没有今天免费缓存: 后台预热免费链) 本次退回更早的付费缓存 → 都没有不给。
    旧付费缓存不能挡住预热, 否则点过付费查询的票会一直显示几周前的 PCR/IV。
    0.45.0 连同拉取时刻一起返回 (盘外 / 盘前拉的链不给 IV); 今天的免费缓存是开盘前拉的, 盘中后台重拉一次。"""
    from app.market_clock import us_clock
    from app.services import alpaca_options, qveris_data
    entry = _paid_cache(data_dir, sym)
    t = qveris_data._fetched_dt(entry) if entry else None

    def paid_rows():
        data = (entry or {}).get("data")
        return (data.get("data") if isinstance(data, dict) else None), (t.isoformat() if t else None)

    if t is not None and t.astimezone(us_clock.tz).date() == today:
        return paid_rows()
    p = alpaca_options._dir(data_dir, sym) / f"{today.isoformat()}.json"
    try:
        free = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        free = None
    if isinstance(free, dict) and free.get("available") and free.get("rows"):
        if us_clock.in_session() and not _iv_ok(free.get("fetched_at")):
            _warm_options(data_dir, sym, today)          # 开盘前拉的: 后台换成盘中的链, 本次先给旧链 (不含 IV)
        return free["rows"], free.get("fetched_at")
    _warm_options(data_dir, sym, today)
    return paid_rows() if entry is not None else (None, None)


def _iv_ok(fetched_at) -> bool:
    from app.market_clock import us_clock
    from app.services import options_analytics
    return options_analytics.iv_visible(fetched_at, us_clock.now())


def _options(data_dir: Path, sym: str, spot: float | None, today: date, earnings_date: date | None) -> dict | None:
    """近月 Put/Call 成交量比 + 财报前平值隐含波动率 (只读缓存)。现价用板卡上的现价 (不出网取报价)。
    0.45.0: 非交易时段 / 链不是盘中拉的 → 不给 IV (PCR 只用成交量, 照给)。"""
    from app.services import options_analytics
    rows, fetched_at = _option_rows(data_dir, sym, today)
    if not rows or not spot:
        return None
    a = options_analytics.analyze(rows, spot, today=today, earnings_date=earnings_date)
    if not a.get("ok"):
        return None
    if not _iv_ok(fetched_at):
        a = options_analytics.strip_iv(a)
    pcr = ((a.get("pcr") or {}).get("front") or {}).get("volume")
    iv = (a.get("earnings_move") or {}).get("atm_iv")
    return {"pcr": pcr if isinstance(pcr, (int, float)) else None,
            "iv": iv if isinstance(iv, (int, float)) else None}


def card_context(data_dir: Path, syms: list[str], prices: dict[str, float], today: date) -> tuple[dict, dict]:
    """({票: {news, earnings, premarket_pct, pcr, iv}}, errors); 缺就不给该键。每类数据各自容错。"""
    errors: dict[str, str] = {}
    out: dict[str, dict] = {s: {} for s in syms}
    if not syms:
        return out, errors

    def news():
        from app.services import news_store
        for s, v in news_store.batch_summary(data_dir, syms).items():
            if s in out and isinstance(v, dict):
                out[s]["news"] = {"count_24h": v.get("count_24h"), "tone": v.get("tone")}
    _section("ctx_news", news, errors)
    earn = _section("ctx_earnings", lambda: _earnings_dates(data_dir, syms), errors) or {}
    for s, (d, timing) in earn.items():
        if today <= d <= today + timedelta(days=EARNINGS_DAYS):
            out[s]["earnings"] = {"date": d.isoformat(), "timing": timing}
    pm = _section("ctx_premarket", lambda: _premarket(data_dir, today), errors) or {}
    for s in syms:
        if s in pm:
            out[s]["premarket_pct"] = round(pm[s], 3)

    def opts():
        for s in syms:
            if s not in prices:              # 0.34.0: 只给板卡取期权 (开盘榜 / 放量榜的票只要新闻与财报, 不为它们预热期权链)
                continue
            try:
                o = _options(data_dir, s, prices.get(s), today, (earn.get(s) or (None,))[0])
            except Exception as e:  # 单票期权坏了只丢这只
                logger.warning("opp page: options %s failed: %s", s, type(e).__name__)
                o = None
            if o:
                out[s].update({k: v for k, v in o.items() if v is not None})
    _section("ctx_options", opts, errors)
    return out, errors


def _settings(data_dir: Path) -> dict:
    """齿轮现值: 当前 swing.json 的 3+3 (不是板上冻结的那套) + 规则集单笔风险。"""
    from app.services import swing_desk as sd
    from app.services import watchlist_v4 as wv
    lg, sh = sd.strategies_from_ruleset(data_dir)
    from app.services import open_radar
    rs = wv.current_ruleset(data_dir)
    from app.opportunity import direction_gate
    gate = direction_gate.active(data_dir)
    return {"long": list(lg), "short": list(sh), "risk_per_trade_pct": (rs.get("params") or {}).get("risk_per_trade_pct"),
            "ruleset_version": rs.get("version"), "evo": rs.get("evo") is True,
            "radar_push": {**open_radar.push_settings(data_dir), **open_radar.push_options()},
            "gate": {"id": gate.id, "name": gate.name}}


def build(data_dir: Path, *, repo=None, loop=None, now: datetime | None = None) -> dict:
    from app.market_clock import us_clock
    from app.services import card_volume as cv
    from app.services import intraday_strength as ids
    from app.services import market_mood, open_radar
    from app.services import swing_desk as sd
    data_dir = Path(data_dir)
    now = us_clock.market_datetime(now or us_clock.now())
    errors: dict[str, str] = {}
    swing = _section("swing", lambda: sd.build_live(data_dir, repo=repo, loop=loop), errors)
    intraday = _section("intraday", lambda: ids.build_live(data_dir, repo=repo, loop=loop), errors)
    mood = _section("mood", lambda: market_mood.view(data_dir), errors)
    phase = _section("phase", lambda: cv.phase(now), errors)
    stage = _section("stage", lambda: stage_of(now), errors)
    settings = _section("settings", lambda: _settings(data_dir), errors)
    radar = _section("radar", lambda: open_radar.view(data_dir, repo=repo, now=now), errors)
    gaps = _section("gaps", lambda: gap_board(data_dir, repo, now, swing, (stage or {}).get("key")), errors)
    # 板卡标注 (新闻条数 / 财报 / 盘前涨跌 / 期权 PCR 与 IV) 页面自 0.50.2 起没有地方显示 (2026-11-10 审计 #8):
    # 不再每次请求去读新闻库、财报日历、期权缓存并在后台预热期权链。ctx 键保留为空 (契约不变); card_context 仍可单独调用。
    ctx: dict = {}
    # 0.39.0 雷达投影 (只读; 0.41.0 起只供状态栏 + 持仓): 机会板失败时不给 (不能把“读不到”画成“没挂单”)
    from app.services import opp_desk, opp_radar
    radar_view = _section("radar_view", lambda: opp_radar.collect(
        data_dir, repo=repo, loop=loop, now=now, swing=swing, intraday=intraday, radar=radar, gaps=gaps,
        missing=[k for k in ("intraday", "radar", "gaps") if k in errors]) if swing is not None else None, errors)
    # 0.41.0 一页作战台: 大盘状态 + 跨引擎共振 (只读)
    desk = _section("desk", lambda: opp_desk.view(
        data_dir, repo=repo, now=now, swing=swing, intraday=intraday, radar=radar, gaps=gaps,
        symbols=sorted(_watch_symbols())), errors)
    # 0.51.0 今日多空 (只读; 没有名单 = None)
    from app.services import ls_rank
    lsr = _section("ls_rank", lambda: ls_rank.view(data_dir, repo=repo, now=now), errors)
    return {"contract": CONTRACT, "generated_at": now.isoformat(timespec="seconds"), "phase": phase, "stage": stage,
            "swing": swing, "intraday": intraday, "radar": radar, "gaps": gaps, "mood": mood, "ctx": ctx,
            "settings": settings, "radar_view": radar_view, "desk": desk, "ls_rank": lsr, "errors": errors}


def _watch_symbols() -> set[str]:
    from app.services.decision_scan_job import _watchlist
    return _watchlist()


# ── 0.34.0 按时段换主角 ─────────────────────────────────────
OPEN_STAGE_END = (10, 30)       # 10:30 ET 前主角 = 开盘放量榜, 之后 = 盘中
STAGE_LABEL = {"pre": "盘前", "open": "开盘", "session": "盘中", "board": "挂单"}


def stage_of(now: datetime) -> dict:
    """当前时段的主角 (美东): 盘前 (04:00 前 / 盘前) = 跳空榜; 09:30-10:30 = 开盘放量榜; 10:30-收盘 = 盘中; 收盘后 / 休市 = 挂单。"""
    from datetime import time as dtime

    from app.market_clock import us_clock
    from app.services import card_volume as cv
    now = us_clock.market_datetime(now)
    ph = cv.phase(now)["phase"]
    close = us_clock.session_close(now.date())
    if ph in ("night", "premarket"):
        key = "pre"
    elif ph in ("open_wait", "session") and now.time() < close:      # 半日市按真实收盘 (cv.phase 只认 16:00)
        key = "open" if now.time() < dtime(*OPEN_STAGE_END) else "session"
    else:
        key = "board"
    return {"key": key, "label": STAGE_LABEL[key]}


_PC_CACHE: dict[tuple, dict[str, float]] = {}


def prev_close_today_basis(data_dir, rows: dict[str, tuple[date, float | None, float | None]], today: date) -> dict[str, float]:
    """{票: (昨收那天, 复权收盘, 原始收盘)} → 今天原始价口径的昨收。
    取原始收盘 (复权价会在当晚盘后才整体回溯, 除权日盘中还是拆前口径), 再除以 (昨收那天, 今天] 内的拆股比例:
    除权日 8 拆 1 的票昨收 800 → 100, 与盘前价 / 现价同口径。缺原始收盘退回复权收盘。"""
    out: dict[str, float] = {}
    last: dict[str, date] = {}
    for s, (d, c, rc) in rows.items():
        v = rc if isinstance(rc, (int, float)) and rc > 0 else c
        if isinstance(v, (int, float)) and v > 0:
            out[s], last[s] = float(v), d
    if out and data_dir is not None:
        try:
            from app.services.corporate_actions import split_factors
            for s, f in split_factors(data_dir, sorted(out), last, today).items():
                out[s] = out[s] / f
        except Exception as e:
            logger.warning("prev close: split factors unavailable: %s", type(e).__name__)
    return out


def _prev_closes(repo, syms: list[str], today: date) -> dict[str, float]:
    """今天之前最近一个交易日的官方收盘, 今天原始价口径 (只读日K, 不含今天的盘中快照行; 除权日按拆股比例换算)。
    同一天同一批票只读一次 (盘前页面 30 秒一轮)。"""
    getter = getattr(repo, "get_daily_batch", None)
    if not syms or getter is None:
        return {}
    key = (today.isoformat(), tuple(sorted(syms)))
    if key in _PC_CACHE:
        return _PC_CACHE[key]
    b = getter(sorted(syms), today - timedelta(days=14), today - timedelta(days=1), columns=["symbol", "date", "close", "raw_close"])
    rows: dict[str, tuple] = {}
    if b is not None and not b.is_empty():
        for g in b.sort("date").partition_by("symbol"):
            d = g["date"][-1]
            rows[str(g["symbol"][0])] = (d if isinstance(d, date) else date.fromisoformat(str(d)[:10]), g["close"][-1],
                                         g["raw_close"][-1] if "raw_close" in g.columns else None)
    store = getattr(repo, "store", None)
    out = prev_close_today_basis(getattr(store, "data_dir", None), rows, today)
    if out:
        if len(_PC_CACHE) > 8:
            _PC_CACHE.clear()
        _PC_CACHE[key] = out
    return out


def gap_board(data_dir: Path, repo, now: datetime, swing: dict | None, stage: str | None) -> dict | None:
    """盘前跳空榜: 自选里盘前价对昨收 |涨跌| >= 3% 的票 (与 09:00 盘前推送同一阈值), 按幅度排。
    09:00 ET 之后读当天落盘的那份 (冻结); 之前只在盘前段现取 Hyperliquid 永续中间价 (15 秒缓存, 与机会板盘前现价同一来源)。
    HL 永续含基差, 只作提醒; 不进裁决。"""
    from app.market_clock import us_clock
    from app.services import card_volume as cv
    from app.services import premarket_gap
    from app.services.decision_scan_job import _watchlist
    today = now.date()
    if not us_clock.is_trading_day(today):
        return None
    th = premarket_gap.THRESHOLD_PCT
    roles: dict[str, str] = {}
    for c in _board_cards(swing):
        if isinstance(c.get("symbol"), str):
            roles[c["symbol"]] = "持仓" if c.get("state") == "holding" else roles.get(c["symbol"], "挂单")
    rec = premarket_gap.latest(data_dir) or {}
    if str(rec.get("day")) == today.isoformat():
        gaps = {k: float(v) for k, v in (rec.get("gaps") or {}).items() if isinstance(v, (int, float))}
        mids = {k: float(v) for k, v in (rec.get("mids") or {}).items() if isinstance(v, (int, float))}
        at, live = str(rec.get("at") or ""), False
    elif stage == "pre":
        syms = sorted(_watchlist())
        cached = cv.premarket_prices_cached(syms)          # 请求里不出网: 没缓存就后台取, 本次标“取数中”
        if cached is None:
            return {"at": now.isoformat(timespec="seconds"), "live": True, "warming": True,
                    "threshold_pct": th, "checked": 0, "rows": []}
        px = {s: v["price"] for s, v in cached.items() if isinstance(v.get("price"), (int, float))}
        pc = _prev_closes(repo, list(px), today)
        gaps = {s: (px[s] / pc[s] - 1) * 100 for s in px if pc.get(s)}
        mids = px
        at, live = now.isoformat(timespec="seconds"), True
    else:
        return None
    rows = [{"symbol": s, "gap_pct": round(g, 3), "price": mids.get(s), **({"role": roles[s]} if s in roles else {})}
            for s, g in gaps.items() if abs(g) >= th]
    rows.sort(key=lambda r: (-abs(r["gap_pct"]), r["symbol"]))
    return {"at": at, "live": live, "threshold_pct": th, "checked": len(gaps), "rows": rows}


def save_settings(data_dir: Path, *, long_ids: list, short_ids: list, risk_pct: float | None, note: str = "",
                  radar_push: dict | None = None) -> dict:
    """规则齿轮的唯一保存口 (0.29.0): 机会板 3+3 + 单笔风险 (+ 0.35.0 开盘榜推送开关 / 门槛)。
    风险变了 (或旧自进化开关还开着) 才新建规则集版本, 勾选 / 看空列表原样沿用, 自进化关掉; 其余不建版本。
    先建版本再写 3+3: 版本写失败 3+3 不动。推送设置只在请求带了才写, 与 3+3 / 风险无关 (不建规则集版本)。"""
    from app.services import open_radar
    from app.services import swing_desk as sd
    from app.services import watchlist_v4 as wv
    data_dir = Path(data_dir)
    push = None
    if radar_push is not None:
        push = open_radar.save_push_settings(data_dir, enabled=radar_push["enabled"], min_rvol=radar_push["min_rvol"])
    cur = wv.current_ruleset(data_dir)
    ruleset = cur
    cur_risk = (cur.get("params") or {}).get("risk_per_trade_pct")
    if (risk_pct is not None and risk_pct != cur_risk) or cur.get("evo") is True:
        params = {"risk_per_trade_pct": risk_pct if risk_pct is not None else cur_risk}
        ruleset = wv.save_ruleset(data_dir, list(cur.get("enabled") or []), params,
                                  note or "opp page settings: risk", short=list(cur.get("short") or []), evo=False)
    swing = sd.save_strategies(data_dir, long_ids, short_ids)
    return {"swing": swing, "ruleset": {"version": ruleset.get("version"),
                                        "risk_per_trade_pct": (ruleset.get("params") or {}).get("risk_per_trade_pct")},
            "radar_push": push if push is not None else open_radar.push_settings(data_dir)}
