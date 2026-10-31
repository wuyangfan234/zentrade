"""对外 API 密钥 (0.44.0) — 让外部 agent / 脚本用 Bearer 密钥调用全部 /api 接口与 MCP 端点。

设计:
  - 明文只在创建时返回一次; 落盘只存 SHA-256 摘要 + 前缀 (data/user_data/api_keys.json, 0600)。
    密钥 = "zt_" + 32 字节随机 (token_urlsafe), 熵足够, 摘要无需加盐/慢哈希。
  - 两档权限:
      full = 与登录后的网页同等的全部操作 (回测、策略增删改、自选、规则集、纸面交易设置…);
      read = 只读 + 纯计算 (GET 全部, 以及回测/选股/代码校验这类不改数据的 POST)。
  - 任何密钥都不能: 改登录密码 / 登录登出 (/api/auth/*)、管理密钥本身 (/api/settings/api-keys*)。
    这两类只认网页登录会话, 防止一把泄露的密钥自我续命或锁死主人。
  - 认证头: Authorization: Bearer zt_... 或 X-API-Key: zt_...。带了密钥头就只按密钥判定, 不再回退 Cookie。
  - last_used_at 至多每 60 秒落盘一次, 避免高频调用反复整文件写。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets as _secrets
import threading
import time
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "zt_"
SCOPES = ("full", "read")
MAX_KEYS = 50
_TOUCH_INTERVAL_S = 60.0

# 任何密钥都不可访问的路径前缀 (只认网页登录会话)
DENY_PREFIXES = ("/api/auth/", "/api/settings/api-keys")
# read 档额外放行的纯计算 POST (不写持久数据)
READ_SCOPE_POST_ALLOW = frozenset({
    "/api/mcp",
    "/api/backtest/run",
    "/api/backtest/strategy/run",
    "/api/backtest/factor/run",
    "/api/backtest/factor/batch",
    "/api/strategies/run",
    "/api/strategies/code/validate",
    "/api/screener/run",
    "/api/kline/daily-batch",
    "/api/kline/minute-batch",
    "/api/kline/instruments/names",
    "/api/factors/validate",
})
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_lock = threading.Lock()


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "api_keys.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        # 坏文件 = 全部密钥失效 (fail-closed), 不回退任何默认
        logger.warning("api_keys.json malformed: %s", type(e).__name__)
        return []
    keys = data.get("keys") if isinstance(data, dict) else None
    return [k for k in keys if isinstance(k, dict) and isinstance(k.get("hash"), str)] if isinstance(keys, list) else []


def _save(keys: list[dict]) -> None:
    atomic_write_text(_path(), json.dumps({"keys": keys}, indent=2, ensure_ascii=False), mode=0o600)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public(k: dict) -> dict:
    return {
        "id": k["id"], "name": k.get("name", ""), "scope": k.get("scope", "read"),
        "prefix": k.get("prefix", ""), "created_at": k.get("created_at"),
        "last_used_at": k.get("last_used_at"), "expires_at": k.get("expires_at"),
    }


def list_keys() -> list[dict]:
    with _lock:
        return [_public(k) for k in _load()]


def create_key(name: str, scope: str, expires_days: int | None = None, *, now: float | None = None) -> tuple[dict, str]:
    """返回 (公开信息, 明文密钥)。明文只此一次。"""
    if scope not in SCOPES:
        raise ValueError(f"scope 只能是 {' / '.join(SCOPES)}")
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("请填写密钥名称")
    if expires_days is not None and not (1 <= int(expires_days) <= 3650):
        raise ValueError("有效期 1-3650 天")
    now = time.time() if now is None else now
    token = TOKEN_PREFIX + _secrets.token_urlsafe(32)
    entry = {
        "id": _secrets.token_hex(6),
        "name": name,
        "scope": scope,
        "prefix": token[:10],
        "hash": _digest(token),
        "created_at": now,
        "last_used_at": None,
        "expires_at": now + int(expires_days) * 86400 if expires_days else None,
    }
    with _lock:
        keys = _load()
        if len(keys) >= MAX_KEYS:
            raise ValueError(f"最多 {MAX_KEYS} 把密钥, 先吊销不用的")
        keys.append(entry)
        _save(keys)
    return _public(entry), token


def revoke_key(key_id: str) -> bool:
    with _lock:
        keys = _load()
        kept = [k for k in keys if k.get("id") != key_id]
        if len(kept) == len(keys):
            return False
        _save(kept)
        return True


def verify(token: str, *, now: float | None = None) -> dict | None:
    """有效 → 公开信息 (含 scope); 无效/过期 → None。"""
    if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 200:
        return None
    now = time.time() if now is None else now
    d = _digest(token)
    with _lock:
        keys = _load()
        hit = None
        for k in keys:
            if hmac.compare_digest(k["hash"], d):
                hit = k
        if hit is None:
            return None
        if hit.get("expires_at") and now >= float(hit["expires_at"]):
            return None
        last = hit.get("last_used_at") or 0
        if now - float(last) >= _TOUCH_INTERVAL_S:
            hit["last_used_at"] = now
            try:
                _save(keys)
            except OSError as e:  # 观测字段写失败不影响鉴权
                logger.warning("api key touch failed: %s", type(e).__name__)
        return _public(hit)


def token_from_headers(headers) -> str | None:
    """取请求里的密钥: Authorization: Bearer zt_... 或 X-API-Key。没带返回 None。"""
    raw = headers.get("x-api-key")
    if raw:
        return raw.strip()
    auth = headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        val = auth[7:].strip()
        if val.startswith(TOKEN_PREFIX):
            return val
    return None


def permission_error(scope: str, method: str, path: str) -> str | None:
    """该密钥能否访问 (method, path); 不能时返回中文原因。"""
    if any(path.startswith(p) for p in DENY_PREFIXES):
        return "API 密钥不能访问登录与密钥管理接口, 请在网页登录后操作"
    if scope == "full":
        return None
    if method.upper() in _READ_METHODS:
        return None
    if method.upper() == "POST" and path.rstrip("/") in READ_SCOPE_POST_ALLOW:
        return None
    return "只读密钥不能执行写操作 (需要「完全控制」密钥)"
