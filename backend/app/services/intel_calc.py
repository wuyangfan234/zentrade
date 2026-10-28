"""盘面情报面板的后台计算 (0.40.0): 全市场宽度、自选财报 (历史反应 / 一致预期 / 隐含波动)。

只在后台任务里跑 (intel_job), 结果落 data/intel/*.json; 页面请求只读这些文件。
口径:
- 宽度股票池 = 维表里的普通股与 ADR, 最近收盘 ≥ $3、近 20 日平均成交额 ≥ $2M (剔除仙股与僵尸股)。
  用日K库落盘的官方日K (复权收盘算均线, 未复权收盘算成交额)。盘中近似值另算 (见 intel_panel.live_breadth)。
- 历史财报反应: 盘前公布 = 当天收盘对前一日收盘; 盘后 / 未注明 = 下一交易日收盘对公布日收盘 (未注明按盘后,
  与证据 v3 的财报事件同一口径)。只取已有日K覆盖的那几次。
- 隐含波动: 财报日后第一个到期日的平值跨式 / 现价 (options_analytics.earnings_move), 只读当天免费期权链缓存。
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

from app.market_clock import us_clock
from app.services import intel_sources as src

logger = logging.getLogger(__name__)

BREADTH_DAYS = 260
MIN_PRICE = 3.0
MIN_DOLLAR_VOL = 2_000_000
SERIES_DAYS = 20
HIST_REPORTS = 8
UPCOMING_TRADING_DAYS = 10


# ── 全市场宽度 ─────────────────────────────────────────────────────

def _eligible_symbols(data_dir: Path) -> set[str] | None:
    import polars as pl
    p = Path(data_dir) / "instruments" / "instruments.parquet"
    if not p.exists():
        return None
    df = pl.read_parquet(p, columns=["symbol", "instrument_type"])
    return set(df.filter(pl.col("instrument_type").is_in(["common_stock", "adr"]))["symbol"].to_list())


def official_parts(data_dir: Path, today: date | None = None) -> list[Path]:
    """已定版的日K分区。盘中实时行情会把当天快照写进当天分区 (merge_live_enriched_asset), 不能当收盘用:
    今天及以后的分区只认官方收盘已同步 (quote_official_close.json) 的; 更早的分区若落在该清单覆盖期内却不在清单里
    (当天盘后定版失败), 也排除。"""
    today = today or us_clock.today()
    off = src.read_json(Path(data_dir) / "quote_official_close.json")
    official = {str(d) for d in off} if isinstance(off, list) else set()
    floor = min(official) if official else None
    out = []
    for f in sorted((Path(data_dir) / "kline_daily_enriched").glob("date=*/part.parquet")):
        d = f.parent.name.split("=", 1)[1]
        if d in official or (d < today.isoformat() and (floor is None or d < floor)):
            out.append(f)
    return out


def compute_breadth(data_dir: Path, *, days: int = BREADTH_DAYS, today: date | None = None) -> dict | None:
    import polars as pl
    parts = official_parts(data_dir, today)[-days:]
    if len(parts) < 30:
        return None
    cols = ["symbol", "date", "close", "high", "low", "volume", "raw_close"]
    frames = []
    for f in parts:
        try:
            d = pl.read_parquet(f)
        except Exception as e:  # 单个分区坏了跳过
            logger.warning("intel breadth: unreadable %s: %s", f.parent.name, type(e).__name__)
            continue
        frames.append(d.select([c for c in cols if c in d.columns]))
    df = pl.concat(frames, how="diagonal_relaxed")
    if "raw_close" not in df.columns:
        df = df.with_columns(pl.col("close").alias("raw_close"))
    elig = _eligible_symbols(data_dir)
    if elig:
        df = df.filter(pl.col("symbol").is_in(list(elig)))
    df = df.filter(pl.col("close") > 0).sort(["symbol", "date"])
    by = "symbol"
    df = df.with_columns(
        prev=pl.col("close").shift(1).over(by),
        ma20=pl.col("close").rolling_mean(20).over(by),
        ma50=pl.col("close").rolling_mean(50).over(by),
        ma200=pl.col("close").rolling_mean(200).over(by),
        hh=pl.col("high").rolling_max(251).shift(1).over(by),
        ll=pl.col("low").rolling_min(251).shift(1).over(by),
        dv20=(pl.col("raw_close") * pl.col("volume")).rolling_mean(20).over(by),
        n=pl.col("close").cum_count().over(by),
    )
    days_all = sorted(df["date"].unique().to_list())
    last_days = days_all[-SERIES_DAYS:]
    sub = df.filter(pl.col("date").is_in(last_days) & (pl.col("raw_close") >= MIN_PRICE)
                    & (pl.col("dv20") >= MIN_DOLLAR_VOL) & pl.col("prev").is_not_null())
    agg = sub.group_by("date").agg(
        universe=pl.len(),
        adv=(pl.col("close") > pl.col("prev")).sum(),
        dec=(pl.col("close") < pl.col("prev")).sum(),
        a20=(pl.col("close") > pl.col("ma20")).sum(), n20=pl.col("ma20").is_not_null().sum(),
        a50=(pl.col("close") > pl.col("ma50")).sum(), n50=pl.col("ma50").is_not_null().sum(),
        a200=(pl.col("close") > pl.col("ma200")).sum(), n200=pl.col("ma200").is_not_null().sum(),
        nh=((pl.col("high") >= pl.col("hh")) & (pl.col("n") >= 200)).sum(),
        nl=((pl.col("low") <= pl.col("ll")) & (pl.col("n") >= 200)).sum(),
    ).sort("date")

    def pct(a, b):
        return round(a / b * 100, 1) if b else None

    series = [{"day": str(r["date"])[:10], "universe": r["universe"], "adv": r["adv"], "dec": r["dec"],
               "up_pct": pct(r["adv"], r["universe"]), "above_ma20_pct": pct(r["a20"], r["n20"]),
               "above_ma50_pct": pct(r["a50"], r["n50"]), "above_ma200_pct": pct(r["a200"], r["n200"]),
               "new_highs": r["nh"], "new_lows": r["nl"]} for r in agg.to_dicts()]
    if not series:
        return None
    last = series[-1]
    d_last = last_days[-1]
    base = sub.filter(pl.col("date") == d_last).select("symbol", "raw_close")
    return {**last, "as_of": last["day"], "series": series, "computed_at": us_clock.now().isoformat(timespec="seconds"),
            "rule": f"普通股与 ADR, 收盘 ≥ ${MIN_PRICE:g}, 20 日均成交额 ≥ ${MIN_DOLLAR_VOL / 1e6:g}M",
            "last_close": {s: round(float(c), 4) for s, c in zip(base["symbol"].to_list(), base["raw_close"].to_list(), strict=True)}}


def latest_partition_day(data_dir: Path, today: date | None = None) -> str | None:
    parts = official_parts(data_dir, today)
    return parts[-1].parent.name.split("=", 1)[1] if parts else None


# ── 自选财报: 历史反应 ─────────────────────────────────────────────

def _money(x) -> float | None:
    try:
        return float(str(x).replace("$", "").replace(",", "").replace("(", "-").replace(")", ""))
    except (TypeError, ValueError):
        return None


def earnings_history_rows(events_dir: Path, symbols: set[str], *, before: date) -> dict[str, list[dict]]:
    """证据 v3 财报日历库 (Nasdaq 按日落盘, 2021-12 起) 里每只票的历次财报 (新 → 旧)。"""
    out: dict[str, list[dict]] = {}
    for p in sorted(Path(events_dir).glob("20*.json")):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d >= before:
            continue
        try:
            js = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for r in js.get("rows") or []:
            sym = str(r.get("symbol") or "").upper().replace("/", ".")
            if sym not in symbols:
                continue
            timing = {"time-pre-market": "bmo", "time-after-hours": "amc"}.get(r.get("time"), "unknown")
            out.setdefault(sym, []).append({"date": d.isoformat(), "timing": timing, "eps": _money(r.get("eps")),
                                            "forecast": _money(r.get("epsForecast")), "surprise_pct": _money(r.get("surprise"))})
    for sym in out:
        out[sym].sort(key=lambda x: x["date"], reverse=True)
    return out


GAP_MIN = 0.02          # 推断时段: 两个开盘跳空里大的那个至少 2%
GAP_RATIO = 2.0         # 且至少是另一个的 2 倍 (2026 年 521 次已知时段回测: 准确率 96.7%)


def infer_timing(d: date, bars: dict[date, tuple[float, float]], fallback: str | None) -> tuple[str, str]:
    """Nasdaq 历史库 99% 的行是 time-not-supplied。按开盘跳空推断: 报告日开盘对前收跳得大 = 盘前公布;
    次日开盘对报告日收盘跳得大 = 盘后公布。跳空不分明时沿用这只票下一次财报的时段 (公司时段基本固定),
    都没有才记 unknown (按盘后算)。返回 (时段, 依据)。"""
    days = sorted(bars)
    if d in bars:
        i = days.index(d)
        if 0 < i < len(days) - 1:
            pc = bars[days[i - 1]][1]
            o0, c0 = bars[d]
            o1 = bars[days[i + 1]][0]
            if pc and c0 and o0 and o1:
                g0, g1 = abs(o0 / pc - 1), abs(o1 / c0 - 1)
                big, small = max(g0, g1), min(g0, g1)
                if big >= GAP_MIN and big >= GAP_RATIO * max(small, 1e-9):
                    return ("bmo" if g0 > g1 else "amc"), "gap"
    if fallback in ("bmo", "amc"):
        return fallback, "next"
    return "unknown", "none"


def reaction_moves(rows: list[dict], closes: dict[date, float], opens: dict[date, float] | None = None,
                   next_timing: str | None = None) -> list[dict]:
    """给每次财报补上反应日涨跌 (%)。日K缺失的那次不给 move。
    源里没给时段的行先推断 (infer_timing); 推断不出才按盘后。"""
    days = sorted(closes)
    bars = {d: (opens[d], c) for d, c in closes.items() if opens and opens.get(d)} if opens else {}
    out = []
    for r in rows:
        d = date.fromisoformat(r["date"])
        r = dict(r)
        if r["timing"] not in ("bmo", "amc"):
            r["timing"], r["timing_basis"] = infer_timing(d, bars, next_timing)
        else:
            r["timing_basis"] = "source"
        if r["timing"] == "bmo":
            react = d if d in closes else None
            base = max((x for x in days if x < d), default=None)
        else:
            base = d if d in closes else None
            react = min((x for x in days if x > d), default=None)
        mv = None
        if react and base and closes.get(base):
            mv = round((closes[react] / closes[base] - 1) * 100, 2)
        out.append({**r, "react_day": react.isoformat() if react else None, "move_pct": mv})
    return out


def summarize_history(rows: list[dict]) -> dict:
    moves = [abs(r["move_pct"]) for r in rows if isinstance(r.get("move_pct"), (int, float))]
    beats = [r for r in rows if isinstance(r.get("surprise_pct"), (int, float))]
    return {"n": len(moves), "median_abs_move": round(statistics.median(moves), 2) if moves else None,
            "max_abs_move": round(max(moves), 2) if moves else None,
            "beat_n": sum(1 for r in beats if r["surprise_pct"] > 0), "surprise_n": len(beats),
            "last": rows[0] if rows else None, "reports": rows[:HIST_REPORTS]}


def compute_earnings_history(data_dir: Path, repo, symbols: list[str], *, today: date,
                             next_timing: dict[str, str] | None = None) -> dict:
    from app.services import research_data
    syms = {s.upper() for s in symbols}
    next_timing = next_timing or {}
    raw = earnings_history_rows(research_data.events_dir(data_dir) / "earnings", syms, before=today)
    raw = {s: r[:HIST_REPORTS] for s, r in raw.items()}
    start = min((date.fromisoformat(r[-1]["date"]) for r in raw.values() if r), default=today) - timedelta(days=10)
    closes: dict[str, dict[date, float]] = {}
    opens: dict[str, dict[date, float]] = {}
    if raw and repo is not None:
        b = repo.get_daily_batch(sorted(raw), start, today, columns=["symbol", "date", "open", "close"])
        if b is not None and not b.is_empty():
            for g in b.partition_by("symbol"):
                sym = str(g["symbol"][0])
                ds = [(d if isinstance(d, date) else date.fromisoformat(str(d)[:10])) for d in g["date"].to_list()]
                closes[sym] = {d: float(c) for d, c in zip(ds, g["close"].to_list(), strict=True) if c}
                if "open" in g.columns:
                    opens[sym] = {d: float(o) for d, o in zip(ds, g["open"].to_list(), strict=True) if o}
    out = {s: summarize_history(reaction_moves(r, closes.get(s, {}), opens.get(s), next_timing.get(s)))
           for s, r in raw.items()}
    return {"computed_at": us_clock.now().isoformat(timespec="seconds"), "symbols": out,
            "source": "Nasdaq 财报日历库 + 日K"}


# ── 未来财报: 一致预期 (Nasdaq) ────────────────────────────────────

def fetch_upcoming(today: date, *, http_get=None, trading_days: int = UPCOMING_TRADING_DAYS, previous: dict | None = None) -> dict:
    from app.services import earnings_calendar as ec
    http_get = http_get or ec._http_json
    days = us_clock.trading_days_between(today, today + timedelta(days=trading_days * 2))[:trading_days]
    rows: dict[str, dict] = {}
    failed = []
    for d in days:
        try:
            body = http_get(ec.NASDAQ_URL + d.isoformat(), headers=ec._NASDAQ_HEADERS)
        except Exception as e:
            logger.info("intel: nasdaq earnings %s unavailable: %s", d, type(e).__name__)
            failed.append(d.isoformat())
            continue
        for r in ((body or {}).get("data") or {}).get("rows") or []:
            sym = str(r.get("symbol") or "").strip().upper()
            if not sym or sym in rows:
                continue
            rows[sym] = {"date": d.isoformat(), "timing": ec._NASDAQ_TIMING.get(r.get("time"), "unknown"),
                         "name": r.get("name"), "eps_forecast": _money(r.get("epsForecast")),
                         "n_est": _money(r.get("noOfEsts")), "last_year_eps": _money(r.get("lastYearEPS")),
                         "fiscal_quarter": r.get("fiscalQuarterEnding"), "market_cap": _money(r.get("marketCap"))}
    if not rows and failed:
        raise ValueError("nasdaq earnings unavailable")
    if failed and previous:                  # 个别日子没拉到: 沿用上一份里那几天的行, 不让表变残缺
        for sym, r in ((previous or {}).get("rows") or {}).items():
            if r.get("date") in failed and sym not in rows:
                rows[sym] = r
    return {"fetched_at": us_clock.now().isoformat(timespec="seconds"), "days": [d.isoformat() for d in days],
            "failed_days": failed, "rows": rows, "source": "Nasdaq 财报日历"}


# ── 隐含波动 (免费期权链) ─────────────────────────────────────────

def implied_move(data_dir: Path, sym: str, spot: float | None, earnings_day: date, today: date) -> dict | None:
    from app.services import alpaca_options, options_analytics
    p = alpaca_options._dir(data_dir, sym) / f"{today.isoformat()}.json"
    try:
        free = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        free = None
    if not (isinstance(free, dict) and free.get("available") and free.get("rows")):
        return None
    chain_spot = parity_spot(free["rows"], earnings_day, today)
    spot = chain_spot or spot             # 与期权报价同一时刻的现价优先; 推不出才用行情快照
    if not spot:
        return None
    a = options_analytics.analyze(free["rows"], spot, today=today, earnings_date=earnings_day)
    em = a.get("earnings_move") if a.get("ok") else None
    if not em or em.get("expected_move_pct") is None:
        return None
    return {"expected_move_pct": round(em["expected_move_pct"] * 100, 2), "atm_iv": em.get("atm_iv"),
            "expiration": em.get("expiration"), "spot": round(spot, 2), "spot_basis": "parity" if chain_spot else "quote",
            "as_of": free.get("fetched_at"), "source": "Alpaca 期权链 (免费)"}


def parity_spot(rows: list[dict], earnings_day: date, today: date) -> float | None:
    """财报后首个到期日上, 看涨看跌中间价差最小的行权价 K: 现价 ≈ K + C - P (到期 ≤ 2 周, 忽略利息与分红)。
    买卖价差 > 20% 的合约不用。"""
    from app.services import options_analytics
    cs = [c for c in options_analytics.normalize(rows) if c["exp"] >= earnings_day.isoformat()]
    if not cs:
        return None
    exp = min(c["exp"] for c in cs)
    if (date.fromisoformat(exp) - today).days > 21:
        return None

    def mid(c):
        b, a = c.get("bid"), c.get("ask")
        if b is None or a is None or b <= 0 or a < b or (a - b) / ((a + b) / 2) > 0.2:
            return None
        return (a + b) / 2
    calls = {c["strike"]: mid(c) for c in cs if c["exp"] == exp and c["type"] == "call"}
    puts = {c["strike"]: mid(c) for c in cs if c["exp"] == exp and c["type"] == "put"}
    pairs = [(k, calls[k], puts[k]) for k in calls if k in puts and calls[k] is not None and puts[k] is not None]
    if not pairs:
        return None
    k, c, p = min(pairs, key=lambda x: abs(x[1] - x[2]))
    s = k + c - p
    return s if s > 0 else None


def upcoming_for(upcoming: dict | None, calendar: dict | None, symbols: list[str], today: date) -> dict[str, dict]:
    """自选/持仓里未来 10 个交易日内的财报: Nasdaq 一致预期为主, 财报日历兜底 (只有日期)。"""
    rows = (upcoming or {}).get("rows") or {}
    events = (calendar or {}).get("events") or {}
    horizon = us_clock.trading_days_between(today, today + timedelta(days=UPCOMING_TRADING_DAYS * 2))[:UPCOMING_TRADING_DAYS]
    last = horizon[-1] if horizon else today
    out = {}
    for s in symbols:
        r = rows.get(s)
        if not r and events.get(s):
            r = {"date": events[s].get("date"), "timing": events[s].get("timing")}
        if not r or not r.get("date"):
            continue
        try:
            d = date.fromisoformat(str(r["date"])[:10])
        except ValueError:
            continue
        if today <= d <= last:
            out[s] = {**r, "date": d.isoformat()}
    return out


def fetch_finnhub_upcoming(symbols: list[str], today: date, token: str | None, *, http_get=None,
                           pause: float = 2.0) -> dict[str, dict]:
    """自选财报的第二来源 (Finnhub, 免费档 60 次/分钟, 逐票查, 只查窗口内的自选)。失败的票不给。"""
    import time as _t
    from urllib.parse import urlencode

    from app.services import earnings_calendar as ec
    if not token or not symbols:
        return {}
    http_get = http_get or ec._http_json
    end = today + timedelta(days=UPCOMING_TRADING_DAYS * 2)
    out: dict[str, dict] = {}
    for i, sym in enumerate(symbols):
        if i and pause:
            _t.sleep(pause)
        try:
            body = http_get(f"{ec.FINNHUB_URL}?{urlencode({'from': today.isoformat(), 'to': end.isoformat(), 'symbol': sym, 'token': token})}")
        except Exception as e:
            logger.info("intel: finnhub earnings %s unavailable: %s", sym, type(e).__name__)
            continue
        rows = sorted((body or {}).get("earningsCalendar") or [], key=lambda r: str(r.get("date")))
        r = next((x for x in rows if str(x.get("symbol") or "").upper() == sym and x.get("date")), None)
        if r:
            eps = r.get("epsEstimate")
            out[sym] = {"date": str(r["date"])[:10], "timing": {"bmo": "bmo", "amc": "amc"}.get(r.get("hour") or "", "unknown"),
                        "eps": round(float(eps), 4) if isinstance(eps, (int, float)) else None}
    return out


def eps_consensus(row: dict, qveris: dict | None, earnings_day: date) -> dict:
    """页面显示用的 EPS 预期: 华尔街调整后口径优先 (QVeris 同一财季 > Finnhub), Nasdaq 只在两者都没有时用 (部分公司是 GAAP 口径)。"""
    if qveris and qveris.get("eps_avg") is not None and qveris.get("period"):
        try:
            gap = (earnings_day - date.fromisoformat(str(qveris["period"])[:10])).days
        except ValueError:
            gap = -1
        if 0 <= gap <= 120:
            return {"eps": qveris["eps_avg"], "source": "QVeris"}
    fh = row.get("finnhub") or {}
    if fh.get("eps") is not None:
        return {"eps": fh["eps"], "source": "Finnhub"}
    if row.get("eps_forecast") is not None:
        return {"eps": row["eps_forecast"], "source": "Nasdaq"}
    return {"eps": None, "source": None}


def read_qveris_estimate(data_dir: Path, sym: str) -> dict | None:
    from app.services import qveris_data
    p = Path(data_dir) / "qveris" / "cache" / "earnings_estimates" / f"{sym.upper()}.json"
    entry = src.read_json(p)
    if not isinstance(entry, dict):
        return None
    try:
        nxt = qveris_data._norm_estimates(entry.get("data")).get("next")
    except Exception:
        return None
    if not nxt:
        return None
    return {"eps_avg": nxt.get("eps_avg"), "eps_change_30d_pct": nxt.get("eps_change_30d_pct"),
            "rev_avg": nxt.get("rev_avg"), "period": nxt.get("date"), "fetched_at": entry.get("fetched_at"),
            "eps_up_30d": nxt.get("eps_up_30d"), "eps_down_30d": nxt.get("eps_down_30d")}


def opex_day(year: int, month: int) -> date:
    """当月第三个星期五 (月度期权到期日); 遇休市提前一天 (近似)。"""
    d = date(year, month, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d if us_clock.is_trading_day(d) else d - timedelta(days=1)


def _parse_iso(s) -> datetime | None:
    try:
        return us_clock.market_datetime(str(s)) if s else None
    except ValueError:
        return None
