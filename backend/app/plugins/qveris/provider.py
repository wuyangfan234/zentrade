"""QVeris 付费数据插件 (无限 2026-10-02 拍板: 期权页 + 财报预期 + 电话会; 每日上限 100 积分; 显示余额)。

REST (与官方 CLI @qverisai/cli 0.11.3 源码一致):
  GET  /auth/credits                    余额 (免费)
  POST /tools/execute?tool_id=...       执行 (收费); 官方 CLI 对 execute 设 maxRetries=0 —— 本插件同样绝不重试

本模块只负责 HTTP 与响应解包; 是否允许花钱 (上限/缓存/去重/记账) 由 app.services.qveris_gate 决定,
任何收费调用必须经由 gate, 不得直接调用 execute()。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from app.data_providers.base import ProviderCapabilities

logger = logging.getLogger(__name__)

BASE = "https://qveris.ai/api/v1"
UA = "zentrade/0.3"

# 只用这三个工具 (2026-10-02 实测: 1 积分/次, 数据完整; 其余候选见评估文档的「不建议接入」)
TOOLS = {
    "options_chain": "alphavantage.realtime_options.retrieve.v1.1e18340d",
    "earnings_estimates": "alphavantage.earnings_estimates.retrieve.v1.7aca3c4a",
    "earnings_transcript": "alphavantage.earnings_call_transcript.retrieve.v1.7aca3c4a",
}
EXPECTED_COST = {"options_chain": 1.0, "earnings_estimates": 1.0, "earnings_transcript": 1.0}


class QverisError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, charged: float | None = None):
        super().__init__(message)
        self.status = status
        self.charged = charged


def _api_key() -> str:
    from app import secrets_store
    return secrets_store.get_env_backed_secret("qveris_api_key", "QVERIS_API_KEY")


def _request(method: str, path: str, key: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - 固定 https 端点
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()[:600].decode("utf-8", "replace")
        try:
            msg = json.loads(raw).get("error_message") or json.loads(raw).get("message") or raw
        except ValueError:
            msg = raw
        hint = {401: "Key 无效", 403: "Key 无权限", 402: "积分不足", 429: "限流"}.get(e.code, "")
        raise QverisError(f"HTTP {e.code} {hint} {msg}".strip()[:300], status=e.code) from None


def probe_api_key(key: str) -> tuple[bool, str]:
    """设置页「先探后存」: 查一次余额 (免费)。"""
    if not key or not key.strip():
        return False, "Key 为空"
    try:
        bal = credits(key.strip())
    except QverisError as e:
        return False, f"探测失败: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"探测失败: {type(e).__name__}"
    return True, f"余额 {bal['remaining']:.2f} 积分"


def availability() -> tuple[bool, str]:
    if not _api_key():
        return False, "未配置 QVeris Key (可在设置页数据源卡片中直接填写)"
    return True, "ok"


def credits(key: str | None = None) -> dict:
    """余额 (免费)。返回 {remaining, purchased_total, daily_free_remaining}。"""
    key = key or _api_key()
    if not key:
        raise QverisError("未配置 QVeris Key")
    d = _request("GET", "/auth/credits", key, timeout=15)
    data: dict = d["data"] if isinstance(d.get("data"), dict) else d
    rem = data.get("remaining_credits")
    if not isinstance(rem, (int, float)):
        raise QverisError("余额响应格式异常")
    return {"remaining": float(rem), "purchased_total": float((data.get("purchased") or {}).get("total") or 0),
            "daily_free_remaining": float((data.get("daily_free") or {}).get("remaining") or 0)}


def execute(kind: str, parameters: dict, *, key: str | None = None, timeout: float = 90.0) -> dict:
    """执行一次收费调用 (不重试)。只能由 qveris_gate 调用。

    返回 {data, charged, execution_id, elapsed_ms}; 失败抛 QverisError (charged 字段带出平台记录的扣费, 一般为 0)。
    """
    if kind not in TOOLS:
        raise QverisError(f"未知数据类型 {kind}")
    key = key or _api_key()
    if not key:
        raise QverisError("未配置 QVeris Key")
    t0 = time.time()
    d = _request("POST", f"/tools/execute?tool_id={TOOLS[kind]}", key,
                 {"parameters": parameters, "max_response_size": 5_000_000}, timeout=timeout)
    billing = d.get("billing") or {}
    charged = billing.get("final_amount_credits", billing.get("recorded_amount_credits", billing.get("list_amount_credits")))
    charged = float(charged) if isinstance(charged, (int, float)) else None
    if not d.get("success"):
        raise QverisError(str(d.get("error_message") or "调用失败")[:300], charged=charged)
    res = d.get("result") or {}
    data = res.get("data") if isinstance(res, dict) else None
    # Alpha Vantage 在额度/参数问题时以 200 + {"Information"/"Note"/"Error Message"} 返回
    if isinstance(data, dict):
        for k in ("Error Message", "Information", "Note"):
            if k in data and len(data) <= 2:
                raise QverisError(f"数据源返回提示: {str(data[k])[:200]}", charged=charged)
    return {"data": data, "charged": charged, "execution_id": d.get("execution_id"),
            "elapsed_ms": d.get("elapsed_time_ms") or round((time.time() - t0) * 1000)}


class QverisProvider:
    """插件框架需要的 provider 实例 (不声明行情数据集, 只作为「可配置 Key 的数据源」出现在设置页)。"""
    name = "qveris"
    capabilities = ProviderCapabilities()

    def __init__(self) -> None:
        from types import SimpleNamespace
        self.config = SimpleNamespace(display_name="QVeris (付费金融数据)", path=None, datasets={})

    def close(self) -> None:
        return None
