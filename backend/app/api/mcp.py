"""MCP 端点 (0.44.0) — 让外部 agent (Claude Desktop / Claude Code / Cursor / Hermes…) 接入 ZenTrade。

传输: MCP Streamable HTTP, 无状态。POST /api/mcp 收 JSON-RPC 2.0 (单条或批量), 直接回 application/json;
GET 返回 405 (本服务不主动推送)。协议版本按客户端请求协商, 不认识的版本回退 LATEST。

鉴权: 与其他 /api 接口同一中间件 —— Authorization: Bearer zt_... (设置 → API 与 MCP 生成) 或网页会话 Cookie。
工具执行 = 用同一把凭据在进程内调用本服务自己的 /api 路由 (httpx ASGITransport), 所以:
  - 只读密钥的写操作照样被中间件 403, 权限只在一处判定;
  - 工具行为与网页按钮完全一致 (同一路由、同一校验、同一落盘)。

工具 = 若干常用操作 (自选、策略、回测、选股、机会板、持仓) + 两个通用工具:
  zentrade_endpoints 列出全部接口 (来自 OpenAPI), zentrade_api 调任意 /api 接口 —— 覆盖全系统操作。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app import __version__

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = SUPPORTED_VERSIONS[0]
_CALL_TIMEOUT_S = 900.0
_MAX_RESULT_CHARS = 60_000

INSTRUCTIONS = (
    "ZenTrade 是美股波段研究 + 回测 + 纸面交易系统。常用操作用专用工具; 其余任何功能先用 "
    "zentrade_endpoints 按关键词找接口, 再用 zentrade_api 调用。所有价格/日期是美东口径, "
    "回测只用已收盘日K。纸面交易不是实盘。"
)


def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


_STR = {"type": "string"}
_SYMS = {"type": "array", "items": {"type": "string"}, "description": "美股代码, 如 [\"NVDA\", \"AAPL\"]"}

# name → (description, inputSchema, readOnly, builder(args) -> (method, path, query, body))
TOOLS: dict[str, tuple[str, dict, bool, Any]] = {}


def _tool(name: str, desc: str, schema: dict, *, read_only: bool):
    def deco(fn):
        TOOLS[name] = (desc, schema, read_only, fn)
        return fn
    return deco


@_tool("watchlist_list", "列出自选股 (代码、名称、备注、分组)。", _schema({}), read_only=True)
def _wl_list(a):
    return "GET", "/api/watchlist", None, None


@_tool("watchlist_add", "把一只或多只股票加入自选。", _schema({
    "symbols": _SYMS, "note": {**_STR, "description": "备注, 可空"},
    "group_id": {**_STR, "description": "分组 id, 可空 (watchlist_groups 查)"},
}, ["symbols"]), read_only=False)
def _wl_add(a):
    body = {"symbols": a["symbols"], "note": a.get("note", "")}
    if a.get("group_id"):
        body["group_id"] = a["group_id"]
    return "POST", "/api/watchlist/batch", None, body


@_tool("watchlist_remove", "把一只股票移出自选。", _schema({"symbol": _STR}, ["symbol"]), read_only=False)
def _wl_rm(a):
    return "DELETE", f"/api/watchlist/{a['symbol'].strip().upper()}", None, None


@_tool("watchlist_groups", "列出自选分组。", _schema({}), read_only=True)
def _wl_groups(a):
    return "GET", "/api/watchlist/groups", None, None


@_tool("strategy_list", "列出可用策略摘要 (id、名称、说明、来源、参数名)。include_research=true 时含草稿。"
       "完整参数定义用 zentrade_api GET /api/strategies/{id}。", _schema({
           "include_research": {"type": "boolean"},
       }), read_only=True)
def _st_list(a):
    return "GET", "/api/strategies", {"include_research": "true"} if a.get("include_research") else None, None


def _summarize_strategies(data):
    """完整策略列表常超 6 万字符 (含评分与参数定义), 截断会让 agent 看不到后半 (自定义策略在后面)。"""
    if not isinstance(data, dict) or not isinstance(data.get("strategies"), list):
        return data
    rows = []
    for st in data["strategies"]:
        if not isinstance(st, dict):
            continue
        rows.append({
            "id": st.get("id"), "name": st.get("name"), "description": (st.get("description") or "")[:160],
            "source": st.get("source"), "research_only": st.get("research_only", False),
            "timeframes": st.get("timeframes"),
            "params": [p.get("id") for p in st.get("params") or [] if isinstance(p, dict)],
        })
    return {"count": len(rows), "strategies": rows, "load_errors": data.get("load_errors") or []}


_POST = {"strategy_list": _summarize_strategies}


@_tool("strategy_get_source", "读取一个策略的 Python 源码。", _schema({"strategy_id": _STR}, ["strategy_id"]),
       read_only=True)
def _st_src(a):
    return "GET", f"/api/strategies/{a['strategy_id']}/source", None, None


@_tool("strategy_validate", "校验策略源码 (语法、META、安全白名单), 不保存。", _schema({
    "code": _STR, "strategy_id": _STR,
}, ["code"]), read_only=True)
def _st_val(a):
    return "POST", "/api/strategies/code/validate", None, {"code": a["code"], "strategy_id": a.get("strategy_id", "")}


@_tool("strategy_save", "新建 (mode=create) 或修改 (mode=update) 自定义策略源码。先用 strategy_validate 校验; "
       "strategy_id 必须以 custom_ 开头且与源码 META.id 一致, 可参考 strategy_get_source 读到的现有策略写法。", _schema({
    "strategy_id": {**_STR, "description": "custom_ 开头, 如 custom_ma20_breakout"}, "code": _STR,
    "mode": {"type": "string", "enum": ["create", "update"]},
    "name": _STR, "description": _STR,
}, ["strategy_id", "code", "mode"]), read_only=False)
def _st_save(a):
    return "POST", "/api/strategies/code/save", None, {
        "strategy_id": a["strategy_id"], "code": a["code"], "mode": a["mode"],
        "target_source": "custom", "name": a.get("name", ""), "description": a.get("description", ""),
    }


@_tool("strategy_save_params", "保存策略的参数覆盖 (overrides, 如 {\"params\": {...}})。", _schema({
    "strategy_id": _STR, "overrides": {"type": "object"},
}, ["strategy_id", "overrides"]), read_only=False)
def _st_cfg(a):
    return "POST", "/api/strategies/config", None, {"strategy_id": a["strategy_id"], "overrides": a["overrides"]}


@_tool("strategy_delete", "删除自定义策略 (内置策略不可删)。", _schema({"strategy_id": _STR}, ["strategy_id"]),
       read_only=False)
def _st_del(a):
    return "DELETE", f"/api/strategies/{a['strategy_id']}", None, None


@_tool("strategy_screen", "用策略对最新收盘数据选股, 返回命中股票。pool 可限定股票池。", _schema({
    "strategy_id": _STR, "pool": _SYMS, "params": {"type": "object"},
}, ["strategy_id"]), read_only=True)
def _st_run(a):
    body = {"strategy_id": a["strategy_id"]}
    for k in ("pool", "params"):
        if a.get(k):
            body[k] = a[k]
    return "POST", "/api/strategies/run", None, body


@_tool("backtest_strategy", "策略回测 (日K, 已收盘数据)。返回统计与交易; 大区间可能要几分钟。", _schema({
    "strategy_id": _STR, "symbols": _SYMS,
    "start": {**_STR, "description": "YYYY-MM-DD, 默认约 1 年前"},
    "end": {**_STR, "description": "YYYY-MM-DD, 默认最新收盘日"},
    "mode": {"type": "string", "enum": ["position", "full"], "description": "position=仓位模拟, full=全量信号"},
    "holding_days": {"type": "integer", "minimum": 1, "maximum": 60},
    "max_positions": {"type": "integer", "minimum": 1, "maximum": 100},
    "initial_capital": {"type": "number"},
    "params": {"type": "object"},
}, ["strategy_id"]), read_only=True)
def _bt(a):
    body = {k: v for k, v in a.items() if v not in (None, "", [])}
    return "POST", "/api/backtest/strategy/run", None, body


@_tool("opportunities_page", "机会页聚合数据 (波段板多空卡、盘中强势、大盘、时段)。", _schema({}), read_only=True)
def _opp(a):
    return "GET", "/api/opportunities/page", None, None


@_tool("portfolio_overview", "纸面账户概览 (现金、持仓、浮动盈亏)。", _schema({}), read_only=True)
def _pf(a):
    return "GET", "/api/portfolio/overview", None, None


@_tool("kline_daily", "读一只股票的日K (enriched, 含常用指标)。", _schema({
    "symbol": _STR, "days": {"type": "integer", "minimum": 10, "maximum": 2000},
}, ["symbol"]), read_only=True)
def _kd(a):
    return "GET", "/api/kline/daily", {"symbol": a["symbol"], "days": str(a.get("days", 120))}, None


@_tool("zentrade_endpoints", "列出 ZenTrade 全部 HTTP 接口 (方法、路径、说明)。可用 keyword 过滤, 如 'rules'、'mining'。",
       _schema({"keyword": _STR}), read_only=True)
def _eps(a):
    return "LOCAL", "endpoints", None, a


@_tool("zentrade_api", "调用任意 ZenTrade /api 接口 (全系统操作的通用入口)。先用 zentrade_endpoints 查路径与参数。"
       "只读密钥只能 GET 与少数纯计算 POST。", _schema({
           "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
           "path": {**_STR, "description": "以 /api/ 开头, 如 /api/opportunities/rules"},
           "query": {"type": "object", "description": "查询参数"},
           "body": {"description": "JSON 请求体"},
       }, ["method", "path"]), read_only=False)
def _api(a):
    return a["method"].upper(), a["path"], a.get("query"), a.get("body")


def _endpoints(app, keyword: str = "") -> dict:
    spec = app.openapi()
    kw = (keyword or "").strip().lower()
    rows = []
    for path, ops in (spec.get("paths") or {}).items():
        if not path.startswith("/api/") or path.startswith(("/api/mcp", "/api/auth", "/api/settings/api-keys")):
            continue
        for method, op in ops.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            summary = (op.get("summary") or "").strip()
            desc = (op.get("description") or "").strip().splitlines()[0:1]
            text = f"{method.upper()} {path} {summary} {' '.join(desc)}"
            if kw and kw not in text.lower():
                continue
            params = [p.get("name") for p in op.get("parameters") or [] if p.get("in") == "query"]
            body_ref = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
            rows.append({
                "method": method.upper(), "path": path, "summary": summary or (desc[0] if desc else ""),
                **({"query": params} if params else {}),
                **({"body_schema": body_ref.get("$ref", "").rsplit("/", 1)[-1] or "object"} if body_ref else {}),
            })
    return {"count": len(rows), "endpoints": rows[:400],
            "note": "完整参数定义见 GET /openapi.json (components.schemas)"}


async def _call_api(request: Request, method: str, path: str, query: dict | None, body: Any) -> dict:
    if not isinstance(path, str) or not path.startswith("/api/") or ".." in path:
        return {"status": 400, "error": "path 必须以 /api/ 开头"}
    if path.startswith("/api/mcp"):
        return {"status": 400, "error": "不能从 MCP 调用 MCP 自身"}
    headers = {}
    for h in ("authorization", "x-api-key", "cookie"):
        if request.headers.get(h):
            headers[h] = request.headers[h]
    transport = httpx.ASGITransport(app=request.app, client=(request.client.host if request.client else "127.0.0.1", 0))
    async with httpx.AsyncClient(transport=transport, base_url="http://zentrade.internal", timeout=_CALL_TIMEOUT_S) as c:
        resp = await c.request(method, path, params={k: v for k, v in (query or {}).items() if v is not None},
                               json=body if body is not None and method not in {"GET", "DELETE"} else None,
                               headers=headers)
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        try:
            data: Any = resp.json()
        except ValueError:
            data = resp.text
    else:
        data = resp.text
    return {"status": resp.status_code, "data": data}


def _text_result(payload: Any, is_error: bool) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > _MAX_RESULT_CHARS:
        # 截断后仍是合法 JSON: 原文放进 partial 字段, agent 能解析也知道被截了
        text = json.dumps({"truncated": True, "original_chars": len(text),
                           "hint": "结果过长已截断; 用更窄的参数 (更短区间、更少股票、keyword) 重试",
                           "partial": text[:_MAX_RESULT_CHARS]}, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _tools_list() -> list[dict]:
    out = []
    for name, (desc, schema, ro, _fn) in TOOLS.items():
        out.append({
            "name": name, "description": desc, "inputSchema": schema,
            "annotations": {"readOnlyHint": ro, "destructiveHint": name in {"strategy_delete", "watchlist_remove"}},
        })
    return out


async def _handle(msg: Any, request: Request) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    mid = msg.get("id")
    method = msg["method"]
    params = msg.get("params") or {}
    if mid is None:  # notification (notifications/initialized 等): 不回包
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        want = str(params.get("protocolVersion") or "")
        return ok({
            "protocolVersion": want if want in SUPPORTED_VERSIONS else LATEST,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "zentrade", "title": "ZenTrade", "version": __version__},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": _tools_list()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            return err(-32602, f"未知工具: {name}")
        if not isinstance(args, dict):
            return err(-32602, "arguments 必须是对象")
        _desc, schema, _ro, fn = TOOLS[name]
        missing = [k for k in schema.get("required", []) if k not in args]
        if missing:
            return ok(_text_result({"error": f"缺少参数: {', '.join(missing)}"}, True))
        try:
            method_, path, query, body = fn(args)
            if method_ == "LOCAL":
                return ok(_text_result(_endpoints(request.app, (body or {}).get("keyword", "")), False))
            res = await _call_api(request, method_, path, query, body)
        except Exception as e:
            logger.warning("mcp tool %s failed: %s", name, type(e).__name__)
            return ok(_text_result({"error": f"{type(e).__name__}: {e}"[:500]}, True))
        status = res.get("status", 500)
        if status < 400 and name in _POST:
            res = {**res, "data": _POST[name](res.get("data"))}
        return ok(_text_result(res if status >= 400 else res.get("data"), status >= 400))
    if method in {"resources/list", "prompts/list"}:
        return ok({method.split("/")[0]: []})
    return err(-32601, f"Method not found: {method}")


@router.post("")
async def mcp_post(request: Request):
    try:
        payload = json.loads(await request.body() or b"null")
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                            status_code=400)
    if isinstance(payload, list):
        if not payload:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Empty batch"}},
                                status_code=400)
        out = [r for r in [await _handle(m, request) for m in payload] if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    res = await _handle(payload, request)
    if res is None:
        return Response(status_code=202)
    return JSONResponse(res)


@router.get("")
def mcp_get():
    return JSONResponse({"detail": "本 MCP 端点只接受 POST (无状态, 不推送 SSE)"}, status_code=405,
                        headers={"Allow": "POST"})


@router.delete("")
def mcp_delete():
    return Response(status_code=405, headers={"Allow": "POST"})
