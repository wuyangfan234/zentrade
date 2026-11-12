"""舆情异动 · 评级。只展示: 不进打分、裁决、下单, 不推送; 不调 AI, 不调付费接口。

三类来源, 每类和该票自身近 20 个交易日「同一时刻」的基线比 (中位数 + MAD 异常度
z = (x - 中位数) / max(1.4826·MAD, √max(中位数, 1))):
  news 新闻       新闻库 24 小时条数 (news) 与其中高重要度条数 (news_hi), 取较大的 z
  social Reddit   wallstreetbets + stocks 24 小时提到该股的帖子数
  attention 维基  公司 / 管理层条目前一天浏览量
每类换算 0-100 (100·(1 - e^(-z/z_scale))), 对「有数据的类」取平均 = 热度; 没数据 / 基线不足的类不计入分母。
上榜: 重点 (内部码 A) = 热度过线 + ≥2 类异常 + 新闻方向明确 + 含硬事件 (监管/诉讼/管理层/指引/财报/并购/增发)
      异动 (内部码 B) = 热度过线;  C = 有类异常但热度未过线 (不展示, 只计数)。

不偷看未来 —— 每条数据有「可得时刻」, as-of 求值只用可得时刻 ≤ as-of 的:
  新闻   max(发布时刻, 打标时刻 tagged_by.at) (打标随采集批次写入, 是可核验的入库上界)
  Reddit 发帖时刻 + 30 分钟 (Arctic Shift 入档延迟实测约 15 分钟); 且时间窗必须落在已完整覆盖的区间内
  维基   当日结束 + 12 小时 (日粒度, 次日才完整; 取保守值)
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from app.services import sentiment_sources as ss

FAMILY_OF = {"news": "news", "news_hi": "news", "reddit": "social", "wiki": "attention"}
FAMILIES = ("news", "social", "attention")
HARD_EVENTS = ("regulatory", "litigation", "management", "guidance", "earnings", "mna", "dilution")
IMP_W = {"high": 3, "medium": 1}
WIKI_LAG = timedelta(hours=12)
REDDIT_STALE = timedelta(hours=2)      # 最近一次成功拉取超过 2 小时: 当前窗口不完整, Reddit 这类不计
DEFAULT_TH = {"window_h": 24, "baseline_days": 20, "min_baseline": 10, "z_anom": 3.0, "min_count": 3,
              "z_scale": 4.0, "heat_line": 40, "dir_min_weight": 3, "dir_ratio": 2.0, "episode_gap_h": 24}
_RELEVANT = ("primary", "secondary")
DETAIL_ITEMS = 8


def thresholds(config: dict | None = None, overrides: dict | None = None) -> dict:
    th = dict(DEFAULT_TH)
    th.update((config or {}).get("thresholds") or {})
    th.update(overrides or {})
    return th


def config_sha(path: Path | None = None) -> str:
    return hashlib.sha256(Path(path or ss.CONFIG_PATH).read_bytes()).hexdigest()


def _ts(dt: datetime) -> float:
    return dt.timestamp()


# ── 交易日 ───────────────────────────────────────────────────────────

def us_prev_trading_days(d: date, n: int) -> list[date]:
    """d 之前 (不含 d) 的 n 个交易日, 由近到远。"""
    from app.market_clock import us_clock
    out, cur = [], d
    while len(out) < n:
        cur = us_clock.previous_trading_day(cur)
        out.append(cur)
    return out


def weekday_prev_days(d: date, n: int) -> list[date]:
    """离线替身 (测试 / 无日历时): 周一到周五算交易日。"""
    out, cur = [], d
    while len(out) < n:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            out.append(cur)
    return out


def _tz():
    from app.market_clock import us_clock
    return us_clock.tz


# ── 输入 (一次加载, 多次 as-of 求值) ─────────────────────────────────

class Series:
    """按发布时刻排序的条目; items = (created_ts, avail_ts, importance, direction, event_type, row)。"""
    __slots__ = ("created", "items")

    def __init__(self):
        self.created: list[float] = []
        self.items: list[tuple] = []

    def finish(self) -> None:
        self.items.sort(key=lambda x: x[0])
        self.created = [x[0] for x in self.items]


class Inputs:
    def __init__(self):
        self.news: dict[str, Series] = {}
        self.news_from: float | None = None          # 新闻库覆盖起点 (更早的基线窗口不算)
        self.reddit: dict[str, Series] = {}
        self.reddit_span: tuple[float, float] | None = None
        self.wiki: dict[str, dict[str, dict[str, int]]] = {}


def _read_news(data_dir: Path, start: datetime, end: datetime) -> list[dict]:
    """只读月分片 (同 id 后写覆盖, 与 news_store 一致)。不建目录。"""
    d = Path(data_dir) / "news"
    if not d.is_dir():
        return []
    rows: dict[str, dict] = {}
    m = date(start.year, start.month, 1)
    while m <= end.date():
        p = d / f"{m.year:04d}-{m.month:02d}.jsonl"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("id"):
                        rows[r["id"]] = r
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return list(rows.values())


def _default_names(symbols: list[str]) -> dict[str, dict]:
    return ss.query_names(symbols, ss.load_config(), None)


def load_inputs(data_dir, symbols: list[str], start: datetime, end: datetime, *, names: dict | None = None) -> Inputs:
    data_dir = Path(data_dir)
    want = set(symbols)
    inp = Inputs()
    first = None
    for r in _read_news(data_dir, start, end):
        created = ss.parse_iso(r.get("created_at"))
        if created is None:
            continue
        tagged = ss.parse_iso((r.get("tagged_by") or {}).get("at"))
        avail = max(created, tagged) if tagged else created
        first = avail if first is None or avail < first else first
        tags = r.get("tags") or {}
        syms = [s for s, t in tags.items() if (t or {}).get("relevance") in _RELEVANT] if tags else \
            list((r.get("mentions") or {}).keys())
        for s in syms:
            if s not in want:
                continue
            t = (tags.get(s) or {}) if tags else {}
            inp.news.setdefault(s, Series()).items.append(
                (_ts(created), _ts(avail), t.get("importance"), t.get("direction"), t.get("event_type"), r))
    for sr in inp.news.values():
        sr.finish()
    inp.news_from = _ts(first) if first else None

    span = ss.reddit_span(data_dir)
    if span:
        inp.reddit_span = (_ts(span[0]), _ts(span[1]))
        names = names or _default_names(symbols)
        pre = {s: (ss.prefilter_words(s, (names.get(s) or {}).get("aliases") or []),
                   (names.get(s) or {}).get("aliases") or []) for s in symbols}
        lag = ss.REDDIT_LAG.total_seconds()
        for p in ss.read_posts(data_dir, start, end):
            title, body = p.get("title") or "", p.get("selftext") or ""
            low = f"{title} {body}".lower()
            for s, (words, aliases) in pre.items():
                if any(w in low for w in words) and (ss.mentions(title, s, aliases) or ss.mentions(body, s, aliases)):
                    c = float(p["created_utc"])
                    inp.reddit.setdefault(s, Series()).items.append((c, c + lag, None, None, None, p))
        for sr in inp.reddit.values():
            sr.finish()

    base = ss.root(data_dir)
    for s in symbols:
        cur = ss.read_json(base / "series" / "wiki" / f"{s}.json")
        if cur and cur.get("articles"):
            inp.wiki[s] = cur["articles"]
    return inp


# ── 单源取值 ─────────────────────────────────────────────────────────

def _window(sr: Series | None, end_ts: float, w_s: float, *, hi: bool = False) -> list[tuple]:
    if sr is None:
        return []
    i = bisect.bisect_right(sr.created, end_ts - w_s)
    j = bisect.bisect_right(sr.created, end_ts)
    return [x for x in sr.items[i:j] if x[1] <= end_ts and (not hi or x[2] == "high")]


def robust_z(x: float, base: list[float], min_baseline: int) -> dict | None:
    if x is None or len(base) < min_baseline:
        return None
    med = median(base)
    mad = median(abs(b - med) for b in base)
    scale = max(1.4826 * mad, math.sqrt(max(med, 1.0)))
    return {"x": round(float(x), 2), "med": round(float(med), 2), "mad": round(float(mad), 2),
            "z": round((x - med) / scale, 2), "n": len(base)}


def _same_clock(t: datetime, days: list[date]) -> list[datetime]:
    tz = _tz()
    lt = t.astimezone(tz)
    return [datetime(d.year, d.month, d.day, lt.hour, lt.minute, lt.second, tzinfo=tz).astimezone(UTC) for d in days]


def _sources(inp: Inputs, sym: str, t: datetime, th: dict, prev_days: Callable[[date, int], list[date]]) -> dict:
    w_s = float(th["window_h"]) * 3600.0
    n_base = int(th["baseline_days"])
    minb = int(th["min_baseline"])
    base_t = _same_clock(t, prev_days(t.astimezone(_tz()).date(), n_base))
    out: dict[str, dict | None] = {}
    if inp.news_from is not None:
        ns = inp.news.get(sym)
        ok = [b for b in base_t if _ts(b) - w_s >= inp.news_from]
        for key, hi in (("news", False), ("news_hi", True)):
            x = len(_window(ns, _ts(t), w_s, hi=hi))
            out[key] = robust_z(x, [len(_window(ns, _ts(b), w_s, hi=hi)) for b in ok], minb)
    out["reddit"] = None
    if inp.reddit_span:
        lo, hi_ts = inp.reddit_span
        if _ts(t) - w_s >= lo and _ts(t) <= hi_ts + REDDIT_STALE.total_seconds():
            rs = inp.reddit.get(sym)
            ok = [b for b in base_t if _ts(b) - w_s >= lo]
            out["reddit"] = robust_z(len(_window(rs, _ts(t), w_s)), [len(_window(rs, _ts(b), w_s)) for b in ok], minb)
    arts = inp.wiki.get(sym) or {}
    day = (t - WIKI_LAG).date() - timedelta(days=1)
    best = None
    wdays = prev_days(day, n_base)
    for a, views in arts.items():
        x = views.get(day.isoformat())
        if x is None:
            continue
        r = robust_z(float(x), [float(views[d.isoformat()]) for d in wdays if d.isoformat() in views], minb)
        if r is not None and (best is None or r["z"] > best["z"]):
            best = {**r, "article": a, "day": day.isoformat()}
    out["wiki"] = best
    return out


def _h(z: float, z_scale: float) -> float:
    return 100.0 * (1.0 - math.exp(-max(z, 0.0) / z_scale))


def evaluate(inp: Inputs, sym: str, t: datetime, th: dict, *,
             prev_days: Callable[[date, int], list[date]] = us_prev_trading_days, detail: bool = True) -> dict:
    t = t.astimezone(UTC)
    w_s = float(th["window_h"]) * 3600
    srcs = _sources(inp, sym, t, th, prev_days)
    fam: dict[str, dict] = {}
    for key, r in srcs.items():
        if r is None:
            continue
        r["anom"] = bool(r["z"] >= th["z_anom"] and r["x"] >= (th["min_count"] if key != "wiki" else 0))
        f = fam.setdefault(FAMILY_OF[key], {"z": r["z"], "anom": False, "sources": {}})
        f["sources"][key] = r
        f["z"] = max(f["z"], r["z"])
        f["anom"] = f["anom"] or r["anom"]
    for f in fam.values():
        f["heat"] = round(_h(f["z"], float(th["z_scale"])))
    heat = round(sum(f["heat"] for f in fam.values()) / len(fam)) if fam else 0
    n_anom = sum(1 for f in fam.values() if f["anom"])
    items = _window(inp.news.get(sym), _ts(t), w_s)
    bull = sum(IMP_W.get(x[2], 0) for x in items if x[3] == "bullish")
    bear = sum(IMP_W.get(x[2], 0) for x in items if x[3] == "bearish")
    hi, lo = max(bull, bear), min(bull, bear)
    if hi >= th["dir_min_weight"] and hi >= th["dir_ratio"] * lo:
        direction = "bullish" if bull > bear else "bearish"
    elif bull or bear:
        direction = "mixed"
    else:
        direction = "none"
    weights: dict[str, int] = defaultdict(int)
    for x in items:
        if x[2] in IMP_W and x[4]:
            weights[x[4]] += IMP_W[x[2]]
    hard = sorted((k for k in weights if k in HARD_EVENTS), key=lambda k: -weights[k])
    other = sorted((k for k in weights if k not in HARD_EVENTS and k != "other"), key=lambda k: -weights[k])
    over = heat >= th["heat_line"]
    if over and n_anom >= 2 and direction in ("bullish", "bearish") and hard:
        grade = "A"
    elif over:
        grade = "B"
    elif n_anom >= 1:
        grade = "C"
    else:
        grade = None
    row: dict[str, Any] = {"symbol": sym, "grade": grade, "heat": heat, "direction": direction,
                           "event_type": (hard or other or [None])[0], "hard": bool(hard),
                           "n_families": n_anom, "families_ok": sorted(fam), "bull_w": bull, "bear_w": bear}
    key_items = [x for x in items if x[2] == "high"] or [x for x in items if x[2] == "medium"]
    row["first_at"] = ss.iso(datetime.fromtimestamp(key_items[0][0], UTC)) if key_items else None
    if detail:
        row["families"] = fam
        row["missing"] = [f for f in FAMILIES if f not in fam]
        top = sorted(items, key=lambda x: (-IMP_W.get(x[2], 0), -x[0]))[:DETAIL_ITEMS]
        row["headlines"] = [{"at": x[5].get("created_at"), "zh": x[5].get("zh_headline") or None,
                             "en": x[5].get("headline"), "importance": x[2], "direction": x[3],
                             "event_type": x[4]} for x in top]
        posts = sorted(_window(inp.reddit.get(sym), _ts(t), w_s), key=lambda x: -x[0])[:DETAIL_ITEMS]
        row["posts"] = [{"at": ss.iso(datetime.fromtimestamp(x[0], UTC)), "title": x[5].get("title") or "",
                         "sub": x[5].get("sub"), "id": x[5].get("id")} for x in posts]
    return row


# ── 看板 (采集线程每轮写 board.json; 页面只读) ───────────────────────

ROLE_ORDER = {"持仓": 0, "自选": 1, "池": 2}


def build_board(data_dir, roles: dict[str, str], now: datetime, *, config: dict | None = None,
                state: dict | None = None, prev_days: Callable[[date, int], list[date]] = us_prev_trading_days,
                overrides: dict | None = None, names: dict | None = None) -> dict:
    cfg = config or ss.load_config()
    th = thresholds(cfg, overrides)
    now = now.astimezone(UTC)
    syms = sorted(roles)
    names = names or ss.query_names(syms, cfg, None)
    inp = load_inputs(data_dir, syms, now - timedelta(days=45), now, names=names)
    rows, counts = [], {"A": 0, "B": 0, "C": 0}
    fam_ok: dict[str, int] = defaultdict(int)
    for s in syms:
        r = evaluate(inp, s, now, th, prev_days=prev_days)
        for f in r["families_ok"]:
            fam_ok[f] += 1
        if r["grade"]:
            counts[r["grade"]] += 1
        if r["grade"] in ("A", "B"):
            r["role"] = roles.get(s)
            rows.append(r)
    rows.sort(key=lambda r: (r["grade"], -r["heat"], ROLE_ORDER.get(r.get("role") or "", 9), r["symbol"]))
    st = state if state is not None else ss.read_state(data_dir)
    return {"contract": "sentiment-v2", "as_of": ss.iso(now), "rows": rows, "counts": counts, "symbols": len(syms),
            "families": {f: fam_ok.get(f, 0) for f in FAMILIES},
            "sources": {s: (st.get(s) or {}).get("status") or "pending" for s in ss.SOURCES},
            "window_h": th["window_h"], "heat_line": th["heat_line"], "config_sha": config_sha()}
