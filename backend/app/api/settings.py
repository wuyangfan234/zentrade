"""设置 API — Key 配置 / 模式切换。

提供面向非开发者的 UI 配置入口,避免逼用户改 .env。
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app import secrets_store
from app.data_providers.custom.config import MAX_TIMEOUT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

def _sync_financial_scheduler_caps(app_state, capset) -> None:
    """财务调度器已删除 (P1-5): 保留签名兼容 routes.py 调用。"""
    return


@router.get("")
def get_settings() -> dict:
    """返回当前配置概况(Key 脱敏)。"""
    from app.config import settings
    from app.services import preferences
    from app.services.ai_provider import (
        ai_configured,
        current_ai_context_window,
        current_ai_max_output_tokens,
        current_ai_model,
        current_codex_command,
        current_codex_model,
        current_codex_reasoning_effort,
        current_openai_model,
        current_openai_reasoning_effort,
    )

    ai_provider = secrets_store.get_ai_config("ai_provider", settings.ai_provider)
    return {
        # 首次使用引导
        "onboarding_completed": preferences.get_onboarding_completed(),
        # AI 配置
        "ai_provider": ai_provider,
        "ai_base_url": secrets_store.get_ai_config("ai_base_url", settings.ai_base_url),
        "ai_api_key_masked": secrets_store.mask(secrets_store.get_ai_key()),
        "has_ai_key": bool(secrets_store.get_ai_key()),
        "ai_configured": ai_configured(ai_provider),
        "ai_model": current_ai_model(),
        "ai_openai_model": current_openai_model(),
        "ai_reasoning_effort": current_openai_reasoning_effort(),
        "ai_codex_model": current_codex_model(),
        "ai_codex_command": current_codex_command(),
        "ai_codex_reasoning_effort": current_codex_reasoning_effort(),
        "ai_user_agent": secrets_store.get_ai_config("ai_user_agent", settings.ai_user_agent),
        "ai_max_output_tokens": current_ai_max_output_tokens(),
        "ai_context_window": current_ai_context_window(),
    }



@router.get("/setup-checklist")
def get_setup_checklist() -> dict:
    """配置清单 (0.42.0): 数据源 / 新闻 / AI / 推送每项是否已配、不配的影响、去哪配。不含任何密钥原文。"""
    from app.services import setup_checklist
    return setup_checklist.build()


class SetupBannerIn(BaseModel):
    dismissed: bool = True


@router.post("/setup-checklist/banner")
def set_setup_banner(req: SetupBannerIn) -> dict:
    """首页「配置未完成」横幅的关闭状态 (存 preferences.json; 必需项缺失时横幅不可关闭, 由前端判断)。"""
    from app.services import preferences
    preferences.save({"setup_banner_dismissed": bool(req.dismissed)})
    return {"ok": True, "dismissed": bool(req.dismissed)}


class ServiceSecretIn(BaseModel):
    value: str = Field("", max_length=512)


# 没有插件卡片的外部服务: 在设置页单独填写, 写 secrets.json (优先于 .env)。值不回显。
_SERVICE_SECRETS = {"finnhub": "finnhub_api_key", "sec_user_agent": "sec_user_agent"}


def _probe_finnhub(key: str) -> tuple[bool, str]:
    """先探后存: 用候选 Key 查一次 AAPL 公司信息。网络错误也算失败 (不写入, 避免覆盖 .env 里有效的 Key)。"""
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request
    url = "https://finnhub.io/api/v1/stock/profile2?" + urllib.parse.urlencode({"symbol": "AAPL", "token": key})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ZenTrade"}), timeout=10) as r:
            body = _json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return False, f"Finnhub 拒绝该 Key (HTTP {e.code})"
    except Exception as e:
        return False, f"探测失败: {type(e).__name__}"
    return (True, "ok") if isinstance(body, dict) and body.get("ticker") else (False, "Finnhub 未返回数据, Key 可能无效")


def _valid_user_agent(value: str) -> bool:
    return "@" in value and all(32 <= ord(ch) < 127 for ch in value)


@router.put("/service-secret/{name}")
def save_service_secret(name: str, req: ServiceSecretIn) -> dict:
    from app import credentials
    field = _SERVICE_SECRETS.get(name)
    if field is None:
        raise HTTPException(status_code=404, detail=f"未知服务 '{name}'")
    value = (req.value or "").strip()
    if name == "sec_user_agent" and value and not _valid_user_agent(value):
        raise HTTPException(status_code=400, detail="SEC 要求包含联系邮箱且只用英文字符, 例如 ZenTrade you@example.com")
    if name == "finnhub" and value:
        ok, msg = _probe_finnhub(value)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
    if value:
        secrets_store.save({field: value})
    else:
        secrets_store.clear(field)
    spec = credentials.FINNHUB if name == "finnhub" else credentials.SEC_UA
    effective, source = credentials.lookup(spec)
    # configured / source 反映清除后实际生效的值 (清掉页面值后 .env 里的仍生效)
    return {"ok": True, "configured": bool(effective), "source": source,
            "masked": (secrets_store.mask(effective) if name == "finnhub" else effective) if effective else ""}


@router.post("/onboarding/complete")
def complete_onboarding() -> dict:
    """标记首次使用向导完成。

    写入 preferences.json,前端守卫据此判断是否需要再次展示向导。
    跨设备/清缓存安全 —— 状态落在后端文件,不依赖浏览器本地存储。
    """
    from app.services import preferences
    done = preferences.set_onboarding_completed(True)
    return {"ok": True, "onboarding_completed": done}


class AiSettingsIn(BaseModel):
    provider: str = "openai_compat"
    base_url: str = ""
    api_key: str | None = None
    model: str = ""
    reasoning_effort: str = Field(default="high", max_length=64)
    codex_command: str = ""
    codex_reasoning_effort: str = ""
    user_agent: str = ""
    max_output_tokens: int | None = None   # 输出上限, 钳制所有任务的 max_tokens
    context_window: int | None = None      # 输入上下文窗口上限 (约 token)


@router.post("/ai")
def save_ai_settings(req: AiSettingsIn) -> dict:
    """保存 AI 配置（全部持久化到 secrets.json）"""
    from app.config import settings
    from app.services.ai_provider import (
        OPENAI_PROVIDER,
        ai_configured,
        current_ai_context_window,
        current_ai_max_output_tokens,
        current_ai_model,
        current_ai_provider,
        current_codex_command,
        current_codex_model,
        current_codex_reasoning_effort,
        current_openai_model,
        current_openai_reasoning_effort,
        normalize_codex_command,
        normalize_codex_model,
        normalize_codex_reasoning_effort,
    )

    updates: dict = {}
    if req.provider:
        updates["ai_provider"] = req.provider
        settings.ai_provider = req.provider
    if req.provider == "codex_cli":
        updates["ai_codex_model"] = normalize_codex_model(req.model)
        try:
            codex_command = normalize_codex_command(req.codex_command)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        codex_reasoning_effort = normalize_codex_reasoning_effort(req.codex_reasoning_effort)
        updates["ai_codex_command"] = codex_command
        updates["ai_codex_reasoning_effort"] = codex_reasoning_effort
        settings.ai_codex_command = codex_command
        settings.ai_codex_reasoning_effort = codex_reasoning_effort
    else:
        if req.base_url:
            updates["ai_base_url"] = req.base_url
            settings.ai_base_url = req.base_url
        if req.api_key is not None:
            if req.api_key:
                updates["ai_api_key"] = req.api_key
                settings.ai_api_key = req.api_key
            else:
                secrets_store.clear("ai_api_key")
                settings.ai_api_key = ""
        if req.model:
            updates["ai_model"] = req.model
            settings.ai_model = req.model
        if req.provider == OPENAI_PROVIDER:
            updates["ai_reasoning_effort"] = req.reasoning_effort.strip()
    # user_agent 允许清空(回到默认浏览器 UA),故无条件持久化
    updates["ai_user_agent"] = req.user_agent
    settings.ai_user_agent = req.user_agent

    # 输出上限 / 输入上下文窗口 (数值配置, 缺省保持原值)
    if req.max_output_tokens is not None:
        if req.max_output_tokens <= 0:
            raise HTTPException(status_code=400, detail="输出上限必须为正整数")
        updates["ai_max_output_tokens"] = req.max_output_tokens
        settings.ai_max_output_tokens = req.max_output_tokens
    if req.context_window is not None:
        if req.context_window <= 0:
            raise HTTPException(status_code=400, detail="上下文窗口必须为正整数")
        updates["ai_context_window"] = req.context_window
        settings.ai_context_window = req.context_window

    if updates:
        secrets_store.save(updates)

    provider = current_ai_provider()
    return {
        "ok": True,
        "ai_provider": provider,
        "ai_model": current_ai_model(),
        "ai_openai_model": current_openai_model(),
        "ai_reasoning_effort": current_openai_reasoning_effort(),
        "ai_codex_model": current_codex_model(),
        "ai_codex_command": current_codex_command(),
        "ai_codex_reasoning_effort": current_codex_reasoning_effort(),
        "ai_configured": ai_configured(provider),
        "ai_max_output_tokens": current_ai_max_output_tokens(),
        "ai_context_window": current_ai_context_window(),
    }


class AiTaskOverrideIn(BaseModel):
    """单个任务的模型覆盖。空字符串 = 清除该字段 (回退全局); api_key=None = 不改动已存 key。

    model_ref: 引用模型库里的一个模型 (0.44.0); None = 不改动已存引用, "" = 取消引用。
    """
    model: str = Field(default="", max_length=128)
    base_url: str = Field(default="", max_length=300)
    api_key: str | None = Field(default=None, max_length=400)
    thinking: str = Field(default="", max_length=16)
    model_ref: str | None = Field(default=None, max_length=40)


def _ai_tasks_view() -> dict:
    """各任务实际生效配置 (不回显 key 原文, 只给是否已设与掩码)。"""
    from app.services.ai_provider import (
        AI_TASKS,
        CODEX_CLI_PROVIDER,
        current_codex_model,
        task_override,
        task_profile,
        task_reasoning_effort,
        task_route,
    )
    out = []
    for task, meta in AI_TASKS.items():
        ov = task_override(task)
        prof = task_profile(task)
        route = task_route(task)
        codex = route == CODEX_CLI_PROVIDER
        out.append({
            "task": task, "label": meta["label"], "default_thinking": meta["default_thinking"],
            "override": {"model": ov.get("model", ""), "base_url": ov.get("base_url", ""),
                         "thinking": ov.get("thinking", ""), "has_api_key": bool(ov.get("api_key")),
                         "api_key_masked": secrets_store.mask(ov.get("api_key") or "") if ov.get("api_key") else "",
                         "model_ref": ov.get("model_ref", "")},
            # reasoning_effort = 首次请求实际发送的思考档位 ('' = 不指定, 模型默认; 网关不认时自动去掉重试)
            # route = api (OpenAI 兼容接口) / codex_cli (全局 Codex 且本任务没单独填模型/Base URL/Key)
            "effective": {"model": current_codex_model() if codex else prof["model"],
                          "base_url": "" if codex else prof["base_url"], "thinking": prof["thinking"],
                          "reasoning_effort": task_reasoning_effort(task), "route": route,
                          "overridden": prof["overridden"],
                          "model_ref": prof.get("model_ref", ""),
                          "model_ref_name": prof.get("model_ref_name", ""),
                          "model_ref_missing": bool(prof.get("model_ref_missing"))},
        })
    return {"tasks": out}


@router.get("/ai/tasks")
def get_ai_tasks() -> dict:
    """按任务选模型 (2026-10-02 产品决策: 新闻翻译打标用 Kimi 关思考, 个股分析等沿用全局)。"""
    return _ai_tasks_view()


@router.post("/ai/tasks/{task}")
def save_ai_task(task: str, req: AiTaskOverrideIn) -> dict:
    from app.services.ai_provider import AI_TASKS, THINKING_MODES
    if task not in AI_TASKS:
        raise HTTPException(status_code=404, detail="未知任务")
    if req.thinking and req.thinking not in THINKING_MODES:
        raise HTTPException(status_code=400, detail=f"thinking 只能是 {' / '.join(THINKING_MODES)} 之一")
    if req.base_url and not req.base_url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="base_url 需以 http(s):// 开头")
    from app.services.ai_provider import ai_model_entry
    all_ov = dict(secrets_store.load().get("ai_task_overrides") or {})
    cur = dict(all_ov.get(task) or {})
    if req.model_ref is not None:
        ref = req.model_ref.strip()
        if ref:
            if ai_model_entry(ref) is None:
                raise HTTPException(status_code=400, detail=f"模型库里没有 '{ref}'")
            cur["model_ref"] = ref
        else:
            cur.pop("model_ref", None)
    for field in ("model", "base_url", "thinking"):
        val = getattr(req, field).strip()
        if val:
            cur[field] = val
        else:
            cur.pop(field, None)
    if req.api_key is not None:
        if req.api_key.strip():
            cur["api_key"] = req.api_key.strip()
        else:
            cur.pop("api_key", None)
    if cur:
        all_ov[task] = cur
    else:
        all_ov.pop(task, None)
    secrets_store.save({"ai_task_overrides": all_ov})
    return _ai_tasks_view()


# ── 模型库 (0.44.0): 同时登记多个模型, 任务按 id 引用 ──

class AiModelIn(BaseModel):
    """模型库条目。api_key=None = 不改动已存 key; "" = 清除 (回退全局 key)。"""
    name: str = Field(..., min_length=1, max_length=60)
    base_url: str = Field(default="", max_length=300)
    model: str = Field(..., min_length=1, max_length=128)
    api_key: str | None = Field(default=None, max_length=400)


def _ai_models_view() -> dict:
    from app.services.ai_provider import ai_models
    overrides = secrets_store.load().get("ai_task_overrides") or {}
    used: dict[str, list[str]] = {}
    if isinstance(overrides, dict):
        for task, ov in overrides.items():
            if isinstance(ov, dict) and ov.get("model_ref"):
                used.setdefault(ov["model_ref"], []).append(task)
    return {"models": [
        {"id": m["id"], "name": m.get("name") or m["id"], "base_url": m.get("base_url", ""),
         "model": m.get("model", ""), "has_api_key": bool(m.get("api_key")),
         "api_key_masked": secrets_store.mask(m["api_key"]) if m.get("api_key") else "",
         "used_by": sorted(used.get(m["id"], []))}
        for m in ai_models()
    ]}


def _new_model_id(name: str, taken: set[str]) -> str:
    import re as _re
    import secrets as _secrets
    base = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "model"
    if not base[0].isalnum():
        base = "m" + base
    cand = base
    while cand in taken:
        cand = f"{base}-{_secrets.token_hex(2)}"
    return cand


@router.get("/ai/models")
def list_ai_models() -> dict:
    return _ai_models_view()


@router.post("/ai/models")
def create_ai_model(req: AiModelIn) -> dict:
    from app.services.ai_provider import ai_models
    if req.base_url and not req.base_url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="base_url 需以 http(s):// 开头")
    models = ai_models()
    if len(models) >= 30:
        raise HTTPException(status_code=400, detail="模型库最多 30 个")
    mid = _new_model_id(req.name, {m["id"] for m in models})
    entry = {"id": mid, "name": req.name.strip(), "base_url": req.base_url.strip(), "model": req.model.strip()}
    if req.api_key and req.api_key.strip():
        entry["api_key"] = req.api_key.strip()
    secrets_store.save({"ai_models": [*models, entry]})
    return {**_ai_models_view(), "created": mid}


@router.put("/ai/models/{model_id}")
def update_ai_model(model_id: str, req: AiModelIn) -> dict:
    from app.services.ai_provider import ai_models
    if req.base_url and not req.base_url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="base_url 需以 http(s):// 开头")
    models = ai_models()
    for m in models:
        if m["id"] == model_id:
            m["name"] = req.name.strip()
            m["base_url"] = req.base_url.strip()
            m["model"] = req.model.strip()
            if req.api_key is not None:
                if req.api_key.strip():
                    m["api_key"] = req.api_key.strip()
                else:
                    m.pop("api_key", None)
            secrets_store.save({"ai_models": models})
            return _ai_models_view()
    raise HTTPException(status_code=404, detail="模型不存在")


@router.delete("/ai/models/{model_id}")
def delete_ai_model(model_id: str) -> dict:
    from app.services.ai_provider import ai_models
    models = ai_models()
    if not any(m["id"] == model_id for m in models):
        raise HTTPException(status_code=404, detail="模型不存在")
    view = _ai_models_view()
    used = next((m["used_by"] for m in view["models"] if m["id"] == model_id), [])
    if used:
        from app.services.ai_provider import AI_TASKS
        labels = "、".join(AI_TASKS.get(t, {}).get("label", t) for t in used)
        raise HTTPException(status_code=409, detail=f"仍被任务引用: {labels}; 先在「AI 模型分工」里改掉再删")
    secrets_store.save({"ai_models": [m for m in models if m["id"] != model_id]})
    return _ai_models_view()


@router.post("/ai/models/{model_id}/test")
async def test_ai_model(model_id: str) -> dict:
    """用该库模型发一句最小请求, 验证地址/Key/模型名可用。"""
    from app.services.ai_provider import _openai_client, ai_model_entry, model_profile
    entry = ai_model_entry(model_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    prof = model_profile(entry)
    if not prof["api_key"]:
        return {"ok": False, "error": "没有可用的 API Key (库模型和全局都未配置)"}
    try:
        client = _openai_client(prof["api_key"], 20.0, base_url=prof["base_url"])
        resp = await client.chat.completions.create(
            model=prof["model"], messages=[{"role": "user", "content": "Reply exactly: OK"}], max_tokens=16,
        )
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        return {"ok": True, "model": prof["model"], "response": text[:80]}
    except Exception as e:
        return {"ok": False, "error": (str(e) or type(e).__name__)[:300]}


# ── 对外 API 密钥 (0.44.0): 只认网页登录会话, 密钥本身不能管理密钥 (中间件已拦) ──

class ApiKeyCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    scope: Literal["full", "read"] = "read"
    expires_days: int | None = Field(default=None, ge=1, le=3650)


@router.get("/api-keys")
def list_api_keys() -> dict:
    from app.services import api_keys
    return {"keys": api_keys.list_keys()}


@router.post("/api-keys")
def create_api_key(req: ApiKeyCreateIn) -> dict:
    """生成密钥。明文 token 只在本次响应里出现一次, 服务端只存摘要。"""
    from app.services import api_keys
    try:
        info, token = api_keys.create_key(req.name, req.scope, req.expires_days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"key": info, "token": token, "keys": api_keys.list_keys()}


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: str) -> dict:
    from app.services import api_keys
    if not api_keys.revoke_key(key_id):
        raise HTTPException(status_code=404, detail="密钥不存在")
    return {"keys": api_keys.list_keys()}


# ── 新版本检查 (0.44.0) ──

@router.get("/update-check")
def get_update_check(force: bool = False) -> dict:
    from app.services import update_check
    return update_check.check(force=force)


@router.delete("/ai")
def clear_ai_settings() -> dict:
    """一键清空 AI 配置(provider / base_url / api_key / model)。

    保留 ai_user_agent —— 自定义请求头与凭证解耦,清空凭证不影响绕过 CDN 拦截的设置。
    """
    from app.config import settings

    secrets_store.clear(
        "ai_provider",
        "ai_base_url",
        "ai_api_key",
        "ai_model",
        "ai_reasoning_effort",
        "ai_codex_model",
        "ai_codex_command",
        "ai_codex_reasoning_effort",
    )
    secrets_store.clear("ai_provider", "ai_base_url", "ai_api_key", "ai_model", "ai_codex_command", "ai_codex_reasoning_effort", "ai_max_output_tokens", "ai_context_window")
    # 同步重置运行时内存(provider 回默认值,其余置空)
    settings.ai_provider = "openai_compat"
    settings.ai_base_url = ""
    settings.ai_api_key = ""
    settings.ai_model = ""
    settings.ai_codex_command = "codex"
    settings.ai_codex_reasoning_effort = ""
    settings.ai_max_output_tokens = 16384
    settings.ai_context_window = 128000

    return {"ok": True}



# ===== 偏好设置 =====

def _realtime_allowed() -> bool:
    """当前档位是否允许实时行情(none/free 不允许)。"""
    from app.services.quote_service import QuoteService
    return QuoteService.is_realtime_allowed()


def _minute_history_days() -> int | None:
    """当前分钟源的 1 分钟历史深度(交易日); None = 深历史基准。

    provider 可选类属性 minute_history_days 声明 (如 stock-sdk = 5,
    免费分时接口只保留最近 5 个交易日); 未声明时视为深历史。
    前端分时档位/默认值据此收窄。
    """
    from app.services import kline_sync, preferences
    provider_name = preferences.get_minute_data_provider()
    provider, fallback, _err = kline_sync._resolve_minute_provider(provider_name)
    if fallback or provider is None:
        return None
    return getattr(provider, "minute_history_days", None)


class MinuteSyncPrefs(BaseModel):
    minute_sync_enabled: bool
    minute_sync_days: int = 5
    # 单段大小(交易日),None 表示不修改现有值。范围 [5, 30],默认 20。
    minute_sync_segment_days: int | None = None


class DataProvidersIn(BaseModel):
    daily_data_provider: str | None = None
    adj_factor_provider: str | None = None
    minute_data_provider: str | None = None
    full_minute_data_provider: str | None = None
    realtime_data_provider: str | None = None
    financial_data_provider: str | None = None


class PluginKeyIn(BaseModel):
    plugin: str
    api_key: str


class DataSourceJobTimeoutPrefs(BaseModel):
    data_source_job_timeout_s: int = Field(ge=60)
    data_source_long_job_timeout_s: int = Field(ge=60)


class MinuteBatchCompressPrefs(BaseModel):
    minute_batch_compress: bool


class DailyBatchCompressPrefs(BaseModel):
    daily_batch_compress: bool


class DatasetConfigIn(BaseModel):
    url: str
    method: str = "GET"
    batch: int | None = None
    rpm: int | None = None
    response_path: str = ""
    field_map: dict[str, str] = {}
    transforms: dict[str, str] = {}
    symbols_param: str = "symbols"
    start_param: str = "start_time"
    end_param: str = "end_time"
    asset_type_param: str | None = None
    freq_param: str | None = None
    timeout: float | None = Field(
        default=None,
        gt=0,
        le=MAX_TIMEOUT,
        allow_inf_nan=False,
    )


class AuthConfigIn(BaseModel):
    type: str = "none"
    token_env: str | None = None
    header: str = "Authorization"
    param: str = "token"


class CustomSourceIn(BaseModel):
    name: str
    display_name: str = ""
    auth: AuthConfigIn = AuthConfigIn()
    datasets: dict[str, DatasetConfigIn] = {}


class CustomSourceTestIn(BaseModel):
    provider: str
    dataset: str
    symbols: list[str] | None = None
    config: CustomSourceIn | None = None


class MiningSchedulePrefs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mining_schedule_enabled: bool
    mining_schedule_weekday: int = Field(ge=0, le=4)
    mining_budget_profile: Literal["balanced", "strict"]


@router.get("/preferences")
def get_preferences() -> dict:
    """返回用户偏好设置。"""
    from app.services import preferences
    return {
        "realtime_quotes_enabled": preferences.get_realtime_quotes_enabled(),
        "realtime_allowed": _realtime_allowed(),
        "watchlist_groups_in_nav": preferences.get_watchlist_groups_in_nav(),
        "minute_sync_enabled": preferences.get_minute_sync_enabled(),
        "minute_sync_days": preferences.get_minute_sync_days(),
        "minute_sync_segment_days": preferences.get_minute_sync_segment_days(),
        "minute_refresh_enabled": preferences.get_minute_refresh_enabled(),
        "minute_refresh_interval": preferences.get_minute_refresh_interval(),
        "daily_data_provider": preferences.get_daily_data_provider(),
        "adj_factor_provider": preferences.get_adj_factor_provider(),
        "minute_data_provider": preferences.get_minute_data_provider(),
        "full_minute_data_provider": preferences.get_full_minute_data_provider(),
        "minute_history_days": _minute_history_days(),
        "realtime_data_provider": preferences.get_realtime_data_provider(),
        "financial_data_provider": preferences.get_financial_provider(),
        "data_source_job_timeout_s": preferences.get_data_source_job_timeout_s(),
        "data_source_long_job_timeout_s": preferences.get_data_source_long_job_timeout_s(),
        "minute_batch_compress": preferences.get_minute_batch_compress(),
        "daily_batch_compress": preferences.get_daily_batch_compress(),
        **preferences.get_realtime_quote_scope(),
        "pipeline_pull_a_share": preferences.get_pipeline_pull_a_share(),
        "pipeline_pull_etf": preferences.get_pipeline_pull_etf(),
        "pipeline_pull_index": preferences.get_pipeline_pull_index(),
        "pipeline_index_symbols": preferences.get_pipeline_index_symbols(),
        "pipeline_schedule": preferences.get_pipeline_schedule(),
        "instruments_schedule": preferences.get_instruments_schedule(),
        "enriched_batch_size": preferences.get_enriched_batch_size(),
        "index_daily_batch_size": preferences.get_index_daily_batch_size(),
        "watchlist_columns": preferences.get_watchlist_columns(),
        "screener_result_columns": preferences.get_screener_result_columns(),
        "sse_refresh_pages": preferences.get_sse_refresh_pages(),
        "strategy_monitor_enabled": preferences.get_strategy_monitor_enabled(),
        "strategy_monitor_ids": preferences.get_strategy_monitor_ids(),
        "system_notify_enabled": preferences.get_system_notify_enabled(),
        # 审计 2026-10-26 L-9: Webhook 地址里的 token、签名密钥、Secret 不再明文回显 (只回掩码 / 是否已设置)
        "feishu_webhook_url": mask_webhook_url(preferences.get_feishu_webhook_url()),
        "feishu_webhook_secret": "",
        "feishu_webhook_secret_set": bool(preferences.get_feishu_webhook_secret()),
        # 应用机器人: 凭据只在服务端环境变量, 这里只回「是否就绪」与目标群, 不回凭据
        "feishu_app_credentials_set": _feishu_app().credentials_present(),
        "feishu_app_chat_id": preferences.get_feishu_app_chat_id(),
        "feishu_app_configured": _feishu_app().is_configured(),
        "wecom_webhook_url": mask_webhook_url(preferences.get_wecom_webhook_url()),
        "custom_webhook_url": mask_webhook_url(preferences.get_custom_webhook_url()),
        "custom_webhook_secret_set": bool(secrets_store.get_custom_webhook_secret()),
        "email_smtp_config": preferences.get_email_smtp_config(),
        "email_smtp_password_set": bool(secrets_store.get_email_smtp_password()),
        "wecom_bot_id": preferences.get_wecom_bot_id(),
        "wecom_bot_secret": "",
        "wecom_bot_secret_set": bool(preferences.get_wecom_bot_secret()),
        "wecom_bot_enabled": preferences.get_wecom_bot_enabled(),
        "webhook_enabled_default": preferences.get_webhook_enabled_default(),
        "webhook_default_channels": preferences.get_webhook_default_channels(),
        "minute_intraday_refresh": preferences.get_minute_intraday_refresh(),
        "minute_intraday_refresh_interval": preferences.get_minute_intraday_refresh_interval(),
        "monitor_ext_fields": preferences.get_monitor_ext_fields(),
        "nav_order": preferences.get_nav_order(),
        "nav_hidden": preferences.get_nav_hidden(),
        "screener_auto_run": preferences.get_screener_auto_run(),
        **preferences.get_mining_schedule(),
    }


@router.get("/data-sources")
def list_data_sources() -> dict:
    """列出已加载的数据源 (内置 / 插件 / 用户自定义)。"""
    from app.data_providers import custom as custom_sources
    return {
        "builtin": [],
        "plugins": custom_sources.list_plugins(),
        "custom": custom_sources.list_sources(),
        "errors": custom_sources.errors(),
        "config_dir": str(custom_sources.data_sources_dir()),
    }


@router.get("/capability-matrix")
def get_capability_matrix() -> dict:
    """能力 x 源路由矩阵: 能力注册表 + 各源能力声明 + 当前路由偏好, 设置页一次拉全。

    偏好值经 preferences getters 注入 (自带合法源校验, 非法值回退默认),
    组装逻辑在 data_providers.capabilities, 本层保持薄。
    厂商内置源已退役 (P1-1b2): 候选 = 插件 + 自定义源。
    """
    from app.data_providers.capabilities import build_capability_matrix
    from app.services import preferences

    return build_capability_matrix(
        {
            "realtime_data_provider": preferences.get_realtime_data_provider(),
            "daily_data_provider": preferences.get_daily_data_provider(),
            "minute_data_provider": preferences.get_minute_data_provider(),
            "full_minute_data_provider": preferences.get_full_minute_data_provider(),
            "adj_factor_provider": preferences.get_adj_factor_provider(),
            "financial_data_provider": preferences.get_financial_provider(),
        },
    )


@router.post("/plugin-key")
def save_plugin_key(req: PluginKeyIn) -> dict:
    """保存插件 API Key(先探后存)。

    流程: probe_plugin_key 用候选 Key 实探 → 有效才写 secrets.json
    ({plugin}_api_key, 优先级高于 .env) → load_all 重扫, 插件即刻变为可切换。
    """
    from app.data_providers import custom as custom_sources

    name = req.plugin.strip().lower()
    key = req.api_key.strip()
    if not key:
        return {"ok": False, "error": "key empty"}
    ok, message = custom_sources.probe_plugin_key(name, key)
    if not ok:
        return {"ok": False, "reason": "invalid", "error": message}
    secrets_store.save({f"{name}_api_key": key})
    custom_sources.load_all()
    status = next((p for p in custom_sources.list_plugins() if p["name"] == name), None)
    return {
        "ok": True,
        "api_key_masked": secrets_store.mask(key),
        "plugin_available": bool(status and status.get("available")),
        "plugin": status,
    }


@router.delete("/plugin-key/{name}")
def clear_plugin_key(name: str) -> dict:
    """清除插件的界面配置 Key(secrets.json);.env 里的同名变量仍然生效。"""
    from app.data_providers import custom as custom_sources

    manifest = custom_sources.plugin_manifest(name)
    if manifest is None or not custom_sources.is_builtin(name):
        raise HTTPException(status_code=404, detail=f"插件 '{name}' 不存在")
    if not manifest.get("api_key_env"):
        raise HTTPException(status_code=400, detail=f"插件 '{name}' 不支持在界面配置 Key")
    secrets_store.clear(f"{name.lower()}_api_key")
    custom_sources.load_all()
    status = next((p for p in custom_sources.list_plugins() if p["name"] == name), None)
    return {
        "ok": True,
        "plugin_available": bool(status and status.get("available")),
        "plugin": status,
    }


@router.post("/data-sources/reload")
def reload_data_sources() -> dict:
    """重新加载 data_sources/*.yaml。"""
    from app.data_providers import custom as custom_sources
    custom_sources.load_all()
    return list_data_sources()


@router.post("/plugins/{name}/install")
def install_plugin(name: str) -> dict:
    """安装指定插件的依赖 (npm install / pip install), 完成后重新扫描。

    根据 plugin.yaml 的 runtime 字段决定安装方式。安装可能耗时较长 (网络下载),
    客户端需设较长超时。
    """
    from app.data_providers import custom as custom_sources
    if not custom_sources.is_builtin(name):
        raise HTTPException(status_code=404, detail=f"插件 '{name}' 不存在")
    ok, message = custom_sources.install_plugin(name)
    # 无论成功失败都重新扫描, 刷新插件状态 (安装可能部分成功)
    custom_sources.load_all()
    result = list_data_sources()
    result["install_ok"] = ok
    result["install_message"] = message
    return result


@router.delete("/plugins/{name}/install")
def uninstall_plugin(name: str) -> dict:
    """卸载指定插件的依赖 (删除 node_modules / pip uninstall), 完成后重新扫描。

    如果该插件当前正被使用, 自动回退为未配置。
    """
    from app.data_providers import custom as custom_sources
    from app.services import preferences
    if not custom_sources.is_builtin(name):
        raise HTTPException(status_code=404, detail=f"插件 '{name}' 不存在")
    ok, message = custom_sources.uninstall_plugin(name)
    # 卸载后若该插件正被使用, 回退为未配置
    for getter, key, default in [
        (preferences.get_daily_data_provider, "daily_data_provider", ""),
        (preferences.get_minute_data_provider, "minute_data_provider", ""),
        (preferences.get_realtime_data_provider, "realtime_data_provider", ""),
        (preferences.get_financial_provider, "financial_data_provider", ""),
    ]:
        if getter() == name:
            preferences.save({key: default})
    custom_sources.load_all()
    result = list_data_sources()
    result["uninstall_ok"] = ok
    result["uninstall_message"] = message
    return result


@router.get("/data-sources/{name}")
def get_data_source(name: str) -> dict:
    """读取一个自定义数据源的完整配置(用于前端编辑回填)。"""
    from app.data_providers import custom as custom_sources
    cfg = custom_sources.get_config_dict(name)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"数据源 '{name}' 不存在")
    return cfg


@router.post("/data-sources")
def save_data_source(req: CustomSourceIn) -> dict:
    """创建或更新一个自定义数据源 yaml, 保存后自动 reload。"""
    from app.data_providers import custom as custom_sources
    config = req.model_dump()
    config["name"] = (config.get("name") or "").lower()
    try:
        custom_sources.save_config(config["name"], config)
        custom_sources.load_all()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return list_data_sources()


@router.delete("/data-sources/{name}")
def delete_data_source(name: str, request: Request) -> dict:
    """删除一个自定义数据源 yaml, 保存后自动 reload。

    若当前总开关选中的就是被删的源, 回退为未配置。
    """
    from app.data_providers import custom as custom_sources
    from app.services import preferences
    try:
        custom_sources.delete_config(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    custom_sources.load_all()
    # 回退被删源的偏好
    updates: dict = {}
    if preferences.get_daily_data_provider() == name:
        updates["daily_data_provider"] = ""
    if preferences.get_realtime_data_provider() == name:
        updates["realtime_data_provider"] = ""
    if preferences.get_financial_provider() == name:
        updates["financial_data_provider"] = ""
    if preferences.get_adj_factor_provider() == name:
        updates["adj_factor_provider"] = ""
    if updates:
        preferences.save(updates)
    # 偏好可能已回退, 重建能力快照
    from app.data_providers.capabilities import current_capability_set
    request.app.state.capabilities = current_capability_set({
        "realtime_data_provider": preferences.get_realtime_data_provider(),
        "daily_data_provider": preferences.get_daily_data_provider(),
        "minute_data_provider": preferences.get_minute_data_provider(),
        "full_minute_data_provider": preferences.get_full_minute_data_provider(),
        "adj_factor_provider": preferences.get_adj_factor_provider(),
        "financial_data_provider": preferences.get_financial_provider(),
    })
    return list_data_sources()


@router.post("/data-sources/test")
def test_data_source(req: CustomSourceTestIn) -> dict:
    """试拉自定义数据源，不写盘。"""
    from app.data_providers import custom as custom_sources

    temporary = req.config is not None
    provider = None
    try:
        if req.config:
            config = req.config.model_dump()
            dataset_config = config["datasets"].get(req.dataset)
            if dataset_config is None:
                raise ValueError(f"dataset '{req.dataset}' is not configured")
            config["datasets"] = {req.dataset: dataset_config}
            provider = custom_sources.create_provider(config)
        else:
            provider = custom_sources.get_provider(req.provider)
        return provider.test_dataset(req.dataset, req.symbols)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"自定义数据源测试失败: {e}") from e
    finally:
        if temporary and provider is not None:
            provider.close()


@router.put("/preferences/data-providers")
def update_data_providers(req: DataProvidersIn, request: Request) -> dict:
    """保存数据源选择。"""
    from app.services import preferences
    updates = req.model_dump(exclude_none=True)
    if updates:
        preferences.save(updates)
    # 刷新能力快照: 当前 provider 变化改变能力集推导结果
    from app.data_providers.capabilities import current_capability_set
    request.app.state.capabilities = current_capability_set({
        "realtime_data_provider": preferences.get_realtime_data_provider(),
        "daily_data_provider": preferences.get_daily_data_provider(),
        "minute_data_provider": preferences.get_minute_data_provider(),
        "full_minute_data_provider": preferences.get_full_minute_data_provider(),
        "adj_factor_provider": preferences.get_adj_factor_provider(),
        "financial_data_provider": preferences.get_financial_provider(),
    })
    return {
        "daily_data_provider": preferences.get_daily_data_provider(),
        "adj_factor_provider": preferences.get_adj_factor_provider(),
        "minute_data_provider": preferences.get_minute_data_provider(),
        "full_minute_data_provider": preferences.get_full_minute_data_provider(),
        "realtime_data_provider": preferences.get_realtime_data_provider(),
        "financial_data_provider": preferences.get_financial_provider(),
    }


@router.put("/preferences/data-source-job-timeouts")
def update_data_source_job_timeouts(req: DataSourceJobTimeoutPrefs) -> dict:
    """保存普通与长数据后台任务的卡死判定时间。"""
    from app.services import preferences
    preferences.save(req.model_dump())
    return req.model_dump()


@router.put("/preferences/minute-batch-compress")
def update_minute_batch_compress(req: MinuteBatchCompressPrefs) -> dict:
    """保存分时详情与批量响应的 gzip 传输压缩开关。逐请求即时读取, 保存后立即生效。"""
    from app.services import preferences
    preferences.save({"minute_batch_compress": req.minute_batch_compress})
    return {"minute_batch_compress": preferences.get_minute_batch_compress()}


@router.put("/preferences/daily-batch-compress")
def update_daily_batch_compress(req: DailyBatchCompressPrefs) -> dict:
    """保存日K详情与批量响应的 gzip 传输压缩开关 (与分时独立)。逐请求即时读取。"""
    from app.services import preferences
    preferences.save({"daily_batch_compress": req.daily_batch_compress})
    return {"daily_batch_compress": preferences.get_daily_batch_compress()}


@router.put("/preferences/mining-schedule")
def update_mining_schedule(req: MiningSchedulePrefs) -> dict:
    """一次更新周度自动 mining 配置。"""
    from app.services import preferences

    return preferences.set_mining_schedule(
        req.mining_schedule_enabled,
        req.mining_schedule_weekday,
        req.mining_budget_profile,
    )


@router.get("/preferences/watchlist-columns")
def get_watchlist_columns() -> dict:
    """返回自选列表列配置。"""
    from app.services import preferences
    cols = preferences.get_watchlist_columns()
    return {"columns": cols}


class NavOrderIn(BaseModel):
    nav_order: list[str]


class NavHiddenIn(BaseModel):
    nav_hidden: list[str]


@router.put("/preferences/nav-order")
def update_nav_order(req: NavOrderIn) -> dict:
    """保存左侧菜单排序（内置页面 path + 扩展分析菜单 id 的有序列表）。"""
    from app.services import preferences
    saved = preferences.set_nav_order(req.nav_order)
    return {"nav_order": saved}


@router.put("/preferences/nav-hidden")
def update_nav_hidden(req: NavHiddenIn) -> dict:
    """保存左侧菜单隐藏项。"""
    from app.services import preferences
    saved = preferences.set_nav_hidden(req.nav_hidden)
    return {"nav_hidden": saved}


@router.put("/preferences/watchlist-columns")
def update_watchlist_columns(req: dict) -> dict:
    """保存自选列表列配置。"""
    from app.services import preferences
    columns = req.get("columns", [])
    saved = preferences.set_watchlist_columns(columns)
    return {"columns": saved}


@router.get("/preferences/screener-result-columns")
def get_screener_result_columns() -> dict:
    """返回策略结果列表列配置。"""
    from app.services import preferences
    cols = preferences.get_screener_result_columns()
    return {"columns": cols}


@router.put("/preferences/screener-result-columns")
def update_screener_result_columns(req: dict) -> dict:
    """保存策略结果列表列配置。"""
    from app.services import preferences
    columns = req.get("columns", [])
    saved = preferences.set_screener_result_columns(columns)
    return {"columns": saved}


@router.put("/preferences/minute-sync")
def update_minute_sync(req: MinuteSyncPrefs) -> dict:
    """保存分钟 K 同步偏好。

    minute_sync_segment_days 为可选:未传(None)时不覆盖现有值,便于开关/天数
    与段大小各自独立更新。
    """
    from app.services import preferences
    days = max(1, min(30, req.minute_sync_days))
    updates: dict = {
        "minute_sync_enabled": req.minute_sync_enabled,
        "minute_sync_days": days,
    }
    if req.minute_sync_segment_days is not None:
        updates["minute_sync_segment_days"] = max(5, min(30, req.minute_sync_segment_days))
    preferences.save(updates)
    return {
        "minute_sync_enabled": req.minute_sync_enabled,
        "minute_sync_days": days,
        "minute_sync_segment_days": preferences.get_minute_sync_segment_days(),
    }


@router.get("/minute-refresh/status")
def minute_refresh_status(request: Request) -> dict:
    """盘中分钟增量刷新服务状态 (开关/能力门控/最近一轮/下一轮)。"""
    svc = getattr(request.app.state, "minute_refresh", None)
    if svc is None:
        return {"available": False}
    return {"available": True, **svc.status()}


class RealtimeQuotesPrefs(BaseModel):
    realtime_quotes_enabled: bool


class RealtimeQuoteScopePrefs(BaseModel):
    realtime_pull_stock: bool | None = None
    realtime_pull_etf: bool | None = None


@router.put("/preferences/realtime-quotes")
def update_realtime_quotes(req: RealtimeQuotesPrefs, request: Request) -> dict:
    """保存全局实时行情开关。

    无实时能力的档位(TickFlow none/free)开关回弹强制关闭;
    starter+ 或自定义实时源(如 fuyao)为全市场实时。前端据此把开关置灰 / 回弹。
    """
    from app.services import preferences
    qs = getattr(request.app.state, "quote_service", None)

    allowed = qs.is_realtime_allowed() if qs else True
    if req.realtime_quotes_enabled and not allowed:
        # 当前档位不允许开启实时行情 — 强制关闭
        preferences.save({"realtime_quotes_enabled": False})
        if qs:
            qs.disable()
        return {"realtime_quotes_enabled": False, "realtime_allowed": False}
    if req.realtime_quotes_enabled:
        # 首用门禁: 本地完全无数据 (日K/enriched 均空, 与看板首用弹窗同口径) 时
        # 禁止开启 — 实时行情的展示 (enriched 底座 + 快照覆盖)、监控
        # 全部依赖本地数据, 空数据下轮询只是空转耗配额, 且无 instruments 维表。
        repo = getattr(request.app.state, "repo", None)
        if repo is not None and repo.latest_daily_date() is None and repo.latest_enriched_date() is None:
            raise HTTPException(
                status_code=409,
                detail="本地暂无行情数据，请先在「数据」页同步后再开启实时行情",
            )
    if req.realtime_quotes_enabled and qs and qs.is_paused():
        # 管道/数据修正运行期间禁止开启实时行情 — 防止写盘竞态
        raise HTTPException(status_code=409, detail="数据同步运行中，实时行情已临时暂停，请稍后再开启")
    if req.realtime_quotes_enabled:
        # 历史完整性门禁: 检测到最近交易日的盘中快照/缺口时禁止开启 —
        # 实时 flush 写出"今天"分区后, 盘后管道的"只刷今天"分支会让停机日的
        # 半日快照永久留存。同时自动创建修复任务, 修完即可正常开启。
        from app.services import data_integrity

        repo = getattr(request.app.state, "repo", None)
        if repo is not None:
            try:
                issues = data_integrity.scan_recent_integrity(repo.store.data_dir)
            except Exception:  # noqa: BLE001
                issues = []
            earliest = data_integrity.earliest_issue_day(issues)
            if issues and data_integrity.within_auto_repair_window(earliest):
                job_id, is_new = data_integrity.launch_integrity_repair(
                    request.app.state, earliest, "realtime_gate",
                )
                if job_id is not None:
                    detail = (
                        f"检测到{data_integrity.describe_issues(issues)}，"
                        + ("已自动创建修复任务，完成后即可开启实时行情"
                           if is_new else "修复任务正在进行中，请稍后再开启")
                        + f"（任务 {job_id}）"
                    )
                    raise HTTPException(status_code=409, detail=detail)

    preferences.save({"realtime_quotes_enabled": req.realtime_quotes_enabled})
    if qs:
        if req.realtime_quotes_enabled:
            qs.enable()
        else:
            qs.disable()

    return {"realtime_quotes_enabled": req.realtime_quotes_enabled, "realtime_allowed": allowed}


@router.put("/preferences/realtime-quote-scope")
def update_realtime_quote_scope(req: RealtimeQuoteScopePrefs) -> dict:
    """保存盘中实时行情范围；独立于盘后管道范围。"""
    from app.services import preferences
    cfg = req.model_dump(exclude_none=True)
    return preferences.set_realtime_quote_scope(cfg)


class WatchlistGroupsInNavPrefs(BaseModel):
    watchlist_groups_in_nav: bool


@router.put("/preferences/watchlist-groups-in-nav")
def update_watchlist_groups_in_nav(req: WatchlistGroupsInNavPrefs) -> dict:
    """保存自选分组是否显示在侧边栏开关。"""
    from app.services import preferences
    preferences.save({"watchlist_groups_in_nav": req.watchlist_groups_in_nav})
    return {"watchlist_groups_in_nav": req.watchlist_groups_in_nav}


class RealtimeMonitorConfigIn(BaseModel):
    sse_refresh_pages: dict[str, bool] | None = None
    strategy_monitor_enabled: bool | None = None
    strategy_monitor_ids: list[str] | None = None
    screener_auto_run: bool | None = None
    minute_intraday_refresh: bool | None = None
    minute_intraday_refresh_interval: int | None = None
    # 盘中分钟增量落盘 (Expert 专有) — 交易时段常驻服务, 归实时监控配置
    minute_refresh_enabled: bool | None = None
    minute_refresh_interval: int | None = None
    monitor_ext_fields: dict | None = None


@router.put("/preferences/realtime-monitor")
def update_realtime_monitor_config(req: RealtimeMonitorConfigIn, request: Request) -> dict:
    """更新实时监控配置。策略监控统一迁移为 MonitorRule,由监控引擎评估。"""
    from app.services import preferences

    cfg = req.model_dump(exclude_none=True)
    result = preferences.set_realtime_monitor_config(cfg)

    # 策略监控开关/池变化 → 同步迁移为 type=strategy 规则 + reload 引擎
    if req.strategy_monitor_ids is not None or req.strategy_monitor_enabled is not None:
        monitor_engine = getattr(request.app.state, "monitor_engine", None)
        strategy_engine = getattr(request.app.state, "strategy_engine", None)
        data_dir = request.app.state.repo.store.data_dir
        if monitor_engine is not None and strategy_engine is not None:
            from app.strategy import monitor_rules as mr_store
            try:
                if preferences.get_strategy_monitor_enabled():
                    ids = preferences.get_strategy_monitor_ids()
                    names = {s["id"]: s.get("name", s["id"]) for s in strategy_engine.list_strategies()}   # list_strategies 返回 dict
                    mr_store.migrate_strategy_monitors(data_dir, ids, names)
                else:
                    # 关闭策略监控: 停用所有策略规则
                    mr_store.migrate_strategy_monitors(data_dir, [], {})
                # reload 规则到引擎
                monitor_engine.set_rules(mr_store.load_all(data_dir))
            except Exception:
                pass

    return result


class PipelinePullTypesIn(BaseModel):
    """盘后管道拉取内容开关(股票 / ETF / 指数 独立控制)。"""
    pipeline_pull_a_share: bool | None = None
    pipeline_pull_etf: bool | None = None
    pipeline_pull_index: bool | None = None


@router.put("/preferences/pipeline-pull-types")
def update_pipeline_pull_types(req: PipelinePullTypesIn) -> dict:
    """更新盘后管道拉取内容开关。"""
    from app.services import preferences
    cfg = req.model_dump(exclude_none=True)
    return preferences.set_pipeline_pull_types(cfg)



class QuoteIntervalIn(BaseModel):
    interval: float


class SystemNotifyPrefsIn(BaseModel):
    enabled: bool


@router.put("/preferences/system-notify")
def update_system_notify(req: SystemNotifyPrefsIn) -> dict:
    """系统通知开关 — 开启后监控告警同时推送到操作系统通知中心。

    纯偏好, 无副作用 (不像策略监控要迁移规则), 直接落盘即可。
    quote_service 在每轮告警评估时读此开关决定是否发系统通知。
    """
    from app.services import preferences
    saved = preferences.set_system_notify_enabled(req.enabled)
    return {"system_notify_enabled": saved}


def _feishu_app():
    from app.services import feishu_app
    return feishu_app


class FeishuAppPrefsIn(BaseModel):
    chat_id: str = ""


@router.get("/preferences/feishu-app/chats")
def list_feishu_app_chats() -> dict:
    """飞书应用机器人所在的群 (推送目标候选)。凭据缺失返回空列表 + 说明, 不报 500。"""
    app = _feishu_app()
    if not app.credentials_present():
        return {"credentials_set": False, "chats": [], "detail": "服务端未配置飞书应用凭据 (ZENTRADE_FEISHU_APP_ID/SECRET)"}
    try:
        return {"credentials_set": True, "chats": app.list_bot_chats()}
    except Exception as exc:  # noqa: BLE001 - 不把上游异常正文 (可能含请求信息) 回给前端
        return {"credentials_set": True, "chats": [], "detail": f"读取机器人所在群失败: {type(exc).__name__}"}


@router.put("/preferences/feishu-app")
def update_feishu_app(req: FeishuAppPrefsIn) -> dict:
    """保存应用机器人推送目标群 (oc_ 开头); 空串 = 清空。"""
    from app.services import preferences

    chat_id = (req.chat_id or "").strip()
    if chat_id and not chat_id.startswith("oc_"):
        raise HTTPException(status_code=400, detail="群 ID 需以 oc_ 开头 (在下拉框里选择机器人所在的群)")
    saved = preferences.set_feishu_app_chat_id(chat_id)
    return {"feishu_app_chat_id": saved, "feishu_app_configured": _feishu_app().is_configured()}


MASK = "\u2022\u2022\u2022\u2022"  # ••••
_MASK_PREFIXES = ("https://open.feishu.cn/open-apis/bot/v2/hook/", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=")


def mask_webhook_url(url: str) -> str:
    """回显用掩码: 保留可识别前缀 + 末 4 位, 中间 token 换成 ••••。空串原样返回。"""
    url = (url or "").strip()
    if not url:
        return ""
    prefix = next((p for p in _MASK_PREFIXES if url.startswith(p)), None)
    if prefix is None:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            prefix = f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else ""
        except ValueError:
            prefix = ""
    rest = url[len(prefix):]
    return f"{prefix}{MASK}{rest[-4:] if len(rest) > 8 else ''}"


def resolve_masked_url(submitted: str, current: str) -> str:
    """保存时: 前端原样回传掩码 = 保持原地址; 含掩码却对不上 = 拒绝 (不能把掩码当地址存)。"""
    submitted = (submitted or "").strip()
    if MASK not in submitted:
        return submitted
    if current and submitted == mask_webhook_url(current):
        return current
    raise HTTPException(status_code=400, detail="地址含掩码字符, 请重新粘贴完整地址")


class FeishuWebhookPrefsIn(BaseModel):
    url: str
    # None = 保持已保存的密钥; 空串 = 清除 (不验签)。清空地址时一并清除
    secret: str | None = None


@router.put("/preferences/feishu-webhook")
def update_feishu_webhook(req: FeishuWebhookPrefsIn) -> dict:
    """飞书 Webhook 地址 + 签名密钥 — 全局一处配置, 所有启用推送的监控规则共用。

    - url: 传入空串表示清空配置; 非空则需为合法的飞书自定义机器人地址。
    - secret: 机器人启用了「签名校验」时填密钥, 留空表示不验签。
    """
    from app.services import preferences, webhook_adapter

    url = resolve_masked_url(req.url, preferences.get_feishu_webhook_url())
    if url and not webhook_adapter.is_valid_feishu_url(url):
        raise HTTPException(
            status_code=400,
            detail="Webhook 地址非法, 需为飞书自定义机器人地址 "
                   "(https://open.feishu.cn/open-apis/bot/v2/hook/...)",
        )
    saved_url = preferences.set_feishu_webhook_url(url)
    if not saved_url:
        preferences.set_feishu_webhook_secret("")
    elif req.secret is not None:
        preferences.set_feishu_webhook_secret(req.secret.strip())
    return {"feishu_webhook_url": mask_webhook_url(saved_url), "feishu_webhook_secret": "",
            "feishu_webhook_secret_set": bool(preferences.get_feishu_webhook_secret())}


class WecomWebhookPrefsIn(BaseModel):
    url: str


@router.put("/preferences/wecom-webhook")
def update_wecom_webhook(req: WecomWebhookPrefsIn) -> dict:
    """企业微信群推送 Webhook 地址 — 与飞书并列的第二推送通道。

    - url: 传入空串表示清空配置; 非空需为合法企业微信群推送 Webhook 地址, 或纯 key。
    - 用户可只填 key (webhook/send?key=xxx 的 xxx 部分), 后端自动补全为完整 URL。
    """
    from app.services import preferences, webhook_adapter

    url = resolve_masked_url(req.url, preferences.get_wecom_webhook_url())
    if url and not webhook_adapter.is_valid_wecom_url(url):
        raise HTTPException(
            status_code=400,
            detail="Webhook 地址非法, 需为企业微信群推送 Webhook 地址 "
                   "(https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=... 或纯 key)",
        )
    saved_url = preferences.set_wecom_webhook_url(url)
    return {"wecom_webhook_url": mask_webhook_url(saved_url)}


class CustomWebhookPrefsIn(BaseModel):
    url: str
    # None preserves the stored secret; an explicit empty string clears it.
    secret: str | None = None


@router.put("/preferences/custom-webhook")
def update_custom_webhook(req: CustomWebhookPrefsIn) -> dict:
    """Configure the generic third-party JSON webhook and optional HMAC secret."""
    from app.services import preferences, webhook_adapter

    url = resolve_masked_url(req.url, preferences.get_custom_webhook_url())
    if url and not webhook_adapter.is_valid_custom_url(url):
        raise HTTPException(status_code=400, detail="Webhook 地址必须是完整的 HTTP(S) URL")
    saved_url = preferences.set_custom_webhook_url(url)
    if not saved_url:
        secrets_store.set_custom_webhook_secret("")
    elif req.secret is not None:
        secrets_store.set_custom_webhook_secret(req.secret)
    return {
        "custom_webhook_url": mask_webhook_url(saved_url),
        "custom_webhook_secret_set": bool(secrets_store.get_custom_webhook_secret()),
    }


class EmailSmtpPrefsIn(BaseModel):
    host: str
    port: int = Field(default=465, ge=1, le=65535)
    security: Literal["ssl", "starttls", "none"] = "ssl"
    username: str = ""
    # None preserves the stored password; an explicit empty string clears it.
    password: str | None = None
    from_address: str = ""
    to_addresses: list[str] = Field(default_factory=list)


@router.put("/preferences/email-smtp")
def update_email_smtp(req: EmailSmtpPrefsIn) -> dict:
    """Configure the SMTP transport shared by monitor alerts and review reports."""
    from app.services import email_adapter, preferences

    host = (req.host or "").strip()
    username = (req.username or "").strip()
    from_address = (req.from_address or username).strip()
    recipients = list(dict.fromkeys(item.strip() for item in req.to_addresses if item.strip()))
    if host:
        if not from_address or not email_adapter.is_valid_email(from_address):
            raise HTTPException(status_code=400, detail="请填写有效的发件人邮箱")
        if not recipients or any(not email_adapter.is_valid_email(item) for item in recipients):
            raise HTTPException(status_code=400, detail="请至少填写一个有效的收件人邮箱")
        effective_password = (
            secrets_store.get_email_smtp_password()
            if req.password is None
            else req.password
        )
        if username and not effective_password:
            raise HTTPException(status_code=400, detail="已填写 SMTP 登录用户名, 请同时填写密码或授权码")
    else:
        username = ""
        from_address = ""
        recipients = []

    config = preferences.set_email_smtp_config({
        "host": host,
        "port": req.port,
        "security": req.security,
        "username": username,
        "from_address": from_address,
        "to_addresses": recipients,
    })
    if not host or not username:
        secrets_store.set_email_smtp_password("")
    elif req.password is not None:
        secrets_store.set_email_smtp_password(req.password)
    return {
        "email_smtp_config": config,
        "email_smtp_password_set": bool(secrets_store.get_email_smtp_password()),
    }


class WebhookTestIn(BaseModel):
    channel: Literal["feishu", "feishu_app", "wecom", "custom", "email"]


@router.post("/preferences/webhook-test")
def test_webhook(req: WebhookTestIn) -> dict:
    """向已保存的 Webhook 地址发送一条测试消息，验证配置是否正确。

    只测试已保存的配置（与生产推送同源），不测试未保存草稿。
    未配置 / 地址非法 / 发送失败均返回 HTTP 200 + {ok: False}，
    前端统一读 detail 渲染绿/红，不抛 400。
    """
    from app.services import preferences, webhook_adapter

    title = "TickFlow Stock Panel 推送测试"
    body = "如果你看到这条消息，说明推送配置正确 🎉"

    if req.channel == "feishu_app":
        app = _feishu_app()
        if not app.credentials_present():
            return {"ok": False, "detail": "服务端未配置飞书应用凭据"}
        if not app.is_configured():
            return {"ok": False, "detail": "尚未选择推送目标群，请先保存"}
        ok = app.send_card("ZenTrade · 推送测试", "", "如果你看到这条消息，说明飞书应用机器人推送配置正确 🎉")
        return {"ok": bool(ok), "detail": "发送成功" if ok else "发送失败，请检查机器人是否仍在该群、应用是否有发消息权限"}
    if req.channel == "feishu":
        url = preferences.get_feishu_webhook_url()
        if not url:
            return {"ok": False, "detail": "尚未配置飞书 Webhook，请先保存"}
        if not webhook_adapter.is_valid_feishu_url(url):
            return {"ok": False, "detail": "已保存的飞书 Webhook 地址非法，请重新保存"}
        secret = preferences.get_feishu_webhook_secret()
        # 诊断用途单次尝试: 失败即返回, 不等生产退避重试 (~17s)
        ok = webhook_adapter.send_feishu(url, title, body, secret, max_attempts=1)
    elif req.channel == "wecom":
        url = preferences.get_wecom_webhook_url()
        if not url:
            return {"ok": False, "detail": "尚未配置企业微信 Webhook，请先保存"}
        if not webhook_adapter.is_valid_wecom_url(url):
            return {"ok": False, "detail": "已保存的企业微信 Webhook 地址非法，请重新保存"}
        ok = webhook_adapter.send_wecom(url, title, body)
    elif req.channel == "custom":
        url = preferences.get_custom_webhook_url()
        if not url:
            return {"ok": False, "detail": "尚未配置第三方 Webhook, 请先保存"}
        if not webhook_adapter.is_valid_custom_url(url):
            return {"ok": False, "detail": "已保存的第三方 Webhook 地址非法, 请重新保存"}
        ok = webhook_adapter.send_custom(
            url,
            title,
            body,
            event_type="test",
            secret=secrets_store.get_custom_webhook_secret(),
            max_attempts=1,
        )
    else:  # email
        from app.services import email_adapter

        config = preferences.get_email_smtp_config()
        if not email_adapter.is_configured(config):
            return {"ok": False, "detail": "尚未完整配置邮件 SMTP, 请先保存"}
        ok = email_adapter.send_email(
            config,
            secrets_store.get_email_smtp_password(),
            title,
            body,
            max_attempts=1,
        )

    if ok:
        return {"ok": True, "detail": "测试消息已发送, 请检查对应接收端"}
    return {"ok": False, "detail": "推送失败：网络不可达或地址/密钥不正确，详情见后端日志"}


class WecomBotPrefsIn(BaseModel):
    bot_id: str
    # None = 保持已保存的 Secret (L-9 起 GET 不回明文, 前端只在用户重填时才传)
    secret: str | None = None
    enabled: bool = True


@router.put("/preferences/wecom-bot")
def update_wecom_bot(req: WecomBotPrefsIn, request: Request) -> dict:
    """企业微信智能机器人(BotID + Secret)配置 — 长连接通道。

    保存凭证后立即重建连接(stop→start), 因每机器人仅允许 1 条长连接。
    - bot_id/secret 均传空串表示清空配置并断开连接。
    - enabled 控制是否启用长连接(凭证齐全时生效)。
    """
    from app.services import preferences

    bot_id = (req.bot_id or "").strip()
    secret = "" if not bot_id else ((req.secret or "").strip() if req.secret is not None
                                    else preferences.get_wecom_bot_secret())
    preferences.set_wecom_bot_id(bot_id)
    preferences.set_wecom_bot_secret(secret)
    # 凭证不齐时强制关闭(避免 enabled=True 但连不上)
    enabled = req.enabled and bool(bot_id) and bool(secret)
    preferences.set_wecom_bot_enabled(enabled)

    # 立即应用: 重建连接
    bot_svc = getattr(request.app.state, "wecom_bot_service", None)
    status: dict = {}
    if bot_svc:
        bot_svc.apply_credential_change()
        status = bot_svc.status()
    return {
        "wecom_bot_id": preferences.get_wecom_bot_id(),
        "wecom_bot_secret": "",
        "wecom_bot_secret_set": bool(preferences.get_wecom_bot_secret()),
        "wecom_bot_enabled": preferences.get_wecom_bot_enabled(),
        "wecom_bot_status": status,
    }


class WecomBotToggleIn(BaseModel):
    enabled: bool


@router.put("/preferences/wecom-bot-toggle")
def toggle_wecom_bot(req: WecomBotToggleIn, request: Request) -> dict:
    """独立开关: 启用/禁用智能机器人长连接(不改动凭证)。

    凭证不齐时强制返回未启用(无法连接)。
    """
    from app.services import preferences

    bot_id = preferences.get_wecom_bot_id()
    secret = preferences.get_wecom_bot_secret()
    enabled = req.enabled and bool(bot_id) and bool(secret)
    preferences.set_wecom_bot_enabled(enabled)

    bot_svc = getattr(request.app.state, "wecom_bot_service", None)
    status: dict = {}
    if bot_svc:
        bot_svc.apply_credential_change()
        status = bot_svc.status()
    return {"wecom_bot_enabled": enabled, "wecom_bot_status": status}


class WebhookEnabledDefaultIn(BaseModel):
    enabled: bool


@router.put("/preferences/webhook-enabled-default")
def update_webhook_enabled_default(req: WebhookEnabledDefaultIn) -> dict:
    """新建监控规则时是否默认勾选推送 (老布尔接口, 兼容旧前端)。

    新数据模型为渠道数组 (webhook_default_channels); 此处转译为
    True→['feishu','wecom'], False→[]。新前端请改用 webhook-default-channels 接口。
    """
    from app.services import preferences

    saved = preferences.set_webhook_enabled_default(req.enabled)
    return {"webhook_enabled_default": saved}


class WebhookDefaultChannelsIn(BaseModel):
    channels: list[str]  # 多选: feishu / wecom / custom / email; 空数组=不推送


@router.put("/preferences/webhook-default-channels")
def update_webhook_default_channels(req: WebhookDefaultChannelsIn) -> dict:
    """新建监控规则默认渠道 + 机会页开盘放量提醒渠道 (多选)。

    监控规则仅把它作为新建预填, 单条规则仍可独立修改; 开盘放量提醒跟随当前勾选。
    空数组=默认不推送且开盘放量不入队。白名单外的渠道会被过滤掉。
    """
    from app.services import preferences

    saved = preferences.set_webhook_default_channels(req.channels)
    return {"webhook_default_channels": saved}


@router.put("/preferences/quote-interval")
def update_quote_interval(req: QuoteIntervalIn, request: Request) -> dict:
    """更新行情轮询间隔。按档位自动 clamp。"""
    qs = getattr(request.app.state, "quote_service", None)
    if not qs:
        return {"interval": req.interval, "min_interval": 6.0, "max_interval": 60.0}
    clamped = qs.set_interval(req.interval)
    return {
        "interval": clamped,
        "min_interval": qs.get_min_interval(),
        "max_interval": qs.MAX_INTERVAL,
    }


@router.get("/preferences/quote-interval")
def get_quote_interval(request: Request) -> dict:
    """获取当前行情轮询间隔和档位限制。"""
    qs = getattr(request.app.state, "quote_service", None)
    if not qs:
        return {"interval": 6.0, "min_interval": 6.0, "max_interval": 60.0}
    return {
        "interval": qs._interval,
        "min_interval": qs.get_min_interval(),
        "max_interval": qs.MAX_INTERVAL,
    }


class PipelineScheduleIn(BaseModel):
    hour: int
    minute: int


@router.put("/preferences/pipeline-schedule")
def update_pipeline_schedule(req: PipelineScheduleIn, request: Request) -> dict:
    """保存盘后管道调度时间并立即 reschedule。"""
    from app.services import preferences
    sched = preferences.set_pipeline_schedule(req.hour, req.minute)

    # 动态 reschedule
    from apscheduler.triggers.cron import CronTrigger
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        scheduler.reschedule_job(
            "daily_pipeline",
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=sched["hour"],
                minute=sched["minute"],
                timezone="America/New_York",
            ),
        )
        logger.info("pipeline rescheduled to %02d:%02d mon-fri", sched["hour"], sched["minute"])

    return sched


@router.put("/preferences/instruments-schedule")
def update_instruments_schedule(req: PipelineScheduleIn, request: Request) -> dict:
    """保存盘前标的维表调度时间并立即 reschedule。"""
    from app.services import preferences
    sched = preferences.set_instruments_schedule(req.hour, req.minute)

    from apscheduler.triggers.cron import CronTrigger
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        scheduler.reschedule_job(
            "pre_market_instruments",
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=sched["hour"],
                minute=sched["minute"],
                timezone="America/New_York",
            ),
        )
    return sched


class EnrichedBatchSizeIn(BaseModel):
    size: int


@router.put("/preferences/enriched-batch-size")
def update_enriched_batch_size(req: EnrichedBatchSizeIn) -> dict:
    """保存 enriched 全量计算批次大小。"""
    from app.services import preferences
    size = preferences.set_enriched_batch_size(req.size)
    return {"enriched_batch_size": size}


class IndexDailyBatchSizeIn(BaseModel):
    size: int


@router.put("/preferences/index-daily-batch-size")
def update_index_daily_batch_size(req: IndexDailyBatchSizeIn) -> dict:
    """保存指数日 K 同步批次大小。"""
    from app.services import preferences
    size = preferences.set_index_daily_batch_size(req.size)
    return {"index_daily_batch_size": size}
