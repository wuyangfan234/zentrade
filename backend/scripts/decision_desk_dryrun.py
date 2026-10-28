#!/usr/bin/env python3
"""决策台干跑: 用指定数据目录 + 评估文件, 按生产路径跑一遍当日决策, 打印结果, 不写生产台账。

运行: .venv/bin/python -m scripts.decision_desk_dryrun --data <dir> --evaluation <evaluation.json> --as-of 2026-10-01
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--evaluation", required=True)
    ap.add_argument("--as-of", required=True)
    args = ap.parse_args()
    src = Path(args.data).resolve()
    work = Path(tempfile.mkdtemp(prefix="zt-desk-"))
    for d in ("kline_daily", "kline_daily_enriched", "adj_factor", "instruments", "kline_index_daily", "kline_index_enriched", "user_data"):
        if (src / d).exists():
            (work / d).symlink_to(src / d)
    (work / "decision_desk").mkdir()
    shutil.copy(args.evaluation, work / "decision_desk" / "evaluation.json")
    os.environ["DATA_DIR"] = str(work)
    from app.config import settings
    settings.data_dir = work
    from app.jobs import daily_pipeline
    from app.opportunity.engine import load_params, scan
    from app.services.opportunity_service import OpportunityService
    from app.storage.repository import DataStore, KlineRepository
    from app.services import strategy_evaluation_runner as ev
    from app.services import decision_scan_job

    repo = KlineRepository(DataStore(work))
    engine = ev._strategy_engine(work)
    daily_pipeline._app_state_ref = SimpleNamespace(strategy_engine=engine)
    decision_scan_job._watchlist = lambda: set(__import__("polars").read_parquet(work / "user_data" / "watchlist.parquet")["symbol"].to_list())
    svc = OpportunityService(repo)
    as_of = date.fromisoformat(args.as_of)
    params = load_params()
    bars, spy = svc._load_universe(params, as_of)
    s1s2 = scan(bars, spy, params, as_of, active_symbols=set(), sector_map=svc.sector_map(), portfolio_risk_used_pct=0.0)
    desk = decision_scan_job.run(svc, as_of, s1s2=s1s2, frames=bars, params=params, excluded=set(), risk_used_pct=0.0)
    r = desk.result
    print("universe", len(bars), "S1/S2 cards", len(s1s2.cards), "watch", len(s1s2.watch))
    print("desk opened", len(r.cards), "watched", len(r.watch), "candidates", len(desk.ranked))
    for c in r.cards:
        d = c.evidence["decision"]
        print(f"  OPEN {c.symbol:6s} {d['tab']:9s} {d['tier']:8s} score={d['score']:.3f} risk={d['risk_pct']:.4f} "
              f"sources={d['sources']} entry={c.entry_zone} inval={c.invalidation} target={c.target_zone} rr={c.reward_risk}")
    print("watch reasons", Counter(w.state_reason.split('：')[0][:30] for w in r.watch).most_common(8))
    print("watchlist candidates", [x["symbol"] for x in desk.ranked if x["tab"] == "watchlist"])
    print("by_source", json.dumps(desk.summary["by_source"], ensure_ascii=False))
    print("meta", desk.summary["meta"])


if __name__ == "__main__":
    main()
