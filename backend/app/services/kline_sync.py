"""日 K 同步服务(§7.7 Step 1)。

调度器在 capability 允许下,把符号集合的日 K 批量同步到本地 Parquet。
策略:
  - 日 K 仅使用 `kline.daily.batch`
  - 除权因子仅使用 `adj_factor`
"""
from __future__ import annotations

import contextlib
import json
import logging
import shutil
import time
import threading
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers.base import AssetType
from app.indicators.pipeline import filter_halt_days
from app.market_clock import US_SESSION_CLOSE, US_SESSION_OPEN, us_clock
from app.services import minute_adjust, preferences
from app.data_providers.models import Cap, CapabilitySet
from app.data_providers.rate_limits import resolve_limit
from app.storage.repository import KlineRepository, replace_with_retry

logger = logging.getLogger(__name__)

# 小查询短缓存：真实无成交分钟/空日不应让每次 UI 轮询都重拉全天。
# 仅缓存成功结果（包括成功空集），错误不缓存；大批量同步/进度回调绕过。
_MINUTE_QUERY_CACHE: dict[tuple, tuple[float, object, pl.DataFrame]] = {}
_MINUTE_QUERY_CACHE_LOCK = threading.Lock()
_MINUTE_QUERY_CACHE_TTL = 60.0



def _atomic_write_parquet(df: pl.DataFrame, out) -> None:
    """先写临时文件再原子替换, 避免进程中断留下损坏的 parquet。

    与 repository._atomic_write_parquet 同语义。adj_factor 的 all.parquet 是全市场
    单文件、每次「读→concat→原地写」, 直接 write_parquet(out) 在进程被 kill
    (dev.sh 清端口用 kill -9)、reap 超时或断电时会留下半截文件, 之后复权视图
    scan_parquet 整条链路报错、enriched 全市场重算不出。临时文件后缀 .tmp 不匹配
    *.parquet glob, 不会被扫描误读。Windows 下目标正被并发读取时由
    replace_with_retry 短退避穿过。
    """
    tmp = out.with_name(out.name + ".tmp")
    df.write_parquet(tmp)
    replace_with_retry(tmp, out)


# 标准列(无论 SDK 返回什么形状,我们把它规范成这套)
CANONICAL_DAILY_COLS = [
    "symbol", "date", "open", "high", "low", "close", "volume", "amount",
]


def _normalize_daily(df_in, default_symbol: str | None = None) -> pl.DataFrame:
    """把 SDK 返回的 pandas/任意 DataFrame 规范成 canonical 列。"""
    if df_in is None or len(df_in) == 0:
        return pl.DataFrame()

    if not isinstance(df_in, pl.DataFrame):
        df = pl.from_pandas(df_in.reset_index() if hasattr(df_in, "reset_index") else df_in)
    else:
        df = df_in

    # 兼容字段名差异
    rename_map = {
        "ts_code": "symbol",
        "trade_date": "date",
        "vol": "volume",
        "amt": "amount",
        "datetime": "date",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})

    if "symbol" not in df.columns and default_symbol is not None:
        df = df.with_columns(pl.lit(default_symbol).alias("symbol"))

    # 类型规范
    if "date" in df.columns and df.schema["date"] != pl.Date:
        df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))

    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    for col in ("volume", "amount"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))

    # 过滤停牌日 (open/high 为 0; close 可能被填充为前收盘价, 不能用全零判断)
    df = filter_halt_days(df)

    # 只保留 canonical 列
    keep = [c for c in CANONICAL_DAILY_COLS if c in df.columns]
    return df.select(keep)


def _instruments_symbol_index(data_dir) -> dict[str, list[str]] | None:
    """裸代码 → instruments 维表完整符号列表; 维表缺失/不可读返回 None。"""
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        return None
    try:
        symbols = pl.read_parquet(path, columns=["symbol"])["symbol"].drop_nulls().to_list()
    except Exception as e:
        logger.warning("instruments 维表读取失败, 裸符号无法补全后缀: %s", e)
        return None
    index: dict[str, list[str]] = {}
    for s in symbols:
        code, _, _suffix = str(s).partition(".")
        if code:
            index.setdefault(code, []).append(str(s))
    return index


def _normalize_bare_symbols(
    symbols: list[str], data_dir,
) -> tuple[list[str], dict[str, str], list[str]]:
    """裸符号(无后缀)按 instruments 维表 code→symbol 补全 (#302)。

    返回 (规范化后的去重请求列表, {裸符号: 补全后符号}, 被跳过的裸符号)。
    上游对裸符号返回 200 空 payload, 本地静默写 0 行 — 补全失败时显式跳过并告警,
    不再带着已知无效的符号发请求。全部带后缀时不读维表, 行为与开销同旧版;
    带后缀符号原样透传, 由上游照常处理。
    """
    bare = [s for s in symbols if "." not in s]
    if not bare:
        return symbols, {}, []

    index = _instruments_symbol_index(data_dir)
    resolved: dict[str, str] = {}
    skipped: list[str] = []
    # 核心指数 ETF (SPY/QQQ/DIA/IWM) 自身即全符号: 显式入盘后管道池但不在普通股
    # 维表 (挂 NYSE Arca / ETF), 不按"维表无匹配"跳过 (9-29 审计: 否则市场门日K
    # 会在裸符号补全环节被静默丢弃)
    from app.services.index_const import CORE_INDEX_SYMBOLS
    core_index = set(CORE_INDEX_SYMBOLS)
    for s in bare:
        matches = (index or {}).get(s, [])
        if s in matches:
            # 美股普通股本身就是维表全符号; 同前缀还有优先股/分级股 (BAC.PRK、BRK.B) 时
            # 不能当作"多个交易所后缀"跳过, 否则 batch 永不拉它, 分区只剩实时快照 (10-11 开机反复重修)
            resolved[s] = s
        elif len(matches) == 1:
            resolved[s] = matches[0]
        elif s in core_index:
            resolved[s] = s
        else:
            skipped.append(s)

    if index is None:
        logger.warning(
            "日K同步: instruments 维表不可用, %d 个裸符号(无交易所后缀)将被跳过 — "
            "请先同步标的维表, 或改用维表中的完整符号", len(skipped))
    elif skipped:
        logger.warning(
            "日K同步: %d 个裸符号在维表中无唯一匹配(不支持或非股票), 已跳过: %s",
            len(skipped), skipped[:20])
    if resolved:
        logger.info("日K同步: 已按维表补全 %d 个裸符号后缀 (样例: %s)",
                    len(resolved), list(resolved.items())[:5])

    out: list[str] = []
    seen: set[str] = set()
    skipped_set = set(skipped)
    for s in symbols:
        if s in skipped_set:
            continue  # 无法补全的裸符号不发请求 (上游必然 200 空 payload)
        full = resolved.get(s, s)
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out, resolved, skipped


