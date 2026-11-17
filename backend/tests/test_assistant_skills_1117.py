"""AI 助手技能管理 (B 档) 与每日体检。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.assistant import chat_service, health
from app.custom.assistant import skills as skills_mod
from app.custom.assistant import tools as assistant_tools
from app.extensions.loader import configure_backend_extensions

KNOWN = {s["function"]["name"] for s in assistant_tools.build_tool_schemas()}

GOOD = """---
name: my-check
title: 我的检查
group: 每日
triggers: [/我的检查]
tools: [get_portfolio]
output: chat
pages: [持仓]
---
1. get_portfolio 读持仓, 写明数据时点。
"""


def test_builtin_skills_all_pass_checker() -> None:
    items = skills_mod.load_all(None)
    assert len(items) == 8
    assert [s.group for s in items][:3] == ["每日", "每日", "每日"]
    for s in items:
        assert s.source == "builtin"
        assert skills_mod.check(s, known_tools=KNOWN, others=items) == [], s.name


def test_checker_rejects_unknown_tool_bad_name_and_trigger_conflict() -> None:
    items = skills_mod.load_all(None)
    bad = skills_mod.parse(GOOD.replace("name: my-check", "name: My Check").replace("[get_portfolio]", "[get_secret]")
                  .replace("[/我的检查]", "[/持仓]"))
    checks = skills_mod.check(bad, known_tools=KNOWN, others=items)
    items_ = {c["item"] for c in checks if c["level"] == "error"}
    assert {"名称", "工具", "触发词"} <= items_
    assert any("持仓体检" in c["detail"] for c in checks)  # 冲突时说出被谁占用


def test_checker_warns_on_undeclared_tool_and_missing_as_of() -> None:
    sk = skills_mod.parse(GOOD.replace("1. get_portfolio 读持仓, 写明数据时点。", "1. get_portfolio 读持仓, 再 get_news 看新闻。"))
    checks = skills_mod.check(sk, known_tools=KNOWN, others=[])
    warns = {c["item"] for c in checks if c["level"] == "warn"}
    assert warns == {"工具", "数据时点"}
    assert not [c for c in checks if c["level"] == "error"]


def test_parse_errors_are_plain_language() -> None:
    with pytest.raises(ValueError, match="设置区"):
        skills_mod.parse("没有设置区的正文")
    with pytest.raises(ValueError, match="格式错误"):
        skills_mod.parse("---\nname: [\n---\nbody")


def test_save_versions_rollback_enable_delete(tmp_path: Path) -> None:
    r1 = skills_mod.save(tmp_path, GOOD, known_tools=KNOWN, note="first")
    assert r1["saved"] and r1["skill"]["version"] == 1
    r2 = skills_mod.save(tmp_path, GOOD.replace("写明数据时点", "按距止损排序, 写明数据时点"), known_tools=KNOWN)
    assert r2["skill"]["version"] == 2
    assert skills_mod.get(tmp_path, "my-check").body.startswith("1. get_portfolio 读持仓, 按距止损")
    vers = skills_mod.versions(tmp_path, "my-check")
    assert [v["version"] for v in vers] == [2, 1] and vers[0]["current"]
    skills_mod.rollback(tmp_path, "my-check", 1)
    assert skills_mod.get(tmp_path, "my-check").version == 1
    skills_mod.set_enabled(tmp_path, "my-check", False)
    assert skills_mod.get(tmp_path, "my-check").enabled is False
    skills_mod.set_enabled(tmp_path, "posture", False)  # 内置技能也能停用
    assert skills_mod.get(tmp_path, "posture").enabled is False
    skills_mod.delete(tmp_path, "my-check")
    assert skills_mod.get(tmp_path, "my-check") is None
    assert (tmp_path / "user_data" / "assistant_skills" / "my-check" / "v2.md").exists()  # 版本文件保留
    with pytest.raises(ValueError, match="内置技能只能停用"):
        skills_mod.delete(tmp_path, "posture")


def test_save_refuses_errors_and_builtin_names(tmp_path: Path) -> None:
    res = skills_mod.save(tmp_path, GOOD.replace("[get_portfolio]", "[get_secret]"), known_tools=KNOWN)
    assert res["saved"] is False
    assert not (tmp_path / "user_data" / "assistant_skills" / "my-check").exists()
    with pytest.raises(ValueError, match="内置技能名"):
        skills_mod.save(tmp_path, GOOD.replace("name: my-check", "name: posture").replace("/我的检查", "/持仓2"), known_tools=KNOWN)


def test_copy_of_builtin_with_new_name_is_allowed(tmp_path: Path) -> None:
    builtin = skills_mod.get(None, "posture")
    copy = skills_mod.Skill(**{**builtin.__dict__, "name": "posture-mine", "title": "我的持仓体检",
                      "triggers": ["/我的持仓"], "based_on": "posture", "source": "user"})
    res = skills_mod.save(tmp_path, copy.to_markdown(), known_tools=KNOWN)
    assert res["saved"], res["checks"]
    assert skills_mod.get(tmp_path, "posture-mine").based_on == "posture"


def test_match_only_on_leading_trigger_and_enabled(tmp_path: Path) -> None:
    assert skills_mod.match(tmp_path, "/持仓 今天怎么样").name == "posture"
    assert skills_mod.match(tmp_path, "我的 /持仓") is None
    assert skills_mod.match(tmp_path, "持仓怎么样") is None
    skills_mod.set_enabled(tmp_path, "posture", False)
    assert skills_mod.match(tmp_path, "/持仓") is None


# ---------------------------------------------------------------- 对话里用技能

def _stream_script(script, capture):
    async def fake_round(messages, tool_schemas, *, temperature=0.3, timeout=240.0):
        capture.setdefault("messages", []).append(list(messages))
        step = script.pop(0)
        for piece in step.get("text_pieces", []):
            yield {"type": "text", "delta": piece}
        yield {"type": "round_end", "tool_calls": step.get("tool_calls", []), "finish_reason": "stop"}
    return fake_round


async def test_trigger_injects_skill_steps_and_records_run(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(chat_service, "stream_openai_round", _stream_script([{"text_pieces": ["好的"]}], captured))
    lines = [line async for line in chat_service.chat_stream(
        history=[{"role": "user", "content": "/持仓 看一下"}], data_dir=str(tmp_path))]
    events = [json.loads(x) for x in lines]
    assert events[0] == {"type": "skill", "name": "posture", "title": "持仓体检"}
    system = captured["messages"][0][0]["content"]
    assert "本轮使用技能「持仓体检」" in system and "get_portfolio 读模拟持仓" in system
    runs = skills_mod.recent_runs(tmp_path, "posture")
    assert len(runs) == 1 and runs[0]["ok"] is True


async def test_plain_question_does_not_use_skill(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(chat_service, "stream_openai_round", _stream_script([{"text_pieces": ["好"]}], captured))
    events = [json.loads(x) async for x in chat_service.chat_stream(
        history=[{"role": "user", "content": "持仓怎么样"}], data_dir=str(tmp_path))]
    assert all(e["type"] != "skill" for e in events)
    assert "本轮使用技能" not in captured["messages"][0][0]["content"]
    assert skills_mod.recent_runs(tmp_path) == []


# ---------------------------------------------------------------- HTTP

class _Store:
    def __init__(self, d):
        self.data_dir = d


class _Repo:
    def __init__(self, d):
        self.store = _Store(d)


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    configure_backend_extensions(app)
    app.state.repo = _Repo(tmp_path)
    return TestClient(app)


def test_skills_http_roundtrip(tmp_path: Path) -> None:
    c = _client(tmp_path)
    listing = c.get("/api/custom/assistant/skills").json()
    assert len(listing["skills"]) == 8 and "get_portfolio" in listing["tools"]
    assert all(s["errors"] == 0 for s in listing["skills"])
    chk = c.post("/api/custom/assistant/skills/check", json={"text": GOOD.replace("[get_portfolio]", "[nope]")}).json()
    assert any(x["level"] == "error" for x in chk["checks"])
    saved = c.post("/api/custom/assistant/skills", json={"text": GOOD, "note": "t"}).json()
    assert saved["saved"] is True
    detail = c.get("/api/custom/assistant/skills/my-check").json()
    assert detail["versions"][0]["version"] == 1 and detail["text"].startswith("---")
    assert c.post("/api/custom/assistant/skills/posture/enabled", json={"enabled": False}).status_code == 200
    assert c.post("/api/custom/assistant/skills/posture/enabled", json={"enabled": "no"}).status_code == 422
    assert c.post("/api/custom/assistant/skills/posture/delete").status_code == 409
    assert c.get("/api/custom/assistant/skills/nope").status_code == 404
    assert c.post("/api/custom/assistant/skills/my-check/rollback", json={"version": 9}).status_code == 404


def test_skill_write_endpoints_blocked_for_read_only_keys() -> None:
    from app.services import api_keys

    for path in ("/api/custom/assistant/skills", "/api/custom/assistant/skills/posture/enabled",
                 "/api/custom/assistant/skills/posture/delete", "/api/custom/assistant/health/run"):
        assert api_keys.permission_error("read", "POST", path) is not None
    assert api_keys.permission_error("read", "GET", "/api/custom/assistant/skills") is None


# ---------------------------------------------------------------- 体检

async def test_health_run_writes_report_and_status_row(monkeypatch, tmp_path: Path) -> None:
    async def fake_exec(name, args, ctx):
        if name == "get_news":
            return {"ok": False, "error": "news store missing"}
        if name == "get_lots":
            return {"ok": True, "result": {"count": 0, "rows": []}}
        return {"ok": True, "result": {"rows": [1]}}

    monkeypatch.setattr(health.assistant_tools, "execute_assistant_tool", fake_exec)
    state = type("St", (), {"repo": _Repo(tmp_path), "quote_service": None, "strategy_engine": None})()
    report = await health.run(state)
    by = {r["name"]: r for r in report["tools"]}
    assert by["get_news"]["state"] == "error"
    assert by["get_lots"]["state"] == "ok"  # 空持仓提醒是正常业务状态
    assert by["run_backtest"]["state"] == "skip"
    assert report["counts"]["error"] == 1
    state_, summary = health.status_row(tmp_path)
    assert state_ == "error" and "个股新闻" in summary
    assert health.latest(tmp_path)["at"] == report["at"]


def test_health_status_idle_before_first_run(tmp_path: Path) -> None:
    state, summary = health.status_row(tmp_path)
    assert state == "idle" and "08:00 ET" in summary and "不推送" in summary


def test_health_job_registered_without_push() -> None:
    src = Path(__file__).resolve().parents[1].joinpath("app/jobs/daily_pipeline.py").read_text(encoding="utf-8")
    block = src[src.index("async def _assistant_health"):src.index('id="zen_assistant_health"')]
    assert "assistant_health.run" in block
    assert "notify" not in block and "enqueue" not in block
