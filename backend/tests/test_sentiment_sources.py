"""舆情采集 (第一期): 5 个源的适配器 / 熔断 / 落盘 / 页面零外网 / 状态行 / 凭据。

夹具 = 2026-11-12 实测真实响应存档 (GDELT、维基原样; StockTwits、ApeWisdom 按真实响应只裁掉没用到的字段)。
Reddit 凭据未批, 没有真实响应可存档: 只测「未配置」路径与按官方文档形状的解析。
"""
import hashlib
import json
import socket
import urllib.error
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import intel_panel as panel
from app.services import sentiment_sources as ss

GDELT_RAW = r'''{"query_details":{"title":"nvidia","date_resolution":"hour"},"timeline":[{"series":"Article Count","data":[{"date":"20261106T000000Z","value":18,"norm":2848},{"date":"20261106T010000Z","value":18,"norm":5061},{"date":"20261106T020000Z","value":30,"norm":4226},{"date":"20261106T030000Z","value":35,"norm":4681},{"date":"20261106T040000Z","value":32,"norm":4343},{"date":"20261106T050000Z","value":34,"norm":4801},{"date":"20261106T060000Z","value":28,"norm":3446},{"date":"20261106T070000Z","value":7,"norm":1089},{"date":"20261106T080000Z","value":48,"norm":6842},{"date":"20261106T090000Z","value":50,"norm":4812},{"date":"20261106T110000Z","value":54,"norm":8630},{"date":"20261106T120000Z","value":8,"norm":3668},{"date":"20261106T130000Z","value":3,"norm":874},{"date":"20261106T140000Z","value":1,"norm":428},{"date":"20261107T000000Z","value":11,"norm":2451},{"date":"20261107T010000Z","value":9,"norm":854},{"date":"20261107T020000Z","value":4,"norm":1047},{"date":"20261107T050000Z","value":8,"norm":1806},{"date":"20261107T060000Z","value":1,"norm":3757},{"date":"20261107T070000Z","value":10,"norm":2203},{"date":"20261107T080000Z","value":0,"norm":423},{"date":"20261107T090000Z","value":2,"norm":1305},{"date":"20261107T100000Z","value":4,"norm":2525},{"date":"20261107T110000Z","value":12,"norm":2390},{"date":"20261107T160000Z","value":2,"norm":674},{"date":"20261107T170000Z","value":7,"norm":3819},{"date":"20261107T180000Z","value":7,"norm":3766},{"date":"20261107T190000Z","value":4,"norm":1025},{"date":"20261107T220000Z","value":0,"norm":1817},{"date":"20261108T000000Z","value":1,"norm":905},{"date":"20261108T090000Z","value":10,"norm":4241},{"date":"20261108T100000Z","value":3,"norm":2471},{"date":"20261108T110000Z","value":4,"norm":994},{"date":"20261108T130000Z","value":7,"norm":4266},{"date":"20261108T140000Z","value":1,"norm":1327},{"date":"20261108T150000Z","value":0,"norm":1530},{"date":"20261108T160000Z","value":1,"norm":2446},{"date":"20261108T170000Z","value":1,"norm":2232},{"date":"20261108T180000Z","value":4,"norm":2384},{"date":"20261108T190000Z","value":3,"norm":2494},{"date":"20261108T200000Z","value":5,"norm":1788},{"date":"20261108T210000Z","value":0,"norm":1142},{"date":"20261109T000000Z","value":21,"norm":1552},{"date":"20261109T010000Z","value":29,"norm":1632},{"date":"20261109T020000Z","value":36,"norm":1702},{"date":"20261109T050000Z","value":32,"norm":3025},{"date":"20261109T060000Z","value":69,"norm":4820},{"date":"20261109T070000Z","value":9,"norm":663},{"date":"20261110T000000Z","value":130,"norm":5614}]}]}'''
ST_RAW = r'''{"symbol":{"id":2925,"symbol":"NVDA","title":"NVIDIA Corp","watchlist_count":682525},"cursor":{"more":true,"since":668962499,"max":668962105},"messages":[{"id":668962499,"created_at":"2026-11-12T18:58:06Z","entities":{"sentiment":null}},{"id":668962491,"created_at":"2026-11-12T18:58:04Z","entities":{"sentiment":{"basic":"Bullish"}}},{"id":668962454,"created_at":"2026-11-12T18:57:46Z","entities":{"sentiment":null}},{"id":668962409,"created_at":"2026-11-12T18:57:29Z","entities":{"sentiment":{"basic":"Bullish"}}},{"id":668962402,"created_at":"2026-11-12T18:57:26Z","entities":{"sentiment":null}},{"id":668962398,"created_at":"2026-11-12T18:57:24Z","entities":{"sentiment":null}},{"id":668962354,"created_at":"2026-11-12T18:57:01Z","entities":{"sentiment":null}},{"id":668962350,"created_at":"2026-11-12T18:56:59Z","entities":{"sentiment":null}},{"id":668962347,"created_at":"2026-11-12T18:56:57Z","entities":{"sentiment":{"basic":"Bearish"}}},{"id":668962342,"created_at":"2026-11-12T18:56:55Z","entities":{"sentiment":{"basic":"Bullish"}}},{"id":668962318,"created_at":"2026-11-12T18:56:42Z","entities":{"sentiment":null}},{"id":668962313,"created_at":"2026-11-12T18:56:40Z","entities":{"sentiment":null}},{"id":668962308,"created_at":"2026-11-12T18:56:38Z","entities":{"sentiment":{"basic":"Bullish"}}},{"id":668962307,"created_at":"2026-11-12T18:56:36Z","entities":{"sentiment":{"basic":"Bearish"}}},{"id":668962293,"created_at":"2026-11-12T18:56:30Z","entities":{"sentiment":null}},{"id":668962265,"created_at":"2026-11-12T18:56:20Z","entities":{"sentiment":null}},{"id":668962234,"created_at":"2026-11-12T18:56:07Z","entities":{"sentiment":null}},{"id":668962209,"created_at":"2026-11-12T18:55:54Z","entities":{"sentiment":{"basic":"Bearish"}}},{"id":668962199,"created_at":"2026-11-12T18:55:49Z","entities":{"sentiment":{"basic":"Bullish"}}},{"id":668962190,"created_at":"2026-11-12T18:55:46Z","entities":{"sentiment":null}},{"id":668962185,"created_at":"2026-11-12T18:55:44Z","entities":{"sentiment":{"basic":"Bullish"}}},{"id":668962167,"created_at":"2026-11-12T18:55:40Z","entities":{"sentiment":{"basic":"Bullish"}}},{"id":668962160,"created_at":"2026-11-12T18:55:37Z","entities":{"sentiment":null}},{"id":668962149,"created_at":"2026-11-12T18:55:33Z","entities":{"sentiment":null}},{"id":668962136,"created_at":"2026-11-12T18:55:27Z","entities":{"sentiment":{"basic":"Bullish"}}},{"id":668962134,"created_at":"2026-11-12T18:55:27Z","entities":{"sentiment":null}},{"id":668962122,"created_at":"2026-11-12T18:55:23Z","entities":{"sentiment":{"basic":"Bullish"}}},{"id":668962118,"created_at":"2026-11-12T18:55:21Z","entities":{"sentiment":null}},{"id":668962112,"created_at":"2026-11-12T18:55:19Z","entities":{"sentiment":{"basic":"Bullish"}}},{"id":668962105,"created_at":"2026-11-12T18:55:17Z","entities":{"sentiment":null}}]}'''
APE_RAW = r'''{"count":853,"pages":9,"current_page":1,"results":[{"rank":1,"ticker":"NVDA","name":"NVIDIA","mentions":3269,"upvotes":21490,"rank_24h_ago":1,"mentions_24h_ago":1009},{"rank":2,"ticker":"AMD","name":"AMD","mentions":750,"upvotes":3265,"rank_24h_ago":4,"mentions_24h_ago":171},{"rank":3,"ticker":"NBIS","name":"Nebius Group","mentions":493,"upvotes":1081,"rank_24h_ago":6,"mentions_24h_ago":175},{"rank":4,"ticker":"SPY","name":"SPDR S&amp;P 500 ETF Trust","mentions":371,"upvotes":1177,"rank_24h_ago":3,"mentions_24h_ago":246},{"rank":5,"ticker":"QQQ","name":"Invesco QQQ ETF","mentions":175,"upvotes":379,"rank_24h_ago":5,"mentions_24h_ago":183},{"rank":6,"ticker":"HR","name":"Healthcare Realty","mentions":132,"upvotes":2102,"rank_24h_ago":115,"mentions_24h_ago":2},{"rank":7,"ticker":"NYT","name":"New York Times","mentions":128,"upvotes":4801,"rank_24h_ago":59,"mentions_24h_ago":5},{"rank":8,"ticker":"CRWV","name":"CoreWeave","mentions":123,"upvotes":352,"rank_24h_ago":2,"mentions_24h_ago":479},{"rank":9,"ticker":"MU","name":"Micron Technology","mentions":116,"upvotes":232,"rank_24h_ago":7,"mentions_24h_ago":211},{"rank":10,"ticker":"AVGO","name":"Broadcom","mentions":101,"upvotes":311,"rank_24h_ago":21,"mentions_24h_ago":33},{"rank":11,"ticker":"GOOG","name":"Alphabet (Google)","mentions":85,"upvotes":414,"rank_24h_ago":11,"mentions_24h_ago":67},{"rank":12,"ticker":"GOOGL","name":"Alphabet (Google)","mentions":85,"upvotes":191,"rank_24h_ago":13,"mentions_24h_ago":51},{"rank":13,"ticker":"AAPL","name":"Apple","mentions":81,"upvotes":150,"rank_24h_ago":18,"mentions_24h_ago":36},{"rank":14,"ticker":"DTE","name":"DTE Energy","mentions":77,"upvotes":566,"rank_24h_ago":12,"mentions_24h_ago":64},{"rank":15,"ticker":"CIA","name":"Citizens Inc","mentions":76,"upvotes":2613,"rank_24h_ago":46,"mentions_24h_ago":11},{"rank":16,"ticker":"PR","name":"Permian Resources","mentions":73,"upvotes":962,"rank_24h_ago":40,"mentions_24h_ago":13},{"rank":17,"ticker":"MSFT","name":"Microsoft","mentions":69,"upvotes":188,"rank_24h_ago":8,"mentions_24h_ago":88},{"rank":18,"ticker":"ET","name":"Energy Transfer Partners","mentions":60,"upvotes":126,"rank_24h_ago":16,"mentions_24h_ago":31},{"rank":19,"ticker":"AM","name":"Antero Midstream","mentions":59,"upvotes":197,"rank_24h_ago":22,"mentions_24h_ago":22},{"rank":20,"ticker":"TSLA","name":"Tesla","mentions":58,"upvotes":94,"rank_24h_ago":19,"mentions_24h_ago":43},{"rank":21,"ticker":"SPCX","name":"SpaceX","mentions":53,"upvotes":79,"rank_24h_ago":10,"mentions_24h_ago":48},{"rank":22,"ticker":"SMCI","name":"Supermicro","mentions":52,"upvotes":138,"rank_24h_ago":32,"mentions_24h_ago":15},{"rank":23,"ticker":"IT","name":"Gartner","mentions":48,"upvotes":355,"rank_24h_ago":29,"mentions_24h_ago":22},{"rank":24,"ticker":"YOU","name":"CLEAR Secure","mentions":46,"upvotes":459,"rank_24h_ago":25,"mentions_24h_ago":22},{"rank":25,"ticker":"INTC","name":"Intel","mentions":45,"upvotes":103,"rank_24h_ago":33,"mentions_24h_ago":23}]}'''
WIKI_RAW = r'''{"items":[{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110100","access":"all-access","agent":"user","views":5539},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110200","access":"all-access","agent":"user","views":5265},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110300","access":"all-access","agent":"user","views":6492},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110400","access":"all-access","agent":"user","views":4768},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110500","access":"all-access","agent":"user","views":4819},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110600","access":"all-access","agent":"user","views":6023},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110700","access":"all-access","agent":"user","views":33238},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110800","access":"all-access","agent":"user","views":122360},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026110900","access":"all-access","agent":"user","views":733363},{"project":"en.wikipedia","article":"Jensen_Huang","granularity":"daily","timestamp":"2026111000","access":"all-access","agent":"user","views":542553}]}'''
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
NAMES = {"NVDA": {"gdelt": "nvidia", "wiki": ["Jensen_Huang"]}}


