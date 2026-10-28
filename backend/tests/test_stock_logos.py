"""0.38.0 自选股公司图标: 归一化、来源顺序、缓存状态与接口。全部离线 (HTTP 桩)。"""
from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.services import stock_logos as sl


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(sl, "_inflight", set())
    monkeypatch.setattr(sl, "_error_until", {})


def _png(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _transparent_glyph(color: tuple[int, int, int], size=(200, 120), hidden_rgb=(0, 0, 0)) -> bytes:
    """透明画布中间一个 80x60 的图形 (左上角挖一个透明小孔, 让它是"形状"而不是纯色块)。

    hidden_rgb = 透明像素里存的 RGB (很多 PNG 把透明像素存成白色)。
    """
    im = Image.new("RGBA", size, (*hidden_rgb, 0))
    for x in range(60, 140):
        for y in range(30, 90):
            hole = 65 <= x < 80 and 35 <= y < 45
            im.putpixel((x, y), (*hidden_rgb, 0) if hole else (*color, 255))
    return _png(im)


def _opaque_tile(bg=(0, 102, 204), fg=(255, 255, 255)) -> bytes:
    im = Image.new("RGB", (100, 100), bg)
    for x in range(30, 70):
        for y in range(30, 70):
            im.putpixel((x, y), fg)
    return _png(im)


def _decode(png: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(png))
    im.load()
    return im


# ---------------------------------------------------------------- 归一化

def test_white_transparent_glyph_gets_dark_tile():
    out = _decode(sl.normalize_image(_transparent_glyph((255, 255, 255))))
    assert out.size == (128, 128)
    assert out.getpixel((2, 2)) == sl.DARK_TILE  # 白字标不会在白底上消失
    assert out.getpixel((64, 64)) == (255, 255, 255)


def test_white_glyph_with_white_transparent_pixels_is_kept():
    # 透明像素存成 (255,255,255,0) 的白色字标: 丢掉 alpha 后整张都是白色, 不能当纯色占位丢掉
    out = sl.normalize_image(_transparent_glyph((255, 255, 255), hidden_rgb=(255, 255, 255)))
    assert out is not None
    assert _decode(out).getpixel((2, 2)) == sl.DARK_TILE


def test_dark_transparent_glyph_gets_light_tile():
    out = _decode(sl.normalize_image(_transparent_glyph((20, 20, 20))))
    assert out.getpixel((2, 2)) == sl.LIGHT_TILE
    assert out.getpixel((64, 64)) == (20, 20, 20)


def test_transparent_glyph_is_cropped_and_enlarged_with_safe_margin():
    # 原图 80x60 的图形只占 200x120 画布中间, 归一化后放大到安全区 (72% 宽), 四边留白不贴边
    out = _decode(sl.normalize_image(_transparent_glyph((200, 30, 30))))
    row = [out.getpixel((x, 64)) for x in range(128)]
    glyph_cols = [x for x, p in enumerate(row) if p == (200, 30, 30)]
    assert glyph_cols and glyph_cols[-1] - glyph_cols[0] + 1 >= int(128 * 0.70)
    assert glyph_cols[0] >= 12 and glyph_cols[-1] <= 115


def test_mostly_dark_glyph_with_white_details_stays_on_white():
    # 深色图形里夹白色细节 (如 SMCI 椭圆里的白字): 只要大部分在白底上看得见, 就统一白底
    im = Image.new("RGBA", (120, 60), (0, 0, 0, 0))
    for x in range(10, 110):
        for y in range(10, 50):
            im.putpixel((x, y), (255, 255, 255, 255) if 40 <= x < 60 else (30, 120, 60, 255))
    out = _decode(sl.normalize_image(_png(im)))
    assert out.getpixel((2, 2)) == sl.LIGHT_TILE


def _rounded_tile(with_glyph: bool) -> bytes:
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for x in range(100):
        for y in range(100):
            corner = (x < 6 or x > 93) and (y < 6 or y > 93)
            if not corner:
                inner = with_glyph and 35 <= x < 65 and 35 <= y < 65
                im.putpixel((x, y), (255, 255, 255, 255) if inner else (232, 43, 45, 255))
    return _png(im)


def test_filled_app_icon_is_full_bleed():
    # 透明圆角里的整块方块图标 (自带底色): 铺满, 不外套衬底
    out = _decode(sl.normalize_image(_rounded_tile(True)))
    assert out.getpixel((64, 1)) == (232, 43, 45)
    assert out.getpixel((1, 1)) == (232, 43, 45)
    assert out.getpixel((64, 64)) == (255, 255, 255)


def test_filled_app_icon_without_glyph_is_placeholder():
    assert sl.normalize_image(_rounded_tile(False)) is None


def test_near_white_tile_is_unified_to_pure_white():
    im = Image.new("RGB", (100, 100), (244, 243, 250))
    for x in range(35, 65):
        for y in range(35, 65):
            im.putpixel((x, y), (230, 80, 30))
    out = _decode(sl.normalize_image(_png(im)))
    assert out.getpixel((1, 1)) == sl.LIGHT_TILE


def test_opaque_tile_keeps_its_background_and_is_square():
    out = _decode(sl.normalize_image(_opaque_tile()))
    assert out.size == (128, 128)
    assert out.getpixel((1, 1)) == (0, 102, 204)
    assert out.getpixel((64, 64)) == (255, 255, 255)


def test_webp_input_is_supported():
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    for x in range(10, 54):
        for y in range(10, 54):
            im.putpixel((x, y), (255, 255, 255, 255) if 25 <= x < 40 else (118, 185, 0, 255))
    out = _decode(sl.normalize_image(_png(im, "WEBP")))
    assert out.size == (128, 128)


@pytest.mark.parametrize("raw", [b"", b"<html>404</html>", b"\x89PNG broken"])
def test_garbage_is_rejected(raw):
    assert sl.normalize_image(raw) is None


def test_tiny_and_blank_images_are_rejected():
    assert sl.normalize_image(_png(Image.new("RGBA", (8, 8), (255, 0, 0, 255)))) is None
    assert sl.normalize_image(_png(Image.new("RGBA", (64, 64), (0, 0, 0, 0)))) is None


@pytest.mark.parametrize("img", [
    Image.new("RGB", (100, 100), (200, 200, 200)),       # 纯色占位图
    Image.new("RGBA", (120, 120), (0, 0, 0, 255)),        # 全黑方块
    Image.new("1", (300, 300)),                           # 1 位纯色
    Image.new("RGBA", (120, 80), (30, 90, 200, 255)).crop((0, 0, 120, 80)),
])
def test_uniform_placeholder_is_rejected_so_next_source_is_tried(img):
    assert sl.normalize_image(_png(img)) is None


def test_solid_block_on_transparent_canvas_is_placeholder():
    im = Image.new("RGBA", (200, 120), (0, 0, 0, 0))
    for x in range(60, 140):
        for y in range(30, 90):
            im.putpixel((x, y), (120, 120, 120, 255))
    assert sl.normalize_image(_png(im)) is None


def test_oversized_dimensions_are_rejected_before_decode():
    big = _png(Image.new("1", (sl.MAX_SIDE + 1, 20)))
    assert sl.MAX_SIDE <= 2048
    assert sl.normalize_image(big) is None


def test_symbol_normalization_blocks_paths():
    assert sl.normalize_symbol(" nvda ") == "NVDA"
    assert sl.normalize_symbol("BRK.B") == "BRK.B"
    for bad in ["../etc", "a/b", "", "X" * 13, "..", "NV DA"]:
        assert sl.normalize_symbol(bad) is None


# ---------------------------------------------------------------- 来源顺序

class _Http:
    def __init__(self, routes: dict[str, tuple[int, bytes] | Exception]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        return 404, b""


def test_falls_through_sources_in_order(tmp_path):
    http = _Http({"parqet": (200, _opaque_tile())})
    assert sl.fetch_logo(tmp_path, "NVDA", http_get=http, token="") == "ok"
    assert [c.split("/")[2] for c in http.calls] == ["companiesmarketcap.com", "assets.parqet.com"]
    idx = json.loads((tmp_path / "stock_logos" / "index.json").read_text())
    assert idx["NVDA"]["source"] == "parqet"
    assert (tmp_path / "stock_logos" / "NVDA.png").is_file()


def test_undecodable_first_source_falls_back(tmp_path):
    http = _Http({"companiesmarketcap": (200, b"<html>soft 404</html>"), "parqet": (200, _opaque_tile())})
    assert sl.fetch_logo(tmp_path, "AMD", http_get=http, token="") == "ok"
    assert json.loads((tmp_path / "stock_logos" / "index.json").read_text())["AMD"]["source"] == "parqet"


def test_preferred_source_is_tried_first(tmp_path):
    http = _Http({"companiesmarketcap": (200, _opaque_tile()), "parqet": (200, _opaque_tile((242, 140, 40)))})
    assert sl.fetch_logo(tmp_path, "MSTR", http_get=http, token="") == "ok"
    assert "parqet" in http.calls[0]
    assert sl.PREFERRED_SOURCE["MSTR"] == "parqet"


def test_finnhub_logo_url_must_be_finnhub_https(tmp_path):
    http = _Http({
        "profile2": (200, json.dumps({"logo": "http://169.254.169.254/latest"}).encode()),
    })
    assert sl.fetch_logo(tmp_path, "SKHY", http_get=http, token="t") == "missing"
    assert not any("169.254" in c for c in http.calls)

    http2 = _Http({
        "profile2": (200, json.dumps({"logo": "https://static2.finnhub.io/file/x/SKHY.png"}).encode()),
        "static2.finnhub.io": (200, _opaque_tile()),
    })
    assert sl.fetch_logo(tmp_path, "SKHY", http_get=http2, token="t") == "ok"


@pytest.mark.parametrize("url", [
    "https://evil.com%40x.finnhub.io/a.png",
    "https://evil.com\\@static2.finnhub.io/a.png",
    "https://127.0.0.1%2f.finnhub.io/a.png",
    "https://user:pw@static2.finnhub.io/a.png",
    "https://static2.finnhub.io:8443/a.png",
    "https://x.finnhub.io/a.png",
    "https://finnhub.io.evil.com/a.png",
    "http://static2.finnhub.io/a.png",
])
def test_finnhub_logo_url_rejects_tricky_hosts(url):
    assert not sl._is_finnhub_static(url)
    assert not sl._host_allowed(url)


def test_finnhub_static_hosts_allowed():
    for u in ["https://static2.finnhub.io/file/x/SKHY.png", "https://static9.finnhub.io/file/x/1.png"]:
        assert sl._is_finnhub_static(u) and sl._host_allowed(u)


def test_default_get_refuses_off_allowlist_without_network(monkeypatch):
    monkeypatch.setattr(sl._opener, "open", lambda *a, **k: pytest.fail("不该出网"))
    assert sl._default_get("https://169.254.169.254/latest") == (403, b"")
    assert sl._default_get("http://companiesmarketcap.com/x.webp") == (403, b"")


def test_redirect_off_allowlist_is_blocked():
    import email.message

    h = sl._SameAllowlistRedirect()
    req = sl.urllib.request.Request("https://static2.finnhub.io/a.png")
    with pytest.raises(sl.urllib.error.HTTPError):
        h.redirect_request(req, None, 302, "Found", email.message.Message(), "http://192.168.1.1/admin")
    ok = h.redirect_request(req, None, 302, "Found", email.message.Message(), "https://static9.finnhub.io/a.png")
    assert ok.full_url == "https://static9.finnhub.io/a.png"


def test_knockout_tile_is_kept():
    # 红色圆角方块, 图形是透明挖空的细线 (6%): 不能当纯色占位丢掉
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for x in range(100):
        for y in range(100):
            corner = (x < 6 or x > 93) and (y < 6 or y > 93)
            knock = 45 <= x < 51 and 20 <= y < 80
            if not corner and not knock:
                im.putpixel((x, y), (220, 30, 40, 255))
    out = sl.normalize_image(_png(im))
    assert out is not None
    o = _decode(out)
    assert o.getpixel((60, 1)) == (220, 30, 40)
    assert o.getpixel((61, 64)) != (220, 30, 40)


@pytest.mark.parametrize("fg,bg", [((255, 255, 0), (255, 255, 255)), ((0, 0, 255), (0, 0, 0))])
def test_low_luma_difference_glyph_is_not_placeholder(fg, bg):
    # 白底黄图 / 黑底蓝图: 亮度差只有 29, 但通道差 255, 是真图标
    im = Image.new("RGB", (100, 100), bg)
    for x in range(30, 70):
        for y in range(30, 70):
            if (x - 50) ** 2 + (y - 50) ** 2 < 400:
                im.putpixel((x, y), fg)
    assert sl.normalize_image(_png(im)) is not None


def test_large_image_is_downscaled_before_processing():
    im = Image.new("RGBA", (2000, 2000), (0, 0, 0, 0))
    from PIL import ImageDraw
    ImageDraw.Draw(im).ellipse((200, 200, 1800, 1800), fill=(10, 120, 200, 255))
    ImageDraw.Draw(im).rectangle((900, 300, 1100, 1700), fill=(255, 255, 255, 0))
    out = sl.normalize_image(_png(im))
    assert out is not None and _decode(out).size == (128, 128)


def test_finnhub_skipped_without_token(tmp_path):
    http = _Http({})
    sl.fetch_logo(tmp_path, "ZZZ", http_get=http, token="")
    assert not any("finnhub" in c for c in http.calls)


def test_all_sources_missing_is_recorded(tmp_path):
    assert sl.fetch_logo(tmp_path, "ZZZQ", http_get=_Http({}), token="", now=1000) == "missing"
    e = json.loads((tmp_path / "stock_logos" / "index.json").read_text())["ZZZQ"]
    assert e["status"] == "missing" and e["checked_at"] == 1000
    assert not (tmp_path / "stock_logos" / "ZZZQ.png").exists()


def test_network_error_is_not_recorded_as_missing(tmp_path):
    http = _Http({"": urllib.error.URLError("down")})
    assert sl.fetch_logo(tmp_path, "NVDA", http_get=http, token="", now=1000) == "error"
    assert not (tmp_path / "stock_logos" / "index.json").exists()
    assert sl._error_until["NVDA"] == 1000 + sl.ERROR_RETRY_S


# ---------------------------------------------------------------- 状态 (不出网)

def _seed(tmp_path: Path, sym: str) -> None:
    d = tmp_path / "stock_logos"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sym}.png").write_bytes(sl.normalize_image(_opaque_tile()))


def test_status_reports_cached_and_schedules_missing(tmp_path, monkeypatch):
    _seed(tmp_path, "NVDA")
    submitted: list[str] = []
    monkeypatch.setattr(sl, "_submit", lambda d, s: submitted.append(s))
    monkeypatch.setattr(sl, "_default_get", lambda url: pytest.fail("status() 不得出网"))
    out = sl.status(tmp_path, ["NVDA", "aapl", "../x", "NVDA"])
    assert set(out["logos"]) == {"NVDA"} and out["logos"]["NVDA"].isdigit()
    assert out["pending"] == ["AAPL"]
    assert submitted == ["AAPL"]


def test_status_respects_missing_and_error_backoff(tmp_path, monkeypatch):
    submitted: list[str] = []
    monkeypatch.setattr(sl, "_submit", lambda d, s: submitted.append(s))
    sl.fetch_logo(tmp_path, "OLD", http_get=_Http({}), token="", now=10_000)
    sl._error_until["NET"] = 20_000
    out = sl.status(tmp_path, ["OLD", "NET"], now=10_500)
    assert out["pending"] == [] and submitted == []
    out = sl.status(tmp_path, ["OLD", "NET"], now=10_000 + sl.MISSING_RETRY_S + 1)
    assert submitted == ["OLD", "NET"]


def test_submit_is_single_flight_on_daemon_workers(tmp_path, monkeypatch):
    import queue as _q

    q: _q.Queue = _q.Queue()
    workers: list = []
    monkeypatch.setattr(sl, "_queue", q)
    monkeypatch.setattr(sl, "_workers", workers)
    monkeypatch.setattr(sl, "_worker", lambda: None)  # 不真跑, 只看排队
    sl._submit(tmp_path, "NVDA")
    sl._submit(tmp_path, "NVDA")
    assert q.qsize() == 1 and q.get_nowait() == (tmp_path, "NVDA")
    assert len(workers) == sl.WORKERS and all(t.daemon for t in workers)


def test_worker_fetches_and_clears_inflight(tmp_path, monkeypatch):
    import queue as _q
    import time as _t

    monkeypatch.setattr(sl, "_queue", _q.Queue())
    monkeypatch.setattr(sl, "_workers", [])
    monkeypatch.setattr(sl, "fetch_logo", lambda d, s: (Path(d) / f"{s}.done").write_text("1"))
    sl._submit(tmp_path, "AMD")
    for _ in range(100):
        if (tmp_path / "AMD.done").exists() and "AMD" not in sl._inflight:
            break
        _t.sleep(0.02)
    assert (tmp_path / "AMD.done").exists() and "AMD" not in sl._inflight


def test_run_clears_inflight_even_on_crash(tmp_path, monkeypatch):
    sl._inflight.add("NVDA")
    monkeypatch.setattr(sl, "fetch_logo", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    sl._run(tmp_path, "NVDA")
    assert "NVDA" not in sl._inflight
    assert sl._error_until["NVDA"] > 0


# ---------------------------------------------------------------- 接口

def _client(tmp_path, monkeypatch, symbols):
    from app.api import watchlist as api
    monkeypatch.setattr(api.watchlist, "list_symbols", lambda: [{"symbol": s} for s in symbols])
    monkeypatch.setattr(sl, "_submit", lambda d, s: None)
    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def test_logo_endpoints(tmp_path, monkeypatch):
    _seed(tmp_path, "NVDA")
    c = _client(tmp_path, monkeypatch, ["NVDA", "AAPL"])
    r = c.get("/api/watchlist/logos")
    assert r.status_code == 200
    body = r.json()
    assert list(body["logos"]) == ["NVDA"] and body["pending"] == ["AAPL"]

    img = c.get("/api/watchlist/logos/nvda")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert "max-age" in img.headers["cache-control"]
    assert _decode(img.content).size == (128, 128)

    assert c.get("/api/watchlist/logos/AAPL").status_code == 404
    assert c.get("/api/watchlist/logos/..%2F..%2Fuser_data%2Fauth.json").status_code == 404
    assert c.get("/api/watchlist/logos/index.json").status_code == 404
