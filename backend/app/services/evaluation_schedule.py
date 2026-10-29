"""决策台证据周更: 每周六 10:00 ET 用统一口径重新评估全部策略 (参考部署实测 2 个策略 1.7 秒、峰值 328 MB)。

- 窗口: 2022-01-13 ~ 上周五 (留一周让最近信号走完 7 日纪律, 不把未走完的交易算进去)
- 持有共享重任务槽 (与回测/挖掘互斥), 交易时段内不跑
- S1/S2 的分钟级真实规则重放结果保持不变 (另行重跑)
- 每次运行追加 data/decision_desk/evaluation_runs.jsonl; 权重在下一次盘后扫描时按周限幅更新
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

from app.market_clock import us_clock
from app.services import strategy_evaluation_runner as runner

logger = logging.getLogger(__name__)

WINDOW_START = date(2022, 1, 13)
SCHEDULE = {"day_of_week": "sat", "hour": 10, "minute": 0}


def window(today: date) -> tuple[date, date]:
    """结束日 = 上周五再往前一周: 信号日之后最多 5 天入场窗 + 7 天持有, 需要约 2 周未来数据才走得完。"""
    last_friday = today - timedelta(days=(today.weekday() - 4) % 7 or 7)
    return WINDOW_START, last_friday - timedelta(days=7)


def _log(data_dir: Path, entry: dict) -> None:
    p = Path(data_dir) / "decision_desk" / "evaluation_runs.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run(app_state) -> dict:
    now = us_clock.now()
    repo = app_state.repo
    data_dir = Path(repo.store.data_dir)
    if us_clock.in_session(now):
        out = {"status": "skipped", "reason": "交易时段内不运行评估", "at": now.isoformat()}
        _log(data_dir, out)
        return out
    start, end = window(now.date())
    t0 = time.perf_counter()
    try:
        from app.services.heavy_job_limiter import shared_heavy_job_limiter
        with shared_heavy_job_limiter.slot("exclusive"):
            payload = runner.run_all(data_dir, start, end, repo=repo, engine=getattr(app_state, "strategy_engine", None))
        out = {"status": "succeeded", "window": [str(start), str(end)], "evaluated": len(payload.get("evaluated", [])),
               "errors": [k for k, v in payload.get("sources", {}).items() if isinstance(v, dict) and v.get("error")],
               "elapsed_s": round(time.perf_counter() - t0, 1), "at": now.isoformat()}
        # v2 新口径并行评估 (2026-10-05 拍板: 新旧并行核对, 确认后再切换)。失败不影响 v1。
        out["v2"] = run_v2(app_state, start, end)
        # v3.1（随机基线/留出期/退市股/多重检验）：失败不影响 v1/v2。内存不足时可由
        # 外部周度计算复用同一生产代码并原子写回；这里有内存守卫。
        out["v3"] = run_v3(app_state, start, end)
    except Exception as e:  # noqa: BLE001
        logger.exception("weekly strategy evaluation failed")
        out = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300], "at": now.isoformat()}
    _log(data_dir, out)
    logger.info("weekly strategy evaluation: %s", out)
    return out


_v2_state: dict = {"running": False, "last": None}


def run_v2(app_state, start=None, end=None) -> dict:
    """v2 评估 (写 evaluation_v2.json 并行文件, 不改许可)。交易时段内不跑; 与回测/挖掘共用重任务槽。"""
    now = us_clock.now()
    repo = app_state.repo
    data_dir = Path(repo.store.data_dir)
    if us_clock.in_session(now):
        return {"status": "skipped", "reason": "交易时段内不运行评估"}
    if start is None or end is None:
        start, end = window(now.date())
    if _v2_state["running"]:
        return {"status": "skipped", "reason": "已有一次 v2 评估在运行"}
    _v2_state["running"] = True
    t0 = time.perf_counter()
    try:
        from app.services.heavy_job_limiter import shared_heavy_job_limiter
        with shared_heavy_job_limiter.slot("exclusive"):
            payload = runner.run_all_v2(data_dir, start, end, repo=repo, engine=getattr(app_state, "strategy_engine", None))
        out = {"status": "succeeded", "window": [str(start), str(end)], "evaluated": len(payload.get("evaluated", [])),
               "permission_changes": sorted(k for k, v in (payload.get("comparison") or {}).items() if v.get("changes_permission")),
               "elapsed_s": round(time.perf_counter() - t0, 1), "at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        logger.exception("weekly strategy evaluation v2 failed")
        out = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300], "at": now.isoformat()}
    finally:
        _v2_state["running"] = False
    _v2_state["last"] = out
    _log(data_dir, {"v2": True, **out})
    return out


def v2_status() -> dict:
    return dict(_v2_state)


def last_runs(data_dir: Path, n: int = 5) -> list[dict]:
    p = Path(data_dir) / "decision_desk" / "evaluation_runs.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return rows[-n:]


_v3_state: dict = {"running": False, "last": None, "stage": None}


def run_v3(app_state, start=None, end=None, *, refresh: bool = True) -> dict:
    """v3.1 评估: 刷新研究数据 (退市股/财报/SEC) → 重建研究数据集 → 评估全部策略 + 事件来源 → evaluation_v3.json。
    只写研究目录与并行证据文件; 许可是否改用 v3 由 evidence_mode 决定。交易时段不跑, 与回测/挖掘共用重任务槽。"""
    from app.services import research_data
    from app.services import strategy_evaluation_v3 as v3
    now = us_clock.now()
    repo = app_state.repo
    data_dir = Path(repo.store.data_dir)
    if us_clock.in_session(now):
        return {"status": "skipped", "reason": "交易时段内不运行评估"}
    if start is None or end is None:
        start, end = window(now.date())
    if _v3_state["running"]:
        return {"status": "skipped", "reason": "已有一次 v3 评估在运行"}
    _v3_state.update(running=True, stage="data")
    t0 = time.perf_counter()
    notes = {}
    try:
        avail = _available_memory_mb()
        if avail is not None and avail < V3_MIN_FREE_MB:
            raise RuntimeError(f"可用内存 {avail} MB < {V3_MIN_FREE_MB} MB，本次 v3 评估跳过（保护交易进程；"
                               "由本机周更脚本用同一生产代码计算后写回）")
        from app.services.heavy_job_limiter import shared_heavy_job_limiter
        if refresh:
            for name, fn in (("delisted", lambda: research_data.refresh_delisted(data_dir)),
                             ("events", lambda: research_data.refresh_events(data_dir, today=now.date()))):
                try:
                    notes[name] = fn()
                except Exception as e:  # noqa: BLE001
                    logger.warning("v3 research data %s failed (soft): %s", name, e)
                    notes[name] = {"status": "failed", "error": f"{type(e).__name__}"}
        with shared_heavy_job_limiter.slot("exclusive"):
            _v3_state["stage"] = "dataset"
            try:
                notes["dataset"] = research_data.build_v3_dataset(data_dir)
            except Exception as e:  # noqa: BLE001
                logger.warning("v3 dataset build failed (soft): %s", e)
                notes["dataset"] = {"status": "failed", "error": f"{type(e).__name__}"}
            eval_dir = research_data.v3_data_dir(data_dir)
            with_delisted = (eval_dir / "kline_daily_enriched").exists() and notes["dataset"].get("status") == "ok"
            if not with_delisted:
                eval_dir = data_dir
            _v3_state["stage"] = "evaluate"
            ev_dir = research_data.events_dir(data_dir)
            payload = _run_v3_isolated(eval_dir, start, end, ev_dir, v3.evaluation_v3_path(data_dir), data_dir)
        payload_note = {"with_delisted": with_delisted, "research": notes}
        _annotate(v3.evaluation_v3_path(data_dir), payload_note)
        out = {"status": "succeeded", "window": [str(start), str(end)], "evaluated": len(payload.get("evaluated", [])),
               "proven": sorted(k for k, v in payload["sources"].items() if isinstance(v, dict) and v.get("tier_v3") == "proven"),
               "with_delisted": with_delisted, "elapsed_s": round(time.perf_counter() - t0, 1), "at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        logger.exception("strategy evaluation v3 failed")
        out = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300], "at": now.isoformat()}
    finally:
        _v3_state.update(running=False, stage=None)
    _v3_state["last"] = out
    _log(data_dir, {"v3": True, **out})
    return out


V3_MIN_FREE_MB = 3200       # 本地实测峰值约 2.6 GB (全市场 1267 天 × 11044 只矩阵), 留 600 MB 余量
V3_TIMEOUT_S = 3 * 3600


def _available_memory_mb() -> int | None:
    """容器/宿主可用内存 (MemAvailable)。读不到返回 None (不据此拦截, 由子进程隔离兜底)。"""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def _v3_child(eval_dir: str, start: str, end: str, ev_dir: str, out_path: str, hyp_dir: str, q) -> None:
    """子进程入口: 全市场矩阵只在子进程里分配; 被 OOM 杀掉也只是这个子进程, 交易进程不受影响。"""
    try:
        from app.config import settings
        settings.data_dir = Path(eval_dir)
        from app.services import strategy_evaluation_v3 as v3
        ev = Path(ev_dir)
        p = v3.run_v3(Path(eval_dir), date.fromisoformat(start), date.fromisoformat(end), engine=None,
                      earn_dir=ev / "earnings", sec_dir=ev / "sec", out_path=Path(out_path),
                      hypothesis_dir=Path(hyp_dir))
        q.put({"ok": True, "evaluated": p.get("evaluated", []),
               "proven": sorted(k for k, v in p["sources"].items() if isinstance(v, dict) and v.get("tier_v3") == "proven")})
    except BaseException as e:  # noqa: BLE001
        q.put({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})


def _run_v3_isolated(eval_dir: Path, start: date, end: date, ev_dir: Path, out_path: Path, hyp_dir: Path) -> dict:
    import multiprocessing as mp
    import queue as _queue
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_v3_child, args=(str(eval_dir), str(start), str(end), str(ev_dir), str(out_path),
                                               str(hyp_dir), q), daemon=True, name="evaluation-v3-child")
    proc.start()
    msg = None
    deadline = time.monotonic() + V3_TIMEOUT_S
    while msg is None and time.monotonic() < deadline:
        try:
            msg = q.get(timeout=5)
        except _queue.Empty:
            if not proc.is_alive():
                break
    proc.join(timeout=30)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=10)
    if msg is None:
        raise RuntimeError(f"v3 子进程异常退出 (exitcode={proc.exitcode}; -9 通常为内存不足被系统终止)")
    if not msg.get("ok"):
        raise RuntimeError(msg.get("error") or "v3 子进程失败")
    return {"evaluated": msg["evaluated"], "sources": {k: {"tier_v3": "proven"} for k in msg["proven"]}}


def _annotate(path: Path, extra: dict) -> None:
    from app.services.fs_utils import atomic_write_text
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        d.update(extra)
        atomic_write_text(path, json.dumps(d, ensure_ascii=False))
    except (OSError, ValueError) as e:
        logger.warning("v3 annotate failed: %s", e)


def v3_status() -> dict:
    return dict(_v3_state)
