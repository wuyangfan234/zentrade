"""0.22.4 固化: 退役路由不再挂载; 机会板路由仍在; evo 面板只返回 retired。"""
import importlib.util

from app.api import opportunities as opp


def _paths():
    return {getattr(r, "path", "") for r in opp.router.routes}


def test_retired_modules_gone():
    assert importlib.util.find_spec("app.services.evo_cards") is None
    assert importlib.util.find_spec("app.services.largecap_short_ref") is None


def test_routes_swing_present_largecap_absent():
    p = _paths()
    assert any(x.endswith("/swing") for x in p)
    assert any(x.endswith("/page") for x in p) and any(x.endswith("/settings") for x in p)
    assert not any("largecap" in x for x in p)


def test_legacy_card_chain_retired_0291():
    """0.29.1: 旧机会卡链路退役 (模块 + 路由 + 09:44 定格任务); swing.json 只有 /settings 一个写口。"""
    import pathlib
    assert importlib.util.find_spec("app.services.watchlist_cards") is None
    assert importlib.util.find_spec("app.services.top_snapshot") is None
    p = _paths()
    for gone in ("/cards", "/top-snapshot", "/watchlist", "/watchlist/{symbol}", "/swing/strategies"):
        assert f"/api/opportunities{gone}" not in p, gone                 # /settlement/cards (回测页卡结局) 照留
    assert "/api/opportunities/settlement/cards" in p
    root = pathlib.Path(opp.__file__).resolve().parents[1]
    assert 'id="zen_top_snapshot"' not in (root / "jobs" / "daily_pipeline.py").read_text(encoding="utf-8")
    from app.services import card_volume as cv
    for fn in ("score", "confirm", "harmony", "intraday", "signal_day_ratio", "TOP_N"):
        assert not hasattr(cv, fn), fn


def test_evo_panel_retired_stub(tmp_path):
    d = opp._evo_panel(tmp_path, {"evo": True})
    assert d["retired"] is True and d["on"] is False and d["cards"] == []
