"""个股新闻采集 — Alpaca(Benzinga) 主源 + Finnhub 补源 + SEC 8-K/6-K 官方公告。

2026-10-02 产品决策:
  数据源「Alpaca(Benzinga) 主 + Finnhub 补 + SEC 8-K，不做财联社」。
  覆盖范围「自选 + 持仓 + 机会卡候选」并集 (方案文档写的「24 只池」实测无对应固定清单，
  已按推荐项滚正为该并集, 范围更全)。

职责: 拉取三源原始新闻 → 跨源聚簇去重 (24 小时窗口内标题高度相似视为同一事件) →
按来源自带的股票标签做相关性标记 (规则, 非模型) → 落盘。翻译/打标由 news_translate.py
负责 (职责分离: 本模块只产出「有哪些事件、关联哪些股票、原文是什么」)。

落盘: data/news/YYYY-MM.jsonl，每行一条已聚簇事件 (NDJSON, 增量追加/改写同月分片)。
不知道: AI、HTTP 路由、前端。
"""
from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_8K_FORMS = {"8-K", "8-K/A", "6-K", "6-K/A"}  # 6-K: 外国发行人 (TSM/SSL/CVE 等) 的等价公告
CLUSTER_WINDOW = timedelta(hours=24)
FOCUSED_MAX_SYMBOLS = 3  # 聚焦判定: 一条新闻涉及的股票数 <= 3 才算「主要讲这几只」

# Alpaca/SEC 返回 dict, Finnhub company-news 返回 list —— 统一用 Any 返回类型, 各调用方自行判形
HttpGet = Callable[..., Any]


