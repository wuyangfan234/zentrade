"""舆情作战中心 · 采集 (第一期)。只展示: 不进打分、裁决、下单, 不推送。

5 个外部源各一个适配器, 每源单独熔断; 出错只记「缺源」, 不抛。结果写到 data/sentiment/:
  series/gdelt/<代码>.json   全球媒体  GDELT DOC 2.0 小时文章数 (同一 IP 每 5 秒 1 次, 单次 13-18 秒)
  series/reddit/<代码>.json  社交      Reddit 官方 API 小时帖子数 (凭据未配置 = 「未配置」, 不发请求)
  series/wiki/<代码>.json    关注      Wikimedia 日浏览量 (公司 + 管理层条目; 当日数据次日才完整)
  snap/stocktwits/<日>.jsonl 社交      StockTwits 最近 30 条消息 → 每小时消息数 + 多空标注
  snap/apewisdom/<日>.jsonl  社交      ApeWisdom 全站排行 → 24 小时提及数
  state.json                 每源状态 / 熔断;  board.json  评级结果 (sentiment_score 计算, 页面只读它)
财经新闻族不在这里采集, 直接读现有新闻库 data/news。

调度: 挂在 zen_intel 每分钟的 tick 上, 盘前盘中每 5 分钟、其余 (含周末) 每 15 分钟起一轮后台单飞线程;
GDELT 慢, 每轮有时间预算, 逐票轮转。
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "sentiment_pool.json"
UA = "ZenTrade-sentiment/1.0 (self-hosted research)"
SOURCES = ("gdelt", "stocktwits", "apewisdom", "reddit", "wiki")
BREAKER_FAILS = 3              # 连续失败 N 轮 → 熔断
BREAKER_COOLDOWN_S = 30 * 60   # 熔断后 30 分钟内不再请求, 之后半开试 1 次
HTTP_TIMEOUT = 30

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_GAP_S = 5.5              # 官方: 同一 IP 每 5 秒 1 次, 超频 429 (回填用)
GDELT_LIVE_GAP_S = 20.0        # 实测 11-12: 7 天/4 天查询算「大查询」, 上一次结束后 6 秒再发仍 429; 实时轮放宽到 20 秒
GDELT_BUDGET_S = 150           # 每轮 GDELT 最多花的墙钟
GDELT_REFRESH_S = 3600         # 每只票的近端刷新间隔
GDELT_HISTORY_D = 30           # 基线要 20 个交易日 ≈ 30 个自然日
GDELT_CHUNK_D = 4              # 4 天窗口返回小时粒度 (实测 11-12)
ST_URL = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"
ST_PER_ROUND = 15              # 未登录约 200 次/小时; 每轮 15 只, 逐票轮转
APE_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}"
APE_PAGES = 5                  # 前 500 名; 名单外记 0
WIKI_URL = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/"
            "{article}/daily/{start}/{end}")
WIKI_REFRESH_S = 6 * 3600
WIKI_PER_ROUND = 8             # 实测 11-12: 一轮连发 46 个请求会 429; 每轮最多 8 只, 请求间隔 1.5 秒
WIKI_GAP_S = 1.5
WIKI_HISTORY_D = 40
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_SEARCH_URL = "https://oauth.reddit.com/r/{subs}/search"
REDDIT_SUBS = "wallstreetbets+stocks+investing+StockMarket"
REDDIT_PER_ROUND = 10

_RUN_LOCK = threading.Lock()
_ST_CURSOR = {"i": 0}
_REDDIT_TOKEN: dict[str, Any] = {}


# ── 通用 ─────────────────────────────────────────────────────────────

def root(data_dir) -> Path:
    return Path(data_dir) / "sentiment"


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def load_config(path: Path | None = None) -> dict:
    return json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))


def _http_get(url: str, headers: dict | None = None, timeout: float = HTTP_TIMEOUT) -> bytes:
    """唯一的出网口 (测试按这个名字换桩)。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - 固定 https 域名
        return r.read()


