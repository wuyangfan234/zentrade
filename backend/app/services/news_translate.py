"""新闻翻译与打标 — 按任务配置的 OpenAI 兼容模型批量调用 (默认关思考, 见 ai_provider.AI_TASKS)。

输入: news_ingest 落盘的已聚簇事件 (data/news/YYYY-MM.jsonl)。
输出: 在同一条记录上补写字段 (原地增量, 不另开文件):
  zh_headline / zh_summary   中文标题 / 中文摘要 (摘要缺失时留空, 不编造)
  tags[symbol] = {relevance: primary|secondary|passing, event_type, direction: bullish|bearish|neutral,
                  importance: high|medium|low, rehash: bool}
  tagged_by = {model, prompt_version, at, mode: llm|fallback}

失败兜底 (可选, NEWS_GOOGLE_TRANSLATE_FALLBACK=1 开启): 模型不可用/返回无法解析 → Google 翻译网页端点
只翻译标题, 不打标 (tags 留空, 前端显示「未打标」), mode=fallback; 下一轮仍会重试打标, 直到模型成功。
未开启时事件保持未翻译, 等下一轮模型重试。
模型判读只用于展示与影子记账, 不参与开仓 (方案第五节)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import PUBLIC_HTTP_USER_AGENT, settings
from app.services import news_ingest

logger = logging.getLogger(__name__)

TASK = "news_translate"
PROMPT_VERSION = "news-tag-v1"
BATCH_SIZE = 20
MAX_BATCHES_PER_RUN = 2  # 每轮最多 40 条; 积压分摊到后续轮 (调度每分钟唤醒)
TIME_BUDGET_S = 120.0    # 单轮翻译墙钟上限 (含预估的下一批耗时); 剩余下一轮继续
# 实测 (2026-10-02, 推理模型关思考): 20 条/批 60-70 秒; 安静时段 ~40 秒。按慢的估。
EST_BATCH_S = 70.0
EVENT_TYPES = ("earnings", "guidance", "rating", "contract", "mna", "product", "regulatory", "dilution",
               "management", "litigation", "macro", "other")
EVENT_LABEL = {"earnings": "财报", "guidance": "业绩指引", "rating": "评级/目标价", "contract": "合同订单", "mna": "并购",
               "product": "产品/技术", "regulatory": "监管/调查", "dilution": "融资稀释", "management": "高管变动",
               "litigation": "诉讼", "macro": "宏观/行业", "other": "其他"}
RELEVANCE = ("primary", "secondary", "passing")
DIRECTIONS = ("bullish", "bearish", "neutral")
IMPORTANCE = ("high", "medium", "low")

SYSTEM_PROMPT = """你是美股新闻编辑, 只做翻译与客观归类, 不预测股价, 不给买卖建议。
对每条新闻输出一个 JSON 对象, 全部放进一个 JSON 数组, 不要输出数组以外的任何文字。
字段:
- i: 输入序号 (整数)
- zh_headline: 标题的简体中文译文; 公司名首次出现保留英文原名, 股票代码原样保留, 数字/机构名/日期必须准确, 不得添加原文没有的信息
- zh_summary: 摘要的简体中文译文 (不超过 120 字); 输入摘要为空时输出空字符串, 不得编造
- tags: 对象, 键为下面给出的「待判股票」中的每个代码, 值为:
    relevance: primary(新闻主角) | secondary(重要配角) | passing(顺带一提)
    event_type: earnings|guidance|rating|contract|mna|product|regulatory|dilution|management|litigation|macro|other
    direction: bullish|bearish|neutral  (只依据新闻本身对该公司的直接含义, 拿不准就 neutral)
    importance: high|medium|low  (high 仅限: 财报/指引大幅超预期或不及预期、并购、增发稀释、监管调查/起诉、
                重大合同、CEO/CFO 突然离职; 观点文/清单文/价格异动描述一律 low)
    rehash: true|false  (旧闻重炒, 如回顾性观点文、"为什么上涨"类事后解释)
