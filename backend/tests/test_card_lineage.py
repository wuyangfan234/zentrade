"""方向 A 谱系: 出卡时冻结规则集版本 / 参数哈希 / 数据日 / 策略指纹 / 档位; 历史文件按当时的规则集与试用开关重建并标“重建”。"""
import json
from datetime import date

import pytest

from app.services import card_lineage as cl
from app.services import watchlist_v4 as wv


def _rs(tmp_path, v, enabled, created_at):
    d = wv.rules_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"R{v}.json").write_text(json.dumps({"version": v, "enabled": enabled, "params": {"risk_per_trade_pct": 1.0},
                                              "created_at": created_at}))


def test_mode_codes_follow_card_permission_semantics():
    pm = {"tradable": {"a"}, "risk_pct": 0.01, "trial_long": {"b"}, "short_tradable": {"s"}, "trial_risk_pct": 0.0025}
    en = {"a", "b", "c"}
    assert cl.mode_code(pm, "a", "long", en) == "O"
    assert cl.mode_code(pm, "b", "long", en) == "T"
    assert cl.mode_code(pm, "s", "short", en) == "T"
    assert cl.mode_code(pm, "c", "long", en) == "L"         # 规则集勾选、不可下单 → 记账
    assert cl.mode_code(pm, "z", "long", en) == "W"         # 只显示 → 观察
    assert cl.mode_code(pm, "x", "short", en) == "W"


def test_params_hash_is_stable_and_sensitive_to_stop_and_params():
    a = cl.params_hash({"risk_per_trade_pct": 1.0}, -0.06, False)
    assert a == cl.params_hash({"risk_per_trade_pct": 1.0}, -0.06, False) and len(a) == 16
    assert a != cl.params_hash({"risk_per_trade_pct": 1.0}, -0.08, False)
    assert a != cl.params_hash({"risk_per_trade_pct": 0.5}, -0.06, False)


def test_rule_family_covers_known_rules_and_unknown_is_other():
    assert cl.rule_family("trend_breakout") == "zt:breakout:1d"
    assert cl.rule_family("pullback_ma20_bounce") == "zt:pullback:1d"
    assert cl.rule_family("phantom_4h_short") == "zt:trend:4h"
    assert cl.rule_family("volume_price_surge") == "zt:volume_event:1d"
    assert cl.rule_family("oversold_bounce") == "zt:reversal:1d"
    assert cl.rule_family("no_such_rule") == "zt:other:1d"


def test_reconstruct_uses_ruleset_and_trial_switch_in_force_at_scan_time(tmp_path):
    _rs(tmp_path, 1, ["macd_golden"], "2026-10-06T12:36:02+08:00")
    _rs(tmp_path, 2, ["macd_golden", "phantom_4h"], "2026-10-08T18:15:59+08:00")
    (wv.rules_dir(tmp_path) / "trial_audit.jsonl").write_text(json.dumps(
        {"enabled": True, "short": ["phantom_4h_short"], "at": "2026-10-10T19:31:26+08:00"}) + "\n")
    early = {"as_of": "2026-09-29", "backfill": True, "generated_at": "2026-10-07T00:51:00+08:00"}
    m = cl.reconstruct(tmp_path, early)
    assert m["ruleset_version"] == 0 and m["lineage_src"] == "reconstructed" and m["strategy_fingerprint"] == {}
    assert m["mode"]({"rule": "macd_golden", "side": "long"}) == "L"
    assert m["mode"]({"rule": "phantom_4h", "side": "long"}) == "W"
    assert m["mode"]({"rule": "phantom_4h_short", "side": "short"}) == "W"       # 试用开关 10-10 才开
    mid = {"as_of": "2026-10-08", "generated_at": "2026-10-09T04:47:30+08:00"}
    m = cl.reconstruct(tmp_path, mid)
    assert m["ruleset_version"] == 2 and m["mode"]({"rule": "phantom_4h", "side": "long"}) == "L"
    late = {"as_of": "2026-10-12", "generated_at": "2026-10-13T04:47:30+08:00"}
    m = cl.reconstruct(tmp_path, late)
    assert m["mode"]({"rule": "macd_golden", "side": "long"}) == "T"
    assert m["mode"]({"rule": "phantom_4h_short", "side": "short"}) == "T"


def test_stamp_reads_current_permission_and_never_writes(tmp_path):
    (tmp_path / "decision_desk").mkdir()
    (tmp_path / "decision_desk" / "evidence_mode.json").write_text(json.dumps({"mode": "v4"}))
    wv.save_ruleset(tmp_path, ["macd_golden"], {})
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    lin, extra = cl.stamp(tmp_path, date(2026, 10, 12), rules=["macd_golden", "tv_ut_bot"])
    assert lin["ruleset_version"] == 1 and lin["contract"] == cl.CONTRACT and lin["data_asof"] == "2026-10-12"
    assert set(lin["strategy_fingerprint"]) == {"macd_golden", "tv_ut_bot"}
    row = {"rule": "macd_golden", "side": "long", "stop": -0.06}
    e = extra(row)
    assert e["mode"] == "O" and len(e["params_hash"]) == 16          # 0.11.0: 规则集内 = 下单
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_stamp_failure_returns_none_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(wv, "permission", lambda d: (_ for _ in ()).throw(RuntimeError("x")))
    assert cl.stamp(tmp_path, date(2026, 10, 12), rules=["a"]) == (None, None)
