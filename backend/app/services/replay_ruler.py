"""回放数字的口径标签 (机会页 B 档, 2026-11-15): 这个回放数字是按哪份自选名单、哪段窗口算的。

- 名单校验和 = 排序后的成员按行拼接取 sha256 前 12 位。信号表 opportunities/watchlist/<日>.json 逐日存了
  当天的完整名单 (watchlist 字段), 所以任意一天的校验和都能在读的时候算出来, 不另存。
- 回放文件 (config/swing_*.json) 的 universe 写成 "watchlist 46 (2026-11-09)": 解析出只数与定名单的日子;
  回看窗口起点早于定名单那天 = 事后名单 (用后来挑的票回看更早的行情)。
- 文件里没写名单的 (今日多空 / 盘中强势的代码常量) → kind = "unrecorded", 页面写「名单未记录」。
- 定名单那天的信号表在 → 用成员校验和比较现在的自选; 不在 → 只比只数 (只数相同时不下结论 = None)。
只读, 只展示; 不进打分、裁决、排序与下单。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

CONTRACT = "replay-ruler-v1"
_UNIV = re.compile(r"watchlist\s+(\d+)\s*\((\d{4}-\d{2}-\d{2})\)", re.IGNORECASE)


def watch_sha(symbols) -> str | None:
    """名单校验和: 成员去重、大写、排序后按行拼接取 sha256 前 12 位。空名单 = None。"""
    xs = sorted({str(s).strip().upper() for s in symbols or [] if str(s).strip()})
    if not xs:
        return None
    return hashlib.sha256("\n".join(xs).encode("utf-8")).hexdigest()[:12]


_MEMBERS: dict[str, tuple[tuple, list[str] | None]] = {}


def members_on(data_dir: Path | None, day: date) -> list[str] | None:
    """那一天信号表里存的自选名单; 当天没有信号表或读不出 = None (不回退到别的日子)。按文件签名缓存 (页面 30 秒一轮)。"""
    if data_dir is None:
        return None
    p = Path(data_dir) / "opportunities" / "watchlist" / f"{day.isoformat()}.json"
    try:
        st = p.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    hit = _MEMBERS.get(str(p))
    if hit and hit[0] == sig:
        return hit[1]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    xs = raw.get("watchlist") if isinstance(raw, dict) else None
    out = [str(x) for x in xs] if isinstance(xs, list) and xs else None
    if len(_MEMBERS) > 16:
        _MEMBERS.clear()
    _MEMBERS[str(p)] = (sig, out)
    return out


def parse_universe(universe) -> tuple[int, date] | None:
    m = _UNIV.search(str(universe or ""))
    if not m:
        return None
    try:
        return int(m.group(1)), date.fromisoformat(m.group(2))
    except ValueError:
        return None


def _window(window) -> list[str] | None:
    if isinstance(window, (list, tuple)) and len(window) >= 2:
        return [str(window[0]), str(window[1])]
    if isinstance(window, str) and "~" in window:
        a, b = window.split("~", 1)
        return [a.strip(), b.strip()]
    return None


def ruler(universe, window, *, data_dir: Path | None = None, watch_now=None) -> dict:
    """一个回放数字的口径。universe = 文件里的名单写法 (None = 没记); window = [起, 止] 或 "2024-01~2026-10"。
    watch_now = 现在的自选 (None = 不比较)。"""
    win = _window(window)
    now = sorted({str(s).upper() for s in watch_now or []}) if watch_now is not None else None
    out: dict = {"contract": CONTRACT, "window": win, "watch_now": len(now) if now is not None else None}
    parsed = parse_universe(universe)
    if parsed is None:
        return {**out, "kind": "unrecorded", "n": None, "fixed": None, "changed": None}
    n, fixed = parsed
    post_hoc = bool(win) and win[0][:10] < fixed.isoformat()
    changed = None
    if now is not None:
        mem = members_on(data_dir, fixed)
        if mem is not None:
            changed = watch_sha(mem) != watch_sha(now)
        elif n != len(now):
            changed = True
    return {**out, "kind": "post_hoc" if post_hoc else "fixed", "n": n, "fixed": fixed.isoformat(), "changed": changed}


def current_watchlist() -> set[str] | None:
    """现在的自选 (与交易环同一来源); 读不到或为空 = None (标签不比较, 不当成“名单变了”)。"""
    try:
        from app.services.decision_scan_job import _watchlist
        w = _watchlist()
    except Exception:
        return None
    return set(w) if w else None


def from_meta(meta: dict | None, *, data_dir: Path | None = None, watch_now=None) -> dict | None:
    """回放文件元数据 (universe / window) → 口径; 文件读不到 (空 dict) = None。"""
    if not isinstance(meta, dict) or not meta:
        return None
    return ruler(meta.get("universe"), meta.get("window"), data_dir=data_dir, watch_now=watch_now)
