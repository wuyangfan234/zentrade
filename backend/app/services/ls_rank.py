# ruff: noqa: RUF002
"""今日多空 (0.51.0): 盘前给自选打分排名, 每天固定 看多 5 / 看空 5, 只展示、只结算, 不进交易环。

口径 (研究第十五轮 G3p "最保守版", 预注册在研究仓):
  特征 8 个, 只用入场日之前的复权日K: 昨日反转 rev1、IBS ibs、5 日反转 rev5、动量 mom (t-6 → t-61)、
    20 日日内收益均值 hksoc、20 日隔夜收益均值 on20、ATR 占比 atrp、20 日成交额对数 ladv。缺值交给模型原生处理。
  训练池 = 每个交易日按此前 60 日成交额中位数, 在全市场普通股里取前 300 (前一日原始收盘 >= $10;
    排除 ETF / 优先股 / 权证 / 单位 / 权利), 再并上自选。2021-04-01 起。
  目标 = 入场日开盘 → 第 5 个交易日收盘的收益, 减当天训练池均值, 再按 1% / 99% 截尾。
  模型 = HistGradientBoostingRegressor (200 轮, 学习率 0.05, 叶子 15, 叶最少 200 样本, L2 1.0, 种子 7)。
  每季度重训一次; 训练只用季度首日 8 个自然日之前的入场日 (5 日目标已实现)。
  上市不足 130 个交易日的自选照常排名, 但不上榜 (与扫描池 min_history_days 一致)。
回放 (本实现在全市场日K上季度滚动样本外, 自选 2024-01 ~ 2026-10, 710 份名单; 开盘进 · 止损 2 ATR · 最多 5 日 · 扣 10bp;
  研究产物 r39_service_walkforward): 看多每笔 +0.23 ATR (随机 +0.19), 看空每笔 −0.15 ATR (随机 −0.21),
  看多 − 自选全体 +0.05 ATR; 排名 IC 0.017 (t 1.9)。分年看多 2024 +0.31 / 2025 +0.23 / 2026 +0.15。
  失败判据: 前向 65 个交易日后 看多 − 自选全体 < 0。

时点: 盘后管道 (16:35 ET 起, 官方收盘定版之后) 先结算到期名单, 再出下一交易日名单; 17:20 / 08:20 ET 补一次。
  页面: 盘前参考价 = 09:00 盘前跳空记录 (09:00 前用盘前实时价缓存, 请求里不出网); 开盘后 = 今天开盘价;
  止损 = 参考价 ∓ 2 ATR; 股数 = 单笔风险 (纸面权益 × 规则集单笔风险) ÷ 2 ATR, 向下取整。
记录: data/opportunities/ls_rank/<名单日>.json (原子整写); 模型: data/opportunities/ls_rank/model/<季度>.pkl。
  名单日开盘之后才生成的名单 (上线当天 / 补跑) 记 late = true: 照常展示和结算, 但不计入前向成绩。
不写台账、不进下单簿、不推送。
"""
from __future__ import annotations

import json
import logging
import math
import pickle
import random
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONTRACT = "ls-rank-v1"
MODEL_VERSION = "gbm-8f-pit300-v1"
FEATURES = ("rev1", "ibs", "rev5", "mom", "hksoc", "on20", "atrp", "ladv")
GBM = {"max_iter": 200, "learning_rate": 0.05, "max_leaf_nodes": 15, "min_samples_leaf": 200,
       "l2_regularization": 1.0, "random_state": 7}
TRAIN_START = date(2021, 4, 1)
WARMUP_DAYS = 120                  # 训练读数据比 TRAIN_START 早多少自然日 (60 日中位 / 61 日动量的暖机)
POOL_TOP = 300
POOL_MIN_PRICE = 10.0
CUTOFF_BUFFER_DAYS = 8
MIN_TRAIN_ROWS = 5000
TRAIN_THREADS = 2                  # 训练限 2 个计算线程: 季度重训在主进程里跑, 不抢交易环的 CPU
TOP_N = 5
HOLD_DAYS = 5
STOP_ATR = 2.0
COST = 0.001                       # 往返 10bp
MIN_HISTORY = 130                  # 上市不足不上榜 (data_freshness.NEW_LISTING_DAYS 同值)
MIN_RANKED = 15                    # 当天能打分的自选少于 15 只 → 不出名单
FEATURE_DAYS = 400                 # 出名单读多少自然日自选日K (特征 + 上市天数)
CHECK_DAYS = 65                    # 前向 65 个交易日后判失败
RANDOM_SEED = 20261106
BACKTEST = {"period": "2024-01~2026-10",
            "long": {"mean_atr": 0.233, "win": 0.532, "rand": 0.186},
            "short": {"mean_atr": -0.154, "win": 0.482, "rand": -0.207},
            "long_minus_all": 0.047}
_EXCLUDE_TYPES = ("etf", "preferred", "warrant", "unit", "right")
_LOCK = threading.Lock()


# ── 路径与读写 ─────────────────────────────────────────────────────

