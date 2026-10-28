from app.factors.registry import factor_columns_view
from app.api.trading import router


def test_no_unavailable_financial_or_share_capital_in_default_run_catalog():
    items=factor_columns_view(available_only=True)
    assert items
    assert not any('turnover' in r['id'] or r['group']=='财务' for r in items)


def test_reconcile_endpoint_exists_and_never_calls_execution_quote(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    loop=SimpleNamespace(opp_svc=SimpleNamespace(advance_lifecycle=MagicMock(),revalidate_unfilled=MagicMock(return_value=set())),status=lambda:{'mode':'paper_only'},run_intraday=lambda:(_ for _ in ()).throw(AssertionError('no fills')))
    app=FastAPI();app.include_router(router);app.state.trading_loop=loop
    with TestClient(app) as c:
        r=c.post('/api/trading/reconcile')
    assert r.status_code==200
    assert r.json()['executed_trades']==0
    loop.opp_svc.revalidate_unfilled.assert_called_once()
