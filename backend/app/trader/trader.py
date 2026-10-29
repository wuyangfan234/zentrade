"""波段纸面交易员 — 持仓生命周期 (docs/architecture.md)。

状态机: filled → managed → closed
三条出场线 (全部来自机会卡, 不临场发明):
- 目标区: 触下沿卖一半 + 余仓止损上移至保本; 触上沿清仓
- 失效点: 收盘跌破 → 次日开盘清仓 (不用盘中瞬时价, 防 Wick 假击穿)
- 时间止损: 第 7 个交易日 15:30 ET 无条件市价清仓 (无任何豁免路径)

移动止损 (只上移不下移):
- 浮盈 ≥1R → 止损上移至入场价 (保本)
- 浮盈 ≥2R → 止损移至 +1R 锁定

做空 (2026-10-03): 卡 side="short" 时全部镜像 — 入场卖空 (fill side="short"), 目标区在下方
(价格 ≤ 目标近端 thi 回补一半, ≤ 远端 tlo 全部回补), 收盘站上失效/止损 → 次日开盘回补,
移动止损只下移不上移。占用购买力 = 名义金额的 2 倍 (卖空所得 + 同额抵押), 不加杠杆。

跳空如实话 (docs/architecture.md): 失效判定用收盘价, 次日开盘价成交 — 隔夜跳空亏损
如实入账, 这正是纸面验证的价值。
"""
from __future__ import annotations

import math
from datetime import date, datetime

from app.ledger.core import CONTRACT, CONTRACT_MARGIN_RATE, Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.trader.sizing import MIN_CASH_DOWNSIZE_RATIO, downsize_for_cash, fee_for, size_position

def is_open_fill(e: dict) -> bool:
    """开仓成交 (多头买入或卖空)。"""
    return e.get("kind") == "fill" and e.get("side") in ("buy", "short")


