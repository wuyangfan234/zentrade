"""飞书应用机器人推送 (飞书 Agent 集成能力 · Channel SDK)。

用户 2026-10-01: 群自定义机器人 Webhook 不稳定, 改用应用机器人发送。
- 凭据: 环境变量 ZENTRADE_FEISHU_APP_ID / ZENTRADE_FEISHU_APP_SECRET (.env, 永不落库、不打印、不出接口)
- 目标: preferences.feishu_app_chat_id (机器人所在群的 oc_ id, 设置页可选)
- 只做出站: 不建立长连接 (推送用 OpenAPI 即可, 无需收消息); SDK 自带 token 缓存与重试
失败一律返回 False 并记 WARNING (不含凭据), 由调用方 (outbox) 决定退避重试。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import suppress

logger = logging.getLogger(__name__)

APP_ID_ENV = "ZENTRADE_FEISHU_APP_ID"
APP_SECRET_ENV = "ZENTRADE_FEISHU_APP_SECRET"
_CARD_MAX_LEN = 28_000          # 卡片 JSON 上限约 30KB, 留余量给 header/结构
_TIMEOUT_S = 15.0
_CANCEL_TIMEOUT_S = 2.0
_LOOP_LOCK = threading.Lock()
_SEND_LOCK = threading.Lock()
_worker: threading.Thread | None = None
_cleanup_failed = False


def _credentials() -> tuple[str, str]:
    return os.environ.get(APP_ID_ENV, "").strip(), os.environ.get(APP_SECRET_ENV, "").strip()


def _saved_chat_id() -> str:
    from app.services import preferences
    return preferences.get_feishu_app_chat_id()


def credentials_present() -> bool:
    app_id, secret = _credentials()
    return bool(app_id and secret)


def is_configured() -> bool:
    return credentials_present() and bool(_saved_chat_id())


def _channel(app_id: str, secret: str):
    from lark_channel import FeishuChannel
    return FeishuChannel(app_id=app_id, app_secret=secret)


def _run(coro):
    """Serialize healthy callers within one queue + send deadline.

    Keep this lock separate from worker state/cleanup. Timed-out callers release
    it, but the owned-worker check still quarantines unfinished native work.
    """
    deadline = time.monotonic() + _TIMEOUT_S
    if not _SEND_LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
        coro.close()
        raise TimeoutError("feishu app queue timed out")
    try:
        if time.monotonic() >= deadline:
            coro.close()
            raise TimeoutError("feishu app queue timed out")
        return _run_owned(coro, deadline)
    finally:
        _SEND_LOCK.release()


def _run_owned(coro, deadline):
    """One owned worker, bounded caller wait; join(timeout) NEVER kills a thread.

    Cooperative cancellation unwinds HTTPX's async context managers. A blocked
    thread or cancellation-resistant coroutine retains ownership until it exits;
    callers fail closed instead of creating replacement workers/connections.
    """
    global _worker
    result: dict = {}
    with _LOOP_LOCK:
        if _cleanup_failed or (_worker is not None and _worker.is_alive()):
            coro.close()  # rejected coroutine has never run / opened a channel
            raise RuntimeError("feishu worker busy or cleanup incomplete")
        loop = asyncio.new_event_loop()

        async def drain():
            pending = {t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()}
            for t in pending:
                t.cancel()
            if pending:
                _, pending = await asyncio.wait(pending, timeout=_CANCEL_TIMEOUT_S)
            return not pending

        def worker():
            global _cleanup_failed
            asyncio.set_event_loop(loop)
            try:
                task = result["task"] = loop.create_task(coro)
                result["value"] = loop.run_until_complete(task)
            except BaseException as exc:
                result["error"] = exc
            finally:
                try:
                    # Unlike asyncio.run shutdown, this does not await cancellation forever.
                    clean = loop.run_until_complete(drain())
                    closing = loop.create_task(loop.shutdown_asyncgens())
                    _, pending = loop.run_until_complete(asyncio.wait({closing}, timeout=_CANCEL_TIMEOUT_S))
                    # CPython 3.11 shutdown_default_executor spawns an unbounded
                    # join-helper thread. Join existing DNS/to_thread workers directly,
                    # with ONE deadline, and quarantine if native work cannot stop.
                    executor = getattr(loop, "_default_executor", None)
                    if executor is not None:
                        executor.shutdown(wait=False, cancel_futures=True)
                        deadline = time.monotonic() + _CANCEL_TIMEOUT_S
                        for thread in executor._threads:
                            thread.join(max(0, deadline - time.monotonic()))
                        clean = clean and not any(thread.is_alive() for thread in executor._threads)
                    if not clean or pending:
                        _cleanup_failed = True
                        logger.warning("飞书异步资源清理未完成, 拒绝再次发送, 需重启进程")
                except BaseException:
                    _cleanup_failed = True
                    logger.warning("飞书事件循环清理失败, 拒绝再次发送, 需重启进程")
                finally:
                    loop.close()
                    asyncio.set_event_loop(None)

        t = _worker = threading.Thread(target=worker, name="feishu-app-send", daemon=True)
        try:
            t.start()
        except BaseException:
            coro.close()
            loop.close()
            raise
    t.join(max(0, deadline - time.monotonic()))
    if t.is_alive():
        # Callback runs only after the worker has installed its task.
        with suppress(RuntimeError):  # loop may close at the timeout boundary
            loop.call_soon_threadsafe(lambda: result["task"].cancel())
        t.join(_CANCEL_TIMEOUT_S)
        if t.is_alive():
            logger.warning("飞书发送取消未完成; Python线程不能硬杀, 退出前拒绝再次发送")
        raise TimeoutError("feishu app send timed out")
    if "error" in result:
        raise result["error"]
    if _cleanup_failed:
        raise RuntimeError("feishu cleanup incomplete")
    return result["value"]


async def _send_card(app_id: str, secret: str, target: str, card: dict):
    # Construct only after worker admission; never reuse a channel across closed loops.
    global _cleanup_failed
    channel = _channel(app_id, secret)
    try:
        return await channel.send(target, {"card": card}, {"receive_id_type": "chat_id"})
    finally:
        # Outbound-only SDK uses HTTPX async-with, not a persistent WS/aiohttp session.
        # stop() is the SDK's public cleanup hook. A blocking stop remains in the owned worker.
        stop = getattr(channel, "stop", None)
        if stop is not None:
            try:
                stop(join_timeout=_CANCEL_TIMEOUT_S)
            except BaseException:
                _cleanup_failed = True
                logger.warning("飞书通道关闭失败, 拒绝再次发送, 需重启进程")
                raise


def build_card(title: str, subtitle: str, body_md: str) -> dict:
    body = body_md if len(body_md) <= _CARD_MAX_LEN else body_md[:_CARD_MAX_LEN] + "\n\n…内容较长已截断"
    elements: list[dict] = []
    if subtitle.strip():
        elements += [{"tag": "markdown", "content": f"**{subtitle.strip()}**"}, {"tag": "hr"}]
    elements.append({"tag": "markdown", "content": body})
    return {"schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
            "body": {"elements": elements}}


def send_card(title: str, subtitle: str, body_md: str, *, chat_id: str | None = None) -> bool:
    """发一张 markdown 卡片到目标群。True = 飞书确认送达。"""
    app_id, secret = _credentials()
    target = (chat_id or _saved_chat_id() or "").strip()
    if not (app_id and secret and target):
        return False
    try:
        result = _run(_send_card(app_id, secret, target, build_card(title, subtitle, body_md)))
    except (Exception, asyncio.CancelledError) as exc:  # 不记录可能含凭据的异常正文
        logger.warning("飞书应用机器人发送异常: %s", type(exc).__name__)
        return False
    if not getattr(result, "success", False):
        code = getattr(getattr(result, "error", None), "code", None) or getattr(result, "error", None)
        logger.warning("飞书应用机器人发送失败: %s", str(code)[:80])
        return False
    return True


def send_text(title: str, body: str, *, chat_id: str | None = None) -> bool:
    """监控告警等短消息: 统一用同一张卡片样式 (无副标题)。"""
    return send_card(title, "", body, chat_id=chat_id)


def list_bot_chats() -> list[dict]:
    """机器人所在的群 (设置页选择推送目标用)。只返回 chat_id/名称。"""
    app_id, secret = _credentials()
    if not (app_id and secret):
        return []
    import httpx

    base = "https://open.feishu.cn/open-apis"
    tok = httpx.post(f"{base}/auth/v3/tenant_access_token/internal",
                     json={"app_id": app_id, "app_secret": secret}, timeout=10).json()
    token = tok.get("tenant_access_token")
    if not token:
        raise RuntimeError(f"tenant token unavailable (code {tok.get('code')})")
    chats, page = [], None
    for _ in range(10):
        params = {"page_size": 100, **({"page_token": page} if page else {})}
        data = httpx.get(f"{base}/im/v1/chats", params=params, timeout=10,
                         headers={"Authorization": f"Bearer {token}"}).json().get("data") or {}
        chats += [{"chat_id": c.get("chat_id"), "name": c.get("name") or ""} for c in data.get("items") or []]
        page = data.get("page_token") if data.get("has_more") else None
        if not page:
            break
    return chats
