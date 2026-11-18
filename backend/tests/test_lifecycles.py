"""状态机登记表 (app/lifecycles.py) 与各属主模块、生成物保持一致。

登记表只在这里被"执行": 属主模块写出表外状态、表里留着没人写的状态、生成物没重新生成, 都在这里失败。
"""
from __future__ import annotations

import importlib.util
import logging
import re
import typing
from pathlib import Path

import pytest

from app import lifecycles

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def _source(lc: lifecycles.Lifecycle) -> str:
    return (BACKEND / lc.owner).read_text(encoding="utf-8")


def _written(src: str, field: str) -> set[str]:
    """属主源码里写进 field 的字面量: x["status"] = "a" / update(status="a") / {"status": "a"} / SQL status='a'。"""
    q = r"""["']"""
    pats = [
        rf"""\[{q}{field}{q}\]\s*=\s*f?{q}([a-z_{{}}]+){q}""",
        rf"""\b{field}\s*=\s*{q}([a-z_]+){q}""",
        rf"""{q}{field}{q}\s*:\s*{q}([a-z_]+){q}""",
    ]
    out: set[str] = set()
    for p in pats:
        out.update(re.findall(p, src))
    return out


@pytest.mark.parametrize("lc", list(lifecycles.registry().values()), ids=lambda lc: lc.id)
def test_structure_is_closed_and_reachable(lc):
    codes = lc.codes
    assert len(codes) == len(lc.states), "状态代码重复"
    assert set(lc.initial) <= codes
    for src, dsts in lc.transitions.items():
        assert src in codes and set(dsts) <= codes, f"{src} 的转换指向表外状态"
        assert src not in lc.terminal, f"终态 {src} 不应再有转换"
        assert src not in dsts, f"{src} 不登记自环"
    seen, todo = set(lc.initial), list(lc.initial)
    while todo:
        for nxt in lc.transitions.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    assert seen == codes, f"从初始状态到不了: {sorted(codes - seen)}"
    assert all(s.label and s.label != s.code for s in lc.states), "每个状态都要有中文名"


@pytest.mark.parametrize("lc", list(lifecycles.registry().values()), ids=lambda lc: lc.id)
def test_every_registered_state_exists_in_owner_source(lc):
    src = (BACKEND / "app/opportunity/card.py").read_text(encoding="utf-8") if lc.id == "opportunity_card" else _source(lc)
    # alpaca_canceled / alpaca_expired / alpaca_rejected 在源码里是 f"alpaca_{status}"
    missing = [c for c in lc.codes if not re.search(rf"""["']{c}["']""", src)
               and not (c.startswith("alpaca_") and f'"{c.removeprefix("alpaca_")}"' in src and 'f"alpaca_{status}"' in src)]
    assert missing == [], f"登记表里有属主模块已经不写的状态: {missing}"


@pytest.mark.parametrize("lifecycle_id", ["mirror_order", "factor_intake_item", "pipeline_job", "notification_outbox"])
def test_owner_writes_only_registered_states(lifecycle_id):
    lc = lifecycles.get(lifecycle_id)
    written = _written(_source(lc), lc.field)
    if "alpaca_{status}" in written:
        written.discard("alpaca_{status}")
        written |= {"alpaca_canceled", "alpaca_expired", "alpaca_rejected"}
    assert written, "没解析到任何状态写入, 正则失效"
    extra = written - lc.codes - _NOT_A_STATE.get(lifecycle_id, set())
    assert extra == set(), f"{lc.owner} 写了登记表之外的状态 {sorted(extra)}: 先登记到 app/lifecycles.py 再重新生成"


# 同一个字段名在属主文件里的其它用途 (不是这台状态机的状态)
_NOT_A_STATE = {
    "notification_outbox": {"not_found", "idle"},     # status() 汇总视图的取值, 不落在 outbox 行上
    "factor_intake_item": {"llm", "drop", "paywall", "stale"},  # prefilter 的中间判定
}


def test_opportunity_card_matches_enum():
    from app.opportunity.card import TERMINAL_STATES, CardState

    lc = lifecycles.get("opportunity_card")
    assert lc.codes == {str(s) for s in CardState}
    assert lc.terminal == {str(s) for s in TERMINAL_STATES}


def test_opportunity_card_labels_match_desk_overview():
    from app.services.desk_overview import STATE_LABEL

    assert {c: lifecycles.label("opportunity_card", c) for c in STATE_LABEL} == STATE_LABEL


def test_pipeline_job_matches_literal():
    from app.services.pipeline_jobs import JobStatus

    assert lifecycles.get("pipeline_job").codes == set(typing.get_args(JobStatus))


def test_mining_run_is_derived_from_owner_tables():
    from app.services import mining_jobs as mj

    lc = lifecycles.get("mining_run")
    assert lc.enforced and lc.codes == mj.RUN_STATUSES and lc.terminal == mj.TERMINAL_RUN_STATUSES
    assert {s: frozenset(d) for s, d in lc.transitions.items()} == {s: d for s, d in mj._ALLOWED_TRANSITIONS.items() if d}


def test_outbox_labels_are_the_owner_labels():
    from app.services.notification_outbox import _LABELS

    lc = lifecycles.get("notification_outbox")
    assert {s.code: s.label for s in lc.states} == _LABELS


def test_observe_never_raises_and_logs_once(caplog):
    lifecycles._SEEN.clear()
    with caplog.at_level(logging.WARNING, logger="app.lifecycles"):
        assert lifecycles.observe("opportunity_card", "open", "active") is True
        assert lifecycles.observe("opportunity_card", "open", "open") is True
        assert lifecycles.observe("no_such_machine", "a", "b") is True
        assert lifecycles.observe("opportunity_card", "closed", "active", card_id="x") is False
        assert lifecycles.observe("opportunity_card", "closed", "active", card_id="y") is False
        assert lifecycles.observe("opportunity_card", object(), None) is False
    assert sum("closed -> active" in r.getMessage() for r in caplog.records) == 1


def test_card_transitions_in_service_are_all_registered(tmp_path):
    """opportunity_service 与 trading_loop 里每一处 _transition 的目标状态, 都能从某个存活状态合法到达。"""
    lc = lifecycles.get("opportunity_card")
    targets: set[str] = set()
    for rel in ("app/services/opportunity_service.py", "app/services/trading_loop.py"):
        targets |= {m.lower() for m in re.findall(r"_transition\(\s*\w+\s*,\s*CardState\.([A-Z]+)", (BACKEND / rel).read_text("utf-8"))}
    assert targets == {"active", "watch", "expired"}
    live = ("open", "active", "watch")
    assert all(any(lc.allows(src, dst) for src in live) for dst in targets)


def test_api_returns_registry():
    from app.api.routes import lifecycles_registry

    body = lifecycles_registry()
    assert [lc["id"] for lc in body["lifecycles"]] == list(lifecycles.registry())
    mirror = next(lc for lc in body["lifecycles"] if lc["id"] == "mirror_order")
    assert {"code": "submission_unknown", "label": "提交结果待确认", "terminal": False} .items() <= next(
        s for s in mirror["states"] if s["code"] == "submission_unknown").items()


def test_generated_files_are_current():
    spec = importlib.util.spec_from_file_location("gen_lifecycles", ROOT / "scripts" / "gen_lifecycles.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    stale = [str(p.relative_to(ROOT)) for p, text in gen.outputs().items()
             if not p.exists() or p.read_text(encoding="utf-8") != text]
    assert stale == [], "生成物过期, 运行 backend/.venv/bin/python scripts/gen_lifecycles.py"
