"""证据 v3 (2026-10-05): 尺子本身的正确性 —— 成卡与生产逐张一致、退市平仓、池子不看未来、Holm、基线同口径、许可规则。"""
import random
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.services import decision_desk as dd
from app.services import evidence_v3 as ev3
from app.services import strategy_evaluation as se
from app.services import strategy_evaluation_v3 as v3


def _series(seed, n=90, gaps=False):
    rng = random.Random(seed)
    c = [rng.uniform(8, 400)]
    for _ in range(n - 1):
        c.append(max(0.5, c[-1] * (1 + rng.gauss(0.001, rng.uniform(0.005, 0.05)))))
    c = np.array(c, dtype=np.float32)
    h = (c * (1 + np.abs(np.random.default_rng(seed).normal(0, 0.02, n)))).astype(np.float32)
    lo = (c * (1 - np.abs(np.random.default_rng(seed + 1).normal(0, 0.02, n)))).astype(np.float32)
    o = ((h + lo) / 2).astype(np.float32)
    if gaps:
        for k in np.random.default_rng(seed).choice(n - 1, 4, replace=False):
            o[k] = h[k] = lo[k] = c[k] = np.nan
    return o, h, lo, c


@pytest.mark.parametrize("seed", range(300))
def test_card_geometry_matches_production_card_from_pick(seed):
    """数组版成卡与生产 card_from_pick 逐字段一致 (包括拒卡)。生产拿到该股全部已有日K 行 (缺失日没有行),
    这里同样用截至 t 的全部历史去缺失后喂给生产函数; 含大段缺失 (近 70 日缺 >10 根) 的反例 (2026-10-06 审查)。"""
    n = [90, 140, 260][seed % 3]
    o, h, lo, c = _series(seed, n=n, gaps=seed % 3 == 0)
    if seed % 7 == 0:                       # 近 70 日内一段长停牌 (缺 25 根)
        k = n - 40
        o[k:k + 25] = h[k:k + 25] = lo[k:k + 25] = c[k:k + 25] = np.nan
    if seed % 11 == 0:                      # 只缺高/低, 收盘在
        h[n - 5] = np.nan
    stop = [-0.05, -0.06, -0.08, None][seed % 4]
    t = len(c) - 1 - (seed % 5)
    b = pl.DataFrame({"date": [date(2025, 1, 1) + timedelta(days=i) for i in range(t + 1)],
                      "open": o[:t + 1], "high": h[:t + 1], "low": lo[:t + 1], "close": c[:t + 1],
                      "volume": np.ones(t + 1)}).filter(pl.col("close").is_not_nan())
    card = dd.card_from_pick("x", "X", b, as_of=date(2026, 6, 1), stop_loss=stop)
    geo = v3.card_geometry(o, h, lo, c, t, stop)
    if card is None:
        assert geo is None
    else:
        assert geo is not None
        lo_, hi_, inval, tz, rps = geo
        assert (lo_, hi_) == card.entry_zone
        assert inval == card.invalidation and rps == card.risk_per_share
        assert tz == card.target_zone


def test_delisted_position_closes_at_last_close_instead_of_dropping():
    """持仓期间退市: v2 记 open_at_end (样本被丢弃 = 幸存者偏差); v3 按最后一个交易日收盘平仓。"""
    o = np.array([10.0, 9.0, 8.0, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan])
    h = np.array([10.05, 9.2, 8.1] + [np.nan] * 6)
    l = np.array([9.9, 8.9, 7.9] + [np.nan] * 6)
    c = np.array([10.0, 9.0, 7.95] + [np.nan] * 6)
    kw = dict(entry_zone=(9.95, 10.05), invalidation=7.0, target=(13.0, 13.5), risk=3.0)
    assert se.simulate_native_daily(o, h, l, c, **kw)["status"] == "open_at_end"
    r = se.simulate_native_daily(o, h, l, c, last_index=2, **kw)
    assert r["status"] == "closed" and r["exit_reason"] == "delisted_exit"
    assert r["r"] == pytest.approx((7.95 * (1 - se.SLIP) - 10.0 * (1 + se.SLIP)) / 3.0 - se.FEE_R(3.0, 10.0 * (1 + se.SLIP)), abs=1e-3)


