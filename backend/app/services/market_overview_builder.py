"""市场总览数据装配(与 HTTP Request 解耦)。

HTTP 总览与只读 AI 助手共用本地美股样本的量价聚合。
涨跌幅缺失不计为平盘，比例只以有效样本为分母；无可用值返回 None。
保留 depth_service 入参兼容既有 API，但不读取它。

公共入口:
    build_market_overview(repo, quote_service, depth_service, as_of)
"""
from __future__ import annotations

import math
import re
from datetime import date
from statistics import median
from typing import Any

import polars as pl

from app.services.ext_data import ExtConfig, ExtConfigStore
from app.services.index_const import CORE_INDEX_NAMES, CORE_INDEX_SYMBOLS
from app.services.screener import ScreenerService

# ================================================================
# 常量(核心指数清单单一权威: app.services.index_const)
# ================================================================

_DIMENSION_SEP = re.compile(r"[、,，;；|/\s]+")


# ================================================================
# 通用工具
# ================================================================

def _finite(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _coverage(valid: int, total: int) -> dict:
    return {
        "status": "unavailable" if not valid else "available" if valid == total else "partial",
        "valid": valid,
        "unknown": total - valid,
    }


def _values(rows: list[dict], key: str, *, nonnegative: bool = False) -> list[float]:
    values = [_finite(row.get(key)) for row in rows]
    return [v for v in values if v is not None and (not nonnegative or v >= 0)]


# ================================================================
# 指数行情(实时 quote_service 优先,回退 kline_index_daily SQL)
# ================================================================

def _quote_status(quote_service) -> dict:
    qs = quote_service
    if not qs:
        return {"enabled": False, "running": False, "quote_age_ms": None, "is_trading_hours": False}
    return qs.status()


def _index_quotes(repo, quote_service, as_of: date | None = None) -> list[dict]:
    rows: list[dict] = []
    source = "unavailable"
    if quote_service and as_of is None:
        df = quote_service.get_index_quotes(list(CORE_INDEX_SYMBOLS))
        if not df.is_empty():
            rows = df.to_dicts()
            source = "realtime_cache"

    if not rows and repo:
        placeholders = ", ".join("?" for _ in CORE_INDEX_SYMBOLS)
        try:
            db_rows = repo.execute_all(
                f"""
                WITH ranked AS (
                    SELECT symbol, date, close,
                           row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                    FROM kline_index_daily
                    WHERE symbol IN ({placeholders})
                      AND (? IS NULL OR date <= ?)
                ), latest AS (
                    SELECT symbol,
                           max(CASE WHEN rn = 1 THEN date END) AS date,
                           max(CASE WHEN rn = 1 THEN close END) AS last_price,
                           max(CASE WHEN rn = 2 THEN close END) AS prev_close
                    FROM ranked
                    WHERE rn <= 2
                    GROUP BY symbol
                )
                SELECT symbol, date, last_price, prev_close
                FROM latest
                """,
                [*CORE_INDEX_SYMBOLS, as_of, as_of],
            )
        except Exception:  # noqa: BLE001
            db_rows = []
        for symbol, dt, last_price, prev_close in db_rows:
            source = "daily"
            change_amount = None
            change_pct = None
            lp = _finite(last_price)
            pc = _finite(prev_close)
            if lp is not None and pc not in (None, 0):
                change_amount = lp - pc
                change_pct = change_amount / pc * 100
            rows.append({
                "symbol": symbol,
                "name": CORE_INDEX_NAMES.get(symbol),
                "date": str(dt) if dt else None,
                "last_price": lp,
                "close": lp,
                "prev_close": pc,
                "change_amount": change_amount,
                "change_pct": change_pct,
            })

    by_symbol = {r.get("symbol"): r for r in rows}
    out = []
    for symbol in CORE_INDEX_SYMBOLS:
        r = by_symbol.get(symbol, {"symbol": symbol})
        out.append({
            "symbol": symbol,
            "name": r.get("name") or CORE_INDEX_NAMES[symbol],
            "last_price": _finite(r.get("last_price") if r.get("last_price") is not None else r.get("close")),
            "change_pct": _finite(r.get("change_pct")),
            "change_amount": _finite(r.get("change_amount")),
            "date": str(r["date"]) if r.get("date") is not None else None,
            "source": source if symbol in by_symbol else "unavailable",
        })
    return out


# ================================================================
# 扩展数据(行业 / 概念)维度聚合
# ================================================================

def _dimension_field(config: ExtConfig, kind: str) -> str | None:
    candidates = ["概念", "concept", "theme"] if kind == "concept" else ["行业", "industry", "sector"]
    for candidate in candidates:
        needle = candidate.lower()
        for field in config.fields:
            haystack = f"{field.name} {field.label}".lower()
            if needle in haystack:
                return field.name
    return None


def _ext_files(data_dir, config: ExtConfig) -> list[str]:
    base = data_dir / "ext_data" / config.id
    if config.mode == "timeseries":
        root = base / "timeseries"
        return [str(p) for p in sorted(root.rglob("*.parquet")) if p.is_file()]
    return [str(p) for p in sorted(base.glob("*.parquet")) if p.is_file()]


def _read_ext_rows(data_dir, config: ExtConfig, dimension_field: str) -> list[dict]:
    files = _ext_files(data_dir, config)
    if not files:
        return []
    try:
        df = pl.read_parquet(files, hive_partitioning=True)
    except TypeError:
        try:
            df = pl.read_parquet(files)
        except Exception:  # noqa: BLE001
            return []
    except Exception:  # noqa: BLE001
        return []
    if df.is_empty() or dimension_field not in df.columns:
        return []

    if config.mode == "timeseries" and "date" in df.columns:
        latest = df.get_column("date").max()
        if latest is not None:
            df = df.filter(pl.col("date") == latest)

    symbol_cols = ["symbol", "code", "股票代码", "代码"]
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            symbol_cols.append(str(mapping["col"]))
    cols = []
    for col in [dimension_field, *symbol_cols]:
        if col in df.columns and col not in cols:
            cols.append(col)
    return df.select(cols).to_dicts()


def _dimension_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    values = [
        v.strip()
        for v in _DIMENSION_SEP.split(str(raw).strip())
        if v.strip() and v.strip().casefold() not in {"nan", "none", "null"}
    ]
    return values


def _symbol_keys(row: dict, config: ExtConfig) -> list[str]:
    fields = ["symbol", "code", "股票代码", "代码"]
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            fields.append(str(mapping["col"]))

    keys: list[str] = []
    for field in fields:
        raw = row.get(field)
        if raw is None:
            continue
        text = str(raw).strip().upper()
        if not text:
            continue
        keys.append(text)
        if "." in text:
            keys.append(text.split(".", 1)[0])
    return keys


def _leader_sort_key(row: dict) -> float:
    """领涨股排序键: 缺涨跌幅的成分股排最后。

    0.00% 是有效涨跌幅, 不能与"无行情"合并成同一个哨兵值 —— 板块整体下跌时
    平盘股就是领涨股。
    """
    value = _finite(row.get("change_pct"))
    return value if value is not None else float("-inf")


def _dimension_rank(rows: list[dict], repo, kind: str, limit: int = 5, level: int | None = None) -> dict:
    if not rows:
        return {"leading": [], "lagging": [], "status": "unavailable", "note": "维度数据缺失"}

    quote_map: dict[str, dict] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        quote_map[symbol] = row
        quote_map[symbol.split(".", 1)[0]] = row

    store = ExtConfigStore(repo.store.data_dir)
    groups: dict[str, dict[str, dict]] = {}
    group_source: dict[str, str] = {}  # 组名 → 首个命中的扩展字段 "configId.field" (看板成分股弹窗用)
    for config in store.load_all():
        field = _dimension_field(config, kind)
        if not field:
            continue
        source_field = f"{config.id}.{field}"
        for ext_row in _read_ext_rows(repo.store.data_dir, config, field):
            quote = None
            for key in _symbol_keys(ext_row, config):
                quote = quote_map.get(key)
                if quote:
                    break
            if not quote:
                continue
            symbol = str(quote.get("symbol") or "")
            for value in _dimension_values(ext_row.get(field)):
                # 行业按 "-" 拆分级: "银行-银行-股份制银行" → level=2 取"银行"(二级)
                if level is not None and "-" in value:
                    parts = value.split("-")
                    value = parts[level - 1] if level <= len(parts) else parts[-1]
                groups.setdefault(value, {})[symbol] = quote
                group_source.setdefault(value, source_field)

    items = []
    for name, by_symbol in groups.items():
        stocks = list(by_symbol.values())
        changes = [_finite(s.get("change_pct")) for s in stocks]
        changes = [v for v in changes if v is not None]
        if not changes:
            continue
        amounts = _values(stocks, "amount", nonnegative=True)
        leader = max(stocks, key=_leader_sort_key)
        items.append({
            "name": name,
            "count": len(stocks),
            "avg_pct": sum(changes) / len(changes),
            "up_count": sum(1 for v in changes if v > 0),
            "down_count": sum(1 for v in changes if v < 0),
            "amount": sum(amounts) if amounts else None,
            "amount_coverage": _coverage(len(amounts), len(stocks)),
            "change_coverage": _coverage(len(changes), len(stocks)),
            "source_field": group_source.get(name),
            "leader": {
                "symbol": leader.get("symbol"),
                "name": leader.get("name"),
                "change_pct": _finite(leader.get("change_pct")),
            },
        })

    leading = sorted(items, key=lambda x: x["avg_pct"], reverse=True)[:limit]
    lagging = sorted(items, key=lambda x: x["avg_pct"])[:limit]
    return {
        "leading": leading, "lagging": lagging,
        "status": "available" if items else "unavailable",
        "note": "仅统计已映射且有行情的维度样本" if items else "维度映射或行情数据缺失",
    }


# ================================================================
# Top 行 / 涨跌幅分桶
# ================================================================

def _top_rows(rows: list[dict], key: str, descending: bool, limit: int = 8) -> list[dict]:
    filtered = [r for r in rows if _finite(r.get(key)) is not None]
    filtered.sort(key=lambda r: _finite(r.get(key)) or 0, reverse=descending)
    return [
        {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "close": _finite(r.get("close")),
            "change_pct": _finite(r.get("change_pct")),
            "amount": _finite(r.get("amount")),
            "turnover_rate": _finite(r.get("turnover_rate")),

        }
        for r in filtered[:limit]
    ]


def _pct_band_rows(values: list[float]) -> list[dict]:
    if not values:
        return []
    bands = [
        ("<-5%", None, -0.05),
        ("-5~-3%", -0.05, -0.03),
        ("-3~-1%", -0.03, -0.01),
        ("-1~0%", -0.01, 0),
        ("0~1%", 0, 0.01),
        ("1~3%", 0.01, 0.03),
        ("3~5%", 0.03, 0.05),
        (">5%", 0.05, None),
    ]
    total = len(values)
    out = []
    for label, low, high in bands:
        count = 0
        for v in values:
            if low is None and v < high:
                count += 1
            elif high is None and v >= low:
                count += 1
            elif low is not None and high is not None and low <= v < high:
                count += 1
        out.append({"label": label, "count": count, "pct": count / total * 100})
    return out


# ================================================================
# 主装配入口
# ================================================================

def build_market_overview(repo, quote_service=None, depth_service=None, as_of: date | None = None) -> dict:
    """美股量价广度；缺失数据保持None，比例仅以有效值为分母。"""
    svc = ScreenerService(repo)
    explicit = as_of is not None
    as_of = as_of or svc.latest_date()
    status = _quote_status(quote_service)
    indices = _index_quotes(repo, quote_service, as_of if explicit else None)
    df = svc._load_enriched_for_date(as_of) if as_of else pl.DataFrame()
    rows = df.to_dicts() if not df.is_empty() else []
    total = len(rows)
    changes = _values(rows, "change_pct")
    coverage = _coverage(len(changes), total)
    up, down = sum(v > 0 for v in changes), sum(v < 0 for v in changes)
    amounts = _values(rows, "amount", nonnegative=True)
    trend = {}
    for key in ("ma5", "ma20", "ma60"):
        pairs = [(r.get("close"), r.get(key)) for r in rows]
        pairs = [(float(c), float(m)) for c,m in pairs if _finite(c) is not None and _finite(m) is not None and c > 0 and m > 0]
        count = sum(c >= m for c,m in pairs)
        trend.update({"above_" + key: count if pairs else None,
                      "above_" + key + "_pct": count / len(pairs) * 100 if pairs else None,
                      key + "_valid": len(pairs)})
    for label, field, sig in (("new_high", "high_60d", "signal_n_day_high"), ("new_low", "low_60d", "signal_n_day_low")):
        flags = []
        for r in rows:
            if r.get(sig) is not None:
                flags.append(bool(r[sig]))
            elif _finite(r.get("close")) is not None and _finite(r.get(field)) is not None:
                flags.append(r['close'] >= r[field] if label == 'new_high' else r['close'] <= r[field])
        trend[label] = sum(flags) if flags else None
    turnover = _values(rows, "turnover_rate", nonnegative=True)
    ratios = _values(rows, "vol_ratio_5d", nonnegative=True)
    return _json_safe({
        "market": "US", "currency": "USD", "as_of": str(as_of) if as_of else None,
        "data_status": coverage['status'],
        "note": "仅统计当前美股样本；未知数据标缺失" if coverage['status'] != 'available' else "当前美股样本量价概览",
        "quote_status": status, "indices": indices,
        "indices_status": _coverage(sum(_finite(r.get('last_price')) is not None for r in indices), len(indices)),
        "breadth": {"total": total, **coverage, "up": up, "down": down, "flat": len(changes)-up-down,
                    "up_pct": up/len(changes)*100 if changes else None,
                    "down_pct": down/len(changes)*100 if changes else None,
                    "avg_pct": sum(changes)/len(changes) if changes else None,
                    "median_pct": median(changes) if changes else None,
                    "strong_up": sum(v >= .03 for v in changes), "strong_down": sum(v <= -.03 for v in changes)},
        "amount": {"total": sum(amounts) if amounts else None, "avg": sum(amounts)/len(amounts) if amounts else None, **_coverage(len(amounts), total)},
        "distribution": _pct_band_rows(changes), "trend": trend,
        "activity": {"avg_turnover": sum(turnover)/len(turnover) if turnover else None,
                     "high_turnover": sum(v >= 5 for v in turnover) if turnover else None,
                     "vol_ratio": sum(ratios)/len(ratios) if ratios else None,
                     "high_vol_ratio": sum(v >= 1.5 for v in ratios)/len(ratios)*100 if ratios else None},
        "top_gainers": _top_rows(rows, 'change_pct', True), "top_losers": _top_rows(rows, 'change_pct', False),
        "turnover_leaders": _top_rows(rows, 'amount', True), "active_leaders": _top_rows(rows, 'turnover_rate', True),
        "concept_rank": _dimension_rank(rows, repo, 'concept'),
        "industry_rank": _dimension_rank(rows, repo, 'industry', level=2),
        "financials": {"status": "unavailable", "note": "财务数据源缺失，不能依据量价数据编造基本面"},
    })