def _stub(calls, fail=()):
    def get(url, headers=None, timeout=30):
        calls.append(url)
        for key in fail:
            if key in url:
                raise urllib.error.URLError("down")
        if "gdeltproject" in url:
            return GDELT_RAW.encode()
        if "stocktwits" in url:
            return ST_RAW.encode()
        if "apewisdom" in url:
            return APE_RAW.encode()
        if "wikimedia" in url:
            return WIKI_RAW.encode()
        raise AssertionError(f"unexpected url {url}")
    return get


@pytest.fixture
def offline(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(ss, "_http_get", _stub(calls))
    monkeypatch.setattr(ss, "reddit_credentials", lambda: ("", ""))
    return calls


def test_parse_real_archives():
    hours = ss.parse_gdelt(GDELT_RAW.encode())
    assert hours["2026-11-09T06"] == 69 and hours["2026-11-08T21"] == 0 and len(hours) == 49
    day = json.loads(GDELT_RAW)
    day["query_details"]["date_resolution"] = "day"
    with pytest.raises(ValueError):                       # 跨度 > 7 天退化成日粒度: 拒收, 不当小时用
        ss.parse_gdelt(json.dumps(day).encode())
    with pytest.raises(ValueError):                       # 限速时返回的是纯文本
        ss.parse_gdelt(b"Please limit requests to one every 5 seconds")
    st = ss.parse_stocktwits(ST_RAW.encode())
    msgs = json.loads(ST_RAW)["messages"]
    assert st["n"] == 30 and st["rate_h"] > 0
    assert st["bull"] == sum(1 for m in msgs if (m["entities"]["sentiment"] or {}).get("basic") == "Bullish")
    assert st["watchers"] == json.loads(ST_RAW)["symbol"]["watchlist_count"]
    ape, pages = ss.parse_apewisdom(APE_RAW.encode())
    assert pages == json.loads(APE_RAW)["pages"] and ape["NVDA"]["mentions"] == json.loads(APE_RAW)["results"][0]["mentions"]
    wiki = ss.parse_wiki(WIKI_RAW.encode())
    assert wiki["2026-11-07"] == 33238 and wiki["2026-11-08"] == 122360 and len(wiki) == 10


def test_reddit_parse_documented_shape():
    body = {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": {"created_utc": 1762635600.0}},
                                                    {"kind": "t3", "data": {"created_utc": 1762636000.0}}]}}
    assert ss.parse_reddit(json.dumps(body).encode()) == {"2025-11-08T21": 2}


