"""因子注册表 API — 因子库 (P1) + 公式校验/试算 (P2) + 自定义/复合因子 CRUD (P3)。"""
from __future__ import annotations

import contextlib
from datetime import date, timedelta

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.factors import store
from app.factors.dsl import FACTOR_COLUMN, compile_formula
from app.factors.registry import (
    factor_available,
    factor_history_available,
    selectable_factors,
    unregister_factor,
)

router = APIRouter(prefix="/api/factors", tags=["factors"])


def _custom_meta(request: Request | None) -> dict[str, dict]:
    """0.47.0: 自定义/复合因子的状态与来源 (内置 / 挖掘 / 采集 / 自写), 供因子库「来源」列。"""
    if request is None or not hasattr(request, "app"):
        return {}
    try:
        defs = store.load_all(_data_dir(request))
    except Exception:  # library still renders without the extra columns
        return {}
    out = {}
    for d in defs:
        src = (d.get("source") or {}).get("type") if isinstance(d.get("source"), dict) else None
        out[str(d.get("id"))] = {"status": d.get("status") or "draft",
                                 "origin": "intake" if src == "intake" else ("mined" if src == "mined" else "custom"),
                                 "description": str(d.get("description") or "")}
    return out


@router.get("")
def list_factors(asset_type: str | None = Query(default=None, pattern="^(stock|etf)$"), request: Request = None) -> dict:  # type: ignore[assignment]
    """注册表因子列表; asset_type 过滤适用资产 (财务因子仅股票)。"""
    specs = selectable_factors(asset_type=asset_type)
    meta = _custom_meta(request)
    return {
        "factors": [
            {
                "id": spec.id,
                "label": spec.label,
                "group": spec.group,
                "kind": spec.kind,
                "version": spec.version,
                "formula": spec.formula_text + ("；当前数据源缺失，需先接入历史财务/股本数据" if spec.pit or "turnover" in spec.id else ""),
                "direction": spec.direction,
                "unit": spec.unit,
                "warmup_bars": spec.warmup_bars,
                "pit": spec.pit,
                "asset_types": sorted(spec.asset_types),
                "stability": spec.stability,
                "scale_free": spec.scale_free,
                "dependencies": sorted(spec.dependencies),
                "origin": (meta.get(spec.id) or {}).get("origin") or ("custom" if spec.kind in ("custom", "composite") else "builtin"),
                "status": (meta.get(spec.id) or {}).get("status") or ("active" if spec.kind in ("base", "virtual") else "draft"),
                "description": (meta.get(spec.id) or {}).get("description", ""),
                "available": factor_available(spec),
                # 当前可用 ≠ 历史可检验: 扩展表快照字段只有当日值 (信号可用, 检验整段为空)
                "history_available": factor_history_available(spec),
            }
            for spec in specs
        ]
    }


class FormulaValidateRequest(BaseModel):
    formula: str = Field(..., min_length=1, max_length=2000)


def _compiled_payload(compiled) -> dict:
    return {
        "ok": compiled.ok,
        "errors": [error.to_dict() for error in compiled.errors],
        "dependencies": sorted(compiled.dependencies),
        "referenced_factors": sorted(compiled.referenced_factors),
        "warmup_bars": compiled.warmup_bars,
        "cross_sectional": compiled.cross_sectional,
    }


@router.post("/validate")
def validate_formula(req: FormulaValidateRequest) -> dict:
    """公式校验: 语法/语义/窗口纪律/依赖推导, 编译期 fail-closed。"""
    return _compiled_payload(compile_formula(req.formula))


class FormulaTrialRequest(FormulaValidateRequest):
    asset_type: str = Field(default="stock", pattern="^(stock|etf)$")
    days: int = Field(default=40, ge=20, le=120)


