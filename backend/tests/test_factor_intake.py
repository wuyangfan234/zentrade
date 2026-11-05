"""0.47.0 public factor intake: parse, pre-filter, four machine checks, budget, model failure, decisions.

No network and no model: `_fetch` and `_call` are stubbed. The cases come from the 2026-11-02 pilot
(Concretum Pills #26, Quantitativo "Less Wrong", Quantified Strategies RSI(2) article).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.factors import registry
from app.services import factor_intake as fi

RSS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><title>Concretum Pills #26</title><link>https://example.com/p26</link><pubDate>Mon, 06 Oct 2026 10:00:00 GMT</pubDate>
<content:encoded><![CDATA[<p>Cross-sectional equity anomaly. In this paper, the authors document that stocks with a higher frequency of negative relative to positive daily returns over the prior month tend to earn higher returns in the following month. Momentum and reversal factor across US stocks, volume and volatility controls.</p>]]></content:encoded></item>
<item><title>Bitcoin halving clock</title><link>https://example.com/btc</link><description>crypto bitcoin solana defi lending</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Less Wrong</title>
<link href="https://example.com/lesswrong"/><published>2026-10-18T12:00:00Z</published>
<content>Price momentum : the classic 12-to-1-month past return. Stocks cross-section factor signal equity momentum reversal volume.</content></entry></feed>"""


@pytest.fixture(autouse=True)
def freeze_intake_clock(monkeypatch):
    # 样例固定在 2026-10 月; 冻结时钟, 避免真实日期越过 30 天新鲜度窗口后批量变 stale。
    monkeypatch.setattr(fi, "_now", lambda: datetime(2026, 11, 3, 12, tzinfo=UTC))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fi, "_public_http_url", lambda url: url)
    return tmp_path


def test_parse_rss_and_atom():
    rss = fi.parse_feed(RSS)
    assert [x["title"] for x in rss] == ["Concretum Pills #26", "Bitcoin halving clock"]
    assert rss[0]["published"].startswith("2026-10-06") and "higher frequency of negative" in rss[0]["body"]
    atom = fi.parse_feed(ATOM)
    assert atom[0]["link"] == "https://example.com/lesswrong" and atom[0]["published"].startswith("2026-10-18")


def test_parse_rejects_dtd_entities():
    bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "x">]><rss><channel><item><title>&a;</title></item></channel></rss>'
    with pytest.raises(ValueError):
        fi.parse_feed(bomb)


def test_prefilter_drops_offtopic_and_flags_paywall():
    assert fi.prefilter("Bitcoin halving", "crypto bitcoin solana defi")[0] == "drop"
    body = "stocks momentum reversal factor signal equity volume close " * 5 + " The entry and exit rules are collected in the subscriber section below."
    assert fi.prefilter("N-Day Low Strategy For Stocks", body)[0] == "paywall"
    assert fi.prefilter("Pills", "stocks momentum reversal factor signal equity volume close " * 40)[0] == "llm"


def test_default_watch_list_has_quantified_strategies_off(data_dir):
    sources = fi.load_sources(data_dir)
    assert len(sources) == 24
    qs = next(s for s in sources if s["id"] == "qstrat")
    assert qs["enabled"] is False
    assert sum(1 for s in sources if s["enabled"]) == 23


def test_quote_must_be_verbatim():
    art = "Price momentum : the classic 12-to-1-month past return. Other text."
    assert fi.quote_verbatim("Price momentum : the classic 12-to-1-month past return", art)
    assert fi.quote_verbatim("price   momentum : the classic 12-to-1-month   past return", art)
    assert not fi.quote_verbatim("Price momentum is the 12-1 month return", art)
    assert not fi.quote_verbatim("momentum", art)  # too short to prove anything


