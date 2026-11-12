"""舆情评级回放 (第一期校准用)。逻辑全在 app/ 里, 命令行只是薄壳:

  python -m app.services.sentiment_replay backfill --data <目录> --start 2026-08-25 --end 2026-11-12
      回填 GDELT 小时文章数 / 维基日浏览量到 <目录>/sentiment/series/ (研究用目录, 不碰生产 data)。
  python -m app.services.sentiment_replay run --data <目录> --start 2026-09-28 --end 2026-11-11 --out <报告.md>
      24 池逐小时 as-of 回放: 每日 A/B 数、NVDA 案例、A/B 事件之后 1/3/5 日相对 QQQ 涨跌 (只统计)。
  python -m app.services.sentiment_replay asof --data <目录> --symbol NVDA --at 2026-11-08T22:00Z [--expect A] [--set k=v]
      单点评级; --expect 不符时退出码 1 (反向验证用)。

只用 as-of 时刻之前已入库的数据 (见 sentiment_score 的可得时刻规则)。行情 = Alpaca SIP 日K, key 只从凭据模块读, 不打印。
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.services import sentiment_sources as ss


# ── 回填 ─────────────────────────────────────────────────────────────

def _chunks(start: datetime, end: datetime, days: int) -> list[tuple[datetime, datetime]]:
    out, a = [], start
    while a < end:
        b = min(a + timedelta(days=days), end)
        out.append((a, b))
        a = b
    return out


def _log(msg: str) -> None:
    print(msg, flush=True)


def backfill(data_dir, start: date, end: date, *, symbols: list[str] | None = None, workers: int = 3,
             gap_s: float = ss.GDELT_GAP_S, chunk_d: int = 7, log=_log, skip_wiki: bool = False,
             skip_gdelt: bool = False) -> dict:
    """GDELT 按 7 天一块 (返回日粒度就对半再拆), 多线程但请求发起间隔 ≥ gap_s (守官方限速); 维基一票一次。
    可断点续跑: 已完成的块记在序列文件 chunks 里。"""
    cfg = ss.load_config()
    names = cfg.get("names") or {}
    syms = symbols or list(cfg.get("pool") or [])
    s_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    e_dt = datetime(end.year, end.month, end.day, tzinfo=UTC)
    stats = {"wiki": 0, "gdelt_ok": 0, "gdelt_fail": 0, "split": 0}
    for s in ([] if skip_wiki else syms):
        arts = (names.get(s) or {}).get("wiki") or []
        if not arts:
            continue
        per = {}
        for a in arts:
            for tries in range(4):
                time.sleep(3.0 + 30 * tries)      # Wikimedia 连发会 429
                try:
                    per[a] = ss.parse_wiki(ss._http_get(ss.wiki_url(a, start, end)))
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"wiki {s} {a} try {tries} failed: {type(e).__name__} {getattr(e, 'code', '')}")
        ss.write_json(ss.root(data_dir) / "series" / "wiki" / f"{s}.json", {"articles": per, "backfill": True})
        stats["wiki"] += 1
    q: queue.Queue = queue.Queue()
    locks = {s: threading.Lock() for s in syms}
    for s in ([] if skip_gdelt else syms):
        term = (names.get(s) or {}).get("gdelt")
        if not term:
            continue
        done = set((ss.read_json(ss.root(data_dir) / "series" / "gdelt" / f"{s}.json") or {}).get("chunks") or [])
        for a, b in _chunks(s_dt, e_dt, chunk_d):
            if f"{ss.iso(a)}/{ss.iso(b)}" not in done:
                q.put((s, term, a, b, 0))
    gate = {"next": 0.0}
    gate_lock = threading.Lock()

    def worker():
        while True:
            try:
                s, term, a, b, tries = q.get_nowait()
            except queue.Empty:
                return
            with gate_lock:
                wait = gate["next"] - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                gate["next"] = time.monotonic() + gap_s
            try:
                try:
                    hours = ss.parse_gdelt(ss._http_get(ss.gdelt_url(term, a, b), timeout=90))
                finally:   # GDELT 的 5 秒按上一次请求结束算 (11-12 实测: 按发起算会连续 429)
                    with gate_lock:
                        gate["next"] = max(gate["next"], time.monotonic() + gap_s)
            except ValueError as e:
                if "day resolution" in str(e) and (b - a) > timedelta(hours=48):
                    mid = a + (b - a) / 2
                    q.put((s, term, a, mid, 0))
                    q.put((s, term, mid, b, 0))
                    stats["split"] += 1
                    continue
                err = e
            except Exception as e:  # noqa: BLE001
                err = e
                log(f"gdelt {s} {ss.iso(a)} try {tries}: {type(e).__name__} {getattr(e, 'code', '')}")
            else:
                with locks[s]:
                    p = ss.root(data_dir) / "series" / "gdelt" / f"{s}.json"
                    cur = ss.read_json(p) or {}
                    out = ss.merge_hours(cur, hours, a, b, term=term)
                    out["chunks"] = sorted(set(cur.get("chunks") or []) | {f"{ss.iso(a)}/{ss.iso(b)}"})
                    ss.write_json(p, out)
                stats["gdelt_ok"] += 1
                continue
            if tries < 3:
                time.sleep(20 * (tries + 1))
                q.put((s, term, a, b, tries + 1))
            else:
                stats["gdelt_fail"] += 1
                log(f"gdelt {s} {ss.iso(a)} failed: {type(err).__name__}: {str(err)[:80]}")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log(f"backfill done: {stats}")
    return stats


# ── 行情 (Alpaca SIP 日K; key 只经凭据模块读, 不打印) ───────────────

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
BENCH = "QQQ"


def fetch_prices(path: Path, symbols: list[str] | None = None, start: str = "2026-07-15", end: str | None = None) -> dict:
    """SIP 右端必须早于 now − 16 分钟 (免费档近端 403)。"""
    import urllib.parse
    end = end or ss.iso(datetime.now(UTC) - timedelta(minutes=20))
    from app import credentials
    key, secret, _ = credentials.alpaca()
    if not key:
        raise SystemExit("alpaca key missing (ZENTRADE_ENV_DIR)")
    syms = sorted(set(symbols or ss.load_config()["pool"]) | {BENCH})
    out: dict[str, dict[str, float]] = {}
    token = None
    while True:
        q = {"symbols": ",".join(syms), "timeframe": "1Day", "start": start, "end": end, "feed": "sip",
             "adjustment": "all", "limit": 10000}
        if token:
            q["page_token"] = token
        body = json.loads(ss._http_get(BARS_URL + "?" + urllib.parse.urlencode(q),
                                       headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}))
        for sym, bars in (body.get("bars") or {}).items():
            for b in bars:
                day = ss.parse_iso(b["t"]).astimezone(_tz()).date().isoformat()
                out.setdefault(sym, {})[day] = float(b["c"])
        token = body.get("next_page_token")
        if not token:
            break
    today = datetime.now(UTC).astimezone(_tz()).date().isoformat()
    out = {s: {d: c for d, c in v.items() if d < today} for s, v in out.items()}   # 当天未收盘的半根不要
    rep = {"source": "Alpaca SIP 1Day adjustment=all", "fetched_at": ss.iso(datetime.now(UTC)), "closes": out}
    ss.write_json(path, rep)
    return rep


def load_prices(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _tz():
    from app.market_clock import us_clock
    return us_clock.tz


def price_calendar(prices: dict):
    """交易日 = 基准 ETF 有日K的日子 (离线、可核验)。"""
    days = sorted(date.fromisoformat(d) for d in (prices["closes"].get(BENCH) or {}))

    def prev(d: date, n: int) -> list[date]:
        import bisect
        i = bisect.bisect_left(days, d)
        return list(reversed(days[max(0, i - n):i]))
    prev.days = days  # type: ignore[attr-defined]
    return prev


# ── 回放 ─────────────────────────────────────────────────────────────

def _overrides(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        k, v = it.split("=", 1)
        out[k.strip()] = float(v)
    return out


def asof(data_dir, sym: str, t: datetime, *, overrides: dict | None = None, prev_days=None) -> dict:
    from app.services import sentiment_score as sc
    th = sc.thresholds(ss.load_config(), overrides)
    inp = sc.load_inputs(data_dir, [sym], t - timedelta(days=60), t)
    return sc.evaluate(inp, sym, t, th, prev_days=prev_days or sc.us_prev_trading_days)


def _brief(r: dict) -> dict:
    fam = {f: {"z": v["z"], "heat": v["heat"], "anom": v["anom"],
               "src": {k: {kk: s[kk] for kk in ("x", "med", "z", "n", "anom") if kk in s} for k, s in v["sources"].items()}}
           for f, v in (r.get("families") or {}).items()}
    return {k: r.get(k) for k in ("symbol", "grade", "heat", "direction", "event_type", "hard", "n_families",
                                  "first_at", "bull_w", "bear_w", "missing")} | {"families": fam}


def run(data_dir, start: date, end: date, *, prices: dict, overrides: dict | None = None,
        symbols: list[str] | None = None) -> dict:
    from app.services import sentiment_score as sc
    cfg = ss.load_config()
    th = sc.thresholds(cfg, overrides)
    syms = symbols or list(cfg["pool"])
    prev = price_calendar(prices)
    tdays = [d for d in prev.days if start <= d <= end]
    t0 = datetime(start.year, start.month, start.day, tzinfo=UTC)
    t1 = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    inp = sc.load_inputs(data_dir, syms, t0 - timedelta(days=45), t1)
    steps = []
    t = t0
    while t < t1:
        steps.append(t)
        t += timedelta(hours=1)
    gap = timedelta(hours=float(th["episode_gap_h"]))
    events, fam_cov = [], {f: 0 for f in sc.FAMILIES}
    timeline: dict[str, list] = {}
    for s in syms:
        last_a = last_ab = None
        for t in steps:
            r = sc.evaluate(inp, s, t, th, prev_days=prev, detail=False)
            for f in r["families_ok"]:
                fam_cov[f] += 1
            if s == "NVDA" and datetime(2026, 11, 8, 12, tzinfo=UTC) <= t <= datetime(2026, 11, 9, 15, tzinfo=UTC):
                timeline.setdefault(s, []).append((t, r))
            g = r["grade"]
            if g == "A":
                if last_a is None or t - last_a >= gap:
                    events.append(("A", s, t, r))
                last_a = t
            if g in ("A", "B"):
                if g == "B" and (last_ab is None or t - last_ab >= gap):
                    events.append(("B", s, t, r))
                last_ab = t
    closes = prices["closes"]

    def fwd(sym: str, t: datetime) -> dict:
        et = t.astimezone(_tz())
        days = prev.days
        import bisect
        # 锚 = 事件之前最后一个已收盘交易日 (16:00 ET 收盘)
        i = bisect.bisect_right(days, et.date()) - 1
        if i >= 0 and days[i] == et.date() and et.hour < 16:
            i -= 1
        out = {"anchor": days[i].isoformat() if i >= 0 else None}
        for k in (1, 3, 5):
            j = i + k
            if i < 0 or j >= len(days):
                out[k] = None
                continue
            a, b = days[i].isoformat(), days[j].isoformat()
            ps, pq = closes.get(sym) or {}, closes.get(BENCH) or {}
            if a in ps and b in ps and a in pq and b in pq:
                out[k] = round((ps[b] / ps[a] - 1) * 100 - (pq[b] / pq[a] - 1) * 100, 2)
            else:
                out[k] = None
        return out
    ev_rows = []
    for g, s, t, r in events:
        ev_rows.append({"grade": g, "symbol": s, "at": ss.iso(t), "et_day": t.astimezone(_tz()).date().isoformat(),
                        "heat": r["heat"], "direction": r["direction"], "event_type": r["event_type"],
                        "n_families": r["n_families"], "fwd": fwd(s, t)})
    per_day: dict[str, dict] = {}
    for e in ev_rows:
        d = per_day.setdefault(e["et_day"], {"A": [], "B": []})
        d[e["grade"]].append(e["symbol"])
    n_a = sum(1 for e in ev_rows if e["grade"] == "A")
    n_b = sum(1 for e in ev_rows if e["grade"] == "B")
    summary = {"trading_days": len(tdays), "A": n_a, "B": n_b,
               "A_per_tday": round(n_a / max(len(tdays), 1), 3), "B_per_tday": round(n_b / max(len(tdays), 1), 3),
               "steps": len(steps), "symbols": len(syms), "config_sha": sc.config_sha(), "thresholds": th}
    probe = datetime(2026, 11, 8, 22, tzinfo=UTC)
    nvda_probe = _brief(sc.evaluate(inp, "NVDA", probe, th, prev_days=prev)) if "NVDA" in syms else None
    first_a = next((ss.iso(t) for t, r in timeline.get("NVDA", []) if r["grade"] == "A"), None)
    return {"summary": summary, "events": ev_rows, "per_day": per_day, "tdays": [d.isoformat() for d in tdays],
            "family_cover": fam_cov, "nvda_timeline": [(ss.iso(t), r) for t, r in timeline.get("NVDA", [])],
            "nvda_probe": nvda_probe, "nvda_first_a": first_a, "prices_source": prices.get("source"),
            "prices_fetched_at": prices.get("fetched_at")}


def _stat(vals: list[float]) -> str:
    v = [x for x in vals if x is not None]
    if not v:
        return "—"
    v.sort()
    mean = sum(v) / len(v)
    med = v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2
    return f"n={len(v)} 均值 {mean:+.2f}% 中位 {med:+.2f}%"


def render(rep: dict) -> str:
    s = rep["summary"]
    L = ["# 舆情评级回放报告（24 池，逐小时 as-of）", "",
         f"- 区间交易日 {s['trading_days']} 个；评估步 {s['steps']} 个 × {s['symbols']} 只",
         f"- A 事件 {s['A']} 个，平均每交易日 **{s['A_per_tday']}**；B 事件 {s['B']} 个，平均每交易日 {s['B_per_tday']}",
         f"- 阈值 `{json.dumps(s['thresholds'], ensure_ascii=False)}`",
         f"- 配置 sha256 `{s['config_sha']}`",
         f"- 行情：{rep.get('prices_source')}（取数 {rep.get('prices_fetched_at')}）；相对 QQQ = 个股涨跌 − QQQ 涨跌，锚 = 事件之前最后一个收盘",
         f"- 族覆盖（有数据的评估步）：{json.dumps(rep['family_cover'], ensure_ascii=False)}", "",
         "## NVDA 案例", "",
         f"- as-of 2026-11-08T22:00Z：`{json.dumps(rep['nvda_probe'], ensure_ascii=False)}`",
         f"- 回放中首次 A：{rep['nvda_first_a']}", "",
         "| as-of (UTC) | 评级 | 热度 | 方向 | 异常族 | 有数据的族 | 类型 |", "|---|---|---|---|---|---|---|"]
    for t, r in rep["nvda_timeline"]:
        L.append(f"| {t} | {r['grade'] or '—'} | {r['heat']} | {r['direction']} | {r['n_families']} | "
                 f"{','.join(r['families_ok'])} | {r['event_type'] or '—'} |")
    L += ["", "## A/B 事件之后相对 QQQ（只统计，不进交易）", ""]
    for g in ("A", "B"):
        for dirn in ("bearish", "bullish", "mixed", "none"):
            ev = [e for e in rep["events"] if e["grade"] == g and e["direction"] == dirn]
            if not ev:
                continue
            L.append(f"- {g} · {dirn}（{len(ev)} 个）：T+1 {_stat([e['fwd'][1] for e in ev])}；"
                     f"T+3 {_stat([e['fwd'][3] for e in ev])}；T+5 {_stat([e['fwd'][5] for e in ev])}")
    L += ["", "## 每日 A/B 数（美东日期）", "", "| 日期 | 交易日 | A | B |", "|---|---|---|---|"]
    days = sorted(set(rep["per_day"]) | set(rep["tdays"]))
    for d in days:
        x = rep["per_day"].get(d) or {"A": [], "B": []}
        L.append(f"| {d} | {'是' if d in rep['tdays'] else '否'} | {len(x['A'])} {' '.join(x['A'])} | "
                 f"{len(x['B'])} {' '.join(x['B'])} |")
    L += ["", "## 事件明细", "", "| 级 | 代码 | as-of | 热度 | 方向 | 类型 | 异常族 | T+1 | T+3 | T+5 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for e in rep["events"]:
        f = e["fwd"]
        L.append(f"| {e['grade']} | {e['symbol']} | {e['at']} | {e['heat']} | {e['direction']} | {e['event_type'] or '—'} | "
                 f"{e['n_families']} | {f[1]} | {f[3]} | {f[5]} |")
    return "\n".join(L) + "\n"


# ── 命令行 ───────────────────────────────────────────────────────────

def _d(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.services.sentiment_replay")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--data", required=True)
    b.add_argument("--start", required=True)
    b.add_argument("--end", required=True)
    b.add_argument("--symbols", default="")
    b.add_argument("--workers", type=int, default=2)
    b.add_argument("--skip-wiki", action="store_true")
    b.add_argument("--skip-gdelt", action="store_true")
    b.add_argument("--gap", type=float, default=ss.GDELT_GAP_S)
    r = sub.add_parser("run")
    r.add_argument("--data", required=True)
    r.add_argument("--start", required=True)
    r.add_argument("--end", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--prices", default="")
    r.add_argument("--set", action="append", default=[])
    a = sub.add_parser("asof")
    a.add_argument("--data", required=True)
    a.add_argument("--symbol", required=True)
    a.add_argument("--at", required=True)
    a.add_argument("--expect", default="")
    a.add_argument("--expect-dir", default="")
    a.add_argument("--set", action="append", default=[])
    args = ap.parse_args(argv)
    if args.cmd == "backfill":
        syms = [x.strip().upper() for x in args.symbols.split(",") if x.strip()] or None
        backfill(Path(args.data), _d(args.start), _d(args.end), symbols=syms, workers=args.workers,
                 skip_wiki=args.skip_wiki, skip_gdelt=args.skip_gdelt, gap_s=args.gap)
        return 0
    overrides = _overrides(args.set)
    data = Path(args.data)
    prices_path = Path(args.prices) if getattr(args, "prices", "") else data / "prices.json"
    if args.cmd == "asof":
        prices = load_prices(prices_path) if prices_path.exists() else None
        prev = price_calendar(prices) if prices else None
        row = asof(data, args.symbol.upper(), ss.parse_iso(args.at), overrides=overrides, prev_days=prev)
        print(json.dumps(_brief(row), ensure_ascii=False, indent=1))
        ok = (not args.expect or row["grade"] == args.expect) and \
             (not args.expect_dir or row["direction"] == args.expect_dir)
        print(f"EXPECT grade={args.expect or '-'} dir={args.expect_dir or '-'} -> {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    prices = load_prices(prices_path) if prices_path.exists() else fetch_prices(prices_path)
    rep = run(data, _d(args.start), _d(args.end), prices=prices, overrides=overrides)
    Path(args.out).write_text(render(rep), encoding="utf-8")
    print(json.dumps(rep["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
