"""盘面情报面板的外部数据源 (0.40.0)。只在后台任务里出网, 页面请求只读这里落的缓存。

- 温度带: CBOE 延迟报价 (VIX / VIX3M / VIX9D, 约 15 分钟延迟)、Alpaca IEX 快照 (板块与跨资产 ETF)、
  美债 10Y + 高收益利差 (复用 market_mood.fetch_rates)。
- 宏观日历: ForexFactory 本周美元事件 (预期 / 前值); 公布后用 FRED 官方序列算「实际」。
  只在 FRED 最新一期正好是这次公布对应的那一期时才给实际值, 对不上就显示「实际待同步」(不猜)。
- 全市场期权 Put/Call: CBOE 每日统计 (收盘后发布, 只有日度)。
每个源失败只记 state 里的 error, 不覆盖已有缓存。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.config import PUBLIC_HTTP_USER_AGENT
from app.market_clock import us_clock
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

_UA = {"User-Agent": PUBLIC_HTTP_USER_AGENT}
_LOCK = threading.Lock()

SECTOR_ETFS = ("XLK", "SMH", "XLC", "XLY", "XLF", "XLI", "XLE", "XLV", "XLP", "XLU", "XLB", "XLRE")
SECTOR_NAMES = {"XLK": "科技", "SMH": "半导体", "XLC": "通信", "XLY": "可选消费", "XLF": "金融", "XLI": "工业",
                "XLE": "能源", "XLV": "医疗", "XLP": "必需消费", "XLU": "公用事业", "XLB": "材料", "XLRE": "地产"}
CROSS_ETFS = ("TLT", "HYG", "UUP", "GLD", "USO", "IBIT")
CROSS_NAMES = {"TLT": "长债", "HYG": "高收益债", "UUP": "美元", "GLD": "黄金", "USO": "原油", "IBIT": "比特币"}
INDEX_ETFS = ("SPY", "QQQ", "DIA", "IWM")
VOL_SYMBOLS = {"vix": "_VIX", "vix3m": "_VIX3M", "vix9d": "_VIX9D"}


# ── 存储 ─────────────────────────────────────────────────────────────

def root(data_dir) -> Path:
    p = Path(data_dir) / "intel"
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        return None


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=1))


def read_cache(data_dir, name: str):
    """只读 (不建目录): 页面请求用。"""
    return read_json(Path(data_dir) / "intel" / f"{name}.json")


def mark_state(data_dir, key: str, *, ok: bool, error: str | None = None, now: datetime | None = None) -> None:
    now = now or us_clock.now()
    with _LOCK:
        p = root(data_dir) / "state.json"
        st = read_json(p) or {}
        cur = st.get(key) or {}
        cur["last_attempt_at"] = now.isoformat(timespec="seconds")
        if ok:
            cur["last_ok_at"] = cur["last_attempt_at"]
            cur["error"] = None
        else:
            cur["error"] = (error or "error")[:160]
        st[key] = cur
        write_json(p, st)


def _get(url: str, timeout: float = 20.0) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read()


# ── 温度带: 波动率 (CBOE 延迟报价) ─────────────────────────────────────

def parse_cboe_quote(body: dict) -> dict | None:
    d = (body or {}).get("data") or {}
    px = d.get("current_price")
    if not isinstance(px, (int, float)) or px <= 0:
        return None
    prev = d.get("prev_day_close")
    return {"value": float(px),
            "prev_close": float(prev) if isinstance(prev, (int, float)) and prev > 0 else None,
            "chg_pct": round(float(d["price_change_percent"]), 2) if isinstance(d.get("price_change_percent"), (int, float)) else None,
            "as_of": d.get("last_trade_time") or (body or {}).get("timestamp")}


VIX_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
VIX_CLOSES_KEEP = 30


def parse_cboe_history(text: str, keep: int = VIX_CLOSES_KEEP) -> dict[str, float]:
    """CBOE 日线历史 CSV (DATE=MM/DD/YYYY, ..., CLOSE) → 最近 keep 天 {YYYY-MM-DD: 收盘}。"""
    out: dict[str, float] = {}
    for line in (text or "").strip().splitlines()[-keep:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            m, d, y = (int(x) for x in parts[0].split("/"))
            out[date(y, m, d).isoformat()] = float(parts[4])
        except ValueError:
            continue
    return out


def official_prev_close(closes: dict[str, float], quote_day: date) -> float | None:
    """报价所在交易日的上一交易日官方收盘; 缺那一天就返回 None (不拿更早的凑)。"""
    v = (closes or {}).get(us_clock.previous_trading_day(quote_day).isoformat())
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def vix_base(value: float, quote_day: date, closes: dict[str, float]) -> float | None:
    """VIX 涨跌的基准收盘。报价日的官方收盘已发布且现值 ≠ 它 = 夜盘 (GTH, 20:15 ET 起) 在动, 对报价日收盘;
    否则 (盘中 / 收盘后到夜盘前现值就是收盘) 对报价日的上一交易日收盘。"""
    close_q = (closes or {}).get(quote_day.isoformat())
    if isinstance(close_q, (int, float)) and close_q > 0 and abs(value - close_q) >= 0.005:
        return float(close_q)
    return official_prev_close(closes, quote_day)


VIX_HISTORY_RETRY_S = 1800


def fetch_vol(http_get=None, *, closes: dict[str, float] | None = None, history_at: str | None = None,
              now: datetime | None = None, http_text=None) -> dict:
    """VIX 涨跌对 CBOE 官方日收盘 (0.42.4 起): 延迟报价里的 price_change_percent 收盘后口径会漂
    (10-27 收 17.52 / 前收 18.39 = -4.73%, 报价给 -4.97%; prev_day_close 收盘后又滚成当天收盘)。
    官方收盘缓存在 vix_closes; 缺需要的那天才拉历史 CSV (~470KB), 30 分钟内最多拉一次。拉不到沿用报价口径。"""
    http_get = http_get or (lambda u: json.loads(_get(u)))
    http_text = http_text or (lambda u: _get(u, timeout=30).decode("utf-8", "replace"))
    now = now or us_clock.now()
    out: dict = {}
    for key, sym in VOL_SYMBOLS.items():
        try:
            q = parse_cboe_quote(http_get(f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/{sym}.json"))
        except Exception as e:  # 单项失败只丢这一项
            logger.info("intel: cboe %s unavailable: %s", sym, type(e).__name__)
            q = None
        if q:
            out[key] = q
    if "vix" not in out:
        raise ValueError("no VIX quote")
    closes = dict(closes or {})
    vix = out["vix"]
    qday = _et_date_naive(vix.get("as_of"))
    out["vix_history_at"] = history_at
    if qday is not None:
        have_prev = official_prev_close(closes, qday) is not None
        have_q = qday.isoformat() in closes
        last = None
        try:
            last = datetime.fromisoformat(history_at) if history_at else None
        except ValueError:
            last = None
        recent = last is not None and (now - last).total_seconds() < VIX_HISTORY_RETRY_S
        # 缺上一交易日: 必须拉; 缺报价日收盘 (收盘后 CBOE 才发布): 节流拉, 用来判夜盘
        if not have_prev or (not have_q and not recent):
            try:
                closes.update(parse_cboe_history(http_text(VIX_HISTORY_URL)))
            except Exception as e:
                logger.info("intel: cboe VIX history unavailable: %s", type(e).__name__)
            out["vix_history_at"] = now.isoformat(timespec="seconds")
        base = vix_base(vix["value"], qday, closes)
        if base is not None:
            vix["prev_close"] = base
            vix["chg_pct"] = round((vix["value"] / base - 1) * 100, 2)
            vix["chg_basis"] = "官方前收"
    out["vix_closes"] = dict(sorted(closes.items())[-VIX_CLOSES_KEEP:])
    if out.get("vix3m"):
        out["ratio"] = round(out["vix"]["value"] / out["vix3m"]["value"], 3)
    out["source"] = "CBOE 延迟报价 (约 15 分钟)"
    return out


def _et_date_naive(s: str | None) -> date | None:
    """CBOE last_trade_time 是美东墙钟 naive 串 ('2026-10-27T16:15:01')。"""
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


# ── 温度带: ETF 快照 (Alpaca IEX) ───────────────────────────────────────

def _et_date(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(us_clock.tz).date()
    except ValueError:
        return None


def _session_of(t: datetime) -> str:
    """成交时刻 (美东) 属于哪一段: pre 盘前 / regular 盘中 / post 盘后。"""
    hm = t.time()
    if hm < time(9, 30):
        return "pre"
    return "post" if hm >= us_clock.session_close(t.date()) else "regular"


def _et_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(us_clock.tz)
    except ValueError:
        return None


def parse_snapshot(snap: dict, today: date) -> dict | None:
    """IEX 快照 → {price, chg_pct, as_of, day, live, session, time_et}。
    今天已有日线 (dailyBar 是今天): 现价对前一日收盘;
    今天还没有日线但已有成交 (盘前 04:00 起): 盘前最新成交对最近一日收盘 (0.42.4 起, 原来整段盘前都显示昨收);
    否则 (夜间 / 休市) 给最近一个完整交易日的涨跌, day 标那一天。"""
    if not isinstance(snap, dict):
        return None
    daily, prev = snap.get("dailyBar") or {}, snap.get("prevDailyBar") or {}
    trade = snap.get("latestTrade") or {}
    d_day = _et_date(daily.get("t"))
    t_at = _et_dt(trade.get("t"))
    t_px = trade.get("p") if isinstance(trade.get("p"), (int, float)) else None
    live = False
    if d_day == today:
        px = t_px if t_px is not None else daily.get("c")
        pc = prev.get("c")
        as_of = trade.get("t") or daily.get("t")
        live = True
    elif t_px is not None and t_at is not None and t_at.date() == today and (d_day is None or d_day < today):
        px, pc, as_of, live = t_px, daily.get("c"), trade.get("t"), True
    else:
        px, pc = daily.get("c"), prev.get("c")
        as_of = daily.get("t")
    if not isinstance(px, (int, float)) or not isinstance(pc, (int, float)) or pc <= 0 or px <= 0:
        return None
    out = {"price": round(float(px), 4), "chg_pct": round((px / pc - 1) * 100, 2), "as_of": as_of,
           "day": today.isoformat() if live else (d_day.isoformat() if d_day else None), "live": live}
    if live and t_at is not None and t_at.date() == today:
        out["session"] = _session_of(t_at)
        out["time_et"] = t_at.strftime("%H:%M")
    return out


def fetch_etfs(provider, now: datetime) -> dict:
    syms = [*INDEX_ETFS, *SECTOR_ETFS, *CROSS_ETFS]
    snaps = provider._get_snapshots(syms, "iex")
    rows = {s: r for s in syms if (r := parse_snapshot(snaps.get(s), now.date()))}
    if not rows:
        raise ValueError("no ETF snapshots")
    return {"rows": rows, "source": "Alpaca IEX 单所报价", "fetched_at": now.isoformat(timespec="seconds")}


def fetch_etf_daily(provider, now: datetime) -> dict[str, dict]:
    """全部温度带 ETF 的 SIP 官方日K (不含今天): 最近收盘 / 前一日收盘 / 5 日涨跌。每天一次。
    盘中涨跌 = IEX 现价对这里的官方收盘 (IEX 自己的 prevDailyBar 是单所最后一笔, 与官方收盘有出入)。"""
    import polars as pl
    end = us_clock.previous_trading_day(now.date()) if now.time() < time(16, 20) else now.date()
    start = datetime.combine(end - timedelta(days=14), time(0), tzinfo=us_clock.tz)
    syms = list(INDEX_ETFS + SECTOR_ETFS + CROSS_ETFS)
    df = provider.get_daily(syms, start, datetime.combine(end, time(23, 59), tzinfo=us_clock.tz))
    out: dict[str, dict] = {}
    if df is None or df.is_empty():
        return out
    for sym in syms:
        g = df.filter(pl.col("symbol") == sym).sort("date")
        c = g["close"].to_list()
        if len(c) < 2 or not c[-2]:
            continue
        out[sym] = {"close": float(c[-1]), "prev_close": float(c[-2]), "day": str(g["date"][-1])[:10],
                    "chg_pct": round((c[-1] / c[-2] - 1) * 100, 2),
                    "d5": round((c[-1] / c[-6] - 1) * 100, 2) if len(c) >= 6 and c[-6] else None}
    return out


def combine_quote(snap_row: dict | None, daily: dict | None, today: date) -> dict | None:
    """温度带一格: 今天有 IEX 成交 → 现价对官方前收; 否则用官方日K的最近一日涨跌 (标那一天)。"""
    if daily and snap_row and snap_row.get("live") and daily.get("day") and daily["day"] < today.isoformat():
        px = snap_row["price"]
        return {"price": px, "chg_pct": round((px / daily["close"] - 1) * 100, 2), "as_of": snap_row.get("as_of"),
                "day": today.isoformat(), "live": True, "base": "官方前收", "d5": daily.get("d5"),
                "session": snap_row.get("session"), "time_et": snap_row.get("time_et")}
    if daily:
        return {"price": daily["close"], "chg_pct": daily["chg_pct"], "as_of": daily["day"], "day": daily["day"],
                "live": False, "base": "官方收盘", "d5": daily.get("d5")}
    if snap_row:
        return {**snap_row, "base": "IEX 前收", "d5": None}
    return None


# ── 全市场期权 Put/Call (CBOE 日度) ────────────────────────────────────

PCR_KEYS = {"TOTAL PUT/CALL RATIO": "total", "EQUITY PUT/CALL RATIO": "equity", "INDEX PUT/CALL RATIO": "index",
            "SPX + SPXW PUT/CALL RATIO": "spx", "EXCHANGE TRADED PRODUCTS PUT/CALL RATIO": "etp"}


def parse_pcr(body: dict) -> dict | None:
    out = {}
    for r in (body or {}).get("ratios") or []:
        k = PCR_KEYS.get(str(r.get("name") or "").upper())
        try:
            v = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        if k and v > 0:
            out[k] = v
    return out if out.get("total") or out.get("equity") else None


def fetch_pcr_day(d: date, http_get=None) -> dict | None:
    """该交易日的 CBOE PCR; 未发布 (403/404) 返回 None。"""
    http_get = http_get or (lambda u: json.loads(_get(u)))
    try:
        body = http_get(f"https://cdn.cboe.com/data/us/options/market_statistics/daily/{d.isoformat()}_daily_options")
    except Exception as e:
        logger.info("intel: cboe pcr %s unavailable: %s", d, type(e).__name__)
        return None
    return parse_pcr(body)


def update_pcr(data_dir, now: datetime, *, days: int = 25, http_get=None) -> dict:
    """补齐近 days 个交易日 (缺的才拉, 今天收盘前不拉)。返回整份缓存。"""
    p = root(data_dir) / "pcr.json"
    cache = read_json(p) or {"days": {}}
    have = cache.setdefault("days", {})
    end = now.date() if now.time() >= time(16, 20) else us_clock.previous_trading_day(now.date())
    want = us_clock.trading_days_between(end - timedelta(days=days * 2), end)[-days:]
    miss = cache.setdefault("miss", {})
    changed = False
    for d in want:
        k = d.isoformat()
        if k in have:
            continue
        last = miss.get(k)                    # 未发布 / 休市: 2 小时内不重试
        if last and now - datetime.fromisoformat(last) < timedelta(hours=2):
            continue
        r = fetch_pcr_day(d, http_get)
        if r:
            have[k] = r
            miss.pop(k, None)
        else:
            miss[k] = now.isoformat(timespec="seconds")
        changed = True
    for k in sorted(have)[:-60]:
        have.pop(k, None)
    for k in [x for x in miss if x < (end - timedelta(days=days * 2)).isoformat()]:
        miss.pop(k, None)
    if changed or not p.exists():
        cache["updated_at"] = now.isoformat(timespec="seconds")
        write_json(p, cache)
    return cache


def pcr_view(cache: dict | None) -> dict | None:
    days = sorted(((cache or {}).get("days") or {}).items())
    if not days:
        return None
    last_day, last = days[-1]
    hist = [v.get("equity") for _, v in days[-21:-1] if isinstance(v.get("equity"), (int, float))]
    avg = round(sum(hist) / len(hist), 2) if hist else None
    return {"day": last_day, **last, "equity_avg20": avg, "n_hist": len(hist),
            "series": [{"day": d, "equity": v.get("equity"), "total": v.get("total")} for d, v in days[-20:]],
            "source": "CBOE 每日期权统计 (收盘后)"}


# ── 宏观日历 (ForexFactory) ───────────────────────────────────────────

def fetch_ff_week(http_get=None) -> list[dict]:
    http_get = http_get or (lambda u: json.loads(_get(u)))
    data = http_get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    out = []
    for e in data or []:
        if e.get("country") != "USD" or e.get("impact") not in ("High", "Medium", "Low"):
            continue
        try:
            at = datetime.fromisoformat(str(e.get("date"))).astimezone(us_clock.tz)
        except ValueError:
            continue
        out.append({"at": at.isoformat(timespec="minutes"), "title": str(e.get("title") or ""),
                    "impact": e["impact"].lower(), "forecast": e.get("forecast") or None, "previous": e.get("previous") or None})
    out.sort(key=lambda x: x["at"])
    return out


def event_key(ev: dict) -> str:
    return f"{ev['at'][:16]}|{ev['title']}"


# FF 标题 → FRED 序列。kind: pct = 环比 %, yoy = 同比 %, level = 原值, diff_k = 差值 (千人 → K),
# k = 原值/1000 → K (ICSA 是人数), m_from_k = 千 → M, k_raw = 千 → K, b_from_m = 百万 → B。
# freq: M 月 / Q 季 / W 周 / D 日; lag: 公布对应的是公布月往前第几期 (月 / 季)。
# 修订类公布 (GDP 二修 / 终值、密歇根终值) 与当期首发同一期, 无法从 FRED 判断是哪一版, 一律不映射。
FRED_MAP: dict[str, dict] = {
    "CPI m/m": {"series": "CPIAUCSL", "kind": "pct", "freq": "M", "lag": 1},
    "Core CPI m/m": {"series": "CPILFESL", "kind": "pct", "freq": "M", "lag": 1},
    "CPI y/y": {"series": "CPIAUCNS", "kind": "yoy", "freq": "M", "lag": 1},
    "Non-Farm Employment Change": {"series": "PAYEMS", "kind": "diff_k", "freq": "M", "lag": 1},
    "Unemployment Rate": {"series": "UNRATE", "kind": "level", "freq": "M", "lag": 1, "unit": "%", "dp": 1},
    "Average Hourly Earnings m/m": {"series": "CES0500000003", "kind": "pct", "freq": "M", "lag": 1},
    "Core PCE Price Index m/m": {"series": "PCEPILFE", "kind": "pct", "freq": "M", "lag": 1},
    "Core PCE Price Index y/y": {"series": "PCEPILFE", "kind": "yoy", "freq": "M", "lag": 1},
    "PCE Price Index m/m": {"series": "PCEPI", "kind": "pct", "freq": "M", "lag": 1},
    "PCE Price Index y/y": {"series": "PCEPI", "kind": "yoy", "freq": "M", "lag": 1},
    "Personal Income m/m": {"series": "PI", "kind": "pct", "freq": "M", "lag": 1},
    "Personal Spending m/m": {"series": "PCE", "kind": "pct", "freq": "M", "lag": 1},
    "Retail Sales m/m": {"series": "RSAFS", "kind": "pct", "freq": "M", "lag": 1},
    "Core Retail Sales m/m": {"series": "RSFSXMV", "kind": "pct", "freq": "M", "lag": 1},
    "PPI m/m": {"series": "PPIFIS", "kind": "pct", "freq": "M", "lag": 1},
    "Core PPI m/m": {"series": "PPIFES", "kind": "pct", "freq": "M", "lag": 1},
    "Durable Goods Orders m/m": {"series": "DGORDER", "kind": "pct", "freq": "M", "lag": 1},
    "Core Durable Goods Orders m/m": {"series": "ADXTNO", "kind": "pct", "freq": "M", "lag": 1},
    "Industrial Production m/m": {"series": "INDPRO", "kind": "pct", "freq": "M", "lag": 1},
    "Building Permits": {"series": "PERMIT", "kind": "m_from_k", "freq": "M", "lag": 1},
    "Housing Starts": {"series": "HOUST", "kind": "m_from_k", "freq": "M", "lag": 1},
    "New Home Sales": {"series": "HSN1F", "kind": "k_raw", "freq": "M", "lag": 1},
    "JOLTS Job Openings": {"series": "JTSJOL", "kind": "m_from_k", "freq": "M", "lag": 2},
    "Trade Balance": {"series": "BOPGSTB", "kind": "b_from_m", "freq": "M", "lag": 2},
    "Advance GDP q/q": {"series": "A191RL1Q225SBEA", "kind": "level", "freq": "Q", "lag": 1, "unit": "%", "dp": 1},
    "Advance GDP Price Index q/q": {"series": "A191RI1Q225SBEA", "kind": "level", "freq": "Q", "lag": 1, "unit": "%", "dp": 1},
    "Employment Cost Index q/q": {"series": "ECIALLCIV", "kind": "pct", "freq": "Q", "lag": 1},
    "Unemployment Claims": {"series": "ICSA", "kind": "k", "freq": "W"},
    "Federal Funds Rate": {"series": "DFEDTARU", "kind": "level", "freq": "D", "unit": "%", "dp": 2},
}

TITLE_ZH = {
    "CPI m/m": "CPI 环比", "Core CPI m/m": "核心 CPI 环比", "CPI y/y": "CPI 同比",
    "Non-Farm Employment Change": "非农新增就业", "Unemployment Rate": "失业率",
    "Average Hourly Earnings m/m": "平均时薪环比", "Core PCE Price Index m/m": "核心 PCE 环比",
    "Core PCE Price Index y/y": "核心 PCE 同比", "PCE Price Index m/m": "PCE 环比", "PCE Price Index y/y": "PCE 同比",
    "Personal Income m/m": "个人收入环比", "Personal Spending m/m": "个人支出环比", "Retail Sales m/m": "零售销售环比",
    "Core Retail Sales m/m": "核心零售销售环比", "PPI m/m": "PPI 环比", "Core PPI m/m": "核心 PPI 环比",
    "Durable Goods Orders m/m": "耐用品订单环比", "Core Durable Goods Orders m/m": "核心耐用品订单环比",
    "Industrial Production m/m": "工业产出环比", "Building Permits": "营建许可", "Housing Starts": "新屋开工",
    "New Home Sales": "新屋销售", "JOLTS Job Openings": "JOLTS 职位空缺", "Trade Balance": "贸易差额",
    "Advance GDP q/q": "GDP 季环比年化 (初值)", "Advance GDP Price Index q/q": "GDP 价格指数 (初值)",
    "Employment Cost Index q/q": "雇佣成本指数季环比", "Unemployment Claims": "初请失业金人数",
    "Federal Funds Rate": "联邦基金利率", "FOMC Statement": "FOMC 声明", "FOMC Press Conference": "FOMC 新闻发布会",
    "FOMC Meeting Minutes": "FOMC 会议纪要", "CB Consumer Confidence": "谘商会消费者信心",
    "ISM Manufacturing PMI": "ISM 制造业 PMI", "ISM Services PMI": "ISM 服务业 PMI",
    "Flash Manufacturing PMI": "制造业 PMI (初值)", "Flash Services PMI": "服务业 PMI (初值)",
    "Prelim UoM Consumer Sentiment": "密歇根消费者信心 (初值)", "Revised UoM Consumer Sentiment": "密歇根消费者信心 (终值)",
    "Prelim UoM Inflation Expectations": "密歇根通胀预期 (初值)", "ADP Non-Farm Employment Change": "ADP 就业",
    "Crude Oil Inventories": "EIA 原油库存", "Empire State Manufacturing Index": "纽约联储制造业指数",
    "Philly Fed Manufacturing Index": "费城联储制造业指数", "Prelim GDP q/q": "GDP 季环比年化 (修正值)",
    "Final GDP q/q": "GDP 季环比年化 (终值)", "Pending Home Sales m/m": "成屋签约销售环比",
    "Existing Home Sales": "成屋销售", "Chicago PMI": "芝加哥 PMI", "Goods Trade Balance": "商品贸易差额 (初值)",
}


def _month_start(d: date, back: int) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def ref_ok(spec: dict, obs: date, released: date) -> bool:
    """FRED 最新一期是否正好是这次公布对应的那一期。"""
    f = spec["freq"]
    if f == "M":
        return obs == _month_start(released, spec.get("lag", 1))
    if f == "Q":
        q_start = date(released.year, (released.month - 1) // 3 * 3 + 1, 1)
        return obs == _month_start(q_start, 3 * spec.get("lag", 1))
    if f == "W":
        return released - timedelta(days=7) < obs < released
    if f == "D":
        return obs >= released
    return False


def parse_fred_csv(text: str) -> list[tuple[date, float]]:
    out = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) == 2 and re.match(r"\d{4}-\d{2}-\d{2}$", r[0]) and re.match(r"-?[\d.]+$", r[1]):
            out.append((date.fromisoformat(r[0]), float(r[1])))
    return out


def fetch_fred(series: str, since: date, http_get=None) -> list[tuple[date, float]]:
    http_get = http_get or (lambda u: _get(u, timeout=25).decode())
    return parse_fred_csv(http_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={since.isoformat()}"))


def compute_actual(spec: dict, obs: list[tuple[date, float]]) -> tuple[float, str] | None:
    """(数值, 展示串)。单位与 FF 预期一致 (环比 % 一位小数; K / M / B)。"""
    if not obs:
        return None
    k = spec["kind"]
    v = obs[-1][1]
    if k == "pct":
        if len(obs) < 2 or not obs[-2][1]:
            return None
        x = round((v / obs[-2][1] - 1) * 100, 1)
        return x, f"{x:.1f}%"
    if k == "yoy":
        ref = obs[-1][0]
        prior = next((val for d, val in obs if d.year == ref.year - 1 and d.month == ref.month), None)
        if not prior:
            return None
        x = round((v / prior - 1) * 100, 1)
        return x, f"{x:.1f}%"
    if k == "diff_k":
        if len(obs) < 2:
            return None
        x = round(v - obs[-2][1])
        return x, f"{x:.0f}K"
    if k == "k":
        x = round(v / 1000)
        return x, f"{x:.0f}K"
    if k == "k_raw":
        x = round(v)
        return x, f"{x:.0f}K"
    if k == "m_from_k":
        x = round(v / 1000, 2)
        return x, f"{x:.2f}M"
    if k == "b_from_m":
        x = round(v / 1000, 1)
        return x, f"{x:.1f}B"
    if k == "level":
        dp = spec.get("dp", 1)
        x = round(v, dp)
        return x, f"{x:.{dp}f}{spec.get('unit', '')}"
    return None


_NUM = re.compile(r"^\s*(-?[\d.]+)\s*([%KMBT]?)\s*$", re.I)
_SCALE = {"": 1.0, "%": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def parse_ff_number(s) -> float | None:
    """'0.3%' / '198K' / '1.40M' / '-130.8B' → 统一到个位 (百分数保持百分数)。"""
    m = _NUM.match(str(s or ""))
    if not m:
        return None
    return float(m.group(1)) * _SCALE[m.group(2).upper()]


DERIVED_KINDS = {"pct", "yoy", "diff_k"}          # 由 FRED 水平值推算 (官方百分比用未取整数据算, 可能差 0.1)


def _tolerance(spec: dict, ref: float) -> float:
    """「前值对拍」允许的差: 百分数 0.15 个点; 人数差值 15K; 其余按 2% 相对误差。"""
    k = spec["kind"]
    if k in ("pct", "yoy") or spec.get("unit") == "%":
        return 0.15
    if k == "diff_k":
        return 15_000
    return max(abs(ref) * 0.02, 1e-9)


def prev_matches(spec: dict, obs: list[tuple[date, float]], previous: str | None) -> bool | None:
    """用同一套算法算上一期, 与 ForexFactory 的「前值」(公布时已含修订) 对拍。
    None = 没有前值可对; False = 口径对不上 (这一项不给实际值, 宁缺不错)。"""
    ref = parse_ff_number(previous)
    if ref is None or len(obs) < 2:
        return None
    mine = compute_actual(spec, obs[:-1])
    if not mine:
        return None
    val = parse_ff_number(mine[1])
    if val is None:
        return None
    return abs(val - ref) <= _tolerance(spec, ref)


def surprise(actual_text: str, forecast: str | None, *, derived: bool = False) -> str | None:
    """实际 vs 预期: above / below / inline; 推算值与预期相差不超过 0.1 个点记 near (推算误差内)。只给方向, 不判好坏。"""
    a, f = parse_ff_number(actual_text), parse_ff_number(forecast)
    if a is None or f is None:
        return None
    if abs(a - f) < 1e-9 * max(1.0, abs(f)):
        return "inline"
    if derived and str(actual_text).strip().endswith("%") and abs(a - f) <= 0.1 + 1e-9:
        return "near"
    return "above" if a > f else "below"


def update_actuals(data_dir, events: list[dict], now: datetime, *, fetch=None, max_series: int = 8) -> dict:
    """已过公布时刻 (且在 4 天内) 的可映射事件, 用 FRED 求实际值; 已拿到的不再请求。"""
    fetch = fetch or fetch_fred
    p = root(data_dir) / "macro_actuals.json"
    cache = read_json(p) or {}
    pending: dict = cache.setdefault("_pending", {})
    todo = []
    for ev in events:
        spec = FRED_MAP.get(ev["title"])
        at = datetime.fromisoformat(ev["at"])
        k = event_key(ev)
        if not spec or k in cache or at > now or now - at > timedelta(days=4):
            continue
        # 公布后 6 小时内每 5 分钟试一次, 之后每小时一次 (FRED 一般几分钟内上新; 对不上期数的别一直打)
        last = pending.get(k)
        gap = timedelta(minutes=5) if now - at < timedelta(hours=6) else timedelta(hours=1)
        if last and now - datetime.fromisoformat(last) < gap:
            continue
        todo.append((ev, spec, at, k))
    series_cache: dict[str, list] = {}
    changed = False
    for _ev, spec, at, k in todo:
        s = spec["series"]
        if s not in series_cache:
            if len(series_cache) >= max_series:
                break
            try:
                series_cache[s] = fetch(s, at.date() - timedelta(days=500))
            except Exception as e:
                logger.info("intel: fred %s unavailable: %s", s, type(e).__name__)
                series_cache[s] = []
        pending[k] = now.isoformat(timespec="seconds")
        changed = True
        obs = series_cache[s]
        if not obs or not ref_ok(spec, obs[-1][0], at.date()):
            continue
        res = compute_actual(spec, obs)
        if not res:
            continue
        check = prev_matches(spec, obs, _ev.get("previous"))
        if check is False:                    # 上一期都对不上: 口径不一致, 不给实际值, 也不再查
            cache[k] = {"status": "mismatch", "series": s, "obs": obs[-1][0].isoformat(),
                        "fetched_at": now.isoformat(timespec="seconds")}
            pending.pop(k, None)
            continue
        cache[k] = {"actual": res[1], "value": res[0], "series": s, "obs": obs[-1][0].isoformat(),
                    "derived": spec["kind"] in DERIVED_KINDS, "prev_checked": check is True,
                    "fetched_at": now.isoformat(timespec="seconds"), "source": f"FRED {s}"}
        pending.pop(k, None)
    cutoff = (now - timedelta(days=10)).isoformat()
    for key in [x for x in cache if x != "_pending" and x[:16] < cutoff[:16]]:
        cache.pop(key, None)
    for key in [x for x in pending if x[:16] < cutoff[:16]]:
        pending.pop(key, None)
    if changed:
        write_json(p, cache)
    return cache
