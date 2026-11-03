"""Public factor intake (0.47.0): watch list -> fetch -> keyword pre-filter -> LLM triage -> LLM extract
-> four machine checks -> dedupe -> quick screen + watchlist backtest -> human decides.

Design rules (approved plan v4, 2026-11-02):
- The LLM only reads articles and writes formulas. It never judges value, never writes the factor
  library, never touches trading. Two AI tasks: ``factor_triage`` (cheap, thinking off) and
  ``factor_extract``; both configured in Settings -> AI -> per-task model.
- Four deterministic checks after extraction: quote is verbatim in the article, formula compiles with
  the production DSL, period consistency (a library factor with a period must appear in the article
  with the same indicator + period: stops "RSI(6)" standing in for "RSI(2)"), cross-article dedupe
  by quote hash.
- Only "duplicate of a library factor" (|rank corr| >= 0.9) is auto-archived. Every other failed
  check still lands in "pending": the user decides (no "must beat the engine" gate).
- Merging creates a ``uf_*`` custom factor with status ``watch`` -> it joins next Friday's mining
  scope (mining_schedule.weekly_extra_factors). Never enters 3+3 or trading.
- Fetch every 6 hours; LLM calls capped per US/Eastern day (default 30, configurable); screening and
  backtests only outside 09:00-17:00 ET on trading days (enriched is republished every ~10 s in session).
- Model failures leave items queued; we never silently fall back to another model.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import re
import socket
import threading
import time
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET  # parsed with DTD / entities rejected, see parse_feed

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

CONTRACT = "factor-intake-v1"
FETCH_INTERVAL_S = 6 * 3600
DEFAULT_DAILY_LIMIT = 30
MAX_BODY_CHARS = 14_000
MAX_FEED_BYTES = 4 * 1024 * 1024
DEDUPE_CORR = 0.9
# Only posts published within this many days go to the model (a feed's first fetch returns its whole history,
# e.g. a 2021 post; old posts would eat the daily model budget for days). Undated posts count as fresh.
FRESH_DAYS = 30
CARD_LIMIT = 40  # intake cards returned to the page (pending ones are never cut)
SCREEN_HORIZON = 5
_UA = {"User-Agent": "Mozilla/5.0 (ZenTrade factor intake; self-hosted)"}

# ---------------------------------------------------------------- default watch list (11-02 measured)
DEFAULT_SOURCES: list[dict] = [
    {"id": "concretum", "name": "Concretum", "kind": "person", "channel": "Substack", "url": "https://concretumgroup.substack.com/feed"},
    {"id": "quantitativo", "name": "Quantitativo", "kind": "person", "channel": "Substack", "url": "https://www.quantitativo.com/feed"},
    {"id": "qedges", "name": "Quantifiable Edges", "kind": "person", "channel": "RSS", "url": "https://quantifiableedges.com/feed/"},
    {"id": "alvarez", "name": "Alvarez Quant Trading", "kind": "person", "channel": "RSS", "url": "https://alvarezquanttrading.com/feed/"},
    {"id": "qoppac", "name": "Rob Carver · qoppac", "kind": "person", "channel": "Atom", "url": "https://qoppac.blogspot.com/feeds/posts/default"},
    {"id": "kinlay", "name": "Jonathan Kinlay", "kind": "person", "channel": "RSS", "url": "https://jonathankinlay.com/feed/"},
    {"id": "clenow", "name": "Following the Trend", "kind": "person", "channel": "RSS", "url": "https://www.followingthetrend.com/feed/"},
    # Quantified Strategies 默认关闭 (30 天 20 篇 16 篇付费、0 公式)
    {"id": "qstrat", "name": "Quantified Strategies", "kind": "site", "channel": "Substack", "url": "https://quantifiedstrategies.substack.com/feed", "enabled": False},
    {"id": "quantpedia", "name": "Quantpedia", "kind": "site", "channel": "RSS", "url": "https://quantpedia.com/feed/"},
    {"id": "cxo", "name": "CXO Advisory", "kind": "site", "channel": "RSS", "url": "https://www.cxoadvisory.com/feed/"},
    {"id": "alphaarch", "name": "Alpha Architect", "kind": "site", "channel": "RSS", "url": "https://alphaarchitect.com/feed/"},
    {"id": "allocsmart", "name": "Allocate Smartly", "kind": "site", "channel": "RSS", "url": "https://allocatesmartly.com/feed/"},
    {"id": "robotwealth", "name": "Robot Wealth", "kind": "site", "channel": "RSS", "url": "https://robotwealth.com/feed/"},
    {"id": "quantstart", "name": "QuantStart", "kind": "site", "channel": "RSS", "url": "https://www.quantstart.com/articles/rss/"},
    {"id": "quantocracy", "name": "Quantocracy", "kind": "aggregator", "channel": "FeedBurner", "url": "https://feeds.feedburner.com/Quantocracy"},
    {"id": "quantseeker", "name": "QuantSeeker", "kind": "aggregator", "channel": "Substack", "url": "https://www.quantseeker.com/feed"},
    {"id": "arxivqfin", "name": "arXiv q-fin", "kind": "paper", "channel": "API",
     "url": "https://export.arxiv.org/api/query?search_query=cat:q-fin.TR+OR+cat:q-fin.ST+OR+cat:q-fin.PM&sortBy=submittedDate&sortOrder=descending&max_results=40"},
    {"id": "osap", "name": "OSAP", "kind": "paper", "channel": "Atom", "url": "https://github.com/OpenSourceAP/CrossSection/releases.atom"},
]
SOURCE_KINDS = ("person", "site", "aggregator", "paper")

_POS = re.compile(r"\b(ibs|rsi|stochastic|williams|momentum|reversal|mean.?reversion|overnight|gap|intraday|close|volume|"
                  r"volatility|drawdown|moving average|breakout|pullback|down days?|up days?|signal|indicator|factor|anomal|"
                  r"cross.?section|equit|stocks?|s&p|spy|qqq|nasdaq|russell)\b", re.I)
_NEG = re.compile(r"\b(crypto|bitcoin|solana|defi|lending|bond|treasur|retirement|pension|bank|insurance|options? pricing|"
                  r"market maker|limit order|hawkes|macro|commodit|crude|gold|fx|currenc|etf rotation|sector etf)\b", re.I)
_PAYWALL = re.compile(r"(subscriber section|paid subscribers|upgrade to paid|subscribe to read)", re.I)

_LOCK = threading.Lock()  # watch list + run flag
# Every read-modify-write of items.json (fetch, model queue, screen, submit, decisions) holds this lock, so a
# click on 并入 / 忽略 / 恢复 during a minutes-long model run is never overwritten by the run's stale snapshot.
_ITEMS_LOCK = threading.RLock()
_RUN = {"running": False}


# ---------------------------------------------------------------- storage
def _dir(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "intake"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:  # corrupt state file: keep it for evidence, start clean
        logger.warning("factor intake: unreadable %s (%s)", path.name, type(exc).__name__)
        return default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt else None


def _et_day(ts: datetime | None = None) -> str:
    from app.market_clock import us_clock
    return us_clock.market_datetime(ts or us_clock.now()).date().isoformat()


# ---------------------------------------------------------------- sources & settings
def load_sources(data_dir: Path) -> list[dict]:
    """Saved watch list; first read seeds the defaults (nothing is fetched by seeding)."""
    path = _dir(data_dir) / "sources.json"
    saved = _read_json(path, None)
    if not isinstance(saved, list):
        seeded = [{**src, "enabled": bool(src.get("enabled", True)), "builtin": True} for src in DEFAULT_SOURCES]
        _write_json(path, seeded)
        return seeded
    return [s for s in saved if isinstance(s, dict) and s.get("id") and s.get("url")]


def _save_sources(data_dir: Path, sources: list[dict]) -> None:
    _write_json(_dir(data_dir) / "sources.json", sources)


# RFC 2544 benchmarking range. Transparent proxies in fake-IP mode (Clash / sing-box / Surge)
# answer every domain from 198.18.0.0/15; Python marks it private, which would block every feed.
# It is never a real LAN, so it is allowed. Loopback / RFC 1918 / link-local / CGNAT / multicast stay blocked.
_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")  # carrier-grade NAT / Tailscale-style overlays: internal


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv4Address) and ip in _FAKE_IP_NET:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _blocked_ip(ip.ipv4_mapped)
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _public_http_url(url: str) -> str:
    """Only public http(s) hosts (no loopback / private / link-local targets)."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("只支持 http(s) 地址")
    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and literal in _FAKE_IP_NET:
        raise ValueError("不允许内网或本机地址")  # a literal fake-IP is not a domain resolved by the proxy
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError("域名解析失败") from exc
    for info in infos:
        if _blocked_ip(ipaddress.ip_address(info[4][0])):
            raise ValueError("不允许内网或本机地址")
    return url.strip()


