"""周度自动挖掘的有界股票池 (用户 2026-10-01 拍板: 上限 200 只, 主题覆盖, 自选全在)。

为什么有界: 1442 个交易日 × 全市场的均衡档挖掘, worker 峰值约 4 GiB, 常见自托管主机 (8-16 GB) 可用内存
约 2 GiB; 200 只同条件实测峰值约 0.9 GiB。调度器不能再按全市场构建请求。

顺序即优先级: 自选 → 主题成员 (按近 N 日成交额降序) → 流动性补足 (普通股/ADR,
股价下限、足够日K)。只保留本地 enriched 有足够历史的标的, 缺数据的记入 dropped,
不静默替换。自选始终保留, 即使超过上限 (自选本身受 UI 规模约束)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "mining_pool.json"


@dataclass(frozen=True)
class MiningPool:
    symbols: list[str]
    max_symbols: int
    by_source: dict[str, int]
    dropped: list[str] = field(default_factory=list)
    config_revision: str | None = None

    def audit(self) -> dict[str, Any]:
        """写进挖掘请求的审计摘要 (不影响运行时解码)。"""
        return {"pool": "weekly_themed_v1", "max_symbols": self.max_symbols, "selected": len(self.symbols),
                "by_source": dict(self.by_source), "dropped": sorted(self.dropped)[:50],
                "config_revision": self.config_revision}


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg = json.loads(Path(path or DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    cap = cfg.get("max_symbols")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise ValueError("mining pool max_symbols must be a positive integer")
    if not isinstance(cfg.get("themes"), dict):
        raise ValueError("mining pool themes must be a mapping")
    return cfg


def _watchlist_symbols() -> list[str]:
    from app.services import watchlist
    return [str(row["symbol"]).upper() for row in watchlist.list_symbols() if row.get("symbol")]


def _recent_liquidity(data_dir: Path, window_days: int) -> pl.DataFrame:
    """近 window_days 个 enriched 交易日: 每只的中位成交额、最低收盘价、出现天数。"""
    root = Path(data_dir) / "kline_daily_enriched"
    parts = sorted(p / "part.parquet" for p in root.glob("date=*") if (p / "part.parquet").is_file())
    parts = parts[-window_days:]
    frames = []
    for path in parts:
        try:
            frames.append(pl.read_parquet(path, columns=["symbol", "close", "amount"]))
        except Exception:  # noqa: BLE001 - 坏分区不让整个调度失败, 只是该日不计
            continue
    if not frames:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "med_amount": pl.Float64, "min_close": pl.Float64, "days": pl.UInt32})
    return (pl.concat(frames, how="diagonal_relaxed")
            .group_by("symbol")
            .agg(pl.col("amount").median().alias("med_amount"), pl.col("close").min().alias("min_close"),
                 pl.len().alias("days")))


def _instrument_types(data_dir: Path) -> dict[str, str]:
    path = Path(data_dir) / "instruments" / "instruments.parquet"
    if not path.exists():
        return {}
    df = pl.read_parquet(path)
    if "instrument_type" not in df.columns:
        return {}
    return dict(zip(df["symbol"].to_list(), df["instrument_type"].to_list()))


def build_mining_pool(data_dir: Path, watchlist: list[str] | None = None, *,
                      config_path: Path | None = None) -> MiningPool:
    cfg = load_config(config_path)
    cap = int(cfg["max_symbols"])
    fill = cfg.get("liquidity_fill") or {}
    window = int(fill.get("window_days", 60))
    min_days = int(fill.get("min_history_days", 55))
    min_close = float(fill.get("min_close", 5.0))
    fill_types = set(fill.get("instrument_types") or ["common_stock", "adr"])

    stats = _recent_liquidity(Path(data_dir), window)
    eligible = stats.filter(pl.col("days") >= min_days)
    amount = dict(zip(eligible["symbol"].to_list(), eligible["med_amount"].to_list()))
    watch = list(dict.fromkeys(s.upper() for s in (watchlist if watchlist is not None else _watchlist_symbols())))

    chosen: list[str] = []
    sources = {"watchlist": 0, "theme": 0, "liquidity": 0}
    dropped: list[str] = []
    for sym in watch:                                   # 自选: 有数据就必在
        if sym in amount:
            chosen.append(sym)
            sources["watchlist"] += 1
        else:
            dropped.append(sym)

    themed = list(dict.fromkeys(s.upper() for t in cfg["themes"].values() for s in t.get("symbols", [])))
    have = set(chosen)
    themed_ok = [s for s in themed if s not in have and s in amount]
    dropped.extend(s for s in themed if s not in have and s not in amount)
    themed_ok.sort(key=lambda s: -float(amount[s] or 0.0))
    for sym in themed_ok:
        if len(chosen) >= cap:
            break
        chosen.append(sym)
        sources["theme"] += 1

    if len(chosen) < cap:                               # 流动性补足
        types = _instrument_types(Path(data_dir))
        have = set(chosen)
        fillers = (eligible.filter(pl.col("min_close") >= min_close)
                   .sort("med_amount", descending=True)["symbol"].to_list())
        for sym in fillers:
            if len(chosen) >= cap:
                break
            if sym in have or types.get(sym) not in fill_types:
                continue
            chosen.append(sym)
            sources["liquidity"] += 1

    return MiningPool(symbols=chosen, max_symbols=cap, by_source=sources, dropped=sorted(set(dropped)),
                      config_revision=str(cfg.get("revision")) if cfg.get("revision") is not None else None)
