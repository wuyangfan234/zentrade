"""ZenTrade 事件溯源台账 (ledger) — P2-1。

铁律 (docs/architecture.md):
- events.jsonl append-only 是唯一事实源; 持仓/净值/回合全部可由事件重放推导
- 任何"改历史"只记冲正事件, 不修改既有行
- seq 严格递增; 重启后从末行续号; 原子写 (tmp+rename)
- 美股除权: corp_action 事件调整数量与成本 (拆股乘因子, 现金分红入 cash)
- 做空 (2026-10-03): fill.side="short" 开空 (卖空所得入现金), 空头头寸 qty 为负;
  close/partial_close 对空头 = 买入回补 (现金减少)。卖空所得不能当可用资金再开仓 —
  空头占用同额购买力 (见 buying_power), 不加杠杆。空头现金分红由空方支付 (cash 减少)。
- 合约式核算 (2026-10-10 产品决策: “机会卡是多空两种方向的, 你按照合约期权的方式进行核算”): fill.accounting="contract"
  的回合不以名义金额进出现金, 只结算盈亏: 开仓现金不动 (费用计入均价), 持仓价值 = qty × (现价 − 均价) (浮动盈亏),
  减仓/平仓把已实现盈亏 (含费用) 记入现金。同一笔交易在任一时点的净值与股票式完全相同 (测试锁定), 区别只在现金与
  购买力: 多空对称, 每个合约回合按名义 × CONTRACT_MARGIN_RATE 占用保证金, 空头不借券、不收卖空所得。

文件布局 (data/ledger/):
  events.jsonl       # 全部事件, 一行一个 JSON
  positions.json     # 物化持仓 (可重建)
  account.json       # 账户 (初始权益/参数/状态)
  nav/daily.jsonl    # 每日定版净值
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LEDGER_SCHEMA_VERSION = 1

# 事件类型 (append-only 台账词汇表)
EVENT_FILL = "fill"                    # 成交 (买/卖)
EVENT_PARTIAL_CLOSE = "partial_close"  # 部分平仓
EVENT_CLOSE = "close"                  # 全部平仓
EVENT_STOP_MOVE = "stop_move"          # 移动止损
EVENT_CORP_ACTION = "corp_action"      # 除权冲正 (拆股/分红)
EVENT_ADJUST = "adjust"                # 其他冲正 (带 reason, 禁止滥用)
EVENT_OPPORTUNITY = "opportunity"      # 机会卡状态转换 (引擎联动, P2-2 接)
EVENT_NAV = "nav"                      # 净值快照事件 (日内; 日终定版走 nav/daily.jsonl)

CONTRACT = "contract"
CONTRACT_MARGIN_RATE = 0.5             # 合约式保证金 = 名义 × 50% (最多 2 倍名义敞口)

_FINAL_KINDS = frozenset({EVENT_FILL, EVENT_PARTIAL_CLOSE, EVENT_CLOSE,
                          EVENT_CORP_ACTION, EVENT_ADJUST})


@dataclass(frozen=True)
class Account:
    """纸面账户 (确认参数见 07)。权益口径: cash + Σ 持仓市值。"""
    initial_equity: float = 100_000.0
    risk_per_trade_pct: float = 0.01      # 单笔风险 = 权益 × 1%
    max_positions: int = 6
    max_portfolio_risk_pct: float = 0.06  # 组合总风险 ≤ 6%
    commission_per_share: float = 0.005   # $0.005/股 (IBKR Pro 近似)
    min_commission: float = 1.0           # 每笔最低 $1
    slippage_bps: float = 5.0             # 滑点 5bps (双边)
    max_holding_days: int = 7             # 时间止损: 7 个交易日 (自成交日起算)

    @classmethod
    def from_file(cls, path: Path | str) -> Account:
        """读取已存参数；只忽略 schema version，不用默认值掩盖读取失败。"""
        with open(path, encoding="utf-8") as f:
            params = json.load(f)
        params.pop("version", None)
        return cls(**params)


def position_value(qty: float, mark: float, avg_cost: float, contract: bool) -> float:
    """持仓对净值的贡献: 股票式 = 带符号市值 qty×现价; 合约式 = 浮动盈亏 qty×(现价 − 均价)。"""
    return qty * (mark - avg_cost) if contract else qty * mark


@dataclass
class Position:
    symbol: str
    qty: float = 0.0                      # 正 = 多头, 负 = 空头
    avg_cost: float = 0.0                 # 多头含买入费用的均价; 空头 = 卖空均价 (扣费用后的净所得/股)
    entry_date: str | None = None         # 首次建仓日 (iso)
    card_id: str | None = None            # 关联机会卡 (P2-2)
    stop_price: float | None = None       # 当前止损线 (移动止损更新)
    contract: bool = False                # 合约式核算回合 (见模块说明)

    @property
    def side(self) -> str:
        return "short" if self.qty < 0 else "long"

    @property
    def size(self) -> float:
        """绝对持仓股数。"""
        return abs(self.qty)


@dataclass
class Ledger:
    """台账: 事件流 + 物化。所有写操作持锁 + 原子。"""
    data_dir: Path
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    read_only: bool = field(default=False, kw_only=True)
    _events_path: Path = field(init=False)
    _seq: int = field(default=0, init=False)
    _cash: float = field(default=0.0, init=False)
    _positions: dict[str, Position] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_events_path", self.data_dir / "events.jsonl")
        if not self.read_only:
            self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def lock(self):
        """共享 RLock，供调用方把检查与追加合并成一个可重入临界区。"""
        return self._lock

    def _require_writable(self) -> None:
        if self.read_only:
            raise PermissionError("只读台账不能写入")

    # ── 初始化 / 恢复 ──────────────────────────────────────

    def init_account(self, account: Account) -> None:
        """首次初始化账户 (已存在则拒绝, force 由调用方显式删目录)。"""
        with self._lock:
            self._require_writable()
            acc_path = self.data_dir / "account.json"
            if acc_path.exists():
                raise FileExistsError("账户已存在, 拒绝覆盖初始化")
            self._atomic_write(acc_path, json.dumps({
                "version": LEDGER_SCHEMA_VERSION,
                **{k: getattr(account, k) for k in Account.__dataclass_fields__},
            }, ensure_ascii=False, indent=2))
            self._cash = account.initial_equity

    # ── 事件写入 ───────────────────────────────────────────

    def append(self, kind: str, payload: dict[str, Any], *, at: str) -> dict:
        """追加一个事件。at = ISO 时间戳 (调用方给市场时钟值)。返回落盘事件。"""
        with self._lock:
            self._require_writable()
            event = {**payload, "seq": self._seq + 1, "at": at, "kind": kind}
            self._validate_trade(event)
            line = json.dumps(event, ensure_ascii=False, allow_nan=False)
            with open(self._events_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq = event["seq"]
            self._apply(event)
            self._persist_materialized()
            return event

    def _validate_trade(self, event: dict) -> None:
        """只在追加边界校验交易；旧事件照常重放，未知 kind 留给上层扩展。"""
        kind = event["kind"]
        if kind == EVENT_CORP_ACTION:
            self._validate_corp_action(event)
            return
        if kind not in (EVENT_FILL, EVENT_PARTIAL_CLOSE, EVENT_CLOSE):
            return
        sym = event.get("symbol")
        if not isinstance(sym, str) or not sym.strip():
            raise ValueError("成交 symbol 不能为空")
        side = event.get("side") if kind == EVENT_FILL else "close"
        if side not in ("buy", "sell", "short", "close"):
            raise ValueError("成交 side 必须为 buy / sell / short")

        def number(name: str, default=None) -> float:
            value = event.get(name, default)
            if value is None or isinstance(value, bool):
                raise ValueError(f"{name} 必须是有限数值")
            try:
                result = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{name} 必须是有限数值") from exc
            if not math.isfinite(result):
                raise ValueError(f"{name} 必须是有限数值")
            return result

        qty, price, fee = number("qty"), number("price"), number("fee", 0.0)
        if qty <= 0 or price <= 0 or fee < 0:
            raise ValueError("成交 qty/price 必须为正，fee 不能为负")
        pos = self._positions.get(sym)
        held = pos.qty if pos else 0.0
        acct = event.get("accounting")
        if acct not in (None, CONTRACT):
            raise ValueError("accounting 只能是 contract 或缺省")
        if kind == EVENT_FILL and held != 0 and bool(acct == CONTRACT) != bool(pos and pos.contract):
            raise ValueError("同一持仓不能混用合约式与股票式核算")
        if side == "sell" and qty > held:
            raise ValueError("卖出数量超过当前持仓")
        if side == "buy" and held < 0:
            raise ValueError("持有空头时不能买入开多 (回补请用 close/partial_close)")
        if side == "short" and held > 0:
            raise ValueError("持有多头时不能卖空")
        if side == "close" and qty > abs(held) + 1e-9:
            raise ValueError("平仓数量超过当前持仓")
        notional = qty * price
        if pos is not None and pos.contract and side in ("sell", "close"):
            cash_after = self._cash + qty * abs(price - pos.avg_cost) - fee
        elif acct == CONTRACT:
            cash_after = self._cash - fee
        elif side in ("buy",) or (side == "close" and held < 0):
            cash_after = self._cash - notional - fee
        else:
            cash_after = self._cash + notional - fee
        if not math.isfinite(notional) or not math.isfinite(cash_after):
            raise ValueError("成交金额溢出")
        if side == "buy":
            cost = (pos.avg_cost * held if pos else 0.0) + notional + fee
            if not math.isfinite(held + qty) or not math.isfinite(cost / (held + qty)):
                raise ValueError("持仓数量或成本溢出")
        if event.get("stop_price") is not None:
            event["stop_price"] = number("stop_price")

    def _validate_corp_action(self, event: dict) -> None:
        """除权事件同样在写入前拒绝坏数值；同一来源 action_id 永不重复入账。"""
        sym = event.get("symbol")
        if not isinstance(sym, str) or not sym.strip():
            raise ValueError("corp_action symbol 不能为空")
        for name, positive in (("factor", True), ("cash_dividend", None)):
            if name not in event:
                continue
            value = event[name]
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} 必须是有限数值")
            if not math.isfinite(value) or (positive and value <= 0):
                raise ValueError(f"{name} 数值非法")
            if name == "cash_dividend":
                # 分红符号必须与持仓方向一致: 多头收分红 (≥0), 空头须向出借方支付 (≤0)
                pos = self._positions.get(sym)
                held = pos.qty if pos else 0.0
                if (value < 0 and held >= 0) or (value > 0 and held < 0):
                    raise ValueError("cash_dividend 符号与持仓方向不符")
        action_id = event.get("action_id")
        if action_id and any(e.get("kind") == EVENT_CORP_ACTION and e.get("action_id") == action_id
                             for e in self.read_events()):
            raise ValueError("公司行动已入账")

    # ── 事件 → 物化 (单一推演点) ────────────────────────────

    def _apply(self, event: dict) -> None:
        kind = event["kind"]
        if kind == EVENT_FILL:
            self._apply_fill(event)
        elif kind in (EVENT_PARTIAL_CLOSE, EVENT_CLOSE):
            self._apply_close(event, partial=(kind == EVENT_PARTIAL_CLOSE))
        elif kind == EVENT_STOP_MOVE:
            sym = event["symbol"]
            if sym in self._positions:
                self._positions[sym].stop_price = event.get("stop_price")
        elif kind == EVENT_CORP_ACTION:
            self._apply_corp_action(event)
        elif kind == EVENT_ADJUST:
            self._cash += float(event.get("cash_delta", 0.0))
        # opportunity/nav/intent 等未知事件不影响现金和头寸

    def _apply_fill(self, e: dict) -> None:
        qty = float(e["qty"])
        price = float(e["price"])
        fee = float(e.get("fee", 0.0))
        sym = e["symbol"]
        side = e["side"]
        pos = self._positions.get(sym)
        if pos is None or pos.qty == 0:
            # 同票归零后是新回合，不继承旧卡/建仓日/止损。
            pos = Position(symbol=sym, contract=e.get("accounting") == CONTRACT)
            self._positions[sym] = pos
        if pos.contract and side == "sell":
            # 合约式减多: 已实现盈亏入现金
            self._cash += qty * (price - pos.avg_cost) - fee
            pos.qty -= qty
            if pos.qty <= 1e-9:
                pos.qty, pos.avg_cost, pos.stop_price = 0.0, 0.0, None
            return
        if side == "short":
            # 卖空开仓: 所得入现金, 头寸记负; avg_cost = 扣费后的每股净所得 (与多头“含费成本”对称)
            # 合约式: 现金不动, 费用已计入均价 (净值与股票式一致)
            if not pos.contract:
                self._cash += qty * price - fee
            new_qty = pos.qty - qty
            pos.avg_cost = ((pos.avg_cost * abs(pos.qty)) + qty * price - fee) / abs(new_qty)
            pos.qty = new_qty
            if pos.entry_date is None:
                pos.entry_date = e.get("trade_date") or e["at"][:10]
            if pos.card_id is None:
                pos.card_id = e.get("card_id")
            if pos.stop_price is None:
                pos.stop_price = e.get("stop_price")
            return
        signed = qty if side == "buy" else -qty
        if not pos.contract:
            self._cash -= signed * price + fee
        new_qty = pos.qty + signed
        if new_qty == 0:
            pos.qty = 0.0
            pos.avg_cost = 0.0
        elif side == "buy":
            pos.avg_cost = (pos.avg_cost * pos.qty + price * qty + fee) / new_qty
            pos.qty = new_qty
            if pos.entry_date is None:
                pos.entry_date = e.get("trade_date") or e["at"][:10]
            if pos.card_id is None:
                pos.card_id = e.get("card_id")
            if pos.stop_price is None:
                pos.stop_price = e.get("stop_price")
        else:
            pos.qty = new_qty
            if pos.qty == 0:
                pos.stop_price = None

    def _apply_close(self, e: dict, *, partial: bool) -> None:
        sym = e["symbol"]
        pos = self._positions.get(sym)
        if pos is None or pos.qty == 0:
            return
        qty = float(e["qty"])
        price = float(e["price"])
        fee = float(e.get("fee", 0.0))
        if pos.contract:
            # 合约式: 只结算已实现盈亏 (多: 价 − 均价; 空: 均价 − 价), 均价不变
            self._cash += qty * ((pos.avg_cost - price) if pos.qty < 0 else (price - pos.avg_cost)) - fee
            pos.qty += qty if pos.qty < 0 else -qty
            if abs(pos.qty) <= 1e-9:
                pos.qty, pos.avg_cost, pos.stop_price = 0.0, 0.0, None
            return
        if pos.qty < 0:
            # 空头回补: 买回股票, 现金减少
            self._cash -= qty * price + fee
            pos.qty += qty
            if pos.qty >= -1e-9:
                pos.qty = 0.0
                pos.avg_cost = 0.0
                pos.stop_price = None
            return
        self._cash += qty * price - fee
        pos.qty -= qty
        if pos.qty <= 1e-9:
            pos.qty = 0.0
            pos.avg_cost = 0.0
            pos.stop_price = None

    def _apply_corp_action(self, e: dict) -> None:
        """拆股: qty × factor, avg_cost ÷ factor; 现金分红: cash_dividend 入账。"""
        sym = e["symbol"]
        factor = float(e.get("factor", 1.0))
        cash_dividend = float(e.get("cash_dividend", 0.0))
        pos = self._positions.get(sym)
        if pos and pos.qty != 0 and factor != 1.0:
            pos.qty = round(pos.qty * factor, 6)
            pos.avg_cost = round(pos.avg_cost / factor, 6)
            if pos.stop_price is not None:
                pos.stop_price = round(pos.stop_price / factor, 6)
        if cash_dividend:
            self._cash += cash_dividend

    # ── 物化持久化与重放 ────────────────────────────────────

    def _persist_materialized(self) -> None:
        snap = {
            "cash": round(self._cash, 6),
            "positions": [
                {
                    "symbol": p.symbol, "qty": p.qty, "avg_cost": p.avg_cost,
                    "entry_date": p.entry_date, "card_id": p.card_id,
                    "stop_price": p.stop_price, **({"contract": True} if p.contract else {}),
                }
                for p in sorted(self._positions.values(), key=lambda x: x.symbol)
                if p.qty != 0
            ],
            "seq": self._seq,
        }
        self._atomic_write(self.data_dir / "positions.json",
                           json.dumps(snap, ensure_ascii=False, indent=2))

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    def replay(self, account: Account, *, persist: bool = True) -> None:
        """锁内恢复；persist=False 或 read_only 时只恢复内存，不写物化文件。"""
        with self._lock:
            self._cash = account.initial_equity
            self._positions = {}
            self._seq = 0
            if not self._events_path.exists():
                return
            with open(self._events_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    self._seq = max(self._seq, int(event["seq"]))
                    self._apply(event)
            if persist and not self.read_only:
                self._persist_materialized()

    # ── 查询 ──────────────────────────────────────────────

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.qty != 0]

    def position(self, symbol: str) -> Position | None:
        p = self._positions.get(symbol)
        return p if p and p.qty != 0 else None

    def market_value(self, marks: dict[str, float]) -> float:
        """带符号市值: 多头为正, 空头为负 (回补负债); 合约式 = 浮动盈亏 qty × (现价 − 均价)。净值 = 现金 + 本值。"""
        return sum(position_value(p.qty, marks.get(p.symbol, p.avg_cost), p.avg_cost, p.contract)
                   for p in self.open_positions)

    def nav(self, marks: dict[str, float]) -> float:
        return self._cash + self.market_value(marks)

    def short_exposure(self, marks: dict[str, float] | None = None) -> float:
        """股票式空头名义金额 (正数; 合约式空头不收卖空所得, 不在此列)。"""
        marks = marks or {}
        return sum(-p.qty * marks.get(p.symbol, p.avg_cost) for p in self.open_positions if p.qty < 0 and not p.contract)

    def contract_margin(self, marks: dict[str, float] | None = None) -> float:
        """合约式回合占用的保证金 = Σ |qty| × 现价 × CONTRACT_MARGIN_RATE (多空对称)。"""
        marks = marks or {}
        return sum(abs(p.qty) * marks.get(p.symbol, p.avg_cost) * CONTRACT_MARGIN_RATE
                   for p in self.open_positions if p.contract)

    def buying_power(self, marks: dict[str, float] | None = None) -> float:
        """可用于新开仓的资金: 现金 − 2×股票式空头名义 (卖空所得 + 同额抵押) + 合约浮动盈亏 − 合约保证金。"""
        marks = marks or {}
        floating = sum(position_value(p.qty, marks.get(p.symbol, p.avg_cost), p.avg_cost, True)
                       for p in self.open_positions if p.contract)
        return self._cash - 2 * self.short_exposure(marks) + floating - self.contract_margin(marks)

    def open_risk(self) -> float:
        """组合当前总风险: 多头 Σ qty×(avg_cost − stop); 空头 Σ |qty|×(stop − avg_cost)。无止损单的多头按全额、空头按均价全额计。"""
        total = 0.0
        for p in self.open_positions:
            if p.qty > 0:
                stop = p.stop_price if p.stop_price is not None else 0.0
                total += p.qty * max(p.avg_cost - stop, 0.0)
            else:
                stop = p.stop_price if p.stop_price is not None else 2 * p.avg_cost
                total += -p.qty * max(stop - p.avg_cost, 0.0)
        return total

    def read_events(self) -> list[dict]:
        with self._lock:
            if not self._events_path.exists():
                return []
            with open(self._events_path, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]

    def snapshot(self) -> dict:
        """同一锁内复制查询数据；调用方查行情时无需阻塞台账写入。

        返回值与台账对象脱离，position/open_positions 仍保留原有活对象语义。
        """
        with self._lock:
            nav_path = self.data_dir / "nav" / "daily.jsonl"
            nav_daily = []
            if nav_path.exists():
                with open(nav_path, encoding="utf-8") as f:
                    nav_daily = [json.loads(line) for line in f if line.strip()]
            return {
                "seq": self._seq,
                "cash": self._cash,
                # contract 只在合约式回合出现 (股票式持仓的输出与旧版逐字相同)
                "positions": [{k: v for k, v in asdict(p).items() if k != "contract" or v} for p in self.open_positions],
                "open_risk": self.open_risk(),
                "events": self.read_events(),
                "nav_daily": nav_daily,
            }

    # ── 日终定版净值 ──────────────────────────────────────

    def write_nav_daily(self, day: date, marks: dict[str, float]) -> dict:
        """收盘定版: nav/daily.jsonl 追加一行。"""
        with self._lock:
            self._require_writable()
            nav_dir = self.data_dir / "nav"
            nav_dir.mkdir(parents=True, exist_ok=True)
            mv = self.market_value(marks)
            entry = {
                "date": day.isoformat(),
                "cash": round(self._cash, 2),
                "market_value": round(mv, 2),
                "nav": round(self._cash + mv, 2),
            }
            path = nav_dir / "daily.jsonl"
            existing = set()
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            try:
                                existing.add(json.loads(line)["date"])
                            except (json.JSONDecodeError, KeyError):
                                continue
            if entry["date"] not in existing:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            return entry
