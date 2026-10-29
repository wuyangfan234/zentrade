"""访问认证 API。

端点:
  GET  /api/auth/status        — 是否已设密码、当前会话是否有效
  POST /api/auth/setup         — 首次设置密码(仅限本机/内网, 防公网抢占)
  POST /api/auth/login         — 登录(密码 → 会话 token, 含限流)
  POST /api/auth/logout        — 注销当前会话
  POST /api/auth/change-password — 改密码(需已登录)

安全:
  - setup 端点只接受本机/内网请求(客户端 IP 见 _client_ip: 隧道用 CF-Connecting-IP, 反代用 XFF 最右一跳), 公网请求 403。
    否则黑客可比用户更早扫到域名, 抢先设密码, 反客为主。
  - login 限流: 同一来源 IP 连续失败 5 次, 锁 5 分钟(内存计数)。
  - 会话 token 通过 HttpOnly cookie 下发, 前端无需手动管理。
"""
from __future__ import annotations

import ipaddress
import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.services import auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "tf_session"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 与 SESSION_TTL 一致

# 限流: { ip: (fail_count, lock_until_ts) }
_fail_counter: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
_fail_lock = Lock()
_MAX_FAILS = 5
_LOCK_SECONDS = 300


def _is_local_network(host: str | None) -> bool:
    """是否本机或内网地址 (回环 / 10.x / 172.16–31.x / 192.168.x)。

    只对**已经过 _client_ip 判定的客户端 IP** 调用。172.16–31.x 在这里仍算内网, 因为局域网直连
    NAS 端口时容器看到的就是 docker 网关 (如 172.25.0.1); 但经隧道/反代来的请求不会落到这个网关
    地址上 —— _client_ip 会改用 Cf-Connecting-IP 或反代追加的那一跳 (BH-01, 2026-10-11)。
    """
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.strip())
    except ValueError:
        return host.strip() == "localhost"
    if ip.is_loopback:
        return True
    return any(ip in net for net in _PRIVATE_NETS)


_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _header_ip(raw: str | None) -> str | None:
    """取一个合法 IP 字符串; 非法/空返回 None。"""
    v = (raw or "").strip()
    if not v:
        return None
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        return None


def _client_ip(request: Request) -> str:
    """取真实客户端 IP (BH-01 修正, 2026-10-11)。

    只有直连 peer 本身是本机/内网 (同机或 docker 网关上的可信反代: cloudflared、Nginx) 时才看转发头:
      1. 有 CF-Connecting-IP → 用它。Cloudflare 边缘写入访客真实 IP, 访客无法伪造或删除。
         可信隧道或反向代理会把访客地址写入此头，容器内 peer 通常是 Docker 网关。
      2. 否则有 X-Forwarded-For → 取**最右**一跳 (紧邻的反代自己追加的连接方地址)。
         旧逻辑取最左段, 而最左段是访客自己可以写的: 伪造 127.0.0.1 即可冒充内网、绕过登录限流。
      3. 都没有 → 直连 peer (本机、局域网直连经 docker NAT 后的网关地址)。
    peer 是公网地址时一律不看转发头 (公网可随意伪造)。
    """
    direct = request.client.host if request.client else ""
    if direct and _is_local_network(direct):
        cf = _header_ip(request.headers.get("cf-connecting-ip"))
        if cf:
            return cf
        xff = request.headers.get("x-forwarded-for")
        if xff:
            hops = [h for h in (_header_ip(x) for x in xff.split(",")) if h]
            if hops:
                return hops[-1]
    return direct or "unknown"


def _cookie_secure(request: Request) -> bool:
    """会话 Cookie 是否带 Secure 标志 (COOKIE_SECURE=auto|true|false)。

    auto: 请求本身是 HTTPS, 或直连 peer 是本机/内网反代且 X-Forwarded-Proto=https 时开启;
    公网直连时不采信转发头 (可伪造)。纯 HTTP 内网访问保持关闭, 否则浏览器会丢弃 Cookie 导致无法登录。
    """
    mode = (settings.cookie_secure or "auto").strip().lower()
    if mode in {"1", "true", "yes", "on"}:
        return True
    if mode in {"0", "false", "no", "off"}:
        return False
    if request.url.scheme == "https":
        return True
    direct = request.client.host if request.client else ""
    if direct and _is_local_network(direct):
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return proto == "https"
    return False


