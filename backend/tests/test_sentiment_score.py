"""舆情评级: 中位数 + MAD 异常度、没数据的类不计分母、重点 / 异动规则、as-of 只用已入库数据、Reddit 覆盖区间、看板。"""
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from app.services import sentiment_score as sc
from app.services import sentiment_sources as ss

PREV = sc.weekday_prev_days
TH = sc.thresholds({"thresholds": {}})
T = datetime(2026, 11, 9, 3, 0, tzinfo=UTC)          # 周一 03:00Z = 周日 22:00 ET
NAMES = {"NVDA": {"aliases": ["nvidia", "jensen"], "wiki": []}, "MU": {"aliases": ["micron"], "wiki": []}}


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
    rows, i, d = [], 0, start
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


def _reddit(data_dir, posts, cover_from, cover_to):
    """posts = [(发帖时刻, 标题)]; 两个版块都写覆盖区间 (stocks 空版块也算已覆盖)。"""
    rows = [{"id": f"r{i}", "created_utc": int(t.timestamp()), "title": title, "selftext": ""}
            for i, (t, title) in enumerate(posts)]
    ss.append_posts(data_dir, "wallstreetbets", rows)
    for sub in ss.REDDIT_SUBS:
        ss.write_json(ss.reddit_dir(data_dir, sub) / "coverage.json", {"from": ss.iso(cover_from), "to": ss.iso(cover_to)})


def _reddit_baseline(end, days=30, per_day=2):
    out, d = [], end
    for _ in range(days):
        d -= timedelta(days=1)
        for k in range(per_day):
            out.append((datetime(d.year, d.month, d.day, 1 + 7 * k, tzinfo=UTC), f"NVDA daily thread {d} {k}"))
    return out