def test_period_guard_catches_rsi6_standing_in_for_rsi2():
    art = "SPY daily data RSI with a 2-day lookback RSI thresholds from 1 through 50. Five-day holding period RSI 6 produced 164 trades"
    assert fi.period_mismatches("if_else(rsi_6 < 6 and close > ts_mean(close, 200), 1, 0)", art) == ["rsi_6"]
    assert fi.period_mismatches("ts_delay(close, 21) / ts_delay(close, 252) - 1", art) == []
    assert fi.period_mismatches("rsi_6 < 30", "the RSI(6) indicator below 30") == []


def _seed(data_dir, entries):
    items = {}
    for e in entries:
        items[e["id"]] = {"source": "concretum", "source_name": "Concretum", "link": "", "published": "2026-10-06T00:00:00+00:00",
                          "fetched_at": datetime.now(UTC).isoformat(), "prefilter": "llm", **e}
    fi._save_items(data_dir, items)


def test_process_queue_triage_extract_and_checks(data_dir, monkeypatch):
    body = ("In this paper, the authors document that stocks with a higher frequency of negative relative to positive daily "
            "returns over the prior month tend to earn higher returns in the following month. RSI with a 2-day lookback.")
    _seed(data_dir, [{"id": "a1", "title": "Pills #26", "body": body, "stage": "queued"},
                     {"id": "a2", "title": "Index timing", "body": body, "stage": "queued", "published": "2026-10-20T00:00:00+00:00"}])
    calls = []

    def fake_call(dd, task, prompt, *, max_tokens):
        calls.append(task)
        fi._charge(dd, task, 100)
        if task == "factor_triage":
            return json.dumps({"market": "us_stock" if "Pills" in prompt else "us_index", "kind": "factor", "paywall": False})
        return json.dumps({"summary_zh": "涨跌天数失衡", "items": [
            {"name_zh": "涨跌天数失衡", "quote": "stocks with a higher frequency of negative relative to positive daily returns over the prior month",
             "dsl": "-1 * (ts_sum(if_else(change_pct > 0, 1, 0), 20) - ts_sum(if_else(change_pct < 0, 1, 0), 20))", "direction": "high"},
            {"name_zh": "编造", "quote": "this sentence is not in the article at all", "dsl": "close", "direction": "high"},
            {"name_zh": "RSI2", "quote": "RSI with a 2-day lookback", "dsl": "rsi_6 < 10", "direction": "low"},
            {"name_zh": "坏公式", "quote": "tend to earn higher returns in the following month", "dsl": "and(close, open)", "direction": "high"},
        ]})

    monkeypatch.setattr(fi, "_call", fake_call)
    monkeypatch.setattr(fi, "_rules_vocab", lambda dd: "")
    out = fi.process_queue(data_dir)
    items = fi.load_items(data_dir)
    assert out["triaged"] == 2 and out["extracted"] == 1
    assert items["a2"]["stage"] == "archived" and items["a2"]["reason"] == "指数择时"
    a1 = items["a1"]
    assert a1["stage"] == "to_screen"
    assert [f["name"] for f in a1["formulas"]] == ["涨跌天数失衡"]  # fabricated quote, period mismatch, bad DSL all dropped
    assert a1["formulas"][0]["warmup"] >= 20 and a1["formulas"][0]["checks"] == {"quote": True, "compile": True, "period": True}


def test_daily_budget_caps_model_calls(data_dir, monkeypatch):
    fi.save_settings(data_dir, daily_limit=2)
    _seed(data_dir, [{"id": f"b{i}", "title": f"t{i}", "body": "x", "stage": "queued"} for i in range(5)])
    n = []

    def fake_call(dd, task, prompt, *, max_tokens):
        n.append(task)
        fi._charge(dd, task, 10)
        return json.dumps({"market": "other", "kind": "none", "paywall": False})

    monkeypatch.setattr(fi, "_call", fake_call)
    fi.process_queue(data_dir)
    assert len(n) == 2
    assert fi.usage(data_dir)["today"] == 2
    assert sum(1 for it in fi.load_items(data_dir).values() if it["stage"] == "queued") == 3
    fi.process_queue(data_dir)  # budget used up: no more calls today
    assert len(n) == 2


