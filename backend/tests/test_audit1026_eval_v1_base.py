"""审计 2026-10-26 M-9: v4 证据模式下周更 v1 评估不能把 v4 许可视图当旧证据写回 evaluation.json。"""
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.services import strategy_evaluation as se
from app.services import strategy_evaluation_runner as runner


def test_v1_eval_under_v4_keeps_v1_file_rows(tmp_path, monkeypatch):
    dd = tmp_path / "decision_desk"
    dd.mkdir()
    before = {
        "sources": {
            "connors_rsi2": {"source": "connors_rsi2", "n": 100, "mean": 0.01, "ci_low": -0.02},
            "phantom_4h": {"source": "phantom_4h", "n": 812, "mean": 0.05, "ci_low": 0.01},
            "breakout": {"source": "breakout", "n": 300, "mean": 0.08, "ci_low": 0.02},
            # 历史已被污染进来的视图形状行 (v4 / v3 / v2 沿用): 应被剔掉
            "phantom_4h_pine": {"v4": True, "enabled": False, "tradable": False, "forward": {"n": 7}},
            "tv_x": {"v3": True, "n": 5},
            "tv_y": {"n": 5, "carried_from": "v1"},
        },
        "trades": {"phantom_4h": [{"r": 1.0}], "breakout": [{"r": 0.5}]},
    }
    (dd / "evaluation.json").write_text(json.dumps(before), encoding="utf-8")
    # 当前模式 = v4, 其许可视图与 v1 文件完全不同
    monkeypatch.setattr(se, "evidence_mode", lambda d: "v4")
    monkeypatch.setattr(se, "_load_v4", lambda d: {"mode": "v4", "sources": {"phantom_4h": {"v4": True}}, "trades": {}})
    monkeypatch.setattr(runner, "evaluate_strategy",
                        lambda sid, svc, engine, s, e, wl: ({"source": sid, "n": 123, "mean": 0.02, "ci_low": -0.01,
                                                             "ci_high": 0.05, "elapsed_s": 0.0}, [{"r": 0.1}]))
    engine = SimpleNamespace(list_strategies=lambda: [{"id": "connors_rsi2", "execution_backend": "matrix_native"}])
    monkeypatch.setattr("app.backtest.strategy.StrategyBacktestService", lambda *a, **k: None)
    monkeypatch.setattr("app.backtest.engine.BacktestEngine", lambda *a, **k: None)
    runner.run_all(tmp_path, date(2022, 1, 13), date(2026, 10, 16), repo=object(), engine=engine, only=["connors_rsi2"])
    after = json.loads((dd / "evaluation.json").read_text(encoding="utf-8"))
    src = after["sources"]
    assert src["connors_rsi2"]["n"] == 123                                  # 本轮重评
    assert src["phantom_4h"] == before["sources"]["phantom_4h"]           # 未重评的 v1 行原样保留
    assert src["breakout"] == before["sources"]["breakout"]
    assert not {"phantom_4h_pine", "tv_x", "tv_y"} & set(src)
    assert not any(isinstance(v, dict) and v.get("v4") for v in src.values())
    assert set(after["trades"]) == {"connors_rsi2", "phantom_4h", "breakout"}
    assert Path(dd / "evaluation.json").exists()


def test_repair_script_drops_view_rows_and_restores_known_only(tmp_path, monkeypatch):
    from scripts import repair_evaluation_v1_0392 as fix
    dd = tmp_path / "decision_desk"
    dd.mkdir()
    cur = {"sources": {"connors_rsi2": {"n": 1}, "phantom_4h": {"v4": True}, "tv_err": {"error": "boom"}},
           "trades": {"connors_rsi2": [1]}}
    clean = {"sources": {"connors_rsi2": {"n": 0}, "phantom_4h": {"n": 812}, "breakout": {"n": 300},
                         "retired_rule": {"n": 9}, "tv_err": {"n": 4}},
             "trades": {"phantom_4h": [2], "breakout": [3], "retired_rule": [4], "tv_err": [5]}}
    (dd / "evaluation.json").write_text(json.dumps(cur), encoding="utf-8")
    (tmp_path / "clean.json").write_text(json.dumps(clean), encoding="utf-8")
    monkeypatch.setattr(fix, "_known_strategies", lambda d: {"connors_rsi2", "phantom_4h", "tv_err"})
    args = ["--data-dir", str(tmp_path), "--clean", str(tmp_path / "clean.json")]
    assert fix.main(args) == 0
    assert json.loads((dd / "evaluation.json").read_text()) == cur                     # dry-run 不写
    assert fix.main([*args, "--apply"]) == 0
    out = json.loads((dd / "evaluation.json").read_text())
    src = out["sources"]
    assert src["connors_rsi2"] == {"n": 1}                                            # 当前行不被旧备份覆盖
    assert src["tv_err"] == {"error": "boom"} and "tv_err" not in out["trades"]       # 报错行不配旧逐笔
    assert src["phantom_4h"]["n"] == 812 and src["phantom_4h"]["restored_from"]
    assert src["breakout"]["n"] == 300                                                # 回放来源不在策略目录也补
    assert "retired_rule" not in src                                                  # 已删除的策略不复活
    assert out["trades"] == {"connors_rsi2": [1], "phantom_4h": [2], "breakout": [3]}
    assert len(list(dd.glob("evaluation.json.pre-0392-*"))) == 1
