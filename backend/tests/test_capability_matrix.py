"""能力路由矩阵契约测试 (厂商内置源退役后语义, P1-1b2)。

覆盖: 注册表与路由偏好字段一一对应、候选按各源 datasets 声明过滤、
候选只含当前可用源 (未就绪插件进 pending 并携带原因)、usable 跟随生效源、
每能力独立路由、偏好指向未知源时的展示回退、API 层注入完整性。
全部用假插件/自定义源, 不依赖真实网络与本地 data/ 目录。
"""
from __future__ import annotations

from app.data_providers import custom as custom_sources
from app.data_providers.capabilities import CAPABILITY_REGISTRY, build_capability_matrix

DEFAULT_CURRENT = {
    "daily_data_provider": "",
    "adj_factor_provider": "",
    "minute_data_provider": "",
    "full_minute_data_provider": "",
    "realtime_data_provider": "",
    "financial_data_provider": "",
}


def _fake_sources(monkeypatch, plugins: list[dict], customs: list[dict] | None = None) -> None:
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: plugins)
    monkeypatch.setattr(custom_sources, "list_sources", lambda: customs or [])


def _by_id(matrix: dict) -> dict[str, dict]:
    return {c["id"]: c for c in matrix["capabilities"]}


def test_registry_covers_all_routing_fields():
    """注册表是能力的单一权威: 可路由能力与偏好键一一对应、无重复。"""
    routable = [c["field"] for c in CAPABILITY_REGISTRY if c["field"] is not None]
    assert sorted(routable) == sorted(DEFAULT_CURRENT)
    assert len(set(routable)) == len(routable)
    assert {c["id"] for c in CAPABILITY_REGISTRY} == {
        "realtime", "daily", "minute", "full_minute", "adj_factor", "financial",
    }
    full_minute = next(c for c in CAPABILITY_REGISTRY if c["id"] == "full_minute")
    assert full_minute["field"] == "full_minute_data_provider"
    for cap in CAPABILITY_REGISTRY:
        assert cap["default"] == ("sec_edgar" if cap["id"] == "financial" else "")  # 2026-10-12 财务默认 EDGAR
        assert "follow" not in cap


def test_matrix_without_any_source(monkeypatch):
    """无插件无自定义源: 每个能力零候选, 未配置 → usable=False。"""
    _fake_sources(monkeypatch, [])
    matrix = build_capability_matrix(dict(DEFAULT_CURRENT))
    assert len(matrix["capabilities"]) == 6
    for cap in matrix["capabilities"]:
        assert cap["candidates"] == []
        assert cap["pending"] == []
        assert cap["usable"] is False
        assert cap["current"] == cap["effective"] == ""


def test_candidates_only_available_unready_goes_pending(monkeypatch):
    """候选只包含声明了该能力且可用的源; 未就绪插件进 pending 并携带原因。"""
    _fake_sources(
        monkeypatch,
        [
            {"name": "fuyao", "display_name": "fuyao", "datasets": ["realtime"],
             "available": True, "status": "ok"},
            {"name": "sdk", "display_name": "SDK", "datasets": ["daily", "minute"],
             "available": False, "status": "依赖未安装"},
        ],
        [{"name": "myhttp", "display_name": "MyHTTP", "datasets": ["financial", "realtime"]}],
    )
    caps = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT)))
    assert [c["name"] for c in caps["realtime"]["candidates"]] == ["fuyao", "myhttp"]
    assert caps["realtime"]["candidates"][1]["kind"] == "custom"
    assert [c["name"] for c in caps["daily"]["candidates"]] == []
    assert [c["name"] for c in caps["daily"]["pending"]] == ["sdk"]
    assert caps["daily"]["pending"][0]["note"] == "依赖未安装"
    assert [c["name"] for c in caps["minute"]["pending"]] == ["sdk"]
    assert [c["name"] for c in caps["financial"]["candidates"]] == ["myhttp"]
    assert "depth5" not in caps   # 五档盘口能力已删除 (美股无可用源)


def test_usable_follows_effective_provider(monkeypatch):
    """usable 跟随生效源: 生效源是可用插件 → usable; 未就绪插件或空 → 不可用。"""
    _fake_sources(
        monkeypatch,
        [
            {"name": "fuyao", "display_name": "fuyao", "datasets": ["realtime", "minute"],
             "available": True, "status": "ok"},
            {"name": "sdk", "display_name": "SDK", "datasets": ["daily", "adj_factor", "minute"],
             "available": False, "status": "依赖未安装"},
        ],
    )
    caps = _by_id(
        build_capability_matrix(
            dict(DEFAULT_CURRENT, realtime_data_provider="fuyao", minute_data_provider="sdk"),
        ),
    )
    # 路由到可用插件
    assert caps["realtime"]["usable"] is True
    # 路由到未就绪插件: 有可用候选 (fuyao) 也不算 usable
    minute = caps["minute"]
    assert [c["name"] for c in minute["candidates"]] == ["fuyao"]
    assert minute["usable"] is False
    # 未配置: 不可用
    assert caps["adj_factor"]["usable"] is False
    # 除权显式路由到未就绪 sdk → 不可用
    caps = _by_id(
        build_capability_matrix(dict(DEFAULT_CURRENT, adj_factor_provider="sdk")),
    )
    assert caps["adj_factor"]["effective"] == "sdk"
    assert caps["adj_factor"]["usable"] is False


