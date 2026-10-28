"""Market data provider abstraction.

Providers normalize external data sources into the internal parquet schema.
"""
from app.data_providers.base import AssetType, MarketDataProvider, ProviderCapabilities

__all__ = ["AssetType", "MarketDataProvider", "ProviderCapabilities"]
