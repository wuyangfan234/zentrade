"""盘面情报面板 (0.40.0): 页面聚合 + 后台刷新任务。只展示, 不进打分、裁决、下单, 不推送。

GET /api/intel/today = build(): 只读 data/intel/*.json 与已有缓存 (大盘情绪 / 新闻库 / 财报日历 / 公司行动 /
期权链), 请求里不出网; 各区块互相隔离, 某块失败只空那一块 (errors 里写原因)。
tick(): 调度每 60 秒调一次, 按时段决定哪些源到期 (见 DUE), 每个源单独计时、单独容错, 失败不覆盖旧缓存。
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

from app.market_clock import us_clock
from app.services import intel_calc as calc
from app.services import intel_sources as src

logger = logging.getLogger(__name__)

CONTRACT = "intel-v1"
IMPACT_CHOICES = ("high", "high_medium", "all")
DEFAULT_SETTINGS = {"macro_impact": "high_medium"}
NEWS_HOURS = 36
NEWS_LIMIT = 40
_TICK_LOCK = threading.Lock()
_WARM_LOCK = threading.Lock()
WARM_MAX = 25
_LIVE_CACHE: dict[str, tuple[float, Any]] = {}


# ── 设置 ─────────────────────────────────────────────────────────────

def load_settings(data_dir) -> dict:
    s = src.read_cache(data_dir, "settings") or {}
    out = dict(DEFAULT_SETTINGS)
    if s.get("macro_impact") in IMPACT_CHOICES:
        out["macro_impact"] = s["macro_impact"]
    return out


def save_settings(data_dir, *, macro_impact: str) -> dict:
    if macro_impact not in IMPACT_CHOICES:
        raise ValueError("macro_impact")
    cur = load_settings(data_dir)
    cur["macro_impact"] = macro_impact
    src.write_json(src.root(data_dir) / "settings.json", cur)
    return cur


def _impact_ok(impact: str, level: str) -> bool:
    if level == "all":
        return True
    if level == "high":
        return impact == "high"
    return impact in ("high", "medium")


# ── 名单 ─────────────────────────────────────────────────────────────

def _watch_symbols() -> list[str]:
    from app.services import watchlist
    return [r["symbol"] for r in watchlist.list_symbols() if r.get("symbol")]


def _positions(loop) -> dict[str, str]:
    out: dict[str, str] = {}
    if loop is None:
        return out
    try:
        for p in loop.ledger.open_positions:
            out[p.symbol] = "short" if p.qty < 0 else "long"
    except Exception as e:
        logger.debug("intel: positions unavailable: %s", type(e).__name__)
    return out


def _roles(loop) -> dict[str, str]:
    roles = {s: "自选" for s in _watch_symbols()}
    for s in _positions(loop):
        roles[s] = "持仓"
    return roles


# ── 页面聚合 ─────────────────────────────────────────────────────────

def _section(name: str, fn, errors: dict) -> Any:
    try:
        return fn()
    except Exception as e:  # 一块失败不拖垮整页
        logger.warning("intel page: %s failed: %s", name, type(e).__name__)
        errors[name] = type(e).__name__
        return None


def _age_min(iso: str | None, now: datetime) -> float | None:
    t = calc._parse_iso(iso)
    return round((now - t).total_seconds() / 60, 1) if t else None


def tape_view(data_dir: Path, now: datetime) -> dict:
    etfs = src.read_cache(data_dir, "etfs") or {}
    daily_cache = src.read_cache(data_dir, "etf_daily") or {}
    daily = daily_cache.get("rows") or {}
    rows = etfs.get("rows") or {}
    # 快照不是当天取的 (调度停了) 就不把它当盘中价
    snap_day = (calc._parse_iso(etfs.get("fetched_at")) or now).date()

    def row(sym, names):
        r = src.combine_quote(rows.get(sym) if snap_day == now.date() else None, daily.get(sym), now.date())
        return {"symbol": sym, "name": names.get(sym, sym), **r} if r else None

    idx_names = {"SPY": "标普500", "QQQ": "纳指100", "DIA": "道指", "IWM": "罗素2000"}
    return {
        "indexes": [r for s in src.INDEX_ETFS if (r := row(s, idx_names))],
        "sectors": [r for s in src.SECTOR_ETFS if (r := row(s, src.SECTOR_NAMES))],
        "cross": [r for s in src.CROSS_ETFS if (r := row(s, src.CROSS_NAMES))],
        "fetched_at": etfs.get("fetched_at"), "age_min": _age_min(etfs.get("fetched_at"), now),
        "source": "现价 Alpaca IEX 单所 · 前收 SIP 官方日K", "daily_day": daily_cache.get("day"),
    }


def vol_view(data_dir: Path, now: datetime) -> dict | None:
    v = src.read_cache(data_dir, "vol")
    if not v:
        return None
    return {**{k: x for k, x in v.items() if k not in ("vix_closes", "vix_history_at")}, "age_min": _age_min(v.get("fetched_at"), now)}


def rates_view(data_dir: Path) -> dict | None:
    return src.read_cache(data_dir, "rates")


def _enriched_warm(repo) -> bool:
    """实时快照缓存已就绪 (未就绪时 get_enriched_latest 会在请求线程里同步重算全市场, 慢几十秒)。"""
    return repo is not None and getattr(repo, "_enriched_cache", None) is not None


def live_breadth(repo, breadth: dict | None, today: date) -> dict | None:
    """盘中近似: 实时快照里同一股票池的上涨家数 (对上一官方收盘)。30 秒进程内缓存。"""
    if not _enriched_warm(repo) or not breadth or not breadth.get("last_close"):
        return None
    hit = _LIVE_CACHE.get("breadth")
    if hit and _time.monotonic() - hit[0] < 30:
        return hit[1]
    df, d = repo.get_enriched_latest()
    out = None
    if d == today and df is not None and not df.is_empty() and str(breadth.get("as_of")) < today.isoformat():
        base = breadth["last_close"]
        px = dict(zip(df["symbol"].to_list(), df["close"].to_list(), strict=True))
        pairs = [(px[s], c) for s, c in base.items() if isinstance(px.get(s), (int, float)) and c]
        if len(pairs) >= 200:
            adv = sum(1 for p, c in pairs if p > c)
            dec = sum(1 for p, c in pairs if p < c)
            out = {"day": today.isoformat(), "n": len(pairs), "adv": adv, "dec": dec,
                   "up_pct": round(adv / len(pairs) * 100, 1), "source": "实时快照 (盘中近似)"}
    _LIVE_CACHE["breadth"] = (_time.monotonic(), out)
    return out


def breadth_view(data_dir: Path, repo, today: date) -> dict | None:
    b = src.read_cache(data_dir, "breadth")
    if not b:
        return None
    out = {k: v for k, v in b.items() if k != "last_close"}
    out["live"] = live_breadth(repo, b, today)
    return out


def calendar_view(data_dir: Path, now: datetime, roles: dict[str, str], settings: dict) -> dict:
    ff = src.read_cache(data_dir, "macro_calendar") or {}
    actuals = src.read_cache(data_dir, "macro_actuals") or {}
    level = settings["macro_impact"]
    events = []
    for ev in ff.get("events") or []:
        if not _impact_ok(ev.get("impact"), level):
            continue
        at = calc._parse_iso(ev.get("at"))
        if at is None:
            continue
        a = actuals.get(src.event_key(ev)) or {}
        mapped = ev["title"] in src.FRED_MAP
        e = {"kind": "macro", "at": ev["at"], "day": at.date().isoformat(), "time": at.strftime("%H:%M"),
             "title": ev["title"], "title_zh": src.TITLE_ZH.get(ev["title"]), "impact": ev.get("impact"),
             "forecast": ev.get("forecast"), "previous": ev.get("previous"), "past": at <= now,
             "actual": a.get("actual"), "actual_source": a.get("source"), "actual_derived": bool(a.get("derived")),
             "surprise": src.surprise(a["actual"], ev.get("forecast"), derived=bool(a.get("derived"))) if a.get("actual") else None,
             "actual_status": ("ok" if a.get("actual") else "mismatch" if a.get("status") == "mismatch" else
                               ("pending" if at <= now and mapped else ("unmapped" if at <= now else None)))}
        events.append(e)
    # 自选财报进同一时间轴 (盘前 08:00 / 盘后 16:05 / 未注明 只放日期)
    up = src.read_cache(data_dir, "earnings_upcoming")
    cal = _earnings_calendar(data_dir)
    for sym, r in calc.upcoming_for(up, cal, sorted(roles), now.date()).items():
        timing = r.get("timing") or "unknown"
        label = {"bmo": "盘前", "amc": "盘后"}.get(timing, "时间未定")
        hm = {"bmo": "08:00", "amc": "16:05"}.get(timing, "")
        events.append({"kind": "earnings", "day": r["date"], "time": hm, "symbol": sym, "role": roles.get(sym),
                       "timing": timing, "title": f"{sym} 财报 · {label}", "past": False,
                       "at": f"{r['date']}T{hm or '12:00'}"})
    # 月度期权到期 (当月 / 下月第三个周五, 在 7 天窗口内才放)
    for d in {calc.opex_day(now.year, now.month), calc.opex_day(now.year + (now.month == 12), now.month % 12 + 1)}:
        if now.date() <= d <= now.date() + timedelta(days=7):
            events.append({"kind": "opex", "day": d.isoformat(), "time": "16:00", "title": "月度期权到期",
                           "past": False, "at": f"{d.isoformat()}T16:00"})
    events.sort(key=lambda e: (e["day"], e.get("time") or "99", e["kind"] != "macro"))
    days = sorted({e["day"] for e in events} | {now.date().isoformat()})
    st = (src.read_cache(data_dir, "state") or {}).get("macro_calendar") or {}
    return {"events": events, "days": days, "today": now.date().isoformat(), "level": level,
            "fetched_at": ff.get("fetched_at"), "error": st.get("error"),
            "source": "ForexFactory 本周日历 · 实际值 FRED", "is_trading_day": us_clock.is_trading_day(now.date()),
            "session_close": us_clock.session_close(now.date()).strftime("%H:%M")}


def _earnings_calendar(data_dir: Path) -> dict | None:
    return src.read_json(Path(data_dir) / "reference" / "earnings" / "calendar.json")


def _spot(repo, sym: str) -> float | None:
    if not _enriched_warm(repo):
        return None
    df, _ = repo.get_enriched_latest()
    if df is None or df.is_empty():
        return None
    import polars as pl
    r = df.filter(pl.col("symbol") == sym)
    if r.is_empty():
        return None
    c = r["close"][0]
    return float(c) if isinstance(c, (int, float)) and c > 0 else None


def trading_days_ahead(today: date, d: date) -> int:
    """财报日离今天几个交易日 (0 = 今天)。今天休市时下一个交易日算 T+1 (原来周六看周一的财报会显示「今天」)。"""
    n = len(us_clock.trading_days_between(today, d))
    return n - 1 if us_clock.is_trading_day(today) else n


def earnings_view(data_dir: Path, repo, now: datetime, roles: dict[str, str]) -> dict:
    up = src.read_cache(data_dir, "earnings_upcoming")
    cal = _earnings_calendar(data_dir)
    hist = (src.read_cache(data_dir, "earnings_history") or {}).get("symbols") or {}
    rows = []
    for sym, r in calc.upcoming_for(up, cal, sorted(roles), now.date()).items():
        h = hist.get(sym) or {}
        d = date.fromisoformat(r["date"])
        iv = calc.implied_move(data_dir, sym, _spot(repo, sym), d, now.date())
        qv = calc.read_qveris_estimate(data_dir, sym)
        fh = r.get("finnhub") or {}
        cons = calc.eps_consensus(r, qv, d)
        rows.append({"symbol": sym, "role": roles.get(sym), "date": r["date"], "timing": r.get("timing"),
                     "days": trading_days_ahead(now.date(), d),
                     "date_conflict": fh.get("date") if fh.get("date") and fh["date"] != r["date"] else None,
                     "eps_consensus": cons["eps"], "eps_source": cons["source"], "eps_nasdaq": r.get("eps_forecast"),
                     "eps_forecast": r.get("eps_forecast"), "n_est": r.get("n_est"), "last_year_eps": r.get("last_year_eps"),
                     "fiscal_quarter": r.get("fiscal_quarter"), "qveris": qv,
                     "hist": {k: h.get(k) for k in ("n", "median_abs_move", "max_abs_move", "beat_n", "surprise_n")} if h else None,
                     "last": h.get("last"), "reports": (h.get("reports") or [])[:4], "implied": iv})
    rows.sort(key=lambda x: (x["date"], x["role"] != "持仓", x["symbol"]))
    return {"rows": rows, "fetched_at": (up or {}).get("fetched_at"),
            "hist_at": (src.read_cache(data_dir, "earnings_history") or {}).get("computed_at"),
            "source": "Nasdaq 一致预期 · 历史反应=日K · 隐含波动=期权链"}


def corp_actions_view(data_dir: Path, now: datetime, roles: dict[str, str]) -> list[dict]:
    from app.services.corporate_actions import CorporateActionService
    cache = CorporateActionService(data_dir).load() or {}
    end = (now.date() + timedelta(days=14)).isoformat()
    out = []
    for a in cache.get("actions") or []:
        sym = a.get("symbol")
        d = a.get("ex_date") or a.get("effective_date") or a.get("process_date")
        if sym in roles and d and now.date().isoformat() <= str(d)[:10] <= end:
            out.append({"symbol": sym, "role": roles[sym], "type": a.get("type"), "date": str(d)[:10],
                        "rate": a.get("rate"), "special": a.get("special")})
    out.sort(key=lambda x: (x["date"], x["symbol"]))
    return out


NEWS_FILTERS = {
    "all": lambda i: True,
    "hold": lambda i: i.get("role") == "持仓",
    "bullish": lambda i: i.get("direction") == "bullish",
    "bearish": lambda i: i.get("direction") == "bearish",
    "macro": lambda i: bool(i.get("macro")) or i.get("event_type") == "macro",
}


def news_view(data_dir: Path, now: datetime, roles: dict[str, str]) -> dict:
    from datetime import UTC

    from app.services import news_store
    utc_now = now.astimezone(UTC)
    rows = news_store.load_recent(data_dir, days=2, now=utc_now)
    items, tone = [], {}
    seen = set()
    for r in rows:
        dt = news_store._parse(r.get("created_at"))
        if not dt or (utc_now - dt).total_seconds() > NEWS_HOURS * 3600:
            continue
        hit = sorted((s for s in (r.get("mentions") or {}) if s in roles), key=lambda s: roles[s] != "持仓")
        macro = any((t or {}).get("event_type") == "macro" for t in (r.get("tags") or {}).values())
        for sym in hit:
            v = news_store.view(r, sym, now=utc_now)
            if not news_store.is_main(v):
                continue
            if v["tagged"] and v["importance"] in ("high", "medium") and v["direction"] in ("bullish", "bearish"):
                t = tone.setdefault(sym, {"bullish": 0, "bearish": 0})
                t[v["direction"]] += 1
            if r["id"] in seen or (v["tagged"] and v["importance"] == "low"):
                continue
            seen.add(r["id"])
            items.append({**v, "role": roles.get(sym)})
        if macro and r["id"] not in seen:
            sym = next(iter(r.get("mentions") or {}), "")
            v = news_store.view(r, sym, now=utc_now) if sym else None
            if v and v.get("importance") in ("high", "medium"):
                seen.add(r["id"])
                items.append({**v, "role": None, "macro": True})
    items.sort(key=lambda v: v.get("published_at") or "", reverse=True)
    # 页面按 全部/持仓/利多/利空/宏观 在前端筛: 每个筛选各保留最新 NEWS_LIMIT 条再取并集
    # (原来先截全部的前 40 条, 持仓 / 宏观新闻排在 40 条之后时筛出来是空的)
    picks = {k: [i for i in items if f(i)] for k, f in NEWS_FILTERS.items()}
    keep = {id(i) for rows in picks.values() for i in rows[:NEWS_LIMIT]}
    return {"items": [i for i in items if id(i) in keep], "total": len(items), "tone": tone, "hours": NEWS_HOURS,
            "counts": {k: len(v) for k, v in picks.items()}, "limit": NEWS_LIMIT,
            "disclaimer": "事件类型、方向与重要度为 AI 判读，未经验证；仅供参考"}  # noqa: RUF001


def build(data_dir: Path, *, repo=None, loop=None, now: datetime | None = None) -> dict:
    from app.services import card_volume as cv
    from app.services import market_mood
    data_dir = Path(data_dir)
    now = us_clock.market_datetime(now or us_clock.now())
    errors: dict[str, str] = {}
    settings = load_settings(data_dir)
    roles = _section("roles", lambda: _roles(loop), errors) or {}
    out = {
        "contract": CONTRACT, "generated_at": now.isoformat(timespec="seconds"),
        "phase": _section("phase", lambda: cv.phase(now), errors), "settings": settings,
        # market_mood.view / news_store 读取时会顺手建目录; 目录不在 = 没有数据, 直接不给 (保持请求只读)
        "mood": _section("mood", lambda: market_mood.view(data_dir, now=now)
                         if (data_dir / "market_mood" / "judgments").is_dir() else None, errors),
        "tape": _section("tape", lambda: tape_view(data_dir, now), errors),
        "vol": _section("vol", lambda: vol_view(data_dir, now), errors),
        "rates": _section("rates", lambda: rates_view(data_dir), errors),
        "pcr": _section("pcr", lambda: src.pcr_view(src.read_cache(data_dir, "pcr")), errors),
        "breadth": _section("breadth", lambda: breadth_view(data_dir, repo, now.date()), errors),
        "calendar": _section("calendar", lambda: calendar_view(data_dir, now, roles, settings), errors),
        "earnings": _section("earnings", lambda: earnings_view(data_dir, repo, now, roles), errors),
        "actions": _section("actions", lambda: corp_actions_view(data_dir, now, roles), errors),
        "news": _section("news", lambda: news_view(data_dir, now, roles) if (data_dir / "news").is_dir() else None, errors),
        "watch_count": len(roles), "state": src.read_cache(data_dir, "state") or {},
    }
    out["errors"] = errors
    return out


# ── 后台刷新 ─────────────────────────────────────────────────────────

def _active(now: datetime) -> bool:
    """交易日 04:00-16:20 ET: 盘前盘中高频刷新。"""
    return us_clock.is_trading_day(now.date()) and dtime(4, 0) <= now.time() <= dtime(16, 20)


# 每个源的刷新间隔 (秒): (活跃时段, 其余时段)
DUE = {"etfs": (60, 1800), "vol": (120, 1800), "rates": (1800, 3600), "macro_calendar": (900, 1800),
       "macro_actuals": (60, 300), "pcr": (3600, 3600), "breadth": (300, 1800), "etf_daily": (1800, 3600),
       "earnings_upcoming": (6 * 3600, 6 * 3600), "earnings_history": (6 * 3600, 6 * 3600), "options_warm": (1800, 3 * 3600)}


# 「立即刷新」只强制跑快源 (实测全量强制 162 秒, 其中 Nasdaq 10 天 + Finnhub 逐票 145 秒,
# 期间占住 tick 锁, 每分钟的 ETF 刷新停摆, 前端 180 秒超时边缘)。财报类按自己的 6 小时节奏跑。
MANUAL_SOURCES = frozenset({"etfs", "vol", "rates", "macro_calendar", "macro_actuals", "pcr", "breadth", "etf_daily"})


def _due(state: dict, key: str, now: datetime) -> bool:
    st = state.get(key) or {}
    last = calc._parse_iso(st.get("last_attempt_at"))
    if last is None:
        return True
    gap = DUE[key][0 if _active(now) else 1]
    return (now - last).total_seconds() >= gap


def _alpaca():
    from app.data_providers.custom import get_provider
    return get_provider("alpaca")


def _warm_chains(data_dir: Path, syms: list[str], now: datetime) -> None:
    """后台线程 (调用方已持有 _WARM_LOCK)。当天缓存若是开盘前拉的 (报价是昨收后的残值), 开盘后重拉一次。"""
    from app.services import alpaca_options
    try:
        for sym in syms:
            p = alpaca_options._dir(data_dir, sym) / f"{now.date().isoformat()}.json"
            t = calc._parse_iso((src.read_json(p) or {}).get("fetched_at")) if p.exists() else None
            if t is not None and t.astimezone(us_clock.tz).time() < dtime(9, 35):
                p.unlink(missing_ok=True)
            try:
                alpaca_options.chain(data_dir, sym, today=now.date())
            except Exception as e:
                logger.info("intel: options warm %s failed: %s", sym, type(e).__name__)
    finally:
        _WARM_LOCK.release()


def tick(data_dir, repo=None, loop=None, *, now: datetime | None = None, force: bool = False,
         only: set[str] | None = None) -> dict | None:
    """跑一轮到期的源。上一轮没跑完 (锁被占) 直接返回 None。"""
    if not _TICK_LOCK.acquire(blocking=False):
        return None
    try:
        return _tick(Path(data_dir), repo, loop, now=us_clock.market_datetime(now or us_clock.now()), force=force, only=only)
    finally:
        _TICK_LOCK.release()


def _tick(data_dir: Path, repo, loop, *, now: datetime, force: bool, only: set[str] | None) -> dict:
    state = src.read_cache(data_dir, "state") or {}
    ran: dict[str, Any] = {}

    def run(key: str, fn):
        if only is not None and key not in only:
            return
        if not force and not _due(state, key, now):
            return
        t0 = _time.monotonic()
        try:
            fn()
            src.mark_state(data_dir, key, ok=True, now=now)
            ran[key] = round(_time.monotonic() - t0, 2)
        except Exception as e:
            logger.warning("intel: %s failed: %s", key, type(e).__name__)
            src.mark_state(data_dir, key, ok=False, error=f"{type(e).__name__}: {e}", now=now)
            ran[key] = f"error {type(e).__name__}"

    def etfs():
        src.write_json(src.root(data_dir) / "etfs.json", src.fetch_etfs(_alpaca(), now))

    def vol():
        prev = src.read_cache(data_dir, "vol") or {}
        v = src.fetch_vol(closes=prev.get("vix_closes"), history_at=prev.get("vix_history_at"), now=now)
        src.write_json(src.root(data_dir) / "vol.json", {**v, "fetched_at": now.isoformat(timespec="seconds")})

    def rates():
        from app.services import market_mood
        r = market_mood.fetch_rates(now)
        src.write_json(src.root(data_dir) / "rates.json", {**r, "fetched_at": now.isoformat(timespec="seconds"),
                                                           "source": "美国财政部 10Y · FRED 高收益利差"})

    def macro_calendar():
        ev = src.fetch_ff_week()
        src.write_json(src.root(data_dir) / "macro_calendar.json",
                       {"events": ev, "fetched_at": now.isoformat(timespec="seconds"), "source": "ForexFactory"})

    def macro_actuals():
        ev = (src.read_cache(data_dir, "macro_calendar") or {}).get("events") or []
        src.update_actuals(data_dir, ev, now)

    def pcr():
        src.update_pcr(data_dir, now)

    def breadth():
        cur = src.read_cache(data_dir, "breadth") or {}
        last = calc.latest_partition_day(data_dir, now.date())
        if not force and cur.get("as_of") and last and cur["as_of"] >= last:
            return
        b = calc.compute_breadth(data_dir, today=now.date())
        if b:
            src.write_json(src.root(data_dir) / "breadth.json", b)

    def etf_daily():
        cur = src.read_cache(data_dir, "etf_daily") or {}
        day = (now.date() if now.time() >= dtime(16, 20) else us_clock.previous_trading_day(now.date())).isoformat()
        if not force and cur.get("day") == day:
            return
        rows = src.fetch_etf_daily(_alpaca(), now)
        days = {r["day"] for r in rows.values()}
        if rows:
            src.write_json(src.root(data_dir) / "etf_daily.json", {"day": max(days), "rows": rows})

    def earnings_upcoming():
        from app.services import earnings_calendar as ec
        up = calc.fetch_upcoming(now.date(), previous=src.read_cache(data_dir, "earnings_upcoming"))
        watch = sorted(calc.upcoming_for(up, _earnings_calendar(data_dir), sorted(_roles(loop)), now.date()))
        fh = calc.fetch_finnhub_upcoming(watch, now.date(), ec._configured_finnhub_token())
        for sym, r in fh.items():
            if sym in up["rows"]:
                up["rows"][sym]["finnhub"] = r
        up["finnhub_n"] = len(fh)
        src.write_json(src.root(data_dir) / "earnings_upcoming.json", up)

    def earnings_history():
        syms = sorted(_roles(loop))
        up = (src.read_cache(data_dir, "earnings_upcoming") or {}).get("rows") or {}
        ev = ((_earnings_calendar(data_dir) or {}).get("events")) or {}
        nxt = {s: (up.get(s) or ev.get(s) or {}).get("timing") for s in syms}
        src.write_json(src.root(data_dir) / "earnings_history.json",
                       calc.compute_earnings_history(data_dir, repo, syms, today=now.date(), next_timing=nxt))

    def options_warm():
        """财报窗口内的自选: 预热当天免费期权链 (与机会页同一份缓存)。按财报日由近到远, 最多 WARM_MAX 只。
        单只要十几秒: 放后台线程跑 (单飞), 不占住 tick 锁, 否则 ETF 1 分钟刷新会停几分钟。"""
        if not us_clock.is_trading_day(now.date()) or now.time() < dtime(9, 35):
            return
        up = src.read_cache(data_dir, "earnings_upcoming")
        todo = calc.upcoming_for(up, _earnings_calendar(data_dir), sorted(_roles(loop)), now.date())
        syms = [s for s, _ in sorted(todo.items(), key=lambda kv: (kv[1]["date"], kv[0]))][:WARM_MAX]
        if not _WARM_LOCK.acquire(blocking=False):
            return
        threading.Thread(target=_warm_chains, args=(data_dir, syms, now), daemon=True, name="intel-options-warm").start()

    run("etfs", etfs)
    run("vol", vol)
    run("rates", rates)
    run("macro_calendar", macro_calendar)
    run("macro_actuals", macro_actuals)
    run("pcr", pcr)
    run("breadth", breadth)
    run("etf_daily", etf_daily)
    run("earnings_upcoming", earnings_upcoming)
    run("earnings_history", earnings_history)
    run("options_warm", options_warm)
    if ran:
        logger.info("intel tick: %s", ran)
    return ran
