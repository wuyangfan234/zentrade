"""策略可访问的指数/ETF 日K读取模块 — 白名单放行的只读数据入口。

供 Custom/AI 策略在 filter_history 内读取任意指数(及 ETF)的完整日K。
策略通过白名单 import 本模块, 调用纯读函数; 禁止写操作或任意文件访问。

设计要点:
  - 模块自身是框架侧信任代码, 对策略的沙箱逃逸拦截(ai_generator._validate_safety)照旧生效。
  - repo 线程安全懒加载(首次调用才构建); DataStore() 默认 settings.data_dir, 与 main.py 同源。
  - 未知 symbol / 数据缺失 → 返回空 DataFrame(不抛), 与 repo 语义一致。
"""
from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Any

import polars as pl

from app.market_time import cn_today

logger = logging.getLogger(__name__)

# 完整历史默认区间下界(实际数据远晚于此, 仅作"全量"占位)。
_FULL_START = date(1990, 1, 1)

# ── repo 懒加载(线程安全) ─────────────────────────────
_repo = None
_lock = threading.Lock()


def _get_repo():
    global _repo
    if _repo is None:
        with _lock:
            if _repo is None:
                from app.storage.repository import DataStore, KlineRepository
                _repo = KlineRepository(DataStore())
    return _repo


def _set_repo(repo: Any) -> None:
    """测试注入: 用 fake repo 替换单例。"""
    global _repo
    with _lock:
        _repo = repo
    _non_stock_cache.clear()


def _reset_repo() -> None:
    """测试清理: 重置单例, 下次调用重新懒加载。"""
    global _repo
    with _lock:
        _repo = None
    _non_stock_cache.clear()


# ── 参数规范化 ─────────────────────────────────────────
def _norm_date(value, default: date) -> date:
    if value is None:
        return default
    if isinstance(value, str):
        return date.fromisoformat(value)
    return value


def _validate_symbol(symbol: Any) -> bool:
    return isinstance(symbol, str) and bool(symbol.strip())


# ── 公开只读 API ───────────────────────────────────────
def get_index_daily(symbol, start=None, end=None, columns=None):
    """读取指数日K(含技术指标)。未知 symbol / 无数据返回空 DataFrame。"""
    if not _validate_symbol(symbol):
        logger.warning("market_data: 非法指数 symbol %r", symbol)
        return pl.DataFrame()
    s = _norm_date(start, _FULL_START)
    # 窗口右端用 cn_today() 而不是主机本地 date.today(): 主机时区不可靠,
    # 未传 end 时可能把当日官方 K 排除在窗口外。
    e = _norm_date(end, cn_today())
    try:
        return _get_repo().get_index_daily(symbol, s, e, columns)
    except Exception as exc:
        logger.warning("market_data get_index_daily failed %s: %s", symbol, exc)
        return pl.DataFrame()


def get_etf_daily(symbol, start=None, end=None, columns=None):
    """读取 ETF 日K(含技术指标)。同 get_index_daily 语义。"""
    if not _validate_symbol(symbol):
        logger.warning("market_data: 非法 ETF symbol %r", symbol)
        return pl.DataFrame()
    s = _norm_date(start, _FULL_START)
    e = _norm_date(end, cn_today())
    try:
        return _get_repo().get_etf_daily(symbol, s, e, columns)
    except Exception as exc:
        logger.warning("market_data get_etf_daily failed %s: %s", symbol, exc)
        return pl.DataFrame()


