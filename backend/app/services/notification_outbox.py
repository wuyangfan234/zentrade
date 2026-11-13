"""持久化通知 outbox；只入队不联网，由专用 scheduler 调用 flush。

SQLite 唯一键去重，发送时不占数据库/入队锁。同进程同文件只有一个
flush；崩溃后的在途项以租约恢复。网络最多交付一次无法由 webhook 保证：
远端接受后、本地确认前崩溃可能重送（at-least-once），不伪称 exactly-once。

Fan-out mode (``targets`` given, 2026-11-13): one row = one message; at send time it goes to
every channel ``targets(payload)`` returns (ticked + configured in system settings). Channels that
already accepted it are kept in ``delivered`` and are never sent again; a retry only covers the
channels that failed. Every channel attempt is appended to ``push_log`` (the push log page).
Without ``targets`` the queue keeps the old single-sender behaviour.
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
_BODY_MAX = 4000


class NotificationOutbox:
    def __init__(self, data_dir: Path, *, sender: Callable[..., bool],
                 configured: Callable[[], bool], now: Callable[[], float] = time.time,
                 max_attempts: int = 5,
                 configured_for_payload: Callable[[dict], bool] | None = None,
                 targets: Callable[[dict], list[str]] | None = None):
        self.path = Path(data_dir).resolve() / "notifications" / "outbox.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sender, self.configured, self.now = sender, configured, now
        self.targets = targets
        self.configured_for_payload = configured_for_payload
        if targets is not None and configured_for_payload is None:
            self.configured_for_payload = self._has_targets
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
            cols = {r["name"] for r in db.execute("PRAGMA table_info(outbox)")}
            if "delivered" not in cols:
                try:
                    db.execute("ALTER TABLE outbox ADD COLUMN delivered TEXT NOT NULL DEFAULT '[]'")
                except sqlite3.OperationalError as exc:  # another thread migrated first
                    if "duplicate column" not in str(exc):
                        raise
            db.execute("""CREATE TABLE IF NOT EXISTS push_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, channel TEXT NOT NULL,
                at REAL NOT NULL, ok INTEGER NOT NULL, error TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS push_log_key ON push_log(key)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=0.25)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _has_targets(self, payload: dict) -> bool:
        return bool(self.targets(payload)) if self.targets is not None else False

    @staticmethod
    def _delivered(row) -> list[str]:
        try:
            value = json.loads(row["delivered"] or "[]")
        except (IndexError, KeyError, TypeError, ValueError):
            return []
        return [str(c) for c in value] if isinstance(value, list) else []

    def _public(self, row) -> dict:
        result = dict(row)
        result.pop("payload", None)
        result.pop("lease_until", None)
        result["delivered"] = self._delivered(row)
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
            rows = db.execute("SELECT status,created_at,sent_at FROM outbox").fetchall()
        # One row per message for every channel: a row is "sent" only when all ticked channels have it.
        counts = dict.fromkeys(_LABELS, 0)
        failed_created = []
        sent_times = []
        for row in rows:
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

    def _send_one(self, payload: dict, channel: str | None) -> tuple[bool, str | None]:
        try:
            ok = bool(self.sender(payload) if channel is None else self.sender(payload, channel))
        except Exception as exc:  # 辅助通道失败不得传播给撮合。
            return False, type(exc).__name__  # 不记录可能含 webhook 的异常正文
        return ok, None if ok else "send_failed"

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
                    payload = json.loads(row["payload"])
                    delivered = self._delivered(row)
                    if self.configured_for_payload is not None and not self._is_payload_configured(payload):
                        # 渠道未配置或已取消勾选: 不消耗发送次数, 一分钟后再看; 不挡住其他渠道。
                        db.execute("""UPDATE outbox SET status='unconfigured',next_attempt_at=?,lease_until=0
                                   WHERE key=?""", (now + 60, row["key"]))
                        continue
                    todo: list[str | None] = [None]
                    if self.targets is not None:
                        todo = [c for c in self.targets(payload) if c not in delivered]
                        if not todo:  # every ticked channel already has it
                            db.execute("""UPDATE outbox SET status='sent',sent_at=?,last_error=NULL,
                                       next_attempt_at=0,lease_until=0 WHERE key=?""", (now, row["key"]))
                            continue
                    attempt = row["attempts"] + 1
                    db.execute("UPDATE outbox SET status='pending',attempts=?,lease_until=? WHERE key=?",
                               (attempt, now + 120, row["key"]))
                # 事务已提交/连接已关闭，慢网络不阻挡新成交入队。
                results = [(channel, *self._send_one(payload, channel)) for channel in todo]
                done = self.now()
                ok = all(sent for _, sent, _ in results)
                delivered += [str(c) for c, sent, _ in results if sent and c is not None]
                if self.targets is None:
                    error = results[0][2]
                else:
                    error = "; ".join(f"{c}:{e}" for c, _, e in results if e) or None
                with self._db() as db:
                    db.executemany("INSERT INTO push_log(key,channel,at,ok,error) VALUES(?,?,?,?,?)",
                                   [(row["key"], c or payload.get("channel") or "-", done, int(sent), e)
                                    for c, sent, e in results])
                    db.execute("""UPDATE outbox SET status=?,sent_at=?,last_error=?,delivered=?,
                               next_attempt_at=?,lease_until=0 WHERE key=?""",
                               ("sent" if ok else "failed", done if ok else None, error,
                                json.dumps(delivered), 0 if ok else done + min(60 * 2 ** (attempt - 1), 3600),
                                row["key"]))
                summary["sent" if ok else "failed"] += 1
                sends += 1
        finally:
            self._flush_lock.release()
        return summary

    def log(self, *, limit: int = 100, only_failed: bool = False) -> dict:
        """Push log: newest first, one item per message with each channel's latest result.

        Rows sent before the push log existed have no per-channel record; they are shown as the
        only channel of that time (feishu) and flagged ``legacy``.
        """
        limit = max(1, min(int(limit), 500))
        sql = ("SELECT * FROM outbox WHERE status != 'sent' ORDER BY created_at DESC, key LIMIT ?"
               if only_failed else "SELECT * FROM outbox ORDER BY created_at DESC, key LIMIT ?")
        with self._db() as db:
            rows = db.execute(sql, (limit,)).fetchall()
            total = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            failed = db.execute("SELECT COUNT(*) FROM outbox WHERE status='failed'").fetchone()[0]
            wanted = {r["key"] for r in rows}
            attempts = [a for a in db.execute("SELECT key,channel,at,ok,error FROM push_log ORDER BY id")
                        if a["key"] in wanted]
        per: dict[str, dict[str, dict]] = {}
        for a in attempts:
            slot = per.setdefault(a["key"], {}).setdefault(a["channel"], {"tries": 0})
            slot.update(channel=a["channel"], ok=bool(a["ok"]), at=a["at"], error=a["error"],
                        tries=slot["tries"] + 1, legacy=False)
        items = []
        for row in rows:
            payload = json.loads(row["payload"])
            channels = list((per.get(row["key"]) or {}).values())
            if not channels and row["status"] == "sent":
                channels = [{"channel": payload.get("channel") or "feishu", "ok": True, "tries": row["attempts"],
                             "at": row["sent_at"], "error": None, "legacy": True}]
            body = str(payload.get("body") or "")
            items.append({
                "key": row["key"], "title": payload.get("title") or "", "subtitle": payload.get("subtitle") or "",
                "body": body if len(body) <= _BODY_MAX else body[:_BODY_MAX] + "…",
                "status": row["status"], "label": _LABELS[row["status"]], "attempts": row["attempts"],
                "exhausted": row["status"] == "failed" and row["attempts"] >= self.max_attempts,
                "created_at": row["created_at"], "sent_at": row["sent_at"], "last_error": row["last_error"],
                "delivered": self._delivered(row), "channels": channels,
            })
        return {"items": items, "total": total, "failed": failed}
