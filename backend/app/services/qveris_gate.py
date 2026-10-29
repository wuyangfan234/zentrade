"""QVeris 成本闸 — 所有收费调用的唯一入口 (2026-10-02 产品决策: 每日上限 100 积分, 显示余额)。

顺序 (每次 fetch):
  1. 缓存: 命中且未过期 → 直接返回, 不花钱 (cached=True)
  2. 在途去重: 同一 (kind, symbol, params) 已有请求在跑 → 等它结束取结果, 不重复下单
  3. 每日上限: 今日已扣 (美东日) + 本次预估 > 上限 → 拒绝 (QuotaExceeded), 不调用
  4. 调用一次, 不重试 (官方 CLI 对 execute 也是 maxRetries=0)
  5. 记账: 无论成功失败都写台账 (平台记录的扣费额; 失败通常 0); 成功写缓存
台账: data/qveris/ledger.jsonl (一次调用一行, 不含任何密钥)
ledger.pending 在调用前持久化, 账本 flush/fsync 成功后才移除。坏行/未确认写入一律停付费,
不跳行、不截尾、不回滚已扣费用。恢复需停写、备份两文件、按平台账单核对并补全记录,
确认每日累计未漏记后才移除 pending; 磁盘错误还需修复存储并重启解除进程内故障锁。
此闸仅支持应用进程单写者 (线程锁不是跨进程文件锁)。
缓存: data/qveris/cache/<kind>/<SYMBOL>[_<variant>].json
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_DAILY_LIMIT = 100.0
class _Slot:
    """一次在途请求: 等待者持有同一对象引用, 结果写在对象上 (不依赖全局表的清理时机)。"""
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict | None = None
        self.error: Exception | None = None


_inflight: dict[str, _Slot] = {}
_lock = threading.Lock()
_ledger_lock = threading.Lock()
# One unresolved billing intent at a time, including different symbols.
_paid_lock = threading.Lock()
_ledger_faults: dict[Path, str] = {}


class QuotaExceeded(RuntimeError):
    pass


class GateError(RuntimeError):
    pass


def _dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "qveris"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _et_day(ts: float | None = None) -> str:
    from app.market_clock import us_clock
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).astimezone(us_clock.tz).strftime("%Y-%m-%d")


def daily_limit() -> float:
    from app import secrets_store
    v = secrets_store.load().get("qveris_daily_limit")
    try:
        return float(v) if v is not None else DEFAULT_DAILY_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT


def set_daily_limit(value: float) -> float:
    from app import secrets_store
    value = float(value)
    if not (0 <= value <= 10000):
        raise ValueError("每日上限需在 0–10000 积分之间")
    secrets_store.save({"qveris_daily_limit": value})
    return value


def _ledger_path(data_dir: Path) -> Path:
    return _dir(data_dir) / "ledger.jsonl"


def _storage_error(p: Path) -> GateError:
    # Do not roll back a possibly charged call or retry an uncertain append.
    msg = "QVeris ledger.jsonl 持久化失败, 收费已停用; 请保留原文件及 ledger.pending, 对账修复后重启"
    _ledger_faults[p.resolve()] = msg
    return GateError(msg)


def _check_ledger(p: Path, *, pending_owner: bool = False) -> None:
    if msg := _ledger_faults.get(p.resolve()):
        raise GateError(msg)
    if not pending_owner and p.with_suffix(".pending").exists():
        raise GateError("QVeris ledger.jsonl 有未确认扣费(ledger.pending), 收费已停用; 请先备份并按平台账单对账, 补全账本后再移除 pending, 勿直接删账本重置额度")


def _valid_row(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    try:
        day, charged = row["day"], row["charged"]
        return (isinstance(day, str) and datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") == day
                and isinstance(row.get("kind"), str) and bool(row["kind"])
                and isinstance(charged, (int, float)) and not isinstance(charged, bool)
                and math.isfinite(charged) and charged >= 0)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _read_ledger(p: Path) -> list[dict]:
    try:
        content = p.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise _storage_error(p) from exc
    rows = []
    # Validate ALL days before filtering: an unreadable date cannot be excluded safely.
    for number, line in enumerate(content.splitlines(keepends=True), 1):
        try:
            if not line.endswith(b"\n"):
                raise ValueError("unterminated record")
            if not line.strip():
                continue
            row = json.loads(line.decode("utf-8"))
            if not _valid_row(row):
                raise ValueError("invalid billing fields")
        except (ValueError, UnicodeError) as exc:
            raise GateError(f"QVeris ledger.jsonl 第 {number} 行损坏, 收费已停用; 请备份原文件并按平台账单恢复, 勿跳过坏行或清空账本") from exc
        rows.append(row)
    return rows


def ledger(data_dir: Path, *, day: str | None = None) -> list[dict]:
    with _ledger_lock:
        p = _ledger_path(data_dir)
        _check_ledger(p)
        rows = _read_ledger(p)
    return [r for r in rows if day is None or r["day"] == day]


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _begin_billing(data_dir: Path, row: dict) -> None:
    """Persist intent BEFORE network so a failed append cannot turn into free quota on restart."""
    from app.services.fs_utils import atomic_write_text
    with _ledger_lock:
        p = _ledger_path(data_dir)
        _check_ledger(p)
        pending = p.with_suffix(".pending")
        try:
            atomic_write_text(pending, json.dumps(row, ensure_ascii=False, allow_nan=False))
            with pending.open("rb") as f:
                os.fsync(f.fileno())
            _sync_directory(p.parent)
            _sync_directory(p.parent.parent)
        except (OSError, ValueError) as exc:
            raise _storage_error(p) from exc


def _append_ledger(data_dir: Path, row: dict, *, pending_owner: bool = False) -> None:
    with _ledger_lock:
        p = _ledger_path(data_dir)
        _check_ledger(p, pending_owner=pending_owner)
        _read_ledger(p)  # Never append to a partial tail or silently repair a cost ledger.
        if not _valid_row(row):
            raise GateError("QVeris ledger.jsonl 扣费记录无效, 需对账后恢复")
        try:
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            _sync_directory(p.parent)
            if pending_owner:
                p.with_suffix(".pending").unlink()
                _sync_directory(p.parent)
        except OSError as exc:
            # Leave both the uncertain bytes and intent for billing reconciliation.
            raise _storage_error(p) from exc


def spent_today(data_dir: Path, *, day: str | None = None) -> float:
    day = day or _et_day()
    return round(sum(float(r.get("charged") or 0) for r in ledger(data_dir, day=day)), 4)


def _cache_path(data_dir: Path, kind: str, symbol: str, variant: str = "") -> Path:
    d = _dir(data_dir) / "cache" / kind
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in symbol.upper() if c.isalnum() or c in ".-")[:12]
    return d / f"{safe}{('_' + variant) if variant else ''}.json"


def cache_get(data_dir: Path, kind: str, symbol: str, variant: str = "") -> dict | None:
    p = _cache_path(data_dir, kind, symbol, variant)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


def _cache_put(data_dir: Path, kind: str, symbol: str, variant: str, payload: dict) -> None:
    from app.services.fs_utils import atomic_write_text
    atomic_write_text(_cache_path(data_dir, kind, symbol, variant), json.dumps(payload, ensure_ascii=False))


def fetch(data_dir: Path, kind: str, symbol: str, parameters: dict, *, fresh: Callable[[dict], bool],
          variant: str = "", trigger: str = "click", execute: Callable[..., dict] | None = None,
          expected_cost: float | None = None, now: float | None = None) -> dict:
    """取一份数据: 缓存 → 去重 → 上限 → 调用 → 记账。

    fresh(cached_entry) -> bool 决定缓存是否仍可用 (由各数据类型自定规则)。
    返回 {data, fetched_at, cached, charged, execution_id}。
    """
    from app.plugins.qveris import provider as qp
    execute = execute or qp.execute
    cost = expected_cost if expected_cost is not None else qp.EXPECTED_COST.get(kind, 1.0)
    symbol = symbol.upper()
    entry = cache_get(data_dir, kind, symbol, variant)
    if entry is not None and fresh(entry):
        return {**entry, "cached": True, "charged": 0.0}

    key = hashlib.sha1(json.dumps([kind, symbol, variant, parameters], sort_keys=True).encode()).hexdigest()
    with _lock:
        slot = _inflight.get(key)
        owner = slot is None
        if owner:
            slot = _inflight[key] = _Slot()
    if not owner:
        if not slot.event.wait(timeout=150):
            raise GateError("同一数据的上一个请求仍在进行，请稍后再试")
        if slot.error is not None:
            raise slot.error
        return {**(slot.result or {}), "cached": True, "charged": 0.0, "deduped": True}

    try:
        with _paid_lock:
            day = _et_day(now)
            spent = spent_today(data_dir, day=day)
            limit = daily_limit()
            if spent + cost > limit + 1e-9:
                raise QuotaExceeded(f"今日 QVeris 积分已用 {spent:g}/{limit:g}，本次需约 {cost:g}，已拒绝（明天美东 0 点重置，或在设置页调高上限）")
            ts = now or time.time()
            row = {"at": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "day": day,
                   "kind": kind, "symbol": symbol, "variant": variant, "trigger": trigger, "expected": cost}
            _begin_billing(data_dir, row)
            try:
                out = execute(kind, parameters)
            except Exception as e:  # noqa: BLE001 — only confirmed charges may resolve the intent
                charged = getattr(e, "charged", None)
                if (not isinstance(charged, (int, float)) or isinstance(charged, bool)
                        or not math.isfinite(charged) or charged < 0):
                    # The POST may have executed before a response/body timeout.
                    # Keep the durable pending marker; unknown is NOT zero spending.
                    raise GateError("QVeris 执行结果及扣费未确认，收费已停用；请保留 ledger.pending 并按平台账单对账") from e
                _append_ledger(data_dir, {**row, "ok": False, "charged": float(charged),
                                          "error": str(e)[:200]}, pending_owner=True)
                raise
            charged = out.get("charged")
            charged = float(charged) if isinstance(charged, (int, float)) else cost  # 平台未回报时按报价记 (保守)
            _append_ledger(data_dir, {**row, "ok": True, "charged": charged, "execution_id": out.get("execution_id"),
                                      "elapsed_ms": out.get("elapsed_ms")}, pending_owner=True)
        payload = {"data": out["data"], "fetched_at": row["at"], "fetched_ts": ts, "execution_id": out.get("execution_id")}
        _cache_put(data_dir, kind, symbol, variant, payload)
        result = {**payload, "cached": False, "charged": charged}
        slot.result = result
        return result
    except Exception as e:  # noqa: BLE001
        slot.error = e
        raise
    finally:
        with _lock:
            _inflight.pop(key, None)
        slot.event.set()


# ── 余额 / 用量 (设置页数据源卡片 + 系统状态) ──────────────────────────────

_balance_cache: dict = {"at": 0.0, "value": None, "error": None}
BALANCE_TTL = 60.0


def balance(*, force: bool = False) -> dict:
    """余额查询免费, 仍做 60 秒缓存避免页面轮询打满接口。"""
    from app.plugins.qveris import provider as qp
    if not force and time.time() - _balance_cache["at"] < BALANCE_TTL and (_balance_cache["value"] or _balance_cache["error"]):
        return {"remaining": (_balance_cache["value"] or {}).get("remaining"), "error": _balance_cache["error"],
                "checked_at": _balance_cache["at"]}
    try:
        v = qp.credits()
        _balance_cache.update(at=time.time(), value=v, error=None)
    except Exception as e:  # noqa: BLE001
        _balance_cache.update(at=time.time(), error=str(e)[:160])
    return {"remaining": (_balance_cache["value"] or {}).get("remaining"), "error": _balance_cache["error"],
            "checked_at": _balance_cache["at"]}


def usage(data_dir: Path, *, force_balance: bool = False) -> dict:
    from app.plugins.qveris import provider as qp
    day = _et_day()
    try:
        rows = ledger(data_dir, day=day)
    except GateError as exc:
        return {"configured": bool(qp._api_key()), "day": day, "spent_today": None,
                "daily_limit": daily_limit(), "by_kind": {}, "recent": [], "error": str(exc),
                "balance": {"remaining": None, "error": "账本异常, 收费已停用"}}
    by_kind: dict[str, dict] = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], {"calls": 0, "credits": 0.0, "failed": 0})
        k["calls"] += 1
        k["credits"] = round(k["credits"] + float(r.get("charged") or 0), 4)
        if not r.get("ok"):
            k["failed"] += 1
    configured = bool(qp._api_key())
    return {"configured": configured, "day": day, "spent_today": round(sum(r["charged"] for r in rows), 4),
            "daily_limit": daily_limit(), "by_kind": by_kind,
            "balance": balance(force=force_balance) if configured else {"remaining": None, "error": "未配置 Key"},
            "recent": rows[-10:][::-1]}
