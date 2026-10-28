"""用 PyneCore 逐根执行参照脚本, 生成 fixtures.json.gz。用法: <pyne-python> gen_fixtures.py <sample.parquet>
sample.parquet: 列 symbol,date,open,high,low,close,volume (真实日K)。另加 3 条合成序列 (含跳空/横盘/极端K)。"""
import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pynecore.core import import_hook  # noqa: F401
from pynecore.core.script_runner import ScriptRunner
from pynecore.core.syminfo import SymInfo
from pynecore.types.ohlcv import OHLCV

HERE = Path(__file__).parent
CASES = {   # 脚本 → [(内核名, 输入参数)]
    "supertrend": [("tv_supertrend", {"Periods": 10, "Multiplier": 3.0}), ("tv_phantom_shift", {"Periods": 14, "Multiplier": 3.2})],
    "ut_bot": [("tv_ut_bot", {"a": 1.0, "c": 10}), ("tv_ut_bot@k2", {"a": 2.0, "c": 10})],
    "chandelier": [("tv_chandelier_exit", {"length": 22, "mult": 3.0})],
    "zlsma": [("tv_zlsma_cross", {"length": 32, "offset": 0})],
    "halftrend": [("tv_halftrend", {"amplitude": 2, "channelDeviation": 2})],
    "squeeze": [("tv_squeeze_momentum", {"length": 20, "mult": 2.0, "lengthKC": 20, "multKC": 1.5})],
    "wavetrend": [("tv_wavetrend", {"n1": 10, "n2": 21, "s1": 4})],
    "smc_swing": [("tv_smc_structure", {"size": 50, "chochOnly": True}), ("tv_smc_structure@bos", {"size": 50, "chochOnly": False}),
                  ("tv_smc_structure@s10", {"size": 10, "chochOnly": True})],
    "phantom_trail": [("tv_phantom_trail", {"trlLen": 22, "trlMult": 3.0})],
}


def syminfo():
    return SymInfo(prefix="NASDAQ", ticker="X", currency="USD", basecurrency="", description="x", period="1D",
                   type="stock", mintick=0.0001, pricescale=10000, minmove=1, pointvalue=1.0, mincontract=1.0,
                   timezone="America/New_York", volumetype="base", opening_hours=[], session_starts=[], session_ends=[])


def synth(seed, n=700):
    rng = np.random.default_rng(seed)
    c = 50 * np.exp(np.cumsum(rng.normal(0.0003, 0.025, n)))
    c[200:260] = c[199] * (1 + rng.normal(0, 0.002, 60))            # 横盘
    c[400] *= 0.8                                                    # 跳空
    o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.006, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.012, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.012, n)))
    h[500] = h[500] * 1.15                                           # 极端长上影
    v = rng.integers(1e5, 5e6, n).astype(float)
    d = pd.bdate_range("2021-01-04", periods=n)
    return pd.DataFrame({"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": v})


def run(script, df, inputs):
    ts = (pd.to_datetime(df["date"]).astype("int64") // 10**6).to_numpy()
    candles = [OHLCV(timestamp=int(t), open=float(a), high=float(b), low=float(x), close=float(y), volume=float(z))
               for t, a, b, x, y, z in zip(ts, df.open, df.high, df.low, df.close, df.volume)]
    r = ScriptRunner(script_path=HERE / f"{script}.py", ohlcv_iter=candles, syminfo=syminfo(), inputs=inputs)
    buy, sell = [], []
    for item in r.run_iter():
        p = item[1]
        buy.append(int(p.get("buy") or 0))
        sell.append(int(p.get("sell") or 0))
    return buy, sell


def main():
    real = pd.read_parquet(sys.argv[1])
    series = {f"real:{s}": g.sort_values("date").reset_index(drop=True) for s, g in real.groupby("symbol")}
    for k in (1, 2, 3):
        series[f"synth:{k}"] = synth(k)
    out = {"pynecore": "9249a0e", "series": {}, "results": {}}
    for name, df in series.items():
        out["series"][name] = {c: [float(x) for x in df[c]] for c in ("open", "high", "low", "close", "volume")}
    for script, cases in CASES.items():
        for kernel, inputs in cases:
            for name, df in series.items():
                b, s = run(script, df, inputs)
                out["results"][f"{kernel}|{name}"] = {"inputs": inputs, "buy": b, "sell": s}
                print(f"{kernel:24s} {name:12s} buy={sum(b):3d} sell={sum(s):3d}", flush=True)
    with gzip.open(HERE / "fixtures.json.gz", "wt") as f:
        json.dump(out, f)


if __name__ == "__main__":
    main()
