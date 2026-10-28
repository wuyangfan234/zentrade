# ruff: noqa: RUF001  # 面向用户的中文文案, 全角标点是有意的
"""配置清单 (0.42.0): 所有需要填 key 或做配置的外部服务、AI 模型与推送渠道, 一个注册表说清楚。

每项回答四个问题: 是什么、不配会怎样、去哪配 (设置页锚点 / 环境变量)、现在配了没有 (来源: 设置页 / .env)。
新部署首页横幅和设置页「配置清单」读同一个接口 GET /api/settings/setup-checklist。

level:
  required    不配系统跑不起来 (日K、交易日历、纸面撮合都依赖它)
  recommended 不配能跑, 但有一块功能缺失或降级
  optional    增强项, 按需

只读: 不发网络请求、不探测 key 有效性 (有效性在保存时「先探后存」已验证), 不返回任何密钥原文。
"""
from __future__ import annotations

import os
from typing import Any

# 新闻翻译的用量估算口径 (生产实测 2026-10-19~10-27): 交易日日均约 600 条 (峰值 1023), 每条英文原文约 230 字符,
# 译文约 86 个汉字 + 标签 JSON, 每批 20 条共用约 700 字符系统提示词。粗算每条入 ~80 token、出 ~120 token。
# 这是关思考的数字; 开思考的推理模型会再多出数倍推理 token。
NEWS_PER_DAY_TYPICAL = 600
NEWS_TOKENS_PER_ITEM = 200


def news_token_estimate(items_per_day: int = NEWS_PER_DAY_TYPICAL) -> dict:
    daily = items_per_day * NEWS_TOKENS_PER_ITEM
    return {"items_per_day": items_per_day, "tokens_per_day": daily, "tokens_per_month": daily * 22}


def _plugin_status(name: str) -> dict:
    try:
        from app.data_providers import custom as custom_sources
        return next((p for p in custom_sources.list_plugins() if p.get("name") == name), {}) or {}
    except Exception:
        return {}


def _src_label(src: str) -> str:
    return {"ui": "设置页", "env": ".env", "env_dir": "本机 env 目录"}.get(src, "")


def _item(id_: str, group: str, label: str, level: str, configured: bool, *, source: str = "",
          purpose: str, impact: str, where: str, env: list[str] | None = None, link: str = "",
          note: str = "", warn: str = "") -> dict[str, Any]:
    return {"id": id_, "group": group, "label": label, "level": level, "configured": bool(configured),
            "source": source, "purpose": purpose, "impact": impact, "where": where, "env": env or [],
            "link": link, "note": note, "warn": warn}


def _ai_items() -> list[dict]:
    from app import secrets_store
    from app.services import ai_provider as ap

    out: list[dict] = []
    provider = ap.current_ai_provider()
    if ap.is_codex_cli_provider(provider):
        # 清单每次页面加载都会请求: 只看命令能否在 PATH 找到, 不实跑 `codex --version` (最长 15 秒)
        import shutil
        configured = bool(shutil.which(ap.current_codex_command() or "codex"))
        model = ap.current_codex_model()
    else:
        # OpenAI 兼容: 地址、模型、Key 三项齐全才算已配置 (0.42.0 起不再预填默认地址与模型)
        model = ap.current_openai_model()
        configured = bool(secrets_store.get_ai_key() and model
                          and secrets_store.get_ai_config("ai_base_url", ap.settings.ai_base_url))
    src = "设置页" if secrets_store.load().get("ai_api_key") else (".env" if os.environ.get("AI_API_KEY") else "")
    out.append(_item(
        "ai", "ai", "AI 模型（全局）", "recommended", configured, source=src,
        purpose="个股分析、新闻翻译与打标、AI 策略生成、助手对话、盘前大盘情绪的默认模型。",
        impact="不配：新闻只有英文原标题（Google 兜底只翻标题，无利多/利空标签），个股分析、AI 策略、助手不可用。交易环与机会板不依赖 AI。",
        where="settings?tab=ai&highlight=ai-connection", env=["AI_PROVIDER", "AI_BASE_URL", "AI_API_KEY", "AI_MODEL"],
        note=f"当前模型：{model}" if configured and model else ""))

    est = news_token_estimate()
    prof = ap.task_profile("news_translate")
    overridden = bool(prof.get("overridden"))
    out.append(_item(
        "ai_news", "ai", "新闻翻译模型", "recommended",
        bool((configured or prof.get("api_key")) and prof.get("model") and prof.get("base_url")),
        source="单独配置" if overridden else ("跟随全局" if configured else ""),
        purpose="把自选、持仓相关的英文新闻译成中文，并标注相关度、利多/利空、重要度。",
        impact="跟随全局模型时，新闻翻译占掉大部分 AI 调用量。",
        where="settings?tab=ai&highlight=ai-task-news_translate",
        note=f"当前：{prof.get('model') or '未配置'}，思考 {prof.get('thinking')}",
        warn=(f"用量大：约 {est['items_per_day']} 条/交易日，粗估约 {est['tokens_per_day'] // 10000} 万 token/日、"
              f"{est['tokens_per_month'] // 10000} 万 token/月（关思考时；开思考会翻数倍）。建议在「AI 模型分工」给它单独配一个"
              f"低价、关思考的模型（如 DeepSeek / Kimi / 通义千问的轻量档），不要用旗舰推理模型。")))
    return out


