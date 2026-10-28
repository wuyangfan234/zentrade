"""P5 (2026-10-11): 助手工具接受 1–5 字母美股代码, 含 KO/GS 这类两字母和 BRK.B 这类带点的类别股。"""

import polars as pl
import pytest

from app.custom.assistant import tools as assistant_tools


@pytest.mark.parametrize("raw, expected", [
    ("KO", "KO"),
    ("GS", "GS"),
    ("F", "F"),
    ("AAPL", "AAPL"),
    ("GOOGL", "GOOGL"),
    ("BRK.B", "BRK.B"),
    ("BF.A", "BF.A"),
    ("ATH.PRA", "ATH.PRA"),
    (" ko ", "KO"),
    ("brk.b", "BRK.B"),
    ("BRK-B", "BRK.B"),
    ("BRK/B", "BRK.B"),
])
def test_validate_symbol_accepts_us_tickers(raw: str, expected: str) -> None:
    assert assistant_tools._validate_symbol(raw) == expected


@pytest.mark.parametrize("raw", [
    "", None, "TOOLONG", "AAPL.TOOLONG", "600519", "600519.SH", "A B", "not a symbol!!", "BRK..B", ".B", "BRK.",
])
def test_validate_symbol_rejects_non_us_tickers(raw) -> None:
    with pytest.raises(ValueError, match="无效的证券代码"):
        assistant_tools._validate_symbol(raw)


class _Repo:
    def get_name_map(self, symbols=None) -> dict[str, str]:
        return {"KO": "Coca-Cola", "BRK.B": "Berkshire Hathaway B"}


class _Quotes:
    def get_quotes_compat(self) -> pl.DataFrame:
        return pl.DataFrame({
            "symbol": ["KO", "BRK.B", "AAPL"],
            "close": [62.1, 480.0, 230.0],
            "change_pct": [0.01, -0.002, 0.0],
            "amount": [1.0e9, 2.0e9, 3.0e9],
        })


async def test_get_stock_quote_accepts_two_letter_and_class_symbols() -> None:
    ctx = assistant_tools.ToolContext.build(repo=_Repo(), quote_service=_Quotes())
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_quote", {"symbols": ["ko", "BRK-B"]}, ctx,
    )
    assert payload["ok"] is True, payload
    rows = {r["symbol"]: r for r in payload["result"]["rows"]}
    assert set(rows) == {"KO", "BRK.B"}
    assert rows["KO"]["name"] == "Coca-Cola"
