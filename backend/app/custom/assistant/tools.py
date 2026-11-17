"""AI 助手工具层 — 核心工具目录透传 + 面向各页面能力的本地查询工具。

设计原则(为项目服务、只读、有界):
- 每个工具对应已注册的本地查询能力(行情/自选/总览/参考ETF/模拟持仓/持仓提醒/
  财报/财务/新闻/机会板/信号库/个股分析/因子值/跑策略), 全部只读。
- 返回体有界: rows 全部带 limit 钳制, enriched 大表先 filter 再取
  少量行; 数值统一四舍五入 4 位, 日期转 ISO 字符串, 保证 JSON 可序列化。
- 未知的工具名回退到核心 services.tool_catalog(因子/策略/数据能力/回测)。

本模块还提供:
- QUICK_SUGGESTS: 空会话快捷指令。
- summarize_tool_result: 工具结果的足迹卡一行摘要(不回传原始大 JSON)。
"""
from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.custom.assistant import tools_extra
from app.market_time import cn_today
from app.services import tool_catalog

# ----------------------------------------------------------------
# 快捷指令: label 展示、prompt 为点击后直接发送的完整问题。
# ----------------------------------------------------------------
QUICK_SUGGESTS: list[dict[str, str]] = [
    {
        "id": "market-overview",
        "label": "今天市场怎么样？",  # noqa: RUF001 — 面向用户中文文案含全角问号
        "prompt": "帮我看看今天的美股样本总览: 涨跌广度、美元成交额、均线趋势、参考ETF与领涨领跌榜，并说明日期、覆盖范围和缺失数据。",
    },
    {
        "id": "portfolio-check",
        "label": "我的持仓怎么样",
        "prompt": "查一下我的模拟账户持仓: 每只的成本、最近收盘、止损、浮动盈亏和距止损, 标出已跌破止损或待离场的, 并说明数据时点。",
    },
    {
        "id": "watchlist-check",
        "label": "我的自选表现如何",
        "prompt": "查一下我的自选股列表, 按今日涨跌幅排序, 标出表现最好和最差的, 并简要点评。",
    },
    {
        "id": "list-strategies",
        "label": "我有哪些策略？",  # noqa: RUF001
        "prompt": "我目前有哪些策略？分别简述每个策略是做什么的、适用什么资产。",  # noqa: RUF001
    },
    {
        "id": "backtest-pick",
        "label": "回测我的策略",
        "prompt": "先用工具看看我有哪些策略, 然后挑一个回测最近半年, 给出收益、回撤、夏普和胜率的解读。",
    },
    {
        "id": "stock-analyze",
        "label": "分析一只个股",
        "prompt": "帮我分析 AAPL: 结合分时与日K看最新走势, 给出关键价位(支撑/压力)、趋势状态和量价特征。",
    },
    {
        "id": "regime-check",
        "label": "当前市场环境",
        "prompt": "用美股总览和 SPY/QQQ/DIA/IWM 参考ETF的实际数据比较当前涨跌广度与量价趋势；数据不足就说明缺失，不推测情绪周期。",
    },
]


# ----------------------------------------------------------------
# 执行上下文 — 由路由从 app.state 注入, 测试可替换为桩对象。
# ----------------------------------------------------------------
@dataclass
class ToolContext:
    repo: Any = None
    quote_service: Any = None
    depth_service: Any = None
    engine: Any = None
    data_dir: Path | None = None

    @classmethod
    def build(
        cls,
        *,
        repo: Any = None,
        quote_service: Any = None,
        depth_service: Any = None,
        engine: Any = None,
        data_dir: str | Path | None = None,
    ) -> ToolContext:
        return cls(
            repo=repo,
            quote_service=quote_service,
            depth_service=depth_service,
            engine=engine,
            data_dir=Path(data_dir) if data_dir else None,
        )


