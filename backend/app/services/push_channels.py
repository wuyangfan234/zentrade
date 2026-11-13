"""Push channels, one place for every push (2026-11-13: unify all channels in system settings).

One selection in settings > system > push decides where *every* push goes: paper trades,
close / premarket boards, market mood, weekly report, held-stock news, ops alerts,
reconcile, corporate actions, open-volume radar and monitor-rule alerts.

- preferences key ``push_channels`` = the ticked channels.
- Never saved (key missing) = every configured channel is on, so a newly configured
  channel immediately receives all pushes.
- Saved: configuring a channel that was not configured before ticks it automatically
  ("add once, applies everywhere"); unticking only stops that channel.
- A channel only sends when it is ticked *and* configured (address / credentials present).
"""
from __future__ import annotations

from app.services import email_adapter, feishu_app, preferences

CHANNELS = preferences.PUSH_CHANNEL_ORDER
BRAND = "ZenTrade"
SCOPE = "成交、收盘与盘前机会板、盘前情绪、周报、持仓新闻、系统告警、对账、开盘放量、监控规则"
# Settings-page test button: one title for every channel, the channel name as subtitle.
TEST_TITLE = f"{BRAND} · 推送测试"
TEST_LABELS = {"feishu_app": "飞书应用机器人", "feishu": "飞书群 Webhook", "wecom": "企业微信",
               "custom": "第三方系统", "email": "邮件"}
TEST_HINT_DEFAULT = f"勾选这个渠道后，{SCOPE}都会发到这里。"
TEST_HINTS = {
    "feishu_app": f"勾选「飞书」后，{SCOPE}都会经应用机器人发到这个群。",
    "feishu": "飞书应用机器人未配置时，飞书推送改走这个群 Webhook。",
}
LABELS = {"feishu": "飞书", "wecom": "企业微信", "custom": "第三方系统", "email": "邮件"}
# Push log: outbox key prefix -> what the message is.
KIND_LABELS = {
    "trade": "成交", "risk": "风险提醒", "corp": "公司行动", "news": "持仓新闻", "daily": "收盘",
    "weekly": "周报", "degrade": "形态降级", "preopen": "盘前机会板", "premarket_gap": "盘前跳空",
    "market_mood": "盘前情绪", "ops": "系统告警", "dq": "数据质量", "swing_reconcile": "机会板对账",
    "open_radar": "开盘放量", "monitor": "监控规则",
}


def channel_configured(channel: str) -> bool:
    if channel == "feishu":
        return feishu_app.is_configured() or bool(preferences.get_feishu_webhook_url())
    if channel == "wecom":
        return bool(preferences.get_wecom_webhook_url())
    if channel == "custom":
        return bool(preferences.get_custom_webhook_url())
    if channel == "email":
        return email_adapter.is_configured(preferences.get_email_smtp_config())
    return False


def configured_channels() -> list[str]:
    return [c for c in CHANNELS if channel_configured(c)]


def enabled_channels() -> list[str]:
    """Ticked channels (never saved -> all configured ones)."""
    saved = preferences.get_push_channels()
    return configured_channels() if saved is None else saved


def active_channels() -> list[str]:
    """Ticked and configured: where a push would actually go right now."""
    return [c for c in enabled_channels() if channel_configured(c)]


def set_enabled(channels: list[str]) -> list[str]:
    preferences.set_push_channels(channels)
    return enabled_channels()


def note_configured(channel: str, was_configured: bool) -> None:
    """Call after saving a channel's address: a newly configured channel joins the saved selection."""
    if was_configured or not channel_configured(channel):
        return
    saved = preferences.get_push_channels()
    if saved is not None and channel not in saved:
        preferences.set_push_channels([*saved, channel])


def label(channel: str | None) -> str:
    return LABELS.get(channel or "", channel or "-")


def brand_title(title: str) -> str:
    """Every push title reads "ZenTrade · <what>" (never the upstream product name)."""
    title = (title or "").strip() or "通知"
    return title if title.startswith(BRAND) else f"{BRAND} · {title}"
