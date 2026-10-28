"""Screener API。"""
from __future__ import annotations

import contextlib
import glob as _glob
import logging
import math
import os
import time
from dataclasses import asdict, replace
from datetime import date, datetime
from typing import Any, Optional

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.config import settings
from app.db_safe import is_valid_ext_ident, quote_ident
from app.services import strategy_cache, strategy_run_queue
from app.services.screener import ScreenerService
from app.strategy import config as strategy_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screener", tags=["screener"])


class CustomRequest(BaseModel):
    conditions: list[str]
    order_by: Optional[str] = None
    limit: int = 30
    pool: Optional[list[str]] = None
    as_of: Optional[date] = None
    ext_columns: Optional[str] = None
    asset_type: str = "stock"


class PresetRequest(BaseModel):
    strategy_id: str
    pool: Optional[list[str]] = None
    as_of: Optional[date] = None
    ext_columns: Optional[str] = None
    asset_type: str = "stock"
    timeframe: str = "1d"


def _safe(result_dict: dict) -> dict:
    """Copy the complete response into JSON-safe values without mutating caches."""
    return _safe_ext_value(result_dict)


def _coverage_warnings(
    svc, as_of, *, engine=None, strategy_ids=None, params_map=None, overrides_map=None,
) -> list[str]:
    """数据充足性提示 (#303): enriched 覆盖低于暖机需求时给出人话警告。

    advisory 元数据: 任何计算失败都静默返回 [], 绝不影响选股主流程。
    引擎/服务无该方法时 (测试 Fake) 同样跳过, 与 build_shared_matrix 的
    可选能力探测同风格。
    """
    try:
        required: int | None = None
        rhb = getattr(engine, "required_history_bars", None)
        if engine is not None and strategy_ids and callable(rhb):
            required = rhb(strategy_ids, params_map=params_map, overrides_map=overrides_map)
        cw = getattr(svc, "coverage_warnings", None)
        if callable(cw):
            return cw(as_of, required_bars=required)
    except Exception:
        logger.debug("coverage warnings unavailable", exc_info=True)
    return []


def _safe_ext_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _safe_ext_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_ext_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


# 标识符安全原语 (转义 + 白名单) 集中在 app.db_safe, 见 Issue #150 注入防护。


# ── 扩展列 value_map 缓存 ────────────────────────────────────────────
# 每次请求 _load_ext_value_maps 都会重新从磁盘读 ext parquet 并重建 {symbol: value}。
# 用底层 parquet 文件的 (路径, mtime) 签名做 memoize: 文件未变则复用上次的 map,
# parquet 被重写 (mtime 变化) 时自动失效重算。仅缓存基于 config 的快照/时序路径,
# 无 config 的 DuckDB view 回退路径不缓存 (少见)。
_ext_value_map_cache: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}


def _ext_parquet_signature(cfg, data_dir) -> Optional[tuple]:
    """该扩展配置底层 parquet 文件的 (路径, mtime) 签名; 出错返回 None (禁用缓存)。"""
    try:
        from app.api.ext_data import _parquet_glob
        pattern = _parquet_glob(cfg, data_dir)
        files = sorted(_glob.glob(pattern, recursive=True))
        if not files:
            return None
        return tuple((f, os.path.getmtime(f)) for f in files)
    except Exception:  # noqa: BLE001
        return None


