"""Read-only public US equity reference data, with atomic last-good cache.

Nasdaq screener sectors are Nasdaq labels, NOT GICS. ETF absence from this stock
screener is not evidence that an unknown symbol is an ETF. No sector is invented.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.market_clock import us_clock

logger = logging.getLogger(__name__)
NASDAQ_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
_CACHE_LOCK = threading.Lock()
_UNKNOWN = {"", "n/a", "na", "none", "null", "unknown", "-", "--", "?"}
# Exact curated benchmark identities; 'Trust' or 'Stock' is NEVER an ETF heuristic.
_BENCHMARK_ETFS = frozenset({"SPY", "QQQ", "DIA", "IWM"})


@dataclass(frozen=True)
class USReference:
    rows: dict[str, dict]
    as_of: str | None
    status: str  # fresh / cache / stale_cache / unavailable


def _label(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return None if value.casefold() in _UNKNOWN else value


def _fetch_nasdaq() -> dict:
    request = urllib.request.Request(NASDAQ_URL, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/",
    })
    with urllib.request.urlopen(request, timeout=25.0) as response:
        return json.loads(response.read())


def _parse_rows(body: dict) -> dict[str, dict]:
    data = body.get("data") if isinstance(body, dict) else None
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("Nasdaq reference has no rows")
    declared = data.get("totalrecords")
    if declared is not None and int(str(declared).replace(",", "")) != len(rows):
        raise ValueError("Nasdaq reference is incomplete (totalrecords != rows)")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid Nasdaq reference row")
        symbol = str(row.get("symbol") or "").strip()
        if not symbol or symbol in result:
            raise ValueError("empty or duplicate Nasdaq reference symbol")
        result[symbol] = {"symbol": symbol, "name": _label(row.get("name")),
                          "sector": _label(row.get("sector")), "industry": _label(row.get("industry"))}
    return result


def _read_cache(path: Path) -> USReference | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("schema_version") != 1 or body.get("source") != "nasdaq_screener":
            raise ValueError("unsupported reference cache")
        as_of = date.fromisoformat(body["as_of"])
        if as_of > us_clock.today():
            raise ValueError("future-dated reference cache")
        rows = _parse_rows({"data": {"rows": body["rows"], "totalrecords": body["totalrecords"]}})
        return USReference(rows, as_of.isoformat(), "cache")
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nasdaq reference cache unreadable: %s", exc)
        return None


def _publish_cache(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".nasdaq-", suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            json.dump(body, f, ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def load_us_reference(data_dir: Path, *, force: bool = False) -> USReference:
    """Refresh at most once per ET date; errors preserve last-good file and date.

    ``as_of`` is the download date, not the issuer's industry revision date.
    No key is needed and no trading/broker endpoint is used.
    """
    path = Path(data_dir) / "reference" / "nasdaq_stocks.json"
    with _CACHE_LOCK:
        old = _read_cache(path)
        now = us_clock.now()
        if not force and old is not None and old.as_of == now.date().isoformat():
            return old
        try:
            rows = _parse_rows(_fetch_nasdaq())
            as_of = now.date().isoformat()
            _publish_cache(path, {"schema_version": 1, "source": "nasdaq_screener",
                                 "url": NASDAQ_URL, "as_of": as_of, "fetched_at": now.isoformat(),
                                 "totalrecords": len(rows), "rows": list(rows.values())})
            return USReference(rows, as_of, "fresh")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Nasdaq reference refresh failed; retaining last-good date=%s: %s",
                           old.as_of if old else None, exc)
            return USReference(old.rows, old.as_of, "stale_cache") if old else USReference({}, None, "unavailable")


def classify_instrument(item: dict) -> dict:
    """Conservative security-type flags, without broad English substring bans.

    Prefer explicit metadata and precise security descriptors. Unknown is a real
    classification: absence from a stock list does not prove ETF or common stock.
    """
    name = str(item.get("name") or "")
    symbol = item.get("symbol")
    explicit = item.get("instrument_type") or item.get("security_type")
    known = {"common_stock", "etf", "preferred", "warrant", "unit", "fund", "adr", "right"}
    if explicit in known:
        kind, source = explicit, "provider"
    elif item.get("is_etf") is True or item.get("asset_type") == "etf":
        kind, source = "etf", "provider"
    elif symbol in _BENCHMARK_ETFS:
        kind, source = "etf", "benchmark_identity"
    else:
        source = "security_name"
        if re.search(r"\bETF\b|\bexchange[ -]traded fund\b", name, re.I):
            kind = "etf"
        elif re.search(r"\bpreferred\b|\bpreference shares\b", name, re.I):
            kind = "preferred"
        elif re.search(r"\bwarrants?\b", name, re.I):
            kind = "warrant"
        elif re.search(r"\brights?\s*$", name, re.I):
            kind = "right"
        elif re.search(r"\bunits?\s*(?:[,(]|$)", name, re.I):
            kind = "unit"
        elif re.search(r"\b(?:closed[ -]end|mutual) fund\b", name, re.I):
            kind = "fund"
        elif re.search(r"\bAmerican Depositary (?:Shares|Receipts)\b|\bADR\b", name, re.I):
            kind = "adr"
        elif re.search(r"\b(?:common stock|common shares|ordinary shares)\b", name, re.I):
            kind = "common_stock"
        else:
            kind, source = "unknown", "unclassified"
    return {"instrument_type": kind, "classification_source": source,
            "asset_type": "etf" if kind == "etf" else "stock",
            "is_etf": kind == "etf", "is_preferred": kind == "preferred",
            "is_warrant": kind == "warrant", "is_unit": kind == "unit",
            "risk_flags": [] if kind in {"common_stock", "adr"} else [kind]}


def enrich_instruments(items: list[dict], reference: USReference) -> list[dict]:
    rows = []
    for item in items:
        row = dict(item)
        ref = reference.rows.get(str(row.get("symbol") or ""))
        if ref:
            for field in ("sector", "industry"):
                if ref.get(field):
                    row[field] = ref[field]
                    row[f"{field}_source"] = "nasdaq_screener"
                    row[f"{field}_as_of"] = reference.as_of
            # Use Nasdaq's security descriptor when the broker label is abbreviated.
            classified = classify_instrument({**row, "name": ref.get("name") or row.get("name")})
        else:
            classified = classify_instrument(row)
        row.update(classified)
        row["reference_status"] = reference.status if ref else "not_listed" if reference.rows else reference.status
        row["reference_as_of"] = reference.as_of
        row["sector"] = _label(row.get("sector"))
        row["industry"] = _label(row.get("industry"))
        rows.append(row)
    return rows
