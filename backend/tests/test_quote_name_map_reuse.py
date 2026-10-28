"""监控 name_map 测试 — 走 repo.get_name_map() memo, 过滤空名称与旧行为一致。"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.quote_service import _monitor_name_map


class _FakeRepo:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.calls = 0

    def get_name_map(self) -> dict[str, str]:
        self.calls += 1
        return dict(self._mapping)


def test_filters_falsy_names_like_legacy_build():
    repo = _FakeRepo({
        "JPM": "浦发银行",
        "BAC": "",          # 空名称: 旧 iter_rows 构建会跳过
        "DJI": None,        # None 名称: 同上
        "VOO": "沪深300ETF",
    })
    assert _monitor_name_map(repo) == {
        "JPM": "浦发银行",
        "VOO": "沪深300ETF",
    }


def test_merges_all_asset_types_from_repo_map():
    # get_name_map 已合并股票 + ETF + 指数 (股票优先), 监控回填无需再分表构建
    repo = _FakeRepo({"JPM": "股票", "VOO": "ETF", "SPX": "指数"})
    assert _monitor_name_map(repo) == repo._mapping


def test_delegates_to_repo_each_call():
    repo = _FakeRepo({"JPM": "浦发银行"})
    _monitor_name_map(repo)
    _monitor_name_map(repo)
    assert repo.calls == 2


def test_empty_repo_map_returns_empty():
    assert _monitor_name_map(_FakeRepo({})) == {}


def test_survives_repo_failure():
    # 与旧行为一致: name_map 构建失败不影响监控主流程 (调用方捕获)
    repo = SimpleNamespace()
    try:
        _monitor_name_map(repo)  # type: ignore[arg-type]
        raised = False
    except AttributeError:
        raised = True
    assert raised, "repo 无 get_name_map 时应抛出, 由调用方 try 兜底"
