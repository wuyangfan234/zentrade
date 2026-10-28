"""盘后管道 + 盘前维表同步。

调度:
  09:10 盘前 — 同步个股维表 instruments (全量覆盖)
  15:35 盘后 — 日K同步 + 增量除权因子 + enriched 计算 + 刷新视图
  (默认 15:35: 盘后固定价 15:30 终止 + 供应商日线定稿缓冲, 见 preferences)

盘后同步策略:
  日 K: QuoteService 交易时段已实时落盘 → 有数据时跳过 batch,首次拉 1 年区间
  除权因子: 从已有数据最新日期的下一天开始增量获取,避免重复拉取和计算
"""
from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from datetime import date as _date
from pathlib import Path

import polars as pl
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.indicators.pipeline import filter_halt_days, run_pipeline
from app.market_time import cn_today
from app.services import instrument_sync, kline_sync
from app.services import preferences as _prefs
from app.data_providers.models import Cap, CapabilitySet
from app.storage.repository import KlineRepository

logger = logging.getLogger(__name__)

ProgressCb = Callable[..., None]


def _prune_partial_enriched_partitions(daily_dir: Path, enriched_dir: Path) -> list[str]:
    """删除 symbol 覆盖不完整的 enriched 日期分区, 返回被删的日期 (#223)。

    自选实时路径会在全市场 enriched 生成前提前创建当日分区 (只有几只自选),
    仅按日期目录计数比较会把它误判为完整分区而跳过计算, 造成日K缺失与
    均线错误。按与加工相同的停牌过滤口径检查 symbol 覆盖, 不能直接比较
    行数: 正常剔除停牌记录会让 enriched 少行, 导致每次管道都删除重算。
    删除后 run_pipeline(new_dates_only=True) 会把它们当"新日期"全市场补齐。
    daily 同日分区不存在 (今日日K尚未同步) 时不处理, 留给当日正常流程。
    """
    import shutil

    pruned: list[str] = []
    for part in enriched_dir.glob("date=*"):
        daily_part = daily_dir / part.stem
        if not daily_part.exists():
            continue
        try:
            expected: set[str] = set()
            # 每次只读单文件的停牌判定列, 不加载全历史或指标宽表。
            for path in daily_part.glob("*.parquet"):
                schema = pl.read_parquet_schema(path)
                if not {"symbol", "open", "high"}.issubset(schema):
                    raise ValueError("daily 缺少 symbol/open/high, 无法判断有效标的覆盖")
                columns = [c for c in ("symbol", "open", "high", "volume", "amount") if c in schema]
                daily = pl.read_parquet(path, columns=columns)
                expected.update(filter_halt_days(daily)["symbol"].drop_nulls().to_list())
            actual: set[str] = set()
            for path in part.glob("*.parquet"):
                actual.update(pl.read_parquet(path, columns=["symbol"])["symbol"].drop_nulls().to_list())
        except Exception as e:
            logger.warning("enriched 覆盖检查跳过 %s, 保留分区: %s", part.name, e)
            continue
        if expected - actual:
            shutil.rmtree(part, ignore_errors=True)
            pruned.append(part.stem.split("=")[1])
    return pruned


def _prune_stale_price_partitions(
    daily_dir: Path, enriched_dir: Path, max_dates: int = 5
) -> list[str]:
    """删除收盘价与官方日线不一致的 enriched 日期分区。

    实时 flush 写入的当日分区行数与 daily 相同, 但收盘价可能停留在收盘集合
    竞价前的快照 (实测: TickFlow 实时端点收盘后仍长期返回旧价, 3392/5554 只
    股票当日收盘价与官方日线不符), #223 的行数校验识别不到。对最近若干交易日
    做值级比对: enriched.raw_close 与 daily.close 任一标的差超过半个最小报价
    单位即删分区, 由后续增量重算按官方日线全市场重建。
    """
    import shutil

    common = sorted(
        (
            p.stem.split("=", 1)[1]
            for p in enriched_dir.glob("date=*")
            if (daily_dir / p.stem).exists()
        ),
        reverse=True,
    )[:max_dates]
    pruned: list[str] = []
    for ds in common:
        try:
            daily = pl.read_parquet(
                daily_dir / f"date={ds}" / "*.parquet", columns=["symbol", "close"]
            )
            enr = pl.read_parquet(
                enriched_dir / f"date={ds}" / "*.parquet", columns=["symbol", "raw_close"]
            )
        except Exception:
            continue  # 列缺失/不可读 → 交给既有完整性检查兜底
        joined = enr.join(daily, on="symbol", how="inner").drop_nulls()
        if joined.is_empty():
            continue
        bad = joined.filter((pl.col("raw_close") - pl.col("close")).abs() > 0.005)
        if not bad.is_empty():
            shutil.rmtree(enriched_dir / f"date={ds}", ignore_errors=True)
            pruned.append(ds)
    return pruned