def test_model_failure_keeps_items_queued(data_dir, monkeypatch):
    _seed(data_dir, [{"id": "c1", "title": "t", "body": "x", "stage": "queued"}])

    def boom(*a, **k):
        raise RuntimeError("AI 服务请求失败(404): model switched off")

    monkeypatch.setattr(fi, "_call", boom)
    out = fi.process_queue(data_dir)
    it = fi.load_items(data_dir)["c1"]
    assert out["errors"] == 1 and it["stage"] == "queued" and "404" in it["last_error"]
    assert fi.overview(data_dir)["last_error"].startswith("RuntimeError")


def test_merge_creates_watch_factor_and_ignore_restore(data_dir, monkeypatch):
    fid = None
    _seed(data_dir, [{"id": "d1", "title": "Less Wrong", "body": "b", "stage": "pending", "formulas": [
        {"name": "12-1月动量", "quote": "Price momentum : the classic 12-to-1-month past return", "quote_hash": "abcdef1234567890",
         "dsl": "ts_delay(close, 21) / ts_delay(close, 252) - 1", "direction": "high", "warmup": 253}]},
        {"id": "d2", "title": "x", "body": "b", "stage": "pending", "formulas": [
        {"name": "x", "quote": "q", "quote_hash": "0000000000000000", "dsl": "close", "direction": "high"}]}])
    try:
        out = fi.merge(data_dir, "d1")
        fid = out["id"]
        assert out["status"] == "watch" and fid.startswith("uf_ci_")
        saved = json.loads((data_dir / "user_data" / "custom_factors" / f"{fid}.json").read_text())
        assert saved["status"] == "watch" and saved["group"] == "采集" and saved["source"]["type"] == "intake"
        assert registry.get_factor(fid) is not None
        assert fi.load_items(data_dir)["d1"]["stage"] == "merged"
        with pytest.raises(KeyError):
            fi.merge(data_dir, "d1")  # already decided
        fi.ignore(data_dir, "d2")
        assert fi.load_items(data_dir)["d2"]["stage"] == "archived"
        fi.restore(data_dir, "d2")
        assert fi.load_items(data_dir)["d2"]["stage"] == "pending"
    finally:
        if fid:
            registry.unregister_factor(fid)


def test_fetch_all_failure_isolated_and_prefilter_applied(data_dir, monkeypatch):
    def fake_fetch(url, timeout=20.0):
        if "concretum" in url:
            return RSS
        if "quantitativo" in url:
            return ATOM
        raise OSError("down")

    monkeypatch.setattr(fi, "_fetch", fake_fetch)
    out = fi.fetch_all(data_dir)
    items = fi.load_items(data_dir)
    assert out["added"] == 3
    stages = {it["title"]: it["stage"] for it in items.values()}
    assert stages["Bitcoin halving clock"] == "archived"
    assert stages["Concretum Pills #26"] == "queued"
    state = json.loads((data_dir / "research" / "intake" / "fetch_state.json").read_text())
    assert state["concretum"]["error"] is None and state["alvarez"]["error"] == "OSError"
    assert "qstrat" not in state or not state["qstrat"].get("last_ok")  # disabled source is never fetched
    again = fi.fetch_all(data_dir)
    assert again["added"] == 0  # dedupe by link + title


def test_add_source_rejects_private_hosts(tmp_path):
    with pytest.raises(ValueError):
        fi.add_source(tmp_path, name="x", url="http://127.0.0.1/feed", kind="site")
    with pytest.raises(ValueError):
        fi.add_source(tmp_path, name="x", url="file:///etc/passwd", kind="site")


def test_builtin_source_cannot_be_deleted(data_dir):
    fi.load_sources(data_dir)
    with pytest.raises(ValueError):
        fi.remove_source(data_dir, "concretum")
    fi.set_source_enabled(data_dir, "concretum", False)
    assert next(s for s in fi.load_sources(data_dir) if s["id"] == "concretum")["enabled"] is False