def test_collect_round_writes_sentiment_dir_and_reddit_unconfigured(tmp_path, offline):
    st = ss.collect_round(tmp_path, ["NVDA"], NAMES, NOW, sleep=lambda s: None)
    assert {k: st[k]["status"] for k in ss.SOURCES} == {
        "gdelt": "ok", "stocktwits": "ok", "apewisdom": "ok", "reddit": "unconfigured", "wiki": "ok"}
    assert not any("reddit" in u for u in offline)       # 未配置不发请求
    g = ss.read_json(tmp_path / "sentiment" / "series" / "gdelt" / "NVDA.json")
    assert g["hours"]["2026-11-09T06"] == 69 and g["from"] == "2026-11-05T12" and g["to"] == "2026-11-09T12"
    assert ss.read_json(tmp_path / "sentiment" / "series" / "wiki" / "NVDA.json")["articles"]["Jensen_Huang"]["2026-11-08"] == 122360
    snap = ss.read_snaps(tmp_path, "stocktwits", NOW, NOW)
    assert snap[0]["rows"]["NVDA"]["n"] == 30
    ape = ss.read_snaps(tmp_path, "apewisdom", NOW, NOW)[0]
    assert ape["rows"]["NVDA"]["mentions"] > 0 and ape["pages"] == 5
    assert ss.read_json(tmp_path / "sentiment" / "state.json")["_round"]["symbols"] == 1


