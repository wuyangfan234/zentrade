"""API 路由 — /health 与 /api/capabilities。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app import __version__

router = APIRouter()


def _current_routing_preferences() -> dict[str, str]:
    from app.services import preferences
    return {
        "realtime_data_provider": preferences.get_realtime_data_provider(),
        "daily_data_provider": preferences.get_daily_data_provider(),
        "minute_data_provider": preferences.get_minute_data_provider(),
        "full_minute_data_provider": preferences.get_full_minute_data_provider(),
        "adj_factor_provider": preferences.get_adj_factor_provider(),
        "financial_data_provider": preferences.get_financial_provider(),
    }


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
    }


@router.get("/api/capabilities")
def capabilities() -> dict:
    """前端用来决定哪些功能可用、哪些灰显 (厂商探测已退役: 从自定义源推导)。"""
    from app.data_providers.capabilities import current_capability_set
    capset = current_capability_set(_current_routing_preferences())
    return {
        "capabilities": capset.to_dict(),
    }


@router.post("/api/capabilities/redetect")
def redetect(request: Request) -> dict:
    """设置页「重新检测」按钮: 按当前偏好重建能力集并热更新快照。"""
    from app.data_providers.capabilities import current_capability_set
    capset = current_capability_set(_current_routing_preferences())
    request.app.state.capabilities = capset
    from app.api.settings import _sync_financial_scheduler_caps
    _sync_financial_scheduler_caps(request.app.state, capset)
    return {
        "capabilities": capset.to_dict(),
    }
