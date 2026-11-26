"""P1-B1 对比: 同一批候选上「旧打分 (命中条数 + 固定 EXCESS_WEIGHT)」vs「新打分 (策略账本收缩超额)」的排序 (只读, 只打印)。

输入只有 backend/config/swing_rule_history.json 与 watchlist_rule_history.json (打印 sha256, 便于核对);
不读也不写任何数据目录 (账本档位由 strategy_ledger.review_cells 在内存里算, 不落审计)。
旧公式按 a091db3 的 swing_desk._card 逐字复刻 (EXCESS_WEIGHT 已从生产代码删除, 只在本脚本里留作对照)。

用法 (backend/ 下):
  .venv/bin/python -m scripts.ledger_rank_diff [--regime 震荡] [--k 100]
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import date

from app.opportunity import strategy_ledger as sl
from app.services import swing_desk as sd

OLD_EXCESS_WEIGHT = {"tv_smc_structure": 0.40, "volume_price_surge": 0.25, "macd_golden": 0.19,
                     "tv_chandelier_exit": 0.06, "tv_halftrend": 0.06}
DAY = date(2026, 11, 20)
LONG_RULES = ("ibs_mean_reversion", "tv_zlsma_cross", "trend_breakout")      # 生产 3+3 多头 (swing_base_rates.json)
BATCH = {"AAPL": ["ibs_mean_reversion", "trend_breakout"], "NVDA": ["tv_zlsma_cross"],
         "AMD": ["ibs_mean_reversion"], "MU": ["tv_zlsma_cross", "ibs_mean_reversion"],
         "TSLA": ["trend_breakout"], "META": ["ibs_mean_reversion"]}


def old_score(rules: list[str]) -> float:
    hr = [r for r in LONG_RULES if r in rules]
    return round(float(len(hr)) + sum(OLD_EXCESS_WEIGHT.get(r, 0.05) * 0.1 for r in hr), 3)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regime", default="震荡", choices=sl.REGIMES)
    ap.add_argument("--k", type=float, default=sl.DEFAULT_K)
    a = ap.parse_args()
    for f in ("swing_rule_history.json", "watchlist_rule_history.json"):
        print(f"sha256 {f} = {hashlib.sha256((sl.CONFIG_DIR / f).read_bytes()).hexdigest()}")
    art = sl.artifact_stats()
    lg = sl.ledger_view({"month": DAY.strftime("%Y-%m"), "cells": sl.review_cells(art["cells"], None)}, a.regime, a.k)
    for r in LONG_RULES:
        c, t = art["cells"][sl.cell_key(r, "long")], lg["cells"][sl.cell_key(r, "long")]["tier"]
        print(f"{r}: n={c['n']} excess={c['excess']} p={c['p']} ci=[{c['ci_low']},{c['ci_high']}] "
              f"tier={t} shrink={sl.shrink(c['excess'], c['n'], a.k):.4f}")
    old = sorted(BATCH, key=lambda s: (-old_score(BATCH[s]), -len(BATCH[s]), sd.tie_key(DAY, s)))
    print("OLD:", [(s, old_score(BATCH[s])) for s in old][:sd.MAX_PER_SIDE])
    sigs = [{"symbol": s, "rule": r, "side": "long"} for s, rs in BATCH.items() for r in rs]
    b = sd.compose_boards(signals=sigs, closes={s: 100.0 for s in BATCH}, atr={s: 2.0 for s in BATCH},
                          h4_state={s: "long" for s in BATCH}, as_of=DAY, long_rules=LONG_RULES, short_rules=(),
                          ledger=lg)
    print("NEW:", [(c["symbol"], c["score"], c["ledger_tier"]) for c in b["long"]])
    print("skipped:", [x for x in b["meta"]["skipped"] if x.get("side") == "long"])


if __name__ == "__main__":
    main()