def add_source(data_dir: Path, *, name: str, url: str, kind: str) -> dict:
    if kind not in SOURCE_KINDS:
        raise ValueError(f"类型只能是 {', '.join(SOURCE_KINDS)}")
    name = name.strip()[:60]
    if not name:
        raise ValueError("名称不能为空")
    url = _public_http_url(url)
    with _LOCK:
        sources = load_sources(data_dir)
        if any(s["url"] == url for s in sources):
            raise ValueError("该地址已在监控清单")
        sid = "u" + hashlib.sha1(url.encode()).hexdigest()[:10]
        row = {"id": sid, "name": name, "kind": kind, "channel": "RSS", "url": url, "enabled": True, "builtin": False}
        _save_sources(data_dir, [*sources, row])
    return row


def set_source_enabled(data_dir: Path, source_id: str, enabled: bool) -> dict:
    with _LOCK:
        sources = load_sources(data_dir)
        hit = next((s for s in sources if s["id"] == source_id), None)
        if hit is None:
            raise KeyError(source_id)
        hit["enabled"] = bool(enabled)
        _save_sources(data_dir, sources)
    return hit


def remove_source(data_dir: Path, source_id: str) -> None:
    with _LOCK:
        sources = load_sources(data_dir)
        hit = next((s for s in sources if s["id"] == source_id), None)
        if hit is None:
            raise KeyError(source_id)
        if hit.get("builtin"):
            raise ValueError("内置来源只能关闭, 不能删除")
        _save_sources(data_dir, [s for s in sources if s["id"] != source_id])


