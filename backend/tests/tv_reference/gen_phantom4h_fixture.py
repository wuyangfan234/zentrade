"""生成 Phantom 4h Pine 原版仪表盘的对照答案（开发用途；需要 PyneCore 环境，生产不依赖）。

输入：真实 4h K 线的最后 2600 根，2 只股票。
输出：tests/tv_reference/phantom_4h_pine.py 在 PyneCore 上逐根运行的对照结果。
用法：PYNECORE_RUNNER_PATH=/path/to/runner python tests/tv_reference/gen_phantom4h_fixture.py <csv目录>
runner 目录必须包含 run_ref.py。
"""
import gzip
import json
import math
import os
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
_runner = os.environ.get("PYNECORE_RUNNER_PATH")
if not _runner:
    raise SystemExit("Set PYNECORE_RUNNER_PATH to the directory containing run_ref.py")
sys.path.insert(0, _runner)
from run_ref import run  # noqa: E402

SYMS = ["NVDA", "COIN"]
TAIL = 2600


def main(csv_dir: Path):
    out = {"source": "Phantom源代码2.md (Phantom Flow Core 中文优化版, Pine v6)", "tail": TAIL, "series": {}}
    for s in SYMS:
        df = pd.read_csv(csv_dir / f"{s}.csv", parse_dates=["ts"]).tail(TAIL).reset_index(drop=True)
        ts = (df["ts"].astype("int64") // 10**6).tolist()
        r = run(str(HERE / "phantom_4h_pine.py"), ts, df.open, df.high, df.low, df.close, df.volume)

        def col(k, cast=int):
            return [None if (x is None or (isinstance(x, float) and math.isnan(x))) else cast(x) for x in (row.get(k) for row in r)]
        out["series"][s] = {
            "ts": df["ts"].dt.strftime("%Y-%m-%d %H:%M").tolist(),
            **{k: [round(float(x), 4) for x in df[k]] for k in ("open", "high", "low", "close", "volume")},
            "shift": col("shift"), "sw_bias": col("sw_bias"), "osc_pos": col("osc_pos"), "above_ma": col("above_ma"),
            "trl_dir": col("trl_dir"), "trl_long": col("trl_long", float)}
        print(s, len(r))
    with gzip.open(HERE / "phantom_4h_pine_fixture.json.gz", "wt") as f:
        json.dump(out, f)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
