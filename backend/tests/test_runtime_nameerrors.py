"""运行期 NameError 回归 (9-29 审计: 线上 3 个按钮一点就崩)。

P1 裁剪删模块时连带删了仍被调用的常量/函数, pytest 全绿但线上 NameError:
- AI 个股分析 → name '_SYSTEM_PROMPT' is not defined
- 自选 粘贴/CSV 导入 → name '_run_candidate_import' is not defined
- 数据页 测试端点 → name '_endpoints_cache' is not defined (TickFlow 遗留, 整体退役)

另加一道静态门禁: 生产代码 (app/) 不得出现 pyflakes「未定义名」(F821 同义),
防同类裁剪事故复发。死分支里的历史残留一并清理, 门禁为零容忍。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]


# ── 1. AI 个股分析: 系统提示词必须存在且是美股口径 ─────────────────

def test_stock_analyzer_system_prompt_defined_and_us_market():
    from app.services import stock_analyzer

    prompt = getattr(stock_analyzer, "_SYSTEM_PROMPT", None)
    assert isinstance(prompt, str) and len(prompt) > 200
    assert "美股" in prompt
    # 不得沿用上游 A 股人设; 涨停类词只允许出现在「美股没有涨跌停」的禁用说明里
    for banned in ("A 股", "A股", "沪深", "北交所", "人民币"):
        assert banned not in prompt, banned
    assert "没有日内涨跌停" in prompt


def test_analyze_stream_reaches_llm_without_nameerror(monkeypatch):
    """流式分析在调用 LLM 前不得因未定义名崩溃 (LLM 以桩替代, 不耗 token)。"""
    import polars as pl

    from app.services import ai_provider, stock_analyzer

    df = pl.DataFrame({
        "date": [f"2026-09-{d:02d}" for d in range(1, 29)],
        "open": [100.0 + i for i in range(28)],
        "high": [101.0 + i for i in range(28)],
        "low": [99.0 + i for i in range(28)],
        "close": [100.5 + i for i in range(28)],
        "volume": [1_000_000.0] * 28,
    })
    monkeypatch.setattr(stock_analyzer, "_load_kline", lambda repo, symbol: df)
    seen: dict = {}

    async def fake_stream(messages, **kw):
        seen["system"] = messages[0]["content"]
        yield "ok"

    monkeypatch.setattr(ai_provider, "stream_ai_text", fake_stream)

    class _Repo:
        def resolve_asset_type(self, symbol):
            return "stock"

    async def run():
        out = []
        async for line in stock_analyzer.analyze_stock_stream(_Repo(), Path("/nonexistent"), "NVDA"):
            out.append(json.loads(line))
        return out

    events = asyncio.run(run())
    kinds = [e["type"] for e in events]
    assert "error" not in kinds, events
    assert kinds[-1] == "done"
    assert "美股" in seen["system"]


# ── 2. 自选导入: 粘贴代码 / CSV 不得 500 ─────────────────────────

@pytest.fixture
def watch_client(tmp_path, monkeypatch):
    import polars as pl

    from app.api import watchlist as wl_api

    inst = tmp_path / "instruments"
    inst.mkdir()
    pl.DataFrame({"symbol": ["AAPL", "NVDA"], "name": ["Apple Inc.", "NVIDIA Corp"]}).write_parquet(
        inst / "instruments.parquet"
    )

    from fastapi import FastAPI
    from types import SimpleNamespace

    app = FastAPI()
    app.include_router(wl_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(wl_api.watchlist, "list_symbols", lambda: [])
    return TestClient(app)


def test_import_codes_returns_candidates(watch_client):
    r = watch_client.post("/api/watchlist/import-codes", json={"text": "AAPL NVDA"})
    assert r.status_code == 200, r.text
    syms = {c["symbol"] for c in r.json()["candidates"]}
    assert {"AAPL", "NVDA"} <= syms


def test_import_codes_empty_is_400_not_500(watch_client):
    r = watch_client.post("/api/watchlist/import-codes", json={"text": "@@@"})
    assert r.status_code == 400, r.text


def test_import_csv_returns_candidates(watch_client):
    r = watch_client.post(
        "/api/watchlist/import-csv",
        files={"file": ("w.csv", b"symbol,name\nAAPL,Apple\n", "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert any(c["symbol"] == "AAPL" for c in r.json()["candidates"])


# ── 3. 数据页 测试端点: TickFlow 多端点测速是厂商遗留, 整体退役 ────────
#
# 该按钮的三个后端 (/endpoints 清单代理、/test_endpoint 测速、/switch_endpoint 切换)
# 服务的是 TickFlow 多区域接入点; 厂商已于 P1-1b2 退役, 清单与切换端点早已不存在,
# 只剩 test_endpoint 因引用已删的缓存/探测函数而 NameError。Alpaca 只有单一端点,
# 该功能无业务意义 → 前后端一起删除, 而不是补回半截实现。

def test_tickflow_endpoint_probe_retired():
    from app.api import settings as settings_api

    paths = {getattr(r, "path", "") for r in settings_api.router.routes}
    assert "/api/settings/test_endpoint" not in paths
    assert not hasattr(settings_api, "TestEndpointIn")

    fe = BACKEND.parent / "frontend" / "src"
    assert not (fe / "components" / "EndpointTestDialog.tsx").exists()
    api_ts = (fe / "lib" / "api.ts").read_text(encoding="utf-8")
    for name in ("testEndpoint:", "listEndpoints:", "switchEndpoint:", "/api/settings/test_endpoint"):
        assert name not in api_ts, name
    data_tsx = (fe / "pages" / "Data.tsx").read_text(encoding="utf-8")
    assert "EndpointTestDialog" not in data_tsx
    assert "测试端点" not in data_tsx


# ── 4. 静态门禁: 生产代码零「未定义名」 ─────────────────────────────

def test_no_undefined_names_in_production_code():
    """ruff F821 (undefined name)。裁剪删模块后必须零命中 (dev 依赖已含 ruff)。"""
    import shutil

    ruff = shutil.which("ruff") or str(Path(sys.executable).with_name("ruff"))
    if not Path(ruff).exists():
        pytest.skip("ruff 未安装 (uv sync --extra dev)")
    proc = subprocess.run(
        [ruff, "check", str(BACKEND / "app"), "--select", "F821",
         "--output-format", "concise", "--no-cache", "--isolated"],
        capture_output=True, text=True,
    )
    hits = [ln for ln in proc.stdout.splitlines() if "F821" in ln]
    assert hits == [], "\n".join(hits)