def load_settings(data_dir: Path) -> dict:
    raw = _read_json(_dir(data_dir) / "settings.json", {}) or {}
    try:
        limit = int(raw.get("daily_limit", DEFAULT_DAILY_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_DAILY_LIMIT
    return {"daily_limit": max(0, min(limit, 1000))}


def save_settings(data_dir: Path, *, daily_limit: int) -> dict:
    if not isinstance(daily_limit, int) or isinstance(daily_limit, bool) or not 0 <= daily_limit <= 1000:
        raise ValueError("每日上限 0-1000")
    _write_json(_dir(data_dir) / "settings.json", {"daily_limit": daily_limit})
    return load_settings(data_dir)


# ---------------------------------------------------------------- usage ledger (per ET day)
def _usage_path(data_dir: Path) -> Path:
    return _dir(data_dir) / "usage.json"


def usage(data_dir: Path) -> dict:
    u = _read_json(_usage_path(data_dir), {}) or {}
    day = _et_day()
    today = (u.get("days") or {}).get(day) or {}
    last30 = sorted((u.get("days") or {}).items())[-30:]
    return {
        "today": int(today.get("calls", 0)),
        "limit": load_settings(data_dir)["daily_limit"],
        "days30_calls": sum(int(v.get("calls", 0)) for _, v in last30),
        "days30_tokens": sum(int(v.get("tokens", 0)) for _, v in last30),
        "by_task": {t: sum(int((v.get("tasks") or {}).get(t, {}).get("calls", 0)) for _, v in last30) for t in ("factor_triage", "factor_extract")},
        "tokens_by_task": {t: sum(int((v.get("tasks") or {}).get(t, {}).get("tokens", 0)) for _, v in last30) for t in ("factor_triage", "factor_extract")},
    }


def _charge(data_dir: Path, task: str, tokens: int) -> None:
    path = _usage_path(data_dir)
    u = _read_json(path, {}) or {}
    days = u.setdefault("days", {})
    day = days.setdefault(_et_day(), {"calls": 0, "tokens": 0, "tasks": {}})
    day["calls"] = int(day.get("calls", 0)) + 1
    day["tokens"] = int(day.get("tokens", 0)) + int(tokens)
    t = day.setdefault("tasks", {}).setdefault(task, {"calls": 0, "tokens": 0})
    t["calls"] += 1
    t["tokens"] += int(tokens)
    for key in sorted(days)[:-60]:
        days.pop(key, None)
    _write_json(path, u)


def _budget_left(data_dir: Path) -> int:
    return max(0, load_settings(data_dir)["daily_limit"] - usage(data_dir)["today"])


# ---------------------------------------------------------------- items
def _items_path(data_dir: Path) -> Path:
    return _dir(data_dir) / "items.json"


def load_items(data_dir: Path) -> dict[str, dict]:
    raw = _read_json(_items_path(data_dir), {}) or {}
    return raw if isinstance(raw, dict) else {}


def _save_items(data_dir: Path, items: dict[str, dict]) -> None:
    cutoff = (_now() - timedelta(days=120)).isoformat()
    keep = {k: v for k, v in items.items() if (v.get("fetched_at") or "") >= cutoff or v.get("stage") in ("pending", "merged")}
    _write_json(_items_path(data_dir), keep)


def _item_id(link: str, title: str) -> str:
    return hashlib.sha1(f"{link}|{title}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------- fetch & parse
def _text(html: str | None) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(UTC)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


_NS = {"atom": "http://www.w3.org/2005/Atom", "content": "http://purl.org/rss/1.0/modules/content/", "dc": "http://purl.org/dc/elements/1.1/"}


def parse_feed(raw: bytes) -> list[dict]:
    """RSS 2.0 / Atom -> [{title, link, published, body}]. DTD / entity declarations are rejected."""
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ValueError("feed contains a DTD / entity declaration")
    root = ET.fromstring(raw)  # DTD rejected above, so no entity expansion
    out: list[dict] = []
    for it in root.iter("item"):
        body = it.findtext("content:encoded", namespaces=_NS) or it.findtext("description") or ""
        out.append({"title": _text(it.findtext("title")), "link": (it.findtext("link") or "").strip(),
                    "published": _iso(_parse_dt(it.findtext("pubDate") or it.findtext("dc:date", namespaces=_NS))),
                    "body": _text(body)[:MAX_BODY_CHARS]})
    for e in root.findall("atom:entry", _NS):
        link = e.find("atom:link", _NS)
        body = e.findtext("atom:content", namespaces=_NS) or e.findtext("atom:summary", namespaces=_NS) or ""
        out.append({"title": _text(e.findtext("atom:title", namespaces=_NS)), "link": (link.get("href") if link is not None else "") or "",
                    "published": _iso(_parse_dt(e.findtext("atom:published", namespaces=_NS) or e.findtext("atom:updated", namespaces=_NS))),
                    "body": _text(body)[:MAX_BODY_CHARS]})
    return [x for x in out if x["title"]]


def _fetch(url: str, timeout: float = 20.0, *, limit: int = MAX_FEED_BYTES) -> bytes:
    """GET a public URL; every redirect hop is re-checked (no bounce to loopback / private hosts)."""
    from urllib.parse import urljoin

    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=False, headers=_UA) as client:
        for _hop in range(6):
            _public_http_url(url)
            with client.stream("GET", url) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    url = urljoin(url, resp.headers["location"])
                    continue
                resp.raise_for_status()
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("response too large")
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ValueError("too many redirects")


def prefilter(title: str, body: str) -> tuple[str, str | None]:
    """Keyword pre-filter (no model): ('drop', reason) | ('paywall', None) | ('llm', None)."""
    text = f"{title} {body}"
    pos = len({m.lower() for m in _POS.findall(text)})
    neg = len({m.lower() for m in _NEG.findall(text)})
    if pos < 3 or neg > pos:
        return "drop", "关键词不相关"
    tail = body[-160:].lower()
    if _PAYWALL.search(text) or (len(body) < 1200 and "read more" in tail):
        return "paywall", None
    return "llm", None


def _fresh(published: str | None, now: datetime | None = None) -> bool:
    d = _parse_dt(published)
    return d is None or ((now or _now()) - d) <= timedelta(days=FRESH_DAYS)


def fetch_all(data_dir: Path, *, now: datetime | None = None) -> dict:
    """Fetch every enabled source, pre-filter new posts, store them. Never calls a model."""
    now = now or _now()
    sources = load_sources(data_dir)
    state = _read_json(_dir(data_dir) / "fetch_state.json", {}) or {}
    fetched: list[tuple[dict, list[dict]]] = []
    for src in sources:
        st = state.setdefault(src["id"], {})
        if not src.get("enabled", True):
            continue
        try:
            entries = parse_feed(_fetch(src["url"]))
            st.update({"last_ok": _iso(now), "error": None, "fails": 0, "last_count": len(entries)})
        except Exception as exc:  # one bad source never stops the others
            st.update({"last_error_at": _iso(now), "error": type(exc).__name__, "fails": int(st.get("fails", 0)) + 1})
            continue
        fetched.append((src, entries))
    added = 0
    with _ITEMS_LOCK:
        items = load_items(data_dir)
        for src, entries in fetched:
            for e in entries:
                iid = _item_id(e["link"], e["title"])
                if iid in items:
                    continue
                stage, reason = prefilter(e["title"], e["body"])
                if stage == "llm" and not _fresh(e.get("published"), now):
                    stage, reason = "stale", f"超过 {FRESH_DAYS} 天"
                items[iid] = {"id": iid, "source": src["id"], "source_name": src["name"], "title": e["title"], "link": e["link"],
                              "published": e["published"], "body": e["body"], "fetched_at": _iso(now),
                              "stage": {"drop": "archived", "paywall": "archived", "stale": "archived", "llm": "queued"}[stage],
                              "reason": reason or ("付费" if stage == "paywall" else None), "prefilter": stage}
                added += 1
        _save_items(data_dir, items)
    _write_json(_dir(data_dir) / "fetch_state.json", state)
    return {"sources": sum(1 for s in sources if s.get("enabled", True)), "added": added}


# ---------------------------------------------------------------- LLM
_TRIAGE_PROMPT = (
    "你是美股量化研究助理。判断下面这篇文章是否给出了可以用美股个股日线数据计算的交易因子或买卖规则。只判断, 不抽取。\n"
    "market: us_stock(美股个股) / us_index(SPY、QQQ 等指数或 ETF 择时) / futures / crypto / other。\n"
    "kind: factor(给每只股票打分排序的公式) / rule(明确的买卖条件) / none(观点、宏观、配置、需要我们没有的数据)。\n"
    "paywall: 正文只是预告、规则在付费区或 Read more 之后 = true。\n"
    '只输出 JSON: {"market":"...","kind":"...","paywall":false}\n\n来源: {SRC}  标题: {TITLE}\n正文:\n{BODY}'
)

_EXTRACT_PROMPT = (
    "你是美股量化研究助理。从文章里抽取可以用日线数据计算的交易因子或交易规则。只抽文章里明确写出的东西, 不补全、不发挥、不编造。\n"
    "数据: 美股个股日线。基础列: {BASE}。\n因子库已有(id 中文名): {LIB}\n"
    "公式算子: {OPS}。时序算子第二个参数是窗口天数(整数常量), 如 ts_mean(close, 20); rank/zscore 是当日截面算子; "
    "逻辑与或非写作中缀 a and b / a or b / not a, 不要写 and(a, b); 比较用 > < >= <=; if_else(cond, a, b)。\n"
    "已有出卡规则(命中就在 dup_rule 写它的 id, 不要重复抽): {RULES}\n"
    "规则: 每条必须带 quote = 从原文逐字复制、能证明这条定义的那一句(<=300 字符, 不翻译不改写), 找不到原句就不要输出这条; "
    "dsl 只用上面的列、因子 id 和算子, 写不出填 null 并在 missing 写缺什么; 原文的指标周期在库里没有(如 RSI(2))就填 null, 不要用相近的列代替; "
    "direction: high=分数越高越好, low=越低越好。\n"
    '只输出 JSON: {"summary_zh":"<=40字","items":[{"name_zh":"<=12字","quote":"原文逐字","dsl":"... 或 null",'
    '"direction":"high|low|null","horizon_days":5,"missing":[],"dup_rule":null,"reported":null}]}\n\n'
    "来源: {SRC}  标题: {TITLE}\n正文:\n{BODY}"
)


def _vocab() -> dict:
    from app.factors.dsl import BASE_COLUMNS, OPERATORS
    from app.factors.registry import selectable_factors

    lib = [(s.id, s.label) for s in selectable_factors(asset_type="stock") if s.kind in ("base", "virtual")]
    return {"base": ", ".join(sorted(BASE_COLUMNS)), "ops": ", ".join(sorted(OPERATORS)), "lib": "; ".join(f"{a} {b}" for a, b in lib)}


def _rules_vocab(data_dir: Path) -> str:
    try:
        from app.services import strategy_evaluation_runner as runner
        eng = runner._strategy_engine(Path(data_dir))
        return "; ".join(f"{s.meta.get('id')} {s.meta.get('name')}" for s in eng.strategy_definitions()
                         if not s.meta.get("research_only") and s.meta.get("id"))
    except Exception:  # vocabulary is a hint only
        return ""


def _fill(template: str, **kw) -> str:
    out = template
    for key, value in kw.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def _parse_json(text: str | None) -> dict | None:
    if not text:
        return None
    s = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


async def _ask(task: str, prompt: str, *, max_tokens: int) -> str:
    from app.services.ai_provider import generate_ai_text
    return await generate_ai_text([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=max_tokens,
                                  timeout=150.0, task=task)


_LOOP: asyncio.AbstractEventLoop | None = None


def _loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP


def _call(data_dir: Path, task: str, prompt: str, *, max_tokens: int) -> str:
    """One metered model call. Raises on failure (item stays queued)."""
    text = _loop().run_until_complete(_ask(task, prompt, max_tokens=max_tokens))
    _charge(data_dir, task, (len(prompt) + len(text or "")) // 4)
    return text or ""


# ---------------------------------------------------------------- checks
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def quote_verbatim(quote: str, article: str) -> bool:
    q = _norm(quote)
    return len(q) >= 12 and q in _norm(article)


_PERIOD_SPEC = [
    (r"^rsi_(\d+)$", r"rsi"), (r"^momentum_(\d+)d$", r"(momentum|return|performance)"), (r"^e?ma(\d+)_bias$", r"(moving average|sma|ema|ma)"),
    (r"^vol_ratio_(\d+)d$", r"volume"), (r"^annual_vol_(\d+)d$", r"volatility"), (r"^max_ret_(\d+)d$", r"(max|maximum)"),
    (r"^up_days_(\d+)d$", r"(up days|positive)"),
]


def _mentions(text: str, n: str, kw: str) -> bool:
    t = text.lower()
    pats = [rf"{kw}\s*\(\s*{n}\s*\)", rf"{n}[- ]?(day|period|bar|days)\s+{kw}", rf"{kw}\s+with\s+a\s+{n}[- ]?(day|period)",
            rf"{kw}\s*{n}[- ]?(day|period)", rf"{kw}\s*[-_]?\s*{n}\b(?=\s*(indicator|lookback|setting|period))"]
    return any(re.search(p, t) for p in pats)


def period_mismatches(dsl: str, article: str) -> list[str]:
    """Library factors with a period that the article never names with the same indicator + period."""
    bad = []
    for tok in re.findall(r"[a-z_]+\d+[a-z_]*", dsl or ""):
        for pat, kw in _PERIOD_SPEC:
            m = re.match(pat, tok)
            if m and not _mentions(article, m.group(1), kw):
                bad.append(tok)
    return sorted(set(bad))


def _compile(dsl: str):
    from app.factors.dsl import compile_formula
    return compile_formula(dsl)


# ---------------------------------------------------------------- triage + extract (model calls)
def _known_quote_hashes(items: dict[str, dict]) -> set[str]:
    """Quote hashes of every formula already extracted (stored per formula, not on the item)."""
    return {f["quote_hash"] for it in items.values() for f in it.get("formulas") or [] if f.get("quote_hash")}


def _commit(data_dir: Path, item_id: str, expected_stage: str, update) -> bool:
    """Apply `update(item)` under the items lock, only if the item is still in `expected_stage`."""
    with _ITEMS_LOCK:
        items = load_items(data_dir)
        it = items.get(item_id)
        if it is None or it.get("stage") != expected_stage:
            return False
        update(it)
        _save_items(data_dir, items)
        return True


def _triage_result(it: dict, text: str) -> None:
    j = _parse_json(text) or {}
    it["triage"] = {k: j.get(k) for k in ("market", "kind", "paywall")}
    it.pop("last_error", None)
    if j.get("paywall"):
        it.update(stage="archived", reason="付费")
    elif j.get("market") != "us_stock":
        it.update(stage="archived", reason={"us_index": "指数择时", "futures": "期货", "crypto": "加密"}.get(str(j.get("market")), "非美股个股"))
    elif j.get("kind") not in ("factor", "rule"):
        it.update(stage="archived", reason="无可算定义")
    else:
        it["stage"] = "to_extract"


def _extract_result(data_dir: Path, item_id: str, text: str) -> None:
    def update(it: dict) -> None:
        it.pop("last_error", None)
        j = _parse_json(text)
        if j is None:
            it.update(stage="archived", reason="模型输出不可解析")
            return
        it["summary"] = j.get("summary_zh")
        # dedupe against the latest state, read under the same lock as the write
        formulas = _check_items(it, j.get("items") or [], _known_quote_hashes(load_items(data_dir)))
        if formulas:
            it["formulas"] = formulas
            it["stage"] = "to_screen"
        else:
            it.update(stage="archived", reason=it.get("reason") or "无可编译公式")

    _commit(data_dir, item_id, "to_extract", update)


def process_queue(data_dir: Path, *, max_calls: int | None = None) -> dict:
    """Run triage / extract on queued items until the per-day budget is used. Model errors keep items queued.

    Model calls run outside the items lock; each result is written back only if the item is still in the
    stage it was read in (a decision made meanwhile wins)."""
    budget = _budget_left(data_dir) if max_calls is None else min(max_calls, _budget_left(data_dir))
    stats = {"triaged": 0, "extracted": 0, "errors": 0, "budget_left": budget}
    with _ITEMS_LOCK:  # posts queued before the freshness window existed (0.47.0 first fetch)
        items = load_items(data_dir)
        stale = [it for it in items.values() if it.get("stage") == "queued" and not _fresh(it.get("published"))]
        for it in stale:
            it.update(stage="archived", reason=f"超过 {FRESH_DAYS} 天", prefilter="stale")
        if stale:
            _save_items(data_dir, items)
    order = [it["id"] for it in sorted(items.values(), key=lambda x: x.get("published") or "", reverse=True)
             if it.get("stage") in ("queued", "to_extract")]
    vocab = None
    rules = None
    for item_id in order:
        if budget <= 0:
            break
        it = load_items(data_dir).get(item_id)
        if it is None:
            continue
        try:
            if it["stage"] == "queued":
                text = _call(data_dir, "factor_triage", _fill(_TRIAGE_PROMPT, SRC=it["source_name"], TITLE=it["title"], BODY=it["body"]), max_tokens=400)
                budget -= 1
                stats["triaged"] += 1
                _commit(data_dir, item_id, "queued", lambda x, t=text: _triage_result(x, t))
                it = load_items(data_dir).get(item_id) or {}
            if it.get("stage") == "to_extract" and budget > 0:
                vocab = vocab or _vocab()
                rules = rules if rules is not None else _rules_vocab(data_dir)
                prompt = _fill(_EXTRACT_PROMPT, BASE=vocab["base"], LIB=vocab["lib"], OPS=vocab["ops"], RULES=rules,
                               SRC=it["source_name"], TITLE=it["title"], BODY=it["body"])
                text = _call(data_dir, "factor_extract", prompt, max_tokens=4000)
                budget -= 1
                stats["extracted"] += 1
                _extract_result(data_dir, item_id, text)
        except Exception as exc:  # model unavailable: keep queued, never switch models silently
            stats["errors"] += 1
            message = f"{type(exc).__name__}: {str(exc)[:160]}"
            _commit(data_dir, item_id, it.get("stage", ""), lambda x, m=message: x.update(last_error=m))
            logger.warning("factor intake model call failed: %s", type(exc).__name__)
            break
    stats["budget_left"] = budget
    return stats


def _check_items(item: dict, raw: list, seen_quotes: set) -> list[dict]:
    article = f"{item['title']} {item['body']}"
    out = []
    reasons = []
    for x in raw[:6]:
        if not isinstance(x, dict):
            continue
        quote = str(x.get("quote") or "")
        dsl = (x.get("dsl") or "").strip() if isinstance(x.get("dsl"), str) else ""
        if not quote_verbatim(quote, article):
            reasons.append("原句对不上")
            continue
        qh = hashlib.sha1(_norm(quote).encode()).hexdigest()[:16]
        if qh in seen_quotes:
            reasons.append("跨文重复")
            continue
        if not dsl:
            reasons.append("缺数据: " + "、".join(str(m) for m in (x.get("missing") or [])[:2]) if x.get("missing") else "写不出公式")
            continue
        compiled = _compile(dsl)
        if not compiled.ok:
            reasons.append(f"编译失败 {compiled.errors[0].code}")
            continue
        bad = period_mismatches(dsl, article)
        if bad:
            reasons.append("周期不一致 " + ",".join(bad))
            continue
        seen_quotes.add(qh)
        direction = x.get("direction") if x.get("direction") in ("high", "low") else "high"
        out.append({"name": str(x.get("name_zh") or "采集因子")[:16], "quote": quote[:300], "quote_hash": qh, "dsl": dsl,
                    "direction": direction, "warmup": int(compiled.warmup_bars), "deps": sorted(compiled.dependencies),
                    "cross_sectional": bool(compiled.cross_sectional), "reported": x.get("reported"),
                    "dup_rule": x.get("dup_rule"),
                    "checks": {"quote": True, "compile": True, "period": True}})
    if not out and reasons:
        item["reason"] = reasons[0][:40]
    return out


# ---------------------------------------------------------------- screen (IC + dedupe + watchlist backtest)
def _screen_one(repo, engine, formula: dict, pool: list[str], watchlist: list[str], end: date) -> dict:
    import math

    import polars as pl

    from app.backtest.factor import FactorBacktestService
    from app.factors import registry
    from app.factors.store import register_definition

    tmp_id = "uf_intake_" + formula["quote_hash"][:10]
    registry.unregister_factor(tmp_id)
    register_definition({"id": tmp_id, "kind": "custom", "label": formula["name"], "formula": formula["dsl"],
                         "status": "draft", "direction": formula["direction"]})
    try:
        lib = [s.id for s in registry.selectable_factors(asset_type="stock")
               if s.kind in ("base", "virtual") and not s.pit and "turnover" not in s.id
               and not (s.dependencies & {"turnover_rate", "total_shares", "float_shares"})]
        start = date(2023, 1, 3)
        panel = engine.load_panel(pool, start - timedelta(days=420), end,
                                  columns=["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"])
        panel = FactorBacktestService._compute_missing_factors(panel, {tmp_id, *lib}).sort(["symbol", "date"])
        panel = panel.with_columns((pl.col("close").shift(-SCREEN_HORIZON).over("symbol") / pl.col("close") - 1).alias("_fwd"))
        win = panel.filter(pl.col("date") >= start)
        d = (win.filter(pl.col(tmp_id).is_finite() & pl.col("_fwd").is_not_null()).group_by("date")
             .agg(pl.corr(pl.col(tmp_id).rank(), pl.col("_fwd").rank()).alias("ic"), pl.len().alias("n"))
             .filter((pl.col("n") >= 30) & pl.col("ic").is_finite()).sort("date"))
        ic_vals = d["ic"].to_numpy()
        ic = float(ic_vals.mean()) if ic_vals.size else None
        t = None
        if ic_vals.size >= 30:
            e = ic_vals - ic_vals.mean()
            s = (e @ e) / ic_vals.size
            for k in range(1, SCREEN_HORIZON + 1):
                s += 2 * (1 - k / (SCREEN_HORIZON + 1)) * (e[k:] @ e[:-k]) / ic_vals.size
            t = float(ic_vals.mean() / math.sqrt(s / ic_vals.size)) if s > 0 else None
        best_id, best = None, 0.0
        for col in lib:
            if col not in win.columns:
                continue
            c = (win.filter(pl.col(tmp_id).is_finite() & pl.col(col).is_finite()).group_by("date")
                 .agg(pl.corr(pl.col(tmp_id).rank(), pl.col(col).rank()).alias("c"), pl.len().alias("n"))
                 .filter((pl.col("n") >= 30) & pl.col("c").is_finite()))
            if c.is_empty():
                continue
            v = float(c["c"].mean())
            if abs(v) > abs(best):
                best_id, best = col, v
        out = {"ic": None if ic is None else round(ic, 4), "t": None if t is None else round(t, 2),
               "nearest": best_id, "nearest_label": _label(best_id),
               "corr": round(best, 3), "duplicate": abs(best) >= DEDUPE_CORR}
        if not out["duplicate"] and watchlist:
            out["backtest"] = _watchlist_backtest(repo, tmp_id, formula["direction"], watchlist, start, end)
        return out
    finally:
        registry.unregister_factor(tmp_id)


def _label(fid: str | None) -> str | None:
    from app.factors.registry import get_factor
    spec = get_factor(fid) if fid else None
    return spec.label if spec is not None else fid


def _watchlist_backtest(repo, factor_id: str, direction: str, symbols: list[str], start: date, end: date) -> dict:
    from app.services import factor_discover
    return factor_discover.backtest_scoring(repo, {factor_id: 1.0}, {factor_id: direction}, symbols, start, end)


def screen_pending(data_dir: Path, repo, *, now: datetime | None = None) -> dict:
    """Quick-screen + dedupe + watchlist backtest for extracted formulas.

    Off-hours only (enriched churns in session); the heavy part runs in a spawned worker, the parent
    owns items.json."""
    from app.services import factor_discover

    if not factor_discover.can_run_heavy(now):
        return {"skipped": "session"}
    items = load_items(data_dir)
    todo = [(it["id"], k, f) for it in items.values() if it.get("stage") == "to_screen"
            for k, f in enumerate(it.get("formulas") or []) if not f.get("screen")]
    if not todo and not any(it.get("stage") == "to_screen" for it in items.values()):
        return {"screened": 0}
    if todo:
        from app.backtest.worker import make_worker_task, run_worker_task
        from app.services.heavy_job_limiter import shared_heavy_job_limiter

        cfg = {"op": "screen", "formulas": [{"key": f"{iid}:{k}", **{x: f[x] for x in ("name", "dsl", "direction", "quote_hash")}}
                                            for iid, k, f in todo],
               "watchlist": factor_discover.watchlist_symbols()}
        with shared_heavy_job_limiter.slot("normal"):
            res = run_worker_task(make_worker_task("factor_lab", Path(data_dir), cfg))
        screens = res.get("screens") or {}
    else:
        screens = {}
    with _ITEMS_LOCK:
        items = load_items(data_dir)  # re-read: decisions may have landed meanwhile
        for iid, k, _f in todo:
            it = items.get(iid)
            if it is None or it.get("stage") != "to_screen" or k >= len(it.get("formulas") or []):
                continue
            it["formulas"][k]["screen"] = screens.get(f"{iid}:{k}") or {"error": "missing"}
        n = 0
        for it in items.values():
            if it.get("stage") != "to_screen" or any(not f.get("screen") for f in it.get("formulas") or []):
                continue
            pend = [f for f in it.get("formulas") or [] if not (f.get("screen") or {}).get("duplicate")]
            if pend:
                it["stage"] = "pending"
            else:
                first = (it.get("formulas") or [{}])[0].get("screen") or {}
                it.update(stage="archived", reason=f"重复 · {first.get('nearest_label') or '库内因子'}", duplicate=True)
            it["screened_at"] = _iso(_now())
            n += 1
        _save_items(data_dir, items)
    return {"screened": n}


def screen_impl(data_dir: Path, repo, cfg: dict) -> dict:
    """Worker side: IC / dedupe / watchlist backtest for each formula (temporary registry entries only)."""
    from app.backtest.engine import BacktestEngine
    from app.services.mining_pool import build_mining_pool

    engine = BacktestEngine(repo)
    pool = list(build_mining_pool(Path(data_dir)).symbols)
    watch = list(cfg.get("watchlist") or [])
    end = repo.latest_enriched_date("stock") or date.today()
    screens = {}
    for f in cfg.get("formulas") or []:
        try:
            screens[f["key"]] = _screen_one(repo, engine, f, pool, watch, end)
        except Exception as exc:  # one formula failing never stops the batch
            logger.warning("factor intake screen failed: %s", type(exc).__name__)
            screens[f["key"]] = {"error": type(exc).__name__}
    return {"screens": screens}


# ---------------------------------------------------------------- tick (scheduler) & submission
def _due(data_dir: Path, now: datetime) -> bool:
    st = _read_json(_dir(data_dir) / "run_state.json", {}) or {}
    last = _parse_dt(st.get("last_fetch"))
    return last is None or (now - last).total_seconds() >= FETCH_INTERVAL_S


def tick(data_dir: Path, repo, *, now: datetime | None = None, force: bool = False) -> dict:
    """Scheduler entry (every 30 min wake-up): fetch every 6 h, then model queue within budget, then screen off-hours."""
    now = now or _now()
    with _LOCK:
        if _RUN["running"]:
            return {"skipped": "running"}
        _RUN["running"] = True
    try:
        out: dict[str, Any] = {}
        if force or _due(data_dir, now):
            out["fetch"] = fetch_all(data_dir, now=now)
            st = _read_json(_dir(data_dir) / "run_state.json", {}) or {}
            st["last_fetch"] = _iso(now)
            _write_json(_dir(data_dir) / "run_state.json", st)
        out["model"] = process_queue(data_dir)
        out["screen"] = screen_pending(data_dir, repo, now=now)
        st = _read_json(_dir(data_dir) / "run_state.json", {}) or {}
        st["last_tick"] = _iso(now)
        st["last_result"] = {k: v for k, v in out.items()}
        _write_json(_dir(data_dir) / "run_state.json", st)
        return out
    finally:
        _RUN["running"] = False


def submit(data_dir: Path, *, url: str = "", text: str = "", title: str = "") -> dict:
    """Manual submission (link / article text / Pine / formula): enters at the extract step."""
    body = text.strip()
    link = url.strip()
    if link and not body:
        body = _text(_fetch(link).decode("utf-8", errors="replace"))
    if not body:
        raise ValueError("链接或正文至少填一个")
    iid = _item_id(link or hashlib.sha1(body.encode()).hexdigest(), title or body[:40])
    with _ITEMS_LOCK:
        items = load_items(data_dir)
        items[iid] = {"id": iid, "source": "manual", "source_name": "投递", "title": (title or body[:60]).strip(), "link": link,
                      "published": _iso(_now()), "body": body[:MAX_BODY_CHARS], "fetched_at": _iso(_now()), "stage": "to_extract",
                      "reason": None, "prefilter": "manual"}
        _save_items(data_dir, items)
    return {"id": iid, "stage": "to_extract"}


# ---------------------------------------------------------------- decisions
def merge(data_dir: Path, item_id: str, formula_index: int = 0) -> dict:
    """Human decision: create a custom factor (status watch) from a pending formula. Joins next Friday's mining."""
    with _ITEMS_LOCK:
        return _merge_locked(data_dir, item_id, formula_index)


def _merge_locked(data_dir: Path, item_id: str, formula_index: int) -> dict:
    from app.factors import store

    items = load_items(data_dir)
    it = items.get(item_id)
    if it is None or it.get("stage") != "pending":
        raise KeyError(item_id)
    formulas = it.get("formulas") or []
    if not 0 <= formula_index < len(formulas):
        raise KeyError(item_id)
    f = formulas[formula_index]
    fid = "uf_" + re.sub(r"[^a-z0-9_]", "", f"ci_{f['quote_hash'][:12]}")
    definition = {
        "id": fid, "kind": "custom", "version": 1, "label": f["name"][:32], "group": "采集", "formula": f["dsl"],
        "description": f"{it.get('source_name') or ''} · {(it.get('published') or '')[:10]} · {(it.get('title') or '')[:80]}"[:500],
        "direction": f["direction"], "status": "watch",
        "source": {"type": "intake", "item": item_id, "url": it.get("link"), "quote": f.get("quote"), "title": it.get("title")},
        "created_at": store._now(), "updated_at": store._now(),
    }
    existing = {d.get("id") for d in store.load_all(data_dir)}
    if fid not in existing:
        store.to_spec(definition)
        with contextlib.suppress(ValueError):  # already registered in this process
            store.register_definition(definition)
        store.save_one(data_dir, definition)
    f["merged_id"] = fid
    it["stage"] = "merged"
    it["decided_at"] = _iso(_now())
    _save_items(data_dir, items)
    return {"id": fid, "status": "watch"}


def ignore(data_dir: Path, item_id: str) -> dict:
    stamp = _iso(_now())
    if not _commit(data_dir, item_id, "pending", lambda it: it.update(stage="archived", reason="已忽略", decided_at=stamp)):
        raise KeyError(item_id)
    return {"id": item_id, "stage": "archived"}


def restore(data_dir: Path, item_id: str) -> dict:
    with _ITEMS_LOCK:
        it = load_items(data_dir).get(item_id)
        if it is None or not it.get("formulas"):
            raise KeyError(item_id)
        stamp = _iso(_now())
        if not _commit(data_dir, item_id, "archived", lambda x: x.update(stage="pending", reason=None, restored_at=stamp)):
            raise KeyError(item_id)
    return {"id": item_id, "stage": "pending"}


# ---------------------------------------------------------------- read model for the page
def overview(data_dir: Path, *, days: int = 30) -> dict:
    items = load_items(data_dir)
    cutoff = _now() - timedelta(days=days)
    # 30-day window by publish date (fetch time for undated posts), same window the model queue uses
    recent = [it for it in items.values()
              if (_parse_dt(it.get("published")) or _parse_dt(it.get("fetched_at")) or cutoff) >= cutoff]
    sources = load_sources(data_dir)
    fstate = _read_json(_dir(data_dir) / "fetch_state.json", {}) or {}
    per = {s["id"]: {"n30": 0, "paywall": 0, "llm": 0, "formulas": 0} for s in sources}
    for it in recent:
        p = per.get(it.get("source"))
        if p is None:
            continue
        p["n30"] += 1
        p["paywall"] += 1 if it.get("reason") == "付费" else 0
        p["llm"] += 1 if it.get("prefilter") == "llm" else 0
        p["formulas"] += len(it.get("formulas") or [])
    src_rows = []
    for s in sources:
        st = fstate.get(s["id"]) or {}
        status = "off" if not s.get("enabled", True) else ("failed" if st.get("error") else ("active" if per[s["id"]]["n30"] else ("idle" if st.get("last_ok") else "new")))
        src_rows.append({**{k: s.get(k) for k in ("id", "name", "kind", "channel", "url", "builtin")}, "enabled": bool(s.get("enabled", True)),
                         **per[s["id"]], "status": status, "last_ok": st.get("last_ok"), "error": st.get("error")})
    funnel = {
        "fetched": len(recent),
        "llm": sum(1 for it in recent if it.get("prefilter") in ("llm", "manual")),
        "us_stock": sum(1 for it in recent if (it.get("triage") or {}).get("market") == "us_stock" or it.get("prefilter") == "manual"),
        "defined": sum(1 for it in recent if it.get("stage") in ("to_screen", "pending", "merged") or it.get("formulas") or it.get("summary")),
        "formulas": sum(len(it.get("formulas") or []) for it in recent),
        "pending": sum(1 for it in items.values() if it.get("stage") == "pending"),
    }
    cards = []
    for it in sorted(items.values(), key=lambda x: x.get("published") or "", reverse=True):
        if not it.get("formulas") or it.get("stage") not in ("pending", "archived", "merged", "to_screen"):
            continue
        cards.append({k: it.get(k) for k in ("id", "source", "source_name", "title", "link", "published", "stage", "reason", "summary", "duplicate")}
                     | {"formulas": [{k: f.get(k) for k in ("name", "quote", "dsl", "direction", "warmup", "screen", "merged_id", "checks")} for f in it["formulas"]]})
    # every pending card is shown (the flow bar / tab badge count them); only decided ones are capped
    pending_cards = [c for c in cards if c["stage"] == "pending"]
    other_cards = [c for c in cards if c["stage"] != "pending"]
    cards = sorted(pending_cards + other_cards[:max(0, CARD_LIMIT - len(pending_cards))],
                   key=lambda c: c.get("published") or "", reverse=True)
    st = _read_json(_dir(data_dir) / "run_state.json", {}) or {}
    return {"contract": CONTRACT, "days": days, "funnel": funnel, "cards": cards, "sources": src_rows,
            "usage": usage(data_dir), "settings": load_settings(data_dir), "last_fetch": st.get("last_fetch"),
            "last_tick": st.get("last_tick"), "queued": sum(1 for it in items.values() if it.get("stage") in ("queued", "to_extract")),
            "last_error": next((it.get("last_error") for it in sorted(items.values(), key=lambda x: x.get("fetched_at") or "", reverse=True) if it.get("last_error") and it.get("stage") in ("queued", "to_extract")), None)}


def running() -> bool:
    return bool(_RUN["running"])


def _since(ts: str | None) -> float | None:
    d = _parse_dt(ts)
    return None if d is None else time.time() - d.timestamp()
