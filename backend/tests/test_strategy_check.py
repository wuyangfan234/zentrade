"""创建策略弹窗的分项校验 / 模板库 / 语法标准摘要 / 报错中文说明。

要守住的三件事:
1. 分项校验不比现行校验宽也不比它严: 全过 <=> 保存接口的校验通过且引擎能装载。
2. 每个模板都过校验、能被引擎装载; 行数与参数个数由源码算出。
3. 中文说明对照的英文原文仍在引擎源码里; 执行方式与章节标题不与代码、文档漂移。
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.api import strategy as strategy_api
from app.backtest import matrix as matrix_mod
from app.services import strategy_check as sc
from app.services.api_keys import READ_SCOPE_POST_ALLOW
from app.strategy import engine as engine_mod
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.engine import StrategyEngine

BUILTIN_DIR = Path(engine_mod.__file__).resolve().parent / "builtin"
STANDARD = Path(__file__).resolve().parents[2] / "docs" / "strategy-standard.md"
GUIDE = Path(__file__).resolve().parents[2] / "docs" / "strategy-development.md"
TEMPLATES = {t["id"]: t for t in sc.list_templates()}
GOOD = TEMPLATES["pullback_ma10"]["code"]


def _items(code: str) -> dict[str, dict]:
    return {i["key"]: i for i in sc.check_code(code)["items"]}


def _authoritative_ok(code: str, tmp_path: Path) -> bool:
    """保存接口的同一套校验 + 引擎装载。"""
    try:
        AIStrategyGenerator._validate_safety(code)
        meta = AIStrategyGenerator._extract_meta(code)
        AIStrategyGenerator._validate_meta_semantics(code, meta)
        sid = meta["id"]
        (tmp_path / f"{sid}.py").write_text(code, encoding="utf-8")
        StrategyEngine(strategy_dirs=[BUILTIN_DIR, tmp_path]).get(sid)
    except Exception:
        return False
    return True


# ───────────────────────── 模板 ─────────────────────────

def test_template_library_covers_five_backends():
    assert len(TEMPLATES) == 6
    assert {t["backend"] for t in TEMPLATES.values()} == {b["id"] for b in sc.BACKENDS}
    assert [t["order"] for t in sc.list_templates()] == sorted(t["order"] for t in TEMPLATES.values())
    assert {t["level"] for t in TEMPLATES.values()} <= {"入门", "进阶"}


@pytest.mark.parametrize("tid", sorted(TEMPLATES))
def test_template_passes_check_and_loads(tid, tmp_path):
    tpl = TEMPLATES[tid]
    code = tpl["code"]
    result = sc.check_code(code)
    assert result["valid"], result["items"]
    assert _authoritative_ok(code, tmp_path)
    meta = AIStrategyGenerator._extract_meta(code)
    assert meta["id"].startswith("custom_")
    assert tpl["lines"] == len(code.splitlines()) == result["overview"]["lines"]
    assert tpl["params"] == len(meta.get("params") or []) == result["overview"]["params"]
    assert tpl["kind"] == result["overview"]["backend_label"]
    loaded = StrategyEngine(strategy_dirs=[BUILTIN_DIR, tmp_path]).get(meta["id"])
    assert loaded.execution_backend == tpl["backend"]


@pytest.mark.skipif(not GUIDE.exists(), reason="docs/ 不在这份代码树里")
def test_three_templates_are_the_guide_examples_verbatim():
    """模板与开发指南里的示例是同一份源码: 改了一边没改另一边, 这条不过。"""
    blocks = re.findall(r"```python\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.S)
    examples = {}
    for block in blocks:
        m = re.search(r'^EXECUTION_BACKEND\s*=\s*"(\w+)"', block, re.M)
        if m and re.search(r"^META\s*=", block, re.M):
            examples[m.group(1)] = block
    pairs = {"pullback_ma10": "polars_expr", "breakout_pullback": "python_history_legacy", "volume_breakout": "matrix_native"}
    for tid, backend in pairs.items():
        assert TEMPLATES[tid]["code"].rstrip("\n") == examples[backend].rstrip("\n"), tid


def test_minute_template_hits_only_the_breakout(tmp_path):
    code = TEMPLATES["opening_range_breakout"]["code"]
    (tmp_path / "custom_opening_range_breakout.py").write_text(code, encoding="utf-8")
    fn = StrategyEngine(strategy_dirs=[tmp_path]).get("custom_opening_range_breakout").filter_minute_history_fn
    start = datetime(2024, 1, 2, 9, 30)
    rows = []
    for sym in ("AAA", "BBB", "CCC"):
        n = 10 if sym == "CCC" else 30          # CCC 不足开盘区间根数
        for i in range(n):
            close, volume = 10.0, 1000.0
            if sym == "AAA" and i >= 15:          # AAA 区间后放量上行; BBB 全天横盘
                close, volume = 10.0 + (i - 14) * 0.05, 3000.0
            rows.append({"symbol": sym, "datetime": start + timedelta(minutes=i), "open": close, "high": close + 0.01,
                         "low": close - 0.01, "close": close, "volume": volume, "amount": close * volume})
    out = fn(pl.DataFrame(rows), {"range_bars": 15, "min_break_pct": 0.002, "min_volume_ratio": 1.2})
    assert out["symbol"].to_list() == ["AAA"]
    assert out["volume_ratio"][0] == pytest.approx(3.0)
    assert out["break_pct"][0] == pytest.approx(10.75 / 10.01 - 1)


# ───────────────────────── 分项校验 ─────────────────────────

def test_bad_items_carry_line_numbers():
    bad = GOOD.replace("import polars as pl\n", "import polars as pl\nimport os\n", 1) \
        .replace('"ma20_bias": 0.2', '"ma20_bias": 0.1', 1)
    lines = bad.splitlines()
    result = sc.check_code(bad)
    items = {i["key"]: i for i in result["items"]}
    assert not result["valid"]
    assert items["safety"]["state"] == "bad" and lines[items["safety"]["line"] - 1] == "import os"
    assert items["safety"]["text"] == "禁止 import os" and items["safety"]["section"] == 5
    assert items["meta"]["state"] == "bad" and '"scoring"' in lines[items["meta"]["line"] - 1]
    assert items["meta"]["text"] == "评分合计 0.9 · 应为 1.0" and items["meta"]["section"] == 2
    assert items["entry"]["state"] == "ok" and items["fields"]["state"] == "ok"
    assert all(i["section"] is None for i in result["items"] if i["state"] != "bad")


def test_meta_and_fields_are_judged_independently():
    code = GOOD.replace('pl.col("vol_ma5")', 'pl.col("turnover_rate")').replace('{"id": "min_momentum",', '{"id": "",')
    items = _items(code)
    lines = code.splitlines()
    assert items["meta"]["state"] == "bad" and items["meta"]["text"] == "第 1 个参数缺 id"
    assert '"id": ""' in lines[items["meta"]["line"] - 1]
    assert items["fields"]["state"] == "bad" and items["fields"]["text"] == "字段 turnover_rate 不可用"
    assert "turnover_rate" in lines[items["fields"]["line"] - 1]


def test_unknown_scoring_field_goes_to_fields_item():
    code = GOOD.replace('"scoring": {"momentum_20d": 0.5, "vol_ratio_5d": 0.3, "ma20_bias": 0.2}',
                        '"scoring": {"momentum_20d": 0.5, "not_a_field": 0.5}')
    assert code != GOOD
    items = _items(code)
    assert items["meta"]["state"] == "ok"
    assert items["fields"]["state"] == "bad" and "not_a_field" in items["fields"]["text"]


def test_syntax_error_and_missing_meta_skip_later_items():
    items = _items("def f(:\n    pass\n")
    assert items["safety"]["state"] == "bad" and items["safety"]["line"] == 1
    assert [items[k]["state"] for k in ("meta", "entry", "fields")] == ["skip"] * 3
    items = _items("import polars as pl\n\n\ndef filter(df, params):\n    return pl.col('close') > 0\n")
    assert items["safety"]["state"] == "ok" and items["meta"]["state"] == "bad"
    assert [items[k]["state"] for k in ("entry", "fields")] == ["skip"] * 2
    assert not sc.check_code("")["valid"]


def test_forbidden_call_and_dunder_point_to_section_6():
    code = GOOD.replace("    min_momentum = float(", "    open('x')\n    min_momentum = float(", 1)
    item = _items(code)["safety"]
    assert item["text"] == "禁止调用 open()" and item["section"] == 6
    assert "open('x')" in code.splitlines()[item["line"] - 1]
    item = _items(GOOD + "\nX = ().__class__\n")["safety"]
    assert item["section"] == 6 and "__class__" in (GOOD + "\nX = ().__class__\n").splitlines()[item["line"] - 1]


def _mutations() -> dict[str, str]:
    matrix = TEMPLATES["volume_breakout"]["code"]
    comp = TEMPLATES["two_strategy_blend"]["code"]
    minute = TEMPLATES["opening_range_breakout"]["code"]
    hist = TEMPLATES["breakout_pullback"]["code"]
    extra_filter = "\n\ndef filter(df, params):\n    return pl.col('close') > 0\n"
    extra_hist = "\n\ndef filter_history(df, params):\n    return df\n"
    out = {
        "good": GOOD,
        "import_os": GOOD.replace("import polars as pl\n", "import polars as pl\nimport os\n", 1),
        "scoring_sum": GOOD.replace('"ma20_bias": 0.2', '"ma20_bias": 0.1', 1),
        "no_entry": GOOD.replace("def filter(", "def basic_filter("),
        "two_entries": GOOD + extra_hist,
        "unknown_backend": GOOD.replace('EXECUTION_BACKEND = "polars_expr"', 'EXECUTION_BACKEND = "pandas"'),
        "removed_field": GOOD.replace('pl.col("vol_ma5")', 'pl.col("turnover_rate")'),
        "param_without_id": GOOD.replace('{"id": "min_momentum",', '{"id": "",'),
        "hist_with_filter": hist + extra_filter,
        "hist_as_polars": hist.replace('EXECUTION_BACKEND = "python_history_legacy"', 'EXECUTION_BACKEND = "polars_expr"'),
        "matrix_without_object": matrix.replace("MATRIX_STRATEGY = ", "MY_STRATEGY = "),
        "matrix_with_filter": matrix + "\n\ndef filter(df, params):\n    return None\n",
        "composite_with_filter": comp + "\n\ndef filter(df, params):\n    return None\n",
        "composite_without_children": re.sub(r'"children": \[.*?\],\n\}', '"children": [],\n}', comp, flags=re.S),
        "minute_daily_timeframe": minute.replace('"timeframes": ["1m"]', '"timeframes": ["1d"]'),
        "minute_with_filter": minute + extra_filter,
        "minute_without_entry": minute.replace("def filter_minute_history(", "def scan("),
    }
    for name, code in out.items():
        assert name == "good" or code != GOOD, name
    assert out["matrix_without_object"] != matrix and out["composite_without_children"] != comp
    assert out["hist_as_polars"] != hist and out["minute_daily_timeframe"] != minute
    return out


MUTATIONS = _mutations()


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_check_agrees_with_validator_and_engine(name, tmp_path):
    code = MUTATIONS[name]
    assert sc.check_code(code)["valid"] == _authoritative_ok(code, tmp_path), sc.check_code(code)["items"]
    assert sc.check_code(code)["valid"] == (name == "good")


def test_entry_item_names_the_offending_line():
    code = MUTATIONS["two_entries"]
    item = _items(code)["entry"]
    assert item["state"] == "bad" and item["section"] == 3
    assert code.splitlines()[item["line"] - 1].startswith("def filter_history(")


# ───────────────────────── 防漂移 ─────────────────────────

def _engine_backends() -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(StrategyEngine)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "valid_backends" for t in node.targets):
            return {ast.literal_eval(e) for e in node.value.elts}
    raise AssertionError("引擎里找不到 valid_backends, 请同步改这条测试")


def test_backends_match_engine():
    assert {b["id"] for b in sc.BACKENDS} == _engine_backends()


def test_engine_error_samples_still_in_source():
    source = inspect.getsource(engine_mod) + inspect.getsource(matrix_mod)
    for entry in sc.ENGINE_ERRORS:
        assert entry["sample"] in source, f"引擎里已没有这句报错, 请同步: {entry['sample']}"


@pytest.mark.parametrize("message, meaning", [
    ("策略保存失败: strategy reload failed: a.py: polars_expr strategy must declare only filter", "单日过滤写法只能有 filter 一个入口"),
    ("minute_filter strategy must declare timeframes == ['1m']", "分钟写法的周期必须是 1 分钟"),
    ("minute_filter daily_history_bars requires filter_minute_history to accept a 'daily' keyword", "声明了日线历史"),
    ("unsupported execution backend 'pandas'; expected one of ['composite', 'polars_expr']", "执行方式写错了"),
    ("META['asset_types'] must be a non-empty string list", "非空的字符串列表"),
    ("SignalMatrix.entry must use uint8, got bool", "数组类型不对"),
    ("composite strategy META['children'] must be a non-empty list", "叠加策略没有子策略"),
])
def test_translate_error(message, meaning):
    out = sc.translate_error(message)
    assert meaning in out and "处理：" in out and "原文：" in out  # noqa: RUF001
    assert out.startswith("策略保存失败：") == message.startswith("策略保存失败")  # noqa: RUF001


def test_translate_error_leaves_unknown_and_chinese_messages_alone():
    for message in ("禁止 import os (不在策略安全白名单)", "策略保存失败: 策略 custom_x 已存在", "boom", ""):
        assert sc.translate_error(message) == message


def test_real_engine_message_is_translated(tmp_path):
    """用引擎真实抛出的报错 (而不是手抄的字符串) 走一遍。"""
    code = MUTATIONS["two_entries"]
    (tmp_path / "custom_pullback_ma10.py").write_text(code, encoding="utf-8")
    eng = StrategyEngine(strategy_dirs=[tmp_path])
    raw = eng.load_errors()[0]["error"]
    assert raw == "polars_expr strategy must declare only filter"
    assert "单日过滤写法只能有 filter 一个入口" in sc.translate_error(f"策略保存失败: strategy reload failed: {raw}")


def test_standard_digest_matches_code_and_docs():
    digest = sc.standard_digest()
    assert digest["allowed_imports"] == sorted(AIStrategyGenerator._ALLOWED_IMPORT_MODULES)
    assert {"open", "exec", "eval"} <= set(digest["forbidden_calls"])
    assert {"__globals__", "__class__"} <= set(digest["forbidden_attrs"])
    assert [b["id"] for b in digest["backends"]] == [b["id"] for b in sc.BACKENDS]
    assert len(digest["errors"]) == len(sc.ENGINE_ERRORS)
    assert [t["n"] for t in digest["toc"]] == list(range(1, len(sc.STANDARD_TOC) + 1))
    # 抽屉里的条目能把人带到对的章节: 分项校验引用的章节号都在目录里
    assert {2, 3, 5, 6} <= {t["n"] for t in digest["toc"]}


@pytest.mark.skipif(not STANDARD.exists(), reason="docs/ 不在这份代码树里")
def test_standard_toc_matches_doc_headings():
    heads = re.findall(r"^## (\d+)\. (.+)$", STANDARD.read_text(encoding="utf-8"), re.M)
    assert [int(n) for n, _ in heads] == list(range(1, len(heads) + 1))
    assert [re.sub(r"[（(].*$", "", title).strip() for _, title in heads] == list(sc.STANDARD_TOC)  # noqa: RUF001


# ───────────────────────── 接口 ─────────────────────────

def test_endpoints_are_registered_and_read_only():
    routes = {(r.path, tuple(sorted(r.methods))) for r in strategy_api.router.routes}
    assert ("/api/strategies/code/check", ("POST",)) in routes
    assert ("/api/strategies/code/kit", ("GET",)) in routes
    assert "/api/strategies/code/check" in READ_SCOPE_POST_ALLOW      # 纯计算, 只读密钥可用
    out = strategy_api.check_strategy_code(strategy_api.StrategyCodeValidateRequest(code=GOOD))
    assert out["valid"] and [i["key"] for i in out["items"]] == ["safety", "meta", "entry", "fields"]
    kit = strategy_api.strategy_code_kit()
    assert len(kit["templates"]) == 6 and kit["standard"]["toc"]