@router.post("/trial")
def trial_formula(req: FormulaTrialRequest, request: Request) -> dict:
    """公式试算: 最近 N 个交易日截面 Rank IC 快照 (复用回测面板与虚拟因子物化路径)。"""
    compiled = compile_formula(req.formula)
    if not compiled.ok:
        raise HTTPException(status_code=400, detail={"errors": [error.to_dict() for error in compiled.errors]})

    from app.api.backtest import _get_engine

    # 交易日 → 自然日换算 (美股年均 252 交易日 ≈ 1.45 自然日/交易日), 留 buffer
    calendar_days = int((compiled.warmup_bars + req.days) * 1.6) + 15
    start = date.today() - timedelta(days=calendar_days)
    # 面板基础物理列 (load_panel 只返回 parquet 物理列, 因子列由补算路径生成)
    base_columns = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
    engine = _get_engine(request)
    panel = engine.load_panel(None, start, date.today(), columns=base_columns, asset_type=req.asset_type)
    if panel.is_empty():
        raise HTTPException(status_code=400, detail="当前数据目录无可用历史数据, 无法试算")

    # 复用检验引擎同一条补算路径 (compute_indicators + 虚拟因子物化), 禁止第二套计算逻辑
    from app.backtest.factor import FactorBacktestService

    physical = set(panel.columns)
    to_compute = set(compiled.referenced_factors) | {
        dep for dep in compiled.dependencies if dep not in physical
    }
    if to_compute:
        panel = FactorBacktestService._compute_missing_factors(panel, to_compute)

    if compiled.frame_transform is None:
        raise HTTPException(status_code=500, detail="编译产物缺少帧变换")
    prepared = compiled.frame_transform(panel)
    if prepared is None:
        raise HTTPException(
            status_code=400,
            detail={"errors": [{
                "code": "E013", "message": "依赖列不可用: 面板缺少公式所需列",
                "position": {"offset": 0, "line": 1},
                "detail": {"missing": sorted((compiled.dependencies | compiled.referenced_factors) - set(panel.columns))},
            }]},
        )

    total_rows = panel.height
    frame = (
        prepared
        .with_columns(
            (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1.0).alias("_next_return")
        )
        .filter(pl.col(FACTOR_COLUMN).is_not_null())
        .unique(subset=["symbol", "date"], keep="last")
        .sort(["symbol", "date"])
    )
    non_null_rows = frame.height
    if non_null_rows == 0:
        return {
            "ok": True, "n_dates": 0, "null_ratio": 1.0,
            "ic_mean": None, "ic_std": None, "ir": None, "ic_win_rate": None,
            "ic_series": [], "message": "试算区间内公式输出全为空 (检查预热窗口与数据范围)",
        }

    ic_frame = (
        frame.filter(pl.col("_next_return").is_not_null())
        .group_by("date")
        .agg(
            pl.corr(pl.col(FACTOR_COLUMN).rank(method="average"), pl.col("_next_return").rank(method="average")).alias("ic"),
            pl.len().alias("n_symbols"),
        )
        # 截面值全相同时 pl.corr 返回 NaN (非 null), 与 FactorBacktestService 同口径剔除
        .filter(pl.col("ic").is_not_null() & pl.col("ic").is_finite())
        .sort("date")
        .tail(req.days)
    )
    ic_series = [
        {"date": str(row["date"]), "ic": round(row["ic"], 4), "n_symbols": row["n_symbols"]}
        for row in ic_frame.to_dicts()
    ]
    if ic_frame.is_empty():
        return {
            "ok": True, "n_dates": 0, "null_ratio": round(1.0 - non_null_rows / max(total_rows, 1), 4),
            "ic_mean": None, "ic_std": None, "ir": None, "ic_win_rate": None,
            "ic_series": [], "message": "无有效 IC 截面 (需每期 ≥2 只标的)",
        }
    stats = ic_frame.select(
        pl.col("ic").mean().alias("mean"),
        pl.col("ic").std(ddof=0).alias("std"),
        (pl.col("ic") > 0).mean().alias("win"),
    ).row(0, named=True)
    ic_std = stats["std"]
    # Newey-West t (lag=1): 与检验页同源口径, 样本过少时不给 (fail-closed)
    t_newey_west = None
    if ic_frame.height >= 5:
        from app.backtest.stats_v2 import newey_west_t

        values = ic_frame["ic"].to_numpy()
        nw = newey_west_t(values, lag=1)
        if nw is not None:
            t_newey_west = round(float(nw[0]), 3)
    return {
        "ok": True,
        "n_dates": ic_frame.height,
        "null_ratio": round(1.0 - non_null_rows / max(total_rows, 1), 4),
        "ic_mean": round(stats["mean"], 4),
        "ic_std": None if ic_std is None else round(ic_std, 4),
        "ir": None if not ic_std or ic_std == 0 else round(stats["mean"] / ic_std, 3),
        "ic_win_rate": round(stats["win"], 4),
        "t_newey_west": t_newey_west,
        "ic_series": ic_series,
    }


