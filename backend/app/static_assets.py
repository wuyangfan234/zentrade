"""前端构建产物 (/assets) 的静态服务: gzip 传输 + 长缓存。

Vite 产物文件名带内容哈希 (内容变 → 文件名变), 因此可以 `immutable` 长缓存;
index.html 另由 spa_fallback 以 no-store 返回, 新版本发布后浏览器总能拿到新文件名。

- 只在 GET、客户端声明接受 gzip、非 Range 请求、文本类且 ≥1KB 时压缩;
  压缩结果按 (路径, mtime_ns, size) 缓存在进程内, 压缩在线程池里做, 不阻塞事件循环。
- gzip 变体使用弱 ETag (W/"..."): 同一内容的不同编码不是字节一致的强校验;
  Starlette 的 is_not_modified 会剥掉 W/ 前缀比较, 条件请求照常 304。
- Range / HEAD / 不接受 gzip 的请求原样走 FileResponse (含 206 分段)。
"""
from __future__ import annotations

import gzip
import os
import threading

import anyio.to_thread
from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

IMMUTABLE = "public, max-age=31536000, immutable"
_COMPRESSIBLE = {".js", ".mjs", ".css", ".svg", ".json", ".map", ".html", ".txt", ".wasm"}
_MIN_SIZE = 1024
_MAX_SIZE = 8 * 1024 * 1024
_MAX_ENTRIES = 512


def _accepts_gzip(accept_encoding: str) -> bool:
    """Accept-Encoding 显式列出 gzip 且 q≠0 才压缩 (`gzip;q=0` 表示拒绝)。"""
    for part in accept_encoding.lower().split(","):
        token, _, params = part.partition(";")
        if token.strip() != "gzip":
            continue
        q = 1.0
        for p in params.split(";"):
            k, _, v = p.partition("=")
            if k.strip() == "q":
                try:
                    q = float(v.strip())
                except ValueError:
                    q = 0.0
        return q > 0
    return False


def _compressible(full_path: str, size: int) -> bool:
    return os.path.splitext(full_path)[1].lower() in _COMPRESSIBLE and _MIN_SIZE <= size <= _MAX_SIZE


class AssetStaticFiles(StaticFiles):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._gz_cache: dict[str, tuple[int, int, bytes]] = {}
        self._gz_lock = threading.Lock()

    def file_response(self, full_path, stat_result, scope: Scope, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        if response.status_code in (200, 304):
            response.headers["cache-control"] = IMMUTABLE
            # 304 须带上 200 会发的 Vary (RFC 9110 §15.4.5); 200 的 Vary 在 get_response 里按同一条件补
            if response.status_code == 304 and _compressible(os.fspath(full_path), stat_result.st_size):
                response.headers["vary"] = "Accept-Encoding"
        return response

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if not isinstance(response, FileResponse) or response.status_code != 200:
            return response
        full_path = os.fspath(response.path)
        st = response.stat_result
        if st is None or not _compressible(full_path, st.st_size):
            return response
        # 可压缩资源的原样响应同样声明 Vary, 共享缓存 (Cloudflare 等) 才会按编码分开存
        response.headers["vary"] = "Accept-Encoding"
        req = Headers(scope=scope)
        if scope.get("method") != "GET" or not _accepts_gzip(req.get("accept-encoding", "")) or "range" in req:
            return response
        body = await anyio.to_thread.run_sync(self._gzip_bytes, full_path, st.st_mtime_ns, st.st_size)
        headers = {
            "content-encoding": "gzip",
            "vary": "Accept-Encoding",
            "cache-control": IMMUTABLE,
            "last-modified": response.headers["last-modified"],
            "etag": "W/" + response.headers["etag"],
        }
        return Response(content=body, status_code=200, headers=headers,
                        media_type=response.headers.get("content-type"))

    def _gzip_bytes(self, full_path: str, mtime_ns: int, size: int) -> bytes:
        with self._gz_lock:
            hit = self._gz_cache.get(full_path)
            if hit is not None and hit[0] == mtime_ns and hit[1] == size:
                return hit[2]
        with open(full_path, "rb") as f:
            raw = f.read()
        body = gzip.compress(raw, compresslevel=6, mtime=0)
        with self._gz_lock:
            if len(self._gz_cache) >= _MAX_ENTRIES:
                self._gz_cache.clear()
            self._gz_cache[full_path] = (mtime_ns, size, body)
        return body
