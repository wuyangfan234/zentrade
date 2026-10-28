"""bughunt-1015 B1: 前端 api.ts 里每个固定路径的请求, 后端都必须有同方法的真实路由 (含 include_router 前缀)。

生产 3bb4e0a 实证: 自选「批量导入」弹窗收截图后调 POST /api/watchlist/import-image, 但 OCR 后端在 P1-5 (f94c259) 已删,
这个路径只撞上 DELETE /api/watchlist/{symbol} → 405, 截图导入必失败。同类死成员还有 GET ocr-status / quotes。
只检查字面量路径 (模板串里的固定前缀不在这里查, 避免误报); 方法取紧随其后的 { method: 'X' }, 缺省 GET。
"""
from __future__ import annotations

import re
from pathlib import Path

API_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "api.ts"

_CALL = re.compile(
    r"""(?:request|fetch|requestJson)(?:<[^>(]*>)?\(\s*['"](?P<path>/api/[A-Za-z0-9_\-/.]*)(?:\?[^'"]*)?['"]"""
    r"""(?P<rest>[^)]{0,240})""",
    re.S,
)
_METHOD = re.compile(r"""method:\s*['"](?P<m>[A-Z]+)['"]""")


def _frontend_calls() -> list[tuple[str, str, int]]:
    src = API_TS.read_text(encoding="utf-8")
    out = []
    for m in _CALL.finditer(src):
        meth = _METHOD.search(m.group("rest") or "")
        out.append((meth.group("m") if meth else "GET", m.group("path"), src[: m.start()].count("\n") + 1))
    return out


def _backend_routes() -> list[tuple[re.Pattern, set[str]]]:
    from app.main import app
    routes = []
    for r in app.routes:
        path, methods = getattr(r, "path", None), getattr(r, "methods", None)
        if path and methods:
            routes.append((re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$"), set(methods)))
    return routes


def test_every_literal_frontend_api_call_has_a_backend_route_with_same_method():
    calls = _frontend_calls()
    assert len(calls) > 100, "解析 api.ts 失败 (调用数过少)"
    routes = _backend_routes()
    dead = [f"api.ts:{line} {meth} {path}" for meth, path, line in calls
            if not any(pat.match(path) and meth in methods for pat, methods in routes)]
    assert dead == [], "前端调用了后端不存在的路由 (点了必失败):\n" + "\n".join(dead)
