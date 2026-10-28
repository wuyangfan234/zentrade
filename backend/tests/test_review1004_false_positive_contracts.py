"""Report #2/#3 acceptance probes: assert the existing financial/data contract."""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.trader.trader import SwingTrader
from app.vendor.oasis_phantom.resample_4h_tv import _resample_4h_tv


def test_extended_us_hours_create_0130_bin_but_no_synthetic_overnight_bars():
    idx = pd.date_range('2026-10-01 04:00', '2026-10-02 19:30', freq='30min')
    idx = idx[(idx.hour >= 4) & (idx.hour < 20)]
    raw = pd.DataFrame({'open': 100., 'high': 101., 'low': 99., 'close': 100., 'volume': 10.}, index=idx)
    result = _resample_4h_tv(raw)
    assert len(raw) == 64
    assert len(result) == 10
    day = result.loc['2026-10-02']
    assert day.index.strftime('%H:%M').tolist() == ['01:30', '05:30', '09:30', '13:30', '17:30']
    assert day.loc['2026-10-02 01:30', 'volume'] == 30.  # 04:00, 04:30, 05:00
    assert day.loc['2026-10-02 17:30', 'volume'] == 50.  # 17:30 through 19:30
    assert result['volume'].sum() == raw['volume'].sum()
    window = result.loc['2026-10-01 13:30':'2026-10-02 09:30']
    assert len(window) == 5  # not six clock bins, not the report's four
    assert not (result.index.hour == 21).any()


def test_sequential_shorts_reserve_sales_proceeds_and_one_times_own_collateral(tmp_path):
    account = Account(initial_equity=100_000., risk_per_trade_pct=.10, max_portfolio_risk_pct=1.,
                      max_positions=6, slippage_bps=0., commission_per_share=0., min_commission=0.)
    ledger = Ledger(tmp_path)
    ledger.init_account(account)
    trader = SwingTrader(ledger, account)
    now = datetime(2026, 10, 5, 10, tzinfo=us_clock.tz)
    marks = {sym: 100. for sym in ['A','B','C','D','E']}
    for i, sym in enumerate(marks):
        card = OpportunityCard(id=sym, symbol=sym, side='short', state=CardState.ACTIVE,
            setup_type='strategy:test', created_date=date(2026, 10, 2), entry_zone=(99., 101.),
            invalidation=105., target_zone=(88., 90.), risk_per_share=5., reward_risk=2.)
        pre_cash, pre_exposure, pre_bp = ledger.cash, ledger.short_exposure(marks), ledger.buying_power(marks)
        trader.try_fill_entry(card, price=100., trade_date=now.date(), at=now.isoformat(), marks=marks)
        now += timedelta(seconds=15)
        fill = trader.try_fill_entry(card, price=100., trade_date=now.date(), at=now.isoformat(), marks=marks)
        now += timedelta(seconds=15)
        if i < 4:
            assert fill is not None and fill['qty'] == 250
            notional = fill['qty'] * fill['price']
            assert ledger.cash == pre_cash + notional
            assert ledger.short_exposure(marks) == pre_exposure + notional
            assert ledger.buying_power(marks) == pytest.approx(pre_bp - notional)
            assert ledger.buying_power(marks) >= 0
        else:
            assert fill is None and ledger.position(sym) is None
    assert ledger.cash == 200_000.
    assert ledger.short_exposure(marks) == 100_000.
    assert ledger.buying_power(marks) == 0.
    assert ledger.nav(marks) == 100_000.
