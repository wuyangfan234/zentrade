"""Alpaca 纸面账户镜像下单 (2026-10-02 产品决策: 开启; ZenTrade 专用纸面账户; 市价单)。

目的: 内部纸面撮合之外, 把每笔成交原样发到 Alpaca **纸面**账户, 记录对方真实成交价, 逐笔对账执行偏差。
这是实盘前最接近真实的执行验证, 不涉及真实资金。

安全阀:
- 只允许 paper-api.alpaca.markets (代码里硬拒绝实盘域名)
- 镜像从开启那一刻起算 (账户为空仓起步): 开启前的内部持仓不镜像, 其卖出也不镜像 (不卖没买过的股票)
- 平仓数量不超过本回合及镜像账户的实际累计成交净仓 (含部分成交, 扣除未决平仓余量)
- 幂等: client_order_id = zt-<台账 seq>; POST 前持久化提交标记, 结果不明只按 CID 对账、不重发
- 每日最多 N 笔提交尝试 (默认 30); 一键停用 (kill switch), 每次开关写审计
- 只在常规交易时段提交; 开仓意图超过 5 分钟过期; 已有暴露的退出等待对账, 不按开仓窗口丢弃
- 内部退出先撤未决开仓、确认终态累计成交后平仓; 撤单受理不等于撤单完成
- 镜像失败绝不影响内部纸面台账 (只读台账, 单独状态文件)
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.market_clock import us_clock

logger = logging.getLogger(__name__)

PAPER_BASE = "https://paper-api.alpaca.markets/v2"
MAX_ORDERS_PER_DAY = 30
SUBMIT_WINDOW_SECONDS = 300
_lock = threading.RLock()


class AlpacaHTTPError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Alpaca HTTP {status}: {body[:160]}")
        self.status = status


def _default_http(method: str, path: str, body: dict | None = None) -> dict:
    from app.plugins.alpaca.provider import _load_credentials
    key, secret = _load_credentials()
    base = os.environ.get("ALPACA_PAPER_BASE", PAPER_BASE).rstrip("/")
    if not base.endswith("/v2"):
        base += "/v2"
    AlpacaMirror.check_base(base)
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                                          "Content-Type": "application/json", "User-Agent": "zentrade-mirror/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise AlpacaHTTPError(e.code, e.read()[:300].decode("utf-8", "replace")) from None


class AlpacaMirror:
    def __init__(self, data_dir: Path, http: Callable | None = None):
        self.dir = Path(data_dir) / "alpaca_mirror"
        self.path = self.dir / "state.json"
        self.audit_path = self.dir / "audit.jsonl"
        self.http = http or _default_http
        self.max_orders_per_day = MAX_ORDERS_PER_DAY
        self._state = self._load()

    @staticmethod
    def check_base(base: str) -> None:
        if "paper-api.alpaca.markets" not in base:
            raise ValueError("镜像只允许 Alpaca 纸面账户 (paper-api.alpaca.markets)")

    # ── 状态 ──
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"enabled": True, "started_seq": None, "orders": {}, "last_error": None}

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _audit(self, entry: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({**entry, "at": us_clock.now().isoformat()}, ensure_ascii=False) + "\n")

    def start(self, events: list[dict]) -> None:
        """首次开启: 记下当前台账水位, 之前的成交不镜像 (Alpaca 账户空仓起步)。"""
        with _lock:
            if self._state.get("started_seq") is None:
                self._state["started_seq"] = max((e["seq"] for e in events), default=0)
                self._audit({"action": "start", "started_seq": self._state["started_seq"]})
                self._save()

    def set_enabled(self, enabled: bool, note: str = "") -> None:
        with _lock:
            self._state = self._load()   # 构造在锁外: 先重读, 不把旧快照写回
            self._state["enabled"] = bool(enabled)
            self._audit({"action": "enable" if enabled else "disable", "note": note})
            self._save()

    # ── 台账 → 待下单 ──
    def sync(self, events: list[dict]) -> int:
        """把镜像起点之后的成交/减仓/平仓登记为待下单意图 (幂等)。"""
        with _lock:
            start = self._state.get("started_seq")
            if start is None:
                return 0
            added = 0
            skip_rounds: set[int] = set()
            for e in events:
                if e.get("kind") == "fill" and (e.get("accounting") == "contract" or e.get("backfill")):
                    skip_rounds.add(e["seq"])
                if e["seq"] <= start or e.get("kind") not in ("fill", "partial_close", "close"):
                    continue
                if e.get("accounting") == "contract" or e.get("backfill") or e.get("entry_seq") in skip_rounds:
                    # 合约式回合 (自选机会卡试用/前向档) 与事后回补回合整轮不镜像: 券商股票单表达不了合约式核算
                    # (空单要借券、按全额名义占资金、每日 30 单上限), 镜像会与台账系统性分叉 (2026-10-10 审查)
                    if e["kind"] == "fill":
                        skip_rounds.add(e["seq"])
                    continue
                cid = f"{self._prefix()}-{e['seq']}"
                if cid in self._state["orders"]:
                    continue
                if e["kind"] == "fill":
                    side, intent = {"short": ("sell", "open_short"),
                                    "sell": ("sell", "close_long")}.get(e.get("side"), ("buy", "open_long"))
                else:
                    short_exit = e.get("position_side") == "short"
                    side = "buy" if short_exit else "sell"
                    intent = "cover" if short_exit else "close_long"
                self._state["orders"][cid] = {"cid": cid, "seq": e["seq"], "symbol": e["symbol"], "side": side,
                                              "intent": intent, "entry_seq": e.get("entry_seq"),
                                              "ledger_qty": float(e.get("qty") or 0), "ledger_price": float(e.get("price") or 0),
                                              "ledger_kind": e["kind"], "ledger_at": e.get("at"), "status": "new"}
                added += 1
            self._assign_rounds()
            if added:
                self._save()
            return added

    def _prefix(self) -> str:
        """client_order_id 前缀: 默认 zt; 纸面账户重置 (台账 seq 从 1 重新开始) 后换新前缀, 不与旧订单撞号。"""
        p = self._state.get("cid_prefix")
        return p if isinstance(p, str) and p.isalnum() and 1 <= len(p) <= 8 else "zt"

    @staticmethod
    def _intent(o: dict) -> str:
        return o.get("intent") or ("close_long" if o["side"] == "sell" else "open_long")

    def _assign_rounds(self) -> None:
        """Use ledger rounds, not broker net positions, including pre-upgrade state.

        entry_seq is authoritative. Older events omit it, so reconstruct rounds
        from ordered internal opens/closes, even if an opening was never mirrored.
        """
        active: dict[str, dict] = {}
        for o in self._orders_locked():
            symbol, intent = o["symbol"], self._intent(o)
            o["intent"] = intent
            short = intent in ("open_short", "cover")
            current = active.get(symbol)
            if intent in ("open_short", "open_long"):
                if current is None or current["short"] != short:
                    current = {"seq": o["seq"], "qty": 0.0, "short": short}
                    active[symbol] = current
                o["round_seq"] = current["seq"]
                current["qty"] += o["ledger_qty"]
            else:
                entry = o.get("entry_seq")
                if entry is not None:
                    opening = self._state["orders"].get(f"{self._prefix()}-{entry}", {})
                    valid = (opening.get("symbol") == symbol and opening.get("seq", o["seq"]) < o["seq"]
                             and opening.get("intent") == ("open_short" if short else "open_long"))
                    o["round_seq"] = opening.get("round_seq") if valid else None
                else:
                    o["round_seq"] = current["seq"] if current and current["short"] == short else None
                if current and current["seq"] == o["round_seq"]:
                    current["qty"] -= o["ledger_qty"]
                    if o["ledger_kind"] == "close" or current["qty"] <= 1e-9:
                        active.pop(symbol, None)

    def _round_orders(self, o: dict) -> list[dict]:
        if o.get("round_seq") is None:
            return []
        return [other for other in self._state["orders"].values()
                if other["symbol"] == o["symbol"] and other.get("round_seq") == o["round_seq"]]

    @staticmethod
    def _attempted(o: dict) -> bool:
        return bool(o.get("submit_attempted_at") or o.get("submitted_at") or o.get("alpaca_id"))

    def _working(self, o: dict) -> bool:
        return o["status"] in ("submitted", "submitting", "submission_unknown") or (
            o["status"] == "new" and self._attempted(o))

    @staticmethod
    def _signed_fill(o: dict) -> float:
        q = float(o.get("filled_qty") or 0)
        return q if o["side"] == "buy" else -q

    def positions(self) -> dict[str, float]:
        with _lock:
            pos: dict[str, float] = {}
            for o in self._state["orders"].values():
                pos[o["symbol"]] = pos.get(o["symbol"], 0.0) + self._signed_fill(o)
            return {s: q for s, q in pos.items() if abs(q) > 1e-9}

    def _pending_qty(self, orders: list[dict], intent: str) -> float:
        return sum(max(0.0, float(o.get("qty") or 0) - float(o.get("filled_qty") or 0))
                   for o in orders if self._intent(o) == intent and self._working(o))

    def _pending_sells(self, symbol: str) -> float:
        return self._pending_qty([o for o in self._orders_locked() if o["symbol"] == symbol], "close_long")

    def _pending_covers(self, symbol: str) -> float:
        return self._pending_qty([o for o in self._orders_locked() if o["symbol"] == symbol], "cover")

    def _today_count(self) -> int:
        today = us_clock.today().isoformat()
        return sum(1 for o in self._state["orders"].values()
                   if str(o.get("submit_attempted_at") or o.get("submitted_at", ""))[:10] == today)

    def _record_broker_order(self, o: dict, response: dict) -> None:
        """Broker filled_qty/average are cumulative in every state, not just filled."""
        status = response.get("status")
        old_qty = float(o.get("filled_qty") or 0)
        raw_qty = response.get("filled_qty")
        terminal = status in ("filled", "canceled", "expired", "rejected")
        if terminal and raw_qty is None:
            raise ValueError("terminal broker response missing cumulative filled_qty")
        qty = float(raw_qty) if raw_qty is not None else old_qty
        if not math.isfinite(qty) or qty < 0 or (terminal and qty < old_qty):
            raise ValueError("invalid or regressing terminal broker filled_qty")
        if qty >= old_qty:
            o["filled_qty"] = qty
            px = float(response.get("filled_avg_price") or 0)
            if px > 0:
                o["filled_price"] = px
                if o["ledger_price"] > 0:
                    dev = (px / o["ledger_price"] - 1) * 1e4
                    o["deviation_bps"] = round(dev if o["side"] == "buy" else -dev, 2)
        if response.get("id"):
            o["alpaca_id"] = response["id"]
        if response.get("filled_at"):
            o["filled_at"] = response["filled_at"]
        o["broker_status"] = status
        if status == "filled":
            o["status"] = "filled"
        elif status in ("canceled", "expired", "rejected"):
            o["status"] = f"alpaca_{status}"
        else:
            # done_for_day / pending_cancel are NOT final cancellation evidence.
            o["status"] = "submitted"

    def _reconcile(self, o: dict) -> bool:
        try:
            response = self.http("GET", f"/orders:by_client_order_id?client_order_id={o['cid']}")
            self._record_broker_order(o, response)
            return True
        except Exception as e:  # noqa: BLE001
            self._state["last_error"] = f"{type(e).__name__}: {e}"[:200]
            return False  # including 404: ambiguous POST is never permission to POST again

    def _settle_openings(self, o: dict) -> bool:
        """Cancel outstanding opening remainders, then GET the final fill quantity.

        An HTTP 204 is only an acknowledgement. A lost DELETE acknowledgement,
        a cancel/fill race, or a failed GET must not release a premature close.
        """
        settled = True
        for opening in self._round_orders(o):
            if self._intent(opening) not in ("open_long", "open_short"):
                continue
            if opening["status"] == "new" and not self._attempted(opening):
                opening["status"] = "skipped_internal_exit"
            if not self._working(opening):
                continue
            self._reconcile(opening)
            if not self._working(opening):
                continue
            if opening.get("alpaca_id"):
                opening["cancel_requested_at"] = us_clock.now().isoformat()
                self._save()
                try:
                    self.http("DELETE", f"/orders/{opening['alpaca_id']}")
                except Exception as e:  # noqa: BLE001
                    self._state["last_error"] = f"{type(e).__name__}: {e}"[:200]
                self._reconcile(opening)
            if self._working(opening):
                settled = False
        return settled

    def _settle_prior_exits(self, o: dict) -> bool:
        """Full liquidation must not borrow quantity reserved by an earlier exit.

        Cancel/GET older partial exits first: if their unfilled part disappears
        after the final close was sized, it would otherwise become an orphan.
        Unknown submissions only reconcile by CID and remain retryable waits.
        """
        settled = True
        for prior in self._round_orders(o):
            if prior['seq'] >= o['seq'] or self._intent(prior) not in ('close_long', 'cover'):
                continue
            if prior['status'] == 'new' and not self._attempted(prior):
                prior['status'] = 'superseded_by_final_close'
            if not self._working(prior):
                continue
            self._reconcile(prior)
            if self._working(prior) and prior.get('alpaca_id'):
                prior['cancel_requested_at'] = us_clock.now().isoformat()
                self._save()
                try:
                    self.http('DELETE', f"/orders/{prior['alpaca_id']}")
                except Exception as e:  # noqa: BLE001
                    self._state['last_error'] = f'{type(e).__name__}: {e}'[:200]
                self._reconcile(prior)
            if self._working(prior):
                settled = False
        return settled

    def _exit_qty(self, o: dict) -> float | None:
        """None = retryable wait; zero = no exposure in this internal round."""
        if o['ledger_kind'] == 'close' and not self._settle_prior_exits(o):
            o['wait_reason'] = 'prior_exit_not_terminal'
            return None
        if not self._settle_openings(o):
            o["wait_reason"] = "opening_not_terminal"
            return None
        related = self._round_orders(o)
        intent = self._intent(o)
        sign = -1 if intent == "cover" else 1
        held = sign * sum(self._signed_fill(other) for other in related)
        pending = self._pending_qty(related, intent)
        global_pending = self._pending_covers(o["symbol"]) if sign < 0 else self._pending_sells(o["symbol"])
        available = min(held - pending, sign * self.positions().get(o["symbol"], 0) - global_pending)
        if available <= 1e-9:
            if held > 1e-9 or pending > 1e-9:
                o["wait_reason"] = "pending_exit_or_position_reconciliation"
                return None
            return 0.0
        # Full close flattens the actual round, including missed/canceled partial exits.
        return available if o["ledger_kind"] == "close" else min(o["ledger_qty"], available)

    def submit_pending(self) -> int:
        with _lock:
            self._assign_rounds()
            n = 0
            for o in self._orders_locked():
                if o["status"] in ("submitting", "submission_unknown") or (
                        o["status"] == "new" and self._attempted(o)):
                    self._reconcile(o)
                    continue
                if o["status"] != "new" or not self._state.get("enabled", True):
                    continue
                intent = self._intent(o)
                is_exit = intent in ("close_long", "cover")
                now = us_clock.now()
                if is_exit:
                    qty = self._exit_qty(o)
                    if qty is None:
                        continue
                    if qty <= 0:
                        o["status"] = "skipped_not_mirrored"
                        continue
                    if not us_clock.in_session(now):
                        o["wait_reason"] = "outside_session"
                        continue
                else:
                    if any(self._intent(other) in ("close_long", "cover") for other in self._round_orders(o)):
                        o["status"] = "skipped_internal_exit"
                        continue
                    try:
                        at = datetime.fromisoformat(str(o["ledger_at"]))
                        if at.tzinfo is None:
                            at = at.replace(tzinfo=us_clock.tz)
                    except (TypeError, ValueError):
                        at = now
                    if not us_clock.in_session(now) or (now - at).total_seconds() > SUBMIT_WINDOW_SECONDS:
                        o["status"] = "expired_out_of_session"
                        continue
                    previous = [other for other in self._orders_locked() if other["symbol"] == o["symbol"]
                                and other["seq"] < o["seq"] and other.get("round_seq") != o["round_seq"]]
                    if any(self._working(other) for other in previous) or any(
                            abs(sum(self._signed_fill(other) for other in previous
                                    if other.get("round_seq") == round_seq)) > 1e-9
                            for round_seq in {other.get("round_seq") for other in previous}):
                        o["wait_reason"] = "previous_round_not_flat"
                        continue
                    qty = o["ledger_qty"]
                if qty <= 0:
                    o["status"] = "skipped_zero_qty"
                    continue
                if self._today_count() >= self.max_orders_per_day:
                    if is_exit:
                        o["wait_reason"] = "daily_cap"
                    else:
                        o["status"] = "blocked_daily_cap"
                    continue
                cid = o["cid"]
                body = {"symbol": o["symbol"], "qty": str(int(qty)) if float(qty).is_integer() else str(qty),
                        "side": o["side"], "type": "market", "time_in_force": "day", "client_order_id": cid}
                o.pop("wait_reason", None)
                # Write-ahead fence: even process death during POST must only recover by CID.
                o.update(status="submitting", qty=float(qty), submit_attempted_at=now.isoformat(),
                         submitted_at=now.isoformat())
                self._save()  # failure here means NO network submission
                try:
                    response = self.http("POST", "/orders", body)
                except AlpacaHTTPError as e:
                    duplicate = e.status == 422 and "client_order_id" in str(e)
                    definitive = 400 <= e.status < 500 and e.status not in (408, 429) and not duplicate
                    o["status"] = "rejected" if definitive else "submission_unknown"
                    o["error"] = self._state["last_error"] = str(e)[:200]
                    if duplicate:
                        self._reconcile(o)
                except Exception as e:  # noqa: BLE001
                    o["status"] = "submission_unknown"
                    self._state["last_error"] = f"{type(e).__name__}: {e}"[:200]
                else:
                    self._record_broker_order(o, response)
                    n += 1
                self._save()
            self._save()
            return n

    def poll(self) -> int:
        with _lock:
            n = 0
            for o in self._state["orders"].values():
                if not self._working(o):
                    continue
                if self._reconcile(o) and o["status"] == "filled":
                    n += 1
            self._save()
            return n

    # ── 展示 ──
    def _orders_locked(self) -> list[dict]:
        """Live rows for internal mutations only; caller holds _lock."""
        return sorted(self._state["orders"].values(), key=lambda o: o["seq"])

    def orders(self) -> list[dict]:
        with _lock:
            return copy.deepcopy(self._orders_locked())

    def summary(self) -> dict:
        with _lock:
            return self._summary_locked()

    def _summary_locked(self) -> dict:
        orders = self._orders_locked()
        filled = [o for o in orders if o["status"] == "filled" and o.get("deviation_bps") is not None]
        devs = [o["deviation_bps"] for o in filled]
        by_status: dict[str, int] = {}
        for o in orders:
            by_status[o["status"]] = by_status.get(o["status"], 0) + 1
        return {"enabled": self._state.get("enabled", True), "started_seq": self._state.get("started_seq"),
                "orders": len(orders), "filled": len(filled), "by_status": by_status,
                "avg_deviation_bps": round(sum(devs) / len(devs), 2) if devs else None,
                "worst_deviation_bps": max(devs) if devs else None, "positions": self.positions(),
                "last_error": self._state.get("last_error"), "max_orders_per_day": self.max_orders_per_day,
                "rules": "只镜像开启之后的成交；市价单；常规时段提交、开仓限成交后 5 分钟；退出先撤未决开仓并对账，等待不丢弃；平仓限本回合累计净仓并扣未决余量；POST 结果不明只查询不重发；每天最多 30 次提交尝试；可一键停用"}

    def tick(self, events: list[dict]) -> dict:
        """调度入口: 登记 → 先对账 → 提交 → 再对账。异常只记录不外抛。"""
        try:
            self.start(events)
            self.sync(events)
            self.poll()
            self.submit_pending()
            self.poll()
        except Exception as e:  # noqa: BLE001
            logger.warning("alpaca mirror tick failed: %s", type(e).__name__)
            with _lock:
                self._state["last_error"] = f"{type(e).__name__}: {e}"[:200]
        return self.summary()