# ── 自定义/复合因子 CRUD (P3) ──────────────────────────────


class CustomFactorCreateRequest(BaseModel):
    id: str | None = Field(default=None, max_length=48)
    label: str = Field(..., min_length=1, max_length=32)
    group: str = Field(default="自定义", max_length=24)
    formula: str = Field(..., min_length=1, max_length=2000)
    description: str = Field(default="", max_length=500)
    direction: str = Field(default="none", pattern="^(high|low|none)$")


class CompositeFactorCreateRequest(BaseModel):
    id: str | None = Field(default=None, max_length=48)
    label: str = Field(..., min_length=1, max_length=32)
    group: str = Field(default="组合", max_length=24)
    members: dict[str, float] = Field(..., min_length=2, max_length=8)
    description: str = Field(default="", max_length=500)
    direction: str = Field(default="none", pattern="^(high|low|none)$")


def _data_dir(request: Request):
    from pathlib import Path

    data_dir = getattr(getattr(request.app.state, "repo", None), "store", None)
    root = getattr(data_dir, "data_dir", None) if data_dir is not None else None
    if root is None:
        raise HTTPException(status_code=500, detail="数据目录不可用")
    return Path(root)


def _slugify_id(label: str, prefix: str) -> str:
    """名称 → id。名称里没有可用的 ASCII 字符 (如纯中文「我的反转」) 时用名称哈希,
    保证结果仍匹配 ^{prefix}_[a-z0-9_]{1,40}$ (旧实现会得到裸 ``uf``, 保存必 400)。"""
    import hashlib
    import re

    base = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_" for ch in label.lower())
    base = re.sub(r"_+", "_", base).strip("_")[:40].rstrip("_")
    if not base:
        base = "f" + hashlib.sha1(label.strip().encode("utf-8")).hexdigest()[:8]
    return f"{prefix}_{base}"


def _resolve_id(requested: str | None, label: str, prefix: str) -> str:
    return requested.strip() if requested and requested.strip() else _slugify_id(label, prefix)


def _reject_existing_id(data_dir, factor_id: str) -> None:
    """新建不得覆盖已有因子 (旧实现把版本 +1 后整份替换: 公式被换、状态回草稿)。改已有因子走 /update。"""
    if any(str(definition.get("id")) == factor_id for definition in store.load_all(data_dir)):
        raise HTTPException(status_code=409, detail=f"id 已存在: {factor_id} · 换一个名称或 id, 改已有因子请从因子库进入编辑")


def _trial_nonempty(request: Request, formula: str, asset_type: str = "stock") -> None:
    """保存前置校验: 公式在最近 40 个交易日有非空输出 (设计 §3.5, fail-closed)。"""
    compiled = compile_formula(formula)
    if not compiled.ok:
        raise HTTPException(status_code=400, detail={"errors": [e.to_dict() for e in compiled.errors]})
    from app.api.backtest import _get_engine
    from app.backtest.factor import FactorBacktestService

    calendar_days = int((compiled.warmup_bars + 40) * 1.6) + 15
    base_columns = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
    engine = _get_engine(request)
    panel = engine.load_panel(None, date.today() - timedelta(days=calendar_days), date.today(), columns=base_columns, asset_type=asset_type)
    if panel.is_empty():
        raise HTTPException(status_code=400, detail="当前无历史数据, 无法完成保存前试算 (fail-closed)")
    physical = set(panel.columns)
    to_compute = set(compiled.referenced_factors) | {d for d in compiled.dependencies if d not in physical}
    if to_compute:
        panel = FactorBacktestService._compute_missing_factors(panel, to_compute)
    prepared = compiled.frame_transform(panel) if compiled.frame_transform else None
    # 只认有限值: power(close-close-1, 0.5) 这类公式非空却全 NaN, 旧门禁 is_not_null 会放过
    if prepared is None or (prepared[FACTOR_COLUMN].is_not_null() & prepared[FACTOR_COLUMN].is_finite()).sum() == 0:
        raise HTTPException(status_code=400, detail="公式在最近 40 个交易日没有有效数值, 拒绝保存")