# 美股代码: 1–5 个字母 (含 KO/GS/F 这类短代码), 可带 1–3 字母类别后缀 (BRK.B / BF.A / ATH.PRA)。
# 输入里的小写、BRK-B / BRK/B 写法规范成仓库里的 BRK.B。
_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z]{1,3})?$")
_CLASS_SEP_RE = re.compile(r"^([A-Z]{1,5})[-/]([A-Z]{1,3})$")


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clean_value(value: Any) -> Any:
    """JSON 序列化清洗: 日期转 ISO, 浮点四舍五入并剔除 nan/inf。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 4)
    return value


def _clean_obj(value: Any) -> Any:
    """递归清洗 dict/list 结构(run_strategy 行等动态载荷)。"""
    if isinstance(value, dict):
        return {str(k): _clean_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_obj(v) for v in value]
    return _clean_value(value)


def _rows(
    df: pl.DataFrame | None,
    limit: int,
    columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """DataFrame → 有界的 JSON 行列表; 空/缺列安全。"""
    if df is None or df.is_empty():
        return []
    if columns:
        keep = [c for c in columns if c in df.columns]
        if keep:
            df = df.select(keep)
    if df.width > 60:
        df = df.select(df.columns[:60])
    rows: list[dict[str, Any]] = []
    for row in df.head(limit).to_dicts():
        rows.append({key: _clean_value(value) for key, value in row.items()})
    return rows


_QUOTE_COLUMNS = (
    "symbol", "close", "open", "high", "low", "volume", "amount",
    "prev_close", "change_pct", "change_amount", "amplitude", "turnover_rate",
)


def _asset_quote_frame(ctx: ToolContext, symbols: list[str]) -> pl.DataFrame:
    """symbols 的行情快照: 股票取 get_quotes_compat, ETF / 指数取各自的 enriched 缓存。

    get_quotes_compat 只含股票 enriched 缓存; ETF 与指数是独立缓存, 与自选页
    /api/watchlist/enriched 一样须按资产类型分流, 否则 ETF / 指数永远查不到价格。
    只对股票缓存里没有的代码判定资产类型, 全是股票时不加载 ETF / 指数缓存。
    """
    frames: list[pl.DataFrame] = []
    df = ctx.quote_service.get_quotes_compat()
    if df is not None and not df.is_empty() and "symbol" in df.columns:
        frames.append(df.filter(pl.col("symbol").is_in(symbols)))
    found = set(frames[0]["symbol"].to_list()) if frames else set()
    missing = [s for s in symbols if s not in found]
    if missing and ctx.repo is not None:
        by_asset: dict[str, list[str]] = {}
        for symbol in missing:
            # 参考 ETF (SPY/QQQ/DIA/IWM) 盘中不在股票缓存, ETF/指数名单又可能为空 (resolve 判成 stock):
            # 固定走指数表, 与侧栏指数条同一份数据
            asset_type = "index" if tools_extra.is_core_index(symbol) else ctx.repo.resolve_asset_type(symbol)
            if asset_type != "stock":
                by_asset.setdefault(asset_type, []).append(symbol)
        for asset_type, members in by_asset.items():
            asset_df, _ = ctx.repo.get_enriched_latest_asset(asset_type)
            if asset_df is None or asset_df.is_empty() or "symbol" not in asset_df.columns:
                continue
            keep = [c for c in _QUOTE_COLUMNS if c in asset_df.columns]
            frames.append(asset_df.filter(pl.col("symbol").is_in(members)).select(keep))
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def _quotes_map(ctx: ToolContext, symbols: list[str]) -> dict[str, dict[str, Any]]:
    """symbol → {close, change_pct} 轻量行情映射(自选/持仓等合并展示用)。"""
    if ctx.quote_service is None or not symbols:
        return {}
    try:
        df = _asset_quote_frame(ctx, symbols)
    except Exception:  # 行情服务未就绪时静默降级为无价格列
        return {}
    if df is None or df.is_empty() or "symbol" not in df.columns:
        return {}
    keep = [c for c in ("symbol", "close", "change_pct") if c in df.columns]
    out: dict[str, dict[str, Any]] = {}
    for row in df.filter(pl.col("symbol").is_in(symbols)).select(keep).to_dicts():
        out[row["symbol"]] = {k: _clean_value(v) for k, v in row.items() if k != "symbol"}
    return out


def _require_repo(ctx: ToolContext) -> Any:
    if ctx.repo is None:
        raise ValueError("行情数据仓库未就绪, 请确认后端已完成启动。")
    return ctx.repo


def _validate_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    symbol = _CLASS_SEP_RE.sub(r"\1.\2", symbol)
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError(f"无效的证券代码: {symbol!r} (美股代码 1–5 个字母, 类别股如 BRK.B; 示例: KO / AAPL)")
    return symbol


# ----------------------------------------------------------------
# 本地查询工具实现 (全部同步, 由 execute_assistant_tool 挪出事件循环)。
# ----------------------------------------------------------------
def _get_stock_quote(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.quote_service is None:
        raise ValueError("实时行情服务未启用, 无法查询个股快照。")
    raw = args.get("symbols") or []
    if not isinstance(raw, list) or not raw:
        raise ValueError("symbols 不能为空, 请传入证券代码数组 (如 ['AAPL'])。")
    symbols = list(dict.fromkeys(_validate_symbol(s) for s in raw[:50]))
    sub = _asset_quote_frame(ctx, symbols)
    names = ctx.repo.get_name_map(symbols) if ctx.repo is not None else {}
    rows = _rows(sub, 50)
    for row in rows:
        row.setdefault("name", names.get(row.get("symbol"), ""))
    if not rows:
        return {"count": 0, "rows": [], "note": "未匹配到行情, 请确认代码格式 (如 AAPL)。"}
    result: dict[str, Any] = {"count": len(rows), "rows": rows}
    # 单只查询时附当日分时小图; 批量查询 N 只附 N 张图会淹没回答, 不附。
    if len(rows) == 1:
        row = rows[0]
        prev_close = row.get("prev_close")
        if prev_close is None and row.get("close") is not None and row.get("change_pct") is not None:
            # 行情快照无昨收列时按 close/(1+pct) 反推, 供分时图画昨收基准线
            prev_close = round(float(row["close"]) / (1.0 + float(row["change_pct"])), 3)
        chart = _intraday_chart_payload(ctx, symbols[0], str(row.get("name") or ""), prev_close)
        if chart is not None:
            result["charts"] = [chart]
    return result


def _finite_float(value: Any) -> float | None:
    """转 float; None / 非数字 / nan / inf 返回 None, 避免写入非法 JSON。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _num(v: Any) -> float | None:
    number = _finite_float(v)
    return None if number is None else round(number, 3)