def _load_ext_value_maps(repo, ext_columns: Optional[str]) -> dict[str, dict[str, Any]]:
    """按请求加载扩展列，返回 {输出列名: {symbol: value}}。

    策略结果缓存是共享文件，不能被不同 ext_columns 组合污染；因此扩展列只在
    返回前通过该投影映射追加到结果副本中。

    基于 config 的路径按 parquet 文件 mtime 签名 memoize, 文件未变时跳过磁盘重读。
    """
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    if not ext_specs:
        return {}

    from app.api.ext_data import _read_ext_dataframe
    from app.services.ext_data import ExtConfigStore

    data_dir = repo.store.data_dir
    ext_store = ExtConfigStore(data_dir)
    configs = {c.id: c for c in ext_store.load_all()}
    value_maps: dict[str, dict[str, Any]] = {}

    for config_id, field_name in ext_specs:
        out_col = f"{config_id}__{field_name}"
        cfg = configs.get(config_id)
        cache_key = (config_id, field_name)
        sig = _ext_parquet_signature(cfg, data_dir) if cfg else None
        try:
            if cfg:
                # 命中缓存 (文件签名一致) → 复用, 免去磁盘重读
                cached = _ext_value_map_cache.get(cache_key)
                if cached is not None and sig is not None and cached[0] == sig:
                    value_maps[out_col] = cached[1]
                    continue
                # 时序扩展表只取最新分区，避免历史分区把同一 symbol JOIN 放大。
                ext_df, _ = _read_ext_dataframe(cfg, data_dir)
            else:
                view_name = f"ext_{config_id}"
                ext_df = repo.execute_frame(
                    f"SELECT symbol, {quote_ident(field_name)} FROM {view_name}"
                )

            if ext_df.is_empty() or "symbol" not in ext_df.columns or field_name not in ext_df.columns:
                continue

            ext_df = ext_df.select(["symbol", field_name]).unique(subset=["symbol"], keep="last")
            vmap = {
                str(row["symbol"]): _safe_ext_value(row.get(field_name))
                for row in ext_df.to_dicts()
                if row.get("symbol")
            }
            value_maps[out_col] = vmap
            if cfg and sig is not None:
                _ext_value_map_cache[cache_key] = (sig, vmap)
        except Exception as e:  # noqa: BLE001
            logger.debug("screener ext column join skipped for %s.%s: %s", config_id, field_name, e)

    return value_maps


def _row_with_ext(row: dict, ext_values: dict[str, dict[str, Any]], symbol: Optional[str] = None) -> dict:
    next_row = dict(row)
    sym = symbol or next_row.get("symbol")
    for out_col, value_map in ext_values.items():
        next_row[out_col] = value_map.get(str(sym)) if sym else None
    return next_row


def _rows_with_ext(rows: list[dict], ext_values: dict[str, dict[str, Any]]) -> list[dict]:
    if not ext_values:
        return rows
    return [_row_with_ext(r, ext_values) for r in rows]


def _result_with_ext(result_dict: dict, ext_values: dict[str, dict[str, Any]]) -> dict:
    if not ext_values:
        return result_dict
    return {**result_dict, "rows": _rows_with_ext(result_dict.get("rows", []), ext_values)}


def _results_with_ext(results: dict[str, dict], ext_values: dict[str, dict[str, Any]]) -> dict[str, dict]:
    if not ext_values:
        return results
    return {sid: _result_with_ext(r, ext_values) for sid, r in results.items()}


def _cache_payload_with_ext(cached: dict, ext_values: dict[str, dict[str, Any]]) -> dict:
    if not ext_values:
        return cached

    payload = dict(cached)
    payload["results"] = _results_with_ext(cached.get("results", {}), ext_values)

    ever_rows = cached.get("today_ever_rows")
    if isinstance(ever_rows, dict):
        enriched_ever: dict[str, dict[str, dict]] = {}
        for sid, sym_map in ever_rows.items():
            if not isinstance(sym_map, dict):
                continue
            enriched_ever[sid] = {
                sym: _row_with_ext(row, ext_values, symbol=sym)
                for sym, row in sym_map.items()
                if isinstance(row, dict)
            }
        payload["today_ever_rows"] = enriched_ever

    return payload


