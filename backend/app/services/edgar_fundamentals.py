"""SEC EDGAR companyfacts → data/financials/{income,balance_sheet,cash_flow,metrics,shares}/part.parquet。

2026-10-12 用户批准「EDGAR 主 + Finnhub 备」重建美股财务 (第 1 批: 同步与落盘, 不动交易逻辑)。

点时 (point-in-time) 口径:
- 每个 (科目, 期间) 只取 **首次申报** 的值 (最早 filed), 后续重述不回写历史, 避免前视;
- announce_date = 该期首次申报日 (10-Q/10-K 的 filed); 下游 fundamentals.py 再 T+1 生效;
- 缺值一律 null, 绝不填 0。
股本: dei:EntityCommonStockSharesOutstanding (总股本), 按用户批准作为 float_shares 近似。
SEC 访问规范: User-Agent 带联系邮箱, 全局 ≤10 次/秒 (这里取 ≥0.12s 间隔)。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)



def user_agent() -> str:
    """SEC 要求 UA 带联系邮箱: 读 SEC_USER_AGENT (见 app/credentials.py)。"""
    from app import credentials
    return credentials.sec_user_agent()


TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
MIN_INTERVAL = 0.12
FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F", "6-K"}
TABLES = ("income", "balance_sheet", "cash_flow", "metrics", "shares")

# 输出列 -> 候选 us-gaap 科目 (按优先级)
DURATION = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}
INSTANT = {
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
}
INCOME_COLS = ["revenue", "gross_profit", "operating_income", "net_income", "eps_diluted"]
BS_COLS = ["total_assets", "total_liabilities", "equity", "cash"]
CF_COLS = ["operating_cash_flow", "capex", "free_cash_flow"]
KEY = ["symbol", "period_end", "period_type"]


def sec_ticker(symbol: str) -> str:
    return symbol.upper().replace(".", "-")


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _period_type(start: str | None, end: str) -> str | None:
    if start is None:
        return None
    d = _days(start, end)
    if 80 <= d <= 100:
        return "Q"
    if 350 <= d <= 380:
        return "FY"
    return None


def _first_reported(rows: list[dict], *, instant: bool) -> dict[tuple, tuple[float, str]]:
    """(end, ptype) -> (val, filed): 每期取最早 filed 的值。"""
    out: dict[tuple, tuple[float, str]] = {}
    for r in rows:
        if r.get("form") not in FORMS or r.get("val") is None or not r.get("end") or not r.get("filed"):
            continue
        if instant:
            if r.get("start"):
                continue
            k = (r["end"], None)
        else:
            pt = _period_type(r.get("start"), r["end"])
            if pt is None:
                continue
            k = (r["end"], pt)
        cur = out.get(k)
        if cur is None or r["filed"] < cur[1]:
            out[k] = (float(r["val"]), r["filed"])
    return out


def _concept(gaap: dict, names: list[str], *, instant: bool) -> dict[tuple, tuple[float, str]]:
    merged: dict[tuple, tuple[float, str]] = {}
    for name in names:  # 高优先级科目先占位, 低优先级只补缺期
        units = (gaap.get(name) or {}).get("units") or {}
        rows = units.get("USD") or units.get("USD/shares") or []
        for k, v in _first_reported(rows, instant=instant).items():
            merged.setdefault(k, v)
    return merged


def _frame(rows: list[dict], cols: list[str]) -> pl.DataFrame:
    schema = {"symbol": pl.Utf8, "period_end": pl.Utf8, "period_type": pl.Utf8, "announce_date": pl.Utf8,
              **{c: pl.Float64 for c in cols}}
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def _div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def parse_companyfacts(symbol: str, facts: dict) -> dict[str, pl.DataFrame]:
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    dei = (facts.get("facts") or {}).get("dei") or {}
    dur = {c: _concept(gaap, n, instant=False) for c, n in DURATION.items()}
    ins = {c: _concept(gaap, n, instant=True) for c, n in INSTANT.items()}

    periods = sorted({k for m in dur.values() for k in m})
    flow: dict[tuple, dict] = {}
    for end, pt in periods:
        rec = {"symbol": symbol, "period_end": end, "period_type": pt}
        filed = []
        for c, m in dur.items():
            v = m.get((end, pt))
            rec[c] = v[0] if v else None
            if v:
                filed.append(v[1])
        rec["announce_date"] = min(filed)
        ocf, capex = rec.get("operating_cash_flow"), rec.get("capex")
        rec["free_cash_flow"] = ocf - capex if ocf is not None and capex is not None else None
        flow[(end, pt)] = rec

    bs_rows = []
    for (end, _), _v in sorted({k: 1 for m in ins.values() for k in m}.items()):
        rec = {"symbol": symbol, "period_end": end, "period_type": "I"}
        filed = []
        for c, m in ins.items():
            v = m.get((end, None))
            rec[c] = v[0] if v else None
            if v:
                filed.append(v[1])
        rec["announce_date"] = min(filed)
        bs_rows.append(rec)
    bs_by_end = {r["period_end"]: r for r in bs_rows}

    share_rows = []
    raw_sh = ((dei.get("EntityCommonStockSharesOutstanding") or {}).get("units") or {}).get("shares") or []
    multi = is_multi_class(symbol, raw_sh)
    sh = _first_reported(raw_sh, instant=True)
    for (end, _), (val, filed) in sorted(sh.items()):
        # 多股类公司: companyfacts 不带股类维度, 单值只代表某一类 → 股本/每股指标不可信, 置 null 并标记
        share_rows.append({"symbol": symbol, "period_end": end, "period_type": "I", "announce_date": filed,
                           "total_shares": None if multi else val, "float_shares": None if multi else val,
                           "multi_class": multi})
    # 每股净资产用「公告日不晚于该期公告」的最新股本
    def shares_asof(day: str):
        best = None
        for r in share_rows:
            if r["announce_date"] <= day and r["total_shares"] is not None:
                best = r["total_shares"]
        return best

    metric_rows = []
    for (end, pt), rec in flow.items():
        prev = None
        for (e2, p2), r2 in flow.items():  # 去年同期: 同类型, 期末相差 ±10 天
            if p2 == pt and abs(_days(e2, end) - 365) <= 10:
                prev = r2
        bs = bs_by_end.get(end) or {}
        eq = bs.get("equity")
        ann = 365 / _days_of(pt)
        ni = rec.get("net_income")
        announce = max([d for d in (rec["announce_date"], bs.get("announce_date")) if d])
        sh_n = shares_asof(announce)
        metric_rows.append({
            "symbol": symbol, "period_end": end, "period_type": pt, "announce_date": announce,
            "roe": _div(ni * ann if ni is not None else None, eq),
            "gross_margin": _div(rec.get("gross_profit"), rec.get("revenue")),
            "net_margin": _div(ni, rec.get("revenue")),
            "revenue_yoy": _div(rec["revenue"] - prev["revenue"], abs(prev["revenue"]))
            if prev and rec.get("revenue") is not None and prev.get("revenue") else None,
            "net_income_yoy": _div(ni - prev["net_income"], abs(prev["net_income"]))
            if prev and ni is not None and prev.get("net_income") else None,
            "debt_to_asset_ratio": _div(bs.get("total_liabilities"), bs.get("total_assets")),
            "bps": _div(eq, sh_n),
        })

    return {
        "income": _frame([{k: r[k] for k in [*KEY, "announce_date", *INCOME_COLS]} for r in flow.values()], INCOME_COLS),
        "cash_flow": _frame([{k: r[k] for k in [*KEY, "announce_date", *CF_COLS]} for r in flow.values()], CF_COLS),
        "balance_sheet": _frame(bs_rows, BS_COLS),
        "metrics": _frame(metric_rows, ["roe", "gross_margin", "net_margin", "revenue_yoy", "net_income_yoy",
                                        "debt_to_asset_ratio", "bps"]),
        "shares": _shares_frame(share_rows),
    }


def is_multi_class(symbol: str, raw_shares: list[dict]) -> bool:
    """带股类后缀的代码 (BRK.B / BF-B) 或同一期同一次申报出现多个不同股本值 (各股类分报) → 多股类。"""
    if "." in symbol or "-" in symbol:
        return True
    seen: dict[tuple, set] = {}
    for r in raw_shares:
        if r.get("end") and r.get("filed") and r.get("val") is not None:
            seen.setdefault((r["end"], r["filed"]), set()).add(r["val"])
    return any(len(v) > 1 for v in seen.values())


SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
PERIODIC_FORMS = ("10-Q", "10-K", "20-F", "40-F", "10-Q/A", "10-K/A")


def latest_periodic_filing(submissions: dict) -> dict | None:
    """最近一份定期报告: accession / 实例文件 (inline XBRL 约定 <主文档>_htm.xml) / 申报日 / 报告期末。"""
    r = (submissions.get("filings") or {}).get("recent") or {}
    for i, form in enumerate(r.get("form") or []):
        if form in PERIODIC_FORMS:
            doc = str(r["primaryDocument"][i])
            return {"acc": str(r["accessionNumber"][i]).replace("-", ""),
                    "doc": doc.rsplit(".", 1)[0] + "_htm.xml", "filed": r["filingDate"][i],
                    "period_end": (r.get("reportDate") or [""] * (i + 1))[i] or r["filingDate"][i]}
    return None


def class_shares_from_instance(xml_text: str, symbol: str) -> tuple[float | None, str | None]:
    """XBRL 实例里按 StatementClassOfStockAxis 分类的 dei 股本 → 本代码那一类的股本与股类字母。
    股类判定: TradingSymbol 所在上下文的股类维度; 没有维度时读同上下文 Security12bTitle 的「Class X」。"""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text.encode("utf-8"))
    ctx: dict[str, str | None] = {}
    for c in root.iter():
        if c.tag.endswith("}context"):
            mem = [m.text.strip() for m in c.iter() if m.tag.endswith("}explicitMember") and m.text
                   and "ClassOfStockAxis" in (m.get("dimension") or "")]
            ctx[c.get("id")] = mem[0] if mem else None
    shares: dict[str, float] = {}
    sym_ctx: dict[str, str] = {}
    titles: dict[str, str] = {}
    for e in root.iter():
        tag, ref = e.tag.rsplit("}", 1)[-1], e.get("contextRef")
        if tag == "EntityCommonStockSharesOutstanding" and e.text and ctx.get(ref):
            shares[_class_letter(ctx[ref])] = float(e.text)
        elif tag == "TradingSymbol" and e.text:
            sym_ctx.setdefault(e.text.strip().upper(), ref)
        elif tag == "Security12bTitle" and e.text:
            titles[ref] = e.text
    ref = sym_ctx.get(symbol.upper().replace(".", "-")) or sym_ctx.get(symbol.upper())
    if ref is None:
        return None, None
    letter = _class_letter(ctx[ref]) if ctx.get(ref) else None
    if letter is None:
        import re
        m = re.search(r"Class\s*([A-Z])\b", titles.get(ref, "").replace("\xa0", " "))
        letter = m.group(1) if m else None
    if letter is None or letter not in shares:
        return None, None
    return shares[letter], letter


def _class_letter(member: str) -> str | None:
    import re
    m = re.search(r"Class([A-Z])Member$", member.split(":")[-1])
    return m.group(1) if m else None


def _shares_frame(rows: list[dict]) -> pl.DataFrame:
    schema = {"symbol": pl.Utf8, "period_end": pl.Utf8, "period_type": pl.Utf8, "announce_date": pl.Utf8,
              "total_shares": pl.Float64, "float_shares": pl.Float64, "multi_class": pl.Boolean, "basis": pl.Utf8}
    rows = [{"basis": None, **r} for r in rows]
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def _days_of(pt: str) -> int:
    return 91 if pt == "Q" else 365


def write_tables(data_dir: Path, parsed: Iterable[dict[str, pl.DataFrame]]) -> dict[str, int]:
    parsed = list(parsed)
    counts = {}
    for name in TABLES:
        frames = [p[name] for p in parsed if not p[name].is_empty()]
        if not frames:
            continue
        df = pl.concat(frames, how="vertical").sort(["symbol", "period_end"])
        d = Path(data_dir) / "financials" / name
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "part.parquet.tmp"
        df.write_parquet(tmp)
        os.replace(tmp, d / "part.parquet")  # 原子替换
        counts[name] = df.height
    return counts


def _default_get(url: str, headers: dict) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def sync(data_dir: Path, symbols: Iterable[str], *, http_get: Callable | None = None,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict:
    get = http_get or _default_get
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "identity"}
    last = [None]

    def throttled(url: str) -> str:
        if last[0] is not None:
            wait = MIN_INTERVAL - (clock() - last[0])
            if wait > 0:
                sleep(wait)
        try:
            return get(url, headers)
        finally:
            last[0] = clock()

    cik_map = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in json.loads(throttled(TICKERS_URL)).values()}
    parsed, ok, missing, failed = [], 0, [], []
    for sym in symbols:
        cik = cik_map.get(sec_ticker(sym))
        if cik is None:
            missing.append(sym)
            continue
        try:
            parsed.append(parse_companyfacts(sym, json.loads(throttled(FACTS_URL.format(cik=cik)))))
            ok += 1
        except Exception as exc:  # 单票失败隔离
            logger.warning("EDGAR %s 失败: %s", sym, exc)
            failed.append(sym)
    counts = write_tables(data_dir, parsed) if parsed else {}
    return {"ok": ok, "missing_cik": missing, "failed": failed, "rows": counts}
