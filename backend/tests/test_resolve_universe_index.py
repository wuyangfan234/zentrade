"""_resolve_universe 指数过滤测试。"""
import pytest

from app.jobs import daily_pipeline
from app.storage.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def test_resolve_universe_excludes_index_symbols(repo, monkeypatch, tmp_path):
    """自选里的指数不进入股票日K/分钟K同步池。"""
    class _Capset:
        def has(self, cap):
            return False

    # 厂商 pools 已退役: universe = watchlist + instruments
    monkeypatch.setattr(
        "app.services.watchlist.list_symbols",
        lambda: [{"symbol": "JPM"}, {"symbol": "SPX"}],
    )
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: {"SPX"})

    universe = daily_pipeline._resolve_universe(_Capset(), repo)
    assert "JPM" in universe
    assert "SPX" not in universe
