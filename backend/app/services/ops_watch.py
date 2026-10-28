# ruff: noqa: RUF001
"""运维盯盘 (2026-10-12): 定时任务运行台账 + 当日应跑清单 + 盘中心跳报警。

- 运行台账: APScheduler 监听器把受管任务的每次执行/失败/错过追加到 ops/jobs_run.jsonl。
  调度器本身仍是内存任务, 不做持久化 (任务大多是闭包, 序列化代价大); 重启后错过的任务靠清单发现。
- 当日应跑清单: 交易日里, 取每个受管 Cron 任务今天的触发时刻, 过了宽限仍无成功记录即"缺跑";
  幂等任务在允许的时间窗内补跑一次, 其余只报警。
- 盘中心跳: 交易环超过 3 分钟没运行、执行池有标的但报价超过 3 分钟没更新、交易环报错 → 推飞书;
  恢复时再推一次。同一原因每天最多报 5 轮。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, time, timedelta
from pathlib import Path

from app.market_clock import us_clock
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# 受管任务: 每天按 Cron 触发、错过会影响当天证据的任务。秒级轮询任务不进台账。
WATCHED = {
    "pre_market_instruments": "盘前标的维表",
    "zen_corporate_actions": "公司行动",
    "zen_earnings_premarket": "财报日历（盘前）",
    "zen_market_mood": "盘前大盘情绪",
    "zen_qveris_estimates": "QVeris 财报预期",
    "zen_premarket_gap": "盘前跳空预警",
    "zen_swing_pm_check": "盘前越止损检查",
    "zen_intraday_strength": "盘中强势卡",
    "zen_intraday_strength_settle": "盘中强势卡收盘结算",
    "zen_open_radar": "开盘放量榜",
    "zen_open_radar_settle": "开盘放量榜收盘记录",
    "zen_market_mood_score": "大盘情绪记分",
    "daily_pipeline": "盘后管道",
    "zen_earnings_postclose": "财报日历（盘后）",
    "zen_market_mood_score_retry": "大盘情绪补记分",
    "zen_fundamentals_sync": "美股财务同步",
    "zen_resonance_settle": "共振影子账本结算",
}
# 可补跑的幂等任务 → 最晚补跑时刻 (ET); None 表示当天任何时候都可补。
# daily_pipeline 有自己的开机补跑, 不在这里重复。
CATCHUP = {
    "zen_corporate_actions": None,
    "zen_earnings_premarket": None,
    "zen_earnings_postclose": None,
    "zen_market_mood": time(9, 30),        # 开盘后再生成就不是"盘前"判断了
    "zen_premarket_gap": time(9, 30),
    "zen_swing_pm_check": time(15, 30),    # 幂等 (每张卡只查一次); 交易环每轮也会补查
    "zen_intraday_strength": None,         # 补跑只结算在途卡 (过了检查点 10 分钟不补出卡), 幂等
    "zen_intraday_strength_settle": None,
    "zen_open_radar": time(10, 30),        # 幂等 (当天已出榜跳过); 过了 10:30 再出开盘榜就不是“开盘”了
    "zen_open_radar_settle": None,
    "zen_qveris_estimates": None,
    "zen_market_mood_score": None,
    "zen_market_mood_score_retry": None,
    "zen_fundamentals_sync": None,         # 合并写、失败保留旧数据, 幂等
    "zen_resonance_settle": None,          # 只补缺的格子, 幂等
}
MISS_GRACE = timedelta(minutes=20)
LOOP_STALE = timedelta(minutes=3)
QUOTE_STALE = timedelta(minutes=3)
BOOT_GRACE = timedelta(minutes=3)       # 开盘头 3 分钟交易环可能还没跑完第一轮
MAX_ALERTS_PER_REASON = 5
# 0.7.1: 正在跑的受管任务 (内存; 进程重启自然清空), 超过 TTL 视为遗留标记失效
RUNNING_TTL = timedelta(hours=3)
REASON_LABEL = {
    "loop_not_running": "交易环没有运行",
    "loop_error": "交易环报错",
    "quotes_stale": "执行报价断流",
}
_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()
_RUNNING: dict[str, datetime] = {}
_CATCHUP_THREADS: dict[str, threading.Thread] = {}


def mark_running(job_id: str, since: datetime | None = None) -> None:
    with _RUN_LOCK:
        _RUNNING[job_id] = since or us_clock.now()


def clear_running(job_id: str) -> None:
    with _RUN_LOCK:
        _RUNNING.pop(job_id, None)


def is_running(job_id: str, now: datetime | None = None) -> bool:
    now = now or us_clock.now()
    with _RUN_LOCK:
        since = _RUNNING.get(job_id)
        if since is None:
            return False
        if now - since > RUNNING_TTL:
            _RUNNING.pop(job_id, None)
            return False
        return True


def wait_catchups(timeout: float | None = None) -> None:
    """测试/关停用: 等待在途补跑线程结束。"""
    for t in list(_CATCHUP_THREADS.values()):
        t.join(timeout)


def _root(data_dir) -> Path:
    p = Path(data_dir) / "ops"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── 运行台账 ─────────────────────────────────────────────────────────

def record_run(data_dir, job_id: str, status: str, *, scheduled: datetime | None = None,
               error: str | None = None, now: datetime | None = None) -> dict:
    now = now or us_clock.now()
    row = {"job": job_id, "status": status, "at": now.isoformat(timespec="seconds"),
           "scheduled": scheduled.astimezone(us_clock.tz).isoformat(timespec="seconds") if scheduled else None}
    if error:
        row["error"] = error[:200]
    with _LOCK, (_root(data_dir) / "jobs_run.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def load_runs(data_dir, day) -> list[dict]:
    f = _root(data_dir) / "jobs_run.jsonl"
    if not f.exists():
        return []
    prefix = day.isoformat()
    out = []
    with f.open(encoding="utf-8") as fh:
        for line in fh:
            if prefix not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("scheduled") or r.get("at") or "")[:10] == prefix:
                out.append(r)
    return out


def attach_listener(scheduler, data_dir) -> None:
    """一个监听器覆盖全部受管任务, 不用逐个包装任务函数。"""
    from apscheduler.events import (
        EVENT_JOB_ERROR,
        EVENT_JOB_EXECUTED,
        EVENT_JOB_MISSED,
        EVENT_JOB_SUBMITTED,
    )

    def _on(event):
        if event.job_id not in WATCHED:
            return
        if event.code == EVENT_JOB_SUBMITTED:
            srt = getattr(event, "scheduled_run_times", None) or []
            mark_running(event.job_id, srt[0].astimezone(us_clock.tz) if srt and srt[0] else None)
            return
        if event.code != EVENT_JOB_MISSED:
            clear_running(event.job_id)
        exc = getattr(event, "exception", None)
        if event.code == EVENT_JOB_MISSED:
            status = "missed"
        elif exc is not None:
            # 复审 (审计 M-5): 定时管道被单飞跳过 = skipped (不是故障), 错误详情写明原因
            status = "skipped" if type(exc).__name__ == "PipelineNotRunError" and str(exc).startswith("skipped") else "error"
        else:
            status = "ok"
        try:
            err = None
            if exc is not None:
                err = f"{type(exc).__name__}: {exc}" if type(exc).__name__ == "PipelineNotRunError" else type(exc).__name__
            record_run(data_dir, event.job_id, status, scheduled=getattr(event, "scheduled_run_time", None), error=err)
        except Exception as e:
            logger.warning("ops: record_run failed: %s", type(e).__name__)

    scheduler.add_listener(_on, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_SUBMITTED)


# ── 当日应跑清单 ─────────────────────────────────────────────────────

def due_today(scheduler, now: datetime) -> list[tuple[str, datetime]]:
    """受管 Cron 任务今天的第一个触发时刻 (只取 <= now 的)。非交易日返回空。"""
    if not us_clock.is_trading_day(now.date()):
        return []
    day_start = datetime.combine(now.date(), time(0), tzinfo=us_clock.tz)
    out = []
    for job_id in WATCHED:
        job = scheduler.get_job(job_id) if scheduler is not None else None
        trig = getattr(job, "trigger", None)
        if trig is None or type(trig).__name__ != "CronTrigger":
            continue
        fire = trig.get_next_fire_time(None, day_start)
        if fire is None:
            continue
        fire = fire.astimezone(us_clock.tz)
        if fire.date() == now.date() and fire <= now:
            out.append((job_id, fire))
    return sorted(out, key=lambda x: x[1])


SKIP_GRACE = timedelta(minutes=30)


def _within(row: dict, now: datetime, span: timedelta) -> bool:
    try:
        at = datetime.fromisoformat(str(row.get("at")))
    except ValueError:
        return False
    return now - at.astimezone(us_clock.tz) < span


def _job_active() -> bool:
    try:
        from app.services.pipeline_jobs import job_store
        return job_store.active_id() is not None
    except Exception:
        return False


def checklist(data_dir, scheduler, now: datetime | None = None) -> list[dict]:
    now = now or us_clock.now()
    runs = load_runs(data_dir, now.date())
    out = []
    for job_id, fire in due_today(scheduler, now):
        mine = [r for r in runs if r["job"] == job_id]
        ok = [r for r in mine if r["status"] in ("ok", "catchup_ok")]
        if ok:
            state = "ok"
        elif is_running(job_id, now):
            state = "running"          # 正点任务或补跑还在跑, 不判缺跑
        elif mine and mine[-1]["status"] == "skipped" and (_job_active() or _within(mine[-1], now, SKIP_GRACE)):
            state = "running"          # 被单飞跳过: 占位任务还在跑, 或刚结束 (失败重试 15 分钟内接手), 不急着告警
        elif any(r["status"] in ("error", "catchup_error", "skipped") for r in mine):
            state = "error"
        elif now - fire < MISS_GRACE:
            state = "pending"
        else:
            state = "missing"
        out.append({"job": job_id, "label": WATCHED[job_id], "due": fire.isoformat(timespec="minutes"),
                    "state": state, "last": mine[-1] if mine else None})
    return out


def _run_catchup(data_dir, jid: str, job) -> None:
    try:
        job.func(*job.args, **job.kwargs)
        record_run(data_dir, jid, "catchup_ok", now=us_clock.now())
    except Exception as e:
        record_run(data_dir, jid, "catchup_error", error=type(e).__name__, now=us_clock.now())
    finally:
        clear_running(jid)


def _catchup(data_dir, scheduler, items: list[dict], now: datetime) -> list[str]:
    """缺跑的幂等任务补跑一次 (同一任务同一天只试一次)。
    0.7.1: 在独立线程里跑, 不阻塞盯盘/心跳; 起跑前先记 catchup_start 并标 running, 同一任务不并发。"""
    done = []
    runs = load_runs(data_dir, now.date())
    for it in items:
        jid = it["job"]
        if it["state"] != "missing" or jid not in CATCHUP:
            continue
        latest = CATCHUP[jid]
        if latest is not None and now.time() > latest:
            continue
        if any(r["job"] == jid and r["status"].startswith("catchup") for r in runs):
            continue
        if is_running(jid, now):
            continue
        job = scheduler.get_job(jid)
        if job is None:
            continue
        logger.warning("ops: %s 今天 %s 应跑未跑, 补跑一次", jid, it["due"][11:16])
        mark_running(jid, now)
        record_run(data_dir, jid, "catchup_start", now=now)
        t = threading.Thread(target=_run_catchup, args=(data_dir, jid, job), name=f"ops-catchup-{jid}", daemon=True)
        _CATCHUP_THREADS[jid] = t
        t.start()
        it["state"] = "running"
        done.append(jid)
    return done


# ── 盘中心跳 ─────────────────────────────────────────────────────────

_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s]*")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_KV_SECRET = re.compile(r"(?i)\b([\w-]*(?:api[_-]?key|apikey|token|secret|password|passwd|signature|access[_-]?key)[\w-]*)"
                        r"(\s*[=:]\s*)(?!\*\*\*)[^\s&,;'\"]+")


def redact(text: str) -> str:
    """0.7.1: 推送前给报错原文脱敏 (URL 查询串、Bearer、key/token/secret 样式的值)。"""
    t = _URL_QUERY.sub(r"\1?***", str(text))
    t = _BEARER.sub(r"\1 ***", t)
    return _KV_SECRET.sub(r"\1\2***", t)


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=us_clock.tz)


def heartbeat_problems(loop, now: datetime) -> list[dict]:
    """返回当前存在的问题 [{reason, detail}]。非盘中或开盘头 3 分钟返回空。"""
    if not us_clock.in_session(now):
        return []
    open_at = datetime.combine(now.date(), us_clock.sessions[0].open, tzinfo=us_clock.tz)
    if now - open_at < BOOT_GRACE:
        return []
    if loop is None:
        return [{"reason": "loop_not_running", "detail": "交易环未初始化"}]
    out = []
    last_run = _parse(getattr(loop, "last_run_at", None))
    if last_run is None or now - last_run > LOOP_STALE:
        out.append({"reason": "loop_not_running",
                    "detail": f"最近运行 {last_run.astimezone(us_clock.tz).strftime('%H:%M:%S') if last_run else '无'}"})
    err = getattr(loop, "last_error", None)
    if err:
        out.append({"reason": "loop_error", "detail": redact(err)[:120]})
    # 0.7.1: 不进台账锁、不重算执行池; 读交易环本轮已算好的计数 (未跑过 → 0, 由 loop_not_running 兜底)
    try:
        n_symbols = int(getattr(loop, "last_execution_symbol_count", None) or 0)
    except (TypeError, ValueError):
        n_symbols = 0
    if n_symbols and not any(p["reason"] == "loop_not_running" for p in out):
        last_q = _parse(getattr(loop, "last_quote_at", None))
        if last_q is None or now - last_q > QUOTE_STALE:
            out.append({"reason": "quotes_stale",
                        "detail": f"执行池 {n_symbols} 只，最新报价 "
                                  f"{last_q.astimezone(us_clock.tz).strftime('%H:%M:%S') if last_q else '无'}，"
                                  f"本轮报价 {getattr(loop, 'last_quote_count', 0)} 只"})
    return out


def _alert_state(data_dir, day) -> dict:
    f = _root(data_dir) / "alert_state.json"
    try:
        st = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    return st if st.get("day") == day.isoformat() else {"day": day.isoformat(), "reasons": {}}


def _save_alert_state(data_dir, st: dict) -> None:
    atomic_write_text(_root(data_dir) / "alert_state.json", json.dumps(st, ensure_ascii=False))


def _payload(day, title: str, lines: list[str]) -> dict:
    return {"title": f"ZenTrade · {title}", "subtitle": day.isoformat(), "body": "\n".join(lines)}


def evaluate_alerts(data_dir, problems: list[dict], checklist_items: list[dict], now: datetime,
                    enqueue) -> list[str]:
    """按状态变化入队: 新问题报一次, 恢复报一次; 缺跑/失败的任务每个每天报一次。返回入队的键。"""
    day = now.date()
    st = _alert_state(data_dir, day)
    reasons = st["reasons"]
    sent = []
    active = {p["reason"]: p for p in problems}
    for reason, p in active.items():
        r = reasons.setdefault(reason, {"n": 0, "open": False})
        if r["open"] or r["n"] >= MAX_ALERTS_PER_REASON:
            continue
        r["n"] += 1
        r["open"] = True
        r["since"] = now.isoformat(timespec="seconds")
        key = f"ops:{day}:{reason}:{r['n']}"
        if enqueue(key, _payload(day, "盘中异常", [f"**{REASON_LABEL[reason]}**", p["detail"],
                                                  f"发现于 {now.strftime('%H:%M')} ET"])):
            sent.append(key)
    in_session = us_clock.in_session(now)
    for reason, r in reasons.items():
        if r.get("open") and reason not in active:
            r["open"] = False
            if not in_session:
                # 0.7.1: 收盘后心跳不再判, 不等于恢复; 静默收口, 不推"已恢复" (异常推送已发过)
                r["closed_unresolved"] = now.isoformat(timespec="seconds")
                logger.info("ops: %s 收盘时仍未恢复, 收口不推送", reason)
                continue
            key = f"ops:{day}:{reason}:{r['n']}:ok"
            if enqueue(key, _payload(day, "盘中恢复", [f"**{REASON_LABEL.get(reason, reason)}** 已恢复",
                                                      f"{str(r.get('since', ''))[11:16]} → {now.strftime('%H:%M')} ET"])):
                sent.append(key)
    for it in checklist_items:
        if it["state"] not in ("missing", "error"):
            continue
        key = f"ops:{day}:job:{it['job']}"
        detail = "未运行" if it["state"] == "missing" else f"失败 {((it.get('last') or {}).get('error') or '')}".strip()
        if enqueue(key, _payload(day, "定时任务缺跑", [f"**{it['label']}**（{it['due'][11:16]} ET）{detail}"])):
            sent.append(key)
    _save_alert_state(data_dir, st)
    return sent


def check(data_dir, scheduler, loop, *, now: datetime | None = None, enqueue=None, catchup: bool = True) -> dict:
    """调度入口 (每 3 分钟): 清单 + 补跑 + 心跳 + 报警。"""
    now = now or us_clock.now()
    if enqueue is None:
        from app.services import notify_opportunity as _no

        def _default_enqueue(key, payload):
            return _no._enqueue(data_dir, key, payload)
        enqueue = _default_enqueue
    items = checklist(data_dir, scheduler, now)
    caught = _catchup(data_dir, scheduler, items, now) if catchup else []
    problems = heartbeat_problems(loop, now)
    sent = evaluate_alerts(data_dir, problems, items, now, enqueue)
    return {"at": now.isoformat(timespec="seconds"), "checklist": items, "problems": problems,
            "caught_up": caught, "alerts": sent}


def status(data_dir, scheduler, loop, *, now: datetime | None = None) -> dict:
    """系统状态页读: 不补跑、不报警。"""
    now = now or us_clock.now()
    return {"checklist": checklist(data_dir, scheduler, now), "problems": heartbeat_problems(loop, now),
            "trading_day": us_clock.is_trading_day(now.date())}
