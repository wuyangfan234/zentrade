# ruff: noqa: RUF001
"""系统状态页的只读聚合 (0.7.1): 在原有 components/jobs 上补 market、since/impact/action/idle_kind/stale,
定时任务合并当日应跑清单。纯函数: 不写文件、不推送、不拿台账锁。"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from app.market_clock import us_clock
from app.services.ops_watch import REASON_LABEL

LOOP_STALE = timedelta(minutes=3)
PREMARKET_GAP_DUE = time(9, 20)       # 跳空预警 09:00 ET 运行, 宽限 20 分钟
DQ_READY = time(17, 30)               # 盘后管道 16:35 ET 起跑, 17:30 后才要求当天的数据质量报告

IMPACT = {
    "paper_loop": "盘中不会开仓、也不会按规则平仓",
    "execution_quotes": "撮合拿不到报价，盘中无法成交",
    "hot_quotes": "持仓页和分时图的实时价格不更新",
    "circuit_breaker": "今天不开新仓或降低仓位",
    "earnings": "财报日历不可用，系统会拦住相关标的开新仓",
    "corporate_actions": "公司行动不可用，系统会拦住相关标的开新仓",
    "data_quality": "日K可能有缺失或错价，明天的信号和证据可能不准",
    "daily_pipeline": "日K/4h/自选信号可能停在旧交易日，机会板硬门会用过期快照",
    "data_freshness": "列出的自选票 4h 方向/打分基于旧 bar，这些票的机会卡可能缺失或过期",
    "ops_watch": "当天该跑的定时任务没跑完，相关数据可能是旧的",
    "premarket_gap": "今天没有盘前跳空预警",
    "notifications": "飞书可能收不到交易和异常提醒",
    "alpaca_mirror": "Alpaca 镜像单可能和纸面台账对不上（不影响纸面交易本身）",
    "news": "个股新闻不更新",
    "qveris": "财报预期等付费数据可能缺失",
    "decision_desk": "机会页的候选可能不是最新的",
    "evidence": "规则依据的数据可能不是最新的",
    "realtime_source": "实时价格可能来自备用源",
}
ACTION = {
    "data_freshness": "看列出的票：数据源是否有数据、是否新上市；盘后管道会补拉全部自选的 30m",
    "paper_loop": "看容器日志里的交易环报错；盘中先不要重启，收盘后再处理",
    "execution_quotes": "查看实时行情源设置和日志里的报价错误",
    "hot_quotes": "查看日志里热报价的错误",
    "data_quality": "打开 data/ops/data_quality.json 看哪几只标的有问题，对照行情源核实",
    "daily_pipeline": "打开数据页看管道任务错误，或 data/job_store/ 最近 failed 记录；修好后可手动补跑 / 等开机 catchup",
    "ops_watch": "看下面「今天的定时任务」里哪项缺跑或失败，再查对应日志",
    "premarket_gap": "查看日志里 zen_premarket_gap 的运行记录",
    "notifications": "失败原因在 data/notifications/outbox.sqlite3 的 last_error；到 设置 → 实时监控 → 飞书应用 点「发送测试」确认通道",
    "earnings": "查看日志里财报日历的刷新错误",
    "corporate_actions": "查看日志里公司行动的刷新错误",
    "alpaca_mirror": "在持仓页查看镜像的最近错误",
}


def market(now: datetime) -> dict:
    d = now.date()
    td = us_clock.is_trading_day(d)
    s = us_clock.sessions[0]
    if not td:
        phase = "closed"
    elif us_clock.in_session(now):
        phase = "session"
    elif now.time() < s.open:
        phase = "premarket"
    else:
        phase = "postmarket"
    nd = d if (td and phase == "premarket") else us_clock.next_trading_day(d)
    return {"phase": phase, "trading_day": td,
            "next_open": datetime.combine(nd, s.open, tzinfo=us_clock.tz).isoformat(timespec="minutes"),
            # 审计 L-5: 半日市收盘 13:00 (日历), 不是 sessions 的 16:00
            "close": datetime.combine(d, us_clock.session_close(d), tzinfo=us_clock.tz).isoformat(timespec="minutes") if td else None}


def _parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=us_clock.tz)).astimezone(us_clock.tz)


def _expected_dq_day(now: datetime, mk: dict):
    d = now.date()
    if mk["trading_day"] and now.time() >= DQ_READY:
        return d
    return us_clock.previous_trading_day(d)


def enrich(components: list[dict], jobs: list[dict], *, now: datetime, loop_status: dict,
           ops: dict, dq: dict | None, pg: dict | None) -> dict:
    mk = market(now)
    sess = mk["phase"] == "session"
    by = {c["id"]: c for c in components}
    for c in components:
        c.setdefault("since", None)
        c["stale"] = False
        if c["id"] in IMPACT:
            c["impact"] = IMPACT[c["id"]]
        if c["id"] in ACTION:
            c["action"] = ACTION[c["id"]]

    # 纸面交易员: 盘中 3 分钟没跑 → 异常; 非盘中 → 休市
    if (c := by.get("paper_loop")) is not None:
        last = _parse(loop_status.get("last_run_at"))
        c["since"] = last.isoformat(timespec="seconds") if last else None
        if sess and c["state"] != "error" and (last is None or now - last > LOOP_STALE):
            c["state"], c["stale"] = "error", True
            c["summary"] = f"交易环已经 {int((now - last).total_seconds() // 60)} 分钟没运行" if last else "今天交易环还没运行过"
        elif not sess and c["state"] == "ok":
            c["state"], c["idle_kind"] = "idle", ("closed" if mk["phase"] in ("closed", "postmarket") else "waiting")
    # 盘中才需要的报价
    for cid in ("execution_quotes", "hot_quotes"):
        if (c := by.get(cid)) is not None and not sess and c["state"] != "error":
            c["state"], c["idle_kind"] = "idle", ("waiting" if mk["phase"] == "premarket" else "closed")
    # 0.35.3: 收盘后 / 盘前, 交易环和热报价都不再运行, last_error 是上一交易时段最后一轮留下的, 下一次成功轮次
    # (要等开盘) 才会覆盖。10-22 收盘前宿主机断网 1.5 小时, 这两项从 16:00 一直红到次日开盘, 状态页报「异常」却无事可做。
    # 非盘中: 降为休市 (idle/closed), 摘要标明是上一交易时段的最后一次报错。盘中照旧标红, 判定不变。
    if not sess:
        for cid in ("paper_loop", "hot_quotes"):
            if (c := by.get(cid)) is not None and c["state"] == "error" and not c.get("stale"):
                c["state"], c["idle_kind"], c["stale"] = "idle", ("waiting" if mk["phase"] == "premarket" else "closed"), True
                c["summary"] = c["summary"].replace("；错误 ", "；上一交易时段最后一次报错（开盘后第一轮成功即清除）：", 1)
                c.pop("action", None)
    # 盘前跳空预警
    if (c := by.get("premarket_gap")) is not None and mk["trading_day"]:
        today = now.date().isoformat()
        has_today = bool(pg) and str(pg.get("day")) == today
        if not has_today:
            if now.time() < PREMARKET_GAP_DUE:
                c["state"], c["idle_kind"] = "idle", "waiting"
                c["summary"] = "等待今天首次运行（09:00 ET）"
            elif mk["phase"] == "session" or now.time() < time(16, 0):
                c["state"], c["stale"] = "error", True
                c["summary"] = "今天 09:00 ET 的跳空预警没有产出"
    # 日K数据质量: 报告日偏旧 → 注意
    if (c := by.get("data_quality")) is not None and dq and dq.get("day"):
        c["since"] = dq.get("at")
        exp = _expected_dq_day(now, mk).isoformat()
        if dq["day"] < exp and c["state"] == "ok":
            c["state"], c["stale"] = "warn", True
            c["summary"] = f"最近一次检查是 {dq['day']}，应有 {exp} 的检查；" + c["summary"]
    # Alpaca 停用是刻意的
    if (c := by.get("alpaca_mirror")) is not None and c["state"] == "warn" and "已停用" in c["summary"]:
        c["state"], c["idle_kind"] = "idle", "disabled"
    # 盘中盯盘: 重新判定
    items = (ops or {}).get("checklist") or []
    if (c := by.get("ops_watch")) is not None and mk["trading_day"]:
        bad = [i for i in items if i["state"] in ("missing", "error")]
        wait = [i for i in items if i["state"] in ("pending", "running")]
        probs = (ops or {}).get("problems") or []
        okn = sum(1 for i in items if i["state"] == "ok")
        if bad or probs:
            c["state"] = "error"
        elif not items:
            c["state"], c["idle_kind"] = "idle", "waiting"
            c["summary"] = "等待今天第一个定时任务运行"
        else:
            c["state"] = "ok"
        if items:
            c["summary"] = (f"今日已到点 {len(items)} 项：完成 {okn}"
                            + (f"，进行中 {len(wait)}" if wait else "")
                            + "".join(f"；{i['label']}{'未运行' if i['state'] == 'missing' else '失败'}" for i in bad)
                            + "".join(f"；{REASON_LABEL.get(p['reason'], p['reason'])}" for p in probs))
    elif c is not None:
        c["idle_kind"] = "closed"
    for c in components:
        if c["state"] == "idle":
            c.setdefault("idle_kind", "waiting")

    # 定时任务: 合并当日清单
    chk = {i["job"]: i for i in items}
    for j in jobs:
        it = chk.get(j["id"])
        if it:
            j["state"] = it["state"]
            j["due"] = it["due"]
            j["last_at"] = (it.get("last") or {}).get("at")
        else:
            j.setdefault("state", None)
    order = {"error": 0, "warn": 1, "idle": 2, "ok": 3}
    components.sort(key=lambda x: order.get(x["state"], 9))
    return mk