"""

AiCall = Callable[..., Awaitable[str]]
HttpGet = Callable[..., Any]


def _user_prompt(batch: list[dict]) -> str:
    lines = []
    for i, ev in enumerate(batch):
        syms = sorted((ev.get("mentions") or {}).keys()) or sorted(ev.get("symbols") or [])
        lines.append(json.dumps({"i": i, "headline": ev.get("headline", ""), "summary": (ev.get("summary") or "")[:600],
                                 "待判股票": syms}, ensure_ascii=False))
    return "逐条处理下列新闻 (每行一条 JSON):\n" + "\n".join(lines)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_llm_output(text: str, batch: list[dict]) -> dict[int, dict]:
    """宽松解析模型输出: 去掉代码围栏, 取第一个 JSON 数组; 字段逐个校验, 非法值丢弃该字段而不是整条。"""
    body = _FENCE_RE.sub("", (text or "").strip())
    start, end = body.find("["), body.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("no json array in model output")
    arr = json.loads(body[start:end + 1])
    out: dict[int, dict] = {}
    for obj in arr if isinstance(arr, list) else []:
        if not isinstance(obj, dict):
            continue
        i = obj.get("i")
        if not isinstance(i, int) or not (0 <= i < len(batch)):
            continue
        zh = str(obj.get("zh_headline") or "").strip()
        if not zh:
            continue
        allowed = set((batch[i].get("mentions") or {}).keys()) or set(batch[i].get("symbols") or [])
        tags = {}
        for sym, t in (obj.get("tags") or {}).items():
            sym = str(sym).upper()
            if sym not in allowed or not isinstance(t, dict):
                continue
            tag = {
                "relevance": t.get("relevance") if t.get("relevance") in RELEVANCE else None,
                "event_type": t.get("event_type") if t.get("event_type") in EVENT_TYPES else "other",
                "direction": t.get("direction") if t.get("direction") in DIRECTIONS else "neutral",
                "importance": t.get("importance") if t.get("importance") in IMPORTANCE else "low",
                "rehash": bool(t.get("rehash")),
            }
            if tag["relevance"] is None:
                continue
            tags[sym] = tag
        summary = str(obj.get("zh_summary") or "").strip()
        if not (batch[i].get("summary") or "").strip():
            summary = ""  # 原文无摘要时不接受模型「补写」
        out[i] = {"zh_headline": zh, "zh_summary": summary[:200], "tags": tags}
    return out


def google_translate(text: str, *, http_get: HttpGet | None = None) -> str | None:
    """兜底: Google 翻译网页端点 (非官方、无 SLA)。失败返回 None, 不抛。

    默认关闭, 需设置 NEWS_GOOGLE_TRANSLATE_FALLBACK=1 显式开启; 使用前请自行确认服务条款。
    """
    if not text:
        return ""
    url = ("https://translate.google.com/translate_a/single?client=at&sl=en&tl=zh-CN&dt=t&q="
           + urllib.parse.quote(text[:500]))
    try:
        if http_get is not None:
            data = http_get(url)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": PUBLIC_HTTP_USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - 固定 https 端点
                data = json.loads(r.read().decode("utf-8"))
        return "".join(seg[0] for seg in (data or [[]])[0] if seg and seg[0]).strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("google translate fallback failed: %s", type(exc).__name__)
        return None


def _needs_work(ev: dict) -> bool:
    tb = ev.get("tagged_by") or {}
    if tb.get("mode") == "rule":  # SEC 公告按表单编号确定性打标, 不送模型
        return False
    return not ev.get("zh_headline") or tb.get("mode") != "llm" or tb.get("prompt_version") != PROMPT_VERSION


async def _default_ai_call(messages, **kw) -> str:
    from app.services.ai_provider import generate_ai_text
    return await generate_ai_text(messages, **kw)


async def translate_batch(batch: list[dict], *, ai_call: AiCall | None = None, model_label: str = "") -> dict[int, dict]:
    ai_call = ai_call or _default_ai_call
    text = await ai_call([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": _user_prompt(batch)}],
                         max_tokens=4000, timeout=120.0, task=TASK)
    return parse_llm_output(text, batch)


def _model_label() -> str:
    try:
        from app.services.ai_provider import task_profile
        return task_profile(TASK).get("model") or ""
    except Exception:  # noqa: BLE001
        return ""


async def translate_pending(data_dir: Path, *, now: datetime | None = None, ai_call: AiCall | None = None,
                            google_get: HttpGet | None = None, max_batches: int = MAX_BATCHES_PER_RUN,
                            priority: set[str] | None = None, time_budget_s: float = TIME_BUDGET_S) -> dict:
    """处理本月与上月分片中尚未完成模型打标的事件 (最新优先)。返回计数, 不抛。"""
    now = now or datetime.now(timezone.utc)
    months = [now, now.replace(day=1) - timedelta(days=1)]  # 本月 + 上月 (月初跨月的事件)
    import time as _time
    stats = {"llm": 0, "fallback": 0, "failed_batches": 0, "pending_after": 0}
    model = _model_label()
    t0 = _time.monotonic()
    # 兜底默认关闭; 显式注入 google_get (测试/调用方自带取数) 时视为开启。
    # 本轮兜底熔断: 首个失败后不再逐条空打 (下一轮自动重试)。
    google_down = google_get is None and not settings.news_google_translate_fallback
    for when in months:
        shard = news_ingest.load_shard(data_dir, when)
        todo = [ev for ev in shard.values() if _needs_work(ev)]
        # 优先: 核心股票 (自选/持仓/可入场卡) 的新闻先译 (用户实际在看), 其次最新优先
        pri = {x.upper() for x in priority or ()}
        todo.sort(key=lambda e: e.get("created_at") or "", reverse=True)
        todo.sort(key=lambda e: 0 if pri & set((e.get("mentions") or {}).keys()) else 1)
        if not todo:
            continue
        changed = False
        batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)][:max_batches]
        max_batches -= len(batches)
        for batch in batches:
            # 开批前就判断: 已用时间 + 本批预计耗时超预算则留到下一轮 (首批总是允许, 保证有进展)
            if stats["llm"] + stats["fallback"] + stats["failed_batches"] and \
                    _time.monotonic() - t0 + EST_BATCH_S > time_budget_s:
                break
            stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                result = await translate_batch(batch, ai_call=ai_call)
            except Exception as exc:  # noqa: BLE001
                logger.warning("news translate batch failed: %s: %s",
                               type(exc).__name__, str(exc)[:160])
                stats["failed_batches"] += 1
                result = {}
            for i, ev in enumerate(batch):
                row = shard[ev["id"]]
                if i in result:
                    row.update(result[i])
                    row["tagged_by"] = {"model": model, "prompt_version": PROMPT_VERSION, "at": stamp, "mode": "llm"}
                    stats["llm"] += 1
                elif not row.get("zh_headline") and not google_down:
                    zh = google_translate(row.get("headline") or "", http_get=google_get)
                    if zh is None:
                        google_down = True
                    if zh:
                        row["zh_headline"] = zh
                        row.setdefault("zh_summary", "")
                        row["tags"] = row.get("tags") or {}
                        row["tagged_by"] = {"model": "google-translate", "prompt_version": None, "at": stamp,
                                            "mode": "fallback"}
                        stats["fallback"] += 1
                changed = True
            # 每批落盘一次: 长批次进度不因进程重启/后续异常而丢 (原子写; 采集在同一锁内串行, 无并发改写)
            news_ingest.save_shard(data_dir, when, shard)
            changed = False
        if changed:
            news_ingest.save_shard(data_dir, when, shard)
        stats["pending_after"] += sum(1 for ev in shard.values() if _needs_work(ev))
        if max_batches <= 0:
            break
    logger.info("news translate: %s", stats)
    return stats


_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_LOCK = __import__("threading").Lock()


def _news_loop() -> asyncio.AbstractEventLoop:
    """新闻翻译专用的长生命周期事件循环 (调度线程内复用)。

    每轮 asyncio.run() 新建/关闭循环时, 本轮创建的 AsyncOpenAI 客户端在循环关闭后才被回收,
    其连接关闭落到已关闭的循环上, 日志刷出 "Task exception was never retrieved / Event loop is closed"。
    复用同一个循环, 客户端关闭始终在活循环上完成。
    """
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            _LOOP = asyncio.new_event_loop()
        return _LOOP


def translate_pending_sync(data_dir: Path, **kw) -> dict:
    """调度线程 (无事件循环) 用的同步入口。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = _news_loop()
        return loop.run_until_complete(translate_pending(data_dir, **kw))
    raise RuntimeError("translate_pending_sync must not be called inside a running event loop")
