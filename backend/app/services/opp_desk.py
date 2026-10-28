"""机会页一页作战台 (0.41.0, 无限 10-28 选「非显然档」): 大盘状态 + 跨引擎共振 + 共振影子账本。

只展示 / 只记录, 不改交易环 / 准入 / 许可 / 下单簿 / 台账 / 价位:
- regime(): 大盘状态 (与 10-22 回测同口径): SPY 对昨收 >= +1% 或自选 >= 50% 涨 >= 1% = 普涨 (up);
  对称为普跌 (down); 其余平常 (normal)。读不到现价 = None。页面据此排四栏顺序。
- resonance(): 同一只票被 >= 2 个引擎同向点名 (盘前跳空 / 开盘放量 / 盘中强势在途卡 / 机会板挂单)。
  已止损的盘中卡不计; 引擎之间方向冲突的票不算。
- 影子账本 (tick / settle): 盘中每 5 分钟把新出现的共振票记一笔 (首次出现时刻 + 现价), 收盘后用官方日K
  回填 1 / 3 / 5 个交易日收盘涨跌 (按方向取正负)。预注册口径写在 SHADOW_RULE, 不进交易环、不推送。
  记录: data/opportunities/resonance/<美东日>.json。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONTRACT = "opp-desk-v1"
BROAD_SPY_PCT = 1.0            # SPY 对昨收 >= +1% (<= -1%)
BROAD_SHARE = 0.5              # 自选 >= 50% 涨 (跌) >= 1%
BROAD_MOVE_PCT = 1.0
MIN_ENGINES = 2
HORIZONS = (1, 3, 5)
SHADOW_START = dtime(9, 30)
SHADOW_RULE = {
    "id": "resonance-shadow-v1",
    "entry": "首次出现共振的检查点 (每 5 分钟, 09:30 至入场截止) 现价",
    "outcome": "第 1 / 3 / 5 个交易日官方收盘对入场价涨跌, 空头取反; 不设止损与目标",
    "scope": "自选; 同票同日只记一次",
}
ENGINES = ("gap", "open", "intraday", "board")


def _num(v) -> float | None:
    import math
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


# ── 大盘状态 ───────────────────────────────────────────────────────

def classify(spy_pct: float | None, ups: int, downs: int, n: int) -> str | None:
    """纯函数: 普涨 / 普跌 / 平常。既无 SPY 也无自选样本 = None。"""
    if spy_pct is None and n == 0:
        return None
    up = (spy_pct is not None and spy_pct >= BROAD_SPY_PCT) or (n > 0 and ups / n >= BROAD_SHARE)
    down = (spy_pct is not None and spy_pct <= -BROAD_SPY_PCT) or (n > 0 and downs / n >= BROAD_SHARE)
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return "normal"


def _index_pct(data_dir: Path, now: datetime) -> dict[str, float]:
    """SPY / QQQ 当日涨跌 (盘面情报温度带同一份缓存: IEX 现价对 SIP 官方前收; 非今日不给)。"""
    from app.services import intel_panel
    out: dict[str, float] = {}
    try:
        tape = intel_panel.tape_view(data_dir, now)
    except Exception as e:
        logger.warning("opp desk: tape unavailable: %s", type(e).__name__)
        return out
    for r in tape.get("indexes") or []:
        if r.get("symbol") in ("SPY", "QQQ") and r.get("live") and _num(r.get("chg_pct")) is not None:
            out[r["symbol"]] = float(r["chg_pct"])
    return out


def regime(data_dir: Path, repo, now: datetime, symbols: list[str]) -> dict:
    from app.market_clock import us_clock
    from app.services import opp_page
    from app.services import swing_desk as sd
    today = now.date()
    idx = _index_pct(data_dir, now) if us_clock.is_trading_day(today) else {}
    chg: dict[str, float] = {}
    px: dict[str, tuple[float, str]] = {}
    if symbols and us_clock.is_trading_day(today) and now.time() >= dtime(4, 0):
        px = sd._live_prices(repo, set(symbols), today, now)
        pc = opp_page._prev_closes(repo, sorted(px), today)
        chg = {s: (p / pc[s] - 1) * 100 for s, (p, _src) in px.items() if pc.get(s)}
    ups = sum(1 for v in chg.values() if v >= BROAD_MOVE_PCT)
    downs = sum(1 for v in chg.values() if v <= -BROAD_MOVE_PCT)
    key = classify(idx.get("SPY"), ups, downs, len(chg))
    return {"key": key, "spy": idx.get("SPY"), "qqq": idx.get("QQQ"), "n": len(chg), "up": ups, "down": downs,
            "up3": sum(1 for v in chg.values() if v >= 3), "chg": {s: round(v, 2) for s, v in chg.items()},
            "px": {s: round(float(px[s][0]), 4) for s in chg}, "at": now.isoformat(timespec="seconds")}


# ── 共振 ───────────────────────────────────────────────────────────

def resonance(*, now: datetime, swing: dict | None, intraday: dict | None, radar: dict | None, gaps: dict | None) -> list[dict]:
    """纯函数。每只票: 各引擎方向; 同向 >= MIN_ENGINES 且无反向 = 共振。"""
    today = now.date().isoformat()
    hits: dict[str, dict[str, dict]] = {}

    def put(sym, eng, side, **kw):
        if isinstance(sym, str) and side in ("long", "short"):
            hits.setdefault(sym, {})[eng] = {"side": side, **kw}
    gp = gaps or {}
    if str(gp.get("at", ""))[:10] == today:
        for r in gp.get("rows") or []:
            g = _num(r.get("gap_pct"))
            if g:
                put(r.get("symbol"), "gap", "long" if g > 0 else "short", pct=round(g, 2))
    rd = radar or {}
    if rd.get("is_today"):
        for r in rd.get("picks") or []:
            put(r.get("symbol"), "open", "long" if r.get("dir", 1) >= 0 else "short", rank=r.get("rank"), rvol=_num(r.get("rvol")))
    for c in (intraday or {}).get("active") or []:
        if isinstance(c, dict) and c.get("status") == "open":
            prev = hits.get(str(c.get("symbol")), {}).get("intraday")
            cards = [*(prev or {}).get("cards", []), {"day": c.get("day"), "r": _num(c.get("r_now"))}]
            put(c.get("symbol"), "intraday", c.get("side") or "long", cards=cards)
    for c in (swing or {}).get("long", []) + (swing or {}).get("short", []):
        if isinstance(c, dict) and c.get("state") in ("pending", "in_zone"):
            put(c.get("symbol"), "board", c.get("side"), state=c.get("state"))
    out = []
    for sym, e in hits.items():
        sides = {v["side"] for v in e.values()}
        if len(e) >= MIN_ENGINES and len(sides) == 1:
            out.append({"symbol": sym, "side": next(iter(sides)), "n": len(e), "engines": {k: e[k] for k in ENGINES if k in e}})
    out.sort(key=lambda x: (-x["n"], x["symbol"]))
    return out


# ── 影子账本 ───────────────────────────────────────────────────────

def shadow_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "resonance"


def _load(data_dir: Path, d: date) -> dict:
    try:
        raw = json.loads((shadow_dir(data_dir) / f"{d.isoformat()}.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("rows"), list):
            return raw
    except (OSError, ValueError):
        pass
    return {"day": d.isoformat(), "rule": SHADOW_RULE["id"], "rows": []}


def _save(data_dir: Path, rec: dict) -> None:
    from app.services.fs_utils import atomic_write_text
    p = shadow_dir(data_dir) / f"{rec['day']}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(rec, ensure_ascii=False, allow_nan=False))


def days(data_dir: Path) -> list[date]:
    out = []
    d = shadow_dir(data_dir)
    for f in d.glob("20*.json") if d.exists() else []:
        try:
            out.append(date.fromisoformat(f.stem))
        except ValueError:
            continue
    return sorted(out)


def record(data_dir: Path, now: datetime, res: list[dict], prices: dict[str, float]) -> int:
    """把新出现的共振票记进当天影子账本 (同票同日一次)。返回新增笔数。"""
    rec = _load(data_dir, now.date())
    seen = {r["symbol"] for r in rec["rows"]}
    n = 0
    for x in res:
        p = _num(prices.get(x["symbol"]))
        if x["symbol"] in seen or p is None or p <= 0:
            continue
        rec["rows"].append({"symbol": x["symbol"], "side": x["side"], "n": x["n"], "engines": sorted(x["engines"]),
                            "at": now.isoformat(timespec="seconds"), "entry": round(p, 4)})
        n += 1
    if n:
        _save(data_dir, rec)
    return n


def settle(data_dir: Path, repo, today: date | None = None) -> int:
    """回填 1 / 3 / 5 个交易日收盘涨跌 (官方日K)。返回本次补上的格子数。"""
    from app.market_clock import us_clock
    today = today or us_clock.today()
    getter = getattr(repo, "get_daily_batch", None)
    if getter is None:
        return 0
    filled = 0
    for d in days(data_dir)[-20:]:
        rec = _load(data_dir, d)
        todo = [r for r in rec["rows"] if any(f"r{h}" not in r for h in HORIZONS)]
        if not todo:
            continue
        tds = us_clock.trading_days_between(d, d + timedelta(days=12))
        targets = {h: tds[h - 1] for h in HORIZONS if len(tds) >= h and tds[h - 1] <= today}
        if not targets:
            continue
        b = getter(sorted({r["symbol"] for r in todo}), d, max(targets.values()), columns=["symbol", "date", "close"])
        if b is None or b.is_empty():
            continue
        closes = {(str(s), str(dt)[:10]): float(c) for s, dt, c in b.select(["symbol", "date", "close"]).iter_rows()}
        changed = False
        for r in todo:
            sg = 1 if r["side"] == "long" else -1
            for h, td in targets.items():
                c = closes.get((r["symbol"], td.isoformat()))
                if f"r{h}" not in r and c and r.get("entry"):
                    r[f"r{h}"] = round(sg * (c / float(r["entry"]) - 1) * 100, 3)
                    filled += 1
                    changed = True
        if changed:
            _save(data_dir, rec)
    return filled


def shadow_stats(data_dir: Path) -> dict:
    rows = [r for d in days(data_dir) for r in _load(data_dir, d)["rows"]]
    done = [r["r5"] for r in rows if isinstance(r.get("r5"), (int, float))]
    return {"rule": SHADOW_RULE["id"], "n": len(rows), "settled": len(done),
            "mean5": round(sum(done) / len(done), 3) if done else None,
            "win5": round(sum(1 for v in done if v > 0) / len(done), 3) if done else None,
            "since": days(data_dir)[0].isoformat() if days(data_dir) else None}


def today_rows(data_dir: Path, d: date) -> dict[str, dict]:
    return {r["symbol"]: r for r in _load(data_dir, d)["rows"]}


def in_window(now: datetime) -> bool:
    from app.market_clock import us_clock
    d = now.date()
    return us_clock.is_trading_day(d) and SHADOW_START <= now.time() < us_clock.entry_close(d)


def tick(data_dir: Path, *, repo=None, loop=None, now: datetime | None = None) -> dict:
    """zen_resonance_shadow (5 分钟): 窗口内算一次共振, 新票记一笔。不抛。"""
    from app.market_clock import us_clock
    from app.services import intraday_strength as ids
    from app.services import open_radar, opp_page
    from app.services import swing_desk as sd
    now = us_clock.market_datetime(now or us_clock.now())
    try:
        if not in_window(now):
            return {"skipped": "window"}
        errors: dict[str, str] = {}
        swing = opp_page._section("swing", lambda: sd.build_live(data_dir, repo=repo, loop=loop), errors)
        intraday = opp_page._section("intraday", lambda: ids.build_live(data_dir, repo=repo, loop=loop), errors)
        radar = opp_page._section("radar", lambda: open_radar.view(data_dir, repo=repo, now=now), errors)
        gaps = opp_page._section("gaps", lambda: opp_page.gap_board(data_dir, repo, now, swing, "session"), errors)
        res = resonance(now=now, swing=swing, intraday=intraday, radar=radar, gaps=gaps)
        if not res:
            return {"recorded": 0}
        px = {s: p for s, (p, src) in sd._live_prices(repo, {x["symbol"] for x in res}, now.date(), now).items()
              if src in ("live", "snapshot")}
        return {"recorded": record(data_dir, now, res, px), "errors": errors}
    except Exception as e:
        logger.warning("resonance shadow tick failed: %s", type(e).__name__)
        return {"error": type(e).__name__}


def view(data_dir: Path, *, repo=None, now: datetime, swing, intraday, radar, gaps, symbols: list[str]) -> dict[str, Any]:
    """/page 的 desk 段 (只读)。"""
    res = resonance(now=now, swing=swing, intraday=intraday, radar=radar, gaps=gaps)
    rg = regime(data_dir, repo, now, symbols)
    rows = today_rows(data_dir, now.date())
    px = rg.pop("px", None) or {}
    for x in res:
        x["chg"] = rg["chg"].get(x["symbol"])
        x["price"] = px.get(x["symbol"])
        x["shadow_at"] = (rows.get(x["symbol"]) or {}).get("at")
    return {"contract": CONTRACT, "regime": rg, "resonance": res, "shadow": shadow_stats(data_dir)}
