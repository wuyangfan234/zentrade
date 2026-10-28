"""自选机会 v4 (2026-10-06): 规则集版本、自选影子账本 (同票基线)、前向确认、许可链、自选表、盘前一条。"""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.market_clock import us_clock
from app.opportunity.card import OpportunityCard
from app.services import decision_desk as dd
from app.services import decision_scan_job as job
from app.services import entry_guard
from app.services import strategy_evaluation as se
from app.services import watchlist_v4 as wv

AS_OF = date(2026, 10, 6)


def _bars(sym, n=110, seed=0, end=AS_OF):
    rng = np.random.default_rng(seed)
    c = 50 * np.cumprod(1 + rng.normal(0.002, 0.02, n))
    d0 = end - timedelta(days=n - 1)
    return pl.DataFrame({"symbol": sym, "date": [d0 + timedelta(days=i) for i in range(n)], "open": c * 0.998,
                         "high": c * 1.02, "low": c * 0.98, "close": c, "volume": np.full(n, 2e6)})


# ── 规则集 ──

def test_ruleset_versions_are_frozen_and_identical_save_is_noop(tmp_path):
    assert wv.current_ruleset(tmp_path)["version"] == 0                  # 默认 R0 (未落盘)
    r1 = wv.save_ruleset(tmp_path, ["macd_golden", "ma_golden_cross"], {"risk_per_trade_pct": 1.0})
    assert r1["version"] == 1 and wv.current_ruleset(tmp_path)["enabled"] == ["ma_golden_cross", "macd_golden"]
    assert wv.save_ruleset(tmp_path, ["ma_golden_cross", "macd_golden"], {"risk_per_trade_pct": 1.0})["version"] == 1
    r2 = wv.save_ruleset(tmp_path, ["macd_golden"], {})
    assert r2["version"] == 2 and wv.versions(tmp_path) == [1, 2]
    assert json.loads((tmp_path / "rules" / "R1.json").read_text())["enabled"] == ["ma_golden_cross", "macd_golden"]
    assert wv.save_ruleset(tmp_path, ["macd_golden"], {"bogus": 9})["params"] == {"risk_per_trade_pct": 1.0}


# ── 影子账本 ──

def _signal(sym, frames, rule="macd_golden", stop=-0.06):
    card = dd.card_from_pick(rule, sym, frames[sym], as_of=AS_OF, stop_loss=stop)
    return {"symbol": sym, "card": card, "stop": stop}


def test_record_writes_rule_cards_and_one_same_symbol_baseline_per_stop(tmp_path):
    frames = {s: _bars(s, seed=i) for i, s in enumerate(["NVDA", "MU", "AAPL", "XYZ"])}
    wl = {"NVDA", "MU", "AAPL"}
    sig = {"macd_golden": [_signal("NVDA", frames), _signal("XYZ", frames)],          # XYZ 不在自选 → 不记
           "trend_breakout": [_signal("MU", frames, "trend_breakout", -0.08)]}
    added = wv.record(tmp_path, AS_OF, watchlist=wl, frames=frames, signals=sig, baseline_stops={-0.06, -0.08, None})
    rows = wv.load(tmp_path)
    assert added["macd_golden"] == 1 and added["trend_breakout"] == 1
    base = [r for r in rows if r["rule"] == wv.BASELINE_ID]
    assert {(r["symbol"], r["stop"]) for r in base} == {(s, st) for s in wl for st in (-0.06, -0.08, None)}
    again = wv.record(tmp_path, AS_OF, watchlist=wl, frames=frames, signals=sig, baseline_stops={-0.06, -0.08, None})
    assert again == {} and len(wv.load(tmp_path)) == len(rows)


def test_record_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(wv, "load", lambda d: (_ for _ in ()).throw(OSError("disk")))
    assert wv.record(tmp_path, AS_OF, watchlist={"A"}, frames={}, signals={}, baseline_stops=set()) == {}


def _fake_ledger(tmp_path, rule_r, base_r, months=4, per_month=40, rule="macd_golden", seed=3):
    """单笔结果 ~ N(均值, 1.2R) (真实 7 日卡的离散度); 规则卡与基线卡独立抽样。"""
    rng = np.random.default_rng(seed)
    rows = []
    for m in range(months):
        for k in range(per_month):
            day = (date(2026, 10, 6) + timedelta(days=31 * m + k % 20)).isoformat()
            sym = f"S{k % 5}"
            rows.append({"as_of": day, "rule": rule, "symbol": sym, "stop": -0.06,
                         "outcome": {"status": "closed", "r": float(rule_r + rng.normal(0, 1.2))}})
            rows.append({"as_of": day, "rule": wv.BASELINE_ID, "symbol": sym, "stop": -0.06,
                         "outcome": {"status": "closed", "r": float(base_r + rng.normal(0, 1.2))}})
    wv._save(tmp_path, rows)