def get_daily(symbol, start=None, end=None, columns=None):
    """按资产类型自动分派读取日K: 指数 → get_index_daily; ETF → get_etf_daily; 股票 → get_daily。"""
    if not _validate_symbol(symbol):
        logger.warning("market_data: 非法 symbol %r", symbol)
        return pl.DataFrame()
    s = _norm_date(start, _FULL_START)
    e = _norm_date(end, cn_today())
    repo = _get_repo()
    try:
        asset_type = repo.resolve_asset_type(symbol)
        if asset_type == "index":
            return repo.get_index_daily(symbol, s, e, columns)
        if asset_type == "etf":
            return repo.get_etf_daily(symbol, s, e, columns)
        return repo.get_daily(symbol, s, e, columns)
    except Exception as exc:
        logger.warning("market_data get_daily failed %s: %s", symbol, exc)
        return pl.DataFrame()


def scan_daily_columns(symbol, start=None, end=None, columns=None):
    """只扫股票日K parquet 的原始列 (如 ["date", "close"]), 不做指标计算、不触发 enriched 缓存刷新。

    0.37.0: 矩阵策略在回测矩阵里缺某只基准 (如 SPY) 时的轻量兜底。get_daily 在缓存冷时会同步重算整份
    enriched 并申请独占重活额度, 回测线程自己已占着额度, 走那条路会互等。未知 symbol / 无数据 = 空表。
    """
    if not _validate_symbol(symbol):
        logger.warning("market_data: 非法 symbol %r", symbol)
        return pl.DataFrame()
    s = _norm_date(start, _FULL_START)
    e = _norm_date(end, cn_today())
    try:
        return _get_repo()._scan_daily_symbol(symbol, s, e, list(columns) if columns else None)
    except Exception as exc:
        logger.warning("market_data scan_daily_columns failed %s: %s", symbol, exc)
        return pl.DataFrame()


def list_index_symbols() -> list[dict]:
    """列出已收录的指数符号(含名称)。无数据返回空列表。"""
    try:
        df = _get_repo().get_instruments_asset("index")
    except Exception as exc:
        logger.warning("market_data list_index_symbols failed: %s", exc)
        return []
    if df.is_empty() or "symbol" not in df.columns:
        return []
    name_col = "name" if "name" in df.columns else None
    cols = ["symbol"] + ([name_col] if name_col else [])
    return [
        {"symbol": row["symbol"], "name": row.get("name")}
        for row in df.select(cols).iter_rows(named=True)
    ]


# ── 非普通股集合 (0.37.0, 截面策略排名池用) ─────────────────
NON_STOCK_TYPES = frozenset({"etf", "fund", "preferred", "warrant", "unit", "right"})
NON_STOCK_FLAGS = ("is_fund", "is_etf", "is_preferred", "is_warrant", "is_unit")
_non_stock_cache: dict = {}


def non_stock_symbols() -> frozenset[str]:
    """instruments 元数据里的 ETF/基金/优先股/权证/单位/权利 (与 opportunity_service._load_universe 同一排除口径)。

    只读股票 instruments 维表 (repo.get_instruments_asset("stock")), 按行数+symbol 首尾缓存; 读不到 = 空集 (不剔除)。
    """
    try:
        df = _get_repo().get_instruments_asset("stock")
    except Exception as exc:
        logger.warning("market_data non_stock_symbols failed: %s", exc)
        return frozenset()
    if df is None or df.is_empty() or "symbol" not in df.columns:
        return frozenset()
    key = (id(df), df.height)
    hit = _non_stock_cache.get("v")
    if hit and hit[0] == key:
        return hit[1]
    try:
        mask = pl.lit(False)
        if "instrument_type" in df.columns:
            mask = mask | pl.col("instrument_type").cast(pl.Utf8).is_in(sorted(NON_STOCK_TYPES)).fill_null(False)
        for flag in NON_STOCK_FLAGS:
            if flag in df.columns:
                mask = mask | pl.col(flag).cast(pl.Boolean, strict=False).fill_null(False)
        out = frozenset(df.filter(mask)["symbol"].cast(pl.Utf8).to_list())
    except Exception as exc:
        logger.warning("market_data non_stock_symbols parse failed: %s", exc)
        out = frozenset()
    _non_stock_cache["v"] = (key, out)
    return out
