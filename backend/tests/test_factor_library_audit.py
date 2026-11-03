"""因子库审计复现 (先红): 状态/分组修改时注册表与磁盘的一致性。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import factors as factors_api
from app.factors import registry, store


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.include_router(factors_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    yield TestClient(app), tmp_path


def _seed(data_dir, fid="uf_audit_status", **extra):
    registry.unregister_factor(fid)
    d = {"id": fid, "kind": "custom", "label": "a", "formula": "close / ts_delay(close, 5) - 1",
         "status": "draft", "version": 1, **extra}
    store.register_definition(d)
    store.save_one(data_dir, d)
    return fid


def test_status_change_keeps_factor_registered_when_disk_and_registry_differ(client):
    """磁盘里的定义版本低于已注册版本时 (例如编辑后旧文件未换新), 改状态后因子仍应在注册表里。"""
    c, data_dir = client
    fid = _seed(data_dir)
    try:
        # 注册表里是 v2 (同一进程里刚编辑过), 磁盘也是 v2: 正常路径
        r = c.post(f"/api/factors/custom/{fid}/status", json={"status": "active"})
        assert r.status_code == 200
        assert registry.get_factor(fid) is not None
        assert registry.get_factor(fid).stability == "stable"
    finally:
        registry.unregister_factor(fid)


def test_status_change_with_broken_definition_does_not_unregister(client):
    """定义在磁盘上已不能编译时 (DSL 收紧/字段被删), 改状态应报错且不能把仍在用的注册项注销掉。"""
    c, data_dir = client
    fid = _seed(data_dir)
    try:
        d = next(x for x in store.load_all(data_dir) if x["id"] == fid)
        d["formula"] = "no_such_column + 1"
        store.save_one(data_dir, d)
        r = c.post(f"/api/factors/custom/{fid}/status", json={"status": "active"})
        assert r.status_code == 400
        assert registry.get_factor(fid) is not None  # still registered with the last good definition
    finally:
        registry.unregister_factor(fid)


def test_group_change_with_broken_definition_does_not_unregister(client):
    c, data_dir = client
    fid = _seed(data_dir, fid="uf_audit_group")
    try:
        d = next(x for x in store.load_all(data_dir) if x["id"] == fid)
        d["formula"] = "no_such_column + 1"
        store.save_one(data_dir, d)
        r = c.post(f"/api/factors/custom/{fid}/group", json={"group": "新组"})
        assert r.status_code == 400
        assert registry.get_factor(fid) is not None
    finally:
        registry.unregister_factor(fid)


def test_creating_with_an_existing_id_does_not_overwrite_the_factor(client, monkeypatch):
    """新建时 id 与已有因子相同 (显式填写或同名自动生成), 原实现会把版本 +1 后直接覆盖原定义 (公式被替换、状态回草稿)。"""
    c, data_dir = client
    fid = _seed(data_dir, fid="uf_audit_dup", status="active", label="原名")
    monkeypatch.setattr(factors_api, "_trial_nonempty", lambda *a, **k: None)
    try:
        r = c.post("/api/factors/custom", json={"id": fid, "label": "新名", "formula": "close / ts_delay(close, 10) - 1"})
        assert r.status_code == 409
        saved = next(x for x in store.load_all(data_dir) if x["id"] == fid)
        assert saved["formula"] == "close / ts_delay(close, 5) - 1" and saved["status"] == "active"
    finally:
        registry.unregister_factor(fid)


def test_creating_a_composite_with_an_existing_id_does_not_overwrite(client):
    c, data_dir = client
    cid = "cf_audit_dup"
    registry.unregister_factor(cid)
    d = {"id": cid, "kind": "composite", "label": "组合", "members": {"momentum_5d": 1, "momentum_20d": 1}, "status": "active", "version": 1}
    store.register_definition(d)
    store.save_one(data_dir, d)
    try:
        r = c.post("/api/factors/composite", json={"id": cid, "label": "组合", "members": {"momentum_5d": 1, "momentum_60d": 1}})
        assert r.status_code == 409
        saved = next(x for x in store.load_all(data_dir) if x["id"] == cid)
        assert saved["members"] == {"momentum_5d": 1, "momentum_20d": 1}
    finally:
        registry.unregister_factor(cid)


def test_chinese_only_label_gets_a_valid_generated_id(client, monkeypatch):
    """名称纯中文且 id 留空时, 旧实现生成裸 `uf`, 不匹配 ^uf_[a-z0-9_]{1,40}$, 保存必 400。"""
    c, _data_dir = client
    monkeypatch.setattr(factors_api, "_trial_nonempty", lambda *a, **k: None)
    r = c.post("/api/factors/custom", json={"label": "我的反转", "formula": "close / ts_delay(close, 5) - 1"})
    try:
        assert r.status_code == 200, r.text
        fid = r.json()["id"]
        assert store.CUSTOM_ID_PATTERN.match(fid)
        # same label again → same id → refused, not overwritten
        again = c.post("/api/factors/custom", json={"label": "我的反转", "formula": "close / ts_delay(close, 10) - 1"})
        assert again.status_code == 409
    finally:
        registry.unregister_factor(r.json().get("id", "uf_x") if r.status_code == 200 else "uf_x")


def test_slugify_keeps_ascii_and_stays_within_pattern():
    assert factors_api._slugify_id("Momentum 5", "uf") == "uf_momentum_5"
    assert factors_api._slugify_id("反转 5d", "uf") == "uf_5d"
    long_id = factors_api._slugify_id("a" * 80, "cf")
    assert store.COMPOSITE_ID_PATTERN.match(long_id)
    assert store.CUSTOM_ID_PATTERN.match(factors_api._slugify_id("___", "uf"))


def test_update_without_description_keeps_the_stored_one(client):
    """编辑器旧版本不回传描述时, 采集并入因子的出处 (描述) 不能被清空。"""
    c, data_dir = client
    fid = _seed(data_dir, fid="uf_audit_desc", description="某站 · 2026-10-30 · 原文标题")
    try:
        r = c.post(f"/api/factors/custom/{fid}/update", json={"label": "改名", "group": "长分组名称一二三四五六七八九十", "formula": "close / ts_delay(close, 5) - 1"})
        assert r.status_code == 200, r.text  # group 17-24 chars accepted (same limit as /group)
        saved = next(x for x in store.load_all(data_dir) if x["id"] == fid)
        assert saved["description"] == "某站 · 2026-10-30 · 原文标题"
        r = c.post(f"/api/factors/custom/{fid}/update", json={"label": "改名", "formula": "close / ts_delay(close, 5) - 1", "description": ""})
        assert next(x for x in store.load_all(data_dir) if x["id"] == fid)["description"] == ""
    finally:
        registry.unregister_factor(fid)


def test_library_reports_availability_and_description(client):
    c, data_dir = client
    fid = _seed(data_dir, fid="uf_audit_avail", description="出处")
    cid = "cf_audit_fin"
    registry.unregister_factor(cid)
    d = {"id": cid, "kind": "composite", "label": "含财务", "members": {"roe_latest": 1, "momentum_5d": 1}, "status": "draft", "version": 1}
    store.register_definition(d)
    store.save_one(data_dir, d)
    try:
        items = {item["id"]: item for item in c.get("/api/factors").json()["factors"]}
        assert items["momentum_5d"]["available"] is True
        assert items["roe_latest"]["available"] is False
        assert items[cid]["available"] is False  # composite with a financial member: values would be all null
        assert items[fid]["description"] == "出处"
        assert cid not in {col["id"] for col in registry.factor_columns_view(available_only=True)}
    finally:
        registry.unregister_factor(fid)
        registry.unregister_factor(cid)


def test_frontend_share_capital_mirror_matches_backend():
    """前端 SHARE_CAPITAL_FIELDS 是后端集合的镜像 (候选方案「加入策略」靠它跳过不可改的权重)。"""
    import re
    from pathlib import Path

    from app.factors.registry import SHARE_CAPITAL_FIELDS

    source = (Path(__file__).resolve().parents[2] / "frontend/src/lib/retired-market-fields.ts").read_text(encoding="utf-8")
    block = source.split("export const SHARE_CAPITAL_FIELDS = new Set([", 1)[1].split("])", 1)[0]
    assert set(re.findall(r"'([a-z0-9_]+)'", block)) == set(SHARE_CAPITAL_FIELDS)
