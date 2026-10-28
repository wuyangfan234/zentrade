"""美股常规时段分钟增量落盘服务。

数据源由 full_minute_data_provider 路由到插件/自定义源，不回退退役厂商。
冷启动/真实缺口走全天修复；支持 get_intraday_latest 或 get_intraday_since
的源走稳态尾窗增量。SIP 延迟在缺口判断里显式扣除，收盘后继续收集尾根。
请求串行不重叠，写入持有仓库写锁；状态展示真实有效轮询间隔与数据延迟。
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from app.market_clock import us_clock
from app.services import preferences

logger = logging.getLogger(__name__)

# 轮询间隔允许范围 (秒): 稳态轮单请求无并发脉冲, 下限 3s;
# 上限 120s — universe 端点每标的只回最新 3 根, 间隔超过 3 分钟必留缺口,
# 每轮都会触发修复轮, 稳态设计失效, 故不允许配到 120s 以上。
REFRESH_INTERVAL_MIN = 3
REFRESH_INTERVAL_MAX = 120
# 等待步长 (秒): 循环小步睡眠, 便于快速停止与偏好热生效。
_LOOP_STEP_S = 2.0
# 当日覆盖滞后超过该分钟数 (≈ universe 单请求 3 根余量) → 触发全天修复轮。
_REPAIR_LAG_MINUTES = 3.0
# 连续空轮达到该次数 → 强制全天修复轮 (自愈 universe 端点持续异常)。
_EMPTY_ROUNDS_TO_REPAIR = 2
# 仅修复轮的自定义源 (无 get_intraday_latest 廉价增量): 全天批量节奏下限,
# 6s 一轮全市场拉取会把绝大多数源打爆。
_REPAIR_ONLY_MIN_INTERVAL_S = 60


def _in_continuous_session(now=None) -> bool:
    """美东常规交易时段 (09:30-16:00), 仅交易日。"""
    return us_clock.in_session(now)


@dataclass
class _RefreshState:
    """服务运行状态 (status() 的内存镜像, 循环线程内更新)。"""

    rounds: int = 0
    last_round_at: float | None = None      # epoch 秒
    last_round_ms: float | None = None      # 单轮耗时
    last_rows: int = 0                      # 上轮写入行数 (合并后)
    last_symbols: int = 0                   # 上轮覆盖标的数
    last_requests: int = 0                  # 上轮请求数 (增量恒 1, 修复=分块数+重试)
    last_mode: str | None = None            # 上轮模式: "increment" / "full"
    last_error: str | None = None
    next_round_at: float | None = None      # epoch 秒
    extra: dict[str, Any] = field(default_factory=dict)


class MinuteRefreshService:
    """盘中分钟增量刷新: 单实例挂 app.state.minute_refresh, 后台守护线程。"""

    def __init__(self, repo) -> None:
        self._repo = repo
        self._app_state: Any | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = _RefreshState()
        self._round_lock = threading.Lock()  # 同时只允许一轮 (手动触发与定时轮互斥)
        self._empty_rounds = 0               # 连续空轮计数 (escalate 到全天修复)
        self._repaired_scope = None
        self._finalized_date = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def set_repo(self, repo) -> None:
        self._repo = repo
        self._repaired_scope = None
        self._finalized_date = None

    def set_app_state(self, app_state: Any) -> None:
        self._app_state = app_state

    def start(self) -> bool:
        """启动后台线程 (幂等)。开关/时段/能力判断都在循环内每轮做, 热生效。"""
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="minute-refresh", daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # 门控
    # ------------------------------------------------------------------

    def capability_ok(self) -> bool:
        """Cap.INTRADAY_UNIVERSE (全量分钟) 存在。能力探测结果缓存在 app.state。

        门控挂在稳态增量的主能力上; TickFlow 路径全天修复轮用的 intraday.batch
        与其同属 Expert 档 (tiers.yaml), 两者必然同时持有。自定义源声明
        full_minute 数据集且被路由时, 由 policy._augment_custom_sources 补授
        同一能力键 — 门控口径对两类源统一。
        """
        capset = getattr(self._app_state, "capabilities", None) if self._app_state else None
        if capset is None:
            return False
        try:
            from app.data_providers.models import Cap

            return capset.has(Cap.INTRADAY_UNIVERSE)
        except Exception:
            return False

    def active_provider(self) -> str:
        """全量分钟生效源名 (自定义源名, 偏好 full_minute_data_provider; 空=未配置)。"""
        try:
            return preferences.get_full_minute_data_provider()
        except Exception:  # noqa: BLE001 — 偏好文件异常按未配置处理
            return ""

    def _resolve_custom(self) -> tuple[object | None, str]:
        """解析自定义源。返回 (provider_or_None, effective_name):

        - 未配置 / 源未声明 full_minute 数据集 / 解析异常 → (None, "")
          (能力门控决定能否真正运行; 厂商降级路径已退役)
        - 成功 → (provider, name)
        """
        from app.services import kline_sync

        name = self.active_provider()
        if not name:
            return (None, "")
        provider, fallback, err = kline_sync._resolve_full_minute_provider(name)
        if fallback:
            if err is not None:
                logger.warning(
                    "full_minute provider %s 解析失败, 本轮无数据源: %s", name, err,
                )
            return (None, "")
        return (provider, name)

    def _custom_supports_increment(self, provider: object) -> bool:
        """自定义源是否实现 get_intraday_latest (稳态增量); 否则仅修复轮。"""
        return any(callable(getattr(provider, method, None)) for method in ("get_intraday_latest", "get_intraday_since"))

    def repair_only(self) -> bool:
        """当前生效源只能全天修复轮 (无廉价增量端点) — 节奏下限抬到 60s。"""
        provider, name = self._resolve_custom()
        return provider is not None and not self._custom_supports_increment(provider)

    def _effective_interval(self) -> int:
        """本轮间隔: 偏好值; 仅修复轮的自定义源下限 60s (全天批量打不住 6s 节奏)。"""
        interval = preferences.get_minute_refresh_interval()
        provider, _ = self._resolve_custom()
        floor = getattr(provider, "minute_refresh_interval_seconds", 0)
        if isinstance(floor, (int, float)):
            interval = max(interval, int(floor))
        if self.repair_only():
            return max(interval, _REPAIR_ONLY_MIN_INTERVAL_S)
        return interval

    def _provider_delay(self) -> int:
        from app.services.kline_sync import minute_delay_minutes
        provider, _ = self._resolve_custom()
        return minute_delay_minutes(provider) if provider is not None else 0

    def _delayed_close_window(self) -> bool:
        # 不按固定墙钟硬停。慢轮可能越过16:18且请求仍截止15:57；
        # 只有启动于收盘+源延迟之后的一轮成功，才完成当日尾段收集。
        now = us_clock.now()
        close = datetime.combine(now.date(), us_clock.session_close(now.date()), tzinfo=us_clock.tz)
        return self._provider_delay() > 0 and close <= now and self._finalized_date != now.date() and us_clock.is_trading_day(now.date())

    def _gate_reason(self) -> str | None:
        """返回本轮不执行的原因 (None = 放行)。"""
        if not preferences.get_minute_refresh_enabled():
            return "disabled"
        if not self.capability_ok():
            return "capability"
        if not _in_continuous_session() and not self._delayed_close_window():
            return "outside_trading_hours"
        # 节假日 (工作日但休市): 周几门控覆盖不到, 由交易日探针剔除。
        # 未知 (None) 维持现状 — 空轮升级机制兜底 (探针失灵时的第二道防线)。
        from app.services import trading_day

        if trading_day.is_trading_day() is False:
            return "holiday"
        return None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                reason = self._gate_reason()
                if reason is None:
                    interval = self._effective_interval()
                    started = time.time()
                    self._run_round()
                    # 固定节奏: 下一轮 = max(本轮起点+间隔, 本轮完成), 不补跑
                    finish = time.time()
                    self._state.next_round_at = max(started + interval, finish)
                    # 等到下一轮 (小步睡眠保持可停/偏好热切换)
                    while not self._stop.is_set():
                        now = time.time()
                        gate = self._gate_reason()
                        if gate is not None:
                            self._state.next_round_at = None
                            break  # 门控关闭 → 回外层等待重评估
                        if now >= self._state.next_round_at:
                            break
                        self._stop.wait(min(_LOOP_STEP_S, max(0.0, self._state.next_round_at - now)))
                    continue
            except Exception as e:
                self._state.last_error = f"round failed: {e}"
                logger.warning("全量分钟轮次异常: %s", e)
            self._stop.wait(_LOOP_STEP_S)

    # ------------------------------------------------------------------
    # 单轮
    # ------------------------------------------------------------------

    def _today_coverage_lag_minutes(self) -> float | None:
        """当日分区最新K距现在的分钟数; None = 当日无数据。

        覆盖度探测只读当日分区文件 (毫秒级); 任何异常按无数据处理 →
        本轮走全天修复, 不会因探测失败而丢增量。
        """
        with contextlib.suppress(Exception):
            part = (
                self._repo.store.data_dir / "kline_minute"
                / f"date={us_clock.today().isoformat()}" / "part.parquet"
            )
            if not part.exists():
                return None
            from app.services.kline_sync import _filter_regular_session
            mx = _filter_regular_session(pl.read_parquet(part, columns=["datetime"]))["datetime"].max()
            if mx is None:
                return None
            # 分区 datetime 是美东墙钟 naive，与美东当前时间比较。
            return (us_clock.now().replace(tzinfo=None) - mx).total_seconds() / 60
        return None

    def _select_mode(self) -> str:
        """选轮次模式: 稳态增量 (universe 单请求) vs 全天修复 (burst 脉冲)。

        当日已有数据且覆盖滞后 ≤ _REPAIR_LAG_MINUTES (≈ 3 根余量) → 增量;
        冷启动 / 断档超阈值 / 连续空轮 → 全天修复。
        """
        if self._empty_rounds >= _EMPTY_ROUNDS_TO_REPAIR:
            return "full"
        lag = self._today_coverage_lag_minutes()
        if lag is None:
            return "full"
        provider, _ = self._resolve_custom()
        if callable(getattr(provider, "get_intraday_since", None)) and not callable(getattr(provider, "get_intraday_latest", None)):
            # 可取任意尾窗的源无需受 latest(3根) 限制。冷启动全池耗时再长，
            # 下一轮也只补落后的尾段，避免修复耗时本身触发永无止境的全天重拉。
            return "increment"
        if lag > _REPAIR_LAG_MINUTES + self._provider_delay():
            return "full"
        return "increment"

    def _run_round(self) -> None:
        from app.services import kline_sync

        t0 = time.perf_counter()
        custom, provider_name = self._resolve_custom()
        if custom is None:
            # 厂商拉取路径已退役 (P1-1b2): 未配置 full_minute 自定义源 → 空转, 不取数
            self._state.last_error = "no full_minute provider configured"
            logger.info("全量分钟本轮跳过: 未配置自定义数据源 (厂商路径已退役)")
            return
        symbols = self._universe()
        scope = (provider_name, us_clock.today(), tuple(symbols))
        mode = "full" if self._repaired_scope != scope else self._select_mode()
        requested_at = us_clock.now()
        if mode == "increment" and not self._custom_supports_increment(custom):
            # 无廉价增量端点的源: 增量轮退化为修复轮 (全天批量幂等覆盖, 数据不丢)
            mode = "full"
        mode_label = "增量" if mode == "increment" else "全天修复"
        with self._round_lock:
            if mode == "increment":
                # fetch 计时只覆盖网络取数; full 分支的 universe 维表读取不计入
                fetch_started = time.perf_counter()
                if callable(getattr(custom, "get_intraday_latest", None)):
                    latest = kline_sync.fetch_intraday_custom_latest(custom, provider_name)
                    df, requests = latest if latest is not None else (pl.DataFrame(), 0)
                else:
                    # 按已落盘最新分钟回退一根重叠更新；不把端点延迟当成 16 分钟缺口。
                    now = us_clock.now()
                    lag = self._today_coverage_lag_minutes()
                    start = now - timedelta(minutes=(lag if lag is not None else self._provider_delay()) + 1)
                    open_at = datetime.combine(now.date(), us_clock.sessions[0].open, tzinfo=us_clock.tz)
                    df, requests = kline_sync.fetch_intraday_custom_since(
                        custom, provider_name, self._universe(), max(open_at, start),
                    )
                self._state.last_symbols = (
                    df["symbol"].n_unique() if not df.is_empty() else 0
                )
            else:
                symbols = self._universe()
                self._state.last_symbols = len(symbols)
                if not symbols:
                    self._state.last_error = "empty universe (instruments 未加载)"
                    logger.warning("全量分钟[%s] 本轮中止: 标的池为空 (instruments 未加载)", mode_label)
                    return
                fetch_started = time.perf_counter()
                df, requests = kline_sync.fetch_intraday_custom_batch(
                    custom, provider_name, symbols,
                )
            fetch_ms = (time.perf_counter() - fetch_started) * 1000
            self._state.last_requests = requests
            if df.is_empty():
                self._empty_rounds += 1
                self._state.last_error = f"intraday {mode} returned no data"
                logger.warning(
                    "全量分钟[%s] 本轮返回空数据: %d 请求, 取数 %.0fms",
                    mode_label, requests, fetch_ms,
                )
                return
            self._empty_rounds = 0
            write_started = time.perf_counter()
            with getattr(self._repo, "_write_lock", contextlib.nullcontext()):
                written = kline_sync._write_minute_partition(
                    df, self._repo.store.data_dir / "kline_minute",
                )
            write_ms = (time.perf_counter() - write_started) * 1000

        if mode == "full":
            self._repaired_scope = scope
        close = datetime.combine(requested_at.date(), us_clock.session_close(requested_at.date()), tzinfo=us_clock.tz)
        if requested_at >= close + timedelta(minutes=self._provider_delay()):
            self._finalized_date = requested_at.date()
        self._state.rounds += 1
        self._state.last_round_at = time.time()
        self._state.last_round_ms = (time.perf_counter() - t0) * 1000
        self._state.last_rows = written
        self._state.last_mode = mode
        self._state.last_error = None
        logger.info(
            "全量分钟[%s] 第 %d 轮: 取数 %.0fms (%d 请求, %d 标的), "
            "落盘 %.0fms (%d 行), 总计 %.0fms",
            mode_label, self._state.rounds, fetch_ms, self._state.last_requests,
            self._state.last_symbols, write_ms, written, self._state.last_round_ms,
        )

    def _universe(self) -> list[str]:
        """美股标的维表，与盘后分钟同步同一来源。"""
        inst = self._repo.get_instruments()
        if inst.is_empty() or "symbol" not in inst.columns:
            return []
        return inst["symbol"].cast(pl.Utf8).unique().sort().to_list()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def is_healthy(self) -> bool:
        """读侧 freshness 判定: 本地 kline_minute 分区是否正被服务持续写入。

        条件: 偏好开启 + 轮询线程存活 + 最近一轮距现在 ≤ max(2*间隔, 30s)。
        健康时消费方 (自选分时等) 可信任本地分区、跳过批量补拉;
        连续失败轮不更新 last_round_at → 自动超时判不健康, 无需单独盯 last_error。
        """
        try:
            if not preferences.get_minute_refresh_enabled():
                return False
        except Exception:  # noqa: BLE001 — 偏好文件异常按不健康处理
            return False
        if self._thread is None or not self._thread.is_alive():
            return False
        last = self._state.last_round_at
        if last is None:
            return False
        interval = self._effective_interval()
        return (time.time() - float(last)) <= max(2.0 * interval, 30.0)

    def status(self) -> dict[str, Any]:
        import contextlib

        enabled = False
        with contextlib.suppress(Exception):
            enabled = preferences.get_minute_refresh_enabled()
        running = self._thread is not None and self._thread.is_alive()
        gate = self._gate_reason()
        return {
            "enabled": enabled,
            "running": running,
            "healthy": self.is_healthy(),
            "provider": self.active_provider(),
            "provider_effective": self._resolve_custom()[1],
            "repair_only": self.repair_only(),
            "interval_seconds": self._effective_interval(),
            "configured_interval_seconds": preferences.get_minute_refresh_interval(),
            "data_delay_minutes": self._provider_delay(),
            "capability_ok": self.capability_ok(),
            "in_trading_hours": _in_continuous_session(),
            "gate_reason": gate if (enabled and running) else (gate or "disabled"),
            "rounds": self._state.rounds,
            "last_round_at": self._state.last_round_at,
            "last_round_ms": self._state.last_round_ms,
            "last_rows": self._state.last_rows,
            "last_symbols": self._state.last_symbols,
            "last_requests": self._state.last_requests,
            "last_mode": self._state.last_mode,
            "next_round_at": self._state.next_round_at,
            "last_error": self._state.last_error,
        }