def sync_daily_batch(symbols: list[str],
                     count: int | None = None,
                     batch_size: int | None = None,
                     rpm: int | None = None,
                     start_time: datetime | None = None,
                     end_time: datetime | None = None,
                     on_chunk_done: Callable[[int, int], None] | None = None,
                     failed_out: list[str] | None = None) -> pl.DataFrame:
    """批量拉取多股日 K。

    优先使用 start_time / end_time 区间 + count=10000,确保覆盖完整时间段。
    仅传 count 时按条数回溯。

    failed_out: 可选出参。拉取失败的分块标的会追加进该 list, 供上层判定「部分失败」
                而非静默当成功(某分块断网 → 这些标的本轮未更新, 保持旧数据)。

    厂商内置源退役后 (P1-1b2) 本函数不再承担直连拉取——全部走
    sync_and_persist_daily_batch 内的 custom provider 路径; 保留签名仅为兼容,
    调用即视为"未配置数据源"并完整登记失败。
    """
    logger.warning("sync_daily_batch 被调用但厂商数据源已退役: %d 标本轮未更新", len(symbols))
    if failed_out is not None:
        failed_out.extend(symbols)
    if on_chunk_done:
        on_chunk_done(1, 1)
    return pl.DataFrame()


def sync_and_persist_daily_batch(
    symbols: list[str],
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
    zero_row_out: list[str] | None = None,
    written_dates_out: set | None = None,
) -> int:
    """批量同步日 K 并落到 Parquet。返回写入的行数。

    start_date/end_date: 外部传入的时间范围(由 pipeline 根据已有数据计算)。
    未传入时默认拉最近 1 年。
    zero_row_out: 可选出参。本轮实际入库 0 行的标的(含无法识别被跳过的裸符号、
                  上游 200 空 payload 的静默 0 行)按调用方原始写法追加进该 list,
                  供上层 fail-loud 展示, 不再"显示成功实则全空" (#302)。
    written_dates_out: 可选出参。本轮实际提交的日期分区 (ISO 字符串) 集合,
                  供盘后管道判定"当日是否已被官方 SIP 重建" (9-29 审计)。
    """
    seen: set[str] = set()
    failed_syms: list[str] = []
    skipped_bare: list[str] = []

    def _finalize(written: int) -> int:
        zero_full = [s for s in requested if s not in seen and s not in failed_syms]
        if zero_full or skipped_bare:
            zero = [orig_by_full.get(s, s) for s in zero_full] + skipped_bare
            logger.warning(
                "日K批量同步: %d/%d 标的本轮入库 0 行 (符号无法识别/停牌/窗口内无数据; 样例: %s)",
                len(zero), len(requested) + len(skipped_bare), zero[:10])
            if zero_row_out is not None:
                zero_row_out.extend(zero)
        return written

    original = list(symbols)
    symbols, resolved, skipped_bare = _normalize_bare_symbols(symbols, repo.store.data_dir)
    requested = symbols
    # 完整符号 → 调用方原始写法: 0 行回报用用户输入的形式, 裸符号也能对上
    orig_by_full: dict[str, str] = {}
    for orig in original:
        orig_by_full.setdefault(resolved.get(orig, orig), orig)

    if not symbols:
        return _finalize(0)

    provider_name = preferences.get_daily_data_provider()
    if provider_name:
        from app.data_providers import custom as custom_sources
        if custom_sources.provider_has_dataset(provider_name, "daily"):
            provider = custom_sources.get_provider(provider_name)
            end_time = end_date or datetime.now()
            days = count or 365
            start_time = start_date or (end_time - timedelta(days=days))
            iter_daily = getattr(provider, "iter_daily", None)
            if callable(iter_daily):

                def _tracking_chunks(chunks):
                    for df in chunks:
                        if not df.is_empty() and "symbol" in df.columns:
                            seen.update(df["symbol"].cast(pl.Utf8).unique().to_list())
                        yield df

                return _finalize(_persist_daily_chunks(
                    _tracking_chunks(iter_daily(
                        symbols,
                        start_time=start_time,
                        end_time=end_time,
                        on_chunk_done=on_chunk_done,
                    )),
                    repo,
                    written_dates_out,
                ))
            df = provider.get_daily(
                symbols,
                start_time=start_time,
                end_time=end_time,
                on_chunk_done=on_chunk_done,
            )
            if df.is_empty():
                return _finalize(0)
            if "symbol" in df.columns:
                seen.update(df["symbol"].cast(pl.Utf8).unique().to_list())
            if written_dates_out is not None and "date" in df.columns:
                written_dates_out.update(str(d) for d in df["date"].unique().to_list())
            repo.append_daily(df)
            try:
                d = repo.store.data_dir.as_posix()
                repo.db.execute(
                    f"""CREATE OR REPLACE VIEW kline_daily AS
                        SELECT * FROM read_parquet('{d}/kline_daily/**/*.parquet', union_by_name=true)"""
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("refresh view failed: %s", e)
            return _finalize(df.height)
        # 自定义源未配置 daily → 回退 TickFlow

    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return _finalize(0)

    limit = resolve_limit(capset, Cap.KLINE_DAILY_BATCH, default_batch=100)

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    df = sync_daily_batch(
        symbols, count=count, batch_size=limit.batch, rpm=limit.rpm,
        start_time=start_time, end_time=end_time,
        on_chunk_done=on_chunk_done,
        failed_out=failed_syms,
    )

    if not df.is_empty() and "symbol" in df.columns:
        seen.update(df["symbol"].cast(pl.Utf8).unique().to_list())

    if df.is_empty():
        return _finalize(0)

    if written_dates_out is not None and "date" in df.columns:
        written_dates_out.update(str(d) for d in df["date"].unique().to_list())
    repo.append_daily(df)

    try:
        d = repo.store.data_dir.as_posix()
        repo.db.execute(
            f"""CREATE OR REPLACE VIEW kline_daily AS
                SELECT * FROM read_parquet('{d}/kline_daily/**/*.parquet', union_by_name=true)"""
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh view failed: %s", e)

    return _finalize(df.height)


def _persist_daily_chunks(chunks, repo: KlineRepository, written_dates_out: set | None = None) -> int:
    """先把流式 provider 结果写入私有 staging,完整取数后再提交正式分区。

    写入前统一 OHLC/量额为 Float64 (_normalize_daily): 部分源对整价票返回 Int64,
    同日多 part 混型时 pl.scan_parquet().collect() 会 SchemaError (生产 2026-10-14)。
    """
    staging_base = repo.store.data_dir / ".daily_sync_staging"
    _sweep_stale_daily_staging(staging_base)
    root = staging_base / uuid.uuid4().hex
    written = 0
    try:
        for index, df in enumerate(chunks):
            if df.is_empty():
                continue
            df = _normalize_daily(df)
            if df.is_empty():
                continue
            for date_df in df.partition_by("date"):
                dt = date_df["date"][0]
                ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
                out = root / f"date={ds}" / f"part-{index}.parquet"
                out.parent.mkdir(parents=True, exist_ok=True)
                date_df.write_parquet(out)
                written += date_df.height

        for date_dir in sorted(root.glob("date=*")):
            files = sorted(date_dir.glob("*.parquet"))
            if files:
                # 再规范化一次: 防历史 staging / 旧 part 混型 (cast 幂等)
                parts = [_normalize_daily(pl.read_parquet(f)) for f in files]
                parts = [p for p in parts if not p.is_empty()]
                if parts:
                    repo.append_daily(pl.concat(parts, how="vertical_relaxed"))
                if written_dates_out is not None:
                    written_dates_out.add(date_dir.name[5:])
    finally:
        shutil.rmtree(root, ignore_errors=True)
        with contextlib.suppress(OSError):
            root.parent.rmdir()

    return written


def _sweep_stale_daily_staging(staging_base, max_age_s: int = 24 * 60 * 60) -> None:
    """清理崩溃遗留的旧同步目录,不碰仍可能活跃的新目录。"""
    if not staging_base.exists():
        return
    cutoff = time.time() - max_age_s
    for run_dir in staging_base.iterdir():
        try:
            if run_dir.is_dir() and run_dir.stat().st_mtime < cutoff:
                shutil.rmtree(run_dir)
        except OSError:
            logger.warning("failed to clean stale daily staging: %s", run_dir)


ADJ_BASIS_FILE = "basis.json"
ADJ_EVENT_BASIS = "event_v2"
_ADJ_EVENT_SCHEMA = {"symbol": pl.Utf8, "trade_date": pl.Date, "ex_factor": pl.Float64, "vol_factor": pl.Float64}


def adj_factor_basis(factor_dir) -> str | None:
    """因子表口径标记。None = 旧版本 (逐日水平比值, 不能与事件口径混用)。"""
    try:
        return json.loads((factor_dir / ADJ_BASIS_FILE).read_text(encoding="utf-8")).get("basis")
    except (OSError, ValueError, AttributeError):
        return None


def _normalize_adj_events(df: pl.DataFrame) -> pl.DataFrame:
    if "vol_factor" not in df.columns:
        df = df.with_columns(pl.lit(1.0).alias("vol_factor"))
    normalized = df.select(list(_ADJ_EVENT_SCHEMA)).cast(_ADJ_EVENT_SCHEMA, strict=False)
    invalid = normalized.filter(
        pl.any_horizontal(pl.all().is_null())
        | (~pl.col("ex_factor").is_finite()) | (pl.col("ex_factor") <= 0)
        | (~pl.col("vol_factor").is_finite()) | (pl.col("vol_factor") <= 0)
        | (pl.col("symbol").str.strip_chars() == "")
    )
    if invalid.height:
        raise ValueError("adjustment factor rows must have valid keys and finite positive factors")
    return normalized


def store_adj_factor_events(factor_dir, new_data: pl.DataFrame, *, replace: bool = False) -> tuple[int, list[str]]:
    """合并写入除权事件表, 返回 (新增/变化行数, 受影响 symbol)。

    旧口径 (无 basis 标记) 的行不参与合并: 它们是逐日水平值, 与事件口径混用会
    制造假复权。replace=True 或旧口径时整表以本次结果为准 (由完整历史重建调用)。
    """
    factor_dir.mkdir(parents=True, exist_ok=True)
    out = factor_dir / "all.parquet"
    incoming = _normalize_adj_events(new_data) if not new_data.is_empty() else pl.DataFrame(schema=_ADJ_EVENT_SCHEMA)
    legacy = adj_factor_basis(factor_dir) != ADJ_EVENT_BASIS
    if replace or legacy or not out.exists():
        existing = pl.DataFrame(schema=_ADJ_EVENT_SCHEMA)
        if legacy and out.exists() and not replace:
            logger.warning("adj_factor: 旧口径因子表 (逐日水平) 不与事件口径合并, 需完整历史重建")
    else:
        existing = _normalize_adj_events(pl.read_parquet(out))
    merged = (pl.concat([existing, incoming])
              .unique(subset=["symbol", "trade_date"], keep="last")
              .sort(["symbol", "trade_date"]))
    changed = merged.join(existing, on=["symbol", "trade_date", "ex_factor", "vol_factor"], how="anti")
    affected = sorted(set(changed["symbol"].to_list()))
    _atomic_write_parquet(merged, out)
    (factor_dir / ADJ_BASIS_FILE).write_text(json.dumps({"basis": ADJ_EVENT_BASIS}), encoding="utf-8")
    return changed.height, affected


REBUILD_PENDING_FILE = "rebuild_pending.json"


def mark_rebuild_pending(factor_dir, symbols: list[str] | None, reason: str) -> None:
    """因子已落盘、enriched 尚未按新因子重算: 先写待办, 崩溃后下一轮据此补做。

    symbols=None 表示全量重建 (口径迁移)。已有全量待办时不被局部待办覆盖。
    """
    factor_dir.mkdir(parents=True, exist_ok=True)
    path = factor_dir / REBUILD_PENDING_FILE
    current = rebuild_pending(factor_dir)
    if current is not None and current.get("symbols") is None:
        return
    if current is not None and symbols is not None:
        symbols = sorted(set(symbols) | set(current.get("symbols") or []))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"symbols": symbols, "reason": reason}), encoding="utf-8")
    tmp.replace(path)


