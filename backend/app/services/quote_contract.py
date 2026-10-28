"""Source/time contract for US quote-derived research volume (not order execution)."""
from __future__ import annotations

import math
from datetime import datetime

from app.market_clock import us_clock

# 全市场合并口径的成交量来源 (可与 SIP 历史均量直接比较)。tickflow: 2026-10-01 实测与 SIP 日K量比 0.999。
CONSOLIDATED_FEEDS = ("sip", "delayed_sip", "tickflow")

CONTRACT_COLUMNS = ["feed", "quote_feed", "volume_feed", "historical_volume_feed", "data_delay", "volume_ts", "volume_status"]


def timestamp(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        if not math.isfinite(value):
            return None
        return datetime.fromtimestamp(value / 1000, tz=us_clock.tz)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def normalize_record(record: dict, now: datetime) -> dict | None:
    """Explicit feeds require a same-day, non-future source timestamp.

    Legacy custom sources without feed metadata retain the pre-existing contract.
    IEX volume can never be used with SIP historical volume. Unavailable volume
    stays null; neither source timestamps nor consolidated volume are fabricated.
    """
    row = dict(record)
    feed = row.get("feed")
    if not feed:
        return row
    quote_time = timestamp(row.get("quote_ts", row.get("timestamp")))
    delay = 900 if feed == "delayed_sip" else 0
    if quote_time is None or quote_time.date() != now.date():
        return None
    if not 0 <= (now - quote_time).total_seconds() <= delay + 120:
        return None
    try:
        price = float(row["last_price"])
        if not math.isfinite(price) or price <= 0:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    row["timestamp"] = int(quote_time.timestamp() * 1000)
    row.setdefault("quote_feed", feed)
    row.setdefault("volume_feed", feed)
    row.setdefault("historical_volume_feed", "sip")
    row["data_delay"] = delay
    volume_time = timestamp(row.get("volume_ts"))
    if feed == "iex":
        status = "iex_volume_disabled"
    elif feed not in CONSOLIDATED_FEEDS or row["quote_feed"] != feed or row["volume_feed"] != feed:
        status = "feed_mismatch"
    elif row["historical_volume_feed"] != "sip":
        status = "history_feed_mismatch"
    elif volume_time is None:
        status = "missing_volume_time"
    elif volume_time.date() != now.date() or not 0 <= (now - volume_time).total_seconds() <= delay + 120:
        status = "stale_volume"
    else:
        status = "valid"
    if status == "valid":
        for key in ("volume", "amount"):
            value = row.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                status = "missing_volume"
                break
    row["volume_status"] = status
    if status != "valid":
        row["volume"] = row["amount"] = None
    return row
