"""AI 助手技能 (0.56.0 起): 一份 Markdown = 一个可复用的问答流程。

形状 (与常见 agent 的 SKILL.md 同一思路, 前置 YAML + 正文步骤):

    ---
    name: posture            # 小写字母/数字/连字符, 全局唯一
    title: 持仓体检
    group: 每日               # 每日 / 研究 / 策略 / 系统 (自定义也可)
    triggers: [/持仓, 持仓体检]
    tools: [get_portfolio, get_earnings, get_news]
    output: chat             # chat 对话 / report 报告
    pages: [持仓]             # 在这些页面打开助手时优先展示
    ---
    1. get_portfolio 读模拟持仓与离场意向
    ...

来源与存储:
- 内置技能随代码发布 (app/custom/assistant/skills/*.md), 只读; 想改先「复制为我的技能」。
- 用户技能存 data/user_data/assistant_skills/<name>/v<n>.md, 每次保存新增一版, current.json 指向现行版;
  回滚 = 把指针改回旧版 (旧文件不删)。enabled.json 记各技能启用/停用 (内置技能也能停用)。
- 运行记录 runs.jsonl (最近 500 条): 技能名、时刻、工具调用数、是否成功; 只记元数据, 不存问答正文。

安全边界: 技能只是给模型的流程说明, 不授予任何权限 —— 能调用的仍只有助手注册的只读工具;
保存前检查器拦下引用了不存在工具、缺必填项、触发词冲突的技能。
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from app.market_clock import us_clock
from app.services.fs_utils import atomic_write_text

BUILTIN_DIR = Path(__file__).resolve().parent / "skills"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
TRIGGER_RE = re.compile(r"^/?[\w\u4e00-\u9fff][\w\u4e00-\u9fff\- ]{0,19}$")
OUTPUTS = ("chat", "report")
MAX_BODY_CHARS = 4000
MAX_SKILLS = 60
MAX_VERSIONS = 30
MAX_RUNS = 500
_LOCK = threading.Lock()
_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


@dataclass
class Skill:
    name: str
    title: str
    group: str = "其他"
    triggers: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    output: str = "chat"
    pages: list[str] = field(default_factory=list)
    body: str = ""
    source: str = "user"          # builtin / user
    version: int = 1
    enabled: bool = True
    based_on: str | None = None   # 复制自哪个内置技能

    def to_markdown(self) -> str:
        front = {
            "name": self.name, "title": self.title, "group": self.group, "triggers": self.triggers,
            "tools": self.tools, "output": self.output, "pages": self.pages,
        }
        if self.based_on:
            front["based_on"] = self.based_on
        return "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip() + "\n---\n" + self.body.strip() + "\n"

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name, "title": self.title, "group": self.group, "triggers": self.triggers,
            "tools": self.tools, "output": self.output, "pages": self.pages, "source": self.source,
            "version": self.version, "enabled": self.enabled, "based_on": self.based_on,
        }


# ----------------------------------------------------------------
# 解析与检查
# ----------------------------------------------------------------

def parse(text: str, *, source: str = "user", version: int = 1) -> Skill:
    """Markdown → Skill。格式错误抛 ValueError (人话)。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("技能内容为空")
    m = _FRONT_RE.match(text.strip() + "\n")
    if not m:
        raise ValueError("缺少开头的 --- 设置区 ---")
    try:
        front = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"设置区格式错误: {str(exc).splitlines()[0]}") from None
    if not isinstance(front, dict):
        raise ValueError("设置区必须是「键: 值」形式")

    def as_list(key: str) -> list[str]:
        value = front.get(key) or []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            raise ValueError(f"{key} 必须是列表")
        return [str(v).strip() for v in value if str(v).strip()]

    return Skill(
        name=str(front.get("name") or "").strip(),
        title=str(front.get("title") or "").strip(),
        group=str(front.get("group") or "其他").strip() or "其他",
        triggers=as_list("triggers"),
        tools=as_list("tools"),
        output=str(front.get("output") or "chat").strip(),
        pages=as_list("pages"),
        body=m.group(2).strip(),
        source=source,
        version=version,
        based_on=(str(front["based_on"]).strip() if front.get("based_on") else None),
    )


