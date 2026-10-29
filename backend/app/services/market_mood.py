# ruff: noqa: RUF001  (面向用户的中文文案, 全角标点是有意的)
"""盘前大盘情绪 (无限 2026-10-12 拍板): 08:35 ET 生成当日三档判断, 16:10 ET 记分。只展示, 不碰交易环。

- 六项清单取数 → LLM 按清单写三档概率与每项倾向 → 代码兜底 (guard): 概率取 5 的倍数且和为 100,
  前两档并列一律"观望", VIX/VIX3M > 1.05 只降档 ("收紧多头"), 缺数据不触发, 每项必须引用原始值。
- 每天只生成一次, 生成后锁定不改写 (judgments/<日>.json); 记分追加到 scores.jsonl。
- 记分口径: QQQ 当日收盘 vs 前一交易日收盘, > +0.3% 涨, < -0.3% 跌, 其余震荡。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.config import PUBLIC_HTTP_USER_AGENT
from app.market_clock import us_clock
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

TASK = "market_mood"
PROMPT_VERSION = "mm-1"
GENERATE_AT = time(8, 35)
SCORE_AT = time(16, 10)
SCORE_SYMBOL = "QQQ"
SCORE_BAND_PCT = 0.3
PANIC_RATIO = 1.05
# 近 69 个交易日 QQQ ±0.3% 口径的经验分布 (2026-10 实测), 只在 LLM 不可用时作兜底起点
BASE_RATES = {"up": 40, "down": 35, "range": 25}
SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLP", "XLU", "XLY", "XLI", "XLB", "XLC", "XLRE", "SMH")
ITEMS = (("trend", "趋势"), ("overnight", "隔夜"), ("calendar", "日历"),
         ("rates", "利率"), ("sectors", "板块与宽度"), ("panic", "恐慌"))
LEAN = {"up": "涨", "down": "跌", "range": "震荡"}
STANCE = {"up": "偏多", "down": "偏空", "range": "观望"}
_UA = {"User-Agent": PUBLIC_HTTP_USER_AGENT}
_LOCK = threading.Lock()


# ── 存储 ─────────────────────────────────────────────────────────────

def _root(data_dir) -> Path:
    p = Path(data_dir) / "market_mood"
    (p / "judgments").mkdir(parents=True, exist_ok=True)
    return p


def load_judgment(data_dir, day: date) -> dict | None:
    f = _root(data_dir) / "judgments" / f"{day.isoformat()}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def latest_judgment(data_dir) -> dict | None:
    files = sorted((_root(data_dir) / "judgments").glob("*.json"))
    for f in reversed(files):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def load_scores(data_dir) -> dict[str, dict]:
    f = _root(data_dir) / "scores.jsonl"
    out: dict[str, dict] = {}
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            out[r["day"]] = r
    return out


# ── 取数 (每项失败只记 missing, 不抛) ─────────────────────────────────

def _get(url: str, timeout: float = 25.0) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read()


def _post_json(url: str, body: dict, timeout: float = 20.0):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={**_UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_overnight(now: datetime) -> dict:
    """HL xyz 纳指100/标普500 永续 5 分钟线: 相对前一交易日 16:00 ET 的涨跌、最近 30 分钟涨跌。"""
    prev = us_clock.previous_trading_day(now.date())
    close_at = datetime.combine(prev, time(16, 0), tzinfo=us_clock.tz)
    out: dict = {}
    for key, coin in (("ndx", "xyz:XYZ100"), ("spx", "xyz:SP500")):
        rows = _post_json("https://api.hyperliquid.xyz/info", {"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "5m", "startTime": _ms(close_at - timedelta(minutes=10))}})
        rows = [r for r in rows or [] if r.get("c")]
        if not rows:
            continue
        after = [r for r in rows if r["t"] >= _ms(close_at)]
        base = float(after[0]["o"]) if after else float(rows[0]["c"])
        last = rows[-1]
        last_px = float(last["c"])
        ref30 = [r for r in rows if r["t"] <= last["t"] - 30 * 60_000]
        px30 = float(ref30[-1]["c"]) if ref30 else None
        out[key] = {"last": last_px, "chg_pct": round((last_px / base - 1) * 100, 2),
                    "chg_30m_pct": round((last_px / px30 - 1) * 100, 2) if px30 else None,
                    "as_of": datetime.fromtimestamp(min(last["T"] + 1, _ms(now)) / 1000, us_clock.tz).isoformat()}
    if not out:
        raise ValueError("no HL candles")
    out["as_of"] = min(v["as_of"] for v in out.values())
    return out


def _cboe_last(name: str) -> tuple[date, float]:
    text = _get(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv").decode()
    rows = [r for r in csv.reader(io.StringIO(text)) if r and re.match(r"\d{2}/\d{2}/\d{4}", r[0])]
    d = datetime.strptime(rows[-1][0], "%m/%d/%Y").date()
    return d, float(rows[-1][4])


def fetch_vix(now: datetime) -> dict:
    d1, vix = _cboe_last("VIX")
    d3, vix3m = _cboe_last("VIX3M")
    return {"vix": vix, "vix3m": vix3m, "ratio": round(vix / vix3m, 3), "as_of": min(d1, d3).isoformat()}


def fetch_rates(now: datetime) -> dict:
    y = now.year
    url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
           f"{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
    rows = list(csv.DictReader(io.StringIO(_get(url).decode())))
    cur, prev = rows[0], rows[1] if len(rows) > 1 else None
    out = {"us10y": float(cur["10 Yr"]), "as_of": datetime.strptime(cur["Date"], "%m/%d/%Y").date().isoformat()}
    if prev:
        out["us10y_chg_bp"] = round((out["us10y"] - float(prev["10 Yr"])) * 100)
    try:   # 高收益债利差 (FRED, T+1 更新), 拿不到不影响 10Y
        start = (now.date() - timedelta(days=20)).isoformat()
        text = _get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2&cosd={start}").decode()
        vals = [(r[0], float(r[1])) for r in csv.reader(io.StringIO(text)) if len(r) == 2 and re.match(r"[\d.]+$", r[1])]
        if vals:
            out["hy_oas"] = vals[-1][1]
            out["hy_as_of"] = vals[-1][0]
            if len(vals) > 1:
                out["hy_chg_bp"] = round((vals[-1][1] - vals[-2][1]) * 100)
    except Exception as e:
        logger.info("market mood: HY OAS unavailable: %s", type(e).__name__)
    return out


def fetch_calendar(now: datetime) -> dict:
    data = json.loads(_get("https://nfs.faireconomy.media/ff_calendar_thisweek.json"))
    day = now.date().isoformat()
    days = {str(e.get("date", ""))[:10] for e in data}
    if day not in days and not any(d > day for d in days):
        raise ValueError("calendar feed does not cover today")
    events = [{"time": str(e["date"])[11:16], "title": e.get("title"), "impact": e.get("impact"),
               "forecast": e.get("forecast") or None}
              for e in data if e.get("country") == "USD" and str(e.get("date", ""))[:10] == day
              and e.get("impact") in ("High", "Medium")]
    return {"events": events, "as_of": now.isoformat(timespec="minutes")}


def fetch_trend(repo, now: datetime) -> dict:
    out: dict = {}
    end = us_clock.previous_trading_day(now.date())
    for sym in ("QQQ", "SPY"):
        df = repo.get_daily(sym, end - timedelta(days=45), end, columns=["date", "close"])
        if df is None or df.height < 21:
            continue
        closes = [float(x) for x in df["close"].to_list()]
        ma20 = sum(closes[-20:]) / 20
        out[sym] = {"close": closes[-1], "chg_pct": round((closes[-1] / closes[-2] - 1) * 100, 2),
                    "vs_ma20_pct": round((closes[-1] / ma20 - 1) * 100, 2),
                    "as_of": str(df["date"][-1])[:10]}
    if not out:
        raise ValueError("no QQQ/SPY daily")
    out["as_of"] = min(v["as_of"] for k, v in out.items() if isinstance(v, dict))
    return out


def fetch_sectors(provider, data_dir, now: datetime) -> dict:
    """板块 ETF (Alpaca SIP 日K, 不进日K库) 1 日/5 日涨跌 + 全市场宽度 (日K库最近一日上涨家数占比)。"""
    import polars as pl
    out: dict = {}
    end = us_clock.previous_trading_day(now.date())
    if provider is not None:
        start = datetime.combine(end - timedelta(days=14), time(0), tzinfo=us_clock.tz)
        df = provider.get_daily(list(SECTOR_ETFS), start, datetime.combine(end, time(23, 59), tzinfo=us_clock.tz))
        rows = {}
        if df is not None and not df.is_empty():
            for sym in SECTOR_ETFS:
                c = df.filter(pl.col("symbol") == sym).sort("date")["close"].to_list()
                if len(c) >= 6:
                    rows[sym] = {"d1": round((c[-1] / c[-2] - 1) * 100, 2), "d5": round((c[-1] / c[-6] - 1) * 100, 2)}
            if rows:
                out["etfs"] = rows
                out["etf_as_of"] = str(df["date"].max())[:10]
    parts = sorted((Path(data_dir) / "kline_daily").glob("date=*/part.parquet"))
    if len(parts) >= 2:
        a = pl.read_parquet(parts[-2], columns=["symbol", "close"])
        b = pl.read_parquet(parts[-1], columns=["symbol", "close"])
        j = b.join(a, on="symbol", suffix="_prev").filter(pl.col("close_prev") > 0)
        if j.height:
            up = j.filter(pl.col("close") > pl.col("close_prev")).height
            out["breadth"] = {"up_pct": round(up / j.height * 100, 1), "n": j.height,
                              "as_of": parts[-1].parent.name.split("=", 1)[1]}
    if not out:
        raise ValueError("no sector/breadth data")
    out["as_of"] = min(x for x in (out.get("etf_as_of"), (out.get("breadth") or {}).get("as_of")) if x)
    return out


# ── 清单 ─────────────────────────────────────────────────────────────

def _fmt_pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _item_text(key: str, v: dict | None) -> str:
    if not v:
        return "缺数据"
    if key == "trend":
        return "  ".join(f"{s} {v[s]['close']:.2f} {_fmt_pct(v[s]['chg_pct'])}，距 MA20 {_fmt_pct(v[s]['vs_ma20_pct'])}"
                         for s in ("QQQ", "SPY") if s in v)
    if key == "overnight":
        names = {"ndx": "纳指100永续", "spx": "标普500永续"}
        return "  ".join(f"{names[k]} {_fmt_pct(v[k]['chg_pct'])}（近30分钟 {_fmt_pct(v[k].get('chg_30m_pct'))}）"
                         for k in ("ndx", "spx") if k in v)
    if key == "calendar":
        ev = v.get("events") or []
        return "无重要数据" if not ev else "；".join(f"{e['time']} {e['title']}" + (f"（预期 {e['forecast']}）" if e.get("forecast") else "")
                                               for e in ev[:6])
    if key == "rates":
        s = f"10Y {v['us10y']:.2f}%" + (f"（{v['us10y_chg_bp']:+d}bp）" if v.get("us10y_chg_bp") is not None else "")
        if v.get("hy_oas") is not None:
            s += f"  高收益债利差 {v['hy_oas']:.2f}%" + (f"（{v['hy_chg_bp']:+d}bp）" if v.get("hy_chg_bp") is not None else "")
        return s
    if key == "sectors":
        parts = []
        etfs = v.get("etfs") or {}
        if etfs:
            ranked = sorted(etfs.items(), key=lambda x: -x[1]["d5"])
            parts.append("5日最强 " + " ".join(f"{s} {_fmt_pct(r['d5'])}" for s, r in ranked[:3]))
            parts.append("最弱 " + " ".join(f"{s} {_fmt_pct(r['d5'])}" for s, r in ranked[-3:]))
        if v.get("breadth"):
            parts.append(f"上涨家数 {v['breadth']['up_pct']:.0f}%（{v['breadth']['n']} 只）")
        return "  ".join(parts) or "缺数据"
    if key == "panic":
        return f"VIX {v['vix']:.2f} / VIX3M {v['vix3m']:.2f} = {v['ratio']:.3f}"
    return json.dumps(v, ensure_ascii=False)


def _stale(key: str, v: dict | None, day: date, now: datetime) -> bool:
    """非最新: 日频数据早于前一交易日; 隔夜报价早于生成时刻 30 分钟。"""
    if not v:
        return False
    if key == "overnight":
        stamps = [x["as_of"] for x in v.values() if isinstance(x, dict) and x.get("as_of")] or (
            [v["as_of"]] if v.get("as_of") else [])
        if not stamps:
            return True
        try:
            return min(datetime.fromisoformat(s) for s in stamps) < now - timedelta(minutes=30)
        except ValueError:
            return True
    if not v.get("as_of"):
        return False
    if key == "calendar":
        return False
    return str(v["as_of"])[:10] < us_clock.previous_trading_day(day).isoformat()


def collect(repo, provider, data_dir, now: datetime, fetchers: dict[str, Callable] | None = None) -> list[dict]:
    default = {
        "trend": lambda: fetch_trend(repo, now),
        "overnight": lambda: fetch_overnight(now),
        "calendar": lambda: fetch_calendar(now),
        "rates": lambda: fetch_rates(now),
        "sectors": lambda: fetch_sectors(provider, data_dir, now),
        "panic": lambda: fetch_vix(now),
    }
    default.update(fetchers or {})
    items = []
    for key, label in ITEMS:
        try:
            v = default[key]()
            status = "ok"
        except Exception as e:
            logger.warning("market mood: %s unavailable: %s", key, type(e).__name__)
            v, status = None, "missing"
        if status == "ok" and _stale(key, v, now.date(), now):
            status = "stale"
        items.append({"key": key, "label": label, "raw": v, "text": _item_text(key, v), "status": status,
                      "as_of": (v or {}).get("as_of")})
    return items


# ── 代码兜底规则 ─────────────────────────────────────────────────────

def _round5(probs: dict) -> dict[str, int]:
    """非负、和为 100、取 5 的倍数; 全零或不可解析回落到经验分布。"""
    vals = {}
    for k in ("up", "down", "range"):
        try:
            vals[k] = max(0.0, float(probs.get(k, 0)))
        except (TypeError, ValueError):
            vals[k] = 0.0
    total = sum(vals.values())
    if total <= 0:
        return dict(BASE_RATES)
    out = {k: round(v / total * 20) * 5 for k, v in vals.items()}
    diff = 100 - sum(out.values())
    order = sorted(out, key=lambda k: -vals[k]) if diff > 0 else sorted(out, key=lambda k: (-out[k], -vals[k]))
    i = 0
    while diff != 0:
        k = order[i % 3]
        step = 5 if diff > 0 else -5
        if out[k] + step >= 0:
            out[k] += step
            diff -= step
        i += 1
    return out


def guard(llm: dict | None, items: list[dict]) -> dict:
    """把 LLM 输出收敛成可记账的判断。返回 probs/main/stance/stars/panic/leans/guards。"""
    llm = llm or {}
    guards: list[str] = []
    probs = _round5(llm.get("probs") or {})
    if not llm.get("probs"):
        guards.append("base_rates")
    ranked = sorted(probs, key=lambda k: -probs[k])
    tie = probs[ranked[0]] == probs[ranked[1]]
    main = "range" if tie else ranked[0]
    stance = "range" if tie else main
    if tie:
        guards.append("tie_to_range")
    by_key = {i["key"]: i for i in items}
    leans = {}
    for key, _ in ITEMS:
        it = by_key.get(key) or {}
        raw = (llm.get("items") or {}).get(key) or {}
        lean = raw.get("lean") if raw.get("lean") in ("up", "down", "range") else "range"
        if it.get("status") == "missing":
            lean = None
        leans[key] = {"lean": lean, "note": str(raw.get("note") or "")[:80] if lean else ""}
    panic_raw = (by_key.get("panic") or {}).get("raw") or {}
    ratio = panic_raw.get("ratio") if (by_key.get("panic") or {}).get("status") != "missing" else None
    panic = "tighten_long" if isinstance(ratio, (int, float)) and ratio > PANIC_RATIO else ("none" if ratio is not None else "unknown")
    if panic == "tighten_long" and stance == "up":
        stance = "range"
        guards.append("panic_downgrade")
    try:
        stars = min(5, max(1, int(llm.get("stars") or 3)))
    except (TypeError, ValueError):
        stars = 3
    missing = sum(1 for i in items if i.get("status") == "missing")
    cap = 2 if tie or missing >= 2 else 5
    if stars > cap:
        stars = cap
        guards.append("stars_capped")
    return {"probs": probs, "main": main, "stance": stance, "stars": stars, "panic": panic,
            "leans": leans, "summary": str(llm.get("summary") or "")[:60], "guards": guards}


# ── LLM ──────────────────────────────────────────────────────────────

_SYSTEM = (
    "你是美股盘前分析员。根据给定的六项清单, 判断今天 QQQ 收盘相对昨收的三档概率: "
    f"涨(>+{SCORE_BAND_PCT}%)、跌(<-{SCORE_BAND_PCT}%)、震荡(其余)。"
    f"参考起点: 近 69 个交易日涨 {BASE_RATES['up']} / 跌 {BASE_RATES['down']} / 震荡 {BASE_RATES['range']}。"
    "只用清单里的数字, 不要编造。每项给倾向 up/down/range 和一句引用原始值的理由; 标了缺数据的项写 range。"
    "只输出 JSON: {\"probs\":{\"up\":int,\"down\":int,\"range\":int},\"stars\":1-5,"
    "\"summary\":\"一句话 ≤30 字\",\"items\":{\"trend\":{\"lean\":..,\"note\":..},...}}"
)


def _prompt(items: list[dict], day: date) -> list[dict]:
    lines = [f"日期 {day.isoformat()}（美东）"]
    for it in items:
        flag = {"missing": "（缺数据）", "stale": "（非最新）"}.get(it["status"], "")
        lines.append(f"- {it['key']} {it['label']}: {it['text']}{flag}")
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


def parse_llm(text: str) -> dict:
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.MULTILINE)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object in model output")
    out = json.loads(body[start:end + 1])
    if not isinstance(out, dict):
        raise ValueError("model output is not an object")
    return out


_LOOP = None


def _default_llm(messages) -> str:
    """调度线程用的同步入口; 复用一个事件循环 (同 news_translate, 避免客户端在已关闭循环上回收)。"""
    import asyncio

    from app.services.ai_provider import generate_ai_text
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(
        generate_ai_text(messages, temperature=0.2, max_tokens=None, timeout=120.0, task=TASK))


def _model_label() -> str:
    try:
        from app.services.ai_provider import task_profile
        return task_profile(TASK).get("model") or ""
    except Exception:
        return ""


# ── 生成 / 记分 ──────────────────────────────────────────────────────

def generate(data_dir, repo, provider, *, now: datetime | None = None, llm: Callable | None = None,
             fetchers: dict | None = None, force: bool = False) -> dict | None:
    """08:35 ET 生成当日判断并锁定。非交易日返回 None; 当日已有判断直接返回 (不改写)。"""
    now = now or us_clock.now()
    day = now.date()
    if not us_clock.is_trading_day(day):
        return None
    with _LOCK:
        existing = load_judgment(data_dir, day)
        if existing and not force:
            return existing
        items = collect(repo, provider, data_dir, now, fetchers)
        mode, err, raw = "llm", None, None
        try:
            raw = parse_llm((llm or _default_llm)(_prompt(items, day)))
        except Exception as e:
            logger.warning("market mood: LLM unavailable, using base rates: %s", type(e).__name__)
            mode, err = "fallback", type(e).__name__
        g = guard(raw, items)
        out = {"day": day.isoformat(), "generated_at": now.isoformat(timespec="seconds"),
               "prompt_version": PROMPT_VERSION, "model": _model_label() if mode == "llm" else None,
               "mode": mode, "error": err, **g,
               "items": [{**it, **g["leans"].get(it["key"], {})} for it in items]}
        out.pop("leans", None)
        atomic_write_text(_root(data_dir) / "judgments" / f"{day.isoformat()}.json",
                          json.dumps(out, ensure_ascii=False, indent=1))
        logger.info("market mood %s: %s %s stars=%d mode=%s", day, out["probs"], out["stance"], out["stars"], mode)
        return out


def outcome(chg_pct: float) -> str:
    if chg_pct > SCORE_BAND_PCT:
        return "up"
    if chg_pct < -SCORE_BAND_PCT:
        return "down"
    return "range"


def score(data_dir, repo, *, day: date | None = None) -> dict | None:
    """16:10 ET 记分: 用日K库 QQQ 当日收盘。日K未到 / 当日无判断 → None (盘后管道后会再调一次)。"""
    day = day or us_clock.today()
    j = load_judgment(data_dir, day)
    if not j:
        return None
    if day.isoformat() in load_scores(data_dir):
        return load_scores(data_dir)[day.isoformat()]
    df = repo.get_daily(SCORE_SYMBOL, day - timedelta(days=10), day, columns=["date", "close"])
    if df is None or df.height < 2 or str(df["date"][-1])[:10] != day.isoformat():
        return None
    c0, c1 = float(df["close"][-2]), float(df["close"][-1])
    chg = round((c1 / c0 - 1) * 100, 2)
    res = outcome(chg)
    row = {"day": day.isoformat(), "close": c1, "chg_pct": chg, "outcome": res, "main": j["main"],
           "stance": j["stance"], "hit": j["main"] == res, "p_outcome": j["probs"].get(res),
           "scored_at": us_clock.now().isoformat(timespec="seconds")}
    with _LOCK, (_root(data_dir) / "scores.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def weekly_stats(data_dir, *, end: date | None = None, days: int = 20) -> dict:
    rows = sorted(load_scores(data_dir).values(), key=lambda r: r["day"])
    if end:
        rows = [r for r in rows if r["day"] <= end.isoformat()]
    rows = rows[-days:]
    n = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    by_main = {}
    for k in ("up", "down", "range"):
        sub = [r for r in rows if r["main"] == k]
        by_main[k] = {"n": len(sub), "hit": sum(1 for r in sub if r["hit"])}
    return {"n": n, "hit": hits, "hit_rate": round(hits / n * 100) if n else None, "by_main": by_main,
            "outcomes": {k: sum(1 for r in rows if r["outcome"] == k) for k in ("up", "down", "range")}}


def message(j: dict) -> dict:
    """飞书一条: 只有结果和原始值, 不带解释性文案。"""
    p = j["probs"]
    stars = "★" * j["stars"] + "☆" * (5 - j["stars"])
    head = f"**{LEAN['range']} {p['range']} · {LEAN['down']} {p['down']} · {LEAN['up']} {p['up']}  →  {STANCE[j['stance']]} {stars}**"
    lines = [head]
    if j.get("panic") == "tighten_long":
        lines.append("恐慌开关：收紧多头")
    for it in j.get("items", []):
        flag = {"missing": "（缺数据）", "stale": "（非最新）"}.get(it.get("status"), "")
        lines.append(f"{it['label']}：{it['text']}{flag}")
    if j.get("mode") == "fallback":
        lines.append("模型不可用，概率为近 69 日分布")
    return {"title": "ZenTrade · 盘前大盘", "subtitle": f"{j['day']} {j['generated_at'][11:16]} ET",
            "body": "\n".join(lines)}


def view(data_dir, *, now: datetime | None = None) -> dict:
    """机会页读: 最新判断 + 是否今日 + 是否最新 + 昨日结果 + 近 20 日命中。"""
    now = now or us_clock.now()
    today = now.date()
    j = latest_judgment(data_dir)
    scores = load_scores(data_dir)
    # 此刻"应有"的最新判断日: 交易日 08:35 后是今天, 否则是上一个交易日
    is_td = us_clock.is_trading_day(today)
    expected = today if is_td and now.time() >= GENERATE_AT else us_clock.previous_trading_day(today)
    out: dict = {"now": now.isoformat(timespec="minutes"), "expected_day": expected.isoformat(),
                 "next_at": None, "judgment": None, "is_today": False, "is_latest": False,
                 "stale_items": [], "missing_items": [], "last_score": None, "stats": weekly_stats(data_dir)}
    nxt = today if us_clock.is_trading_day(today) and now.time() < GENERATE_AT else us_clock.next_trading_day(today)
    out["next_at"] = datetime.combine(nxt, GENERATE_AT, tzinfo=us_clock.tz).isoformat(timespec="minutes")
    if j:
        out["judgment"] = j
        out["is_today"] = j["day"] == today.isoformat()
        out["is_latest"] = j["day"] == expected.isoformat()
        out["stale_items"] = [i["label"] for i in j.get("items", []) if i.get("status") == "stale"]
        out["missing_items"] = [i["label"] for i in j.get("items", []) if i.get("status") == "missing"]
        out["score"] = scores.get(j["day"])
    prev = sorted((r for r in scores.values() if not j or r["day"] != j["day"]), key=lambda r: r["day"])
    if prev:
        out["last_score"] = prev[-1]
    return out
