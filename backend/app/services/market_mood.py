# ruff: noqa: RUF001  (面向用户的中文文案, 全角标点是有意的)
"""盘前大盘情绪 (2026-10-12 产品决策): 08:35 ET 生成当日三档判断, 16:10 ET 记分。只展示, 不碰交易环。

- 六项清单取数 → LLM 按清单写三档概率与每项倾向 → 代码兜底 (guard): 概率取 5 的倍数且和为 100,
  前两档并列一律"观望", VIX/VIX3M > 1.05 只降档 ("收紧多头"), 缺数据不触发, 每项必须引用原始值。
- 每天只生成一次, 生成后锁定不改写 (judgments/<日>.json); 记分追加到 scores.jsonl
  (16:10 可能是盘中快照价; 18:00 按定版收盘补记, 收盘价不同则追加覆盖行, 读时同日后写的生效)。
- 整点更新: 09:25 与 10:00-15:00 每个整点 (半日市只到收盘前) 按同一清单加第 7 项 "QQQ 现价" 重跑一次,
  只追加快照 (updates/<日>.jsonl), 原判与已写快照都不改写; 过了检查点 15 分钟不补, 模型失败不写、不退回经验分布。
  收盘后与原判用同一收盘价逐时点记分 (update_scores.jsonl, 键 = 日 + 时点, 规则同原判)。
- 记分口径: QQQ 当日收盘 vs 前一交易日收盘, > +0.3% 涨, < -0.3% 跌, 其余震荡; 两档并列 (观望) 不算判对。
  另记三档概率打分 (Brier: 各档 (概率 - 是否发生)^2 之和, 0 = 全对, 越低越准)。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
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
# 整点更新 (只展示): 检查点 (ET) = 开盘前 09:25 + 10:00-15:00 每个整点; 半日市只留收盘前的点
UPDATE_PROMPT_VERSION = "mmu-1"
UPDATE_SLOTS = (time(9, 25), time(10, 0), time(11, 0), time(12, 0), time(13, 0), time(14, 0), time(15, 0))
UPDATE_WINDOW = timedelta(minutes=15)       # 检查点后 15 分钟内才跑, 迟到不补
UPDATE_MISS_AFTER = timedelta(minutes=30)   # 检查点过 30 分钟仍无快照 = 页面标 "未更新"
LIVE_ITEM = ("live", "QQQ 现价")
LIVE_STALE = timedelta(minutes=10)          # 最新成交早于 10 分钟前 = 非最新
_UA = {"User-Agent": PUBLIC_HTTP_USER_AGENT}
_LOCK = threading.Lock()
_UPDATE_LOCK = threading.Lock()             # 整点更新: 同一时点只写一条
_LLM_LOCK = threading.Lock()                # 模块级事件循环同一时刻只给一个调用用 (原判补跑与整点更新可能并发)


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


def _read_jsonl(path: Path) -> list[dict]:
    """只读 (不建目录); 坏行跳过。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    """追加若干行 (调用方持锁)。上一行没写完 (结尾不是换行) 时先补一个换行, 坏行不连累新行。"""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    head = ""
    try:
        if path.stat().st_size > 0:
            with path.open("rb") as f:
                f.seek(-1, 2)
                head = "" if f.read(1) == b"\n" else "\n"
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as f:
        f.write(head + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def _updates_path(data_dir, day: date) -> Path:
    return Path(data_dir) / "market_mood" / "updates" / f"{day.isoformat()}.jsonl"


def load_updates(data_dir, day: date) -> list[dict]:
    """当日整点快照, 按时点升序; 同一时点先写的生效 (不改写)。只读, 不建目录。"""
    seen: dict[str, dict] = {}
    for r in _read_jsonl(_updates_path(data_dir, day)):
        if r.get("day") == day.isoformat() and isinstance(r.get("slot"), str) and isinstance(r.get("probs"), dict):
            seen.setdefault(r["slot"], r)
    return [seen[k] for k in sorted(seen)]


def load_update_scores(data_dir) -> dict[str, dict]:
    """逐时点记分 {"<日> <时点>": 行}, 同键后写的生效。只读, 不建目录。"""
    out: dict[str, dict] = {}
    for r in _read_jsonl(Path(data_dir) / "market_mood" / "update_scores.jsonl"):
        if isinstance(r.get("day"), str) and isinstance(r.get("slot"), str):
            out[f"{r['day']} {r['slot']}"] = r
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
    """HL xyz 纳指100/标普500 永续 5 分钟线: 相对前一交易日收盘时刻 (半日市 13:00) 的涨跌、最近 30 分钟涨跌。"""
    prev = us_clock.previous_trading_day(now.date())
    close_at = datetime.combine(prev, us_clock.session_close(prev), tzinfo=us_clock.tz)
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


def _official_closes(part: Path):
    """一个日K分区里已定版的收盘: 丢掉盘中快照行 (quote_ts 非空); 早期分区没有这一列。"""
    import polars as pl
    cols = ["symbol", "close", *(["quote_ts"] if "quote_ts" in pl.read_parquet_schema(part) else [])]
    df = pl.read_parquet(part, columns=cols)
    return df.filter(pl.col("quote_ts").is_null()).drop("quote_ts") if "quote_ts" in cols else df


def fetch_sectors(provider, data_dir, now: datetime) -> dict:
    """板块 ETF (Alpaca SIP 日K, 不进日K库) 1 日/5 日涨跌 + 全市场宽度 (日K库前一交易日上涨家数占比)。"""
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
    # 只认前一交易日及更早的分区, 并丢掉快照行: 盘中当天分区一直在写盘中快照 (11-19 生产 14:00 ET 实测 7032 行全是快照),
    # 前一交易日分区也会留下少量未定版的快照行 (同日 162 行)。整点更新在盘中重跑, 不能把盘中价当成昨日宽度。
    parts = [p for p in sorted((Path(data_dir) / "kline_daily").glob("date=*/part.parquet"))
             if p.parent.name.split("=", 1)[1] <= end.isoformat()]
    if len(parts) >= 2:
        a = _official_closes(parts[-2])
        b = _official_closes(parts[-1])
        j = b.join(a, on="symbol", suffix="_prev").filter(pl.col("close_prev") > 0)
        if j.height:
            up = j.filter(pl.col("close") > pl.col("close_prev")).height
            out["breadth"] = {"up_pct": round(up / j.height * 100, 1), "n": j.height,
                              "as_of": parts[-1].parent.name.split("=", 1)[1]}
    if not out:
        raise ValueError("no sector/breadth data")
    out["as_of"] = min(x for x in (out.get("etf_as_of"), (out.get("breadth") or {}).get("as_of")) if x)
    return out


def fetch_live(data_dir, now: datetime) -> dict:
    """第 7 项 (只给整点更新): QQQ 此刻最新成交对官方前收 = 盘面情报温度带缓存 (Alpaca IEX 最新成交 · SIP 官方前收,
    情报任务盘前盘中每 60 秒刷新)。只读缓存, 不出网; 缓存里没有今天的成交 = 没有现价。"""
    from app.services import intel_calc, intel_panel
    tape = intel_panel.tape_view(Path(data_dir), now)
    row = next((r for r in tape.get("indexes") or [] if r.get("symbol") == SCORE_SYMBOL), None)
    if not row or not row.get("live") or row.get("day") != now.date().isoformat():
        raise ValueError("no live QQQ quote today")
    at = intel_calc._parse_iso(row.get("as_of"))
    price, chg = row.get("price"), row.get("chg_pct")
    if at is None or not isinstance(price, (int, float)) or not isinstance(chg, (int, float)):
        raise ValueError("bad QQQ tape row")
    return {"price": float(price), "chg_pct": float(chg), "session": row.get("session"), "time_et": at.strftime("%H:%M"),
            "source": tape.get("source"), "as_of": at.isoformat(timespec="seconds")}


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
    if key == "live":
        when = " ".join(x for x in ({"pre": "盘前", "regular": "盘中", "post": "盘后"}.get(v.get("session") or ""),
                                    v.get("time_et"), "ET") if x)
        return f"QQQ {v['price']:.2f} {_fmt_pct(v['chg_pct'])}（对官方前收，{when}）"
    return json.dumps(v, ensure_ascii=False)


def _stale(key: str, v: dict | None, day: date, now: datetime) -> bool:
    """非最新: 日频数据早于前一交易日; 隔夜报价早于生成时刻 30 分钟; QQQ 现价的最新成交早于 10 分钟。"""
    if not v:
        return False
    if key == "live":
        try:
            return datetime.fromisoformat(str(v.get("as_of"))) < now - LIVE_STALE
        except (TypeError, ValueError):
            return True
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


def collect(repo, provider, data_dir, now: datetime, fetchers: dict[str, Callable] | None = None, *,
            live: bool = False) -> list[dict]:
    """六项清单逐项取数 (每项失败只记缺数据); live=True (整点更新) 时追加第 7 项 QQQ 现价。"""
    default = {
        "trend": lambda: fetch_trend(repo, now),
        "overnight": lambda: fetch_overnight(now),
        "calendar": lambda: fetch_calendar(now),
        "rates": lambda: fetch_rates(now),
        "sectors": lambda: fetch_sectors(provider, data_dir, now),
        "panic": lambda: fetch_vix(now),
        "live": lambda: fetch_live(data_dir, now),
    }
    default.update(fetchers or {})
    spec = (*ITEMS, LIVE_ITEM) if live else ITEMS
    items = []
    for key, label in spec:
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
    base_keys = [k for k, _ in ITEMS]                      # 六项照旧; 整点更新多出的项 (QQQ 现价) 接在后面
    for key in base_keys + [k for k in by_key if k not in base_keys]:
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


# 整点更新用的提示词: 同一清单加 QQQ 现价; 不给经验分布起点, 也不把 08:35 原判喂回去 (各时点独立判断、独立记分)
_SYSTEM_UPDATE = (
    "你是美股盘中分析员。根据给定清单, 判断今天 QQQ 收盘相对昨收的三档概率: "
    f"涨(>+{SCORE_BAND_PCT}%)、跌(<-{SCORE_BAND_PCT}%)、震荡(其余)。记分只看今天收盘, 不看此刻涨跌。"
    "清单第 7 项 live 是 QQQ 此刻最新成交相对官方前收的涨跌 (开盘前为盘前成交); 离收盘越近, 剩余可波动的时间越短。"
    "其余六项与盘前同一口径, 每项标了截至时点。只用清单里的数字, 不要编造。"
    "每项给倾向 up/down/range 和一句引用原始值的理由; 标了缺数据的项写 range。"
    "只输出 JSON: {\"probs\":{\"up\":int,\"down\":int,\"range\":int},\"stars\":1-5,"
    "\"summary\":\"一句话 ≤30 字\",\"items\":{\"trend\":{\"lean\":..,\"note\":..},...,\"live\":{\"lean\":..,\"note\":..}}}"
)


def _asof_short(v) -> str:
    s = str(v or "")
    return s[5:10] + " " + s[11:16] if len(s) >= 16 else s[:10]


def _prompt_update(items: list[dict], now: datetime) -> list[dict]:
    day = now.date()
    close = datetime.combine(day, us_clock.session_close(day), tzinfo=us_clock.tz)
    left = max(0, round((close - now).total_seconds() / 60))
    pre = "（未开盘）" if now.time() < us_clock.sessions[0].open else ""
    lines = [f"日期 {day.isoformat()}（美东），此刻 {now.strftime('%H:%M')} ET{pre}，距收盘 {left} 分钟"]
    for it in items:
        flag = {"missing": "（缺数据）", "stale": "（非最新）"}.get(it["status"], "")
        asof = f"（截至 {_asof_short(it.get('as_of'))}）" if it.get("as_of") and it["status"] != "missing" else ""
        lines.append(f"- {it['key']} {it['label']}: {it['text']}{flag}{asof}")
    return [{"role": "system", "content": _SYSTEM_UPDATE}, {"role": "user", "content": "\n".join(lines)}]


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

    from app.services.ai_provider import OPENAI_REQUEST_TIMEOUT_S, generate_ai_text
    global _LOOP
    with _LLM_LOCK:     # 同一个循环不能被两个线程同时 run_until_complete
        if _LOOP is None or _LOOP.is_closed():
            _LOOP = asyncio.new_event_loop()
        return _LOOP.run_until_complete(
            generate_ai_text(messages, temperature=0.2, max_tokens=None, timeout=OPENAI_REQUEST_TIMEOUT_S, task=TASK))


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


# ── 整点更新 ─────────────────────────────────────────────────────────

def update_slots(day: date) -> list[time]:
    """当日整点更新检查点 (ET): 非交易日为空; 半日市只留收盘前的点 (13:00 收盘 = 到 12:00)。"""
    if not us_clock.is_trading_day(day):
        return []
    close = us_clock.session_close(day)
    return [s for s in UPDATE_SLOTS if s < close]


def due_slot(now: datetime) -> time | None:
    """now 落在哪个检查点的执行窗口 [检查点, 检查点 + 15 分钟); 都不在 = None (迟到不补)。"""
    now = us_clock.market_datetime(now)
    for s in update_slots(now.date()):
        start = datetime.combine(now.date(), s, tzinfo=us_clock.tz)
        if start <= now < start + UPDATE_WINDOW:
            return s
    return None


def _usable_probs(p) -> bool:
    """模型给的三档概率能用 (至少一档是正的有限数)。不能用就不写快照, 不像原判那样退回经验分布。"""
    if not isinstance(p, dict):
        return False
    total = 0.0
    for k in ("up", "down", "range"):
        try:
            v = float(p.get(k, 0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            return False
        total += max(v, 0.0)
    return total > 0


def update(data_dir, repo, provider, *, now: datetime | None = None, llm: Callable | None = None,
           fetchers: dict | None = None) -> dict | None:
    """09:25 与 10:00-15:00 整点: 同一清单 + QQQ 现价重跑一次, 追加一条快照 (updates/<日>.jsonl)。
    不在检查点窗口 / 非交易日 / 当日没有 08:35 原判 = None; 同一时点已有快照直接返回 (不再调模型);
    模型失败或没给出可用概率 = 不写。08:35 原判与已写的快照都不改写。只展示, 不推送。"""
    now = us_clock.market_datetime(now or us_clock.now())
    day = now.date()
    slot = due_slot(now)
    if slot is None:
        return None
    key = slot.strftime("%H:%M")
    with _UPDATE_LOCK:
        if load_judgment(data_dir, day) is None:
            return None
        done = {u["slot"]: u for u in load_updates(data_dir, day)}
        if key in done:
            return done[key]
        items = collect(repo, provider, data_dir, now, fetchers, live=True)
        try:
            raw = parse_llm((llm or _default_llm)(_prompt_update(items, now)))
            if not _usable_probs(raw.get("probs")):
                raise ValueError("no usable probs in model output")
            g = guard(raw, items)
        except Exception as e:
            logger.warning("market mood update %s %s skipped (model unavailable): %s", day, key, type(e).__name__)
            return None
        out = {"day": day.isoformat(), "slot": key, "generated_at": now.isoformat(timespec="seconds"),
               "prompt_version": UPDATE_PROMPT_VERSION, "model": _model_label(), "mode": "llm", "error": None, **g,
               "items": [{**it, **g["leans"].get(it["key"], {})} for it in items]}
        out.pop("leans", None)
        _append_jsonl(_updates_path(data_dir, day), [out])
        logger.info("market mood update %s %s: %s %s stars=%d", day, key, out["probs"], out["stance"], out["stars"])
        return out


def outcome(chg_pct: float) -> str:
    if chg_pct > SCORE_BAND_PCT:
        return "up"
    if chg_pct < -SCORE_BAND_PCT:
        return "down"
    return "range"


def _official(repo, day: date) -> bool:
    """day 的日K已按官方收盘定版 (盘后管道标记过)。读不到 = 否。"""
    try:
        got = repo.latest_settled_daily_date()
    except Exception:
        return False
    return isinstance(got, date) and got >= day


def brier(probs, res: str | None) -> float | None:
    """三档概率打分 (Brier): 各档 (概率 - 是否发生)^2 之和。0 = 全对, 越低越准, 最差 2。概率或结果读不到 = None。"""
    try:
        ps = {k: float(probs[k]) / 100 for k in ("up", "down", "range")}
    except (KeyError, TypeError, ValueError):
        return None
    if res not in ps or not all(math.isfinite(v) for v in ps.values()):
        return None
    return round(sum((v - (1.0 if k == res else 0.0)) ** 2 for k, v in ps.items()), 4)


def _score_base(day: date, close: float, chg: float, res: str, j: dict, slot: str | None = None) -> dict:
    """一行记分 (原判与各时点同一口径)。两档并列 (观望) 不押方向, 不算判对。"""
    probs = j.get("probs") or {}
    tie = "tie_to_range" in (j.get("guards") or [])
    return {"day": day.isoformat(), **({"slot": slot} if slot else {}), "close": close, "chg_pct": chg, "outcome": res,
            "main": j.get("main"), "stance": j.get("stance"), "hit": not tie and j.get("main") == res,
            "p_outcome": probs.get(res), "probs": probs, "brier": brier(probs, res)}


def _settle(old: dict | None, base: dict, close: float, final: bool, at: str) -> dict | None:
    """记分规则 (原判与各时点共用): 已定版不再改; 收盘价没变 = 只在这次定版时补 final (结果不动), 否则不写;
    收盘价变了 = 按新价重算 (带 rescored_from)。返回要追加的行, 不用写 = None。"""
    if old and old.get("final"):
        return None
    if old and old.get("close") == close:
        return {**old, "final": True} if final else None
    return {**base, "scored_at": at, "final": final, **({"rescored_from": old.get("close")} if old else {})}


def score(data_dir, repo, *, day: date | None = None) -> dict | None:
    """16:10 ET 记分: 用日K库 QQQ 当日收盘。日K未到 / 当日无判断 → None (盘后管道后会再调一次)。
    16:10 那次可能还是盘中快照价 (收盘竞价没进); 18:00 补记分按定版收盘重算, 收盘价不同就覆盖 (追加一行, 读时后写的生效)。
    已按定版收盘记过 (final) 的不再改。当日整点快照用同一收盘价逐时点各记一行 (update_scores.jsonl, 规则相同)。
    返回原判那一行; 已写的行不回补、不改写。"""
    day = day or us_clock.today()
    j = load_judgment(data_dir, day)
    if not j:
        return None
    old = load_scores(data_dir).get(day.isoformat())
    prefix = f"{day.isoformat()} "
    old_u = {k[len(prefix):]: r for k, r in load_update_scores(data_dir).items() if k.startswith(prefix)}
    pending = [u for u in load_updates(data_dir, day) if not (old_u.get(u["slot"]) or {}).get("final")]
    if old and old.get("final") and not pending:
        return old
    df = repo.get_daily(SCORE_SYMBOL, day - timedelta(days=10), day, columns=["date", "close"])
    if df is None or df.height < 2 or str(df["date"][-1])[:10] != day.isoformat():
        return old
    c0, c1 = float(df["close"][-2]), float(df["close"][-1])
    final = _official(repo, day)
    chg = round((c1 / c0 - 1) * 100, 2)
    res = outcome(chg)
    at = us_clock.now().isoformat(timespec="seconds")
    row = _settle(old, _score_base(day, c1, chg, res, j), c1, final, at)
    urows = [r for u in pending
             if (r := _settle(old_u.get(u["slot"]), _score_base(day, c1, chg, res, u, u["slot"]), c1, final, at))]
    with _LOCK:
        if row is not None:
            _append_jsonl(_root(data_dir) / "scores.jsonl", [row])
        _append_jsonl(_root(data_dir) / "update_scores.jsonl", urows)
    return row if row is not None else old


def _mean3(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


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
    # 概率打分: 原判 (较早的记分行没存概率 = 读当天判断文件) 与同一批日子的固定分布对照; 各时点只算同一批日子
    pairs = []
    for r in rows:
        b = r.get("brier") if isinstance(r.get("brier"), (int, float)) else None
        if b is None:
            probs = r.get("probs")
            if not isinstance(probs, dict):
                try:
                    probs = (load_judgment(data_dir, date.fromisoformat(r["day"])) or {}).get("probs")
                except ValueError:
                    probs = None
            b = brier(probs, r.get("outcome"))
        base = brier(BASE_RATES, r.get("outcome"))
        if b is not None and base is not None:
            pairs.append((b, base))
    window = {r["day"] for r in rows}
    by_slot: dict[str, list[dict]] = {}
    for r in load_update_scores(data_dir).values():
        if r.get("day") in window:
            by_slot.setdefault(r["slot"], []).append(r)
    slots = []
    for s in sorted(by_slot):
        rs = by_slot[s]
        bs = [(r["brier"], base) for r in rs if isinstance(r.get("brier"), (int, float))
              and (base := brier(BASE_RATES, r.get("outcome"))) is not None]
        slots.append({"slot": s, "n": len(rs), "hit": sum(1 for r in rs if r.get("hit")),
                      "brier": _mean3([b for b, _ in bs]), "brier_base": _mean3([x for _, x in bs])})
    return {"n": n, "hit": hits, "hit_rate": round(hits / n * 100) if n else None, "by_main": by_main,
            "outcomes": {k: sum(1 for r in rows if r["outcome"] == k) for k in ("up", "down", "range")},
            "brier": _mean3([b for b, _ in pairs]), "brier_base": _mean3([x for _, x in pairs]), "slots": slots}


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


def _next_update_at(now: datetime) -> str | None:
    """下一个整点更新检查点 (今天还没到的; 否则下一个交易日 09:25)。"""
    today = now.date()
    for s in update_slots(today):
        at = datetime.combine(today, s, tzinfo=us_clock.tz)
        if at > now:
            return at.isoformat(timespec="minutes")
    try:
        nxt = us_clock.next_trading_day(today)
    except ValueError:
        return None
    slots = update_slots(nxt)
    return datetime.combine(nxt, slots[0], tzinfo=us_clock.tz).isoformat(timespec="minutes") if slots else None


def _update_missing(j: dict | None, ups: list[dict], now: datetime) -> str | None:
    """当日某个检查点过了 30 分钟仍无快照, 且晚于最新一次快照 → 该时点 (页面标 "未更新"); 否则 None。"""
    today = now.date()
    if not j or j.get("day") != today.isoformat():
        return None
    due = [s.strftime("%H:%M") for s in update_slots(today)
           if datetime.combine(today, s, tzinfo=us_clock.tz) + UPDATE_MISS_AFTER <= now]
    if not due:
        return None
    last = ups[-1]["slot"] if ups else ""
    return due[-1] if due[-1] not in {u["slot"] for u in ups} and due[-1] > last else None


def view(data_dir, *, now: datetime | None = None) -> dict:
    """机会页读: 最新判断 (08:35 原判) + 当日整点快照 (latest = 最新一次) + 是否今日 + 是否最新 + 昨日结果
    + 近 20 日命中与打分。原有字段口径不变 (情报页、历史日回看、复盘照旧读 08:35 原判)。"""
    now = us_clock.market_datetime(now or us_clock.now())
    today = now.date()
    j = latest_judgment(data_dir)
    scores = load_scores(data_dir)
    # 此刻"应有"的最新判断日: 交易日 08:35 后是今天, 否则是上一个交易日
    is_td = us_clock.is_trading_day(today)
    expected = today if is_td and now.time() >= GENERATE_AT else us_clock.previous_trading_day(today)
    out: dict = {"now": now.isoformat(timespec="minutes"), "expected_day": expected.isoformat(),
                 "next_at": None, "judgment": None, "is_today": False, "is_latest": False,
                 "stale_items": [], "missing_items": [], "last_score": None, "stats": weekly_stats(data_dir),
                 "latest": None, "updates": [], "next_update_at": _next_update_at(now), "update_missing": None}
    nxt = today if us_clock.is_trading_day(today) and now.time() < GENERATE_AT else us_clock.next_trading_day(today)
    out["next_at"] = datetime.combine(nxt, GENERATE_AT, tzinfo=us_clock.tz).isoformat(timespec="minutes")
    if j:
        out["judgment"] = j
        out["is_today"] = j["day"] == today.isoformat()
        out["is_latest"] = j["day"] == expected.isoformat()
        out["stale_items"] = [i["label"] for i in j.get("items", []) if i.get("status") == "stale"]
        out["missing_items"] = [i["label"] for i in j.get("items", []) if i.get("status") == "missing"]
        out["score"] = scores.get(j["day"])
        try:
            ups = load_updates(data_dir, date.fromisoformat(str(j["day"])))
        except ValueError:
            ups = []
        if ups:
            out["latest"] = ups[-1]
            out["updates"] = [{k: u.get(k) for k in ("slot", "generated_at", "probs", "main", "stance", "stars")}
                              for u in ups]
        out["update_missing"] = _update_missing(j, ups, now)
    prev = sorted((r for r in scores.values() if not j or r["day"] != j["day"]), key=lambda r: r["day"])
    if prev:
        out["last_score"] = prev[-1]
    return out