def _http_post(url: str, data: dict, headers: dict | None = None, timeout: float = HTTP_TIMEOUT) -> bytes:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, **(headers or {})}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.read()


# ── 名单 ─────────────────────────────────────────────────────────────

def roles(loop=None, config: dict | None = None) -> dict[str, str]:
    """用户拍板: 只盯 自选 + 持仓 + 24 池。持仓 > 自选 > 池。"""
    cfg = config or load_config()
    out = {s: "池" for s in cfg.get("pool") or []}
    try:
        from app.services import watchlist
        for r in watchlist.list_symbols():
            if r.get("symbol"):
                out[str(r["symbol"]).upper()] = "自选"
    except Exception as e:  # noqa: BLE001
        logger.debug("sentiment: watchlist unavailable: %s", type(e).__name__)
    if loop is not None:
        try:
            for p in loop.ledger.open_positions:
                if p.symbol:
                    out[str(p.symbol).upper()] = "持仓"
        except Exception as e:  # noqa: BLE001
            logger.debug("sentiment: positions unavailable: %s", type(e).__name__)
    return out


_NAME_DROP = (" common stock", " class a", " class b", " ordinary shares", " american depositary shares",
              " corporation", " incorporated", " holdings", " holding", " corp", " inc", " ltd", " plc", " co", " n.v.",
              " s.a.", " group", ",", ".")


def clean_name(name: str) -> str:
    """行情名 → GDELT 查询短语 (只给池外的自选/持仓用; 池内用配置里人工核过的查询名)。"""
    s = f" {str(name or '').lower()} "
    for _ in range(3):
        for w in _NAME_DROP:
            s = s.replace(w + " ", " ")
    s = " ".join(s.split())
    if len(s) < 3:
        return ""
    return f'"{s}"' if " " in s else s


def query_names(symbols: list[str], config: dict, repo=None) -> dict[str, dict]:
    names = {s: dict(v) for s, v in (config.get("names") or {}).items()}
    missing = [s for s in symbols if s not in names]
    if missing and repo is not None:
        try:
            nm = repo.get_name_map(missing) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug("sentiment: name map unavailable: %s", type(e).__name__)
            nm = {}
        for s in missing:
            term = clean_name(nm.get(s, ""))
            if term:
                names[s] = {"gdelt": term, "wiki": []}
    return names


# ── 状态 / 熔断 ──────────────────────────────────────────────────────

def read_state(data_dir) -> dict:
    return read_json(root(data_dir) / "state.json") or {}


def breaker_open(st: dict, now: datetime) -> bool:
    until = parse_iso(st.get("open_until"))
    return until is not None and now < until


def mark(state: dict, source: str, now: datetime, *, ok: bool, n: int = 0, error: str = "",
         status: str | None = None) -> None:
    st = state.setdefault(source, {})
    st["last_attempt_at"] = iso(now)
    if ok:
        st.update(status=status or "ok", last_ok_at=iso(now), error="", fails=0, open_until=None, n=n)
        return
    fails = int(st.get("fails") or 0) + 1
    st.update(status=status or "missing", error=error[:200], fails=fails, n=0)
    if fails >= BREAKER_FAILS:
        st["status"] = "breaker"
        st["open_until"] = iso(now + timedelta(seconds=BREAKER_COOLDOWN_S))


# ── GDELT ───────────────────────────────────────────────────────────

def gdelt_url(term: str, start: datetime, end: datetime) -> str:
    q = {"query": term, "mode": "timelinevolraw", "format": "json",
         "startdatetime": start.astimezone(UTC).strftime("%Y%m%d%H%M%S"),
         "enddatetime": end.astimezone(UTC).strftime("%Y%m%d%H%M%S")}
    return GDELT_URL + "?" + urllib.parse.urlencode(q)