@router.post("/custom")
def create_custom_factor(req: CustomFactorCreateRequest, request: Request) -> dict:
    """保存自定义公式因子: 编译通过 + 服务端试算非空 (fail-closed)。"""
    data_dir = _data_dir(request)
    factor_id = _resolve_id(req.id, req.label, "uf")
    _reject_existing_id(data_dir, factor_id)
    definition = {
        "id": factor_id,
        "kind": "custom",
        "version": 1,
        "label": req.label,
        "group": req.group,
        "formula": req.formula,
        "description": req.description,
        "direction": req.direction,
        "status": "draft",
        "created_at": store._now(),
        "updated_at": store._now(),
    }
    try:
        store.to_spec(definition)  # 先做 schema/id/编译校验
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _trial_nonempty(request, req.formula)
    try:
        store.register_definition(definition)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.save_one(data_dir, definition)
    return {"ok": True, "id": factor_id, "version": definition["version"]}


@router.post("/composite")
def create_composite_factor(req: CompositeFactorCreateRequest, request: Request) -> dict:
    """保存复合因子: 成员校验 + 循环引用检查 (无需试算, 值由成员物化路径计算)。"""
    data_dir = _data_dir(request)
    factor_id = _resolve_id(req.id, req.label, "cf")
    _reject_existing_id(data_dir, factor_id)
    definition = {
        "id": factor_id,
        "kind": "composite",
        "version": 1,
        "label": req.label,
        "group": req.group,
        "members": req.members,
        "description": req.description,
        "direction": req.direction,
        "status": "draft",
        "created_at": store._now(),
        "updated_at": store._now(),
    }
    try:
        store.register_definition(definition)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.save_one(data_dir, definition)
    return {"ok": True, "id": factor_id, "version": definition["version"]}


class CustomFactorUpdateRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=32)
    # 与「因子库 · 改分组」(/group, 24) 同上限: 旧值 16 让在因子库改过长分组的因子再也无法在编辑器保存
    group: str = Field(default="自定义", max_length=24)
    formula: str = Field(..., min_length=1, max_length=2000)
    # None = 不改 (旧客户端不回传描述时, 采集并入因子的出处不再被清空)
    description: str | None = Field(default=None, max_length=500)
    direction: str = Field(default="none", pattern="^(high|low|none)$")


