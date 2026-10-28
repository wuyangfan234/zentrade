"""纸面账户重置 (2026-10-06 无限拍板“重置纸面账户”)。台账唯一写者是应用进程, 所以重置只能在进程内做:
持交易环锁 → 归档台账/卡册/熔断/净值到 data/audit_backups/ledger-reset-<ts>/ → 新台账 (同参数) → 交易环重放 →
Alpaca 纸面镜像换 client_order_id 前缀 (新台账 seq 从 1 重新开始, 不能和旧订单撞号) 并把已镜像持仓平掉。
交易时段内拒绝。返回归档目录与事件数。"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from app.market_clock import us_clock

logger = logging.getLogger(__name__)

ARCHIVE_ITEMS = ("ledger", "opportunities/cards", "opportunities/book", "opportunities/top_snapshots", "opportunities/pm_check", "circuit_breaker", "decision_desk/runs",
                 "decision_desk/weights.json", "opportunities/radar_frames")
# 0.39.0 雷达帧记录的是旧台账的持仓 / 名额状态: 随台账一起归档
# 0.28.0 盘中强势卡 (opportunities/intraday) 不归档: 它不下单、不占资金, 是独立的出卡战绩, 重置纸面账户不该清掉


class BrokerFlattenError(RuntimeError):
    """Alpaca 纸面镜像平仓失败: 重置整体放弃, 本地什么都没改。"""


def reset(data_dir: Path, loop, *, note: str = "") -> dict:
    """顺序 (全程持镜像锁, 镜像 15 秒任务读台账也在同一把锁里 → 与重置完全串行, 旧状态不会被写回):
    1) Alpaca 纸面: 撤全部挂单 + 逐个平仓 —— 任一步失败抛 BrokerFlattenError, 本地不动;
    2) 镜像状态: 旧状态另存, 新前缀, 起点 started_seq=None (下一次镜像任务按当时台账水位定起点 → 旧 seq 永不重放;
       即便后面归档失败仍是旧台账, 起点=旧台账末尾, 镜像持仓视为空, 旧仓平仓不会再卖第二次);
    3) 持台账锁: 归档台账/卡册/熔断/运行记录/权重 → 新台账 (同参数) → 交易环重新绑定。"""
    if us_clock.in_session():
        raise RuntimeError("交易时段内不重置")
    from app.services.alpaca_mirror import AlpacaMirror, _lock as mlock
    data_dir = Path(data_dir)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = data_dir / "audit_backups" / f"ledger-reset-{ts}"
    with mlock:
        m = AlpacaMirror(data_dir)
        closed = _flatten_broker(m)
        old = dict(m._state)
        if m.path.exists():
            shutil.copy2(m.path, m.dir / f"state.pre-reset-{ts}.json")
        prefix = "z" + ts[2:8] + ts[9]   # z + yymmdd + 时的十位 = 8 位 (镜像只认 ≤8 位字母数字)
        if prefix == m._prefix():
            prefix = "y" + ts[2:8] + ts[10]
        m._state = {"enabled": old.get("enabled", True), "started_seq": None, "orders": {}, "last_error": None,
                    "cid_prefix": prefix}
        m._audit({"action": "reset", "prefix": prefix, "archived_orders": len(old.get("orders") or {}), "closed": closed})
        m._save()
        lock = loop.ledger._lock if loop is not None else None
        if lock is not None:
            lock.acquire()
        try:
            acc_path = data_dir / "ledger" / "account.json"
            account_json = json.loads(acc_path.read_text(encoding="utf-8"))
            events_path = data_dir / "ledger" / "events.jsonl"
            n_events = sum(1 for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()) \
                if events_path.exists() else 0
            dst.mkdir(parents=True, exist_ok=False)
            moved = []
            for item in ARCHIVE_ITEMS:
                src = data_dir / item
                if not src.exists():
                    continue
                target = dst / item
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(target))
                moved.append(item)
            (data_dir / "ledger").mkdir(parents=True, exist_ok=True)
            (data_dir / "ledger" / "account.json").write_text(json.dumps(account_json, ensure_ascii=False, indent=2),
                                                              encoding="utf-8")
            (dst / "RESET.json").write_text(json.dumps({"at": datetime.now().astimezone().isoformat(), "events": n_events,
                                                        "moved": moved, "note": note, "mirror_prefix": prefix,
                                                        "broker_closed": closed}, ensure_ascii=False, indent=1))
            if loop is not None:
                from app.ledger.core import Account, Ledger
                loop.account = Account.from_file(data_dir / "ledger" / "account.json")
                loop.ledger = Ledger(data_dir=data_dir / "ledger")
                loop.ledger.replay(loop.account)
                loop.opp_svc.bind_ledger(loop.ledger)
                loop.trader.ledger = loop.ledger
                loop.trader.account = loop.account
                loop.trader._cards.clear()
                loop.breaker_status = None
                loop._notify_seq = 0
        finally:
            if lock is not None:
                lock.release()
    logger.warning("paper account reset: archived %d events to %s; mirror prefix %s", n_events, dst, prefix)
    return {"archive": str(dst.relative_to(data_dir)), "events": n_events, "moved": moved,
            "mirror": {"prefix": prefix, "closed": closed}}


def _flatten_broker(m) -> list[str]:
    """撤 Alpaca 纸面账户全部挂单, 逐个平仓 (盘外提交的平仓单在下一个开盘成交)。失败 → BrokerFlattenError。"""
    try:
        m.http("DELETE", "/orders")
        closed = []
        for pos in m.http("GET", "/positions") or []:
            m.http("DELETE", f"/positions/{pos['symbol']}")
            closed.append(pos["symbol"])
        return closed
    except Exception as e:  # noqa: BLE001
        raise BrokerFlattenError(f"Alpaca 纸面平仓失败, 未重置: {type(e).__name__}") from None
