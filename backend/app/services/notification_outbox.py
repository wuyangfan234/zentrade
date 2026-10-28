"""持久化通知 outbox；只入队不联网，由专用 scheduler 调用 flush。

SQLite 唯一键去重，发送时不占数据库/入队锁。同进程同文件只有一个
flush；崩溃后的在途项以租约恢复。网络最多交付一次无法由 webhook 保证：
远端接受后、本地确认前崩溃可能重送（at-least-once），不伪称 exactly-once。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from threading import Lock
import time
from typing import Callable

_LABELS = {"unconfigured": "未配置", "pending": "待发送", "sent": "成功", "failed": "失败"}
_REGISTRY_LOCK = Lock()
_FLUSH_LOCKS: dict[str, Lock] = {}


class NotificationOutbox:
    def __init__(self, data_dir: Path, *, sender: Callable[[dict], bool],
                 configured: Callable[[], bool], now: Callable[[], float] = time.time,
                 max_attempts: int = 5,
                 configured_for_payload: Callable[[dict], bool] | None = None):
        self.path = Path(data_dir).resolve() / "notifications" / "outbox.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sender, self.configured, self.now = sender, configured, now
        self.configured_for_payload = configured_for_payload
        self.max_attempts = max(1, max_attempts)
        with _REGISTRY_LOCK:
            self._flush_lock = _FLUSH_LOCKS.setdefault(str(self.path), Lock())
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS outbox (
                key TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                next_attempt_at REAL NOT NULL DEFAULT 0, sent_at REAL,
                last_error TEXT, lease_until REAL NOT NULL DEFAULT 0
            )""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=0.25)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _public(self, row) -> dict:
        result = dict(row)
        result.pop("payload", None)
        result.pop("lease_until", None)
        result["label"] = _LABELS[result["status"]]
        result["exhausted"] = result["status"] == "failed" and result["attempts"] >= self.max_attempts
        result["durable"] = True
        return result

    def _is_payload_configured(self, payload: dict) -> bool:
        if self.configured_for_payload is not None:
            return bool(self.configured_for_payload(payload))
        return bool(self.configured())

    def enqueue(self, key: str, payload: dict) -> dict:
        """只做本地事务；重复键不覆盖原始内容/退避/已发送状态。"""
        status = "pending" if self._is_payload_configured(payload) else "unconfigured"
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO outbox(key,payload,status,created_at) VALUES(?,?,?,?)",
                       (key, encoded, status, self.now()))
            row = db.execute("SELECT * FROM outbox WHERE key=?", (key,)).fetchone()
        return self._public(row)

    def status(self, key: str | None = None) -> dict:
        """不返回正文或凭据；counts 为持久化状态，configured 为当前配置状态。"""
        with self._db() as db:
            if key is not None:
                row = db.execute("SELECT * FROM outbox WHERE key=?", (key,)).fetchone()
                return self._public(row) if row else {"key": key, "status": "not_found"}
            rows = db.execute("SELECT status,created_at,sent_at,payload FROM outbox").fetchall()
        # 聚合状态沿用原飞书口径: 旧无 channel 消息 + feishu 渠道。其他渠道只能按 key 查,
        # 其成功不得把飞书失败误判为已恢复。
        counts = dict.fromkeys(_LABELS, 0)
        failed_created = []
        sent_times = []
        for row in rows:
            channel = json.loads(row["payload"]).get("channel")
            if channel not in (None, "feishu"):
                continue
            counts[row["status"]] += 1
            if row["status"] == "failed":
                failed_created.append(row["created_at"])
            if row["status"] == "sent" and row["sent_at"] is not None:
                sent_times.append(row["sent_at"])
        last_sent = max(sent_times, default=None)
        unresolved = sum(created > (last_sent or 0) for created in failed_created)
        last_failed = max(failed_created, default=None)
        configured = bool(self.configured())
        status = ("unconfigured" if not configured else "failed" if unresolved
                  else "pending" if counts["pending"] or counts["unconfigured"]
                  else "sent" if counts["sent"] else "idle")
        return {"configured": configured, "status": status, "label": _LABELS.get(status, "已配置，暂无通知"),
                "counts": counts, "labels": dict(_LABELS), "unresolved_failed": unresolved,
                "last_sent_at": last_sent, "last_failed_at": last_failed}

    def flush(self, max_items: int = 10) -> dict:
        """有界发送，仅在独立通知 job/worker 调用，不能放撮合锁内。

        每条最多五次发送调用，失败 60/120/240/480 秒退避，耗尽保留 failed。
        底层 webhook 自身还有最多三次带退避的 HTTP 尝试，每次超时 5 秒。
        """
        summary = {"sent": 0, "failed": 0, "busy": False}
        if not self._flush_lock.acquire(blocking=False):
            return {**summary, "busy": True}
        try:
            if self.configured_for_payload is None and not self.configured():
                with self._db() as db:
                    db.execute("UPDATE outbox SET status='unconfigured' WHERE status='pending'")
                return summary
            send_budget = max(0, min(max_items, 50))
            sends = 0
            while sends < send_budget:
                now = self.now()
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute("""SELECT * FROM outbox WHERE status != 'sent'
                        AND attempts < ? AND next_attempt_at <= ? AND lease_until <= ?
                        ORDER BY created_at, key LIMIT 1""", (self.max_attempts, now, now)).fetchone()
                    if row is None:
                        break
                    if self.configured_for_payload is not None:
                        payload = json.loads(row["payload"])
                        if not self._is_payload_configured(payload):
                            # 渠道未配置或已取消勾选: 不消耗发送次数, 一分钟后再看; 不挡住其他渠道。
                            db.execute("""UPDATE outbox SET status='unconfigured',next_attempt_at=?,lease_until=0
                                       WHERE key=?""", (now + 60, row["key"]))
                            continue
                    attempt = row["attempts"] + 1
                    db.execute("UPDATE outbox SET status='pending',attempts=?,lease_until=? WHERE key=?",
                               (attempt, now + 120, row["key"]))
                # 事务已提交/连接已关闭，慢网络不阻挡新成交入队。
                error = None
                try:
                    ok = bool(self.sender(json.loads(row["payload"])))
                    if not ok:
                        error = "send_failed"
                except Exception as exc:  # 辅助通道失败不得传播给撮合。
                    ok, error = False, type(exc).__name__  # 不记录可能含 webhook 的异常正文
                done = self.now()
                with self._db() as db:
                    db.execute("""UPDATE outbox SET status=?,sent_at=?,last_error=?,
                               next_attempt_at=?,lease_until=0 WHERE key=?""",
                               ("sent" if ok else "failed", done if ok else None, error,
                                0 if ok else done + min(60 * 2 ** (attempt - 1), 3600), row["key"]))
                summary["sent" if ok else "failed"] += 1
                sends += 1
        finally:
            self._flush_lock.release()
        return summary
