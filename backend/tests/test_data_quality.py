"""日K数据质量自动检查: 数值自洽、跳变、关键标的缺行、两源对价、推送 (2026-10-12)。"""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import data_integrity
from app.services import data_quality as dq

ET = us_clock.tz
FRI, MON = date(2026, 10, 9), date(2026, 10, 12)


@pytest.fixture(autouse=True)
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def write(tmp_path, day, rows):
    p = tmp_path / "kline_daily" / f"date={day.isoformat()}"
    p.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema={"symbol": pl.Utf8, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
                               "close": pl.Float64, "volume": pl.Float64}, orient="row").write_parquet(p / "part.parquet")


def good(sym, c=100.0):
    return (sym, c, c * 1.01, c * 0.99, c, 1000.0)


def test_clean_partition_has_no_issues(tmp_path):
    write(tmp_path, FRI, [good("QQQ"), good("SPY")])
    write(tmp_path, MON, [good("QQQ", 101), good("SPY", 101)])
    assert dq.numeric_issues(tmp_path, MON, keys=["QQQ", "SPY"]) == []


def test_each_numeric_problem_is_reported(tmp_path):
    write(tmp_path, MON, [
        ("A", 100.0, 99.0, 98.0, 100.0, 10.0),        # 最高 < 收
        ("B", 100.0, 102.0, 101.0, 100.0, 10.0),      # 最低 > 收
        ("C", None, 101.0, 99.0, 100.0, 10.0),        # 缺开盘
        ("D", 100.0, 101.0, 99.0, 100.0, -1.0),       # 量为负
        ("E", 0.0, 0.0, 0.0, 0.0, 0.0),               # 价非正
    ])
    kinds = {i["kind"]: i["symbols"] for i in dq.numeric_issues(tmp_path, MON)}
    assert kinds["high_below_body"] == ["A"]
    assert kinds["low_above_body"] == ["B"]
    assert kinds["null_open"] == ["C"]
    assert kinds["negative_volume"] == ["D"]
    assert kinds["nonpositive_price"] == ["E"]


def test_jump_flagged_unless_corporate_action(tmp_path, monkeypatch):
    write(tmp_path, FRI, [good("X", 100), good("Y", 100), good("Z", 100)])
    write(tmp_path, MON, [good("X", 140), good("Y", 50), good("Z", 105)])
    monkeypatch.setattr(dq, "_corp_action_symbols", lambda d, day: {"Y"})        # Y 是拆股
    jump = next(i for i in dq.numeric_issues(tmp_path, MON) if i["kind"] == "suspicious_jump")
    assert jump["n"] == 1 and jump["symbols"] == ["X +40%"]


def test_key_symbol_missing(tmp_path):
    write(tmp_path, MON, [good("SPY")])
    miss = next(i for i in dq.numeric_issues(tmp_path, MON, keys=["QQQ", "SPY", "AAPL"]) if i["kind"] == "key_symbol_missing")
    assert miss["symbols"] == ["QQQ", "AAPL"]


def test_missing_partition_left_to_integrity(tmp_path):
    assert dq.numeric_issues(tmp_path, MON) == []


class Prov:
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc = rows, exc

    def get_daily(self, symbols, start, end, asset_type="stock"):
        if self.exc:
            raise self.exc
        return pl.DataFrame(self.rows, schema={"symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64}, orient="row")


def _prov(monkeypatch, p):
    import app.data_providers.custom.loader as loader
    monkeypatch.setattr(loader, "get_provider", lambda name: p)


def test_cross_check_ok_and_deviation(tmp_path, monkeypatch):
    write(tmp_path, MON, [good("QQQ", 100), good("SPY", 200)])
    _prov(monkeypatch, Prov([("QQQ", MON, 100.2), ("SPY", MON, 200.0)]))
    r = dq.cross_check(tmp_path, MON, ["QQQ", "SPY"], provider_name="tickflow")
    assert r["status"] == "ok" and r["compared"] == 2 and r["max_dev_pct"] == 0.2
    _prov(monkeypatch, Prov([("QQQ", MON, 103.0), ("SPY", MON, 200.0)]))
    r = dq.cross_check(tmp_path, MON, ["QQQ", "SPY"], provider_name="tickflow")
    assert r["status"] == "deviation" and [d["symbol"] for d in r["deviations"]] == ["QQQ"]


def test_cross_check_unavailable_is_not_an_issue(tmp_path, monkeypatch):
    write(tmp_path, MON, [good("QQQ")])
    _prov(monkeypatch, Prov(exc=TimeoutError()))
    assert dq.cross_check(tmp_path, MON, ["QQQ"], provider_name="x")["status"] == "unavailable"
    _prov(monkeypatch, Prov([("QQQ", FRI, 100.0)]))                               # 对方还没有当天数据
    assert dq.cross_check(tmp_path, MON, ["QQQ"], provider_name="x")["reason"] == "no_rows_for_day"


def test_run_pushes_once_per_day_only_when_bad(tmp_path, monkeypatch):
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda d, today=None: [])
    monkeypatch.setattr(dq, "cross_check", lambda d, day, syms, provider_name=None: {"status": "ok", "compared": 2, "max_dev_pct": 0.0})
    write(tmp_path, FRI, [good("QQQ"), good("SPY")])
    write(tmp_path, MON, [good("QQQ"), good("SPY")])
    sent = []
    r = dq.run(tmp_path, now=datetime(2026, 10, 12, 17, 0, tzinfo=ET), enqueue=lambda k, p: sent.append(k))
    assert r["ok"] and r["day"] == "2026-10-12" and sent == []
    assert dq.latest(tmp_path)["ok"] is True
    write(tmp_path, MON, [good("SPY")])                                           # QQQ 缺行
    r = dq.run(tmp_path, now=datetime(2026, 10, 12, 17, 0, tzinfo=ET), enqueue=lambda k, p: sent.append((k, p["body"])))
    assert not r["ok"] and sent[0][0] == "dq:2026-10-12" and "QQQ" in sent[0][1]
    assert "数值 1 类异常" in dq.summary(dq.latest(tmp_path))


def test_run_before_close_checks_previous_day(tmp_path, monkeypatch):
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda d, today=None: [])
    monkeypatch.setattr(dq, "cross_check", lambda *a, **k: {"status": "unavailable", "reason": "no_partition"})
    r = dq.run(tmp_path, now=datetime(2026, 10, 12, 9, 0, tzinfo=ET), enqueue=lambda k, p: None)
    assert r["day"] == "2026-10-09"


