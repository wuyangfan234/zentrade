"""新闻 API — /api/news (只读; 全站鉴权中间件已覆盖)。

  GET /api/news/stock?symbol=NVDA&days=7&main_only=1   个股新闻 (中文优先视图)
  GET /api/news/summary?symbols=NVDA,LITE              批量摘要 (列表角标/机会卡「近期催化」)
  GET /api/news/status                                 采集/翻译状态 (系统状态页)
  POST /api/news/refresh                               立即跑一轮采集+翻译 (手动)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import news_store

router = APIRouter(prefix="/api/news", tags=["news"])

DISCLAIMER = "事件类型、方向与重要度为 AI 判读，未经验证；仅供参考，不参与开仓决策"


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


@router.get("/stock")
def stock_news(request: Request, symbol: str = Query(..., min_length=1, max_length=10),
               days: int = Query(7, ge=1, le=30), main_only: bool = Query(False),
               limit: int = Query(50, ge=1, le=200)) -> dict:
    sym = symbol.strip().upper()
    items = news_store.for_symbol(_data_dir(request), sym, days=days, limit=limit, main_only=main_only)
    return {"symbol": sym, "days": days, "items": items, "summary": news_store.summarize(items),
            "disclaimer": DISCLAIMER}


@router.get("/summary")
def news_summary(request: Request, symbols: str = Query("", max_length=2000)) -> dict:
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:100]
    return {"summaries": news_store.batch_summary(_data_dir(request), syms) if syms else {}, "disclaimer": DISCLAIMER}


@router.get("/status")
def news_status(request: Request) -> dict:
    return news_store.status(_data_dir(request))


@router.post("/refresh")
def news_refresh(request: Request) -> dict:
    from app.services import news_job
    loop = getattr(request.app.state, "trading_loop", None)
    res = news_job.tick(_data_dir(request), loop, getattr(request.app.state, "repo", None), force=True)
    if res is None:
        raise HTTPException(status_code=409, detail="上一轮新闻采集仍在进行，请稍后再试")
    return res
