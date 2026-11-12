"""舆情采集: Reddit (Arctic Shift) / 维基两个适配器、重试 / 熔断 / 覆盖区间 / 落盘、页面零外网、状态行。

夹具 = 真实响应存档 (Arctic Shift wallstreetbets 2026-11-08 20:40–21:30Z 原样; 422 正文原样; 维基 Jensen_Huang 原样)。
"""
import hashlib
import json
import socket
import urllib.error
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import intel_panel as panel
from app.services import sentiment_sources as ss

ARCTIC_RAW = r'''{"data":[{"created_utc":1794173200,"id":"1xr9m3v","selftext":"Since October:\n\n\\-Elections pump then massive dump  \n\\-political “narrative shift” where Dems will apparently hinder AI progress into oblivion   \n\\-fear of 2027 massive capex spending of hyper scalers \n\nNow this Jensen affair news. This seems oddly orchestrated but without any real substance. The bears obv feeding into it and propagating FUD. Personally, I feel this is a massive overreaction. Anyone else agree with my line of thinking? ","title":"Strange timing of Jensen affair news"},{"created_utc":1794172910,"id":"1xr9i29","selftext":"https://www.axios.com/2026/11/08/nvidia-ceo-jensen-huang-affair-email-to-company","title":"Jensen’s official apology letter"},{"created_utc":1794172555,"id":"1xr9d3s","selftext":"[removed]","title":"[ Removed by moderator ]"},{"created_utc":1794172547,"id":"1xr9cz7","selftext":"[removed]","title":"[ Removed by moderator ]"},{"created_utc":1794172433,"id":"1xr9bch","selftext":"[removed]","title":"[ Removed by moderator ]"},{"created_utc":1794172013,"id":"1xr95oe","selftext":"[removed]","title":"[ Removed by moderator ]"},{"created_utc":1794171785,"id":"1xr92hs","selftext":"[removed]","title":"[ Removed by moderator ]"},{"created_utc":1794171638,"id":"1xr90c7","selftext":"This post contains content not supported on old Reddit. [Click here to view the full post](https://sh.reddit.com/r/wallstreetbets/comments/1xr90c7)","title":"What Are Your Moves Tomorrow, November 9, 2026"},{"created_utc":1794171522,"id":"1xr8yo9","selftext":"[removed]","title":"[ Removed by moderator ]"},{"created_utc":1794171312,"id":"1xr8vl8","selftext":"[removed]","title":"[ Removed by moderator ]"},{"created_utc":1794171276,"id":"1xr8v3o","selftext":"Nvidia’s Board to Investigate Reports That Jensen Huang Was Spying Target - The Wall Street Journal","title":"Nvidia’s Board to Investigate Reports That Jensen Huang Was Spying Target — The Wall Street Journal"},{"created_utc":1794171190,"id":"1xr8tvu","selftext":"","title":"We can't even have fun anymore"},{"created_utc":1794171022,"id":"1xr8rie","selftext":"","title":"Statement from NVIDIA Board of Directors"},{"created_utc":1794170829,"id":"1xr8ovg","selftext":"[Based on this review of Anthropic's S1](https://leonlins.com/writing/anthropic_ipo), looks like the bull vs bear case is:\n\n1. How long do you extrapolate high, unprecedented revenue growth rates and customer retention rates? Do open models and current usage restriction trends become a large enough headwind to dent the bull case?\n2. Do you wait for more of their planned capex spend to hit financials to better understand the impact? Will wall street care about this, or is it already factored in to expectations? The company is already soft guiding towards much greater cash burn, will this actually be a surprise?\n3. Their channel fee take rates seem to be getting worse, even as they get more revenue from channel partners (hyperscalers). Is this an issue?\n\nSeems expensive at IPO valuation but what could change your mind?","title":"Anthropic's IPO: Can unprecedented revenue growth outrun 'irresponsibly large' investment?"},{"created_utc":1794170423,"id":"1xr8j8j","selftext":"","title":"Nvidia board opens foreign-espionage probe after Jensen Huang admits affair to staff"}]}'''
ARCTIC_422 = r'''{"data":null,"error":"Timeout. Maybe slow down a bit"}'''
WIKI_RAW = r'''{"items":[{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110100","access":"all-access","agent":"user","views":5539},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110200","access":"all-access","agent":"user","views":5265},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110300","access":"all-access","agent":"user","views":6492},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110400","access":"all-access","agent":"user","views":4768},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110500","access":"all-access","agent":"user","views":4819},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110600","access":"all-access","agent":"user","views":6023},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110700","access":"all-access","agent":"user","views":33238},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110800","access":"all-access","agent":"user","views":122360},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110900","access":"all-access","agent":"user","views":733363},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026111000","access":"all-access","agent":"user","views":542553}]}'''
EMPTY = b'{"data":[]}'
NOW = datetime(2026, 11, 8, 22, 0, tzinfo=UTC)
NAMES = {"NVDA": {"aliases": ["nvidia", "jensen"], "wiki": ["Jensen_Huang"]}}


