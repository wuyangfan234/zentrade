"""因子检验审计复现 (先红): 年化区间 / 分组夏普丢期 / 预热窗口 / 期数不足提示 / 批量叫法 / 换代 500。"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.backtest.factor import FactorBacktestService, FactorBatchConfig, FactorConfig


def _bdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class _Engine:
    def __init__(self, rows):
        self.df = pl.DataFrame(rows)
        self.loads: list[date] = []

    def load_panel(self, symbols, start, end, columns, asset_type, **_kw):
        self.loads.append(start)
        df = self.df.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        if symbols is not None:
            df = df.filter(pl.col("symbol").is_in(symbols))
        return df.select([c for c in columns if c in df.columns] + (["fac"] if "fac" in df.columns else []))


def _rows(days, close_fn, fac_fn, n_sym=8):
    return [
        {"symbol": f"S{k}", "date": d, "open": close_fn(i, k), "high": close_fn(i, k), "low": close_fn(i, k),
         "close": close_fn(i, k), "volume": 1e6, "amount": close_fn(i, k) * 1e6, "turnover_rate": None,
         "fac": float(fac_fn(i, k))}
        for i, d in enumerate(days) for k in range(n_sym)
    ]


def _run(rows, factor="fac", **kw):
    return FactorBacktestService(_Engine(rows)).run(FactorConfig(factor_name=factor, symbols=None, **kw))


def test_all_history_start_does_not_spread_annual_return_over_a_century():
    days = _bdays(date(2026, 5, 1), 126)
    rows = _rows(days, lambda i, k: 100 * (1 + (k - 3.5) * 0.001) ** i, lambda i, k: k)
    real = _run(rows, start=days[0], end=days[-1], n_groups=4, rebalance="daily", slippage_bps=0)
    allh = _run(rows, start=date(1900, 1, 1), end=days[-1], n_groups=4, rebalance="daily", slippage_bps=0)
    assert real.group_stats[-1]["annual_return"] > 0.5
    assert allh.group_stats[-1]["annual_return"] == pytest.approx(real.group_stats[-1]["annual_return"], rel=0.02)


def test_group_sharpe_and_win_rate_include_the_first_period():
    # Q1 per-period returns -10%, +10%, +10% (monthly, 3 rebalances + 1 trailing)
    service = FactorBacktestService(_Engine(_rows(_bdays(date(2026, 1, 2), 3), lambda i, k: 1.0, lambda i, k: k)))
    nav = [{"date": "2026-01-02", "Q1": 0.9}, {"date": "2026-02-02", "Q1": 0.99}, {"date": "2026-03-02", "Q1": 1.089}]
    stats = service._calc_group_stats(nav, date(2026, 1, 2), date(2026, 3, 31), "monthly")
    assert stats[0]["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert stats[0]["sharpe"] > 0


def test_single_rebalance_period_reports_the_real_reason():
    days = _bdays(date(2026, 9, 2), 18)
    r = _run(_rows(days, lambda i, k: 100 + i + k, lambda i, k: k), start=days[0], end=days[-1], n_groups=4, rebalance="monthly", slippage_bps=0)
    assert r.ic_mean is None
    assert r.error is not None and "调仓" in r.error
    # 截面无差异 (IC 算不出) 时不报错, 分层结果照常给出
    flat = _bdays(date(2026, 3, 2), 40)
    r2 = _run(_rows(flat, lambda i, k: 100 + i + k, lambda i, k: 1.0), start=flat[0], end=flat[-1], n_groups=4, rebalance="daily", slippage_bps=0)
    assert r2.error is None and r2.ic_mean is None and r2.group_stats


def test_warmup_window_follows_the_longest_factor():
    from app.backtest import factor as factor_mod

    days = _bdays(date(2025, 1, 2), 450)
    import math

    engine = _Engine(_rows(days, lambda i, k: 100 + i * 0.05 * (k + 1) + 8 * math.sin(i / (5 + k) + k), lambda i, k: k))
    service = FactorBacktestService(engine)
    start = days[-60]
    result = service.run(FactorConfig(factor_name="position_240d", symbols=None, start=start, end=days[-1], n_groups=4, rebalance="daily", slippage_bps=0))
    assert min(engine.loads) <= start - timedelta(days=241 * 1.4)
    assert result.error is None, result.error
    assert factor_mod.FACTOR_WARMUP_DAYS == 120  # default floor unchanged


def test_batch_labels_come_from_the_live_registry(tmp_path):
    from app.factors import registry, store

    fid = "uf_inspect_label"
    registry.unregister_factor(fid)
    store.register_definition({"id": fid, "kind": "custom", "label": "探针因子", "group": "自定义", "formula": "close / ts_delay(close, 5) - 1", "status": "draft", "version": 1})
    try:
        days = _bdays(date(2026, 3, 2), 80)
        service = FactorBacktestService(_Engine(_rows(days, lambda i, k: 100 + i * (k + 1) * 0.1, lambda i, k: k)))
        result = service.run_batch(FactorBatchConfig(factor_names=[fid], symbols=None, start=days[20], end=days[-1], n_groups=4, rebalance="daily", slippage_bps=0))
        item = result.results[0]
        assert item.label == "探针因子" and item.group == "自定义", (item.label, item.group, item.error)
    finally:
        registry.unregister_factor(fid)


def test_generation_change_returns_a_readable_409(monkeypatch):
    from app.api import backtest as backtest_api
    from app.enriched_generation import EnrichedGenerationUnavailableError

    def boom(self, *a, **k):
        raise EnrichedGenerationUnavailableError("enriched data changed while the snapshot was being read")

    monkeypatch.setattr(FactorBacktestService, "run", boom)
    monkeypatch.setattr(FactorBacktestService, "run_batch", boom)
    monkeypatch.setattr(backtest_api, "_get_engine", lambda request: SimpleNamespace())
    monkeypatch.setattr(backtest_api, "_settled_end", lambda request, end, asset_type: end)
    app = FastAPI()
    app.include_router(backtest_api.router)
    client = TestClient(app, raise_server_exceptions=False)
    r1 = client.post("/api/backtest/factor/run", json={"factor_name": "momentum_20d"})
    r2 = client.post("/api/backtest/factor/batch", json={"factor_names": ["momentum_20d"]})
    for r in (r1, r2):
        assert r.status_code == 409, r.text
        assert "指标数据正在发布更新" in r.json()["detail"]


def test_slippage_is_charged_by_actual_turnover():
    """静态因子 (分组从不换仓) 不应每期扣满额往返; 只在建仓时扣一次。"""
    days = _bdays(date(2026, 5, 1), 126)
    flat = _rows(days, lambda i, k: 100.0, lambda i, k: k)  # price flat, factor never changes
    r = _run(flat, start=days[0], end=days[-1], n_groups=4, rebalance="daily", slippage_bps=5)
    # one round trip of 2 * 5bp on entry only, not 125 of them (old: -11.8%)
    assert r.group_stats[0]["total_return"] == pytest.approx(-0.001, abs=1e-4)
    assert r.long_short_stats["total_return"] == pytest.approx(-0.001, abs=1e-4)
    assert r.config["cost_basis"] == "turnover"


def test_full_rotation_still_pays_a_round_trip_each_period():
    """每期整组换仓时与旧口径一致: 每期扣满额往返。"""
    days = _bdays(date(2026, 5, 1), 21)
    # factor ranking flips every day → Q1 and Q4 fully swap members each period
    rot = _rows(days, lambda i, k: 100.0, lambda i, k: k if i % 2 == 0 else -k)
    r = _run(rot, start=days[0], end=days[-1], n_groups=2, rebalance="daily", slippage_bps=5)
    periods = len(r.group_nav)
    assert r.group_stats[0]["total_return"] == pytest.approx((1 - 0.001) ** periods - 1, abs=1e-4)


def test_factor_candidate_accepts_cost_basis(tmp_path):
    """检验页「保存候选」会带 cost_basis; 候选存储白名单要认它, 否则保存必 400。"""
    from app.backtest.candidates import CandidateStore

    store = CandidateStore(tmp_path)
    item = store.create(kind="factor", name="动量候选", source_id="momentum_20d",
                        config={"factor_name": "momentum_20d", "slippage_bps": 5, "cost_basis": "turnover"}, metrics={}, data_as_of=None)
    assert item["config"]["cost_basis"] == "turnover"
