"""逐字摘自 oasis-quant engine/phantom_4h_rt_monitor.py @52730bb: 1h → 4h，TV 口径 09:30 ET 对齐。"""


def _resample_4h_tv(df1h):
    """1h → 4h，TV 口径 09:30 ET 对齐（09:30/13:30/17:30/21:30/01:30/05:30）。"""
    return df1h.resample("4h", origin="start_day", offset="9h30min").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"}).dropna(subset=["close"])
