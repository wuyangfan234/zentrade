"""Integration failure paths around durable notifications."""
from datetime import date
from types import SimpleNamespace
import sqlite3
from app.ledger.core import Account
from app.services.trading_loop import TradingLoop
from app.services import notify_opportunity as notify


def test_enqueue_failure_does_not_advance_repair_watermark(tmp_path, monkeypatch):
    loop=TradingLoop(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),Account())
    loop.boot()
    loop.ledger.append('fill',{'symbol':'TEST','side':'buy','qty':1,'price':100,'fee':1,'card_id':'c1','trade_date':'2026-09-25'},at='2026-09-25T10:00:00-04:00')
    attempts=[]
    def enqueue(*args):
        attempts.append(args[1]['seq'])
        return len(attempts)>1
    monkeypatch.setattr(notify,'enqueue_trade_event',enqueue)
    loop._enqueue_events();loop._enqueue_events()
    assert attempts==[1,1]
    assert loop._notify_seq==1


def test_missing_benchmark_notice_is_not_false_bear_market(tmp_path,monkeypatch):
    monkeypatch.setattr(notify.preferences,'get_feishu_webhook_url',lambda:'')
    result=SimpleNamespace(as_of=date(2026,9,25),gate='unavailable',cards=[],watch=[],scanned=1,params_revision=2)
    notify.enqueue_daily_opportunities(tmp_path,result)
    db=sqlite3.connect(tmp_path/'notifications/outbox.sqlite3')
    body=db.execute('select payload from outbox').fetchone()[0];db.close()
    assert '基准数据不足' in body
    assert 'SPY<MA200' not in body


def _loop(tmp_path):
    loop = TradingLoop(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), Account())
    loop.boot()
    return loop


def test_partial_exits_reach_the_outbox_not_only_fills_and_closes(tmp_path, monkeypatch):
    """docs/architecture.md: 开/平仓即时推。目标区/财报减半的部分退出此前从未入队。"""
    loop = _loop(tmp_path)
    loop.ledger.append('fill', {'symbol': 'TEST', 'side': 'buy', 'qty': 10, 'price': 100, 'fee': 1, 'card_id': 'c1',
                                'trade_date': '2026-09-24'}, at='2026-09-24T10:00:00-04:00')
    loop.ledger.append('partial_close', {'symbol': 'TEST', 'qty': 5, 'price': 110, 'fee': 1, 'card_id': 'c1',
                                         'reason': 'earnings_reduce', 'trade_date': '2026-09-25'},
                       at='2026-09-25T15:35:00-04:00')
    seen = []
    monkeypatch.setattr(notify, 'enqueue_trade_event', lambda d, e: seen.append(e['kind']) or True)
    loop._enqueue_events()
    assert seen == ['fill', 'partial_close']


def test_missed_earnings_reduction_is_pushed_once(tmp_path, monkeypatch):
    """财报减半窗口错过 = 持仓裸奔过财报, 属于必须让人知道的风险事件。"""
    monkeypatch.setattr(notify.feishu_app, 'is_configured', lambda: False)
    monkeypatch.setattr(notify.preferences, 'get_feishu_webhook_url', lambda: '')
    loop = _loop(tmp_path)
    loop.ledger.append('risk_note', {'symbol': 'TEST', 'reason': 'earnings_reduce_missed', 'earnings_date': '2026-09-26',
                                     'earnings_timing': 'bmo', 'detail': '财报前减仓窗口内未取得新鲜报价，未能减仓',
                                     'trade_date': '2026-09-25'}, at='2026-09-25T16:00:00-04:00')
    loop._enqueue_events(); loop._enqueue_events()
    db = sqlite3.connect(tmp_path / 'notifications/outbox.sqlite3')
    rows = db.execute('select key, payload from outbox').fetchall(); db.close()
    assert len(rows) == 1 and rows[0][0].startswith('risk:')
    assert '未能减仓' in rows[0][1] and 'TEST' in rows[0][1]