def _update_cache_strategy(data_dir, as_of: str, strategy_id: str, safe_data: dict) -> None:
    """单跑后更新缓存中该策略的结果，保持缓存与最新计算一致。"""
    from app.services import strategy_cache
    cached = strategy_cache.read_cache(data_dir)
    if cached and cached.get("as_of") == as_of:
        results = cached.get("results", {})
        results[strategy_id] = {
            "total": safe_data.get("total", 0),
            "as_of": as_of,
            "rows": safe_data.get("rows", []),
        }
        if safe_data.get("warnings"):
            # 数据不足提示 (#303) 随缓存下发 (get_cached 原样读出),
            # 单跑刷新不得冲掉 run_all 写入的提示
            results[strategy_id]["warnings"] = safe_data["warnings"]
        strategy_cache.write_cache(data_dir, as_of, results)


@router.get("/strategies")
def strategies(
    request: Request,
    asset_type: str = Query("stock"),
    timeframe: str = Query("1d"),
):
    """兼容策略清单端点；唯一数据源为 StrategyEngine。"""
    data_dir = request.app.state.repo.store.data_dir
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="策略引擎未初始化")
    presets = []
    for meta in engine.list_strategies():
        if meta.get("research_only"):
            continue
        if asset_type not in meta.get("asset_types", ["stock"]):
            continue
        if timeframe not in meta.get("timeframes", ["1d"]):
            continue
        sid = meta["id"]
        overrides = strategy_config.load_override(data_dir, sid)
        presets.append({
            **meta,
            "name": overrides.get("name") or meta["name"],
            "description": overrides.get("description") or meta.get("description", ""),
        })

    return _safe({"presets": presets, "load_errors": engine.load_errors()})


@router.post("/run")
def run_custom(req: CustomRequest, request: Request):
    repo = request.app.state.repo
    svc = ScreenerService(repo, asset_type=req.asset_type)
    as_of = req.as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400,
                            detail="无可用数据日期 — enriched 表为空,请先运行盘后管道")
    result = svc.run(
        as_of=as_of,
        conditions=req.conditions,
        order_by=req.order_by,
        limit=req.limit,
        pool=req.pool,
    )
    safe_data = _safe(asdict(result))
    warnings = _coverage_warnings(svc, as_of)
    if warnings:
        safe_data["warnings"] = warnings
    ext_values = _load_ext_value_maps(repo, req.ext_columns)
    return _result_with_ext(safe_data, ext_values)


@router.post("/run_preset")
def run_preset(req: PresetRequest, request: Request):
    repo = request.app.state.repo
    svc = ScreenerService(repo, asset_type=req.asset_type)
    as_of = req.as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400, detail="无可用数据日期 — enriched 表为空,请先运行盘后管道")

    # 加载用户保存的策略配置
    data_dir = request.app.state.repo.store.data_dir
    ext_values = _load_ext_value_maps(repo, req.ext_columns)
    overrides = strategy_config.load_override(data_dir, req.strategy_id)
    engine = getattr(request.app.state, "strategy_engine", None)
    if not engine:
        raise HTTPException(status_code=404, detail=f"策略引擎未初始化或策略 {req.strategy_id} 不存在")

    try:
        if not engine.has(req.strategy_id):
            raise ValueError(f"unknown strategy: {req.strategy_id}")
        if engine.get(req.strategy_id).meta.get("research_only"):
            raise ValueError(f"unknown strategy: {req.strategy_id}")
        params = dict(overrides.get("params") or {})
        current = None
        if req.timeframe == "1h":
            from app.services.watchlist import list_symbols
            symbols = [row["symbol"] for row in list_symbols() if isinstance(row.get("symbol"), str)]
            if req.pool:
                allowed = set(req.pool)
                symbols = [symbol for symbol in symbols if symbol in allowed]
            if not symbols:
                raise ValueError("自选为空, 1 小时策略只扫自选")
            current = svc._load_enriched_for_date(as_of).filter(pl.col("symbol").is_in(symbols))
        context = svc.build_strategy_context(
            engine,
            as_of,
            [req.strategy_id],
            timeframe=req.timeframe,
            params_map={req.strategy_id: params},
            overrides_map={req.strategy_id: overrides or {}},
            current=current,
        )
        result = engine.run(
            req.strategy_id,
            context,
            pool=req.pool,
            params=params,
            overrides=overrides or None,
        )
    except ValueError as e:
        status_code = 404 if "unknown strategy" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e)) from e

    safe_data = _safe(asdict(result))
    if req.timeframe == "1d":
        # 数据不足提示随结果返回并写入盘后缓存 (#303); 分钟周期结果不写入盘后缓存
        # (strategy_cache 是日线语义, as_of/updated_at 混入分钟结果会污染页面秒加载路径),
        # 分钟策略数据源也与日线 enriched 历史无关, 不提示。
        warnings = _coverage_warnings(
            svc, as_of, engine=engine, strategy_ids=[req.strategy_id],
            params_map={req.strategy_id: params},
            overrides_map={req.strategy_id: overrides or {}},
        )
        if warnings:
            safe_data["warnings"] = warnings
        _update_cache_strategy(data_dir, str(as_of), req.strategy_id, safe_data)

    return _result_with_ext(safe_data, ext_values)