def parse_gdelt(body: bytes) -> dict[str, int]:
    """timelinevolraw → {'YYYY-MM-DDTHH': 文章数}。15 分钟粒度时按小时累加。非 JSON (限速/报错文本) 抛 ValueError。"""
    d = json.loads(body.decode("utf-8", "replace").strip() or "{}")
    if not isinstance(d, dict) or "timeline" not in d:
        raise ValueError("gdelt: no timeline")
    if (d.get("query_details") or {}).get("date_resolution") == "day":   # 跨度 > 7 天会退化成日粒度, 不能当小时用
        raise ValueError("gdelt: day resolution")
    out: dict[str, int] = {}
    for series in d.get("timeline") or []:
        for p in series.get("data") or []:
            raw = str(p.get("date") or "")
            if len(raw) < 11:
                continue
            key = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}T{raw[9:11]}"
            out[key] = out.get(key, 0) + int(p.get("value") or 0)
    return out


def hour_key(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H")


def merge_hours(cur: dict | None, hours: dict[str, int], start: datetime, end: datetime, **meta) -> dict:
    """把一次查询结果并入小时序列。覆盖区间 [from, to) 内没出现的小时视为 0; 区间外 = 没有数据。"""
    cur = dict(cur or {})
    h = dict(cur.get("hours") or {})
    s_key, e_key = hour_key(start), hour_key(end)
    for k in [k for k in h if s_key <= k < e_key]:   # 本次覆盖区间以新结果为准
        del h[k]
    h.update({k: v for k, v in hours.items() if s_key <= k < e_key and v})
    lo = min(filter(None, [cur.get("from"), s_key]))
    hi = max(filter(None, [cur.get("to"), e_key]))
    return {**cur, **meta, "hours": dict(sorted(h.items())), "from": lo, "to": hi}


def _gdelt_plan(cur: dict | None, now: datetime) -> tuple[datetime, datetime] | None:
    """下一次该查的区间: 先补近端 (超过刷新间隔), 再往回补历史到 30 天。"""
    end = now.replace(minute=0, second=0, microsecond=0)
    fetched = parse_iso((cur or {}).get("fetched_at"))
    if not cur or fetched is None or (now - fetched).total_seconds() >= GDELT_REFRESH_S:
        return end - timedelta(days=GDELT_CHUNK_D), end
    lo = datetime.strptime(cur["from"], "%Y-%m-%dT%H").replace(tzinfo=UTC)
    if lo > end - timedelta(days=GDELT_HISTORY_D):
        return lo - timedelta(days=GDELT_CHUNK_D), lo
    return None


def fetch_gdelt(data_dir, symbols: list[str], names: dict, now: datetime, *, budget_s: float = GDELT_BUDGET_S,
                sleep=time.sleep) -> int:
    """按需逐票查; 返回本轮成功查询数。单票失败抛给调用方 (算本源失败, 已写的票保留)。"""
    t0 = time.monotonic()
    todo = []
    for s in symbols:
        term = (names.get(s) or {}).get("gdelt")
        if not term:
            continue
        p = root(data_dir) / "series" / "gdelt" / f"{s}.json"
        cur = read_json(p)
        plan = _gdelt_plan(cur, now)
        if plan:
            fetched = parse_iso((cur or {}).get("fetched_at"))
            todo.append((fetched or datetime.min.replace(tzinfo=UTC), s, term, p, cur, plan))
    todo.sort(key=lambda x: x[0])
    done = 0
    for _, s, term, p, cur, (a, b) in todo:
        if time.monotonic() - t0 > budget_s:
            break
        if done:
            sleep(GDELT_LIVE_GAP_S)
        try:
            hours = parse_gdelt(_http_get(gdelt_url(term, a, b), timeout=60))
        except (urllib.error.HTTPError, ValueError):
            if done:          # 中途被限速: 已写的保留, 本轮到此为止 (首个就失败才算本源缺源)
                break
            raise
        near = b >= now.replace(minute=0, second=0, microsecond=0)
        meta = {"term": term, "fetched_at": iso(now)} if near else {"term": term}
        write_json(p, merge_hours(cur, hours, a, b, **meta))
        done += 1
    return done


# ── StockTwits ──────────────────────────────────────────────────────

def parse_stocktwits(body: bytes) -> dict:
    """最近 30 条 → 每小时消息数 (按最新与最旧两条的时间跨度推)、多空标注数、关注人数。"""
    d = json.loads(body)
    msgs = d.get("messages") or []
    times = sorted(t for t in (parse_iso(m.get("created_at")) for m in msgs) if t)
    bull = sum(1 for m in msgs if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bullish")
    bear = sum(1 for m in msgs if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bearish")
    span_h = max((times[-1] - times[0]).total_seconds() / 3600, 1 / 60) if len(times) >= 2 else None
    rate = round((len(times) - 1) / span_h, 2) if span_h else 0.0
    return {"rate_h": rate, "n": len(msgs), "bull": bull, "bear": bear,
            "newest": iso(times[-1]) if times else None, "oldest": iso(times[0]) if times else None,
            "watchers": (d.get("symbol") or {}).get("watchlist_count")}


def fetch_stocktwits(data_dir, symbols: list[str], now: datetime, *, per_round: int = ST_PER_ROUND) -> int:
    if not symbols:
        return 0
    i = _ST_CURSOR["i"] % len(symbols)
    batch = (symbols[i:] + symbols[:i])[:per_round]
    _ST_CURSOR["i"] = i + len(batch)
    rows, errors = {}, 0
    for s in batch:
        try:
            rows[s] = parse_stocktwits(_http_get(ST_URL.format(sym=urllib.parse.quote(s))))
        except urllib.error.HTTPError as e:
            if e.code == 404:          # 没有这只票的消息流: 不算源故障
                continue
            raise
        except (ValueError, KeyError):
            errors += 1
    if not rows and errors:
        raise ValueError("stocktwits: all parse failed")
    append_snap(data_dir, "stocktwits", now, rows)
    return len(rows)


# ── ApeWisdom ───────────────────────────────────────────────────────

def parse_apewisdom(body: bytes) -> tuple[dict[str, dict], int]:
    d = json.loads(body)
    out = {}
    for r in d.get("results") or []:
        t = str(r.get("ticker") or "").upper()
        if t:
            out[t] = {"mentions": int(r.get("mentions") or 0), "upvotes": int(r.get("upvotes") or 0),
                      "rank": r.get("rank"), "mentions_24h_ago": r.get("mentions_24h_ago"),
                      "rank_24h_ago": r.get("rank_24h_ago")}
    return out, int(d.get("pages") or 1)


def fetch_apewisdom(data_dir, symbols: list[str], now: datetime) -> int:
    seen: dict[str, dict] = {}
    pages = 1
    page = 1
    while page <= min(pages, APE_PAGES):
        rows, pages = parse_apewisdom(_http_get(APE_URL.format(page=page)))
        seen.update(rows)
        page += 1
    covered = page - 1
    rows = {s: seen.get(s) or {"mentions": 0, "rank": None, "absent": True} for s in symbols}
    append_snap(data_dir, "apewisdom", now, rows, pages=covered)
    return sum(1 for s in symbols if s in seen)


# ── Wikimedia 浏览量 ────────────────────────────────────────────────

def wiki_url(article: str, start: date, end: date) -> str:
    return WIKI_URL.format(article=urllib.parse.quote(article, safe=""), start=start.strftime("%Y%m%d"),
                           end=end.strftime("%Y%m%d"))


def parse_wiki(body: bytes) -> dict[str, int]:
    d = json.loads(body)
    out = {}
    for it in d.get("items") or []:
        ts = str(it.get("timestamp") or "")
        if len(ts) >= 8:
            out[f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"] = int(it.get("views") or 0)
    return out


def fetch_wiki(data_dir, symbols: list[str], names: dict, now: datetime, *, history_d: int = WIKI_HISTORY_D,
               force: bool = False, per_round: int = WIKI_PER_ROUND, sleep=time.sleep) -> int:
    """最久没刷新的先来, 每轮最多 per_round 只; 中途被限速时已写的保留 (首个就失败才算本源缺源)。"""
    end = now.date()
    todo = []
    for s in symbols:
        arts = (names.get(s) or {}).get("wiki") or []
        if not arts:
            continue
        p = root(data_dir) / "series" / "wiki" / f"{s}.json"
        cur = read_json(p) or {}
        fetched = parse_iso(cur.get("fetched_at"))
        if not force and fetched and (now - fetched).total_seconds() < WIKI_REFRESH_S:
            continue
        todo.append((fetched or datetime.min.replace(tzinfo=UTC), s, arts, p, cur))
    todo.sort(key=lambda x: x[0])
    done = calls = 0
    start = end - timedelta(days=history_d)
    for _, s, arts, p, cur in todo[:per_round]:
        per = dict(cur.get("articles") or {})
        try:
            for a in arts:
                if calls:
                    sleep(WIKI_GAP_S)
                calls += 1
                days = parse_wiki(_http_get(wiki_url(a, start, end)))
                per[a] = dict(sorted({**(per.get(a) or {}), **days}.items()))
        except (urllib.error.HTTPError, ValueError):
            if done:
                break
            raise
        write_json(p, {"articles": per, "fetched_at": iso(now)})
        done += 1
    return done


# ── Reddit (官方 API, 用户申请中) ───────────────────────────────────

def reddit_credentials() -> tuple[str, str]:
    from app import credentials
    return credentials.reddit()


def parse_reddit(body: bytes) -> dict[str, int]:
    d = json.loads(body)
    out: dict[str, int] = {}
    for ch in ((d.get("data") or {}).get("children") or []):
        ts = (ch.get("data") or {}).get("created_utc")
        if isinstance(ts, (int, float)):
            k = hour_key(datetime.fromtimestamp(ts, UTC))
            out[k] = out.get(k, 0) + 1
    return out


def _reddit_token(cid: str, secret: str, now: datetime) -> str:
    tok = _REDDIT_TOKEN.get("token")
    if tok and _REDDIT_TOKEN.get("cid") == cid and now < _REDDIT_TOKEN.get("exp", now):
        return tok
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    d = json.loads(_http_post(REDDIT_TOKEN_URL, {"grant_type": "client_credentials"},
                              headers={"Authorization": f"Basic {auth}"}))
    tok = d.get("access_token")
    if not tok:
        raise ValueError("reddit: no token")
    _REDDIT_TOKEN.update(token=tok, cid=cid, exp=now + timedelta(seconds=int(d.get("expires_in") or 3600) - 120))
    return tok


def fetch_reddit(data_dir, symbols: list[str], now: datetime, cid: str, secret: str) -> int:
    tok = _reddit_token(cid, secret, now)
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = end - timedelta(days=1)
    done = 0
    for s in symbols[:REDDIT_PER_ROUND]:
        q = urllib.parse.urlencode({"q": s, "restrict_sr": "on", "sort": "new", "t": "day", "limit": 100})
        hours = parse_reddit(_http_get(REDDIT_SEARCH_URL.format(subs=REDDIT_SUBS) + "?" + q,
                                       headers={"Authorization": f"Bearer {tok}"}))
        p = root(data_dir) / "series" / "reddit" / f"{s}.json"
        write_json(p, merge_hours(read_json(p), hours, start, end, fetched_at=iso(now)))
        done += 1
    return done


# ── 快照落盘 ─────────────────────────────────────────────────────────

def append_snap(data_dir, source: str, now: datetime, rows: dict, **meta) -> None:
    d = root(data_dir) / "snap" / source
    d.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": iso(now), **meta, "rows": rows}, ensure_ascii=False, separators=(",", ":"))
    with open(d / f"{now.astimezone(UTC).date().isoformat()}.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_snaps(data_dir, source: str, start: datetime, end: datetime) -> list[dict]:
    d = root(data_dir) / "snap" / source
    if not d.is_dir():
        return []
    out = []
    day = start.date()
    while day <= end.date():
        p = d / f"{day.isoformat()}.jsonl"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                t = parse_iso(r.get("at"))
                if t and start <= t <= end:
                    out.append(r)
        day += timedelta(days=1)
    return out


# ── 一轮 ─────────────────────────────────────────────────────────────

def collect_round(data_dir, symbols: list[str], names: dict, now: datetime, *, sleep=time.sleep,
                  only: set[str] | None = None) -> dict:
    """跑一轮全部源。每源单独熔断; 出错记「缺源」(status=missing) 不抛。返回 {源: 状态行}。"""
    state = read_state(data_dir)
    cid, secret = ("", "")
    try:
        cid, secret = reddit_credentials()
    except Exception as e:  # noqa: BLE001
        logger.debug("sentiment: reddit credentials unavailable: %s", type(e).__name__)
    jobs = {
        "gdelt": lambda: fetch_gdelt(data_dir, symbols, names, now, sleep=sleep),
        "stocktwits": lambda: fetch_stocktwits(data_dir, symbols, now),
        "apewisdom": lambda: fetch_apewisdom(data_dir, symbols, now),
        "wiki": lambda: fetch_wiki(data_dir, symbols, names, now, sleep=sleep),
        "reddit": lambda: fetch_reddit(data_dir, symbols, now, cid, secret),
    }
    for source in SOURCES:
        if only is not None and source not in only:
            continue
        st = state.get(source) or {}
        if source == "reddit" and not (cid and secret):
            state[source] = {**st, "status": "unconfigured", "last_attempt_at": iso(now), "error": "", "n": 0}
            continue
        if breaker_open(st, now):
            continue
        try:
            n = jobs[source]()
            mark(state, source, now, ok=True, n=int(n or 0))
        except Exception as e:  # noqa: BLE001 - 一个源坏了只记缺源, 不拖其他源
            logger.info("sentiment: %s failed: %s", source, type(e).__name__)
            mark(state, source, now, ok=False, error=f"{type(e).__name__}: {e}")
    state["_round"] = {"at": iso(now), "symbols": len(symbols)}
    write_json(root(data_dir) / "state.json", state)
    return state


def run_round(data_dir, *, loop=None, repo=None, now: datetime | None = None, sleep=time.sleep) -> dict | None:
    """后台线程入口: 采集 → 评级 → 写 board.json。上一轮未完直接返回 None。"""
    if not _RUN_LOCK.acquire(blocking=False):
        return None
    try:
        from app.services import sentiment_score
        now = (now or datetime.now(UTC)).astimezone(UTC)
        cfg = load_config()
        rl = roles(loop, cfg)
        symbols = sorted(rl)
        names = query_names(symbols, cfg, repo)
        state = collect_round(data_dir, symbols, names, now, sleep=sleep)
        try:
            board = sentiment_score.build_board(data_dir, rl, now, config=cfg, state=state)
            write_json(root(data_dir) / "board.json", board)
        except Exception as e:  # noqa: BLE001
            logger.warning("sentiment: board failed: %s", type(e).__name__)
            state["_board"] = {"error": f"{type(e).__name__}: {e}"[:200], "at": iso(now)}
            write_json(root(data_dir) / "state.json", state)
        return state
    finally:
        _RUN_LOCK.release()


def kick(data_dir, *, loop=None, repo=None, now: datetime | None = None) -> bool:
    """zen_intel tick 调: 后台起一轮 (单飞, 不占 tick 锁)。已有一轮在跑返回 False。"""
    if _RUN_LOCK.locked():
        return False
    threading.Thread(target=run_round, args=(data_dir,), kwargs={"loop": loop, "repo": repo, "now": now},
                     daemon=True, name="intel-sentiment").start()
    return True


def status_summary(data_dir) -> dict:
    """状态页一行用: 每源状态 + 最近一轮时间 + 评级计数。只读。"""
    st = read_json(root(data_dir) / "state.json") or {}
    board = read_json(root(data_dir) / "board.json") or {}
    per = {s: (st.get(s) or {}).get("status") or "pending" for s in SOURCES}
    return {"sources": per, "round_at": (st.get("_round") or {}).get("at"), "counts": board.get("counts") or {},
            "board_error": (st.get("_board") or {}).get("error"),
            "errors": {s: (st.get(s) or {}).get("error") for s in SOURCES if (st.get(s) or {}).get("error")}}


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
