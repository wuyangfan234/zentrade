"""自选股公司图标 (0.38.0)。

页面只读本地缓存, 请求内绝不出网: 缺图的票交给后台线程按来源顺序抓一次, 归一化成
128x128 的方形 PNG 后落盘 `data/stock_logos/<代码>.png`, 结果记在 `index.json`。

来源顺序 (10-24 对当时 44 只自选逐个目视核对后定):
  1. companiesmarketcap — 品牌最新 (IREN/MU/BMNR/COHR 等改名或换标后的新标), 缺少数 ADR;
  2. Parqet            — 覆盖面广, 但个别代码映射到别家公司 (SPCX→axs), 只作第二顺位;
  3. Finnhub profile2   — 需 key, 唯一给出清晰 SK hynix 的源; 部分老标 (II-VI/旧 Intel);
  4. FMP image-stock    — 全覆盖兜底, 部分老标与通用比特币图标。
都没有 = 记 missing, 7 天后再试; 网络错误不记 missing (1 小时内不重试)。前端缺图显示字母头像。

归一化: 透明底图标先裁掉留白再按比例放大, 统一放白底; 只有白色字标这类在白底上看不见的
才放深底。自带底色的圆角方块图标直接铺满。不透明的方块图标裁掉纯色边后按原底色补边,
近白底 (浅灰/淡紫) 统一成纯白。
"""
from __future__ import annotations

import io
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

SIZE = 128
SOURCES = ("cmc", "parqet", "finnhub", "fmp")
# 10-24 目视核对后改首选源的票: 默认顺序第一张图是旧标/通用符号, 指定源才是能一眼认出的现行标。
PREFERRED_SOURCE = {
    "MSTR": "parqet",  # cmc = 单色比特币符号; parqet = Strategy 橙色新标
    "GLW": "fmp",      # cmc = 单个字母 C; fmp = CORNING 字标
    "TSM": "parqet",   # cmc = 晶圆底纹 + 小字, 小尺寸发糊; parqet = 红底白字 tsmc
}
MISSING_RETRY_S = 7 * 24 * 3600
ERROR_RETRY_S = 3600
TIMEOUT_S = 8.0
MAX_BYTES = 2 * 1024 * 1024
MAX_SIDE = 2048  # 10-24 实测来源最大 1534x1225; 4096 的图解码峰值约 265MB, 3 线程并发会顶到 0.8GB
WORKERS = 3
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
_UA = {"User-Agent": "Mozilla/5.0 (ZenTrade stock logo cache)"}
LIGHT_TILE = (255, 255, 255)
DARK_TILE = (39, 39, 42)  # zinc-800: 比暗色页面底 (#0A0A0B/#18181B) 亮一档, 方块边界看得见

# (status, bytes); 404 等 HTTP 错误返回状态码, 网络层错误抛异常
HttpGet = Callable[[str], tuple[int, bytes]]

_lock = threading.Lock()
_inflight: set[str] = set()
_error_until: dict[str, float] = {}
# 后台补图: 守护线程 + 队列 (不用 ThreadPoolExecutor: 它的线程非守护, 退出时会等正在取的图, 拖慢容器重启)
_queue: queue.Queue = queue.Queue()
_workers: list[threading.Thread] = []


def normalize_symbol(symbol: str) -> str | None:
    s = (symbol or "").strip().upper()
    return s if _SYMBOL.match(s) else None


def logo_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / "stock_logos"


def _index_path(data_dir: Path | str) -> Path:
    return logo_dir(data_dir) / "index.json"


def _load_index(data_dir: Path | str) -> dict:
    p = _index_path(data_dir)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _update_index(data_dir: Path | str, symbol: str, entry: dict) -> None:
    with _lock:
        d = _load_index(data_dir)
        d[symbol] = entry
        p = _index_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(d, ensure_ascii=False, sort_keys=True, indent=1))


def logo_path(data_dir: Path | str, symbol: str) -> Path | None:
    s = normalize_symbol(symbol)
    if not s:
        return None
    p = logo_dir(data_dir) / f"{s}.png"
    return p if p.is_file() else None


# ---------------------------------------------------------------- 归一化

