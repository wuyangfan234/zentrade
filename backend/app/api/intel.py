"""盘面情报 API — /api/intel (只读展示; 全站鉴权中间件已覆盖)。

  GET  /api/intel/today      整页聚合 (只读缓存, 请求里不出网)
  POST /api/intel/settings   宏观事件重要度档位 (high / high_medium / all)
  POST /api/intel/refresh    立即刷新快源 (行情/波动率/利率/日历/PCR/宽度; 财报类按 6 小时节奏, 不在这里强制; 上一轮未完返回 409)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services import intel_panel

router = APIRouter(prefix="/api/intel", tags=["intel"])


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


@router.get("/today")
def intel_today(request: Request) -> dict:
    return intel_panel.build(_data_dir(request), repo=getattr(request.app.state, "repo", None),
                             loop=getattr(request.app.state, "trading_loop", None))


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
