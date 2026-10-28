"""Short borrow eligibility: strict broker booleans, timestamped 30-second TTL.

This is a read-only assets query, never an order. Expired/failed/malformed
responses fail closed. Entry intent and final fill explicitly refresh it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from app.market_clock import us_clock

logger = logging.getLogger(__name__)
TTL_SECONDS = 30


def _cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "decision_desk" / "shortable.json"


def _load(data_dir: Path) -> dict:
    try:
        d = json.loads(_cache_path(data_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("symbols"), dict) else {"symbols": {}}
    except (OSError, ValueError):
        return {"symbols": {}}


def _save(data_dir: Path, data: dict) -> None:
    from app.services.fs_utils import atomic_write_text
    p = _cache_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(data, ensure_ascii=False))


def _query(symbol: str) -> dict | None:
    from app.plugins.alpaca import provider as ap
    p = ap.AlpacaProvider()
    if not p.key or not p.secret:
        return None
    d = ap._get(f"{ap.PAPER_BASE}/assets/{symbol}", p.key, p.secret)
    # Do not coerce strings/numbers to bool: "false" and 1 are not broker proof.
    return {"shortable": d.get("shortable"), "easy_to_borrow": d.get("easy_to_borrow")}


def _fresh(hit, now) -> bool:
    if not isinstance(hit, dict):
        return False
    try:
        at = datetime.fromisoformat(hit['checked_at'])
        age = (now - at).total_seconds() if at.tzinfo else -1
        return 0 <= age < TTL_SECONDS
    except (KeyError, ValueError, TypeError):
        return False


def checker(data_dir: Path, *, query=None):
    """callable(symbol, *, refresh=False) -> bool; reload persisted cache each call.

    Tests may inject a provider response, never a production fail-open boolean.
    checked_at is request start, so a slow request cannot fabricate freshness.
    """
    def ok(symbol: str, *, refresh: bool = False) -> bool:
        now = us_clock.now()
        cache = _load(data_dir)
        hit = cache['symbols'].get(symbol)
        if refresh or not _fresh(hit, now):
            reason = 'borrow_unavailable'
            try:
                result = (query if query is not None else _query)(symbol)
                valid = isinstance(result, dict) and all(type(result.get(k)) is bool for k in ('shortable', 'easy_to_borrow'))
                if valid:
                    reason = '' if result['shortable'] and result['easy_to_borrow'] else 'not_easy_to_borrow'
            except Exception as e:  # noqa: BLE001
                logger.warning("shortable check %s failed: %s", symbol, type(e).__name__)
                valid, result = False, {}
            hit = {'shortable': result['shortable'] if valid else False,
                   'easy_to_borrow': result['easy_to_borrow'] if valid else False,
                   'checked_at': now.isoformat(), 'ttl_seconds': TTL_SECONDS, 'reason': reason}
            cache['symbols'][symbol] = hit
            try:
                _save(data_dir, cache)
            except OSError:
                pass
        return (_fresh(hit, us_clock.now()) and hit.get('shortable') is True
                and hit.get('easy_to_borrow') is True)
    return ok
