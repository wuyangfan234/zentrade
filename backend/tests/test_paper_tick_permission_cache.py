"""10-09 盘中撮合热点回归: v4 模式不读 v1 证据文件; 自选名单按文件签名缓存且改动立即生效。"""
import json
import os

from app.config import settings
from app.services import decision_scan_job as job
from app.services import strategy_evaluation as se
from app.services import watchlist


def test_v4_mode_does_not_read_v1_evaluation(tmp_path, monkeypatch):
    d = tmp_path / "decision_desk"
    d.mkdir(parents=True)
    (d / "evidence_mode.json").write_text(json.dumps({"mode": "v4"}))
    (d / "evaluation.json").write_text(json.dumps({"sources": {"x": {}}}))
    read = []
    orig = se._read
    monkeypatch.setattr(se, "_read", lambda p: (read.append(p.name), orig(p))[1])
    monkeypatch.setattr(se, "_load_v4", lambda data_dir: {"sources": {}})
    assert se.load_evaluation(tmp_path) == {"sources": {}}
    assert "evaluation.json" not in read


def test_v1_mode_still_reads_v1(tmp_path):
    d = tmp_path / "decision_desk"
    d.mkdir(parents=True)
    (d / "evaluation.json").write_text(json.dumps({"sources": {"good": {"n": 1}}}))
    assert se.load_evaluation(tmp_path)["sources"]["good"] == {"n": 1}


def test_watchlist_cached_and_invalidated_on_change(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    job._WL_CACHE.clear()
    watchlist.add("AAPL")
    calls = []
    orig = watchlist.list_symbols
    monkeypatch.setattr(watchlist, "list_symbols", lambda: (calls.append(1), orig())[1])
    assert job._watchlist() == {"AAPL"}
    assert job._watchlist() == {"AAPL"}
    assert len(calls) == 1                         # 第二次命中缓存
    monkeypatch.setattr(watchlist, "list_symbols", orig)
    watchlist.add("MSFT")
    p = watchlist._path()
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))  # 保证签名变化
    assert job._watchlist() == {"AAPL", "MSFT"}     # 改动立即生效


def test_watchlist_cache_returns_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    job._WL_CACHE.clear()
    watchlist.add("AAPL")
    a = job._watchlist()
    a.add("HACK")
    assert job._watchlist() == {"AAPL"}


def test_watchlist_read_failure_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    job._WL_CACHE.clear()
    monkeypatch.setattr(watchlist, "list_symbols", lambda: (_ for _ in ()).throw(OSError("boom")))
    assert job._watchlist() == set()
