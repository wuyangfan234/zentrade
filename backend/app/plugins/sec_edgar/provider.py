"""SEC EDGAR 财务源插件: 只声明 financial 数据集; 数据由 fundamentals_sync 定时落盘到 data/financials/。"""
from __future__ import annotations

from types import SimpleNamespace

from app.data_providers.base import ProviderCapabilities


def availability() -> tuple[bool, str]:
    return True, "ok"


class SecEdgarProvider:
    name = "sec_edgar"
    capabilities = ProviderCapabilities(financial=True)

    def __init__(self) -> None:
        self.config = SimpleNamespace(display_name="SEC EDGAR (美股财务)", path=None, datasets={})

    def close(self) -> None:
        return None
