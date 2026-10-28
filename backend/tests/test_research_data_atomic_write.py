"""BH-04: 退市股研究缓存 (list.json / bars.parquet / names.json) 必须原子写。

以前直接覆写目标文件: 每周刷新写到一半进程被杀 (部署重启/OOM/断电) 就留下半截文件,
之后 refresh_delisted / build_v3_dataset 读它恒抛错, evaluation_schedule 把异常当软失败
吞掉, v3 评估静默退回无退市样本的数据 (幸存者偏差), 且不自愈。
这里让写入「写一半就失败」, 断言旧文件原封不动、仍可读。不联网: 密钥与两个拉取函数全部打桩。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import polars as pl
import pytest

from app.services import research_data as rd


@pytest.fixture()
def delisted_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = rd.research_root(tmp_path) / "delisted"
    root.mkdir(parents=True)
    old_list = [{"ticker": "AAA", "delisted_utc": "2023-01-01T00:00:00Z", "primary_exchange": "XNYS"}]
    (root / "list.json").write_text(json.dumps(old_list))
    (root / "names.json").write_text(json.dumps({"AAA~20230101": ["Old", "CS"]}))
    pl.DataFrame({  # 列顺序与 refresh_delisted 拼出来的 wide 一致 (symbol..volume, rsym, close_adj)
        "symbol": ["AAA"] * 3,
        "date": pl.date_range(pl.date(2022, 12, 28), pl.date(2022, 12, 30), eager=True),
        "open": [1.0] * 3, "high": [1.0] * 3, "low": [1.0] * 3, "close": [1.0] * 3,
        "volume": [1.0] * 3, "rsym": ["AAA~20230101"] * 3, "close_adj": [1.0] * 3,
    }).write_parquet(root / "bars.parquet")

    monkeypatch.setattr(rd, "_secret", lambda name: "dummy")
    new_row = {"ticker": "BBB", "delisted_utc": "2023-06-01T00:00:00Z", "primary_exchange": "XNAS"}
    monkeypatch.setattr(rd, "_reference", lambda key, active: [] if active else [*old_list, new_row])

    def _daily(headers, symbols, start, end, adj):
        return [{"symbol": "BBB", "date": "2023-05-31", "open": 2.0, "high": 2.0, "low": 2.0,
                 "close": 2.0, "volume": 10.0}]

    monkeypatch.setattr(rd, "_alpaca_daily", _daily)
    monkeypatch.setattr(rd.time, "sleep", lambda s: None)
    return root


def _torn_parquet(monkeypatch: pytest.MonkeyPatch) -> None:
    """DataFrame.write_parquet 写出前一半字节后抛错 (模拟中途被杀)。"""
    real = pl.DataFrame.write_parquet

    def _torn(self, file, *args, **kwargs):
        real(self, file, *args, **kwargs)
        p = Path(file)
        data = p.read_bytes()
        p.write_bytes(data[: len(data) // 2])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", _torn)


def _torn_text(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Path.write_text 写到指定文件名 (或其 .tmp) 时只写前 4 个字节然后抛错。"""
    real = Path.write_text

    def _torn(self: Path, data: str, *args, **kwargs):
        if self.name.startswith(name):
            real(self, data[:4], *args, **kwargs)
            raise OSError(28, "No space left on device")
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _torn)


def _stale_list(root: Path) -> None:
    stale = time.time() - 30 * 86400
    os.utime(root / "list.json", (stale, stale))  # 名单过期 → 重拉名单, 带出新退市股 BBB 需要补拉日K


def test_torn_bars_write_keeps_old_parquet(delisted_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stale_list(delisted_root)
    _torn_parquet(monkeypatch)
    with pytest.raises(OSError):
        rd.refresh_delisted(delisted_root.parents[2])
    bars = pl.read_parquet(delisted_root / "bars.parquet")  # 修前: File out of specification
    assert bars["rsym"].to_list() == ["AAA~20230101"] * 3


def test_torn_list_write_keeps_old_list(delisted_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lst = delisted_root / "list.json"
    _stale_list(delisted_root)
    _torn_text(monkeypatch, "list.json")
    with pytest.raises(OSError):
        rd.refresh_delisted(delisted_root.parents[2])
    assert [r["ticker"] for r in json.loads(lst.read_text())] == ["AAA"]


def test_torn_names_write_keeps_old_names(delisted_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _torn_text(monkeypatch, "names.json")
    with pytest.raises(OSError):
        rd.refresh_delisted(delisted_root.parents[2])
    assert json.loads((delisted_root / "names.json").read_text()) == {"AAA~20230101": ["Old", "CS"]}


def test_refresh_still_writes_normally(delisted_root: Path) -> None:
    lst = delisted_root / "list.json"
    _stale_list(delisted_root)
    out = rd.refresh_delisted(delisted_root.parents[2])
    assert out["status"] == "ok"
    assert {r["ticker"] for r in json.loads(lst.read_text())} == {"AAA", "BBB"}
    bars = pl.read_parquet(delisted_root / "bars.parquet")
    assert set(bars["rsym"].unique().to_list()) == {"AAA~20230101", "BBB~20230601"}
    assert not list(delisted_root.glob("*.tmp"))
