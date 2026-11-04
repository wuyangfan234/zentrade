"""自定义/复合因子存储 (P3) — data/user_data/custom_factors/*.json。

镜像 custom_signals 的持久化写法; 单文件损坏只禁用该因子并告警, 不影响启动
(对齐 CONTRIBUTING 第 4 节插件隔离要求)。生命周期状态: draft → active →
watch → retired (P4 状态机, 存储字段就绪, 迁移逻辑见巡检设计)。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from app.factors.dsl import compile_formula
from app.factors.registry import FactorSpec, factor_dependencies, get_factor, register_factor
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

CUSTOM_ID_PATTERN = re.compile(r"^uf_[a-z0-9_]{1,40}$")
COMPOSITE_ID_PATTERN = re.compile(r"^cf_[a-z0-9_]{1,40}$")
MAX_COMPOSITE_MEMBERS = 8
STATUSES = frozenset({"draft", "active", "watch", "retired"})


def _dir(data_dir: Path) -> Path:
    directory = data_dir / "user_data" / "custom_factors"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _path(data_dir: Path, factor_id: str) -> Path:
    return _dir(data_dir) / f"{factor_id}.json"


def load_all(data_dir: Path) -> list[dict]:
    """读取全部自定义/复合因子定义; 损坏文件与顶层非对象的文件跳过。"""
    out: list[dict] = []
    for file in sorted(_dir(data_dir).glob("*.json")):
        try:
            definition = json.loads(file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("custom factor load failed %s: %s", file.name, exc)
            continue
        # 顶层是 [] / 字符串时旧实现在启动注册里抛 AttributeError, 排在它后面的因子全部静默丢失
        if not isinstance(definition, dict):
            logger.warning("custom factor load skipped %s: 顶层不是对象", file.name)
            continue
        out.append(definition)
    return out


def save_one(data_dir: Path, definition: dict) -> None:
    target = _path(data_dir, str(definition["id"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(definition, ensure_ascii=False, indent=2))


def delete_one(data_dir: Path, factor_id: str) -> bool:
    target = _path(data_dir, factor_id)
    if target.exists():
        target.unlink()
        return True
    return False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def to_spec(definition: dict) -> FactorSpec:
    """定义 → FactorSpec; 校验失败抛 ValueError (调用方 fail-closed)。

    custom: 依赖/预热由 DSL 编译推导 (编译失败即拒绝注册)。
    composite: 依赖 = 成员递归展开; 预热 = 成员最大值; 循环引用拒绝。
    """
    kind = str(definition.get("kind", "custom"))
    factor_id = str(definition.get("id", ""))
    label = str(definition.get("label", "")).strip()
    if not label:
        raise ValueError("label 不能为空")
    pattern = COMPOSITE_ID_PATTERN if kind == "composite" else CUSTOM_ID_PATTERN
    if not pattern.match(factor_id):
        raise ValueError(f"id 必须匹配 {pattern.pattern}")
    status = str(definition.get("status", "draft"))
    if status not in STATUSES:
        raise ValueError(f"status 必须是 {sorted(STATUSES)} 之一")

    if kind == "custom":
        formula = str(definition.get("formula", ""))
        compiled = compile_formula(formula)
        if not compiled.ok:
            first = compiled.errors[0]
            raise ValueError(f"公式无效 [{first.code}]: {first.message}")
        return FactorSpec(
            id=factor_id,
            label=label,
            group=str(definition.get("group", "自定义")),
            formula_text=formula,
            kind="custom",
            version=int(definition.get("version", 1)),
            dependencies=frozenset(compiled.dependencies),
            warmup_bars=compiled.warmup_bars,
            direction=str(definition.get("direction", "none")),  # type: ignore[arg-type]
            stability="stable" if status == "active" else "experimental",
        )

    if kind != "composite":
        raise ValueError(f"未知 kind: {kind}")
    members_raw = definition.get("members")
    if not isinstance(members_raw, dict) or not (2 <= len(members_raw) <= MAX_COMPOSITE_MEMBERS):
        raise ValueError(f"composite 成员必须是 {2}~{MAX_COMPOSITE_MEMBERS} 个")
    from app.factors.dsl import BASE_COLUMNS

    components: list[tuple[str, float]] = []
    for member_id, weight in members_raw.items():
        member_id = str(member_id)
        if member_id == factor_id:
            raise ValueError("composite 不能引用自身")
        try:
            weight = float(weight)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"成员 {member_id} 权重必须是数字") from exc
        if not weight:
            raise ValueError(f"成员 {member_id} 权重不能为 0")
        # 成员 = 注册表因子 或 enriched 基准列 (已物化, 可直接参与组合)
        if get_factor(member_id) is None and member_id not in BASE_COLUMNS:
            raise ValueError(f"未知成员因子: {member_id}")
        components.append((member_id, weight))
    # 环检测: 沿成员链做 DFS, 只把「当前递归路径」上的节点当环。
    # 旧实现用全局 seen, 父子两层共用同一个基础列 (都含 volume) 就被误判为环。
    def _reaches_self(member_id: str, path: frozenset[str]) -> bool:
        if member_id == factor_id or member_id in path:
            return True
        member_spec = get_factor(member_id)
        if member_spec is None or member_spec.kind != "composite":
            return False
        next_path = path | {member_id}
        return any(_reaches_self(child, next_path) for child, _ in member_spec.components)

    if any(_reaches_self(member_id, frozenset()) for member_id, _ in components):
        raise ValueError("composite 成员存在循环引用")
    dependencies = factor_dependencies([member_id for member_id, _ in components])
    warmup = max(
        ((get_factor(member_id).warmup_bars if get_factor(member_id) else 1) for member_id, _ in components),
        default=1,
    )
    formula_text = " + ".join(
        f"{weight:g}*zscore({member_id})" for member_id, weight in components
    )
    return FactorSpec(
        id=factor_id,
        label=label,
        group=str(definition.get("group", "组合")),
        formula_text=formula_text,
        kind="composite",
        version=int(definition.get("version", 1)),
        dependencies=dependencies,
        warmup_bars=warmup,
        direction=str(definition.get("direction", "none")),  # type: ignore[arg-type]
        components=tuple(components),
        stability="stable" if status == "active" else "experimental",
    )


def register_definition(definition: dict) -> FactorSpec:
    """定义 → spec → 注册 (重复 id 版本未升时由注册表拒绝)。"""
    spec = to_spec(definition)
    register_factor(spec)
    return spec


def load_into_registry(data_dir: Path) -> list[str]:
    """启动期把存储中的因子注册进注册表; 单个失败只跳过并告警。

    多轮加载: 定义之间可能互相引用 (composite 成员、custom 公式里的 uf_*), 文件按字母序
    加载, 被引用方可能排在后面。失败的定义延后重试, 直到某一轮没有新加载成功为止
    (旧实现固定 3 轮, 4 级以上反向字母序链会漏载最上层)。剩下的统一告警, 不阻塞启动。
    """
    loaded: list[str] = []
    pending = list(load_all(data_dir))
    while pending:
        deferred: list[tuple[dict, Exception]] = []
        for definition in pending:
            try:
                register_definition(definition)
                loaded.append(str(definition["id"]))
            except ValueError as exc:
                deferred.append((definition, exc))
            except Exception as exc:
                logger.warning("custom factor 注册失败 %s: %s", definition.get("id"), exc)
        if len(deferred) == len(pending):
            for definition, exc in deferred:
                logger.warning("custom factor 注册失败 %s: %s", definition.get("id"), exc)
            break
        pending = [definition for definition, _ in deferred]
    return loaded
