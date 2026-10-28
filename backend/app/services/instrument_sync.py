"""标的维表同步服务。

读取当前日K provider 的标的，保留证券类型与行业字段；美股经 Nasdaq
公开 reference 补充真实 sector/industry，带来源日期，失败不伪造新鲜度。
"""
from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet
from app.market_clock import us_clock

logger = logging.getLogger(__name__)

_METADATA_FIELDS = (
    "sector", "industry", "sector_source", "industry_source", "sector_as_of", "industry_as_of",
    "source", "asset_type", "instrument_type", "classification_source", "risk_flags",
    "is_etf", "is_preferred", "is_warrant", "is_unit", "reference_status", "reference_as_of",
)


def _flatten_instruments(items: list[dict]) -> list[dict]:
    """把 SDK 返回的 Instrument 列表 flatten 成扁平行。"""
    rows = []
    for item in items:
        row = {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "code": item.get("code"),
            "exchange": item.get("exchange"),
            "region": item.get("region"),
            "type": item.get("type"),
        }
        ext = item.get("ext") or {}
        for key in _METADATA_FIELDS:
            row[key] = item.get(key) if item.get(key) is not None else ext.get(key)
        row["listing_date"] = ext.get("listing_date")
        row["total_shares"] = ext.get("total_shares")
        row["float_shares"] = ext.get("float_shares")
        row["tick_size"] = ext.get("tick_size")
        rows.append(row)
    return rows


def _fetch_instruments_via_provider() -> list[dict] | None:
    """若当前日K数据源已配置且该 provider 提供 get_instruments, 用它拉标的维表。

    返回 flatten 行列表; 未命中(无可用源)时返回 None (厂商直连已退役 P1-1b2)。
    标的维表跟随日K数据源(二者天然耦合, 无独立偏好项)。
    """
    from app.services import preferences

    provider_name = preferences.get_daily_data_provider()
    if not provider_name:
        return None
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider(provider_name):
        return None
    provider = custom_sources.get_provider(provider_name)
    if not hasattr(provider, "get_instruments"):
        return None
    try:
        items = provider.get_instruments("stock") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("provider %s get_instruments 失败: %s", provider_name, e)
        return None
    rows = _flatten_instruments(items)
    logger.info("instruments via %s: %d stocks", provider_name, len(rows))
    return rows


def sync_instruments(data_dir: Path) -> int:
    """全量同步标的维表 → data/instruments/instruments.parquet。

    返回写入的行数。厂商直连已退役: 无可用源时返回 0 (保持现有表)。
    """
    all_rows = _fetch_instruments_via_provider()
    if all_rows is None:
        logger.info("instruments sync skipped: 无可用日K数据源 (厂商直连已退役)")
        return 0

    if not all_rows:
        return 0

    # Reference belongs in sync, not the fast snapshot path (no screener GET per poll).
    if any(row.get("source") == "alpaca" or row.get("region") == "US" for row in all_rows):
        from app.services.us_reference import enrich_instruments, load_us_reference
        reference = load_us_reference(data_dir)
        all_rows = enrich_instruments(all_rows, reference)
        # Older installations can have known sectors in parquet without a reference cache.
        # Preserve those labels with their ORIGINAL dates instead of blanking/re-dating them.
        old_path = data_dir / "instruments" / "instruments.parquet"
        if old_path.exists():
            old = {r["symbol"]: r for r in pl.read_parquet(old_path).to_dicts()}
            for row in all_rows:
                previous = old.get(row["symbol"], {})
                for field in ("sector", "industry"):
                    if not row.get(field) and previous.get(field):
                        row[field] = previous[field]
                        row[f"{field}_source"] = previous.get(f"{field}_source")
                        stamp = previous.get(f"{field}_as_of") or previous.get("as_of")
                        row[f"{field}_as_of"] = str(stamp) if stamp is not None else None

    df = pl.DataFrame(all_rows)
    df = df.with_columns(pl.lit(us_clock.today()).alias("as_of"))

    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(df, out)

    logger.info("instruments synced: %d rows → %s", df.height, out)
    return df.height
