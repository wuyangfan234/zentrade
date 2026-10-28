#!/usr/bin/env python3
"""S1/S2 setup 真实规则重放 (批1, 取代 replay_breakout.py 的错误口径)。

与旧回放的区别:
- 选股用生产同一个 ``detect_candidates`` (含 ATR 地板/反垃圾/RS 横截面), 横截面只含
  通过生产流动性门槛的标的 (与 OpportunityService._load_universe 同口径);
- 复权按时点 (point-in-time): 扫描日 D 只使用 D 及之前生效的除权事件, 不用未来拆股
  回溯改写当时的价格/成交量;
- 撮合与持仓管理调用生产 ``SwingTrader`` (临时目录真账本, 不碰生产台账), 全部价格为
  当日实际成交价 (as-traded), 股数与每股佣金因而与实盘同口径:
  入区首帧只登记意向、下一源帧+滑点成交; 目标下沿减半、上沿全平; 1R/2R 单向移动止损;
  收盘跌破失效/止损 → 次交易日真实开盘成交; 第 7 个交易日 15:30 强制退出;
- 盘中价格路径用 Alpaca SIP 真实 1 分钟 K (09:30-16:00 常规时段)。分钟收盘价在该分钟
  **结束**时才可用 (bar 起点 + 1 分钟), 不能在 bar 起点使用;
- 开盘价跳空 >2% 不追; 开盘证据缺失或非有限不入场;
- 缺数据不得改变结果: 持仓期缺定版日K或缺分钟 → incomplete_data / no_minute_data,
  不计入 R 统计; 空分钟响应不缓存为终态, 下次重试。

限制 (写进结果): 只含当前在市股票 (幸存者偏差, 偏乐观); 每卡独立账户, 不含组合层
6仓/6%风险/行业上限的挤占; 分钟K取收盘价路径, 同一分钟内高低点的先后不可知;
置信区间按入场日聚类 (同日多笔强相关, 独立单元是天不是笔)。

运行: .venv/bin/python -m scripts.replay_setups_realistic --data <dir> --out <dir>
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import random
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

import polars as pl

from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import detect_candidates, load_params
from app.trader.trader import SwingTrader

logger = logging.getLogger("replay")
ENTRY_WINDOW_DAYS = 5          # 卡有效入场窗 (交易日)
GAP_LIMIT = 1.02               # 开盘超入场区上沿 2% 不追
MINUTE = timedelta(minutes=1)


# ── 数据准备 ─────────────────────────────────────────────────

def _check_quarantine(data_dir: Path) -> None:
    if (data_dir / "VALIDATION_PENDING.json").exists():
        raise ValueError("dataset quarantined: validation pending; replay is blocked")


def load_raw_daily(data_dir: Path, symbols: set[str] | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """读 as-traded 原始日K 与除权事件表 (时点复权在扫描时按日做)。"""
    _check_quarantine(data_dir)
    from app.indicators.pipeline import filter_halt_days
    from app.parquet import scan_daily_parquet
    lf = scan_daily_parquet(str(data_dir / "kline_daily" / "**" / "*.parquet"),
                            cast_options=pl.ScanCastOptions(integer_cast="allow-float"))
    if symbols is not None:
        lf = lf.filter(pl.col("symbol").is_in(sorted(symbols)))
    raw = lf.select("symbol", "date", "open", "high", "low", "close", "volume").collect()
    raw = filter_halt_days(raw).sort(["symbol", "date"])
    factors = pl.read_parquet(data_dir / "adj_factor" / "all.parquet")
    return raw, factors


def load_adjusted_daily(data_dir: Path, symbols: set[str] | None = None) -> pl.DataFrame:
    """全样本前复权 (仅用于展示/对账, 不用于决策; 决策用 point_in_time_adjust)。"""
    from app.indicators.pipeline import _apply_adj_factor
    raw, factors = load_raw_daily(data_dir, symbols)
    raw = raw.with_columns(pl.col("close").alias("raw_close"))
    return _apply_adj_factor(raw, factors).drop("ex_rights", strict=False).sort(["symbol", "date"])


def point_in_time_adjust(raw: pl.DataFrame, factors: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """只用 as_of 及之前生效的除权事件前复权; 最新价 = as_of 当日真实成交价。"""
    from app.indicators.pipeline import _apply_adj_factor
    frame = raw.filter(pl.col("date") <= as_of)
    if "raw_close" not in frame.columns:
        frame = frame.with_columns(pl.col("close").alias("raw_close"))
    known = factors.filter(pl.col("trade_date").cast(pl.Date) <= as_of) if not factors.is_empty() else factors
    return _apply_adj_factor(frame, known).drop("ex_rights", strict=False).sort(["symbol", "date"])


def production_liquid_pool(bars_by_symbol: dict[str, pl.DataFrame], params: dict, as_of: date) -> dict[str, pl.DataFrame]:
    """与 OpportunityService._load_universe 同口径: 近 30 自然日 ADV 与最新价过门槛才进横截面。"""
    u = params["universe"]
    floor = as_of - timedelta(days=30)
    out = {}
    for sym, b in bars_by_symbol.items():
        if b.height < u["min_history_days"]:
            continue
        recent = b.filter(pl.col("date") >= floor) if "date" in b.columns else b.tail(21)
        if recent.is_empty():
            continue
        adv = float((recent["close"] * recent["volume"]).mean())
        if adv >= u["min_adv_usd"] and float(recent["close"][-1]) >= u["min_price"]:
            out[sym] = b
    return out


def eligible_symbols(instruments: Path) -> tuple[set[str], dict[str, str]]:
    """与生产 OpportunityService._load_universe 同口径: 排除 ETF/基金/优先股/权证/单位/权利。"""
    inst = pl.read_parquet(instruments)
    inst = inst.filter(~pl.col("instrument_type").is_in(["etf", "fund", "preferred", "warrant", "unit", "right"]).fill_null(False))
    for flag in ("is_fund", "is_etf", "is_preferred", "is_warrant", "is_unit"):
        if flag in inst.columns:
            inst = inst.filter(~pl.col(flag).fill_null(False))
    sectors = {r["symbol"]: r["sector"] for r in inst.select("symbol", "sector").to_dicts() if r.get("sector")}
    return set(inst["symbol"].to_list()), sectors


# ── 撮合模拟 ─────────────────────────────────────────────────

@dataclass
class TradeResult:
    card_id: str
    symbol: str
    setup: str
    created: str
    status: str   # no_fill / gap_expired / closed / open_at_end / no_minute_data / incomplete_data
    entry_date: str | None = None
    exit_date: str | None = None
    r_multiple: float | None = None
    pnl: float | None = None
    exit_reasons: list[str] = field(default_factory=list)
    reward_risk: float | None = None


class MinuteSource:
    """按 (symbol, day) 取真实常规时段 1 分钟 K (as-traded 原始价)。空响应不缓存, 下次重试。"""

    def __init__(self, provider, cache_dir: Path):
        self.provider, self.cache_dir = provider, cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.requests = 0

    RUN_GAP_DAYS = 10   # 相邻需求日相隔超过这么多自然日就另起一段请求

    @classmethod
    def _runs(cls, days: list[date]) -> list[tuple[date, date]]:
        """把需求日切成连续段: 同一标的相隔数年的两张卡不能拉成一次跨年请求。"""
        runs: list[tuple[date, date]] = []
        for d in days:
            if runs and (d - runs[-1][1]).days <= cls.RUN_GAP_DAYS:
                runs[-1] = (runs[-1][0], d)
            else:
                runs.append((d, d))
        return runs

    def fetch_many(self, need: dict[str, set[date]]) -> None:
        """按标的补齐缺失分钟K; 每段连续需求日一次请求。"""
        for sym, days in sorted(need.items()):
            missing = sorted(d for d in days if not (self.cache_dir / f"{sym}_{d}.parquet").exists())
            for first, last in self._runs(missing):
                start = datetime.combine(first, datetime.min.time())
                end = datetime.combine(last, datetime.max.time().replace(microsecond=0))
                self.requests += 1
                try:
                    df = self.provider.get_minute([sym], start, end)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("minute fetch failed %s %s~%s: %s", sym, first, last, exc)
                    continue
                if df.is_empty():
                    continue  # 源端空响应无法区分「真无成交」与「暂时缺失」, 不写终态缓存
                df = df.with_columns(pl.col("datetime").dt.date().alias("_d"))
                wanted = set(missing)
                for g in df.partition_by("_d"):
                    d = g["_d"][0]
                    if d in wanted:
                        g.drop("_d").select([c for c in ("symbol", "datetime", "open", "close") if c in g.columns]
                                            ).write_parquet(self.cache_dir / f"{sym}_{d}.parquet")

    def day(self, sym: str, d: date) -> pl.DataFrame:
        p = self.cache_dir / f"{sym}_{d}.parquet"
        if not p.exists():
            return pl.DataFrame()
        return pl.read_parquet(p).sort("datetime")


def _finite_positive(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def simulate_card(card: OpportunityCard, daily: pl.DataFrame, minutes: MinuteSource,
                  trading_days: list[date], account: Account) -> TradeResult:
    """单卡独立账户: 调用生产 SwingTrader, 真账本写在临时目录。价格全部为 as-traded。"""
    res = TradeResult(card.id, card.symbol, card.setup_type, card.created_date.isoformat(), "no_fill",
                      reward_risk=card.reward_risk)
    after = [d for d in trading_days if d > card.created_date]
    if not after:
        res.status = "no_future_data"
        return res
    closes = {r["date"]: r for r in daily.to_dicts()}
    with tempfile.TemporaryDirectory(prefix="zt-replay-") as tmp:
        ledger = Ledger(Path(tmp) / "ledger")
        ledger.init_account(account)
        trader = SwingTrader(ledger, account)
        active = card.with_state(CardState.ACTIVE, "replay")
        trader.register_card(active)
        entry_window = after[:ENTRY_WINDOW_DAYS]
        horizon = after[:ENTRY_WINDOW_DAYS + account.max_holding_days + 2]
        filled = False
        saw_entry_minutes = False
        for day in horizon:
            holding = ledger.position(card.symbol) is not None
            bars = minutes.day(card.symbol, day)
            if bars.is_empty():
                if holding:
                    res.status = "no_minute_data"
                    break
                if not filled and day in entry_window:
                    continue
                break
            if not filled and day in entry_window:
                saw_entry_minutes = True
            first = bars.row(0, named=True)
            open_price = (_finite_positive(first["open"]) if first["datetime"].time() == us_clock.sessions[0].open
                          else None)
            if holding and open_price is not None:
                at = datetime.combine(day, us_clock.sessions[0].open, tzinfo=us_clock.tz)
                trader.execute_pending_exit(card.symbol, open_price=open_price, trade_date=day,
                                            at=at.isoformat())
            if not filled:
                if day not in entry_window:
                    break
                if open_price is None:
                    continue  # 开盘证据缺失/非有限不入场 (生产同规则)
                if open_price > card.entry_zone[1] * GAP_LIMIT:
                    res.status = "gap_expired"
                    break
            for row in bars.iter_rows(named=True):
                price = _finite_positive(row["close"])
                if price is None:
                    continue
                available = (row["datetime"] + MINUTE).replace(tzinfo=us_clock.tz)  # 收盘价在分钟结束时可用
                if not us_clock.in_session(available):
                    continue
                at = available.isoformat()
                if ledger.position(card.symbol) is not None:
                    trader.manage_intraday(card.symbol, price=price, trade_date=day, at=at, quote_ts=at)
                elif not filled:
                    event = trader.try_fill_entry(active, price=price, trade_date=day, at=at, quote_ts=at)
                    if event is not None:
                        filled = True
                        res.entry_date = day.isoformat()
                if filled and ledger.position(card.symbol) is None:
                    break
            if filled and ledger.position(card.symbol) is not None:
                official = closes.get(day)
                close_price = _finite_positive(official["close"]) if official else None
                if close_price is None:
                    res.status = "incomplete_data"  # 缺定版收盘价无法判失效, 不能跳过继续算结果
                    break
                close_at = datetime.combine(day, us_clock.sessions[-1].close, tzinfo=us_clock.tz)
                trader.manage_close(card.symbol, close_price=close_price, trade_date=day,
                                    at=close_at.isoformat())
            if filled and ledger.position(card.symbol) is None:
                break
        events = ledger.read_events()
        exits = [e for e in events if e["kind"] in ("partial_close", "close")]
        fills = [e for e in events if e["kind"] == "fill"]
        if not fills:
            if res.status == "no_fill" and not saw_entry_minutes:
                res.status = "no_minute_data"
            return res
        fill = fills[0]
        res.exit_reasons = [e.get("reason", "?") for e in exits]
        if res.status in ("no_minute_data", "incomplete_data"):
            return res
        if ledger.position(card.symbol) is not None:
            res.status = "open_at_end"
            return res
        cost = fill["qty"] * fill["price"] + fill["fee"]
        proceeds = sum(e["qty"] * e["price"] - e["fee"] for e in exits)
        risk = fill["qty"] * fill["risk_per_share"]
        res.status = "closed"
        res.pnl = round(proceeds - cost, 2)
        res.r_multiple = round(res.pnl / risk, 4) if risk > 0 else None
        res.exit_date = exits[-1]["trade_date"] if exits else None
        return res


def _day_clustered_ci(results: list[TradeResult], draws: int = 2000, seed: int = 7) -> tuple[list[float] | None, int]:
    """按入场日聚类的 bootstrap 95% CI: 同日多笔强相关, 重抽样单元是交易日。"""
    by_day: dict[str, list[float]] = {}
    for r in results:
        by_day.setdefault(r.entry_date or r.created, []).append(r.r_multiple)
    days = list(by_day.values())
    if len(days) < 2:
        return None, len(days)
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = [x for _ in days for x in rng.choice(days)]
        means.append(statistics.fmean(sample))
    means.sort()
    return [round(means[int(0.025 * draws)], 3), round(means[int(0.975 * draws) - 1], 3)], len(days)


def summarize(results: list[TradeResult]) -> dict:
    out = {}
    for setup in sorted({r.setup for r in results}) + ["ALL"]:
        rs = [r for r in results if setup == "ALL" or r.setup == setup]
        closed_rs = [r for r in rs if r.status == "closed" and r.r_multiple is not None]
        closed = [r.r_multiple for r in closed_rs]
        by_status = {}
        for r in rs:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        item = {"cards": len(rs), "by_status": by_status, "closed": len(closed)}
        if closed:
            mean = statistics.fmean(closed)
            wins = [r for r in closed if r > 0]
            losses = [-r for r in closed if r < 0]
            ci, n_days = _day_clustered_ci(closed_rs)
            item.update({
                "avg_r": round(mean, 3), "median_r": round(statistics.median(closed), 3),
                "ci95_day_clustered": ci, "independent_days": n_days,
                "win_rate": round(len(wins) / len(closed), 3),
                "profit_factor": round(sum(wins) / sum(losses), 3) if losses else None,
                "total_r": round(sum(closed), 2),
            })
            reasons = {}
            for r in closed_rs:
                if r.exit_reasons:
                    reasons[r.exit_reasons[-1]] = reasons.get(r.exit_reasons[-1], 0) + 1
            item["final_exit_reasons"] = reasons
        out[setup] = item
    return out


def _write_checkpoint(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str))
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="含 kline_daily/adj_factor/instruments 的数据目录")
    ap.add_argument("--instruments", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", default=None, help="扫描起始日 (默认: 数据首日+260 交易日)")
    ap.add_argument("--end", default=None)
    ap.add_argument("--minute-cache", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir, out_dir = Path(args.data), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    params = load_params()
    account = Account()
    t0 = time.time()

    stocks, _sectors = eligible_symbols(Path(args.instruments))
    raw, factors = load_raw_daily(data_dir, stocks | {"SPY"})
    spy_raw = raw.filter(pl.col("symbol") == "SPY")
    days = sorted(spy_raw["date"].to_list())
    start = date.fromisoformat(args.start) if args.start else days[min(len(days) - 1, 260)]
    end = date.fromisoformat(args.end) if args.end else days[-1]
    scan_days = [d for d in days if start <= d <= end]
    logger.info("raw daily rows=%d symbols=%d scan_days=%d (%s~%s)", raw.height, raw["symbol"].n_unique(),
                len(scan_days), scan_days[0], scan_days[-1])
    raw_by_symbol = {g["symbol"][0]: g.sort("date") for g in raw.partition_by("symbol") if g["symbol"][0] in stocks}
    factors_by_symbol = {g["symbol"][0]: g for g in factors.partition_by("symbol")} if not factors.is_empty() else {}
    empty_factors = factors.head(0)

    scan_ckpt = out_dir / "scan-checkpoint.json"
    ckpt = json.loads(scan_ckpt.read_text()) if scan_ckpt.exists() else {}
    if ckpt.get("scope") != [start.isoformat(), end.isoformat(), params["revision"]]:
        ckpt = {"scope": [start.isoformat(), end.isoformat(), params["revision"]], "done": [], "cards": []}
    done = set(ckpt["done"])
    cards = [OpportunityCard.from_dict(c) for c in ckpt["cards"]]
    lookback = 400
    for i, as_of in enumerate(scan_days):
        if as_of.isoformat() in done:
            continue
        window_start = as_of - timedelta(days=lookback)
        sub = {}
        for s, b in raw_by_symbol.items():
            w = b.filter((pl.col("date") >= window_start) & (pl.col("date") <= as_of))
            if w.height and w["date"][-1] == as_of:
                sub[s] = point_in_time_adjust(w, factors_by_symbol.get(s, empty_factors), as_of)
        sub = production_liquid_pool(sub, params, as_of)
        spy = point_in_time_adjust(spy_raw.filter(pl.col("date") >= window_start), factors_by_symbol.get("SPY", empty_factors), as_of)
        detected = detect_candidates(sub, spy, params, as_of)
        if detected.gate != "unavailable":
            cards.extend(detected.candidates)
        done.add(as_of.isoformat())
        if i % 20 == 0 or i == len(scan_days) - 1:
            _write_checkpoint(scan_ckpt, {"scope": ckpt["scope"], "done": sorted(done),
                                          "cards": [c.to_dict() for c in cards]})
            logger.info("scan %s (%d/%d): pool=%d gate=%s candidates=%d total=%d", as_of, i + 1, len(scan_days),
                        len(sub), detected.gate, len(detected.candidates), len(cards))
    # 同票冷却/排他: 同一 symbol 在入场窗内重复出卡只保留第一张 (生产排他同票活跃卡)
    cards.sort(key=lambda c: (c.created_date, c.id))
    kept, last_seen = [], {}
    for c in cards:
        prev = last_seen.get(c.symbol)
        if prev is not None and len([d for d in scan_days if prev < d <= c.created_date]) <= ENTRY_WINDOW_DAYS:
            continue
        kept.append(c)
        last_seen[c.symbol] = c.created_date
    logger.info("cards detected=%d kept after same-symbol exclusion=%d", len(cards), len(kept))

    from app.plugins.alpaca.provider import AlpacaProvider
    minutes = MinuteSource(AlpacaProvider(), Path(args.minute_cache or out_dir / "minute_cache"))
    need: dict[str, set[date]] = {}
    for c in kept:
        after = [d for d in days if d > c.created_date][:ENTRY_WINDOW_DAYS + account.max_holding_days + 2]
        need.setdefault(c.symbol, set()).update(after)
    logger.info("minute fetch: %d symbols, %d symbol-days", len(need), sum(len(v) for v in need.values()))
    minutes.fetch_many(need)
    logger.info("minute fetch done: %d requests (%.0fs)", minutes.requests, time.time() - t0)

    # 撮合按 as-traded 价: 卡上价位是创建日的时点复权价 = 创建日真实价, 与原始分钟同口径;
    # 创建后到平仓前若发生拆股, 该卡在生产中也会被失效/同步 (公司行动链路), 这里标 incomplete。
    results = []
    with patch.object(us_clock, "_calendar_provider", lambda s, e: [d for d in days if s <= d <= e]):
        for c in kept:
            sym_raw = raw_by_symbol.get(c.symbol)
            if sym_raw is None:
                continue
            horizon_end = ([d for d in days if d > c.created_date][:ENTRY_WINDOW_DAYS + account.max_holding_days + 2]
                           or [c.created_date])[-1]
            sym_factors = factors_by_symbol.get(c.symbol, empty_factors)
            split_in_window = (not sym_factors.is_empty()) and sym_factors.filter(
                (pl.col("trade_date").cast(pl.Date) > c.created_date) & (pl.col("trade_date").cast(pl.Date) <= horizon_end)
                & ((pl.col("vol_factor") - 1).abs() > 0.01)).height > 0
            if split_in_window:
                results.append(TradeResult(c.id, c.symbol, c.setup_type, c.created_date.isoformat(), "incomplete_data",
                                           reward_risk=c.reward_risk, exit_reasons=["split_during_window"]))
                continue
            results.append(simulate_card(c, sym_raw, minutes, days, account))
    summary = {
        "kind": "realistic-rule-replay",
        "not_production_trades": True,
        "rules": "production SwingTrader + detect_candidates; point-in-time adjustment; as-traded SIP 1m path "
                 "(close usable at bar end); Account defaults",
        "limitations": [
            "survivorship: only currently-listed symbols (optimistic bias)",
            "each card simulated in an isolated account (no 6-position / 6% portfolio / sector crowding)",
            "minute close path; intra-minute high/low ordering unknown",
            "same-symbol duplicate cards within the 5-day entry window collapsed to the first",
            "CI is a day-clustered bootstrap; cross-day correlation is not modeled",
            "no earnings-reduction / corporate-action / degradation guards in replay",
        ],
        "scan_range": [scan_days[0].isoformat(), scan_days[-1].isoformat()],
        "scan_days": len(scan_days),
        "params_revision": params["revision"],
        "cards_detected": len(cards),
        "cards_simulated": len(results),
        "minute_requests": minutes.requests,
        "elapsed_s": round(time.time() - t0, 1),
        "summary": summarize(results),
    }
    (out_dir / "replay-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    (out_dir / "replay-trades.json").write_text(json.dumps([r.__dict__ for r in results], ensure_ascii=False,
                                                           indent=1, default=str))
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