def _check_login_rate_limit(ip: str) -> None:
    """登录失败限流检查, 触发则抛 429。锁定过期后重置计数(重新给 5 次机会)。"""
    with _fail_lock:
        count, until = _fail_counter.get(ip, (0, 0.0))
        now = time.time()
        if until > now:
            wait = int(until - now)
            raise HTTPException(
                status_code=429,
                detail=f"登录失败次数过多, 请 {wait} 秒后重试",
            )
        if until and until <= now:
            # 锁定已过期: 清除旧计数, 否则之后每失败一次都会立刻再锁 5 分钟
            _fail_counter.pop(ip, None)


def _record_login_fail(ip: str) -> None:
    """记录一次登录失败, 达阈值则锁定。"""
    with _fail_lock:
        # 防内存膨胀: 条目过多时清掉已过锁定期的记录
        if len(_fail_counter) > 1000:
            now = time.time()
            for stale in [k for k, (_, u) in _fail_counter.items() if u <= now]:
                _fail_counter.pop(stale, None)
        count, until = _fail_counter.get(ip, (0, 0.0))
        count += 1
        if count >= _MAX_FAILS:
            until = time.time() + _LOCK_SECONDS
            logger.warning("auth login locked for %s after %d fails", ip, count)
        _fail_counter[ip] = (count, until)


def _clear_login_fails(ip: str) -> None:
    """登录成功后清除该 IP 的失败计数。"""
    with _fail_lock:
        _fail_counter.pop(ip, None)


# ================================================================
# 端点
# ================================================================

class PasswordIn(BaseModel):
    password: str = Field(min_length=6, max_length=128)


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


@router.get("/status")
def auth_status(request: Request) -> dict:
    """认证状态: 是否已设密码 + 当前请求是否已登录。"""
    token = request.cookies.get(COOKIE_NAME)
    return {
        "configured": auth.is_configured(),
        "authenticated": bool(token and auth.is_valid_session(token)),
    }


@router.post("/setup")
def setup_password(req: PasswordIn, request: Request) -> dict:
    """首次设置访问密码。仅限本机/内网请求(防公网抢占)。

    若已设置过密码, 返回 409(改密码走 /change-password)。
    """
    # 关键: 限制只有服务器主人(本机/内网)能设密码
    client_ip = _client_ip(request)
    if not _is_local_network(client_ip):
        logger.warning("setup rejected from non-local ip: %s", client_ip)
        raise HTTPException(
            status_code=403,
            detail="首次设置密码仅允许本机或内网访问,请通过 SSH/本地浏览器操作",
        )

    if auth.is_configured():
        raise HTTPException(status_code=409, detail="密码已设置,如需修改请登录后使用改密码功能")

    auth.set_password(req.password)
    logger.info("access password set up from %s", client_ip)
    return {"ok": True, "configured": True}


@router.post("/login")
def login(req: LoginIn, request: Request, response: Response) -> dict:
    """登录: 密码 → 会话 token(写 HttpOnly cookie)。含失败限流。"""
    ip = _client_ip(request)
    _check_login_rate_limit(ip)

    if not auth.is_configured():
        raise HTTPException(status_code=409, detail="尚未设置访问密码")

    token = auth.verify_and_create_session(req.password)
    if not token:
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="密码错误")

    _clear_login_fails(ip)
    # HttpOnly: 防 XSS 窃取; SameSite=Lax: 防 CSRF; Path=/: 全站生效
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        secure=_cookie_secure(request),
    )
    return {"ok": True, "authenticated": True}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    """注销当前会话。"""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        auth.revoke_session(token)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password(req: ChangePasswordIn, request: Request) -> dict:
    """修改密码: 需验证旧密码, 成功后所有会话失效(含当前, 需重新登录)。"""
    token = request.cookies.get(COOKIE_NAME)
    if not (token and auth.is_valid_session(token)):
        raise HTTPException(status_code=401, detail="请先登录")

    if not auth.is_configured():
        raise HTTPException(status_code=409, detail="尚未设置访问密码")

    # 验证旧密码
    new_token = auth.verify_and_create_session(req.old_password)
    if not new_token:
        ip = _client_ip(request)
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="旧密码错误")
    # 临时 token 用完即弃
    auth.revoke_session(new_token)

    # 改密码(set_password 会清空所有会话)
    auth.set_password(req.new_password)
    return {"ok": True, "message": "密码已修改, 请重新登录"}