def _overlay_realtime_result(base: dict | None, realtime: dict) -> dict:
    """监控实时结果叠加到盘后结果上 (0.39.4 审计 M-8).

    realtime["covered_symbols"] 为 None/缺省 = 全市场实时结果, 整体替换;
    否则只覆盖这些股票: 覆盖范围内以实时行为准, 范围外保留盘后全市场结果.
    盘后结果不是同一天的, 不拿来拼 (避免新旧两天混在一张表里), 仍用实时子集.
    """
    covered = realtime.get("covered_symbols")
    clean = {k: v for k, v in realtime.items() if k != "covered_symbols"}
    if covered is None or not isinstance(base, dict) or base.get("as_of") != realtime.get("as_of"):
        return clean
    covered_set = {str(symbol) for symbol in covered}
    kept = [
        row for row in base.get("rows") or []
        if isinstance(row, dict) and str(row.get("symbol")) not in covered_set
    ]
    rows = kept + list(realtime.get("rows") or [])
    if any(isinstance(row.get("score"), (int, float)) for row in rows):
        # 两路结果都按评分降序, 拼接后重排; 没评分的排在后面
        rows.sort(key=lambda row: (
            not isinstance(row.get("score"), (int, float)),
            -float(row["score"]) if isinstance(row.get("score"), (int, float)) else 0.0,
        ))
    merged = {**base, **clean, "rows": rows, "total": len(rows)}
    if "computed_at" in base:
        merged["computed_at"] = base["computed_at"]
    return merged


def _cached_with_realtime(request: Request) -> dict:
    """读取盘后缓存，并用监控引擎的实时结果覆盖同策略。"""
    data_dir = request.app.state.repo.store.data_dir
    cached = strategy_cache.read_cache(data_dir)
    if cached is None:
        cached = {"as_of": None, "results": {}, "updated_at": None}

    # 叠加监控引擎内存里的实时结果 (若有), 用新鲜数据覆盖同策略的盘后结果
    monitor_engine = getattr(request.app.state, "monitor_engine", None)
    if monitor_engine is not None:
        realtime_results = monitor_engine.latest_strategy_results()
        if realtime_results:
            results = dict(cached.get("results") or {})
            for sid, realtime in realtime_results.items():
                results[sid] = _overlay_realtime_result(results.get(sid), realtime)
            cached = dict(cached)
            cached["results"] = results
            # 有实时数据时, 以最新时间戳为准
            import time as _time
            cached["updated_at"] = int(_time.time() * 1000)

    return cached


