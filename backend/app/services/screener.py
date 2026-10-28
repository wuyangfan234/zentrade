"""Screener 服务(§6.3)。

性能优化:
  - enriched parquet 仅存 14 列基础数据, 指标和信号即时计算
  - preset 策略: 从内存缓存或即时计算获取完整指标, ~10-50ms
  - custom SQL: DuckDB (用户传 SQL WHERE 字符串), ~10-50ms
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.parquet import scan_enriched_parquet
from app.storage.repository import KlineRepository

logger = logging.getLogger(__name__)

# 常用指标 (MA5/10/20、BOLL(20)、量比等) 的近似最小暖机窗口。
# 用于没有精确回看需求的场景 (自定义 SQL 选股、盘后管道) 的数据充足性提示 (#303);
# 策略运行用 engine.required_history_bars 的精确值。
MIN_INDICATOR_WARMUP_DAYS = 30


def regular_session_hours(minute: pl.DataFrame) -> pl.DataFrame:
    """把分钟 K 收成美东 9:30 对齐的常规时段小时线。

    0.39.4 (审计 M-7): naive 时间戳是美东墙钟 (分钟库口径), 直接按美东解释, 不再当 UTC 转换;
    带时区的输入才转换到美东. 输入可以是 1 分钟 K 或 30 分钟 K, 按 9:30 起每 60 分钟一桶
    (09:30, 10:30, ..., 15:30), 最后一桶到收盘为止 (正常日 30 分钟; 半日市按
    us_clock.session_close(d) 截止). 桶的最后一个周期已到 (或当天后面还有数据, 或是已收盘的
    历史日) 才算完整, 盘中尚未走完的那一桶丢弃.
    """
    if minute.is_empty() or "datetime" not in minute.columns:
        return pl.DataFrame()
    from app.market_clock import us_clock

    et_zone = "America/New_York"
    work = minute
    if work.schema["datetime"] == pl.Utf8:
        work = work.with_columns(pl.col("datetime").str.to_datetime())
    dtype = work.schema["datetime"]
    if not isinstance(dtype, pl.Datetime):
        return pl.DataFrame()
    if dtype.time_zone is None:
        et = pl.col("datetime").dt.replace_time_zone(et_zone, ambiguous="earliest", non_existent="null")
    else:
        et = pl.col("datetime").dt.convert_time_zone(et_zone)
    work = work.with_columns(et.alias("_et")).filter(pl.col("_et").is_not_null())
    work = work.with_columns(
        pl.col("_et").dt.date().alias("_d"),
        (pl.col("_et").dt.hour().cast(pl.Int32) * 60 + pl.col("_et").dt.minute().cast(pl.Int32)).alias("_m"),
    )
    open_minute = 9 * 60 + 30
    days = work["_d"].unique().to_list()
    if not days:
        return pl.DataFrame()
    closes = []
    for d in days:
        close = us_clock.session_close(d)
        closes.append(close.hour * 60 + close.minute)
    close_df = pl.DataFrame(
        {"_d": days, "_close_m": closes},
        schema={"_d": pl.Date, "_close_m": pl.Int32},
    )
    work = work.join(close_df, on="_d", how="left").filter(
        (pl.col("_m") >= open_minute) & (pl.col("_m") < pl.col("_close_m"))
    )
    if work.is_empty():
        return pl.DataFrame()
    work = work.sort(["symbol", "_et"])
    # 输入周期: 同一只同一天相邻两根的最小正间隔 (1 分钟 K 为 1, 30 分钟 K 为 30)
    gaps = (
        work.with_columns((pl.col("_m") - pl.col("_m").shift(1).over(["symbol", "_d"])).alias("_gap"))
        .filter(pl.col("_gap") > 0)["_gap"]
    )
    step = int(gaps.min()) if gaps.len() else 1
    work = work.with_columns(
        (open_minute + (pl.col("_m") - open_minute) // 60 * 60).alias("_bucket_m"),
    ).with_columns(
        pl.min_horizontal(pl.col("_bucket_m") + 60, pl.col("_close_m")).alias("_bucket_end_m"),
        pl.col("_m").max().over(["symbol", "_d"]).alias("_day_last_m"),
    )
    today = us_clock.today()
    hours = (
        work.group_by(["symbol", "_d", "_bucket_m"], maintain_order=True)
        .agg(
            pl.col("_et").first().alias("_first_et"),
            pl.col("open").first().alias("open"),
            pl.col("high").max().alias("high"),
            pl.col("low").min().alias("low"),
            pl.col("close").last().alias("close"),
            pl.col("volume").sum().alias("volume"),
            pl.col("_m").max().alias("_last_m"),
            pl.col("_bucket_end_m").first().alias("_bucket_end_m"),
            pl.col("_day_last_m").first().alias("_day_last_m"),
        )
        .filter(
            (pl.col("_last_m") + step >= pl.col("_bucket_end_m"))
            | (pl.col("_day_last_m") >= pl.col("_bucket_end_m"))
            | (pl.col("_d") < today)
        )
    )
    if hours.is_empty():
        return pl.DataFrame()
    return (
        hours.with_columns(
            (
                pl.col("_first_et").dt.truncate("1d")
                + pl.duration(minutes=pl.col("_bucket_m"))
            ).alias("datetime"),
        )
        .select(["symbol", "datetime", "open", "high", "low", "close", "volume"])
        .with_columns(pl.col("datetime").dt.date().alias("date"))
        .sort(["symbol", "datetime"])
    )


def enriched_history_days(data_dir, asset_type: str = "stock", as_of: date | None = None) -> int:
    """本地 enriched 在 as_of 及之前覆盖的交易日数 (#303)。

    按 date=* 分区目录名计数 (目录列举 O(天数)), 不读 parquet 内容 —
    只做数据充足性提示, 不进入指标计算路径。
    """
    from app.storage.repository import enriched_dirname

    root = Path(data_dir) / enriched_dirname(asset_type)
    if not root.exists():
        return 0
    days = [d.name[5:] for d in root.glob("date=*") if d.is_dir() and len(d.name) > 5]
    if as_of is not None:
        as_of_s = as_of.isoformat()
        days = [d for d in days if d <= as_of_s]
    return len(days)


# ── 进程级历史数据缓存 (避免 run_all 每次重新扫描 parquet + 计算指标) ──
_history_cache: dict[tuple[str, date, int], tuple[float, pl.DataFrame]] = {}
_HISTORY_CACHE_TTL = 120.0  # 秒



@dataclass
class ScreenerResult:
    as_of: date
    strategy: str | None
    rows: list[dict] = field(default_factory=list)
    total: int = 0
    elapsed_ms: float = 0.0


class ScreenerService:
    def __init__(self, repo: KlineRepository, asset_type: str = "stock") -> None:
        self.repo = repo
        self.asset_type = asset_type
        from app.storage.repository import enriched_dirname
        self._enriched_dirname = enriched_dirname(asset_type)

    @staticmethod
    def clear_history_cache() -> None:
        """清空进程级 _history_cache (TTL 缓存)。

        清除数据后调用, 避免内存里的旧历史窗口残留导致策略/看板仍命中旧数据。
        """
        _history_cache.clear()

    def _load_enriched_for_date(self, target_date: date) -> pl.DataFrame:
        """从 enriched parquet 读取指定日期的基础数据并即时计算完整指标+信号。

        enriched parquet 仅存 14 列。读取后需要即时计算 ma/ema/macd/kdj/rsi/boll/momentum/signal 等列。
        对于最新日, 优先使用内存缓存 (已包含完整指标)。
        """
        # 优先使用 repo 最新日缓存
        cache, cache_date = self.repo.get_enriched_latest_asset(self.asset_type)
        if cache is not None and not cache.is_empty() and cache_date == target_date:
            df = cache
            # JOIN instruments
            df_i = self.repo.get_instruments_asset(self.asset_type)
            if not df_i.is_empty():
                inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in df_i.columns]
                if "name" not in df.columns:
                    df = df.join(df_i.select(inst_cols), on="symbol", how="left")
            return df

        # 尝试从 repo 级预计算历史缓存中提取目标日期 (仅 stock: 该缓存为股票专用)
        if self.asset_type == "stock":
            cached_hist = self.repo.get_enriched_history(target_date, 1)
            if cached_hist is not None and not cached_hist.is_empty() and "date" in cached_hist.columns:
                df = cached_hist.filter(pl.col("date") == target_date)
                if not df.is_empty():
                    logger.debug("_load_enriched_for_date: repo history cache for %s", target_date)
                    # JOIN instruments
                    df_i = self.repo.get_instruments_asset(self.asset_type)
                    if not df_i.is_empty():
                        inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in df_i.columns]
                        if "name" not in df.columns:
                            df = df.join(df_i.select(inst_cols), on="symbol", how="left")
                    return df

        # 历史日期: 从 parquet 读取 14 列, 即时计算指标 (慢路径)
        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        ds = target_date.isoformat()
        target_parquet = enriched_dir / f"date={ds}" / "part.parquet"

        if not target_parquet.exists():
            return pl.DataFrame()

        try:
            df = pl.read_parquet(target_parquet)
        except Exception as e:  # noqa: BLE001
            logger.warning("load_enriched_for_date failed: %s", e)
            return pl.DataFrame()

        if df.is_empty():
            return df

        # 即时计算指标: 需要加载历史窗口作 warmup
        df_full = self._compute_enriched_full(df, target_date)
        return df_full

    def _compute_enriched_full(self, df_target: pl.DataFrame, target_date: date) -> pl.DataFrame:
        """从 14 列基础数据即时计算完整 enriched (含全部指标和信号)。

        读取历史数据作为指标计算的 warmup, 计算完成后只返回目标日期的行。
        """
        from app.indicators.pipeline import (
            compute_indicators,
            compute_turnover,
            compute_signals,
        )

        # 加载 warmup 历史 (目标日期前 ~120 天)
        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        start = target_date - timedelta(days=150)
        # turnover_rate 是 enriched 存储列, 必须随行透传: 否则即时计算后该列
        # 丢失, 自定义 SQL 用它做条件会 Binder Error 被吞成空结果 (#187)
        read_cols = ["symbol", "date", "open", "high", "low", "close", "volume",
                     "amount", "raw_close", "raw_high", "raw_low", "turnover_rate"]

        try:
            lf = (
                scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet"))
                .filter(
                    (pl.col("date") >= start)
                    & (pl.col("date") <= target_date)
                )
                .sort(["symbol", "date"])
            )
            available = [c for c in read_cols if c in lf.schema]
            df_hist = lf.select(available).collect()
        except Exception as e:  # noqa: BLE001
            logger.warning("warmup history load failed: %s", e)
            df_hist = df_target

        if df_hist.is_empty():
            df_hist = df_target

        # 计算指标
        df_full = compute_indicators(df_hist)
        df_full = compute_signals(df_full)

        # 计算换手率 (需要 instruments; 股票专有, ETF 跳过)
        instruments = self.repo.get_instruments_asset(self.asset_type)
        if self.asset_type == "stock" and instruments is not None and not instruments.is_empty():
            df_full = compute_turnover(
                df_full,
                instruments,
                historical_shares=self.repo.get_historical_shares(),
            )

        # 只保留目标日期
        df_result = df_full.filter(pl.col("date") == target_date)

        # JOIN instruments (name, total_shares, float_shares)
        if not instruments.is_empty():
            inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in instruments.columns]
            if "name" not in df_result.columns:
                df_result = df_result.join(instruments.select(inst_cols), on="symbol", how="left")

        return df_result

    def _load_enriched_history(self, target_date: date, lookback_days: int) -> pl.DataFrame:
        """读取目标日期之前的基础行情数据, 供历史窗口策略使用。

        优先从 repo 内存缓存获取 (启动时已预计算), 命中时 0ms。
        缓存 miss 时走 scan_parquet + compute_indicators 慢路径。
        """
        # 优先级 1: repo 级预计算缓存 (启动时 _refresh_enriched 已计算完整历史; 仅 stock)
        t0 = time.perf_counter()
        if self.asset_type == "stock":
            cached = self.repo.get_enriched_history(target_date, lookback_days)
            if cached is not None and not cached.is_empty():
                # JOIN instruments (repo 缓存不含 name 等列)
                instruments = self.repo.get_instruments_asset(self.asset_type)
                if instruments is not None and not instruments.is_empty() and "name" not in cached.columns:
                    inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"]
                                 if c in instruments.columns]
                    cached = cached.join(instruments.select(inst_cols), on="symbol", how="left")
                elapsed = (time.perf_counter() - t0) * 1000
                logger.info("_load_enriched_history(%s, %d): repo cache hit, %.1fms, %d rows",
                            target_date, lookback_days, elapsed, len(cached))
                return cached

        # 优先级 2: 进程级 history_cache (之前的 TTL 缓存)
        cache_key = (self.asset_type, target_date, lookback_days)
        now = time.monotonic()
        ttl_cached = _history_cache.get(cache_key)
        if ttl_cached is not None:
            ts, cached_df = ttl_cached
            if now - ts < _HISTORY_CACHE_TTL:
                logger.debug("history TTL cache hit: %s lookback=%d", target_date, lookback_days)
                return cached_df
            del _history_cache[cache_key]

        # 优先级 3: scan_parquet + compute_indicators (慢路径, ~5s)
        logger.warning("_load_enriched_history cache miss, computing indicators (%s, %d)...",
                       target_date, lookback_days)
        from app.indicators.pipeline import (
            compute_indicators,
            compute_turnover,
            compute_signals,
        )

        warmup = 60
        # 0.24.1: 旧式 min(..., 180) 把任何请求都封顶在 180 日历天 (~124 交易日), 需要 MA200 的策略
        # (connors_rsi2 / mdd_multi_down_days) 在盘后扫描永远 0 命中。按交易日请求量放宽 (1.5 倍日历天 + 30)。
        start = target_date - timedelta(days=max(min((lookback_days + warmup) * 2, 180), int(lookback_days * 1.5) + 30))

        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        # 同 _compute_enriched_full: turnover_rate 存储列随行透传 (#187)
        read_cols = ["symbol", "date", "open", "high", "low", "close", "volume",
                     "amount", "raw_close", "raw_high", "raw_low", "turnover_rate"]

        try:
            lf = (
                scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet"))
                .filter((pl.col("date") >= start) & (pl.col("date") <= target_date))
                .sort(["symbol", "date"])
            )
            available = [c for c in read_cols if c in lf.collect_schema().names()]
            df_hist = lf.select(available).collect()
        except Exception as e:  # noqa: BLE001
            logger.warning("load_enriched_history failed: %s", e)
            return pl.DataFrame()

        if df_hist.is_empty():
            return pl.DataFrame()

        df_full = compute_indicators(df_hist)
        df_full = compute_signals(df_full)

        instruments = self.repo.get_instruments_asset(self.asset_type)
        if self.asset_type == "stock" and instruments is not None and not instruments.is_empty():
            df_full = compute_turnover(
                df_full,
                instruments,
                historical_shares=self.repo.get_historical_shares(),
            )

        if instruments is not None and not instruments.is_empty():
            inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in instruments.columns]
            if "name" not in df_full.columns:
                df_full = df_full.join(instruments.select(inst_cols), on="symbol", how="left")

        # 裁剪掉 warmup 部分, 只保留 lookback 范围 (减少 group_by 开销)。
        # 按交易日计数: 从数据里实际存在的交易日序列取最后 lookback_days 个交易日,
        # 不能用 timedelta(days=N) (自然日), 否则周末/节假日会让窗口偏少, 与回测不一致。
        if "date" in df_full.columns:
            trading_dates = df_full["date"].unique().sort()
            if len(trading_dates) > lookback_days:
                lookback_start = trading_dates[-(lookback_days + 1)]
            else:
                lookback_start = trading_dates[0]
            df_full = df_full.filter(pl.col("date") >= lookback_start)

        df_full = df_full.sort(["symbol", "date"])

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info("_load_enriched_history(%s, %d): computed in %.1fms, %d rows",
                    target_date, lookback_days, elapsed, len(df_full))

        _history_cache[cache_key] = (now, df_full)
        if len(_history_cache) > 10:
            expired = [k for k, (ts, _) in _history_cache.items() if now - ts > _HISTORY_CACHE_TTL]
            for k in expired:
                del _history_cache[k]

        return df_full

    def run(
        self,
        as_of: date,
        conditions: list[str],
        order_by: str | None = None,
        limit: int = 30,
        pool: list[str] | None = None,
    ) -> ScreenerResult:
        """自定义 SQL 条件选股。

        先通过 Polars 即时计算完整指标, 再用 DuckDB 做 SQL WHERE 过滤。
        kline_enriched DuckDB 视图只有 14 列, 不能直接用于指标过滤。
        """
        t0 = time.perf_counter()

        if not conditions:
            return ScreenerResult(as_of=as_of, strategy=None)

        # 从即时计算获取完整 enriched 数据
        df = self._load_enriched_for_date(as_of)
        if df.is_empty():
            return ScreenerResult(as_of=as_of, strategy=None)

        # Pool 过滤
        if pool:
            df = df.filter(pl.col("symbol").is_in(pool))

        # 用 DuckDB 做 SQL 过滤 (注册临时视图)
        # 用独立的 :memory: 连接 (而非复用 repo 共享连接的 cursor): conditions 是用户
        # 传入的 SQL 片段, 隔离连接下注入至多能碰 read_csv/read_parquet 文件; 若复用共享
        # 连接则会把 app 已注册的真实业务表也暴露给注入, 扩大攻击面。隔离连接创建开销极低。
        # 再关闭 external_access, 让注入的文件读写函数 (read_parquet/COPY 等) 直接报错,
        # 视图数据仍通过 con.register 注入, 不受该开关影响 (#224)。
        con = None
        try:
            import duckdb
            con = duckdb.connect(
                database=":memory:", config={"enable_external_access": False}
            )
            con.register("enriched", df.to_arrow())
            where = " AND ".join(f"({c})" for c in conditions)
            sql = f"SELECT * FROM enriched WHERE {where}"
            if order_by:
                sql += f" ORDER BY {order_by}"
            if limit:
                sql += f" LIMIT {limit}"
            df_result = con.execute(sql).pl()
        except Exception as e:  # noqa: BLE001
            logger.warning("screener SQL query failed: %s", e)
            df_result = pl.DataFrame()
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass

        rows = df_result.to_dicts() if not df_result.is_empty() else []
        elapsed = (time.perf_counter() - t0) * 1000

        return ScreenerResult(
            as_of=as_of,
            strategy=None,
            rows=rows,
            total=len(rows),
            elapsed_ms=elapsed,
        )

    def build_strategy_context(
        self,
        engine,
        as_of: date,
        strategy_ids: list[str],
        *,
        timeframe: str = "1d",
        params_map: dict[str, dict] | None = None,
        overrides_map: dict[str, dict] | None = None,
        current: pl.DataFrame | None = None,
        market=None,
        cache_key: str | None = None,
    ):
        """按调用方要求装配标准策略数据上下文，不解释策略公式。"""
        from app.strategy.engine import StrategyDataContext

        if current is None:
            current = self._load_enriched_for_date(as_of)
        if timeframe == "1h":
            history = self._load_hour_history(as_of, current)
            return StrategyDataContext(
                asset_type=self.asset_type,
                timeframe=timeframe,
                as_of=as_of,
                current=current,
                history=history,
                market=None,
                cache_key=cache_key,
            )
        if timeframe == "1m":
            # 分钟策略数据源是本地当日分钟K分区 (单分区文件直读), 与日线
            # enriched 历史窗口无关, 不走 required_history_bars 日线路径。
            history = self._load_minute_history(as_of, current)
            # 策略声明 META["daily_history_bars"] 时额外装配日线 enriched 窗口,
            # 供分钟策略叠加日线维度条件 (如 N 日内放量过)。
            daily_history = None
            if engine is not None:
                daily_bars = engine.minute_daily_history_bars(strategy_ids)
                if daily_bars > 0:
                    daily_history = self._load_enriched_history(as_of, daily_bars)
            return StrategyDataContext(
                asset_type=self.asset_type,
                timeframe=timeframe,
                as_of=as_of,
                current=current,
                history=history,
                daily_history=daily_history,
                market=None,
                cache_key=cache_key,
            )
        history_bars = engine.required_history_bars(
            strategy_ids,
            params_map=params_map,
            overrides_map=overrides_map,
        )
        history = None
        if history_bars > 1:
            history = self._load_enriched_history(as_of, history_bars)
        return StrategyDataContext(
            asset_type=self.asset_type,
            timeframe=timeframe,
            as_of=as_of,
            current=current,
            history=history,
            market=market,
            cache_key=cache_key,
        )

    def _load_minute_history(self, as_of: date, current: pl.DataFrame | None) -> pl.DataFrame:
        """分钟策略数据源: 优先 as_of 当日分钟分区, 缺失时回退全市场最近分区。

        只按日期直读单个分区文件 (get_minute_by_dates), 与全量 glob 扫描解耦,
        内存只随当日分区大小 (~67万行) 走。标的池限定为 enriched 快照 universe;
        分区与快照的日期差是允许的 (分钟分区可能比 enriched 更新, 行自带时间戳)。
        """
        if self.asset_type != "stock":
            raise ValueError("分钟策略当前仅支持个股 (stock)")
        symbols: list[str] = []
        if current is not None and not current.is_empty():
            symbols = current["symbol"].cast(pl.Utf8).unique().to_list()
        if not symbols:
            return pl.DataFrame()
        df = self.repo.get_minute_by_dates(symbols, [as_of])
        if df.is_empty():
            fallback = self.repo.latest_minute_date_global()
            if fallback is None:
                raise ValueError(
                    "无分钟K数据 — 请先在 数据→分钟K 完成同步, 或开启盘中增量刷新"
                )
            if fallback != as_of:
                df = self.repo.get_minute_by_dates(symbols, [fallback])
        return df

    def _load_hour_history(self, as_of: date, current: pl.DataFrame | None) -> pl.DataFrame:
        """1 小时策略的数据: 常规时段 30 分钟 K 合成 9:30 对齐的小时线。

        只给声明 timeframe=1h 的策略用。生产分钟库大约 25 个交易日,
        TKE 的预热大约要 80 根小时线, 所以这里能出最近信号, 给不出长回测。
        """
        if self.asset_type != "stock":
            raise ValueError("1 小时策略当前只支持个股")
        symbols: list[str] = []
        if current is not None and not current.is_empty():
            symbols = current["symbol"].cast(pl.Utf8).unique().to_list()
        if not symbols:
            return pl.DataFrame()
        end = as_of
        start = as_of - timedelta(days=40)
        days = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor += timedelta(days=1)
        minute = self.repo.get_minute_by_dates(symbols, days)
        if minute.is_empty():
            raise ValueError("无分钟K, 1 小时策略需要常规时段分钟线, 请先同步分钟K")
        return regular_session_hours(minute)

    def latest_date(self) -> date | None:
        if self.asset_type != "stock":
            _, d = self.repo.get_enriched_latest_asset(self.asset_type)
            return d
        d = self.repo.enriched_latest_date()
        if d:
            return d
        # 回退 DuckDB
        try:
            res = self.repo.execute_one(
                "SELECT max(date) FROM kline_enriched",
            )
            if res and res[0]:
                d = res[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:  # noqa: BLE001
            return None
        return None

    def coverage_warnings(self, as_of: date, *, required_bars: int | None = None) -> list[str]:
        """数据充足性提示 (#303): enriched 覆盖不足时返回用户可读警告, 充足返回 []。

        空库首跑只拉到 1 个交易日时, 均线/动量/量比等指标暖机不足, 选股会静默
        全 0 — 这里把"数据不够"显式说出来。required_bars 缺省用通用暖机窗口
        (自定义 SQL 选股); 策略运行传 engine.required_history_bars 的精确值。
        available 为 0 时 enriched 为空, 上层 latest_date 已 400, 不重复提示。
        """
        available = enriched_history_days(self.repo.store.data_dir, self.asset_type, as_of)
        if available == 0:
            return []
        need = required_bars if required_bars and required_bars > 0 else MIN_INDICATOR_WARMUP_DAYS
        if available >= need:
            return []
        return [
            f"本地数据仅覆盖 {available} 个交易日, 低于本次计算所需约 {need} 天暖机窗口 — "
            "指标可能失真或全部落空 (选股 0 命中)。建议先全量回填日K并重算指标 "
            "(数据页「日K批量同步」, 符号需带交易所后缀)"
        ]
