"""自选 CSV/TXT 与粘贴 ticker 导入测试 (美股语义, P1-5 重写)。

覆盖: 编码回退、ticker 抽取 (去重保序/常见词排除)、instruments 校验、
CSV 行解析 (逗号/Tab)、粘贴导入上限。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from app.services.watchlist_csv import (
    decode_csv_bytes,
    extract_tickers,
    import_watchlist_codes,
    import_watchlist_csv,
    parse_csv_rows,
)


def _write_instruments(data_dir: Path) -> None:
    inst = data_dir / "instruments"
    inst.mkdir()
    pl.DataFrame({
        "symbol": ["AAPL", "NVDA", "TSLA"],
        "name": ["Apple Inc.", "NVIDIA Corp.", "Tesla Inc."],
    }).write_parquet(inst / "instruments.parquet")


# ── 编码 ──────────────────────────────────────────────────

def test_decode_utf8_and_gbk():
    assert decode_csv_bytes("AAPL,NVDA".encode("utf-8")) == "AAPL,NVDA"
    assert decode_csv_bytes("AAPL,NVDA".encode("gb18030")) == "AAPL,NVDA"


def test_decode_empty_rejected():
    with pytest.raises(ValueError, match="空文件"):
        decode_csv_bytes(b"")


# ── ticker 抽取 ──────────────────────────────────────────

def test_extract_tickers_dedup_and_common_words():
    # 抽取宽容 (英文词会混入), 真校验在 instruments 匹配; 排除表拦截高频杂词
    tickers = extract_tickers("BUY AAPL, NVDA; THE SELL list: MSFT")
    assert "AAPL" in tickers and "NVDA" in tickers and "MSFT" in tickers
    assert "BUY" not in tickers and "THE" not in tickers and "SELL" not in tickers
    assert tickers.count("AAPL") == 1


def test_extract_tickers_class_share():
    assert "BRK.B" in extract_tickers("BRK.B and AAPL")


# ── CSV 行解析 ────────────────────────────────────────────

def test_parse_csv_rows_comma_and_tab():
    # 行内所有大写词都抽 (公司名也会混入, 由 instruments 匹配层去伪)
    rows = parse_csv_rows("AAPL,Apple Inc\nNVDA\tNVIDIA Corp")
    assert rows[0][0] == "AAPL" and rows[1][0] == "NVDA"


def test_parse_csv_rows_skips_blank():
    assert parse_csv_rows("\n\nAAPL\n\n") == [["AAPL"]]


# ── 导入校验 ──────────────────────────────────────────────

def test_import_codes_matched_and_unmatched(tmp_path):
    _write_instruments(tmp_path)
    out = import_watchlist_codes("AAPL NVDA XYZ", tmp_path)
    assert out["matched_count"] == 2
    assert out["unmatched_count"] == 1
    matched = {c["symbol"] for c in out["candidates"] if c["matched"]}
    assert matched == {"AAPL", "NVDA"}


def test_import_codes_marks_existing(tmp_path):
    _write_instruments(tmp_path)
    out = import_watchlist_codes("AAPL", tmp_path, existing_symbols={"AAPL"})
    assert out["candidates"][0]["already_in_watchlist"] is True


def test_import_codes_over_limit_rejected(tmp_path):
    # 纯字母代码列: 超过上限拒绝 (4 位码避开常见词排除表的碰撞)
    from itertools import product
    codes = ["".join(c) for c in product("ABCDEFGHIJKLMNOPQRSTUVWXYZ", repeat=4)][:1001]
    with pytest.raises(ValueError, match="最多导入"):
        import_watchlist_codes(" ".join(codes), tmp_path)


def test_import_csv_end_to_end(tmp_path):
    _write_instruments(tmp_path)
    out = import_watchlist_csv("AAPL,Apple\nNVDA,NVIDIA\nXYZ,Unknown".encode(), tmp_path)
    assert out["matched_count"] == 2
    syms = [c["symbol"] for c in out["candidates"] if c["matched"]]
    assert syms == ["AAPL", "NVDA"]


def test_import_no_instruments_table_all_unmatched(tmp_path):
    out = import_watchlist_codes("AAPL", tmp_path)
    assert out["candidates"][0]["matched"] is False
