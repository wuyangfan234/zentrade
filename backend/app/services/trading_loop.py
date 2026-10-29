"""Single paper-only coordinator. Source-stamped quotes required; no broker APIs."""
from __future__ import annotations
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState
from app.services import earnings_guard, entry_guard
from app.services.circuit_breaker import CircuitBreaker
from app.services.corporate_actions import CorporateActionService
from app.services.earnings_calendar import EarningsCalendar
from app.services.opportunity_service import OpportunityService
from app.services.setup_status import SetupStatusStore
from app.trader.trader import SwingTrader

from app.services import pm_rank  # noqa: E402

logger = logging.getLogger(__name__)

WATCHLIST_EXIT = 'watchlist_removed'   # 0.33.0: 移出自选 -> 次日开盘平仓 (exit_intent / close 的 reason)

_IEX_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='iex-xcheck')
SLOW_RUN_S = 10.0  # 撮合一轮超过此秒数记日志 (调度间隔 15 秒)
MAX_QUOTE_AGE_SECONDS = 120
EXECUTION_FEEDS = ('tickflow', 'iex', 'sip')
CROSS_CHECK_MAX_BPS = 50.0      # TickFlow 与新鲜 IEX 价差超过 50 bps → 拒用该报价
CROSS_CHECK_IEX_MAX_AGE = 30.0  # 只拿 30 秒内的 IEX 成交做核对 (冷门股 IEX 成交稀疏, 旧价不能否决)