def _http_422(url):
    return urllib.error.HTTPError(url, 422, "Unprocessable", None, None)


def test_parse_real_archive_and_422_body():
    rows = ss.parse_arctic(ARCTIC_RAW.encode())
    assert len(rows) == 15 and rows[0]["created_utc"] > rows[-1]["created_utc"]
    assert rows[0]["title"] == "Strange timing of Jensen affair news" and len(rows[0]["selftext"]) <= ss.REDDIT_TEXT_MAX
    with pytest.raises(ValueError):                       # 「Timeout. Maybe slow down a bit」: data=null
        ss.parse_arctic(ARCTIC_422.encode())


def test_mentions_use_news_rules_and_aliases():
    rows = ss.parse_arctic(ARCTIC_RAW.encode())
    hit = [r["title"] for r in rows if ss.mentions(r["title"], "NVDA", ["nvidia", "jensen"])]
    assert len(hit) == 5 and "Statement from NVIDIA Board of Directors" in hit
    assert not any(ss.mentions(r["title"], "META", ["meta", "facebook"]) for r in rows)
    assert ss.mentions("bought more $MU calls", "MU", ["micron"]) and not ss.mentions("much music", "MU", ["micron"])
    assert ss.mentions("Meta earnings", "META", ["meta"]) and not ss.mentions("meta analysis", "META", ["meta"])
    assert "jensen" in ss.prefilter_words("NVDA", ["nvidia", "jensen"])


def test_first_round_writes_posts_and_coverage(tmp_path, monkeypatch):
    calls = []

    def get(url, headers=None, timeout=60):
        calls.append(url)
        return ARCTIC_RAW.encode() if len(calls) == 1 else EMPTY
    monkeypatch.setattr(ss, "_http_get", get)
    added = ss.fetch_reddit_sub(tmp_path, "wallstreetbets", NOW, sleep=lambda s: None)
    assert added == 15
    day = tmp_path / "sentiment" / "reddit" / "wallstreetbets" / "2026-11-08.jsonl"
    assert len(day.read_text().splitlines()) == 15
    cov = ss.read_coverage(tmp_path, "wallstreetbets")
    assert cov["to"] == ss.iso(NOW)
    assert cov["from"] == ss.iso(NOW - timedelta(days=ss.REDDIT_KEEP_D))   # 回补拿到空页 = 已到底
    assert "after=2026-11-08T16" in calls[0]                               # 首轮先拉近 6 小时
    calls.clear()
    assert ss.fetch_reddit_sub(tmp_path, "wallstreetbets", NOW + timedelta(minutes=15), sleep=lambda s: None) == 0
    assert len(day.read_text().splitlines()) == 15                          # 同 id 去重