def test_last_index_default_is_byte_identical_to_v2():
    rng = np.random.default_rng(4)
    for _ in range(200):
        c = 50 * np.cumprod(1 + rng.normal(0, 0.03, 15))
        o, h, l = c * 0.995, c * 1.02, c * 0.98
        kw = dict(entry_zone=(c[0] * 0.99, c[0]), invalidation=c[0] * 0.93, target=(c[0] * 1.14, c[0] * 1.16),
                  risk=c[0] * 0.065)
        assert se.simulate_native_daily(o, h, l, c, **kw) == se.simulate_native_daily(o, h, l, c, last_index=None, **kw)


def test_pool_uses_only_past_data_and_matches_production_thresholds():
    T, N = 200, 3
    close = np.full((T, N), 10.0)
    vol = np.full((T, N), 3e6)            # $3000 万/天
    vol[:, 1] = 1e6                        # $1000 万/天 → 永远不进
    close[150:, 2] = 4.0                   # 跌破 $5 → 之后不进
    vol[160, 0] = 0.0                      # 单日无量不影响均值门槛
    ok = ev3.pool_mask(close, vol, np.array([True, True, True]))
    assert not ok[128, 0] and ok[129, 0]   # 第 130 根起才够历史
    assert not ok[:, 1].any()
    assert ok[149, 2] and not ok[150, 2]
    # 改未来数据不影响过去的池子
    close2 = close.copy(); close2[180:, 0] = 1.0
    assert (ev3.pool_mask(close2, vol, np.array([True] * 3))[:180] == ok[:180]).all()


def test_tradable_asset_mask_excludes_etf_and_leveraged_but_keeps_unknown_large_caps():
    inst = {"GOOG": {"instrument_type": "unknown", "name": "Alphabet Inc. Class C Capital Stock"},
            "TQQQ": {"instrument_type": "unknown", "name": "ProShares UltraPro QQQ"},
            "MUU": {"instrument_type": "etf", "is_etf": True, "name": "Direxion Shares ETF Trust"},
            "BILI": {"instrument_type": "adr", "name": "Bilibili Inc. American Depositary Shares"},
            "SIVB~20230328": {"instrument_type": "common_stock", "name": "SVB Financial Group (已退市)"}}
    m = ev3.tradable_asset_mask(list(inst), inst)
    assert dict(zip(inst, m)) == {"GOOG": True, "TQQQ": False, "MUU": False, "BILI": True, "SIVB~20230328": True}


def test_holm_is_step_down_and_stops_at_first_failure():
    # m=4: 门槛依次 .0125/.0167/.025/.05
    assert ev3.holm({"a": 0.001, "b": 0.012, "c": 0.02, "d": 0.5}) == {"a": True, "b": True, "c": True, "d": False}
    # c=0.03 > .025 失败 → 之后的 d=0.04 (虽 < .05) 也不过: 逐步下降法一旦失败即停
    assert ev3.holm({"a": 0.001, "b": 0.012, "c": 0.03, "d": 0.04}) == {"a": True, "b": True, "c": False, "d": False}
    # p 缺失算作 1.0 但仍计入 m: m=2 时 a 需 ≤ .025
    assert ev3.holm({"a": 0.04, "b": None}) == {"a": False, "b": False}
    assert ev3.holm({"a": 0.02, "b": None}) == {"a": True, "b": False}


def test_verdict_requires_all_four_preregistered_conditions():
    full = {"n": 500, "mean": 0.10}
    ex = {"mean": 0.08}
    hold = {"n": 150, "mean": 0.05}
    assert ev3.verdict(full, ex, hold, True)[0] == "proven"
    # v3.1: 探索档停用 (安慰剂校准: 纯随机来源误入探索 >10%) → 方向对但未过多重检验 = 只展示
    tier, reason = ev3.verdict(full, ex, hold, False)
    assert tier == "negative" and "运气" in reason
    assert ev3.verdict({"n": 500, "mean": -0.01}, ex, hold, True)[0] == "negative"     # 本身不赚钱
    assert ev3.verdict(full, ex, {"n": 150, "mean": -0.01}, True)[0] == "negative"     # 留出期没跑赢随机
    assert ev3.verdict(full, ex, {"n": 50, "mean": 0.2}, True)[0] == "negative"        # 留出期样本不够
    assert ev3.verdict({"n": 200, "mean": 0.3}, ex, hold, True)[0] == "negative"       # 全期样本不够