def _intraday_chart_payload(
    ctx: ToolContext, symbol: str, name: str, prev_close: float | None = None,
) -> dict[str, Any] | None:
    """当日分时小图 payload — 本地分钟K收盘序列, 非模型生成(结果可核对)。

    点数封顶 240(一个交易日); 本地无分钟数据时返回 None, 不阻断行情回答。
    """
    if ctx.repo is None:
        return None
    try:
        asset_type = ctx.repo.resolve_asset_type(symbol)
        # 取行情快照所在交易日 (昨收基准线也来自快照行): 周末/节假日/开盘前快照停在
        # 最近交易日, 按服务器本地 date.today() 查分钟分区会永远为空; 无快照日期时按北京日期。
        _, trade_date = ctx.repo.get_enriched_latest_asset(asset_type, refresh=False)
        df = ctx.repo.get_minute(symbol, trade_date or cn_today(), asset_type)
    except Exception:  # noqa: BLE001  分钟分区缺失/损坏时降级为无图
        return None
    if df is None or df.is_empty() or "close" not in df.columns or "datetime" not in df.columns:
        return None
    points: list[list[Any]] = []
    for row in df.sort("datetime").to_dicts():
        dt = row.get("datetime")
        close = _finite_float(row.get("close"))
        if dt is None or close is None:
            continue
        t = dt.strftime("%H:%M") if hasattr(dt, "strftime") else str(dt)[-8:-3]
        volume = _finite_float(row.get("volume"))
        points.append([t, round(close, 3), 0.0 if volume is None else round(volume, 2)])
    if len(points) < 2:
        return None
    payload: dict[str, Any] = {
        "kind": "intraday",
        "symbol": symbol,
        "name": name,
        "points": points[-240:],
    }
    if prev_close is not None:
        payload["prev_close"] = prev_close
    return payload


