"""SPA 兜底路由的静态根文件放行 (品牌改版回归)。

上游 spa_fallback 对一切未匹配路径返回 index.html —— dist 根目录的 public/
资源 (favicon.svg) 永远拿到 HTML, 浏览器标签图标失效。修复后: _static 内单层、
非 index.html 的现存文件原样返回; 其余 (前端路由、穿越尝试、不存在文件)
仍回退 index.html。
"""
from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch) -> TestClient:
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>ZenTrade</title>", encoding="utf-8")
    (static / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")

    from app.config import settings
    monkeypatch.setattr(settings, "static_dir", static)
    import app.main as main_mod
    main_mod = importlib.reload(main_mod)  # 重新注册 spa_fallback (模块级读 static_dir)
    return TestClient(main_mod.app)


def test_root_static_file_served_as_is(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch)
    r = c.get("/favicon.svg")
    assert r.status_code == 200
    assert r.text.startswith("<svg")
    assert "svg" in r.headers["content-type"]


def test_spa_route_falls_back_to_index(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch)
    for path in ("/", "/watchlist", "/settings", "/not-a-file.svg"):
        r = c.get(path)
        assert r.status_code == 200
        assert "<title>ZenTrade</title>" in r.text, path
        assert r.headers["cache-control"].startswith("no-store")


def test_traversal_never_escapes_static_root(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch)
    for path in ("/../secret.txt", "/..%2Fsecret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
        r = c.get(path)
        assert "TOP-SECRET" not in r.text, path