def test_robust_z_median_mad_and_poisson_floor():
    r = sc.robust_z(10, [1, 2, 2, 3, 2, 1, 2, 3, 2, 2], 10)
    assert r["med"] == 2 and r["mad"] == 0 and r["z"] == pytest.approx((10 - 2) / 2 ** 0.5, abs=0.01)
    r = sc.robust_z(100, [10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 10)
    assert r["med"] == 55 and r["mad"] == 25 and r["z"] == pytest.approx(45 / (1.4826 * 25), abs=0.01)
    assert sc.robust_z(5, [1] * 9, 10) is None             # 基线不足 = 该源不可用


def _event(tmp_path, *, tagged_late=False, with_wiki=True, hard=True, n_hi=8, reddit=None):
    rows = _baseline_rows(T)
    first = datetime(2026, 11, 8, 20, 51, tzinfo=UTC)
    for k in range(n_hi):
        created = first + timedelta(minutes=5 * k)
        rows.append(_row(f"e{k}", created, imp="high", direction="bearish", ev="management" if hard else "other",
                         tagged=T + timedelta(minutes=30) if tagged_late else created + timedelta(minutes=10)))
    _write_news(tmp_path, rows)
    if with_wiki:
        _wiki(tmp_path, "NVDA", _wiki_days(date(2026, 11, 9), spike={"2026-11-07": 33238, "2026-11-08": 122360}))
    if reddit is not None:
        _reddit(tmp_path, reddit, T - timedelta(days=40), T)
    return sc.load_inputs(tmp_path, ["NVDA"], T - timedelta(days=45), T, names=NAMES)


def test_focus_needs_two_families_direction_and_hard_event(tmp_path):
    r = sc.evaluate(_event(tmp_path), "NVDA", T, TH, prev_days=PREV)
    assert r["grade"] == "A" and r["direction"] == "bearish" and r["event_type"] == "management"
    assert r["n_families"] == 2 and set(r["families_ok"]) == {"news", "attention"}
    assert r["missing"] == ["social"]
    assert r["heat"] == round((r["families"]["news"]["heat"] + r["families"]["attention"]["heat"]) / 2)
    assert r["first_at"] == "2026-11-08T20:51:00Z" and r["headlines"][0]["zh"].startswith("标题e")
    assert r["posts"] == []


def test_single_family_is_watch_not_focus(tmp_path):
    r = sc.evaluate(_event(tmp_path, with_wiki=False), "NVDA", T, TH, prev_days=PREV)
    assert r["n_families"] == 1 and r["grade"] == "B"


def test_reddit_family_counts_matched_posts_and_can_be_the_second_family(tmp_path):
    spike = [(T - timedelta(hours=h, minutes=40), f"Jensen post {h}") for h in range(1, 9)]
    noise = [(T - timedelta(hours=2), "Micron earnings soon"), (T - timedelta(hours=3), "much music, no ticker")]
    r = sc.evaluate(_event(tmp_path, with_wiki=False, reddit=_reddit_baseline(T) + spike + noise), "NVDA", T, TH,
                    prev_days=PREV)
    soc = r["families"]["social"]["sources"]["reddit"]
    assert soc["x"] == 9 and soc["med"] == 2 and soc["anom"] is True     # 8 帖异动 + 窗口里 1 帖日常
    assert r["grade"] == "A" and set(r["families_ok"]) == {"news", "social"}
    assert [p["title"] for p in r["posts"]][:2] == ["Jensen post 1", "Jensen post 2"]
    assert r["posts"][0]["sub"] == "wallstreetbets"


def test_reddit_post_not_available_until_30_minutes_after_posting(tmp_path):
    late = [(T - timedelta(minutes=20), f"Nvidia fresh {k}") for k in range(10)]
    r = sc.evaluate(_event(tmp_path, with_wiki=False, reddit=_reddit_baseline(T) + late), "NVDA", T, TH, prev_days=PREV)
    assert r["families"]["social"]["sources"]["reddit"]["x"] == 1   # 只剩 1 帖日常, 20 分钟前的 10 帖还不可得


def test_reddit_needs_coverage_for_window_and_fresh_fetch(tmp_path):
    _event(tmp_path, with_wiki=False)
    _reddit(tmp_path, _reddit_baseline(T), T - timedelta(hours=12), T)
    inp = sc.load_inputs(tmp_path, ["NVDA"], T - timedelta(days=45), T, names=NAMES)
    assert "social" not in sc.evaluate(inp, "NVDA", T, TH, prev_days=PREV)["families_ok"]   # 24 小时窗口没覆盖全
    _reddit(tmp_path, [], T - timedelta(days=40), T - timedelta(hours=3))
    inp = sc.load_inputs(tmp_path, ["NVDA"], T - timedelta(days=45), T, names=NAMES)
    assert "social" not in sc.evaluate(inp, "NVDA", T, TH, prev_days=PREV)["families_ok"]   # 3 小时没拉到新帖: 不完整


def test_no_hard_event_is_watch(tmp_path):
    r = sc.evaluate(_event(tmp_path, hard=False), "NVDA", T, TH, prev_days=PREV)
    assert r["grade"] == "B" and r["hard"] is False


def test_as_of_ignores_items_tagged_after_as_of(tmp_path):
    r = sc.evaluate(_event(tmp_path, tagged_late=True), "NVDA", T, TH, prev_days=PREV)
    assert r["bull_w"] == 0 and r["bear_w"] == 0 and r["direction"] == "none" and r["grade"] != "A"
    later = sc.evaluate(sc.load_inputs(tmp_path, ["NVDA"], T - timedelta(days=45), T + timedelta(hours=1), names=NAMES),
                        "NVDA", T + timedelta(hours=1), TH, prev_days=PREV)
    assert later["grade"] == "A"


def test_wiki_day_not_used_before_it_is_complete_plus_lag(tmp_path):
    inp = _event(tmp_path)
    at = datetime(2026, 11, 8, 11, 0, tzinfo=UTC)
    assert sc.evaluate(inp, "NVDA", at, TH, prev_days=PREV)["families"]["attention"]["sources"]["wiki"]["day"] == "2026-11-06"
    r = sc.evaluate(inp, "NVDA", at + timedelta(hours=2), TH, prev_days=PREV)
    assert r["families"]["attention"]["sources"]["wiki"]["day"] == "2026-11-07"


def test_quiet_symbol_has_no_grade_and_threshold_raise_drops_focus(tmp_path):
    inp = _event(tmp_path)
    assert sc.evaluate(inp, "NVDA", datetime(2026, 11, 4, 3, 0, tzinfo=UTC), TH, prev_days=PREV)["grade"] is None
    assert sc.evaluate(inp, "NVDA", T, sc.thresholds(None, {"heat_line": 99}), prev_days=PREV)["grade"] != "A"


def test_build_board_lists_focus_and_watch_only(tmp_path):
    _event(tmp_path)
    b = sc.build_board(tmp_path, {"NVDA": "池", "MU": "自选"}, T, prev_days=PREV, names=NAMES)
    assert b["counts"]["A"] == 1 and [r["symbol"] for r in b["rows"]] == ["NVDA"]
    row = b["rows"][0]
    assert row["role"] == "池" and "line" not in row and b["config_sha"] == sc.config_sha()
    assert b["families"] == {"news": 2, "social": 0, "attention": 1}


def test_pool_config_is_the_24_names():
    cfg = ss.load_config()
    assert cfg["pool"] == ["SNDK", "SPCX", "MU", "DELL", "NVDA", "TSLA", "CRCL", "INTC", "MRVL", "PLTR", "AMD", "AAPL",
                           "META", "RKLB", "ORCL", "LITE", "MSTR", "AAOI", "AVGO", "ARM", "COIN", "AMZN", "TSM", "MSFT"]
    assert set(cfg["names"]) == set(cfg["pool"]) and all(v["aliases"] for v in cfg["names"].values())
