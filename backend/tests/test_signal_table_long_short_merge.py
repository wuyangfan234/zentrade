"""0.43.2 回归: 自选信号表合并多空时, 同一条 TradingView 规则的看空行不能冲掉看多行。

例: 收盘上穿 ZLSMA (tv_zlsma_cross) 当天看多命中 LLY、PLTR, 同一规则的卖出信号命中 GOOGL (只观察的看空卡)。
落表时 `{**sig, **short}` 按规则名合并字典, 看空列表整条覆盖看多列表 → 信号表只剩 GOOGL 看空,
3+3 机会板少了多头卡。"""
import json
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl

from app.opportunity.engine import ScanResult
from app.services import decision_scan_job as job
from app.services import swing_desk as sd
from app.services import watchlist_v4 as wv

D0 = date(2026, 10, 29)


def _frames(symbols, n=80, up=True):
    days = [D0 - timedelta(days=n - 1 - i) for i in range(n)]
    c = np.linspace(100, 130, n) if up else np.linspace(130, 100, n)
    return {s: pl.DataFrame({"symbol": s, "date": days, "open": c, "high": c * 1.02, "low": c * 0.98, "close": c,
                             "volume": 1e6}) for s in symbols}


def _snapshot(tmp_path, *, hits, sell_hits, watch):
    frames = _frames(watch)
    out = SimpleNamespace(summary={})
    job._watchlist_v4_snapshot(tmp_path, D0, watchlist=set(watch), frames=frames, hits=hits,
                               stops={"tv_zlsma_cross": None}, stops_by_symbol={}, s1s2=ScanResult(D0), out=out,
                               sell_hits=sell_hits, h4_state={s: "long" for s in watch})
    return json.loads((wv.signals_dir(tmp_path) / f"{D0.isoformat()}.json").read_text())


def test_same_rule_long_and_short_rows_both_reach_signal_table(tmp_path):
    table = _snapshot(tmp_path,
                      hits={"tv_zlsma_cross": [{"symbol": "LLY", "rank": 1}, {"symbol": "PLTR", "rank": 2}]},
                      sell_hits={"tv_zlsma_cross": [{"symbol": "GOOGL", "rank": None}]},
                      watch=["LLY", "PLTR", "GOOGL"])
    rows = {(x["rule"], x["symbol"], x["side"]) for x in table["signals"]}
    assert ("tv_zlsma_cross", "LLY", "long") in rows
    assert ("tv_zlsma_cross", "PLTR", "long") in rows
    assert ("tv_zlsma_cross", "GOOGL", "short") in rows


def test_board_gets_long_card_when_same_rule_also_has_short_hits(tmp_path):
    """端到端: 信号表 → 机会板。LLY 4h 同向多头 + 3+3 勾了 ZLSMA 多 → 板上必须有 LLY 多头卡。"""
    table = _snapshot(tmp_path,
                      hits={"tv_zlsma_cross": [{"symbol": "LLY", "rank": 1}]},
                      sell_hits={"tv_zlsma_cross": [{"symbol": "GOOGL", "rank": None}]},
                      watch=["LLY", "GOOGL"])
    boards = sd.compose_boards(signals=table["signals"], closes={k: v for k, v in table["closes"].items() if v},
                               as_of=D0, long_rules=("tv_zlsma_cross",), short_rules=("tv_smc_structure",),
                               atr=table["atr"], h4_state=table["h4_state"], short_ok=False)
    assert [c["symbol"] for c in boards["long"]] == ["LLY"]


def test_lineage_stamp_sees_every_rule_once(tmp_path, monkeypatch):
    """谱系按规则名盖章: 同一规则既有多又有空时只算一条规则, 不因合并方式丢规则或重复。"""
    from app.services import card_lineage
    seen = {}

    def fake_stamp(data_dir, as_of, rules):
        seen["rules"] = list(rules)
        return None, None
    monkeypatch.setattr(card_lineage, "stamp", fake_stamp)
    _snapshot(tmp_path, hits={"tv_zlsma_cross": [{"symbol": "LLY", "rank": 1}]},
              sell_hits={"tv_zlsma_cross": [{"symbol": "GOOGL", "rank": None}]}, watch=["LLY", "GOOGL"])
    assert seen["rules"] == ["tv_zlsma_cross"]