@router.post("/custom/{factor_id}/update")
def update_custom_factor(factor_id: str, req: CustomFactorUpdateRequest, request: Request) -> dict:
    """编辑已有自定义因子: 编译校验 + 试算非空 (与创建同一门禁) → 版本提升注册。

    公式变化时状态回 draft (生命周期语义: 编辑后需重新检验激活); 仅改名称/分组保留状态。
    """
    data_dir = _data_dir(request)
    target = None
    for definition in store.load_all(data_dir):
        if str(definition.get("id")) == factor_id:
            target = definition
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"自定义因子不存在: {factor_id}")
    if str(target.get("kind", "custom")) != "custom":
        raise HTTPException(status_code=400, detail=f"仅自定义因子支持公式编辑 (kind={target.get('kind')})")
    formula_changed = str(target.get("formula")) != req.formula
    if formula_changed:
        _trial_nonempty(request, req.formula)
    target.update({
        "label": req.label,
        "group": req.group,
        "formula": req.formula,
        "description": str(target.get("description") or "") if req.description is None else req.description,
        "direction": req.direction,
        "version": int(target.get("version", 1)) + 1,  # 版本提升 → 注册表允许覆盖
        "status": "draft" if formula_changed else str(target.get("status", "draft")),
        "updated_at": store._now(),
    })
    try:
        store.register_definition(target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.save_one(data_dir, target)
    if formula_changed:
        _rebuild_dependents(data_dir, factor_id)
        _invalidate_runtime(request)
    return {"ok": True, "id": factor_id, "version": target["version"], "status": target["status"]}


def _rebuild_dependents(data_dir, factor_id: str) -> list[str]:
    """改公式后重建引用它的自定义 / 复合因子 (递归)。

    依赖列与预热是注册时从被引用方推导出来的: 旧实现只重注册被编辑的因子,
    uf_base 预热从 1 变 101 后, 引用它的 uf_dep 仍按 1 根预热取数, 首段结果是空的。
    这里按新定义重推一遍 (定义文件不存推导值, 无需落盘); 某个依赖方重建失败只告警, 保留原注册项。
    """
    import logging

    from app.factors.registry import register_factor

    definitions = {str(d.get("id")): d for d in store.load_all(data_dir)}
    rebuilt: list[str] = []
    frontier = [factor_id]
    seen = {factor_id}
    while frontier:
        current = frontier.pop(0)
        for other_id, definition in definitions.items():
            if other_id in seen or not _definition_references(definition, current):
                continue
            seen.add(other_id)
            try:
                spec = store.to_spec(definition)
            except ValueError as exc:
                logging.getLogger(__name__).warning("依赖方重建失败 %s: %s", other_id, exc)
                continue
            unregister_factor(other_id)
            register_factor(spec)
            rebuilt.append(other_id)
            frontier.append(other_id)
    return rebuilt


def _definition_references(definition: dict, factor_id: str) -> bool:
    """定义是否引用 factor_id: 复合看成员, 自定义按 DSL 编译出的 referenced_factors。"""
    members = definition.get("members")
    if isinstance(members, dict) and factor_id in members:
        return True
    if str(definition.get("kind", "custom")) == "custom":
        compiled = compile_formula(str(definition.get("formula") or ""))
        return factor_id in compiled.referenced_factors
    return False


def _find_references(data_dir, factor_id: str) -> list[str]:
    """扫描策略、复合/自定义因子、自定义信号、研究候选中的引用 (删除前 fail-closed 检查)。

    旧实现只看复合成员与策略文件: uf_b 公式里写 uf_a、信号条件 left=uf_a、研究候选
    factor_names 含 uf_a 时都能静默删掉 uf_a, 留下运行期才暴露的失效引用。
    """
    import json
    import re

    # 0.39.4 (审计 M-16): 评分引用实际落在 user_data/strategy_overrides/*.json (策略页保存的配置)
    # 和 strategies/**/*.py (挖掘发布/AI/叠加策略源码); 旧的 strategies/*.json 保留兼容.
    # 按标识符整词匹配, 避免 uf_a 误命中 uf_ab.
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(factor_id)}(?![A-Za-z0-9_])")
    references: list[str] = []
    scan_roots = (
        (data_dir / "strategies", ("*.json",), False),
        (data_dir / "strategies", ("*.py",), True),
        (data_dir / "user_data" / "strategy_overrides", ("*.json",), False),
    )
    for root, globs, recursive in scan_roots:
        if not root.is_dir():
            continue
        for glob in globs:
            files = root.rglob(glob) if recursive else root.glob(glob)
            for file in sorted(files):
                if not file.is_file():
                    continue
                try:
                    text = file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if pattern.search(text):
                    references.append(file.relative_to(data_dir).as_posix())
    for definition in store.load_all(data_dir):
        if str(definition.get("id")) == factor_id:
            continue
        if _definition_references(definition, factor_id):
            references.append(f"custom_factors/{definition.get('id')}.json")
    signals_dir = data_dir / "user_data" / "custom_signals"
    if signals_dir.is_dir():
        for file in sorted(signals_dir.glob("*.json")):
            try:
                signal = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            conditions = signal.get("conditions") if isinstance(signal, dict) else None
            if any(
                isinstance(cond, dict) and factor_id in (str(cond.get("left") or ""), str(cond.get("right") or ""))
                for cond in conditions or []
            ):
                references.append(file.relative_to(data_dir).as_posix())
    candidates_path = data_dir / "user_data" / "research_candidates.json"
    if candidates_path.is_file():
        try:
            candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            candidates = []
        for item in candidates if isinstance(candidates, list) else []:
            config = item.get("config") if isinstance(item, dict) else None
            if not isinstance(config, dict):
                continue
            raw_names = config.get("factor_names")
            names = [str(name) for name in raw_names] if isinstance(raw_names, list) else []
            if config.get("factor_name") == factor_id or factor_id in names:
                references.append(f"research_candidates.json#{item.get('id')}")
    return references


