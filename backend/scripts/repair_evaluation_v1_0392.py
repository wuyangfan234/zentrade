"""一次性数据修复 (审计 2026-10-26 M-9): v4 模式下的周更 v1 评估曾把 v4 许可视图写进 v1 evaluation.json。

污染形态 (生产 10-24 快照实证):
- sources 里出现 v4 形状行 (v2/v3 模式下同类 bug 会留 carried_from / "v3": true 行, 一并剔除) (phantom_4h / phantom_4h_pine = {"v4": true, "enabled", "tradable", "forward", ...});
- 未被周更重评的 v1 来源 (breakout / pullback 分钟回放证据) 和它们的逐笔记录被删除。

修复 = 以当前文件为准, 删掉视图形状行; 当前文件缺失而干净备份里有的来源/逐笔, 从干净备份补回
(不覆盖当前已有行; 只补现行策略目录里还在的策略与 REPLAY_SOURCES 列出的非目录来源; 补回行带 restored_from)。
默认只打印差异 (dry-run); --apply 才写, 写前把原文件复制为 evaluation.json.pre-0392-<时间戳>。

用法 (容器内):
  /app/.venv/bin/python -m scripts.repair_evaluation_v1_0392 --data-dir /app/data \\
      --clean /app/data/audit_backups/pre-watchlist-v4-20261006-061413/decision_desk/evaluation.json [--apply]
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

# 不在策略目录里的现行证据来源: 分钟回放 (replay_summary) 与 4h 原生评估 (phantom_4h_source)
REPLAY_SOURCES = ("breakout", "pullback", "phantom_4h", "phantom_4h_short")


def _is_view_row(v) -> bool:
    return isinstance(v, dict) and (v.get("v4") is True or v.get("v3") is True or "carried_from" in v)


def plan(current: dict, clean: dict, known: set[str] | None = None, *, clean_label: str = "") -> dict:
    """known = 现行策略目录里的策略 id (None = 不限)。只补回 known 内的策略与回放来源, 已删除/改名的策略不复活。"""
    cur_src = dict(current.get("sources") or {})
    cur_tr = dict(current.get("trades") or {})
    drop = sorted(k for k, v in cur_src.items() if _is_view_row(v))
    for k in drop:
        cur_src.pop(k)
    allowed = None if known is None else set(known) | set(REPLAY_SOURCES)
    restore, skipped = [], []
    for k, v in sorted((clean.get("sources") or {}).items()):
        if k in cur_src or _is_view_row(v) or not isinstance(v, dict) or v.get("error"):
            continue
        if allowed is not None and k not in allowed:
            skipped.append(k)
            continue
        cur_src[k] = {**v, "restored_from": clean_label or "backup"}
        restore.append(k)
    # 逐笔只补给本次补回的来源 (当前文件里已有的行, 包括报错行, 不配旧逐笔)
    restore_tr = sorted(k for k in (clean.get("trades") or {}) if k in restore and k not in cur_tr)
    for k in restore_tr:
        cur_tr[k] = clean["trades"][k]
    fixed = dict(current)
    fixed["sources"], fixed["trades"] = cur_src, cur_tr
    return {"fixed": fixed, "dropped_view_rows": drop, "restored_sources": restore, "restored_trades": restore_tr,
            "skipped_unknown": skipped}


def _known_strategies(data_dir: Path) -> set[str] | None:
    try:
        from app.services.strategy_evaluation_runner import _strategy_engine
        return {m["id"] for m in _strategy_engine(data_dir).list_strategies()}
    except Exception as e:
        print(f"warn: 策略目录读取失败 ({type(e).__name__}), 需 --allow-unknown 才补回")
        return set()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--clean", required=True, type=Path, help="污染前的干净 evaluation.json 备份")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--allow-unknown", action="store_true", help="不按现行策略目录过滤 (仅测试/特殊情况)")
    a = ap.parse_args(argv)
    path = a.data_dir / "decision_desk" / "evaluation.json"
    current = json.loads(path.read_text(encoding="utf-8"))
    clean = json.loads(a.clean.read_text(encoding="utf-8"))
    known = None if a.allow_unknown else _known_strategies(a.data_dir)
    r = plan(current, clean, known, clean_label=str(a.clean))
    print(json.dumps({k: v for k, v in r.items() if k != "fixed"}, ensure_ascii=False))
    print(f"sources {len(current.get('sources') or {})} -> {len(r['fixed']['sources'])}")
    if not a.apply:
        print("dry-run: 未写入 (加 --apply 才写)")
        return 0
    if not (r["dropped_view_rows"] or r["restored_sources"] or r["restored_trades"]):
        print("无需修复")
        return 0
    bak = path.with_name(f"evaluation.json.pre-0392-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, bak)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(r["fixed"], ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    print(f"written; backup = {bak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