def _lum(rgb: tuple[float, float, float]) -> float:
    """WCAG 相对亮度。"""
    def ch(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(a: float, b: float) -> float:
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def pick_tile(visible_on_light: float) -> tuple[int, int, int]:
    """透明底图标的衬底: 统一白底; 只有图形在白底上几乎看不见 (可见像素 < 30%, 如白色字标) 才用深底。"""
    return LIGHT_TILE if visible_on_light >= 0.30 else DARK_TILE


def _visible_on_light(img) -> float:
    """不透明像素里, 放在白底上对比度 ≥ 1.6 的占比 (按 alpha 加权)。"""
    raw = img.tobytes()
    white = _lum(LIGHT_TILE)
    tot = vis = 0.0
    for i in range(0, len(raw), 4):
        a = raw[i + 3]
        if a < 32:
            continue
        w = a / 255.0
        tot += w
        if _contrast(_lum((raw[i], raw[i + 1], raw[i + 2])), white) >= 1.6:
            vis += w
    return vis / tot if tot else 0.0


_THRESH_8 = [0] * 9 + [255] * 247
_THRESH_40 = [0] * 41 + [255] * 215
_BELOW_200 = [255] * 200 + [0] * 56


def _diff_mask(rgb, color):
    """与 color 的逐像素差 (取 R/G/B 三通道里最大的那个; 用亮度会把白底黄字算成同色)。"""
    from PIL import Image, ImageChops

    r, g, b = ImageChops.difference(rgb, Image.new("RGB", rgb.size, color)).split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def _is_uniform(rgb) -> bool:
    """整张只有一种颜色 (各通道差都 ≤ 40)。"""
    return not _diff_mask(rgb, rgb.getpixel((0, 0))).point(_THRESH_40).getbbox()


def _inner(img):
    """去掉四周 12% (圆角所在) 的内部区域: 判定"镂空"只看这里, 圆角透明不算图形。"""
    w, h = img.size
    dx, dy = max(1, int(w * 0.12)), max(1, int(h * 0.12))
    return img.crop((dx, dy, w - dx, h - dy))


def _edge_color(img) -> tuple[int, int, int]:
    """不透明方块图标的底色 = 四条边上不透明像素里出现最多的颜色 (按 16 级量化计票)。"""
    from collections import Counter

    w, h = img.size
    raw = img.tobytes()
    votes: Counter = Counter()
    exact: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    pts = [(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)]
    for x, y in pts:
        i = (y * w + x) * 4
        r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if a < 200:
            continue
        key = (r // 16, g // 16, b // 16)
        votes[key] += 1
        exact.setdefault(key, (r, g, b))
    if not votes:
        return LIGHT_TILE
    return exact[votes.most_common(1)[0][0]]


def _glyph_box(glyph) -> int:
    """图标在 128 方块里占的边长: 横向字标 (宽高比 ≥1.8) 86%, 其余 72%, 留出前端圆角与描边的安全区。"""
    return int(SIZE * (0.86 if glyph.width / glyph.height >= 1.8 else 0.72))


def _is_filled_tile(glyph) -> bool:
    """透明底里画的是一整块自带底色的方块 (圆角方块 App 图标): 近正方形且铺满自身外框 ≥ 90%。

    这类图标直接铺满 128 方块 (透明圆角用它自己的边色补齐), 不再外套一圈衬底。
    """
    w, h = glyph.size
    a = glyph.getchannel("A").resize((min(64, w), min(64, h))).tobytes()
    return 0.85 <= w / h <= 1.18 and sum(1 for v in a if v > 128) / len(a) >= 0.90


def _fit(img, box: int):
    from PIL import Image

    w, h = img.size
    scale = min(box / w, box / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    return img.resize((nw, nh), Image.Resampling.LANCZOS)


def normalize_image(raw: bytes) -> bytes | None:
    """任意格式图标 → 128x128 RGB PNG; 解不开、太小、全透明或纯色占位返回 None。"""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(raw)) as im0:
            if im0.width > MAX_SIDE or im0.height > MAX_SIDE:
                return None
            im0.draft("RGB", (512, 512))  # JPEG 解码时直接降采样; 其他格式无副作用
            im = im0.convert("RGBA")
            if im.width > 512 or im.height > 512:
                im.thumbnail((512, 512), Image.Resampling.LANCZOS)
    except Exception:  # 坏图一律当没有
        return None
    if im.width < 16 or im.height < 16:
        return None
    alpha = im.getchannel("A")
    small = alpha.resize((min(64, im.width), min(64, im.height))).tobytes()
    transparent_share = sum(1 for a in small if a < 16) / len(small)

    if transparent_share > 0.05:
        bbox = alpha.point(_THRESH_8).getbbox()
        if not bbox:
            return None
        glyph = im.crop(bbox)
        if _is_filled_tile(glyph):
            edge = _edge_color(glyph)
            out = Image.new("RGBA", glyph.size, (*edge, 255))
            out.alpha_composite(glyph)
            flat = out.convert("RGB")
            if _is_uniform(flat):
                if not _inner(glyph).getchannel("A").point(_BELOW_200).getbbox():
                    return None  # 圆角方块里没有图形 = 占位图
                # 镂空图标 (图形是透明挖空的): 改用和方块底色反差的底色垫在下面, 让挖空处显出来
                under = DARK_TILE if _lum(edge) > 0.5 else LIGHT_TILE
                out = Image.new("RGBA", glyph.size, (*under, 255))
                out.alpha_composite(glyph)
                flat = out.convert("RGB")
                if _is_uniform(flat):
                    return None  # 圆角方块里真的没有图形 = 占位图
            return _png(flat.resize((SIZE, SIZE), Image.Resampling.LANCZOS))
        if not glyph.getchannel("A").point(_BELOW_200).getbbox() and _is_uniform(glyph.convert("RGB")):
            return None  # 外框内全不透明且只有一种颜色 = 纯色块占位图
        small = glyph.copy()
        small.thumbnail((64, 64))
        tile = pick_tile(_visible_on_light(small))
        glyph = _fit(glyph, _glyph_box(glyph))
        out = Image.new("RGBA", (SIZE, SIZE), (*tile, 255))
        out.alpha_composite(glyph, ((SIZE - glyph.width) // 2, (SIZE - glyph.height) // 2))
        return _png(out.convert("RGB"))

    bg = _edge_color(im)
    flat = Image.new("RGBA", im.size, (*bg, 255))
    flat.alpha_composite(im)
    rgb = flat.convert("RGB")
    if _is_uniform(rgb) and _inner(im).getchannel("A").point(_BELOW_200).getbbox():
        # 少量透明挖空 (镂空图标): 挖空处垫反差色, 让图形显出来
        under = DARK_TILE if _lum(bg) > 0.5 else LIGHT_TILE
        flat = Image.new("RGBA", im.size, (*under, 255))
        flat.alpha_composite(im)
        rgb = flat.convert("RGB")
    diff = _diff_mask(rgb, bg)
    if bg != LIGHT_TILE and _lum(bg) > 0.75:
        # 浅灰/淡紫等近白底统一成纯白, 和其余白底图标一致
        near = diff.point([255] * 41 + [0] * 215)
        rgb.paste(LIGHT_TILE, (0, 0, rgb.width, rgb.height), near)
        bg = LIGHT_TILE
        diff = _diff_mask(rgb, bg)
    bbox = diff.point(_THRESH_40).getbbox()
    if not bbox:
        return None  # 整张纯色 = 占位图, 交给下一个来源
    if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) < 0.8 * rgb.width * rgb.height:
        glyph = rgb.crop(bbox)
        glyph = _fit(glyph, _glyph_box(glyph))
        out = Image.new("RGB", (SIZE, SIZE), bg)
        out.paste(glyph, ((SIZE - glyph.width) // 2, (SIZE - glyph.height) // 2))
        return _png(out)
    side = max(rgb.width, rgb.height)
    if rgb.width != rgb.height:
        sq = Image.new("RGB", (side, side), bg)
        sq.paste(rgb, ((side - rgb.width) // 2, (side - rgb.height) // 2))
        rgb = sq
    return _png(rgb.resize((SIZE, SIZE), Image.Resampling.LANCZOS))


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- 取图

_FINNHUB_STATIC = re.compile(r"^static\d{0,2}\.finnhub\.io$")
_ALLOWED_HOSTS = {"companiesmarketcap.com", "assets.parqet.com", "financialmodelingprep.com", "finnhub.io"}


def _is_finnhub_static(url: str) -> bool:
    """Finnhub profile2 给的图标地址只认 https://staticN.finnhub.io/<路径>: 无账号、端口、%、反斜杠。"""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (u.scheme == "https" and not any(c in u.netloc for c in "@:%\\")
            and bool(_FINNHUB_STATIC.match(u.netloc)) and u.path.startswith("/"))


def _host_allowed(url: str) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if u.scheme != "https" or any(c in u.netloc for c in "@:%\\"):
        return False
    return u.netloc in _ALLOWED_HOSTS or bool(_FINNHUB_STATIC.match(u.netloc))


class _SameAllowlistRedirect(urllib.request.HTTPRedirectHandler):
    """跟随重定向时目标仍须在白名单内 (static2→static9.finnhub.io 这类合法跳转放行)。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_allowed(newurl):
            raise urllib.error.HTTPError(newurl, 403, "redirect off allowlist", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameAllowlistRedirect)


def _default_get(url: str) -> tuple[int, bytes]:
    if not _host_allowed(url):
        return 403, b""
    req = urllib.request.Request(url, headers=_UA)
    try:
        with _opener.open(req, timeout=TIMEOUT_S) as r:  # 只连白名单 https 主机
            body = r.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                return 413, b""
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, b""


def _finnhub_token() -> str | None:
    try:
        from app.services.earnings_calendar import _configured_finnhub_token

        return _configured_finnhub_token()
    except Exception:
        return None


def _source_url(source: str, symbol: str, http_get: HttpGet, token: str | None) -> str | None:
    dash = symbol.replace(".", "-")
    if source == "cmc":
        return f"https://companiesmarketcap.com/img/company-logos/128/{dash}.webp"
    if source == "parqet":
        return f"https://assets.parqet.com/logos/symbol/{urllib.parse.quote(symbol)}?format=png&size=128"
    if source == "fmp":
        return f"https://financialmodelingprep.com/image-stock/{dash}.png"
    if source == "finnhub":
        if not token:
            return None
        q = urllib.parse.urlencode({"symbol": symbol, "token": token})
        status, body = http_get(f"https://finnhub.io/api/v1/stock/profile2?{q}")
        if status != 200:
            return None
        try:
            logo = (json.loads(body) or {}).get("logo") or ""
        except ValueError:
            return None
        return logo if _is_finnhub_static(logo) else None
    return None


def fetch_logo(data_dir: Path | str, symbol: str, *, http_get: HttpGet | None = None,
               token: str | None = None, now: float | None = None) -> str:
    """按来源顺序取一只票的图标并落盘。返回 'ok' / 'missing' / 'error'。"""
    s = normalize_symbol(symbol)
    if not s:
        return "missing"
    get = http_get or _default_get
    tok = token if token is not None else _finnhub_token()
    had_error = False
    tried: list[str] = []
    first = PREFERRED_SOURCE.get(s)
    order = ((first,) if first else ()) + tuple(x for x in SOURCES if x != first)
    for source in order:
        try:
            url = _source_url(source, s, get, tok)
            if not url:
                continue
            tried.append(source)
            status, body = get(url)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            had_error = True
            logger.debug("logo %s %s 网络错误: %s", s, source, type(e).__name__)
            continue
        if status != 200 or not body:
            continue
        png = normalize_image(body)
        if png is None:
            continue
        d = logo_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{s}.png.tmp"
        tmp.write_bytes(png)
        tmp.replace(d / f"{s}.png")
        _update_index(data_dir, s, {"status": "ok", "source": source,
                                    "fetched_at": int(now if now is not None else time.time())})
        return "ok"
    if had_error:
        _error_until[s] = (now if now is not None else time.time()) + ERROR_RETRY_S
        return "error"
    _update_index(data_dir, s, {"status": "missing", "tried": tried,
                                "checked_at": int(now if now is not None else time.time())})
    return "missing"


# ---------------------------------------------------------------- 查询 + 后台补图

def _needs_fetch(index: dict, symbol: str, data_dir: Path | str, now: float) -> bool:
    if logo_path(data_dir, symbol):
        return False
    if _error_until.get(symbol, 0) > now:
        return False
    e = index.get(symbol) or {}
    recently_missing = e.get("status") == "missing" and now - float(e.get("checked_at") or 0) < MISSING_RETRY_S
    return not recently_missing


def _run(data_dir: Path | str, symbol: str) -> None:
    try:
        fetch_logo(data_dir, symbol)
    except Exception as e:  # 后台补图失败不影响任何页面
        logger.warning("logo %s 取图失败: %s", symbol, type(e).__name__)
        _error_until[symbol] = time.time() + ERROR_RETRY_S
    finally:
        with _lock:
            _inflight.discard(symbol)


def _worker() -> None:
    while True:
        data_dir, symbol = _queue.get()
        _run(data_dir, symbol)


def _submit(data_dir: Path | str, symbol: str) -> None:
    with _lock:
        if symbol in _inflight:
            return
        _inflight.add(symbol)
        while len(_workers) < WORKERS:
            t = threading.Thread(target=_worker, daemon=True, name=f"stock-logo-{len(_workers)}")
            t.start()
            _workers.append(t)
    _queue.put((data_dir, symbol))


def status(data_dir: Path | str, symbols: list[str], *, schedule: bool = True,
           now: float | None = None) -> dict:
    """{logos: {代码: 版本}, pending: [仍在取的代码]}。不出网; schedule=True 时缺图的票排进后台。"""
    t = now if now is not None else time.time()
    index = _load_index(data_dir)
    logos: dict[str, str] = {}
    pending: list[str] = []
    for raw in symbols:
        s = normalize_symbol(raw)
        if not s or s in logos:
            continue
        p = logo_path(data_dir, s)
        if p:
            logos[s] = str(int(p.stat().st_mtime))
            continue
        if _needs_fetch(index, s, data_dir, t):
            pending.append(s)
            if schedule:
                _submit(data_dir, s)
        elif s in _inflight:
            pending.append(s)
    return {"logos": logos, "pending": pending}