class TradingLoop:
    def __init__(self, repo, account: Account):
        self.repo, self.account = repo, account
        self.ledger = Ledger(data_dir=repo.store.data_dir / 'ledger')
        self.opp_svc = OpportunityService(repo)
        self.opp_svc.bind_ledger(self.ledger)
        self.earnings = EarningsCalendar(repo.store.data_dir)
        self.corp_actions = CorporateActionService(repo.store.data_dir)
        self.setup_status = SetupStatusStore(repo.store.data_dir)
        self.breaker = CircuitBreaker(repo.store.data_dir)
        self.breaker_status = None
        self.trader = SwingTrader(self.ledger, account)
        self._boot = False
        self._run_lock = threading.Lock()
        self.last_error = self.last_run_at = self.last_quote_at = None
        self.last_execution_symbol_count = None  # 0.7.1: 供盯盘心跳读, 免得它进台账锁重算
        self.last_quote_count = 0
        self.rejected_quotes = []
        self.entry_blockers = []
        self._notify_seq = 0
        self.book_status = None
        self.execution_source = {'active': None, 'primary': None, 'primary_error': None, 'cross_checked': 0, 'deviations': [],
                                 'timing': {}}

    def boot(self):
        with self.ledger._lock:
            if self._boot:
                return
            path = self.ledger.data_dir / 'account.json'
            if path.exists():
                self.account = Account.from_file(path)
                self.trader.account = self.account
                self.ledger.replay(self.account)
            else:
                self.ledger.init_account(self.account)
            self._boot = True
            self._sync_card_book()
        self._enqueue_events()

    def ensure_protection_feeds(self):
        """保护数据缺失/过期则刷新一次 (网络 I/O)。由调度器在启动后异步调用,
        不放在 boot(): 应用启动与部署健康检查不能等外部数据源。失败只降级为暂停新开仓。"""
        self._ensure_earnings_calendar()

    def _ensure_earnings_calendar(self):
        """保护数据缺失/过期则尝试刷新一次; 失败只降级 (暂停新开仓)。"""
        try:
            if not self.earnings.usable():
                self.earnings.refresh()
        except Exception as e:  # noqa: BLE001
            logger.warning('earnings calendar boot refresh failed: %s', type(e).__name__)
        try:
            if not self.corp_actions.usable(us_clock.now()):
                self.refresh_corporate_actions()
        except Exception as e:  # noqa: BLE001
            logger.warning('corporate actions boot refresh failed: %s', type(e).__name__)

    def refresh_corporate_actions(self):
        """拉取持仓 + 未终态卡标的的公司行动窗口, 成功后立即对持仓入账 (幂等)。"""
        now = us_clock.now()
        self.ensure_watchlist_book(now)   # 盘前 08:00 先补齐机会卡下单簿, 当日要交易的票都在覆盖范围内
        with self.ledger._lock:
            symbols = self.corporate_action_symbols()
        status = self.corp_actions.refresh(symbols, now)
        if status.get('status') == 'connected':
            self.apply_corporate_actions(now)
            self._enqueue_events()
        return status

    def _book_only(self) -> bool:
        """0.35.2: v4 (只交易自选机会卡下单簿) 下交易环只处理下单簿卡 + 有持仓的卡。
        旧决策台卡在 v4 准入里恒被 v4_book_only 拦下, 每 15 秒为 1000+ 张永不成交的卡复验日K、跑准入、取报价,
        10-22 实测一轮中位 11.7 秒 (间隔 15 秒, 盘中跳过 34 轮)。读模式失败 = 不收窄 (照旧全量, 只是慢)。"""
        try:
            from app.services import strategy_evaluation
            return strategy_evaluation.evidence_mode(self.repo.store.data_dir) == 'v4'
        except Exception:
            return False

    def _sync_card_book(self):
        self.trader._cards.clear()
        for card in self.opp_svc.loop_cards(us_clock.today(), book_only=self._book_only()):
            self.trader.register_card(card)

    def execution_symbols(self):
        book_only = self._book_only()
        cards = self.opp_svc.loop_cards(us_clock.today(), book_only=book_only)
        symbols = [c.symbol for c in cards if c.state in (CardState.OPEN, CardState.ACTIVE, CardState.WATCH, CardState.FILLED)]
        if book_only:
            try:
                from app.services import decision_scan_job
                # v4 只交易自选: 下单簿卡本来就只出自选; 移出自选的卡不再取报价 (持仓始终取)
                wl = decision_scan_job._watchlist()
                symbols = [s for s in symbols if s in wl]
            except Exception:
                pass
        out = sorted(set(symbols + [p.symbol for p in self.ledger.open_positions]))
        self.last_execution_symbols = out      # 热报价池读这份缓存 (hot_quotes.pool), 不在 5 秒轮里重放台账
        return out

    def corporate_action_symbols(self):
        """公司行动覆盖范围 = 执行池 + 自选 (0.35.2: 执行池收窄后, 自选里还没出卡的票照旧在早上覆盖, 盘中出卡不必等补拉)。"""
        out = set(self.execution_symbols())
        if self._book_only():
            try:
                from app.services import decision_scan_job
                out |= set(decision_scan_job._watchlist())
            except Exception:
                pass
        return sorted(out)

    @staticmethod
    def _timestamp(value):
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                raise ValueError('invalid_timestamp')
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=us_clock.tz)
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError('source_timestamp_needs_timezone')
        return value.astimezone(us_clock.tz)

    def _fresh_quotes(self, marks, now):
        if hasattr(marks, 'to_dicts'):
            marks = marks.to_dicts()
        if isinstance(marks, list):
            marks = {q['symbol']: q for q in marks}
        out, rejected = {}, []
        for sym, q in marks.items():
            try:
                if not isinstance(q, dict):
                    raise ValueError('unstamped_price')
                t = self._timestamp(q.get('timestamp'))
                age = (now - t).total_seconds()
                if isinstance(q['price'], bool):
                    raise ValueError('invalid_price')
                price = float(q['price'])
                if not math.isfinite(price) or price <= 0:
                    raise ValueError('invalid_price')
                if age < 0 or age > MAX_QUOTE_AGE_SECONDS or t.date() != now.date():
                    raise ValueError('stale_or_future')
                if q.get('feed') not in EXECUTION_FEEDS:
                    raise ValueError('non_execution_feed')
                if not us_clock.in_session(t):
                    raise ValueError('outside_session')
                row = {**q, 'price': price, 'timestamp': t.isoformat()}
                if q.get('open') is not None:
                    try:
                        ot = self._timestamp(q.get('open_timestamp'))
                        if ot.date() != now.date() or ot.time() != us_clock.sessions[0].open:
                            raise ValueError('not_regular_open')
                        if isinstance(q['open'], bool):
                            raise ValueError('bad_open')
                        op = float(q['open'])
                        if not math.isfinite(op) or op <= 0:
                            raise ValueError('bad_open')
                        row['open'] = op
                    except (ValueError, TypeError, OverflowError):
                        row.pop('open', None)
                out[sym] = row
            except (ValueError, TypeError, KeyError, OverflowError, OSError) as e:
                rejected.append({'symbol': sym, 'reason': str(e)})
        self.rejected_quotes = rejected
        self.last_quote_count = len(out)
        self.last_quote_at = max((q['timestamp'] for q in out.values()), default=None)
        return out

    def _iex_provider(self):
        from app.data_providers.custom import get_provider
        return get_provider('alpaca')

    def _timed_iex(self, symbols):
        t0 = time.monotonic()
        try:
            return list(self._iex_provider().get_execution_quotes(symbols))
        finally:
            self.execution_source['timing']['iex_s'] = round(time.monotonic() - t0, 2)

    def _execution_quotes(self, provider, symbols):
        """主源报价 (TickFlow) + IEX 交叉核对; 主源失败回退 IEX。返回 (报价列表, 被拒报价)。"""
        name = getattr(provider, 'name', None) or 'unknown'
        getter = getattr(provider, 'get_execution_quotes', None)
        if not callable(getter):
            raise RuntimeError('数据源未提供带源时点的纸面执行报价')
        st = self.execution_source
        st['primary'] = name
        # 交叉核对与主源并发 (10-09 实测: TickFlow 3.3-4.1s + IEX 3.0-3.2s 串行, 加复验 > 15 秒间隔)
        iex_future = None
        if name not in ('alpaca', 'iex'):
            iex_future = _IEX_POOL.submit(self._timed_iex, symbols)
        t0 = time.monotonic()
        try:
            quotes = list(getter(symbols))
            st['timing']['primary_s'] = round(time.monotonic() - t0, 2)
            st['primary_error'] = None
            feeds = {q.get('feed') for q in quotes if q.get('feed')}
            st['active'] = feeds.pop() if len(feeds) == 1 else name
        except Exception as e:  # noqa: BLE001
            st['primary_error'] = f'{type(e).__name__}: {e}'[:200]
            if name in ('alpaca', 'iex'):
                raise
            logger.warning('execution primary %s failed, falling back to IEX: %s', name, type(e).__name__)
            st['active'] = 'iex_fallback'
            self.trader.quote_feed = 'iex'
            if iex_future is not None:
                return list(iex_future.result()), []
            return list(self._iex_provider().get_execution_quotes(symbols)), []
        self.trader.quote_feed = quotes[0].get('feed') if quotes else name
        if name in ('alpaca', 'iex') or st['active'] in ('iex', 'sip') or not quotes:
            return quotes, []
        try:
            rows = iex_future.result() if iex_future is not None else self._timed_iex(symbols)
            iex_rows = {r['symbol']: r for r in rows}
        except Exception as e:  # noqa: BLE001
            logger.info('IEX cross-check unavailable: %s', type(e).__name__)
            return quotes, []
        now = us_clock.now()
        kept, rejected = [], []
        for q in quotes:
            ref = iex_rows.get(q['symbol'])
            if ref:
                try:
                    ref_age = (now - self._timestamp(ref['timestamp'])).total_seconds()
                except (ValueError, TypeError, KeyError, OverflowError, OSError):
                    ref_age = None
                if ref_age is not None and 0 <= ref_age <= CROSS_CHECK_IEX_MAX_AGE and ref.get('price'):
                    st['cross_checked'] += 1
                    bps = abs(q['price'] / ref['price'] - 1) * 1e4
                    if bps > CROSS_CHECK_MAX_BPS:
                        rej = {'symbol': q['symbol'], 'reason': f'cross_source_deviation_{bps:.0f}bps',
                               'primary': q['price'], 'iex': ref['price']}
                        rejected.append(rej)
                        st['deviations'] = [*st['deviations'][-19:], {**rej, 'at': now.isoformat()}]
                        continue
            kept.append(q)
        return kept, rejected

    def run_intraday(self, provider=None):
        if not self._boot:
            self.boot()
        now = us_clock.now()
        if not us_clock.in_session(now) or not self._run_lock.acquire(blocking=False):
            return []
        try:
            self.last_run_at = now.isoformat()
            timing = self.execution_source['timing'] = {}
            t_run = time.monotonic()
            # 0.16.1: 入场前置步骤各自隔离异常 —— 出错只停开仓, 持仓管理/待执行卖出照常
            errors: list[str] = []

            def _guard(name, fn):
                try:
                    return fn()
                except Exception as e:  # noqa: BLE001
                    errors.append(f'{name}: {type(e).__name__}: {e}')
                    logger.exception('paper intraday %s failed; new entries paused this round', name)
                    return None
            _guard('ensure_watchlist_book', lambda: self.ensure_watchlist_book(now))
            _guard('cover_new_symbols', lambda: self._cover_new_symbols(now))
            # 0.27.0: 盘前越止损检查的补查 (定时任务没跑到 / 盘中新落簿的第 1 天做多卡)。交易环这里不出网 (SIP 由
            # 09:20/09:30/09:40 定时任务取), 09:44 起只用本地 09:00 HL 价兜底, 锁被占就跳过, 不拖持仓管理。
            # 不走 _guard: 它自己不抛, 失败也不暂停其它卡开仓 (没查过的第 1 天做多卡由准入 premarket_check_pending 单独拦)
            self.premarket_void_check(now, from_trader=True)

            narrow = {'book_only': True} if self._book_only() else {}   # 非 v4 = 调用形状与 0.35.1 完全相同

            def _lifecycle():
                with self.ledger._lock:
                    self.opp_svc.advance_lifecycle(now, {}, quotes_ready=False, **narrow)
            _guard('advance_lifecycle', _lifecycle)
            entry_symbols = _guard('revalidate_unfilled', lambda: self.opp_svc.revalidate_unfilled(now, **narrow))
            if errors:
                entry_symbols = set()
            timing['revalidate_s'] = round(time.monotonic() - t_run, 2)
            with self.ledger._lock:
                symbols = self.execution_symbols()
            self.last_execution_symbol_count = len(symbols)
            if not symbols:
                self.last_error = '; '.join(errors) or None
                return []
            if provider is None:
                from app.data_providers.custom import get_provider
                from app.services.preferences import get_realtime_data_provider
                provider = get_provider(get_realtime_data_provider())
            t_q = time.monotonic()
            quotes, deviations = self._execution_quotes(provider, symbols)
            timing['quotes_s'] = round(time.monotonic() - t_q, 2)
            t_f = time.monotonic()
            result = self.intraday_fill_check(quotes, entry_symbols=entry_symbols)
            timing['fill_s'] = round(time.monotonic() - t_f, 2)
            timing['total_s'] = round(time.monotonic() - t_run, 2)
            timing['symbols'] = len(symbols)
            if timing['total_s'] > SLOW_RUN_S:
                logger.info('paper intraday slow: %s', timing)
            if deviations:
                self.rejected_quotes = [*self.rejected_quotes, *deviations]
            if errors:
                self.entry_blockers = [*(self.entry_blockers or []), {'symbol': '*', 'reason': 'entry_prestep_failed'}]
            self.last_error = '; '.join(errors) or None
            return result
        except Exception as e:
            self.last_error = f'{type(e).__name__}: {e}'
            logger.exception('paper intraday failed; no broker orders exist')
            return []
        finally:
            self._run_lock.release()

    CORP_MERGE_RETRY_S = 300

    def _cover_new_symbols(self, now):
        """执行标的里有今早公司行动缓存没覆盖的票 (盘中新进簿) → 合并补拉; 失败 5 分钟后重试, 不影响已覆盖的票。"""
        try:
            if not self.corp_actions.usable(now):
                return
            with self.ledger._lock:
                uncovered = sorted(s for s in self.execution_symbols() if not self.corp_actions.covers(s))
            if not uncovered:
                return
            last = getattr(self, '_corp_merge_at', None)
            if last and (now - last).total_seconds() < self.CORP_MERGE_RETRY_S:
                return
            self._corp_merge_at = now
            res = self.corp_actions.refresh_merge(uncovered, now)
            if res.get('merged'):
                self.apply_corporate_actions(now)
        except Exception as e:  # noqa: BLE001
            logger.warning('corporate actions merge for new symbols failed: %s', type(e).__name__)

    def premarket_void_check(self, now=None, cards=None, *, from_trader=False):
        """0.27.0 (10-19 产品决策 B): 做多机会板卡第 1 天 09:00 盘前价已越过止损 -> 整卡作废 (swing_pm_check)。
        cards=None -> 读全部卡 (定时任务); from_trader=True -> 交易环每轮用上一轮已同步的卡 (不多读一次台账)。
        出错只记日志、不抛: 还没查过的第 1 天做多卡由准入 premarket_check_pending 单独拦下, 不连坐其它卡的开仓。"""
        from app.services import swing_pm_check as pmc
        now = us_clock.market_datetime(now or us_clock.now())
        try:
            if cards is None:
                cards = list(self.trader._cards.values()) if from_trader else self.opp_svc.all_cards(now.date())
            res = pmc.run(self.repo.store.data_dir, cards, now, allow_sip=not from_trader)
            # 作废以记录文件为准 (不只看本轮新查的): 上一轮写了记录、但改台账失败的卡, 这一轮补上 (幂等)
            void = pmc.void_ids(self.repo.store.data_dir, now.date())
            live_states = (CardState.OPEN, CardState.ACTIVE, CardState.WATCH)
            if not any(c.id in void and c.state in live_states for c in cards):
                return res
            with self.ledger._lock:
                # 锁内重读: 定时补跑与交易环可能同时走到这里, 只作废仍未终结的卡, 不重复写事件
                fresh = [c for c in self.opp_svc.all_cards(now.date()) if c.id in void and c.state in live_states]
                intents = entry_guard.IntentIndex(self.ledger)
                for card in fresh:
                    entry_guard.cancel_pending(self.ledger, card, now, pmc.REASON, intents)
                    self.opp_svc._transition(card, CardState.EXPIRED, '盘前越过止损作废', now)
                if fresh:
                    self._sync_card_book()
            if fresh:
                logger.info('swing premarket check %s: checked=%d void=%s', now.date(), res.get('checked', 0),
                            [c.id for c in fresh])
            return res
        except Exception as e:
            logger.warning('swing premarket check failed (unchecked day-1 long cards stay blocked): %s', type(e).__name__)
            return {'error': type(e).__name__}

    def ensure_watchlist_book(self, now=None):
        """盘中: 用上一交易日收盘为止的已收盘日K 补齐自选机会卡下单簿 (v4 纸面试用档/前向确认档)。
        上一交易日的盘后信号表缺失时不补 (扫描失败的那天, 不能把“没数据”当“没触发”)。失败只记日志。"""
        try:
            from app.services import watchlist_book, watchlist_v4
            now = now or us_clock.now()
            today = now.date()
            prev = us_clock.previous_trading_day(today)
            data_dir = self.repo.store.data_dir
            if not (watchlist_v4.signals_dir(data_dir) / f'{prev.isoformat()}.json').exists():
                self.book_status = {'skipped': 'previous_scan_missing', 'as_of_close': prev.isoformat()}
                return {}
            added = watchlist_book.ensure(data_dir, self.repo, today=today, as_of_close=prev)
            self.book_status = {'as_of_close': prev.isoformat(), 'added': added, 'at': now.isoformat()}
            return added
        except Exception as e:  # noqa: BLE001
            logger.warning('watchlist book ensure failed: %s', type(e).__name__)
            return {}

    def intraday_fill_check(self, marks, *, entry_symbols: set[str] | None = None):
        if not self._boot:
            self.boot()
        now = us_clock.now()
        if not us_clock.in_session(now):
            return []
        quotes = self._fresh_quotes(marks, now)
        self.entry_blockers = [{'symbol':s,'reason':'regular_open_unavailable'} for s,q in quotes.items() if q.get('open') is None]
        earnings_ok = self.earnings.usable(now)
        if not earnings_ok:
            self.entry_blockers.append({'symbol': '*', 'reason': 'earnings_calendar_unavailable'})
        corp_ok = self.corp_actions.usable(now)
        if not corp_ok:
            self.entry_blockers.append({'symbol': '*', 'reason': 'corporate_actions_unavailable'})
        flagged = self._flagged_corporate_symbols(now.date()) if corp_ok else set()
        valuation_missing = entry_guard.missing_valuation(self.ledger, quotes)
        if valuation_missing:
            self.entry_blockers.append({'symbol': '*', 'reason': 'valuation_incomplete', 'missing_symbols': valuation_missing})
        try:
            nav_now, prev_close, peak = self._breaker_inputs(now, quotes)
            self.breaker_status = self.breaker.update(day=now.date(), nav_now=nav_now,
                                                      prev_close_nav=prev_close, peak_nav=peak)
        except Exception as e:  # noqa: BLE001
            # fail-closed (审计 #2, 2026-10-12): 熔断评估出错时不知道今天是否该停, 一律暂停新开仓;
            # 持仓管理与退出照常。旧实现置 None 后 breaker_block=False, 等于出错就放行。
            logger.warning('circuit breaker evaluation failed, blocking new entries: %s', type(e).__name__)
            self.breaker_status = {'error': type(e).__name__, 'block_new_entries': True,
                                   'risk_multiplier': 1.0, 'unavailable': True}
        breaker_block = bool(self.breaker_status and self.breaker_status['block_new_entries'])
        risk_mult = (self.breaker_status or {}).get('risk_multiplier', 1.0)
        if breaker_block:
            reason = 'circuit_breaker_unavailable' if self.breaker_status.get('unavailable') else 'circuit_breaker_day_halt'
            self.entry_blockers.append({'symbol': '*', 'reason': reason})
        generated = []
        with self.ledger._lock:
            generated.extend(self.apply_corporate_actions(now))
            self.opp_svc.advance_lifecycle(now, quotes, **({'book_only': True} if self._book_only() else {}))
            self.entry_blockers.extend(self.opp_svc.entry_blockers)
            self._sync_card_book()
            for pos in list(self.ledger.open_positions):
                q = quotes.get(pos.symbol)
                if q and q.get('open') is not None:
                    event = self.trader.execute_pending_exit(pos.symbol, open_price=q['open'], trade_date=now.date(), at=now.isoformat())
                    if event:
                        generated.append(event)
                # 时间止损/目标区优先于财报减半: 先管理, 仍持仓才考虑事件减半
                if q:
                    generated.extend(self.trader.manage_intraday(pos.symbol, price=q['price'], trade_date=now.date(), at=now.isoformat(), quote_ts=q['timestamp']))
                generated.extend(self._earnings_reduce(pos.symbol, q, now))
            days = self.opp_svc.list_card_days()
            gate = (self.opp_svc.load_cards(date.fromisoformat(days[-1])) or {}).get('gate') if days else 'unavailable'
            # Exit management is independent of permission to enter a new position.
            if gate in ('green', 'yellow'):
                effective = min(self.account.max_portfolio_risk_pct, 0.03) if gate == 'yellow' else self.account.max_portfolio_risk_pct
                self.trader.account = replace(self.account, max_portfolio_risk_pct=effective,
                                              risk_per_trade_pct=self.account.risk_per_trade_pct * risk_mult)
                try:
                    cooldown = self.opp_svc.cooldown_symbols(now.date())
                    sectors = self.opp_svc.sector_map()
                    # 0.31.0: 同一轮内先尝试盘前同向放量的卡 (只改尝试顺序; 每日开仓名额占满时它们先拿到)
                    for card in pm_rank.entry_order(self.trader._cards.values(), self.repo.store.data_dir, now.date()):
                        if card.state != CardState.ACTIVE or card.symbol in cooldown or (entry_symbols is not None and card.symbol not in entry_symbols):
                            continue
                        if not earnings_ok or not corp_ok or breaker_block or valuation_missing:
                            continue  # 保护数据不可用或当日熔断: 暂停一切新开仓 (持仓管理照常)
                        if card.symbol in flagged:
                            self.entry_blockers.append({'symbol': card.symbol, 'reason': 'corporate_action_pending', 'card_id': card.id})
                            continue
                        if not self.corp_actions.covers(card.symbol):
                            self.entry_blockers.append({'symbol': card.symbol, 'reason': 'corporate_actions_not_covered', 'card_id': card.id})
                            continue
                        if self.setup_status.is_degraded(card.setup_type):
                            # 只观察: 卡保持可见/可审计, 不开新仓; 持仓管理不受影响
                            self.entry_blockers.append({'symbol': card.symbol, 'reason': f'setup_degraded:{card.setup_type}', 'card_id': card.id})
                            continue
                        if earnings_guard.entry_blocked(self.earnings.event_for(card.symbol), now.date()):
                            self.entry_blockers.append({'symbol': card.symbol, 'reason': 'earnings_window', 'card_id': card.id})
                            continue
                        q = quotes.get(card.symbol)
                        if not q or q.get('open') is None:
                            continue  # ACTIVE旧卡同样必须取得当日开盘证据才能入场
                        # Earlier fills in this same batch may have added a short.
                        current_missing = entry_guard.missing_valuation(self.ledger, quotes)
                        if current_missing:
                            self.entry_blockers.append({'symbol': card.symbol, 'reason': 'valuation_incomplete', 'card_id': card.id,
                                                        'missing_symbols': current_missing})
                            entry_guard.cancel_pending(self.ledger, card, now, 'valuation_incomplete')
                            continue
                        admission = entry_guard.evaluate(card, data_dir=self.repo.store.data_dir, now=now,
                            ledger=self.ledger, cards=self.trader._cards.values(), default_risk=self.account.risk_per_trade_pct,
                            check_borrow=True, refresh_borrow=True, sectors=sectors)
                        if not admission.allowed:
                            self.entry_blockers.extend({'symbol': card.symbol, 'reason': r, 'card_id': card.id} for r in admission.blockers)
                            entry_guard.cancel_pending(self.ledger, card, now, admission.blockers[0])
                            self.opp_svc._transition(card, CardState.WATCH, '准入阻断：' + '；'.join(admission.blockers), now)
                            continue
                        self.trader.account = replace(self.account, max_portfolio_risk_pct=effective,
                                                      risk_per_trade_pct=admission.risk_pct * risk_mult,
                                                      max_positions=entry_guard.position_cap(self.account, admission, effective, self.ledger))
                        # Preserve the frozen card on disk, record actual entry tier in the intent for quota replay.
                        entry_card = replace(card, evidence={**card.evidence, 'decision': {
                            **entry_guard.decision(card),
                            'entry_tier': admission.tier or ('explore' if admission.explore else 'proven')}})
                        event = self.trader.try_fill_entry(entry_card, price=q['price'], trade_date=now.date(),
                            at=now.isoformat(), quote_ts=q['timestamp'], marks={s: row['price'] for s, row in quotes.items()})
                        if event:
                            generated.append(event)
                finally:
                    self.trader.account = self.account
        try:  # 准入漏斗: 拦截原因按 (日, 标的, 原因) 去重落盘; 只记录, 失败不影响撮合
            from app.services import entry_funnel
            entry_funnel.record_blockers(self.repo.store.data_dir, now, self.entry_blockers)
        except Exception as e:
            logger.warning('entry funnel record failed (soft): %s', type(e).__name__)
        self._enqueue_events()
        return generated

    def _breaker_inputs(self, now, quotes):
        """熔断输入: 盘中净值, 上一交易日定版净值, 历史最高定版净值。

        盘中净值必须与基准 (上一交易日官方收盘定版) 同口径: 有新鲜报价用报价; 缺报价的持仓用
        上一交易日官方收盘价 (= 基准里它的估值, 对当日涨跌贡献 0), 当日新建仓用成交成本。
        旧实现缺报价按成本估值, 10-02 开盘首轮 3/5 持仓无报价 → 抹掉昨日浮盈, 记成 -2.638% 误触
        (真实开盘 -1.275%)。前收取不到时宁可本轮不判 (返回 None, 不触发), 不用成本价凑数。
        """
        import json as _json
        if entry_guard.missing_valuation(self.ledger, quotes):
            return None, None, None  # short liabilities require current quotes for EVERY holding
        marks = {s: q['price'] for s, q in quotes.items() if q.get('price')}
        today_s = now.date().isoformat()
        for pos in self.ledger.open_positions:
            if pos.symbol in marks:
                continue
            if (pos.entry_date or today_s) >= today_s:
                marks[pos.symbol] = pos.avg_cost  # 今日新建仓: 基准里是现金, 成本即同口径
                continue
            pc = self._prev_official_close(pos.symbol, now.date())
            if pc is None:
                return None, None, None
            marks[pos.symbol] = pc
        nav_now = self.ledger.nav(marks)
        path = self.ledger.data_dir / 'nav' / 'daily.jsonl'
        rows = []
        if path.exists():
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    rows.append(_json.loads(line))
                except ValueError:
                    continue
        today = now.date().isoformat()
        prior = [r for r in rows if str(r.get('date')) < today and r.get('nav')]
        prev_close = prior[-1]['nav'] if prior else None
        peak = max([r['nav'] for r in prior] + [self.account.initial_equity]) if prior else self.account.initial_equity
        return nav_now, prev_close, peak

    def _prev_official_close(self, symbol: str, day):
        """上一交易日官方收盘价 (未复权, 与收盘定版 post_close_manage 同列); 按 (日, 股) 缓存。"""
        cache = getattr(self, '_prev_close_cache', None)
        if cache is None or cache.get('_day') != day:
            cache = self._prev_close_cache = {'_day': day}
        if symbol in cache:
            return cache[symbol]
        value = None
        try:
            prev = us_clock.previous_trading_day(day)
            frame = self.repo.get_daily(symbol, prev, prev)
            if frame is not None and not frame.is_empty() and ('date' not in frame.columns or frame['date'][-1] == prev):
                col = 'raw_close' if 'raw_close' in frame.columns else 'close'
                v = frame[col][-1]
                if v is not None and math.isfinite(float(v)) and float(v) > 0:
                    value = float(v)
        except Exception as e:  # noqa: BLE001
            logger.warning('breaker prev close %s unavailable: %s', symbol, type(e).__name__)
        if value is not None:
            cache[symbol] = value  # 失败不缓存, 下一轮重试
        return value

    def apply_corporate_actions(self, now):
        """除权日 (美东) 当天起: 持仓的拆股/反拆/现金分红自动入账 (action_id 幂等);
        未成交卡遇拆合股 → 冻结价位失效, 卡作废; 其它公司行动只标记 (见 _flagged_corporate_symbols)。

        现金分红按除权日持股数 × 每股金额在除权日入账 (纸面简化: 实际到账为 payable_date, 事件内注明)。
        """
        now = us_clock.market_datetime(now)
        today = now.date()
        generated = []
        with self.ledger._lock:
            booked = {e.get('action_id') for e in self.ledger.read_events() if e['kind'] == 'corp_action'}
            for action in self.corp_actions.actions():
                if not action.get('auto') or action.get('id') in booked:
                    continue
                ex_day = date.fromisoformat(str(action['ex_date'])[:10])
                if ex_day > today:
                    continue
                symbol = action['symbol']
                pos = self.ledger.position(symbol)
                if action['type'] in ('forward_split', 'reverse_split'):
                    self._expire_cards_for_split(symbol, ex_day, action, now)
                if pos is None or not pos.entry_date or date.fromisoformat(pos.entry_date) >= ex_day:
                    continue  # 除权日前未持有 (或除权日当天/之后才建仓, 买入价已是除权后) 不享有
                payload = {'symbol': symbol, 'action_id': action['id'], 'action_type': action['type'],
                           'ex_date': ex_day.isoformat(), 'trade_date': today.isoformat()}
                if action['type'] in ('forward_split', 'reverse_split'):
                    payload['factor'] = float(action['factor'])
                elif action['type'] == 'cash_dividend':
                    # 空头 (qty<0) 须向出借方支付分红 → 负数入账
                    payload.update(cash_dividend=round(pos.qty * float(action['rate']), 6),
                                   rate=float(action['rate']), payable_date=action.get('payable_date'),
                                   special=bool(action.get('special')))
                else:
                    continue
                generated.append(self.ledger.append('corp_action', payload, at=now.isoformat()))
        return generated

    def _expire_cards_for_split(self, symbol, ex_day, action, now):
        for card in self.opp_svc.all_cards(now.date()):
            if card.symbol == symbol and card.state in (CardState.OPEN, CardState.ACTIVE, CardState.WATCH) \
                    and card.created_date < ex_day:
                self.opp_svc._transition(card, CardState.EXPIRED, '除权/拆股后冻结价位失效', now)

    def _flagged_corporate_symbols(self, today):
        """非自动处理的公司行动 (并购/分拆/改代码等) 在窗口内涉及的标的: 禁新开仓。"""
        out = set()
        for action in self.corp_actions.actions():
            if action.get('auto'):
                continue
            try:
                ex_day = date.fromisoformat(str(action.get('ex_date'))[:10])
            except ValueError:
                continue
            if ex_day >= today - timedelta(days=10):
                out.add(action['symbol'])
        return out

    def _earnings_reduce(self, symbol, quote, now):
        """财报前减半: 窗口内用新鲜报价减半; 窗口过后仍未减仓 → 记一次'未能减仓', 不补做。"""
        pos = self.ledger.position(symbol)
        event = self.earnings.event_for(symbol) if pos else None
        if pos is None or not event or not pos.entry_date:
            return []
        entry_day = date.fromisoformat(pos.entry_date)
        if not earnings_guard.reduction_needed(event, entry_day, self.account.max_holding_days):
            return []
        history = [e for e in self.ledger.read_events() if e.get('symbol') == symbol]
        entry_seq = next((e['seq'] for e in reversed(history) if e['kind'] == 'fill' and e.get('side') in ('buy', 'short')), 0)
        this_round = [e for e in history if e['seq'] >= entry_seq]
        done = any(e.get('reason') in ('earnings_reduce', 'earnings_reduce_missed') for e in this_round)
        if done:
            return []
        if earnings_guard.in_reduce_window(event, now):
            if not quote:
                return []  # 窗口内继续等下一帧新鲜报价
            ev = self.trader.reduce_for_event(symbol, price=quote['price'], trade_date=now.date(), at=now.isoformat(),
                                              quote_ts=quote['timestamp'], reason='earnings_reduce')
            return [ev] if ev else []
        if earnings_guard.reduce_window_missed(event, now):
            return [self.ledger.append('risk_note', {
                'symbol': symbol, 'reason': 'earnings_reduce_missed', 'earnings_date': event['date'],
                'earnings_timing': event.get('timing'), 'entry_seq': entry_seq,
                'detail': '财报前减仓窗口内未取得新鲜报价，未能减仓；按规则不在之后补做',
                'trade_date': now.date().isoformat()}, at=now.isoformat())]
        return []

    def post_close_manage(self, trade_date: date | None = None, *, confirmed_official: bool = False):
        if not self._boot:
            self.boot()
        day, now = trade_date or us_clock.today(), us_clock.now()
        if day > now.date() or (day == now.date() and now.time() < us_clock.sessions[-1].close):
            return {'events': 0, 'nav': None, 'skipped': 'regular_session_not_closed'}
        if not confirmed_official:
            return {'events': 0, 'nav': None, 'skipped': 'official_close_not_confirmed'}
        with self.ledger._lock:
            cutoff = datetime.combine(day, us_clock.sessions[-1].close, tzinfo=us_clock.tz)
            state_kinds = {'fill','close','partial_close','stop_move','corp_action','adjust','entry_intent','exit_intent'}
            if any(e['kind'] in state_kinds and us_clock.market_datetime(e['at']) > cutoff for e in self.ledger.read_events()):
                # 历史收盘重建需要历史账本视图；严禁对当前仓位应用过去价格或写过去NAV。
                return {'events':0,'nav':None,'skipped':'ledger_ahead_of_close'}
            self._sync_card_book()
            marks, generated = {}, []
            # 0.16.1: 只认官方日K, 不走 get_daily 的盘中缓存覆盖 (管道定版时缓存尚未刷新)
            official = getattr(self.repo, 'get_official_daily', None) or self.repo.get_daily
            for pos in self.ledger.open_positions:
                frame = official(pos.symbol, day, day)
                if not frame.is_empty() and ('date' not in frame.columns or frame['date'][-1] == day):
                    price_col = 'raw_close' if 'raw_close' in frame.columns else 'close'
                    value = frame[price_col][-1]
                    if value is not None and math.isfinite(float(value)) and float(value) > 0:
                        marks[pos.symbol] = float(value)
            missing = sorted(p.symbol for p in self.ledger.open_positions if p.symbol not in marks)
            if missing:
                # 缺价只跳过这几只的止损判断; 净值无法按官方价定版 → 当日不写 NAV (补跑判定据此重试)
                logger.warning('post close: %s 缺官方收盘, 跳过其止损判断且当日不定版净值', missing)
            at = datetime.combine(day, us_clock.sessions[-1].close, tzinfo=us_clock.tz)
            for pos in list(self.ledger.open_positions):
                if pos.symbol in marks:
                    generated.extend(self.trader.manage_close(pos.symbol, close_price=marks[pos.symbol], trade_date=day, at=at.isoformat()))
            generated.extend(self._watchlist_exit(day, marks, at))
            self.opp_svc.advance_lifecycle(at, {})
            nav = None if missing else self.ledger.write_nav_daily(day, marks)
        self._enqueue_events()
        return {'events': len(generated), 'nav': nav, 'missing_symbols': missing}

    def _watchlist_exit(self, day: date, marks: dict, at: datetime) -> list[dict]:
        """0.33.0 (10-22 产品决策 B): 票已移出自选 -> 收盘定版时记离场意向, 下一交易日开盘全平 (与收盘破止损同一条执行路径)。
        读不到自选 / 自选为空 = 不动 (不能因名单读失败把全部持仓平掉)。当天已有离场意向 (收盘破止损) 的不重复记。
        缺官方收盘的票本次跳过, 下一次收盘定版再记。调用方持台账锁。"""
        from app.services import decision_scan_job
        watch = decision_scan_job._watchlist()
        if not watch:
            if self.ledger.open_positions:
                logger.warning('watchlist exit: 自选读不到或为空, 本次不按自选平仓')
            return []
        out = []
        for pos in list(self.ledger.open_positions):
            if pos.symbol in watch or pos.symbol not in marks:
                continue
            plan, history = self.trader._round_context(pos)
            if self.trader._pending_exit(plan, history) is not None:
                continue
            out.append(self.ledger.append('exit_intent', {
                'symbol': pos.symbol, 'card_id': plan.get('card_id'), 'entry_seq': plan['seq'],
                'reason': WATCHLIST_EXIT, 'signal_price': marks[pos.symbol], 'trade_date': day.isoformat(),
            }, at=at.isoformat()))
        if out:
            logger.info('watchlist exit %s: %s', day, [e['symbol'] for e in out])
        return out

    def _enqueue_events(self):
        try:
            from app.services import notify_opportunity
            for e in self.ledger.read_events():
                if e['seq'] <= self._notify_seq:
                    continue
                ok = True
                if e['kind'] in ('fill', 'partial_close', 'close'):
                    # 部分退出 (目标区减半 / 财报前减半) 也是成交, 与开平仓同样即时推送
                    ok = notify_opportunity.enqueue_trade_event(self.repo.store.data_dir, e)
                elif e['kind'] == 'risk_note' and e.get('reason') == 'earnings_reduce_missed':
                    ok = notify_opportunity.enqueue_risk_note(self.repo.store.data_dir, e)
                elif e['kind'] == 'corp_action':
                    ok = notify_opportunity.enqueue_corp_action(self.repo.store.data_dir, e)
                if not ok:
                    break  # 不推进水位，下轮从不可变台账补漏
                self._notify_seq = e['seq']
        except (ImportError, OSError) as e:
            logger.warning('paper notification enqueue unavailable: %s', type(e).__name__)

    def status(self):
        with self.ledger._lock:
            return {'mode': 'paper_only', 'booted': self._boot,
                    'cash': round(self.ledger.cash, 2) if self._boot else None,
                    'open_positions': len(self.ledger.open_positions) if self._boot else 0,
                    'open_risk': round(self.ledger.open_risk(), 2) if self._boot else None,
                    'registered_cards': len(self.trader._cards), 'last_run_at': self.last_run_at,
                    'last_quote_at': self.last_quote_at, 'last_quote_count': self.last_quote_count,
                    'rejected_quotes': self.rejected_quotes, 'last_error': self.last_error,
                    'validation_missing_symbols': self.opp_svc.validation_missing_symbols,
                    'entry_blockers': self.entry_blockers, 'watchlist_book': self.book_status,
                    'circuit_breaker': {k: v for k, v in (self.breaker_status or {}).items() if k != 'state'} or None,
                    'execution_source': dict(self.execution_source)}