def root(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "ls_rank"


def day_path(data_dir: Path, day: date) -> Path:
    return root(data_dir) / f"{day.isoformat()}.json"


def model_path(data_dir: Path, quarter: str) -> Path:
    return root(data_dir) / "model" / f"{quarter}.pkl"


def _write_json(p: Path, obj: dict) -> None:
    from app.services.fs_utils import atomic_write_text
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(obj, ensure_ascii=False, indent=1, allow_nan=False))


def load_day(data_dir: Path, day: date) -> dict | None:
    p = day_path(data_dir, day)
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("day") != day.isoformat() or not isinstance(d.get("rows"), list):
        return None
    return d


def list_days(data_dir: Path) -> list[date]:
    r = root(data_dir)
    out = []
    if r.exists():
        for p in r.glob("*.json"):
            try:
                out.append(date.fromisoformat(p.stem))
            except ValueError:
                continue
    return sorted(out)


# ── 小工具 ────────────────────────────────────────────────────────

def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def quarter_of(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def train_end(d: date) -> date:
    """d 所在季度的模型只用 < 此日期的入场日训练 (季度首日 − 8 自然日)。"""
    return quarter_start(d) - timedelta(days=CUTOFF_BUFFER_DAYS)


def _watchlist() -> list[str]:
    from app.services.decision_scan_job import _watchlist as wl
    return sorted(wl())


def _sklearn_version() -> str:
    import sklearn
    return str(sklearn.__version__)


# ── 数据 ──────────────────────────────────────────────────────────

def excluded_symbols(data_dir: Path) -> set[str]:
    """ETF / 优先股 / 权证 / 单位 / 权利 (标的维表); 读不到 = 空集。"""
    import polars as pl
    try:
        i = pl.read_parquet(Path(data_dir) / "instruments" / "instruments.parquet")
    except Exception:
        return set()
    cond = pl.lit(False)
    for c in ("is_etf", "is_preferred", "is_warrant", "is_unit"):
        if c in i.columns:
            cond = cond | pl.col(c).fill_null(False)
    if "instrument_type" in i.columns:
        cond = cond | pl.col("instrument_type").is_in(list(_EXCLUDE_TYPES)).fill_null(False)
    return set(i.filter(cond)["symbol"].to_list())


def load_daily(data_dir: Path, start: date, end: date, *, symbols: list[str] | None = None,
               columns: tuple[str, ...] = ("open", "high", "low", "close", "volume", "amount", "raw_close"),
               priority: str = "background"):
    """已落盘的复权日K [start, end]。调用方保证 end 是已定版的收盘日 (盘中快照分区不读)。"""
    import polars as pl

    from app.parquet import scan_enriched_parquet
    from app.polars_guard import guarded_collect
    from app.storage.repository import _partition_source
    base = Path(data_dir) / "kline_daily_enriched"
    if not base.exists() or not any(base.glob("date=*")):
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, **{c: pl.Float64 for c in columns}})
    lf = scan_enriched_parquet(_partition_source(str(base / "**" / "*.parquet"), start, end))
    lf = lf.filter((pl.col("date") >= start) & (pl.col("date") <= end))
    if symbols is not None:
        lf = lf.filter(pl.col("symbol").is_in(list(symbols)))
    return guarded_collect(lf.select(["symbol", "date", *columns]), priority=priority).sort(["symbol", "date"])  # type: ignore[arg-type]


def _dv(df):
    import polars as pl
    return pl.when(pl.col("amount") > 0).then(pl.col("amount")).otherwise(pl.col("close") * pl.col("volume"))


def pool_members(narrow, watch: set[str], excluded: set[str]):
    """(symbol, date) 训练池成员: 逐日前 300 流动股 ∪ 自选。narrow 列: symbol date close volume amount raw_close。"""
    import polars as pl
    d = narrow.with_columns(_dv(narrow).alias("dv")).sort(["symbol", "date"])
    d = d.with_columns([pl.col("dv").shift(1).rolling_median(60, min_samples=40).over("symbol").alias("adv60m"),
                        pl.col("raw_close").shift(1).over("symbol").alias("praw")])
    d = d.filter(pl.col("date") >= TRAIN_START)
    wl = pl.col("symbol").is_in(sorted(watch))
    d = d.filter(wl | ~pl.col("symbol").is_in(sorted(excluded)))
    d = d.with_columns(pl.when(pl.col("praw") >= POOL_MIN_PRICE).then(pl.col("adv60m")).otherwise(None)
                       .rank("ordinal", descending=True).over("date").alias("liq_rank"))
    return d.filter(wl | (pl.col("liq_rank") <= POOL_TOP)).select(["symbol", "date"])