def rebuild_pending(factor_dir) -> dict | None:
    try:
        return json.loads((factor_dir / REBUILD_PENDING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_rebuild_pending(factor_dir) -> None:
    try:
        (factor_dir / REBUILD_PENDING_FILE).unlink()
    except FileNotFoundError:
        pass


def sync_adj_factor(symbols: list[str], repo: KlineRepository,
                    capset: CapabilitySet,
                    start_time: datetime | None = None,
                    end_time: datetime | None = None,
                    on_chunk_done: Callable[[int, int], None] | None = None,
                    asset_type: str = "stock",
                    *, replace: bool = False,
                    anchor_dates: dict | None = None) -> tuple[int, list[str]]:
    """同步除权因子(Starter+)。SDK 接口:`tf.klines.ex_factors(symbols=...)`。

    支持增量: 传 start_time/end_time 只拉取该时间范围内的新除权事件。
    返回 (写入行数, 受影响的 symbol 列表) — 供 enriched 局部重算使用。
    """
    if not symbols:
        return 0, []

    provider_name = preferences.get_adj_factor_provider()
    if provider_name:
        from app.data_providers import custom as custom_sources
        if custom_sources.provider_has_dataset(provider_name, "adj_factor"):
            provider = custom_sources.get_provider(provider_name)
            extra: dict = {"anchor_dates": anchor_dates} if anchor_dates else {}
            new_data = provider.get_adj_factors(
                symbols, start_time=start_time, end_time=end_time,
                asset_type=asset_type, on_chunk_done=on_chunk_done, **extra,
            )
            factor_dir = repo.store.data_dir / ("adj_factor_etf" if asset_type == "etf" else "adj_factor")
            if new_data.is_empty() and not replace:
                # 成功的空结果 = 窗口内无除权; 源失败已在 provider 抛出, 不会走到这里
                return 0, []
            return store_adj_factor_events(factor_dir, new_data, replace=replace)
        # 自定义源未配置 adj_factor → 无可用复权源 (厂商腿已退役)

    return 0, []


CANONICAL_MINUTE_COLS = [
    "symbol", "datetime", "open", "high", "low", "close", "volume", "amount",
]


def _enforce_minute_market_wallclock(df: pl.DataFrame, *, source: str) -> pl.DataFrame:
    """显式时区转换；naive 按 ET 契约直通，绝不根据小时分布猜 UTC。

    盘后 17:00 ET 与 UTC 墙钟无法靠数值区分。猜测平移会制造盘中成交，
    覆写真实 OHLCV。UTC 源必须提供 tz-aware 字段或 epoch，由映射层转换。
    非常规时段交给下一层过滤，不改写成交时间。
    """
    if df.is_empty() or "datetime" not in df.columns:
        return df
    dtype = df.schema["datetime"]
    if dtype == pl.Utf8:
        df = df.with_columns(pl.col("datetime").str.to_datetime(strict=False))
    elif not isinstance(dtype, pl.Datetime):
        df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
    dtype = df.schema["datetime"]
    if isinstance(dtype, pl.Datetime) and dtype.time_zone is not None:
        df = df.with_columns(pl.col("datetime").dt.convert_time_zone(str(us_clock.tz)).dt.replace_time_zone(None))
    return df.with_columns(pl.col("datetime").cast(pl.Datetime("us")))


def _filter_regular_session(df: pl.DataFrame) -> pl.DataFrame:
    """保留美东常规交易时段的分钟根 [09:30, 16:00)。

    Alpaca 及部分自定义源会同时返回盘前/盘后根；这些根不属于本地分钟
    K 契约，必须在所有下游入口统一剔除。16:00 是右边界，不是第 391 根。
    """
    if df.is_empty() or "datetime" not in df.columns:
        return df
    open_minute = US_SESSION_OPEN.hour * 60 + US_SESSION_OPEN.minute
    close_minute = US_SESSION_CLOSE.hour * 60 + US_SESSION_CLOSE.minute
    minute_of_day = (
        pl.col("datetime").dt.hour().cast(pl.Int32) * 60
        + pl.col("datetime").dt.minute().cast(pl.Int32)
    )
    # 审计 L-1: 半日市 (13:00 收盘) 13:00 后的根是盘后, 按当日日历收盘剔除
    early = _early_close_minutes(df["datetime"].dt.date().unique().to_list())
    if early:
        close_expr = pl.col("datetime").dt.date().replace_strict(
            list(early), list(early.values()), default=close_minute, return_dtype=pl.Int32)
    else:
        close_expr = pl.lit(close_minute)
    return df.filter((minute_of_day >= open_minute) & (minute_of_day < close_expr))


def _early_close_minutes(days) -> dict:
    """{日期: 收盘分钟数} 只含半日市 (日历早收盘)。"""
    out = {}
    for d in days:
        if d is None:
            continue
        c = us_clock.session_close(d)
        if c < US_SESSION_CLOSE:
            out[d] = c.hour * 60 + c.minute
    return out


# 兼容别名: 旧名逐步退役 (P1-2 收编完成后删除)
_enforce_minute_beijing_wallclock = _enforce_minute_market_wallclock



def _normalize_minute(df_in, default_symbol: str | None = None) -> pl.DataFrame:
    """把上游返回的分钟 K 数据规范成 canonical 列 (datetime 收口为美东墙钟)。"""
    if df_in is None or len(df_in) == 0:
        return pl.DataFrame()

    if not isinstance(df_in, pl.DataFrame):
        df = pl.from_pandas(df_in.reset_index() if hasattr(df_in, "reset_index") else df_in)
    else:
        df = df_in

    rename_map = {
        "ts_code": "symbol",
        "vol": "volume",
        "amt": "amount",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})

    # datetime 列:优先用 timestamp(毫秒精度),其次 trade_time
    if "timestamp" in df.columns:
        # 毫秒时间戳为 UTC 基准; 契约要求美东墙钟 naive (P1-2)
        df = df.with_columns(
            pl.from_epoch(pl.col("timestamp").cast(pl.Int64), time_unit="ms")
            .dt.replace_time_zone("UTC")
            .dt.convert_time_zone("America/New_York")
            .dt.replace_time_zone(None)
            .alias("datetime")
        ).drop("timestamp")
        for drop_col in ("trade_time", "trade_date"):
            if drop_col in df.columns:
                df = df.drop(drop_col)
    elif "trade_time" in df.columns:
        df = df.rename({"trade_time": "datetime"})
        if "trade_date" in df.columns:
            df = df.drop("trade_date")
    elif "trade_date" in df.columns:
        df = df.rename({"trade_date": "datetime"})

    if "datetime" in df.columns:
        # 时区契约守卫: 须在下方通用 us-cast 之前, 避免带时区列被静默剥成 UTC-naive
        df = _enforce_minute_market_wallclock(df, source="legacy")
        df = _filter_regular_session(df)

    if "symbol" not in df.columns and default_symbol is not None:
        df = df.with_columns(pl.lit(default_symbol).alias("symbol"))

    # 类型规范:统一转 Datetime('us')
    if "datetime" in df.columns:
        dt_type = df.schema["datetime"]
        if not isinstance(dt_type, pl.Datetime) or dt_type.time_unit != "us":
            df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))

    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    for col in ("volume", "amount"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))

    keep = [c for c in CANONICAL_MINUTE_COLS if c in df.columns]
    return df.select(keep)