class SwingTrader:
    """纸面撮合。检查与落账共用 Ledger 的 RLock；进程内唯一账本写入者。"""

    def __init__(self, ledger: Ledger, account: Account) -> None:
        self.ledger = ledger
        self.account = account
        self._cards: dict[str, OpportunityCard] = {}
        self.quote_feed: str | None = None  # 当前撮合报价来源 (tickflow / iex), 写入成交事件供审计

    # ── 入场 ─────────────────────────────────────────────

    def try_fill_entry(
        self,
        card: OpportunityCard,
        *,
        price: float,
        trade_date: date,
        at: str,
        quote_ts: str | datetime | None = None,
        marks: dict[str, float] | None = None,
    ) -> dict | None:
        """首帧入区只记 entry_intent；后续更新帧按新价撮合，仅返回 fill。

        at 是执行时点，quote_ts 是源行情时点。省略 quote_ts 时仅为旧调用/离线
        回放以 at 兼容；真实 Loop 必须显式传源 timestamp，并在上游拒绝陈旧报价。
        intent/拒单水位均落账，重启不把同一源帧当成下一帧。卡生命周期由调用方管。
        """
        if card.state != CardState.ACTIVE:
            return None
        timing = self._quote_times(price, trade_date, at, quote_ts)
        if timing is None:
            return None
        now, quoted = timing
        if not us_clock.in_entry_window(now) or not us_clock.in_entry_window(quoted):
            return None
        with self.ledger._lock:
            # 空头回补负债随现价变化，不能按历史成本释放购买力。
            # 生产传入同一轮已验证新鲜的marks；已有空头时缺任一持仓估值则拒新意向/新成交。
            valuation = marks or {}
            positions = self.ledger.open_positions
            if any(p.qty < 0 and not p.contract for p in positions):   # 合约式空头无回补负债, 见 missing_valuation
                for pos in positions:
                    mark = valuation.get(pos.symbol)
                    if (isinstance(mark, bool) or not isinstance(mark, (int, float))
                            or not math.isfinite(mark) or mark <= 0):
                        return None
            history = self.ledger.read_events()
            if self.ledger.position(card.symbol) is not None:
                return None
            buys = [e for e in history if is_open_fill(e)]
            if any(e.get("card_id") == card.id for e in buys):
                return None  # 同卡一生一个回合，平仓后仍不可复开
            symbol_buys = [e for e in buys if e.get("symbol") == card.symbol]
            if symbol_buys and quoted <= us_clock.market_datetime(symbol_buys[-1].get("quote_ts") or symbol_buys[-1]["at"]):
                return None
            intents = [e for e in history if e["kind"] == "entry_intent" and e.get("card_id") == card.id]
            intent = intents[-1] if intents else None
            if intent and quoted <= max(us_clock.market_datetime(intent["quote_ts"]), us_clock.market_datetime(intent["at"])):
                return None  # 源帧递增仍可能早于下单决策；下一帧必须严格晚于intent.at
            # 被容量拦下的意向已被消费；下一次撮合必须重新由入区快照触发，不能在区外追价。
            if not intent or intent["trade_date"] != trade_date.isoformat() or intent.get("status") == "blocked":
                lo, hi = card.entry_zone
                if not lo <= price <= hi:
                    return None
                self.ledger.append("entry_intent", {
                    "symbol": card.symbol, "card_id": card.id, "price": price,
                    "quote_ts": quoted.isoformat(), "trade_date": trade_date.isoformat(),
                    "proposal": card.to_dict(), "status": "pending",
                }, at=at)
                return None

            frozen = OpportunityCard.from_dict(intent["proposal"])
            # 0.16.1: 后续帧同样必须在入场区内、且在失效价内侧; 不满足不成交 (intent 保留, 等下一帧)
            zlo, zhi = frozen.entry_zone
            if not zlo <= price <= zhi:
                return None
            if (price >= frozen.invalidation) if frozen.is_short else (price <= frozen.invalidation):
                return None
            current_decision = (card.evidence or {}).get("decision") or {}
            frozen_decision = (frozen.evidence or {}).get("decision") or {}
            # Guard metadata is execution-time authority; legacy callers have only
            # the card tier (or, for old pending intents, the frozen proposal).
            entry_tier = (current_decision.get("entry_tier") or current_decision.get("tier")
                          or frozen_decision.get("entry_tier") or frozen_decision.get("tier"))
            short = frozen.is_short
            # 自选机会卡下单簿 (2026-10-10): 合约式核算 —— 只结算盈亏, 多空对称按名义 × 保证金率占用购买力
            contract = frozen_decision.get("book") is True
            equity = self.ledger.nav(valuation)  # 与购买力使用同一轮持仓估值
            planned_qty = qty = size_position(frozen, equity, self.account)
            slip = self.account.slippage_bps / 10000
            fill_price = round(price * (1 - slip if short else 1 + slip), 4)
            # 卖空占用 2×名义 (所得 + 同额抵押, 所得记入现金); 做多占用 1×名义 — 两者对购买力的净占用都是
            # 1×名义 + 佣金, 不加杠杆。现金不足 → 按剩余可用资金缩量 (2026-10-05 产品决策), 缩到原计划 25% 以下不成交。
            available = self.ledger.buying_power(valuation)
            sizing = None
            reason = None
            # 单笔风险上限 (2026-10-12 产品决策): 股数按建卡区间中点算, 实际成交价离失效价更远时风险会超预算
            # (10-09 实测最大 1.15 倍)。按成交价重算: 实际风险 = 股数 * |成交价 - 失效价| <= 权益 * 单笔风险。
            actual_rps = (frozen.invalidation - fill_price) if short else (fill_price - frozen.invalidation)
            risk_cap = None
            if actual_rps <= 0:
                return None  # 成交价已在失效价外侧 (含滑点): 拒单, 不以 0 风险入账
            if qty > 0 and equity > 0:
                capped = int(equity * self.account.risk_per_trade_pct / actual_rps + 1e-9)
                if capped < qty:
                    risk_cap = {"planned_qty": qty, "qty": capped,
                                "budget": round(equity * self.account.risk_per_trade_pct, 2),
                                "actual_risk_per_share": round(actual_rps, 4)}
                    planned_qty = qty = capped
            if qty <= 0:
                reason = "zero_size"
            elif len(self.ledger.open_positions) >= self.account.max_positions:
                reason = "max_positions"
            elif qty * fill_price * (CONTRACT_MARGIN_RATE if contract else 1) + fee_for(qty, self.account) > available:
                qty = downsize_for_cash(planned_qty, fill_price * (CONTRACT_MARGIN_RATE if contract else 1),
                                        available, self.account)
                if qty <= 0 or qty < planned_qty * MIN_CASH_DOWNSIZE_RATIO:
                    reason = "cash"
                else:
                    sizing = {"planned_qty": planned_qty, "qty": qty, "reason": "cash_downsized",
                              "available_cash": round(available, 2), "ratio": round(qty / planned_qty, 4)}
            fee = fee_for(qty, self.account)
            new_risk = qty * (max(frozen.invalidation - fill_price, 0) if short else max(fill_price - frozen.invalidation, 0))
            if reason is None and self.ledger.open_risk() + new_risk + fee > equity * self.account.max_portfolio_risk_pct:
                reason = "portfolio_risk"
            if reason:
                # 消费此失败快照；不能在同一源帧上等现金/额度变化后偷成交。
                self.ledger.append("entry_intent", {
                    "symbol": card.symbol, "card_id": card.id, "price": price,
                    "quote_ts": quoted.isoformat(), "trade_date": trade_date.isoformat(),
                    "proposal": intent["proposal"], "status": "blocked", "reason": reason,
                }, at=at)
                return None
            return self.ledger.append("fill", {
                "symbol": card.symbol, "side": "short" if short else "buy", "qty": qty, "price": fill_price,
                "fee": fee, "trade_date": trade_date.isoformat(), "card_id": card.id,
                "setup_type": frozen.setup_type, "stop_price": frozen.invalidation,
                "invalidation": frozen.invalidation, "target_zone": list(frozen.target_zone),
                "risk_per_share": frozen.risk_per_share, "max_holding_days": frozen.max_holding_days,
                "quote_ts": quoted.isoformat(), "entry_intent_seq": intent["seq"],
                # Permission can change after the frozen price proposal. Quota replay
                # must use the actual execution tier, not the first-frame tier.
                "entry_tier": entry_tier,
                **({"accounting": CONTRACT, "risk_pct": self.account.risk_per_trade_pct} if contract else {}),
                **({"sizing": sizing} if sizing else {}),
                **({"risk_cap": risk_cap} if risk_cap else {}),
                **({"quote_feed": self.quote_feed} if self.quote_feed else {}),
            }, at=at)

    @staticmethod
    def _quote_times(price: float, trade_date: date, at: str,
                     quote_ts: str | datetime | None) -> tuple[datetime, datetime] | None:
        try:
            now = us_clock.market_datetime(at)
            quoted = us_clock.market_datetime(at if quote_ts is None else quote_ts)
            if not math.isfinite(price) or price <= 0:
                return None
        except (ValueError, TypeError, AttributeError):
            return None
        if now.date() != trade_date or quoted.date() != trade_date or quoted > now:
            return None
        return now, quoted

    # ── 持仓管理：收盘只判失效，次开盘执行；盘中管理不看开仓开关 ──

    def manage_close(
        self, symbol: str, *, close_price: float, trade_date: date, at: str,
    ) -> list[dict]:
        """消费定版收盘价。跌破失效/移动止损只记 exit_intent，不在收盘价卖出。"""
        if self._quote_times(close_price, trade_date, at, None) is None or not us_clock.is_trading_day(trade_date):
            return []
        with self.ledger._lock:
            pos = self.ledger.position(symbol)
            if pos is None:
                return []
            plan, history = self._round_context(pos)
            evaluated_at = us_clock.market_datetime(at)
            if trade_date < date.fromisoformat(plan["trade_date"]) or any(us_clock.market_datetime(e["at"]) > evaluated_at for e in history):
                return []  # 不对新仓或已推进的持仓状态应用历史收盘
            if self._pending_exit(plan, history) is not None:
                return []
            invalidation = plan.get("invalidation")
            reason = None
            if pos.qty < 0:  # 空头: 收盘站上失效/止损
                if invalidation is not None and close_price > invalidation:
                    reason = "invalidation"
                elif pos.stop_price is not None and close_price > pos.stop_price:
                    reason = "trailing_stop"
            elif invalidation is not None and close_price < invalidation:
                reason = "invalidation"
            elif pos.stop_price is not None and close_price < pos.stop_price:
                reason = "trailing_stop"
            if reason:
                return [self.ledger.append("exit_intent", {
                    "symbol": symbol, "card_id": plan.get("card_id"), "entry_seq": plan["seq"],
                    "reason": reason, "signal_price": close_price,
                    "trade_date": trade_date.isoformat(),
                }, at=at)]
            moved = self._move_stop(pos, plan, close_price, history, trade_date, at)
            return [moved] if moved else []

    def execute_pending_exit(
        self, symbol: str, *, open_price: float, trade_date: date, at: str,
    ) -> dict | None:
        """下一可用交易日开盘执行 pending exit；open_price 必须为该日真实开盘价。

        调用方在取得开盘价后调用，不得用收盘/失效点/最新快照伪装开盘价。
        允许停机恢复后在后续开盘补执行；同日、盘前、休市和已闭合回合不执行。
        """
        timing = self._quote_times(open_price, trade_date, at, None)
        if timing is None or not us_clock.in_session(timing[0]):
            return None
        with self.ledger._lock:
            pos = self.ledger.position(symbol)
            if pos is None:
                return None
            plan, history = self._round_context(pos)
            intent = self._pending_exit(plan, history)
            if intent is None or trade_date <= date.fromisoformat(intent["trade_date"]):
                return None
            open_at = datetime.combine(trade_date, us_clock.sessions[0].open, tzinfo=us_clock.tz)
            if open_at <= us_clock.market_datetime(intent["at"]):
                return None
            return self._close_all(pos, open_price, trade_date, at, reason=intent["reason"],
                                   plan=plan, exit_intent_seq=intent["seq"])

    def manage_intraday(
        self, symbol: str, *, price: float, trade_date: date, at: str,
        quote_ts: str | datetime | None = None,
    ) -> list[dict]:
        """常规盘中：时间强退 → 目标上沿全平 → 下沿单次减半 → 单向移动止损。

        不依赖卡 ACTIVE/开仓时段/开仓开关。quote_ts 兼容规则同 try_fill_entry。
        失效/移动止损不做盘中 Wick 止损，必须由 manage_close 判定。
        """
        timing = self._quote_times(price, trade_date, at, quote_ts)
        if timing is None:
            return []
        now, quoted = timing
        if not us_clock.in_session(now) or not us_clock.in_session(quoted):
            return []
        with self.ledger._lock:
            pos = self.ledger.position(symbol)
            if pos is None:
                return []
            plan, history = self._round_context(pos)
            watermarks = [us_clock.market_datetime(e["quote_ts"]) for e in history if e.get("quote_ts")]
            if watermarks and quoted <= max(watermarks):
                return []
            entry_day = date.fromisoformat(plan["trade_date"])
            max_days = min(plan["max_holding_days"], self.account.max_holding_days)
            if us_clock.holding_exit_due(entry_day, max_days, now):
                return [self._close_all(pos, price, trade_date, at, reason="time_stop", plan=plan,
                                        quote_ts=quoted.isoformat())]
            if self._pending_exit(plan, history) is not None:
                return []  # 已有次开盘单不能拿最新价偷换；硬时间止损仍优先
            target = plan.get("target_zone")
            short = pos.qty < 0
            # 远端 = 全平 (多: 上沿; 空: 下沿 target[0]); 近端 = 减半 (多: 下沿; 空: 上沿 target[1])
            far_hit = target and (price <= target[0] if short else price >= target[1])
            near_hit = target and (price <= target[1] if short else price >= target[0])
            if far_hit:
                return [self._close_all(pos, price, trade_date, at, reason="target_zone_upper", plan=plan,
                                        quote_ts=quoted.isoformat())]
            events: list[dict] = []
            partial_taken = any(e["kind"] == "partial_close" and e.get("reason") == "target_zone_lower" for e in history)
            if near_hit and not partial_taken and pos.size >= 2:
                qty = int(pos.size // 2)
                event = self.ledger.append("partial_close", {
                    **({"position_side": "short"} if short else {}),
                    "symbol": symbol, "qty": qty, "price": self._exit_price(price, short),
                    "fee": fee_for(qty, self.account), "reason": "target_zone_lower",
                    "card_id": plan.get("card_id"), "entry_seq": plan["seq"],
                    "trade_date": trade_date.isoformat(), "quote_ts": quoted.isoformat(),
                }, at=at)
                events.append(event)
                history.append(event)
            moved = self._move_stop(pos, plan, price, history, trade_date, at, quoted.isoformat())
            if moved:
                events.append(moved)
            return events

    def reduce_for_event(self, symbol: str, *, price: float, trade_date: date, at: str,
                         quote_ts: str | datetime | None, reason: str) -> dict | None:
        """事件前减半 (财报等): 每回合每种 reason 只做一次, 对当前剩余持仓减半。

        与目标区下沿减半独立叠加: 已因目标下沿减过半的回合, 财报前对剩余再减半。
        只接受本回合建仓之后、严格递增的新鲜源帧, 与盘中管理同一水位。
        """
        timing = self._quote_times(price, trade_date, at, quote_ts)
        if timing is None:
            return None
        now, quoted = timing
        if not us_clock.in_session(now) or not us_clock.in_session(quoted):
            return None
        with self.ledger._lock:
            pos = self.ledger.position(symbol)
            if pos is None or pos.size < 2:
                return None
            plan, history = self._round_context(pos)
            if any(e["kind"] == "partial_close" and e.get("reason") == reason for e in history):
                return None
            watermarks = [us_clock.market_datetime(e["quote_ts"]) for e in history if e.get("quote_ts")]
            if watermarks and quoted <= max(watermarks):
                return None
            qty = int(pos.size // 2)
            return self.ledger.append("partial_close", {
                **({"position_side": "short"} if pos.qty < 0 else {}),
                "symbol": symbol, "qty": qty, "price": self._exit_price(price, pos.qty < 0),
                "fee": fee_for(qty, self.account), "reason": reason,
                "card_id": plan.get("card_id"), "entry_seq": plan["seq"],
                "trade_date": trade_date.isoformat(), "quote_ts": quoted.isoformat(),
            }, at=at)

    @staticmethod
    def _pending_exit(plan: dict, history: list[dict]) -> dict | None:
        """只承认在本轮建仓之后、收盘之后形成的退出意向；旧坏事件不触发倒时成交。"""
        entry_day = date.fromisoformat(plan["trade_date"])
        entry_at = us_clock.market_datetime(plan.get("at") or datetime.combine(entry_day, us_clock.sessions[0].open))
        for event in reversed(history):
            if event["kind"] != "exit_intent":
                continue
            signal_day = date.fromisoformat(event["trade_date"])
            decided_at = us_clock.market_datetime(event["at"])
            close_at = datetime.combine(signal_day, us_clock.sessions[-1].close, tzinfo=us_clock.tz)
            if signal_day >= entry_day and decided_at >= max(entry_at, close_at):
                return event
        return None

    def _round_context(self, pos) -> tuple[dict, list[dict]]:
        """fill 冻结提案优先；旧台账才回退卡册。按本轮 entry seq 隔离历史。"""
        history = [e for e in self.ledger.read_events() if e.get("symbol") == pos.symbol]
        entry = next((e for e in reversed(history) if is_open_fill(e)), {})
        card = self._card_of(pos)
        plan = {
            "seq": entry.get("seq", 0), "card_id": pos.card_id,
            "risk_per_share": card.risk_per_share if card else 0,
            "max_holding_days": card.max_holding_days if card else self.account.max_holding_days,
            "invalidation": card.invalidation if card else entry.get("stop_price"),
            "target_zone": card.target_zone if card else None,
            "trade_date": pos.entry_date or entry.get("at", "")[:10],
        }
        plan.update({k: v for k, v in entry.items() if v is not None})
        rounds = [e for e in history if e["seq"] >= plan["seq"]]
        # 冻结 fill 不改写（append-only）；本回合建仓后的拆股/反拆按因子累乘，
        # 价格类计划字段除以累计因子，与台账 qty×factor / 价格÷factor 同口径。
        # 每股风险同比缩放、股数同比放大 → 美元风险与回合 R 不变。
        factor = 1.0
        for e in rounds:
            if e["kind"] == "corp_action" and e["seq"] > plan["seq"]:
                factor *= float(e.get("factor") or 1.0)
        if factor != 1.0:
            for key in ("invalidation", "risk_per_share", "price", "stop_price"):
                if isinstance(plan.get(key), (int, float)):
                    plan[key] = plan[key] / factor
            if plan.get("target_zone"):
                plan["target_zone"] = [v / factor for v in plan["target_zone"]]
        plan["split_factor"] = factor
        return plan, rounds

    def _move_stop(self, pos, plan: dict, price: float, history: list[dict],
                   trade_date: date, at: str, quote_ts: str | None = None) -> dict | None:
        risk = plan["risk_per_share"]
        short = pos.qty < 0
        r_now = ((pos.avg_cost - price) if short else (price - pos.avg_cost)) / risk if risk > 0 else 0
        partial_taken = any(e["kind"] == "partial_close" and e.get("reason") == "target_zone_lower" for e in history)
        if short:  # 空头止损只下移
            current = pos.stop_price if pos.stop_price is not None else float("inf")
            desired = current
            if partial_taken or r_now >= 1:
                desired = min(desired, pos.avg_cost)
            if r_now >= 2:
                desired = min(desired, pos.avg_cost - risk)
            desired = round(desired, 4) if desired != float("inf") else desired
            if desired >= current - 1e-9:
                return None
        else:
            current = pos.stop_price or 0
            desired = current
            if partial_taken or r_now >= 1:
                desired = max(desired, pos.avg_cost)
            if r_now >= 2:
                desired = max(desired, pos.avg_cost + risk)
            desired = round(desired, 4)
            if desired <= current + 1e-9:
                return None
        return self.ledger.append("stop_move", {
            "symbol": pos.symbol, "card_id": plan.get("card_id"), "entry_seq": plan["seq"],
            "stop_price": desired, "reason": "breakeven_after_partial" if partial_taken and r_now < 2 else f"trailing_{r_now:.1f}R",
            "trade_date": trade_date.isoformat(), "quote_ts": quote_ts,
        }, at=at)

    def _sell_price(self, price: float) -> float:
        return round(price * (1 - self.account.slippage_bps / 10000), 4)

    def _exit_price(self, price: float, short: bool) -> float:
        """平仓成交价: 多头卖出吃滑点向下, 空头买回吃滑点向上。"""
        return round(price * (1 + self.account.slippage_bps / 10000), 4) if short else self._sell_price(price)

    def _close_all(self, pos, price: float, trade_date: date, at: str,
                   *, reason: str, plan: dict, **execution) -> dict:
        short = pos.qty < 0
        qty = pos.size
        fee = fee_for(qty, self.account)
        fill_price = self._exit_price(price, short)
        risk = plan["risk_per_share"]
        move = (pos.avg_cost - fill_price) if short else (fill_price - pos.avg_cost)
        r = (move * qty - fee) / (qty * risk) if risk > 0 else 0
        return self.ledger.append("close", {
            **({"position_side": "short"} if short else {}),
            "symbol": pos.symbol, "qty": qty, "price": fill_price, "fee": fee,
            "reason": reason, "card_id": plan.get("card_id"), "entry_seq": plan["seq"],
            "r_multiple": round(r, 2), "trade_date": trade_date.isoformat(), **execution,
            **({"quote_feed": self.quote_feed} if self.quote_feed else {}),
        }, at=at)

    # ── 卡索引 (台账事件里存了 card_id, 这里由外部注入卡册) ──

    def register_card(self, card: OpportunityCard) -> None:
        with self.ledger._lock:
            self._cards[card.id] = card

    def _card_of(self, pos) -> OpportunityCard | None:
        return self._cards.get(pos.card_id or "")