def _day(i):
    return (date(2022, 1, 3) + timedelta(days=i)).isoformat()


def test_cluster_boot_p_value_detects_real_edge_and_rejects_noise():
    rng = random.Random(1)
    noise = [{"signal_day": _day(i // 3), "excess": rng.gauss(0, 1)} for i in range(3000)]
    edge = [{"signal_day": _day(i // 3), "excess": rng.gauss(0.15, 1)} for i in range(3000)]
    assert ev3.cluster_boot(noise, "excess")["p"] > 0.01
    assert ev3.cluster_boot(edge, "excess")["p"] < 0.001


def test_monthly_clustering_widens_interval_when_days_within_a_month_are_correlated():
    """同月内结果相关 (持仓重叠/行情共振) 时, 按日聚类会低估波动; 按月聚类 (v3.1) 给出更宽的区间。"""
    rng = random.Random(2)
    rows = []
    for m in range(50):
        shock = rng.gauss(0, 0.3)
        for k in range(60):
            rows.append({"signal_day": (date(2022, 1, 1) + timedelta(days=31 * m + k % 28)).isoformat(),
                         "excess": shock + rng.gauss(0, 1)})
    by_day = ev3.cluster_boot(rows, "excess", unit="day")
    by_month = ev3.cluster_boot(rows, "excess", unit="month")
    assert (by_month["ci_high"] - by_month["ci_low"]) > 1.5 * (by_day["ci_high"] - by_day["ci_low"])


def test_baseline_uses_same_rules_and_same_stop_as_source():
    """基线: 同日同池随机抽样, 用与来源同一 Sim (同成卡同回放) 和同一止损比例; 不同止损分别缓存。"""
    calls = []

    class FakeSim:
        def run(self, i, t, stop):
            calls.append((i, t, stop))
            return {"status": "closed", "r": float(i)}

    pool = np.zeros((5, 300), dtype=bool)
    pool[3, 100:200] = True
    b = v3.Baseline(FakeSim(), pool)
    m = b.mean(3, -0.06)
    assert 100 <= m <= 199 and all(100 <= i < 200 and t == 3 and s == -0.06 for i, t, s in calls)
    n = len(calls)
    assert b.mean(3, -0.06) == m and len(calls) == n          # 缓存
    b.mean(3, -0.08)
    assert len(calls) == 2 * n                                 # 不同止损重新抽
    assert b.mean(2, -0.06) is None                            # 当天池子为空


def test_finalize_source_splits_holdout_and_reports_concentration():
    trades = [{"symbol": "A", "signal_day": f"2024-0{1 + i % 9}-1{i % 9}", "r": 0.1, "base_r": 0.0, "excess": 0.1,
               "exit_reason": "time_stop"} for i in range(60)]
    trades += [{"symbol": "B~20250101", "signal_day": f"2025-0{1 + i % 9}-1{i % 9}", "r": -0.2, "base_r": 0.0,
                "excess": -0.2, "exit_reason": "delisted_exit"} for i in range(40)]
    s = ev3.finalize_source(trades, holdout_start=date(2025, 1, 1))
    assert s["discovery"]["n"] == 60 and s["holdout"]["n"] == 40 and s["holdout"]["mean"] == -0.2
    assert s["delisted_trades"] == 40 and s["delisted_exits"] == 40
    assert set(s["by_year"]) == {"2024", "2025"}


def test_pool_uses_real_traded_price_not_split_adjusted_price():
    """反向并股: 真实价 $0.50 (复权后 $5.04) 不能进池; 真实成交额决定流动性。"""
    from types import SimpleNamespace
    T = 200
    adj_close = np.full((T, 1), 5.04)
    raw_close = np.full((T, 1), 0.5036)
    amount = np.full((T, 1), 3e7)                      # 真实成交额 $3000 万
    md = SimpleNamespace(close=adj_close, volume=amount / adj_close,
                         fields={"raw_close": raw_close, "amount": amount})
    c, v = ev3.real_price_volume(md)
    assert not ev3.pool_mask(c, v, np.array([True])).any()          # 真实价 < $5 → 不进
    md.fields["raw_close"] = np.full((T, 1), 6.0)
    c, v = ev3.real_price_volume(md)
    assert ev3.pool_mask(c, v, np.array([True]))[150:].all()        # 真实价 $6、成交额 $3000 万 → 进
    no_raw = SimpleNamespace(close=adj_close, volume=amount / adj_close, fields={})
    c, _ = ev3.real_price_volume(no_raw)
    assert (c == adj_close).all()                                   # 缺字段时退回复权价


def test_failed_run_keeps_previous_complete_file_and_event_errors_do_not_abort(tmp_path, monkeypatch):
    """运行中只写 .partial; 中途异常不覆盖上一份完整 evaluation_v3.json; 事件数据坏不拖垮策略来源。"""
    import json as _json
    out = tmp_path / "decision_desk" / "evaluation_v3.json"
    out.parent.mkdir(parents=True)
    out.write_text(_json.dumps({"sources": {"old": {"tier_v3": "negative"}}, "generated_at": "prev"}))
    monkeypatch.setattr(v3, "_instruments", lambda repo: {})

    class Boom(Exception):
        pass

    from app.services import strategy_evaluation_runner as runner
    monkeypatch.setattr(runner, "_signals_for", lambda *a, **k: (_ for _ in ()).throw(Boom("x")))
    fake_engine = SimpleNamespace(list_strategies=lambda: [{"id": "s1", "execution_backend": "matrix_native"}])
    monkeypatch.setattr(v3, "_append_hypothesis_ledger", lambda *a, **k: None)
    from app.backtest import strategy as bs
    monkeypatch.setattr(bs, "StrategyBacktestService", lambda *a, **k: None)
    from app.backtest import engine as be
    monkeypatch.setattr(be, "BacktestEngine", lambda *a, **k: None)
    # 单个策略失败 → 记 error, 运行完成后才替换正式文件 (这里完成了, 所以替换; 但内容是新一轮的)
    p = v3.run_v3(tmp_path, date(2022, 1, 13), date(2022, 2, 1), repo=SimpleNamespace(), engine=fake_engine,
                  out_path=out)
    assert "error" in p["sources"]["s1"]
    assert not out.with_name("evaluation_v3.partial.json").exists()
    # 取消: 正式文件保持上一份
    out.write_text(_json.dumps({"sources": {}, "generated_at": "prev2"}))
    p = v3.run_v3(tmp_path, date(2022, 1, 13), date(2022, 2, 1), repo=SimpleNamespace(), engine=fake_engine,
                  out_path=out, cancel=lambda: True)
    assert _json.loads(out.read_text())["generated_at"] == "prev2"


def test_event_readers_ignore_macos_metadata_files(tmp_path):
    """macOS 打包会带 ._xxx AppleDouble 元数据文件 (二进制); 事件读取只认 YYYY-MM-DD.json / YYYYqN_form345.zip。"""
    import json as _json
    from app.services import event_sources as es
    e = tmp_path / "earnings"
    e.mkdir()
    (e / "2023-02-22.json").write_text(_json.dumps({"date": "2023-02-22", "rows": [{"symbol": "NVDA", "eps": "$0.65",
                                                    "epsForecast": "$0.48", "surprise": "35.42", "noOfEsts": "12",
                                                    "time": "time-after-hours"}]}))
    (e / "._2023-02-22.json").write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \xa3")
    rows = es.earnings_rows(e)
    assert len(rows) == 1 and rows[0]["symbol"] == "NVDA"
    s = tmp_path / "sec"
    s.mkdir()
    (s / "._2023q1_form345.zip").write_bytes(b"\x00\x05\x16\x07")
    assert es.sec_coverage_end(s) is None