@router.get("/cached")
def get_cached(
    request: Request,
    ext_columns: Optional[str] = Query(None, description="逗号分隔: config_id.field_name"),
):
    """读取策略结果缓存, 并叠加监控引擎本轮实时算出的结果。

    - 盘后缓存 (strategy_cache.json): 非监控策略 / 页面秒加载用, run_all 写入。
    - 监控引擎内存结果 (latest_strategy_results): 实时行情每轮对「加入监控的策略」算出,
      不落盘 (避免与 read_cache 的 mtime 校验冲突), 在此直接叠加覆盖盘后结果。
      被监控的策略拿到新鲜数据, 非监控策略仍用盘后缓存。
    """
    cached = _cached_with_realtime(request)

    # 无任何数据 (盘后缓存空 + 无实时结果) → 返回空标记, 前端据此提示
    if not cached.get("results") and cached.get("as_of") is None:
        return {"as_of": None, "results": {}, "updated_at": None}

    ext_values = _load_ext_value_maps(request.app.state.repo, ext_columns)
    return _safe(_cache_payload_with_ext(cached, ext_values))


@router.get("/cached-summary")
def get_cached_summary(request: Request):
    """返回策略卡片所需的轻量摘要，不序列化股票明细。"""
    cached = _cached_with_realtime(request)
    results = cached.get("results") or {}
    summary = {
        sid: {
            "total": int(result.get("total") or 0),
            "as_of": result.get("as_of"),
            # 渐进式 run_all 写入的计算时间戳; 监控实时叠加/旧缓存无此字段 → None,
            # 前端视为新鲜 (有值即为最新一轮实时结果)
            "computed_at": result.get("computed_at"),
        }
        for sid, result in results.items()
        if isinstance(result, dict)
    }

    cached_as_of = cached.get("as_of")
    ever_rows = cached.get("today_ever_rows") or {}
    ever_counts = {}
    for sid, result in results.items():
        if not isinstance(result, dict) or result.get("as_of") != cached_as_of:
            continue
        current_symbols = {
            str(row["symbol"])
            for row in result.get("rows") or []
            if isinstance(row, dict) and row.get("symbol")
        }
        ever_counts[sid] = len(set((ever_rows.get(sid) or {}).keys()) | current_symbols)
    return {
        "as_of": cached_as_of,
        "results": summary,
        "today_ever_counts": ever_counts,
        "updated_at": cached.get("updated_at"),
    }


@router.get("/cached-result/{strategy_id}")
def get_cached_result(
    strategy_id: str,
    request: Request,
    ext_columns: Optional[str] = Query(None, description="逗号分隔: config_id.field_name"),
):
    """按需返回单个策略的完整明细及其今日失效行。"""
    cached = _cached_with_realtime(request)
    raw_result = (cached.get("results") or {}).get(strategy_id)
    if not isinstance(raw_result, dict):
        return {
            "result": None,
            "today_ever_rows": None,
            "strategy_ids_by_symbol": {},
            "updated_at": cached.get("updated_at"),
        }

    ext_values = _load_ext_value_maps(request.app.state.repo, ext_columns)
    result = {
        "as_of": raw_result.get("as_of"),
        "strategy": strategy_id,
        "rows": _rows_with_ext(raw_result.get("rows") or [], ext_values),
        "total": int(raw_result.get("total") or 0),
        "elapsed_ms": 0.0,
    }

    ever_rows = None
    if cached.get("as_of") == result["as_of"]:
        strategy_ever_rows = (cached.get("today_ever_rows") or {}).get(strategy_id)
        if isinstance(strategy_ever_rows, dict):
            ever_rows = {
                symbol: _row_with_ext(row, ext_values, symbol=symbol)
                for symbol, row in strategy_ever_rows.items()
                if isinstance(row, dict)
            }

    selected_symbols = {
        str(row["symbol"])
        for row in raw_result.get("rows") or []
        if isinstance(row, dict) and row.get("symbol")
    }
    strategy_ids_by_symbol: dict[str, list[str]] = {symbol: [] for symbol in selected_symbols}
    for sid, cached_result in (cached.get("results") or {}).items():
        if not isinstance(cached_result, dict) or cached_result.get("as_of") != result["as_of"]:
            continue
        for row in cached_result.get("rows") or []:
            symbol = str(row.get("symbol")) if isinstance(row, dict) and row.get("symbol") else None
            if symbol in strategy_ids_by_symbol:
                strategy_ids_by_symbol[symbol].append(sid)

    return _safe({
        "result": result,
        "today_ever_rows": ever_rows,
        "strategy_ids_by_symbol": strategy_ids_by_symbol,
        "updated_at": cached.get("updated_at"),
    })