def test_corporate_action_booking_is_pushed(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.feishu_app, 'is_configured', lambda: False)
    monkeypatch.setattr(notify.preferences, 'get_feishu_webhook_url', lambda: '')
    loop = _loop(tmp_path)
    loop.ledger.append('corp_action', {'symbol': 'TEST', 'action_id': 'a1', 'action_type': 'forward_split',
                                       'factor': 2.0, 'ex_date': '2026-09-25', 'trade_date': '2026-09-25'},
                       at='2026-09-25T09:30:00-04:00')
    loop._enqueue_events()
    db = sqlite3.connect(tmp_path / 'notifications/outbox.sqlite3')
    rows = db.execute('select key, payload from outbox').fetchall(); db.close()
    assert len(rows) == 1 and rows[0][0] == 'corp:a1'
    assert '拆股' in rows[0][1]


def test_daily_push_carries_the_paper_daily_report(tmp_path, monkeypatch):
    """docs/architecture.md: 日报 + 管理动作并入日报不单独推。当日净值/持仓/止损上移随每日推送一起发。"""
    monkeypatch.setattr(notify.feishu_app, 'is_configured', lambda: False)
    monkeypatch.setattr(notify.preferences, 'get_feishu_webhook_url', lambda: '')
    result = SimpleNamespace(as_of=date(2026, 9, 30), gate='green', cards=[], watch=[], scanned=10, params_revision=2)
    daily = {'nav': {'nav': 100659.47, 'cash': 46264.07, 'market_value': 54395.4},
             'positions': [{'symbol': 'SSL', 'qty': 1590, 'avg_cost': 13.77, 'stop_price': 13.22}],
             'actions': [{'kind': 'stop_move', 'symbol': 'SSL', 'stop_price': 13.5, 'reason': 'trailing_1.2R'}],
             'prev_nav': 100000.0}
    notify.enqueue_daily_opportunities(tmp_path, result, daily=daily)
    db = sqlite3.connect(tmp_path / 'notifications/outbox.sqlite3')
    body = db.execute('select payload from outbox').fetchone()[0]; db.close()
    assert '净值 $100,659.47' in body and '+0.66%' in body
    assert 'SSL' in body and '止损' in body and '13.50' in body


def test_daily_push_splits_pool_and_watchlist_and_names_sources(tmp_path, monkeypatch):
    """需求 2026-10-02: 每日机会推送按 全股票池 / 自选股 分段, 每张卡写明来源与权重。"""
    from app.opportunity.card import CardState, OpportunityCard
    monkeypatch.setattr(notify.feishu_app, 'is_configured', lambda: False)
    monkeypatch.setattr(notify.preferences, 'get_feishu_webhook_url', lambda: '')

    def card(sym, tab, sources, tier='proven'):
        return OpportunityCard(f'c-{sym}', sym, 'strategy:good', date(2026, 10, 1), (100., 101.), 95., (112., 114.),
                               risk_per_share=5.5, reward_risk=2.1, state=CardState.OPEN,
                               evidence={'decision': {'tab': tab, 'sources': sources, 'tier': tier, 'score': 0.31,
                                                      'risk_pct': 0.01 if tier == 'proven' else 0.0025}})
    result = SimpleNamespace(as_of=date(2026, 10, 1), gate='green', scanned=2000, params_revision=3,
                             cards=[card('AAA', 'pool', ['good', 'trend_breakout']), card('NVDA', 'watchlist', ['good'], 'explore')],
                             watch=[])
    notify.enqueue_daily_opportunities(tmp_path, result)
    db = sqlite3.connect(tmp_path / 'notifications/outbox.sqlite3')
    body = db.execute('select payload from outbox').fetchone()[0]; db.close()
    assert body.index('全股票池') < body.index('AAA') < body.index('自选股') < body.index('NVDA')
    assert 'good+trend_breakout' in body and '探索单' in body