def test_adj_factor_routes_independently(monkeypatch):
    """除权独立路由 (跟随日K已下线): 显式切到声明除权的插件即生效, 与日K当前源无关。"""
    _fake_sources(
        monkeypatch,
        [{"name": "sdk", "display_name": "SDK", "datasets": ["adj_factor"],
          "available": True, "status": "ok"}],
    )
    caps = _by_id(
        build_capability_matrix(dict(DEFAULT_CURRENT, adj_factor_provider="sdk")),
    )
    adj = caps["adj_factor"]
    assert adj["current"] == adj["effective"] == "sdk"
    assert adj["usable"] is True
    assert caps["daily"]["effective"] == ""
    assert caps["daily"]["usable"] is False


def test_unknown_current_display_falls_back_to_name(monkeypatch):
    """偏好指向未注册源 (正常经 getters 校验不会发生): 展示回退为原始名, 不抛异常。"""
    _fake_sources(monkeypatch, [])
    caps = _by_id(
        build_capability_matrix(dict(DEFAULT_CURRENT, realtime_data_provider="ghost")),
    )
    assert caps["realtime"]["current"] == "ghost"
    assert caps["realtime"]["current_display"] == "ghost"
    assert caps["realtime"]["effective_display"] == "ghost"
    assert caps["realtime"]["usable"] is False


def test_full_minute_routable_like_other_capabilities(monkeypatch):
    """全量分钟行: 与其他能力同样可路由 — 声明 full_minute 数据集的源进候选,
    路由到它则 usable=True; 未配置则不可用。"""
    _fake_sources(
        monkeypatch,
        [],
        [{"name": "myfm", "display_name": "MyFM", "datasets": ["full_minute"]}],
    )
    caps = _by_id(build_capability_matrix(dict(DEFAULT_CURRENT)))
    fm = caps["full_minute"]
    assert fm["field"] == "full_minute_data_provider"
    assert [c["name"] for c in fm["candidates"]] == ["myfm"]
    assert fm["usable"] is False

    routed = dict(DEFAULT_CURRENT, full_minute_data_provider="myfm")
    fm_routed = _by_id(build_capability_matrix(routed))["full_minute"]
    assert fm_routed["usable"] is True
    assert fm_routed["effective"] == "myfm"


def test_api_endpoint_injects_all_routing_preferences(monkeypatch):
    """回归 (#301): get_capability_matrix 组装 current 必须覆盖注册表全部路由字段。"""
    from app.api import settings as settings_api
    from app.data_providers import capabilities as capabilities_mod
    from app.services import preferences

    getter_values = {
        "realtime_data_provider": "rt-src",
        "daily_data_provider": "daily-src",
        "minute_data_provider": "min-src",
        "full_minute_data_provider": "fm-src",
        "adj_factor_provider": "adj-src",
        "financial_data_provider": "fin-src",
    }
    getter_names = {
        "realtime_data_provider": "get_realtime_data_provider",
        "daily_data_provider": "get_daily_data_provider",
        "minute_data_provider": "get_minute_data_provider",
        "full_minute_data_provider": "get_full_minute_data_provider",
        "adj_factor_provider": "get_adj_factor_provider",
        "financial_data_provider": "get_financial_provider",
    }
    for field, getter in getter_names.items():
        monkeypatch.setattr(preferences, getter, lambda f=field: getter_values[f])

    captured: dict[str, dict] = {}

    def _spy(current):
        captured["current"] = dict(current)
        return build_capability_matrix(current)

    monkeypatch.setattr(capabilities_mod, "build_capability_matrix", _spy)
    settings_api.get_capability_matrix()

    routable_fields = {c["field"] for c in CAPABILITY_REGISTRY if c["field"] is not None}
    assert routable_fields == set(getter_values)
    assert captured["current"] == getter_values


def test_api_endpoint_full_minute_usable_follows_preference(monkeypatch):
    """端到端回归 (#301 用户场景): 已路由自定义源 → 全量分钟卡片可用。"""
    from app.api import settings as settings_api
    from app.services import preferences

    _fake_sources(
        monkeypatch,
        [],
        [{"name": "eltdx", "display_name": "ELTDX", "datasets": ["full_minute"]}],
    )
    for name in (
        "get_realtime_data_provider", "get_daily_data_provider",
        "get_minute_data_provider",
        "get_adj_factor_provider", "get_financial_provider",
    ):
        monkeypatch.setattr(preferences, name, lambda: "")
    monkeypatch.setattr(preferences, "get_full_minute_data_provider", lambda: "eltdx")

    fm = _by_id(settings_api.get_capability_matrix())["full_minute"]
    assert fm["effective"] == "eltdx"
    assert fm["usable"] is True
