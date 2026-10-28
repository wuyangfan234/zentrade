# ruff: noqa: RUF001
"""美股财务定时同步 (第 2 批): SEC EDGAR 主源 + Finnhub 备源 → data/financials/*。

- 每晚盘后跑一轮; 优先补「从没同步过」的标的, 其次最久未刷新的, 每轮上限 MAX_PER_RUN
  (companyfacts 每只 2-5MB, 全市场一次拉完太重, 轮转约一周覆盖全池)。
- 合并写: 本轮没刷新到的标的保留旧行; 本轮全部失败 → 不写任何文件, 抛 SyncFailedError
  让盯盘台账记 error 并告警, 不影响其他任务。
- Finnhub 备源只补 EDGAR 拿不到的标的 (无 CIK/解析失败, 如外国发行人)。Finnhub 没有申报日:
  指标按期末 + 45 天 (季) / 90 天 (年) 保守生效; 股本取当天快照, 生效日 = 同步日。
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import polars as pl

from app.services import edgar_fundamentals as ef
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

JOB_ID = "zen_fundamentals_sync"
MAX_PER_RUN = 600
STATE_FILE = "_sync_state.json"
STATUS_FILE = "_last_run.json"
FINNHUB = "https://finnhub.io/api/v1"
FINNHUB_INTERVAL = 1.1  # 免费档 60 次/分
_FH_METRIC = {"roe": "roe", "grossMargin": "gross_margin", "netMargin": "net_margin",
              "totalDebtToTotalAsset": "debt_to_asset_ratio", "bookValue": None}


class SyncFailedError(RuntimeError):
    """本轮一只都没同步成功: 旧数据原样保留, 由调度监听器记 error 并告警。"""


def pick_batch(symbols: Iterable[str], state: dict[str, str], *, limit: int = MAX_PER_RUN) -> list[str]:
    syms = list(dict.fromkeys(symbols))
    never = [s for s in syms if s not in state]
    seen = sorted((s for s in syms if s in state), key=lambda s: state[s])
    return (never + seen)[:limit]


def _finnhub_rows(sym: str, metric: dict, profile: dict, today: str) -> dict[str, pl.DataFrame]:
    by_period: dict[tuple[str, str], dict] = {}
    for freq, pt, lag in (("annual", "FY", 90), ("quarterly", "Q", 45)):
        series = ((metric.get("series") or {}).get(freq)) or {}
        for key, col in _FH_METRIC.items():
            if col is None:
                continue
            for pnt in series.get(key) or []:
                end, v = pnt.get("period"), pnt.get("v")
                if not end or v is None:
                    continue
                rec = by_period.setdefault((end, pt), {
                    "symbol": sym, "period_end": end, "period_type": pt,
                    "announce_date": (date.fromisoformat(end) + timedelta(days=lag)).isoformat()})
                rec[col] = float(v)
    rows = [r for r in by_period.values() if r["announce_date"] <= today]
    mcols = ["roe", "gross_margin", "net_margin", "revenue_yoy", "net_income_yoy", "debt_to_asset_ratio", "bps"]
    out = {"metrics": ef._frame([{**{c: None for c in mcols}, **r} for r in rows], mcols)}
    out["shares"] = finnhub_share_rows(sym, profile, today)
    return out


def finnhub_share_period(today: str) -> tuple[str, str]:
    """Finnhub 股本无申报日: 视为「期末 +45 天 ≤ 今天」的最近季末那一期, 生效日 = 期末 +45 天。"""
    d = date.fromisoformat(today)
    y, q = d.year, (d.month - 1) // 3  # 本季之前的季末
    while True:
        if q == 0:
            y, q = y - 1, 4
        end = date(y, q * 3, 31 if q in (1, 4) else 30)
        ann = end + timedelta(days=45)
        if ann <= d:
            return end.isoformat(), ann.isoformat()
        q -= 1


def finnhub_share_rows(sym: str, profile: dict, today: str) -> pl.DataFrame:
    so = profile.get("shareOutstanding")  # 百万股, 合计口径 (多股类也是合计)
    if not so:
        return ef._shares_frame([])
    end, ann = finnhub_share_period(today)
    return ef._shares_frame([{"symbol": sym, "period_end": end, "period_type": "I", "announce_date": ann,
                              "total_shares": float(so) * 1e6, "float_shares": float(so) * 1e6,
                              "multi_class": ef.is_multi_class(sym, []), "basis": "approx_total"}])


def load_watchlist(data_dir: Path) -> set[str]:
    """ZenTrade 自选 (data/user_data/watchlist.parquet, 与 app.services.watchlist 同源)。"""
    path = Path(data_dir) / "user_data" / "watchlist.parquet"
    try:
        return {str(x).upper() for x in pl.read_parquet(path, columns=["symbol"])["symbol"].drop_nulls()}
    except Exception:
        return set()


def _merge_write(data_dir: Path, parsed: dict[str, list[pl.DataFrame]], refreshed: set[str]) -> dict[str, int]:
    counts = {}
    for name in ef.TABLES:
        new = [f for f in parsed.get(name, []) if not f.is_empty()]
        path = Path(data_dir) / "financials" / name / "part.parquet"
        old = pl.read_parquet(path) if path.exists() else None
        if not new:
            continue
        frames = [pl.concat(new, how="diagonal_relaxed")]
        if old is not None and not old.is_empty():
            frames.append(old.filter(~pl.col("symbol").is_in(list(refreshed))))
        df = pl.concat(frames, how="diagonal_relaxed").sort(["symbol", "period_end"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        df.write_parquet(tmp)
        os.replace(tmp, path)
        counts[name] = df.height
    return counts


def run(data_dir: Path, symbols: Iterable[str], *, http_get: Callable | None = None,
        finnhub_token: str | None = None, sleep: Callable[[float], None] = time.sleep,
        today: str | None = None, limit: int = MAX_PER_RUN, watchlist: set[str] | None = None,
        multi_class: set[str] | None = None) -> dict:
    """watchlist: 只有自选票在 EDGAR 缺合计股本 (多股类/外国发行人) 时才用 Finnhub 股本兜底 (用户 2026-10-12);
    非自选保持置空。multi_class: 额外强制判为多股类的代码 (运维/测试用)。"""
    watchlist = {s.upper() for s in (watchlist or set())}
    multi_class = {s.upper() for s in (multi_class or set())}
    data_dir = Path(data_dir)
    today = today or date.today().isoformat()
    get = http_get or ef._default_get
    state_path = data_dir / "financials" / STATE_FILE
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    batch = pick_batch(symbols, state, limit=limit)
    headers = {"User-Agent": ef.user_agent(), "Accept-Encoding": "identity"}

    parsed: dict[str, list[pl.DataFrame]] = {}
    refreshed: set[str] = set()
    edgar_ok, fallback, errors = 0, [], []
    has_shares: set[str] = set()
    ciks: dict[str, int] = {}

    def add(sym: str, tables: dict[str, pl.DataFrame], source: str) -> None:
        for k, f in tables.items():
            parsed.setdefault(k, []).append(f.with_columns(pl.lit(source).alias("source")))
        refreshed.add(sym)

    try:
        cik_map = {str(v["ticker"]).upper(): int(v["cik_str"])
                   for v in json.loads(get(ef.TICKERS_URL, headers)).values()}
    except Exception as exc:
        errors.append(f"tickers:{type(exc).__name__}")
        cik_map = None
    for sym in batch:
        cik = cik_map.get(ef.sec_ticker(sym)) if cik_map is not None else None
        if cik is None:
            fallback.append(sym)
            continue
        sleep(ef.MIN_INTERVAL)
        try:
            tables = ef.parse_companyfacts(sym, json.loads(get(ef.FACTS_URL.format(cik=cik), headers)))
            if sym.upper() in multi_class:
                tables["shares"] = tables["shares"].with_columns(
                    pl.lit(None, pl.Float64).alias("total_shares"), pl.lit(None, pl.Float64).alias("float_shares"),
                    pl.lit(True).alias("multi_class"))
                tables["metrics"] = tables["metrics"].with_columns(pl.lit(None, pl.Float64).alias("bps"))
            add(sym, tables, "sec_edgar")
            ciks[sym] = cik
            if tables["shares"]["float_shares"].drop_nulls().len() > 0:
                has_shares.add(sym)
            edgar_ok += 1
        except Exception as exc:
            logger.warning("EDGAR %s 失败: %s", sym, type(exc).__name__)
            fallback.append(sym)

    # 自选里缺合计股本的票: 先取 EDGAR 定期报告 XBRL 实例里本股类的股本 (GOOG 口径修正)
    need_shares = [x for x in batch if x.upper() in watchlist and x not in has_shares]
    class_ok: list[str] = []
    for sym in need_shares:
        cik = ciks.get(sym) or (cik_map.get(ef.sec_ticker(sym)) if cik_map else None)
        if cik is None:
            continue
        try:
            sleep(ef.MIN_INTERVAL)
            filing = ef.latest_periodic_filing(json.loads(get(ef.SUBMISSIONS_URL.format(cik=cik), headers)))
            if filing is None:
                continue
            sleep(ef.MIN_INTERVAL)
            xml = get(ef.ARCHIVE_URL.format(cik=cik, acc=filing["acc"], doc=filing["doc"]), headers)
            val, letter = ef.class_shares_from_instance(xml, sym)
            if val is None:
                continue
            parsed.setdefault("shares", []).append(ef._shares_frame([{
                "symbol": sym, "period_end": filing["period_end"], "period_type": "I",
                "announce_date": filing["filed"], "total_shares": val, "float_shares": val,
                "multi_class": True, "basis": f"class:{letter}"}]).with_columns(pl.lit("sec_edgar_class").alias("source")))
            refreshed.add(sym)
            has_shares.add(sym)
            class_ok.append(sym)
        except Exception as exc:
            logger.warning("EDGAR 分类股本 %s 失败: %s", sym, type(exc).__name__)
    need_shares = [x for x in need_shares if x not in has_shares]

    finnhub_ok, finnhub_shares = 0, []
    if finnhub_token:
        for sym in list(dict.fromkeys(fallback + need_shares)):
            try:
                tables: dict[str, pl.DataFrame] = {}
                if sym in fallback:
                    q = urlencode({"symbol": sym, "metric": "all", "token": finnhub_token})
                    sleep(FINNHUB_INTERVAL)
                    tables = _finnhub_rows(sym, json.loads(get(f"{FINNHUB}/stock/metric?{q}", {})), {}, today)
                if sym in need_shares:
                    sleep(FINNHUB_INTERVAL)
                    profile = json.loads(get(f"{FINNHUB}/stock/profile2?{urlencode({'symbol': sym, 'token': finnhub_token})}", {}))
                    sh = finnhub_share_rows(sym, profile, today)
                    tables["shares"] = sh
                    if not sh.is_empty():
                        finnhub_shares.append(sym)
                for k, f in tables.items():
                    parsed.setdefault(k, []).append(f.with_columns(pl.lit("finnhub").alias("source")))
                refreshed.add(sym)
                finnhub_ok += 1
            except Exception as exc:  # 不记录 URL (含 token)
                logger.warning("Finnhub %s 失败: %s", sym, type(exc).__name__)

    if batch and not refreshed:
        raise SyncFailedError(f"财务同步本轮 0/{len(batch)} 成功 {errors}; 旧数据保留")
    counts = _merge_write(data_dir, parsed, refreshed)
    state.update({s: today for s in refreshed})
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(state_path, json.dumps(state, ensure_ascii=False, sort_keys=True))
    res = {"batch": len(batch), "edgar_ok": edgar_ok, "finnhub_ok": finnhub_ok, "finnhub_shares": finnhub_shares, "class_shares": class_ok,
           "failed": sorted(set(batch) - refreshed), "rows": counts, "errors": errors}
    logger.info("fundamentals sync: %s", {k: v for k, v in res.items() if k != "failed"})
    return res


def stock_universe(data_dir: Path) -> list[str]:
    path = Path(data_dir) / "instruments" / "instruments.parquet"
    if not path.exists():
        return []
    df = pl.read_parquet(path)
    if "asset_type" in df.columns:
        df = df.filter(pl.col("asset_type").is_null() | (pl.col("asset_type") == "stock"))
    return sorted(df["symbol"].drop_nulls().cast(pl.Utf8).to_list())


def write_status(data_dir: Path, status: dict) -> None:
    p = Path(data_dir) / "financials" / STATUS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(status, ensure_ascii=False, default=str))


def read_status(data_dir: Path) -> dict | None:
    try:
        return json.loads((Path(data_dir) / "financials" / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status_component(data_dir: Path) -> tuple[str, str]:
    """系统状态页部件: (state, summary)。整轮失败 → error; 成功 → ok; 从没跑过 → idle。"""
    st = read_status(data_dir)
    if not st:
        return "idle", "等待首次同步（交易日 18:30 ET）"
    at = str(st.get("at") or "")[:16].replace("T", " ")
    if not st.get("ok"):
        return "error", f"{at} ET 同步失败，旧数据保留：{str(st.get('error') or '')[:120]}"
    summ = (f"{at} ET 同步 {st.get('batch', 0)} 只：EDGAR {st.get('edgar_ok', 0)}，"
            f"分类股本 {len(st.get('class_shares') or [])}，Finnhub 股本 {len(st.get('finnhub_shares') or [])}")
    if st.get("failed"):
        summ += f"；失败 {len(st['failed'])}"
    return "ok", summ


def scheduled(data_dir: Path) -> dict:
    from app.market_clock import us_clock
    at = us_clock.now().isoformat(timespec="seconds")
    try:
        res = _scheduled_run(data_dir)
    except Exception as exc:
        write_status(data_dir, {"ok": False, "at": at, "error": str(exc)[:200]})
        raise
    write_status(data_dir, {"ok": True, "at": at, **res})
    return res


def _scheduled_run(data_dir: Path) -> dict:
    from app.services.earnings_calendar import _configured_finnhub_token
    return run(data_dir, stock_universe(data_dir), finnhub_token=_configured_finnhub_token(),
               watchlist=load_watchlist(data_dir))
