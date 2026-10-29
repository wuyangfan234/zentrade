"""QVeris 三项数据的业务层: 期权摘要 / 财报一致预期 / 财报电话会 (2026-10-02 产品决策, 每日上限 100 积分)。

缓存新鲜度 (命中即免费):
  options_chain       盘中 15 分钟; 非盘中: 直到下一次开盘 (收盘后链不变)
  earnings_estimates  同一美东日内有效 (预期一天内基本不变)
  earnings_transcript 永久 (按季度区分, 已发生的电话会不会变)
每日自动: 10 个交易日内有财报的「持仓 + 机会卡候选」各查一次财报预期 (trigger=auto, 计入每日上限)。
所有数据只用于展示与 AI 分析输入, 不参与开仓。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.services import qveris_gate as gate

logger = logging.getLogger(__name__)

OPTIONS_TTL_S = 15 * 60
AUTO_LOOKAHEAD_TRADING_DAYS = 10


# ── 新鲜度规则 ─────────────────────────────────────────────────

def _fetched_dt(entry: dict) -> datetime | None:
    ts = entry.get("fetched_ts")
    return datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float)) else None


def options_fresh(entry: dict, *, now: datetime | None = None) -> bool:
    from app.market_clock import us_clock
    now = us_clock.market_datetime(now or us_clock.now())
    t = _fetched_dt(entry)
    if t is None:
        return False
    if us_clock.in_session(now):
        return (now - t).total_seconds() < OPTIONS_TTL_S and us_clock.in_session(us_clock.market_datetime(t))
    # 非盘中: 若取数之后没有经历过开盘, 链没有变化 → 有效
    return not _session_opened_between(t, now)


def _session_opened_between(t0: datetime, t1: datetime) -> bool:
    """t0 与 t1 之间是否出现过一次常规时段开盘 (09:30 ET)。"""
    from app.market_clock import us_clock
    d0, d1 = t0.astimezone(us_clock.tz), t1.astimezone(us_clock.tz)
    day = d0.date()
    while day <= d1.date():
        if us_clock.is_trading_day(day):
            open_dt = datetime.combine(day, us_clock.sessions[0].open, tzinfo=us_clock.tz)
            if d0 < open_dt <= d1:
                return True
        day += timedelta(days=1)
    return False


def estimates_fresh(entry: dict, *, now: datetime | None = None) -> bool:
    from app.market_clock import us_clock
    now = now or datetime.now(timezone.utc)
    t = _fetched_dt(entry)
    return t is not None and t.astimezone(us_clock.tz).date() == now.astimezone(us_clock.tz).date()


def estimates_fresh_until(earnings_date: date | None):
    """0.22.4 自动任务用: 财报日前已买过的预期一直有效 (财报日当天及之前不重复付费); 无财报日回退当日有效。"""
    def _f(entry: dict, *, now: datetime | None = None) -> bool:
        from app.market_clock import us_clock
        t = _fetched_dt(entry)
        if t is None:
            return False
        if earnings_date is None:
            return estimates_fresh(entry, now=now)
        now_d = (now or datetime.now(timezone.utc)).astimezone(us_clock.tz).date()
        fetched_d = t.astimezone(us_clock.tz).date()
        # 取数须在本财报窗口内 (财报日前 30 天以内), 且今天未过财报日
        return now_d <= earnings_date and fetched_d >= earnings_date - timedelta(days=30)
    return _f


def transcript_fresh(entry: dict, **_) -> bool:
    data = entry.get("data") or {}
    return bool((data.get("transcript") if isinstance(data, dict) else None))


# ── 期权 ─────────────────────────────────────────────────────

def spot_price(repo, symbol: str) -> tuple[float | None, str]:
    """现价: 实时热报价 (TickFlow) 优先, 否则最近日K收盘。返回 (价格, 来源说明)。"""
    try:
        from app.services import hot_quotes
        p = hot_quotes.provider()
        get_quotes = getattr(p, "get_execution_quotes", None) if p is not None else None
        if callable(get_quotes):
            q = get_quotes([symbol], max_age_seconds=120)
            if q and q[0].get("price"):
                return float(q[0]["price"]), "实时报价"
    except Exception as e:  # noqa: BLE001
        logger.debug("spot via realtime failed: %s", type(e).__name__)
    try:
        from app.market_clock import us_clock
        end = us_clock.today()
        df = repo.get_daily(symbol, end - timedelta(days=14), end, columns=["date", "close"])
        if df is not None and not df.is_empty():
            return float(df["close"][-1]), f"{df['date'][-1]} 收盘"
    except Exception as e:  # noqa: BLE001
        logger.debug("spot via daily failed: %s", type(e).__name__)
    return None, "无现价"


def _free_options(data_dir: Path, repo, symbol: str, earnings_date: date | None) -> dict | None:
    from app.market_clock import us_clock
    from app.services import alpaca_options, options_analytics
    try:
        ch = alpaca_options.chain(data_dir, symbol, today=us_clock.today())
    except Exception as e:  # noqa: BLE001
        logger.debug("free options %s: %s", symbol, type(e).__name__)
        return None
    if not ch.get("available"):
        return None
    spot, spot_src = spot_price(repo, symbol)
    analysis = options_analytics.analyze(ch["rows"], spot or 0, today=us_clock.today(), earnings_date=earnings_date)
    return {"symbol": symbol, "available": True, "fetched_at": ch.get("fetched_at"), "cached": ch.get("cached"),
            "charged": 0.0, "fresh": True, "spot": spot, "spot_source": spot_src, "analysis": analysis,
            "cost": 0.0, "source": "alpaca"}


def options_summary(data_dir: Path, repo, symbol: str, *, refresh: bool, earnings_date: date | None = None,
                    execute=None) -> dict:
    """refresh=False: 只读缓存 (不花钱); refresh=True: 缓存过期才调用 (1 积分)。"""
    from app.market_clock import us_clock
    from app.services import options_analytics
    symbol = symbol.upper()
    if not refresh:
        entry = gate.cache_get(data_dir, "options_chain", symbol)
        # 0.23.2: 付费缓存不是今天 (美东) 的 → 用免费 Alpaca 链 (当天缓存); 免费源失败再退回旧付费缓存
        paid_today = entry is not None and (_fetched_dt(entry) is not None and
                                            _fetched_dt(entry).astimezone(us_clock.tz).date() == us_clock.today())
        if not paid_today:
            free = _free_options(data_dir, repo, symbol, earnings_date)
            if free is not None:
                return free
        if entry is None:
            return {"symbol": symbol, "available": False, "reason": "尚未查询", "cost": 1.0}
        res = {**entry, "cached": True, "charged": 0.0}
    else:
        res = gate.fetch(data_dir, "options_chain", symbol,
                         {"function": "REALTIME_OPTIONS", "symbol": symbol, "require_greeks": "true"},
                         fresh=options_fresh, execute=execute)
    rows = (res.get("data") or {}).get("data") if isinstance(res.get("data"), dict) else None
    spot, spot_src = spot_price(repo, symbol)
    analysis = options_analytics.analyze(rows or [], spot or 0, today=us_clock.today(), earnings_date=earnings_date)
    return {"symbol": symbol, "available": True, "fetched_at": res.get("fetched_at"), "cached": res.get("cached"),
            "charged": res.get("charged"), "fresh": options_fresh(res) if res.get("fetched_ts") else False,
            "spot": spot, "spot_source": spot_src, "analysis": analysis, "cost": 1.0}


# ── 财报一致预期 ─────────────────────────────────────────────────

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


UNREPORTED_WINDOW_DAYS = 50  # 财季结束后通常 25–45 天内发财报; 结束不足 50 天的财季视为「待公布」


def _norm_estimates(data, today: date | None = None) -> dict:
    rows = (data or {}).get("estimates") or [] if isinstance(data, dict) else []
    out = []
    for r in rows:
        out.append({
            "date": r.get("date"), "horizon": r.get("horizon"),
            "eps_avg": _f(r.get("eps_estimate_average")), "eps_high": _f(r.get("eps_estimate_high")),
            "eps_low": _f(r.get("eps_estimate_low")), "eps_analysts": _f(r.get("eps_estimate_analyst_count")),
            "eps_avg_7d_ago": _f(r.get("eps_estimate_average_7_days_ago")),
            "eps_avg_30d_ago": _f(r.get("eps_estimate_average_30_days_ago")),
            "eps_up_7d": _f(r.get("eps_estimate_revision_up_trailing_7_days")),
            "eps_down_7d": _f(r.get("eps_estimate_revision_down_trailing_7_days")),
            "eps_up_30d": _f(r.get("eps_estimate_revision_up_trailing_30_days")),
            "eps_down_30d": _f(r.get("eps_estimate_revision_down_trailing_30_days")),
            "rev_avg": _f(r.get("revenue_estimate_average")), "rev_high": _f(r.get("revenue_estimate_high")),
            "rev_low": _f(r.get("revenue_estimate_low")), "rev_analysts": _f(r.get("revenue_estimate_analyst_count")),
        })
    # 待公布与未来的财季/财年 (财季结束不足 50 天也算待公布), 按期末升序
    from app.market_clock import us_clock
    today = today or us_clock.today()
    cutoff = (today - timedelta(days=UNREPORTED_WINDOW_DAYS)).isoformat()
    upcoming = sorted((r for r in out if (r["date"] or "") >= cutoff), key=lambda r: r["date"] or "")
    # horizon 实测取值: "fiscal quarter" / "fiscal year"
    quarters = [r for r in upcoming if "quarter" in str(r["horizon"]).lower()][:4]
    years = [r for r in upcoming if "year" in str(r["horizon"]).lower()][:2]
    nxt = quarters[0] if quarters else (upcoming[0] if upcoming else None)
    if nxt and nxt["eps_avg"] is not None and nxt["eps_avg_30d_ago"]:
        nxt = {**nxt, "eps_change_30d_pct": round(nxt["eps_avg"] / nxt["eps_avg_30d_ago"] - 1, 4) if nxt["eps_avg_30d_ago"] else None}
    return {"next": nxt, "quarters": quarters, "years": years, "rows": len(out)}


def estimates(data_dir: Path, symbol: str, *, refresh: bool, trigger: str = "click", execute=None,
              fresh=None) -> dict:
    symbol = symbol.upper()
    if not refresh:
        entry = gate.cache_get(data_dir, "earnings_estimates", symbol)
        if entry is None:
            return {"symbol": symbol, "available": False, "reason": "尚未查询", "cost": 1.0}
        res = {**entry, "cached": True, "charged": 0.0}
    else:
        res = gate.fetch(data_dir, "earnings_estimates", symbol, {"function": "EARNINGS_ESTIMATES", "symbol": symbol},
                         fresh=fresh or estimates_fresh, trigger=trigger, execute=execute)
    return {"symbol": symbol, "available": True, "fetched_at": res.get("fetched_at"), "cached": res.get("cached"),
            "charged": res.get("charged"), "fresh": estimates_fresh(res) if res.get("fetched_ts") else False,
            "estimates": _norm_estimates(res.get("data")), "cost": 1.0}


# ── 财报电话会 ───────────────────────────────────────────────────

def latest_quarter(today: date) -> str:
    """最近一个已结束的自然季度 (电话会通常在季度结束后 3–8 周召开)。用户可指定其他季度。"""
    q = (today.month - 1) // 3  # 0..3 当前季度序号
    y, q = (today.year, q) if q > 0 else (today.year - 1, 4)
    return f"{y}Q{q}"


def transcript(data_dir: Path, symbol: str, quarter: str, *, refresh: bool, execute=None) -> dict:
    symbol, quarter = symbol.upper(), quarter.upper()
    if not refresh:
        entry = gate.cache_get(data_dir, "earnings_transcript", symbol, quarter)
        if entry is None:
            return {"symbol": symbol, "quarter": quarter, "available": False, "reason": "尚未查询", "cost": 1.0}
        res = {**entry, "cached": True, "charged": 0.0}
    else:
        res = gate.fetch(data_dir, "earnings_transcript", symbol,
                         {"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": symbol, "quarter": quarter},
                         fresh=transcript_fresh, variant=quarter, execute=execute)
    data = res.get("data") or {}
    segs = data.get("transcript") or [] if isinstance(data, dict) else []
    segments = [{"speaker": s.get("speaker"), "title": s.get("title"), "content": s.get("content"),
                 "sentiment": _f(s.get("sentiment"))} for s in segs]
    sents = [s["sentiment"] for s in segments if s["sentiment"] is not None]
    return {"symbol": symbol, "quarter": quarter, "available": bool(segments), "fetched_at": res.get("fetched_at"),
            "cached": res.get("cached"), "charged": res.get("charged"), "segments": segments,
            "avg_sentiment": round(sum(sents) / len(sents), 3) if sents else None,
            "reason": None if segments else "该季度暂无电话会记录（可能尚未召开，或季度填写有误）",
            "digest": (gate.cache_get(data_dir, "transcript_digest", symbol, quarter) or {}).get("data"), "cost": 1.0}


async def digest_transcript(data_dir: Path, symbol: str, quarter: str, *, ai_call=None) -> dict | None:
    """Kimi 提炼电话会要点 (中文, 只用原文, 永久缓存; 不再收 QVeris 积分)。"""
    from app.services.fs_utils import atomic_write_text
    import json as _json
    cached = gate.cache_get(data_dir, "transcript_digest", symbol, quarter)
    if cached:
        return cached.get("data")
    t = transcript(data_dir, symbol, quarter, refresh=False)
    if not t.get("available"):
        return None
    text = "\n".join(f"[{s['speaker']} | {s['title']}] {s['content']}" for s in t["segments"])[:60000]
    if ai_call is None:
        from app.services.ai_provider import generate_ai_text
        ai_call = generate_ai_text
    prompt = ("下面是美股公司财报电话会的英文原文。请只依据原文, 用简体中文输出 6–10 条要点, 每条一行、以「- 」开头, 覆盖: "
              "业绩亮点与不及预期处、管理层给出的指引(数字原样保留)、需求/订单/产能/价格的表述、风险与不确定性、问答环节分析师最关心的问题。"
              "不要给投资建议, 不要编造原文没有的数字。\n\n" + text)
    out = await ai_call([{"role": "user", "content": prompt}], max_tokens=1500, timeout=180.0, task="news_translate")
    points = [ln.strip()[2:].strip() for ln in (out or "").splitlines() if ln.strip().startswith("- ")]
    if not points:
        return None
    digest = {"points": points, "model_task": "news_translate"}
    p = gate._cache_path(data_dir, "transcript_digest", symbol, quarter)
    atomic_write_text(p, _json.dumps({"data": digest, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                                     ensure_ascii=False))
    return digest


# ── 每日自动: 10 个交易日内有财报的持仓 + 候选 ─────────────────────────

def _watchlist_symbols() -> set[str] | None:
    try:
        from app.services import watchlist as _wl
        return {str(e.get("symbol")).upper() for e in _wl.list_symbols() if e.get("symbol")}
    except Exception as e:  # noqa: BLE001
        logger.debug("auto targets: watchlist unavailable: %s", type(e).__name__)
        return None


def _swing_candidates(data_dir: Path, repo=None) -> list[str]:
    """机会板当前活卡 (做多+做空) ∩ 自选池。读失败 → 空 (只查持仓, 宁少勿多)。repo 用于空头 SPY 门。"""
    try:
        from app.services import swing_desk
        b = swing_desk.build(data_dir, repo=repo) or {}
    except Exception as e:  # noqa: BLE001
        logger.debug("auto targets: swing unavailable: %s", type(e).__name__)
        return []
    wl = _watchlist_symbols()
    if not wl:
        return []
    out = {str(c.get("symbol")).upper() for side in ("long", "short") for c in (b.get(side) or [])
           if isinstance(c, dict) and c.get("symbol")}
    return sorted(out & wl)


def auto_targets(data_dir: Path, loop, *, today: date | None = None) -> list[dict]:
    from app.market_clock import us_clock
    from app.services.earnings_calendar import EarningsCalendar
    today = today or us_clock.today()
    days = []
    d = today
    while len(days) < AUTO_LOOKAHEAD_TRADING_DAYS:
        d = us_clock.next_trading_day(d)
        days.append(d)
    horizon = days[-1]
    syms: dict[str, str] = {}
    if loop is not None:
        try:
            for p in loop.ledger.open_positions:
                syms[p.symbol.upper()] = "持仓"
        except Exception as e:  # noqa: BLE001
            logger.debug("auto targets: positions unavailable: %s", type(e).__name__)
    # 0.22.4: 候选只取 0.22 机会板活卡 (swing_desk 做多/做空) ∩ 自选池; 不再把旧版全市场非终态卡当候选
    for s in _swing_candidates(data_dir, getattr(loop, "repo", None)):
        syms.setdefault(s, "候选")
    cal = EarningsCalendar(data_dir)
    out = []
    for s, role in sorted(syms.items()):
        ev = cal.event_for(s)
        if not ev:
            continue
        try:
            ed = date.fromisoformat(str(ev["date"])[:10])
        except ValueError:
            continue
        if today <= ed <= horizon:
            out.append({"symbol": s, "role": role, "earnings_date": ed.isoformat(), "timing": ev.get("timing")})
    # 持仓优先 (额度不够时先保证持仓)
    out.sort(key=lambda r: (r["role"] != "持仓", r["earnings_date"], r["symbol"]))
    return out


def auto_refresh_estimates(data_dir: Path, loop, *, today: date | None = None, execute=None) -> dict:
    """每日一次: 已在当日缓存的跳过 (免费), 上限触发即停, 失败不重试只记录。"""
    if not _configured():
        return {"skipped": "未配置 QVeris Key"}
    targets = auto_targets(data_dir, loop, today=today)
    done, cached, failed, blocked = [], [], [], None
    for t in targets:
        try:
            ed = date.fromisoformat(t["earnings_date"]) if t.get("earnings_date") else None
            r = estimates(data_dir, t["symbol"], refresh=True, trigger="auto", execute=execute,
                          fresh=estimates_fresh_until(ed))
            (cached if r.get("cached") else done).append(t["symbol"])
        except gate.QuotaExceeded as e:
            blocked = str(e)
            break
        except Exception as e:  # noqa: BLE001
            failed.append({"symbol": t["symbol"], "error": str(e)[:120]})
    res = {"targets": len(targets), "fetched": done, "cached": cached, "failed": failed, "blocked": blocked,
           "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    from app.services.fs_utils import atomic_write_text
    import json as _json
    atomic_write_text(gate._dir(data_dir) / "auto_state.json", _json.dumps({**res, "target_list": targets}, ensure_ascii=False))
    logger.info("qveris auto estimates: %s", {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()})
    return res


def auto_state(data_dir: Path) -> dict | None:
    import json as _json
    p = gate._dir(data_dir) / "auto_state.json"
    try:
        return _json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


def _configured() -> bool:
    from app.plugins.qveris import provider as qp
    return bool(qp._api_key())