def build_panel(daily):
    """每 (symbol, 入场日 t) 一行; 特征只用 t-1 及更早, 另带 r5d (t 开盘 → t+4 收盘)。"""
    import polars as pl
    d = daily.sort(["symbol", "date"]).with_columns(_dv(daily).alias("dv"))
    d = d.with_columns([pl.col("close").shift(k).over("symbol").alias(f"c{k}") for k in (1, 2, 6, 61)]
                       + [pl.col("high").shift(1).over("symbol").alias("h1"), pl.col("low").shift(1).over("symbol").alias("l1")])
    d = d.with_columns([
        pl.col("dv").shift(1).rolling_mean(20).over("symbol").alias("adv20"),
        pl.max_horizontal(pl.col("high") - pl.col("low"), (pl.col("high") - pl.col("c1")).abs(),
                          (pl.col("low") - pl.col("c1")).abs()).alias("tr"),
        (pl.col("close") / pl.col("open") - 1).alias("roc"),
        (pl.col("open") / pl.col("c1") - 1).alias("on_ret"),
        pl.col("close").shift(-4).over("symbol").alias("c5f"),
    ])
    d = d.with_columns([
        pl.col("tr").shift(1).rolling_mean(14).over("symbol").alias("atr"),
        pl.col("roc").shift(1).rolling_mean(20, min_samples=15).over("symbol").alias("hksoc"),
        pl.col("on_ret").shift(1).rolling_mean(20, min_samples=15).over("symbol").alias("on20"),
    ])
    d = d.with_columns([
        (-(pl.col("c1") / pl.col("c2") - 1)).alias("rev1"),
        (-((pl.col("c1") - pl.col("l1")) / (pl.col("h1") - pl.col("l1")))).alias("ibs"),
        (-(pl.col("c1") / pl.col("c6") - 1)).alias("rev5"),
        (pl.col("c6") / pl.col("c61") - 1).alias("mom"),
        (pl.col("atr") / pl.col("c1")).alias("atrp"),
        pl.col("adv20").log().alias("ladv"),
        (pl.col("c5f") / pl.col("open") - 1).alias("r5d"),
    ])
    return d.select(["symbol", "date", *FEATURES, "r5d"])


def _finite_matrix(df):
    import numpy as np
    x = df.select(list(FEATURES)).to_numpy().astype(float)
    x[~np.isfinite(x)] = np.nan                  # 除零的 ±inf 当缺值 (模型原生处理缺值)
    return x


def feature_rows(daily, symbols: list[str]) -> dict[str, dict]:
    """名单日的特征: 把每只票最后一根已收盘日K 当作 "t-1"。{票: {特征..., close, atr, bars, last}}。"""
    import numpy as np
    import polars as pl
    out: dict[str, dict] = {}
    for (sym,), g in daily.filter(pl.col("symbol").is_in(list(symbols))).group_by(["symbol"]):
        g = g.sort("date")
        n = g.height
        if n < 62:
            continue
        o, h, lo, c = (g[k].to_numpy().astype(float) for k in ("open", "high", "low", "close"))
        amt = g["amount"].fill_null(0.0).to_numpy().astype(float)
        vol = g["volume"].fill_null(0.0).to_numpy().astype(float)
        dv = np.where(amt > 0, amt, c * vol)
        tr = np.maximum.reduce([h[1:] - lo[1:], np.abs(h[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])])
        atr = float(tr[-14:].mean())
        rng = h[-1] - lo[-1]
        mdv = float(dv[-20:].mean())
        with np.errstate(divide="ignore", invalid="ignore"):
            f = {"rev1": -(c[-1] / c[-2] - 1), "ibs": -((c[-1] - lo[-1]) / rng) if rng > 0 else float("nan"),
                 "rev5": -(c[-1] / c[-6] - 1), "mom": c[-6] / c[-61] - 1,
                 "hksoc": float((c / o - 1)[-20:].mean()), "on20": float((o[1:] / c[:-1] - 1)[-20:].mean()),
                 "atrp": atr / c[-1], "ladv": math.log(mdv) if mdv > 0 else float("nan")}
        out[str(sym)] = {**{k: float(v) for k, v in f.items()}, "close": float(c[-1]), "atr": atr, "bars": n,
                         "last": g["date"][-1]}
    return out


# ── 训练 ──────────────────────────────────────────────────────────

def _load_model(p: Path) -> dict | None:
    try:
        with p.open("rb") as f:
            m = pickle.load(f)  # 本服务自己写在 data/ 下的模型文件, 不接受外部上传
    except Exception as e:
        logger.warning("ls_rank: model load failed %s: %s", p.name, type(e).__name__)
        return None
    if not (isinstance(m, dict) and "est" in m and isinstance(m.get("meta"), dict)):
        return None
    meta = m["meta"]
    if meta.get("version") != MODEL_VERSION or meta.get("sklearn") != _sklearn_version():
        return None                               # 口径或依赖版本变了 → 重训
    return m


def train(data_dir: Path, day: date, *, narrow=None, daily=None, watch: list[str] | None = None,
          excluded: set[str] | None = None) -> dict:
    """训练 day 所在季度的模型并落盘 (同季度已有且口径一致 → 直接读)。"""
    q = quarter_of(day)
    mp = model_path(data_dir, q)
    if mp.exists():
        m = _load_model(mp)
        if m is not None:
            return m
    from app.services.heavy_job_limiter import shared_heavy_job_limiter
    with shared_heavy_job_limiter.slot("normal", timeout=1800):
        return _train(data_dir, day, narrow=narrow, daily=daily, watch=watch, excluded=excluded)