def test_forward_uses_same_symbol_baseline_and_promotes_only_when_all_bars_met(tmp_path):
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    _fake_ledger(tmp_path, rule_r=0.30, base_r=0.30, months=4)          # 绝对 R 很高, 但和同票随机日一样 → 不晋级
    st = wv.status(tmp_path)
    assert st["n"] == 160 and abs(st["excess"]) < 0.3 and st["abs_mean"] > 0 and st["promoted"] is False
    _fake_ledger(tmp_path, rule_r=0.8, base_r=0.0, months=4)
    st = wv.status(tmp_path)
    assert st["promoted"] is True and st["tradable_rules"] == ["macd_golden"]
    _fake_ledger(tmp_path, rule_r=0.8, base_r=0.0, months=2, per_month=80)   # 月份不够 (样本够)
    assert wv.status(tmp_path)["promoted"] is False


def test_forward_threshold_tightens_with_every_ruleset_version(tmp_path):
    for k in range(5):
        wv.save_ruleset(tmp_path, ["macd_golden"] + (["ma_golden_cross"] if k % 2 else []), {})
    assert wv.status(tmp_path)["threshold"] == pytest.approx(0.05 / 5)


def test_forward_status_fail_closed(tmp_path, monkeypatch):
    wv._STATUS_CACHE.clear()
    monkeypatch.setattr(wv, "excess_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    st = wv.status(tmp_path)
    assert st["promoted"] is False and st["tradable_rules"] == []


# ── 许可链 (v4) ──

def _mode(tmp_path, mode="v4"):
    d = tmp_path / "decision_desk"
    d.mkdir(parents=True, exist_ok=True)
    (d / "evidence_mode.json").write_text(json.dumps({"mode": mode}))
    se._V4_CACHE.clear()


def test_v4_permission_only_for_enabled_promoted_rules_and_ignores_source_registry(tmp_path):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 1.0})
    reg = dd.SourceRegistry(tmp_path)
    reg.set_status("macd_golden", "observe")                             # 旧登记表不再参与
    assert job.source_permission(tmp_path, "macd_golden")["tradable"] is True    # 0.11.0 单一口径: 规则集内即可下单 (不等前向)
    _fake_ledger(tmp_path, rule_r=0.8, base_r=0.0, months=4)
    se._V4_CACHE.clear()
    p = job.source_permission(tmp_path, "macd_golden")
    assert p["tradable"] is True and p["risk_pct"] == pytest.approx(0.01)
    assert job.source_permission(tmp_path, "ma_golden_cross")["tradable"] is False   # 未勾选
    assert job.source_permission(tmp_path, "breakout")["tradable"] is False


def test_v4_permission_cache_reflects_ruleset_change(tmp_path):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    _fake_ledger(tmp_path, rule_r=0.8, base_r=0.0, months=4)
    assert job.source_permission(tmp_path, "macd_golden")["tradable"] is True
    wv.save_ruleset(tmp_path, ["ma_golden_cross"], {})
    assert job.source_permission(tmp_path, "macd_golden")["tradable"] is False


def test_v4_entry_guard_blocks_symbols_not_in_current_watchlist(tmp_path, monkeypatch):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    _fake_ledger(tmp_path, rule_r=0.8, base_r=0.0, months=4)
    us_clock._calendar_provider = None
    card = OpportunityCard(id="c", symbol="NVDA", setup_type="strategy:macd_golden", created_date=AS_OF,
                           entry_zone=(100.0, 101.0), invalidation=95.0, target_zone=(112.0, 114.0), risk_per_share=5.5,
                           reward_risk=2.1, evidence={"decision": {"tier": "proven", "risk_pct": 0.01, "tab": "watchlist",
                                                                   "tradable_sources": ["macd_golden"]}})
    now = datetime(2026, 10, 7, 11, tzinfo=us_clock.tz)
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA"})
    adm = entry_guard.evaluate(card, data_dir=tmp_path, now=now)
    # 2026-10-10: v4 的自选开仓只走机会卡下单簿 (watchlist_book), 决策台卡即使前向通过也只观察 (不重复开仓)
    assert adm.blockers == ("v4_book_only",) and adm.risk_pct == pytest.approx(0.01)
    monkeypatch.setattr(job, "_watchlist", lambda: {"MU"})
    assert "not_in_watchlist" in entry_guard.evaluate(card, data_dir=tmp_path, now=now).blockers
    monkeypatch.setattr(job, "_watchlist", lambda: set())                      # 读不到名单 = 禁入
    assert "not_in_watchlist" in entry_guard.evaluate(card, data_dir=tmp_path, now=now).blockers
    # 扫描时不在自选 (tab=pool) 的卡, 后来加进自选也不能开
    from dataclasses import replace as _rp
    pool_card = _rp(card, evidence={"decision": {**card.evidence["decision"], "tab": "pool"}})
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA"})
    assert "not_in_watchlist" in entry_guard.evaluate(pool_card, data_dir=tmp_path, now=now).blockers


