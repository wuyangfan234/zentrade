#!/usr/bin/env python3
"""P2-2 验收回放: 突破 setup 近 2 年开卡统计 (真实 Alpaca 数据)。

做法: 拉流动性池日K (近 500 交易日) → 逐日滑动 as_of 调 scan() →
统计开卡数/分布/每张卡的后续 7 日走向 (MFE/MAE/是否触目标或失效)。

运行 (后台):  .venv/bin/python -m scripts.replay_breakout --top 300
产物:  backend/scripts/replay_breakout_result.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime as dt_cls, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import polars as pl

from app.market_clock import us_clock
from app.opportunity.engine import load_params, scan
from app.plugins.alpaca.provider import AlpacaProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("replay")

OUT = Path(__file__).with_name("replay_breakout_result.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=300, help="流动性池大小 (按 ADV 排序)")
    ap.add_argument("--days", type=int, default=500, help="回看交易日数")
    args = ap.parse_args()

    params = load_params()
    provider = AlpacaProvider()

    # 1. 标的池
    inst = provider.instruments_df()
    logger.info("instruments: %d", inst.height)

    # 2. 拉全池近 1 个月日K 算 ADV 选 top
    end = us_clock.today()
    start_probe = end - timedelta(days=45)
    logger.info("ADV probe: %s ~ %s ...", start_probe, end)
    t0 = time.time()
    probe = provider.get_daily(inst["symbol"].to_list(),
                               dt_cls.combine(start_probe, dt_cls.min.time()),
                               dt_cls.combine(end, dt_cls.min.time()))
    logger.info("probe done: %d rows in %.0fs", probe.height, time.time() - t0)
    adv = (probe.group_by("symbol")
           .agg((pl.col("close") * pl.col("volume")).mean().alias("adv"))
           .sort("adv", descending=True))
    universe = adv["symbol"].head(args.top).to_list()
    logger.info("universe top %d by ADV (cutoff $%.1fM)", len(universe),
                float(adv["adv"][args.top - 1]) / 1e6)

    # 3. 拉全历史
    hist_start = end - timedelta(days=int(args.days * 1.6) + 260)
    logger.info("history pull: %s ~ %s ...", hist_start, end)
    t0 = time.time()
    allbars = provider.get_daily(
        universe + ["SPY"],
        dt_cls.combine(hist_start, dt_cls.min.time()),
        dt_cls.combine(end, dt_cls.min.time()))
    logger.info("history: %d rows in %.0fs", allbars.height, time.time() - t0)

    spy = allbars.filter(pl.col("symbol") == "SPY").sort("date")
    by_symbol = {s: allbars.filter(pl.col("symbol") == s).sort("date") for s in universe}

    # 4. 逐日回放
    trading_days = [d for d in spy["date"].to_list() if d >= end - timedelta(days=args.days * 1.45)]
    cards_total = []
    gate_days = {"green": 0, "yellow": 0}
    for as_of in trading_days:
        scan_input = {}
        for s, df in by_symbol.items():
            sub = df.filter(pl.col("date") <= as_of)
            if sub.height >= params["universe"]["min_history_days"]:
                scan_input[s] = sub
        spy_sub = spy.filter(pl.col("date") <= as_of)
        result = scan(scan_input, spy_sub, params, as_of)
        gate_days[result.gate] += 1
        cards_total.extend(result.cards)

    logger.info("回放完成: %d 个交易日, 开卡 %d 张 (green %d / yellow %d 天)",
                len(trading_days), len(cards_total), gate_days["green"], gate_days["yellow"])

    # 5. 每张卡的后续 7 日走向 (MFE/MAE)
    outcomes = []
    for card in cards_total:
        df = by_symbol[card.symbol]
        future = df.filter((pl.col("date") > card.created_date))
        entry_mid = sum(card.entry_zone) / 2
        filled = future.filter((pl.col("high") >= card.entry_zone[0]) & (pl.col("low") <= card.entry_zone[1]))
        if filled.is_empty():
            outcomes.append({"id": card.id, "symbol": card.symbol, "state": "no_fill",
                             "rr": card.reward_risk})
            continue
        fill_day = filled["date"][0]
        post = future.filter(pl.col("date") >= fill_day).head(7)
        mfe = float(post["high"].max())
        mae = float(post["low"].min())
        hit_target = mfe >= card.target_zone[0]
        hit_stop = mae <= card.invalidation
        r_multiple = ((card.target_zone[0] - entry_mid) if hit_target else (card.invalidation - entry_mid)) / card.risk_per_share
        outcomes.append({
            "id": card.id, "symbol": card.symbol, "setup": card.setup_type,
            "rr_planned": card.reward_risk, "fill_day": str(fill_day),
            "mfe": mfe, "mae": mae,
            "hit_target": bool(hit_target), "hit_stop": bool(hit_stop),  # numpy bool → python bool (JSON 可序列化)
            "r_outcome": round(r_multiple, 2),
        })

    hit_t = sum(1 for o in outcomes if o.get("hit_target"))
    hit_s = sum(1 for o in outcomes if o.get("hit_stop"))
    no_fill = sum(1 for o in outcomes if o.get("state") == "no_fill")
    rs = [o["r_outcome"] for o in outcomes if "r_outcome" in o]

    report = {
        "as_of_range": [str(trading_days[0]), str(trading_days[-1])],
        "trading_days": len(trading_days),
        "universe_size": len(universe),
        "cards": len(cards_total),
        "gate_days": gate_days,
        "outcomes": {"hit_target": hit_t, "hit_stop": hit_s, "no_fill": no_fill,
                      "evaluated": len(rs)},
        "r_stats": {
            "mean": round(sum(rs) / len(rs), 2) if rs else None,
            "wins": sum(1 for r in rs if r > 0),
            "losses": sum(1 for r in rs if r <= 0),
        },
        "params_revision": params["revision"],
        "outcome_detail": outcomes,
    }
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    logger.info("报告: %s", OUT)
    print(json.dumps({k: v for k, v in report.items() if k != "outcome_detail"}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
