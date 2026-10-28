"""AI 报告 created_at 时区测试 — 必须是美东墙钟 (P1-2), 不随服务器时区漂移。

三类 AI 报告(财务分析 / 个股分析 / 大盘复盘)共用 JsonReportStore 补 created_at,
前端 fmtRelative 用 `new Date(created_at)` 按浏览器本地时区解析这串 naive 时间。
服务端若用宿主机时钟(Docker 镜像默认 UTC), 刚生成的报告会被显示成「8 小时前」,
「今天是否已生成过报告」的判定(created_at 前 10 位 == 浏览器今天)也会误判。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.json_report_store import JsonReportStore
from app.market_clock import us_clock

CN_TZ = us_clock.tz


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = JsonReportStore("ai_reports.json", 20, id_prefix="rpt")
    monkeypatch.setattr(s, "_path", lambda: tmp_path / "ai_reports.json")
    return s


def test_created_at_follows_market_clock(store):
    """created_at 与美东墙钟一致 (宿主机时区漂移不影响)。"""
    saved = store.save_report({"symbol": "MCD", "content": "正文"})

    got = datetime.fromisoformat(saved["created_at"])
    expected = datetime.now(CN_TZ).replace(tzinfo=None)
    assert abs((got - expected).total_seconds()) < 5


def test_created_at_keeps_naive_second_precision_format(store):
    """格式不变: 秒精度、无时区后缀, 历史记录与前端解析保持兼容。"""
    saved = store.save_report({"symbol": "MCD", "content": "正文"})

    created_at = saved["created_at"]
    assert len(created_at) == 19
    assert created_at[10] == "T"
    assert datetime.fromisoformat(created_at).tzinfo is None
