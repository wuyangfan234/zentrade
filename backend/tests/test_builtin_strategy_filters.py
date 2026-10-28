"""Builtin strategies must not carry A-share default filters (price<=200 CNY, 20亿市值, 1亿成交额):
on US data they silently zero the strategy (trend_breakout selected 0 stocks in 4.7 years)."""
import ast
from pathlib import Path

BUILTIN = Path(__file__).parents[1] / "app" / "strategy" / "builtin"


def _meta(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "META" for t in node.targets):
            return ast.literal_eval(node.value)
    return {}


def test_no_builtin_strategy_overrides_us_default_filter_with_a_share_values():
    offenders = {}
    for f in sorted(BUILTIN.glob("*.py")):
        bf = (_meta(f) or {}).get("basic_filter") or {}
        bad = {k: v for k, v in bf.items()
               if (k == "price_max" and v is not None and v <= 500) or (k == "market_cap_min" and v)
               or (k == "exclude_st") or (k == "amount_min" and v and v > 5e7)}
        if bad:
            offenders[f.stem] = bad
    assert offenders == {}