def test_v1_v3_modes_unchanged_by_v4_code(tmp_path):
    _mode(tmp_path, "v1")
    (tmp_path / "decision_desk" / "evaluation.json").write_text(json.dumps({"sources": {"good": {"n": 1800, "mean": 0.13,
                                                                                              "ci_low": 0.02}}}))
    dd.SourceRegistry(tmp_path).set_status("good", "enabled")
    assert job.source_permission(tmp_path, "good")["tradable"] is True


# ── 盘前一条 ──

def test_preopen_message_is_numbers_only():
    from app.services import premarket_gap
    table = {"ruleset": {"version": 3}, "rows": [
        {"symbol": "NVDA", "signal": {"name": "MACD金叉"}, "trigger": 188.5, "invalidation": 181.2, "tradable": False},
        {"symbol": "LITE", "position": {"day": 4, "max_days": 7, "stop": 1010.0}},
        {"symbol": "AAPL", "signal": None, "position": None}]}
    res = {"gaps": {"NVDA": 0.9, "LITE": -0.4, "AAPL": 3.6}, "threshold_pct": 3.0}
    msg = premarket_gap.preopen_message(table, res, AS_OF, datetime(2026, 10, 6, 9, 0))
    body = msg["body"]
    assert "NVDA MACD金叉 触发 188.50 失效 181.20 盘前 +0.9% 记账" in body
    assert "LITE 持 4/7 止损 1010.00 盘前 -0.4%" in body
    assert "跳空 AAPL +3.6%" in body
    for word in ("能不能", "接下来", "建议", "我们", "。"):
        assert word not in body


def test_premarket_scan_records_all_gaps():
    from app.services import premarket_gap
    res = premarket_gap.scan(scope={"A": ["自选"], "B": ["自选"]}, prev_close={"A": 100, "B": 100}, mids={"A": 101, "B": 95})
    assert res["gaps"] == {"A": 1.0, "B": -5.0} and [a["symbol"] for a in res["alerts"]] == ["B"]


def test_settle_uses_shared_replay_and_marks_outcomes(tmp_path):
    frames = {s: _bars(s, seed=i, n=140, end=AS_OF + timedelta(days=30)) for i, s in enumerate(["NVDA", "MU"])}
    snap = {s: b.filter(pl.col("date") <= AS_OF) for s, b in frames.items()}
    sig = {"macd_golden": [_signal("NVDA", snap)]}
    wv.record(tmp_path, AS_OF, watchlist={"NVDA", "MU"}, frames=snap, signals=sig, baseline_stops={-0.06})
    full = pl.concat(list(frames.values()))
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: full.filter(pl.col("symbol").is_in(syms)))
    assert wv.settle(tmp_path, repo, today=AS_OF + timedelta(days=5)) == 0
    n = wv.settle(tmp_path, repo, today=AS_OF + timedelta(days=30))
    rows = wv.load(tmp_path)
    assert n == len(rows) and all(r["outcome"]["status"] in ("closed", "no_fill", "gap_expired") for r in rows)
    assert all("source" not in r for r in rows)                         # 不把临时字段写回账本


