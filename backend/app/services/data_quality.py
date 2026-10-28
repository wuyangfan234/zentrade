# ruff: noqa: RUF001
"""日K数据质量自动检查 (2026-10-12): 盘后自动跑, 有问题推飞书。只读, 不改数据。

三类检查:
1. 完整性 — 复用 data_integrity.scan_recent_integrity (分区缺失 / 盘中快照残留)。
2. 数值自洽 — 最新交易日分区: 最高价不低于开收、最低价不高于开收、价量非负、
   单日涨跌超 ±25% 且无公司行动、关键标的 (指数 ETF + 持仓 + 活跃卡) 缺行。
3. 小范围对价 — 关键标的 (≤25 只) 的收盘价与另一个数据源比, 偏差超 0.5% 报出。
   全市场 8,496 只不对价 (没必要, 也打不起这个请求量)。

10-02 事故形态: 日K分区被实时快照覆盖、开盘价缺失, 当天没人发现。这套检查当天就能报出来。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.market_clock import us_clock

logger = logging.getLogger(__name__)

KEY_ETFS = ("QQQ", "SPY")
MAX_CROSS_SYMBOLS = 25
CROSS_TOL_PCT = 0.5          # 两源收盘价偏差上限
JUMP_PCT = 25.0              # 单日涨跌超过此值且无公司行动 → 可疑
SAMPLE = 5                   # 报告里每类问题最多列几个标的
PENNY = 1.0                  # 前收盘低于此价的仙股不做跳变检查 (0.12.1)
_DERIV_DOT = {"WS", "WT", "W", "RT", "R", "U", "UN"}


def is_derivative_symbol(sym: str) -> bool:
    """权证 / 权利 / 单位类代码 (0.12.1)。纳斯达克 5 字母代码第 5 位 W/R/U, 纽交所 .WS/.RT/.U 等后缀。"""
    if not sym:
        return False
    if "." in sym:
        return sym.rsplit(".", 1)[1].upper() in _DERIV_DOT
    return len(sym) == 5 and sym[-1].upper() in "WRU"


def alert_symbols(data_dir, keys: list[str] | None) -> set[str]:
    """跳变告警范围: 指数 ETF + 持仓 + 活跃卡 (keys) + 自选。其余标的只记录不推送 (0.12.1)。"""
    out = set(KEY_ETFS) | set(keys or [])
    try:
        import polars as pl
        p = Path(data_dir) / "user_data" / "watchlist.parquet"
        if p.exists():
            out |= set(pl.read_parquet(p, columns=["symbol"])["symbol"].to_list())
    except Exception as e:
        logger.warning("data quality: 自选读取失败: %s", type(e).__name__)
    return out


def _part(data_dir, day: date, table: str = "kline_daily") -> Path:
    return Path(data_dir) / table / f"date={day.isoformat()}" / "part.parquet"


def key_symbols(data_dir, loop=None) -> list[str]:
    """关键标的: 指数 ETF + 当前持仓 + 活跃卡, 去重后截断。"""
    out = list(KEY_ETFS)
    if loop is not None:
        try:
            with loop.ledger._lock:
                out += [p.symbol for p in loop.ledger.open_positions]
            out += [c.symbol for c in loop.trader._cards.values()]
        except Exception as e:
            logger.warning("data quality: 关键标的取用失败: %s", type(e).__name__)
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq[:MAX_CROSS_SYMBOLS]


def numeric_issues(data_dir, day: date, *, keys: list[str] | None = None,
                   alert: set[str] | None = None, observed: list | None = None) -> list[dict]:
    """最新日K分区的数值自洽检查。分区缺失交给完整性检查报, 这里只返回空。

    跳变: alert=None 时全部可疑跳变都报; 给了 alert 集合则只报集合内的, 其余写进 observed (只记录)。"""
    import polars as pl

    p = _part(data_dir, day)
    if not p.exists():
        return []
    try:
        df = pl.read_parquet(p, columns=["symbol", "open", "high", "low", "close", "volume"])
    except Exception as e:
        return [{"kind": "unreadable", "detail": f"{type(e).__name__}", "symbols": []}]
    if df.is_empty():
        return [{"kind": "empty_partition", "detail": "分区无数据行", "symbols": []}]
    out = []

    def add(kind: str, mask, detail: str) -> None:
        bad = df.filter(mask)
        if bad.height:
            out.append({"kind": kind, "detail": detail, "n": bad.height,
                        "symbols": bad["symbol"].to_list()[:SAMPLE]})

    hi, lo = pl.col("high"), pl.col("low")
    op, cl = pl.col("open"), pl.col("close")
    add("high_below_body", (hi < cl) | (hi < op.fill_null(cl)), "最高价低于开盘或收盘")
    add("low_above_body", (lo > cl) | (lo > op.fill_null(cl)), "最低价高于开盘或收盘")
    add("nonpositive_price", (cl <= 0) | (hi <= 0) | (lo <= 0), "价格非正")
    add("negative_volume", pl.col("volume") < 0, "成交量为负")
    add("null_close", cl.is_null(), "收盘价缺失")
    add("null_open", op.is_null(), "开盘价缺失")

    prev = us_clock.previous_trading_day(day)
    pp = _part(data_dir, prev)
    if pp.exists():
        try:
            before = pl.read_parquet(pp, columns=["symbol", "close"]).rename({"close": "prev_close"})
            joined = df.select(["symbol", "close"]).join(before, on="symbol", how="inner").filter(
                (pl.col("prev_close") > 0) & pl.col("close").is_not_null())
            jump = joined.with_columns(((pl.col("close") / pl.col("prev_close") - 1) * 100).alias("chg")).filter(
                (pl.col("chg").abs() > JUMP_PCT) & (pl.col("prev_close") >= PENNY))
            if jump.height:
                flagged = _corp_action_symbols(data_dir, day) | _source_split_symbols(data_dir, day)
                rows = [r for r in jump.rows(named=True)
                        if r["symbol"] not in flagged and not is_derivative_symbol(r["symbol"])]
                if alert is not None:
                    if observed is not None:
                        observed.extend(f"{r['symbol']} {r['chg']:+.0f}%" for r in rows if r["symbol"] not in alert)
                    rows = [r for r in rows if r["symbol"] in alert]
                if rows:
                    out.append({"kind": "suspicious_jump", "n": len(rows),
                                "detail": f"单日涨跌超 ±{JUMP_PCT:.0f}% 且无公司行动",
                                "symbols": [f"{r['symbol']} {r['chg']:+.0f}%" for r in rows[:SAMPLE]]})
        except Exception as e:
            logger.warning("data quality: 跳变检查失败: %s", type(e).__name__)

    if keys:
        have = set(df["symbol"].to_list())
        missing = [s for s in keys if s not in have]
        if missing:
            out.append({"kind": "key_symbol_missing", "n": len(missing),
                        "detail": "关键标的当日无日K行", "symbols": missing[:SAMPLE]})
    return out


def _corp_action_symbols(data_dir, day: date) -> set[str]:
    try:
        from app.services.corporate_actions import CorporateActionService
        actions = CorporateActionService(Path(data_dir)).actions()
    except Exception:
        return set()
    out = set()
    for a in actions or []:
        ex = str(a.get("ex_date") or "")[:10]
        if ex and abs((date.fromisoformat(ex) - day).days) <= 1:
            out.add(a.get("symbol"))
    return out


def _source_split_symbols(data_dir, day: date) -> set[str]:
    """数据源 (Alpaca raw/all 双拉) 复权事件表里当天有事件的标的。本地公司行动表只覆盖持仓/卡, 全市场拆合股靠这里。"""
    import polars as pl
    out: set[str] = set()
    for sub in ("adj_factor", "adj_factor_etf"):
        p = Path(data_dir) / sub / "all.parquet"
        if not p.exists():
            continue
        try:
            df = pl.read_parquet(p, columns=["symbol", "trade_date"])
            out |= set(df.filter(pl.col("trade_date") == day)["symbol"].to_list())
        except Exception as e:
            logger.warning("data quality: 复权事件读取失败: %s", type(e).__name__)
    return out


def cross_check(data_dir, day: date, symbols: list[str], *, provider_name: str | None = None) -> dict:
    """关键标的收盘价与另一个数据源对比。取不到数据不算问题, 如实记 unavailable。"""
    import polars as pl

    p = _part(data_dir, day)
    if not p.exists() or not symbols:
        return {"status": "unavailable", "reason": "no_partition" if not p.exists() else "no_symbols"}
    if provider_name is None:
        from app.services.preferences import get_daily_data_provider, get_realtime_data_provider
        daily = get_daily_data_provider()
        rt = get_realtime_data_provider()
        provider_name = rt if rt and rt != daily else ("tickflow" if daily != "tickflow" else "alpaca")
    try:
        from app.data_providers.custom.loader import get_provider
        end = datetime.combine(day, time(23, 59), tzinfo=us_clock.tz)
        df = get_provider(provider_name).get_daily(symbols, end - timedelta(days=6), end)
        local = pl.read_parquet(p, columns=["symbol", "close"]).filter(pl.col("symbol").is_in(symbols))
        if df is None or df.is_empty() or local.is_empty():
            return {"status": "unavailable", "reason": "empty", "provider": provider_name}
        other = (df.filter(pl.col("date") == day).select(["symbol", "close"])
                 .rename({"close": "other_close"}))
        if other.is_empty():
            return {"status": "unavailable", "reason": "no_rows_for_day", "provider": provider_name}
        j = local.join(other, on="symbol", how="inner").filter(pl.col("close") > 0)
        j = j.with_columns(((pl.col("other_close") / pl.col("close") - 1).abs() * 100).alias("dev"))
        bad = j.filter(pl.col("dev") > CROSS_TOL_PCT)
        return {"status": "ok" if bad.is_empty() else "deviation", "provider": provider_name,
                "compared": j.height, "max_dev_pct": round(float(j["dev"].max() or 0), 3),
                "deviations": [{"symbol": r["symbol"], "local": r["close"], "other": r["other_close"],
                                "dev_pct": round(r["dev"], 2)} for r in bad.rows(named=True)[:SAMPLE]]}
    except Exception as e:
        return {"status": "unavailable", "reason": type(e).__name__, "provider": provider_name}


def run(data_dir, *, loop=None, now: datetime | None = None, enqueue=None) -> dict:
    """盘后调度入口: 三类检查 + 有问题推飞书 (按日去重)。"""
    from app.services import data_integrity

    now = now or us_clock.now()
    day = us_clock.previous_trading_day(now.date()) if not us_clock.is_trading_day(now.date()) \
        or now.time() < time(16, 0) else now.date()
    keys = key_symbols(data_dir, loop)
    integrity = data_integrity.scan_recent_integrity(Path(data_dir), today=now.date())
    observed: list[str] = []
    numeric = numeric_issues(data_dir, day, keys=keys, alert=alert_symbols(data_dir, keys), observed=observed)
    cross = cross_check(data_dir, day, keys)
    report = {"day": day.isoformat(), "at": now.isoformat(timespec="seconds"),
              "integrity": [{"day": i.day.isoformat(), "table": i.table, "kind": i.kind} for i in integrity],
              "numeric": numeric, "cross": cross,
              "observed": {"jumps": observed},
              "ok": not integrity and not numeric and cross["status"] != "deviation"}
    if not report["ok"]:
        if enqueue is None:
            from app.services import notify_opportunity as _no

            def _default(key, payload):
                return _no._enqueue(data_dir, key, payload)
            enqueue = _default
        enqueue(f"dq:{day}", {"title": "ZenTrade · 数据质量", "subtitle": day.isoformat(),
                              "body": "\n".join(report_lines(report))})
    try:
        import json as _json

        from app.services.fs_utils import atomic_write_text
        d = Path(data_dir) / "ops"
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_text(d / "data_quality.json", _json.dumps(report, ensure_ascii=False, default=str))
    except Exception as e:
        logger.warning("data quality: 报告落盘失败: %s", type(e).__name__)
    logger.info("data quality %s: ok=%s integrity=%d numeric=%d cross=%s",
                day, report["ok"], len(integrity), len(numeric), cross["status"])
    return report


def report_lines(report: dict) -> list[str]:
    lines = []
    for i in report["integrity"]:
        lines.append(f"- {i['day']} {i['table']}：" + ("分区缺失" if i["kind"] == "missing" else "盘中快照残留"))
    for n in report["numeric"]:
        syms = ("：" + "、".join(str(s) for s in n["symbols"])) if n.get("symbols") else ""
        count = f"（{n['n']} 只）" if n.get("n") else ""
        lines.append(f"- {n['detail']}{count}{syms}")
    c = report["cross"]
    if c["status"] == "deviation":
        lines.append("- 两源收盘价不一致：" + "、".join(
            f"{d['symbol']} 本地 {d['local']} / {c.get('provider')} {d['other']}（差 {d['dev_pct']}%）"
            for d in c["deviations"]))
    if not lines:
        lines.append("无异常")
    return lines


def summary(report: dict) -> str:
    """系统状态页一句话。"""
    c = report["cross"]
    parts = [f"{report['day']} 日K"]
    parts.append("完整性正常" if not report["integrity"] else f"完整性 {len(report['integrity'])} 处异常")
    parts.append("数值自洽" if not report["numeric"] else f"数值 {len(report['numeric'])} 类异常")
    if c["status"] == "ok":
        parts.append(f"对价 {c['compared']} 只一致（最大偏差 {c['max_dev_pct']}%）")
    elif c["status"] == "deviation":
        parts.append(f"对价 {len(c['deviations'])} 只不一致（最大 {c['max_dev_pct']}%）")
    else:
        parts.append(f"对价不可用（{c.get('reason')}）")
    return "；".join(parts)


def latest(data_dir) -> dict | None:
    import json as _json
    try:
        return _json.loads((Path(data_dir) / "ops" / "data_quality.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
