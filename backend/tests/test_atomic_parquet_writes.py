"""持久化 parquet 的原地覆盖写 — 写到一半被中断时, 旧文件必须保持完好。

仓库里分区 parquet 的写入已经统一走「临时文件 + 替换」(repository / kline_sync 的
`_atomic_write_parquet`, watchlist、depth_service、mining 等同款), 注释写明原因:
直接 `write_parquet(out)` 在进程被 kill (dev.sh 清端口用 kill -9)、断电时留下半截
文件, 之后读侧整条链路报错。下面这些持久化文件仍是直接覆盖写:

- ext_data.write_ext_parquet / fix_symbol_format — 用户上传/推送的扩展数据
- instrument_sync.sync_instruments — 标的维表

中断的模拟: 让 `DataFrame.write_parquet` 往目标路径写入半截字节后抛出异常, 等价于
进程在写入过程中被杀。修复后半截字节落在 `.tmp` 上, 目标文件不动。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.services import ext_data as ext_svc
from app.services import instrument_sync
from app.services.ext_data import ExtConfig, ExtField
from app.services.fs_utils import atomic_write_parquet

_real_write = pl.DataFrame.write_parquet


@pytest.fixture
def crash(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """armed=True 时, write_parquet 只写出半截字节就中断 (模拟 kill -9 / 断电)。"""
    state = SimpleNamespace(armed=False)

    def _write(self, file, *args, **kwargs):
        if state.armed:
            Path(file).write_bytes(b"PAR1\x15\x00truncated")
            raise OSError("simulated kill during write_parquet")
        return _real_write(self, file, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_parquet", _write)
    return state


def _seed(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _real_write(df, path)


def _no_tmp_left(path: Path) -> bool:
    return not path.with_name(path.name + ".tmp").exists()


# ---------- 公共实现 ----------


def test_atomic_write_parquet_replaces_target_and_cleans_up(tmp_path: Path) -> None:
    target = tmp_path / "part.parquet"
    _seed(pl.DataFrame({"v": [1]}), target)

    atomic_write_parquet(pl.DataFrame({"v": [2, 3]}), target)

    assert pl.read_parquet(target)["v"].to_list() == [2, 3]
    assert [p.name for p in tmp_path.iterdir()] == ["part.parquet"]


def test_interrupted_atomic_write_keeps_target_and_hides_tmp_from_globs(tmp_path: Path, crash) -> None:
    target = tmp_path / "date=2026-03-02" / "part.parquet"
    old = pl.DataFrame({"v": [1]})
    _seed(old, target)
    crash.armed = True

    with pytest.raises(OSError, match="simulated kill"):
        atomic_write_parquet(pl.DataFrame({"v": [2]}), target)

    assert_frame_equal(pl.read_parquet(target), old)
    # 半截的 .tmp 不会被 `**/*.parquet` 视图扫描到
    assert [p.name for p in tmp_path.glob("**/*.parquet")] == ["part.parquet"]


# ---------- 扩展数据 ----------


@pytest.mark.parametrize("mode", ["snapshot", "timeseries"])
def test_ext_write_interrupted_keeps_existing_rows(tmp_path: Path, crash, mode: str) -> None:
    config = ExtConfig(
        id="hot", label="人气", mode=mode,
        fields=[ExtField("symbol", "string"), ExtField("heat", "float")],
    )
    cfg_dir = tmp_path / "ext_data" / "hot"
    target = cfg_dir / "part.parquet" if mode == "snapshot" else (
        cfg_dir / "timeseries" / "date=2026-03-02" / "part.parquet"
    )
    old = pl.DataFrame({"symbol": ["BAC", "JPM"], "heat": [1.0, 2.0]})
    _seed(old, target)
    new = pl.DataFrame({"symbol": ["BAC"], "heat": [9.0]})

    crash.armed = True
    with pytest.raises(OSError, match="simulated kill"):
        ext_svc.write_ext_parquet(new, config, tmp_path, snapshot_date=date(2026, 3, 2))
    assert_frame_equal(pl.read_parquet(target), old)

    crash.armed = False
    ext_svc.write_ext_parquet(new, config, tmp_path, snapshot_date=date(2026, 3, 2))
    merged = dict(pl.read_parquet(target).select("symbol", "heat").iter_rows())
    assert merged == {"BAC": 9.0, "JPM": 2.0}
    assert _no_tmp_left(target)


def test_fix_symbol_format_interrupted_keeps_partition(tmp_path: Path, crash) -> None:
    config = ExtConfig(
        id="hot", label="人气", mode="timeseries",
        fields=[ExtField("symbol", "string"), ExtField("heat", "float")],
    )
    target = tmp_path / "ext_data" / "hot" / "timeseries" / "date=2026-03-02" / "part.parquet"
    old = pl.DataFrame({"symbol": ["BRKB", "BFB"], "heat": [1.0, 2.0]})
    _seed(old, target)
    # 裸代码只按 instruments 维表 code → symbol 映射 (旧市场 6 开头兜底规则已删除)
    inst = tmp_path / "instruments" / "instruments.parquet"
    inst.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"code": ["BRKB", "BFB"], "symbol": ["BRK.B", "BF.B"]}).write_parquet(inst)

    crash.armed = True
    assert ext_svc.fix_symbol_format(config, tmp_path) == 0  # 单文件失败按原逻辑记日志跳过
    assert_frame_equal(pl.read_parquet(target), old)

    crash.armed = False
    assert ext_svc.fix_symbol_format(config, tmp_path) == 1
    assert pl.read_parquet(target)["symbol"].to_list() == ["BRK.B", "BF.B"]
    assert _no_tmp_left(target)


# ---------- 标的维表 ----------


def _instruments(names: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"symbol": ["JPM", "BAC"], "name": names})


def test_sync_instruments_interrupted_keeps_existing_table(tmp_path: Path, crash, monkeypatch) -> None:
    target = tmp_path / "instruments" / "instruments.parquet"
    old = _instruments(["浦发银行", "平安银行"])
    _seed(old, target)
    monkeypatch.setattr(
        instrument_sync, "_fetch_instruments_via_provider",
        lambda: [{"symbol": "SCHW", "name": "招商银行"}],
    )

    crash.armed = True
    with pytest.raises(OSError, match="simulated kill"):
        instrument_sync.sync_instruments(tmp_path)
    assert_frame_equal(pl.read_parquet(target), old)

    crash.armed = False
    assert instrument_sync.sync_instruments(tmp_path) == 1
    assert pl.read_parquet(target)["symbol"].to_list() == ["SCHW"]
    assert _no_tmp_left(target)


# ---------- 财务表 ----------
