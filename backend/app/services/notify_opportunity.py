# ruff: noqa: RUF001
"""美股纸面交易通知：持久化入队与网络发送隔离。

撮合只调用 enqueue_trade_event(data_dir, event)，启动可重扫不可变台账补漏。
独立 scheduler 每 60 秒调用 flush_notifications(data_dir, limit=10)，max_instances=1。
enqueue 的 True 仅表示持久化/去重成功，送达以 notification_status 为准。
发送通道: 旧纸面通知仍走飞书应用机器人 / 群 Webhook; 显式带 channel 的消息按全局勾选渠道
分别入队与投递。飞书应用机器人优先于群 Webhook; 未配置时留存 unconfigured, 不丢事件、不假报成功。
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import logging
from pathlib import Path

from app import secrets_store
from app.opportunity.engine import ScanResult
from app.services import email_adapter, feishu_app, preferences, webhook_adapter
from app.services.notification_outbox import NotificationOutbox
from app.services.webhook_adapter import send_feishu_card

logger = logging.getLogger(__name__)
_SETUP_LABEL = {"breakout": "突破", "pullback": "回踩"}


def _send_feishu(payload: dict) -> bool:
    """应用机器人 (Channel SDK) 优先; 只配了旧群 Webhook 时才走 Webhook。"""
    if feishu_app.is_configured():
        return feishu_app.send_card(payload["title"], payload.get("subtitle", ""), payload["body"])
    url = preferences.get_feishu_webhook_url()
    if not url:
        return False
    return send_feishu_card(url, payload["title"], payload.get("subtitle", ""),
                            payload["body"], preferences.get_feishu_webhook_secret())


def _send_payload(payload: dict) -> bool:
    """旧消息仍走飞书; 带 channel 的消息按当前全局渠道设置投递。"""
    channel = payload.get("channel")
    if channel is not None and channel not in preferences.get_webhook_default_channels():
        return False
    if channel in (None, "feishu"):
        return _send_feishu(payload)
    title, body = payload["title"], payload["body"]
    if channel == "wecom":
        return webhook_adapter.send_wecom(preferences.get_wecom_webhook_url(), title, body)
    if channel == "custom":
        return webhook_adapter.send_custom(
            preferences.get_custom_webhook_url(), title, body,
            payload.get("event_type") or "notification", payload.get("data") or {},
            secret=secrets_store.get_custom_webhook_secret(),
        )
    if channel == "email":
        return email_adapter.send_email(
            preferences.get_email_smtp_config(), secrets_store.get_email_smtp_password(), title, body,
        )
    return False


def _configured() -> bool:
    return feishu_app.is_configured() or bool(preferences.get_feishu_webhook_url())


def _channel_configured(channel: str) -> bool:
    if channel == "feishu":
        return _configured()
    if channel == "wecom":
        return bool(preferences.get_wecom_webhook_url())
    if channel == "custom":
        return bool(preferences.get_custom_webhook_url())
    if channel == "email":
        return email_adapter.is_configured(preferences.get_email_smtp_config())
    return False


def _payload_configured(payload: dict) -> bool:
    channel = payload.get("channel")
    if channel is None:
        return _configured()
    return channel in preferences.get_webhook_default_channels() and _channel_configured(channel)


def _queue(data_dir: Path) -> NotificationOutbox:
    return NotificationOutbox(
        data_dir, sender=_send_payload, configured=_configured,
        configured_for_payload=_payload_configured,
    )


def _enqueue(data_dir: Path, key: str, payload: dict) -> bool:
    try:
        _queue(data_dir).enqueue(key, payload)
        return True
    except Exception as exc:
        # 磁盘错误不能回滚已撮合的交易；原始事件仍在 ledger，boot 重扫可补漏。
        logger.warning("通知入队失败，等待台账补漏: %s", type(exc).__name__)
        return False


def enqueue_global(data_dir: Path, key: str, payload: dict, *,
                   event_type: str = "notification", data: dict | None = None,
                   channels: list[str] | None = None) -> dict:
    """按当前全局勾选渠道分别入队; channels 可限定为本次仍需补入队的子集。"""
    current = preferences.get_webhook_default_channels()
    selected = current if channels is None else [channel for channel in channels if channel in current]
    queued = []
    for channel in selected:
        item = {**payload, "channel": channel, "event_type": event_type, "data": data or {}}
        if _enqueue(data_dir, f"{key}:{channel}", item):
            queued.append(channel)
    return {"selected": selected, "queued": queued,
            "ok": bool(selected) and len(queued) == len(selected)}


def notification_status(data_dir: Path) -> dict:
    """返回当前配置、持久化计数和中文状态；不返回 webhook/签名/正文。"""
    try:
        return _queue(data_dir).status()
    except Exception as exc:
        return {"status": "failed", "label": "失败", "error": type(exc).__name__, "durable": False}


def flush_notifications(data_dir: Path, limit: int = 10) -> dict:
    """专用通知 job 的有界网络出口；不要在撮合线程或台账锁内调用。"""
    try:
        queue = _queue(data_dir)
        result = queue.flush(max_items=limit)
        result["notification_status"] = queue.status()
        return result
    except Exception as exc:
        logger.warning("通知发送队列不可用: %s", type(exc).__name__)
        return {"sent": 0, "failed": 0, "error": type(exc).__name__}


def _card_md(card: dict) -> str:
    setup = _SETUP_LABEL.get(card.get("setup_type", ""), card.get("setup_type", ""))
    decision = (card.get("evidence") or {}).get("decision") or {}
    if decision.get("sources"):
        tag = "探索单 · " if decision.get("tier") == "explore" else ""
        setup = f"{tag}来源 {'+'.join(decision['sources'])} · 综合分 {decision.get('score', 0):.2f}"
    entry, target = card.get("entry_zone", [0, 0]), card.get("target_zone", [0, 0])
    side = "做空 · " if card.get("side") == "short" else ""
    return (
        f"**{card['symbol']}** · {side}{setup} · RR {card.get('reward_risk', 0):.1f}\n"
        f"> 入场 ${entry[0]:.2f}~{entry[1]:.2f} · 失效 ${card.get('invalidation', 0):.2f} · "
        f"目标 ${target[0]:.2f}~{target[1]:.2f}\n"
        f"> 最长持仓 {card.get('max_holding_days', 7)} 交易日"
    )


def _daily_report_md(daily: dict) -> list[str]:
    """纸面日报: 净值、持仓、当日管理动作 (docs/architecture.md: 管理动作并入日报不单独推)。"""
    nav = (daily.get("nav") or {})
    lines = ["\n**纸面日报**"]
    if nav.get("nav") is not None:
        chg = ""
        prev = daily.get("prev_nav")
        if prev:
            chg = f"（{(nav['nav'] / prev - 1):+.2%}）"
        positions = daily.get("positions") or []
        # 合约式持仓的 market_value 是浮动盈亏, 不是市值
        label = "持仓浮盈" if positions and all(p.get("contract") for p in positions) else (
            "持仓市值/浮盈" if any(p.get("contract") for p in positions) else "持仓市值")
        lines.append(f"> 净值 ${nav['nav']:,.2f}{chg} · 现金 ${nav.get('cash', 0):,.2f} · {label} ${nav.get('market_value', 0):,.2f}")
    positions = daily.get("positions") or []
    if positions:
        for p in positions:
            stop = f" · 止损 ${p['stop_price']:.2f}" if p.get("stop_price") else ""
            side = "空 " if p['qty'] < 0 else ""
            kind = " · 合约" if p.get("contract") else ""
            lines.append(f"- {p['symbol']} {side}{abs(p['qty']):g} 股 @ ${p['avg_cost']:.2f}{stop}{kind}")
    else:
        lines.append("- 空仓")
    actions = daily.get("actions") or []
    if actions:
        lines.append("**当日管理动作**")
        for a in actions:
            if a.get("kind") == "stop_move":
                lines.append(f"- {a['symbol']} 止损上移至 ${float(a['stop_price']):.2f}（{a.get('reason', '')}）")
            else:
                lines.append(f"- {a.get('symbol', '')} {a.get('kind')} {a.get('reason', '')}".strip())
    return lines


def enqueue_daily_opportunities(data_dir: Path, result: ScanResult, daily: dict | None = None) -> bool:
    """每日只入队一次（含零机会日），不联网。daily 给出时附带纸面日报。v4 模式只推自选信号行。"""
    try:
        from app.services import strategy_evaluation
        if strategy_evaluation.evidence_mode(data_dir) == "v4":
            return _enqueue_v4_daily(data_dir, result.as_of, daily)
    except Exception as exc:  # noqa: BLE001
        logger.warning("v4 daily push fallback: %s", type(exc).__name__)
    gate = {"green": "🟢 绿灯", "yellow": "🟡 黄灯 (SPY<MA200, 减量)"}.get(result.gate, "基准数据不足，暂停新开卡")
    day = result.as_of.isoformat()
    lines = [f"**{day}** · {gate} · 扫描 {result.scanned} 只\n"]
    if result.cards:
        lines.append(f"**明日机会 {len(result.cards)} 张:**\n")
        tabbed = [c.to_dict() for c in result.cards]
        pool = [c for c in tabbed if ((c.get("evidence") or {}).get("decision") or {}).get("tab") != "watchlist"]
        mine = [c for c in tabbed if ((c.get("evidence") or {}).get("decision") or {}).get("tab") == "watchlist"]
        if mine:
            lines.append(f"**全股票池 {len(pool)} 张**")
            if pool:
                lines.extend(_card_md(c) for c in pool)
            else:
                lines.append("（无）")
            lines.append(f"\n**自选股 {len(mine)} 张**")
            lines.extend(_card_md(c) for c in mine)
        else:
            lines.extend(_card_md(c) for c in pool)
    else:
        lines.append("**今日无机会。** 不为出手而出手。")
    if result.watch:
        lines.append(f"\n观察 {len(result.watch)} 张 (组合约束降级, 只看不买)")
    if daily:
        lines.extend(_daily_report_md(daily))
    return _enqueue(data_dir, f"daily:{day}", {
        "title": "ZenTrade · 每日机会", "subtitle": f"{day} · rev{result.params_revision}",
        "body": "\n".join(lines),
    })


def _swing_lines(data_dir: Path, as_of) -> list[str] | None:
    """机会板模式 (rules/swing.json 存在): 推送内容 = 机会板当日的卡 (= 纸面下单簿), 不再列旧规则集信号。
    返回 None = 不是机会板模式。"""
    from app.services import swing_desk as sd
    if not sd.swing_config_path(data_dir).exists():
        return None
    board = sd.board_for_day(data_dir, as_of, _repo_for_push()) or {}
    lines = []
    for side, tag in (("long", "多"), ("short", "空")):
        for c in board.get(side) or []:
            lo, hi = c["entry_zone"]
            lines.append(f"{c['symbol']} {tag} 挂单 {lo:.2f}-{hi:.2f} 止损 {c['stop']:.2f} 目标 {c['target']:.2f}"
                         f" · {'+'.join(x['name'] for x in c.get('strategies') or [])}")
    meta = board.get("meta") or {}
    if meta.get("short_gate") is False:
        lines.append("空头关闭 (SPY 在 50 日线上)")
    return lines


def _repo_for_push():
    try:
        from app.jobs.daily_pipeline import _get_app_state
        return getattr(_get_app_state(), "repo", None)
    except Exception:
        return None


def _enqueue_v4_daily(data_dir: Path, as_of, daily: dict | None) -> bool:
    from app.services import watchlist_v4 as wv
    swing = None
    try:
        swing = _swing_lines(data_dir, as_of)
    except Exception as exc:
        logger.warning("swing daily push lines failed: %s", type(exc).__name__)
    if swing is not None:
        lines = [f"**{as_of.isoformat()} 收盘 · 机会板**", *(swing or ["无新卡"])]
        nav = (daily or {}).get("nav") or {}
        if nav.get("nav") is not None:
            lines.append(f"净值 {nav['nav']:,.2f}")
        return _enqueue(data_dir, f"daily:{as_of.isoformat()}", {"title": "ZenTrade · 收盘", "subtitle": as_of.isoformat(),
                                                                 "body": "\n".join(lines)})
    sig = wv.latest_signals(data_dir) or {}
    rs = wv.current_ruleset(data_dir)
    en = set(rs.get("enabled") or [])
    rows = [x for x in sig.get("signals") or [] if x.get("rule") in en and x.get("side") != "short"
            and sig.get("as_of") == as_of.isoformat()]
    lines = [f"**{as_of.isoformat()} 收盘 · R{rs.get('version')}**"]
    seen = set()
    for x in sorted(rows, key=lambda r: r["symbol"]):
        if x["symbol"] in seen:
            continue
        seen.add(x["symbol"])
        lines.append(f"{x['symbol']} 触发 {x['entry_zone'][1]:.2f} 失效 {x['invalidation']:.2f} 目标 {x['target_zone'][0]:.2f}")
    if not seen:
        lines.append("无新信号")
    nav = (daily or {}).get("nav") or {}
    if nav.get("nav") is not None:
        lines.append(f"净值 {nav['nav']:,.2f}")
    return _enqueue(data_dir, f"daily:{as_of.isoformat()}", {"title": "ZenTrade · 收盘", "subtitle": as_of.isoformat(),
                                                             "body": "\n".join(lines)})


def enqueue_trade_event(data_dir: Path, event: dict) -> bool:
    """按 seq/card/day 去重；保留部分退出，但不把单次部分退出称为回合 R。"""
    kind = event.get("kind")
    if kind not in ("fill", "partial_close", "close"):
        return False
    if event.get("backfill"):
        return True       # 回补的历史成交 (事后按分钟K重放补记): 不推送, 水位照常推进
    day = str(event.get("trade_date") or str(event.get("at", ""))[:10])
    seq = event.get("seq")
    if seq is None:  # 兼容旧调用，完整事件摘要避免同日两次减仓互相覆盖。
        seq = hashlib.sha256(json.dumps(event, sort_keys=True, default=str).encode()).hexdigest()
    key = f"trade:{seq}:{event.get('card_id', '')}:{day}"
    contract = event.get("accounting") == "contract"
    open_title = ("开空" if event.get("side") == "short" else "开多") if contract else (
        "卖空开仓" if event.get("side") == "short" else "买入")
    title = {"fill": open_title, "partial_close": "部分退出", "close": "平仓"}[kind]
    body = (f"**{event.get('symbol')}** · {_SETUP_LABEL.get(event.get('setup_type'), event.get('setup_type', ''))}\n"
            f"> {event.get('qty')} 股 @ ${float(event.get('price') or 0):.2f} · "
            f"费 ${float(event.get('fee') or 0):.2f}\n> 卡 {event.get('card_id')}")
    if kind == "fill" and contract and isinstance(event.get("risk_pct"), (int, float)):
        body += f"\n> 风险 {event['risk_pct'] * 100:.2f}% · 止损 {event.get('stop_price')}"
    sizing = event.get("sizing") if kind == "fill" else None
    if isinstance(sizing, dict) and sizing.get("reason") == "cash_downsized":
        body += f"\n> 现金不足缩量成交：计划 {sizing.get('planned_qty')} 股，实成 {sizing.get('qty')} 股"
    if kind != "fill":
        body += f"\n> 原因 {event.get('exit_reason') or event.get('reason', '')}"
        if kind == "partial_close":
            body += " · 回合尚未闭合，净 R 待全平复盘"
    return _enqueue(data_dir, key, {"title": f"ZenTrade · 纸面{title}", "subtitle": day, "body": body})


def enqueue_risk_note(data_dir: Path, event: dict) -> bool:
    """财报前减半窗口错过: 持仓将带着财报隔夜, 一次性告知 (按 seq 去重)。"""
    when = {"bmo": "盘前", "amc": "盘后"}.get(event.get("earnings_timing"), "时段未知")
    body = (f"**{event.get('symbol')}** · 财报 {event.get('earnings_date')}（{when}）\n"
            f"> {event.get('detail') or '财报前减仓窗口内未能减仓'}\n"
            f"> 持仓将带着财报过夜，请人工关注")
    return _enqueue(data_dir, f"risk:{event.get('seq')}", {
        "title": "ZenTrade · 风险提醒", "subtitle": str(event.get("trade_date") or str(event.get("at", ""))[:10]),
        "body": body})


_CORP_LABEL = {"forward_split": "拆股", "reverse_split": "反向拆股", "cash_dividend": "现金分红"}


def _et_label(iso_utc) -> str:
    """UTC ISO 时间 → 美东 'YYYY-MM-DD HH:MM' (推送统一美东口径)。"""
    from datetime import datetime as _dt
    from app.market_clock import us_clock
    try:
        return _dt.fromisoformat(str(iso_utc).replace("Z", "+00:00")).astimezone(us_clock.tz).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(iso_utc or "")[:16]


def enqueue_news_alert(data_dir: Path, item: dict) -> bool:
    """持仓重大利空新闻 (无限 2026-10-02 拍板「仅持仓 + 重要度高时推送」) — 按事件 id + 股票去重, 一事一推。

    item 是 news_store.view() 的展示视图 (已是中文)。标注「AI 判读」, 不给任何操作结论。
    """
    sym = item.get("symbol", "")
    when = _et_label(item.get("published_at"))
    src = "、".join(item.get("sources") or [])
    lines = [f"**{sym}** · 持仓 · {item.get('event_label') or '新闻'} · 重要度高 · 偏利空",
             f"> {item.get('headline', '')}"]
    if item.get("summary"):
        lines.append(f"> {item['summary']}")
    lines.append(f"> {when} 美东 · 来源 {src}" + (f" · [原文]({item['url']})" if item.get("url") else ""))
    if (item.get("related_count") or 0) > 1:
        lines.append(f"> 48 小时内同类报道共 {item['related_count']} 条，详见个股「新闻」页")
    lines.append("> 方向与重要度为 AI 判读，未经验证；持仓按原计划管理，是否处理由你决定")
    return _enqueue(data_dir, f"news:{item.get('id')}:{sym}", {
        "title": "ZenTrade · 持仓新闻提醒", "subtitle": when[:10], "body": "\n".join(lines)})


def enqueue_corp_action(data_dir: Path, event: dict) -> bool:
    """公司行动自动入账 (拆合股调整持仓/冻结价位、现金分红入账) — 按 action_id 去重。"""
    kind = event.get("action_type", "")
    detail = []
    if event.get("factor") is not None:
        detail.append(f"比例 {event.get('factor')}")
    if event.get("rate") is not None:
        detail.append(f"每股 ${float(event['rate']):.4f}")
    if event.get("cash_dividend") is not None:
        detail.append(f"入账 ${float(event['cash_dividend']):.2f}")
    body = (f"**{event.get('symbol')}** · {_CORP_LABEL.get(kind, kind)}（除权日 {event.get('ex_date')}）\n"
            f"> {' · '.join(detail) or '已自动入账'}")
    key = f"corp:{event.get('action_id') or event.get('seq')}"
    return _enqueue(data_dir, key, {"title": "ZenTrade · 公司行动", "subtitle": str(event.get("ex_date") or ""),
                                    "body": body})


def enqueue_setup_degraded(data_dir: Path, setups: list[str], state: dict) -> bool:
    """setup 被自动降级为只观察: 一次性通知 (按 setup+降级时间去重)。"""
    lines = []
    for setup in setups:
        ev = (state.get(setup) or {}).get("evidence") or {}
        lines.append(f"- **{_SETUP_LABEL.get(setup, setup)}** 已降级为只观察（停新开仓）："
                     f"近 4 周闭合 {ev.get('samples')} 笔，平均 R {ev.get('avg_r')}")
    lines.append("持仓管理不受影响；需在持仓页人工复核后手动恢复。")
    stamp = ",".join(f"{s}@{(state.get(s) or {}).get('degraded_at')}" for s in setups)
    return _enqueue(data_dir, f"degrade:{stamp}", {"title": "ZenTrade · 形态降级", "subtitle": "四周负期望",
                                                   "body": "\n".join(lines)})


def enqueue_weekly_review(data_dir: Path, report: dict) -> bool:
    """周报入队，按 week_end 去重；R 缺失明确标注。"""
    day = str(report["week_end"])
    lines = [f"本周闭合回合 **{report['trips']}** 笔（仅全平计数，净盈亏扣全部费用）。"]
    for row in report.get("by_setup", []):
        avg_r = row.get("avg_r")
        r_label = f"{avg_r:+.2f}R" if avg_r is not None else "缺首填风险，无法计算"
        lines.append(f"- {_SETUP_LABEL.get(row['setup_type'], row['setup_type'])}: {row['count']} 笔，"
                     f"胜率 {row['win_rate']:.1%}，均 R {r_label}（有效 R 样本 {row.get('r_count', 0)}）")
    mood = (report.get("by_mood") or {}).get("by_stance") or []
    if mood:
        lines.append("按盘前大盘情绪（累计实时回合）：" + "；".join(
            f"{m['label']} {m['count']} 笔 胜 {m['wins']}" + (f" 均 {m['avg_r']:+.2f}R" if m.get("avg_r") is not None else "")
            for m in mood))
    if report.get("note"):
        lines.append(str(report["note"]))
    return _enqueue(data_dir, f"weekly:{day}", {
        "title": "ZenTrade · 纸面周报", "subtitle": day, "body": "\n".join(lines),
    })


def push_weekly_report(data_dir: Path, week_end: date) -> dict:
    """便捷定时入口：计算只读周报→持久化→真正调用推送 API（非撮合入口）。"""
    from app.review.review import weekly_report
    if not enqueue_weekly_review(data_dir, weekly_report(data_dir, week_end)):
        return {"status": "failed", "label": "失败", "durable": False}
    flush_notifications(data_dir)
    return _queue(data_dir).status(f"weekly:{week_end.isoformat()}")