def test_settings_validate_strictly(tmp_path):
    for bad in (-1, 1001, True, "5"):
        with pytest.raises(ValueError):
            fi.save_settings(tmp_path, daily_limit=bad)
    assert fi.save_settings(tmp_path, daily_limit=12)["daily_limit"] == 12


def test_ai_tasks_registered():
    from app.services.ai_provider import AI_TASKS
    assert AI_TASKS["factor_triage"]["default_thinking"] == "off"
    assert "factor_extract" in AI_TASKS


def test_intake_never_touches_trading_files(data_dir, monkeypatch):
    """Intake writes only research/intake and (on merge) user_data/custom_factors."""
    monkeypatch.setattr(fi, "_fetch", lambda url, timeout=20.0: RSS)
    fi.fetch_all(data_dir)
    written = {p.relative_to(data_dir).parts[0] + "/" + p.relative_to(data_dir).parts[1] for p in data_dir.rglob("*") if p.is_file()}
    assert written <= {"research/intake"}


def test_merge_without_publish_date_and_hostile_hash(data_dir):
    """Manual submissions may have no date; quote hash never reaches the file name unsanitized."""
    _seed(data_dir, [{"id": "m1", "title": "pasted", "body": "b", "stage": "pending", "published": None, "formulas": [
        {"name": "x", "quote": "q", "quote_hash": "../../etc/xx", "dsl": "close / ts_delay(close, 2) - 1", "direction": "high"}]}])
    out = fi.merge(data_dir, "m1")
    try:
        assert "/" not in out["id"] and "." not in out["id"]
        assert (data_dir / "user_data" / "custom_factors" / f"{out['id']}.json").exists()
    finally:
        registry.unregister_factor(out["id"])


def test_fetch_rechecks_every_redirect_hop(monkeypatch):
    """A public feed that redirects to an internal address is refused at the redirect hop."""
    import httpx
    hops = []

    def handler(request):
        hops.append(str(request.url))
        if request.url.host == "feed.example.com":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8080/secret"})
        return httpx.Response(200, content=b"secret")

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    checked = []

    def guard(url):
        checked.append(url)
        if "127.0.0.1" in url:
            raise ValueError("不允许内网或本机地址")
        return url

    monkeypatch.setattr(fi, "_public_http_url", guard)
    with pytest.raises(ValueError):
        fi._fetch("https://feed.example.com/rss")
    assert hops == ["https://feed.example.com/rss"] and checked[-1].startswith("http://127.0.0.1")