def test_watchlist_signals_cover_all_rules_and_s1s2_but_not_short_or_non_watchlist():
    from app.opportunity.engine import ScanResult
    frames = {s: _bars(s, seed=i) for i, s in enumerate(["NVDA", "MU", "XYZ"])}
    hits = {"macd_golden": [{"symbol": "NVDA", "rank": 30}, {"symbol": "XYZ", "rank": 1}],
            "phantom_4h": [{"symbol": "MU", "rank": 2}], "phantom_4h_short": [{"symbol": "NVDA", "rank": 1}]}
    s1 = OpportunityCard(id="b", symbol="MU", setup_type="breakout", created_date=AS_OF, entry_zone=(60.0, 60.5),
                         invalidation=57.0, target_zone=(68.0, 69.0), risk_per_share=3.25, reward_risk=2.3)
    out = job.watchlist_signals(AS_OF, watchlist={"NVDA", "MU"}, frames=frames, hits=hits,
                                stops={"macd_golden": -0.06}, stops_by_symbol={"phantom_4h": {"MU": -0.04}},
                                s1s2=ScanResult(AS_OF, cards=[s1]))
    assert {k: [x["symbol"] for x in v] for k, v in out.items()} == {"macd_golden": ["NVDA"], "phantom_4h": ["MU"],
                                                                     "breakout": ["MU"]}
    assert out["macd_golden"][0]["rank"] == 30 and out["phantom_4h"][0]["per_symbol_stop"] is True


def test_account_reset_archives_everything_and_restarts_from_initial_equity(tmp_path, monkeypatch):
    from app.ledger.core import Account, Ledger
    from app.services import account_reset as ar
    from app.services import alpaca_mirror as am
    monkeypatch.setattr(us_clock, "in_session", lambda *a: False)
    led = Ledger(data_dir=tmp_path / "ledger")
    led.init_account(Account())
    led.replay(Account())
    led.append("opportunity", {"card_id": "x", "symbol": "A", "state": "watch", "reason": "r", "trade_date": "2026-10-05"},
               at="2026-10-05T10:00:00-04:00")
    (tmp_path / "opportunities" / "cards").mkdir(parents=True)
    (tmp_path / "opportunities" / "cards" / "2026-10-05.json").write_text("{}")
    (tmp_path / "opportunities" / "book").mkdir(parents=True)                 # 机会卡下单簿 (2026-10-10) 也归档
    (tmp_path / "opportunities" / "book" / "2026-10-05.json").write_text("{}")
    calls = []
    m = am.AlpacaMirror(tmp_path, http=lambda method, path, body=None: calls.append((method, path)) or
                        ([{"symbol": "CORZ"}] if path == "/positions" else {}))
    m._state.update(started_seq=33, orders={"zt-81": {"cid": "zt-81"}})
    m._save()
    monkeypatch.setattr(am, "_default_http", lambda method, path, body=None: calls.append((method, path)) or
                        ([{"symbol": "CORZ"}] if path == "/positions" else {}))
    loop = SimpleNamespace(ledger=led, account=Account(), opp_svc=SimpleNamespace(bind_ledger=lambda l: None),
                           trader=SimpleNamespace(ledger=led, account=None, _cards={"x": 1}), breaker_status=1, _notify_seq=5)
    out = ar.reset(tmp_path, loop, note="test")
    assert out["events"] == 1 and "ledger" in out["moved"] and "opportunities/cards" in out["moved"]
    assert "opportunities/book" in out["moved"] and not (tmp_path / "opportunities" / "book").exists()
    assert (tmp_path / out["archive"] / "ledger" / "events.jsonl").exists()
    assert not (tmp_path / "ledger" / "events.jsonl").exists() and (tmp_path / "ledger" / "account.json").exists()
    assert loop.ledger.cash == Account().initial_equity and loop.trader._cards == {}
    st = json.loads((tmp_path / "alpaca_mirror" / "state.json").read_text())
    assert st["orders"] == {} and st["started_seq"] is None and st["cid_prefix"] != "zt"
    assert calls.index(("DELETE", "/orders")) < calls.index(("GET", "/positions"))
    assert am.AlpacaMirror(tmp_path, http=lambda *a, **k: {})._prefix() == st["cid_prefix"]   # 生成的前缀必须被镜像接受
    assert ("DELETE", "/positions/CORZ") in calls
    m2 = am.AlpacaMirror(tmp_path, http=lambda *a, **k: {})
    m2.start([])                       # 新台账空 → 起点 0
    m2.sync([{"seq": 1, "kind": "fill", "symbol": "NVDA", "side": "buy", "qty": 3, "price": 100, "at": "x"}])
    assert list(m2._state["orders"]) == [f"{st['cid_prefix']}-1"]
    monkeypatch.setattr(us_clock, "in_session", lambda *a: True)
    with pytest.raises(RuntimeError):
        ar.reset(tmp_path, loop)


