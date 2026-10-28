"""10-11: 开机完整性检查每次都把 10-05~10-09 判为「停机前盘中快照」并重修。

根因: 日K批量同步前的裸符号补全 (`kline_sync._normalize_bare_symbols`, A 股时代为 600000→600000.SH
写的) 按 "." 前缀建索引。美股优先股/分级股本身带点 (BAC.PRK、ABR.PRD、BRK.B), 于是 BAC、C、BA、ABR
这类有优先股的普通股匹配到多个符号 → 判为"无唯一匹配"被跳过, batch 永远不拉它们。它们分区里只剩实时
行情写的行 (quote_ts 早于收盘线), 完整性检查每次开机都判快照 → 修复 → 又被跳过 → 下次开机再修。
修法: 裸符号在维表里有完全同名的条目时直接用它 (美股普通股); 仍无完全同名且多个后缀候选的 (A 股
830001.BJ/.SH) 照旧跳过。
"""
from __future__ import annotations

from datetime import date, datetime, time

import polars as pl

from app.market_time import CN_TZ
from app.services import kline_sync
from app.services.data_integrity import scan_recent_integrity
from app.storage.repository import DataStore, KlineRepository

TODAY = date(2026, 10, 12)      # 周一
FRIDAY = date(2026, 10, 9)


def _ts_ms(day: date, t: time) -> int:
    return int(datetime.combine(day, t, tzinfo=CN_TZ).timestamp() * 1000)


def _write_instruments(data_dir, symbols):
    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols}).write_parquet(out)


def _row(symbol, day, quote_ts, close=10.0, volume=1000.0):
    return {"symbol": symbol, "date": day, "open": close, "high": close, "low": close, "close": close,
            "volume": volume, "amount": close * volume, "quote_ts": quote_ts}


def _write_partition(data_dir, day, rows):
    part = data_dir / "kline_daily" / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema_overrides={"quote_ts": pl.Int64}).write_parquet(part / "part.parquet")


class _Provider:
    """批量源: 对请求到的每个符号返回官方日K (无 quote_ts = 权威历史)。"""

    def __init__(self, day):
        self.day = day
        self.requested: list[str] = []

    def iter_daily(self, symbols, *args, **kwargs):
        self.requested = list(symbols)
        yield pl.DataFrame([{k: v for k, v in _row(s, self.day, None, close=11.0, volume=5e6).items() if k != "quote_ts"}
                            for s in symbols])


def _route(monkeypatch, provider):
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "fuyao")
    from app.data_providers import custom
    monkeypatch.setattr(custom, "provider_has_dataset", lambda name, dataset: name == "fuyao" and dataset == "daily")
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)


def test_us_common_stock_with_preferred_listing_resolves_to_itself(tmp_path):
    _write_instruments(tmp_path, ["BAC", "BAC.PRK", "BAC.PRN", "C", "C.PRN", "BRK.B", "MSFT", "830001.BJ", "830001.SH"])

    out, resolved, skipped = kline_sync._normalize_bare_symbols(["BAC", "C", "MSFT", "BRK.B", "830001"], tmp_path)

    assert out == ["BAC", "C", "MSFT", "BRK.B"]
    assert resolved == {"BAC": "BAC", "C": "C", "MSFT": "MSFT"}
    assert skipped == ["830001"]        # 无完全同名、多个后缀候选 → 仍跳过


