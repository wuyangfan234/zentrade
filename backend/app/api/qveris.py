"""QVeris 付费数据 API — /api/qveris (全站鉴权中间件已覆盖; 密钥只在后端)。

  GET  /api/qveris/usage                         余额 + 今日用量 + 上限 + 最近调用 (余额查询免费)
  POST /api/qveris/limit {daily_limit}           调整每日上限
  GET  /api/qveris/options?symbol=&refresh=0|1    期权摘要 (refresh=1 才可能花 1 积分; 缓存命中免费)
  GET  /api/qveris/estimates?symbol=&refresh=0|1  财报一致预期
  GET  /api/qveris/transcript?symbol=&quarter=&refresh=0|1  财报电话会 (+ 要点)
  POST /api/qveris/transcript/digest {symbol, quarter}      Kimi 提炼要点 (不收 QVeris 积分)
  GET  /api/qveris/auto                          每日自动财报预期的最近一次结果
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services import qveris_data, qveris_gate

router = APIRouter(prefix="/api/qveris", tags=["qveris"])
NOTE = "QVeris 付费数据（约 1 积分/次，缓存命中免费）；只用于展示与辅助判断，不参与开仓"


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


def _sym(symbol: str) -> str:
    s = symbol.strip().upper()
    if not s or len(s) > 10 or not all(c.isalnum() or c in ".-" for c in s):
        raise HTTPException(status_code=422, detail="股票代码不合法")
    return s


def _guard(fn):
    """把成本闸/数据源错误映射成用户可读的 HTTP 错误 (不泄露密钥)。"""
    from app.plugins.qveris.provider import QverisError
    try:
        return fn()
    except qveris_gate.QuotaExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from None
    except qveris_gate.GateError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except QverisError as e:
        raise HTTPException(status_code=502, detail=f"QVeris 调用失败: {e}") from None


def _require_key():
    from app.plugins.qveris import provider as qp
    if not qp._api_key():
        raise HTTPException(status_code=400, detail="未配置 QVeris Key，请到 设置 → 数据源 填写")


@router.get("/usage")
def usage(request: Request, refresh_balance: bool = Query(False)) -> dict:
    return {**qveris_gate.usage(_data_dir(request), force_balance=refresh_balance), "note": NOTE}


class LimitIn(BaseModel):
    daily_limit: float = Field(..., ge=0, le=10000)


@router.post("/limit")
def set_limit(body: LimitIn, request: Request) -> dict:
    qveris_gate.set_daily_limit(body.daily_limit)
    return qveris_gate.usage(_data_dir(request))


def _earnings_date(request: Request, symbol: str) -> date | None:
    from app.services.earnings_calendar import EarningsCalendar
    ev = EarningsCalendar(_data_dir(request)).event_for(symbol)
    try:
        return date.fromisoformat(str(ev["date"])[:10]) if ev else None
    except (KeyError, ValueError):
        return None


@router.get("/options")
def options(request: Request, symbol: str = Query(..., max_length=10), refresh: bool = Query(False)) -> dict:
    sym = _sym(symbol)
    if refresh:
        _require_key()
    ed = _earnings_date(request, sym)
    out = _guard(lambda: qveris_data.options_summary(_data_dir(request), request.app.state.repo, sym,
                                                     refresh=refresh, earnings_date=ed))
    return {**out, "earnings_date": ed.isoformat() if ed else None, "note": NOTE}


@router.get("/estimates")
def estimates(request: Request, symbol: str = Query(..., max_length=10), refresh: bool = Query(False)) -> dict:
    sym = _sym(symbol)
    if refresh:
        _require_key()
    ed = _earnings_date(request, sym)
    out = _guard(lambda: qveris_data.estimates(_data_dir(request), sym, refresh=refresh))
    return {**out, "earnings_date": ed.isoformat() if ed else None, "note": NOTE}


def _quarter(q: str | None) -> str:
    from app.market_clock import us_clock
    q = (q or qveris_data.latest_quarter(us_clock.today())).strip().upper()
    if len(q) != 6 or not q[:4].isdigit() or q[4] != "Q" or q[5] not in "1234":
        raise HTTPException(status_code=422, detail="季度格式应为 2026Q3")
    return q


@router.get("/transcript")
def transcript(request: Request, symbol: str = Query(..., max_length=10), quarter: str | None = Query(None),
               refresh: bool = Query(False)) -> dict:
    sym, q = _sym(symbol), _quarter(quarter)
    if refresh:
        _require_key()
    out = _guard(lambda: qveris_data.transcript(_data_dir(request), sym, q, refresh=refresh))
    return {**out, "note": NOTE}


class DigestIn(BaseModel):
    symbol: str = Field(..., max_length=10)
    quarter: str = Field(..., max_length=6)


@router.post("/transcript/digest")
async def transcript_digest(body: DigestIn, request: Request) -> dict:
    sym, q = _sym(body.symbol), _quarter(body.quarter)
    try:
        d = await qveris_data.digest_transcript(_data_dir(request), sym, q)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"要点提炼失败: {type(e).__name__}: {str(e)[:160]}") from None
    if d is None:
        raise HTTPException(status_code=404, detail="请先查询该季度电话会原文")
    return {"symbol": sym, "quarter": q, "digest": d}


@router.get("/auto")
def auto(request: Request) -> dict:
    return qveris_data.auto_state(_data_dir(request)) or {"at": None, "targets": 0}
