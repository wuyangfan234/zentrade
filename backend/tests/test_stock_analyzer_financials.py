"""0.16.2 AI 个股分析接入财务 (SEC EDGAR 同步, data/financials/{metrics,income})。"""
from datetime import date

import polars as pl

from app.services import stock_analyzer as sa

TODAY = date(2026, 10, 14)


def _write(tmp_path, table, rows):
    d = tmp_path / "financials" / table
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(d / "part.parquet")


def _seed(tmp_path):
    base = {"symbol": "NVDA", "period_type": "Q", "source": "sec_edgar"}
    periods = [("2025-07-27", "2025-08-27"), ("2025-10-26", "2025-11-19"), ("2026-01-25", "2026-02-25"),
               ("2026-04-26", "2026-05-20"), ("2026-07-26", "2026-08-26"), ("2026-10-25", "2026-11-19")]  # 最后一期未申报
    _write(tmp_path, "metrics", [{**base, "period_end": pe, "announce_date": ad, "roe": 0.3, "gross_margin": 0.7,
                                  "net_margin": 0.5, "revenue_yoy": 0.6, "net_income_yoy": 1.2,
                                  "debt_to_asset_ratio": 0.28, "bps": 9.5} for pe, ad in periods]
           + [{"symbol": "AAPL", "period_type": "Q", "source": "sec_edgar", "period_end": "2026-06-27",
               "announce_date": "2026-07-31", "roe": 1.5, "gross_margin": 0.46, "net_margin": 0.25, "revenue_yoy": 0.1,
               "net_income_yoy": 0.1, "debt_to_asset_ratio": 0.8, "bps": 4.0}])
    _write(tmp_path, "income", [{**base, "period_end": pe, "announce_date": ad, "revenue": 4.6e10, "gross_profit": 3.3e10,
                                 "operating_income": 6.37e10, "net_income": 5.97e10, "eps_diluted": 2.46}
                                for pe, ad in periods])


def test_load_financials_injects_latest_four_announced_periods_with_period_and_source(tmp_path):
    _seed(tmp_path)
    fins = sa._load_financials(tmp_path, "NVDA", today=TODAY)
    for table in ("metrics", "income"):
        rows = fins[table]
        assert [r["period_end"] for r in rows] == ["2025-10-26", "2026-01-25", "2026-04-26", "2026-07-26"]
        assert all(r["source"] == "sec_edgar" and r["announce_date"] <= "2026-10-14" for r in rows)
        assert all(r.get("symbol") in (None, "NVDA") for r in rows)
    assert fins["income"][-1]["eps_diluted"] == 2.46


def test_unannounced_period_not_visible_early(tmp_path):
    _seed(tmp_path)
    fins = sa._load_financials(tmp_path, "NVDA", today=date(2026, 11, 18))
    assert fins["metrics"][-1]["period_end"] == "2026-07-26"
    fins = sa._load_financials(tmp_path, "NVDA", today=date(2026, 11, 19))
    assert fins["metrics"][-1]["period_end"] == "2026-10-25"


def test_no_data_returns_empty_and_prompt_falls_back(tmp_path):
    assert sa._load_financials(tmp_path, "NVDA", today=TODAY) == {}
    _seed(tmp_path)
    assert sa._load_financials(tmp_path, "ZZZZ", today=TODAY) == {}
    p = sa._build_user_prompt([], {}, {}, 10.0, "ZZZZ", "")
    assert "该标的暂无财务数据" in p and "最新财务数据" not in p


def test_prompt_with_data_cites_numbers_period_and_source(tmp_path):
    _seed(tmp_path)
    fins = sa._load_financials(tmp_path, "NVDA", today=TODAY)
    p = sa._build_user_prompt([], fins, {}, 10.0, "NVDA", "")
    assert "最新财务数据" in p and "2026-07-26" in p and "sec_edgar" in p and "暂无财务数据" not in p
    assert "报告期" in p and "来源" in p


def test_etf_and_index_keep_their_fallback_copy():
    assert "该标的为 ETF" in sa._build_user_prompt([], {}, {}, 10.0, "SPY", "", asset_type="etf")
    assert "该标的为指数" in sa._build_user_prompt([], {}, {}, 10.0, "^GSPC", "", asset_type="index")


def test_system_prompt_no_longer_claims_not_connected():
    s = sa._SYSTEM_PROMPT
    assert "未接入财务报表" not in s.split("### 4.")[1].split("### 5.")[0].splitlines()[1]
    assert "报告期" in s and "来源" in s and "绝对不要编造" in s