@router.get("/market-snapshot")
def market_snapshot(request: Request):
    """最新全市场轻量行情快照，供板块/概念聚合分析使用。"""
    import polars as pl

    repo = request.app.state.repo
    svc = ScreenerService(repo)
    as_of = svc.latest_date()
    if not as_of:
        return {"as_of": None, "rows": []}

    df = svc._load_enriched_for_date(as_of)
    if df.is_empty():
        return {"as_of": str(as_of), "rows": []}

    if "close" in df.columns and "total_shares" in df.columns and "market_cap" not in df.columns:
        df = df.with_columns((pl.col("close") * pl.col("total_shares")).alias("market_cap"))
    if "close" in df.columns and "float_shares" in df.columns and "float_market_cap" not in df.columns:
        df = df.with_columns((pl.col("close") * pl.col("float_shares")).alias("float_market_cap"))

    cols = [
        "symbol", "name", "close", "change_pct", "amount", "volume",
        "turnover_rate", "vol_ratio_5d", "total_shares", "float_shares",
        "market_cap", "float_market_cap",
    ]
    df = df.select([c for c in cols if c in df.columns])
    rows = df.to_dicts()
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None

    return {"as_of": str(as_of), "rows": rows}


def _run_all_progressive(
    *,
    repo,
    engine,
    svc: ScreenerService,
    as_of,
    asset_type: str,
    timeframe: str,
    all_ids: list[str],
    params_map: dict,
    overrides_map: dict,
    first_return_s: float,
    t_total: float,
) -> dict:
    """run_all 渐进式执行: 快策略随响应先返回, 慢策略后台算完逐个落缓存。

    执行全程在单飞执行器里 (见 services/strategy_run_queue.py): 相同请求
    搭车现有执行, 不同请求排队; HTTP 侧只轮询状态快照到首返时限。
    """
    data_dir = repo.store.data_dir
    key = (asset_type, timeframe, str(as_of), tuple(sorted(all_ids)))
    ordered_ids = strategy_run_queue.order_strategy_ids(
        all_ids, strategy_run_queue.load_run_timings(data_dir)
    )

    def job(handle: strategy_run_queue.StrategyRunHandle) -> None:
        context = svc.build_strategy_context(
            engine,
            as_of,
            ordered_ids,
            timeframe=timeframe,
            params_map=params_map,
            overrides_map=overrides_map,
        )
        # 逐策略 run_all 不会把矩阵回写 context.market → 每个矩阵策略都会重建
        # 全市场矩阵 (小服务器上单次数秒到十余秒)。这里按字段并集一次建好复用;
        # FakeEngine 等无该方法的实现跳过 (保持旧行为)。
        if getattr(context, "market", None) is None:
            build_matrix = getattr(engine, "build_shared_matrix", None)
            if callable(build_matrix):
                matrix = build_matrix(
                    context,
                    [(sid, engine.get(sid)) for sid in ordered_ids],
                    params_map,
                    overrides_map,
                )
                if matrix is not None:
                    context = replace(context, market=matrix)
        all_results: dict[str, dict] = {}
        elapsed_map: dict[str, float] = {}
        for sid in ordered_ids:
            t0 = time.perf_counter()
            # 逐策略隔离: 单个策略崩溃 (如自定义代码的数据类型错误) 只记
            # 错误跳过, 不让整批剩余策略陪葬 — 其余策略照常算完落缓存。
            try:
                single = engine.run_all(
                    context,
                    params_map=params_map,
                    overrides_map=overrides_map,
                    strategy_ids=[sid],
                    parallel=False,
                )
                result = single[sid]
            except Exception as e:
                logger.warning("run_all: 策略 %s 执行失败, 跳过: %s", sid, e, exc_info=True)
                handle.fail_one(sid, str(e))
                continue
            safe_result = _safe(asdict(result))
            payload = {
                "total": safe_result["total"],
                "as_of": str(as_of),
                "rows": safe_result.get("rows", []),
                "computed_at": int(time.time() * 1000),
            }
            if timeframe == "1d":
                w = _coverage_warnings(
                    svc, as_of, engine=engine, strategy_ids=[sid],
                    params_map=params_map, overrides_map=overrides_map,
                )
                if w:
                    payload["warnings"] = w
            all_results[sid] = payload
            elapsed_map[sid] = (time.perf_counter() - t0) * 1000
            # 逐策略增量落盘 (write_cache 同日按 sid 合并), 前端轮询即可逐个看到
            try:
                strategy_cache.write_cache(data_dir, str(as_of), {sid: payload})
            except Exception:
                logger.warning("run_all 渐进写入缓存失败: %s", sid, exc_info=True)
            handle.complete(sid, {k: v for k, v in payload.items() if k != "rows"})
        # 收尾: 与旧版口径一致的整体重写 + 耗时落盘供下次排序
        if all_results:
            with contextlib.suppress(Exception):
                strategy_cache.write_cache(data_dir, str(as_of), all_results)
        strategy_run_queue.record_run_timings(data_dir, elapsed_map)

    handle = strategy_run_queue.MANAGER.get_or_submit(key, ordered_ids, job)
    deadline = time.perf_counter() + first_return_s
    snap = handle.snapshot()
    while not snap["done"] and time.perf_counter() < deadline:
        time.sleep(0.2)
        snap = handle.snapshot()

    done_results = snap["results"]
    if snap["error"] and not done_results:
        raise HTTPException(status_code=500, detail=snap["error"])
    logger.info(
        "run_all: first return %.1fms (%d done, %d pending)",
        (time.perf_counter() - t_total) * 1000,
        len(done_results),
        len(snap["pending"]),
    )
    return {
        "as_of": str(as_of),
        "results": done_results,
        "pending": snap["pending"],
        "errors": snap["errors"],
        "complete": snap["done"] and not snap["error"],
        "error": snap["error"],
        "started_at": snap["started_at_ms"],
    }