def test_key_symbols_from_loop():
    loop = SimpleNamespace(
        ledger=SimpleNamespace(_lock=__import__("threading").Lock(), open_positions=[SimpleNamespace(symbol="AAPL")]),
        trader=SimpleNamespace(_cards={"c": SimpleNamespace(symbol="NVDA"), "d": SimpleNamespace(symbol="AAPL")}))
    assert dq.key_symbols(None, loop) == ["QQQ", "SPY", "AAPL", "NVDA"]
    assert dq.key_symbols(None, None) == ["QQQ", "SPY"]


# ── 0.12.1: 跳变检查去误报 ──────────────────────────────────────────
def _adj(tmp_path, rows):
    d = tmp_path / "adj_factor"
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema={"symbol": pl.Utf8, "trade_date": pl.Date, "ex_factor": pl.Float64,
                               "vol_factor": pl.Float64}, orient="row").write_parquet(d / "all.parquet")


@pytest.mark.parametrize("sym", ["CDTTW", "ARCLR", "ALISU", "ASGI.RT", "COPL.WS", "ABC.U", "ABCD.WT"])
def test_jump_ignores_warrants_rights_units(sym):
    assert dq.is_derivative_symbol(sym)


@pytest.mark.parametrize("sym", ["SNOW", "LULU", "SPY", "NVDA", "BRK.B", "AAPL", "QQQ"])
def test_jump_keeps_ordinary_symbols(sym):
    assert not dq.is_derivative_symbol(sym)


def test_jump_ignores_derivatives_and_penny_stocks(tmp_path, monkeypatch):
    monkeypatch.setattr(dq, "_corp_action_symbols", lambda d, day: set())
    write(tmp_path, FRI, [good("X", 100), good("ABCDW", 1.0), good("P", 0.5)])
    write(tmp_path, MON, [good("X", 140), good("ABCDW", 2.0), good("P", 0.9)])
    jump = next(i for i in dq.numeric_issues(tmp_path, MON) if i["kind"] == "suspicious_jump")
    assert jump["symbols"] == ["X +40%"]


def test_jump_ignores_split_from_source_adj_factor(tmp_path, monkeypatch):
    """NXGL 1:20 反拆: 本地公司行动表只覆盖持仓/卡, 数据源 adj_factor 有事件 → 不报。"""
    monkeypatch.setattr(dq, "_corp_action_symbols", lambda d, day: set())
    write(tmp_path, FRI, [good("NXGL", 2.0), good("X", 100)])
    write(tmp_path, MON, [good("NXGL", 38.1), good("X", 140)])
    _adj(tmp_path, [("NXGL", MON, 0.05, 0.05), ("X", FRI, 0.05, 0.05)])           # X 的事件不在当天
    jump = next(i for i in dq.numeric_issues(tmp_path, MON) if i["kind"] == "suspicious_jump")
    assert jump["symbols"] == ["X +40%"]


def test_jump_only_alerts_for_alert_set_rest_observed(tmp_path, monkeypatch):
    monkeypatch.setattr(dq, "_corp_action_symbols", lambda d, day: set())
    write(tmp_path, FRI, [good("X", 100), good("W1", 100)])
    write(tmp_path, MON, [good("X", 140), good("W1", 50)])
    observed: list = []
    issues = dq.numeric_issues(tmp_path, MON, alert={"W1"}, observed=observed)
    jump = next(i for i in issues if i["kind"] == "suspicious_jump")
    assert jump["symbols"] == ["W1 -50%"]
    assert observed == ["X +40%"]
    assert not any(i["kind"] == "suspicious_jump" for i in dq.numeric_issues(tmp_path, MON, alert=set()))


def test_run_alerts_watchlist_jump_but_only_records_others(tmp_path, monkeypatch):
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda d, today=None: [])
    monkeypatch.setattr(dq, "cross_check", lambda *a, **k: {"status": "ok", "compared": 2, "max_dev_pct": 0.0})
    monkeypatch.setattr(dq, "_corp_action_symbols", lambda d, day: set())
    write(tmp_path, FRI, [good("QQQ"), good("SPY"), good("ZZZ", 10), good("WL", 10)])
    write(tmp_path, MON, [good("QQQ"), good("SPY"), good("ZZZ", 30), good("WL", 10)])
    sent = []
    now = datetime(2026, 10, 12, 17, 0, tzinfo=ET)
    r = dq.run(tmp_path, now=now, enqueue=lambda k, p: sent.append(k))
    assert r["ok"] and sent == [] and r["observed"]["jumps"] == ["ZZZ +200%"]
    (tmp_path / "user_data").mkdir()
    pl.DataFrame({"symbol": ["ZZZ"]}).write_parquet(tmp_path / "user_data" / "watchlist.parquet")
    r = dq.run(tmp_path, now=now, enqueue=lambda k, p: sent.append(k))
    assert not r["ok"] and sent == ["dq:2026-10-12"]
