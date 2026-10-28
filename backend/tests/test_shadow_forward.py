"""影子前向账本 (2026-10-05 优先级②): 不占资金; 快照冻结; 满期结算与 v2/v3 同一回放; 基线同日同止损; 失败不影响扫描。"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
import polars as pl

from app.opportunity.card import OpportunityCard
from app.services import shadow_forward as sf
from app.services import strategy_evaluation as se


def _bars(sym, n=110, drift=0.003, seed=0):
    rng = np.random.default_rng(seed)
    c = 50 * np.cumprod(1 + rng.normal(drift, 0.02, n))
    d0 = date(2026, 6, 1)
    return pl.DataFrame({"symbol": sym, "date": [d0 + timedelta(days=i) for i in range(n)], "open": c * 0.998,
                         "high": c * 1.02, "low": c * 0.98, "close": c, "volume": np.full(n, 2e6)})


def _frames(k=60):
    return {f"S{i:02d}": _bars(f"S{i:02d}", seed=i) for i in range(k)}


AS_OF = date(2026, 8, 19)  # 合成日K 06-01 起 110 天 → 信号日之后还有 ~30 天


def test_record_writes_top_n_per_source_plus_seeded_random_baseline(tmp_path):
    frames = _frames()
    hits = {"good": [{"symbol": f"S{i:02d}", "rank": i + 1} for i in range(15)],
            "phantom_4h_short": [{"symbol": "S01", "rank": 1}]}
    added = sf.record(tmp_path, AS_OF, frames=frames, hits=hits, stops={"good": -0.06, "phantom_4h_short": 0.03},
                      now=datetime(2026, 8, 19, 21, tzinfo=timezone.utc))
    rows = sf.load(tmp_path)
    assert added["good"] <= 10 and all(r["rank"] <= 10 for r in rows if r["source"] == "good")
    assert "phantom_4h_short" not in added                     # 空头本轮不记
    base = [r for r in rows if r["source"] == sf.BASELINE_ID]
    assert base and all(r["stop_loss"] == -0.06 for r in base)  # 基线用与来源相同的止损比例
    # 同一天再记一次: 不重复; 同一日期种子 → 同一批基线股票
    again = sf.record(tmp_path, AS_OF, frames=frames, hits=hits, stops={"good": -0.06})
    assert again == {} and len(sf.load(tmp_path)) == len(rows)
    other = tmp_path / "other"
    sf.record(other, AS_OF, frames=frames, hits={}, stops={})
    assert {r["symbol"] for r in sf.load(other)} == {r["symbol"] for r in base}


def test_native_cards_recorded_with_their_own_geometry(tmp_path):
    card = OpportunityCard(id="x", symbol="S03", setup_type="breakout", created_date=AS_OF, entry_zone=(60.0, 60.5),
                           invalidation=57.0, target_zone=(68.0, 69.0), risk_per_share=3.25, reward_risk=2.3)
    sf.record(tmp_path, AS_OF, frames=_frames(), hits={}, stops={}, native_cards=[("breakout", card)])
    row = next(r for r in sf.load(tmp_path) if r["source"] == "breakout")
    assert row["entry_zone"] == [60.0, 60.5] and row["invalidation"] == 57.0 and row["stop_loss"] == "native"


def test_settle_uses_same_replay_as_evaluation_and_leaves_unfinished_pending(tmp_path):
    frames = _frames(12)
    sf.record(tmp_path, AS_OF, frames={k: v.filter(pl.col("date") <= AS_OF) for k, v in frames.items()},
              hits={"good": [{"symbol": "S00", "rank": 1}]}, stops={"good": -0.06})
    full = pl.concat(list(frames.values()))
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: full.filter(pl.col("symbol").is_in(syms)))
    assert sf.settle(tmp_path, repo, today=AS_OF + timedelta(days=5)) == 0          # 未满 14 天不结算
    n = sf.settle(tmp_path, repo, today=AS_OF + timedelta(days=20))
    rows = sf.load(tmp_path)
    assert n == len(rows)
    r = next(x for x in rows if x["source"] == "good")
    after = full.filter((pl.col("symbol") == "S00") & (pl.col("date") > AS_OF)).sort("date")
    exp = se.simulate_native_daily(after["open"].to_numpy(), after["high"].to_numpy(), after["low"].to_numpy(),
                                   after["close"].to_numpy(), entry_zone=tuple(r["entry_zone"]),
                                   invalidation=r["invalidation"], target=tuple(r["target_zone"]), risk=r["risk"])
    assert r["outcome"]["status"] == exp["status"] and r["outcome"]["r"] == exp.get("r")
    s = sf.summary(tmp_path)
    assert s["sources"]["good"]["snapshots"] == 1 and s["sources"][sf.BASELINE_ID]["snapshots"] >= 1


def test_record_never_raises_into_the_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "load", lambda d: (_ for _ in ()).throw(OSError("disk")))
    assert sf.record(tmp_path, AS_OF, frames=_frames(3), hits={}, stops={}) == {}


def _full_bars(sym, days=110, seed=0, stop_after=None, split_on=None, ratio=1.0):
    b = _bars(sym, n=days, seed=seed)
    if stop_after is not None:
        b = b.head(stop_after)
    if split_on is not None:   # 复权日K: 拆股后整段 (含拆股前) 价格被除以 ratio
        b = b.with_columns([(pl.col(k) / ratio).alias(k) for k in ("open", "high", "low", "close")])
    return b


def test_settle_closes_names_that_stop_trading_mid_hold_instead_of_dropping_them(tmp_path):
    """持仓期间退市/停牌: 按最后一根收盘平仓 (delisted_exit), 不能永远待结算 (否则前向样本只剩活下来的)。"""
    live = {f"S{i:02d}": _bars(f"S{i:02d}", n=110, seed=i) for i in range(3)}
    dead = _full_bars("DEAD", seed=9, stop_after=82)          # 信号日后只再交易 2 天
    frames = {k: v.filter(pl.col("date") <= AS_OF) for k, v in {**live, "DEAD": dead}.items()}
    sf.record(tmp_path, AS_OF, frames=frames, hits={"good": [{"symbol": "DEAD", "rank": 1}]}, stops={"good": -0.06})
    full = pl.concat([*live.values(), dead])
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: full.filter(pl.col("symbol").is_in(syms)))
    sf.settle(tmp_path, repo, today=AS_OF + timedelta(days=30))
    r = next(x for x in sf.load(tmp_path) if x["source"] == "good")
    assert r["outcome"] is not None and r["outcome"]["status"] in ("closed", "no_fill", "gap_expired")
    if r["outcome"]["status"] == "closed":
        assert r["outcome"]["exit_reason"] == "delisted_exit"


def test_settle_rescales_bars_when_a_split_happens_after_the_snapshot(tmp_path):
    """快照后拆股 (复权日K整段 ÷2): 结算前按快照收盘换回同一基准, 结果与“没拆股”完全相同。"""
    b = _bars("SPL", n=110, seed=4)
    frames = {"SPL": b.filter(pl.col("date") <= AS_OF)}
    sf.record(tmp_path / "a", AS_OF, frames=frames, hits={"good": [{"symbol": "SPL", "rank": 1}]}, stops={"good": -0.06})
    sf.record(tmp_path / "b", AS_OF, frames=frames, hits={"good": [{"symbol": "SPL", "rank": 1}]}, stops={"good": -0.06})
    same = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: b.filter(pl.col("symbol").is_in(syms)))
    halved = b.with_columns([(pl.col(k) / 2).alias(k) for k in ("open", "high", "low", "close")])
    split = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: halved.filter(pl.col("symbol").is_in(syms)))
    sf.settle(tmp_path / "a", same, today=AS_OF + timedelta(days=30))
    sf.settle(tmp_path / "b", split, today=AS_OF + timedelta(days=30))
    ra = next(x for x in sf.load(tmp_path / "a") if x["source"] == "good")["outcome"]
    rb = next(x for x in sf.load(tmp_path / "b") if x["source"] == "good")["outcome"]
    assert ra["status"] == rb["status"] and ra.get("r") == pytest.approx(rb.get("r"), abs=1e-6)
    assert rb.get("price_rescaled") == pytest.approx(2.0)


def test_settle_rows_dispatches_short_rows_to_short_replay(tmp_path):
    """side="short" 的影子卡 (自选空头规则/基线) 按空头规则结算: 同一几何阴跌行情空头正 R, 按多头回放会是负 R。"""
    day = date(2026, 10, 5)
    days = [day - timedelta(days=35) + timedelta(days=i) for i in range(36)]
    closes = [100.0] * 16 + [100 * 0.985 ** i for i in range(1, 21)]
    df = pl.DataFrame({"symbol": "X", "date": days, "open": closes, "high": [x * 1.01 for x in closes],
                       "low": [x * 0.99 for x in closes], "close": closes})
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: df)
    as_of = days[15]                                   # 之后一路阴跌 ~1.5%/日
    row = {"as_of": as_of.isoformat(), "source": "t:short", "symbol": "X", "side": "short", "stop_loss": None,
           "entry_zone": [99.0, 100.0], "invalidation": 105.0, "target_zone": [80.0, 85.0], "risk": 5.0,
           "ref_close": None, "outcome": None}
    assert sf.settle_rows([row], repo, today=day) == 1
    assert row["outcome"]["status"] == "closed" and row["outcome"]["r"] > 0
    after = df.filter(pl.col("date") > as_of)
    exp = se.simulate_native_daily_short(after["open"].to_numpy(), after["high"].to_numpy(),
                                         after["low"].to_numpy(), after["close"].to_numpy(),
                                         entry_zone=(99.0, 100.0), invalidation=105.0, target=(80.0, 85.0), risk=5.0)
    assert row["outcome"]["r"] == exp.get("r") and row["outcome"]["status"] == exp["status"]