def _daily_kline_chart_payload(symbol: str, name: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """日K 小图 payload — [date, open, high, low, close, volume], 前端渲染蜡烛图。

    点数封顶 120, 避免长区间把 NDJSON 流式事件撑大; 不足 2 根不附图。
    """
    points = [
        [
            str(row["date"]), _num(row.get("open")), _num(row.get("high")),
            _num(row.get("low")), _num(row.get("close")), _num(row.get("volume")),
        ]
        for row in rows
        if row.get("date") is not None and row.get("close") is not None and row.get("open") is not None
    ]
    if len(points) < 2:
        return None
    return {"kind": "daily_kline", "symbol": symbol, "name": name, "points": points[-120:]}


def _daily_frame(repo: Any, symbol: str, start: date, end: date) -> pl.DataFrame:
    """日线: 参考 ETF 在股票表与指数表各有一份, 盘中股票表缺当日 → 取最后日期更新的那份。"""
    df = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)
    if not tools_extra.is_core_index(symbol):
        return df
    alt = repo.get_daily_asset("index", symbol, start, end)
    if alt is None or alt.is_empty():
        return df
    if df is None or df.is_empty() or alt["date"].max() > df["date"].max():
        return alt
    return df


def _get_stock_daily(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    repo = _require_repo(ctx)
    symbol = _validate_symbol(args.get("symbol"))
    days = _clamp(args.get("days"), 10, 500, 60)
    end = cn_today()
    start = end - timedelta(days=int(days * 1.9) + 20)
    df = _daily_frame(repo, symbol, start, end)
    if df is None or df.is_empty():
        return {"symbol": symbol, "rows": [], "note": "本地没有该标的的日线数据。"}
    keep = [
        c for c in (
            "date", "open", "high", "low", "close", "volume", "amount",
            "change_pct", "turnover_rate", "amplitude", "ma5", "ma10", "ma20", "ma60",
        ) if c in df.columns
    ]
    rows = _rows(df.select(keep).tail(days), days)
    names = repo.get_name_map([symbol])
    name = names.get(symbol, "")
    result: dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "count": len(rows),
        "rows": rows,
    }
    kline = _daily_kline_chart_payload(symbol, name, rows)
    if kline is not None:
        result["charts"] = [kline]
    return result


