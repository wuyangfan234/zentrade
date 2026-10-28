"""GOOG 口径修正 (用户 2026-10-12 批准): 多股类公司分母只取本类股本。
优先: 最近一期 10-Q/10-K/20-F 的 XBRL 实例里 dei:EntityCommonStockSharesOutstanding 按 StatementClassOfStockAxis 分类,
用 TradingSymbol 的维度或 Security12bTitle 的「Class X」对上本代码的股类; 对不上 → Finnhub 合计股本并标 approx_total。"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
from test_edgar_fundamentals import _facts

from app.services import edgar_fundamentals as ef
from app.services import fundamentals_sync as fs

X = """<?xml version="1.0"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
 xmlns:dei="http://xbrl.sec.gov/dei/2024" xmlns:us-gaap="http://fasb.org/us-gaap/2024" xmlns:goog="http://x">
<xbrli:context id="c1"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>
<xbrli:context id="cA"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassAMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-07-15</xbrli:instant></xbrli:period></xbrli:context>
<xbrli:context id="cB"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassBMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-07-15</xbrli:instant></xbrli:period></xbrli:context>
<xbrli:context id="cC"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">goog:CapitalClassCMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-07-15</xbrli:instant></xbrli:period></xbrli:context>
<dei:EntityCommonStockSharesOutstanding contextRef="cA">5868000000</dei:EntityCommonStockSharesOutstanding>
<dei:EntityCommonStockSharesOutstanding contextRef="cB">835000000</dei:EntityCommonStockSharesOutstanding>
<dei:EntityCommonStockSharesOutstanding contextRef="cC">5527000000</dei:EntityCommonStockSharesOutstanding>
<dei:TradingSymbol contextRef="cA">GOOGL</dei:TradingSymbol>
<dei:TradingSymbol contextRef="cC">GOOG</dei:TradingSymbol>
<dei:TradingSymbol contextRef="c1">META</dei:TradingSymbol>
<dei:Security12bTitle contextRef="c1">Class&#160;A Common Stock, par value</dei:Security12bTitle>
<dei:TradingSymbol contextRef="cB">ZZZ</dei:TradingSymbol>
</xbrli:xbrl>"""


def test_class_by_trading_symbol_dimension():
    assert ef.class_shares_from_instance(X, "GOOG") == (5527000000.0, "C")
    assert ef.class_shares_from_instance(X, "GOOGL") == (5868000000.0, "A")


def test_class_by_security_title_when_no_dimension():
    assert ef.class_shares_from_instance(X, "META") == (5868000000.0, "A")


def test_unmapped_symbol_returns_none():
    assert ef.class_shares_from_instance(X, "NOPE") == (None, None)


SUBS = {"filings": {"recent": {"form": ["8-K", "10-Q"], "accessionNumber": ["x", "0001-26-000071"],
                               "primaryDocument": ["a.htm", "goog-20260630.htm"], "filingDate": ["2026-09-01", "2026-07-23"],
                               "reportDate": ["", "2026-06-30"]}}}


def _get(fh_shares=12230.0, instance=X):
    def http_get(url, headers):
        if url.endswith("company_tickers.json"):
            return json.dumps({"0": {"cik_str": 1652044, "ticker": "GOOG"}, "1": {"cik_str": 1652044, "ticker": "GOOGL"}})
        if "submissions/CIK0001652044" in url:
            return json.dumps(SUBS)
        if url.endswith("000107/goog-20260630_htm.xml") or url.endswith("goog-20260630_htm.xml"):
            assert "/Archives/edgar/data/1652044/000126000071/" in url
            if instance is None:
                raise OSError("no instance")
            return instance
        if "stock/profile2" in url:
            return json.dumps({"shareOutstanding": fh_shares})
        if "stock/metric" in url:
            return json.dumps({})
        return json.dumps(_facts())
    return http_get


def test_watchlist_multi_class_uses_edgar_class_shares(tmp_path: Path):
    fs.run(tmp_path, ["GOOG"], http_get=_get(), finnhub_token="T", sleep=lambda s: None, today="2026-10-12",
           watchlist={"GOOG"}, multi_class={"GOOG"})
    sh = pl.read_parquet(tmp_path / "financials/shares/part.parquet").filter(pl.col("float_shares").is_not_null())
    r = sh.row(0, named=True)
    assert sh.height == 1 and r["float_shares"] == 5527000000.0
    assert r["source"] == "sec_edgar_class" and r["basis"] == "class:C" and r["announce_date"] == "2026-07-23"
    assert r["period_end"] == "2026-06-30"


def test_falls_back_to_finnhub_total_marked_approx(tmp_path: Path):
    fs.run(tmp_path, ["GOOG"], http_get=_get(instance=None), finnhub_token="T", sleep=lambda s: None,
           today="2026-10-12", watchlist={"GOOG"}, multi_class={"GOOG"})
    sh = pl.read_parquet(tmp_path / "financials/shares/part.parquet").filter(pl.col("float_shares").is_not_null())
    r = sh.row(0, named=True)
    assert r["source"] == "finnhub" and r["basis"] == "approx_total" and r["float_shares"] == pytest.approx(12230e6)


def test_non_watchlist_multi_class_no_class_lookup(tmp_path: Path):
    calls = []
    g = _get()

    def spy(url, headers):
        calls.append(url)
        return g(url, headers)
    fs.run(tmp_path, ["GOOG"], http_get=spy, finnhub_token="T", sleep=lambda s: None, today="2026-10-12",
           watchlist=set(), multi_class={"GOOG"})
    assert not any("submissions" in u for u in calls)
