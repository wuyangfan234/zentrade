"""/assets 静态资源的传输与缓存 (2026-10-07 性能审查)。

生产实测: /assets 下 JS 不压缩 (echarts 1MB 原样传) 且无 Cache-Control, 每次进页面
对 11~29 个 chunk 逐个发条件请求 (304)。Vite 产物文件名带内容哈希, 内容变 → 文件名变,
因此可以长缓存 (immutable) + gzip 传输; index.html 仍 no-store (见 test_spa_static_root)。
"""
from __future__ import annotations

import gzip
import importlib

from fastapi.testclient import TestClient

BODY = ("export const x = '" + "ZenTrade " * 400 + "';\n").encode()


def _client(tmp_path, monkeypatch) -> TestClient:
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>ZenTrade</title>", encoding="utf-8")
    (static / "assets" / "index-AbC123_x.js").write_bytes(BODY)
    from app.config import settings
    monkeypatch.setattr(settings, "static_dir", static)
    import app.main as main_mod
    main_mod = importlib.reload(main_mod)
    return TestClient(main_mod.app)


def test_hashed_asset_is_gzipped_and_immutable(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch)
    r = c.get("/assets/index-AbC123_x.js", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.content == BODY  # 解压后与原文件逐字节一致
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "accept-encoding" in r.headers.get("vary", "").lower()


def test_asset_without_gzip_support_is_identity(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch)
    r = c.get("/assets/index-AbC123_x.js", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.content == BODY
    assert "immutable" in r.headers["cache-control"]
    assert r.headers.get("vary") == "Accept-Encoding"  # 共享缓存 (Cloudflare) 必须按编码分开存


def test_gzip_body_on_the_wire_is_smaller(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch)
    with c.stream("GET", "/assets/index-AbC123_x.js", headers={"Accept-Encoding": "gzip"}) as r:
        raw = b"".join(r.iter_raw())
    assert gzip.decompress(raw) == BODY
    assert len(raw) < len(BODY) // 5


def test_missing_asset_is_404_and_not_cached(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch)
    r = c.get("/assets/nope-12345678.js", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 404
    assert "immutable" not in r.headers.get("cache-control", "")


def test_conditional_request_still_304(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch)
    etag = c.get("/assets/index-AbC123_x.js").headers["etag"]
    r = c.get("/assets/index-AbC123_x.js", headers={"If-None-Match": etag, "Accept-Encoding": "gzip"})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers.get("vary") == "Accept-Encoding"  # RFC 9110 §15.4.5: 304 带上 200 会发的 Vary


def test_range_request_is_not_compressed(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch)
    r = c.get("/assets/index-AbC123_x.js", headers={"Range": "bytes=0-99", "Accept-Encoding": "gzip"})
    assert r.status_code == 206
    assert "content-encoding" not in r.headers
    assert r.content == BODY[:100]


def test_index_html_still_no_store(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch)
    r = c.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["cache-control"].startswith("no-store")


def test_gzip_explicitly_refused_is_identity(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch)
    for ae in ("gzip;q=0, identity", "br, gzip; q=0.0", "identity"):
        r = c.get("/assets/index-AbC123_x.js", headers={"Accept-Encoding": ae})
        assert "content-encoding" not in r.headers, ae
        assert r.content == BODY
    r = c.get("/assets/index-AbC123_x.js", headers={"Accept-Encoding": "br;q=1.0, gzip;q=0.8, *;q=0.1"})
    assert r.headers["content-encoding"] == "gzip"
