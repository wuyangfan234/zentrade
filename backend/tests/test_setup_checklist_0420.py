"""0.42.0 统一凭据与配置清单: 取值顺序、无本机写死路径、清单不泄露密钥、新闻翻译提示。"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ENV_NAMES = ("ZENTRADE_ALPACA_KEY", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "FINNHUB_API_KEY",
             "SEC_USER_AGENT", "MASSIVE_API_KEY", "POLYGON_API_KEY", "ZENTRADE_ENV_DIR", "AI_API_KEY")


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for n in ENV_NAMES:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))   # 旧实现会读 ~/.legacy/secrets; 新实现不应读 HOME
    for k, v in (("ai_api_key", ""), ("ai_provider", "openai_compat"), ("ai_model", ""), ("ai_base_url", "")):
        monkeypatch.setattr(settings, k, v)              # 其他测试可能改过全局 settings
    return tmp_path


def _secrets(tmp_path: Path, **kv):
    p = tmp_path / "user_data" / "secrets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(kv))


def test_order_ui_then_env_then_env_dir(store, monkeypatch):
    from app import credentials as c
    d = store / "envdir"
    d.mkdir()
    (d / "finnhub.env").write_text("# c\nFINNHUB_API_KEY='from-dir'\n")
    assert c.lookup(c.FINNHUB) == ("", "")            # 没设 ZENTRADE_ENV_DIR: 不读任何文件
    monkeypatch.setenv("ZENTRADE_ENV_DIR", str(d))
    assert c.lookup(c.FINNHUB) == ("from-dir", "env_dir")
    monkeypatch.setenv("FINNHUB_API_KEY", "from-env")
    assert c.lookup(c.FINNHUB) == ("from-env", "env")
    _secrets(store, finnhub_api_key="from-ui")
    assert c.lookup(c.FINNHUB) == ("from-ui", "ui")


def test_home_legacy_dir_is_never_read(store):
    from app import credentials as c
    legacy = store / "home" / ".legacy" / "secrets"
    legacy.mkdir(parents=True)
    (legacy / "alpaca.env").write_text("ALPACA_API_KEY_ID=x\nALPACA_API_SECRET_KEY=y\n")
    (legacy / "finnhub.env").write_text("FINNHUB_API_KEY=z\n")
    assert c.alpaca() == ("", "", "") and c.finnhub() is None


def test_alpaca_ui_key_reaches_every_consumer(store, monkeypatch):
    """设置页只填 KEY_ID:SECRET 时, 交易日历 / 新闻 / 研究数据也要拿得到 (0.41 前只有行情插件认)。"""
    _secrets(store, alpaca_api_key="KID:SEC")
    from app.market_clock import calendar
    from app.plugins.alpaca import provider
    from app.services import news_ingest, research_data
    assert provider._load_credentials() == ("KID", "SEC")
    assert calendar._secrets()[:2] == ("KID", "SEC")
    assert news_ingest.alpaca_credentials() == ("KID", "SEC")
    assert research_data._secret("ALPACA_API_KEY_ID") == "KID"
    assert research_data._secret("ALPACA_API_SECRET_KEY") == "SEC"


def test_alpaca_env_pair_still_works(store, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "a")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "b")
    from app import credentials as c
    assert c.alpaca() == ("a", "b", "env")


def test_sec_user_agent_has_no_personal_default(store, monkeypatch):
    from app import credentials as c
    from app.services import edgar_fundamentals as ef
    from app.services import research_data
    assert c.sec_user_agent() == c.DEFAULT_SEC_USER_AGENT
    assert "@" not in ef.user_agent() and "@" not in research_data._sec_ua()
    monkeypatch.setenv("SEC_USER_AGENT", "ZenTrade me@example.com")
    assert ef.user_agent() == "ZenTrade me@example.com"


def test_no_hardcoded_personal_identifiers_in_app():
    root = Path(__file__).resolve().parents[1] / "app"
    bad = []
    for p in root.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for needle in ("Path.home() / \".legacy", ".legacy/secrets", "@gmail.com", "grokbot.com", "runninghub"):
            if needle in text:
                bad.append(f"{p.relative_to(root)}: {needle}")
    assert bad == []


@pytest.fixture
def client(store, monkeypatch):
    from app.api import settings as settings_api
    from app.services import preferences
    prefs: dict = {}
    monkeypatch.setattr(preferences, "load", lambda: dict(prefs))
    monkeypatch.setattr(preferences, "save", lambda d: prefs.update(d))
    app = FastAPI()
    app.include_router(settings_api.router)
    return TestClient(app), prefs


def test_checklist_fresh_install_flags_missing(client):
    c, _ = client
    body = c.get("/api/settings/setup-checklist").json()
    ids = {i["id"]: i for i in body["items"]}
    assert {"alpaca", "finnhub", "sec_ua", "ai", "ai_news", "tickflow", "qveris", "notify"} <= set(ids)
    assert body["summary"]["ready"] is False and "alpaca" in body["summary"]["required_missing"]
    assert ids["alpaca"]["level"] == "required" and not ids["alpaca"]["configured"]
    assert "ai" in body["summary"]["recommended_missing"]
    warn = ids["ai_news"]["warn"]
    assert "低价" in warn and "关思考" in warn and "token" in warn
    for i in body["items"]:
        assert i["impact"] and i["purpose"]


def test_checklist_never_returns_secret_values(client, store, monkeypatch):
    fake = {"alpaca": "fake-id:fake-sec", "finnhub": "fake-fh", "ai": "fake-ai"}   # 测试假值
    _secrets(store, alpaca_api_key=fake["alpaca"], finnhub_api_key=fake["finnhub"], ai_api_key=fake["ai"])
    c, _ = client
    raw = c.get("/api/settings/setup-checklist").text
    for s in ("fake-id", "fake-sec", "fake-fh", "fake-ai"):
        assert s not in raw
    body = json.loads(raw)
    ids = {i["id"]: i for i in body["items"]}
    assert ids["alpaca"]["configured"] and ids["alpaca"]["source"] == "设置页"
    assert ids["finnhub"]["configured"] and body["summary"]["ready"] is True


def test_service_secret_put_and_clear(client, store, monkeypatch):
    from app import credentials
    from app.api import settings as settings_api
    c, _ = client
    assert c.put("/api/settings/service-secret/sec_user_agent", json={"value": "no-email"}).status_code == 400
    assert c.put("/api/settings/service-secret/sec_user_agent", json={"value": "Zen a@b.c\nX-Evil: 1"}).status_code == 400
    r = c.put("/api/settings/service-secret/sec_user_agent", json={"value": "ZenTrade me@example.com"}).json()
    assert r["configured"] and r["source"] == "ui"
    # Finnhub 先探后存: 探测失败不写入, 不会覆盖 .env 里有效的 Key
    monkeypatch.setenv("FINNHUB_API_KEY", "env-good")
    monkeypatch.setattr(settings_api, "_probe_finnhub", lambda k: (False, "Finnhub 拒绝该 Key (HTTP 401)"))
    assert c.put("/api/settings/service-secret/finnhub", json={"value": "bad"}).status_code == 400
    assert credentials.finnhub() == "env-good"
    monkeypatch.setattr(settings_api, "_probe_finnhub", lambda k: (True, "ok"))
    r = c.put("/api/settings/service-secret/finnhub", json={"value": "fake-ui-key"}).json()
    assert r["configured"] and r["source"] == "ui" and "fake-ui-key" not in json.dumps(r)
    assert credentials.finnhub() == "fake-ui-key"
    # 清掉页面值: 返回实际仍生效的 .env 值来源, 不谎报未配置
    r = c.put("/api/settings/service-secret/finnhub", json={"value": ""}).json()
    assert r["configured"] is True and r["source"] == "env" and credentials.finnhub() == "env-good"
    assert c.put("/api/settings/service-secret/unknown", json={"value": "x"}).status_code == 404


def test_ai_needs_key_url_and_model(client, store):
    c, _ = client
    _secrets(store, ai_api_key="fake-ai")                       # 只有 Key: 不算已配置
    ids = {i["id"]: i for i in c.get("/api/settings/setup-checklist").json()["items"]}
    assert ids["ai"]["configured"] is False and ids["ai_news"]["configured"] is False
    _secrets(store, ai_api_key="fake-ai", ai_base_url="https://api.example.com/v1", ai_model="m-lite")
    ids = {i["id"]: i for i in c.get("/api/settings/setup-checklist").json()["items"]}
    assert ids["ai"]["configured"] is True and ids["ai_news"]["configured"] is True


def test_codex_provider_does_not_spawn_subprocess(client, store, monkeypatch):
    from app.services import ai_provider
    _secrets(store, ai_provider="codex_cli")
    monkeypatch.setattr(ai_provider, "codex_cli_available", lambda: (_ for _ in ()).throw(AssertionError("spawned")))
    assert c_get(client)["summary"] is not None


def c_get(client):
    return client[0].get("/api/settings/setup-checklist").json()


def test_research_secret_unknown_name_is_none(store):
    from app.services import research_data
    assert research_data._secret("NOPE_API_KEY") is None


def test_banner_dismiss(client):
    c, prefs = client
    assert c.get("/api/settings/setup-checklist").json()["dismissed"] is False
    c.post("/api/settings/setup-checklist/banner", json={"dismissed": True})
    assert prefs["setup_banner_dismissed"] is True
    assert c.get("/api/settings/setup-checklist").json()["dismissed"] is True


def test_every_where_link_targets_an_existing_anchor(client):
    """0.42.2: 「去配置」必须带 highlight=<锚点>, 且锚点在前端真实存在 (0.42.0 的 #hash 与无锚点链接点了不动)。"""
    import re
    from urllib.parse import parse_qs, urlsplit
    front = Path(__file__).resolve().parents[2] / "frontend" / "src"
    src = "\n".join(p.read_text(encoding="utf-8") for p in front.rglob("*.tsx") if ".test." not in p.name)
    tabs = set(re.findall(r"key: '([a-z-]+)',\s+label:", (front / "pages" / "Settings.tsx").read_text(encoding="utf-8")))
    items = c_get(client)["items"]
    for i in items:
        if not i["where"]:
            continue
        u = urlsplit("/" + i["where"])
        assert not u.fragment, i["where"]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        assert q.get("tab") in tabs, i["where"]
        anchor = q.get("highlight")
        assert anchor, f"{i['id']} 缺 highlight"
        if anchor.startswith("setup-"):
            assert anchor == f"setup-{i['id']}" or anchor == "setup-checklist"
        elif anchor.startswith("ai-task-"):
            assert "id={`ai-task-${row.task}`}" in src
        else:
            assert f'anchor="{anchor}"' in src, f"{i['id']}: 前端没有锚点 {anchor}"
        if "source" in q:
            assert (Path(__file__).resolve().parents[1] / "app" / "plugins" / q["source"] / "plugin.yaml").exists()


def test_alpaca_key_masked_covers_env_pair_and_hides_secret(store, monkeypatch):
    """0.42.3: Alpaca 卡片的「已配置」脱敏串, .env 两个变量也要显示; 只露 KEY_ID 的首尾, SECRET 不出后端。"""
    from app.data_providers.custom import loader
    assert loader._plugin_key_masked("alpaca", "ZENTRADE_ALPACA_KEY") == ""
    monkeypatch.setenv("ALPACA_API_KEY_ID", "PKFAKEKEYID0001")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "fake-secret-value-zzzz")
    m = loader._plugin_key_masked("alpaca", "ZENTRADE_ALPACA_KEY")
    assert m.startswith("PKFA") and m.endswith("0001") and "zzzz" not in m
    _secrets(store, alpaca_api_key="PKUIKEYID9999:ui-secret-yyyy")
    m = loader._plugin_key_masked("alpaca", "ZENTRADE_ALPACA_KEY")
    assert m.startswith("PKUI") and "yyyy" not in m


def test_tickflow_register_link_uses_referral(client):
    """TickFlow 申请入口 = 注册页: 配置清单与数据源详情 (plugin.yaml homepage) 同一链接。"""
    import yaml

    from app.services import setup_checklist
    ref = "https://tickflow.org/auth/register?ref=LE2A6H94VW"
    assert ref == setup_checklist.TICKFLOW_REGISTER_URL
    c, _ = client
    ids = {i["id"]: i for i in c.get("/api/settings/setup-checklist").json()["items"]}
    assert ids["tickflow"]["link"] == ref
    manifest = Path(__file__).resolve().parents[1] / "app" / "plugins" / "tickflow" / "plugin.yaml"
    assert yaml.safe_load(manifest.read_text(encoding="utf-8"))["homepage"] == ref
