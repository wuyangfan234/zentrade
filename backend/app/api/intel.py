"""盘面情报 API — /api/intel (只读展示; 全站鉴权中间件已覆盖)。

  GET  /api/intel/today      整页聚合 (只读缓存, 请求里不出网)
  POST /api/intel/settings   宏观事件重要度档位 (high / high_medium / all)
  POST /api/intel/refresh    立即刷新快源 (行情/波动率/利率/日历/PCR/宽度; 财报类按 6 小时节奏, 不在这里强制; 上一轮未完返回 409)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.services import intel_panel

router = APIRouter(prefix="/api/intel", tags=["intel"])


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


@router.get("/today")
def intel_today(request: Request) -> dict:
    return intel_panel.build(_data_dir(request), repo=getattr(request.app.state, "repo", None),
                             loop=getattr(request.app.state, "trading_loop", None))


@router.get("/asof/days")
def intel_asof_days(request: Request) -> dict:
    from app.market_clock import us_clock
    today = us_clock.market_datetime(us_clock.now()).date()
    return {"today": today.isoformat(), "days": intel_panel.asof_days(_data_dir(request), today)}


@router.get("/asof")
def intel_asof(request: Request, day: str = Query(..., max_length=10)) -> dict:
    """历史日回看 (只读): 当天冻结的整页 + 模型状态; 没存档的块标未存档。"""
    from datetime import date as _date

    from app.market_clock import us_clock
    try:
        d = _date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=422, detail="day 须为 YYYY-MM-DD") from None
    today = us_clock.market_datetime(us_clock.now()).date()
    if d >= today:
        raise HTTPException(status_code=422, detail="只能回看今天之前的日子")
    repo = getattr(request.app.state, "repo", None)
    getter = getattr(repo, "latest_settled_daily_date", None)
    return intel_panel.asof_view(_data_dir(request), d, today=today, settled=getter() if getter else None)


class IntelSettingsIn(BaseModel):
    macro_impact: str


@router.post("/settings")
def intel_settings(request: Request, body: IntelSettingsIn) -> dict:
    try:
        return intel_panel.save_settings(_data_dir(request), macro_impact=body.macro_impact)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"macro_impact 只能是 {', '.join(intel_panel.IMPACT_CHOICES)}") from None


@router.post("/refresh")
def intel_refresh(request: Request) -> dict:
    res = intel_panel.tick(_data_dir(request), getattr(request.app.state, "repo", None),
                           getattr(request.app.state, "trading_loop", None), force=True,
                           only=set(intel_panel.MANUAL_SOURCES))
    if res is None:
        raise HTTPException(status_code=409, detail="上一轮情报刷新仍在进行，请稍后再试")  # noqa: RUF001
    return {"ran": res}
