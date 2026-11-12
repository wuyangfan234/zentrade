"""舆情作战中心 · 评级 (第一期)。只展示: 不进打分、裁决、下单, 不推送。

热度: 每个源的当前值 与 该票自身近 20 个交易日「同一时刻」的基线比 (中位数 + MAD 异常度
z = (x − 中位数) / max(1.4826·MAD, √max(中位数, 1))), 源 → 族取最大 z, 每族换算 0–100
(100·(1 − e^(−z/z_scale))), 再对「有数据的族」取平均 —— 缺源 / 基线不足的族不计入分母。
  族: news 财经新闻 (新闻库条数 news + 高重要度条数 news_hi) / media 全球媒体 (GDELT)
      social 社交 (StockTwits / ApeWisdom / Reddit) / attention 关注 (维基浏览量)
评级: A = 热度过线 + ≥2 族异常 + 方向明确 + 含硬事件 (监管/诉讼/管理层/指引/财报/并购/增发)
      B = 热度过线;  C = 有源异常但热度未过线 (只计数)。

不偷看未来 —— 每条数据有「可得时刻」, as-of 求值只用可得时刻 ≤ as-of 的:
  新闻库   max(发布时刻, 打标时刻 tagged_by.at) (打标随采集批次写入, 是可核验的入库上界)
  GDELT    小时桶结束 + 15 分钟 (官方 15 分钟更新一次; 只用完整小时)
  Reddit   小时桶结束 (只用完整小时)
  快照源   快照时刻 (StockTwits / ApeWisdom)
  维基     当日结束 + 12 小时 (日粒度, 次日才完整; 取保守值)
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Callable

from app.services import sentiment_sources as ss

FAMILY_OF = {"news": "news", "news_hi": "news", "gdelt": "media", "stocktwits": "social", "apewisdom": "social",
             "reddit": "social", "wiki": "attention"}
FAMILIES = ("news", "media", "social", "attention")
HARD_EVENTS = ("regulatory", "litigation", "management", "guidance", "earnings", "mna", "dilution")
IMP_W = {"high": 3, "medium": 1}
GDELT_LAG = timedelta(minutes=15)
WIKI_LAG = timedelta(hours=12)
SNAP_STALE = timedelta(minutes=45)
SNAP_TOL = timedelta(minutes=30)
DEFAULT_TH = {"window_h": 24, "baseline_days": 20, "min_baseline": 10, "z_anom": 3.0, "min_count": 3,
              "z_scale": 4.0, "heat_line": 40, "dir_min_weight": 3, "dir_ratio": 2.0, "episode_gap_h": 24}
_RELEVANT = ("primary", "secondary")


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

class NewsSeries:
    __slots__ = ("created", "items")

    def __init__(self):
        self.created: list[float] = []
        self.items: list[tuple] = []   # (created_ts, avail_ts, importance, direction, event_type, row)


class Inputs:
    def __init__(self):
        self.news: dict[str, NewsSeries] = {}
        self.news_from: float | None = None          # 新闻库覆盖起点 (更早的基线窗口不算)
        self.hourly: dict[str, dict[str, dict]] = {"gdelt": {}, "reddit": {}}
        self.snaps: dict[str, dict[str, list[tuple[float, dict]]]] = {"stocktwits": {}, "apewisdom": {}}
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


def load_inputs(data_dir, symbols: list[str], start: datetime, end: datetime) -> Inputs:
    data_dir = Path(data_dir)
    want = set(symbols)
    inp = Inputs()
    first = None
    for r in _read_news(data_dir, start, end):
        created = ss.parse_iso(r.get("created_at"))
        if created is None:
            continue
        tb = r.get("tagged_by") or {}
        tagged = ss.parse_iso(tb.get("at"))
        avail = max(created, tagged) if tagged else created
        first = avail if first is None or avail < first else first
        tags = r.get("tags") or {}
        syms = [s for s, t in tags.items() if (t or {}).get("relevance") in _RELEVANT] if tags else \
            list((r.get("mentions") or {}).keys())
        for s in syms:
            if s not in want:
                continue
            t = (tags.get(s) or {}) if tags else {}
            item = (_ts(created), _ts(avail), t.get("importance"), t.get("direction"), t.get("event_type"), r)
            inp.news.setdefault(s, NewsSeries()).items.append(item)
    for ns in inp.news.values():
        ns.items.sort(key=lambda x: x[0])
        ns.created = [x[0] for x in ns.items]
    inp.news_from = _ts(first) if first else None
    base = ss.root(data_dir)
    for src in ("gdelt", "reddit"):
        for s in symbols:
            cur = ss.read_json(base / "series" / src / f"{s}.json")
            if cur and cur.get("from") and cur.get("to"):
                inp.hourly[src][s] = cur
    for src in ("stocktwits", "apewisdom"):
        per: dict[str, list] = defaultdict(list)
        for snap in ss.read_snaps(data_dir, src, start, end):
            t = ss.parse_iso(snap.get("at"))
            for s, v in (snap.get("rows") or {}).items():
                if s in want and isinstance(v, dict):
                    per[s].append((_ts(t), v))
        for s, lst in per.items():
            lst.sort(key=lambda x: x[0])
            inp.snaps[src][s] = lst
    for s in symbols:
        cur = ss.read_json(base / "series" / "wiki" / f"{s}.json")
        if cur and cur.get("articles"):
            inp.wiki[s] = cur["articles"]
    return inp


# ── 单源取值 ─────────────────────────────────────────────────────────

def _news_window(ns: NewsSeries | None, end_ts: float, w_s: float, *, hi: bool = False) -> int:
    if ns is None:
        return 0
    i = bisect.bisect_right(ns.created, end_ts - w_s)
    j = bisect.bisect_right(ns.created, end_ts)
    return sum(1 for x in ns.items[i:j] if x[1] <= end_ts and (not hi or x[2] == "high"))


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _hourly_window(cur: dict | None, t: datetime, w_h: int, lag: timedelta) -> int | None:
    if not cur:
        return None
    end = _floor_hour(t - lag)
    start = end - timedelta(hours=w_h)
    if ss.hour_key(start) < cur["from"] or ss.hour_key(end) > cur["to"]:
        return None
    hours = cur.get("hours") or {}
    total, k = 0, start
    while k < end:
        total += int(hours.get(ss.hour_key(k), 0))
        k += timedelta(hours=1)
    return total


def _snap_at(lst: list | None, t: datetime, *, tol: timedelta, exact: bool) -> dict | None:
    """exact=False: t 之前最近一张且不超过 tol; exact=True: 离 t 最近 (前后 tol 内, 但不晚于调用方的 as-of)。"""
    if not lst:
        return None
    ts = _ts(t)
    keys = [x[0] for x in lst]
    i = bisect.bisect_right(keys, ts)
    cands = []
    if i > 0:
        cands.append(lst[i - 1])
    if exact and i < len(lst):
        cands.append(lst[i])
    best = min(cands, key=lambda x: abs(x[0] - ts), default=None)
    if best is None or abs(best[0] - ts) > tol.total_seconds():
        return None
    return best[1]


def robust_z(x: float, base: list[float], min_baseline: int) -> dict | None:
    if x is None or len(base) < min_baseline:
        return None
    med = median(base)
    mad = median(abs(b - med) for b in base)
    scale = max(1.4826 * mad, math.sqrt(max(med, 1.0)))
    return {"x": round(float(x), 2), "med": round(float(med), 2), "mad": round(float(mad), 2),
            "z": round((x - med) / scale, 2), "n": len(base)}


# ── as-of 求值 ───────────────────────────────────────────────────────

def _same_clock(t: datetime, days: list[date]) -> list[datetime]:
    tz = _tz()
    lt = t.astimezone(tz)
    out = []
    for d in days:
        out.append(datetime(d.year, d.month, d.day, lt.hour, lt.minute, lt.second, tzinfo=tz).astimezone(UTC))
    return out


def _sources(inp: Inputs, sym: str, t: datetime, th: dict, prev_days: Callable[[date, int], list[date]]) -> dict:
    w_h = int(th["window_h"])
    w_s = w_h * 3600.0
    n_base = int(th["baseline_days"])
    minb = int(th["min_baseline"])
    t_et = t.astimezone(_tz())
    base_t = _same_clock(t, prev_days(t_et.date(), n_base))
    out: dict[str, dict | None] = {}
    ns = inp.news.get(sym)
    if inp.news_from is not None:
        ok = [b for b in base_t if _ts(b) - w_s >= inp.news_from]
        for key, hi in (("news", False), ("news_hi", True)):
            x = _news_window(ns, _ts(t), w_s, hi=hi)
            out[key] = robust_z(x, [_news_window(ns, _ts(b), w_s, hi=hi) for b in ok], minb)
    for src, lag in (("gdelt", GDELT_LAG), ("reddit", timedelta(0))):
        cur = inp.hourly[src].get(sym)
        x = _hourly_window(cur, t, w_h, lag)
        if x is None:
            out[src] = None
            continue
        base = [v for b in base_t if (v := _hourly_window(cur, b, w_h, lag)) is not None]
        out[src] = robust_z(x, base, minb)
    for src, field in (("stocktwits", "rate_h"), ("apewisdom", "mentions")):
        lst = inp.snaps[src].get(sym)
        cur = _snap_at(lst, t, tol=SNAP_STALE, exact=False)
        if cur is None or cur.get(field) is None:
            out[src] = None
            continue
        base = [float(v[field]) for b in base_t
                if (v := _snap_at(lst, b, tol=SNAP_TOL, exact=True)) is not None and v.get(field) is not None]
        r = robust_z(float(cur[field]), base, minb)
        if r is not None and src == "stocktwits":
            r["bull"], r["bear"] = cur.get("bull"), cur.get("bear")
        out[src] = r
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


def _window_items(inp: Inputs, sym: str, t: datetime, w_s: float) -> list[tuple]:
    ns = inp.news.get(sym)
    if ns is None:
        return []
    end = _ts(t)
    i = bisect.bisect_right(ns.created, end - w_s)
    j = bisect.bisect_right(ns.created, end)
    return [x for x in ns.items[i:j] if x[1] <= end]


def evaluate(inp: Inputs, sym: str, t: datetime, th: dict, *,
             prev_days: Callable[[date, int], list[date]] = us_prev_trading_days, detail: bool = True) -> dict:
    t = t.astimezone(UTC)
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
    items = _window_items(inp, sym, t, float(th["window_h"]) * 3600)
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
    line = heat >= th["heat_line"]
    if line and n_anom >= 2 and direction in ("bullish", "bearish") and hard:
        grade = "A"
    elif line:
        grade = "B"
    elif n_anom >= 1:
        grade = "C"
    else:
        grade = None
    row: dict[str, Any] = {"symbol": sym, "grade": grade, "heat": heat, "direction": direction,
                           "event_type": (hard or other or [None])[0], "hard": bool(hard),
                           "n_sources": sum(1 for f in fam.values() for r in f["sources"].values() if r["anom"]),
                           "n_families": n_anom, "families_ok": sorted(fam), "bull_w": bull, "bear_w": bear}
    key_items = [x for x in items if x[2] == "high"] or [x for x in items if x[2] == "medium"]
    row["first_at"] = ss.iso(datetime.fromtimestamp(key_items[0][0], UTC)) if key_items else None
    if detail:
        row["families"] = fam
        row["missing"] = [f for f in FAMILIES if f not in fam]
        top = sorted(items, key=lambda x: (-IMP_W.get(x[2], 0), -x[0]))[:5]
        row["headlines"] = [{"at": x[5].get("created_at"), "zh": x[5].get("zh_headline") or None,
                             "en": x[5].get("headline"), "importance": x[2], "direction": x[3],
                             "event_type": x[4]} for x in top]
    return row


# ── 看板 (采集线程每轮写 board.json; 页面只读) ───────────────────────

LINE_HOURS = 48
ROLE_ORDER = {"持仓": 0, "自选": 1, "池": 2}


def build_board(data_dir, roles: dict[str, str], now: datetime, *, config: dict | None = None,
                state: dict | None = None, prev_days: Callable[[date, int], list[date]] = us_prev_trading_days,
                overrides: dict | None = None) -> dict:
    cfg = config or ss.load_config()
    th = thresholds(cfg, overrides)
    now = now.astimezone(UTC)
    syms = sorted(roles)
    inp = load_inputs(data_dir, syms, now - timedelta(days=45), now)
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
            steps = [_floor_hour(now) - timedelta(hours=h) for h in range(LINE_HOURS, 0, -1)] + [now]
            r["line"] = [[ss.iso(x), evaluate(inp, s, x, th, prev_days=prev_days, detail=False)["heat"]] for x in steps]
            rows.append(r)
    rows.sort(key=lambda r: (r["grade"], -r["heat"], ROLE_ORDER.get(r.get("role") or "", 9), r["symbol"]))
    st = state if state is not None else ss.read_state(data_dir)
    return {"contract": "sentiment-v1", "as_of": ss.iso(now), "rows": rows, "counts": counts, "symbols": len(syms),
            "families": {f: fam_ok.get(f, 0) for f in FAMILIES},
            "sources": {s: (st.get(s) or {}).get("status") or "pending" for s in ss.SOURCES},
            "window_h": th["window_h"], "heat_line": th["heat_line"], "config_sha": config_sha()}
