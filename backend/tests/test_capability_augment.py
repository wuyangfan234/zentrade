"""运行时能力集构建 (current_capability_set) 回归测试。

厂商内置源退役后 (P1-1b2) 的新语义:
某数据集的当前 provider 已配置且声明了该数据集 → 授予对应能力,
限速取该数据集声明值 (rpm/batch); 未配置/未声明 → fail-closed 不授予。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.data_providers.capabilities import current_capability_set
from app.data_providers.models import Cap, CapabilitySet

_FIELDS = {
    "daily": "daily_data_provider",
    "adj": "adj_factor_provider",
    "minute": "minute_data_provider",
    "financial": "financial_data_provider",
    "full_minute": "full_minute_data_provider",
}


def _current(**providers: str) -> dict[str, str]:
    return {field: providers.get(key, "") for key, field in _FIELDS.items()}


def _mock_sources(monkeypatch, declared: dict[str, set[str]], rpm: int = 30, batch: int = 100) -> None:
    """mock custom_sources: declared = {provider_name: {datasets}}。
    get_provider 返回带 config.datasets[ds].rpm/batch 的伪 provider。"""
    def has_dataset(name, ds):
        return ds in declared.get(name, set())

    def get_provider(name):
        datasets = declared.get(name)
        if datasets is None:
            raise ValueError(f"unknown provider: {name}")
        return SimpleNamespace(
            config=SimpleNamespace(
                datasets={ds: SimpleNamespace(rpm=rpm, batch=batch) for ds in datasets}
            )
        )

    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset", has_dataset,
    )
    monkeypatch.setattr(
        "app.data_providers.custom.get_provider", get_provider,
    )


def test_daily_custom_source_grants_daily_batch(monkeypatch):
    _mock_sources(monkeypatch, {"mock_src": {"daily"}})
    capset = current_capability_set(_current(daily="mock_src"))
    assert capset.has(Cap.KLINE_DAILY_BATCH)
    assert not capset.has(Cap.ADJ_FACTOR)
    assert not capset.has(Cap.KLINE_MINUTE_BATCH)
    assert not capset.has(Cap.FINANCIAL)


def test_adj_custom_source_grants_adj_factor(monkeypatch):
    _mock_sources(monkeypatch, {"mock_src": {"adj_factor"}})
    capset = current_capability_set(_current(adj="mock_src"))
    assert capset.has(Cap.ADJ_FACTOR)


def test_full_minute_custom_source_grants_intraday_universe(monkeypatch):
    """声明 full_minute 且被路由 → 授予 INTRADAY_UNIVERSE (minute_refresh 门控口径)。"""
    _mock_sources(monkeypatch, {"mock_src": {"full_minute"}})
    capset = current_capability_set(_current(full_minute="mock_src"))
    assert capset.has(Cap.INTRADAY_UNIVERSE)
    assert not capset.has(Cap.KLINE_MINUTE_BATCH)


def test_full_minute_dataset_without_routing_not_granted(monkeypatch):
    """源声明了 full_minute 但路由未指向它 → 不授予。"""
    _mock_sources(monkeypatch, {"mock_src": {"full_minute"}})
    capset = current_capability_set(_current())  # 全未配置
    assert not capset.has(Cap.INTRADAY_UNIVERSE)


def test_minute_custom_source_grants_minute_batch(monkeypatch):
    _mock_sources(monkeypatch, {"mock_src": {"minute"}})
    capset = current_capability_set(_current(minute="mock_src"))
    assert capset.has(Cap.KLINE_MINUTE_BATCH)


def test_financial_custom_source_grants_financial(monkeypatch):
    _mock_sources(monkeypatch, {"mock_src": {"financial"}})
    capset = current_capability_set(_current(financial="mock_src"))
    assert capset.has(Cap.FINANCIAL)


def test_provider_active_but_dataset_not_declared_no_grant(monkeypatch):
    _mock_sources(monkeypatch, {"mock_src": set()})
    capset = current_capability_set(_current(daily="mock_src", minute="mock_src",
                                             adj="mock_src", financial="mock_src"))
    assert not capset.has(Cap.KLINE_DAILY_BATCH)
    assert not capset.has(Cap.ADJ_FACTOR)
    assert not capset.has(Cap.KLINE_MINUTE_BATCH)
    assert not capset.has(Cap.FINANCIAL)


def test_nothing_configured_no_grant(monkeypatch):
    """全部未配置 → 空能力集 (fail-closed)。"""
    _mock_sources(monkeypatch, {})
    capset = current_capability_set(_current())
    assert len(capset.all()) == 0


def test_limits_come_from_dataset_declaration(monkeypatch):
    """限速取该数据集 YAML 声明的 rpm/batch。"""
    _mock_sources(monkeypatch, {"mock_src": {"minute"}}, rpm=77, batch=42)
    capset = current_capability_set(_current(minute="mock_src"))
    lim = capset.limits(Cap.KLINE_MINUTE_BATCH)
    assert lim is not None and lim.rpm == 77 and lim.batch == 42


def test_update_data_providers_refreshes_capability_snapshot(monkeypatch):
    """切换数据源后 app.state.capabilities 快照应刷新。"""
    from app.api import settings as settings_api

    monkeypatch.setattr("app.services.preferences.save", lambda upd: None)
    for name in (
        "get_realtime_data_provider", "get_daily_data_provider",
        "get_minute_data_provider", "get_full_minute_data_provider",
        "get_adj_factor_provider",
        "get_financial_provider",
    ):
        monkeypatch.setattr(f"app.services.preferences.{name}", lambda: "")
    sentinel = CapabilitySet()
    monkeypatch.setattr(
        "app.data_providers.capabilities.current_capability_set", lambda current: sentinel,
    )

    mock_request = MagicMock()
    settings_api.update_data_providers(
        MagicMock(model_dump=lambda exclude_none: {"daily_data_provider": "mock_src"}),
        mock_request,
    )
    assert mock_request.app.state.capabilities is sentinel