def test_v4_bad_risk_param_fails_closed(tmp_path, monkeypatch):
    from app.services import strategy_evaluation as se
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    _fake_ledger(tmp_path, rule_r=0.6, base_r=0.0, months=4)
    cur = wv.rules_dir(tmp_path) / f"R{wv.current_ruleset(tmp_path)['version']}.json"
    d = json.loads(cur.read_text()); d["params"]["risk_per_trade_pct"] = "abc"; cur.write_text(json.dumps(d))
    se._V4_CACHE.clear(); wv._STATUS_CACHE.clear()
    ev = se._load_v4(tmp_path)
    assert all(not v["tradable"] or v["risk_pct"] == 0.0 for v in ev["sources"].values())
    from app.services import decision_desk as dd
    assert all(dd.source_weight(v)["risk_pct"] == 0.0 for v in ev["sources"].values())



# ── 对抗审查回归 (2026-10-06) ──

def _promoted(tmp_path, rules=("macd_golden",)):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, list(rules), {})
    _fake_ledger(tmp_path, rule_r=0.8, base_r=0.0, months=4)
    se._V4_CACHE.clear(); wv._STATUS_CACHE.clear()
    assert job.source_permission(tmp_path, "macd_golden")["tradable"] is True


@pytest.mark.parametrize("params", [{"risk_per_trade_pct": None}, {"risk_per_trade_pct": [1]}, {"risk_per_trade_pct": True},
                                    {"risk_per_trade_pct": float("nan")}, {"risk_per_trade_pct": 5}, "x"])
def test_malformed_ruleset_params_never_raise_and_never_trade(tmp_path, params):
    _promoted(tmp_path)
    cur = wv.rules_dir(tmp_path) / "R1.json"
    d = json.loads(cur.read_text()); d["params"] = params; cur.write_text(json.dumps(d))
    se._V4_CACHE.clear(); wv._STATUS_CACHE.clear()
    assert job.source_permission(tmp_path, "macd_golden")["tradable"] is False
    assert wv.permission(tmp_path)["tradable"] == set()


def test_malformed_enabled_list_never_raises(tmp_path):
    _promoted(tmp_path)
    cur = wv.rules_dir(tmp_path) / "R1.json"
    d = json.loads(cur.read_text()); d["enabled"] = [{"x": 1}, "macd_golden"]; cur.write_text(json.dumps(d))
    se._V4_CACHE.clear(); wv._STATUS_CACHE.clear()
    assert job.source_permission(tmp_path, "macd_golden")["tradable"] is False


@pytest.mark.parametrize("damage", ["pointer_garbage", "pointer_missing", "version_missing", "pointer_bool"])
def test_corrupt_rules_pointer_never_falls_back_to_defaults(tmp_path, damage):
    _promoted(tmp_path)
    rd = wv.rules_dir(tmp_path)
    if damage == "pointer_garbage":
        (rd / "current.json").write_text("{")
    elif damage == "pointer_missing":
        (rd / "current.json").unlink()
    elif damage == "version_missing":
        (rd / "R1.json").unlink()
    else:
        (rd / "current.json").write_text(json.dumps({"version": True}))
    se._V4_CACHE.clear(); wv._STATUS_CACHE.clear()
    rs = wv.current_ruleset(tmp_path)
    assert rs["enabled"] == [] and rs.get("invalid") is True
    assert job.source_permission(tmp_path, "macd_golden")["tradable"] is False


def test_unsaved_default_r0_is_display_only(tmp_path):
    _mode(tmp_path)
    _fake_ledger(tmp_path, rule_r=0.8, base_r=0.0, months=4)
    se._V4_CACHE.clear(); wv._STATUS_CACHE.clear()
    assert wv.current_ruleset(tmp_path)["version"] == 0
    assert wv.permission(tmp_path)["tradable"] == set()


def test_corrupt_shadow_ledger_blocks_promotion_and_is_not_rewritten(tmp_path):
    _promoted(tmp_path)
    p = wv.path(tmp_path)
    with p.open("a") as f:
        f.write("garbage\n1\n")
    before = p.read_bytes()
    se._V4_CACHE.clear(); wv._STATUS_CACHE.clear()
    st = wv.status(tmp_path)
    assert st["promoted"] is False and st["ledger_corrupt"] == 2         # 前向统计只读, 0.11.0 起不参与许可
    frames = {"NVDA": _bars("NVDA")}
    assert wv.record(tmp_path, AS_OF, watchlist={"NVDA"}, frames=frames, signals={}, baseline_stops=set()) == {}
    assert wv.settle(tmp_path, repo=None, today=AS_OF) == 0
    assert p.read_bytes() == before                                              # 不静默删行