def _http_json(url: str, headers: dict | None = None, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "zentrade"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - 固定 https 端点
        return json.loads(r.read() or b"null") or {}


def alpaca_credentials() -> tuple[str, str] | None:
    """统一规则 (app/credentials.py), 设置页填的 Alpaca Key 同样生效。"""
    from app import credentials
    key, secret, _src = credentials.alpaca()
    return (key, secret) if key and secret else None


def finnhub_token() -> str | None:
    from app import credentials
    return credentials.finnhub()


_WS_RE = re.compile(r"\s+")


def clean_text(text: str | None) -> str:
    """源文本清洗: HTML 实体还原 (&amp; → &), 空白折叠 (Benzinga 摘要常带成串空行)。"""
    return _WS_RE.sub(" ", html.unescape(text or "")).strip()


# ── SEC 8-K item → 中文含义与确定性标签 (官方表单编号含义固定, 不交给模型猜) ──
# importance 只在结构性重大事项给 high; direction 仅对结构性不利事项 (稀释/退市/财报不可依赖/破产/减值) 标 bearish,
# 其余一律 neutral (公告本身不说明好坏)。
SEC_ITEMS = {
    "1.01": ("签订重大协议", "contract", "medium", "neutral"),
    "1.02": ("终止重大协议", "contract", "medium", "neutral"),
    "1.03": ("破产或接管", "regulatory", "high", "bearish"),
    "2.01": ("完成收购或资产处置", "mna", "high", "neutral"),
    "2.02": ("披露经营业绩", "earnings", "high", "neutral"),
    "2.03": ("新增重大债务", "other", "medium", "neutral"),
    "2.05": ("重组/退出成本", "other", "medium", "neutral"),
    "2.06": ("重大资产减值", "other", "medium", "bearish"),
    "3.01": ("退市或不符上市规则通知", "regulatory", "high", "bearish"),
    "3.02": ("未注册发行股份 (稀释)", "dilution", "high", "bearish"),
    "3.03": ("股东权利重大变更", "other", "medium", "neutral"),
    "4.01": ("更换审计师", "management", "medium", "neutral"),
    "4.02": ("既往财报不可再依赖", "regulatory", "high", "bearish"),
    "5.01": ("控制权变更", "mna", "high", "neutral"),
    "5.02": ("董事/高管变动", "management", "medium", "neutral"),
    "5.03": ("修改公司章程", "other", "low", "neutral"),
    "5.07": ("股东大会表决结果", "other", "low", "neutral"),
    "7.01": ("Reg FD 信息披露", "other", "low", "neutral"),
    "8.01": ("其他重要事项", "other", "low", "neutral"),
}
_IMPORTANCE_RANK = {"low": 0, "medium": 1, "high": 2}


def sec_rule_tag(symbol: str, form: str, items: str) -> tuple[str, dict]:
    """返回 (中文标题, 标签)。多个 item 取最重要者定标签; 9.01 (附件) 不计。6-K 无 item 编号。"""
    codes = [c.strip() for c in (items or "").split(",") if c.strip() and c.strip() != "9.01"]
    known = [(c, SEC_ITEMS[c]) for c in codes if c in SEC_ITEMS]
    if form.startswith("6-K"):
        title = f"{symbol} 提交外国发行人临时报告 (6-K)"
        tag = {"relevance": "primary", "event_type": "other", "direction": "neutral", "importance": "low", "rehash": False}
        return title, tag
    if not known:
        title = f"{symbol} 提交 8-K 公告" + (f" (事项 {', '.join(codes)})" if codes else "")
        return title, {"relevance": "primary", "event_type": "other", "direction": "neutral", "importance": "low",
                       "rehash": False}
    top = max(known, key=lambda kv: _IMPORTANCE_RANK[kv[1][2]])
    _, (_, etype, imp, direction) = top
    if any(v[3] == "bearish" for _, v in known):
        direction = "bearish"
    title = f"{symbol} 8-K 公告: " + "、".join(v[0] for _, v in known)
    return title, {"relevance": "primary", "event_type": etype, "direction": direction, "importance": imp, "rehash": False}


# ── 源拉取 ──────────────────────────────────────────────────

def fetch_alpaca_news(symbols: list[str], start: datetime, end: datetime, *, key: str, secret: str,
                       http_get: HttpGet | None = None, max_pages: int = 20) -> list[dict]:
    """Alpaca 新闻 (Benzinga 供稿), 自带 symbols 标签。分页跟随 next_page_token。"""
    http_get = http_get or _http_json
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    out: list[dict] = []
    token = None
    for _ in range(max_pages):
        params = {"symbols": ",".join(symbols), "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 50, "sort": "desc", "include_content": "false"}
        if token:
            params["page_token"] = token
        body = http_get(f"{ALPACA_NEWS_URL}?{urllib.parse.urlencode(params)}", headers=headers)
        for n in body.get("news") or []:
            out.append({"source": "alpaca", "source_id": str(n.get("id")), "headline": clean_text(n.get("headline")),
                        "summary": clean_text(n.get("summary")), "url": n.get("url") or "",
                        "created_at": n.get("created_at"), "symbols": [s.upper() for s in n.get("symbols") or []]})
        token = body.get("next_page_token")
        if not token:
            break
    return out


def fetch_finnhub_news(symbol: str, start: str, end: str, *, token: str, http_get: HttpGet | None = None) -> list[dict]:
    """Finnhub 个股新闻 (单股查询, related 字段即该股), 噪音较高 (观点/清单文多), 仅作补源。"""
    http_get = http_get or _http_json
    params = {"symbol": symbol, "from": start, "to": end, "token": token}
    body = http_get(f"{FINNHUB_NEWS_URL}?{urllib.parse.urlencode(params)}")
    rows = body if isinstance(body, list) else []
    out = []
    for n in rows:
        related = [s.strip().upper() for s in str(n.get("related") or symbol).split(",") if s.strip()]
        dt = n.get("datetime")
        created = datetime.fromtimestamp(dt, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(dt, (int, float)) else None
        out.append({"source": "finnhub", "source_id": str(n.get("id")), "headline": clean_text(n.get("headline")),
                    "summary": clean_text(n.get("summary")), "url": n.get("url") or "",
                    "created_at": created, "symbols": related or [symbol]})
    return out


def _sec_ua() -> str:
    from app import credentials
    return credentials.sec_user_agent()


def sec_cik_map(*, http_get: HttpGet | None = None) -> dict[str, int]:
    """官方 ticker→CIK 映射 (免 key, 全市场, 每周刷新即可——映射基本不变)。"""
    http_get = http_get or _http_json
    body = http_get(SEC_TICKERS_URL, headers={"User-Agent": _sec_ua()})
    return {str(v.get("ticker") or "").upper(): int(v["cik_str"]) for v in (body or {}).values() if v.get("ticker") and v.get("cik_str") is not None}


def fetch_sec_filings(symbol: str, cik: int, *, since: datetime, http_get: HttpGet | None = None) -> list[dict]:
    """该股近期 8-K/6-K (官方重大事项公告: 并购/增发/高管变动/诉讼等), 100% 相关 (单一发行人)。"""
    http_get = http_get or _http_json
    body = http_get(SEC_SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": _sec_ua()})
    recent = ((body or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    out = []
    for i, form in enumerate(forms):
        if form not in SEC_8K_FORMS:
            continue
        accepted = recent.get("acceptanceDateTime", [None] * len(forms))[i]
        try:
            ts = datetime.fromisoformat(str(accepted).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ts < since:
            continue
        acc = str(recent.get("accessionNumber", [""] * len(forms))[i]).replace("-", "")
        doc = recent.get("primaryDocument", [""] * len(forms))[i]
        items = recent.get("items", [""] * len(forms))[i]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}" if acc and doc else ""
        zh, tag = sec_rule_tag(symbol, form, items)
        out.append({"source": "sec", "source_id": f"{cik}:{acc}", "headline": f"{symbol} SEC {form} filing (items {items or 'n/a'})",
                    "summary": "", "url": url, "created_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "symbols": [symbol],
                    "form": form, "items": items or "", "zh_headline": zh, "zh_summary": "", "tags": {symbol: tag},
                    "tagged_by": {"model": "sec-items-rule", "prompt_version": "sec-items-v1",
                                  "at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "rule"}})
    return out


# ── 提及门 (规则): 来源打的股票标签不等于新闻在讲这只票 ──────────────────
# 实测 (2026-10-02, 41 只 24 小时 774 簇): Finnhub 的 related 标签大量是「顺带一提」,
# 如 NVDA 标签挂在「Is Carmax a Buy After Its Latest Earnings Report?」上。
# 规则: 标题或摘要里出现代码 ($NVDA / (NVDA) / 独立词 NVDA) 或公司名主体, 才算提及。
# 只做「是否提及」, 不判断利好利空 (那是模型打标的事)。

_NAME_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|n\.?v|s\.?a|ag|se|holdings?|group|"
    r"technologies|technology|platforms|markets|global|common|stock|class|[a-c]|capital|ordinary|shares|"
    r"american|depositary|new|york|registry|internet|the)\b\.?", re.IGNORECASE)
# 这些代码本身是常见英文词, 只认 $XXX / (XXX) 形式, 不认独立词 (否则 "now"/"arm" 满天飞)
_COMMON_WORD_TICKERS = {"NOW", "ARM", "MU", "TH", "ON", "IT", "ALL", "ARE", "CAN", "SO", "AI", "BE", "GO", "HAS", "LOW", "KEY",
                        "REAL", "DOCS", "SNOW", "COIN", "LITE", "HOOD", "META", "CRCL"}
# 常用别名 (简称/旧名), 公司名主体规则覆盖不到的
_EXTRA_ALIASES = {
    "GOOG": ["google", "alphabet"], "GOOGL": ["google", "alphabet"], "META": ["meta platforms", "facebook"],
    "TSM": ["tsmc", "taiwan semiconductor"], "SPCX": ["spacex"], "MSTR": ["microstrategy", "strategy inc"],
    "LLY": ["eli lilly", "lilly"], "SKHY": ["sk hynix", "hynix"], "AVGO": ["broadcom"], "AMD": ["advanced micro devices"],
    "MU": ["micron"], "NOW": ["servicenow"], "CRCL": ["circle internet", "circle"], "HOOD": ["robinhood"],
    "COIN": ["coinbase"], "LITE": ["lumentum"], "AAOI": ["applied optoelectronics"], "SNDK": ["sandisk"],
    "DOCS": ["doximity"], "TH": ["target hospitality"], "ARM": ["arm holdings"], "SSL": ["sasol"], "CVE": ["cenovus"],
    "NBIS": ["nebius"], "RDW": ["redwire"], "ASTS": ["ast spacemobile", "spacemobile"], "IREN": ["iren"],
}


# 公司名主体是普通英文词时, 小写匹配会大量误命中 ("strategy"/"circle"/"meta"), 只认首字母大写
_AMBIGUOUS_ALIASES = {"strategy", "circle", "meta", "lilly", "target", "arm", "snowflake", "oracle", "dell", "intel",
                      "apple", "amazon", "tesla", "iren", "micron"}


def derive_aliases(symbol: str, name: str | None) -> list[str]:
    """公司名去掉后缀/股份类别词, 取主体 (如 'Lumentum Holdings Inc. Common Stock' → 'lumentum')。"""
    out = list(_EXTRA_ALIASES.get(symbol.upper(), []))
    if name:
        core = _NAME_SUFFIX_RE.sub(" ", name.replace(",", " ").replace("&", " & "))
        core = re.sub(r"\s+", " ", core).strip().lower().rstrip("&").strip()
        if len(core) >= 4 and core not in out:
            out.append(core)
    return out


def _mentions(text: str, symbol: str, aliases: list[str]) -> bool:
    if not text:
        return False
    sym = re.escape(symbol.upper())
    if re.search(rf"(\${sym}\b|\({sym}\)|:{sym}\b)", text):  # $NVDA / (NVDA) / NASDAQ:NVDA
        return True
    # 独立大写代码: 普通代码直接命中 (NVDA); 常见英文词代码 (NOW/ARM/MU...) 只认全大写且后接
    # stock/shares 或逗号分隔的代码列表 ("MU Stock Muted" / "MU, NKE, IBRX"), 避免句首 "NOW" 误命中
    if symbol.upper() not in _COMMON_WORD_TICKERS:
        if re.search(rf"(?<![A-Za-z]){sym}(?![A-Za-z])", text):
            return True
    elif re.search(rf"(?<![A-Za-z]){sym}(?=\s+(?:[Ss]tock|[Ss]hares)\b|\s*,\s*[A-Z]{{1,5}}\b)", text):
        return True
    low = text.lower()
    for a in aliases:
        if a in _AMBIGUOUS_ALIASES:
            if re.search(rf"(?<![A-Za-z]){re.escape(a.title())}(?![a-z])", text):
                return True
        elif re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", low):
            return True
    return False


def mentioned_symbols(headline: str, summary: str, aliases: dict[str, list[str]],
                      candidates: set[str]) -> dict[str, str]:
    """返回 {symbol: 'headline'|'summary'}: 该股在标题还是摘要里被点名。未点名的不返回。"""
    out: dict[str, str] = {}
    for s in candidates:
        al = aliases.get(s, [])
        if _mentions(headline, s, al):
            out[s] = "headline"
        elif _mentions(summary, s, al):
            out[s] = "summary"
    return out


# ── 聚簇去重 + 相关性标记 ──────────────────────────────────────

_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _title_tokens(headline: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(headline) if len(w) > 2}


def _similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """标题词汇 Jaccard 重叠 (不同源同一事件的标题会有细微差异, 不用精确匹配)。"""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    return inter / len(ta | tb) >= threshold


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def cluster_dedupe(items: list[dict]) -> list[dict]:
    """24 小时窗口内标题高度相似的多源记录聚成一簇: 保留最早发布时间, 合并来源与关联股票。"""
    def _sort_key(x: dict) -> datetime:
        dt = _parse_dt(x.get("created_at"))
        assert dt is not None  # 已经被上面的 filter 保证非空
        return dt

    dated = sorted((it for it in items if _parse_dt(it.get("created_at"))), key=_sort_key)
    clusters: list[dict] = []
    for it in dated:
        it_dt = _parse_dt(it["created_at"])
        placed = False
        for c in clusters if it["source"] != "sec" else ():  # SEC 公告标题是模板 (代码+表单+编号), 只按 accession 去重, 不聚簇
            if "sec" in c["sources"]:
                continue
            if it_dt - c["_earliest_dt"] > CLUSTER_WINDOW:
                continue
            if not set(it["symbols"]) & set(c["symbols"]):  # 不同公司的新闻不能因标题相似合并
                continue
            if any(_similar(it["headline"], h) for h in c["_headlines"]):
                c["sources"] = sorted(set(c["sources"] + [it["source"]]))
                c["symbols"] = sorted(set(c["symbols"]) | set(it["symbols"]))
                c["_headlines"].append(it["headline"])
                c["refs"].append({"source": it["source"], "source_id": it["source_id"], "url": it["url"]})
                if it_dt < c["_earliest_dt"]:
                    c["_earliest_dt"] = it_dt
                    c["created_at"] = it["created_at"]
                    c["headline"] = it["headline"]
                    c["summary"] = it.get("summary") or c["summary"]
                placed = True
                break
        if not placed:
            cluster = {
                "id": f"{it['source']}:{it['source_id']}", "headline": it["headline"], "summary": it.get("summary") or "",
                "created_at": it["created_at"], "symbols": sorted(set(it["symbols"])), "sources": [it["source"]],
                "refs": [{"source": it["source"], "source_id": it["source_id"], "url": it["url"]}],
                "_earliest_dt": it_dt, "_headlines": [it["headline"]],
            }
            for k in ("form", "items", "zh_headline", "zh_summary", "tags", "tagged_by"):  # SEC 规则字段
                if k in it:
                    cluster[k] = it[k]
            clusters.append(cluster)
    for c in clusters:
        c.pop("_earliest_dt", None)
        c.pop("_headlines", None)
        c["scope"] = "focused" if len(c["symbols"]) <= FOCUSED_MAX_SYMBOLS else "broad"
    return clusters


# ── 落盘 (NDJSON, 按月分片, 原子写) ─────────────────────────────

def shard_path(data_dir: Path, when: datetime) -> Path:
    d = Path(data_dir) / "news"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{when.strftime('%Y-%m')}.jsonl"


def load_shard(data_dir: Path, when: datetime) -> dict[str, dict]:
    """按事件 id 读入该月分片, 便于与新采集结果合并去重 (同一事件多次拉取不重复落盘)。"""
    p = shard_path(data_dir, when)
    out: dict[str, dict] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["id"]] = row
    return out


def save_shard(data_dir: Path, when: datetime, rows: dict[str, dict]) -> None:
    from app.services.fs_utils import atomic_write_text
    ordered = sorted(rows.values(), key=lambda r: r.get("created_at") or "")
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in ordered)
    atomic_write_text(shard_path(data_dir, when), text + ("\n" if text else ""))


def _ref_keys(row: dict) -> set[str]:
    return {f"{r.get('source')}:{r.get('source_id')}" for r in row.get("refs") or []} | {row["id"]}


def merge_into_shard(data_dir: Path, clusters: list[dict]) -> int:
    """按月分片合并新聚簇: 已存在的事件保留已有的翻译/打标字段, 只补充 sources/symbols/refs。

    匹配既有事件不只看 id: 后续采集若带来更早的同簇记录, 新簇 id 会变; 只要任一来源引用
    (source:source_id) 已在分片里出现过, 就并入那条既有事件, 不重复落盘。
    """
    by_month: dict[str, list[dict]] = {}
    for c in clusters:
        dt = _parse_dt(c.get("created_at")) or datetime.now(timezone.utc)
        by_month.setdefault(dt.strftime("%Y-%m"), []).append(c)
    added = 0
    for month, rows in by_month.items():
        when = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
        existing = load_shard(data_dir, when)
        ref_index = {k: rid for rid, row in existing.items() for k in _ref_keys(row)}
        for c in rows:
            hit = next((ref_index[k] for k in _ref_keys(c) if k in ref_index), None)
            if hit is not None:
                prev = existing[hit]
                prev["sources"] = sorted(set(prev.get("sources", [])) | set(c["sources"]))
                prev["symbols"] = sorted(set(prev.get("symbols", [])) | set(c["symbols"]))
                known = {f"{r.get('source')}:{r.get('source_id')}" for r in prev.get("refs") or []}
                prev["refs"] = (prev.get("refs") or []) + [r for r in c.get("refs") or []
                                                           if f"{r.get('source')}:{r.get('source_id')}" not in known]
                prev["scope"] = "focused" if len(prev["symbols"]) <= FOCUSED_MAX_SYMBOLS else "broad"
                if c.get("mentions"):
                    merged = dict(prev.get("mentions") or {})
                    for sym, where in c["mentions"].items():
                        if merged.get(sym) != "headline":  # headline 优先级高于 summary
                            merged[sym] = where
                    prev["mentions"] = dict(sorted(merged.items()))
                for k in _ref_keys(c):
                    ref_index[k] = hit
            else:
                existing[c["id"]] = c
                for k in _ref_keys(c):
                    ref_index[k] = c["id"]
                added += 1
        save_shard(data_dir, when, existing)
    return added


# ── SEC CIK 映射磁盘缓存 (每周刷新; 映射基本不变, 不必每轮拉 600KB 全表) ──────

CIK_CACHE_TTL = timedelta(days=7)


def load_cik_map(data_dir: Path, *, now: datetime | None = None, http_get: HttpGet | None = None) -> dict[str, int]:
    """读缓存; 缓存缺失或超过 7 天才联网刷新。刷新失败时沿用旧缓存 (不清空)。"""
    from app.services.fs_utils import atomic_write_text
    now = now or datetime.now(timezone.utc)
    p = Path(data_dir) / "reference" / "sec_cik.json"
    cached: dict = {}
    try:
        cached = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        cached = {}
    fetched = _parse_dt(cached.get("fetched_at"))
    if cached.get("map") and fetched and now - fetched < CIK_CACHE_TTL:
        return {k: int(v) for k, v in cached["map"].items()}
    try:
        fresh = sec_cik_map(http_get=http_get)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SEC CIK 映射刷新失败, 沿用旧缓存: %s", type(exc).__name__)
        return {k: int(v) for k, v in (cached.get("map") or {}).items()}
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps({"fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "map": fresh}, ensure_ascii=False))
    return fresh


# ── 高层入口 (调度任务调用) ──────────────────────────────────────

ALL_SOURCES = frozenset({"alpaca", "finnhub", "sec"})


def apply_mention_gate(clusters: list[dict], symbols: list[str], names: dict[str, str] | None) -> list[dict]:
    """给每簇写入 mentions = {symbol: 'headline'|'summary'} (只含请求集内且被点名的股票)。

    SEC 公告按发行人 100% 相关, 直接记为 headline。完全未点名任何请求股票的簇丢弃
    (实测 24 小时 774 簇中约 36% 属此类: 来源标签挂了但正文没讲这只票)。
    """
    wanted = set(symbols)
    aliases = {s: derive_aliases(s, (names or {}).get(s)) for s in wanted}
    out = []
    for c in clusters:
        cand = set(c["symbols"]) & wanted
        if "sec" in c["sources"]:
            mentions = {s: "headline" for s in cand}
        else:
            mentions = mentioned_symbols(c.get("headline") or "", c.get("summary") or "", aliases, cand)
        if mentions:
            c["mentions"] = dict(sorted(mentions.items()))
            out.append(c)
    return out


def ingest(data_dir: Path, symbols: list[str], *, now: datetime | None = None, lookback: timedelta = timedelta(days=2),
           sources: frozenset[str] | set[str] = ALL_SOURCES, names: dict[str, str] | None = None,
           finnhub_symbols: list[str] | None = None,
           alpaca_get: HttpGet | None = None, finnhub_get: HttpGet | None = None, sec_get: HttpGet | None = None,
           sec_cik_cache: dict[str, int] | None = None) -> dict:
    """拉取三源 → 聚簇 → 落盘。单源失败不影响其他源 (fail-soft, 如实记录错误)。

    sources 由调度方决定节奏: Alpaca/Finnhub 每 5 分钟; SEC 每 30 分钟 (官方限速 10 次/秒,
    且每只票的 submissions 文件较大, 没必要高频)。
    """
    now = now or datetime.now(timezone.utc)
    start = now - lookback
    symbols = sorted({s.upper() for s in symbols if s})
    errors: list[str] = []
    items: list[dict] = []

    if "alpaca" in sources:
        creds = alpaca_credentials()
        if not creds:
            errors.append("alpaca: no credentials")
        elif symbols:
            try:
                items += fetch_alpaca_news(symbols, start, now, key=creds[0], secret=creds[1], http_get=alpaca_get)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"alpaca: {type(exc).__name__}")

    if "finnhub" in sources:
        token = finnhub_token()
        if not token:
            errors.append("finnhub: no token")
        else:
            fh = sorted({x.upper() for x in finnhub_symbols}) if finnhub_symbols is not None else symbols
            for sym in fh:
                try:
                    items += fetch_finnhub_news(sym, start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"),
                                                token=token, http_get=finnhub_get)
                except Exception as exc:  # noqa: BLE001
                    code = getattr(exc, "code", None)
                    errors.append(f"finnhub[{sym}]: {type(exc).__name__}{f' {code}' if code else ''}")
                    if code == 429:  # 限速: 本轮停止 Finnhub (补源, 下一轮继续), 不逐股撞墙
                        errors.append("finnhub: rate limited, rest skipped this round")
                        break

    if "sec" in sources:
        cik_map = sec_cik_cache if sec_cik_cache is not None else load_cik_map(data_dir, now=now, http_get=sec_get)
        if not cik_map:
            errors.append("sec: cik map unavailable")
        for sym in symbols:
            cik = cik_map.get(sym)
            if cik is None:
                continue
            try:
                items += fetch_sec_filings(sym, cik, since=start, http_get=sec_get)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"sec[{sym}]: {type(exc).__name__}")

    clusters = cluster_dedupe(items)
    # 只保留与请求股票集有交集且正文真的点名了该股的簇 (来源标签 ≠ 在讲这只票)
    tagged = [c for c in clusters if set(c["symbols"]) & set(symbols)]
    kept = apply_mention_gate(tagged, symbols, names)
    added = merge_into_shard(data_dir, kept)
    logger.info("news ingest: %d raw -> %d clusters -> %d tagged -> %d mentioned -> %d new, errors=%s",
                len(items), len(clusters), len(tagged), len(kept), added, errors)
    return {"raw": len(items), "clusters": len(clusters), "tagged": len(tagged), "kept": len(kept),
            "added": added, "errors": errors}
