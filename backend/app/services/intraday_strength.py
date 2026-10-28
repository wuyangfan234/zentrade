"""盘中强势卡 (0.28.0, 无限 2026-10-20 拍板: 只上机会板 + 自动结算记战绩, 不下单; 波段出场)。

规则 (预注册 frozen offline research specification, 生产口径 = prodspec D1):
  美东 10:00-15:00 每 30 分钟检查一次自选, 形态只用 20 分钟前已完成的 5 分钟K (SIP 免费档扣留 15 分钟):
    涨幅 >= 2% (对昨收) · 现价在当日均价线 (VWAP) 上 · 量比 >= 1.5 (到此刻累计量 / 前 20 个交易日同一时刻均值)
    · 走势效率 >= 0.3173 (|现价-开盘| / 逐根路程, 训练段 2023-24 第 60 百分位, 冻结) · 70% 以上K线收在均价线上
    · 离当日高点回撤 <= 当日振幅 1/3 · 出卡那一刻实时价仍在均价线上。每只票每天最多一张。
  卡价位 (出卡即冻结): 入场 = 出卡时实时价 (+5 基点成本); 止损 = min(均价线, 近 30 分钟低点), 距入场限定 0.5%-4%;
  目标 = 入场 + 2 倍风险; 最多持有 5 个交易日 (出卡当天算第 1 天)。
  结算 (按 5 分钟K): 同一根既碰止损又碰目标按先止损; 跳空按开盘价; 第 5 天收盘仍未触发按收盘价; 每边 5 基点。
  量比基线只认 >= 30 根 5 分钟K 的交易日 (新股上市前零星成交日不算)。
回放 (2023-01~2026-10, 自选 68 只, 生产代码同路径 prod_stats.py): 训练 2023-24 每笔 +0.13R, 95% [0.02, 0.25];
  留出 2025-26 +0.10R, 95% [-0.04, 0.24] (预注册 97.5% 口径 [-0.06, 0.26]); 年约 860 张, 胜率 39%。
  留出区间跨 0 = 未证明有优势; 同规则在非自选 323 只上为负 (自选名单有事后挑选成分)。
  所以只记录不下单: 不写台账、不进交易环、不进下单簿 (测试 test_not_in_trading_chain 守着)。

数据: Alpaca SIP 5 分钟K (adjustment=split, 常规时段), 只在定时任务里出网 (不在交易环/接口里)。
  前 30 个交易日缓存到 data/opportunities/intraday/m5/<日>.parquet (量比基线 + 结算), 冷启动生产实测 232 秒 / 102 次请求,
  之后每天补前一天约 5 秒; 每天第一次运行时现拉最近缓存日比收盘, 差 > 1% (拆并股) 的票整段重拉。
  取数失败不抛给调度器 (只记录的功能不推飞书), 记进当天记录 errors, 状态页标黄, 下一个检查点自动重试。
  跨进程文件锁 (定时任务与 docker exec 补算互斥)。
记录: data/opportunities/intraday/<出卡美东日>.json = {day, cards, checks, errors} (原子整写; 结算只改卡的结局字段)。
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

logger = logging.getLogger(__name__)
_UTC = UTC

CONTRACT = "intraday-strength-v1"
BAR_MIN = 5
SLOTS = 78                      # 09:30-16:00 = 78 根 5 分钟K
OPEN = time(9, 30)
CHECKS = [time(h, m) for h in range(10, 16) for m in (0, 30)][:11]   # 10:00 ... 15:00
DELAY_MIN = 20                  # 形态只用 t-20 分钟前已完成的K
LOOKBACK_BARS = 6               # 近 30 分钟低点
BASE_DAYS = 20                  # 量比基线天数
MIN_DAY_BARS = 30               # 一个交易日至少 30 根 5 分钟K 才算“有效日” (新股上市前的零星成交日不算, 否则量比基线被稀释)
HIST_FETCH_DAYS = 30            # 缓存回看交易日 (留出假日/缺数余量)
MIN_CHG = 0.02
MIN_RVOL = 1.5
MIN_ER = 0.3173                 # 训练段第 60 百分位 (replay.py ER_q60 = 0.317303), 冻结
MIN_ABOVE = 0.70
MAX_PULL = 0.33
SLIP = 0.0005
MIN_RISK = 0.005
MAX_RISK = 0.04
TARGET_R = 2.0
HOLD_DAYS = 5
MAX_LATE_S = 600                # 定时任务晚于检查点 10 分钟以上 = 过期, 不出卡 (形态与价已不是那一刻)
LIVE_MAX_AGE_S = 120
SUSPECT_GAP = 0.40              # 开盘对昨收 > 40% = 疑似拆并股/数据错, 不出卡
SPLIT_MISMATCH = 0.15           # 现拉的出卡那一根与卡上入场参考价差 > 15% = 期间拆并股 (5:4 拆股也能识别), 卡作废
ADJ_DRIFT = 0.01                # 缓存里昨收与现拉的昨收差 > 1% = 复权口径变了 (拆股), 该票历史缓存重拉
PARTIAL_S = 60                  # 出卡晚于所在 5 分钟K 开始 60 秒以上 -> 那一根只看收盘
EXCLUDE = frozenset({"SPY", "QQQ", "DIA", "IWM"})
HISTORY_MAX = 40
# 回放成绩 (生产代码同路径重算 prod_stats.py, 留出段 2025-01~2026-10, 每笔 R 与周聚类 95% 区间): 只展示, 不参与出卡
ODDS = {"n": 1541, "win": 0.386, "mean_r": 0.098, "ci_r": [-0.037, 0.235], "train_mean_r": 0.131,
        "period": "2025-01~2026-10", "sig": 0}
NOTIONAL_CAP = 0.25


# ── 路径 ───────────────────────────────────────────────
def root(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "intraday"


def day_path(data_dir: Path, d: date) -> Path:
    return root(data_dir) / f"{d.isoformat()}.json"


def cache_path(data_dir: Path, d: date) -> Path:
    return root(data_dir) / "m5" / f"{d.isoformat()}.parquet"


def card_id(symbol: str, d: date) -> str:
    return f"ids-{d.isoformat()}-{symbol}"


# ── 记录读写 ─────────────────────────────────────────────
def load_day(data_dir: Path, d: date) -> dict:
    empty = {"day": d.isoformat(), "cards": [], "checks": [], "errors": []}
    try:
        raw = json.loads(day_path(data_dir, d).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(raw, dict):
        return empty
    cards = [c for c in raw.get("cards") or [] if isinstance(c, dict) and c.get("symbol") and c.get("card_id")]
    checks = [c for c in raw.get("checks") or [] if isinstance(c, dict)]
    errors = [c for c in raw.get("errors") or [] if isinstance(c, dict)]
    out = {"day": d.isoformat(), "cards": cards, "checks": checks, "errors": errors}
    if isinstance(raw.get("ok_at"), str):          # 0.35.3: 最近一次取数成功 (检查点或纯结算), 状态页据此判「已恢复」
        out["ok_at"] = raw["ok_at"]
    return out


def _save_day(data_dir: Path, rec: dict) -> None:
    from app.services.fs_utils import atomic_write_text
    p = day_path(data_dir, date.fromisoformat(rec["day"]))
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(rec, ensure_ascii=False, allow_nan=False))


def record_days(data_dir: Path) -> list[date]:
    out = []
    d = root(data_dir)
    for f in d.glob("20*.json") if d.exists() else []:
        try:
            day = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if f.stem == day.isoformat():
            out.append(day)
    return sorted(out)


# ── 5 分钟K: 帧 <-> 每票每日 78 槽数组 ──────────────────────
def _slot_frame(df: pl.DataFrame) -> pl.DataFrame:
    """symbol, t(UTC ISO) ... -> + d (美东日), k (槽 0..77); 只留常规时段。"""
    if df.is_empty():
        return df.with_columns(pl.lit(None, pl.Date).alias("d"), pl.lit(None, pl.Int32).alias("k"))
    et = pl.col("t").str.to_datetime(time_zone="UTC").dt.convert_time_zone("America/New_York")
    df = df.with_columns(et.alias("_et"))
    df = df.with_columns(
        pl.col("_et").dt.date().alias("d"),
        ((pl.col("_et").dt.hour().cast(pl.Int32) * 60 + pl.col("_et").dt.minute().cast(pl.Int32) - 570) // BAR_MIN).alias("k"),
    ).drop("_et")
    # 审计 L-3: 半日市 (13:00 收盘) 13:00 后的根是盘后, 槽上限按当日日历收盘截
    from app.market_clock import us_clock
    lim = {}
    for d in df["d"].unique().to_list():
        if d is None:
            continue
        c = us_clock.session_close(d)
        n = ((c.hour * 60 + c.minute) - (OPEN.hour * 60 + OPEN.minute)) // BAR_MIN
        if n < SLOTS:
            lim[d] = n
    cap = (pl.col("d").replace_strict(list(lim), list(lim.values()), default=SLOTS, return_dtype=pl.Int32)
           if lim else pl.lit(SLOTS))
    return df.filter((pl.col("k") >= 0) & (pl.col("k") < cap))


COLS = ("o", "h", "l", "c", "v", "vw")


def to_arrays(df: pl.DataFrame) -> dict[tuple[str, date], dict[str, np.ndarray]]:
    """{(symbol, 美东日): {o,h,l,c,v,vw: 长 78 的数组 (缺 = nan)}}。"""
    out: dict[tuple[str, date], dict[str, np.ndarray]] = {}
    if df.is_empty():
        return out
    df = _slot_frame(df)
    for (sym, d), g in df.group_by(["symbol", "d"]):
        arr = {c: np.full(SLOTS, np.nan) for c in COLS}
        k = g["k"].to_numpy()
        for c in COLS:
            arr[c][k] = g[c].to_numpy().astype(float)
        out[(str(sym), d)] = arr
    return out


def _ffill(a: np.ndarray) -> np.ndarray:
    idx = np.where(~np.isnan(a), np.arange(len(a)), 0)
    np.maximum.accumulate(idx, out=idx)
    return a[idx]


def completed_slots(check: time) -> int:
    """检查点 t 时可用的已完成槽数 (只用 t-20 分钟前结束的K)。10:00 -> 2 (09:30, 09:35)。"""
    m = check.hour * 60 + check.minute - DELAY_MIN - (OPEN.hour * 60 + OPEN.minute)
    return max(0, min(SLOTS, m // BAR_MIN))


def day_valid(a: dict) -> bool:
    """有效交易日: 有成交 (今天的数组用这个)。"""
    return bool(np.nansum(a["v"]) > 0)


def hist_valid(a: dict | None) -> bool:
    """可进量比基线的历史日: 有成交且 >= MIN_DAY_BARS 根 5 分钟K (0.28.0 审查: SPCX 上市前每天 1-7 根零星成交,
    旧口径算作有效日, 量比基线被稀释成 0 附近, 上市第 20 天量比虚高)。"""
    return a is not None and day_valid(a) and int(np.sum(~np.isnan(a["c"]))) >= MIN_DAY_BARS


def last_close(a: dict) -> float | None:
    c = a["c"][~np.isnan(a["c"])]
    return float(c[-1]) if len(c) else None


def features(a: dict, n: int, prev_close: float | None, base_cum: list[float]) -> dict | None:
    """当天数组 a 的前 n 槽 -> 形态特征 (与回放 replay.features 逐式相同)。数据不够 -> None。"""
    if n < 1 or prev_close is None or not (prev_close > 0):
        return None
    o, h, lo, c, v, vw = (a[x][:n] for x in COLS)
    if not np.nansum(v) > 0:
        return None
    vo = o[~np.isnan(o)]
    if not len(vo):
        return None
    dopen = float(vo[0])
    cf = _ffill(c)
    px_now = float(cf[-1])
    if math.isnan(px_now):
        return None
    v0 = np.nan_to_num(v)
    cv = np.cumsum(v0)
    cpv = np.cumsum(np.nan_to_num(vw) * v0)
    with np.errstate(invalid="ignore", divide="ignore"):
        rvwap = np.where(cv > 0, cpv / cv, np.nan)
    vwap = float(rvwap[-1])
    prev = np.concatenate([[dopen], cf[:-1]])
    path = float(np.sum(np.nan_to_num(np.abs(cf - prev))))
    with np.errstate(invalid="ignore"):
        above = float(np.sum(np.nan_to_num((cf > rvwap).astype(float)))) / n
    hh = h[~np.isnan(h)]
    ll = lo[~np.isnan(lo)]
    hod = float(hh.max()) if len(hh) else -math.inf
    lod = float(ll.min()) if len(ll) else math.inf
    pull = (hod - px_now) / (hod - lod) if hod > lod else math.nan
    lw = lo[max(0, n - LOOKBACK_BARS):]
    lw = lw[~np.isnan(lw)]
    low_n = float(lw.min()) if len(lw) else math.inf
    if len(base_cum) < BASE_DAYS:
        rvol = math.nan
    else:
        b = float(np.mean(base_cum[-BASE_DAYS:]))
        rvol = float(cv[-1]) / b if b > 0 else math.nan
    return {"P": px_now, "chg": px_now / prev_close - 1, "vwap": vwap, "rvol": rvol,
            "er": abs(px_now - dopen) / path if path > 0 else math.nan,
            "above": above, "pull": pull, "low_n": low_n, "dopen": dopen, "prev_close": prev_close}


def passes(f: dict | None) -> bool:
    if not f:
        return False
    try:
        return (f["chg"] >= MIN_CHG and f["P"] > f["vwap"] and f["rvol"] >= MIN_RVOL and f["er"] >= MIN_ER
                and f["above"] >= MIN_ABOVE and f["pull"] <= MAX_PULL
                and abs(f["dopen"] / f["prev_close"] - 1) <= SUSPECT_GAP)
    except (TypeError, KeyError):
        return False


def geometry(entry_ref: float, vwap: float, low_n: float) -> dict | None:
    """入场 (含 5 基点) / 止损 (min(均价线, 近 30 分钟低点), 距入场 0.5%-4%) / 目标 (+2R)。实时价不在止损上方 -> None。"""
    stop_raw = min(vwap, low_n)
    if not (entry_ref > 0) or math.isnan(stop_raw) or not (entry_ref > stop_raw):
        return None
    entry = entry_ref * (1 + SLIP)
    stop = stop_raw
    if entry - stop < MIN_RISK * entry:
        stop = entry * (1 - MIN_RISK)
    if entry - stop > MAX_RISK * entry:
        stop = entry * (1 - MAX_RISK)
    risk = entry - stop
    return {"entry_ref": round(entry_ref, 4), "entry": round(entry, 4), "stop": round(stop, 4),
            "target": round(entry + TARGET_R * risk, 4), "risk": round(risk, 4),
            "stop_pct": round((stop / entry - 1) * 100, 2), "target_pct": round(TARGET_R * risk / entry * 100, 2)}


def base_cums(history: list[dict | None], n: int) -> list[float]:
    """前若干个交易日 (旧 -> 新) 的数组 -> 有效历史日到同一槽的累计量 (旧 -> 新)。"""
    out = []
    for a in history:
        if a is not None and hist_valid(a):
            out.append(float(np.nansum(np.nan_to_num(a["v"][:n]))))
    return out


def eligible(history: list[dict | None]) -> bool:
    """前 BASE_DAYS 个有效历史日齐、且昨天本身是有效日 (昨收可信)。"""
    prev = history[-1] if history else None
    return sum(1 for a in history if hist_valid(a)) >= BASE_DAYS and hist_valid(prev)


def evaluate(symbol: str, today: dict, history: list[dict | None], prev_day: dict | None, check: time,
             live: float | None) -> dict | None:
    """单票单检查点: 过规则 -> 卡价位与特征; 不过 -> None。history = 今天之前的交易日数组 (旧 -> 新, 缺 = None)。"""
    n = completed_slots(check)
    if not eligible(history) or prev_day is None:
        return None
    f = features(today, n, last_close(prev_day), base_cums(history, n))
    if not passes(f) or live is None or not (live > f["vwap"]):
        return None
    g = geometry(float(live), f["vwap"], f["low_n"])
    if g is None:
        return None
    feat = {k: (None if isinstance(f[k], float) and not math.isfinite(f[k]) else round(float(f[k]), 4))
            for k in ("chg", "vwap", "rvol", "er", "above", "pull", "low_n", "prev_close", "P")}
    return {**g, "features": feat}


# ── 结算 ─────────────────────────────────────────────────
def settle(card: dict, bars: list[tuple[date, dict]], *, window_over: bool) -> dict | None:
    """按 5 分钟K 结算一张卡。bars = [(美东日, 当日数组)] 出卡日起 5 个交易日内有数据的日子 (旧 -> 新, 每次结算现拉,
    复权口径一致)。window_over = 第 5 个交易日已收盘 (且数据已到)。返回 {exit, exit_reason, exit_at, r} 或 None (还没结束)。
    出卡那一根起算, 与回放同口径; slot_partial (出卡晚于该根开始 60 秒以上) 时那一根只看收盘, 不用出卡前的高低点。
    持有期跳空是真实盈亏, 照算; 只有出卡那一根的价位与卡上入场参考价对不上 (> 20%) 才判拆并股作废。"""
    entry, stop, target = float(card["entry"]), float(card["stop"]), float(card["target"])
    risk = entry - stop
    if not risk > 0:
        return None
    k0 = int(card.get("slot") or 0)
    partial = bool(card.get("slot_partial"))
    first = True
    last_px, last_at = None, ""
    fire_day = str(card.get("day") or "")
    ref = card.get("entry_ref")
    for d, a in bars[:HOLD_DAYS]:
        on_fire_day = d.isoformat() == fire_day
        start = k0 if on_fire_day else 0
        if on_fire_day and isinstance(ref, (int, float)) and ref > 0:
            px0 = a["o"][k0] if not math.isnan(a["o"][k0]) else a["c"][k0]
            if not math.isnan(px0) and abs(float(px0) / float(ref) - 1) > SPLIT_MISMATCH:
                return {"exit": None, "exit_reason": "corp_action_suspect", "exit_at": f"{d.isoformat()}T{_slot_time(k0)}",
                        "r": None}
        for k in range(start, SLOTS):
            o, h, lo, c = a["o"][k], a["h"][k], a["l"][k], a["c"][k]
            gap_ok = not first                  # 与回放同: 出卡那一槽不看开盘跳空, 之后每一槽 (含无成交槽之后) 都看
            entry_bar = first
            first = False
            if math.isnan(o) and math.isnan(c):
                continue
            at = f"{d.isoformat()}T{_slot_time(k)}"
            if gap_ok:
                if not math.isnan(o) and o <= stop:
                    return _exit(card, o, "stop", at, entry, risk)
                if not math.isnan(o) and o >= target:
                    return _exit(card, o, "target", at, entry, risk)
            if entry_bar and partial:           # 出卡前的那段高低点不算, 只看这一根的收盘
                if not math.isnan(c) and c <= stop:
                    return _exit(card, c, "stop", at, entry, risk)
                if not math.isnan(c) and c >= target:
                    return _exit(card, c, "target", at, entry, risk)
            else:
                if not math.isnan(lo) and lo <= stop:
                    return _exit(card, stop, "stop", at, entry, risk)
                if not math.isnan(h) and h >= target:
                    return _exit(card, target, "target", at, entry, risk)
            if not math.isnan(c):
                last_px, last_at = c, at
    if window_over:
        if last_px is None:
            return {"exit": None, "exit_reason": "no_data", "exit_at": None, "r": None}
        return _exit(card, last_px, "time", last_at, entry, risk)
    return None


def _slot_time(k: int) -> str:
    m = OPEN.hour * 60 + OPEN.minute + k * BAR_MIN
    return f"{m // 60:02d}:{m % 60:02d}"


def _exit(card: dict, px: float, reason: str, at: str, entry: float, risk: float) -> dict:
    r = (float(px) * (1 - SLIP) - entry) / risk
    return {"exit": round(float(px), 4), "exit_reason": reason, "exit_at": at, "r": round(r, 4)}


# ── 取数 (只在定时任务里) ──────────────────────────────────
def fetch_bars(symbols: list[str], start: date, end: date, *, until: datetime | None = None) -> pl.DataFrame:
    """Alpaca SIP 5 分钟K [start 09:30, end 16:00 (或 until)] ET, adjustment=split。失败抛异常。"""
    from app.market_clock import us_clock
    from app.plugins.alpaca import provider as ap
    p = ap.AlpacaProvider()
    if not p.key or not p.secret:
        raise RuntimeError("alpaca credentials missing")
    lo = datetime.combine(start, OPEN, tzinfo=us_clock.tz)
    hi = datetime.combine(end, us_clock.session_close(end), tzinfo=us_clock.tz)
    if until is not None and until < hi:
        hi = until
    rows = []
    for sym, b in ap._iter_bars(p.key, p.secret, list(symbols), "5Min", lo, hi, "split", "sip"):
        try:
            rows.append((sym, str(b["t"]), float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]),
                         float(b["v"]), float(b.get("vw") or b["c"])))
        except (KeyError, TypeError, ValueError):
            continue
    return pl.DataFrame(rows, schema={"symbol": pl.Utf8, "t": pl.Utf8, **{c: pl.Float64 for c in COLS}}, orient="row")


def _read_cache(data_dir: Path, d: date) -> tuple[pl.DataFrame, set[str]]:
    p = cache_path(data_dir, d)
    try:
        df = pl.read_parquet(p)
    except Exception:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "t": pl.Utf8, **{c: pl.Float64 for c in COLS}}), set()
    meta = p.with_suffix(".symbols.json")
    try:
        req = set(json.loads(meta.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        req = set(df["symbol"].unique().to_list())
    return df, req


def _write_cache(data_dir: Path, d: date, df: pl.DataFrame, requested: set[str]) -> None:
    from app.services.fs_utils import atomic_write_parquet, atomic_write_text
    p = cache_path(data_dir, d)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(df.unique(subset=["symbol", "t"], keep="last").sort(["symbol", "t"]), p)
    atomic_write_text(p.with_suffix(".symbols.json"), json.dumps(sorted(requested)))
    # 只保留最近 HIST_FETCH_DAYS + 10 个缓存日
    files = sorted(p.parent.glob("20*.parquet"))
    for old in files[: max(0, len(files) - (HIST_FETCH_DAYS + 10))]:
        for f in (old, old.with_suffix(".symbols.json")):
            with contextlib.suppress(OSError):
                f.unlink()


def ensure_history(data_dir: Path, symbols: list[str], today: date, *, fetch=None,
                   force: set[str] | None = None) -> dict[date, pl.DataFrame]:
    """今天之前 HIST_FETCH_DAYS 个交易日的 5 分钟K: 缓存缺的 (日或票) 补齐写缓存。返回 {日: 帧}。
    按“缺的日子区间”给票分组各拉各的 (新加自选的票只拉它自己 30 天, 不连带全部票重拉)。
    force = 复权口径变了的票 (拆股): 这些票所有日子的旧行先删再重拉。冷启动 (30 天 x 45 只) 生产实测 232 秒 / 102 次请求,
    之后每天只补前一天 (约 5 秒)。"""
    from app.market_clock import us_clock
    fetch = fetch or fetch_bars
    force = set(force or ())
    days = us_clock.trading_days_between(today - timedelta(days=HIST_FETCH_DAYS * 2 + 10), today - timedelta(days=1))
    days = days[-HIST_FETCH_DAYS:]
    have: dict[date, tuple[pl.DataFrame, set[str]]] = {d: _read_cache(data_dir, d) for d in days}
    groups: dict[tuple[date, date], list[str]] = {}
    for s in symbols:
        miss = [d for d in days if s in force or s not in have[d][1]]
        if miss:
            groups.setdefault((miss[0], miss[-1]), []).append(s)
    for (d0, d1), ss in sorted(groups.items()):
        fr = _slot_frame(fetch(ss, d0, d1))
        sset = set(ss)
        for d in days:
            if not (d0 <= d <= d1):
                continue
            part = fr.filter(pl.col("d") == d).select(["symbol", "t", *COLS])
            old, req = have[d]
            old = old.select(["symbol", "t", *COLS])
            drop = sorted(sset & force)
            if drop:
                old = old.filter(~pl.col("symbol").is_in(drop))
            merged = pl.concat([old, part], how="vertical_relaxed") if not old.is_empty() else part
            req2 = req | sset
            _write_cache(data_dir, d, merged, req2)
            have[d] = (_read_back(merged), req2)
    return {d: have[d][0] for d in days}


def _read_back(df: pl.DataFrame) -> pl.DataFrame:
    """与 _write_cache 同口径去重 (同票同根留最后一条), 返回给本轮使用。"""
    return df.unique(subset=["symbol", "t"], keep="last").sort(["symbol", "t"])


def adj_drift(data_dir: Path, ref_day: date, fresh_arr: dict) -> set[str]:
    """缓存里某天的收盘 vs 现拉同一天的收盘 (同一复权口径应一致): 差 > 1% = 期间拆并股, 这些票历史缓存重拉。"""
    df, _req = _read_cache(data_dir, ref_day)
    if df.is_empty():
        return set()
    cached = to_arrays(df)
    out = set()
    for (s, d), a in fresh_arr.items():
        if d != ref_day or (s, d) not in cached:
            continue
        c0, c1 = last_close(cached[(s, d)]), last_close(a)
        if c0 and c1 and abs(c1 / c0 - 1) > ADJ_DRIFT:
            out.add(s)
    return out


def _live_prices(symbols: list[str], *, quote=None) -> dict[str, float]:
    """出卡那一刻的实时价: 5 秒热报价 (<= 2 分钟) -> Alpaca IEX 快照最新成交 (<= 2 分钟)。都没有 = 不出卡。
    (不用 get_execution_quotes: 它还会再拉 09:30 开盘分钟根, 这里用不到, 白白多两次出网。)"""
    import time as _t
    out: dict[str, float] = {}
    if quote is not None:
        return {s: float(v) for s, v in quote(symbols).items() if isinstance(v, (int, float)) and v > 0}
    try:
        from app.api.portfolio import _live_marks
        now_ms = _t.time() * 1000
        for s, v in _live_marks(list(symbols)).items():
            ts = v[1] if len(v) > 1 else None
            if isinstance(ts, (int, float)) and 0 <= now_ms - ts <= LIVE_MAX_AGE_S * 1000 and v[0] > 0:
                out[s] = float(v[0])
    except Exception as e:
        logger.debug("intraday strength: hot quotes unavailable: %s", type(e).__name__)
    rest = [s for s in symbols if s not in out]
    if rest:
        try:
            from app.plugins.alpaca import provider as ap
            p = ap.AlpacaProvider()
            snaps = p._get_snapshots(rest, "iex") if p.key and p.secret else {}
            now_ms = _t.time() * 1000
            for s in rest:
                tr = (snaps.get(s) or {}).get("latestTrade") or {} if isinstance(snaps.get(s), dict) else {}
                px, ts = ap._positive_number(tr.get("p")), ap._rfc3339_ms(tr.get("t"))
                if px is not None and ts is not None and 0 <= now_ms - ts <= LIVE_MAX_AGE_S * 1000:
                    out[s] = float(px)
        except Exception as e:
            logger.warning("intraday strength: iex quotes unavailable: %s", type(e).__name__)
    return out


def _watchlist() -> list[str]:
    from app.services import decision_scan_job as job
    return sorted(s for s in job._watchlist() if s not in EXCLUDE)


def checkpoint_of(now: datetime) -> time | None:
    """now (美东) 对应的检查点 (晚到 <= 10 分钟); 不在任何检查点窗口 -> None。"""
    for c in CHECKS:
        t0 = datetime.combine(now.date(), c, tzinfo=now.tzinfo)
        if 0 <= (now - t0).total_seconds() <= MAX_LATE_S:
            return c
    return None


# ── 主流程 ───────────────────────────────────────────────
LOCK_WAIT_S = 240                # 0.34.0 定时任务拿锁最多等 4 分钟: 开盘放量榜 (09:51/09:56, 同一把锁) 取数慢时不让 10:00 检查点整轮跳过


def _acquire(data_dir: Path, wait: bool, wait_s: float = 0):
    """跨进程文件锁 (定时任务在应用进程里, 补算用 docker exec 另起进程, 两边写同一批文件)。拿不到 -> None。
    wait=True 一直等; 否则最多等 wait_s 秒 (每秒试一次), 0 = 不等。"""
    import fcntl
    import time as _t
    d = root(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    fh = (d / ".lock").open("a+")
    deadline = _t.monotonic() + max(0.0, float(wait_s))
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            return fh
        except OSError:
            if wait or _t.monotonic() >= deadline:
                fh.close()
                return None
            _t.sleep(min(1.0, max(0.0, deadline - _t.monotonic())))


def _release(fh) -> None:
    import fcntl
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def run(data_dir: Path, now: datetime | None = None, *, symbols: list[str] | None = None, fetch=None,
        quote=None, settle_only: bool = False, backfill: bool = False, wait: bool = False, wait_s: float = 0) -> dict:
    """定时任务入口: 检查点出卡 + 结算在途卡。不碰台账、不下单。backfill=True: 上线前用同一代码补算, 卡带 backfill 标记
    (不进自然战绩)。wait=False (定时任务): 锁被占着就跳过; 补算用 wait=True 排队。取数失败不抛 (记进当天记录)。"""
    from app.market_clock import us_clock
    now = us_clock.market_datetime(now or us_clock.now())
    today = now.date()
    if not us_clock.is_trading_day(today):
        return {"skipped": "not_trading_day"}
    fh = _acquire(Path(data_dir), wait, wait_s)
    if fh is None:
        return {"skipped": "busy"}
    try:
        return _run_locked(Path(data_dir), now, today, symbols=symbols, fetch=fetch or fetch_bars, quote=quote,
                           settle_only=settle_only, backfill=backfill)
    finally:
        _release(fh)


def _run_locked(data_dir: Path, now: datetime, today: date, *, symbols, fetch, quote, settle_only: bool,
                backfill: bool = False) -> dict:
    from app.market_clock import us_clock
    syms = sorted(s for s in set(symbols if symbols is not None else _watchlist()) - EXCLUDE if _SYM_RE.fullmatch(s))
    close_t = us_clock.session_close(today)
    session_end = datetime.combine(today, close_t, tzinfo=us_clock.tz)
    after_close = now >= session_end + timedelta(minutes=17)        # 当天 5 分钟K 已齐 (SIP 扣留 15 分钟 + 余量)
    out: dict[str, Any] = {"day": today.isoformat(), "fired": [], "settled": []}
    open_cards = _open_cards(data_dir, today)
    check = None if settle_only else checkpoint_of(now)
    if check is not None and check >= close_t:
        check = None
    if check is None and not open_cards and not after_close:
        return {**out, "skipped": "nothing_to_do"}          # 不在检查点、没有在途卡、也不是收盘后落缓存: 不出网
    need = sorted(set(syms) | {c["symbol"] for _, c in open_cards})
    if not need:
        return {**out, "skipped": "no_symbols"}
    until = min(now - timedelta(minutes=16), session_end)
    try:
        drift = _drift_once(data_dir, today, need, fetch)
        hist = ensure_history(data_dir, need, today, fetch=fetch, force=drift)
        if drift:
            mark_drift_done(data_dir, today)
        today_df = fetch(need, today, today, until=until)
    except Exception as e:
        # 只记录的功能: 取数失败不抛给调度器 (不推飞书), 记进当天记录, 状态页标黄; 下一个检查点自然重试
        logger.warning("intraday strength: fetch failed: %s", type(e).__name__)
        _note_error(data_dir, today, now, check, e)
        return {**out, "error": type(e).__name__}
    _note_ok(data_dir, today, now)
    today_arr = {k: v for k, v in to_arrays(today_df).items() if k[1] == today}
    cut = max(0, min(SLOTS, ((until.hour * 60 + until.minute) - (OPEN.hour * 60 + OPEN.minute)) // BAR_MIN))
    for a in today_arr.values():                # 还没走完的那根 (SIP 右端之后) 不进结算
        for c in COLS:
            a[c][cut:] = np.nan
    if after_close and today_arr:
        _write_cache(data_dir, today, _slot_frame(today_df).filter(pl.col("d") == today).select(["symbol", "t", *COLS]),
                     set(need))
    hist_arr: dict[date, dict] = {d: to_arrays(df) for d, df in hist.items()}
    days = sorted(hist_arr)
    if check is not None:
        out["check"] = check.strftime("%H:%M")
        out["fired"] = _fire(data_dir, today, now, check, syms, today_arr, hist_arr, days, quote, backfill)
    out["settled"] = _settle_all(data_dir, today, open_cards, today_arr, hist_arr, after_close)
    return out


_SYM_RE = re.compile(r"[A-Z][A-Z0-9.]{0,9}")   # 美股代码 (BRK.B 之类); 非美股代码进 Alpaca 会让整批 400
_DRIFT_DONE: dict[str, date] = {}


def _drift_once(data_dir: Path, today: date, symbols: list[str], fetch) -> set[str]:
    """每个交易日查一次复权口径: 现拉“最近一个有缓存的交易日”, 与缓存比收盘, 差 > 1% 的票 (期间有拆并股)
    历史缓存全部重拉。没有任何缓存 (冷启动) 不用查。约 5 秒 / 天。"""
    from app.market_clock import us_clock
    key = str(data_dir)
    if _DRIFT_DONE.get(key) == today:
        return set()
    prev = us_clock.trading_days_between(today - timedelta(days=HIST_FETCH_DAYS * 2 + 10), today - timedelta(days=1))
    ref = next((d for d in reversed(prev[-HIST_FETCH_DAYS:]) if cache_path(data_dir, d).exists()), None)
    drift: set[str] = set()
    if ref is not None:
        drift = adj_drift(data_dir, ref, to_arrays(fetch(symbols, ref, ref)))
        if drift:
            logger.warning("intraday strength: adjustment drift (split?) -> refetch history: %s", sorted(drift))
    if not drift:
        _DRIFT_DONE[key] = today      # 没有要重拉的: 今天查完了
    # 有要重拉的票: 等调用方 ensure_history 成功后 mark_drift_done 再标记; 中途取数失败, 下一个任务会重查 (0.34.1 审查)
    return drift


def mark_drift_done(data_dir: Path, today: date) -> None:
    _DRIFT_DONE[str(data_dir)] = today


def _note_ok(data_dir: Path, today: date, now: datetime) -> None:
    """0.35.3: 记最近一次取数成功时刻。只在当天之前有过取数失败时才写 (平常每个检查点 _fire 已写盘, 不多一次 IO)。"""
    try:
        rec = load_day(data_dir, today)
        if rec["errors"]:
            rec["ok_at"] = now.isoformat(timespec="seconds")
            _save_day(data_dir, rec)
    except Exception as e2:
        logger.warning("intraday strength: ok note failed: %s", type(e2).__name__)


def _note_error(data_dir: Path, today: date, now: datetime, check, e: Exception) -> None:
    try:
        rec = load_day(data_dir, today)
        rec["errors"] = ([*rec.get("errors", []), {"at": now.isoformat(timespec="seconds"),
                                                   "check": check.strftime("%H:%M") if check else None,
                                                   "error": type(e).__name__}])[-20:]
        _save_day(data_dir, rec)
    except Exception as e2:
        logger.warning("intraday strength: error note failed: %s", type(e2).__name__)


def _fire(data_dir, today, now, check, syms, today_arr, hist_arr, days, quote, backfill=False) -> list[str]:
    rec = load_day(data_dir, today)
    have = {c["symbol"] for c in rec["cards"]}
    cand = []
    feats: dict[str, dict] = {}
    for s in syms:
        if (s, today) not in today_arr:
            continue
        try:
            history = [hist_arr[d].get((s, d)) for d in days]
            prev = history[-1] if history else None
            n = completed_slots(check)
            if not eligible(history) or prev is None:
                continue
            f = features(today_arr[(s, today)], n, last_close(prev), base_cums(history, n))
        except Exception as e:
            if s not in have:
                raise                            # 出卡候选: 与 0.33.0 一样直接失败 (外层记错误)
            logger.warning("intraday strength: heat features %s skipped: %s", s, type(e).__name__)
            continue                             # 已出过卡的票只用于放量前 5: 坏了只少这一行
        if f:
            feats[s] = f
        if s not in have and passes(f):
            cand.append(s)
    fired = []
    live = _live_prices(cand, quote=quote) if cand else {}
    # 出卡时刻 = 拿到实时价的时刻 (生产: 取完数和报价之后的真实时钟; 注入报价的测试/补算: 检查点时刻)。
    # 晚于检查点 10 分钟 = 价已不是那一刻, 本检查点不出卡 (只记一条 late)。
    from app.market_clock import us_clock
    fire_now = now if quote is not None else us_clock.market_datetime(us_clock.now())
    late = (fire_now - datetime.combine(today, check, tzinfo=us_clock.tz)).total_seconds() > MAX_LATE_S
    mins = (fire_now.hour * 60 + fire_now.minute) - (OPEN.hour * 60 + OPEN.minute)
    slot = max(0, min(SLOTS - 1, mins // BAR_MIN))
    bar_start = datetime.combine(today, OPEN, tzinfo=us_clock.tz) + timedelta(minutes=slot * BAR_MIN)
    partial = (fire_now - bar_start).total_seconds() > PARTIAL_S
    near = []
    for s in [] if late else cand:
        history = [hist_arr[d].get((s, d)) for d in days]
        res = evaluate(s, today_arr[(s, today)], history, history[-1], check, live.get(s))
        if res is None:
            try:                                 # 只展示: 坏了只少这一行, 不拖累出卡与落盘
                near.append(_near_row(s, feats[s], live.get(s)))
            except Exception as e:
                logger.warning("intraday strength: near row %s skipped: %s", s, type(e).__name__)
            continue
        rec["cards"].append({"contract": CONTRACT, "card_id": card_id(s, today), "symbol": s, "side": "long",
                             "day": today.isoformat(), "check": check.strftime("%H:%M"),
                             "fired_at": fire_now.isoformat(timespec="seconds"), "slot": int(slot),
                             **({"slot_partial": True} if partial else {}), **res,
                             "hold_days": HOLD_DAYS, "status": "open", **({"backfill": True} if backfill else {})})
        fired.append(s)
    try:
        heat = heat_rows(feats)
    except Exception as e:                       # 同上: 放量前 5 算坏了 = 空, 卡照常落盘
        logger.warning("intraday strength: heat rows skipped: %s", type(e).__name__)
        heat = []
    rec["checks"].append({"at": fire_now.isoformat(timespec="seconds"), "check": check.strftime("%H:%M"),
                          "symbols": len(syms), "candidates": len(cand), "fired": fired,
                          "heat": heat, "near": near,
                          **({"late": True} if late else {}), **({"backfill": True} if backfill else {})})
    _save_day(data_dir, rec)
    return fired


HEAT_N = 5


def _r4(x) -> float | None:
    return round(float(x), 4) if isinstance(x, (int, float)) and math.isfinite(x) else None


def heat_rows(feats: dict[str, dict], n: int = HEAT_N) -> list[dict]:
    """0.34.0 放量前 N (只展示): 到检查点的累计量比 (与出卡同一个量比) 从高到低, 不分涨跌。
    回放 (自选 68 只, 留出 2025-08~): 10:30 前 5 当天收盘涨 >= 5% 的比例 29% vs 其余 9%; 从检查点到收盘平均 ~0%, 照着追没有优势。"""
    rows = [{"symbol": s, "rvol": _r4(f.get("rvol")), "chg": _r4(f.get("chg")), "price": _r4(f.get("P")),
             "vwap": _r4(f.get("vwap"))} for s, f in feats.items() if _r4(f.get("rvol")) is not None]
    rows.sort(key=lambda r: (-r["rvol"], r["symbol"]))
    return rows[:n]


def _near_row(s: str, f: dict, live: float | None) -> dict:
    """过了前 6 道门、没出卡的票 (只展示): 为什么差一步。"""
    if live is None:
        why = "no_live"
    elif not (live > f["vwap"]):
        why = "below_vwap"
    else:
        why = "geometry"
    return {"symbol": s, "why": why, "chg": _r4(f.get("chg")), "rvol": _r4(f.get("rvol")), "er": _r4(f.get("er")),
            "vwap": _r4(f.get("vwap")), "live": _r4(live)}


def backfill_days(data_dir: Path, days: list[date], *, symbols: list[str] | None = None, fetch=None) -> dict:
    """上线前补算最近几个已收盘交易日 (同一代码; 出卡价 = 检查点那一根 5 分钟K 开盘价, 回放同口径)。
    只写本页自己的记录文件, 卡带 backfill 标记; 已有记录的日子跳过 (不覆盖)。排队等锁 (不和定时任务抢)。
    当天的 5 分钟K 每个补算日只拉一次, 各检查点按“当时能看到的”截断 (不偷看)。"""
    from app.market_clock import us_clock
    fetch = fetch or fetch_bars
    syms = sorted(s for s in set(symbols if symbols is not None else _watchlist()) - EXCLUDE if _SYM_RE.fullmatch(s))
    out = {}
    for d in sorted(days):
        if d > us_clock.today() or (d == us_clock.today() and us_clock.now() < datetime.combine(
                d, us_clock.session_close(d), tzinfo=us_clock.tz) + timedelta(minutes=17)):
            out[d.isoformat()] = "not_closed"       # 当天 5 分钟K 还没齐 (SIP 扣留 15 分钟), 补了会把不完整的一天写进缓存
            continue
        if load_day(data_dir, d)["checks"]:
            out[d.isoformat()] = "exists"
            continue
        full: dict[tuple, pl.DataFrame] = {}

        def memo(ss, start, end, until=None, d=d, full=full):
            if start != d or end != d:
                return fetch(ss, start, end, until=until) if until is not None else fetch(ss, start, end)
            key = tuple(sorted(ss))
            if key not in full:
                full[key] = fetch(list(ss), d, d)
            df = full[key]
            if until is None or df.is_empty():
                return df
            t = pl.col("t").str.to_datetime(time_zone="UTC")
            return df.filter(t < pl.lit(until.astimezone(_UTC)))
        arr = to_arrays(memo(syms, d, d))
        n = 0
        for c in CHECKS:
            k = ((c.hour * 60 + c.minute) - (OPEN.hour * 60 + OPEN.minute)) // BAR_MIN
            px = {s: float(arr[(s, d)]["o"][k]) for s in syms if (s, d) in arr and not math.isnan(arr[(s, d)]["o"][k])}
            res = run(data_dir, datetime.combine(d, c, tzinfo=us_clock.tz), symbols=syms, fetch=memo,
                      quote=lambda ss, px=px: {s: px[s] for s in ss if s in px}, backfill=True, wait=True)
            n += len(res.get("fired") or [])
        run(data_dir, datetime.combine(d, time(16, 25), tzinfo=us_clock.tz), symbols=syms, fetch=memo, backfill=True,
            wait=True)
        out[d.isoformat()] = n
    return out


def _open_cards(data_dir: Path, today: date) -> list[tuple[date, dict]]:
    out = []
    for d in record_days(data_dir):
        if d > today or (today - d).days > HOLD_DAYS * 3 + 7:
            continue
        for c in load_day(data_dir, d)["cards"]:
            if c.get("status") == "open":
                out.append((d, c))
    return out


def _settle_all(data_dir: Path, today: date, open_cards, today_arr, hist_arr, day_complete: bool) -> list[str]:
    from app.market_clock import us_clock
    done: dict[date, dict[str, dict]] = {}
    # 今天出的卡也要结算 (_fire 刚写入)
    seen = {x["card_id"] for _, x in open_cards}
    cards = open_cards + [(today, c) for c in load_day(data_dir, today)["cards"]
                          if c.get("status") == "open" and c["card_id"] not in seen]
    for d, c in cards:
        try:
            sess = us_clock.trading_days_between(d, today)[:HOLD_DAYS]
            bars = []
            for sd in sess:
                a = today_arr.get((c["symbol"], sd)) if sd == today else (hist_arr.get(sd) or {}).get((c["symbol"], sd))
                if a is not None:               # 过去某天没数据 (停牌/缓存缺) -> 跳过那天; 今天没数据 -> 只看过去
                    bars.append((sd, a))
            over = len(sess) >= HOLD_DAYS and (sess[-1] < today or day_complete)
            res = settle(c, bars, window_over=over)
        except (KeyError, TypeError, ValueError) as e:   # 单张坏卡不拖垮整轮
            logger.warning("intraday strength: settle skipped %s: %s", c.get("card_id"), type(e).__name__)
            continue
        if res is not None:
            done.setdefault(d, {})[c["card_id"]] = res
    settled = []
    for d, upd in done.items():
        rec = load_day(data_dir, d)
        for c in rec["cards"]:
            r = upd.get(c.get("card_id"))
            if r is not None and c.get("status") == "open":
                c.update(r, status="void" if r.get("exit_reason") in ("no_data", "corp_action_suspect") else "closed")
                settled.append(c["card_id"])
        _save_day(data_dir, rec)
    return settled


# ── 机会板接口 (只读, 不出网) ───────────────────────────────
def build_live(data_dir: Path, *, repo=None, loop=None, now: datetime | None = None) -> dict:
    from app.market_clock import us_clock
    now = us_clock.market_datetime(now or us_clock.now())
    today = now.date()
    active, history, closed = [], [], []
    for d in sorted(record_days(data_dir), reverse=True)[:60]:
        for c in load_day(data_dir, d)["cards"]:
            if c.get("status") == "open":
                active.append(dict(c))
            else:
                history.append(dict(c))
                if not c.get("backfill") and isinstance(c.get("r"), (int, float)):
                    closed.append(float(c["r"]))
    # 现价 + 当前 R (热报价 <= 2 分钟; 否则最近收盘)
    syms = {c["symbol"] for c in active}
    px: dict[str, tuple[float, str]] = {}
    if syms:
        try:
            from app.services import swing_desk as sd
            px = sd._live_prices(repo, syms, today, now)
            rest = syms - set(px)
            for s, (p, _asof) in sd._last_closes(repo, rest, today).items():
                px[s] = (p, "close")
        except Exception as e:
            logger.warning("intraday strength: prices unavailable: %s", type(e).__name__)
    eq = rk = None
    try:
        from app.services import desk_overview as ov
        from app.services import swing_desk as sd
        lg, _ = ov._account(loop, data_dir)
        eq, rk = sd._equity_and_risk(data_dir, lg)
    except Exception as e:
        logger.warning("intraday strength: equity unavailable: %s", type(e).__name__)
    for c in active:
        p = px.get(c["symbol"])
        try:
            if p:
                c["price"], c["price_source"] = round(float(p[0]), 4), p[1]
                c["r_now"] = round((float(p[0]) * (1 - SLIP) - float(c["entry"])) / float(c["risk"]), 2) if c.get("risk") else None
            c["day_n"] = len(us_clock.trading_days_between(date.fromisoformat(c["day"]), today)) or 1
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            c["r_now"] = None
        c["odds"] = odds_usd(c, eq, rk)
    for c in history:
        c["odds"] = None
    active.sort(key=lambda c: c.get("fired_at") or "", reverse=True)
    history.sort(key=lambda c: (c.get("exit_at") or c.get("fired_at") or ""), reverse=True)
    rec = load_day(data_dir, today)
    stats = None
    if closed:
        stats = {"n": len(closed), "win": round(sum(r > 0 for r in closed) / len(closed), 3),
                 "mean_r": round(sum(closed) / len(closed), 3), "sum_r": round(sum(closed), 2)}
    nxt = next((c.strftime("%H:%M") for c in CHECKS
                if datetime.combine(today, c, tzinfo=us_clock.tz) > now and us_clock.is_trading_day(today)), None)
    return {"contract": CONTRACT, "today": today.isoformat(), "now": now.isoformat(timespec="seconds"),
            "active": active, "history": history[:HISTORY_MAX], "stats": stats, "replay": ODDS,
            "checks_today": rec["checks"][-12:], "next_check": nxt,
            "rule": {"min_chg": MIN_CHG, "min_rvol": MIN_RVOL, "min_er": MIN_ER, "min_above": MIN_ABOVE,
                     "max_pull": MAX_PULL, "hold_days": HOLD_DAYS, "target_r": TARGET_R}}


def odds_usd(card: dict, equity, risk_pct) -> dict:
    """回放留出段每笔期望 -> 按本卡计划股数折美元 (股数 = 权益 x 单笔风险 / 每股风险, 名义 <= 权益 25%)。"""
    out = dict(ODDS)
    try:
        rps, entry = float(card["risk"]), float(card["entry"])
        if equity and risk_pct and rps > 0 and entry > 0:
            qty = int(min(float(equity) * float(risk_pct) / rps, float(equity) * NOTIONAL_CAP / entry))
            if qty > 0:
                usd = qty * rps
                out.update(qty=qty, risk_usd=round(usd), ev_usd=round(ODDS["mean_r"] * usd),
                           ci_usd=[round(ODDS["ci_r"][0] * usd), round(ODDS["ci_r"][1] * usd)])
    except (KeyError, TypeError, ValueError):
        pass
    return out


def status(data_dir: Path) -> dict:
    """状态页一行: 今天检查了几次、出了几张、在途几张、最近一次检查、今天取数失败几次。"""
    from app.market_clock import us_clock
    today = us_clock.today()
    rec = load_day(data_dir, today)
    open_n = len(_open_cards(data_dir, today))
    last = rec["checks"][-1] if rec["checks"] else None
    err = rec["errors"][-1] if rec["errors"] else None
    # 0.35.3: 之后有一次取数成功 (下一个检查点或 16:25 结算) = 已恢复, 不再标黄; 只在摘要里留痕
    ok_at = rec.get("ok_at")
    unresolved = sum(1 for e in rec["errors"] if not ok_at or str(e.get("at") or "") > ok_at)
    return {"day": today.isoformat(), "checks": len(rec["checks"]), "fired": len(rec["cards"]), "open": open_n,
            "last_at": last.get("at") if last else None, "errors": len(rec["errors"]), "unresolved": unresolved,
            "recovered_at": ok_at if rec["errors"] and not unresolved else None,
            "last_error": f"{str(err.get('at') or '')[11:16]} {err.get('error')}" if err else None}