def test_corrupt_evidence_mode_keeps_v4_watchlist_restriction(tmp_path):
    _promoted(tmp_path)
    (tmp_path / "decision_desk" / "evidence_mode.json").write_text("{")
    assert se.evidence_mode(tmp_path) == "v4"
    (tmp_path / "decision_desk" / "evidence_mode.json").write_text(json.dumps({"mode": "bogus"}))
    assert se.evidence_mode(tmp_path) == "v4"


def test_same_day_rescan_with_changed_stop_does_not_duplicate_rule_card(tmp_path):
    frames = {"NVDA": _bars("NVDA")}
    wv.record(tmp_path, AS_OF, watchlist={"NVDA"}, frames=frames, signals={"macd_golden": [_signal("NVDA", frames)]},
              baseline_stops={-0.06})
    again = wv.record(tmp_path, AS_OF, watchlist={"NVDA"}, frames=frames,
                      signals={"macd_golden": [_signal("NVDA", frames, stop=-0.08)]}, baseline_stops={-0.06})
    assert again.get("macd_golden", 0) == 0
    assert sum(1 for r in wv.load(tmp_path) if r["rule"] == "macd_golden") == 1


def test_v4_cache_returns_deep_copies(tmp_path):
    _promoted(tmp_path)
    a = se.load_evaluation(tmp_path)
    a["sources"]["macd_golden"]["forward"]["n"] = -1
    a["ruleset"]["enabled"].append("evil")
    b = se.load_evaluation(tmp_path)
    assert b["sources"]["macd_golden"]["forward"]["n"] != -1 and "evil" not in b["ruleset"]["enabled"]


def test_reset_aborts_without_local_changes_when_broker_flatten_fails(tmp_path, monkeypatch):
    from app.ledger.core import Account, Ledger
    from app.services import account_reset as ar
    from app.services import alpaca_mirror as am
    monkeypatch.setattr(us_clock, "in_session", lambda *a: False)
    led = Ledger(data_dir=tmp_path / "ledger"); led.init_account(Account()); led.replay(Account())
    led.append("opportunity", {"card_id": "x", "symbol": "A", "state": "watch", "reason": "r", "trade_date": "2026-10-05"},
               at="2026-10-05T10:00:00-04:00")

    def boom(method, path, body=None):
        if method == "DELETE" and path.startswith("/positions/"):
            raise RuntimeError("down")
        return [{"symbol": "CORZ"}] if path == "/positions" else {}
    monkeypatch.setattr(am, "_default_http", boom)
    m = am.AlpacaMirror(tmp_path, http=boom); m._state.update(started_seq=33, orders={"zt-81": {"cid": "zt-81"}}); m._save()
    loop = SimpleNamespace(ledger=led, account=Account(), opp_svc=SimpleNamespace(bind_ledger=lambda l: None),
                           trader=SimpleNamespace(ledger=led, account=None, _cards={}), breaker_status=None, _notify_seq=0)
    with pytest.raises(ar.BrokerFlattenError):
        ar.reset(tmp_path, loop)
    assert (tmp_path / "ledger" / "events.jsonl").exists() and not (tmp_path / "audit_backups").exists()
    assert json.loads((tmp_path / "alpaca_mirror" / "state.json").read_text())["started_seq"] == 33


def test_mirror_tick_and_reset_share_one_lock():
    """镜像任务在 daily_pipeline 中持 alpaca_mirror._lock 读台账+载状态; 重置先拿同一把锁 —— 源码层核对。"""
    import inspect
    from app.jobs import daily_pipeline
    from app.services import account_reset as ar
    assert "with mirror_lock:" in inspect.getsource(daily_pipeline)
    assert "with mlock:" in inspect.getsource(ar.reset)


# ── 做空规则 (规则集 short 列表 + SPY<50日线 门槛) ──

def test_ruleset_short_saves_round_trips_and_defaults(tmp_path):
    assert wv.current_ruleset(tmp_path)["short"] == []                     # 默认 R0
    r = wv.save_ruleset(tmp_path, ["macd_golden"], {}, short=["tv_halftrend"])
    assert r["short"] == ["tv_halftrend"] and wv.current_ruleset(tmp_path)["short"] == ["tv_halftrend"]
    r2 = wv.save_ruleset(tmp_path, ["macd_golden", "trend_breakout"], {})  # short=None → 沿用上一版
    assert r2["version"] == 2 and r2["short"] == ["tv_halftrend"]
    r3 = wv.save_ruleset(tmp_path, ["macd_golden"], {}, short=["macd_golden", "tv_ut_bot"])   # 不能做空的被滤掉
    assert r3["short"] == ["tv_ut_bot"]
    assert wv.save_ruleset(tmp_path, ["macd_golden"], {}, short=["tv_ut_bot"])["version"] == r3["version"]