def test_real_snapshot_is_repaired_and_repaired_partition_is_not_flagged_again(monkeypatch, tmp_path):
    _write_instruments(tmp_path, ["BAC", "BAC.PRK", "MSFT"])
    # 周五分区: MSFT 已是 batch 权威行; BAC 只有停机前 13:25 的实时行 (真快照)
    _write_partition(tmp_path, FRIDAY, [_row("BAC", FRIDAY, _ts_ms(FRIDAY, time(13, 25))),
                                        _row("MSFT", FRIDAY, None)])
    _write_partition(tmp_path, TODAY, [_row("MSFT", TODAY, None)])
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert [(i.day, i.kind) for i in issues] == [(FRIDAY, "snapshot")]   # 真快照仍被识别

    provider = _Provider(FRIDAY)
    _route(monkeypatch, provider)
    kline_sync.sync_and_persist_daily_batch(
        ["BAC", "MSFT"], KlineRepository(DataStore(tmp_path)), object(),
        start_date=datetime(2026, 10, 9), end_date=datetime(2026, 10, 9, 23),
    )

    assert "BAC" in provider.requested
    fixed = pl.read_parquet(tmp_path / "kline_daily" / f"date={FRIDAY.isoformat()}" / "part.parquet")
    bac = fixed.filter(pl.col("symbol") == "BAC")
    assert bac["close"].to_list() == [11.0] and bac["quote_ts"].null_count() == 1
    assert scan_recent_integrity(tmp_path, today=TODAY) == []           # 修好后下次开机不再重修


# 10-11 部署后读回: 修复跑完 10-06~10-09 已干净, 10-05 仍被判快照 —— 分区里只剩 HOLO、TOP 两行
# 13:24 ET 的实时行 (有成交), 这两个符号不在 instruments 维表 (batch 标的池 = 维表 + 自选 + 核心 ETF),
# batch 永远补不到它们 → 每次开机仍判快照重修。不在标的池的孤儿实时行, 在已有 batch 权威行的
# 股票日K分区里不应让整分区反复重修; 在标的池内的实时行 / 整分区都是实时行 / 维表缺失时照旧判快照。


def test_orphan_realtime_rows_outside_batch_universe_do_not_flag_repaired_partition(tmp_path):
    _write_instruments(tmp_path, ["BAC", "MSFT"])
    _write_partition(tmp_path, FRIDAY, [_row("BAC", FRIDAY, None), _row("MSFT", FRIDAY, None),
                                        _row("HOLO", FRIDAY, _ts_ms(FRIDAY, time(13, 24)), volume=91174.0),
                                        _row("TOP", FRIDAY, _ts_ms(FRIDAY, time(13, 24)), volume=26274.0)])
    _write_partition(tmp_path, TODAY, [_row("MSFT", TODAY, None)])

    assert scan_recent_integrity(tmp_path, today=TODAY) == []


def test_universe_member_realtime_row_is_still_a_snapshot_even_with_orphans(tmp_path):
    _write_instruments(tmp_path, ["BAC", "MSFT"])
    _write_partition(tmp_path, FRIDAY, [_row("BAC", FRIDAY, _ts_ms(FRIDAY, time(13, 25))), _row("MSFT", FRIDAY, None),
                                        _row("HOLO", FRIDAY, _ts_ms(FRIDAY, time(13, 24)))])
    _write_partition(tmp_path, TODAY, [_row("MSFT", TODAY, None)])

    assert [(i.day, i.kind) for i in scan_recent_integrity(tmp_path, today=TODAY)] == [(FRIDAY, "snapshot")]


def test_all_realtime_partition_with_only_orphans_is_still_a_snapshot(tmp_path):
    _write_instruments(tmp_path, ["BAC", "MSFT"])
    _write_partition(tmp_path, FRIDAY, [_row("HOLO", FRIDAY, _ts_ms(FRIDAY, time(13, 24)))])
    _write_partition(tmp_path, TODAY, [_row("MSFT", TODAY, None)])

    assert [(i.day, i.kind) for i in scan_recent_integrity(tmp_path, today=TODAY)] == [(FRIDAY, "snapshot")]


def test_missing_instruments_keeps_old_fail_closed_behaviour(tmp_path):
    _write_partition(tmp_path, FRIDAY, [_row("BAC", FRIDAY, None), _row("MSFT", FRIDAY, None),
                                        _row("HOLO", FRIDAY, _ts_ms(FRIDAY, time(13, 24)))])
    _write_partition(tmp_path, TODAY, [_row("MSFT", TODAY, None)])

    assert [(i.day, i.kind) for i in scan_recent_integrity(tmp_path, today=TODAY)] == [(FRIDAY, "snapshot")]
