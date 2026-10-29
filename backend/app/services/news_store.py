"""新闻读取层 — 按股票/批量查询已落盘新闻 (只读)。供 API、AI 个股分析、决策台影子记账、推送共用。

返回给前端/模型的一律是「中文优先」视图: 有 zh_headline 用中文, 没有时如实标 translated=False
(前端显示原文 + 「未翻译」), 不在读取层做任何翻译或推断。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services import news_ingest

EVENT_LABEL = {"earnings": "财报", "guidance": "业绩指引", "rating": "评级/目标价", "contract": "合同订单", "mna": "并购",
               "product": "产品/技术", "regulatory": "监管/调查", "dilution": "融资稀释", "management": "高管变动",
               "litigation": "诉讼", "macro": "宏观/行业", "other": "其他"}
SOURCE_LABEL = {"alpaca": "Benzinga", "finnhub": "Finnhub", "sec": "SEC 公告"}
_REL_RANK = {"primary": 0, "secondary": 1, "passing": 2}
_IMP_RANK = {"high": 0, "medium": 1, "low": 2}


def _months(now: datetime, days: int) -> list[datetime]:
    out, cur = [], now
    start = now - timedelta(days=days)
    while True:
        out.append(cur)
        first = cur.replace(day=1)
        if first <= start:
            break
        cur = first - timedelta(days=1)
    return out


def _parse(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def load_recent(data_dir: Path, *, days: int = 7, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    rows: dict[str, dict] = {}
    for when in _months(now, days):
        for rid, row in news_ingest.load_shard(data_dir, when).items():
            dt = _parse(row.get("created_at"))
            if dt and since <= dt <= now + timedelta(minutes=5):
                rows[rid] = row
    return sorted(rows.values(), key=lambda r: r.get("created_at") or "", reverse=True)


def view(row: dict, symbol: str, *, now: datetime | None = None) -> dict:
    """单条新闻相对某只股票的展示视图。"""
    now = now or datetime.now(timezone.utc)
    tag = (row.get("tags") or {}).get(symbol) or {}
    dt = _parse(row.get("created_at"))
    tb = row.get("tagged_by") or {}
    refs = row.get("refs") or []
    return {
        "id": row["id"],
        "symbol": symbol,
        "published_at": row.get("created_at"),
        "age_hours": round((now - dt).total_seconds() / 3600, 1) if dt else None,
        "headline": row.get("zh_headline") or row.get("headline") or "",
        "summary": row.get("zh_summary") or "",
        "original_headline": row.get("headline") or "",
        "translated": bool(row.get("zh_headline")),
        "tagged": bool(tag),
        "tag_mode": tb.get("mode"),
        "relevance": tag.get("relevance"),
        "event_type": tag.get("event_type"),
        "event_label": EVENT_LABEL.get(tag.get("event_type") or "", None),
        "direction": tag.get("direction"),
        "importance": tag.get("importance"),
        "rehash": bool(tag.get("rehash")),
        "mention": (row.get("mentions") or {}).get(symbol),
        "scope": row.get("scope"),
        "sources": [SOURCE_LABEL.get(s, s) for s in row.get("sources") or []],
        "url": next((r.get("url") for r in refs if r.get("url")), ""),
        "other_symbols": [s for s in row.get("symbols") or [] if s != symbol][:6],
    }


def is_main(v: dict) -> bool:
    """「主角新闻」: 打标为 primary; 未打标时退回「标题点名」。顺带一提/旧闻重炒不算。"""
    if v["tagged"]:
        return v["relevance"] == "primary" and not v["rehash"]
    return v["mention"] == "headline"


def for_symbol(data_dir: Path, symbol: str, *, days: int = 7, limit: int = 50, main_only: bool = False,
               now: datetime | None = None, rows: list[dict] | None = None) -> list[dict]:
    symbol = symbol.upper()
    rows = rows if rows is not None else load_recent(data_dir, days=days, now=now)
    out = []
    for r in rows:
        if symbol not in (r.get("mentions") or {}):
            continue
        v = view(r, symbol, now=now)
        if main_only and not is_main(v):
            continue
        out.append(v)
    return out[:limit]


TONE_HOURS = 48
TONE_IMPORTANCE = ("high", "medium")


def tone(views: list[dict]) -> dict:
    """机会卡「利空 N / 利多 N」角标 (0.16.0, 10-14 产品决策): 48 小时内、已打标、重要度高或中的主角新闻
    (primary 且非旧闻重炒) 按方向计数; 中性不计。只做标注, 不参与开仓。各附最新 3 条标题供悬停。"""
    out = {"bearish": 0, "bullish": 0, "bearish_heads": [], "bullish_heads": []}
    hits = [v for v in views if v["tagged"] and is_main(v) and v["importance"] in TONE_IMPORTANCE
            and v["age_hours"] is not None and v["age_hours"] <= TONE_HOURS and v["direction"] in ("bearish", "bullish")]
    for v in sorted(hits, key=lambda v: v["age_hours"]):
        d = v["direction"]
        out[d] += 1
        if len(out[f"{d}_heads"]) < 3:
            out[f"{d}_heads"].append(v["headline"])
    return out


def summarize(views: list[dict]) -> dict:
    """角标/机会卡用的摘要: 24h 主角新闻数、是否有高重要度利空、最重要的 1-2 条、48h 利空/利多条数 (tone)。"""
    recent = [v for v in views if v["age_hours"] is not None and v["age_hours"] <= 24 and is_main(v)]
    tagged_main = [v for v in views if is_main(v) and v["tagged"]]
    ranked = sorted(tagged_main, key=lambda v: (_IMP_RANK.get(v["importance"] or "low", 3),
                                                _REL_RANK.get(v["relevance"] or "passing", 3), v["age_hours"] or 1e9))
    top = [v for v in ranked if v["importance"] in ("high", "medium") and (v["age_hours"] or 1e9) <= 48][:2]
    return {
        "count_24h": len(recent),
        "high_bearish_48h": any(v["importance"] == "high" and v["direction"] == "bearish"
                                and (v["age_hours"] or 1e9) <= 48 for v in tagged_main),
        "high_bullish_48h": any(v["importance"] == "high" and v["direction"] == "bullish"
                                and (v["age_hours"] or 1e9) <= 48 for v in tagged_main),
        "top": top,
        "tone": tone(views),
    }


def batch_summary(data_dir: Path, symbols: list[str], *, days: int = 3, now: datetime | None = None) -> dict[str, dict]:
    rows = load_recent(data_dir, days=days, now=now)
    return {s.upper(): summarize(for_symbol(data_dir, s, rows=rows, now=now, limit=200)) for s in symbols if s}


def status(data_dir: Path, *, now: datetime | None = None) -> dict:
    """系统状态页用: 最近采集/翻译情况 (不含密钥)。"""
    now = now or datetime.now(timezone.utc)
    p = Path(data_dir) / "news" / "state.json"
    try:
        state = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        state = {}
    rows = load_recent(data_dir, days=1, now=now)
    return {**state, "events_24h": len(rows),
            "translated_24h": sum(1 for r in rows if r.get("zh_headline")),
            "tagged_24h": sum(1 for r in rows if (r.get("tagged_by") or {}).get("mode") in ("llm", "rule"))}


def status_state(data_dir: Path) -> dict:
    """只读 news/state.json (不统计事件); 读不了返回空。"""
    p = Path(data_dir) / "news" / "state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def write_state(data_dir: Path, **fields) -> None:
    from app.services.fs_utils import atomic_write_text
    p = Path(data_dir) / "news" / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        cur = {}
    cur.update(fields)
    atomic_write_text(p, json.dumps(cur, ensure_ascii=False, indent=1))