def test_ruleset_bad_short_type_is_invalid_and_trades_nothing(tmp_path):
    wv.save_ruleset(tmp_path, ["macd_golden"], {}, short=["tv_ut_bot"])
    cur = wv.rules_dir(tmp_path) / "R1.json"
    d = json.loads(cur.read_text())
    d["short"] = "tv_ut_bot"
    cur.write_text(json.dumps(d))
    wv._STATUS_CACHE.clear()
    wv._PM_CACHE.clear()
    assert wv.current_ruleset(tmp_path).get("invalid") is True
    pm = wv.permission(tmp_path)
    assert pm["tradable"] == set() and pm["short_tradable"] == set()


def test_permission_short_tradable_follows_ruleset_and_old_files_have_none(tmp_path):
    _mode(tmp_path)
    assert wv.permission(tmp_path)["short_tradable"] == set()              # 默认 R0 不开空
    wv.save_ruleset(tmp_path, ["macd_golden"], {}, short=["tv_halftrend"])
    pm = wv.permission(tmp_path)
    assert pm["short_tradable"] == {"tv_halftrend"} and pm["risk_pct"] == pytest.approx(0.01)
    assert wv.rule_permission(pm, "tv_halftrend", "short") == (True, 0.01, "rule")
    assert wv.rule_permission(pm, "phantom_4h_short", "short")[0] is False
    cur = wv.rules_dir(tmp_path) / "R1.json"
    d = json.loads(cur.read_text())
    d.pop("short")
    cur.write_text(json.dumps(d))   # 旧文件没有 short 键
    wv._PM_CACHE.clear()
    wv._STATUS_CACHE.clear()
    assert wv.permission(tmp_path)["short_tradable"] == set()


# ── 做空大盘门槛 ──

def _spy_repo(closes, end):
    n = len(closes)
    d0 = end - timedelta(days=n - 1)
    df = pl.DataFrame({"symbol": wv.SHORT_GATE_INDEX, "date": [d0 + timedelta(days=i) for i in range(n)],
                       "open": closes, "high": [x * 1.01 for x in closes], "low": [x * 0.99 for x in closes],
                       "close": closes})
    return SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None:
                           df.filter(pl.col("symbol").is_in(syms) & (pl.col("date") <= e)))


def test_short_gate_true_false_and_missing(tmp_path):
    wv._SHORT_GATE_CACHE.clear()
    g = wv.short_gate(_spy_repo([100.0] * 60 + [90.0], AS_OF), AS_OF)        # 收盘 90 < 均值 ≈99.8
    assert g["ok"] is True and g["close"] == 90.0 and g["ma50"] > 90
    wv._SHORT_GATE_CACHE.clear()
    assert wv.short_gate(_spy_repo([90.0] * 60 + [110.0], AS_OF), AS_OF)["ok"] is False
    wv._SHORT_GATE_CACHE.clear()
    assert wv.short_gate(_spy_repo([100.0] * 60, AS_OF - timedelta(days=1)), AS_OF)["ok"] is None   # 缺当天
    wv._SHORT_GATE_CACHE.clear()
    assert wv.short_gate(_spy_repo([100.0] * 30, AS_OF), AS_OF)["ok"] is None                        # 不足 50 根
    broken = SimpleNamespace(get_daily_batch=lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert wv.short_gate(broken, AS_OF)["ok"] is None
    assert wv.short_gate(None, AS_OF)["ok"] is None


def test_short_gate_unsorted_rows_and_stray_symbol_still_correct(tmp_path):
    """批接口返回倒序行 + 混入非 SPY 行: 先筛 symbol 再排序再按 day 截断, 结果不受影响。"""
    wv._SHORT_GATE_CACHE.clear()
    closes = [100.0] * 60 + [90.0]
    n = len(closes)
    days = [AS_OF - timedelta(days=n - 1 - i) for i in range(n)]        # 倒序
    df = pl.DataFrame({"symbol": [wv.SHORT_GATE_INDEX] * n, "date": days, "close": closes})
    stray = pl.DataFrame({"symbol": ["QQQ"], "date": [AS_OF], "close": [1.0]})
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: pl.concat([df, stray]))
    g = wv.short_gate(repo, AS_OF)
    assert g["ok"] is True and g["close"] == 90.0 and g["ma50"] > 90