def _invalidate_runtime(request: Request) -> None:
    """公式变更 / 强删后: 清 DSL 编译缓存与策略结果缓存, 下一轮评分按新定义重算。

    旧实现只在注册表里换了 spec, 策略页缓存 (strategy_cache) 与盘中引擎状态仍是旧公式的结果。
    """
    from app.api.strategy import _invalidate_strategy_runtime
    from app.factors.dsl import compile_formula_cached

    compile_formula_cached.cache_clear()
    # 测试/最小应用没有 repo.store 时跳过磁盘缓存
    with contextlib.suppress(AttributeError):
        _invalidate_strategy_runtime(request)


@router.delete("/custom/{factor_id}")
def delete_custom_factor(factor_id: str, request: Request, force: bool = Query(default=False)) -> dict:
    """删除自定义/复合因子; 有引用时列出引用方并拒绝 (需 force)。"""
    data_dir = _data_dir(request)
    from app.factors.registry import get_factor

    # 引用检查必须排在存在性判定之前: 下面用来探测「盘上是否有定义」的
    # store.delete_one 本身就会删文件, 反过来会出现「拒绝删除」但定义已被删掉。
    references = _find_references(data_dir, factor_id)
    if references and not force:
        raise HTTPException(
            status_code=409,
            detail={"message": "该因子仍有引用, 拒绝删除 (可带 force=true 强制)", "references": references},
        )
    if get_factor(factor_id) is None and not store.delete_one(data_dir, factor_id):
        raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
    try:
        unregister_factor(factor_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.delete_one(data_dir, factor_id)
    if force:
        _invalidate_runtime(request)
    return {"ok": True, "id": factor_id, "removed_references": references}


def _reregister_metadata(factor_id: str, definition: dict) -> None:
    """元数据变更 (status/group) 不提升版本, 需先注销再注册; 但必须先校验新定义。

    旧实现先注销: 定义已无法编译时 (DSL 收紧、成员被删) 返回 400, 因子却已从注册表消失,
    在用的策略评分直到重启都取不到它。现在校验失败时原注册项原样保留。
    """
    from app.factors.registry import register_factor

    try:
        spec = store.to_spec(definition)
        unregister_factor(factor_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    register_factor(spec)


class FactorStatusRequest(BaseModel):
    status: str = Field(..., pattern="^(draft|active|watch|retired)$")


@router.post("/custom/{factor_id}/status")
def update_factor_status(factor_id: str, req: FactorStatusRequest, request: Request) -> dict:
    """生命周期状态迁移 (P4): draft->active->watch->retired, 编辑后回 draft。"""
    data_dir = _data_dir(request)
    target = None
    for definition in store.load_all(data_dir):
        if str(definition.get("id")) == factor_id:
            target = definition
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"自定义因子不存在: {factor_id}")
    target["status"] = req.status
    target["updated_at"] = store._now()
    _reregister_metadata(factor_id, target)  # 状态与 stability 联动
    store.save_one(data_dir, target)
    return {"ok": True, "id": factor_id, "status": req.status}


class FactorGroupRequest(BaseModel):
    group: str = Field(..., min_length=1, max_length=24)


@router.post("/custom/{factor_id}/group")
def update_factor_group(factor_id: str, req: FactorGroupRequest, request: Request) -> dict:
    """修改单个自定义/复合因子的分组 (内置因子分组与快照/预设绑定, 不可改)。"""
    data_dir = _data_dir(request)
    group = req.group.strip()
    if not group:
        raise HTTPException(status_code=400, detail="分组名不能为空")
    target = None
    for definition in store.load_all(data_dir):
        if str(definition.get("id")) == factor_id:
            target = definition
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"自定义因子不存在: {factor_id}")
    target["group"] = group
    target["updated_at"] = store._now()
    _reregister_metadata(factor_id, target)
    store.save_one(data_dir, target)
    return {"ok": True, "id": factor_id, "group": group}