@router.post("/run_all")
def run_all(request: Request, body: Optional[dict] = None):
    """批量运行指定策略；注册、路由和执行均由 StrategyEngine 负责。"""
    from datetime import date as date_type

    t_total = time.perf_counter()

    body = body or {}
    repo = request.app.state.repo
    asset_type = str(body.get("asset_type") or "stock")
    timeframe = str(body.get("timeframe") or "1d")
    svc = ScreenerService(repo, asset_type=asset_type)
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="策略引擎未初始化")

    # 解析日期
    raw_date = body.get("as_of")
    if raw_date:
        # 与 /custom、/preset 的 `as_of: date` 同口径: 只收 ISO 日期字符串。
        # 非字符串原样透传会让 str(as_of) 把 "20260904" 之类写进 strategy_cache.json,
        # 与其它入口写的 "2026-09-04" 不是同一格式, 后续按 as_of 比对缓存永远失配。
        if not isinstance(raw_date, str):
            raise HTTPException(status_code=400, detail="as_of 必须是 YYYY-MM-DD 日期字符串")
        try:
            as_of = date_type.fromisoformat(raw_date)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"日期格式错误: {e}") from e
    else:
        as_of = svc.latest_date()
    if not as_of:
        return {"as_of": None, "results": {}}

    data_dir = request.app.state.repo.store.data_dir

    requested_ids = body.get("strategy_ids")
    if requested_ids and isinstance(requested_ids, list):
        all_ids = [str(sid) for sid in requested_ids]
        unknown = [
            sid
            for sid in all_ids
            if not engine.has(sid) or engine.get(sid).meta.get("research_only")
        ]
        if unknown:
            raise HTTPException(status_code=404, detail=f"unknown strategies: {unknown}")
    else:
        all_ids = [
            meta["id"]
            for meta in engine.list_strategies()
            if not meta.get("research_only")
            and asset_type in meta.get("asset_types", ["stock"])
            and timeframe in meta.get("timeframes", ["1d"])
        ]

    if not all_ids:
        return {"as_of": str(as_of), "results": {}}

    # 批量预加载所有 override 配置
    t0 = time.perf_counter()
    all_overrides = strategy_config.list_overrides(data_dir)
    logger.info("run_all: list_overrides took %.1fms (%d overrides)", (time.perf_counter() - t0) * 1000, len(all_overrides))

    params_map = {
        sid: dict((all_overrides.get(sid) or {}).get("params") or {})
        for sid in all_ids
    }
    overrides_map = {sid: all_overrides.get(sid, {}) for sid in all_ids}

    # 渐进式返回 (页面首屏路径): 按历史耗时升序执行, 首返时限内算完的随响应
    # 返回, 慢策略转后台继续算并逐个写入策略缓存, 前端轮询 cached-summary 点亮。
    # 仅日线 + summary_only (策略页卡片) 启用; 分钟/明细请求保持整段阻塞。
    first_return_s = settings.strategy_run_all_first_return_s
    if body.get("summary_only") and timeframe == "1d" and first_return_s > 0:
        return _run_all_progressive(
            repo=repo,
            engine=engine,
            svc=svc,
            as_of=as_of,
            asset_type=asset_type,
            timeframe=timeframe,
            all_ids=all_ids,
            params_map=params_map,
            overrides_map=overrides_map,
            first_return_s=first_return_s,
            t_total=t_total,
        )

    try:
        context = svc.build_strategy_context(
            engine,
            as_of,
            all_ids,
            timeframe=timeframe,
            params_map=params_map,
            overrides_map=overrides_map,
        )
        engine_results = engine.run_all(
            context,
            params_map=params_map,
            overrides_map=overrides_map,
            strategy_ids=all_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    results: dict[str, dict] = {}
    for sid, result in engine_results.items():
        safe_result = _safe(asdict(result))
        results[sid] = {
            "total": safe_result["total"],
            "as_of": str(as_of),
            "rows": safe_result.get("rows", []),
        }
        if timeframe == "1d":
            w = _coverage_warnings(
                svc, as_of, engine=engine, strategy_ids=[sid],
                params_map=params_map, overrides_map=overrides_map,
            )
            if w:
                results[sid]["warnings"] = w

    elapsed = (time.perf_counter() - t_total) * 1000
    logger.info("run_all: total took %.1fms (%d strategies)", elapsed, len(all_ids))

    # 写入策略缓存 (供页面秒加载); 分钟周期结果不落盘 (日线语义缓存)
    if results and timeframe == "1d":
        try:
            strategy_cache.write_cache(data_dir, str(as_of), results)
        except Exception:  # noqa: BLE001
            pass

    if body.get("summary_only"):
        return {
            "as_of": str(as_of),
            "results": {
                sid: {"total": result["total"], "as_of": result["as_of"]}
                for sid, result in results.items()
            },
        }

    ext_values = _load_ext_value_maps(repo, body.get("ext_columns"))
    return {"as_of": str(as_of), "results": _results_with_ext(results, ext_values)}



def _parse_ext_columns(ext_columns: str) -> list[tuple[str, str]]:
    """解析 'config_id1.field1,config_id2.field2' 为 [(config_id, field_name), ...]。"""
    result = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id = config_id.strip()
        field_name = field_name.strip()
        if not config_id or not field_name:
            continue
        if not is_valid_ext_ident(config_id) or "\x00" in field_name:
            continue
        result.append((config_id, field_name))
    return result