def test_record_and_settle_short_rows_and_short_baseline(tmp_path):
    n = 140
    c = 100 * np.cumprod(1 + np.full(n, -0.008))                            # 一路阴跌 → 空头走盈利
    d0 = AS_OF + timedelta(days=30) - timedelta(days=n - 1)
    frames_full = {s: pl.DataFrame({"symbol": s, "date": [d0 + timedelta(days=i) for i in range(n)],
                                    "open": c * 0.999, "high": c * 1.01, "low": c * 0.99, "close": c,
                                    "volume": np.full(n, 2e6)}) for s in ("NVDA", "MU")}
    snap = {s: b.filter(pl.col("date") <= AS_OF) for s, b in frames_full.items()}
    card = dd.card_from_pick("phantom_4h_short", "NVDA", snap["NVDA"], as_of=AS_OF, stop_loss=0.04, side="short")
    assert card is not None and card.is_short
    added = wv.record(tmp_path, AS_OF, watchlist={"NVDA", "MU"}, frames=snap, signals={}, baseline_stops={-0.06},
                      short_signals={"phantom_4h_short": [{"symbol": "NVDA", "card": card, "stop": 0.04,
                                                           "per_symbol_stop": True}]})
    rows = wv.load(tmp_path)
    sr = [r for r in rows if r["rule"] == "phantom_4h_short:short"]
    sb = [r for r in rows if r["rule"] == wv.SHORT_BASELINE_ID]
    assert added["phantom_4h_short:short"] == 1 and len(sr) == 1
    assert sr[0]["side"] == "short" and sr[0]["per_symbol_stop"] is True
    assert {r["symbol"] for r in sb} == {"NVDA", "MU"}
    assert all(r["side"] == "short" and r["stop"] is None for r in sb)
    assert all("side" not in r for r in rows if r["rule"] not in (wv.SHORT_BASELINE_ID, "phantom_4h_short:short"))
    full = pl.concat(list(frames_full.values()))
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: full.filter(pl.col("symbol").is_in(syms)))
    assert wv.settle(tmp_path, repo, today=AS_OF + timedelta(days=30)) == len(rows)
    rows = wv.load(tmp_path)
    closed_short = [r for r in rows if r.get("side") == "short" and r["outcome"]["status"] == "closed"]
    assert closed_short and all(r["outcome"]["r"] > 0 for r in closed_short)     # 空头按空头回放结算 (正 R)


def test_excess_rows_short_uses_short_baseline_and_stays_out_of_long_pool(tmp_path):
    rows = [
        {"as_of": "2026-10-08", "rule": "phantom_4h_short:short", "symbol": "NVDA", "side": "short",
         "stop": 0.04, "per_symbol_stop": True, "outcome": {"status": "closed", "r": 0.9}},
        {"as_of": "2026-10-08", "rule": wv.SHORT_BASELINE_ID, "symbol": "NVDA", "side": "short", "stop": None,
         "outcome": {"status": "closed", "r": 0.2}},
        {"as_of": "2026-10-08", "rule": "macd_golden", "symbol": "NVDA", "stop": -0.06,
         "outcome": {"status": "closed", "r": 0.5}},
        {"as_of": "2026-10-08", "rule": wv.BASELINE_ID, "symbol": "NVDA", "stop": -0.06,
         "outcome": {"status": "closed", "r": 0.1}},
    ]
    wv._save(tmp_path, rows)
    ex = {x["rule"]: x["ex"] for x in wv.excess_rows(tmp_path)}
    assert ex["phantom_4h_short:short"] == pytest.approx(0.7)      # 0.9 - 无止损空头基线 0.2 (不看止损档)
    assert ex["macd_golden"] == pytest.approx(0.4)
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    wv._STATUS_CACHE.clear()
    st = wv.status(tmp_path)
    assert st["n"] == 1 and "phantom_4h_short:short" in st["per_rule"]   # 空头行不进多头合并统计, 只进 per_rule


def test_table_falls_back_to_daily_close_without_signals_or_live_quotes(tmp_path, monkeypatch):
    from app.services import watchlist_table as wt
    _mode(tmp_path)
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA", "MU"})
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda syms: {})
    df = pl.concat([_bars("NVDA"), _bars("MU", seed=3)])
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: df.filter(pl.col("symbol").is_in(syms)))
    t = wt.build(data_dir=tmp_path, repo=repo, loop=None, engine=None)
    assert {r["symbol"]: r["price"] is not None for r in t["rows"]} == {"NVDA": True, "MU": True}