@pytest.mark.parametrize("ip,blocked", [
    ("198.18.15.181", False), ("198.19.255.1", False),          # proxy fake-IP range: allowed
    ("127.0.0.1", True), ("10.1.2.3", True), ("192.168.1.20", True), ("172.16.0.9", True),
    ("169.254.169.254", True), ("100.64.0.1", True), ("0.0.0.0", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("93.184.216.34", False),
])
def test_blocked_ip_ranges(ip, blocked):
    import ipaddress
    assert fi._blocked_ip(ipaddress.ip_address(ip)) is blocked


def test_literal_fake_ip_url_is_refused():
    with pytest.raises(ValueError):
        fi._public_http_url("http://198.18.0.1/feed")


OLD_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Old momentum note</title><link>https://example.com/old</link><pubDate>Wed, 29 Dec 2021 10:00:00 GMT</pubDate>
<description>stocks momentum reversal factor signal equity volume close cross-section anomaly stocks momentum reversal factor signal equity volume close cross-section anomaly stocks momentum reversal factor signal equity volume close cross-section anomaly stocks momentum reversal factor signal equity volume close cross-section anomaly stocks momentum reversal factor signal equity volume close cross-section anomaly stocks momentum reversal factor signal equity volume close cross-section anomaly stocks momentum reversal factor signal equity volume close cross-section anomaly stocks momentum reversal factor signal equity volume close cross-section anomaly stocks momentum reversal factor signal equity volume close cross-section anomaly stocks momentum reversal factor signal equity volume close cross-section anomaly</description></item>
</channel></rss>"""


def test_first_fetch_archives_posts_older_than_window(data_dir, monkeypatch):
    """0.47.0: a feed's first fetch returns its whole history; only the last 30 days go to the model."""
    monkeypatch.setattr(fi, "_fetch", lambda url, timeout=20.0: OLD_RSS)
    fi.fetch_all(data_dir, now=datetime(2026, 11, 2, tzinfo=UTC))
    items = fi.load_items(data_dir)
    old = [v for v in items.values() if v["title"] == "Old momentum note"]
    assert old and all(v["stage"] == "archived" and v["reason"] == "超过 30 天" for v in old)
    assert fi.overview(data_dir)["funnel"]["fetched"] == 0  # 30-day funnel counts by publish date


def test_stale_queued_items_are_archived_before_any_model_call(data_dir, monkeypatch):
    _seed(data_dir, [{"id": "s1", "title": "t", "body": "x", "stage": "queued", "published": "2021-12-29T00:00:00+00:00"}])
    called = []
    monkeypatch.setattr(fi, "_call", lambda *a, **k: called.append(1) or "{}")
    fi.process_queue(data_dir)
    assert called == [] and fi.load_items(data_dir)["s1"]["reason"] == "超过 30 天"


# ---------------------------------------------------------------- 0.47.3: 投递不再是黑箱
def _manual(data_dir):
    return next(v for v in fi.load_items(data_dir).values() if v.get("source") == "manual")


def test_manual_submission_jumps_the_queue(data_dir, monkeypatch):
    """投递优先: 当天只剩一次调用时, 先处理投递而不是更早的 RSS 文章。"""
    fi.save_settings(data_dir, daily_limit=1)
    body = "过去 20 日收盘位置横截面最强的前 10% 做多, 排名跌回中位数卖出"
    _seed(data_dir, [{"id": "rss1", "title": "rss", "body": "x", "stage": "to_extract"}])
    fi.submit(data_dir, text=body, title="我的想法")
    seen = []

    def fake_call(dd, task, prompt, *, max_tokens, counted=True):
        seen.append("manual" if "我的想法" in prompt else "rss")
        return json.dumps({"summary_zh": "x", "items": [
            {"name_zh": "位置", "quote": "过去 20 日收盘位置横截面最强的前 10%", "dsl": "close", "direction": "high"}]})

    monkeypatch.setattr(fi, "_call", fake_call)
    fi.process_queue(data_dir)
    assert seen == ["manual"]


def test_extract_retries_once_without_quota(data_dir, monkeypatch):
    """解析失败自动重试一次; 重试记在 retries 里, 不占每日 calls 额度。"""
    fi.save_settings(data_dir, daily_limit=5)
    fi.submit(data_dir, text="动量因子: 过去 20 日收益横截面排名, 最强的一批做多", title="动量")
    replies = iter(["抱歉, 这个我无法处理。", json.dumps({"summary_zh": "动量", "items": [
        {"name_zh": "动量20", "quote": "过去 20 日收益横截面排名", "dsl": "ts_delta(close, 20)", "direction": "high"}]})])
    calls = []

    def fake_call(dd, task, prompt, *, max_tokens, counted=True):
        calls.append(counted)
        fi._charge(dd, task, 10, counted=counted)
        return next(replies)

    monkeypatch.setattr(fi, "_call", fake_call)
    out = fi.process_queue(data_dir)
    it = _manual(data_dir)
    assert out["extracted"] == 1 and out["retried"] == 1
    assert calls == [True, False]
    assert it["stage"] == "to_screen" and it["attempts"] == 2
    assert fi.usage(data_dir)["today"] == 1 and fi.usage(data_dir)["retries_today"] == 1


def test_extract_failure_is_classified_and_keeps_model_text(data_dir, monkeypatch):
    """失败落到六类之一, 并留下模型原话; retry_grant 用完即清。"""
    fi.submit(data_dir, text="某篇讲横截面动量的文章正文", title="失败样例")
    monkeypatch.setattr(fi, "_call", lambda dd, task, prompt, *, max_tokens, counted=True: "我不能回答这个问题")
    fi.process_queue(data_dir)
    it = _manual(data_dir)
    assert it["stage"] == "archived"
    assert it["failure"] == "model_format" and it["reason"] == "返回格式不对"
    assert it["last_model_text"] == "我不能回答这个问题"
    assert it.get("retry_grant") is None
    assert fi.overview(data_dir)["funnel"]["failed"] == 1


def test_retry_requeues_without_daily_quota(data_dir, monkeypatch):
    """手动重试: 额度已用尽也能跑一次, 跑完清掉放行标记。"""
    fi.save_settings(data_dir, daily_limit=1)
    fi.submit(data_dir, text="我的因子: 收盘位置", title="t")
    iid = next(iter(fi.load_items(data_dir)))
    items = fi.load_items(data_dir)
    items[iid].update(stage="archived", failure="model_format", reason="返回格式不对")
    fi._save_items(data_dir, items)
    fi._charge(data_dir, "factor_extract", 10)  # 今日额度已用尽
    out = fi.retry(data_dir, iid)
    assert out["stage"] == "to_extract" and out["retry_count"] == 1
    assert fi.load_items(data_dir)[iid]["retry_grant"] is True
    called = []

    def fake_call(dd, task, prompt, *, max_tokens, counted=True):
        called.append(counted)
        return "不是 JSON"

    monkeypatch.setattr(fi, "_call", fake_call)
    fi.process_queue(data_dir)
    assert called and called[0] is True  # 额度 0 仍然执行
    assert fi.load_items(data_dir)[iid]["stage"] == "archived"


def test_overview_lists_submission_before_any_formula(data_dir):
    """「我的投递」不受「至少一条公式」过滤: 排队中的也列出来, 采集网格不重复展示。"""
    fi.submit(data_dir, text="正文内容", title="还没跑")
    page = fi.overview(data_dir)
    assert page["funnel"]["submissions"] == 1 and page["funnel"]["queued"] == 1
    assert len(page["submissions"]) == 1
    assert page["submissions"][0]["stage"] == "to_extract"
    assert page["submissions"][0]["model_text"] == ""
    assert page["cards"] == []


def test_retry_api_404_and_422(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.factor_lab import router

    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    client = TestClient(app)
    assert client.post("/api/factor-lab/intake/items/nope/retry").status_code == 404
    fi.submit(tmp_path, text="正文", title="t")
    iid = next(iter(fi.load_items(tmp_path)))
    assert client.post(f"/api/factor-lab/intake/items/{iid}/retry").status_code == 422  # 还在排队
    items = fi.load_items(tmp_path)
    items[iid].update(stage="archived", failure="model_format", reason="返回格式不对")
    fi._save_items(tmp_path, items)
    res = client.post(f"/api/factor-lab/intake/items/{iid}/retry")
    assert res.status_code == 200 and res.json()["stage"] == "to_extract"


def test_process_only_skips_feeds_and_reports_next_tick(data_dir, monkeypatch):
    """立即处理: 只跑模型队列, 不抓源; 页面能拿到下一次自动唤醒时间。"""
    fi.submit(data_dir, text="横截面动量: 过去 20 日收益排名", title="立即处理")
    fetched = []

    def no_fetch(*a, **k):
        fetched.append(1)
        raise AssertionError("process_only must not fetch feeds")

    monkeypatch.setattr(fi, "_fetch", no_fetch)
    monkeypatch.setattr(fi, "_call", lambda dd, task, prompt, *, max_tokens, counted=True: "不是 JSON")
    out = fi.process_only(data_dir, None)
    assert fetched == [] and out["model"]["extracted"] == 1
    it = _manual(data_dir)
    assert it["stage"] == "archived" and it["failure"] == "model_format"
    page = fi.overview(data_dir)
    # 手动处理只记 last_process; last_tick / next_tick 跟定时任务走, 不被手动处理往后推
    assert page["last_process"] and page["last_tick"] is None and page["next_tick"] is None
