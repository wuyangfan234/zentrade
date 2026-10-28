"""自选股 CSV/TXT 与粘贴代码批量导入：解码 → 抽 ticker → instruments 校验。

美股语义 (P1-5 重写): ticker 为 1-5 位字母 (AAPL/NVDA), 兼容 BRK.B 风格点号,
CSV/TXT 逗号或 Tab 分隔自动识别, 编码回退 UTF-8 → GB18030。
校验主数据 = 本地 instruments 表 (Alpaca 标的维表); 未命中进 unmatched 由前端确认。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

# 美股 ticker: 1-5 位大写字母, 可选 .A/.B 类股后缀 (BRK.B); 排除常见英文词误报
_TICKER_RE = re.compile(r"\b([A-Z]{1,5}(?:\.[AB])?)\b")
_COMMON_WORDS = frozenset({
    "THE", "AND", "FOR", "ARE", "NOT", "YOU", "ALL", "CAN", "HER", "WAS",
    "ONE", "OUR", "OUT", "DAY", "GET", "HAS", "HIM", "HIS", "HOW", "ITS",
    "MAY", "NEW", "NOW", "OLD", "SEE", "TWO", "WAY", "WHO", "DID", "BUY",
    "SELL", "HOLD", "HIGH", "LOW", "OPEN", "CLOSE", "DATE", "NAME", "LAST",
    "PRICE", "STOCK", "USD", "ETF", "CSV", "TXT",
})

# 名称列里的股票类别词 ("Alphabet Inc. Class A"): 其中的单字母不是代码 (否则误识别成 A=Agilent, B=Barrick)
_SHARE_CLASS_RE = re.compile(r"\bCLASS\s+[A-Z]\b")

_ENCODINGS = ("utf-8-sig", "gb18030")


@dataclass
class ImportCandidate:
    code: str
    symbol: str | None
    name: str | None
    matched: bool
    already_in_watchlist: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "symbol": self.symbol,
            "name": self.name,
            "matched": self.matched,
            "already_in_watchlist": self.already_in_watchlist,
        }


def _finalize(provider: str, text: str, codes: list[str], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    matched_count = sum(1 for c in candidates if c["matched"])
    return {
        "provider": provider,
        "raw_text": text,
        "codes": codes,
        "candidates": candidates,
        "matched_count": matched_count,
        "unmatched_count": len(candidates) - matched_count,
    }


def decode_csv_bytes(raw: bytes) -> str:
    """把上传字节解码为文本，兼容 UTF-8 / GBK 系编码。"""
    if not raw:
        raise ValueError("空文件")
    last_err: Exception | None = None
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError) as e:
            last_err = e
    raise ValueError("无法识别文件编码，请另存为 UTF-8 或 GBK 后重试") from last_err


def extract_tickers(text: str) -> list[str]:
    """全文抽取 ticker (去重保序)。排除常见英文词误报。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _TICKER_RE.finditer(_SHARE_CLASS_RE.sub(" ", text.upper())):
        t = m.group(1)
        if t in _COMMON_WORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def build_instrument_lookups(data_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    """从本地 instruments 表构建 (symbol→symbol, symbol→name) 两表。"""
    inst_path = Path(data_dir) / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return {}, {}
    df = pl.read_parquet(inst_path)
    symbol_to_symbol = {s: s for s in df["symbol"].to_list() if s}
    symbol_to_name = {s: n for s, n in zip(df["symbol"].to_list(), df["name"].to_list()) if s}
    return symbol_to_symbol, symbol_to_name


def resolve_candidates(
    codes: list[str],
    symbol_to_symbol: dict[str, str],
    symbol_to_name: dict[str, str],
    existing_symbols: set[str] | None,
) -> list[ImportCandidate]:
    existing = existing_symbols or set()
    out: list[ImportCandidate] = []
    for code in codes:
        symbol = symbol_to_symbol.get(code)
        out.append(ImportCandidate(
            code=code,
            symbol=symbol,
            name=symbol_to_name.get(symbol) if symbol else None,
            matched=symbol is not None,
            already_in_watchlist=symbol in existing if symbol else False,
        ))
    return out


def parse_csv_rows(text: str) -> list[list[str]]:
    """解析 CSV/TXT 文本为行内 ticker 列表 (行序保持)。"""
    if not text.strip():
        return []
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = "\t" if first.count("\t") > first.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[list[str]] = []
    for raw_row in reader:
        cells = [c.strip() for c in raw_row if c is not None]
        if not cells:
            continue
        tickers: list[str] = []
        for cell in cells:
            tickers.extend(extract_tickers(cell))
        rows.append(tickers)
    return rows


def import_watchlist_csv(
    raw: bytes,
    data_dir: Path,
    *,
    existing_symbols: set[str] | None = None,
) -> dict[str, Any]:
    """解析 CSV/TXT 字节并返回候选列表（不写入自选）。"""
    text = decode_csv_bytes(raw)
    rows = parse_csv_rows(text)
    unique: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for t in row:
            if t not in seen:
                seen.add(t)
                unique.append(t)
    symbol_to_symbol, symbol_to_name = build_instrument_lookups(data_dir)
    candidates = resolve_candidates(unique, symbol_to_symbol, symbol_to_name, existing_symbols)
    return _finalize("csv", text, unique, [c.to_dict() for c in candidates])


def import_watchlist_codes(
    text: str,
    data_dir: Path,
    *,
    existing_symbols: set[str] | None = None,
    max_codes: int = 1000,
) -> dict[str, Any]:
    """解析粘贴的 ticker 并返回候选列表（不写入自选）。"""
    codes = extract_tickers(text)
    if not codes:
        return _finalize("codes", text, [], [])
    if len(codes) > max_codes:
        raise ValueError(f"一次最多导入 {max_codes} 个代码，已识别 {len(codes)} 个")
    symbol_to_symbol, symbol_to_name = build_instrument_lookups(data_dir)
    candidates = resolve_candidates(codes, symbol_to_symbol, symbol_to_name, existing_symbols)
    return _finalize("codes", text, codes, [c.to_dict() for c in candidates])