def check(skill: Skill, *, known_tools: set[str], others: list[Skill]) -> list[dict[str, str]]:
    """保存前检查。返回 [{level: error|warn, item, detail}]; 有 error 不许保存。

    学 QuantMind lint_skills 的三条 (名与必填、触发词、触发词冲突), 再加「引用的工具必须存在」。
    """
    out: list[dict[str, str]] = []

    def add(level: str, item: str, detail: str) -> None:
        out.append({"level": level, "item": item, "detail": detail})

    if not NAME_RE.match(skill.name or ""):
        add("error", "名称", "只能用小写字母、数字和连字符, 2-40 个字符, 字母开头")
    if not skill.title or len(skill.title) > 20:
        add("error", "标题", "必填, 不超过 20 个字")
    if skill.output not in OUTPUTS:
        add("error", "输出", "只能是 chat 或 report")
    if not skill.body:
        add("error", "步骤", "正文不能为空")
    elif len(skill.body) > MAX_BODY_CHARS:
        add("error", "步骤", f"正文超过 {MAX_BODY_CHARS} 字")
    if not skill.triggers:
        add("error", "触发词", "至少一个, 例如 /持仓")
    for trig in skill.triggers:
        if not TRIGGER_RE.match(trig):
            add("error", "触发词", f"「{trig}」格式不对: 20 字以内, 不含标点")
    if not skill.tools:
        add("error", "工具", "至少声明一个用到的工具")
    unknown = [t for t in skill.tools if t not in known_tools]
    if unknown:
        add("error", "工具", "不存在: " + "、".join(unknown))
    mentioned = set(re.findall(r"\b(?:get|list|run)_[a-z_]+\b", skill.body))
    undeclared = sorted(t for t in mentioned if t not in skill.tools)
    if undeclared:
        add("warn", "工具", "步骤里提到但未声明: " + "、".join(undeclared))
    taken: dict[str, str] = {}
    for other in others:
        if other.name == skill.name or not other.enabled:
            continue
        for trig in other.triggers:
            taken[_norm_trigger(trig)] = other.title
    for trig in skill.triggers:
        owner = taken.get(_norm_trigger(trig))
        if owner:
            add("error", "触发词", f"「{trig}」已被「{owner}」使用")
    if "数据时点" not in skill.body and "时点" not in skill.body and "as_of" not in skill.body:
        add("warn", "数据时点", "步骤里没有要求标注数据时点")
    return out


def _norm_trigger(trig: str) -> str:
    return trig.strip().lstrip("/").lower()


# ----------------------------------------------------------------
# 存储
# ----------------------------------------------------------------

def _root(data_dir: Path | str) -> Path:
    return Path(data_dir) / "user_data" / "assistant_skills"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _builtin() -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    for path in sorted(BUILTIN_DIR.glob("*.md")):
        try:
            skill = parse(path.read_text(encoding="utf-8"), source="builtin")
        except ValueError:
            continue
        out[skill.name] = skill
    return out


def _user(data_dir: Path | str) -> dict[str, Skill]:
    root = _root(data_dir)
    out: dict[str, Skill] = {}
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        cur = _read_json(d / "current.json", {})
        version = int(cur.get("version") or 0)
        path = d / f"v{version}.md"
        if not version or not path.exists():
            continue
        try:
            skill = parse(path.read_text(encoding="utf-8"), source="user", version=version)
        except ValueError:
            continue
        if skill.name != d.name:
            continue
        out[skill.name] = skill
    return out


