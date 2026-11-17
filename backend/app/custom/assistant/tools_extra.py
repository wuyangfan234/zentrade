"""AI 助手查询工具 (第二批) — 模拟持仓 / 财报 / 财务 / 新闻 / 机会板, 以及修复用的辅助函数。

全部只读、有界, 与 tools.py 同一契约 (同步函数, 由 execute_assistant_tool 挪出事件循环)。
每个结果都带数据时点 (as_of / 日期 / 更新时刻), 让模型能说清"这是哪一刻的数据"。

拆出这个模块是为了不让 tools.py 继续膨胀; 工具注册仍集中在 tools.py 的 _LOCAL_TOOLS。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.market_clock import us_clock

# ----------------------------------------------------------------
# 参考 ETF (SPY/QQQ/DIA/IWM): 实时缓存为空时回退本地指数日K收盘
# ----------------------------------------------------------------


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _r(value: Any, nd: int = 4) -> float | None:
    number = _finite(value)
    return None if number is None else round(number, nd)


def core_index_symbols() -> tuple[str, ...]:
    from app.services.index_const import CORE_INDEX_SYMBOLS

    return CORE_INDEX_SYMBOLS


def index_rows(repo: Any, quote_service: Any) -> tuple[list[dict[str, Any]], str]:
    """参考 ETF 行情: 优先实时缓存, 为空回退 kline_index_daily 最近两根收盘 (与页面侧栏同口径)。

    返回 (rows, source)。change_pct / amplitude 一律为百分数 (1.23 = 1.23%)。
    source: realtime (实时缓存) / close (本地收盘, 行内 date 为收盘日) / unavailable。
    """
    from app.services.index_const import CORE_INDEX_NAMES

    symbols = list(core_index_symbols())
    if quote_service is not None:
        try:
            df = quote_service.get_index_quotes(symbols)
        except Exception:
            df = pl.DataFrame()
        if df is not None and not df.is_empty():
            rows = []
            for row in df.to_dicts():
                rows.append({
                    "symbol": row.get("symbol"),
                    "name": CORE_INDEX_NAMES.get(str(row.get("symbol")), row.get("name") or ""),
                    "last_price": _r(row.get("last_price") if row.get("last_price") is not None else row.get("close")),
                    "prev_close": _r(row.get("prev_close")),
                    "change_pct": _r(row.get("change_pct")),
                    "change_amount": _r(row.get("change_amount")),
                    "amplitude": _r(row.get("amplitude")),
                    "high": _r(row.get("high")),
                    "low": _r(row.get("low")),
                    "amount": _r(row.get("amount"), 0),
                    "source": "realtime",
                })
            return rows, "realtime"

    if repo is None:
        return [], "unavailable"
    end = us_clock.today()
    start = end - timedelta(days=20)
    rows = []
    for symbol in symbols:
        try:
            df = repo.get_daily_asset("index", symbol, start, end)
        except Exception:
            df = pl.DataFrame()
        if df is None or df.is_empty() or "close" not in df.columns:
            continue
        df = df.sort("date")
        last = df.tail(1).to_dicts()[0]
        prev = df.tail(2).head(1).to_dicts()[0] if df.height >= 2 else {}
        close, prev_close = _finite(last.get("close")), _finite(prev.get("close"))
        high, low = _finite(last.get("high")), _finite(last.get("low"))
        change = change_pct = amplitude = None
        if close is not None and prev_close:
            change = close - prev_close
            change_pct = change / prev_close * 100
            if high is not None and low is not None:
                amplitude = (high - low) / prev_close * 100
        rows.append({
            "symbol": symbol,
            "name": CORE_INDEX_NAMES.get(symbol, ""),
            "last_price": _r(close),
            "prev_close": _r(prev_close),
            "change_pct": _r(change_pct),
            "change_amount": _r(change),
            "amplitude": _r(amplitude),
            "high": _r(high),
            "low": _r(low),
            "amount": _r(last.get("amount"), 0),
            "date": str(last.get("date"))[:10] if last.get("date") is not None else None,
            "source": "close",
        })
    return rows, ("close" if rows else "unavailable")


def is_core_index(symbol: str) -> bool:
    return symbol in core_index_symbols()


# ----------------------------------------------------------------
# 模拟持仓 (纸面台账, 只读)
# ----------------------------------------------------------------

_EXIT_REASON = {
    "invalidation": "跌破止损",
    "trailing_stop": "跌破移动止损",
    "watchlist_removed": "移出自选",
}
_MARK_LOOKBACK_DAYS = 10


def _pending_exits(events: list[dict], positions: list[dict]) -> dict[str, dict]:
    """每个在场持仓: 本回合建仓之后、最近一条 exit_intent (收盘登记, 次一交易日开盘按开盘价模拟成交)。"""
    out: dict[str, dict] = {}
    for pos in positions:
        symbol = pos["symbol"]
        mine = [e for e in events if e.get("symbol") == symbol]
        opens = [e for e in mine if e.get("kind") == "fill" and e.get("side") in ("buy", "short")]
        entry_seq = opens[-1].get("seq", 0) if opens else 0
        intents = [e for e in mine if e.get("kind") == "exit_intent" and (e.get("seq") or 0) > entry_seq]
        closes = [e for e in mine if e.get("kind") == "fill" and e.get("side") in ("sell", "cover")
                  and (e.get("seq") or 0) > (intents[-1].get("seq", 0) if intents else 1 << 60)]
        if intents and not closes:
            last = intents[-1]
            out[symbol] = {
                "reason": last.get("reason"),
                "reason_label": _EXIT_REASON.get(str(last.get("reason")), "计划离场"),
                "signal_price": _r(last.get("signal_price")),
                "trade_date": last.get("trade_date"),
                "logged_at": last.get("at"),
            }
    return out


def get_portfolio(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """模拟账户持仓 (纸面台账): 股数、成本、止损、最近收盘、浮动盈亏、距止损、待执行离场意向。"""
    from app.ledger.core import Account, Ledger, position_value

    if ctx.data_dir is None:
        raise ValueError("数据目录未就绪。")
    ledger_dir = Path(ctx.data_dir) / "ledger"
    if not (ledger_dir / "account.json").exists():
        return {"count": 0, "rows": [], "note": "模拟台账未初始化。"}
    lg = Ledger(data_dir=ledger_dir, read_only=True)
    lg.replay(Account.from_file(ledger_dir / "account.json"), persist=False)
    snap = lg.snapshot()
    positions = snap["positions"]
    events = snap["events"]
    pending = _pending_exits(events, positions)
    today = us_clock.today()
    rows: list[dict[str, Any]] = []
    marks: dict[str, float] = {}
    nav = float(snap["cash"])
    mark_dates: set[str] = set()
    names = ctx.repo.get_name_map([p["symbol"] for p in positions]) if ctx.repo is not None and positions else {}
    for pos in positions:
        symbol = pos["symbol"]
        qty = float(pos["qty"])
        avg = float(pos["avg_cost"])
        stop = _finite(pos.get("stop_price"))
        close = close_date = None
        if ctx.repo is not None:
            try:
                df = ctx.repo.get_daily(symbol, today - timedelta(days=_MARK_LOOKBACK_DAYS), today)
            except Exception:
                df = pl.DataFrame()
            if df is not None and not df.is_empty():
                df = df.sort("date")
                close = _finite(df["close"][-1])
                close_date = str(df["date"][-1])[:10]
                split_after = getattr(lg, "split_factor_after", None)
                if close is not None and callable(split_after):
                    factor = _finite(split_after(symbol, close_date))
                    if factor:
                        close = close / factor
        mark = close if close is not None else avg
        marks[symbol] = mark
        nav += position_value(qty, mark, avg, bool(pos.get("contract", False)))
        if close_date:
            mark_dates.add(close_date)
        side = "short" if qty < 0 else "long"
        pnl = (mark - avg) * qty
        dist = None
        if close is not None and stop is not None and close:
            dist = (close - stop) / close * 100 if side == "long" else (stop - close) / close * 100
        rows.append({
            "symbol": symbol,
            "name": names.get(symbol, ""),
            "side": side,
            "qty": abs(qty),
            "avg_cost": _r(avg),
            "close": _r(close),
            "close_date": close_date,
            "stop": _r(stop),
            "pnl": _r(pnl, 2),
            "pnl_pct": _r((mark / avg - 1) * 100 * (1 if side == "long" else -1), 2) if avg else None,
            "stop_distance_pct": _r(dist, 2),
            "below_stop": bool(dist is not None and dist < 0),
            "plan_risk": _r(abs(qty) * max((avg - stop) if side == "long" else (stop - avg), 0.0), 2)
            if stop is not None else None,
            "entry_date": pos.get("entry_date"),
            "days_held": us_clock.days_held(date.fromisoformat(pos["entry_date"]), today) if pos.get("entry_date") else None,
            "pending_exit": pending.get(symbol),
        })
    rows.sort(key=lambda r: (not r["pending_exit"], r["stop_distance_pct"] if r["stop_distance_pct"] is not None else 1e9))
    return {
        "count": len(rows),
        "rows": rows,
        "cash": _r(snap["cash"], 2),
        "nav": _r(nav, 2),
        "open_risk": _r(snap["open_risk"], 2),
        "unrealized_pnl": _r(sum(r["pnl"] or 0.0 for r in rows), 2),
        "as_of": max(mark_dates) if mark_dates else None,
        "ledger_last_event_at": events[-1].get("at") if events else None,
        "note": "纸面模拟账户, 不是实盘; 收盘价估值, 盘中现价以持仓页为准。"
                + ("" if rows else " 当前无持仓。"),
    }


# ----------------------------------------------------------------
# 财报日历与历史反应
# ----------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_TIMING = {"bmo": "盘前", "amc": "盘后", "dmh": "盘中"}


def get_earnings(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """财报日历 (未来约两周) + 历史财报反应。symbols 为空时 = 自选 + 模拟持仓。"""
    from app.custom.assistant.tools import _validate_symbol

    if ctx.data_dir is None:
        raise ValueError("数据目录未就绪。")
    data_dir = Path(ctx.data_dir)
    raw = args.get("symbols") or []
    if raw and not isinstance(raw, list):
        raise ValueError("symbols 必须是代码数组。")
    symbols = [_validate_symbol(s) for s in raw[:50]]
    scope = "指定"
    if not symbols:
        symbols = _default_universe(ctx)
        scope = "自选+持仓"
    cal = _read_json(data_dir / "reference" / "earnings" / "calendar.json") or {}
    events = cal.get("events") or {}
    hist_doc = _read_json(data_dir / "intel" / "earnings_history.json") or {}
    hist = hist_doc.get("symbols") or {}
    rows = []
    for symbol in symbols:
        ev = events.get(symbol) if isinstance(events, dict) else None
        h = hist.get(symbol) or {}
        if not ev and not h:
            continue
        last = h.get("last") or {}
        rows.append({
            "symbol": symbol,
            "next_date": (ev or {}).get("date"),
            "next_timing": _TIMING.get(str((ev or {}).get("timing")), (ev or {}).get("timing")),
            "history_n": h.get("n"),
            "median_abs_move_pct": h.get("median_abs_move"),
            "max_abs_move_pct": h.get("max_abs_move"),
            "beat_n": h.get("beat_n"),
            "surprise_n": h.get("surprise_n"),
            "last_report": {k: last.get(k) for k in ("date", "eps", "forecast", "surprise_pct", "move_pct")} if last else None,
        })
    rows.sort(key=lambda r: (r["next_date"] is None, r["next_date"] or "", r["symbol"]))
    upcoming = [r for r in rows if r["next_date"]]
    return {
        "scope": scope,
        "checked": len(symbols),
        "count": len(rows),
        "upcoming": len(upcoming),
        "rows": rows[:60],
        "calendar_window": cal.get("window"),
        "calendar_fetched_at": cal.get("fetched_at"),
        "history_computed_at": hist_doc.get("computed_at"),
        "note": "历史反应 = 财报后首个交易日的涨跌幅绝对值; 日历只覆盖 calendar_window 区间, 区间外的财报不在表内。",
    }


def _default_universe(ctx: Any) -> list[str]:
    symbols: list[str] = []
    try:
        from app.services import watchlist as watchlist_service

        symbols += [str(i.get("symbol") or "") for i in watchlist_service.list_symbols() if i.get("symbol")]
    except Exception:
        pass
    try:
        held = get_portfolio({}, ctx).get("rows") or []
        symbols += [r["symbol"] for r in held]
    except Exception:
        pass
    return list(dict.fromkeys(s for s in symbols if s))


# ----------------------------------------------------------------
# 财务指标 (SEC EDGAR 主源 / Finnhub 备源, 只取已公告的期)
# ----------------------------------------------------------------

_PCT_FIELDS = ("roe", "gross_margin", "net_margin", "revenue_yoy", "net_income_yoy", "debt_to_asset_ratio")


def get_financials(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """个股财务: 最近 4 期已公告的关键指标与利润表 (比率转成百分数)。"""
    from app.custom.assistant.tools import _validate_symbol
    from app.services.stock_analyzer import _load_financials

    if ctx.data_dir is None:
        raise ValueError("数据目录未就绪。")
    symbol = _validate_symbol(args.get("symbol"))
    if is_core_index(symbol):
        return {"symbol": symbol, "periods": [], "note": "参考 ETF 没有公司财务数据。"}
    fin = _load_financials(Path(ctx.data_dir), symbol, today=us_clock.today())
    metrics = {(r.get("period_end"), r.get("period_type")): r for r in fin.get("metrics") or []}
    income = {(r.get("period_end"), r.get("period_type")): r for r in fin.get("income") or []}
    keys = sorted(set(metrics) | set(income), key=lambda k: (str(k[0]), str(k[1])))
    periods = []
    for key in keys[-4:]:
        m, i = metrics.get(key) or {}, income.get(key) or {}
        row: dict[str, Any] = {
            "period_end": key[0],
            "period_type": {"Q": "季度", "FY": "年度", "A": "年度"}.get(str(key[1]), key[1]),
            "announce_date": m.get("announce_date") or i.get("announce_date"),
            "source": m.get("source") or i.get("source"),
        }
        for field in _PCT_FIELDS:
            v = _finite(m.get(field))
            row[f"{field}_pct"] = round(v * 100, 2) if v is not None else None
        row["bps"] = _r(m.get("bps"))
        for field in ("revenue", "gross_profit", "operating_income", "net_income"):
            v = _finite(i.get(field))
            row[f"{field}_usd_bn"] = round(v / 1e9, 3) if v is not None else None
        row["eps_diluted"] = _r(i.get("eps_diluted"))
        periods.append(row)
    name = ctx.repo.get_name_map([symbol]).get(symbol, "") if ctx.repo is not None else ""
    if not periods:
        return {"symbol": symbol, "name": name, "periods": [], "note": "本地没有该股已公告的财务数据。"}
    return {
        "symbol": symbol,
        "name": name,
        "periods": periods,
        "latest_announce": periods[-1]["announce_date"],
        "note": "只含公告日不晚于今天的期; 百分比字段已是百分数; 金额单位十亿美元。",
    }


# ----------------------------------------------------------------
# 个股新闻 (近 N 天, 已翻译打标)
# ----------------------------------------------------------------

_IMP_RANK = {"high": 0, "medium": 1, "low": 2}


def get_news(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """个股近 N 天新闻: 只取主角新闻 (打标 primary 或标题点名), 按重要度再按时间排序。"""
    from app.custom.assistant.tools import _validate_symbol
    from app.services import news_store

    if ctx.data_dir is None:
        raise ValueError("数据目录未就绪。")
    symbol = _validate_symbol(args.get("symbol"))
    try:
        days = max(1, min(int(args.get("days") or 7), 14))
    except (TypeError, ValueError):
        days = 7
    try:
        limit = max(1, min(int(args.get("limit") or 12), 30))
    except (TypeError, ValueError):
        limit = 12
    views = news_store.for_symbol(Path(ctx.data_dir), symbol, days=days, limit=2000)
    main = [v for v in views if news_store.is_main(v)]
    main.sort(key=lambda v: (_IMP_RANK.get(v.get("importance") or "low", 3),
                             v["age_hours"] if v.get("age_hours") is not None else 1e9))
    items = [{
        "published_at": v.get("published_at"),
        "age_hours": v.get("age_hours"),
        "headline": v.get("headline"),
        "summary": (v.get("summary") or "")[:200],
        "event": v.get("event_label") or v.get("event_type"),
        "direction": {"bullish": "利多", "bearish": "利空", "neutral": "中性"}.get(str(v.get("direction"))),
        "importance": {"high": "高", "medium": "中", "low": "低"}.get(str(v.get("importance"))),
        "sources": v.get("sources"),
    } for v in main[:limit]]
    tone = news_store.tone(views)
    return {
        "symbol": symbol,
        "days": days,
        "total": len(views),
        "main": len(main),
        "count": len(items),
        "bearish_48h": tone.get("bearish"),
        "bullish_48h": tone.get("bullish"),
        "rows": items,
        "note": "方向与重要度来自模型打标, 只作参考; 原文以来源为准。",
    }


# ----------------------------------------------------------------
# 机会板 (只读; 区分可下单与仅参考)
# ----------------------------------------------------------------

_SKIP = {
    "holding_other": "已持仓", "cooldown": "刚卖出暂不买回", "earnings": "临近财报", "direction_conflict": "多空冲突",
    "paper_watch": "账户限制", "premarket_beyond_stop": "盘前已破止损", "entry_closed": "今日入场已截止",
}


def _card_row(c: dict) -> dict[str, Any]:
    verdict = c.get("verdict")
    if verdict == "holding":
        status = "已持仓"
    elif verdict == "skip":
        status = "不做 · " + _SKIP.get(str(c.get("verdict_reason") or c.get("blocked")), "其他")
    elif c.get("state") == "in_zone":
        status = "已到进场价"
    else:
        status = "等进场"
    return {
        "symbol": c.get("symbol"),
        "side": {"long": "做多", "short": "做空"}.get(str(c.get("side")), c.get("side")),
        "status": status,
        "entry_zone": [_r(x) for x in (c.get("entry_zone") or [])],
        "stop": _r(c.get("stop")),
        "target": _r(c.get("target")),
        "price": _r(c.get("price")),
        "price_asof": c.get("price_asof"),
        "qty": c.get("qty"),
        "rr": _r(c.get("rr"), 2),
        "strategies": [s.get("name") or s.get("id") for s in (c.get("strategies") or []) if isinstance(s, dict)],
        "signal_day": c.get("signal_day"),
        "entry_day": f"{c.get('entry_day_n')}/{c.get('entry_days')}" if c.get("entry_days") else None,
    }


def get_opportunities(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """机会板 (策略命中, 可进入纸面下单) + 今日多空模型名单 (仅参考)。只读页面同一份聚合数据。"""
    from app.services import opp_page

    if ctx.data_dir is None:
        raise ValueError("数据目录未就绪。")
    page = opp_page.build(Path(ctx.data_dir), repo=ctx.repo, loop=None)
    swing = page.get("swing") or {}
    cards = [*(swing.get("long") or []), *(swing.get("short") or [])]
    board = [_card_row(c) for c in cards]
    lsr = page.get("ls_rank") or {}
    model = {
        "day": lsr.get("day"),
        "as_of": lsr.get("as_of"),
        "long": [{"symbol": r.get("symbol"), "rank": r.get("rank")} for r in (lsr.get("long") or [])[:10]],
        "short": [{"symbol": r.get("symbol"), "rank": r.get("rank")} for r in (lsr.get("short") or [])[:10]],
    } if lsr else None
    mood = ((page.get("mood") or {}).get("judgment") or {})
    return {
        "board_as_of": swing.get("as_of"),
        "phase": (page.get("phase") or {}).get("label"),
        "board": board,
        "board_counts": {
            "total": len(board),
            "holding": sum(1 for r in board if r["status"] == "已持仓"),
            "in_zone": sum(1 for r in board if r["status"] == "已到进场价"),
            "waiting": sum(1 for r in board if r["status"] == "等进场"),
            "skip": sum(1 for r in board if r["status"].startswith("不做")),
        },
        "model_watch": model,
        "market_view": {"day": mood.get("day"), "stance": mood.get("stance"), "probs": mood.get("probs"),
                        "summary": mood.get("summary")} if mood else None,
        "errors": sorted((page.get("errors") or {}).keys()),
        "note": "机会板 = 策略命中, 会进入纸面模拟下单; 今日多空名单与大盘判断 = 模型观察, 仅参考, 不据此下单。两者不合成分数。",
    }


# ----------------------------------------------------------------
# 因子现算 (物化列里没有时)
# ----------------------------------------------------------------

_HISTORY_DAYS = 300


def factor_frame(repo: Any, factor: str) -> tuple[pl.DataFrame, Any, str]:
    """因子在最新交易日的截面 (symbol, factor, change_pct)。

    顺序: 物化列 → 策略引擎同一现算函数 (最新截面) → 带历史窗口现算 → 财务因子 (公告日门控)。
    返回 (frame, as_of, method); 都算不出抛 ValueError (带人话原因)。
    """
    from app.factors.registry import get_factor
    from app.strategy.scoring import materialize_scoring_columns

    latest, as_of = repo.get_enriched_latest()
    if latest is None or latest.is_empty():
        raise ValueError("本地暂无行情数据, 无法读取因子值。")
    spec = get_factor(factor)
    if factor in latest.columns and spec is not None:
        # 物化列可能与 change_pct 等基础列同名 (如 change_pct 本身)
        return _cross_section(latest, factor), as_of, "materialized"
    if spec is None:
        raise ValueError(f"没有 id 为 {factor} 的因子, 请先用 list_factors 确认因子 id。")

    from app.backtest.fundamentals import (
        FUNDAMENTAL_FACTOR_NAMES,
        attach_fundamental_factors,
        load_fundamental_snapshot,
    )

    if factor in FUNDAMENTAL_FACTOR_NAMES:
        snap = load_fundamental_snapshot(Path(repo.store.data_dir))
        if snap is None:
            raise ValueError("本地没有财务数据, 无法计算财务因子。")
        panel = latest.select([c for c in ("symbol", "date", "close", "change_pct") if c in latest.columns]).sort(["symbol", "date"])
        frame = attach_fundamental_factors(panel, snap, [factor])
        return _cross_section(frame, factor), as_of, "fundamental"

    try:
        frame = materialize_scoring_columns(latest, [factor])
    except Exception:
        frame = latest
    if factor in frame.columns and frame[factor].drop_nulls().len() > 0:
        return _cross_section(frame, factor), as_of, "computed"

    from app.services.screener import ScreenerService

    hist = ScreenerService(repo)._load_enriched_history(as_of, _HISTORY_DAYS)
    if hist is None or hist.is_empty():
        raise ValueError(f"因子 {factor} 需要历史窗口, 本地历史数据不足。")
    frame = materialize_scoring_columns(hist, [factor])
    if factor not in frame.columns:
        raise ValueError(f"因子 {factor} 无法在本地数据上计算 (依赖列缺失)。")
    frame = frame.filter(pl.col("date") == as_of)
    if frame[factor].drop_nulls().len() == 0:
        raise ValueError(f"因子 {factor} 在 {as_of} 没有有效值 (历史窗口不足或数据缺失)。")
    return _cross_section(frame, factor), as_of, "computed_history"


def _cross_section(frame: pl.DataFrame, factor: str) -> pl.DataFrame:
    cols = ["symbol", factor] + (["change_pct"] if "change_pct" in frame.columns and factor != "change_pct" else [])
    out = frame.select(cols)
    if factor == "change_pct":
        out = out.with_columns(pl.col("change_pct").alias("_chg"))
    return out


# ----------------------------------------------------------------
# 策略执行结果瘦身 (原始行 70 列, 30 行约 5 万字符)
# ----------------------------------------------------------------

STRATEGY_ROW_FIELDS = (
    "symbol", "name", "close", "change_pct", "score", "amount", "turnover_rate",
    "rsi_14", "ma20", "ma60", "momentum_20d", "vol_ratio_5d", "atr_14", "high_60d", "low_60d",
)


def slim_strategy_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in STRATEGY_ROW_FIELDS if k in row}
    for key, value in row.items():
        # 策略自带的命中说明 (reason / signal / setup 等文本字段) 保留
        if (
            key not in out
            and isinstance(value, str)
            and key not in ("date",)
            and len(out) < 24
            and any(token in key for token in ("reason", "signal", "setup", "note", "tag"))
        ):
            out[key] = value
    return out


def now_iso() -> str:
    return datetime.now(us_clock.tz).isoformat(timespec="seconds")