def _get_stock_analysis(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.indicators.levels import compute_levels, summarize_levels

    repo = _require_repo(ctx)
    symbol = _validate_symbol(args.get("symbol"))
    end = cn_today()
    df = _daily_frame(repo, symbol, end - timedelta(days=500), end)
    if df is None or df.is_empty():
        return {"symbol": symbol, "note": "本地没有该标的的日线数据, 无法分析。"}
    recent = df.tail(120)
    close = float(recent["close"][-1]) if recent.height else None
    levels = compute_levels(recent)
    keep = [
        c for c in (
            "date", "close", "change_pct", "amount", "turnover_rate", "amplitude",
            "ma5", "ma10", "ma20", "ma60", "high_60d", "low_60d",
        ) if c in recent.columns
    ]
    latest_rows = _rows(recent.select(keep).tail(1), 1)
    names = repo.get_name_map([symbol])
    return {
        "symbol": symbol,
        "name": names.get(symbol, ""),
        "close": _clean_value(close),
        "summary": summarize_levels(levels, close),
        "latest": latest_rows[0] if latest_rows else {},
        "levels": _clean_obj(levels),
    }



def _get_watchlist(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.services import watchlist as watchlist_service

    items = watchlist_service.list_symbols()
    if not items:
        return {"count": 0, "rows": [], "note": "自选列表为空。"}
    symbols = [str(i.get("symbol") or "") for i in items if i.get("symbol")]
    quotes = _quotes_map(ctx, symbols)
    names = ctx.repo.get_name_map(symbols) if ctx.repo is not None else {}
    rows = []
    for item in items[:300]:
        symbol = str(item.get("symbol") or "")
        quote = quotes.get(symbol, {})
        rows.append({
            "symbol": symbol,
            "name": names.get(symbol, ""),
            "note": item.get("note") or "",
            "close": quote.get("close"),
            "change_pct": quote.get("change_pct"),
        })
    return {"count": len(rows), "rows": rows}


def _get_market_overview(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.services.market_overview_builder import build_market_overview

    repo = _require_repo(ctx)
    return build_market_overview(repo, quote_service=ctx.quote_service)


def _get_indices(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """参考 ETF: 实时缓存优先; 为空 (重启后到开盘前、休市) 回退本地指数日K收盘, 行内 date 标收盘日。"""
    rows, source = tools_extra.index_rows(ctx.repo, ctx.quote_service)
    if not rows:
        return {"count": 0, "rows": [], "source": "unavailable",
                "note": "实时缓存与本地指数日K都没有数据, 请检查数据页的指数同步。"}
    note = ("盘中实时快照。" if source == "realtime"
            else f"实时行情未在运行 (休市或开盘前), 以下为 {rows[0].get('date')} 收盘数据。")
    return {"count": len(rows), "rows": rows, "source": source, "note": note}




def _get_lots(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.strategy.lots import load_all

    if ctx.data_dir is None:
        raise ValueError("数据目录未就绪。")
    lots = load_all(ctx.data_dir)
    if not lots:
        return {"count": 0, "rows": [],
                "note": "持仓提醒为空。持仓提醒是用户手动记录的提醒, 不是模拟账户持仓; 模拟持仓请用 get_portfolio。"}
    symbols = [str(lot.get("symbol") or "") for lot in lots if lot.get("symbol")]
    quotes = _quotes_map(ctx, symbols)
    names = ctx.repo.get_name_map(symbols) if ctx.repo is not None else {}
    rows = []
    for lot in lots[:100]:
        symbol = str(lot.get("symbol") or "")
        quote = quotes.get(symbol, {})
        rows.append({
            "symbol": symbol,
            "name": names.get(symbol, ""),
            "qty": lot.get("qty"),
            "cost_price": lot.get("cost_price"),
            "buy_date": _clean_value(lot.get("buy_date")),
            "target_pct": lot.get("target_pct"),
            "stop_pct": lot.get("stop_pct"),
            "close": quote.get("close"),
            "change_pct": quote.get("change_pct"),
        })
    return {"count": len(rows), "rows": rows}


def _list_signals(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.strategy.custom_signals import load_all

    if ctx.data_dir is None:
        raise ValueError("数据目录未就绪。")
    rows = [_clean_obj(r) for r in load_all(ctx.data_dir)[:100]]
    return {"count": len(rows), "rows": rows}


def _run_strategy(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.services.screener import ScreenerService
    from app.strategy.config import load_override

    repo = _require_repo(ctx)
    if ctx.engine is None:
        raise ValueError("策略引擎未就绪。")
    strategy_id = str(args.get("strategy_id") or "").strip()
    if not strategy_id:
        raise ValueError("strategy_id 不能为空, 可先用 list_strategies 查看。")
    meta = next((m for m in ctx.engine.list_strategies(include_research=True) if m.get("id") == strategy_id), None)
    if meta is None:
        raise ValueError(f"没有 id 为 {strategy_id} 的策略, 可先用 list_strategies 查看。")
    unavailable = tool_catalog.strategy_unavailable_reason(meta)
    if unavailable:
        raise ValueError(f"策略 {meta.get('name') or strategy_id} {unavailable}。")
    svc = ScreenerService(repo)
    as_of = svc.latest_date()
    if as_of is None:
        raise ValueError("本地暂无行情数据, 无法执行策略。")
    overrides = load_override(ctx.data_dir, strategy_id) or {} if ctx.data_dir else {}
    context = svc.build_strategy_context(
        ctx.engine, as_of, [strategy_id], overrides_map={strategy_id: overrides},
    )
    result = ctx.engine.run(strategy_id, context, overrides=overrides or None)
    rows = [_clean_obj(tools_extra.slim_strategy_row(r)) for r in (getattr(result, "rows", None) or [])[:30]]
    return {
        "strategy_id": strategy_id,
        "name": meta.get("name") or "",
        "as_of": _clean_value(getattr(result, "as_of", as_of)),
        "total": int(getattr(result, "total", 0) or 0),
        "elapsed_ms": round(float(getattr(result, "elapsed_ms", 0.0) or 0.0)),
        "rows": rows,
    }


def _get_factor_values(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    repo = _require_repo(ctx)
    factor = str(args.get("factor") or "").strip()
    if not factor:
        raise ValueError("factor 不能为空, 可先用 list_factors 查看可用因子 id。")
    top = _clamp(args.get("top"), 5, 50, 30)
    descending = str(args.get("direction") or "desc") != "asc"
    df, as_of, method = tools_extra.factor_frame(repo, factor)
    valid = df.drop_nulls(subset=[factor]).filter(pl.col(factor).is_not_nan() if df.schema[factor].is_float() else pl.lit(True))
    sub = valid.sort(factor, descending=descending).head(top)
    names = repo.get_name_map(sub["symbol"].to_list()) if sub.height else {}
    rows = _rows(sub, top)
    for row in rows:
        row.setdefault("name", names.get(row.get("symbol"), ""))
        if "_chg" in row:
            row.pop("_chg")
    from app.factors.registry import get_factor

    spec = get_factor(factor)
    return {
        "factor": factor,
        "label": spec.label if spec else "",
        "as_of": _clean_value(as_of),
        "valid": valid.height,
        "method": {"materialized": "已物化", "computed": "现算", "computed_history": "按历史窗口现算",
                   "fundamental": "财务公告门控"}.get(method, method),
        "count": len(rows),
        "rows": rows,
    }


_LOCAL_TOOLS: dict[str, Callable[[dict[str, Any], ToolContext], Any]] = {
    "get_stock_quote": _get_stock_quote,
    "get_stock_daily": _get_stock_daily,
    "get_stock_analysis": _get_stock_analysis,
    "get_watchlist": _get_watchlist,
    "get_market_overview": _get_market_overview,
    "get_indices": _get_indices,
    "get_lots": _get_lots,
    "list_signals": _list_signals,
    "run_strategy": _run_strategy,
    "get_factor_values": _get_factor_values,
    "get_portfolio": tools_extra.get_portfolio,
    "get_earnings": tools_extra.get_earnings,
    "get_financials": tools_extra.get_financials,
    "get_news": tools_extra.get_news,
    "get_opportunities": tools_extra.get_opportunities,
}


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _local_tool_schemas() -> list[dict[str, Any]]:
    return [
        _schema(
            "get_stock_quote",
            "查询一只或多只股票(≤50)的实时行情快照: 最新价、涨跌幅、成交额、换手率、振幅等。",
            {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "美股代码数组, 如 ['AAPL', 'MSFT']",
                },
            },
            ["symbols"],
        ),
        _schema(
            "get_stock_daily",
            "查询个股日线行情(近 N 个交易日, ≤500): OHLCV、涨跌幅、换手率与常用均线。",
            {
                "symbol": {"type": "string", "description": "美股代码, 如 AAPL"},
                "days": {"type": "integer", "description": "返回最近 N 个交易日 (10-500, 默认 60)"},
            },
            ["symbol"],
        ),
        _schema(
            "get_stock_analysis",
            "个股分析: 基于 120 日日线计算 11 类关键价位(支撑/压力/枢轴/布林等)与文字摘要, 附最新行情快照。",
            {"symbol": {"type": "string", "description": "证券代码"}},
            ["symbol"],
        ),
        _schema(
            "get_watchlist",
            "查询用户的自选股列表(含备注), 并合并最新价与今日涨跌幅, 用于回答「我的自选表现如何」。",
            {},
            [],
        ),
        _schema(
            "get_market_overview",
            "美股本地样本总览: 参考ETF、涨跌广度、成交额(USD)、涨跌幅分布、均线趋势与领涨领跌榜；含日期、有效样本数和缺失状态，不代表全市场。无参数。",
            {},
            [],
        ),
        _schema(
            "get_indices",
            "查询美股参考ETF行情(SPY/QQQ/DIA/IWM), 返回美元价格与百分数涨跌幅。盘中为实时快照; 休市或开盘前回退最近收盘, 行内 date 为收盘日。",
            {},
            [],
        ),
        _schema(
            "get_portfolio",
            "查询模拟账户(纸面交易台账)的持仓: 股数、成本、止损、最近收盘、浮动盈亏、距止损、待执行的离场意向, 以及净值/现金/计划风险。问「我的持仓/持有哪些股票/离止损多远」用这个。",
            {},
            [],
        ),
        _schema(
            "get_lots",
            "查询用户手动记录的持仓提醒(lots, 持仓提醒页): 不是模拟账户持仓。只有用户明确问「持仓提醒」时才用。",
            {},
            [],
        ),
        _schema(
            "get_earnings",
            "查询财报日历(约未来两周)与历史财报反应(财报后首日涨跌幅中位/最大、超预期次数)。symbols 为空时查自选+模拟持仓。",
            {"symbols": {"type": "array", "items": {"type": "string"}, "description": "美股代码数组, 可空"}},
            [],
        ),
        _schema(
            "get_financials",
            "查询个股最近 4 期已公告财务: ROE、毛利率、净利率、营收/净利同比、资产负债率、营收与净利(十亿美元)、EPS, 附报告期与公告日。",
            {"symbol": {"type": "string", "description": "美股代码, 如 NVDA"}},
            ["symbol"],
        ),
        _schema(
            "get_news",
            "查询个股近 N 天的主角新闻(已翻译打标): 标题、摘要、事件类型、利多/利空、重要度, 按重要度与时间排序。",
            {
                "symbol": {"type": "string", "description": "美股代码"},
                "days": {"type": "integer", "description": "天数 1-14, 默认 7"},
                "limit": {"type": "integer", "description": "条数 1-30, 默认 12"},
            },
            ["symbol"],
        ),
        _schema(
            "get_opportunities",
            "查询机会板(策略命中、会进入纸面模拟下单的卡: 方向、状态、进场区、止损、目标、股数)与今日多空模型名单、大盘判断(这两项仅参考、不下单)。",
            {},
            [],
        ),
        _schema(
            "list_signals",
            "检索用户自定义信号的目录(信号库页): 名称、表达式、分组与启用状态。",
            {},
            [],
        ),
        _schema(
            "run_strategy",
            "以最新交易日数据执行一个选股策略并返回命中标的(≤30 行精简字段)。strategy_id 先用 list_strategies 查询; 标了 unavailable 的策略不能在日线选股里运行。",
            {"strategy_id": {"type": "string", "description": "策略 id"}},
            ["strategy_id"],
        ),
        _schema(
            "get_factor_values",
            "查询某因子在最新交易日的全市场取值排名(Top N), 附股票名称与今日涨跌幅。未物化的因子会现算(需历史窗口的按近 300 天计算, 财务因子按公告日门控)。factor 用因子 id, 可先用 list_factors 查询。",
            {
                "factor": {"type": "string", "description": "因子 id"},
                "top": {"type": "integer", "description": "返回前 N 名 (5-50, 默认 30)"},
                "direction": {"type": "string", "enum": ["desc", "asc"], "description": "排序方向, 默认 desc(从大到小)"},
            },
            ["factor"],
        ),
    ]


def build_tool_schemas() -> list[dict[str, Any]]:
    """核心工具目录(因子/策略/能力/回测) + 本模块的页面查询工具。"""
    return [*tool_catalog.build_tool_schemas(), *_local_tool_schemas()]


async def execute_assistant_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """统一执行入口: 本地工具(to_thread 防阻塞) → 核心工具目录回退。

    契约与核心 tool_catalog.execute_tool 一致: {"ok": bool, "result"|"error"}。
    """
    handler = _LOCAL_TOOLS.get(name)
    if handler is not None:
        try:
            result = await asyncio.to_thread(handler, dict(args or {}), ctx)
            return {"ok": True, "result": result}
        except Exception as exc:  # 工具失败按契约回填给模型, 不打断对话流
            return {"ok": False, "error": str(exc) or exc.__class__.__name__}
    return await tool_catalog.execute_tool(
        name,
        args,
        engine=ctx.engine,
        data_dir=str(ctx.data_dir) if ctx.data_dir else None,
    )


def summarize_tool_result(name: str, payload: dict[str, Any]) -> str:
    """把工具返回压缩成一行人类可读摘要, 用于前端足迹卡。摘要只是展示, 载荷形状意外时退回通用文案, 不抛错。"""
    try:
        return _summarize(name, payload)
    except Exception:
        return "完成" if payload.get("ok") else "失败"


def _summarize(name: str, payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        error = str(payload.get("error") or "执行失败")
        return f"失败: {error[:80]}"

    result = payload.get("result")
    if not isinstance(result, dict):
        return "完成"

    def _count(key: str = "rows") -> int:
        rows = result.get(key)
        return len(rows) if isinstance(rows, list) else int(result.get("count") or 0)

    if name == tool_catalog.LIST_FACTORS:
        factors = result.get("factors")
        return f"返回 {len(factors) if isinstance(factors, list) else 0} 个因子"
    if name == tool_catalog.LIST_STRATEGIES:
        strategies = result.get("strategies")
        return f"返回 {len(strategies) if isinstance(strategies, list) else 0} 个策略"
    if name == tool_catalog.LIST_DATA_CAPABILITIES:
        caps = result.get("capabilities")
        return f"返回 {len(caps) if isinstance(caps, list) else 0} 项能力"
    if name == tool_catalog.RUN_BACKTEST:
        stats = result.get("stats")
        if not isinstance(stats, dict) or not stats:
            return "回测完成(无指标)"
        return " · ".join(str(p) for p in (
            f"总收益 {stats.get('total_return', '-')}",
            f"年化 {stats.get('annual_return', '-')}",
            f"回撤 {stats.get('max_drawdown', '-')}",
            f"夏普 {stats.get('sharpe', '-')}",
        ))
    if name == "get_stock_quote":
        return f"查询 {_count()} 只个股实时行情"
    if name == "get_stock_daily":
        return f"返回 {_count()} 个交易日日线"
    if name == "get_stock_analysis":
        return "完成个股关键价位分析"
    if name == "get_watchlist":
        return f"返回自选 {_count()} 只"
    if name == "get_market_overview":
        breadth = result.get("breadth") or {}
        if result.get("data_status") == "unavailable" or breadth.get("status") == "unavailable":
            return "美股总览: 涨跌广度数据缺失（非平盘/中性）"
        up = breadth.get("up")
        down = breadth.get("down")
        summary = f"美股样本: 涨 {up if up is not None else '缺失'} / 跌 {down if down is not None else '缺失'}"
        if breadth.get("unknown"):
            summary += f" · 涨跌幅缺失 {breadth['unknown']} 只"
        if result.get("data_status") == "partial":
            summary += " · 部分数据缺失"
        return summary
    if name == "get_indices":
        src = {"realtime": "实时", "close": "收盘"}.get(str(result.get("source")), "")
        return f"返回 {_count()} 条参考ETF行情" + (f" · {src}" if src else "")
    if name == "get_portfolio":
        pend = sum(1 for r in result.get("rows") or [] if isinstance(r, dict) and r.get("pending_exit"))
        return f"模拟持仓 {_count()} 只" + (f" · 待离场 {pend}" if pend else "") + (f" · 收盘 {result['as_of']}" if result.get("as_of") else "")
    if name == "get_earnings":
        return f"检查 {result.get('checked', 0)} 只 · 两周内财报 {result.get('upcoming', 0)} 场"
    if name == "get_financials":
        periods = result.get("periods") or []
        return f"{result.get('symbol', '')} 财务 {len(periods)} 期" + (f" · 最近公告 {result['latest_announce']}" if result.get("latest_announce") else "")
    if name == "get_news":
        return f"{result.get('symbol', '')} 近 {result.get('days', 7)} 日主角新闻 {result.get('main', 0)} 条"
    if name == "get_opportunities":
        c = result.get("board_counts") or {}
        return f"机会板 {c.get('total', 0)} 张 · 持仓 {c.get('holding', 0)} · 等进场 {c.get('waiting', 0)}"
    if name == "get_lots":
        return f"返回持仓提醒 {_count()} 条"
    if name == "list_signals":
        return f"返回 {_count()} 个信号定义"
    if name == "run_strategy":
        return f"命中 {result.get('total', 0)} 只 · 耗时 {result.get('elapsed_ms', '-')}ms"
    if name == "get_factor_values":
        return f"因子 {result.get('label') or result.get('factor', '-')} Top{_count()} · 有效 {result.get('valid', 0)} 只"

    return f"返回 {_count()} 行" if _count() else "完成"
