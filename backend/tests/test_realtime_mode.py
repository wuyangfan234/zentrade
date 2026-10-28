"""回归测试: 实时行情模式判定 (厂商档位已退役 P1-1b2)。

新语义: 配置了声明 realtime 数据集的插件/自定义源 → full_market; 否则 none。
"""
from app.services.quote_service import QuoteService


def _route(monkeypatch, provider: str, has_dataset: bool) -> None:
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: provider)
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, ds: has_dataset and ds == "realtime",
    )


def test_custom_realtime_source_is_full_market(monkeypatch):
    """声明 realtime 的自定义源 → 全市场模式。"""
    _route(monkeypatch, "fuyao", True)
    assert QuoteService.realtime_mode() == "full_market"
    assert QuoteService.is_realtime_allowed() is True


def test_unconfigured_has_no_realtime(monkeypatch):
    """未配置实时源 → none。"""
    _route(monkeypatch, "", False)
    assert QuoteService.realtime_mode() == "none"
    assert QuoteService.is_realtime_allowed() is False


def test_source_without_realtime_dataset_has_no_realtime(monkeypatch):
    """配置了源但未声明 realtime 数据集 → none (fail-closed)。"""
    _route(monkeypatch, "daily_only_src", False)
    assert QuoteService.realtime_mode() == "none"
