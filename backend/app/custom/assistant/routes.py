"""AI 助手 HTTP 端点 — 薄层: 参数校验 + 流式响应映射。

POST /api/custom/assistant/chat      NDJSON 事件流(协议见模块 __init__)
GET  /api/custom/assistant/status     供应商配置状态(前端门控展示)
GET  /api/custom/assistant/suggests   空会话快捷指令
GET  /api/custom/assistant/health     助手体检 (最近一次结果; 每日 08:00 ET 自动跑, 只记录不推送)
POST /api/custom/assistant/health/run 立即体检一次
GET  /api/custom/assistant/skills                  技能列表 (内置 + 自建, 含启用状态与最近运行)
GET  /api/custom/assistant/skills/{name}           技能详情 (全文 + 检查 + 版本 + 最近运行)
POST /api/custom/assistant/skills/check            只检查不保存
POST /api/custom/assistant/skills                  保存 (新建或新版本)
POST /api/custom/assistant/skills/{name}/enabled   启用 / 停用
POST /api/custom/assistant/skills/{name}/rollback  回滚到指定版本
POST /api/custom/assistant/skills/{name}/delete    删除自建技能
写操作走全站同一权限中间件: 只读 API 密钥不能调用。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.custom.assistant import health as assistant_health
from app.custom.assistant import skills as assistant_skills
from app.custom.assistant import tools as assistant_tools
from app.custom.assistant.chat_service import chat_stream
from app.custom.assistant.streaming import ASSISTANT_TASK
from app.services.ai_provider import (
    ai_configured,
    current_ai_model,
    current_ai_provider,
    is_codex_cli_provider,
    task_profile,
)
from app.services.ndjson_heartbeat import with_heartbeat


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class AssistantContext(BaseModel):
    page: str = Field(default="", max_length=100)
    symbol: str = Field(default="", max_length=32)


class ChatRequest(BaseModel):
    messages: list[HistoryMessage] = Field(min_length=1, max_length=200)
    context: AssistantContext | None = None


class SkillText(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    note: str = Field(default="", max_length=80)


class SkillEnabled(BaseModel):
    enabled: bool = Field(strict=True)


class SkillRollback(BaseModel):
    version: int = Field(ge=1, le=10000, strict=True)


def _data_dir(request: Request):
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(503, "数据仓库未就绪")
    return repo.store.data_dir


def _known_tools() -> set[str]:
    return {s["function"]["name"] for s in assistant_tools.build_tool_schemas()}


def _skill_view(data_dir, skill) -> dict:
    runs = assistant_skills.recent_runs(data_dir, skill.name, limit=7)
    return {**skill.summary(), "last_run": runs[-1] if runs else None,
            "runs_ok": sum(1 for r in runs if r.get("ok")), "runs_n": len(runs)}


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/custom/assistant", tags=["custom-assistant"])

    @router.post("/chat")
    async def chat(request: Request, req: ChatRequest):
        engine = getattr(request.app.state, "strategy_engine", None)
        repo = getattr(request.app.state, "repo", None)
        data_dir = str(repo.store.data_dir) if repo is not None else None
        context = req.context.model_dump() if req.context else None

        async def gen():
            async for line in with_heartbeat(chat_stream(
                history=[m.model_dump() for m in req.messages],
                context=context,
                engine=engine,
                data_dir=data_dir,
                repo=repo,
                quote_service=getattr(request.app.state, "quote_service", None),
            )):
                yield line + "\n"

        return StreamingResponse(
            gen(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/status")
    def status() -> dict:
        configured = ai_configured()
        codex = is_codex_cli_provider()
        return {
            "configured": configured,
            "provider": current_ai_provider(),
            # 助手实际用的模型 = AI 设置「AI 模型分工」里「助手对话」一行 (未单独配置时 = 全局模型);
            # 全局 Codex CLI 时助手不可用 (无 tools 协议), 照旧显示全局模型
            "model": current_ai_model() if codex else task_profile(ASSISTANT_TASK)["model"],
            "supports_tools": configured and not codex,
        }

    @router.get("/suggests")
    def suggests() -> dict:
        return {"suggests": assistant_tools.QUICK_SUGGESTS}

    # ---------------- 体检 ----------------
    @router.get("/health")
    def health(request: Request) -> dict:
        return assistant_health.latest(_data_dir(request)) or {"at": None, "tools": [], "summary": None}

    @router.post("/health/run")
    async def health_run(request: Request) -> dict:
        return await assistant_health.run(request.app.state)

    # ---------------- 技能 ----------------
    @router.get("/skills")
    def skills_list(request: Request) -> dict:
        data_dir = _data_dir(request)
        items = assistant_skills.load_all(data_dir)
        known = _known_tools()
        rows = []
        for s in items:
            checks = assistant_skills.check(s, known_tools=known, others=items)
            rows.append({**_skill_view(data_dir, s),
                         "errors": sum(1 for c in checks if c["level"] == "error"),
                         "warnings": sum(1 for c in checks if c["level"] == "warn")})
        return {"skills": rows, "tools": sorted(known)}

    @router.post("/skills/check")
    def skills_check(request: Request, body: SkillText) -> dict:
        data_dir = _data_dir(request)
        try:
            skill = assistant_skills.parse(body.text)
        except ValueError as exc:
            return {"checks": [{"level": "error", "item": "格式", "detail": str(exc)}], "skill": None}
        others = assistant_skills.load_all(data_dir)
        return {"checks": assistant_skills.check(skill, known_tools=_known_tools(), others=others),
                "skill": skill.summary()}

    @router.post("/skills")
    def skills_save(request: Request, body: SkillText) -> dict:
        try:
            return assistant_skills.save(_data_dir(request), body.text, known_tools=_known_tools(), note=body.note)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/skills/{name}")
    def skills_get(request: Request, name: str) -> dict:
        data_dir = _data_dir(request)
        skill = assistant_skills.get(data_dir, name)
        if skill is None:
            raise HTTPException(404, f"没有技能「{name}」")
        others = assistant_skills.load_all(data_dir)
        return {**_skill_view(data_dir, skill), "text": skill.to_markdown(), "body": skill.body,
                "checks": assistant_skills.check(skill, known_tools=_known_tools(), others=others),
                "versions": assistant_skills.versions(data_dir, name) if skill.source == "user" else [],
                "runs": assistant_skills.recent_runs(data_dir, name, limit=7)}

    @router.post("/skills/{name}/enabled")
    def skills_enable(request: Request, name: str, body: SkillEnabled) -> dict:
        try:
            assistant_skills.set_enabled(_data_dir(request), name, body.enabled)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"name": name, "enabled": body.enabled}

    @router.post("/skills/{name}/rollback")
    def skills_rollback(request: Request, name: str, body: SkillRollback) -> dict:
        try:
            return assistant_skills.rollback(_data_dir(request), name, body.version)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @router.post("/skills/{name}/delete")
    def skills_delete(request: Request, name: str) -> dict:
        try:
            assistant_skills.delete(_data_dir(request), name)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"name": name, "deleted": True}

    return router