def build() -> dict:
    from app import credentials
    from app.services import preferences

    items: list[dict] = []

    # ── 行情数据 ──
    k, s, src = credentials.alpaca()
    items.append(_item(
        "alpaca", "market", "Alpaca（美股行情）", "required", bool(k and s), source=_src_label(src),
        purpose="SIP 历史日K / 分钟K、IEX 实时报价、NYSE 交易日历、公司行动、纸面镜像账户、Alpaca 新闻。",
        impact="不配：没有日K和交易日历，机会板、回测、纸面交易都无法运行。",
        where="settings?tab=data-sources&source=alpaca&highlight=source-detail", env=["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"],
        link="https://app.alpaca.markets/signup", note="免费账户即可；设置页填写格式 KEY_ID:SECRET。"))
    tf = _plugin_status("tickflow")
    items.append(_item(
        "tickflow", "market", "TickFlow（盘中实时）", "optional", bool(tf.get("available")),
        source="已配置" if tf.get("available") else "",
        purpose="全市场合并口径实时报价，约 5 秒刷新；用于盘中强势、热报价与纸面撮合。",
        impact="不配：实时行情用 Alpaca IEX（单一交易所，成交量偏小），其余功能不受影响。",
        where="settings?tab=data-sources&source=tickflow&highlight=source-detail", env=["TICKFLOW_API_KEY"], link="https://tickflow.org"))

    # ── 新闻与基本面 ──
    fh = credentials.finnhub()
    items.append(_item(
        "finnhub", "news", "Finnhub", "recommended", bool(fh),
        source=_src_label(credentials.lookup(credentials.FINNHUB)[1]),
        purpose="个股新闻（核心票）、财报日期交叉核对、EDGAR 缺失时的财务与股本兜底、公司图标。",
        impact="不配：新闻只剩 Alpaca + SEC 公告；财报日历只有 Nasdaq 单源，Nasdaq 不可用时暂停新开仓。",
        where="settings?tab=data-sources&highlight=setup-finnhub", env=["FINNHUB_API_KEY"],
        link="https://finnhub.io/register", note="免费档即可。"))
    ua = credentials.sec_user_agent()
    items.append(_item(
        "sec_ua", "news", "SEC EDGAR 联系邮箱", "recommended", ua != credentials.DEFAULT_SEC_USER_AGENT,
        source=_src_label(credentials.lookup(credentials.SecretSpec("SEC_USER_AGENT", "sec_user_agent"))[1]),
        purpose="SEC 免费接口要求请求头带可联系的邮箱；用于财务报表同步和 8-K 公告新闻。",
        impact="不配：用占位标识请求，SEC 可能限流或拒绝，财务数据与公告新闻会缺失。",
        where="settings?tab=data-sources&highlight=setup-sec_ua", env=["SEC_USER_AGENT"],
        link="https://www.sec.gov/os/accessing-edgar-data",
        note="格式示例：ZenTrade your-name@example.com"))
    qv = _plugin_status("qveris")
    items.append(_item(
        "qveris", "news", "QVeris（付费）", "optional", bool(qv.get("available")),
        source="已配置" if qv.get("available") else "",
        purpose="期权全链与隐含波动率、财报一致预期、电话会摘要。",
        impact="不配：个股弹窗没有这三项；盘面情报的期权隐含波幅用 CBOE 免费数据。",
        where="settings?tab=data-sources&source=qveris&highlight=source-detail", env=["QVERIS_API_KEY"], link="https://qveris.ai",
        note="按积分计费，设置页可设每日上限。"))
    items.append(_item(
        "free_sources", "news", "免 Key 公开数据", "optional", True, source="内置",
        purpose="Nasdaq 财报日历、CBOE 延迟报价与 Put/Call、FRED 宏观、ForexFactory 日历、Hyperliquid 盘前参考价、Google 翻译兜底。",
        impact="无需配置。均为公开端点，不保证可用；单个源失败只影响对应卡片。",
        where="", note="使用前请自行确认各源服务条款。"))

    # ── AI ──
    items.extend(_ai_items())

    # ── 推送 ──
    try:
        from app.services import email_adapter, feishu_app
        push = any((feishu_app.is_configured(), preferences.get_feishu_webhook_url(), preferences.get_wecom_webhook_url(),
                    preferences.get_custom_webhook_url(), email_adapter.is_configured(preferences.get_email_smtp_config())))
    except Exception:
        push = False
    items.append(_item(
        "notify", "notify", "消息推送", "optional", push, source="已配置" if push else "",
        purpose="盘前机会板、开仓/平仓、持仓新闻、监控规则触发推送到飞书 / 企业微信 / Webhook / 邮件。",
        impact="不配：所有提醒只在页面上看，不会主动推送。",
        where="settings?tab=monitoring&highlight=webhooks"))

    req_missing = [i["id"] for i in items if i["level"] == "required" and not i["configured"]]
    rec_missing = [i["id"] for i in items if i["level"] == "recommended" and not i["configured"]]
    return {
        "items": items,
        "summary": {"required_missing": req_missing, "recommended_missing": rec_missing,
                    "ready": not req_missing},
        "dismissed": bool(preferences.load().get("setup_banner_dismissed")),
    }