def _timestamp_to_market_datetime(col: pl.Expr) -> pl.Expr:
    """timestamp (UTC 毫秒) → 美东墙钟 naive datetime 表达式 (P1-2b)。"""
    return (
        pl.from_epoch(col.cast(pl.Int64), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .dt.convert_time_zone("America/New_York")
        .dt.replace_time_zone(None)
    )


# 兼容别名 (旧名逐步退役)
_timestamp_to_beijing_datetime = _timestamp_to_market_datetime


def _datetime_to_ms(dt: datetime) -> int:
    """datetime → 毫秒时间戳 (供 SDK start_time / end_time 使用)。"""
    return int(dt.timestamp() * 1000)


def _write_minute_partition(df: pl.DataFrame, minute_dir) -> int:
    """按 _trade_date 分区落盘分钟 K (读旧→concat→unique→原子写)。返回写入行数。

    抽自原 sync_and_persist_minute 末尾的循环, 供流式落盘 (每段一次) 与一次性迁移共用。
    """
    if df.is_empty():
        return 0
    df = _filter_regular_session(df)
    if df.is_empty():
        return 0
    df = df.with_columns(pl.col("datetime").dt.date().alias("_trade_date"))
    written = 0
    for day_df in df.partition_by("_trade_date"):
        trade_date = day_df["_trade_date"][0]
        out = minute_dir / f"date={trade_date}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            existing = _filter_regular_session(pl.read_parquet(out))
            if "datetime" in existing.columns:
                existing = existing.filter(pl.col("datetime").is_not_null())
            new_rows = day_df.drop("_trade_date")
            # 只对本次触及的 symbol 合并去重, 未触及行原样保留: 单股补齐时避免
            # 为合并几行数据把全市场分区整体 unique 的写放大 (issue #305)。
            touched = new_rows["symbol"].unique().to_list()
            same = existing.filter(pl.col("symbol").is_in(touched))
            other = existing.filter(~pl.col("symbol").is_in(touched))
            merged = pl.concat([same, new_rows]).unique(
                subset=["symbol", "datetime"], keep="last",
            )
            day_df = pl.concat([other, merged])
        else:
            day_df = day_df.drop("_trade_date")
        day_df = day_df.sort("symbol", "datetime")
        _atomic_write_parquet(day_df, out)
        written += day_df.height
    return written


def _resolve_minute_provider(
    provider_name: str,
) -> tuple[object | None, bool, str | None]:
    """统一解析 custom minute provider, 把所有 resolver 调用纳入同一异常边界。

    供 _try_custom_minute 和 sync_and_persist_minute 共用, 避免两处分别调
    provider_has_dataset / get_provider 时漏掉异常边界 (Issue 2 加固项)。

    返回 (provider, should_fallback, error_msg):
      - provider_name 为空或未配 minute dataset → (None, True, None)  静默降级
      - resolver 异常 (registry 损坏 / 插件失效 / provider name 不存在) → (None, True, str(e))
      - 成功 → (provider, False, None)

    上层依据 error_msg 决定是否 logger.warning (区分"未配"与"异常")。
    注意: provider.get_minute() 仍由调用方在自身 try 块内调用 (业务异常, 非解析异常)。
    """
    if not provider_name:
        return (None, True, None)
    from app.data_providers import custom as custom_sources
    try:
        if not custom_sources.provider_has_dataset(provider_name, "minute"):
            return (None, True, None)
        provider = custom_sources.get_provider(provider_name)
        return (provider, False, None)
    except Exception as e:  # noqa: BLE001
        return (None, True, str(e))


def _try_custom_minute(
    symbols: list[str],
    start_time: datetime | None,
    end_time: datetime | None,
    asset_type: AssetType,
    freq: str = "1m",
    on_chunk_done: Callable[[int, int, str], None] | None = None,
) -> tuple[pl.DataFrame | None, bool]:
    """尝试从自定义分钟源拉取。返回 (df, should_fallback)。

    返回契约:
      (None, True)   → 未配自定义源 / 未配 minute dataset / 自定义源异常 → 无可用数据
      (df, False)    → 自定义源成功(含空 df) → 直接用, 不回退

    resolver 异常边界由 _resolve_minute_provider 统一兜底; 业务调用
    (provider.get_minute) 仍在本函数 try 块内, 与 resolver 异常分离
    便于日志区分 ("resolution failed" vs "call failed")。

    on_chunk_done 适配: 上层回调是 3 参 (cur, total, seg_label), provider
    实现内部以 2 参 (cur, total) 调用。这里包装一层, provider 调 2 参时补
    默认 seg_label="custom" 转发给上层, 保证进度展示不降级。
    """
    provider_name = preferences.get_minute_data_provider()
    provider, fallback, err = _resolve_minute_provider(provider_name)
    if fallback:
        if err is not None:
            logger.warning("custom minute provider %s resolution failed, no usable minute data: %s",
                           provider_name, err)
        return (None, True)

    cache_key = (id(provider), tuple(symbols), start_time, end_time, asset_type, freq)
    cacheable = len(symbols) <= 32 and on_chunk_done is None
    if cacheable:
        with _MINUTE_QUERY_CACHE_LOCK:
            cached = _MINUTE_QUERY_CACHE.get(cache_key)
            if cached and cached[0] > time.monotonic() and cached[1] is provider:
                return cached[2].clone(), False

    # 包装 on_chunk_done: provider 调 2 参 → 补 seg_label="custom" → 转发上层 3 参
    wrapped_cb: Callable[[int, int], None] | None = None
    if on_chunk_done is not None:
        def _wrapped_cb(cur: int, total: int) -> None:
            on_chunk_done(cur, total, "custom")
        wrapped_cb = _wrapped_cb

    try:
        df = provider.get_minute(
            symbols, start_time=start_time, end_time=end_time,
            asset_type=asset_type, freq=freq, on_chunk_done=wrapped_cb,
        )
    except Exception as e:
        logger.warning("custom minute provider %s call failed, no usable minute data: %s",
                       provider_name, e)
        return (None, True)
    try:
        # 时区契约守卫: 插件/自定义源帧同样收口为美东墙钟 (CONTRIBUTING §3.3)
        df = _enforce_minute_market_wallclock(df, source=provider_name)
        df = _filter_regular_session(df)
    except Exception as e:
        logger.warning("custom minute provider %s datetime 契约校验失败, no usable minute data: %s",
                       provider_name, e)
        return (None, True)
    if cacheable:
        with _MINUTE_QUERY_CACHE_LOCK:
            if len(_MINUTE_QUERY_CACHE) >= 32:
                _MINUTE_QUERY_CACHE.pop(next(iter(_MINUTE_QUERY_CACHE)))
            _MINUTE_QUERY_CACHE[cache_key] = (time.monotonic() + _MINUTE_QUERY_CACHE_TTL, provider, df.clone())
    return (df, False)


def sync_minute_batch(
    symbols: list[str],
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    count: int | None = None,
    batch_size: int | None = None,
    rpm: int | None = None,
    on_chunk_done: Callable[[int, int, str] | None] | None = None,
    segment_trading_days: int = 20,
    on_segment: Callable[[pl.DataFrame], None] | None = None,
    asset_type: AssetType = "stock",
    raw_basis: bool = False,
) -> pl.DataFrame:
    """批量拉取多股分钟 K。

    优先使用 start_time / end_time 区间, 确保所有标的覆盖同一时间段。
    count 仅作为 fallback 保留。
    on_chunk_done(current, total) 每个 chunk 完成后回调。

    segment_trading_days: 单段大小 (交易日), 控制每次 SDK 请求覆盖的天数。
        TickFlow count 上限 10000 根/股, 1 天 240 根 → 物理上限 ~41 交易日;
        默认 20 (4800 根, 安全余量足), 范围建议 [5, 30]。
        段越小: 单次内存峰值越低 (适合小内存机器), 但总批数↑ → 限速 sleep↑ → 更慢。
        段越大: 速度越快, 内存峰值越高。
    on_segment: 每个时间段拉完后回调 (传入该段拼接后的 DataFrame)。
        传入时走「流式落盘」: 段内结果累积到 seg_out, 段末 concat 后回调并清空,
        不进入全局 out → 内存峰值从「全量」降到「单段」。适用于 sync_and_persist_minute。
        不传时 (如 get_minute_batch 的实时补拉) 保持原契约: 累积进 out 末尾一次性返回。
    """
    df, fallback = _try_custom_minute(
        symbols, start_time=start_time, end_time=end_time,
        asset_type=asset_type, freq="1m", on_chunk_done=on_chunk_done,
    )
    if not fallback:
        # 自定义源成功: 遵守与 TickFlow 路径一致的 on_segment 契约。
        # 传了 on_segment (如 sync_and_persist_minute 流式落盘) → 调 on_segment, 返回空 df;
        # 未传 on_segment (如 fetch_minute_single 实时补拉) 或空 df → 原样返回 df。
        df = df if df is not None else pl.DataFrame()
        if on_segment and not df.is_empty():
            # 空 df 不调 on_segment, 与 TickFlow 路径 `if seg_out:` (L684) 对称
            on_segment(df)
            return pl.DataFrame()
        return df

    # 厂商分钟源已退役 (P1-1b2): 自定义源失败即本轮无数据
    return pl.DataFrame()



def intraday_monitor_support(capset: CapabilitySet | None) -> dict[str, object]:
    """返回分时信号监控可用的数据能力和单轮标的上限。"""
    provider_name = preferences.get_minute_data_provider()
    _, fallback, error = _resolve_minute_provider(provider_name)
    if not fallback:
        return {
            "available": True, "source": "custom_minute", "max_symbols": 100,
            "reason": "使用已配置的分钟数据插件",
        }
    if error is not None:
        logger.warning("minute provider resolution failed while checking monitor support: %s", error)
    if capset is None:
        return {
            "available": False, "source": None, "max_symbols": 0,
            "reason": "需要分钟 K 或日内分时数据权限",
        }
    for cap, source in (
        (Cap.INTRADAY_BATCH, "intraday_batch"),
        (Cap.KLINE_MINUTE_BATCH, "minute_batch"),
    ):
        if capset.has(cap):
            limits = capset.limits(cap)
            return {
                "available": True, "source": source,
                "max_symbols": max(1, int(limits.batch or 100)) if limits else 100,
                "reason": "日内分时数据可用" if cap == Cap.INTRADAY_BATCH else "分钟 K 数据可用",
            }
    for cap, source in (
        (Cap.INTRADAY, "intraday_single"),
        (Cap.KLINE_MINUTE_BY_SYMBOL, "minute_single"),
    ):
        if capset.has(cap):
            return {
                "available": True, "source": source, "max_symbols": 1,
                "reason": "当前权限仅支持单标的分时监控",
            }
    return {
        "available": False, "source": None, "max_symbols": 0,
        "reason": "需要分钟 K 或日内分时数据权限",
    }


def fetch_intraday_monitor_batch(
    symbols: list[str],
    capset: CapabilitySet | None,
    source: str | None = None,
    *,
    now: datetime | None = None,
) -> pl.DataFrame:
    """盘中监控分时拉取。厂商日内接口已退役 (P1-1b2): 返回空 df,
    监控链路按「本轮无数据」降级; 自定义分钟源走 sync_minute_batch 路径。"""
    return pl.DataFrame()



def fetch_intraday_full_market_burst(
    symbols: list[str],
    capset: CapabilitySet | None,
    *,
    count: int = 300,
) -> tuple[pl.DataFrame, int]:
    """全市场当日分钟脉冲拉取。厂商日内接口已退役 (P1-1b2): 返回空。"""
    return (pl.DataFrame(), 0)



def fetch_intraday_universe_increment(
    universe: str,
    repo: KlineRepository,
    capset: CapabilitySet | None,
    *,
    count: int = 300,
) -> tuple[pl.DataFrame, int]:
    """universe 增量分钟拉取。厂商接口已退役 (P1-1b2): 返回空。"""
    return (pl.DataFrame(), 0)



def _resolve_full_minute_provider(
    provider_name: str,
) -> tuple[object | None, bool, str | None]:
    """解析全量分钟生效的自定义源。返回 (provider, should_fallback, error_msg):

    - provider_name 为空 / 未配 full_minute dataset → (None, True, None)
    - resolver 异常 (registry 损坏 / 插件失效 / 源不存在) → (None, True, str(e))
    - 成功 → (provider, False, None)

    与 _resolve_minute_provider 同构, 仅数据集名不同。
    """
    if not provider_name:
        return (None, True, None)
    from app.data_providers import custom as custom_sources
    try:
        if not custom_sources.provider_has_dataset(provider_name, "full_minute"):
            return (None, True, None)
        provider = custom_sources.get_provider(provider_name)
        return (provider, False, None)
    except Exception as e:  # noqa: BLE001
        return (None, True, str(e))


def fetch_intraday_custom_batch(
    provider: object,
    provider_name: str,
    symbols: list[str],
) -> tuple[pl.DataFrame, int]:
    """自定义源全量分钟修复轮: 当日窗口全市场批量拉取 (不落盘)。

    provider 契约 (见 docs/plugin-development.md):
    - get_intraday_batch(symbols, count, asset_type) 优先 — 源自管批量端点;
    - 未实现则回退 get_minute(symbols, 当日窗口) — 复用逐标的分钟K机制,
      请求数按 chunk 回调统计。
    帧统一过美东墙钟守卫 (与 _try_custom_minute 同纪律)。
    返回 (当日分钟K, 请求数); 失败返回空 df 由调用方按空轮处理。
    """
    try:
        method = getattr(provider, "get_intraday_batch", None)
        if callable(method):
            df = method(symbols)
            counter = getattr(provider, "minute_request_count", None)
            requests = counter if isinstance(counter, int) else 1
        else:
            counted = {"requests": 0}

            def _count_requests(cur: int, total: int) -> None:
                counted["requests"] = max(counted["requests"], int(total))

            end = us_clock.now()
            start = end.replace(hour=0, minute=0, second=0, microsecond=0)
            df = provider.get_minute(
                symbols, start_time=start, end_time=end,
                asset_type="stock", freq="1m", on_chunk_done=_count_requests,
            )
            requests = counted["requests"]
    except Exception as e:  # noqa: BLE001
        logger.warning("custom full_minute batch via %s failed: %s", provider_name, e)
        return (pl.DataFrame(), 0)
    try:
        df = _enforce_minute_market_wallclock(df, source=provider_name)
        df = _filter_regular_session(df)
    except Exception as e:  # noqa: BLE001
        logger.warning("custom full_minute datetime 契约校验失败 (%s): %s", provider_name, e)
        return (pl.DataFrame(), 0)
    return (df, max(requests, 1))


def fetch_intraday_custom_latest(
    provider: object,
    provider_name: str,
    *,
    count: int = 3,
) -> tuple[pl.DataFrame, int] | None:
    """自定义源全量分钟稳态增量轮 (不落盘)。

    provider 可选实现 get_intraday_latest(symbols=None, count) → 每只标的最新
    count 根分钟K, 尽量单请求/低请求量 (TickFlow 的 intraday.universe 同义)。
    未实现返回 None — 调用方降级为仅修复轮模式。失败返回 (空 df, 0) 按空轮处理。
    """
    method = getattr(provider, "get_intraday_latest", None)
    if not callable(method):
        return None
    try:
        df = method(count=count)
    except Exception as e:  # noqa: BLE001
        logger.warning("custom full_minute latest via %s failed: %s", provider_name, e)
        return (pl.DataFrame(), 0)
    try:
        df = _enforce_minute_market_wallclock(df, source=provider_name)
        df = _filter_regular_session(df)
    except Exception as e:  # noqa: BLE001
        logger.warning("custom full_minute datetime 契约校验失败 (%s): %s", provider_name, e)
        return (pl.DataFrame(), 0)
    return (df, 1)


def minute_delay_minutes(provider=None) -> int:
    """生效源声明的历史分钟延迟；没有声明即零，不猜测其它源订阅。"""
    if provider is None:
        provider, _, _ = _resolve_minute_provider(preferences.get_minute_data_provider())
    value = getattr(provider, "minute_delay_minutes", 0)
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def minute_expected_count(trade_date: date, now: datetime, delay_minutes: int = 0) -> int:
    """可获得的完整分钟根数；时段取 MarketClock，不把延迟当缺口。"""
    local = now.astimezone(us_clock.tz) if now.tzinfo else now.replace(tzinfo=us_clock.tz)
    start = datetime.combine(trade_date, US_SESSION_OPEN, tzinfo=us_clock.tz)
    cutoff = local - timedelta(minutes=delay_minutes)
    close = us_clock.session_close(trade_date)          # 审计 L-1: 半日市 210 根
    day_minutes = (close.hour * 60 + close.minute) - (US_SESSION_OPEN.hour * 60 + US_SESSION_OPEN.minute)
    return max(0, min(day_minutes, int((cutoff - start).total_seconds() // 60)))


def minute_coverage_complete(df: pl.DataFrame, expected: int, trade_date: date) -> bool:
    """按分钟标签覆盖判定，不允许重复根/盘前根/90%阈值冒充完整。"""
    if df.is_empty() or "datetime" not in df.columns:
        return False
    start = datetime.combine(trade_date, US_SESSION_OPEN)
    times = set(df["datetime"].to_list())
    return all(start + timedelta(minutes=i) in times for i in range(expected))


def fetch_intraday_custom_since(provider, provider_name: str, symbols: list[str], start: datetime):
    """按服务水位取增量；失败返回空且不前移水位。"""
    try:
        df = provider.get_intraday_since(symbols, start)
        df = _filter_regular_session(_enforce_minute_market_wallclock(df, source=provider_name))
        counter = getattr(provider, "minute_request_count", None)
        return df, counter if isinstance(counter, int) else 1
    except Exception as e:
        logger.warning("custom full_minute increment via %s failed: %s", provider_name, e)
        return pl.DataFrame(), 0


def fetch_minute_single(
    symbol: str,
    trade_date: date,
    asset_type: AssetType = "stock",
    *,
    capset: CapabilitySet,
    raw_basis: bool = False,
) -> pl.DataFrame:
    """拉取单股单日常规时段分钟；源端延迟照实保留，不写本地。"""
    from datetime import datetime
    # 分钟墙钟是美东时间，取完整常规交易时段 [09:30, 16:00)。
    # 两端带 ET 时区，传到 Alpaca/自定义源时不会被宿主机时区重解释。
    start_time = datetime(
        trade_date.year, trade_date.month, trade_date.day,
        US_SESSION_OPEN.hour, US_SESSION_OPEN.minute, tzinfo=us_clock.tz,
    )
    end_time = datetime(
        trade_date.year, trade_date.month, trade_date.day,
        US_SESSION_CLOSE.hour, US_SESSION_CLOSE.minute, tzinfo=us_clock.tz,
    )

    # 自定义数据源分流: 与 sync_minute_batch 一致, 配了自定义分钟源时走 custom provider,
    # 避免无 TickFlow Pro+ 权限的用户分时图首次打开(本地无数据)时补拉失败返回空。
    df, fallback = _try_custom_minute(
        [symbol], start_time=start_time, end_time=end_time,
        asset_type=asset_type, freq="1m",
    )
    if not fallback:
        # 见 sync_minute_batch 同分支注释: df 在此必非 None。
        return df if df is not None else pl.DataFrame()

    # 厂商分钟源已退役 (P1-1b2): 自定义源未命中即空
    return pl.DataFrame()



def fetch_adj_factor_single(symbol: str) -> pl.DataFrame:
    """实时拉取单股除权因子。厂商接口已退役 (P1-1b2): 返回空 (按无除权处理)。"""
    return pl.DataFrame()



def _as_market_time(d: datetime) -> datetime:
    """落盘的分钟 datetime 是美东墙钟 naive, 带上美东时区再交给取数窗口。

    naive 值经 _datetime_to_ms 会被 .timestamp() 按服务器本地时区解释, 与同
    窗口另一端的服务器本地时间混用后整体错位 (UTC 容器上错 8 小时)。
    """
    return d if d.tzinfo is not None else d.replace(tzinfo=us_clock.tz)


def _latest_minute_datetime(repo: KlineRepository) -> datetime | None:
    """本地分钟 K 数据的最新时间 (美东时区)。"""
    try:
        res = repo.execute_one("SELECT max(datetime) FROM kline_minute")
        if res and res[0]:
            d = res[0]
            if isinstance(d, datetime):
                return _as_market_time(d)
            return _as_market_time(datetime.fromisoformat(str(d)))
    except Exception:  # noqa: BLE001
        pass
    return None


def _earliest_minute_datetime(repo: KlineRepository) -> datetime | None:
    """本地分钟 K 数据的最早时间 (美东时区, 用于向前扩展的起点)。"""
    try:
        res = repo.execute_one("SELECT min(datetime) FROM kline_minute")
        if res and res[0]:
            d = res[0]
            if isinstance(d, datetime):
                return _as_market_time(d)
            return _as_market_time(datetime.fromisoformat(str(d)))
    except Exception:  # noqa: BLE001
        pass
    return None


def _cleanup_null_datetime_minute(repo: KlineRepository) -> None:
    """检测并清除 datetime 全为 null 的旧版分钟 K 数据(迁移用)。"""
    minute_dir = repo.store.data_dir / "kline_minute"
    if not minute_dir.exists():
        return
    try:
        row = repo.execute_one(
            "SELECT count(*) AS total, count(datetime) AS non_null FROM kline_minute"
        )
        if row and row[0] > 0 and (row[1] is None or row[1] == 0):
            # 全部 datetime 为 null — 清除所有分钟 K parquet
            n = 0
            for f in minute_dir.rglob("*.parquet"):
                f.unlink()
                n += 1
            logger.info("cleaned %d corrupted minute-K parquet files (null datetime)", n)
    except Exception as e:  # noqa: BLE001
        logger.debug("minute cleanup check failed: %s", e)


def _migrate_symbol_to_date_partition(repo: KlineRepository) -> None:
    """将旧版 symbol= 分区迁移为 date= 分区。迁移完成后删除旧目录。"""
    minute_dir = repo.store.data_dir / "kline_minute"
    if not minute_dir.exists():
        return

    old_dirs = [d for d in minute_dir.iterdir() if d.is_dir() and d.name.startswith("symbol=")]
    if not old_dirs:
        return

    logger.info("migrating %d symbol-partitioned minute-K dirs to date partition…", len(old_dirs))

    all_frames: list[pl.DataFrame] = []
    for sym_dir in old_dirs:
        for pq in sym_dir.glob("*.parquet"):
            try:
                df = pl.read_parquet(pq)
                if "datetime" in df.columns:
                    df = df.filter(pl.col("datetime").is_not_null())
                if not df.is_empty():
                    all_frames.append(df)
            except Exception:  # noqa: BLE001
                pass

    if not all_frames:
        # 数据全部不可用，直接删旧目录
        for d in old_dirs:
            d.mkdir(parents=True, exist_ok=True)
            for f in d.rglob("*"):
                if f.is_file():
                    f.unlink()
            d.rmdir()
        return

    combined = pl.concat(all_frames, how="diagonal_relaxed")
    combined = combined.unique(subset=["symbol", "datetime"], keep="last")

    # 按日期写新分区
    combined = combined.with_columns(pl.col("datetime").dt.date().alias("_trade_date"))
    for day_df in combined.partition_by("_trade_date"):
        trade_date = day_df["_trade_date"][0]
        out = minute_dir / f"date={trade_date}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        day_df = day_df.drop("_trade_date").sort("symbol", "datetime")
        _atomic_write_parquet(day_df, out)

    # 删旧目录
    for d in old_dirs:
        for f in d.rglob("*"):
            if f.is_file():
                f.unlink()
        # 移除空目录
        try:
            d.rmdir()
        except OSError:
            pass

    logger.info("minute-K migration done: %d rows migrated", combined.height)


def sync_and_persist_minute(
    symbols: list[str],
    repo: KlineRepository,
    capset: CapabilitySet,
    days: int = 5,
    on_chunk_done: Callable[[int, int, str], None] | None = None,
    extend_backward: bool = False,
    force_full_days: bool = False,
) -> int:
    """同步分钟 K 并存到 Parquet。返回写入行数。

    存储口径由基准标记决定 (services/minute_adjust): 存量未迁移 → SDK adjust=qfq
    前复权 (旧行为); 已迁移 → adjust='none' 原始价落盘, 复权读取时投影。
    使用 start_time / end_time 区间拉取, 确保所有标的覆盖同一时间段。
    on_chunk_done(current, total) 每个 chunk 完成后回调。
    force_full_days=True 时强制回溯 days 自然日 (不增量补, 用于个股补齐历史)。
    """
    minute_provider = preferences.get_minute_data_provider()
    # resolver 调用统一走 _resolve_minute_provider, 与 _try_custom_minute 共用异常边界。
    # resolver 异常时视为非 custom (minute_is_custom=False), 走 capset 检查 →
    # sync_minute_batch 内 _try_custom_minute 会再次 resolver 异常 → fallback TickFlow。
    _, fallback, resolve_err = _resolve_minute_provider(minute_provider)
    minute_is_custom = not fallback
    if resolve_err is not None:
        logger.warning("custom minute provider %s resolution failed at sync_and_persist_minute, treating as non-custom: %s",
                       minute_provider, resolve_err)
    if not symbols:
        return 0
    if not minute_is_custom and not capset.has(Cap.KLINE_MINUTE_BATCH):
        return 0

    # 迁移:旧版 _normalize_minute 未转换 timestamp→datetime,导致全部 datetime 为 null
    # 检测到后直接清除(这些数据无法使用)
    _cleanup_null_datetime_minute(repo)

    # 迁移:旧版按 symbol= 分区转为 date= 分区
    _migrate_symbol_to_date_partition(repo)

    # 窗口两端统一为美东时区: 起止点会与本地分钟 K 的美东墙钟混用, 用服务器
    # 本地时间会让窗口整体错位 (UTC 容器上起点晚于终点, 增量补拉一个请求都发不出)。
    now = us_clock.now()

    if extend_backward:
        # 向前扩展模式: 从本地最早数据往前补, 叠加已有数据避免缺口。
        earliest_dt = _earliest_minute_datetime(repo)
        # 按交易日换算自然日 (7/5 系数)。>41 交易日时 +10 天余量覆盖节假日。
        # (分段由 sync_minute_batch 的 segment_trading_days 控制, 与此处的区间天数独立。)
        calendar_days = int(days * 7 / 5) + (10 if days > 41 else 0)
        if earliest_dt:
            end_time = earliest_dt
            start_time = end_time - timedelta(days=calendar_days)
        else:
            # 本地无数据 → 从今天往前拉
            start_time = now - timedelta(days=calendar_days)
            end_time = now
    else:
        # 默认增量模式: 首次拉取回溯 N 天, 已有数据则从最新时间增量补到今天
        # force_full_days=True: 强制回溯 days 自然日 (个股补齐历史, 不增量)
        last_dt = _latest_minute_datetime(repo)
        if force_full_days:
            # 按交易日换算自然日 (7/5 系数), 确保覆盖足够交易日
            calendar_days = int(days * 7 / 5) + 5
            start_time = now - timedelta(days=calendar_days)
        elif last_dt:
            # 盘后补齐必须回到最后分区的开盘：某只股票最新，不代表全池完整。
            start_time = datetime.combine(last_dt.date(), US_SESSION_OPEN, tzinfo=us_clock.tz)
        else:
            start_time = now - timedelta(days=days)
        end_time = now

    limit = resolve_limit(
        capset,
        Cap.KLINE_MINUTE_BATCH,
        default_batch=100,
        default_rpm=30,
        default_rpm_when_unset=False,
    )

    # 流式落盘: 每段拉完立即写盘, 内存峰值 = 单段 (而非全量)。
    # 全量攒内存曾导致 1 年全市场分钟 K OOM 卡死 (3 亿行 / 数十 GB)。
    minute_dir = repo.store.data_dir / "kline_minute"
    written_box = [0]  # list 闭包, 绕过 Python 闭包外层赋值

    def _persist(seg_df: pl.DataFrame) -> None:
        # 单股自动补齐可能与另一个补齐请求同时写同一日期分区。Windows 不允许
        # 替换仍被另一写入占用的临时文件,因此读-改-写必须复用仓库写锁。
        with repo._write_lock:
            written_box[0] += _write_minute_partition(seg_df, minute_dir)

    segment_days = preferences.get_minute_sync_segment_days()
    sync_minute_batch(
        symbols, start_time=start_time, end_time=end_time,
        batch_size=limit.batch, rpm=limit.rpm,
        on_chunk_done=on_chunk_done,
        segment_trading_days=segment_days,
        on_segment=_persist,
        asset_type="stock",
        raw_basis=minute_adjust.minute_basis_is_raw(repo.store.data_dir),
    )

    if written_box[0] == 0:
        return 0
    written = written_box[0]

    # 刷新视图
    try:
        d = repo.store.data_dir.as_posix()
        repo.db.execute(
            f"""CREATE OR REPLACE VIEW kline_minute AS
                SELECT * FROM read_parquet('{d}/kline_minute/**/*.parquet', union_by_name=true)"""
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh kline_minute view failed: %s", e)

    logger.info("minute K synced: %d rows (%d symbols)", written, len(symbols))
    return written
