"""舆情异动 · 采集。只展示: 不进打分、裁决、下单, 不推送; 不调 AI, 不调付费接口。

三类来源, 都免费、都有历史 (上线当天就有基线, 能回放验证):
  新闻   现有新闻库 data/news (这里不采集, 评分时直接读)
  Reddit Arctic Shift 公开档案 (无需 key): 按版块 + 时间窗拉全部帖子, 本地按代码 / 公司名匹配。
         它的按词搜索与聚合接口在大版块上基本都返回「Timeout」(11-12 实测), 只能整版拉。
         帖子入档延迟约 15 分钟, 评分时按「发帖 + 30 分钟」算可得。无 SLA、动态限流 (约一半请求 422), 必须重试。
  维基   Wikimedia 日浏览量 (公司 + 管理层条目)

结果写到 data/sentiment/:
  reddit/<版块>/<UTC 日期>.jsonl   原始帖子 (id, created_utc, title, selftext 前 600 字)
  reddit/<版块>/coverage.json      已完整覆盖的时间段 {from, to}
  series/wiki/<代码>.json          各条目日浏览量
  state.json                       每源状态 / 熔断;  board.json  评级结果 (页面只读它)

调度: 挂在 zen_intel 每分钟的 tick 上, 盘前盘中每 5 分钟、其余 (含周末) 每 15 分钟起一轮后台单飞线程。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "sentiment_pool.json"
UA = "ZenTrade-sentiment/1.0 (self-hosted research)"
SOURCES = ("reddit", "wiki")
BREAKER_FAILS = 3              # 连续失败 N 轮 → 熔断
BREAKER_COOLDOWN_S = 30 * 60   # 熔断后 30 分钟内不再请求, 之后半开试 1 次
HTTP_TIMEOUT = 60

ARCTIC_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
REDDIT_SUBS = ("wallstreetbets", "stocks")
REDDIT_LAG = timedelta(minutes=30)     # 入档延迟实测约 15 分钟, 取保守值
REDDIT_KEEP_D = 45                     # 原始帖子保留天数 (基线要 20 个交易日)
REDDIT_NEW_PAGES = 5                   # 每轮向新补的最多页数 (每页 100 帖)
REDDIT_BACK_PAGES = 4                  # 每轮向旧回补的最多页数, 直到覆盖 REDDIT_KEEP_D 天
REDDIT_OVERLAP = timedelta(hours=1)
REDDIT_RETRY_S = (3, 8)                # 422「Timeout. Maybe slow down a bit」/ 超时: 退避后重试
REDDIT_BUDGET_S = 150
REDDIT_TEXT_MAX = 600

WIKI_URL = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/"
            "{article}/daily/{start}/{end}")
WIKI_REFRESH_S = 6 * 3600
WIKI_HISTORY_D = 40
WIKI_PER_ROUND = 8             # 实测 11-12: 一轮连发 46 个请求会 429; 每轮最多 8 只, 请求间隔 1.5 秒
WIKI_GAP_S = 1.5

_RUN_LOCK = threading.Lock()


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


# ── 名单 / 匹配词 ────────────────────────────────────────────────────

def roles(loop=None, config: dict | None = None) -> dict[str, str]:
    """名单 = 自选 + 持仓 + 配置里的默认关注池。持仓 > 自选 > 池。"""
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


def query_names(symbols: list[str], config: dict, repo=None) -> dict[str, dict]:
    """{代码: {aliases: Reddit 匹配用的公司名/人名, wiki: 条目}}。池内用配置 (人工核过), 池外按行情名推公司名。"""
    from app.services.news_ingest import derive_aliases
    names = {s: {"aliases": list(v.get("aliases") or []), "wiki": list(v.get("wiki") or [])}
             for s, v in (config.get("names") or {}).items()}
    missing = [s for s in symbols if s not in names]
    nm: dict[str, str] = {}
    if missing and repo is not None:
        try:
            nm = repo.get_name_map(missing) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug("sentiment: name map unavailable: %s", type(e).__name__)
    for s in missing:
        names[s] = {"aliases": derive_aliases(s, nm.get(s)), "wiki": []}
    return names


def mentions(text: str, symbol: str, aliases: list[str]) -> bool:
    """与新闻库同一套规则 ($NVDA / 独立大写代码 / 公司名; 常见英文词代码与公司名只认严格形式)。"""
    from app.services.news_ingest import _mentions
    return _mentions(text, symbol, aliases)


def prefilter_words(symbol: str, aliases: list[str]) -> tuple[str, ...]:
    """先用小写子串粗筛 (快), 命中才跑正则。"""
    words = {symbol.lower()}
    for a in aliases:
        w = re.split(r"[\s\-.]", a.lower())[0]
        if len(w) >= 3:
            words.add(w)
    return tuple(sorted(words))


# ── 状态 / 熔断 ──────────────────────────────────────────────────────

def read_state(data_dir) -> dict:
    return read_json(root(data_dir) / "state.json") or {}


def breaker_open(st: dict, now: datetime) -> bool:
    until = parse_iso(st.get("open_until"))
    return until is not None and now < until


def mark(state: dict, source: str, now: datetime, *, ok: bool, n: int = 0, error: str = "") -> None:
    st = state.setdefault(source, {})
    st["last_attempt_at"] = iso(now)
    if ok:
        st.update(status="ok", last_ok_at=iso(now), error="", fails=0, open_until=None, n=n)
        return
    fails = int(st.get("fails") or 0) + 1
    st.update(status="missing", error=error[:200], fails=fails, n=0)
    if fails >= BREAKER_FAILS:
        st["status"] = "breaker"
        st["open_until"] = iso(now + timedelta(seconds=BREAKER_COOLDOWN_S))


# ── Reddit (Arctic Shift) ───────────────────────────────────────────

def reddit_dir(data_dir, sub: str) -> Path:
    return root(data_dir) / "reddit" / sub


def arctic_url(sub: str, *, after: datetime | None = None, before: float | None = None) -> str:
    q: dict[str, Any] = {"subreddit": sub, "limit": 100, "sort": "desc", "fields": "id,created_utc,title,selftext"}
    if after is not None:
        q["after"] = after.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    if before is not None:
        q["before"] = str(int(before))
    return ARCTIC_URL + "?" + urllib.parse.urlencode(q)


def parse_arctic(body: bytes) -> list[dict]:
    """→ [{id, created_utc, title, selftext}], 按时间倒序。422 正文 / 坏 JSON 抛 ValueError。"""
    d = json.loads(body)
    if not isinstance(d, dict) or d.get("error") or not isinstance(d.get("data"), list):
        raise ValueError(f"arctic: {str(d.get('error') if isinstance(d, dict) else d)[:80]}")
    out = []
    for r in d["data"]:
        if r.get("id") and isinstance(r.get("created_utc"), (int, float)):
            out.append({"id": str(r["id"]), "created_utc": int(r["created_utc"]), "title": r.get("title") or "",
                        "selftext": (r.get("selftext") or "")[:REDDIT_TEXT_MAX]})
    return out


_NET_ERRORS = (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError)


def _arctic_page(url: str, sleep) -> list[dict]:
    err: Exception | None = None
    for k in range(len(REDDIT_RETRY_S) + 1):
        if k:
            sleep(REDDIT_RETRY_S[k - 1])
        try:
            return parse_arctic(_http_get(url))
        except _NET_ERRORS as e:
            err = e
    raise err  # type: ignore[misc]


def append_posts(data_dir, sub: str, posts: list[dict]) -> int:
    """按发帖 UTC 日期落盘, 同 id 去重。返回新增条数。"""
    by_day: dict[str, list[dict]] = {}
    for p in posts:
        day = datetime.fromtimestamp(p["created_utc"], UTC).date().isoformat()
        by_day.setdefault(day, []).append(p)
    d = reddit_dir(data_dir, sub)
    d.mkdir(parents=True, exist_ok=True)
    added = 0
    for day, rows in by_day.items():
        f = d / f"{day}.jsonl"
        seen: set[str] = set()
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    seen.add(json.loads(line)["id"])
                except (ValueError, KeyError):
                    continue
        new = []
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                new.append(r)
        if new:
            with open(f, "a", encoding="utf-8") as fh:
                fh.write("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in new))
            added += len(new)
    return added


def read_coverage(data_dir, sub: str) -> dict | None:
    c = read_json(reddit_dir(data_dir, sub) / "coverage.json")
    return c if isinstance(c, dict) and c.get("from") and c.get("to") else None


def fetch_reddit_sub(data_dir, sub: str, now: datetime, *, sleep=time.sleep, budget_s: float = REDDIT_BUDGET_S) -> int:
    """先向新补到与已有覆盖接上, 再向旧回补到 REDDIT_KEEP_D 天。覆盖始终是一段连续区间 [from, to]。
    向新补没接上 (停机太久) → 覆盖从这次拉到的最旧一帖重新算, 之后的轮次把旧的补回来。"""
    t0 = time.monotonic()
    cov = read_coverage(data_dir, sub)
    to_prev = parse_iso(cov["to"]) if cov else None
    stop_at = (to_prev - REDDIT_OVERLAP) if to_prev else now - timedelta(hours=6)
    before: float | None = None
    oldest = None
    added = pages = 0
    connected = False
    while pages < REDDIT_NEW_PAGES:
        if pages:
            sleep(2)
        rows = _arctic_page(arctic_url(sub, after=stop_at, before=before), sleep)
        pages += 1
        added += append_posts(data_dir, sub, rows)
        if rows:
            oldest = rows[-1]["created_utc"]
            before = oldest
        if len(rows) < 100:
            connected = True
            break
    if connected and cov:
        cov = {"from": cov["from"], "to": iso(now)}
    else:
        start = datetime.fromtimestamp(oldest, UTC) if (oldest and not connected) else stop_at
        cov = {"from": iso(start), "to": iso(now)}
    write_json(reddit_dir(data_dir, sub) / "coverage.json", cov)
    floor = now - timedelta(days=REDDIT_KEEP_D)
    back = 0
    while back < REDDIT_BACK_PAGES and time.monotonic() - t0 < budget_s:
        lo = parse_iso(cov["from"])
        if lo is None or lo <= floor:
            break
        sleep(2)
        try:
            rows = _arctic_page(arctic_url(sub, after=floor, before=lo.timestamp()), sleep)
        except _NET_ERRORS:
            break                      # 回补失败不算本源故障, 下一轮接着补
        back += 1
        added += append_posts(data_dir, sub, rows)
        new_from = datetime.fromtimestamp(rows[-1]["created_utc"], UTC) if len(rows) == 100 else floor
        cov = {"from": iso(new_from), "to": cov["to"]}
        write_json(reddit_dir(data_dir, sub) / "coverage.json", cov)
    _prune(data_dir, sub, now)
    return added


def _prune(data_dir, sub: str, now: datetime) -> None:
    keep = (now - timedelta(days=REDDIT_KEEP_D + 2)).date().isoformat()
    for f in reddit_dir(data_dir, sub).glob("*.jsonl"):
        if f.stem < keep:
            f.unlink(missing_ok=True)


def fetch_reddit(data_dir, now: datetime, *, sleep=time.sleep, subs: tuple[str, ...] = REDDIT_SUBS) -> int:
    """全部版块都失败才算本源失败; 部分失败的版块下一轮再补。"""
    added, errors = 0, []
    for sub in subs:
        try:
            added += fetch_reddit_sub(data_dir, sub, now, sleep=sleep)
        except _NET_ERRORS as e:
            errors.append(f"{sub}: {type(e).__name__} {getattr(e, 'code', '')}".rstrip())
    if errors and len(errors) == len(subs):
        raise ValueError("; ".join(errors))
    return added


def read_posts(data_dir, start: datetime, end: datetime, subs: tuple[str, ...] = REDDIT_SUBS) -> list[dict]:
    out = []
    for sub in subs:
        d = reddit_dir(data_dir, sub)
        if not d.is_dir():
            continue
        day = start.date()
        while day <= end.date():
            f = d / f"{day.isoformat()}.jsonl"
            if f.exists():
                for line in f.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if start.timestamp() <= r.get("created_utc", 0) <= end.timestamp():
                        r["sub"] = sub
                        out.append(r)
            day += timedelta(days=1)
    return out


def reddit_span(data_dir, subs: tuple[str, ...] = REDDIT_SUBS) -> tuple[datetime, datetime] | None:
    """所有版块共同覆盖的区间 (取最晚的起点、最早的终点); 任一版块没有覆盖 = 无。"""
    lo = hi = None
    for sub in subs:
        c = read_coverage(data_dir, sub)
        if not c:
            return None
        a, b = parse_iso(c["from"]), parse_iso(c["to"])
        if a is None or b is None:
            return None
        lo = a if lo is None or a > lo else lo
        hi = b if hi is None or b < hi else hi
    return (lo, hi) if lo and hi and lo < hi else None


# ── Wikimedia 浏览量 ────────────────────────────────────────────────

def wiki_url(article: str, start, end) -> str:
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
    """最久没刷新的先来, 每轮最多 per_round 只; 中途被限速时已写的保留 (首个就失败才算本源失败)。"""
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


# ── 一轮 ─────────────────────────────────────────────────────────────

def collect_round(data_dir, symbols: list[str], names: dict, now: datetime, *, sleep=time.sleep,
                  only: set[str] | None = None) -> dict:
    """跑一轮全部源。每源单独熔断; 出错只记 status=missing 不抛。返回 {源: 状态行}。"""
    state = read_state(data_dir)
    jobs = {
        "reddit": lambda: fetch_reddit(data_dir, now, sleep=sleep),
        "wiki": lambda: fetch_wiki(data_dir, symbols, names, now, sleep=sleep),
    }
    for source in SOURCES:
        if only is not None and source not in only:
            continue
        if breaker_open(state.get(source) or {}, now):
            continue
        try:
            mark(state, source, now, ok=True, n=int(jobs[source]() or 0))
        except Exception as e:  # noqa: BLE001 - 一个源坏了只记失败, 不拖其他源
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
        names = query_names(sorted(rl), cfg, repo)
        state = collect_round(data_dir, sorted(rl), names, now, sleep=sleep)
        try:
            board = sentiment_score.build_board(data_dir, rl, now, config=cfg, state=state, names=names)
            write_json(root(data_dir) / "board.json", board)
            state.pop("_board", None)
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
    """状态页一行用: 每源状态 + 最近一轮时间 + 上榜数。只读。"""
    st = read_json(root(data_dir) / "state.json") or {}
    board = read_json(root(data_dir) / "board.json") or {}
    return {"sources": {s: (st.get(s) or {}).get("status") or "pending" for s in SOURCES},
            "round_at": (st.get("_round") or {}).get("at"), "counts": board.get("counts") or {},
            "board_error": (st.get("_board") or {}).get("error"),
            "errors": {s: (st.get(s) or {}).get("error") for s in SOURCES if (st.get(s) or {}).get("error")}}