def _train(data_dir: Path, day: date, *, narrow, daily, watch, excluded) -> dict:
    import numpy as np
    import polars as pl
    from sklearn.ensemble import HistGradientBoostingRegressor
    q = quarter_of(day)
    mp = model_path(data_dir, q)
    end = train_end(day)
    data_end = end + timedelta(days=10)          # 让 end 之前入场日的 5 日目标能算出来
    watch_set = set(watch if watch is not None else _watchlist())
    if narrow is None:
        narrow = load_daily(data_dir, TRAIN_START - timedelta(days=WARMUP_DAYS), data_end,
                            columns=("close", "volume", "amount", "raw_close"))
    members = pool_members(narrow, watch_set, excluded if excluded is not None else excluded_symbols(data_dir))
    syms = sorted(set(members["symbol"].unique().to_list()))
    if daily is None:
        daily = load_daily(data_dir, TRAIN_START - timedelta(days=WARMUP_DAYS), data_end, symbols=syms)
    t = build_panel(daily.filter(pl.col("symbol").is_in(syms))).join(members, on=["symbol", "date"], how="inner")
    t = t.filter((pl.col("date") < end) & pl.col("r5d").is_not_null() & pl.col("r5d").is_finite())
    if t.height < MIN_TRAIN_ROWS:
        raise ValueError(f"ls_rank: 训练样本太少 ({t.height})")
    t = t.with_columns((pl.col("r5d") - pl.col("r5d").mean().over("date")).alias("yd"))
    y = t["yd"].to_numpy()
    lo, hi = float(np.quantile(y, 0.01)), float(np.quantile(y, 0.99))
    from threadpoolctl import threadpool_limits
    with threadpool_limits(limits=TRAIN_THREADS):
        est = HistGradientBoostingRegressor(**GBM).fit(_finite_matrix(t), np.clip(y, lo, hi))
    meta = {"version": MODEL_VERSION, "sklearn": _sklearn_version(), "quarter": q,
            "trained_to": t["date"].max().isoformat(), "rows": t.height, "symbols": t["symbol"].n_unique(),
            "days": t["date"].n_unique(), "trained_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    mp.parent.mkdir(parents=True, exist_ok=True)
    tmp = mp.with_suffix(".tmp")
    with tmp.open("wb") as f:
        pickle.dump({"meta": meta, "est": est}, f)
    tmp.replace(mp)
    logger.info("ls_rank: trained %s rows=%d symbols=%d to=%s", q, meta["rows"], meta["symbols"], meta["trained_to"])
    return {"meta": meta, "est": est}


# ── 出名单 ────────────────────────────────────────────────────────

def _earnings(data_dir: Path, syms: list[str]) -> dict[str, dict]:
    from app.services.earnings_calendar import EarningsCalendar
    cal = EarningsCalendar(data_dir)
    out = {}
    for s in syms:
        try:
            ev = cal.event_for(s)
        except Exception:
            ev = None
        if isinstance(ev, dict) and ev.get("date"):
            out[s] = {"date": str(ev["date"])[:10], "timing": ev.get("timing")}
    return out


def _signal_tags(data_dir: Path, as_of: date, syms: set[str]) -> dict[str, list[tuple[str, str]]]:
    """当天盘后自选信号表里命中的现有策略 [(方向, 中文名)] —— 只作理由标签, 不进排序。读不到 = 空。"""
    from app.services import swing_desk as sd
    try:
        snap = sd.load_watchlist_snapshot(data_dir, as_of) or {}
    except Exception:
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    for r in snap.get("signals") or []:
        if not isinstance(r, dict) or r.get("symbol") not in syms or r.get("side") not in ("long", "short"):
            continue
        rule = str(r.get("rule") or "")
        if not rule or rule.startswith(("phantom_4h", "ai_", "__")):
            continue
        out.setdefault(r["symbol"], []).append((r["side"], sd.strategy_name(rule, r["side"])))
    return out


def _prev_list(data_dir: Path, before: date) -> dict | None:
    for d in reversed(list_days(data_dir)):
        if d < before:
            rec = load_day(data_dir, d)
            if rec is not None:
                return rec
    return None


def generate(data_dir: Path, as_of: date, *, next_day: date | None = None, watch: list[str] | None = None,
             daily=None, model: dict | None = None, force: bool = False) -> dict:
    """as_of = 已定版的收盘日; 名单写给 next_day (默认下一个交易日)。已存在且非 force → 不动。"""
    import numpy as np
    import polars as pl

    from app.market_clock import us_clock
    data_dir = Path(data_dir)
    nd = next_day or us_clock.next_trading_day(as_of)
    if load_day(data_dir, nd) is not None and not force:
        return {"status": "exists", "day": nd.isoformat()}
    syms = sorted(set(watch if watch is not None else _watchlist()))
    if not syms:
        return {"status": "skipped", "reason": "empty_watchlist"}
    with _LOCK:
        if daily is None:
            daily = load_daily(data_dir, as_of - timedelta(days=FEATURE_DAYS), as_of, symbols=syms)
        daily = daily.filter(pl.col("date") <= as_of)
        feats = feature_rows(daily, syms)
        ok = {s: f for s, f in feats.items() if f["last"] == as_of}
        stale = sorted(s for s, f in feats.items() if f["last"] != as_of)
        if len(ok) < MIN_RANKED:
            return {"status": "skipped", "reason": "too_few_symbols", "n": len(ok)}
        model = model or train(data_dir, nd, watch=syms)
        order = sorted(ok)
        x = np.array([[ok[s][k] for k in FEATURES] for s in order], dtype=float)
        x[~np.isfinite(x)] = np.nan
        pred = model["est"].predict(x)
    ranked = sorted(zip(order, (float(p) for p in pred), strict=True), key=lambda z: (-z[1], z[0]))
    n = len(ranked)
    prev = _prev_list(data_dir, nd)
    prev_rank = {r["symbol"]: r.get("rank") for r in (prev or {}).get("rows") or [] if isinstance(r, dict) and r.get("symbol")}
    prev_long, prev_short = set((prev or {}).get("long") or []), set((prev or {}).get("short") or [])
    eligible = [s for s, _ in ranked if ok[s]["bars"] >= MIN_HISTORY]
    longs = eligible[:TOP_N]
    shorts = [s for s in reversed(eligible) if s not in longs][:TOP_N]
    keep_long = set(eligible[:2 * TOP_N]) - set(longs)
    keep_short = set(list(reversed(eligible))[:2 * TOP_N]) - set(shorts) - set(longs)
    earn = _earnings(data_dir, syms)
    tags = _signal_tags(data_dir, as_of, set(syms))
    rows = []
    for i, (s, sc) in enumerate(ranked):
        f = ok[s]
        side = "long" if s in longs else "short" if s in shorts else None
        book = side or ("keep_long" if s in keep_long else "keep_short" if s in keep_short
                        else "out_long" if s in prev_long else "out_short" if s in prev_short else None)
        names = sorted({nm for sd_, nm in tags.get(s, []) if side is None or sd_ == side})
        rows.append({"symbol": s, "rank": i + 1, "pct": round(100 * (1 - (i + 0.5) / n), 1), "score_bp": round(sc * 1e4, 1),
                     "side": side, "young": f["bars"] < MIN_HISTORY, "bars": f["bars"],
                     "close": round(f["close"], 4), "atr": round(f["atr"], 4), "earnings": earn.get(s), "tags": names[:4],
                     "prev_rank": prev_rank.get(s),
                     "is_new": bool(side) and prev is not None and s not in (prev_long if side == "long" else prev_short),
                     "book": book})
    rnd = random.Random(f"{RANDOM_SEED}-{nd.isoformat()}")    # 随机对照: 同一批可上榜票里随机 5 多 5 空, 前向一起结算
    pool = list(eligible)
    rnd.shuffle(pool)
    from app.market_clock import US_SESSION_OPEN
    made = us_clock.market_datetime(us_clock.now())
    late = made >= datetime.combine(nd, US_SESSION_OPEN, tzinfo=us_clock.tz)
    rec = {"contract": CONTRACT, "day": nd.isoformat(), "as_of": as_of.isoformat(),
           "made_at": made.isoformat(timespec="seconds"), **({"late": True} if late else {}),
           "model": {"version": MODEL_VERSION, "quarter": model["meta"]["quarter"],
                     "trained_to": model["meta"]["trained_to"], "rows": model["meta"]["rows"]},
           "params": {"top": TOP_N, "hold_days": HOLD_DAYS, "stop_atr": STOP_ATR, "cost": COST, "min_history": MIN_HISTORY},
           "n": n, "missing": sorted(set(syms) - set(feats)), "stale": stale, "prev_day": (prev or {}).get("day"),
           "long": longs, "short": shorts, "random": {"long": pool[:TOP_N], "short": pool[TOP_N:2 * TOP_N]},
           "rows": rows}
    _write_json(day_path(data_dir, nd), rec)
    logger.info("ls_rank %s (as of %s): long=%s short=%s n=%d", nd, as_of, longs, shorts, n)
    return {"status": "ok", "day": nd.isoformat(), "long": longs, "short": shorts, "n": n}


# ── 结算 ──────────────────────────────────────────────────────────

def exit_r(bars: list[tuple], side: str, atr: float, *, stop_atr: float = STOP_ATR, cost: float = COST) -> tuple[float, str] | None:
    """bars = [(开, 高, 低, 收)] 从入场日起至少 HOLD_DAYS 根。开盘进; 次日起开盘跳空穿止损按开盘价出;
    盘中触及止损按止损出 (−stop_atr); 否则第 HOLD_DAYS 天收盘出。返回 (ATR 计的 R, 'stop' | 'time')。"""
    if len(bars) < HOLD_DAYS or not atr or atr <= 0:
        return None
    sg = 1 if side == "long" else -1
    e = bars[0][0]
    stop = e - sg * stop_atr * atr
    c = cost * e / atr
    for k in range(HOLD_DAYS):
        o, h, lo, _cl = bars[k]
        if k > 0 and ((sg > 0 and o <= stop) or (sg < 0 and o >= stop)):
            return sg * (o - e) / atr - c, "stop"
        if (lo <= stop) if sg > 0 else (h >= stop):
            return -stop_atr - c, "stop"
    return sg * (bars[HOLD_DAYS - 1][3] - e) / atr - c, "time"


def settle_record(rec: dict, daily) -> dict | None:
    """一份名单的结算: 名单上全部可结算的票 (自选全体也要, 用来算 "看多 − 自选全体")。缺上榜票的 K 线 → None (下次再试)。"""
    import polars as pl
    day = date.fromisoformat(rec["day"])
    rows = {r["symbol"]: r for r in rec.get("rows") or [] if isinstance(r, dict) and r.get("symbol")}
    out: dict[str, dict] = {}
    for (sym,), g in daily.filter((pl.col("date") >= day) & pl.col("symbol").is_in(list(rows))).group_by(["symbol"]):
        g = g.sort("date").head(HOLD_DAYS)
        if g.height < HOLD_DAYS or g["date"][0] != day:
            continue
        bars = list(zip(g["open"].to_list(), g["high"].to_list(), g["low"].to_list(), g["close"].to_list(), strict=True))
        a = _num(rows[sym].get("atr"))
        rl = exit_r(bars, "long", a) if a else None
        rs = exit_r(bars, "short", a) if a else None
        if rl and rs:
            out[str(sym)] = {"long": round(rl[0], 4), "long_exit": rl[1], "short": round(rs[0], 4), "short_exit": rs[1],
                             "end": g["date"][-1].isoformat()}
    need = set(rec.get("long") or []) | set(rec.get("short") or [])
    if not need or not need <= set(out):
        return None
    return out


def settle(data_dir: Path, through: date, *, daily=None) -> dict:
    """结算全部 HOLD_DAYS 根 K 线已在 through (已定版收盘日) 之前齐全、还没结算的名单。幂等。"""
    from app.market_clock import us_clock
    data_dir = Path(data_dir)
    todo = [rec for rec in (load_day(data_dir, d) for d in list_days(data_dir) if d <= through)
            if rec is not None and not rec.get("settled")]
    if not todo:
        return {"settled": 0}
    if daily is None:
        lo = min(date.fromisoformat(r["day"]) for r in todo)
        syms = sorted({r["symbol"] for rec in todo for r in rec.get("rows") or [] if isinstance(r, dict) and r.get("symbol")})
        daily = load_daily(data_dir, lo, through, symbols=syms, columns=("open", "high", "low", "close"))
    n = 0
    for rec in todo:
        res = settle_record(rec, daily)
        if res is None:
            continue
        rec["settled"] = {"at": us_clock.now().isoformat(timespec="seconds"), "end": max(v["end"] for v in res.values()), "r": res}
        _write_json(day_path(data_dir, date.fromisoformat(rec["day"])), rec)
        n += 1
    return {"settled": n, "pending": len(todo) - n}


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _r3(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


def series(recs: list[dict]) -> dict | None:
    """一组已结算名单的汇总 (单位 ATR)。合计用已舍入的两项相加, 页面上加法对得上。"""
    lv, sv, rlv, rsv, av, pd_ = [], [], [], [], [], []
    for rec in recs:
        r = rec["settled"]["r"]
        ll = [r[s]["long"] for s in rec.get("long") or [] if s in r]
        ss = [r[s]["short"] for s in rec.get("short") or [] if s in r]
        rlv += [r[s]["long"] for s in (rec.get("random") or {}).get("long") or [] if s in r]
        rsv += [r[s]["short"] for s in (rec.get("random") or {}).get("short") or [] if s in r]
        al = [v["long"] for v in r.values()]
        lv += ll
        sv += ss
        if al:
            av.append(sum(al) / len(al))
        if ll and ss:
            pd_.append(sum(ll) / len(ll) + sum(ss) / len(ss))
    if not lv or not sv:
        return None
    ml, ms, mrl, mrs, ma = (_r3(_mean(v)) for v in (lv, sv, rlv, rsv, av))
    assert ml is not None and ms is not None
    return {"n_lists": len(recs), "n_long": len(lv), "n_short": len(sv),
            "long": ml, "short": ms, "pair": _r3(ml + ms),
            "rand_long": mrl, "rand_short": mrs, "rand_pair": _r3(mrl + mrs) if mrl is not None and mrs is not None else None,
            "all": ma, "long_minus_all": _r3(ml - ma) if ma is not None else None,
            "win_long": _r3(sum(v > 0 for v in lv) / len(lv)), "win_short": _r3(sum(v > 0 for v in sv) / len(sv)),
            "win_pair": _r3(sum(v > 0 for v in pd_) / len(pd_)) if pd_ else None}


_SETTLE_CACHE: dict[str, tuple[tuple, dict]] = {}


def settle_view(data_dir: Path) -> dict:
    """前向结算汇总 (全部 / 近 8 周 / 最近一份)。按名单文件签名缓存, 页面 30 秒一轮不重复解析。"""
    r = root(data_dir)
    files = sorted(r.glob("*.json")) if r.exists() else []
    sig = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    hit = _SETTLE_CACHE.get(str(r))
    if hit and hit[0] == sig:
        return hit[1]
    done = sorted((x for x in (load_day(data_dir, d) for d in list_days(data_dir)) if x and x.get("settled")),
                  key=lambda x: x["day"])
    recs = [x for x in done if not x.get("late")]          # 开盘后才生成的名单不计入前向成绩
    n_late = sum(1 for x in (load_day(data_dir, d) for d in list_days(data_dir)) if x and x.get("late"))
    weeks: dict[str, list[dict]] = {}
    for x in recs:
        d = date.fromisoformat(x["day"])
        weeks.setdefault((d - timedelta(days=d.weekday())).isoformat(), []).append(x)
    wk = []
    for k in sorted(weeks)[-8:]:
        s = series(weeks[k])
        if s:
            wk.append({"week": weeks[k][0]["day"][5:], **s})
    last = None
    if recs:
        lr, rr = recs[-1], recs[-1]["settled"]["r"]
        sides = {side: [{"symbol": s, "r": round(rr[s][side], 3), "exit": rr[s][f"{side}_exit"]} for s in lr.get(side) or [] if s in rr]
                 for side in ("long", "short")}
        last = {"made": lr["day"], "settled": lr["settled"]["end"], **sides,
                "sum_long": round(sum(x["r"] for x in sides["long"]), 3), "sum_short": round(sum(x["r"] for x in sides["short"]), 3)}
    out = {"days": len(recs), "check_days": CHECK_DAYS, "late": n_late, "first": recs[0]["day"] if recs else None,
           "last_list": recs[-1]["day"] if recs else None, "total": series(recs), "weeks": wk, "last": last}
    if len(_SETTLE_CACHE) > 8:
        _SETTLE_CACHE.clear()
    _SETTLE_CACHE[str(r)] = (sig, out)
    return out


# ── 页面投影 (只读) ────────────────────────────────────────────────

def risk_usd(data_dir: Path) -> tuple[float | None, float | None]:
    """(纸面权益, 单笔风险美元) = 最近定版净值 × 规则集单笔风险; 读不到 → 风险 None (页面不给股数)。"""
    from app.services import watchlist_v4 as wv
    eq = None
    try:
        nav = Path(data_dir) / "ledger" / "nav" / "daily.jsonl"
        lines = [x for x in nav.read_text(encoding="utf-8").splitlines() if x.strip()] if nav.exists() else []
        eq = _num(json.loads(lines[-1]).get("nav")) if lines else None
    except (OSError, ValueError, AttributeError):
        eq = None
    rp = wv.risk_pct_of(wv.current_ruleset(data_dir))
    if eq is None or rp is None:
        return eq, None
    return eq, round(eq * rp / 100, 2)


def ref_prices(data_dir: Path, repo, now: datetime, syms: list[str]) -> tuple[dict[str, float], dict[str, float], dict | None]:
    """参考价 / 盘前涨跌 / 盘前时点。开盘后 = 今天开盘价 (盘中快照); 盘前 = 09:00 跳空记录, 09:00 前用盘前实时价缓存。
    请求里不出网 (盘前缓存没有时后台单飞取, 本次不给)。"""
    from app.services import card_volume as cv
    from app.services import premarket_gap
    from app.services import swing_desk as sd
    today = now.date()
    px: dict[str, float] = {}
    pre: dict[str, float] = {}
    meta = None
    rec = premarket_gap.latest(data_dir) or {}
    if str(rec.get("day")) == today.isoformat():
        for s, v in (rec.get("gaps") or {}).items():
            if s in syms and _num(v) is not None:
                pre[s] = float(v)
        for s, v in (rec.get("mids") or {}).items():
            if s in syms and _num(v):
                px[s] = float(v)
        meta = {"at": str(rec.get("at") or "") or None, "live": False}
    elif cv.phase(now)["phase"] == "premarket":
        cached = cv.premarket_prices_cached(syms) or {}
        for s, v in cached.items():
            if s in syms and _num((v or {}).get("price")):
                px[s] = float(v["price"])
        meta = {"at": now.isoformat(timespec="seconds"), "live": True} if px else None
    try:
        bars = sd._today_bars(repo, today, set(syms)) if repo is not None else {}
    except Exception:
        bars = {}
    for s, (o, _h, _l, _c) in bars.items():
        if _num(o):
            px[s] = float(o)
    return px, pre, meta


_SPARK: dict[tuple, dict] = {}


def sparks(data_dir: Path, syms: list[str], as_of: date) -> dict[str, list[float]]:
    """上榜票最近 21 根收盘 (原始价, 只画走势)。同一天同一批票只读一次。"""
    key = (str(data_dir), as_of.isoformat(), tuple(sorted(syms)))
    if key in _SPARK:
        return _SPARK[key]
    out: dict[str, list[float]] = {}
    if syms:
        try:
            d = load_daily(data_dir, as_of - timedelta(days=45), as_of, symbols=syms, columns=("raw_close",), priority="interactive")
            for (s,), g in d.group_by(["symbol"]):
                c = g.sort("date")["raw_close"].drop_nulls().to_list()[-21:]
                if len(c) >= 2:
                    out[str(s)] = [round(float(x), 2) for x in c]
        except Exception as e:
            logger.warning("ls_rank: spark failed: %s", type(e).__name__)
            return out
    if len(_SPARK) > 8:
        _SPARK.clear()
    _SPARK[key] = out
    return out


def pick_day(days: list[date], today: date) -> date | None:
    """展示哪一份: 已出的未来名单 (收盘后出的下一交易日) 优先, 其次今天, 再其次最近一份。"""
    if not days:
        return None
    future = [d for d in days if d > today]
    if future:
        return future[0]
    return today if today in days else days[-1]


def view(data_dir: Path, *, repo=None, now: datetime | None = None) -> dict | None:
    """页面段 (只读)。没有名单 = None。"""
    from app.market_clock import us_clock
    data_dir = Path(data_dir)
    now = us_clock.market_datetime(now or us_clock.now())
    today = now.date()
    pick = pick_day(list_days(data_dir), today)
    rec = load_day(data_dir, pick) if pick else None
    if rec is None or pick is None:
        return None
    rows_all = [r for r in rec["rows"] if isinstance(r, dict) and isinstance(r.get("symbol"), str)]
    syms = [r["symbol"] for r in rows_all]
    live = pick == today and us_clock.is_trading_day(today)
    px, pre, pre_meta = ref_prices(data_dir, repo, now, syms) if live else ({}, {}, None)
    eq, risk = risk_usd(data_dir)
    until = pick
    try:
        for _ in range(HOLD_DAYS - 1):
            until = us_clock.next_trading_day(until)
    except ValueError:
        pass
    listed = set(rec.get("long") or []) | set(rec.get("short") or [])
    sp = sparks(data_dir, sorted(listed), date.fromisoformat(rec["as_of"]))
    rows = []
    for r in rows_all:
        s = r["symbol"]
        close, atr = _num(r.get("close")), _num(r.get("atr"))
        ref = px.get(s) or close
        pre_pct = pre.get(s)
        if pre_pct is None and s in px and close:
            pre_pct = (px[s] / close - 1) * 100
        side = r.get("side") if s in listed else None
        stop = qty = None
        if side and ref and atr:
            stop = round(ref - STOP_ATR * atr if side == "long" else ref + STOP_ATR * atr, 2)
            qty = math.floor(risk / (STOP_ATR * atr)) if risk else None
        rows.append({"symbol": s, "rank": r.get("rank"), "pct": r.get("pct"), "side": side, "young": bool(r.get("young")),
                     "earnings": r.get("earnings"), "tags": list(r.get("tags") or []), "prev_rank": r.get("prev_rank"),
                     "is_new": bool(r.get("is_new")), "book": r.get("book"),
                     "close": close, "atr": atr, "ref": round(ref, 2) if ref else None,
                     "pre_pct": round(pre_pct, 2) if pre_pct is not None else None, "stop": stop, "qty": qty,
                     "spark": sp.get(s)})
    by = {r["symbol"]: r for r in rows}
    return {"contract": CONTRACT, "day": rec["day"], "hold_until": until.isoformat(), "as_of": rec["as_of"],
            "made_at": rec.get("made_at"), "model": f"GBM {rec['model']['quarter']}", "trained_to": rec["model"]["trained_to"],
            "hold_days": HOLD_DAYS, "stop_atr": STOP_ATR, "risk_usd": risk, "equity": eq, "pre": pre_meta,
            "is_today": pick == today, "late": bool(rec.get("late")), "prev_day": rec.get("prev_day"), "backtest": BACKTEST,
            "long": [by[s] for s in rec.get("long") or [] if s in by],
            "short": [by[s] for s in rec.get("short") or [] if s in by],
            "all": rows, "n": rec.get("n"), "missing": rec.get("missing") or [], "stale": rec.get("stale") or [],
            "settle": settle_view(data_dir)}


# ── 调度入口 ──────────────────────────────────────────────────────

def run_post_close(data_dir: Path, day: date) -> dict:
    """盘后 (day 已定版): 先结算到期名单, 再出下一交易日名单。两步各自容错。"""
    out: dict[str, Any] = {}
    try:
        out["settle"] = settle(data_dir, day)
    except Exception as e:
        logger.warning("ls_rank settle failed (soft): %s", type(e).__name__)
        out["settle"] = {"error": type(e).__name__}
    try:
        out["generate"] = generate(data_dir, day)
    except Exception as e:
        logger.warning("ls_rank generate failed (soft): %s: %s", type(e).__name__, e)
        out["generate"] = {"error": type(e).__name__}
    return out


def ensure(data_dir: Path, repo) -> dict:
    """补跑 (17:20 / 08:20 ET): 按最近一个已定版收盘日结算 + 出名单 (已有则不动)。"""
    getter = getattr(repo, "latest_settled_daily_date", None)
    as_of = getter() if getter else None
    if as_of is None:
        return {"status": "skipped", "reason": "no_settled_day"}
    return {"as_of": as_of.isoformat(), **run_post_close(data_dir, as_of)}
