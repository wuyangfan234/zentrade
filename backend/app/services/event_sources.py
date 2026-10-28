"""事件来源 (2026-10-05 预登记, design/evidence-v3-preregistration.md 第 9 节)。

E1 earnings_beat_drift  财报超预期后漂移 (做多)
E2 insider_cluster_buy  内部人集中买入 (做多)

两个函数都只依赖“事件发生时已经公开”的信息, 返回 {信号日: [(symbol, 排序分), ...]} (同日按分数降序)。
信号日 = 能在其收盘后看到事件的那个交易日; 交易规则与策略来源完全相同 (次日起入场窗)。
"""
from __future__ import annotations

import io
import json
import zipfile
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import polars as pl

EVENT_SOURCES = {
    "earnings_beat_drift": "财报超预期后漂移",
    "insider_cluster_buy": "内部人集中买入",
}
EVENT_STOP_LOSS = -0.06

E1_MIN_SURPRISE = 5.0
E1_MIN_ABS_FORECAST = 0.05
E1_MIN_ESTIMATES = 2
E1_MIN_REACTION = 0.02
E2_MIN_VALUE = 25_000.0
E2_WINDOW_DAYS = 30
E2_MIN_INSIDERS = 2


def _money(x) -> float | None:
    if x is None:
        return None
    s = str(x).replace("$", "").replace(",", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _next_day(days: list[date], d: date, *, inclusive: bool) -> date | None:
    i = bisect_left(days, d) if inclusive else bisect_right(days, d)
    return days[i] if i < len(days) else None


def earnings_rows(earn_dir: Path) -> list[dict]:
    rows = []
    for p in sorted(Path(earn_dir).glob("20*.json")):   # 只认 YYYY-MM-DD.json (排除 macOS ._ 元数据等杂文件)
        js = json.loads(p.read_text())
        d = date.fromisoformat(js["date"])
        for r in js.get("rows") or []:
            rows.append({"date": d, "symbol": (r.get("symbol") or "").upper().replace("/", "."), "time": r.get("time"),
                         "eps": _money(r.get("eps")), "forecast": _money(r.get("epsForecast")),
                         "surprise": _money(r.get("surprise")), "n_est": _money(r.get("noOfEsts"))})
    return rows


def earnings_beat_signals(earn_dir: Path, days: list[date], close_by_symbol: dict[str, dict[date, float]]) -> dict[date, list[tuple[str, float]]]:
    """E1: 超预期 ≥5%、|预期| ≥0.05、家数 ≥2、反应日涨 ≥2% → 信号日 = 反应日。
    反应日: 盘前公布 = 当天; 盘后/未知 = 下一个交易日 (未知按盘后, 保证不用公布前信息)。"""
    out: dict[date, list[tuple[str, float]]] = defaultdict(list)
    for r in earnings_rows(earn_dir):
        if r["surprise"] is None or r["forecast"] is None or r["eps"] is None:
            continue
        if r["surprise"] < E1_MIN_SURPRISE or abs(r["forecast"]) < E1_MIN_ABS_FORECAST or (r["n_est"] or 0) < E1_MIN_ESTIMATES:
            continue
        react = _next_day(days, r["date"], inclusive=(r["time"] == "time-pre-market"))
        if react is None:
            continue
        closes = close_by_symbol.get(r["symbol"])
        if not closes or react not in closes:
            continue
        i = bisect_left(days, react)
        prev = next((closes[days[j]] for j in range(i - 1, max(i - 6, -1), -1) if days[j] in closes), None)
        if not prev or prev <= 0:
            continue
        if closes[react] / prev - 1 < E1_MIN_REACTION:
            continue
        out[react].append((r["symbol"], float(r["surprise"])))
    for d in out:
        out[d].sort(key=lambda x: -x[1])
    return dict(out)


def _read_tsv(z: zipfile.ZipFile, name: str, cols: list[str]) -> pl.DataFrame:
    df = pl.read_csv(io.BytesIO(z.read(name)), separator="\t", quote_char=None, infer_schema_length=0,
                     truncate_ragged_lines=True)
    return df.select([c for c in cols if c in df.columns])


def insider_purchases(sec_dir: Path) -> pl.DataFrame:
    """全部季度 Form 4 公开市场买入 (P/A), 董事或高管 (不含仅 10% 大股东), 单笔 ≥ $25,000。"""
    frames = []
    for zp in sorted(Path(sec_dir).glob("20*_form345.zip")):
        z = zipfile.ZipFile(zp)
        sub = _read_tsv(z, "SUBMISSION.tsv", ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK",
                                               "ISSUERTRADINGSYMBOL"])
        own = _read_tsv(z, "REPORTINGOWNER.tsv", ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNER_RELATIONSHIP"])
        tr = _read_tsv(z, "NONDERIV_TRANS.tsv", ["ACCESSION_NUMBER", "TRANS_CODE", "TRANS_ACQUIRED_DISP_CD",
                                                  "TRANS_SHARES", "TRANS_PRICEPERSHARE"])
        tr = tr.filter((pl.col("TRANS_CODE") == "P") & (pl.col("TRANS_ACQUIRED_DISP_CD") == "A"))
        tr = tr.with_columns((pl.col("TRANS_SHARES").cast(pl.Float64, strict=False)
                              * pl.col("TRANS_PRICEPERSHARE").cast(pl.Float64, strict=False)).alias("value"))
        tr = tr.group_by("ACCESSION_NUMBER").agg(pl.col("value").sum())
        own = own.filter(pl.col("RPTOWNER_RELATIONSHIP").str.contains("Director|Officer").fill_null(False))
        own = own.group_by("ACCESSION_NUMBER").agg(pl.col("RPTOWNERCIK").first())
        sub = sub.filter(pl.col("DOCUMENT_TYPE").is_in(["4", "4/A"]))
        df = sub.join(own, on="ACCESSION_NUMBER").join(tr, on="ACCESSION_NUMBER")
        frames.append(df)
    df = pl.concat(frames, how="vertical_relaxed")
    df = df.with_columns(pl.col("FILING_DATE").str.strptime(pl.Date, "%d-%b-%Y", strict=False).alias("filed"),
                         pl.col("ISSUERTRADINGSYMBOL").str.to_uppercase().str.strip_chars().alias("symbol"))
    df = df.filter(pl.col("value") >= E2_MIN_VALUE).filter(pl.col("filed").is_not_null() & pl.col("symbol").is_not_null())
    return df.select(["symbol", "ISSUERCIK", "RPTOWNERCIK", "filed", "value"]).unique()


def insider_cluster_signals(sec_dir: Path, days: list[date]) -> dict[date, list[tuple[str, float]]]:
    """E2: 同一公司 30 天内 ≥2 个不同内部人买入; 凑够第二人的申报日 = 触发日; 信号日 = 触发日之后第一个交易日;
    同一公司 30 天内只触发一次; 排序 = 30 天内买入总金额。"""
    df = insider_purchases(sec_dir).sort("filed")
    out: dict[date, list[tuple[str, float]]] = defaultdict(list)
    for (cik,), g in df.group_by(["ISSUERCIK"]):
        rows = g.sort("filed").to_dicts()
        last_trigger: date | None = None
        for i, r in enumerate(rows):
            d = r["filed"]
            if last_trigger is not None and (d - last_trigger).days < E2_WINDOW_DAYS:
                continue
            win = [x for x in rows[: i + 1] if (d - x["filed"]).days < E2_WINDOW_DAYS]
            if len({x["RPTOWNERCIK"] for x in win}) >= E2_MIN_INSIDERS:
                sig = _next_day(days, d, inclusive=False)
                if sig is None:
                    continue
                out[sig].append((r["symbol"], float(sum(x["value"] for x in win))))
                last_trigger = d
    for d in out:
        out[d].sort(key=lambda x: -x[1])
    return dict(out)


def sec_coverage_end(sec_dir: Path) -> date | None:
    """已下载季度数据覆盖到的最后一天 (之后的申报还没有发布)。"""
    qs = []
    for zp in Path(sec_dir).glob("20*_form345.zip"):
        y, q = zp.name[:4], zp.name[5]
        qs.append((int(y), int(q)))
    if not qs:
        return None
    y, q = max(qs)
    return date(y, 3 * q, 1) + timedelta(days=32) - timedelta(days=(date(y, 3 * q, 1) + timedelta(days=32)).day)
