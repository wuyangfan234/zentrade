"""创建策略弹窗的只读辅助: 分项带行号校验 / 模板库 / 语法标准摘要 / 引擎英文报错的中文说明。

判定一律调用现行校验器 (app.strategy.ai_generator) 的同一批函数, 本模块只把结果分到四项并找出行号,
不另立规则; 「入口」一项对照引擎装载规则 (engine._load_file 的五种执行方式), 由测试逐例比对。
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
from functools import lru_cache
from pathlib import Path

from app.indicators.pipeline import ENRICHED_COLUMNS
from app.strategy.ai_generator import (
    AIStrategyGenerator,
    _strategy_execution_backend,
    _top_level_assignment,
    find_meta_assignment,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "strategy_templates"

# 五种执行方式: 与 engine._load_file 的 valid_backends 一一对应 (测试比对)
BACKENDS: tuple[dict, ...] = (
    {"id": "polars_expr", "label": "单日过滤", "must": "filter", "forbid": "filter_history"},
    {"id": "python_history_legacy", "label": "历史窗口", "must": "filter_history", "forbid": "filter"},
    {"id": "matrix_native", "label": "矩阵", "must": "MATRIX_STRATEGY", "forbid": "filter / filter_history"},
    {"id": "composite", "label": "叠加", "must": "META.children", "forbid": "业务代码"},
    {"id": "minute_filter", "label": "分钟", "must": "filter_minute_history", "forbid": "日线入口"},
)
_BACKEND_LABEL = {b["id"]: b["label"] for b in BACKENDS}

# 语法标准 (docs/strategy-standard.md) 的章节标题, 测试与文档比对
STANDARD_TOC: tuple[str, ...] = (
    "文件",
    "META",
    "执行方式",
    "矩阵写法的契约",
    "允许的 import",
    "禁止的调用与访问",
    "只是约定、机器不检查的",
    "保存前自检",
    "这份标准不管的",
)

# 引擎与矩阵内核的英文报错 → 中文说明与处理办法。sample 是源码里的原文片段 (测试核对仍在源码中)。
ENGINE_ERRORS: tuple[dict, ...] = (
    {"sample": "polars_expr strategy must declare only filter",
     "pattern": r"polars_expr strategy must declare only filter",
     "meaning": "单日过滤写法只能有 filter 一个入口", "fix": "补上 filter，并删掉 filter_history"},  # noqa: RUF001
    {"sample": "python_history_legacy strategy must declare only filter_history",
     "pattern": r"python_history_legacy strategy must declare only filter_history",
     "meaning": "历史窗口写法只能有 filter_history 一个入口", "fix": "补上 filter_history，并删掉 filter"},  # noqa: RUF001
    {"sample": "matrix_native strategy must declare MATRIX_STRATEGY",
     "pattern": r"matrix_native strategy must declare MATRIX_STRATEGY",
     "meaning": "矩阵写法缺少 MATRIX_STRATEGY 对象", "fix": "补上 MATRIX_STRATEGY，或改用别的执行方式"},  # noqa: RUF001
    {"sample": "MATRIX_STRATEGY must implement MatrixStrategy",
     "pattern": r"MATRIX_STRATEGY must implement MatrixStrategy",
     "meaning": "MATRIX_STRATEGY 缺少必需的方法",
     "fix": "补齐 required_fields、required_warmup_bars、compute_signals"},
    {"sample": "matrix_native strategy must not declare filter or filter_history",
     "pattern": r"matrix_native strategy must not declare filter or filter_history",
     "meaning": "矩阵写法里不能再写 filter 或 filter_history", "fix": "删掉这两个函数"},
    {"sample": "composite strategy must not declare filter, filter_history or MATRIX_STRATEGY",
     "pattern": r"composite strategy must not declare filter, filter_history or MATRIX_STRATEGY",
     "meaning": "叠加策略里不能写业务代码", "fix": "只保留 META.children"},
    {"sample": "composite strategy META['children'] must be a non-empty list",
     "pattern": r"composite strategy META\['children'\] must be a non-empty list",
     "meaning": "叠加策略没有子策略", "fix": "在 META.children 里至少写一个子策略"},
    {"sample": "minute_filter strategy must declare only filter_minute_history",
     "pattern": r"minute_filter strategy must declare only filter_minute_history",
     "meaning": "分钟写法只能有 filter_minute_history 一个入口", "fix": "补上它，并删掉日线入口"},  # noqa: RUF001
    {"sample": "minute_filter strategy must declare timeframes == ['1m']",
     "pattern": r"minute_filter strategy must declare timeframes == \['1m'\]",
     "meaning": "分钟写法的周期必须是 1 分钟", "fix": '把 META.timeframes 改成 ["1m"]'},
    {"sample": "minute_filter daily_history_bars must be within [0, 250]",
     "pattern": r"minute_filter daily_history_bars must be within \[0, 250\]",
     "meaning": "日线历史根数超出范围", "fix": "daily_history_bars 改到 0 至 250 之间"},
    {"sample": "filter_minute_history to accept a 'daily' keyword",
     "pattern": r"minute_filter daily_history_bars requires\s+filter_minute_history to accept a 'daily' keyword",
     "meaning": "声明了日线历史，但函数不接收它", "fix": "给 filter_minute_history 加上 daily=None 关键字参数"},  # noqa: RUF001
    {"sample": "unsupported execution backend",
     "pattern": r"unsupported execution backend '?([\w.-]*)'?(?:; expected one of \[[^\]]*\])?",
     "meaning": "执行方式写错了", "fix": "EXECUTION_BACKEND 只能是五种执行方式之一"},
    {"sample": "must be a non-empty string list",
     "pattern": r"META\[['\"]?(\w+)['\"]?\] must be a non-empty string list",
     "meaning": "META 里这一项必须是非空的字符串列表", "fix": '照 ["stock"]、["1d"] 的样子写'},
    {"sample": "SignalMatrix.score must contain only finite values",
     "pattern": r"SignalMatrix\.score must contain only finite values",
     "meaning": "评分里有空值或无穷大", "fix": "用 np.where(np.isfinite(x), x, 0.0) 兜底"},
    {"sample": "must use {dtype}, got {array.dtype}",
     "pattern": r"SignalMatrix\.(\w+) must use (\w+), got (\w+)",
     "meaning": "信号矩阵的数组类型不对", "fix": "entry 与 exit 用 astype(np.uint8)，score 用 astype(np.float32)"},  # noqa: RUF001
    {"sample": "cannot load module from",
     "pattern": r"cannot load module from \S+",
     "meaning": "策略文件无法作为 Python 模块载入", "fix": "检查文件名与语法"},
)
_ENGINE_ERROR_RES = tuple((re.compile(e["pattern"]), e) for e in ENGINE_ERRORS)


def translate_error(message: str) -> str:
    """把引擎的英文报错换成中文说明 + 处理办法, 原文附在末尾; 认不出的原样返回。"""
    text = str(message or "")
    for regex, entry in _ENGINE_ERROR_RES:
        m = regex.search(text)
        if m:
            head = "策略保存失败：" if text.startswith("策略保存失败") else ""  # noqa: RUF001
            return f"{head}{entry['meaning']}。处理：{entry['fix']}。原文：{m.group(0)}"  # noqa: RUF001
    return text


# ───────────────────────── 分项校验 ─────────────────────────

_SAFETY_LOCATORS = (
    (re.compile(r"^禁止 import (\S+)"), "import"),
    (re.compile(r"^禁止 from (\S+) import"), "from"),
    (re.compile(r"^禁止调用 (\w+)\(\)"), "call"),
    (re.compile(r"^禁止访问属性 (\w+)"), "attr"),
    (re.compile(r"^禁止下标访问 (\w+)"), "subscript"),
)


def _locate_safety(tree: ast.Module, message: str) -> tuple[int | None, str]:
    """安全校验只报第一处; 按同样的遍历顺序找回那个节点的行号。返回 (行号, 类别)。"""
    for regex, kind in _SAFETY_LOCATORS:
        m = regex.match(message)
        if not m:
            continue
        name = m.group(1)
        for node in ast.walk(tree):
            if kind == "import" and isinstance(node, ast.Import) and any(a.name == name for a in node.names):
                return node.lineno, kind
            if kind == "from" and isinstance(node, ast.ImportFrom) and str(node.module) == name:
                return node.lineno, kind
            if kind == "call" and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == name:
                return node.lineno, kind
            if kind == "attr" and isinstance(node, ast.Attribute) and node.attr == name:
                return node.lineno, kind
            if kind == "subscript" and isinstance(node, ast.Subscript) \
                    and isinstance(node.slice, ast.Constant) and node.slice.value == name:
                return node.lineno, kind
        return None, kind
    return None, ""


def _meta_key_line(meta_node: ast.Dict | None, key: str) -> int | None:
    if meta_node is None:
        return None
    for k in meta_node.keys:
        if isinstance(k, ast.Constant) and k.value == key:
            return k.lineno
    return None


def _meta_value_node(meta_node: ast.Dict | None, key: str) -> ast.expr | None:
    if meta_node is None:
        return None
    for k, v in zip(meta_node.keys, meta_node.values, strict=False):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _first_string_line(tree: ast.AST, value: str) -> int | None:
    lines = [
        node.lineno for node in ast.walk(tree)
        if (isinstance(node, ast.Constant) and node.value == value)
        or (isinstance(node, ast.Attribute) and node.attr == value)
    ]
    return min(lines) if lines else None


def _semantics_error(code: str, meta: dict) -> str | None:
    try:
        AIStrategyGenerator._validate_meta_semantics(code, meta)
    except ValueError as e:
        return str(e)
    return None


def _is_field_error(message: str) -> bool:
    return message.startswith(("美股策略", "META.scoring 引用了不可用字段"))


def _meta_only_error(meta: dict) -> str | None:
    """只查 META 自身的结构 (参数 id、评分权重与合计), 不受字段问题干扰。"""
    scoring = meta.get("scoring", {})
    if isinstance(scoring, dict):
        scoring = {
            (f"w{i}" if isinstance(k, str) and k else k): v
            for i, (k, v) in enumerate(scoring.items())
        }
    probe = {"params": meta.get("params", []), "scoring": scoring}
    return _semantics_error('EXECUTION_BACKEND = "python_history_legacy"\n', probe)


def _fields_only_error(code: str, meta: dict) -> str | None:
    """只查字段是否可用, 不受 META 结构问题干扰。"""
    scoring = meta.get("scoring", {})
    names = [k for k in scoring if isinstance(k, str) and k] if isinstance(scoring, dict) else []
    probe = {**meta, "params": [], "scoring": {k: 1.0 / len(names) for k in names}}
    message = _semantics_error(code, probe)
    return message if message and _is_field_error(message) else None


def _item(key: str, label: str, state: str, text: str, *, line: int | None = None,
          detail: str = "", section: int | None = None) -> dict:
    return {
        "key": key, "label": label, "state": state, "text": text, "line": line,
        "detail": detail, "section": section if state == "bad" else None,
    }


def _skip(key: str, label: str) -> dict:
    return _item(key, label, "skip", "前一项修正后再查")


def _toplevel_names(tree: ast.Module) -> dict[str, int]:
    names: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.setdefault(node.name, node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.setdefault(target.id, node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            names.setdefault(node.target.id, node.lineno)
    return names


def _check_entry(tree: ast.Module, meta: dict) -> dict:
    """对照引擎装载规则: 每种执行方式必须有且只有自己的入口。"""
    backend = _strategy_execution_backend(tree, meta)
    names = _toplevel_names(tree)
    be_line = names.get("EXECUTION_BACKEND")
    label = _BACKEND_LABEL.get(backend)

    def bad(text: str, line: int | None = None) -> dict:
        return _item("entry", "入口", "bad", text, line=line if line is not None else be_line, section=3)

    if label is None:
        return bad(f"执行方式 {backend} 不支持")
    entries = ("filter", "filter_history", "filter_minute_history", "MATRIX_STRATEGY")
    own = {
        "polars_expr": "filter", "python_history_legacy": "filter_history",
        "matrix_native": "MATRIX_STRATEGY", "minute_filter": "filter_minute_history", "composite": None,
    }[backend]
    # 引擎对各执行方式禁止的其他入口 (minute 入口只在 minute_filter 下检查)
    forbid = {
        "polars_expr": ("filter_history",), "python_history_legacy": ("filter",),
        "matrix_native": ("filter", "filter_history"),
        "composite": ("filter", "filter_history", "MATRIX_STRATEGY"),
        "minute_filter": ("filter", "filter_history", "MATRIX_STRATEGY"),
    }[backend]
    if own is not None and own not in names:
        return bad(f"缺入口 {own}")
    for other in entries:
        if other in forbid and other in names:
            return bad(f"{label}写法不能再写 {other}", names[other])
    if backend == "composite":
        children = meta.get("children")
        if not isinstance(children, (list, tuple)) or not children:
            return bad("缺子策略 META.children")
        return _item("entry", "入口", "ok", f"子策略 {len(children)} 个 · {label}")
    if backend == "minute_filter" and meta.get("timeframes") != ["1m"]:
        return bad('周期须为 ["1m"]')
    return _item("entry", "入口", "ok", f"{own} · {label}")


def _literal_top(tree: ast.Module, name: str):
    found = _top_level_assignment(tree, name)
    if found is None or found[1] is None:
        return None
    try:
        return ast.literal_eval(found[1])
    except (ValueError, SyntaxError):
        return None


def _num(value) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def check_code(code: str) -> dict:
    """四项检查: 安全 / META / 入口 / 字段。每项 ok、bad 或 skip (前项未过无法判断), bad 尽量带行号。"""
    code = str(code or "")
    overview = {"lines": len(code.splitlines()), "params": None, "backend": None, "backend_label": None,
                "stop_loss": None, "max_hold_days": None}
    labels = (("safety", "安全"), ("meta", "META"), ("entry", "入口"), ("fields", "字段"))

    def result(items: list[dict]) -> dict:
        return {"valid": all(i["state"] == "ok" for i in items), "items": items, "overview": overview}

    if not code.strip():
        return result([_skip(k, lb) for k, lb in labels])
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        first = _item("safety", "安全", "bad", f"语法错误 · {e.msg}", line=e.lineno,
                      detail=f"Python 语法错误: {e.msg}", section=1)
        return result([first] + [_skip(k, lb) for k, lb in labels[1:]])

    # 1. 安全
    try:
        AIStrategyGenerator._validate_safety(code)
        n_imports = sum(
            len(n.names) if isinstance(n, ast.Import) else 1
            for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        )
        safety = _item("safety", "安全", "ok", f"import {n_imports} 个 · 均在清单内")
    except ValueError as e:
        message = str(e)
        line, kind = _locate_safety(tree, message)
        safety = _item("safety", "安全", "bad", message.split(" (", 1)[0], line=line, detail=message,
                       section=5 if kind in {"import", "from"} else 6)

    # 2. META
    meta: dict | None = None
    meta_node: ast.Dict | None = None
    meta_line: int | None = None
    try:
        found = find_meta_assignment(code)
        if found is None:
            raise ValueError("找不到 META 字典")
        meta_line, meta_node = found[0].lineno, found[1]
        meta = AIStrategyGenerator._extract_meta(code)
    except ValueError as e:
        message = str(e)
        text = "META 须是纯字面量字典" if "字面量" in message else message
        meta_item = _item("meta", "META", "bad", text, line=meta_line, detail=message, section=2)
        return result([safety, meta_item, _skip("entry", "入口"), _skip("fields", "字段")])

    params = meta.get("params")
    n_params = len(params) if isinstance(params, (list, tuple)) else 0
    scoring = meta.get("scoring", {})
    backend = _strategy_execution_backend(tree, meta)
    overview.update({
        "params": n_params, "backend": backend, "backend_label": _BACKEND_LABEL.get(backend),
        "stop_loss": _num(_literal_top(tree, "STOP_LOSS")),
        "max_hold_days": _num(_literal_top(tree, "MAX_HOLD_DAYS")),
    })

    full_error = _semantics_error(code, meta)
    meta_error = _meta_only_error(meta)
    fields_error = _fields_only_error(code, meta)
    # 兜底: 两个分项探针都没报, 但整体校验报了 → 按消息归类, 保证不会比现行校验宽
    if full_error and not meta_error and not fields_error:
        if _is_field_error(full_error):
            fields_error = full_error
        else:
            meta_error = full_error

    if meta_error:
        line, text = _meta_key_line(meta_node, "scoring"), meta_error
        m = re.match(r"META\.params\[(\d+)\]", meta_error)
        if m:
            node = _meta_value_node(meta_node, "params")
            index = int(m.group(1))
            if isinstance(node, (ast.List, ast.Tuple)) and index < len(node.elts):
                line = node.elts[index].lineno
            else:
                line = _meta_key_line(meta_node, "params")
            text = f"第 {index + 1} 个参数缺 id"
        elif "权重总和" in meta_error and isinstance(scoring, dict):
            total = sum(float(v) for v in scoring.values())
            text = f"评分合计 {round(total, 6):g} · 应为 1.0"
        meta_item = _item("meta", "META", "bad", text, line=line, detail=meta_error, section=2)
    else:
        tail = "评分合计 1.0" if isinstance(scoring, dict) and scoring else "不评分"
        meta_item = _item("meta", "META", "ok", f"参数 {n_params} 个 · {tail}")

    # 3. 入口
    entry = _check_entry(tree, meta)

    # 4. 字段
    if fields_error:
        line, text = None, fields_error
        m = re.search(r"字段: (\w+)$", fields_error) or re.search(r"过滤: (\w+)$", fields_error)
        if m:
            line = _first_string_line(tree, m.group(1))
            text = f"字段 {m.group(1)} 不可用"
        elif fields_error.startswith("META.scoring 引用了不可用字段"):
            line = _meta_key_line(meta_node, "scoring")
            names = re.findall(r"'(\w+)'", fields_error.split(";", 1)[0])
            text = f"评分字段 {'、'.join(names)} 不可用" if names else "评分字段不可用"
        fields = _item("fields", "字段", "bad", text, line=line, detail=fields_error, section=2)
    else:
        known = set(ENRICHED_COLUMNS)
        used = {
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in known
        }
        fields = _item("fields", "字段", "ok", f"{len(used)} 个 · 均可用")

    return result([safety, meta_item, entry, fields])


# ───────────────────────── 语法标准摘要 ─────────────────────────

@lru_cache(maxsize=1)
def _safety_sets() -> dict[str, list[str]]:
    """安全校验里的名单是函数内的局部集合, 从源码读出来展示 (测试核对与文档一致)。"""
    out: dict[str, list[str]] = {"forbidden_calls": [], "forbidden_dunder_attrs": [], "forbidden_subscript_strs": []}
    try:
        source = textwrap.dedent(inspect.getsource(AIStrategyGenerator._validate_safety))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return out
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in out:
                value = node.value.left if isinstance(node.value, ast.BinOp) else node.value
                if isinstance(value, ast.Set):
                    out[target.id] = sorted(ast.literal_eval(e) for e in value.elts)
    return out


def standard_digest() -> dict:
    sets = _safety_sets()
    return {
        "toc": [{"n": i + 1, "title": t} for i, t in enumerate(STANDARD_TOC)],
        "backends": [dict(b) for b in BACKENDS],
        "allowed_imports": sorted(AIStrategyGenerator._ALLOWED_IMPORT_MODULES),
        "forbidden_calls": sets["forbidden_calls"],
        "forbidden_attrs": sets["forbidden_dunder_attrs"],
        "errors": [{"sample": e["sample"], "meaning": e["meaning"], "fix": e["fix"]} for e in ENGINE_ERRORS],
    }


# ───────────────────────── 模板库 ─────────────────────────

@lru_cache(maxsize=1)
def list_templates() -> tuple[dict, ...]:
    """模板 = 同名的 .json (名称、难度、排序) + .pytpl (完整策略源码); 行数、参数个数、写法由源码算出。"""
    out = []
    for info_path in sorted(TEMPLATE_DIR.glob("*.json")):
        code_path = info_path.with_suffix(".pytpl")
        if not code_path.exists():
            continue
        info = json.loads(info_path.read_text(encoding="utf-8"))
        code = code_path.read_text(encoding="utf-8")
        tree = ast.parse(code)
        meta = AIStrategyGenerator._extract_meta(code)
        backend = _strategy_execution_backend(tree, meta)
        params = meta.get("params")
        out.append({
            "id": info_path.stem,
            "name": str(info["name"]),
            "level": str(info["level"]),
            "order": int(info.get("order", 0)),
            "backend": backend,
            "kind": _BACKEND_LABEL.get(backend, backend),
            "lines": len(code.splitlines()),
            "params": len(params) if isinstance(params, (list, tuple)) else 0,
            "code": code,
        })
    out.sort(key=lambda t: (t["order"], t["id"]))
    return tuple(out)
