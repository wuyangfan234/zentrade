"""BH-05: secrets.json 的读-改-写必须串行, 并发保存不能丢掉别人的 Key。

FastAPI 同步端点跑在线程池里, 两个写密钥请求重叠时, 不加锁会各自基于旧快照整文件
覆盖 (后写者抹掉先写者的 Key), 共用的固定 secrets.json.tmp 还会让一方 os.replace 抛
FileNotFoundError。preferences.save 对同一问题已加 _SAVE_LOCK, 这里锁定 secrets_store
也一样。为了让竞态稳定复现, load() 读完后睡一小会儿, 把读与写之间的窗口撑大。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app import secrets_store


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    return tmp_path


@pytest.fixture()
def slow_load(monkeypatch: pytest.MonkeyPatch) -> None:
    real = secrets_store.load

    def _slow():
        out = real()
        time.sleep(0.02)
        return out

    monkeypatch.setattr(secrets_store, "load", _slow)


def _run_parallel(fns) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(fns))

    def _wrap(fn):
        barrier.wait()
        try:
            fn()
        except BaseException as e:  # 收集给断言
            errors.append(e)

    threads = [threading.Thread(target=_wrap, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return errors


def test_concurrent_saves_keep_every_key(data_dir: Path, slow_load) -> None:
    keys = [f"key_{i}" for i in range(8)]
    errors = _run_parallel([lambda k=k: secrets_store.save({k: f"v-{k}"}) for k in keys])
    assert errors == []
    stored = secrets_store.load()
    assert {k: stored.get(k) for k in keys} == {k: f"v-{k}" for k in keys}


def test_concurrent_save_and_clear_do_not_lose_unrelated_keys(data_dir: Path, slow_load) -> None:
    secrets_store.save({"keep": "1", "drop": "2"})
    errors = _run_parallel([
        lambda: secrets_store.clear("drop"),
        lambda: secrets_store.save({"added": "3"}),
    ])
    assert errors == []
    assert secrets_store.load() == {"keep": "1", "added": "3"}
