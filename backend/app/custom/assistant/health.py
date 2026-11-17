"""AI 助手体检: 用真实数据把全部注册工具各调一次, 结果写 ops/assistant_health.json。

- 只读; 每日 08:00 ET 由调度跑一次, 设置页/系统状态页可手动触发。
- 只记录、不推送 (体检结果不进任何推送渠道)。
- 每个工具给一个代表性入参; 需要代码的用自选第一只 (没有自选用 AAPL), 参考 ETF 用 SPY。
- 回测是重任务 (子进程), 体检只确认策略目录可读、不实跑回测, 免得每天占一个回测槽。

判定: ok = 调用成功且结果非空; warn = 成功但空 (如持仓提醒为 0 条是正常业务状态, 单列 empty_ok);
error = 工具抛错。系统状态页只在 error 时标红。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.custom.assistant import tools as assistant_tools
from app.market_clock import us_clock
from app.services.fs_utils import atomic_write_text

# 空结果属正常业务状态的工具 (用户没建持仓提醒 / 信号库 / 自选里两周内没财报)
EMPTY_OK = frozenset({"get_lots", "list_signals", "get_earnings"})
SKIP = {"run_backtest": "重任务, 体检只核对策略目录"}
LABEL = {
    "list_factors": "因子目录", "list_strategies": "策略目录", "list_data_capabilities": "数据能力目录",
    "run_backtest": "策略回测", "get_stock_quote": "个股行情", "get_stock_daily": "个股日线",
    "get_stock_analysis": "关键价位", "get_watchlist": "自选", "get_market_overview": "美股总览",
    "get_indices": "参考ETF行情", "get_portfolio": "模拟持仓", "get_lots": "持仓提醒", "get_earnings": "财报日历",
    "get_financials": "财务指标", "get_news": "个股新闻", "get_opportunities": "机会板", "list_signals": "信号库",
    "run_strategy": "执行策略", "get_factor_values": "因子排名",
}


def _path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "ops" / "assistant_health.json"


def latest(data_dir: Path | str) -> dict[str, Any] | None:
    try:
        return json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sample_symbol() -> str:
    try:
        from app.services import watchlist as watchlist_service

        items = watchlist_service.list_symbols()
        for item in items:
            sym = str(item.get("symbol") or "")
            if sym and sym not in ("SPY", "QQQ", "DIA", "IWM"):
                return sym
    except Exception:
        pass
    return "AAPL"


def _args(name: str, symbol: str, strategy_id: str | None) -> dict[str, Any]:
    return {
        "get_stock_quote": {"symbols": [symbol]},
        "get_stock_daily": {"symbol": symbol, "days": 20},
        "get_stock_analysis": {"symbol": symbol},
        "get_financials": {"symbol": symbol},
        "get_news": {"symbol": symbol, "days": 3, "limit": 3},
        "get_factor_values": {"factor": "momentum_5d", "top": 5},
        "run_strategy": {"strategy_id": strategy_id or ""},
    }.get(name, {})


def _non_empty(name: str, result: Any) -> bool:
    if not isinstance(result, dict):
        return bool(result)
    for key in ("rows", "factors", "strategies", "capabilities", "periods", "board"):
        value = result.get(key)
        if isinstance(value, list):
            return len(value) > 0
    if name == "get_market_overview":
        return (result.get("breadth") or {}).get("status") != "unavailable"
    if name == "get_stock_analysis":
        return bool(result.get("levels"))
    return bool(result)


async def run(app_state: Any) -> dict[str, Any]:
    """跑一遍体检并落盘。app_state = FastAPI app.state (repo / quote_service / strategy_engine)。"""
    repo = getattr(app_state, "repo", None)
    if repo is None:
        raise RuntimeError("数据仓库未就绪")
    data_dir = repo.store.data_dir
    engine = getattr(app_state, "strategy_engine", None)
    ctx = assistant_tools.ToolContext.build(
        repo=repo, quote_service=getattr(app_state, "quote_service", None),
        engine=engine, data_dir=data_dir,
    )
    symbol = _sample_symbol()
    strategy_id = None
    if engine is not None:
        from app.services import tool_catalog

        for meta in engine.list_strategies(include_research=False):
            if not tool_catalog.strategy_unavailable_reason(meta) and meta.get("source") == "builtin":
                strategy_id = meta.get("id")
                break
    started_all = time.monotonic()
    rows: list[dict[str, Any]] = []
    for schema in assistant_tools.build_tool_schemas():
        name = schema["function"]["name"]
        if name in SKIP:
            rows.append({"name": name, "label": LABEL.get(name, name), "state": "skip", "detail": SKIP[name], "ms": 0})
            continue
        t0 = time.monotonic()
        payload = await assistant_tools.execute_assistant_tool(name, _args(name, symbol, strategy_id), ctx)
        ms = int((time.monotonic() - t0) * 1000)
        if not payload.get("ok"):
            state, detail = "error", str(payload.get("error") or "失败")[:160]
        elif _non_empty(name, payload.get("result")):
            state, detail = "ok", assistant_tools.summarize_tool_result(name, payload)
        else:
            state = "ok" if name in EMPTY_OK else "warn"
            detail = assistant_tools.summarize_tool_result(name, payload) + (" · 空结果" if state == "warn" else "")
        rows.append({"name": name, "label": LABEL.get(name, name), "state": state, "detail": detail[:160], "ms": ms})
    counts = {k: sum(1 for r in rows if r["state"] == k) for k in ("ok", "warn", "error", "skip")}
    report = {
        "at": us_clock.now().isoformat(timespec="seconds"),
        "symbol": symbol,
        "secs": round(time.monotonic() - started_all, 1),
        "tools": rows,
        "counts": counts,
        "summary": f"{counts['ok']}/{len(rows) - counts['skip']} 正常" + (f" · {counts['skip']} 跳过" if counts["skip"] else "")
                   + (f" · {counts['warn']} 空结果" if counts["warn"] else "")
                   + (f" · {counts['error']} 故障" if counts["error"] else ""),
    }
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(report, ensure_ascii=False, indent=1))
    return report


def status_row(data_dir: Path | str) -> tuple[str, str]:
    """系统状态页一行: (state, summary)。从未体检 → idle。"""
    rep = latest(data_dir)
    if not rep or not rep.get("at"):
        return "idle", "每日 08:00 ET 自动体检一次（只记录，不推送）"  # noqa: RUF001 — 面向用户中文文案
    counts = rep.get("counts") or {}
    bad = [r["label"] for r in rep.get("tools") or [] if r.get("state") == "error"]
    state = "error" if bad else ("warn" if counts.get("warn") else "ok")
    at = str(rep.get("at"))
    summary = f"{at[5:10]} {at[11:16]} ET · {rep.get('summary', '')}"
    if bad:
        summary += " · 故障: " + "、".join(bad[:4])
    return state, summary