def test_breaker_per_source_records_missing_and_never_raises(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(ss, "_http_get", _stub(calls, fail=("gdeltproject",)))
    monkeypatch.setattr(ss, "reddit_credentials", lambda: ("", ""))
    for i in range(3):
        st = ss.collect_round(tmp_path, ["NVDA"], NAMES, NOW.replace(minute=i), sleep=lambda s: None)
        assert st["stocktwits"]["status"] == "ok"           # 一个源坏了不拖其他源
    assert st["gdelt"]["status"] == "breaker" and st["gdelt"]["fails"] == 3 and "URLError" in st["gdelt"]["error"]
    calls.clear()
    st = ss.collect_round(tmp_path, ["NVDA"], NAMES, NOW.replace(minute=10), sleep=lambda s: None)
    assert not any("gdeltproject" in u for u in calls)     # 熔断期内不再请求
    calls.clear()
    st = ss.collect_round(tmp_path, ["NVDA"], NAMES, NOW.replace(minute=45), sleep=lambda s: None)
    assert any("gdeltproject" in u for u in calls)         # 30 分钟后半开重试


def test_first_failure_is_missing_not_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_http_get", _stub([], fail=("apewisdom",)))
    monkeypatch.setattr(ss, "reddit_credentials", lambda: ("", ""))
    st = ss.collect_round(tmp_path, ["NVDA"], NAMES, NOW, sleep=lambda s: None)
    assert st["apewisdom"]["status"] == "missing" and st["apewisdom"]["fails"] == 1


def test_kick_is_single_flight(tmp_path, monkeypatch):
    assert ss._RUN_LOCK.acquire(blocking=False)
    try:
        assert ss.kick(tmp_path) is False
    finally:
        ss._RUN_LOCK.release()


def test_intel_tick_schedules_sentiment_every_5_or_15_minutes():
    assert panel.DUE["sentiment"] == (300, 900)
    assert "sentiment" not in panel.MANUAL_SOURCES          # 「立即刷新」不强制跑慢源


def _seed_board(tmp_path):
    d = tmp_path / "sentiment"
    d.mkdir(parents=True)
    board = {"as_of": "2026-11-09T12:00:00Z", "rows": [{"symbol": "NVDA", "grade": "A", "heat": 61, "direction": "bearish",
                                                        "event_type": "management", "first_at": "2026-11-08T20:51:22Z"}],
             "counts": {"A": 1, "B": 0, "C": 2}, "symbols": 30, "families": {"news": 30}, "heat_line": 40, "window_h": 24}
    (d / "board.json").write_text(json.dumps(board))
    (d / "state.json").write_text(json.dumps({"gdelt": {"status": "ok"}, "reddit": {"status": "unconfigured"},
                                              "apewisdom": {"status": "missing", "error": "URLError: down"},
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
    assert _digest(tmp_path) == before                     # 数据目录前后 sha 不变
    assert tried == []
    s = out["sentiment"]
    assert s["rows"][0]["symbol"] == "NVDA" and s["counts"] == {"A": 1, "B": 0, "C": 2}
    assert s["sources"]["reddit"]["status"] == "unconfigured" and s["sources"]["stocktwits"]["status"] == "pending"
    assert "sentiment" not in out["errors"]


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
    assert out["contract"] == "intel-v1"


def test_system_status_row(tmp_path):
    from app.api import trading
    rows = []
    _seed_board(tmp_path)

    def add(cid, label, state, summary, source=""):
        rows.append({"id": cid, "label": label, "state": state, "summary": summary})

    def safe(fn, default=None):
        return fn()
    trading._sentiment_row(add, safe, tmp_path)
    r = rows[0]
    assert r["id"] == "sentiment" and r["label"] == "舆情采集" and r["state"] == "warn"
    assert "A 1 · B 0 · C 2" in r["summary"] and "缺源 ApeWisdom" in r["summary"] and "Reddit 未配置" in r["summary"]
    assert "11-09 07:00 ET" in r["summary"]
    rows.clear()
    trading._sentiment_row(add, safe, tmp_path / "nothing")
    assert rows[0]["state"] == "idle"


def test_reddit_credentials_packed(monkeypatch):
    from app import credentials, secrets_store
    from app.api import settings as settings_api
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    monkeypatch.setenv("REDDIT_API_KEY", "abc:xyz")
    assert credentials.reddit() == ("abc", "xyz")
    monkeypatch.setenv("REDDIT_API_KEY", "no-colon")
    assert credentials.reddit() == ("", "")
    monkeypatch.delenv("REDDIT_API_KEY")
    monkeypatch.delenv(credentials.ENV_DIR_VAR, raising=False)
    assert credentials.reddit() == ("", "")
    assert settings_api._valid_reddit("id:secret") and not settings_api._valid_reddit("id:") \
        and not settings_api._valid_reddit("a:b\nX-Evil: 1")
    assert "reddit" in settings_api._SERVICE_SECRETS


def test_clean_name_for_off_pool_symbols():
    assert ss.clean_name("NVIDIA Corporation Common Stock") == "nvidia"
    assert ss.clean_name("Rocket Lab USA, Inc.") == '"rocket lab usa"'
    assert ss.clean_name("") == ""


def _wiki_limited(ok_calls):
    n = {"wiki": 0}

    def get(url, headers=None, timeout=30):
        n["wiki"] += 1
        if n["wiki"] > ok_calls:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        return WIKI_RAW.encode()
    return get, n


def test_wiki_rate_limit_keeps_partial_progress_and_caps_per_round(tmp_path, monkeypatch):
    names = {s: {"wiki": ["A"]} for s in ("AAA", "BBB", "CCC")}
    get, n = _wiki_limited(2)
    monkeypatch.setattr(ss, "_http_get", get)
    assert ss.fetch_wiki(tmp_path, ["AAA", "BBB", "CCC"], names, NOW, sleep=lambda s: None) == 2   # 第 3 个 429: 停, 已写的保留
    assert (tmp_path / "sentiment" / "series" / "wiki" / "BBB.json").exists()
    get, n = _wiki_limited(0)
    monkeypatch.setattr(ss, "_http_get", get)
    with pytest.raises(urllib.error.HTTPError):                                                  # 首个就 429: 本源缺源
        ss.fetch_wiki(tmp_path / "x", ["AAA"], names, NOW, sleep=lambda s: None)
    get, n = _wiki_limited(99)
    monkeypatch.setattr(ss, "_http_get", get)
    many = {f"S{i}": {"wiki": ["A"]} for i in range(12)}
    assert ss.fetch_wiki(tmp_path / "y", sorted(many), many, NOW, sleep=lambda s: None) == ss.WIKI_PER_ROUND
    assert n["wiki"] == ss.WIKI_PER_ROUND


def test_gdelt_rate_limit_mid_round_keeps_written(tmp_path, monkeypatch):
    n = {"g": 0}

    def get(url, headers=None, timeout=30):
        n["g"] += 1
        if n["g"] > 1:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        return GDELT_RAW.encode()
    monkeypatch.setattr(ss, "_http_get", get)
    names = {"NVDA": {"gdelt": "nvidia"}, "MU": {"gdelt": "micron"}}
    assert ss.fetch_gdelt(tmp_path, ["NVDA", "MU"], names, NOW, sleep=lambda s: None) == 1
    assert len(list((tmp_path / "sentiment" / "series" / "gdelt").glob("*.json"))) == 1
