#!/usr/bin/env python3
"""统一口径评估全部策略 → data/decision_desk/evaluation.json (决策台权重的唯一证据来源)。

逻辑在 app/services/strategy_evaluation_runner.py (生产周度任务同一份代码)。

运行: .venv/bin/python -m scripts.evaluate_strategies --data <data_dir> --start 2022-01-13 --end 2026-09-22
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--start", default="2022-01-13")
    ap.add_argument("--end", default="2026-09-22")
    ap.add_argument("--replay-trades", default=None)
    ap.add_argument("--only", default=None, help="comma separated strategy ids")
    ap.add_argument("--out", default=None)
    ap.add_argument("--v2", action="store_true", help="v2 口径 (原生半仓止盈近似), 写 evaluation_v2.json 并行文件")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(args.data).resolve()
    os.environ["DATA_DIR"] = str(data_dir)
    from app.config import settings
    settings.data_dir = data_dir
    from app.services import strategy_evaluation_runner as runner
    if args.v2:
        payload = runner.run_all_v2(data_dir, date.fromisoformat(args.start), date.fromisoformat(args.end),
                                    only=args.only.split(",") if args.only else None,
                                    out_path=Path(args.out) if args.out else None)
        print(json.dumps(payload.get("comparison"), ensure_ascii=False, indent=1))
        return
    payload = runner.run_all(data_dir, date.fromisoformat(args.start), date.fromisoformat(args.end),
                             only=args.only.split(",") if args.only else None,
                             replay_trades=Path(args.replay_trades) if args.replay_trades else None,
                             out_path=Path(args.out) if args.out else None)
    print(json.dumps({k: {x: v.get(x) for x in ("n", "mean", "ci_low", "ci_high", "win_rate")} for k, v in payload["sources"].items()},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
