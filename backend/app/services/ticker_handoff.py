"""代码换主 (ticker hand-off) 的除权污染过滤。

2026-10-01 生产核账实证: 同一个代码先被 A 公司让出 (改用别的代码), 后被另一家公司 B 接手
时, Alpaca adjustment=all 的历史序列会把 A 的分红/拆股套在 B 的价格上 (GOLD: Barrick 的
18 次分红套到 A-Mark; CBIO: 前任的特别分红让复权价偏差 6 倍; 另有 CR/DOO/FFR/RBC)。
单只请求与批量请求返回的调整也不一致 (CALY 批量干净、单只被污染), 不能信任源端口径。

判定 (只用 Alpaca 公司行动数据, 不猜):
- 让出: name_change old_symbol=S → 其他代码, 让出方发行人 = old_cusip 前 6 位
- 接手: name_change new_symbol=S (来自其他代码), 时间不早于让出, 发行人与让出方不同
- 外来行动: 列在 S 名下、发行人属于让出方、除权日早于接手日的行动
- 若同一天现任发行人自己也有行动, 保留 (不误删真实事件)
同一发行人的 CUSIP 变更 / when-issued 往返 / 改名都不是换主。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from urllib.parse import urlencode

_CHUNK = 100
_ACTION_TYPES = "cash_dividend,stock_dividend,forward_split,reverse_split,unit_split,spin_off"


def _issuer(cusip: str | None) -> str:
    return (cusip or "")[:6]


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _handoffs(names: Iterable[Mapping], symbols: Iterable[str]) -> dict[str, tuple[date, str, set[str]]]:
    """symbol → (接手日, 现任发行人, 让出方发行人集合)。"""
    names = [n for n in names if isinstance(n, Mapping)]
    out: dict[str, tuple[date, str, set[str]]] = {}
    for sym in set(symbols):
        departures = [n for n in names if n.get("old_symbol") == sym and n.get("new_symbol") != sym]
        arrivals = [n for n in names if n.get("new_symbol") == sym and n.get("old_symbol") != sym]
        best: tuple[date, str, set[str]] | None = None
        for arr in arrivals:
            at = _day(arr.get("process_date"))
            current = _issuer(arr.get("new_cusip"))
            if at is None or not current:
                continue
            departed = {_issuer(d.get("old_cusip")) for d in departures
                        if (_day(d.get("process_date")) or date.max) <= at and _issuer(d.get("old_cusip")) not in ("", current)}
            if departed and (best is None or at > best[0]):
                best = (at, current, departed)
        if best is not None:
            out[sym] = best
    return out


def _action_rows(payload: Mapping) -> list[Mapping]:
    return [row for group, rows in payload.items() if group != "name_changes" and isinstance(rows, list)
            for row in rows if isinstance(row, Mapping)]


def foreign_action_dates(payload: Mapping, symbols: Iterable[str]) -> dict[str, set[date]]:
    """纯函数: 公司行动原始分组 → 每个代码需要剔除的外来除权日 (空集合不返回)。"""
    handoffs = _handoffs(payload.get("name_changes") or [], symbols)
    rows = _action_rows(payload)
    out: dict[str, set[date]] = {}
    for sym, (arrival, current, departed) in handoffs.items():
        mine = [r for r in rows if (r.get("symbol") or r.get("source_symbol")) == sym]
        own = {_day(r.get("ex_date")) for r in mine if _issuer(r.get("cusip") or r.get("source_cusip")) == current}
        foreign = {_day(r.get("ex_date")) for r in mine
                   if _issuer(r.get("cusip") or r.get("source_cusip")) in departed
                   and (_day(r.get("ex_date")) or date.max) < arrival}
        bad = {d for d in foreign - own if d is not None}
        if bad:
            out[sym] = bad
    return out


def fetch_corporate_actions(symbols: list[str], start: date, end: date, *, types: str) -> dict:
    """按代码分块拉 Alpaca 公司行动并合并分组 (分页拉全; 失败直接抛出, 调用方 fail-closed)。"""
    from app.services.corporate_actions import API_URL, _default_http_get

    merged: dict[str, list] = {}
    for i in range(0, len(symbols), _CHUNK):
        chunk = symbols[i:i + _CHUNK]
        token: str | None = None
        for _ in range(100):
            params = {"symbols": ",".join(chunk), "types": types, "start": start.isoformat(),
                      "end": end.isoformat(), "limit": 1000}
            if token:
                params["page_token"] = token
            body = _default_http_get(f"{API_URL}?{urlencode(params)}")
            for group, rows in (body.get("corporate_actions") or {}).items():
                merged.setdefault(group, []).extend(rows or [])
            token = body.get("next_page_token")
            if not token:
                break
        else:
            raise RuntimeError("公司行动分页超过上限")
    return merged


def foreign_dates_for(symbols: Iterable[str], start: date, end: date) -> dict[str, set[date]]:
    """两段查询: 先查改名找出换主代码, 只对它们再拉分红/拆股等行动。"""
    symbols = sorted({s for s in symbols if s})
    if not symbols:
        return {}
    names = fetch_corporate_actions(symbols, start, end, types="name_change").get("name_changes") or []
    candidates = sorted(_handoffs(names, symbols))
    if not candidates:
        return {}
    actions = fetch_corporate_actions(candidates, start, end, types=_ACTION_TYPES)
    return foreign_action_dates({**actions, "name_changes": names}, candidates)


def purge_handoff_events(data_dir, *, apply: bool = False, today: date | None = None) -> dict:
    """一次性清理已落盘因子表中的外来事件; apply 时写回、登记重算待办并重算受影响个股。"""
    from pathlib import Path

    import polars as pl

    from app.market_clock import us_clock
    from app.services import kline_sync

    factor_dir = Path(data_dir) / "adj_factor"
    table = pl.read_parquet(factor_dir / "all.parquet")
    if table.is_empty():
        return {"removed": 0, "symbols": []}
    start = table["trade_date"].min()
    foreign = foreign_dates_for(table["symbol"].unique().to_list(), start, today or us_clock.today())
    pairs = [(s, d) for s, days in foreign.items() for d in days]
    if not pairs:
        return {"removed": 0, "symbols": []}
    drop = pl.DataFrame(pairs, schema={"symbol": pl.Utf8, "trade_date": pl.Date}, orient="row")
    removed = table.join(drop, on=["symbol", "trade_date"], how="semi")
    report = {"removed": removed.height, "symbols": sorted(set(removed["symbol"].to_list())),
              "events": removed.sort(["symbol", "trade_date"]).to_dicts()}
    if not apply or removed.is_empty():
        return report
    kept = table.join(drop, on=["symbol", "trade_date"], how="anti")
    kline_sync._atomic_write_parquet(kept, factor_dir / "all.parquet")
    kline_sync.mark_rebuild_pending(factor_dir, report["symbols"], "ticker_handoff_purge")
    from app.indicators.pipeline import run_pipeline

    report["enriched_rows"] = run_pipeline(data_dir=Path(data_dir), symbols=report["symbols"])
    pending = kline_sync.rebuild_pending(factor_dir)
    if pending and pending.get("symbols") is not None and set(pending["symbols"]) <= set(report["symbols"]):
        kline_sync.clear_rebuild_pending(factor_dir)
    return report


if __name__ == "__main__":  # python -m app.services.ticker_handoff [--apply]
    import json
    import sys

    from app.config import settings

    result = purge_handoff_events(settings.data_dir, apply="--apply" in sys.argv)
    result["events"] = [{**e, "trade_date": str(e["trade_date"])} for e in result.get("events", [])]
    print(json.dumps(result, ensure_ascii=False, indent=1))
