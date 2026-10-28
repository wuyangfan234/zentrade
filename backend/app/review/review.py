"""只读复盘：归零才闭合一回合，分批退出按 FIFO 分摊入场费用。

R 分母冻结于首填风险，不随移动止损/拆股变化。缺风险的历史回合仍计
净盈亏/胜率，但 R 为 None，不伪造为 0R。复盘绝不 replay/改写台账。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
import json
import math
from pathlib import Path


@dataclass
class RoundTrip:
    symbol: str
    card_id: str | None
    setup_type: str
    entry_date: date
    exit_date: date
    qty_opened: float
    avg_entry: float
    avg_exit: float
    pnl: float
    planned_risk: float
    r_multiple: float | None
    exit_reason: str
    total_fees: float = 0.0
    exit_reason_contributions: dict[str, dict] = field(default_factory=dict)
    backfill: bool = False                # 首笔开仓是事后回补 (逐分钟重放补记), 不是实时成交


@dataclass
class SetupStats:
    setup_type: str
    count: int = 0
    wins: int = 0
    r_count: int = 0
    total_r: float | None = None
    avg_r: float | None = None
    win_rate: float = 0.0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float | None = None


def _event_day(event: dict) -> date:
    return date.fromisoformat(str(event.get("trade_date") or event["at"][:10]))


@dataclass
class RoundTripState:
    """多头: cost = 买入支出, proceeds = 卖出所得。空头: cost = 回补支出, proceeds = 卖空所得 (镜像, pnl 同一公式)。"""
    first: dict
    lots: list[dict] = field(default_factory=list)
    qty_opened: float = 0.0
    cost: float = 0.0
    proceeds: float = 0.0
    qty_exited: float = 0.0
    fees: float = 0.0
    contributions: dict[str, dict] = field(default_factory=dict)
    cash_dividends: float = 0.0
    split_factor: float = 1.0
    entry_fees: float = 0.0
    exit_qty_basis: float = 0.0
    exits: list[dict] = field(default_factory=list)
    stop_price: float | None = None

    @property
    def short(self) -> bool:
        return self.first.get("side") == "short"

    @property
    def entry_notional(self) -> float:
        return self.proceeds + self.entry_fees if self.short else self.cost - self.entry_fees

    @property
    def pnl(self) -> float:
        """Unrounded net cash flow; round only the displayed dollar amount."""
        return self.proceeds - self.cost + self.cash_dividends

    def corp_action(self, event: dict) -> None:
        factor = float(event.get("factor") or 1)
        if not math.isfinite(factor) or factor <= 0:
            return
        if factor != 1:
            self.split_factor *= factor
            for lot in self.lots:
                lot["qty"] = round(lot["qty"] * factor, 6)
            if self.stop_price is not None:
                self.stop_price /= factor
        # Signed dollar cash flow is separate from execution prices / lot costs.
        # A dividend belongs to the open round, not to a synthetic trade or fee.
        dividend = float(event.get("cash_dividend") or 0)
        if math.isfinite(dividend) and dividend:
            self.cash_dividends += dividend
            item = self.contributions.setdefault("cash_dividend", {"qty": 0.0, "pnl": 0.0, "fees": 0.0})
            item["pnl"] += dividend

    def add_fill(self, event: dict) -> None:
        qty, price, fee = float(event["qty"]), float(event["price"]), float(event.get("fee") or 0)
        # Keep opening quantity in first-fill units so a later post-split fill
        # cannot multiply the frozen dollar-risk denominator by the split.
        self.qty_opened += qty / self.split_factor
        self.fees += fee
        self.entry_fees += fee
        if self.short:
            # 卖空所得 (扣费) 计入 proceeds; lot 记“入场费”供分摊
            self.proceeds += qty * price - fee
            self.lots.append({"qty": qty, "cost": qty * price - fee, "fee": fee})
            return
        cost = qty * price + fee
        self.cost += cost
        self.lots.append({"qty": qty, "cost": cost, "fee": fee})

    def exit(self, event: dict) -> bool:
        qty = float(event["qty"])
        remaining = sum(lot["qty"] for lot in self.lots)
        # 标签不能替代持仓数；损坏/超量退出不能伪造闭合回合。
        if qty > remaining and not math.isclose(qty, remaining, rel_tol=1e-10, abs_tol=1e-12):
            return False
        fee = float(event.get("fee") or 0)
        if self.short:
            buyback = qty * float(event["price"]) + fee
        proceeds = qty * float(event["price"]) - fee
        needed, cost, entry_fee = min(qty, remaining), 0.0, 0.0
        for lot in self.lots:
            if needed <= 0 or lot["qty"] <= 0:
                continue
            used = min(needed, lot["qty"])
            portion = used / lot["qty"]
            allocated_cost, allocated_fee = lot["cost"] * portion, lot["fee"] * portion
            cost += allocated_cost
            entry_fee += allocated_fee
            lot["qty"] -= used
            lot["cost"] -= allocated_cost
            lot["fee"] -= allocated_fee
            needed -= used
        self.qty_exited += qty
        self.exit_qty_basis += qty / self.split_factor
        self.exits.append(event)
        self.fees += fee
        reason = str(event.get("exit_reason") or event.get("reason") or "unknown")
        contribution = self.contributions.setdefault(reason, {"qty": 0.0, "pnl": 0.0, "fees": 0.0})
        contribution["qty"] += qty
        if self.short:
            self.cost += buyback
            contribution["pnl"] += cost - buyback  # 这部分卖空所得 − 回补支出
        else:
            self.proceeds += proceeds
            contribution["pnl"] += proceeds - cost
        contribution["fees"] += entry_fee + fee
        return math.isclose(sum(lot["qty"] for lot in self.lots), 0.0, abs_tol=1e-12)

    def closed(self, event: dict) -> RoundTrip:
        rps = float(self.first.get("risk_per_share") or 0)
        risk = self.qty_opened * rps if math.isfinite(rps) and rps > 0 else 0.0
        pnl = self.pnl
        contributions = {
            reason: {**item, "r_multiple": item["pnl"] / risk if risk > 0 else None}
            for reason, item in self.contributions.items()
        }
        if self.short:
            avg_entry, avg_exit = self.proceeds / self.qty_opened, self.cost / self.qty_exited
        else:
            avg_entry, avg_exit = self.cost / self.qty_opened, self.proceeds / self.qty_exited
        return RoundTrip(
            symbol=self.first["symbol"], card_id=self.first.get("card_id"),
            setup_type=self.first.get("setup_type") or "unknown",
            entry_date=_event_day(self.first), exit_date=_event_day(event),
            qty_opened=self.qty_opened, avg_entry=avg_entry,
            avg_exit=avg_exit, pnl=round(pnl, 2),
            planned_risk=risk, r_multiple=pnl / risk if risk > 0 else None,
            exit_reason=str(event.get("exit_reason") or event.get("reason") or "unknown"),
            total_fees=self.fees, exit_reason_contributions=contributions,
            backfill=bool(self.first.get("backfill")),
        )


def build_round_states(events: list[dict]) -> tuple[list[RoundTripState], dict[str, RoundTripState]]:
    """Review/API 共用只读计算器；按 symbol 持仓实际归零划分回合。

    拆股只改数量/每股价格单位，美元成本与风险不变；分红为有符号现金流。
    同 card 归零重开不继承旧回合。缺 card 的旧台账仍按同票持仓配对。
    未知事件忽略，输入事件不变，不读取或改写任何台账文件。
    """
    closed: list[RoundTripState] = []
    opens: dict[str, RoundTripState] = {}
    for event in events:
        kind, symbol = event.get("kind"), event.get("symbol")
        if not symbol:
            continue
        if kind == "corp_action":
            if symbol in opens:
                opens[symbol].corp_action(event)
            continue
        if kind == "stop_move" and symbol in opens:
            opens[symbol].stop_price = event.get("stop_price")
        if kind not in ("fill", "partial_close", "close"):
            continue
        if kind == "fill" and event.get("side", "buy") not in ("buy", "short"):
            continue
        qty = float(event.get("qty") or 0)
        if not math.isfinite(qty) or qty <= 0:
            continue
        if kind == "fill":
            opens.setdefault(symbol, RoundTripState(event, stop_price=event.get("stop_price"))).add_fill(event)
        elif symbol in opens:
            state = opens[symbol]
            if state.exit(event):
                closed.append(state)
                del opens[symbol]
    return closed, opens


def build_round_trips(events: list[dict]) -> list[RoundTrip]:
    """按追加顺序返回闭合回合；退出事件的 r_multiple 不是整回合 R。"""
    closed, _ = build_round_states(events)
    return [state.closed(state.exits[-1]) for state in closed]


def setup_stats(trips: list[RoundTrip]) -> list[SetupStats]:
    """胜率以闭合回合净盈亏统计；R 指标排除缺冻结风险的回合。"""
    groups: dict[str, list[RoundTrip]] = defaultdict(list)
    for trip in trips:
        groups[trip.setup_type].append(trip)
    out = []
    for setup, items in sorted(groups.items()):
        values = [t.r_multiple for t in items if t.r_multiple is not None]
        gross_win = sum(r for r in values if r > 0)
        gross_loss = -sum(r for r in values if r < 0)
        wins = sum(t.pnl > 0 for t in items)
        out.append(SetupStats(
            setup_type=setup, count=len(items), wins=wins, r_count=len(values),
            total_r=round(sum(values), 2) if values else None,
            avg_r=round(sum(values) / len(values), 2) if values else None,
            win_rate=round(wins / len(items), 3),
            gross_win=round(gross_win, 2), gross_loss=round(gross_loss, 2),
            profit_factor=round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        ))
    return out


MOOD_LABEL = {"up": "涨", "down": "跌", "range": "震荡", None: "无判断"}


def mood_breakdown(data_dir: Path, trips: list[RoundTrip]) -> dict:
    """按开仓日的盘前大盘情绪分组 (2026-10-12)。只读关联 market_mood/judgments 与 scores, 不改台账。

    by_stance: 当天盘前判断的倾向; by_outcome: 当天 QQQ 实际走势 (16:10 记分)。
    回补回合不计入 (不是实时决策)。样本少时只呈现数字, 不下结论。
    """
    from app.services import market_mood

    scores = market_mood.load_scores(data_dir)
    stance_of: dict[str, str | None] = {}
    groups: dict[str, dict[str, list[RoundTrip]]] = {"by_stance": defaultdict(list), "by_outcome": defaultdict(list)}
    for t in trips:
        if t.backfill:
            continue
        d = t.entry_date.isoformat()
        if d not in stance_of:
            j = market_mood.load_judgment(data_dir, t.entry_date)
            stance_of[d] = j.get("stance") if j else None
        groups["by_stance"][stance_of[d] or "none"].append(t)
        groups["by_outcome"][(scores.get(d) or {}).get("outcome") or "none"].append(t)
    out = {}
    for name, g in groups.items():
        rows = []
        for key in ("up", "range", "down", "none"):
            items = g.get(key) or []
            if not items:
                continue
            rs = [t.r_multiple for t in items if t.r_multiple is not None]
            rows.append({"key": key, "label": MOOD_LABEL.get(None if key == "none" else key), "count": len(items),
                         "wins": sum(t.pnl > 0 for t in items),
                         "avg_r": round(sum(rs) / len(rs), 2) if rs else None, "r_count": len(rs),
                         "pnl": round(sum(t.pnl for t in items), 2)})
        out[name] = rows
    return out


def weekly_report(data_dir: Path, week_end: date) -> dict:
    """只读事件流；区间为 week_end 前六天至当天（共七天，含两端）。"""
    ledger_dir = Path(data_dir) / "ledger"
    events_path = ledger_dir / "events.jsonl"
    events = []
    if events_path.exists():
        with events_path.open(encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
    trips = build_round_trips(events)
    week_start = week_end - timedelta(days=6)
    week_trips = [t for t in trips if week_start <= t.exit_date <= week_end]
    stats = setup_stats(week_trips)
    report = {
        "week_start": week_start.isoformat(), "week_end": week_end.isoformat(),
        "trips": len(week_trips), "by_setup": [vars(s) for s in stats],
        # 仅提示复核，不代表已满足连续四周期望为负或自动执行降级。
        "degrade_candidates": [s.setup_type for s in stats
                               if s.r_count >= 5 and s.avg_r is not None and s.avg_r < 0],
        "nav_latest": None,
    }
    try:  # 全部已闭合实时回合按大盘情绪分组 (累计, 不限本周; 样本攒得慢)
        report["by_mood"] = mood_breakdown(Path(data_dir), trips)
    except Exception as exc:
        report["by_mood"] = {"error": type(exc).__name__}
    if not (ledger_dir / "account.json").exists():
        report["note"] = "台账未初始化"
    return report