def test_disconnected_sweep_restarts_coverage(tmp_path, monkeypatch):
    ss.write_json(ss.reddit_dir(tmp_path, "stocks") / "coverage.json",
                  {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"})
    n = {"k": 0}

    def get(url, headers=None, timeout=60):
        if "after=2026-09" in url:                       # 回补这一轮失败: 覆盖起点停在这次最旧一帖
            raise _http_422(url)
        n["k"] += 1
        page = n["k"] - 1
        base = int(NOW.timestamp()) - page * 6000
        return json.dumps({"data": [{"id": f"{page}-{i}", "created_utc": base - i * 60, "title": "x"}
                                    for i in range(100)]}).encode()
    monkeypatch.setattr(ss, "_http_get", get)
    ss.fetch_reddit_sub(tmp_path, "stocks", NOW, sleep=lambda s: None)
    cov = ss.read_coverage(tmp_path, "stocks")
    assert n["k"] == ss.REDDIT_NEW_PAGES
    assert cov["to"] == ss.iso(NOW) and cov["from"] > "2026-11-01"           # 满页没接上: 从这次最旧一帖重算


def test_retry_on_422_then_ok(tmp_path, monkeypatch):
    n = {"k": 0}
    slept = []

    def get(url, headers=None, timeout=60):
        n["k"] += 1
        if n["k"] <= 2:
            raise _http_422(url)
        return ARCTIC_RAW.encode() if n["k"] == 3 else EMPTY
    monkeypatch.setattr(ss, "_http_get", get)
    assert ss.fetch_reddit_sub(tmp_path, "wallstreetbets", NOW, sleep=slept.append) == 15
    assert slept[:2] == list(ss.REDDIT_RETRY_S)


def test_round_marks_failure_then_breaker_and_never_raises(tmp_path, monkeypatch):
    calls = []

    def get(url, headers=None, timeout=60):
        calls.append(url)
        if "arctic-shift" in url:
            raise _http_422(url)
        return WIKI_RAW.encode()
    monkeypatch.setattr(ss, "_http_get", get)
    for i in range(3):
        st = ss.collect_round(tmp_path, ["NVDA"], NAMES, NOW + timedelta(hours=7 * i), sleep=lambda s: None)
        assert st["wiki"]["status"] == "ok"                 # 一个源坏了不拖其他源
    assert st["reddit"]["status"] == "breaker" and st["reddit"]["fails"] == 3 and "422" in st["reddit"]["error"]
    t3 = NOW + timedelta(hours=14)
    calls.clear()
    ss.collect_round(tmp_path, ["NVDA"], NAMES, t3 + timedelta(minutes=10), sleep=lambda s: None)
    assert not any("arctic-shift" in u for u in calls)      # 熔断期内不再请求
    ss.collect_round(tmp_path, ["NVDA"], NAMES, t3 + timedelta(minutes=45), sleep=lambda s: None)
    assert any("arctic-shift" in u for u in calls)          # 30 分钟后半开重试


def test_one_sub_failing_is_not_source_failure(tmp_path, monkeypatch):
    def get(url, headers=None, timeout=60):
        if "subreddit=stocks" in url:
            raise _http_422(url)
        return ARCTIC_RAW.encode() if "before" not in url else EMPTY
    monkeypatch.setattr(ss, "_http_get", get)
    assert ss.fetch_reddit(tmp_path, NOW, sleep=lambda s: None) == 15
    assert ss.reddit_span(tmp_path) is None                 # 有一个版块没覆盖: 评分时整类不计


def test_wiki_parse_and_rate_limit_keeps_partial(tmp_path, monkeypatch):
    wiki = ss.parse_wiki(WIKI_RAW.encode())
    assert wiki["2026-11-07"] == 33238 and wiki["2026-11-08"] == 122360 and len(wiki) == 10
    n = {"k": 0, "ok": 2}

    def get(url, headers=None, timeout=60):
        n["k"] += 1
        if n["k"] > n["ok"]:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        return WIKI_RAW.encode()
    monkeypatch.setattr(ss, "_http_get", get)
    names = {s: {"wiki": ["A"]} for s in ("AAA", "BBB", "CCC")}
    assert ss.fetch_wiki(tmp_path, ["AAA", "BBB", "CCC"], names, NOW, sleep=lambda s: None) == 2
    n.update(k=0, ok=0)
    with pytest.raises(urllib.error.HTTPError):             # 首个就 429: 本源失败
        ss.fetch_wiki(tmp_path / "x", ["AAA"], names, NOW, sleep=lambda s: None)
    n.update(k=0, ok=999)
    many = {f"S{i}": {"wiki": ["A"]} for i in range(12)}
    assert ss.fetch_wiki(tmp_path / "y", sorted(many), many, NOW, sleep=lambda s: None) == ss.WIKI_PER_ROUND
    assert n["k"] == ss.WIKI_PER_ROUND


def test_kick_is_single_flight(tmp_path):
    assert ss._RUN_LOCK.acquire(blocking=False)
    try:
        assert ss.kick(tmp_path) is False
    finally:
        ss._RUN_LOCK.release()


def test_intel_tick_schedules_sentiment_every_5_or_15_minutes():
    assert panel.DUE["sentiment"] == (300, 900)
    assert "sentiment" not in panel.MANUAL_SOURCES


def _seed_board(tmp_path):
    d = tmp_path / "sentiment"
    d.mkdir(parents=True)
    board = {"as_of": "2026-11-09T12:00:00Z", "rows": [{"symbol": "NVDA", "grade": "A", "heat": 61, "direction": "bearish",
                                                        "event_type": "management", "first_at": "2026-11-08T20:51:22Z"}],
             "counts": {"A": 1, "B": 0, "C": 2}, "symbols": 30}
    (d / "board.json").write_text(json.dumps(board))
    (d / "state.json").write_text(json.dumps({"wiki": {"status": "ok"},
                                              "reddit": {"status": "missing", "error": "HTTPError: 422"},
                                              "_round": {"at": "2026-11-09T12:00:00Z", "symbols": 30}}))


def _digest(root):
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        h.update(str(f).encode() + (f.read_bytes() if f.is_file() else b"/"))
    return h.hexdigest()


def test_page_build_reads_board_offline_and_read_only(tmp_path, monkeypatch):
    _seed_board(tmp_path)
    tried = []

    def no_net(*a, **k):
        tried.append(a)
        raise AssertionError("page build must not touch network")
    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(socket, "create_connection", no_net)
    before = _digest(tmp_path)
    loop = SimpleNamespace(ledger=SimpleNamespace(open_positions=[]))
    out = panel.build(tmp_path, repo=None, loop=loop, now=datetime(2026, 11, 9, 7, 5, tzinfo=UTC))
    assert _digest(tmp_path) == before and tried == []
    s = out["sentiment"]
    assert s["rows"][0]["symbol"] == "NVDA" and s["total"] == 1
    assert s["sources"] == {"reddit": "missing", "wiki": "ok"} and "sentiment" not in out["errors"]


def test_page_build_without_sentiment_dir_creates_nothing(tmp_path):
    before = _digest(tmp_path)
    out = panel.build(tmp_path, repo=None, loop=None, now=datetime(2026, 11, 9, 7, 5, tzinfo=UTC))
    assert _digest(tmp_path) == before and not (tmp_path / "sentiment").exists()
    assert out["sentiment"]["rows"] == [] and out["sentiment"]["as_of"] is None


def test_corrupt_board_only_blanks_its_section(tmp_path):
    _seed_board(tmp_path)
    (tmp_path / "sentiment" / "board.json").write_text("{broken")
    out = panel.build(tmp_path, repo=None, loop=None, now=datetime(2026, 11, 9, 7, 5, tzinfo=UTC))
    assert out["sentiment"] is None and out["errors"]["sentiment"] == "JSONDecodeError"


def test_system_status_row_plain_words(tmp_path):
    from app.api import trading
    rows = []
    _seed_board(tmp_path)

    def add(cid, label, state, summary, source=""):
        rows.append({"id": cid, "label": label, "state": state, "summary": summary})
    trading._sentiment_row(add, lambda fn, default=None: fn(), tmp_path)
    r = rows[0]
    assert r["id"] == "sentiment" and r["state"] == "warn"
    assert r["summary"] == "重点 1 只 · 异动 0 只；最近一轮 11-09 07:00 ET；Reddit 暂时取不到"
    rows.clear()
    trading._sentiment_row(add, lambda fn, default=None: fn(), tmp_path / "nothing")
    assert rows[0]["state"] == "idle"


def test_off_pool_symbols_get_aliases_from_name():
    class Repo:
        def get_name_map(self, syms):
            return {"HOOD": "Robinhood Markets, Inc. Class A Common Stock"}
    names = ss.query_names(["NVDA", "HOOD"], ss.load_config(), Repo())
    assert names["NVDA"]["aliases"] == ["nvidia", "jensen"] and "robinhood" in names["HOOD"]["aliases"]
