"""黄金样本 (2026-10-11 A/D 改造): 信号表多了谱系字段 (顶层 lineage + 每行 mode/params_hash) 之后,
从文件生成的下单簿、交易员读到的卡、开仓许可必须与改造前逐字段相同。交易链一行不改, 这里锁住“只多不改”。"""
import copy
import json
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import card_lineage as cl
from app.services import decision_scan_job as job
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv
from app.opportunity.card import OpportunityCard

SIG_DAY = date(2026, 10, 6)
CLOSE = date(2026, 10, 8)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda syms: {})


def _clear():
    from app.services import strategy_evaluation as se
    se._V4_CACHE.clear()
    wv._STATUS_CACHE.clear()
    wv._PM_CACHE.clear()
    wb._LOAD_CACHE.clear()
    wb._ENSURE_KEY.clear()
    wb._ENSURE_RETRY.clear()


def _bars(sym, closes, end):
    days, d = [], end
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    c = np.array(closes, float)
    return pl.DataFrame({"symbol": sym, "date": days[::-1], "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": np.full(len(c), 2e6)})


def _card(sym, px=100.0, side="long"):
    if side == "short":
        return OpportunityCard(id=f"opp-{sym}-s", symbol=sym, setup_type="x", created_date=SIG_DAY, side="short",
                               entry_zone=(px * 0.99, px), invalidation=px * 1.06, target_zone=(px * 0.86, px * 0.877),
                               risk_per_share=px * 0.065, reward_risk=2.0)
    return OpportunityCard(id=f"opp-{sym}-l", symbol=sym, setup_type="x", created_date=SIG_DAY, entry_zone=(px, px * 1.01),
                           invalidation=px * 0.94, target_zone=(px * 1.12, px * 1.14), risk_per_share=px * 0.065,
                           reward_risk=2.0)


def _world(d, *, trial):
    (d / "decision_desk").mkdir(parents=True, exist_ok=True)
    (d / "decision_desk" / "evidence_mode.json").write_text(json.dumps({"mode": "v4"}))
    wv.save_ruleset(d, ["macd_golden"], {})
    if trial:
        pass  # 0.11.0: 试用档已退役
    _clear()


SIGNALS = {"macd_golden": [{"symbol": "NVDA", "card": _card("NVDA"), "stop": -0.06},
                           {"symbol": "AAPL", "card": _card("AAPL"), "stop": -0.06}],
           "trend_breakout": [{"symbol": "MU", "card": _card("MU"), "stop": -0.06}],
           "phantom_4h_short": [{"symbol": "AMD", "card": _card("AMD", side="short"), "stop": 0.05}],
           "tv_ut_bot": [{"symbol": "AAPL", "card": _card("AAPL", side="short"), "stop": 0.05}]}
WATCH = {"NVDA", "AAPL", "MU", "AMD"}


def _repo():
    frames = [_bars(s, [100] * 30 + [99, 99], CLOSE) for s in sorted(WATCH)]
    full = pl.concat(frames)
    return SimpleNamespace(store=SimpleNamespace(data_dir=None), get_daily_batch=lambda syms, s, e, columns=None: full.filter(
        pl.col("symbol").is_in(syms) & (pl.col("date") >= s) & (pl.col("date") <= e)))


def _strip(x):
    x = copy.deepcopy(x)
    x.pop("built_at", None)
    x.pop("generated_at", None)
    return x


@pytest.mark.parametrize("trial", [False, True])
def test_book_and_trader_inputs_identical_with_and_without_lineage(tmp_path, trial):
    plain, stamped = tmp_path / "plain", tmp_path / "stamped"
    for d in (plain, stamped):
        _world(d, trial=trial)
    wv.save_signals(plain, SIG_DAY, SIGNALS, watchlist=WATCH, frames={})
    lin, extra = cl.stamp(stamped, SIG_DAY, rules=sorted(SIGNALS))
    assert lin is not None and extra is not None
    wv.save_signals(stamped, SIG_DAY, SIGNALS, watchlist=WATCH, frames={}, lineage=lin, row_extra=extra)
    a = json.loads((wv.signals_dir(plain) / f"{SIG_DAY}.json").read_text())
    b = json.loads((wv.signals_dir(stamped) / f"{SIG_DAY}.json").read_text())
    # 只多不改: 原有每个键值逐字相同
    assert {k: v for k, v in b.items() if k != "lineage"} | {"generated_at": None, "signals": None} == \
        {**a, "generated_at": None, "signals": None}
    assert len(a["signals"]) == len(b["signals"])
    for x, y in zip(a["signals"], b["signals"]):
        assert {k: y[k] for k in x} == x and set(y) - set(x) == {"mode", "params_hash"}
    _clear()
    pm_a, pm_b = wv.permission(plain), wv.permission(stamped)
    assert pm_a["tradable"] == pm_b["tradable"] and pm_a["risk_pct"] == pm_b["risk_pct"]
    assert pm_a["short_tradable"] == pm_b["short_tradable"]
    repo = _repo()
    _clear()
    ba = wb.build_day(plain, repo, SIG_DAY, as_of_close=CLOSE, watch=WATCH, pm=wv.permission(plain))
    _clear()
    bb = wb.build_day(stamped, repo, SIG_DAY, as_of_close=CLOSE, watch=WATCH, pm=wv.permission(stamped))
    assert _strip(ba) == _strip(bb)
    if trial:
        assert ba["cards"], "黄金样本必须真的产出下单簿卡"
    # 落盘后交易员读到的卡 (opportunity_service 从簿文件读) 也逐字段相同
    wb.save_day(plain, ba)
    wb.save_day(stamped, bb)
    _clear()
    la, lb = wb.load_day(plain, SIG_DAY), wb.load_day(stamped, SIG_DAY)
    assert _strip(la) == _strip(lb)


def test_snapshot_writes_lineage_but_book_ignores_it(tmp_path, monkeypatch):
    """扫描写信号表时带谱系; 谱系失败不影响落表。"""
    _world(tmp_path, trial=True)
    monkeypatch.setattr(job, "watchlist_signals", lambda as_of, **kw: {k: v for k, v in SIGNALS.items() if k != "tv_ut_bot"
                                                                         and k != "phantom_4h_short"})
    monkeypatch.setattr(job, "watchlist_short_signals", lambda as_of, **kw: {"phantom_4h_short": SIGNALS["phantom_4h_short"]})
    out = SimpleNamespace(summary={})
    job._watchlist_v4_snapshot(tmp_path, SIG_DAY, watchlist=WATCH, frames={}, hits={}, stops={}, stops_by_symbol={},
                               s1s2={}, out=out)
    p = json.loads((wv.signals_dir(tmp_path) / f"{SIG_DAY}.json").read_text())
    assert p["lineage"]["ruleset_version"] == 1 and all("mode" in x for x in p["signals"])
    monkeypatch.setattr(cl, "stamp", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    job._watchlist_v4_snapshot(tmp_path / "b", SIG_DAY, watchlist=WATCH, frames={}, hits={}, stops={}, stops_by_symbol={},
                               s1s2={}, out=out)
    p2 = json.loads((wv.signals_dir(tmp_path / "b") / f"{SIG_DAY}.json").read_text())
    assert "lineage" not in p2 and len(p2["signals"]) == len(p["signals"])
