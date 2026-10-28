"""批次④ 研究 (2026-10-05, 只读, 不改生产): 用 v2 口径 (原生半仓止盈日线近似) 回答三个问题。

Q1 策略自身评分名次是否有信息量: 全池前 10 内, 第 1-3 名 vs 第 4-10 名的平均 R。
Q2 自选通道 (无限拍板 4) 的候选质量: 自选股在池内命中、但不在全池前 10 (第 11 名之后) 的平均 R,
   对照同一来源的前 10。自选通道用的是来源级许可, 这里检验“更深名次”的样本是否明显更差。
Q3 同主题扎堆: 同日同来源前 10 里同一细分行业 ≥3 只的交易, 与非扎堆交易的平均 R 对比。

全部按真实成交日聚类 95% CI。日线近似, 不是实际成交; 研究结论不自动进入排序或许可。
运行: cd backend && .venv/bin/python -m scripts.research_desk_v2 --data <dir> --out <json>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parents[1]))

WATCHLIST = ["AAOI", "AAPL", "AMD", "AMZN", "ARM", "ASML", "ASTS", "AVGO", "COIN", "CRCL", "DELL", "GOOG", "HOOD", "INTC",
             "IREN", "LITE", "LLY", "META", "MRNA", "MRVL", "MSFT", "MSTR", "MU", "NBIS", "NOW", "NVDA", "ORCL", "PLTR",
             "RDW", "RKLB", "SKHY", "SMCI", "SNDK", "SNOW", "SPCX", "TSLA", "TSM"]
DEEP_MAX = 30  # 研究看到第 30 名


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--start", default="2022-01-13")
    ap.add_argument("--end", default="2026-09-18")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    data_dir = Path(args.data).resolve()
    os.environ["DATA_DIR"] = str(data_dir)
    from app.config import settings
    settings.data_dir = data_dir
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.services import decision_desk as dd
    from app.services import strategy_evaluation as se
    from app.services import strategy_evaluation_runner as runner
    from app.storage.repository import DataStore, KlineRepository

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    repo = KlineRepository(DataStore(data_dir))
    engine = runner._strategy_engine(data_dir)
    svc = StrategyBacktestService(BacktestEngine(repo), engine)
    inst = pl.read_parquet(sorted((data_dir / "instruments").glob("*.parquet"))[0])
    industry = {r["symbol"]: r["industry"] for r in inst.select(["symbol", "industry"]).to_dicts() if r.get("industry")}
    wl = set(WATCHLIST)
    ids = [m["id"] for m in engine.list_strategies() if not m.get("research_only") and m.get("execution_backend") == "matrix_native"]
    if args.only:
        ids = [i for i in ids if i in set(args.only.split(","))]
    out = {"window": [str(start), str(end)], "contract": se.CONTRACT_V2, "watchlist": WATCHLIST, "sources": {}}
    pooled = defaultdict(list)
    for sid in ids:
        t0 = time.perf_counter()
        md, entry, score, stop_loss = runner._signals_for(sid, svc, engine, start, end)
        days = np.array([date.fromisoformat(x[:10]) for x in md.timestamp_labels])
        dv = np.where(np.isfinite(md.close) & np.isfinite(md.volume), md.close * md.volume, np.nan)
        ranks = se.prior_dollar_volume_rank(dv, window=60)
        desc = bool(engine.get(sid).meta.get("descending", True))
        groups = defaultdict(list)
        for t in np.flatnonzero((days >= start) & (days <= end)):
            hit = np.flatnonzero(entry[t] & (ranks[t] > 0) & (ranks[t] <= se.TOP_N))
            if hit.size == 0:
                continue
            key = np.nan_to_num(score[t, hit], nan=-np.inf if desc else np.inf)
            order = hit[np.argsort(-key if desc else key, kind="stable")]
            top = order[: se.PICKS_PER_DAY]
            ind_count = defaultdict(int)
            for i in top:
                ind = industry.get(md.symbols[i])
                if ind:
                    ind_count[ind] += 1
            for pos, i in enumerate(order[:max(DEEP_MAX, se.PICKS_PER_DAY)] if len(order) else []):
                sym = md.symbols[i]
                rank = pos + 1
                in_top = rank <= se.PICKS_PER_DAY
                deep_wl = (not in_top) and sym in wl
                if not in_top and not deep_wl and rank > DEEP_MAX:
                    continue
                b = runner._daily_frames(md, i, t)
                card = dd.card_from_pick(sid, sym, b, as_of=days[t], stop_loss=stop_loss)
                if card is None:
                    continue
                fut = slice(t + 1, t + 1 + se.ENTRY_WINDOW + se.MAX_HOLD + 2)
                res = se.simulate_native_daily(md.open[fut, i], md.high[fut, i], md.low[fut, i], md.close[fut, i],
                                               entry_zone=card.entry_zone, invalidation=card.invalidation,
                                               target=card.target_zone, risk=card.risk_per_share)
                if res["status"] != "closed":
                    continue
                tr = {"r": res["r"], "entry_day": str(days[t + 1 + res["fill_index"]]), "bucket": "x"}
                if in_top:
                    groups["top1_3" if rank <= 3 else "top4_10"].append(tr)
                    ind = industry.get(sym)
                    groups["crowded" if ind and ind_count[ind] >= 3 else "not_crowded"].append(tr)
                    if sym in wl:
                        groups["watchlist_in_top10"].append(tr)
                else:
                    groups["rank11_30"].append(tr) if rank <= DEEP_MAX else None
                    if sym in wl:
                        groups["watchlist_lane"].append(tr)
        stats = {k: se._stats(v, 7) for k, v in groups.items()}
        for k, v in groups.items():
            pooled[k].extend({**x, "entry_day": f"{sid}|{x['entry_day']}"} for x in v)
        out["sources"][sid] = {k: {x: s.get(x) for x in ("n", "mean", "ci_low", "ci_high", "win_rate")} for k, s in stats.items()}
        out["sources"][sid]["elapsed_s"] = round(time.perf_counter() - t0, 1)
        print(sid, json.dumps(out["sources"][sid], ensure_ascii=False), flush=True)
    # 合并各来源: 按 (来源, 成交日) 聚类
    out["pooled"] = {k: {x: s.get(x) for x in ("n", "mean", "ci_low", "ci_high", "win_rate")}
                     for k, s in ((k, se._stats(v, 7)) for k, v in pooled.items())}
    out["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out["pooled"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
