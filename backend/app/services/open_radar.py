# ruff: noqa: RUF001
"""开盘放量榜 (0.34.0, 无限 10-22 选「机会页按时段换主角」): 开盘 5 分钟按“比平时放量多少”给自选排名, 只展示不下单。

规则 (预注册 frozen offline replay specification, 与回放 orb.py 同式):
  开盘区间 = 09:30 那根 5 分钟K (SIP, adjustment=split)。
  相对量 = 这一根的量 / 前 14 个有效日同一根的均量 (有效日 = 当天 >= 30 根 5 分钟K, 在前 30 个交易日内取, 至少 10 天有值)。
  资格: 开盘价 > $5, 前 14 个有效日均日成交量 >= 100 万股, 前 14 个有效日 ATR >= $0.50, 昨天是有效日。
  点名 = 开盘 5 分钟收阳 (收 > 开) 且相对量 >= 1, 按相对量从高到低前 3 只。
回放 (自选 68 只, 2023-01~2026-10, 切点 2025-08-25):
  点名票当天收盘涨 >= 5% 的比例 训练 21.0% / 留出 31.9%, 其余开盘收阳的票 5.9% / 13.3%;
  非自选 80 只 16.3% / 20.3% vs 3.4% / 5.3% (不是自选名单溢价)。
  点名票从 09:35 到收盘平均只再涨 0.2%, 一半收在 09:35 下方: 照着追没有优势, 所以只点名、不给价位、不下单。
时点: SIP 免费档扣留 15 分钟, 09:30 那根 09:50 后才拿得到 -> 09:51 ET 出榜 (北京夏令时 21:51 / 冬令时 22:51), 09:56 再试一次。
  收盘后 16:40 ET 记每只票当天收盘 (从盘中强势的 5 分钟K 缓存读, 缺才出网), 供页面战绩与失败判据。
数据: 复用盘中强势的 30 日 5 分钟K 缓存 (data/opportunities/intraday/m5/), 同一把跨进程文件锁。
记录: data/opportunities/radar/<美东日>.json (原子整写)。不写台账、不进交易环、不进下单簿。
推送 (0.35.0, 无限 10-22「开盘榜推送到飞书, 开关我手动决定」): 出榜后把相对量 >= 门槛的点名票合成一条提醒。
  开关与门槛在机会页齿轮 (data/opportunities/radar_push.json; 缺 = 开 / >= 3x); 渠道跟随设置页全局勾选。
  每个美东日每渠道最多一条 (outbox 键 open_radar:<日>:<渠道>),
  没有票达门槛不推; 10:30 ET 后不推 (重启补跑出榜若已过 10:30 也不推)。只提醒, 不触发任何交易动作。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.services import intraday_strength as ids

logger = logging.getLogger(__name__)

CONTRACT = "open-radar-v1"
LOOKBACK_VALID = 14
MIN_BASE_DAYS = 10
MIN_PRICE = 5.0
MIN_ADV = 1_000_000.0
MIN_ATR = 0.5
MIN_RVOL = 1.0
TOP_N = 3
LOCK_WAIT_S = 60                # 与盘中强势共用一把锁; 补算占着时最多等 1 分钟
READY_AT = time(9, 51)          # 09:30 那根 5 分钟K 在免费 SIP 上 09:50 后才完整可见
STATS_MAX_DAYS = 120
# 回放成绩 (只展示): 点名票收盘涨 >= 5% 的比例 vs 其余开盘收阳的票
REPLAY = {"period": "2025-08~2026-10", "hit": 0.319, "other": 0.133, "ctl_hit": 0.203, "ctl_other": 0.053,
          "after_0935_mean_pct": 0.26}
# 推送门槛回放 (留出段 2025-08~2026-10, 290 个交易日; 与页面同口径 = 点名前 3 里相对量 >= 门槛的票;
# offline push-frequency replay): 有推送的交易日占比 / 日均几只 / 推到的票当天收涨 >= 5%
PUSH_LEVELS = (1.0, 2.0, 3.0, 5.0)
PUSH_DEFAULT = {"enabled": True, "min_rvol": 3.0}
PUSH_DEADLINE = time(10, 30)    # 过了 10:30 再推“开盘放量”已不是开盘 (与 ops_watch 补跑截止同一刻)
PUSH_REPLAY = {
    1.0: {"days": 0.986, "per_day": 2.82, "up5": 0.334},
    2.0: {"days": 0.672, "per_day": 1.30, "up5": 0.448},
    3.0: {"days": 0.390, "per_day": 0.58, "up5": 0.500},
    5.0: {"days": 0.155, "per_day": 0.19, "up5": 0.593},
}


def root(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "radar"


def day_path(data_dir: Path, d: date) -> Path:
    return root(data_dir) / f"{d.isoformat()}.json"


def load_day(data_dir: Path, d: date) -> dict | None:
    try:
        raw = json.loads(day_path(data_dir, d).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("rows"), list):
        return None
    raw["rows"] = [r for r in raw["rows"] if isinstance(r, dict) and isinstance(r.get("symbol"), str)]
    return raw


def _save_day(data_dir: Path, rec: dict) -> None:
    from app.services.fs_utils import atomic_write_text
    p = day_path(data_dir, date.fromisoformat(rec["day"]))
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(rec, ensure_ascii=False, allow_nan=False))


def record_days(data_dir: Path) -> list[date]:
    d = root(data_dir)
    out = []
    for f in d.glob("20*.json") if d.exists() else []:
        try:
            out.append(date.fromisoformat(f.stem))
        except ValueError:
            continue
    return sorted(out)


# ── 纯函数 ───────────────────────────────────────────────
def _fin(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _daily(a: dict) -> dict | None:
    c = a["c"][~np.isnan(a["c"])]
    if not len(c):
        return None
    h, lo = a["h"][~np.isnan(a["h"])], a["l"][~np.isnan(a["l"])]
    return {"h": float(h.max()) if len(h) else math.nan, "l": float(lo.min()) if len(lo) else math.nan, "c": float(c[-1]),
            "v": float(np.nansum(a["v"])), "or_v": float(a["v"][0]) if not math.isnan(a["v"][0]) else math.nan}


def _prev_valid_close(hist: list[dict | None], j: int) -> float | None:
    """hist[j] 之前 (最多回看 5 天) 最近一个有效日的收盘。
    只在 30 个交易日的缓存窗口里找: 有效日稀疏 (新股 / 长期停牌) 时窗口最前面那天找不到前收, 少算一个真实波幅;
    回放 orb.py 在全量日历上找。正常票 30 天全是有效日, 两边一致 (10-20 / 10-21 生产副本对拍逐票相同)。"""
    for k in range(j - 1, max(-1, j - 6), -1):
        if ids.hist_valid(hist[k]):
            return ids.last_close(hist[k])
    return None


def evaluate(today: dict, hist: list[dict | None]) -> dict | None:
    """单票: 今天的数组 (至少有 09:30 那根) + 今天之前的交易日数组 (旧 -> 新, 缺 = None, 最多 30 天) -> 一行; 不够资格 -> None。"""
    o0, h0, l0, c0, v0 = (today[x][0] for x in ("o", "h", "l", "c", "v"))
    if any(math.isnan(x) for x in (o0, h0, l0, c0, v0)) or not o0 > MIN_PRICE:
        return None
    if not hist or not ids.hist_valid(hist[-1]):
        return None
    valid = [j for j in range(len(hist)) if ids.hist_valid(hist[j])][-LOOKBACK_VALID:]
    if len(valid) < LOOKBACK_VALID:
        return None
    days = [_daily(hist[j]) for j in valid]
    trs = []
    for j, x in zip(valid, days, strict=True):
        pc = _prev_valid_close(hist, j)
        if pc is None or x is None:
            continue
        trs.append(max(x["h"] - x["l"], abs(x["h"] - pc), abs(x["l"] - pc)))
    orv = [x["or_v"] for x in days if x and not math.isnan(x["or_v"])]
    if len(trs) < MIN_BASE_DAYS or len(orv) < MIN_BASE_DAYS or not np.mean(orv) > 0:
        return None
    adv, atr = float(np.mean([x["v"] for x in days if x])), float(np.mean(trs))
    if adv < MIN_ADV or atr < MIN_ATR:
        return None
    pc = ids.last_close(hist[-1])
    if not pc:
        return None
    rv = v0 / float(np.mean(orv))
    out = {"rvol": round(rv, 3), "rvol_raw": rv, "dir": 1 if c0 > o0 else (-1 if c0 < o0 else 0),
            "open": round(o0, 4), "or_high": round(h0, 4), "or_low": round(l0, 4), "or_close": round(c0, 4),
            "prev_close": round(pc, 4), "gap_pct": round((o0 / pc - 1) * 100, 3), "or_chg_pct": round((c0 / pc - 1) * 100, 3),
            "atr": round(atr, 4)}
    # 3) 任一数非有限 (缺价 / 缺量) = 不给这一行: 落盘用 allow_nan=False, 一个 NaN 会让整份榜写不下去
    if not all(_fin(v) for k, v in out.items() if k != "dir") or not _fin(adv):
        return None
    return out


def rank(rows: dict[str, dict]) -> list[dict]:
    """全部有资格的票 -> 按相对量从高到低; 点名 = 收阳且相对量 >= 1 的前 3 (pick=True, rank 1..3)。"""
    raw = lambda r: r.get("rvol_raw", r["rvol"])  # noqa: E731
    out = sorted(({"symbol": s, **r} for s, r in rows.items()), key=lambda r: (-raw(r), r["symbol"]))
    n = 0
    for r in out:
        if r["dir"] == 1 and raw(r) >= MIN_RVOL and n < TOP_N:
            n += 1
            r["pick"], r["rank"] = True, n
        else:
            r["pick"] = False
    for r in out:
        r.pop("rvol_raw", None)                  # 只用来排序, 不落盘
    return out


# ── 编排 ─────────────────────────────────────────────────
def _scan(data_dir: Path, day: date, syms: list[str], fetch, *, until: datetime | None, drift: bool = False) -> dict[str, dict]:
    need = sorted(set(syms) | {c["symbol"] for _, c in ids._open_cards(data_dir, day)})
    # 与盘中强势同一套复权口径检查 (每个交易日一次, 谁先跑谁做): 拆并股的票历史缓存整段重拉
    force = ids._drift_once(data_dir, day, need, fetch) if drift else set()
    hist = ids.ensure_history(data_dir, need, day, fetch=fetch, force=force)
    if force:
        ids.mark_drift_done(data_dir, day)        # 重拉成功才算今天查完 (失败时盘中强势 10:00 会重查)
    hist_arr = {d: ids.to_arrays(df) for d, df in hist.items()}
    days = sorted(hist_arr)
    today_df = fetch(syms, day, day, until=until) if until is not None else fetch(syms, day, day)
    today_arr = {k: v for k, v in ids.to_arrays(today_df).items() if k[1] == day}
    rows = {}
    for s in syms:
        a = today_arr.get((s, day))
        if a is None:
            continue
        r = evaluate(a, [hist_arr[d].get((s, d)) for d in days])
        if r is not None:
            rows[s] = r
    return rows


def run(data_dir: Path, now: datetime | None = None, *, symbols: list[str] | None = None, fetch=None) -> dict:
    """09:51 / 09:56 ET 定时任务: 当天没出榜才算 (幂等)。取数失败不抛 (记进当天记录, 09:56 再试)。"""
    from app.market_clock import us_clock
    data_dir = Path(data_dir)
    now = us_clock.market_datetime(now or us_clock.now())
    day = now.date()
    if not us_clock.is_trading_day(day):
        return {"skipped": "not_trading_day"}
    if now.time() < READY_AT:
        return {"skipped": "too_early"}
    cur = load_day(data_dir, day)
    if cur and cur.get("rows"):
        return {"skipped": "exists", "picks": [r["symbol"] for r in cur["rows"] if r.get("pick")]}
    fh = ids._acquire(data_dir, wait=False, wait_s=LOCK_WAIT_S)
    if fh is None:
        return {"skipped": "busy"}
    try:
        syms = sorted(s for s in set(symbols if symbols is not None else ids._watchlist()) - ids.EXCLUDE
                      if ids._SYM_RE.fullmatch(s))
        if not syms:
            return {"skipped": "no_symbols"}
        until = min(now - timedelta(minutes=16), datetime.combine(day, us_clock.session_close(day), tzinfo=us_clock.tz))
        try:
            rows = _scan(data_dir, day, syms, fetch or ids.fetch_bars, until=until, drift=True)
        except Exception as e:
            logger.warning("open radar: fetch failed: %s", type(e).__name__)
            rec = cur or {"contract": CONTRACT, "day": day.isoformat(), "rows": []}
            rec["errors"] = [*(rec.get("errors") or []), {"at": now.isoformat(timespec="seconds"), "error": type(e).__name__}][-10:]
            _save_day(data_dir, rec)
            return {"error": type(e).__name__}
    finally:
        ids._release(fh)
    ranked = rank(rows)
    rec = {"contract": CONTRACT, "day": day.isoformat(), "at": now.isoformat(timespec="seconds"), "symbols": len(syms),
           "rows": ranked, "errors": (cur or {}).get("errors") or []}
    _save_day(data_dir, rec)
    picks = [r["symbol"] for r in ranked if r["pick"]]
    logger.info("open radar %s: %d eligible, picks %s", day, len(ranked), picks)
    return {"day": day.isoformat(), "eligible": len(ranked), "picks": picks}


def settle(data_dir: Path, now: datetime | None = None, *, fetch=None, day: date | None = None) -> dict:
    """收盘后记每行当天收盘 / 最高 (vs 昨收)。先读盘中强势写好的当天 5 分钟K 缓存, 缺的票才出网。幂等。"""
    from app.market_clock import us_clock
    data_dir = Path(data_dir)
    now = us_clock.market_datetime(now or us_clock.now())
    day = day or now.date()
    rec = load_day(data_dir, day)
    if not rec or not rec.get("rows"):
        return {"skipped": "no_record"}
    if day == now.date() and now < datetime.combine(day, us_clock.session_close(day), tzinfo=us_clock.tz) + timedelta(minutes=17):
        return {"skipped": "not_closed"}
    need = [r["symbol"] for r in rec["rows"] if not _fin(r.get("close"))]
    if not need:
        return {"skipped": "done"}
    df, _req = ids._read_cache(data_dir, day)
    arr = {k: v for k, v in ids.to_arrays(df).items() if k[1] == day}
    miss = [s for s in need if (s, day) not in arr]
    if miss:
        try:
            arr.update({k: v for k, v in ids.to_arrays((fetch or ids.fetch_bars)(miss, day, day)).items() if k[1] == day})
        except Exception as e:
            logger.warning("open radar settle: fetch failed: %s", type(e).__name__)
    n = 0
    for r in rec["rows"]:
        a = arr.get((r["symbol"], day))
        x = _daily(a) if a is not None else None
        pc = r.get("prev_close")
        if x is None or not _fin(pc) or not pc > 0:
            continue
        r["close"], r["high"] = round(x["c"], 4), round(x["h"], 4)
        r["chg_close_pct"] = round((x["c"] / pc - 1) * 100, 3)
        r["high_pct"] = round((x["h"] / pc - 1) * 100, 3)
        n += 1
    left = [r["symbol"] for r in rec["rows"] if not _fin(r.get("close"))]
    if left:
        # 还有票没记上收盘 (取数失败 / 缓存缺): 不标已结算, 记一条错误, 17:40 ET 那次再补 (只展示的功能: 不抛给调度器, 不推外部渠道)
        rec["errors"] = [*(rec.get("errors") or []), {"at": now.isoformat(timespec="seconds"), "error": "settle_missing",
                                                       "symbols": left[:10]}][-10:]
    else:
        rec["settled_at"] = now.isoformat(timespec="seconds")
    _save_day(data_dir, rec)
    return {"day": day.isoformat(), "settled": n, "missing": len(left)}


def backfill_days(data_dir: Path, days: list[date], *, symbols: list[str] | None = None, fetch=None) -> dict:
    """上线前用同一代码补算最近几个已收盘交易日 (行带 backfill, 不进上线后战绩)。已有记录的日子跳过。排队等锁。"""
    from app.market_clock import us_clock
    data_dir = Path(data_dir)
    fetch = fetch or ids.fetch_bars
    syms = sorted(s for s in set(symbols if symbols is not None else ids._watchlist()) - ids.EXCLUDE if ids._SYM_RE.fullmatch(s))
    out = {}
    for d in sorted(days):
        close_dt = datetime.combine(d, us_clock.session_close(d), tzinfo=us_clock.tz) + timedelta(minutes=17)
        if not us_clock.is_trading_day(d) or us_clock.now() < close_dt:
            out[d.isoformat()] = "not_closed"
            continue
        if load_day(data_dir, d):
            out[d.isoformat()] = "exists"
            continue
        fh = ids._acquire(data_dir, wait=True)
        try:
            cached, _ = ids._read_cache(data_dir, d)

            def fetch_day(ss, start, end, until=None, d=d, cached=cached):
                if start == end == d and not cached.is_empty() and set(ss) <= set(cached["symbol"].unique().to_list()):
                    return cached.filter(pl.col("symbol").is_in(list(ss)))
                return fetch(ss, start, end, until=until) if until is not None else fetch(ss, start, end)
            rows = _scan(data_dir, d, syms, fetch_day, until=None)
        finally:
            ids._release(fh)
        ranked = rank(rows)
        for r in ranked:
            r["backfill"] = True
        _save_day(data_dir, {"contract": CONTRACT, "day": d.isoformat(), "at": datetime.combine(d, READY_AT, tzinfo=us_clock.tz)
                             .isoformat(timespec="seconds"), "symbols": len(syms), "rows": ranked, "errors": [], "backfill": True})
        settle(data_dir, close_dt + timedelta(minutes=30), fetch=fetch, day=d)
        out[d.isoformat()] = [r["symbol"] for r in ranked if r["pick"]]
    return out


# ── 全局渠道推送 (0.35.0) ────────────────────────────────
def push_path(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "radar_push.json"


def push_settings(data_dir: Path) -> dict:
    """开关与门槛。文件缺 / 坏 / 值不在档位里 -> 默认 (开, >= 3x)。"""
    out = dict(PUSH_DEFAULT)
    try:
        raw = json.loads(push_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    th = raw.get("min_rvol")
    if isinstance(th, (int, float)) and not isinstance(th, bool) and float(th) in PUSH_LEVELS:
        out["min_rvol"] = float(th)
    return out


def save_push_settings(data_dir: Path, *, enabled: bool, min_rvol: float) -> dict:
    """机会页齿轮写口 (经 opp_page.save_settings)。调用方已校验档位。"""
    from app.services.fs_utils import atomic_write_text
    if not isinstance(enabled, bool) or float(min_rvol) not in PUSH_LEVELS:
        raise ValueError("radar push settings out of range")
    rec = {"enabled": enabled, "min_rvol": float(min_rvol),
           "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    p = push_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(rec, ensure_ascii=False))
    return {"enabled": rec["enabled"], "min_rvol": rec["min_rvol"]}


def push_options() -> dict:
    """齿轮用: 各档位的回放频率 (单一来源, 前端不写死数字)。"""
    return {"period": REPLAY["period"], "other": REPLAY["other"],
            "levels": [{"v": v, **PUSH_REPLAY[v]} for v in PUSH_LEVELS]}


_TIMING = {"bmo": "盘前", "amc": "盘后"}


def push_message(day: date, at: datetime, picks: list[dict], live: dict[str, dict], ctx: dict[str, dict], th: float) -> dict:
    rp = PUSH_REPLAY.get(th) or {}
    lines = [f"**{day.isoformat()} · 美东 {at:%H:%M} · 开盘 5 分钟放量 ≥{th:g}x**"]
    for r in picks:
        sym = r["symbol"]
        bits = [f"相对量 {r['rvol']:.1f}x", f"跳空 {r['gap_pct']:+.1f}%", f"开盘 5 分钟 {r['or_chg_pct']:+.1f}%"]
        lv = live.get(sym) or {}
        if _fin(lv.get("chg_now_pct")):
            bits.append(f"现在 {lv['chg_now_pct']:+.1f}%")
        c = ctx.get(sym) or {}
        e = c.get("earnings") or {}
        if e.get("date"):
            bits.append(f"财报 {str(e['date'])[5:]}{_TIMING.get(e.get('timing'), '')}")
        n = (c.get("news") or {}).get("count_24h")
        if n:
            bits.append(f"新闻 {n}")
        lines.append(f"- **{sym}** " + " · ".join(bits))
    if rp:
        lines.append(f"回放 {REPLAY['period']}：本档约 {rp['days']:.0%} 交易日有推送，推送票当日收涨 ≥5% 占 "
                     f"{rp['up5']:.0%}，其余开盘收阳 {REPLAY['other']:.0%}")
    lines.append(f"回放：点名票 09:35 后平均再涨 {REPLAY['after_0935_mean_pct']}%，追入无统计优势")
    lines.append("观察，不给价位、不下单；渠道：设置 → 监控；开关与门槛：机会页规则设置")
    return {"title": "ZenTrade · 开盘放量", "subtitle": day.isoformat(), "body": "\n".join(lines)}


def push(data_dir: Path, *, repo=None, now: datetime | None = None, enqueue=None) -> dict:
    """09:51 / 09:56 ET 出榜后: 开关开、有点名票达门槛、10:30 ET 前 -> 按全局渠道分别入队。
    幂等: 已入队的日子不再推 (outbox 按 日+渠道 去重); 没票达门槛只记一笔, 之后调低门槛, 下一次任务还会再看。不抛。"""
    from app.market_clock import us_clock
    data_dir = Path(data_dir)
    now = us_clock.market_datetime(now or us_clock.now())
    day = now.date()
    cfg = push_settings(data_dir)
    if not cfg["enabled"]:
        return {"skipped": "off"}
    if now.time() >= PUSH_DEADLINE:
        return {"skipped": "late"}
    rec = load_day(data_dir, day)
    if not rec or not rec.get("at") or rec.get("backfill"):
        return {"skipped": "no_board"}
    selected = None
    if enqueue is None:
        from app.services import notify_opportunity
        selected = notify_opportunity.preferences.get_webhook_default_channels()
        if not selected:
            return {"skipped": "no_channels"}
    previous_channels = []
    pending_channels = list(selected or [])
    if ((rec.get("push") or {}).get("symbols")):
        if enqueue is not None:
            return {"skipped": "sent", "symbols": rec["push"]["symbols"]}
        previous_channels = rec["push"].get("channels") or ["feishu"]
        pending_channels = [channel for channel in pending_channels if channel not in previous_channels]
        if not pending_channels:
            return {"skipped": "sent", "symbols": rec["push"]["symbols"]}
    th = cfg["min_rvol"]
    picks = [r for r in rec["rows"] if r.get("pick") and _fin(r.get("rvol")) and r["rvol"] >= th]
    stamp = {"at": now.isoformat(timespec="seconds"), "min_rvol": th, "symbols": [r["symbol"] for r in picks]}
    if not picks:
        if not ((rec.get("push") or {}).get("symbols")):
            rec["push"] = stamp
            _save_day(data_dir, rec)
        return {"pushed": [], "min_rvol": th}
    live: dict[str, dict] = {}
    try:
        v = view(data_dir, repo=repo, now=now)
        if v.get("is_today"):
            live = {r["symbol"]: r for r in v.get("picks") or []}
    except Exception as e:
        logger.warning("open radar push: live prices unavailable: %s", type(e).__name__)
    ctx: dict = {}
    try:
        from app.services import opp_page
        ctx, _ = opp_page.card_context(data_dir, stamp["symbols"], {}, day)
    except Exception as e:
        logger.warning("open radar push: context unavailable: %s", type(e).__name__)
    msg = push_message(day, now, picks, live, ctx, th)
    channels = None
    if enqueue is None:
        from app.services import notify_opportunity
        routed = notify_opportunity.enqueue_global(
            data_dir, f"open_radar:{day.isoformat()}", msg, event_type="open_radar",
            data={"day": day.isoformat(), "symbols": stamp["symbols"], "min_rvol": th},
            channels=pending_channels,
        )
        ok, channels = routed["ok"], routed["queued"]
    else:
        ok = bool(enqueue(data_dir, f"open_radar:{day.isoformat()}", msg))
    if ok:
        rec["push"] = stamp if channels is None else {
            **stamp, "channels": sorted(set(previous_channels) | set(channels)),
        }
        _save_day(data_dir, rec)
    logger.info("open radar push %s: %s (>= %s, queued=%s)", day, stamp["symbols"], th, ok)
    result = {"pushed": stamp["symbols"], "min_rvol": th, "queued": ok}
    if channels is not None:
        result["channels"] = channels
    return result


# ── 页面 ─────────────────────────────────────────────────
def _stats(data_dir: Path, upto: date) -> dict | None:
    """上线后 (不含补算) 已收盘的点名票: 只数, 收盘涨 >= 5% 只数。"""
    n = hit = 0
    for d in record_days(data_dir)[-STATS_MAX_DAYS:]:
        if d > upto:
            continue
        rec = load_day(data_dir, d) or {}
        for r in rec.get("rows") or []:
            if r.get("pick") and not r.get("backfill") and _fin(r.get("chg_close_pct")):
                n += 1
                hit += r["chg_close_pct"] >= 5
    return {"n": n, "hit": hit} if n else None


def view(data_dir: Path, *, repo=None, now: datetime | None = None) -> dict:
    """机会页「开盘」栏: 今天的榜 (没出就给最近一个交易日的榜并标 is_today=False) + 现价 + 上线后战绩。只读。
    新闻 / 财报标注由机会页聚合的 ctx 统一给, 这里不重复读。"""
    from app.market_clock import us_clock
    data_dir = Path(data_dir)
    now = us_clock.market_datetime(now or us_clock.now())
    today = now.date()
    days = [d for d in record_days(data_dir) if d <= today]
    rec = None
    for d in reversed(days):
        r = load_day(data_dir, d)
        if r and (r.get("rows") or (d == today and r.get("at"))):   # 今天跑过但没有一只有资格 = 今天的空榜, 不退回昨天
            rec = r
            break
    trading = us_clock.is_trading_day(today)
    cfg = push_settings(data_dir)
    base = {"contract": CONTRACT, "today": today.isoformat(), "ready_at": READY_AT.strftime("%H:%M"),
            "pending": trading and now.time() < READY_AT, "replay": REPLAY,
            "stats": _stats(data_dir, today), "push": {**cfg, "today": None}}
    if rec is None:
        return {**base, "day": None, "is_today": False, "picks": [], "others": [], "eligible": 0, "at": None}
    is_today = rec["day"] == today.isoformat()
    if is_today and isinstance(rec.get("push"), dict):
        base["push"]["today"] = {"symbols": list(rec["push"].get("symbols") or []), "at": rec["push"].get("at")}
    picks = [dict(r) for r in rec["rows"] if r.get("pick")]
    others = [dict(r) for r in rec["rows"] if not r.get("pick") and r["rvol"] >= 2][:3]
    if is_today and (picks or others):
        try:
            from app.services import swing_desk as sd
            px = sd._live_prices(repo, {r["symbol"] for r in picks + others}, today, now)
        except Exception as e:
            logger.warning("open radar view: prices unavailable: %s", type(e).__name__)
            px = {}
        for r in picks + others:
            p = px.get(r["symbol"])
            if p and _fin(p[0]) and r.get("prev_close"):
                r["price"], r["price_source"] = round(float(p[0]), 4), p[1]
                r["chg_now_pct"] = round((float(p[0]) / r["prev_close"] - 1) * 100, 3)
    return {**base, "day": rec["day"], "is_today": is_today, "at": rec.get("at"), "picks": picks, "others": others,
            "eligible": len(rec["rows"]), "backfill": bool(rec.get("backfill")), "settled": bool(rec.get("settled_at")),
            "errors": len(rec.get("errors") or [])}


def status(data_dir: Path) -> dict:
    from app.market_clock import us_clock
    today = us_clock.today()
    rec = load_day(data_dir, today) or {}
    return {"day": today.isoformat(), "ready": bool(rec.get("rows")), "ran": bool(rec.get("at")), "picks": [r["symbol"] for r in rec.get("rows") or [] if r.get("pick")],
            "eligible": len(rec.get("rows") or []), "errors": len(rec.get("errors") or []), "settled": bool(rec.get("settled_at")),
            "push": push_settings(data_dir), "pushed": list((rec.get("push") or {}).get("symbols") or [])}