def _enabled_map(data_dir: Path | str) -> dict[str, bool]:
    raw = _read_json(_root(data_dir) / "enabled.json", {})
    return {str(k): bool(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def load_all(data_dir: Path | str | None) -> list[Skill]:
    """全部技能 (内置 + 用户); 同名时用户技能覆盖内置 (复制改过的版本优先)。"""
    skills = _builtin()
    if data_dir is not None:
        skills.update(_user(data_dir))
        enabled = _enabled_map(data_dir)
        for name, skill in skills.items():
            skill.enabled = enabled.get(name, True)
    order = {"每日": 0, "研究": 1, "策略": 2, "系统": 3}
    return sorted(skills.values(), key=lambda s: (order.get(s.group, 9), s.source != "builtin", s.name))


def get(data_dir: Path | str | None, name: str) -> Skill | None:
    return next((s for s in load_all(data_dir) if s.name == name), None)


def versions(data_dir: Path | str, name: str) -> list[dict[str, Any]]:
    d = _root(data_dir) / name
    if not d.exists():
        return []
    cur = int((_read_json(d / "current.json", {}) or {}).get("version") or 0)
    meta = _read_json(d / "versions.json", [])
    out = []
    for item in meta if isinstance(meta, list) else []:
        v = int(item.get("version") or 0)
        if (d / f"v{v}.md").exists():
            out.append({**item, "current": v == cur})
    return sorted(out, key=lambda x: -x["version"])


def save(data_dir: Path | str, text: str, *, known_tools: set[str], note: str = "") -> dict[str, Any]:
    """校验后保存为新版本 (内置技能不可覆盖, 只能另起名)。返回 {skill, checks}。"""
    with _LOCK:
        skill = parse(text)
        existing = load_all(data_dir)
        builtin_names = {s.name for s in existing if s.source == "builtin"}
        user_names = {s.name for s in existing if s.source == "user"}
        if skill.name in builtin_names and skill.name not in user_names:
            raise ValueError(f"「{skill.name}」是内置技能名, 请先复制为我的技能再修改")
        if skill.name not in user_names and len(user_names) >= MAX_SKILLS:
            raise ValueError(f"自建技能最多 {MAX_SKILLS} 个")
        checks = check(skill, known_tools=known_tools, others=existing)
        if any(c["level"] == "error" for c in checks):
            return {"saved": False, "checks": checks, "skill": skill.summary()}
        d = _root(data_dir) / skill.name
        d.mkdir(parents=True, exist_ok=True)
        meta = _read_json(d / "versions.json", [])
        meta = meta if isinstance(meta, list) else []
        version = max([int(m.get("version") or 0) for m in meta] + [0]) + 1
        skill.version = version
        atomic_write_text(d / f"v{version}.md", skill.to_markdown())
        meta.append({"version": version, "saved_at": us_clock.now().isoformat(timespec="seconds"),
                     "note": note[:80], "warnings": sum(1 for c in checks if c["level"] == "warn")})
        atomic_write_text(d / "versions.json", json.dumps(meta[-MAX_VERSIONS:], ensure_ascii=False, indent=1))
        atomic_write_text(d / "current.json", json.dumps({"version": version}))
        return {"saved": True, "checks": checks, "skill": skill.summary()}


def rollback(data_dir: Path | str, name: str, version: int) -> dict[str, Any]:
    with _LOCK:
        d = _root(data_dir) / name
        if not (d / f"v{int(version)}.md").exists():
            raise ValueError(f"「{name}」没有 v{version}")
        atomic_write_text(d / "current.json", json.dumps({"version": int(version)}))
        return {"name": name, "version": int(version)}


def delete(data_dir: Path | str, name: str) -> None:
    """删除用户技能: 只移走现行指针 (版本文件保留, 可从磁盘手动恢复); 内置技能不能删, 只能停用。"""
    with _LOCK:
        d = _root(data_dir) / name
        if not (d / "current.json").exists():
            raise ValueError(f"「{name}」不是自建技能, 内置技能只能停用")
        (d / "current.json").rename(d / f"deleted-{datetime.now().strftime('%Y%m%d%H%M%S')}.json")


def set_enabled(data_dir: Path | str, name: str, enabled: bool) -> None:
    with _LOCK:
        if get(data_dir, name) is None:
            raise ValueError(f"没有技能「{name}」")
        root = _root(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        cur = _enabled_map(data_dir)
        cur[name] = bool(enabled)
        atomic_write_text(root / "enabled.json", json.dumps(cur, ensure_ascii=False, indent=1))


# ----------------------------------------------------------------
# 运行记录 (只记元数据)
# ----------------------------------------------------------------

def record_run(data_dir: Path | str | None, name: str, *, ok: bool, tools: int, ms: int) -> None:
    if data_dir is None:
        return
    with _LOCK:
        root = _root(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        path = root / "runs.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        lines.append(json.dumps({"skill": name, "at": us_clock.now().isoformat(timespec="seconds"),
                                 "ok": bool(ok), "tools": int(tools), "ms": int(ms)}, ensure_ascii=False))
        atomic_write_text(path, "\n".join(lines[-MAX_RUNS:]) + "\n")


def recent_runs(data_dir: Path | str | None, name: str | None = None, limit: int = 7) -> list[dict[str, Any]]:
    if data_dir is None:
        return []
    path = _root(data_dir) / "runs.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if name is None or row.get("skill") == name:
            rows.append(row)
    return rows[-limit:]


# ----------------------------------------------------------------
# 对话里使用
# ----------------------------------------------------------------

def match(data_dir: Path | str | None, text: str) -> Skill | None:
    """用户消息以触发词开头 (如「/持仓 …」) → 对应技能; 只匹配启用的技能。"""
    head = (text or "").strip()
    if not head.startswith("/"):
        return None
    token = head.split(maxsplit=1)[0]
    for skill in load_all(data_dir):
        if skill.enabled and any(_norm_trigger(t) == _norm_trigger(token) for t in skill.triggers):
            return skill
    return None


def instruction(skill: Skill) -> str:
    """注入 system prompt 的技能说明 (只描述流程, 不改变工具权限)。"""
    tools = "、".join(skill.tools)
    out = "对话" if skill.output == "chat" else "报告 (用小标题分节, 结尾列出数据时点)"
    return (
        f"\n\n本轮使用技能「{skill.title}」(只读流程, 不改变你的工具权限)。按下面步骤完成, "
        f"优先使用这些工具: {tools}。输出形式: {out}。\n"
        f"--- 技能步骤 ---\n{skill.body}\n--- 结束 ---"
    )
