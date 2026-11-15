"""挖掘候选发布: 默认名称 / 自定义名称 / 发布后改名 (0.56.x 易用性修复, 11-16)。

线上 11-16 实测: “ATR(14)原值 低”候选发布后叫“因子组合 · atr_14”, 发布时不能改名, 发布后也没有改名入口。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from test_mining_api import _FACTOR_SIGNATURE, _client, _successful_run

from app.api.strategy import router as strategy_router
from app.services import swing_candidates
from app.services.mining_candidates import (
    _replace_meta_name,
    clean_strategy_name,
    default_strategy_name,
)
from app.strategy.engine import StrategyEngine

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
PUBLISH = f"/api/backtest/mining/runs/result-run/candidates/{quote(_FACTOR_SIGNATURE, safe='')}/publish"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    client, store = _client(tmp_path)
    _successful_run(store)
    engine = StrategyEngine(strategy_dirs=[BUILTIN_DIR, tmp_path / "strategies" / "custom"])
    client.app.state.strategy_engine = engine
    client.app.state.monitor_engine = SimpleNamespace(invalidate_strategy_state=lambda: None)
    client.app.include_router(strategy_router)
    checks: list[dict] = []
    monkeypatch.setattr(
        swing_candidates, "request_check",
        lambda data_dir, repo, **kw: checks.append(kw) or {"started": True},
    )
    return SimpleNamespace(client=client, engine=engine, checks=checks, tmp_path=tmp_path)


def test_default_name_is_factor_label_plus_direction():
    assert default_strategy_name({
        "factor_names": ["atr_14"], "directions": {"atr_14": "low"},
    }) == "ATR(14)原值 低"
    assert default_strategy_name({
        "factor_names": ["atr_14", "vol_ratio_5d"],
        "directions": {"atr_14": "low", "vol_ratio_5d": "high"},
    }) == "ATR(14)原值 低 + 5日量比 高"
    # 注册表里没有的因子退回 id, 不抛错
    assert default_strategy_name({"factor_names": ["uf_x"], "directions": {}}) == "uf_x 高"


def test_result_exposes_default_name_for_publish_dialog(env):
    body = env.client.get("/api/backtest/mining/runs/result-run/result").json()
    assert body["candidates"][0]["display_name"] == "5日量比 高"


def test_publish_without_name_uses_default_and_checks_only_new_strategy(env):
    resp = env.client.post(PUBLISH)

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["name"] == "5日量比 高"
    assert env.engine.get(out["strategy_id"]).meta["name"] == "5日量比 高"
    # 发布后只验收这一条, 不再把全部系统发现策略重跑一遍 (那会独占重任务额度约 4 分钟)
    assert env.checks == [{"reason": "mining_publish", "strategy_ids": [out["strategy_id"]]}]


def test_publish_with_custom_name_then_republish_renames(env):
    first = env.client.post(PUBLISH, json={"name": "  低波  量比 "}).json()
    sid = first["strategy_id"]
    assert first["name"] == "低波 量比"
    assert env.engine.get(sid).meta["name"] == "低波 量比"

    # 已发布过的候选再次发布: 幂等, 传新名 = 改名; id / 打分不变
    again = env.client.post(PUBLISH, json={"name": "量比优选"})
    assert again.status_code == 200, again.text
    assert again.json() == {"ok": True, "strategy_id": sid, "name": "量比优选"}
    strategy = env.engine.get(sid)
    assert strategy.meta["name"] == "量比优选"
    assert strategy.matrix_strategy._scoring == {"vol_ratio_5d": 1.0}

    # 不带名称再发布一次: 保持现有名称, 不被默认名覆盖
    keep = env.client.post(PUBLISH).json()
    assert keep["name"] == "量比优选"


def test_publish_rejects_name_used_by_another_strategy(env):
    resp = env.client.post(PUBLISH, json={"name": "MA 金叉"})
    assert resp.status_code == 400
    assert "名称已被其他策略使用" in resp.json()["detail"]
    assert not list((env.tmp_path / "strategies" / "custom").glob("mined_factor_*.py"))


def test_rename_endpoint(env):
    sid = env.client.post(PUBLISH).json()["strategy_id"]

    ok = env.client.patch(f"/api/strategies/{sid}/name", json={"name": "我的量比"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == "我的量比"
    assert env.engine.get(sid).meta["name"] == "我的量比"
    detail = env.client.get(f"/api/strategies/{sid}").json()
    assert detail["name"] == "我的量比" and detail["renamable"] is True
    assert env.client.get("/api/strategies/ma_golden_cross").json()["renamable"] is False

    dup = env.client.patch(f"/api/strategies/{sid}/name", json={"name": "MA 金叉"})
    assert dup.status_code == 400 and "名称已被其他策略使用" in dup.json()["detail"]

    builtin = env.client.patch("/api/strategies/ma_golden_cross/name", json={"name": "改不了"})
    assert builtin.status_code == 400 and "只有挖掘发布的策略" in builtin.json()["detail"]

    missing = env.client.patch("/api/strategies/mined_factor_nope/name", json={"name": "x"})
    assert missing.status_code == 404

    bad = env.client.patch(f"/api/strategies/{sid}/name", json={"name": "a\nb"})
    assert bad.status_code == 400
    assert env.engine.get(sid).meta["name"] == "我的量比"


@pytest.mark.parametrize("raw,message", [
    ("", "不能为空"), ("   ", "不能为空"), ("x" * 41, "最多 40"), ("a\tb", "控制字符"), (3, "文字"),
])
def test_clean_strategy_name_rejects(raw, message):
    with pytest.raises(ValueError, match=message):
        clean_strategy_name(raw)


def test_replace_meta_name_keeps_rest_of_source_byte_identical():
    source = (
        '"""doc"""\n'
        "from x import y\n\n"
        "META = {'id': 'mined_factor_ab', 'name': '因子组合 · atr_14', 'tags': ['中文']}\n\n"
        "SCORING = {'atr_14': 1.0}\n"
    )
    out = _replace_meta_name(source, "ATR(14)原值 低")
    assert out == source.replace("因子组合 · atr_14", "ATR(14)原值 低")


def test_replace_meta_name_handles_multiline_meta():
    source = "A = 1\nMETA = {\n    'id': 's',\n    'name': '旧名',\n}\nB = 2\n"
    out = _replace_meta_name(source, "新名")
    assert out == "A = 1\nMETA = {'id': 's', 'name': '新名'}\nB = 2\n"
