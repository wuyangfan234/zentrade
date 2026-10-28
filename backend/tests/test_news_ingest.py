"""新闻采集: 三源拉取、跨源聚簇去重、相关性过滤、按月分片落盘。"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services import news_ingest as ni

NOW = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)


def test_alpaca_news_follows_pagination_and_tags_symbols():
    calls = []

    def http(url, headers=None):
        calls.append(url)
        if "page_token" not in url:
            return {"news": [{"id": 1, "headline": "Lumentum rips higher", "summary": "s", "url": "u1",
                              "created_at": "2026-10-02T10:00:00Z", "symbols": ["LITE"]}],
                    "next_page_token": "p2"}
        return {"news": [{"id": 2, "headline": "Older item", "summary": "", "url": "u2",
                          "created_at": "2026-10-01T10:00:00Z", "symbols": ["lite", "coh"]}]}

    rows = ni.fetch_alpaca_news(["LITE"], NOW - timedelta(days=2), NOW, key="k", secret="s", http_get=http)
    assert len(rows) == 2 and len(calls) == 2
    assert rows[1]["symbols"] == ["LITE", "COH"]  # 大小写归一


def test_finnhub_news_parses_unix_datetime_and_related_field():
    def http(url):
        return [{"id": 9, "headline": "h", "summary": "s", "url": "u", "datetime": 1759410000, "related": "LITE,COH"}]
    rows = ni.fetch_finnhub_news("LITE", "2026-09-30", "2026-10-02", token="t", http_get=http)
    assert rows[0]["symbols"] == ["LITE", "COH"]
    assert rows[0]["created_at"].startswith("2025")  # 真实 unix 时间戳转换 (不依赖测试当下日期)


def test_sec_filings_only_recent_8k_and_6k_with_items_and_link():
    cik = 1045810
    body = {"filings": {"recent": {
        "form": ["4", "8-K", "10-Q", "6-K"],
        "acceptanceDateTime": ["2026-09-01T00:00:00.000Z", "2026-10-01T16:00:00.000Z",
                               "2026-08-01T00:00:00.000Z", "2026-10-02T08:00:00.000Z"],
        "accessionNumber": ["0001-26-0001", "0001-26-0002", "0001-26-0003", "0001-26-0004"],
        "primaryDocument": ["a.htm", "b.htm", "c.htm", "d.htm"],
        "items": ["", "2.02,9.01", "", "8.01"],
    }}}

    def http(url, headers=None):
        return body
    rows = ni.fetch_sec_filings("NVDA", cik, since=NOW - timedelta(days=5), http_get=http)
    assert {r["source_id"] for r in rows} == {f"{cik}:000126000{n}" for n in (2, 4)}
    assert all(r["symbols"] == ["NVDA"] and r["url"].startswith("https://www.sec.gov/") for r in rows)
    assert any("2.02" in r["headline"] for r in rows)


def test_sec_filings_excludes_items_before_since():
    body = {"filings": {"recent": {
        "form": ["8-K"], "acceptanceDateTime": ["2026-01-01T00:00:00.000Z"],
        "accessionNumber": ["0001-26-0009"], "primaryDocument": ["x.htm"], "items": [""],
    }}}
    rows = ni.fetch_sec_filings("NVDA", 1, since=NOW - timedelta(days=5), http_get=lambda u, headers=None: body)
    assert rows == []


def test_cluster_dedupe_merges_similar_headlines_within_24h_keeps_earliest():
    items = [
        {"source": "alpaca", "source_id": "1", "headline": "Lumentum, Coherent Rip Higher As Washington Targets China",
         "summary": "s1", "url": "u1", "created_at": "2026-10-02T18:00:00Z", "symbols": ["LITE", "COHR"]},
        {"source": "finnhub", "source_id": "2", "headline": "Lumentum Coherent Rip Higher As Washington Targets China Chips",
         "summary": "", "url": "u2", "created_at": "2026-10-02T17:30:00Z", "symbols": ["LITE"]},
        {"source": "sec", "source_id": "3", "headline": "Totally unrelated 8-K filing about dividends",
         "summary": "", "url": "u3", "created_at": "2026-10-02T19:00:00Z", "symbols": ["NVDA"]},
    ]
    clusters = ni.cluster_dedupe(items)
    assert len(clusters) == 2
    merged = next(c for c in clusters if "LITE" in c["symbols"])
    assert merged["created_at"] == "2026-10-02T17:30:00Z"  # 最早时间点保留
    assert sorted(merged["sources"]) == ["alpaca", "finnhub"]
    assert sorted(merged["symbols"]) == ["COHR", "LITE"]
    assert merged["scope"] == "focused"
    unrelated = next(c for c in clusters if "NVDA" in c["symbols"])
    assert unrelated["scope"] == "focused"


def test_cluster_dedupe_does_not_merge_across_24h_window():
    items = [
        {"source": "alpaca", "source_id": "1", "headline": "Lumentum Coherent Rip Higher As Washington Targets China",
         "summary": "", "url": "u1", "created_at": "2026-10-01T00:00:00Z", "symbols": ["LITE"]},
        {"source": "finnhub", "source_id": "2", "headline": "Lumentum Coherent Rip Higher As Washington Targets China",
         "summary": "", "url": "u2", "created_at": "2026-10-02T01:00:00Z", "symbols": ["LITE"]},
    ]
    assert len(ni.cluster_dedupe(items)) == 2


def test_broad_news_with_many_symbols_is_scoped_broad():
    items = [{"source": "alpaca", "source_id": "1", "headline": "10 Information Technology Stocks Whale Activity",
              "summary": "", "url": "u", "created_at": "2026-10-02T00:00:00Z",
              "symbols": ["AAOI", "APLD", "CIFR", "INTC", "MRVL", "MU"]}]
    clusters = ni.cluster_dedupe(items)
    assert clusters[0]["scope"] == "broad"


def test_merge_into_shard_preserves_existing_translation_fields(tmp_path):
    first = [{"id": "alpaca:1", "headline": "h", "summary": "s", "created_at": "2026-10-02T10:00:00Z",
              "symbols": ["LITE"], "sources": ["alpaca"], "refs": [], "scope": "focused"}]
    ni.merge_into_shard(tmp_path, first)
    shard = ni.load_shard(tmp_path, NOW)
    shard["alpaca:1"]["zh_headline"] = "翻译后的中文标题"
    shard["alpaca:1"]["event_type"] = "regulatory"
    ni.save_shard(tmp_path, NOW, shard)

    # 第二次采集同一事件 (新多了一个来源), 翻译字段不能被覆盖/丢失
    second = [{"id": "alpaca:1", "headline": "h", "summary": "s", "created_at": "2026-10-02T10:00:00Z",
               "symbols": ["LITE", "COHR"], "sources": ["finnhub"], "refs": [], "scope": "focused"}]
    added = ni.merge_into_shard(tmp_path, second)
    assert added == 0
    reread = ni.load_shard(tmp_path, NOW)
    assert reread["alpaca:1"]["zh_headline"] == "翻译后的中文标题"
    assert sorted(reread["alpaca:1"]["sources"]) == ["alpaca", "finnhub"]
    assert sorted(reread["alpaca:1"]["symbols"]) == ["COHR", "LITE"]


def test_ingest_filters_out_news_not_about_requested_symbols(tmp_path, monkeypatch):
    """Alpaca 批量拉取可能带回未请求的联带股票 (如大盘综述提到的其他票), 不落盘不属于请求集的簇。"""
    monkeypatch.setattr(ni, "alpaca_credentials", lambda: ("k", "s"))   # 不读取宿主机环境里的真实凭据
    def alpaca(url, headers=None):
        return {"news": [{"id": 1, "headline": "Unrelated big index roundup piece", "summary": "", "url": "u",
                          "created_at": "2026-10-02T10:00:00Z", "symbols": ["SPY", "QQQ"]}]}
    result = ni.ingest(tmp_path, ["LITE"], now=NOW, alpaca_get=alpaca, finnhub_get=lambda *a, **k: [],
                       sec_get=lambda *a, **k: {}, sec_cik_cache={})
    assert result["raw"] == 1 and result["added"] == 0
    assert ni.load_shard(tmp_path, NOW) == {}


def test_ingest_is_fail_soft_per_source_and_reports_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(ni, "alpaca_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(ni, "finnhub_token", lambda: "t")

    def alpaca_down(url, headers=None):
        raise RuntimeError("alpaca 500")

    def finnhub(url):
        return [{"id": 5, "headline": "Lumentum Leads As Technology And Semiconductors Drive Order", "summary": "",
                "url": "u", "datetime": 1759410000, "related": "LITE"}]
    result = ni.ingest(tmp_path, ["LITE"], now=NOW, alpaca_get=alpaca_down, finnhub_get=finnhub,
                       sec_get=lambda *a, **k: {}, sec_cik_cache={})
    assert any("alpaca" in e for e in result["errors"])
    assert result["added"] == 1  # finnhub 仍然成功写入


def test_ingest_without_any_credentials_reports_errors_and_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ni, "alpaca_credentials", lambda: None)
    monkeypatch.setattr(ni, "finnhub_token", lambda: None)
    result = ni.ingest(tmp_path, ["LITE"], now=NOW, sec_get=lambda *a, **k: {}, sec_cik_cache={})
    assert result["added"] == 0
    assert any("no credentials" in e for e in result["errors"])
    assert any("no token" in e for e in result["errors"])


def test_sec_cik_map_parses_official_payload():
    body = {"0": {"cik_str": 1045810, "ticker": "nvda", "title": "NVIDIA CORP"},
            "1": {"cik_str": 1, "ticker": None, "title": "x"}}
    m = ni.sec_cik_map(http_get=lambda u, headers=None: body)
    assert m == {"NVDA": 1045810}


def test_merge_matches_existing_event_by_source_ref_when_cluster_id_changes(tmp_path):
    """第二轮采集带来更早的同簇记录 → 新簇 id 变了, 但来源引用已存在 → 并入旧事件, 不重复, 翻译保留。"""
    first = [{"id": "finnhub:2", "headline": "h", "summary": "", "created_at": "2026-10-02T18:00:00Z",
              "symbols": ["LITE"], "sources": ["finnhub"],
              "refs": [{"source": "finnhub", "source_id": "2", "url": "u2"}], "scope": "focused"}]
    ni.merge_into_shard(tmp_path, first)
    shard = ni.load_shard(tmp_path, NOW)
    shard["finnhub:2"]["zh_headline"] = "已翻译"
    ni.save_shard(tmp_path, NOW, shard)

    second = [{"id": "alpaca:1", "headline": "h", "summary": "", "created_at": "2026-10-02T17:30:00Z",
               "symbols": ["LITE", "COHR"], "sources": ["alpaca", "finnhub"],
               "refs": [{"source": "alpaca", "source_id": "1", "url": "u1"},
                        {"source": "finnhub", "source_id": "2", "url": "u2"}], "scope": "focused"}]
    assert ni.merge_into_shard(tmp_path, second) == 0
    reread = ni.load_shard(tmp_path, NOW)
    assert list(reread) == ["finnhub:2"]
    row = reread["finnhub:2"]
    assert row["zh_headline"] == "已翻译"
    assert sorted(row["sources"]) == ["alpaca", "finnhub"]
    assert sorted(r["source_id"] for r in row["refs"]) == ["1", "2"]


def test_cik_map_cache_is_reused_within_ttl_and_kept_on_refresh_failure(tmp_path):
    calls = []

    def http(url, headers=None):
        calls.append(url)
        return {"0": {"cik_str": 1045810, "ticker": "NVDA"}}
    assert ni.load_cik_map(tmp_path, now=NOW, http_get=http) == {"NVDA": 1045810}
    assert ni.load_cik_map(tmp_path, now=NOW + timedelta(days=3), http_get=http) == {"NVDA": 1045810}
    assert len(calls) == 1  # 7 天内走缓存

    def down(url, headers=None):
        raise RuntimeError("sec 503")
    # 过期后刷新失败: 沿用旧缓存而不是返回空
    assert ni.load_cik_map(tmp_path, now=NOW + timedelta(days=8), http_get=down) == {"NVDA": 1045810}


def test_ingest_respects_source_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(ni, "alpaca_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(ni, "finnhub_token", lambda: "t")
    hit = {"alpaca": 0, "finnhub": 0, "sec": 0}

    def alpaca(url, headers=None):
        hit["alpaca"] += 1
        return {"news": []}

    def finnhub(url):
        hit["finnhub"] += 1
        return []

    def sec(url, headers=None):
        hit["sec"] += 1
        return {}
    ni.ingest(tmp_path, ["LITE"], now=NOW, sources={"alpaca", "finnhub"}, alpaca_get=alpaca, finnhub_get=finnhub,
              sec_get=sec, sec_cik_cache={"LITE": 1})
    assert hit == {"alpaca": 1, "finnhub": 1, "sec": 0}
    ni.ingest(tmp_path, ["LITE"], now=NOW, sources={"sec"}, alpaca_get=alpaca, finnhub_get=finnhub,
              sec_get=sec, sec_cik_cache={"LITE": 1})
    assert hit == {"alpaca": 1, "finnhub": 1, "sec": 1}


# ── 提及门: 用 2026-10-02 实测采集里的真实标题做样本 ──────────────────────

NAMES = {"NVDA": "NVIDIA Corporation Common Stock", "LITE": "Lumentum Holdings Inc. Common Stock",
         "MU": "Micron Technology, Inc. Common Stock", "NOW": "SERVICENOW, INC.", "MSTR": "Strategy Inc Common Stock Class A",
         "LLY": "Eli Lilly & Co.", "GOOG": "Alphabet Inc. Class C Capital Stock", "ARM": "Arm Holdings plc American Depositary Shares"}
ALIASES = {s: ni.derive_aliases(s, n) for s, n in NAMES.items()}


@pytest.mark.parametrize("headline,sym", [
    ("Lumentum, Coherent Rip Higher As Washington Targets Chinese Optical Transceivers", "LITE"),
    ("Nvidia Could Have a Monster October. Here's Why.", "NVDA"),
    ("Here's Why Investors Keep Buying Nvidia (NVDA)", "NVDA"),
    ("MU Stock Muted Despite Blowout Quarter: Analysts Weigh In", "MU"),
    ("Dow, S&P 500, Nasdaq Futures Rise As Markets Eye Jobs Report: MU, NKE, IBRX", "MU"),
    ("ServiceNow Beats On Subscription Revenue", "NOW"),
    ("Strategy Adds 5,000 Bitcoin To Treasury", "MSTR"),
    ("Eli Lilly Obesity Pill Wins FDA Nod", "LLY"),
    ("Google Unveils New TPU", "GOOG"),
])
def test_mention_gate_keeps_headlines_that_name_the_stock(headline, sym):
    assert ni.mentioned_symbols(headline, "", ALIASES, {sym}) == {sym: "headline"}


@pytest.mark.parametrize("headline,sym", [
    ("Is Carmax a Buy After Its Latest Earnings Report?", "NVDA"),            # Finnhub related=NVDA, 正文无关
    ("Uber Wants 50,000 Rivians. Here's Why Robotaxis Might Be Rivian's Biggest Opportunity.", "NVDA"),
    ("NOW Is The Time To Rotate Into Value", "NOW"),                           # 常见词代码不能被句首大写骗
    ("Our strategy for October: buy the dip", "MSTR"),                         # 泛化别名小写不算
    ("Arm yourself with these dividend picks", "ARM"),
    ("Trend vs. Hype", "NVDA"),
])
def test_mention_gate_drops_tag_only_noise(headline, sym):
    assert ni.mentioned_symbols(headline, "", ALIASES, {sym}) == {}


def test_mention_gate_records_summary_level_separately():
    m = ni.mentioned_symbols("GMI Cloud Raises $660 Million For AI Infrastructure",
                             "The company will deploy thousands of Nvidia Blackwell GPUs.", ALIASES, {"NVDA"})
    assert m == {"NVDA": "summary"}


def test_derive_aliases_strips_suffixes_and_trailing_ampersand():
    assert "lumentum" in ni.derive_aliases("LITE", "Lumentum Holdings Inc. Common Stock")
    assert "eli lilly" in ni.derive_aliases("LLY", "Eli Lilly & Co.")
    assert all(not a.endswith("&") for a in ni.derive_aliases("LLY", "Eli Lilly & Co."))


def test_ingest_drops_tag_only_clusters_and_keeps_sec_filings(tmp_path, monkeypatch):
    monkeypatch.setattr(ni, "alpaca_credentials", lambda: None)
    monkeypatch.setattr(ni, "finnhub_token", lambda: "t")

    def finnhub(url):
        return [{"id": 1, "headline": "Is Carmax a Buy After Its Latest Earnings Report?", "summary": "", "url": "u1",
                 "datetime": 1790924000, "related": "NVDA"},
                {"id": 2, "headline": "Nvidia Could Have a Monster October", "summary": "", "url": "u2",
                 "datetime": 1790925000, "related": "NVDA"}]
    sec_body = {"filings": {"recent": {"form": ["8-K"], "acceptanceDateTime": ["2026-10-02T12:00:00.000Z"],
                                       "accessionNumber": ["0001-26-0002"], "primaryDocument": ["b.htm"], "items": ["2.02"]}}}
    r = ni.ingest(tmp_path, ["NVDA"], now=NOW, names=NAMES, finnhub_get=finnhub,
                  sec_get=lambda u, headers=None: sec_body, sec_cik_cache={"NVDA": 1045810})
    assert r["kept"] == 2 and r["added"] == 2  # Carmax 噪音被丢, Nvidia 新闻 + 8-K 保留
    rows = ni.load_shard(tmp_path, NOW)
    assert {row["headline"][:6] for row in rows.values()} == {"Nvidia", "NVDA S"}
    sec = next(r for r in rows.values() if "sec" in r["sources"])
    assert sec["zh_headline"] == "NVDA 8-K 公告: 披露经营业绩" and sec["tagged_by"]["mode"] == "rule"
    assert sec["tags"]["NVDA"]["event_type"] == "earnings" and sec["tags"]["NVDA"]["importance"] == "high"
    assert all(row["mentions"] == {"NVDA": "headline"} for row in rows.values())



# ── SEC 表单编号确定性打标 + 源文本清洗 ───────────────────────────────

@pytest.mark.parametrize("form,items,zh,etype,imp,direction", [
    ("8-K", "2.02,9.01", "NVDA 8-K 公告: 披露经营业绩", "earnings", "high", "neutral"),
    ("8-K", "7.01,8.01", "NVDA 8-K 公告: Reg FD 信息披露、其他重要事项", "other", "low", "neutral"),
    ("8-K", "3.02", "NVDA 8-K 公告: 未注册发行股份 (稀释)", "dilution", "high", "bearish"),
    ("8-K", "5.02,8.01", "NVDA 8-K 公告: 董事/高管变动、其他重要事项", "management", "medium", "neutral"),
    ("8-K", "1.01,2.06", "NVDA 8-K 公告: 签订重大协议、重大资产减值", "contract", "medium", "bearish"),  # 任一不利 → bearish
    ("8-K", "9.01", "NVDA 提交 8-K 公告", "other", "low", "neutral"),                                  # 只有附件
    ("6-K", "", "NVDA 提交外国发行人临时报告 (6-K)", "other", "low", "neutral"),
])
def test_sec_rule_tag(form, items, zh, etype, imp, direction):
    title, tag = ni.sec_rule_tag("NVDA", form, items)
    assert title == zh
    assert (tag["event_type"], tag["importance"], tag["direction"], tag["relevance"]) == (etype, imp, direction, "primary")


def test_clean_text_unescapes_html_and_collapses_whitespace():
    assert ni.clean_text("Hardware, Storage &amp; Peripherals") == "Hardware, Storage & Peripherals"
    assert ni.clean_text("\n\n\nSource: TradePulse  |  Oct 1\n\nMarket") == "Source: TradePulse | Oct 1 Market"
    assert ni.clean_text(None) == ""



def test_sec_filings_never_cluster_together_even_with_template_titles():
    """生产首轮实测: 'X SEC 8-K filing (items ...)' 模板标题让 12 家公司的公告被并成一条 (SSL 被挂上 HRL 公告)。"""
    items = [{"source": "sec", "source_id": f"{i}:acc{i}", "headline": f"{s} SEC 8-K filing (items 7.01,9.01)", "summary": "",
              "url": "", "created_at": "2026-10-02T10:00:00Z", "symbols": [s]} for i, s in enumerate(["HRL", "SSL", "MU"])]
    clusters = ni.cluster_dedupe(items)
    assert len(clusters) == 3 and all(len(c["symbols"]) == 1 for c in clusters)


def test_similar_headlines_about_different_companies_do_not_merge():
    items = [
        {"source": "alpaca", "source_id": "1", "headline": "Shares Rise After Strong Quarterly Earnings Beat Estimates",
         "summary": "", "url": "", "created_at": "2026-10-02T10:00:00Z", "symbols": ["AAA"]},
        {"source": "finnhub", "source_id": "2", "headline": "Shares Rise After Strong Quarterly Earnings Beat Estimates",
         "summary": "", "url": "", "created_at": "2026-10-02T10:05:00Z", "symbols": ["BBB"]},
    ]
    assert len(ni.cluster_dedupe(items)) == 2