class PipelineStageError(RuntimeError):
    """管道有阶段软失败(数据可能陈旧)时抛出, 让上层 job_store 把任务标记为 failed。

    这些阶段单独 try/except 吞掉异常以不中断整条管道, 但一旦失败即代表对应数据陈旧。
    抛出前进度协议已走完(done/100), 故前端进度条正常收尾, 仅终态如实反映为 failed ——
    不再"部分失败却报成功"。
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("盘后管道部分阶段失败: " + "; ".join(errors))


def _noop(stage: str, pct: int, msg: str, **kwargs) -> None:  # noqa: ARG001
    pass


def _invalidate(table: str | None = None) -> None:
    """stage 写完调用,让 /api/data/status 只重算被影响的那张表。"""
    from app.api.data import invalidate_data_cache
    invalidate_data_cache(table)


def _resolve_universe(capset: CapabilitySet, repo=None) -> list[str]:
    """解析标的池。

    厂商 pools (CN_Equity_A 等) 已退役 (P1-1b2): 标的池 = 本地 instruments 表 + 自选;
    全市场池由后续 Alpaca provider / 机会引擎 universe 提供 (P2)。

    repo 传入时过滤自选兜底里的指数 symbol (指数日K走独立 kline_index_* 存储,
    进股票池会污染 kline_daily/kline_minute)。ETF 刻意保留 (既有行为)。
    """
    from app.services import watchlist as _watchlist_svc
    base: set[str] = set()
    try:
        base.update(e.get("symbol") for e in _watchlist_svc.list_symbols() if e.get("symbol"))
    except Exception as e:  # noqa: BLE001
        logger.warning("watchlist pool unavailable: %s", e)
    d = Path(settings.data_dir)
    inst_path = d / "instruments" / "instruments.parquet"
    if inst_path.exists():
        try:
            inst = pl.read_parquet(inst_path, columns=["symbol"])
            base.update(inst["symbol"].to_list())
        except Exception as e:  # noqa: BLE001
            logger.warning("instruments supplement failed: %s", e)
    # 核心指数 ETF (SPY/QQQ/DIA/IWM) 显式入池: SPY<MA200 市场状态门依赖 SPY 日K,
    # 但它们挂 NYSE Arca / 不在普通股 instruments, 不显式加入永远没有日K,
    # 市场门将永远"数据不足不瞎拦"绿灯放行 (9-29 审计实证)。
    from app.services.index_const import CORE_INDEX_SYMBOLS
    base.update(CORE_INDEX_SYMBOLS)
    # 过滤自选兜底里的指数 symbol (指数日K走独立 kline_index_* 存储,
    # 进股票池会污染 kline_daily/kline_minute)。ETF 刻意保留 (既有行为)。
    if repo is not None:
        base -= set(repo.get_index_symbol_set()) - set(CORE_INDEX_SYMBOLS)
    return sorted(base)


def run_instruments_sync(repo: KlineRepository) -> dict:
    """盘前同步个股维表。同步完成后刷新 enriched 内存缓存,
    确保跨天后选股/机会引擎读到基于最新维表的数据 (而非前一交易日残留)。
    """
    rows = instrument_sync.sync_instruments(repo.store.data_dir)
    _refresh_instruments_view(repo)
    _invalidate("instruments")
    # 维表更新后重建 enriched 缓存 (clear + refresh, 与设置页「清理并刷新」同等效果)
    if rows > 0:
        repo.clear_cache()
        repo.refresh_cache()
    return {"instruments_rows": rows}


def run_now(
    repo: KlineRepository,
    capset: CapabilitySet,
    on_progress: ProgressCb | None = None,
    override_start_date: _date | None = None,
    session_day: _date | None = None,
) -> dict:
    """立即执行一次盘后管道,支持进度回调。

    跳过的 stage **不 emit**,避免前端把"无 capability"的卡片错误标记为 active/done。
    result 里带 skipped_stages 列表供前端展示。

    override_start_date: 传入时强制走 batch 拉取分支,用该日期作为日K/除权/指数的
        拉取起点(到今天),用于「数据修正/补数据」场景。None 时走原有自动判定逻辑。
    """
    emit = on_progress or _noop
    skipped: list[str] = []
    # 阶段软失败累积: 下列阶段 try/except 吞异常以不中断管道, 但失败即代表数据可能陈旧。
    # 管道末尾若非空则抛 PipelineStageError, 让任务终态如实标记为 failed(而非误报成功)。
    stage_errors: list[str] = []

    # Step 0: 先同步个股维表, 再解析标的池 — 确保标的池基于最新 instruments
    emit("sync_instruments", 2, "同步个股维表…")
    inst_rows = instrument_sync.sync_instruments(repo.store.data_dir)
    if inst_rows > 0:
        _refresh_instruments_view(repo)
    emit("sync_instruments", 8, f"个股维表同步完成,{inst_rows} 只标的")
    _invalidate("instruments")

    emit("resolve_universe", 9, "解析标的池…")
    universe = _resolve_universe(capset, repo)
    emit("resolve_universe", 10, f"标的池规模:{len(universe)} 只")

    # Step 1: 日 K 同步
    #   override_start_date 传入 → 强制 batch 拉取 [override_start_date ~ today] (数据修正)
    #   付费档 + 今天有数据 → 实时行情接口拉一次覆写（1请求全市场）
    #   有历史数据 → batch K-line API 补齐缺口
    #   无任何数据 → batch K-line API 拉首次 1 年
    from datetime import timedelta as _td, datetime as _dt
    latest_daily = repo.latest_daily_date()
    # 管道「今天」必须是北京日期: 美西主机 15:35 北京时间仍是本地昨天,
    # date.today() 会把昨日日K当成已齐, 当日官方收盘价永远拉不进来。
    today = cn_today()
    today_exists = latest_daily and latest_daily >= today
    # 收盘定版目标日 (审计 2026-10-26 H-2): 开机补跑/失败重试跨过美东零点时, 目标是上一交易日,
    # 不是「今天」。只由补跑路径显式传入; 手动与 16:35 正常路径仍为今天 (避免盘中手动跑重复定版昨日)。
    close_day = session_day or today
    new_daily_days = 0

    # 完整性自愈: 检测最近交易日的盘中快照/缺口 (盘中停机后次日开实时会留下
    # 中午快照, 而下方"今天已有数据→只刷今天"分支会让它永久留存)。
    # 命中 → 本次管道放弃实时覆写分支, 降级为从最早坏日起的范围拉取。
    integrity_issues: list = []
    stale_day: _date | None = None
    etf_stale_day: _date | None = None
    index_stale_day: _date | None = None
    if override_start_date is None:
        try:
            from app.services import data_integrity
            integrity_issues = data_integrity.scan_recent_integrity(
                repo.store.data_dir, today=today,
            )
            if integrity_issues:
                stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_daily",))
                etf_stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_etf_daily",))
                index_stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_index_daily",))
                logger.warning(
                    "integrity: 检测到 %d 个不完整分区(%s), 本次管道改走范围拉取修复",
                    len(integrity_issues), data_integrity.describe_issues(integrity_issues),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("integrity scan failed (soft, 按无坏数据处理): %s", e)
            integrity_issues = []
    # 日K范围拉取的起点(分支3补缺口/分支4首次/数据修正); 实时增量/跳过时为 None。
    # 供 Step 1.5 除权因子回溯范围对齐: 范围拉取→用日K范围, 非范围→最近N天兜底。
    daily_range_start: _date | None = None
    _daily_written_dates: set = set()  # 本轮官方批次实际提交的日期分区 (出参收集)

    # 股票日K拉取开关(默认开);关闭时跳过日K同步,保留已有数据。
    # 数据修正(override_start_date)时即使关闭开关也强制拉取 — 修正就是来补数据的。
    pull_a_share = _prefs.get_pipeline_pull_a_share()
    if not pull_a_share and not override_start_date:
        emit("sync_daily", 45, "已跳过股票日K同步(拉取内容未勾选)")
        logger.info("sync_daily: skipped (pipeline_pull_a_share=False)")
    elif override_start_date:
        # 数据修正: 强制用传入日期作起点 batch 拉取, 忽略实时行情覆写分支。
        start_date = override_start_date
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] repair/override", start_date, today)

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.now(),  # 旧口径 today@00:00 是空窗口 (9-29 审计): 当日 SIP 日K须覆盖到 now; SIP 禁窗由 provider 钳制
            on_chunk_done=_daily_chunk_progress,
            written_dates_out=_daily_written_dates,
        )
        gap_days = (today - start_date).days
        new_daily_days = gap_days
        emit("sync_daily", 45, f"日K 完成,覆盖 {gap_days} 天")
        logger.info("sync_daily: [%s ~ %s] done, %d days", start_date, today, gap_days)
    elif False:  # 厂商实时覆写分支已退役 (P1-1b2): 自定义实时源覆写走 quote_service
        pass

    elif latest_daily or stale_day:
        # 有历史 → batch 补齐缺口。
        # 也覆盖"今天已有数据但无实时行情权限(free/none)"的降级场景:
        #   此时 start_date = latest_daily = today,batch 刷新当天日K。
        # 完整性修复场景: start_date = min(本地最新日, 最早坏日) —
        #   today_exists 时 latest_daily=今天, 不取 min 会漏掉坏日。
        start_date = min(d for d in (latest_daily, stale_day) if d is not None)
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] %s", start_date, today,
                    "refresh today" if today_exists else "gap fill")

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.now(),  # 旧口径 today@00:00 是空窗口 (9-29 审计): 当日 SIP 日K须覆盖到 now; SIP 禁窗由 provider 钳制
            on_chunk_done=_daily_chunk_progress,
            written_dates_out=_daily_written_dates,
        )
        gap_days = (today - start_date).days
        new_daily_days = gap_days
        emit("sync_daily", 45, f"日K 完成,覆盖 {gap_days} 天")
        logger.info("sync_daily: [%s ~ %s] done, %d days", start_date, today, gap_days)
    else:
        # 首次：无任何数据 → batch 拉 1 年
        start_date = today - _td(days=365)
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] initial fetch", start_date, today)

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.now(),  # 旧口径 today@00:00 是空窗口 (9-29 审计): 当日 SIP 日K须覆盖到 now; SIP 禁窗由 provider 钳制
            on_chunk_done=_daily_chunk_progress,
            written_dates_out=_daily_written_dates,
        )
        new_daily_days = 365
        emit("sync_daily", 45, "日K 完成")
        logger.info("sync_daily: [%s ~ %s] done", start_date, today)
    _invalidate("daily")

    # 官方收盘日标记 (9-29 审计): 当日分区若被本轮官方 SIP 批次写入, 通知
    # quote_service 当日静默 — 否则管道后 IEX 快照轮询会把官方日K覆写回小量
    # (实证: NVDA 4.5M vs 真实 2亿, 指数 ETF 行被整区覆写丢弃)。
    try:
        if close_day.isoformat() in _daily_written_dates:
            _qs = getattr(_get_app_state() or object(), "quote_service", None)
            if _qs is not None and hasattr(_qs, "mark_official_close"):
                _qs.mark_official_close(close_day)
    except Exception:  # noqa: BLE001
        logger.warning("mark_official_close failed (soft)", exc_info=True)

    # 指数表官方化 (10-09 核实): 指数独立同步已删, 指数表只有盘中快照写入 → 每天被完整性
    # 检查判坏, 总览/分时读到盘中值。把本轮官方批次写入的日期里 SPY/QQQ/DIA/IWM 复制进指数表。
    try:
        if _daily_written_dates:
            from app.services.index_sync import sync_core_index_from_stock
            sync_core_index_from_stock(repo, [_date.fromisoformat(str(d)) for d in _daily_written_dates])
    except Exception:
        logger.warning("index_sync failed (soft)", exc_info=True)

    # 完整性修复时删除股票 enriched 的坏分区: 增量重算只算 enriched 里不存在
    # 的日期, 盘中快照日分区已存在(虽是错的), 不删永远不会被重算。删除后
    # Step 2 把这些日期当"新日期"重算 (剩余分区最近 60 天做历史前缀, 窗口 ≤5 天回看充足)。
    repair_start = override_start_date if override_start_date is not None else stale_day
    if repair_start is not None:
        try:
            from app.services.data_integrity import prune_enriched_partitions
            pruned = prune_enriched_partitions(
                repo.store.data_dir, repair_start, "kline_daily_enriched",
            )
            if pruned:
                logger.info("integrity: 已删除 %d 个待重算的 enriched 分区 (≥ %s)", pruned, repair_start)
        except Exception as e:  # noqa: BLE001
            logger.warning("enriched prune failed (soft): %s", e)


    # 单标的新鲜度: 全局 max(date) 会被任一有今日数据的标的"拉高", 掩盖停牌/复牌/
    # 一直拉失败而掉队的个股缺口(全局判据只刷"今天", 永不回补掉队标的的历史缺口)。
    # 这里检测并**可见化**(WARNING + 计入结果), 让掉队标的不再隐形。
    # (自动回补暂不做 —— 需带退市判定, 否则对已退市标的每轮空拉浪费 API 额度。)
    lagging_symbols: list[str] = []
    if pull_a_share and latest_daily:
        try:
            lagging_symbols = repo.symbols_lagging(today, min_gap_days=3)
            if lagging_symbols:
                logger.warning("日K新鲜度: %d 只标的落后 >3 日 (停牌/退市/拉取失败; 样例: %s)",
                               len(lagging_symbols), lagging_symbols[:10])
        except Exception as e:  # noqa: BLE001
            logger.warning("laggard detection failed: %s", e)
            stage_errors.append(f"laggard detection: {e}")

    # Step 1.5: 同步除权因子 — 范围与日K拉取方式对齐
    #   日K范围拉取(补缺口/首次) → 除权用日K范围 [daily_range_start, now]
    #     首次会覆盖整个日K区间内的历史除权事件; 补缺口天然只增量(起点=latest_daily≈昨天)
    #   日K实时增量/跳过(分支2/分支1) → 除权兜底拉最近 30 天, 补可能遗漏的新除权
    #     (这两类分支不拉历史日K, 除权不能用日K范围, 只能兜底最近几日)
    written_adj = 0
    affected_symbols: list[str] = []
    adj_basis_migration = False
    adj_provider = _prefs.get_adj_factor_provider()
    can_sync_adj = capset.has(Cap.ADJ_FACTOR) or bool(adj_provider)
    if can_sync_adj:
        from datetime import datetime, timedelta
        adj_end = datetime.now()
        if daily_range_start is not None:
            # 多留一周: 事件由相邻交易日水平比值识别, 窗口首日无法与前一日比较。
            adj_start = datetime.combine(daily_range_start, datetime.min.time()) - timedelta(days=7)
        else:
            # 日K实时增量/跳过时, 除权兜底拉最近 N 天, 覆盖周末/长假/停机期间的新除权事件。
            # 15 天: 覆盖长假 + 故障恢复缓冲; 事件表 merge+unique 幂等, 多拉无副作用。
            adj_start = adj_end - timedelta(days=15)
        factor_dir = repo.store.data_dir / "adj_factor"
        # 旧口径 (逐日水平) 因子表不能增量续写: 按本地全部日K历史重建事件, 并全量重算 enriched。
        if kline_sync.adj_factor_basis(factor_dir) != kline_sync.ADJ_EVENT_BASIS:
            earliest = repo.earliest_daily_date()
            if earliest is not None:
                adj_basis_migration = True
                adj_start = min(adj_start, datetime.combine(earliest, datetime.min.time()))
                logger.warning("sync_adj: 因子表为旧口径, 按完整历史 [%s ~] 重建除权事件", earliest)
        adj_start_str = adj_start.strftime("%Y-%m-%d")
        adj_end_str = adj_end.strftime("%Y-%m-%d")
        emit("sync_adj", 50, f"获取除权因子 [{adj_start_str} ~ {adj_end_str}]…")
        logger.info("sync_adj: [%s ~ %s] start", adj_start_str, adj_end_str)

        def _adj_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_adj", 50 + int(10 * cur / tot),
                 f"除权因子批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        # 长停牌票在增量窗口里没有前一根 bar: 按每票最近有效日K向前补锚点。
        anchors = {} if adj_basis_migration else _adj_anchor_dates(repo, universe, adj_start.date())
        try:
            written_adj, affected_symbols = kline_sync.sync_adj_factor(
                universe, repo, capset,
                start_time=adj_start, end_time=adj_end,
                on_chunk_done=_adj_chunk_progress,
                replace=adj_basis_migration, anchor_dates=anchors,
            )
        except Exception as e:  # noqa: BLE001
            # 源端失败: 保留旧因子与迁移待办, 本轮如实失败; 不得用残缺因子发布 enriched。
            adj_basis_migration = False
            written_adj, affected_symbols = 0, []
            stage_errors.append(f"sync_adj: {type(e).__name__}: {e}")
            logger.warning("sync_adj failed, factors unchanged: %s", e)
        else:
            if adj_basis_migration:
                kline_sync.mark_rebuild_pending(factor_dir, None, "adj_basis_migration")
            elif affected_symbols:
                kline_sync.mark_rebuild_pending(factor_dir, affected_symbols, "adj_factor_changed")
        if affected_symbols:
            _refresh_single_view(repo, "adj_factor")
            emit("sync_adj", 60, f"除权因子完成,新增 {len(affected_symbols)} 只个股")
            logger.info("sync_adj: [%s ~ %s] done, %d symbols", adj_start_str, adj_end_str, len(affected_symbols))
        else:
            emit("sync_adj", 60, "除权因子完成,无新增")
            logger.info("sync_adj: [%s ~ %s] no new factors", adj_start_str, adj_end_str)
        _invalidate("adj_factor")
    else:
        skipped.append("sync_adj")
        logger.info("sync_adj skipped: no ADJ_FACTOR capability")

    # 上一轮因子已提交但 enriched 未完成 (崩溃/失败): 从持久待办恢复重算范围。
    adj_dir = repo.store.data_dir / "adj_factor"
    pending = kline_sync.rebuild_pending(adj_dir)
    if pending is not None:
        if pending.get("symbols") is None:
            adj_basis_migration = True
        else:
            affected_symbols = sorted(set(affected_symbols) | set(pending["symbols"]))
    # 口径迁移未完成且本轮除权源失败: 因子不可信, 本轮不发布 enriched (旧产物原样保留)。
    adj_untrusted = (any(e.startswith("sync_adj:") for e in stage_errors)
                     and kline_sync.adj_factor_basis(adj_dir) != kline_sync.ADJ_EVENT_BASIS)

    # Step 2: 计算 enriched
    #   判断策略:
    #     - 首次 (enriched 目录不存在) → 全量
    #     - 往前扩展历史 (新日期 < enriched 已有最早日期) → 全量
    #       前面的除权因子会改变累积因子链,影响后面所有日期的复权价格
    #     - 往后新增日期或已有历史区间内的缺口
    #       → 增量补新区块(所有标的) + 受除权影响个股全日期重算
    #     - 无新日期 + 有新除权因子 → 增量: 只重算受影响个股的全部日期
    #     - 无新日期 + 无变化 → 跳过
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    enriched_exists = enriched_dir.exists() and any(enriched_dir.glob("date=*"))
    daily_dir = repo.store.data_dir / "kline_daily"
    daily_days = len(list(daily_dir.glob("date=*"))) if daily_dir.exists() else 0
    prev_enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_exists else 0

    # 部分分区修复 (#223) + 收盘价过期分区修复: 删除被实时合并提前创建、覆盖不全
    # 或收盘价停留在竞价前快照的 enriched 分区, 让下方计数比较与增量计算把它们
    # 重新当新日期处理 (值级比对以官方日线为准, 实时源不纠错也能自愈)
    if enriched_exists:
        partial_pruned = _prune_partial_enriched_partitions(daily_dir, enriched_dir)
        stale_pruned = _prune_stale_price_partitions(daily_dir, enriched_dir)
        pruned_dates = sorted(set(partial_pruned) | set(stale_pruned))
        if pruned_dates:
            logger.warning(
                "compute_enriched: 发现 %d 个异常 enriched 分区 (覆盖不全 %d / 收盘价过期 %d), "
                "已删除待重算: %s",
                len(pruned_dates), len(partial_pruned), len(stale_pruned),
                ", ".join(pruned_dates[:10]),
            )
            enriched_exists = enriched_dir.exists() and any(enriched_dir.glob("date=*"))
            prev_enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_exists else 0

    # 判断新日期方向: 找 daily 和 enriched 的日期集合做比较
    forward_incremental = False
    backward_extension = False

    if daily_days > prev_enriched_days and enriched_exists:
        daily_dates = sorted(d.stem.split("=")[1] for d in daily_dir.glob("date=*"))
        enriched_dates = sorted(d.stem.split("=")[1] for d in enriched_dir.glob("date=*"))
        earliest_enriched = enriched_dates[0]
        new_dates = set(daily_dates) - set(enriched_dates)
        if new_dates:
            # 有新日期早于 enriched 最早日期 → 往前扩展
            if any(d < earliest_enriched for d in new_dates):
                backward_extension = True
            # 包含中间被删的异常分区; 没有新增末日也必须补算。
            # 往前扩展仍由下方优先走全量分支。
            forward_incremental = True

    def _enriched_batch_progress(cur: int, tot: int) -> None:
        emit("compute_enriched", 65 + int(23 * cur / tot),
             f"计算指标 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    if adj_untrusted:
        written_enriched = 0
        skipped.append("compute_enriched")
        logger.warning("compute_enriched: skipped, adjustment migration pending and factor source failed")
    elif not enriched_exists or backward_extension or adj_basis_migration:
        # 首次 / 往前扩展 / 复权口径迁移 → 全量
        emit("compute_enriched", 65, "全量计算 enriched…")
        logger.info("compute_enriched: full rebuild (first=%s, backward=%s, adj_migration=%s, daily=%d, enriched=%d)",
                    not enriched_exists, backward_extension, adj_basis_migration, daily_days, prev_enriched_days)
        written_enriched = run_pipeline(on_batch_done=_enriched_batch_progress)
        new_enriched_days = len(list(enriched_dir.glob("date=*")))
        emit("compute_enriched", 88, f"enriched 完成,覆盖 {new_enriched_days} 天")
        logger.info("compute_enriched: full rebuild done, %d days", new_enriched_days)
    elif forward_incremental:
        # 往后新增日期: 增量补新区块 + 受影响个股全日期重算
        symbols_to_recompute = list(set(affected_symbols)) if affected_symbols else []
        emit("compute_enriched", 65,
             f"增量计算 enriched (新日期 + {len(symbols_to_recompute)} 只个股重算)…"
             if symbols_to_recompute else "增量计算 enriched (新日期)…")
        logger.info("compute_enriched: forward incremental, %d symbols to recompute",
                    len(symbols_to_recompute))
        written_enriched = run_pipeline(
            new_dates_only=True,
            symbols=symbols_to_recompute or None,
            on_batch_done=_enriched_batch_progress,
        )
        new_enriched_days = len(list(enriched_dir.glob("date=*")))
        emit("compute_enriched", 88, f"enriched 完成,覆盖 {new_enriched_days} 天")
        logger.info("compute_enriched: forward incremental done, %d days", new_enriched_days)
    elif affected_symbols:
        # 无新日期,仅除权因子变更 → 只重算受影响个股的全部日期
        emit("compute_enriched", 65, f"增量计算 enriched ({len(affected_symbols)} 只个股)…")
        logger.info("compute_enriched: adj_factor incremental, %d symbols", len(affected_symbols))
        written_enriched = run_pipeline(symbols=affected_symbols, on_batch_done=_enriched_batch_progress)
        emit("compute_enriched", 88, f"enriched 完成,{len(affected_symbols)} 只个股")
    else:
        written_enriched = 0
        logger.info("compute_enriched: skip (no new daily, no adj_factor changes)")
    if not adj_untrusted:
        # 上面各分支都已覆盖待办范围 (全量 / 新日期+受影响票 / 受影响票), 走到这里即完成。
        kline_sync.clear_rebuild_pending(adj_dir)
    _refresh_single_view(repo, "kline_enriched")
    _invalidate("enriched")

    # Step 2.1: 数据充足性可见化 (#303) — 空库首跑/仅当日实时覆写 1 天的库,
    # 均线/动量/量比等指标暖机不足, 管道各 stage 都"成功"但选股会静默全 0。
    enriched_total_days = warn_if_enriched_too_thin(repo.store.data_dir)

    # Step 2.3: 指数/ETF 独立同步已删除 (P1-5): ZenTrade MVP 仅普通股,
    # SPY/QQQ 等指数 ETF 随股票日K同批次拉取 (Alpaca 按 ticker 一视同仁)。
    skipped.append("sync_index")

    # Step 2.5: 分钟 K 同步(可选) — 未启用或无 capability 时静默跳过(不 emit)
    from app.services import preferences
    minute_on = preferences.get_minute_sync_enabled()
    minute_days = preferences.get_minute_sync_days()
    written_minute = 0
    if minute_on and capset.has(Cap.KLINE_MINUTE_BATCH):
        minute_start = today - _td(days=minute_days)
        emit("sync_minute", 90, f"获取分钟K [{minute_start} ~ {today}]…")
        logger.info("sync_minute: [%s ~ %s] start", minute_start, today)
        minute_symbols = _resolve_minute_symbols(capset, repo)
        def _minute_chunk_progress(cur: int, tot: int, seg_label: str = "") -> None:
            emit("sync_minute", 90 + int(3 * cur / tot),
                 f"分钟K 批次 {cur}/{tot}" + (f" [{seg_label}]" if seg_label else ""),
                 stage_pct=int(100 * cur / tot), skip_log=True)
        written_minute = kline_sync.sync_and_persist_minute(
            minute_symbols, repo, capset, days=minute_days,
            on_chunk_done=_minute_chunk_progress,
        )
        minute_dir = repo.store.data_dir / "kline_minute"
        minute_cover_days = len(list(minute_dir.glob("date=*"))) if minute_dir.exists() else 0
        emit("sync_minute", 93, f"分钟K完成,覆盖 {minute_cover_days} 天")
        logger.info("sync_minute: [%s ~ %s] done, %d days", minute_start, today, minute_cover_days)
        _invalidate("minute")
    else:
        skipped.append("sync_minute")
        if minute_on:
            logger.info("sync_minute skipped: no KLINE_MINUTE_BATCH capability")
        else:
            logger.info("sync_minute skipped: user disabled")

    # 市场环境 / 主线 / 旧模拟账户结算已退役 (P1-3), 只运行下方 ZenTrade 纸面台账。

    # Step 3: 刷新视图
    emit("refresh_views", 95, "刷新 DuckDB 视图…")
    _refresh_views(repo)

    # 纸面旁路必须有已收盘/官方日线证据；跳过或失败也写进结果，不伪称成功。
    zen_summary: dict = {}
    try:
        emit("zen_trade", 97, "ZenTrade 盘后检查…")
        zen_summary = _run_zen_post_close(repo, close_day)
        logger.info("zen_trade: %s", zen_summary)
        emit("zen_trade", 97, f"ZenTrade {zen_summary.get('status', 'unknown')}")
    except Exception as e:
        zen_summary = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        logger.warning("zen_trade failed (soft, 不阻断数据管道): %s", e)
        emit("zen_trade", 97, "ZenTrade 盘后失败，详见任务结果")

    emit("done", 100, "完成")
    _invalidate(None)  # 兜底:全清

    result = {
        "universe_size": len(universe),
        "daily_days": new_daily_days,
        "adj_factor_symbols": len(affected_symbols),
        "enriched_days": written_enriched,
        "minute_rows": written_minute,
        "zen_trade": zen_summary,
        "lagging_symbols": len(lagging_symbols),
        "enriched_total_days": enriched_total_days,
        "integrity_repair_from": repair_start.isoformat() if repair_start else None,
        "integrity_issues": len(integrity_issues),
        "skipped_stages": skipped,
        "stage_errors": stage_errors,
    }

    # 有阶段软失败: 进度协议已走完(done/100, 前端进度条正常收尾), 但数据可能陈旧,
    # 抛出让上层 job_store 把终态标记为 failed —— 不再"部分失败却报成功"。
    if stage_errors:
        raise PipelineStageError(stage_errors)

    return result


def _adj_anchor_dates(repo, universe: list[str], window_start: _date, buffer_days: int = 7) -> dict[str, _date]:
    """停牌跨过增量窗口前段的票, 返回其窗口前最后一个有效日K日期作为除权比较锚点。

    正常交易的票在窗口前 buffer_days 内已有 bar, 首个事件可以与之比较, 不需要锚点。
    """
    lookup = getattr(repo, "daily_anchor_dates", None)
    if not callable(lookup):
        return {}
    try:
        anchors = lookup(window_start, _td_days(buffer_days))
    except Exception as e:  # noqa: BLE001
        logger.warning("adj anchor lookup failed, incremental window unchanged: %s", e)
        return {}
    wanted = set(universe)
    return {s: d for s, d in anchors.items() if s in wanted}


def _td_days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def _run_zen_post_close(repo, day: _date) -> dict:
    """唯一盘后纸面编排入口；盘中手动日K同步不能偷跑机会/NAV定版。"""
    from app.market_clock import us_clock
    from app.services.trading_loop import TradingLoop
    from app.ledger.core import Account
    now = us_clock.now()
    if day > now.date() or (day == now.date() and now.time() < us_clock.sessions[-1].close):
        return {"status": "skipped", "reason": "regular_session_not_closed"}
    quote_service = _get_app_state_attr("quote_service")
    if quote_service is None or not quote_service.is_official_close(day):
        return {"status": "skipped", "reason": "official_close_not_confirmed"}
    loop = _get_app_state_attr("trading_loop")
    if loop is None:
        loop = TradingLoop(repo, Account())
        loop.boot()
        _set_app_state_attr("trading_loop", loop)
    managed = loop.post_close_manage(day, confirmed_official=True)
    if managed.get("skipped"):
        return {"status": "skipped", "reason": managed["skipped"], "missing_symbols": managed.get("missing_symbols", [])}
    result = loop.opp_svc.run_daily_scan(day)
    if managed.get("missing_symbols"):
        logger.warning("zen_trade %s: 缺官方收盘 %s, 止损判断只跳过这几只, 当日净值未定版", day, managed["missing_symbols"])
    try:  # 自选影子账本: 每日结算满期卡 (≤ 自选数只票, 很快); 失败不影响扫描与推送
        from app.services import watchlist_v4
        watchlist_v4.settle(repo.store.data_dir, repo, today=day)
    except Exception as e:  # noqa: BLE001
        logger.warning("watchlist v4 settle failed (soft): %s", type(e).__name__)
    try:  # 方向 A 卡级结局结算 (研究级, 只写 research/settlement/; 目录不存在 = 回填未获批, 不做)
        from app.services import card_settlement
        card_settlement.run_if_enabled(repo.store.data_dir, repo, as_of_close=day)
    except Exception as e:  # noqa: BLE001
        logger.warning("card settlement failed (soft): %s", type(e).__name__)
    from app.services.notify_opportunity import enqueue_daily_opportunities
    enqueue_daily_opportunities(repo.store.data_dir, result, daily=_paper_daily_report(loop, day, managed))
    try:  # 0.26.0 机会板卡自动结算 + 每日对账 (只写 research/swing_settle/; 不影响交易)。放在收盘推送之后:
        # 对账要在自选上重算 3+3 信号 (实测约 1~3 秒、+300 MB), 不让它拖慢收盘推送。
        from app.services import swing_settlement
        logger.info("swing settle %s: %s", day, swing_settlement.run_daily(repo.store.data_dir, repo, day=day))
    except Exception as e:
        logger.warning("swing settle failed (soft): %s", type(e).__name__)
    return {"status": "completed", "manage_events": managed["events"],
            "nav": managed["nav"]["nav"] if managed.get("nav") else None,
            "missing_symbols": managed.get("missing_symbols") or [], "cards": len(result.cards),
            "watch": len(result.watch), "gate": result.gate}


def _paper_daily_report(loop, day: _date, managed: dict) -> dict | None:
    """日报素材: 当日定版净值、上一日净值、持仓、当日止损上移等管理动作。失败不阻断推送。"""
    try:
        import json as _json
        nav_path = loop.ledger.data_dir / "nav" / "daily.jsonl"
        rows = [_json.loads(x) for x in nav_path.read_text(encoding="utf-8").splitlines() if x.strip()] if nav_path.exists() else []
        prev = next((r.get("nav") for r in reversed(rows) if str(r.get("date")) < day.isoformat()), None)
        positions = [{"symbol": p.symbol, "qty": p.qty, "avg_cost": p.avg_cost, "stop_price": p.stop_price,
                      **({"contract": True} if getattr(p, "contract", False) else {})}
                     for p in loop.ledger.open_positions]
        actions = [e for e in loop.ledger.read_events()
                   if e.get("kind") == "stop_move" and str(e.get("trade_date") or "") == day.isoformat()]
        return {"nav": managed.get("nav"), "prev_nav": prev, "positions": positions, "actions": actions}
    except Exception as e:  # noqa: BLE001
        logger.warning("paper daily report unavailable: %s", type(e).__name__)
        return None


def warn_if_enriched_too_thin(data_dir: Path) -> int:
    """enriched 总覆盖天数; 低于常见指标暖机窗口时 WARN 引导全量回填 (#303)。

    返回天数供管道 result 上报。目录列举 O(天数), 不在热路径。
    """
    from app.services.screener import MIN_INDICATOR_WARMUP_DAYS, enriched_history_days

    days = enriched_history_days(data_dir)
    if days < MIN_INDICATOR_WARMUP_DAYS:
        logger.warning(
            "enriched 仅覆盖 %d 个交易日 (<%d): 指标暖机不足, 选股可能全部 0 命中且无提示 — "
            "建议全量回填 (同步标的维表 → 日K批量同步(带后缀符号) → 重算 enriched)",
            days, MIN_INDICATOR_WARMUP_DAYS,
        )
    return days


def _refresh_views(repo: KlineRepository) -> None:
    """刷新所有 DuckDB 视图 —— 委托给 repository 的唯一权威实现 rebuild_views()。"""
    repo.rebuild_views()


def _refresh_single_view(repo: KlineRepository, name: str) -> None:
    """刷新单个 DuckDB 视图。"""
    d = repo.store.data_dir.as_posix()
    paths = {
        "kline_daily": f"{d}/kline_daily/**/*.parquet",
        "kline_enriched": f"{d}/kline_daily_enriched/**/*.parquet",
        "kline_index_daily": f"{d}/kline_index_daily/**/*.parquet",
        "kline_index_enriched": f"{d}/kline_index_enriched/**/*.parquet",
        "kline_etf_daily": f"{d}/kline_etf_daily/**/*.parquet",
        "kline_etf_enriched": f"{d}/kline_etf_enriched/**/*.parquet",
        "kline_etf_minute": f"{d}/kline_etf_minute/**/*.parquet",
        "kline_minute": f"{d}/kline_minute/**/*.parquet",
        "adj_factor": f"{d}/adj_factor/**/*.parquet",
        "adj_factor_etf": f"{d}/adj_factor_etf/**/*.parquet",
        "instruments": f"{d}/instruments/**/*.parquet",
        "instruments_index": f"{d}/instruments_index/**/*.parquet",
        "instruments_etf": f"{d}/instruments_etf/**/*.parquet",
    }
    path = paths.get(name)
    if not path:
        return
    try:
        repo.db.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{path}', union_by_name=true)"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh view %s failed: %s", name, e)


def _resolve_minute_symbols(capset: CapabilitySet, repo=None) -> list[str]:
    """分钟 K 同步标的 — 与日K共用同一标的池。"""
    return _resolve_universe(capset, repo)


def _refresh_instruments_view(repo: KlineRepository) -> None:
    """单独刷新 instruments 视图。"""
    d = repo.store.data_dir.as_posix()
    try:
        repo.db.execute(
            f"CREATE OR REPLACE VIEW instruments AS "
            f"SELECT * FROM read_parquet('{d}/instruments/**/*.parquet', union_by_name=true)"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh instruments view failed: %s", e)



# 最近一次 _run_tracked 的结局 (ok / skipped / cancelled; 异常直接上抛)。审计 M-5: 单飞跳过 / 被取消时
# 调度器只看到「正常返回」, 运维台账记 ok; 定时入口据此改为抛 PipelineNotRunError。
# 复审: 按线程隔离 (补跑 / 重试 / 16:35 定时可能并发, 共享字典会互相改写结局)。
_OUTCOME_TLS = __import__("threading").local()


class _OutcomeView:
    """dict 风格的线程本地视图 (仅本线程最近一次 _run_tracked 的结局)。"""

    def _d(self) -> dict:
        d = getattr(_OUTCOME_TLS, "d", None)
        if d is None:
            d = _OUTCOME_TLS.d = {}
        return d

    def __getitem__(self, k):
        return self._d()[k]

    def __setitem__(self, k, v):
        self._d()[k] = v

    def get(self, k, default=None):
        return self._d().get(k, default)


_LAST_OUTCOME = _OutcomeView()
# 单飞跳过的目标交易日 -> 跳过时刻 (失败重试据此接手: 跳过时没有管道任务记录, 旧判定以为「没跑过」)。
_PIPELINE_SKIPPED: dict = {}


class PipelineNotRunError(RuntimeError):
    """定时盘后管道没有真正跑完 (单飞跳过 / 被取消)。上抛让 APScheduler 记 error, 运维台账不再记 ok。"""


def _run_tracked(fn, job_label: str) -> bool:
    """调度触发时包装 JobStore 跟踪，确保同步历史有记录。

    单飞: 若已有活跃(pending∨running)任务(手动同步中), 本次调度直接跳过, 不并发。
    重任务执行槽: 再挡一层僵尸并发(reap 后线程仍活时不得并行写 parquet)。
    返回 True 仅表示任务已成功并且执行槽已释放。
    """
    from app.services.pipeline_jobs import JobCancelledError, job_store, release_run_slot, run_with_capacity, try_acquire_run_slot

    _LAST_OUTCOME[job_label] = "skipped"
    job_id, is_new = job_store.create()
    if not is_new:
        logger.info("scheduled %s 跳过: 已有活跃任务在运行 (job_id=%s)", job_label, job_id)
        _LAST_OUTCOME["detail"] = f"skipped: 已有任务在运行 job_id={job_id}"
        return False
    if not try_acquire_run_slot(job_id):
        logger.warning("scheduled %s 跳过: 重任务执行槽被占用(疑似上次任务卡死)", job_label)
        job_store.fail(job_id, f"scheduled {job_label} skipped: 已有数据任务在运行")
        _LAST_OUTCOME["detail"] = f"skipped: 重任务执行槽被占用 job_id={job_id}"
        return False
    _LAST_OUTCOME[job_label] = "cancelled"
    _LAST_OUTCOME["detail"] = f"cancelled: job_id={job_id}"

    def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                 skip_log: bool = False) -> None:
        job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

    succeeded = False
    try:
        result = run_with_capacity(job_id, lambda: fn(on_progress=progress))
        job_store.succeed(job_id, result)
        succeeded = True
        _LAST_OUTCOME[job_label] = "ok"
        logger.info("scheduled %s completed: job_id=%s", job_label, job_id)
    except JobCancelledError:
        # 已由 terminate() 标记失败(卡死/手动取消), 拉取线程在分块回调处自行退出
        logger.warning("scheduled %s cancelled: job_id=%s", job_label, job_id)
    except Exception as e:
        # 错误摘要进 job_store; 再上抛让 APScheduler EVENT_JOB_ERROR → ops/jobs_run.jsonl
        # 记 status=error (此前吞异常导致台账假 ok, 系统状态看不到失败)。
        err = f"{type(e).__name__}: {str(e)[:160]}"
        logger.exception("scheduled %s failed: job_id=%s", job_label, job_id)
        job_store.fail(job_id, f"scheduled {job_label} failed: {err}")
        raise
    finally:
        release_run_slot(job_id)
    return succeeded


def pipeline_catchup_target(jobs, now, sched, *, nav_dates: set[str] | None = None) -> _date | None:
    """启动补跑判定 (审计 #6, 2026-10-12): 返回需要补跑盘后管道的交易日, 不需要补返回 None。

    调度器用内存 jobstore, 盘后管道运行中被重启打断 (或重启错过 cron 时刻) 后不会再跑,
    次日无新卡、无收盘推送。规则:
    - 目标日 = 最近一个已过调度时刻的交易日;
    - 该日 16:00 ET 之后已有成功的盘后管道, 且 zen_trade.status == completed、当日净值已写入 → 不补
      (0.16.1; nav_dates=None 表示净值文件不可读, 只看状态);
    - 交易日 09:00 ET 到当日调度时刻之间不补 (管道会暂停行情并占用执行槽, 不能和交易环抢);
      当天盘后管道会把缺的日K一并拉齐。
    """
    from datetime import UTC, datetime, time, timedelta

    from app.market_clock import us_clock
    sched_t = time(sched["hour"], sched["minute"])
    today = now.date()
    if us_clock.is_trading_day(today) and time(9, 0) <= now.time() < sched_t:
        return None
    d = today if us_clock.is_trading_day(today) and now.time() >= sched_t else us_clock.previous_trading_day(today)
    close_at = datetime.combine(d, time(16, 0), tzinfo=us_clock.tz)
    for j in jobs:
        if j.get("status") != "succeeded" or not isinstance(j.get("result"), dict):
            continue
        zen = j["result"].get("zen_trade")
        if not isinstance(zen, dict) or zen.get("status") != "completed":
            continue
        if nav_dates is not None and d.isoformat() not in nav_dates:
            continue
        try:
            started = datetime.fromisoformat(str(j.get("started_at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if started >= close_at - timedelta(seconds=1):
            return None
    return d


def _nav_dates(data_dir) -> set[str] | None:
    """纸面净值已定版的日期 (ledger/nav/daily.jsonl)。读不了返回 None。"""
    import json as _json
    from pathlib import Path
    p = Path(data_dir) / "ledger" / "nav" / "daily.jsonl"
    try:
        if not p.exists():
            return set()
        out = set()
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.add(str(_json.loads(line)["date"]))
                except (ValueError, KeyError):
                    continue
        return out
    except OSError:
        return None


def _run_data_quality(state) -> None:
    """日K数据质量: 盘后管道成功后自动检查, 有问题推飞书 (只读, 失败不影响管道)。"""
    try:
        from app.services import data_quality
        if state is not None:
            data_quality.run(state.repo.store.data_dir, loop=getattr(state, "trading_loop", None))
    except Exception:
        logger.exception("data quality check failed; daily pipeline remains succeeded")


def record_pipeline_catchup(data_dir, now=None, *, session_day=None) -> bool:
    """0.35.3: 盘后管道经补跑 (开机 / 失败重试 / 手动) 成功后, 给运维台账补一条 catchup_ok。

    盯盘清单只认 jobs_run.jsonl: 16:35 那次失败后即使补跑成功, 当天也会一直显示「盘后管道失败」(10-22 实例)。
    只在交易日、已过调度时刻、且当天还没有成功记录时写; 返回是否写入。
    session_day (0.39.2): 跨过美东零点的补跑按目标交易日记 (scheduled = 目标日调度时刻), 不按今天。"""
    from datetime import datetime, time

    from app.market_clock import us_clock
    from app.services import ops_watch
    now = now or us_clock.now()
    day = session_day or now.date()
    sched = _prefs.get_pipeline_schedule()
    fire = datetime.combine(day, time(sched["hour"], sched["minute"]), tzinfo=us_clock.tz)
    if not us_clock.is_trading_day(day) or now < fire:
        return False
    runs = ops_watch.load_runs(data_dir, day)
    if any(r.get("job") == "daily_pipeline" and r.get("status") in ("ok", "catchup_ok") for r in runs):
        return False
    ops_watch.record_run(data_dir, "daily_pipeline", "catchup_ok", scheduled=fire, now=now)
    return True


def after_manual_pipeline_success(app_state, result) -> None:
    """0.35.3: 手动 POST /api/pipeline/run 成功后的收尾, 与调度路径一致: 日K数据质量 + 运维台账补记。
    此前手动补跑不跑数据质量, 状态页「日K数据质量」停在上一交易日。不起周度挖掘 (只由调度路径起)。"""
    if not isinstance(result, dict) or ((result.get("zen_trade") or {}).get("status") != "completed"):
        return
    _run_data_quality(app_state)
    try:
        if app_state is not None:
            record_pipeline_catchup(app_state.repo.store.data_dir)
    except Exception:
        logger.exception("pipeline catch-up ledger note failed")


def catchup_too_close_to_open(now, target) -> bool:
    """0.39.2 复审: 补跑过去交易日要定版昨日 (一轮十几分钟), 交易日 08:30 ET 起不再发起, 免得和开盘交易环重叠。
    与失败重试 (pipeline_retry_due) 同一条 08:30 线; 当天 16:35 管道会把缺的日K一并拉齐 (昨日净值仍缺, 状态页可见)。"""
    from datetime import time

    from app.market_clock import us_clock
    return target < now.date() and us_clock.is_trading_day(now.date()) and now.time() >= time(8, 30)


def _scheduled_pipeline_task(pipeline_fn, *, catchup: bool = False, session_day=None) -> bool:
    """Run weekly mining only after the tracked daily pipeline has fully succeeded."""
    if not _run_tracked(pipeline_fn, "daily_pipeline"):
        return False
    state = _get_app_state()
    _run_data_quality(state)
    if catchup:
        try:
            if state is not None:
                record_pipeline_catchup(state.repo.store.data_dir, session_day=session_day)
        except Exception:
            logger.exception("pipeline catch-up ledger note failed")
    try:
        from app.services.mining_schedule import run_weekly_mining

        result = run_weekly_mining(state)
        logger.info("scheduled mining result: %s", result)
    except Exception:
        logger.exception("scheduled mining enqueue failed; daily pipeline remains succeeded")
    return True


# 0.35.3: 盘后管道失败自动重试 (10-22: 14:59-16:36 ET NAS 断网, 16:35 管道在 sync_daily 因 URLError 失败,
# 开机补跑只在启动后 120 秒判定一次, 网络恢复后没人重跑, 直到北京次日 08:43 手动补跑)。
# 每 15 分钟看一次: 目标日 (pipeline_catchup_target) 收盘后最近一次管道失败, 且离失败已过退避间隔 → 再跑一次。
PIPELINE_RETRY_GAPS_MIN = (15, 30, 60, 120)
# job_store 与维表同步 / 扩展历史 / 重算 enriched 等共用; 只有这些阶段名或标签属于盘后管道
_PIPELINE_ONLY_STAGES = {"resolve_universe", "sync_daily", "sync_adj", "compute_enriched", "refresh_views", "zen_trade"}


def _is_pipeline_job(j: dict) -> bool:
    res = j.get("result") if isinstance(j.get("result"), dict) else {}
    return ("zen_trade" in res or "daily_days" in res or str(j.get("error") or "").startswith("scheduled daily_pipeline")
            or j.get("stage") in _PIPELINE_ONLY_STAGES)


def pipeline_retry_due(jobs, now, sched, *, nav_dates=None, attempts: int = 0, active: bool = False,
                       skipped_at=None) -> _date | None:
    """返回该重试的目标交易日, 不该重试返回 None。纯函数 (调度包装见 start_scheduler)。

    - 有活跃任务 / 已重试满 len(PIPELINE_RETRY_GAPS_MIN) 次 → 不试;
    - 目标日 = pipeline_catchup_target (已含: 交易日 09:00 至调度时刻不跑、当天已成功不跑);
      另外交易日 08:30 起也不起新重试 (一轮十几分钟, 不能拖进 09:00 后);
    - 目标日收盘后最近一次任务必须是 failed (只重试真失败, 没跑过的交给开机补跑), 且结束时刻距今 ≥ 第 n 档间隔。"""
    from datetime import UTC, datetime, time, timedelta

    from app.market_clock import us_clock
    if active or attempts >= len(PIPELINE_RETRY_GAPS_MIN):
        return None
    if us_clock.is_trading_day(now.date()) and time(8, 30) <= now.time() < time(sched["hour"], sched["minute"]):
        return None
    d = pipeline_catchup_target(jobs, now, sched, nav_dates=nav_dates)
    if d is None:
        return None
    close_at = datetime.combine(d, time(16, 0), tzinfo=us_clock.tz)

    def _ts(v):
        try:
            t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    after = [j for j in jobs if _is_pipeline_job(j)
             and (s := _ts(j.get("started_at"))) is not None and s >= close_at - timedelta(seconds=1)]
    if not after:
        # 审计 M-5: 定时管道被单飞跳过时没有任务记录; skipped_at = 跳过时刻, 按失败同档退避重试
        if skipped_at is None:
            return None
        done = skipped_at
    else:
        last = max(after, key=lambda j: _ts(j.get("started_at")))
        if last.get("status") != "failed":
            return None
        done = _ts(last.get("finished_at")) or _ts(last.get("started_at"))
    if done is None or now - done < timedelta(minutes=PIPELINE_RETRY_GAPS_MIN[attempts]):
        return None
    return d


def start_scheduler(repo: KlineRepository, capset: CapabilitySet) -> AsyncIOScheduler:
    """启动调度器。

    工作日 09:10 — 同步个股维表
    工作日 HH:MM — 盘后管道（时间由用户偏好决定，默认 15:35）
    """
    from app.services import preferences
    sched = preferences.get_pipeline_schedule()
    inst_sched = preferences.get_instruments_schedule()

    scheduler = AsyncIOScheduler(timezone="America/New_York")

    # 盘前: 同步 instruments（时间由偏好决定）
    def _instruments_task(on_progress=None):
        emit = on_progress or _noop
        emit("sync_instruments", 0, "同步个股维表…")
        result = run_instruments_sync(repo)
        emit("done", 100, f"个股维表同步完成,{result.get('instruments_rows', 0)} 只标的")
        return result

    scheduler.add_job(
        lambda: _run_tracked(_instruments_task, "instruments_sync"),
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=inst_sched["hour"], minute=inst_sched["minute"],
                            timezone="America/New_York"),
        id="pre_market_instruments",
        misfire_grace_time=1800,
        replace_existing=True,
    )

    # 盘后: 日 K + enriched（时间由偏好决定）
    def _pipeline_then_refresh(on_progress=None, session_day=None):
        # 与手动触发 (/api/pipeline/run) 对齐: 管道落盘后重建 Polars 内存缓存,
        # 否则 live_agg 的昨日基准列会停留在旧交易日
        # (仅手动触发或重启才会刷缓存, cron 调度路径此前漏了这步)。
        # 用 app.state 上的**实时** capset(周期重探会热更新它), 而非启动时捕获的
        # 旧 capset —— 否则 Key 中途过期/续费后, 调度管道仍按旧档位打端点。
        app_state = _get_app_state()
        capset_live = getattr(app_state, "capabilities", None) or capset
        # 管道运行期间暂停实时行情取数, 防止覆写同一批 parquet 竞态
        qs = getattr(app_state, "quote_service", None)
        try:
            if qs:
                with qs.paused():
                    result = run_now(repo, capset_live, on_progress=on_progress, session_day=session_day)
            else:
                result = run_now(repo, capset_live, on_progress=on_progress, session_day=session_day)
        finally:
            # 即便有阶段软失败(run_now 末尾抛 PipelineStageError), 已落盘的日K/enriched
            # 仍需刷进内存缓存, 否则 live_agg 基准列停留在旧交易日。放 finally 保证部分
            # 成功也生效; 随后异常继续上抛, 由 _run_tracked 标记任务 failed。
            repo.refresh_cache()
        return result

    def _scheduled_daily():
        ok = _scheduled_pipeline_task(_pipeline_then_refresh)
        outcome = _LAST_OUTCOME.get("daily_pipeline")
        if not ok and outcome in ("skipped", "cancelled"):
            from app.market_clock import us_clock
            now = us_clock.now()
            if outcome == "skipped":
                _PIPELINE_SKIPPED[now.date().isoformat()] = now
            raise PipelineNotRunError(_LAST_OUTCOME.get("detail") or f"daily_pipeline {outcome}")
        return ok

    scheduler.add_job(
        _scheduled_daily,
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=sched["hour"], minute=sched["minute"],
                            timezone="America/New_York"),
        id="daily_pipeline",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 启动补跑: 重启打断或错过当日盘后管道时, 开机 2 分钟后补一次 (判定见 pipeline_catchup_target)
    def _pipeline_catchup():
        from app.market_clock import us_clock
        from app.services.pipeline_jobs import job_store
        target = pipeline_catchup_target(job_store.list_recent(50), us_clock.now(), sched,
                                         nav_dates=_nav_dates(repo.store.data_dir))
        if target is None:
            logger.info("pipeline catch-up: 最近交易日盘后管道已成功或当前不宜补跑, 跳过")
            return
        if catchup_too_close_to_open(us_clock.now(), target):
            logger.warning("pipeline catch-up: %s 补跑会拖进开盘 (08:30 ET 后不起), 交给当天 16:35 管道", target)
            return
        logger.warning("pipeline catch-up: %s 盘后管道没有成功记录, 启动补跑", target)
        _scheduled_pipeline_task(functools.partial(_pipeline_then_refresh, session_day=target), catchup=True,
                                 session_day=target)

    from datetime import timedelta as _td

    from app.market_clock import us_clock as _clock
    scheduler.add_job(_pipeline_catchup, trigger="date", run_date=_clock.now() + _td(seconds=120),
                      id="daily_pipeline_catchup", max_instances=1, misfire_grace_time=3600, replace_existing=True)

    # 0.35.3: 盘后管道失败自动重试 (判定见 pipeline_retry_due; 每个目标日最多 4 次, 间隔 15/30/60/120 分钟)
    _retry_attempts: dict[str, int] = {}

    def _pipeline_retry():
        from app.market_clock import us_clock
        from app.services.pipeline_jobs import job_store
        jobs = job_store.list_recent(50)
        now = us_clock.now()
        nav = _nav_dates(repo.store.data_dir)
        probe = pipeline_catchup_target(jobs, now, sched, nav_dates=nav)
        key = probe.isoformat() if probe else ""
        target = pipeline_retry_due(jobs, now, sched, nav_dates=nav, attempts=_retry_attempts.get(key, 0),
                                    active=job_store.active_id() is not None,
                                    skipped_at=_PIPELINE_SKIPPED.get(key) if key else None)
        if target is None:
            return None
        _retry_attempts[key] = _retry_attempts.get(key, 0) + 1
        logger.warning("pipeline retry: %s 盘后管道失败, 第 %d 次自动重试", target, _retry_attempts[key])
        return _scheduled_pipeline_task(functools.partial(_pipeline_then_refresh, session_day=target), catchup=True,
                                        session_day=target)

    scheduler.add_job(_pipeline_retry, trigger=IntervalTrigger(minutes=15), id="daily_pipeline_retry",
                      max_instances=1, coalesce=True, misfire_grace_time=600, replace_existing=True)

    # 与全市场行情解耦：只取活跃卡/观察/持仓的小池执行报价。
    def _paper_tick():
        loop = getattr(_get_app_state(), "trading_loop", None)
        if loop is not None:
            return loop.run_intraday()

    scheduler.add_job(_paper_tick, trigger=IntervalTrigger(seconds=15),
        id="zen_paper_tick", max_instances=1, coalesce=True,
        misfire_grace_time=10, replace_existing=True)

    # 机会板每 30 分钟刷新 (无限 2026-10-16): 美东交易日北京时间 20:00 起 (夏令时 = 美东 08:00, 冬令时 = 07:00)
    # 到 16:00 收盘, 每个整点/半点落一次机会板快照; 页面每 60 秒读实时板, 快照只用于记录卡的状态变化 (灰色历史)。
    def _swing_refresh():
        from app.services import swing_desk
        st = _get_app_state()
        r = getattr(st, "repo", None)
        if r is None or not swing_desk.refresh_due(_clock.now()):
            return None
        try:
            return swing_desk.snapshot(r.store.data_dir, repo=r, loop=getattr(st, "trading_loop", None))
        except Exception as e:
            logger.warning("swing refresh failed (soft): %s", type(e).__name__)
            return None

    scheduler.add_job(_swing_refresh, trigger=CronTrigger(day_of_week="mon-sun", minute="0,30", timezone="Asia/Shanghai"),
        id="zen_swing_refresh", max_instances=1, coalesce=True, misfire_grace_time=600, replace_existing=True)

    # 0.39.0 机会页雷达帧 (只展示): 15 秒探测一次状态变化 (台账 / 四个引擎落盘 / 热报价下的挂单距离),
    # 变了才重算并追加一帧到 opportunities/radar_frames/<美东日>.jsonl, 再经 /api/intraday/stream 推 opp_radar_updated。
    # 窗口 = 机会板首刷前 5 分钟到收盘后 10 分钟; 失败只记日志, 不碰交易环。
    def _radar_frame():
        from app.services import opp_radar
        st = _get_app_state()
        r = getattr(st, "repo", None)
        if r is None:
            return None
        qs = getattr(st, "quote_service", None)
        return opp_radar.tick(r.store.data_dir, repo=r, loop=getattr(st, "trading_loop", None),
                              notify=getattr(qs, "notify_opp_radar_updated", None))

    scheduler.add_job(_radar_frame, trigger=IntervalTrigger(seconds=15), id="zen_radar_frame", max_instances=1,
        coalesce=True, misfire_grace_time=10, replace_existing=True)

    # 0.41.0 共振影子账本 (只记录): 09:30 至入场截止每 5 分钟把新出现的共振票记一笔; 16:45 ET 回填 1/3/5 日收盘涨跌。
    def _resonance_shadow():
        from app.services import opp_desk
        st = _get_app_state()
        r = getattr(st, "repo", None)
        if r is None:
            return None
        return opp_desk.tick(r.store.data_dir, repo=r, loop=getattr(st, "trading_loop", None))

    def _resonance_settle():
        from app.services import opp_desk
        st = _get_app_state()
        r = getattr(st, "repo", None)
        if r is None:
            return None
        return opp_desk.settle(r.store.data_dir, r)

    scheduler.add_job(_resonance_shadow, trigger=CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5",
        timezone="America/New_York"), id="zen_resonance_shadow", max_instances=1, coalesce=True, misfire_grace_time=120,
        replace_existing=True)
    scheduler.add_job(_resonance_settle, trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=45,
        timezone="America/New_York"), id="zen_resonance_settle", max_instances=1, coalesce=True, misfire_grace_time=3600,
        replace_existing=True)

    # 决策台证据周更: 周六 10:00 ET 统一口径重评全部策略 (持重任务槽; 交易时段不跑)。
    def _weekly_strategy_evaluation():
        from app.services import evaluation_schedule
        res = evaluation_schedule.run(_get_app_state())
        # 新闻催化影子证据: 回填满 7 日纪律的候选结局 (只观察, 失败不影响策略评估)
        try:
            from app.market_clock import us_clock
            from app.services import news_shadow
            n = news_shadow.resolve_outcomes(repo.store.data_dir, repo, today=us_clock.today())
            logger.info("news shadow outcomes resolved: %d", n)
        except Exception as e:  # noqa: BLE001
            logger.warning("news shadow resolve failed (soft): %s", type(e).__name__)
        # 影子前向账本: 结算满 7 日纪律的影子卡 (只记账, 不影响交易)
        try:
            from app.market_clock import us_clock
            from app.services import shadow_forward
            n = shadow_forward.settle(repo.store.data_dir, repo, today=us_clock.today())
            logger.info("shadow forward settled: %d", n)
        except Exception as e:  # noqa: BLE001
            logger.warning("shadow forward settle failed (soft): %s", type(e).__name__)
        return res

    scheduler.add_job(_weekly_strategy_evaluation,
        trigger=CronTrigger(day_of_week="sat", hour=10, minute=0, timezone="America/New_York"),
        id="zen_weekly_strategy_evaluation", max_instances=1, coalesce=True, misfire_grace_time=6 * 3600,
        replace_existing=True)

    # Alpaca 纸面镜像 (2026-10-02 批准): 每 15 秒把台账新成交以市价单发到 Alpaca 纸面账户并回收成交价。
    def _alpaca_mirror_tick():
        loop = getattr(_get_app_state(), "trading_loop", None)
        if loop is None:
            return None
        from app.services.alpaca_mirror import AlpacaMirror, _lock as mirror_lock
        with mirror_lock:   # 读台账 + 载入状态 + 写回 在同一把锁里: 与纸面账户重置串行 (重置先拿这把锁)
            return AlpacaMirror(repo.store.data_dir).tick(loop.ledger.read_events())

    scheduler.add_job(_alpaca_mirror_tick, trigger=IntervalTrigger(seconds=15),
        id="zen_alpaca_mirror", max_instances=1, coalesce=True, misfire_grace_time=10, replace_existing=True)

    # 盘前大盘情绪: 08:35 ET 生成当日判断并锁定 (只展示, 不碰交易环)
    def _market_mood_generate():
        from app.market_clock import us_clock as _clk
        if not _clk.is_trading_day(_clk.today()):
            return None
        from app.services import market_mood
        provider = None
        try:
            from app.data_providers.custom import get_provider
            provider = get_provider("alpaca")
        except Exception as e:
            logger.warning("market mood: alpaca provider unavailable: %s", type(e).__name__)
        try:
            res = market_mood.generate(repo.store.data_dir, repo, provider)
        except Exception as e:
            logger.warning("market mood generate failed (soft): %s", type(e).__name__)
            return None
        try:
            from app.services import notify_opportunity as _no
            if res and _no._enqueue(repo.store.data_dir, f"market_mood:{res['day']}", market_mood.message(res)):
                _no.flush_notifications(repo.store.data_dir, limit=5)
        except Exception as e:
            logger.warning("market mood push failed (soft): %s", type(e).__name__)
        return res

    def _market_mood_score():
        from app.market_clock import us_clock as _clk
        if not _clk.is_trading_day(_clk.today()):
            return None
        from app.services import market_mood
        try:
            return market_mood.score(repo.store.data_dir, repo)
        except Exception as e:
            logger.warning("market mood score failed (soft): %s", type(e).__name__)
            return None

    scheduler.add_job(_market_mood_generate,
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=35, timezone="America/New_York"),
        id="zen_market_mood", max_instances=1, coalesce=True, misfire_grace_time=1200, replace_existing=True)
    scheduler.add_job(_market_mood_score,
        trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=10, timezone="America/New_York"),
        id="zen_market_mood_score", max_instances=1, coalesce=True, misfire_grace_time=3600, replace_existing=True)
    scheduler.add_job(_market_mood_score,
        trigger=CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone="America/New_York"),
        id="zen_market_mood_score_retry", max_instances=1, coalesce=True, misfire_grace_time=3600, replace_existing=True)

    # 盘前跳空预警: 美东 09:00 (北京夏令时 21:00 / 冬令时 22:00), 交易日。HL 永续价只提醒不交易。
    def _premarket_gap():
        from app.market_clock import us_clock as _clk
        if not _clk.is_trading_day(_clk.today()):
            return None
        from app.services import premarket_gap
        try:
            res = premarket_gap.run(repo.store.data_dir, getattr(_get_app_state(), "trading_loop", None))
        except Exception as e:  # noqa: BLE001
            logger.warning("premarket gap failed (soft): %s", type(e).__name__)
            return None
        try:
            from app.services.notify_opportunity import flush_notifications
            flush_notifications(repo.store.data_dir, limit=5)
        except Exception as e:  # noqa: BLE001
            logger.warning("premarket gap flush failed (soft): %s", type(e).__name__)
        return res

    scheduler.add_job(_premarket_gap,
        trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone="America/New_York"),
        id="zen_premarket_gap", max_instances=1, coalesce=True, misfire_grace_time=600, replace_existing=True)

    # 0.27.0 (无限 10-19 选 B): 机会板做多卡第 1 天 09:00 盘前价越过止损 -> 整卡作废。SIP 扣留 15 分钟,
    # 08:30-09:00 那根 09:16 后才拿得到 -> 09:20 ET 起 (北京夏令时 21:20 / 冬令时 22:20), 09:30 / 09:40 再补两次
    # (只查还没查过的卡; 出网只在这里, 不在交易环); 交易环 09:44 起用本地 09:00 HL 价兜底。
    def _swing_pm_check():
        from app.market_clock import us_clock as _clk
        if not _clk.is_trading_day(_clk.today()):
            return None
        loop = getattr(_get_app_state(), "trading_loop", None)
        if loop is None:
            return None
        if not getattr(loop, "_boot", False):
            loop.boot()
        loop.ensure_watchlist_book()
        res = loop.premarket_void_check()
        try:  # 0.31.0 盘前定稿 (只重排/展示; 失败不影响作废检查与交易)
            from app.services import pm_rank
            pm_rank.run(repo.store.data_dir, repo, _clk.market_datetime(_clk.now()))
        except Exception as e:
            logger.warning("pm rank failed (soft): %s", type(e).__name__)
        return res

    scheduler.add_job(_swing_pm_check,
        trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute="20,30,40", timezone="America/New_York"),
        id="zen_swing_pm_check", max_instances=1, coalesce=True, misfire_grace_time=600, replace_existing=True)

    # 0.30.0: card-replay checks for mined / AI strategies requested on a trading day 09:00-17:00 ET
    # (gear button or strategy publish) are queued and started here. During the session the enriched data is
    # rewritten every ~10 s, so the full-market read cannot get a stable snapshot; by 17:05 the 16:35 pipeline
    # (~13 min) is done. A fixed time also covers requests made after the pipeline and a failed pipeline day.
    def _swing_candidates_queue():
        from app.services import swing_candidates
        return swing_candidates.run_pending(repo.store.data_dir, repo)

    scheduler.add_job(_swing_candidates_queue,
        trigger=CronTrigger(day_of_week="mon-fri", hour=17, minute=5, timezone="America/New_York"),
        id="zen_swing_candidates_queue", max_instances=1, coalesce=True, misfire_grace_time=3600, replace_existing=True)

    # 0.28.0 盘中强势卡 (无限 10-20: 只上机会板 + 自动结算记战绩, 不下单): 10:00-15:00 ET 每 30 分钟检查自选出卡
    # (形态用 20 分钟前已完成的 SIP 5 分钟K + 出卡时实时价), 同时按 5 分钟K 结算在途卡; 16:25 ET 再结算一次
    # (当天K线齐了, 第 5 天到期的卡按收盘结)。不碰台账、不进交易环。
    def _intraday_strength():
        from app.services import intraday_strength
        st = _get_app_state()
        r = getattr(st, "repo", None)
        if r is None:
            return None
        res = intraday_strength.run(r.store.data_dir, wait_s=intraday_strength.LOCK_WAIT_S)
        logger.info("intraday strength: %s", {k: v for k, v in res.items() if k != "settled"})
        return res

    scheduler.add_job(_intraday_strength,
        trigger=CronTrigger(day_of_week="mon-fri", hour="10-15", minute="0,30", timezone="America/New_York"),
        id="zen_intraday_strength", max_instances=1, coalesce=True, misfire_grace_time=600, replace_existing=True)
    scheduler.add_job(_intraday_strength,
        trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=25, timezone="America/New_York"),
        id="zen_intraday_strength_settle", max_instances=1, coalesce=True, misfire_grace_time=3600,
        replace_existing=True)

    # 0.34.0 开盘放量榜 (无限 10-22 选「机会页按时段换主角」): 09:30 那根 SIP 5 分钟K 09:50 后才完整可见,
    # 09:51 ET 出榜 (北京夏令时 21:51 / 冬令时 22:51), 09:56 再试一次 (当天已出榜就跳过); 16:40 ET 记收盘, 17:40 补缺。
    # 不写台账、不进交易环。0.35.0: 出榜后按齿轮开关 / 门槛与全局渠道推一条提醒 (每天每渠道最多一条, 推送失败不影响出榜)。
    def _open_radar():
        from app.services import open_radar
        st = _get_app_state()
        r = getattr(st, "repo", None)
        if r is None:
            return None
        res = open_radar.run(r.store.data_dir)
        logger.info("open radar: %s", res)
        try:
            pushed = open_radar.push(r.store.data_dir, repo=r)
            if pushed.get("queued"):
                from app.services.notify_opportunity import flush_notifications
                flush_notifications(r.store.data_dir, limit=5)
            res = {**(res or {}), "push": pushed}
        except Exception as e:
            logger.warning("open radar push failed (soft): %s", type(e).__name__)
        return res

    def _open_radar_settle():
        from app.services import open_radar
        st = _get_app_state()
        r = getattr(st, "repo", None)
        if r is None:
            return None
        return open_radar.settle(r.store.data_dir)

    scheduler.add_job(_open_radar,
        trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute="51,56", timezone="America/New_York"),
        id="zen_open_radar", max_instances=1, coalesce=True, misfire_grace_time=600, replace_existing=True)
    scheduler.add_job(_open_radar_settle,
        trigger=CronTrigger(day_of_week="mon-fri", hour="16-17", minute=40, timezone="America/New_York"),
        id="zen_open_radar_settle", max_instances=1, coalesce=True, misfire_grace_time=3600, replace_existing=True)

    # 0.29.1: 09:44 ET 首选定格快照 (zen_top_snapshot) 随旧机会卡退役; 历史快照 data/opportunities/top_snapshots/ 只读保留

    # 美股财务同步 (2026-10-12): 盘后 18:30 ET 轮转拉 EDGAR (+Finnhub 备源); 失败抛错由盯盘台账记录告警, 旧数据保留
    def _fundamentals_sync():
        from app.services import fundamentals_sync
        return fundamentals_sync.scheduled(repo.store.data_dir)

    scheduler.add_job(_fundamentals_sync,
        trigger=CronTrigger(day_of_week="mon-fri", hour=18, minute=30, timezone="America/New_York"),
        id="zen_fundamentals_sync", max_instances=1, coalesce=True, misfire_grace_time=3600, replace_existing=True)

    # 5 秒热报价 (持仓/活跃卡/自选/正在看的个股, ≤50 只): 持仓现价与分时实时拼接。不撮合、不落盘。
    def _hot_quotes_tick():
        from app.services import hot_quotes
        return hot_quotes.tick(getattr(_get_app_state(), "trading_loop", None))

    scheduler.add_job(_hot_quotes_tick, trigger=IntervalTrigger(seconds=5),
        id="zen_hot_quotes", max_instances=1, coalesce=True,
        misfire_grace_time=4, replace_existing=True)

    # 个股新闻 (无限 2026-10-02 拍板): 每分钟唤醒, 内部按节奏真跑 (盘前/盘中 5 分钟, 其余 30 分钟; SEC 30 分钟)。
    # 采集 → Kimi 关思考翻译打标 → 持仓高重要度利空入队飞书。只展示与记账, 不参与开仓。
    def _news_tick():
        from app.services import news_job
        state = _get_app_state()
        if state is None:
            return None
        return news_job.tick(state.repo.store.data_dir, getattr(state, "trading_loop", None), state.repo)

    scheduler.add_job(_news_tick, trigger=IntervalTrigger(seconds=60),
        id="zen_news", max_instances=1, coalesce=True,
        misfire_grace_time=55, replace_existing=True)

    # 盘面情报面板 (0.40.0, 无限 2026-10-26 选 B 档): 每分钟唤醒, 内部按源各自节奏刷新 (盘前盘中 1-30 分钟, 其余更慢)。
    # 只写 data/intel/ 缓存供页面读; 不进打分、裁决、下单, 不推送。
    def _intel_tick():
        from app.services import intel_panel
        state = _get_app_state()
        if state is None:
            return None
        return intel_panel.tick(state.repo.store.data_dir, state.repo, getattr(state, "trading_loop", None))

    scheduler.add_job(_intel_tick, trigger=IntervalTrigger(seconds=60),
        id="zen_intel", max_instances=1, coalesce=True,
        misfire_grace_time=55, replace_existing=True)

    # QVeris 财报一致预期 (无限 2026-10-02 拍板「10 个交易日内有财报的持仓和候选每天自动查一次」):
    # 美东 08:45 (财报日历 08:30 刷新之后) 跑一次; 计入每日 100 积分上限, 当日已缓存的不再扣费。
    def _qveris_auto_estimates():
        from app.services import qveris_data
        state = _get_app_state()
        if state is None:
            return None
        return qveris_data.auto_refresh_estimates(state.repo.store.data_dir, getattr(state, "trading_loop", None))

    scheduler.add_job(_qveris_auto_estimates,
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=45, timezone="America/New_York"),
        id="zen_qveris_estimates", max_instances=1, coalesce=True, misfire_grace_time=3600, replace_existing=True)

    # 财报日历: 盘前 08:30 与盘后 17:00 刷新; 失败不覆盖缓存, 状态如实降级。
    def _refresh_earnings():
        from app.services.earnings_calendar import EarningsCalendar
        status = EarningsCalendar(repo.store.data_dir).refresh()
        logger.info("earnings calendar refresh: %s", {k: status.get(k) for k in ("status", "source", "fetched_at", "error")})
        return status

    for job_id, hour, minute in (("zen_earnings_premarket", 8, 30), ("zen_earnings_postclose", 17, 0)):
        scheduler.add_job(_refresh_earnings,
            trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone="America/New_York"),
            id=job_id, max_instances=1, coalesce=True, misfire_grace_time=3600, replace_existing=True)

    # 公司行动: 盘前 08:00 拉取关心标的 (持仓 + 未成交卡) 的窗口, 并立即对持仓入账。
    def _refresh_corporate_actions():
        loop = getattr(_get_app_state(), "trading_loop", None)
        if loop is None:
            return None
        status = loop.refresh_corporate_actions()
        logger.info("corporate actions refresh: %s", {k: status.get(k) for k in ("status", "fetched_at", "error")})
        return status

    scheduler.add_job(_refresh_corporate_actions,
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone="America/New_York"),
        id="zen_corporate_actions", max_instances=1, coalesce=True, misfire_grace_time=3600, replace_existing=True)

    # 启动后一次性补齐保护数据 (缺失/过期才真正拉取), 在调度线程里跑, 不阻塞应用启动
    def _protection_boot_refresh():
        loop = getattr(_get_app_state(), "trading_loop", None)
        if loop is not None:
            loop.ensure_protection_feeds()

    scheduler.add_job(_protection_boot_refresh, id="zen_protection_boot_refresh",
                      max_instances=1, misfire_grace_time=3600, replace_existing=True)

    def _flush_paper_notifications():
        from app.services.notify_opportunity import flush_notifications
        state = _get_app_state()
        if state is not None:
            return flush_notifications(state.repo.store.data_dir, limit=10)

    scheduler.add_job(_flush_paper_notifications, trigger=IntervalTrigger(seconds=60),
        id="zen_notification_outbox", max_instances=1, coalesce=True,
        misfire_grace_time=30, replace_existing=True)

    # ZenTrade 周报 (周日 09:00 ET): 回合统计 + setup 分组 + 降级建议 (docs/architecture.md)
    def _weekly_review():
        try:
            from app.review import weekly_report
            repo = getattr(_get_app_state(), "repo", None) if _get_app_state() else None
            if repo is None:
                return
            import json
            from app.market_clock import us_clock
            from app.services import notify_opportunity
            from app.services import setup_status
            from app.review.review import build_round_trips
            rep = weekly_report(repo.store.data_dir, us_clock.today())
            notify_opportunity.enqueue_weekly_review(repo.store.data_dir, rep)
            logger.info("zen weekly review: %s", {k: v for k, v in rep.items() if k != "by_setup"})
            # 四周负期望自动降级 (只降不升; 恢复只能人工)
            events_path = repo.store.data_dir / "ledger" / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
                      if line.strip()] if events_path.exists() else []
            store = setup_status.SetupStatusStore(repo.store.data_dir)
            degraded = setup_status.run_weekly_evaluation(store, build_round_trips(events), us_clock.today(),
                                                          at=us_clock.now().isoformat())
            if degraded:
                notify_opportunity.enqueue_setup_degraded(repo.store.data_dir, degraded, store.load())
                logger.warning("setup degraded to watch-only: %s", degraded)
        except Exception as e:  # noqa: BLE001
            logger.warning("zen weekly review failed (soft): %s", e)

    scheduler.add_job(
        _weekly_review,
        trigger=CronTrigger(day_of_week="sun", hour=9, minute=0, timezone="America/New_York"),
        id="zen_weekly_review",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 周期性能力集重建: 自定义源配置变更无需重启即可被发现。
    # 只热更新 app.state.capabilities(API 端点、盘后管道 _pipeline_then_refresh 均读它)。
    def _reprobe_capabilities():
        from app.data_providers.capabilities import current_capability_set
        from app.services import preferences as _prefs
        app_state = _get_app_state()
        if app_state is None:
            return
        try:
            old = getattr(app_state, "capabilities", None)
            old_n = len(old.all()) if old else -1
            new_capset = current_capability_set({
                "realtime_data_provider": _prefs.get_realtime_data_provider(),
                "daily_data_provider": _prefs.get_daily_data_provider(),
                "minute_data_provider": _prefs.get_minute_data_provider(),
                "full_minute_data_provider": _prefs.get_full_minute_data_provider(),
                "adj_factor_provider": _prefs.get_adj_factor_provider(),
                "financial_data_provider": _prefs.get_financial_provider(),
            })
            app_state.capabilities = new_capset
            new_n = len(new_capset.all())
            if old_n != new_n:
                logger.warning(
                    "能力集变化: %d → %d capabilities。数据源配置变更已热更新 "
                    "app.state.capabilities。", old_n, new_n,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("周期能力重建失败(保留现有能力集): %s", e)

    scheduler.add_job(
        _reprobe_capabilities,
        trigger=IntervalTrigger(minutes=60),
        id="reprobe_capabilities",
        misfire_grace_time=600,
        replace_existing=True,
    )

    # 运维盯盘: 受管任务运行台账 + 每 3 分钟核对当日应跑清单 / 盘中心跳 (不碰交易环)
    from app.services import ops_watch
    ops_watch.attach_listener(scheduler, repo.store.data_dir)

    def _ops_check():
        from app.market_clock import us_clock as _clk
        if not _clk.is_trading_day(_clk.today()):
            return None
        try:
            return ops_watch.check(repo.store.data_dir, scheduler, getattr(_get_app_state(), "trading_loop", None))
        except Exception as e:
            logger.warning("ops check failed (soft): %s", type(e).__name__)
            return None

    scheduler.add_job(_ops_check, trigger=IntervalTrigger(minutes=3), id="zen_ops_check",
                      max_instances=1, coalesce=True, misfire_grace_time=120, replace_existing=True)

    scheduler.start()
    logger.info("scheduler started; instruments@%02d:%02d ET, pipeline@%02d:%02d ET, paper_tick=15s",
                inst_sched["hour"], inst_sched["minute"], sched["hour"], sched["minute"])
    return scheduler


# app_state 延迟引用(start_scheduler 在 lifespan 早期调用, app.state 可能还没就绪)
_app_state_ref = None


def set_app_state(app_state) -> None:
    """lifespan 注册 app.state 引用, 供 scheduled job 访问 quote_service 等单例。"""
    global _app_state_ref
    _app_state_ref = app_state


def _get_app_state():
    return _app_state_ref


def _get_app_state_attr(name: str):
    """app_state 属性安全读取 (无引用/未初始化返回 None)。"""
    state = _get_app_state()
    return getattr(state, name, None) if state is not None else None


def _set_app_state_attr(name: str, value) -> None:
    state = _get_app_state()
    if state is not None:
        setattr(state, name, value)
