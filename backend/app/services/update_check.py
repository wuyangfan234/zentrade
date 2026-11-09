"""新版本检查 (0.44.0) — 自托管部署发现上游仓库有更新时提醒。

查法 (服务端出网, 不依赖浏览器 CORS / GitHub 未认证限额按访客 IP 计):
  0.55.0 起版本固定, 按默认分支提交的祖先关系判断是否有更新。
  历史版本保留 Releases / VERSION 比较; 未知或无共同历史不宣称最新。
结果在进程内缓存 6 小时; 手动检查 (force) 绕过缓存。失败不抛, 返回 status=error。

配置 (.env):
  UPDATE_CHECK_ENABLED=false   关闭 (离线/内网部署)
  UPDATE_CHECK_REPO=owner/repo 自己的 fork 想跟自己的仓库时改这里
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

from app import __version__, build_info
from app.config import settings

logger = logging.getLogger(__name__)

CACHE_TTL_S = 6 * 3600
_TIMEOUT_S = 10.0
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_lock = threading.Lock()
_cache: dict = {}


def parse_version(v: str) -> tuple[int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", (v or "").strip().lstrip("vV"))[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def _get_json(url: str) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"ZenTrade/{__version__} (update-check)",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None


def _get_text(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": f"ZenTrade/{__version__} (update-check)"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
            return r.status, r.read(64).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def _fetch(repo: str) -> dict:
    status, rel = _get_json(f"https://api.github.com/repos/{repo}/releases/latest")
    if status == 200 and rel and rel.get("tag_name"):
        return {
            "latest": str(rel["tag_name"]),
            "url": rel.get("html_url") or f"https://github.com/{repo}/releases/latest",
            "notes": str(rel.get("body") or "")[:4000],
            "published_at": rel.get("published_at"),
            "source": "release",
        }
    status, text = _get_text(f"https://raw.githubusercontent.com/{repo}/HEAD/VERSION")
    latest = text.strip().splitlines()[0].strip() if status == 200 and text.strip() else ""
    if latest and re.match(r"^v?\d+(\.\d+){0,2}", latest):
        return {
            "latest": latest,
            "url": f"https://github.com/{repo}/blob/HEAD/CHANGELOG.md",
            "notes": "",
            "published_at": None,
            "source": "version_file",
        }
    if status == 404:
        raise LookupError("仓库不可访问 (私有或不存在)")
    raise LookupError(f"未取到版本信息 (HTTP {status})")


def _fetch_revision(repo: str, current_revision: str) -> dict:
    """Compare ancestry, not unequal hashes: private/diverged trees are unknown."""
    if current_revision == "unknown":
        raise LookupError("当前构建没有提交标识, 无法检查更新")
    base = f"https://api.github.com/repos/{repo}"
    status, commit = _get_json(f"{base}/commits/HEAD")
    latest = commit.get("sha", "") if status == 200 and isinstance(commit, dict) else ""
    if not isinstance(latest, str) or not re.fullmatch(r"[0-9a-f]{40}", latest):
        raise LookupError(f"未取到最新提交 (HTTP {status})")
    comparison = "identical"
    if current_revision != latest:
        status, compared = _get_json(f"{base}/compare/{current_revision}...{latest}")
        comparison = compared.get("status") if status == 200 and isinstance(compared, dict) else None
    if comparison not in {"ahead", "behind", "identical"}:
        raise LookupError("当前构建与更新仓库的提交历史无法比较")
    return {
        "status": "update_available" if comparison == "ahead" else "latest",
        "latest": f"v{__version__}",
        "latest_revision": latest,
        "source": "commit",
        "url": f"https://github.com/{repo}/commit/{latest}",
        "notes": "",
        "published_at": None,
    }


def check(*, force: bool = False, now: float | None = None) -> dict:
    """返回 {enabled, repo, current, status, latest?, url?, notes?, source?, checked_at, error?}。

    status: disabled / latest / update_available / error
    """
    now = time.time() if now is None else now
    repo = (settings.update_check_repo or "").strip()
    current = __version__
    revision = build_info.revision()
    base = {"enabled": bool(settings.update_check_enabled and repo), "repo": repo, "current": current,
            "current_revision": revision}
    if not settings.update_check_enabled or not repo:
        return {**base, "status": "disabled", "checked_at": None}
    if not _REPO_RE.match(repo):
        return {**base, "status": "error", "error": "UPDATE_CHECK_REPO 格式应为 owner/repo", "checked_at": None}
    with _lock:
        hit = _cache.get(repo)
        if hit and not force and now - hit["checked_at"] < CACHE_TTL_S and hit.get("current") == (current, revision):
            return {**base, **hit["result"], "checked_at": hit["checked_at"], "cached": True}
    try:
        if parse_version(current) >= (0, 55, 0):
            result = _fetch_revision(repo, revision)
        else:
            info = _fetch(repo)
            status = "update_available" if is_newer(info["latest"], current) else "latest"
            result = {"status": status, **info}
    except Exception as e:
        logger.info("update check failed for %s: %s", repo, type(e).__name__)
        result = {"status": "error", "error": str(e)[:200] if isinstance(e, LookupError) else "网络不可达或超时"}
    with _lock:
        _cache[repo] = {"result": result, "checked_at": now, "current": (current, revision)}
    return {**base, **result, "checked_at": now, "cached": False}
