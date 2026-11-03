"""因子页审计复现 (先红): 采集条目并发丢更新、决定记录并发、卡片截断、下次运行时刻。"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

from app.services import factor_discover as fd
from app.services import factor_intake as fi


def _seed(data_dir, entries):
    items = {}
    for e in entries:
        items[e["id"]] = {"source": "concretum", "source_name": "Concretum", "link": "", "published": "2026-10-30T00:00:00+00:00",
                          "fetched_at": datetime.now(UTC).isoformat(), "prefilter": "llm", **e}
    fi._save_items(data_dir, items)


def test_decision_made_during_model_queue_is_not_overwritten(tmp_path, monkeypatch):
    """用户在模型队列运行期间「忽略」一条待处理条目, 队列跑完后不能把它写回待处理。"""
    _seed(tmp_path, [
        {"id": "q1", "title": "t", "body": "x", "stage": "queued"},
        {"id": "p1", "title": "pending", "body": "x", "stage": "pending",
         "formulas": [{"name": "n", "quote": "q", "quote_hash": "h" * 16, "dsl": "close", "direction": "high"}]},
    ])

    def fake_call(dd, task, prompt, *, max_tokens):
        fi.ignore(dd, "p1")  # 页面上的点击发生在模型调用期间
        return json.dumps({"market": "other", "kind": "none", "paywall": False})

    monkeypatch.setattr(fi, "_call", fake_call)
    fi.process_queue(tmp_path)
    items = fi.load_items(tmp_path)
    assert items["q1"]["stage"] == "archived"
    assert items["p1"]["stage"] == "archived" and items["p1"]["reason"] == "已忽略"


def test_decision_made_during_fetch_is_not_overwritten(tmp_path, monkeypatch):
    _seed(tmp_path, [{"id": "p1", "title": "pending", "body": "x", "stage": "pending",
                      "formulas": [{"name": "n", "quote": "q", "quote_hash": "h" * 16, "dsl": "close", "direction": "high"}]}])
    monkeypatch.setattr(fi, "_public_http_url", lambda url: url)

    def fake_fetch(url, timeout=20.0):
        if "concretum" in url and fi.load_items(tmp_path)["p1"]["stage"] == "pending":
            fi.ignore(tmp_path, "p1")
        raise OSError("down")

    monkeypatch.setattr(fi, "_fetch", fake_fetch)
    fi.fetch_all(tmp_path)
    assert fi.load_items(tmp_path)["p1"]["stage"] == "archived"


def test_pending_cards_are_never_cut_by_the_card_cap(tmp_path):
    """卡片列表上限 40 只截已归档; 待处理的必须全部出现, 否则流程条/角标与列表对不上。"""
    entries = [{"id": f"a{i:02d}", "title": f"dup {i}", "body": "x", "stage": "archived", "duplicate": True,
                "published": f"2026-10-{(i % 28) + 1:02d}T12:00:00+00:00",
                "formulas": [{"name": "d", "quote": "q", "quote_hash": f"{i:016d}", "dsl": "close", "direction": "high"}]}
               for i in range(45)]
    entries.append({"id": "old_pending", "title": "old", "body": "x", "stage": "pending", "published": "2026-10-01T00:00:00+00:00",
                    "formulas": [{"name": "p", "quote": "q", "quote_hash": "p" * 16, "dsl": "close", "direction": "high"}]})
    _seed(tmp_path, entries)
    ov = fi.overview(tmp_path, days=3650)
    assert ov["funnel"]["pending"] == 1
    assert any(c["id"] == "old_pending" for c in ov["cards"])


def test_concurrent_decisions_are_all_recorded(tmp_path, monkeypatch):
    week = {"week": "2026-W44", "run_id": "weekly-2026-W44", "candidates": [{"signature": f"s{i}"} for i in range(20)]}
    (tmp_path / "research" / "discover").mkdir(parents=True)
    (tmp_path / "research" / "discover" / "2026-W44.json").write_text(json.dumps(week), encoding="utf-8")
    orig = fd._decisions

    def slow(dd):
        out = orig(dd)
        threading.Event().wait(0.01)
        return out

    monkeypatch.setattr(fd, "_decisions", slow)
    threads = [threading.Thread(target=fd.decide, args=(tmp_path, "2026-W44", f"s{i}", "discard")) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(orig(tmp_path)) == 20


def test_next_weekly_is_today_until_the_discover_job_has_run(monkeypatch):
    """发现任务周五 17:40 ET 才跑; 17:00-17:40 之间「下次」仍应是当天。"""
    from zoneinfo import ZoneInfo

    from app.market_clock import us_clock
    friday = datetime(2026, 11, 6, 17, 20, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(us_clock, "now", lambda: friday)
    monkeypatch.setattr(us_clock, "today", lambda: friday.date())
    monkeypatch.setattr(us_clock, "is_trading_day", lambda d: d.weekday() < 5)
    assert fd._schedule()["next_weekly"] == "2026-11-06"


def test_cross_article_dedupe_sees_quotes_from_earlier_runs(tmp_path, monkeypatch):
    """原句去重要看已有条目里每条公式的 quote_hash (存在 formulas[] 里, 不在条目顶层)。"""
    quote = "stocks with a higher frequency of negative relative to positive daily returns over the prior month"
    import hashlib
    qh = hashlib.sha1(fi._norm(quote).encode()).hexdigest()[:16]
    _seed(tmp_path, [
        {"id": "old", "title": "first", "body": quote, "stage": "pending",
         "formulas": [{"name": "n", "quote": quote, "quote_hash": qh, "dsl": "close", "direction": "high"}]},
        {"id": "new", "title": "second", "body": f"repost: {quote}", "stage": "to_extract"},
    ])

    def fake_call(dd, task, prompt, *, max_tokens):
        return json.dumps({"summary_zh": "s", "items": [{"name_zh": "x", "quote": quote,
                                                        "dsl": "close / ts_delay(close, 5) - 1", "direction": "high"}]})

    monkeypatch.setattr(fi, "_call", fake_call)
    monkeypatch.setattr(fi, "_rules_vocab", lambda dd: "")
    fi.process_queue(tmp_path)
    new = fi.load_items(tmp_path)["new"]
    assert new["stage"] == "archived" and new["reason"] == "跟已有因子重复"


def test_model_error_flag_clears_after_a_successful_call(tmp_path, monkeypatch):
    _seed(tmp_path, [{"id": "a", "title": "t", "body": "x", "stage": "queued", "last_error": "RuntimeError: 404"}])
    fi.save_settings(tmp_path, daily_limit=1)  # 分拣成功后预算用完, 条目停在待抽取
    monkeypatch.setattr(fi, "_call", lambda *a, **k: json.dumps({"market": "us_stock", "kind": "factor", "paywall": False}))
    fi.process_queue(tmp_path)
    assert fi.load_items(tmp_path)["a"]["stage"] == "to_extract"
    assert fi.overview(tmp_path)["last_error"] is None


def test_engine_recalc_returns_the_same_flat_shape_as_candidate_recalc(tmp_path, monkeypatch):
    """前端按 DiscoverStats 平铺字段读重算结果; 当前出卡引擎重算原来返回 {stats, yearly, legs} 嵌套, 指标全显示 —。"""
    ref = {"stats": {"annual_return": 0.3, "n_trades": 10}, "yearly": {"2024": 0.1}, "legs": [{"id": "r"}]}
    monkeypatch.setattr(fd, "_service", lambda repo, dd: object())
    monkeypatch.setattr(fd, "engine_reference", lambda *a, **k: ref)
    from datetime import date
    out = fd._recalc_impl(tmp_path, None, "engine", symbols=["AAA"], start=date(2024, 1, 2), end=date(2024, 6, 1))
    assert out["annual_return"] == 0.3 and out["n_trades"] == 10
    assert out["yearly"] == {"2024": 0.1} and out["legs"] == [{"id": "r"}]
