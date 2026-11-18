#!/usr/bin/env python3
"""由 backend/app/lifecycles.py 生成 docs/state-machines.md 与 frontend/src/lib/lifecycles.generated.ts。

用法 (在仓库根, 用后端虚拟环境的解释器):
    backend/.venv/bin/python scripts/gen_lifecycles.py           # 写文件
    backend/.venv/bin/python scripts/gen_lifecycles.py --check   # 只比对, 不一致退出 1
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

DOC = ROOT / "docs" / "state-machines.md"
TS = ROOT / "frontend" / "src" / "lib" / "lifecycles.generated.ts"

_DOC_HEAD = """# 状态机一览

<!-- 本文件由 scripts/gen_lifecycles.py 生成, 不要手改。改状态请改 backend/app/lifecycles.py 后重新生成。 -->

系统里会落盘、跨进程或跨前后端传递的状态都登记在 `backend/app/lifecycles.py`。这份文档、
`GET /api/lifecycles` 接口和前端的中文状态名都从那一份登记表生成。

- **新增或改名状态**：改 `backend/app/lifecycles.py` → `backend/.venv/bin/python scripts/gen_lifecycles.py` → 提交生成物。
  属主模块写出登记表之外的状态时，`backend/tests/test_lifecycles.py` 会失败。
- **登记不等于拦截**：标「只登记」的状态机仍由属主模块自己推进，登记表不改变它的行为；
  机会卡的转换另有一道只记日志的核对（表外转换写一条 warning，不拦截）。
- **读状态的中文名**：后端 `lifecycles.label(id, code)`，前端 `lifecycleLabel(id, code)`。
"""

_DOC_TAIL = """## 没有登记的状态

下面这些也是状态，但都是从别的数据当场算出来的，或者只活在一个页面里，不落盘、没有「转换」可言，所以不进登记表。
改它们时直接看属主文件。

| 状态 | 取值 | 属主 |
| --- | --- | --- |
| 交易时段 | `closed` `night` `premarket` `open_wait` `session` `after` | `backend/app/services/card_volume.py::phase` |
| 机会板卡结局（回放） | `pending` `holding` `closed` `no_fill` `void` `invalid` | `backend/app/services/swing_settlement.py` |
| 规则回放结局 | `pending` `triggered` `closed` `expired` `gap_expired` | `backend/app/services/card_replay.py` |
| 盘中强势卡 | `open` `closed` `void` | `backend/app/services/intraday_strength.py` |
| 入场意图 | `pending` `blocked` | `backend/app/trader/trader.py`（台账 `entry_intent` 事件） |
| 因子成熟度 | `experiment` `observing` `active` `retired` | `backend/app/services/factor_maturity.py::state_at` |
| 模型对照结论 | `none` `running` `promoted` `rejected` | `backend/app/services/ls_model.py` |
| 研究候选 | `pending` `validated` `rejected` | `backend/app/backtest/candidates.py` |
| 更新检查 | `idle` `checking` `latest` `found` `error` `disabled` | `frontend/src/lib/updateCheck.ts` |
| 行情推送连接 | `connected` `reconnecting` `disconnected` | `frontend/src/lib/useQuoteStream.ts` |
| 个股分析弹窗 | `loading` `streaming` `done` `error` | `frontend/src/lib/stockAnalysisStore.ts` |
| 侧栏 | `expanded` `rail` `hidden` | `frontend/src/components/Layout.tsx` |
"""


def render_doc(payload: dict) -> str:
    parts = [_DOC_HEAD]
    for lc in payload["lifecycles"]:
        terminal = [s["code"] for s in lc["states"] if s["terminal"]]
        parts.append(f"\n## {lc['title']}（`{lc['id']}`）\n")
        parts.append(f"- 属主：`backend/{lc['owner']}`，字段 `{lc['field']}`")
        parts.append(f"- 落盘：`{lc['stored']}`")
        parts.append("- 表外转换：" + ("属主模块直接拒绝" if lc["enforced"] else "只登记，不拦截"))
        parts.append("\n```mermaid\nstateDiagram-v2")
        parts.extend(f"    [*] --> {code}" for code in lc["initial"])
        for src, dsts in lc["transitions"].items():
            parts.extend(f"    {src} --> {dst}" for dst in dsts)
        parts.extend(f"    {code} --> [*]" for code in terminal)
        parts.append("```\n")
        parts.append("| 代码 | 中文名 | 终态 | 说明 |\n| --- | --- | --- | --- |")
        parts.extend(f"| `{s['code']}` | {s['label']} | {'是' if s['terminal'] else ''} | {s['note']} |"
                     for s in lc["states"])
    parts.append("\n" + _DOC_TAIL)
    return "\n".join(parts)


def render_ts(payload: dict) -> str:
    labels = {lc["id"]: {s["code"]: s["label"] for s in lc["states"]} for lc in payload["lifecycles"]}
    terminal = {lc["id"]: [s["code"] for s in lc["states"] if s["terminal"]] for lc in payload["lifecycles"]}
    return (
        "// 本文件由 scripts/gen_lifecycles.py 生成, 不要手改。\n"
        "// 来源: backend/app/lifecycles.py (状态机登记表)。改状态后重新生成并提交。\n\n"
        f"export const LIFECYCLE_LABELS = {json.dumps(labels, ensure_ascii=False, indent=2)} as const\n\n"
        f"export const LIFECYCLE_TERMINAL = {json.dumps(terminal, ensure_ascii=False, indent=2)} as const\n\n"
        "export type LifecycleId = keyof typeof LIFECYCLE_LABELS\n\n"
        "/** 状态中文名; 登记表里没有的代码原样返回。 */\n"
        "export function lifecycleLabel(id: LifecycleId, code: string): string {\n"
        "  return (LIFECYCLE_LABELS[id] as Record<string, string>)[code] ?? code\n"
        "}\n"
    )


def outputs() -> dict[Path, str]:
    from app import lifecycles

    payload = lifecycles.as_payload()
    return {DOC: render_doc(payload), TS: render_ts(payload)}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for path, text in outputs().items():
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path.relative_to(ROOT))
        else:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print("生成物过期, 请运行 scripts/gen_lifecycles.py: " + ", ".join(map(str, stale)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
