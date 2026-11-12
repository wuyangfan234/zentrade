"""舆情评级 (第一期): 中位数 + MAD 异常度、缺源族不计分母、A/B/C 规则、as-of 只用已入库数据、看板。"""
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from app.services import sentiment_score as sc
from app.services import sentiment_sources as ss

PREV = sc.weekday_prev_days
TH = sc.thresholds({"thresholds": {}})
T = datetime(2026, 11, 9, 3, 0, tzinfo=UTC)          # 周一 03:00Z = 周日 22:00 ET


def _row(i, created, sym="NVDA", imp="low", direction="neutral", ev="other", tagged=None, rel="primary"):
    return {"id": f"x{i}", "headline": f"h{i}", "zh_headline": f"标题{i}", "created_at": ss.iso(created),
            "symbols": [sym], "sources": ["finnhub"], "mentions": {sym: "headline"},
            "tags": {sym: {"relevance": rel, "event_type": ev, "direction": direction, "importance": imp}},
            "tagged_by": {"at": ss.iso(tagged or created + timedelta(minutes=5)), "mode": "llm"}}


def _write_news(data_dir, rows):
    d = data_dir / "news"
    d.mkdir(parents=True, exist_ok=True)
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["created_at"][:7], []).append(r)
    for m, rs in by.items():
        (d / f"{m}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rs))


def _baseline_rows(start, days=30, per_day=4, sym="NVDA"):
    """每个工作日固定几条低重要度新闻 (分布在全天), 当基线。"""
    rows, i = [], 0
    d = start
    for _ in range(days):
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        for k in range(per_day):
            rows.append(_row(f"b{i}", datetime(d.year, d.month, d.day, 2 + 5 * k, 0, tzinfo=UTC), sym=sym))
            i += 1
    return rows


def _wiki(data_dir, sym, views_by_day):
    ss.write_json(ss.root(data_dir) / "series" / "wiki" / f"{sym}.json", {"articles": {"CEO": views_by_day}})


def _wiki_days(end: date, base=5000, spike=None):
    out, d = {}, end - timedelta(days=45)
    while d <= end:
        out[d.isoformat()] = base + (d.toordinal() % 7) * 50
        d += timedelta(days=1)
    out.update(spike or {})
    return out


def test_robust_z_median_mad_and_poisson_floor():
    r = sc.robust_z(10, [1, 2, 2, 3, 2, 1, 2, 3, 2, 2], 10)
    assert r["med"] == 2 and r["mad"] == 0 and r["z"] == pytest.approx((10 - 2) / 2 ** 0.5, abs=0.01)
    r = sc.robust_z(100, [10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 10)
    assert r["med"] == 55 and r["mad"] == 25 and r["z"] == pytest.approx(45 / (1.4826 * 25), abs=0.01)
    assert sc.robust_z(5, [1] * 9, 10) is None             # 基线不足 = 该源不可用


def _event(tmp_path, *, tagged_late=False, with_wiki=True, hard=True, n_hi=8):
    rows = _baseline_rows(T)
    first = datetime(2026, 11, 8, 20, 51, tzinfo=UTC)
    for k in range(n_hi):
        created = first + timedelta(minutes=5 * k)
        rows.append(_row(f"e{k}", created, imp="high", direction="bearish", ev="management" if hard else "other",
                         tagged=T + timedelta(minutes=30) if tagged_late else created + timedelta(minutes=10)))
    _write_news(tmp_path, rows)
    if with_wiki:
        _wiki(tmp_path, "NVDA", _wiki_days(date(2026, 11, 9), spike={"2026-11-07": 33238, "2026-11-08": 122360}))
    return sc.load_inputs(tmp_path, ["NVDA"], T - timedelta(days=45), T)


def test_a_grade_needs_two_families_direction_and_hard_event(tmp_path):
    r = sc.evaluate(_event(tmp_path), "NVDA", T, TH, prev_days=PREV)
    assert r["grade"] == "A" and r["direction"] == "bearish" and r["event_type"] == "management"
    assert r["n_families"] == 2 and set(r["families_ok"]) == {"news", "attention"}
    assert r["missing"] == ["media", "social"]
    # 缺源的族不计入分母: 热度 = 两个有数据的族的平均
    assert r["heat"] == round((r["families"]["news"]["heat"] + r["families"]["attention"]["heat"]) / 2)
    assert r["first_at"] == "2026-11-08T20:51:00Z"
    assert r["headlines"][0]["zh"].startswith("标题e")


def test_single_family_is_b_not_a(tmp_path):
    r = sc.evaluate(_event(tmp_path, with_wiki=False), "NVDA", T, TH, prev_days=PREV)
    assert r["n_families"] == 1 and r["grade"] == "B"


def test_no_hard_event_is_b(tmp_path):
    r = sc.evaluate(_event(tmp_path, hard=False), "NVDA", T, TH, prev_days=PREV)
    assert r["grade"] == "B" and r["hard"] is False


def test_as_of_ignores_items_tagged_after_as_of(tmp_path):
    """发布在 as-of 之前、打标 (入库) 在 as-of 之后的新闻不能用: 不计数、不给方向。"""
    r = sc.evaluate(_event(tmp_path, tagged_late=True), "NVDA", T, TH, prev_days=PREV)
    assert r["bull_w"] == 0 and r["bear_w"] == 0 and r["direction"] == "none"
    assert r["grade"] != "A"
    later = sc.evaluate(sc.load_inputs(tmp_path, ["NVDA"], T - timedelta(days=45), T + timedelta(hours=1)),
                        "NVDA", T + timedelta(hours=1), TH, prev_days=PREV)
    assert later["grade"] == "A"


def test_wiki_day_not_used_before_it_is_complete_plus_lag(tmp_path):
    inp = _event(tmp_path)
    at = datetime(2026, 11, 8, 11, 0, tzinfo=UTC)           # 11-07 结束 + 12 小时之前: 只能用 11-06
    r = sc.evaluate(inp, "NVDA", at, TH, prev_days=PREV)
    assert r["families"]["attention"]["sources"]["wiki"]["day"] == "2026-11-06"
    r = sc.evaluate(inp, "NVDA", at + timedelta(hours=2), TH, prev_days=PREV)
    assert r["families"]["attention"]["sources"]["wiki"]["day"] == "2026-11-07"


def test_quiet_symbol_has_no_grade_and_threshold_raise_drops_a(tmp_path):
    inp = _event(tmp_path)
    quiet = sc.evaluate(inp, "NVDA", datetime(2026, 11, 4, 3, 0, tzinfo=UTC), TH, prev_days=PREV)
    assert quiet["grade"] is None
    assert sc.evaluate(inp, "NVDA", T, sc.thresholds(None, {"heat_line": 99}), prev_days=PREV)["grade"] != "A"


def test_hourly_window_needs_full_coverage():
    cur = {"from": "2026-11-05T00", "to": "2026-11-09T00", "hours": {"2026-11-08T20": 5}}
    assert sc._hourly_window(cur, datetime(2026, 11, 8, 22, 20, tzinfo=UTC), 24, sc.GDELT_LAG) == 5
    assert sc._hourly_window(cur, datetime(2026, 11, 9, 2, 0, tzinfo=UTC), 24, sc.GDELT_LAG) is None


def test_build_board_lists_only_a_b_with_heat_line(tmp_path):
    _event(tmp_path)
    b = sc.build_board(tmp_path, {"NVDA": "池", "MU": "自选"}, T, prev_days=PREV)
    assert b["counts"]["A"] == 1 and [r["symbol"] for r in b["rows"]] == ["NVDA"]
    row = b["rows"][0]
    assert row["role"] == "池" and len(row["line"]) == sc.LINE_HOURS + 1 and row["line"][-1][1] == row["heat"]
    assert b["families"]["news"] == 2 and b["config_sha"] == sc.config_sha()   # 新闻库覆盖期内 0 条也是有效数据, 不是缺源


def test_pool_config_is_the_24_names():
    cfg = ss.load_config()
    assert cfg["pool"] == ["SNDK", "SPCX", "MU", "DELL", "NVDA", "TSLA", "CRCL", "INTC", "MRVL", "PLTR", "AMD", "AAPL",
                           "META", "RKLB", "ORCL", "LITE", "MSTR", "AAOI", "AVGO", "ARM", "COIN", "AMZN", "TSM", "MSFT"]
    assert set(cfg["names"]) == set(cfg["pool"])
