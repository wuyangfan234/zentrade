"""Frozen daily proposals, event-derived lifecycle, and point-in-time scans."""
from __future__ import annotations

import json
import math
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
import polars as pl
from app.market_clock import us_clock
from app.services import entry_guard
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult, load_params, scan, universe_filter

logger = logging.getLogger(__name__)


class OpportunityService:
    desk_summary: dict | None = None

    def __init__(self, repo) -> None:
        self.repo = repo
        self._dir = None
        self.ledger = None
        self._lock = threading.RLock()
        self._validation_key = None
        self._validation_eligible = set()
        self.validation_missing_symbols = []
        self.entry_blockers = []

    def bind_ledger(self, ledger) -> None:
        self.ledger = ledger
        self._lock = ledger._lock

    @property
    def store_dir(self) -> Path:
        if self._dir is None:
            self._dir = Path(self.repo.store.data_dir) / 'opportunities'
        return self._dir

    def _cards_path(self, day: date) -> Path:
        return self.store_dir / 'cards' / f'{day.isoformat()}.json'

    def _raw_cards(self, day: date):
        p = self._cards_path(day)
        return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None

    def _events(self, as_of: date | None = None) -> list[dict]:
        if self.ledger is not None:
            events = self.ledger.read_events()
        else:
            p = Path(self.repo.store.data_dir) / 'ledger/events.jsonl'
            events = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
        return [e for e in events if as_of is None or str(e.get('trade_date') or e.get('at',''))[:10] <= as_of.isoformat()]

    def save_scan(self, result: ScanResult) -> None:
        with self._lock:
            old = self._raw_cards(result.as_of) or {}
            # Existing proposal wins, including on parameter-revision rescan.
            cards = {c['symbol']: c for c in old.get('cards', [])}
            watch = {c['symbol']: c for c in old.get('watch', [])}
            for c in result.cards:
                if c.symbol not in cards and c.symbol not in watch:
                    cards[c.symbol] = c.to_dict()
            for c in result.watch:
                if c.symbol not in cards and c.symbol not in watch:
                    watch[c.symbol] = c.to_dict()
            watch = {s:c for s,c in watch.items() if s not in cards}
            payload = dict(as_of=result.as_of.isoformat(), gate=result.gate,
                           params_revision=result.params_revision, scanned=result.scanned,
                           cards=list(cards.values()), watch=list(watch.values()), rejected_count=len(result.rejected))
            p = self._cards_path(result.as_of)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix('.tmp')
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(p)

    def _transitions(self, as_of: date | None = None) -> dict:
        transitions = {}
        for ev in self._events(as_of):
            cid = ev.get('card_id')
            if not cid:
                continue
            if ev['kind'] == 'opportunity':
                transitions[cid] = (ev.get('state'), ev.get('reason',''))
            elif ev['kind'] == 'fill' and ev.get('side') in ('buy', 'short'):
                transitions[cid] = ('filled', '纸面成交')
            elif ev['kind'] == 'close':
                transitions[cid] = ('closed', ev.get('reason','纸面平仓'))
        return transitions

    def load_cards(self, day: date, *, as_of: date | None = None) -> dict | None:
        payload = self._raw_cards(day)
        if payload is None:
            return None
        transitions = self._transitions(as_of)
        cards, watch = [], []
        seen = set()
        for c in payload.get('cards', []) + payload.get('watch', []):
            if c['id'] in seen:
                continue
            seen.add(c['id'])
            c = dict(c)
            if c['id'] in transitions:
                c['state'],c['state_reason'] = transitions[c['id']]
            (watch if c['state'] == 'watch' else cards).append(c)
        live_symbols = {c['symbol'] for c in cards if c['state'] in ('open','active','filled')}
        payload['cards'] = cards
        payload['watch'] = [c for c in watch if c['symbol'] not in live_symbols]
        return payload

    def book_cards(self, as_of: date | None = None) -> list[OpportunityCard]:
        """自选机会卡下单簿 (watchlist_book, v4 纸面试用档) 的卡, 叠加台账状态。读失败的日子跳过。"""
        from app.services import watchlist_book
        data_dir = Path(self.repo.store.data_dir)
        ref = as_of or us_clock.today()
        days = [d for d in watchlist_book.book_days(data_dir)
                if d <= ref and (ref - d).days <= watchlist_book.LIVE_DAYS]
        if not days:
            return []
        transitions = self._transitions(as_of)
        out = []
        for d in days:
            payload = watchlist_book.load_day(data_dir, d) or {}
            conflicts = set(payload.get('conflict_ids') or [])
            for c in payload.get('cards', []):
                try:
                    c = dict(c)
                    if c.get('id') in transitions:
                        c['state'], c['state_reason'] = transitions[c['id']]
                    if c.get('id') in conflicts:
                        ev = dict(c.get('evidence') or {})
                        dec = dict(ev.get('decision') or {})
                        dec['hard_blockers'] = sorted({*(dec.get('hard_blockers') or []), 'direction_conflict'})
                        c['evidence'] = {**ev, 'decision': dec}
                    out.append(OpportunityCard.from_dict(c))
                except (KeyError, TypeError, ValueError):
                    logger.warning('watchlist book %s: bad card skipped', d)
        return out

    def list_card_days(self) -> list[str]:
        return sorted(p.stem for p in (self.store_dir / 'cards').glob('*.json'))

    def all_cards(self, as_of: date | None = None) -> list[OpportunityCard]:
        out = {}
        for day in self.list_card_days():
            d = date.fromisoformat(day)
            if as_of and d > as_of:
                continue
            payload = self.load_cards(d, as_of=as_of) or {}
            for c in payload.get('cards', []) + payload.get('watch', []):
                out[c['id']] = OpportunityCard.from_dict(c)
        for c in self.book_cards(as_of):
            out.setdefault(c.id, c)
        return list(out.values())

    def _transition(self, card, state, reason, now):
        if self.ledger is None or card.state == state:
            return
        self.ledger.append('opportunity', {'card_id':card.id,'symbol':card.symbol,
            'state':str(state),'reason':reason,'trade_date':now.date().isoformat()}, at=now.isoformat())

    def cooldown_symbols(self, as_of: date) -> set[str]:
        limit = load_params()['portfolio']['same_symbol_cooldown_days']
        out=set()
        for e in self._events(as_of):
            if e.get('kind') != 'close':
                continue
            day = date.fromisoformat(str(e.get('trade_date') or e['at'])[:10])
            elapsed = us_clock.trading_days_between(day + timedelta(days=1), as_of)
            if len(elapsed) < limit:
                out.add(e['symbol'])
        return out

    def sector_map(self) -> dict[str, str]:
        try:
            df = self.repo.get_instruments()
            if 'sector' not in df.columns:
                return {}
            return {r['symbol']: r['sector'] for r in df.to_dicts() if r.get('sector') and r['sector'] not in ('?','N/A','Unknown')}
        except (AttributeError, TypeError):
            return {}

    def industry_map(self) -> dict[str, str]:
        """细分行业 (只用于“同主题扎堆”提示, 不进组合约束)。"""
        try:
            df = self.repo.get_instruments()
            if 'industry' not in df.columns:
                return {}
            return {r['symbol']: r['industry'] for r in df.select(['symbol', 'industry']).to_dicts()
                    if r.get('industry') and r['industry'] not in ('?', 'N/A', 'Unknown')}
        except (AttributeError, TypeError):
            return {}

    def loop_cards(self, as_of: date | None = None, *, book_only: bool = False) -> list[OpportunityCard]:
        """交易环要处理的卡。book_only (0.35.2, v4 = 只交易下单簿): 只留下单簿卡 + 有持仓的卡。
        v4 下其余旧决策台卡在准入里恒被 v4_book_only 拦下, 永不开仓 (每晚 200-300 张观察卡, 10-22 盘中 1291 张);
        它们的到期仍由收盘后 advance_lifecycle (不带 book_only) 处理, 新闻覆盖等读 all_cards 的地方不受影响。"""
        cards = self.all_cards(as_of)
        if not book_only:
            return cards
        held = {p.card_id for p in self.ledger.open_positions if p.card_id} if self.ledger is not None else set()
        return [c for c in cards if entry_guard.is_book(c) or c.id in held]

    def revalidate_unfilled(self, now: datetime, *, book_only: bool = False) -> set[str]:
        """启动/新卡/每日首轮复验旧提案，只使用上一已收盘交易日的数据。"""
        cards = [c for c in self.loop_cards(now.date(), book_only=book_only)
                 if c.state in (CardState.OPEN, CardState.ACTIVE, CardState.WATCH)]
        if not cards:
            return set()
        params = load_params()
        key = (now.date(), params['revision'], tuple(sorted(c.id for c in cards)))
        if key == self._validation_key:
            return set(self._validation_eligible)
        sessions = us_clock.trading_days_between(now.date()-timedelta(days=14), now.date()-timedelta(days=1))
        symbols = sorted({c.symbol for c in cards})
        self.validation_missing_symbols = symbols
        if not sessions or not callable(getattr(self.repo, 'get_daily_batch', None)):
            return set()
        end = sessions[-1]
        data = self.repo.get_daily_batch(symbols, end-timedelta(days=400), end)
        data = data.filter(pl.col('date') <= end) if not data.is_empty() else data
        frames = {g['symbol'][0]: g.sort('date') for g in data.partition_by('symbol')} if not data.is_empty() else {}
        eligible, missing = set(), set()
        with self._lock:
            for card in cards:
                frame = frames.get(card.symbol)
                if frame is None or frame.height < params['universe']['min_history_days'] or frame['date'][-1] != end:
                    missing.add(card.symbol)
                    continue  # 数据不足只能禁入，不能凭空断言提案失效
                if universe_filter(frame, params):
                    eligible.add(card.symbol)
                elif self.ledger.position(card.symbol) is None:
                    self._transition(card, CardState.EXPIRED, '入场前复验未通过流动性/ATR门槛', now)
        self.validation_missing_symbols = sorted(missing)
        # 有缺数据时下轮重试；完整证据按交易日/卡集合缓存。
        self._validation_key = key if not missing else None
        self._validation_eligible = eligible
        return set(eligible)

    def advance_lifecycle(self, now: datetime, quotes: dict, *, bars_by_symbol=None, quotes_ready=True,
                          book_only: bool = False) -> None:
        if self.ledger is None:
            return
        now = now.astimezone(us_clock.tz) if now.tzinfo else now.replace(tzinfo=us_clock.tz)
        params = load_params()
        pf = params['portfolio']
        self.entry_blockers = []
        with self._lock:
            cards = self.loop_cards(now.date(), book_only=book_only)
            sector_map = self.sector_map()
            active = {c.symbol for c in cards if c.state in (CardState.OPEN,CardState.ACTIVE,CardState.FILLED)}
            occupied = {}
            for c in cards:
                if c.state in (CardState.OPEN,CardState.ACTIVE,CardState.FILLED):
                    sec=sector_map.get(c.symbol) or c.evidence.get('sector')
                    if sec: occupied[sec]=occupied.get(sec,0)+1
            cooldown = self.cooldown_symbols(now.date())
            claimed = set()
            card_days = self.list_card_days()
            gate = (self._raw_cards(date.fromisoformat(card_days[-1])) or {}).get('gate') if card_days else None
            # The scheduler's pre-fetch pass has no quote evidence yet; it must
            # still recheck sources, but not cancel intents merely for that absence.
            missing_marks = entry_guard.missing_valuation(self.ledger, quotes) if quotes_ready else []
            intents = entry_guard.IntentIndex(self.ledger)      # 本轮只读一次台账
            for c in sorted(cards,key=lambda c:(c.state == CardState.WATCH,c.created_date,c.id)):
                if c.state not in (CardState.OPEN,CardState.ACTIVE,CardState.WATCH):
                    continue
                days=us_clock.trading_days_between(c.created_date+timedelta(days=1),now.date())
                n=len(days)
                win = pf['watchlist_promotion_days']
                ed = entry_guard.decision(c).get('entry_days')     # tight-v1 机会板卡: 2 日入场窗
                if type(ed) is int and 1 <= ed < win:
                    win = ed
                cutoff = n > win or (n == win and now.time() >= us_clock.session_close(now.date()))
                if cutoff:
                    entry_guard.cancel_pending(self.ledger, c, now, 'entry_window_expired', intents)
                    self._transition(c,CardState.EXPIRED,f'{win}个交易日入场窗结束',now)
                    continue
                if bars_by_symbol is not None and c.symbol in bars_by_symbol and not universe_filter(bars_by_symbol[c.symbol],params):
                    self._transition(c,CardState.EXPIRED,'最新流动性/波动门槛未通过',now)
                    continue
                if n == 0 or not us_clock.in_session(now):
                    continue
                q=quotes.get(c.symbol) or {}
                admission = entry_guard.evaluate(c, data_dir=self.repo.store.data_dir, now=now,
                    ledger=self.ledger, cards=cards, claimed=claimed,
                    default_risk=self.ledger_account().risk_per_trade_pct, check_borrow=bool(q), sectors=sector_map)
                reasons = list(admission.blockers)
                if missing_marks:
                    reasons.append('valuation_incomplete')
                if reasons:
                    self.entry_blockers.extend({'symbol': c.symbol, 'reason': reason, 'card_id': c.id} for reason in reasons)
                    entry_guard.cancel_pending(self.ledger, c, now, reasons[0], intents)
                    self._transition(c, CardState.WATCH, '准入阻断：' + '；'.join(reasons), now)
                    if c.symbol in active:
                        active.discard(c.symbol)
                        old_sec = sector_map.get(c.symbol) or c.evidence.get('sector')
                        if old_sec:
                            occupied[old_sec] = max(0, occupied.get(old_sec, 0) - 1)
                    continue
                price=q.get('price')
                if price is None:
                    continue
                open_price=q.get('open')
                if not isinstance(open_price,(int,float)) or isinstance(open_price,bool) or not math.isfinite(open_price) or open_price<=0:
                    continue  # 开盘缺失不能被解释成没有跳空
                if entry_guard.decision(c).get('limit_pullback') is True:
                    pass  # tight-v1 限价回踩卡: 开盘远离入场区不作废, 价回到区内才成交 (入场窗 2 日兜底)
                elif c.side == 'short':
                    if open_price < c.entry_zone[0]*0.98:
                        self._transition(c,CardState.EXPIRED,'开盘价低于入场区下沿2%（空单不追）',now)
                        continue
                elif open_price > c.entry_zone[1]*1.02:
                    self._transition(c,CardState.EXPIRED,'开盘价超入场区上沿2%',now)
                    continue
                sec=sector_map.get(c.symbol) or c.evidence.get('sector')
                if not sec:
                    continue  # 真行业未验证不激活，不把未知凑成单个行业
                if c.state == CardState.OPEN:
                    self._transition(c,CardState.ACTIVE,'次交易日开盘确认有效',now)
                    if admission.explore:
                        claimed.add(c.id)
                elif c.state == CardState.ACTIVE and admission.explore:
                    claimed.add(c.id)
                elif c.state == CardState.WATCH and c.entry_zone[0] <= price <= c.entry_zone[1]:
                    account=self.ledger_account()
                    cap_risk = account.max_portfolio_risk_pct if gate != 'yellow' else min(account.max_portfolio_risk_pct, 0.03)
                    # 自选机会卡 (v4): 不设行业上限 (自选 59 只里 37 只科技), 持仓数按“组合风险 ÷ 单笔风险”
                    sector_full = (not admission.book) and occupied.get(sec,0) >= pf['sector_max_active']
                    if c.symbol in active or c.symbol in cooldown or sector_full:
                        continue
                    if len(self.ledger.open_positions) >= entry_guard.position_cap(account, admission, cap_risk, self.ledger):
                        continue
                    equity=self.ledger.nav({s:q['price'] for s,q in quotes.items() if q.get('price')})
                    if equity <= 0 or self.ledger.open_risk()+equity*admission.risk_pct > equity*account.max_portfolio_risk_pct:
                        continue
                    self._transition(c,CardState.ACTIVE,'观察卡入区且组合容量恢复',now)
                    active.add(c.symbol);occupied[sec]=occupied.get(sec,0)+1
                    if admission.explore:
                        claimed.add(c.id)

    def ledger_account(self):
        from app.ledger.core import Account
        p=Path(self.repo.store.data_dir)/'ledger/account.json'
        return Account.from_file(p) if hasattr(Account,'from_file') and p.exists() else Account()

    def _active_symbols(self, as_of: date | None = None) -> set[str]:
        return {c.symbol for c in self.all_cards(as_of) if c.state in (CardState.OPEN,CardState.ACTIVE,CardState.FILLED)}

    def run_daily_scan(self, as_of: date | None = None) -> ScanResult:
        as_of=as_of or us_clock.today()
        params=load_params()
        bars,spy=self._load_universe(params,as_of)
        # 0.22+: evo precompute / 大市值弱势栏已退役 (出卡走 swing_desk)
        with self._lock:
            now=datetime.combine(as_of,us_clock.sessions[-1].close,tzinfo=us_clock.tz)
            self.advance_lifecycle(now,{},bars_by_symbol=bars)
            active=self._active_symbols(as_of)
            risk=0.0
            if self.ledger is not None:
                equity=self.ledger.nav({s:float(b['close'][-1]) for s,b in bars.items() if not b.is_empty()})
                if equity>0: risk=self.ledger.open_risk()/equity*100
            excluded=active|self.cooldown_symbols(as_of)
            result=scan(bars,spy,params,as_of,active_symbols=excluded,
                        sector_map=self.sector_map(),portfolio_risk_used_pct=risk)
            # 全系统决策台 (2026-10-02): 策略库全部来源 + S1/S2 统一成卡、按证据加权后替换当日卡。
            # 决策台失败时保留 S1/S2 原结果并在 desk_summary 里写明原因, 不静默。
            self.desk_summary = None
            try:
                from app.services import decision_scan_job
                desk = decision_scan_job.run(self, as_of, s1s2=result, frames=bars, params=params,
                                             excluded=excluded, risk_used_pct=risk)
                if desk is not None:
                    result, self.desk_summary = desk.result, desk.summary
            except Exception as e:  # noqa: BLE001
                logger.exception('decision desk failed; keeping S1/S2 scan')
                self.desk_summary = {'error': f'{type(e).__name__}: {e}'[:300]}
            self.save_scan(result)
        logger.info('daily opportunity scan %s: scanned=%d cards=%d watch=%d gate=%s',as_of,result.scanned,len(result.cards),len(result.watch),result.gate)
        return result

    def _load_universe(self, params: dict, as_of: date | None = None):
        end=as_of or us_clock.today()
        start=end-timedelta(days=400)
        inst=self.repo.get_instruments()
        if inst.is_empty(): return {},pl.DataFrame()
        # reference明确标ETF/基金/结构性产品的排除；无元数据时不靠Stock/Trust子串猜。
        if 'instrument_type' in inst.columns:
            inst = inst.filter(~pl.col('instrument_type').is_in(['etf','fund','preferred','warrant','unit','right']).fill_null(False))
        for flag in ('is_fund', 'is_etf', 'is_preferred', 'is_warrant', 'is_unit'):
            if flag in inst.columns:
                inst = inst.filter(~pl.col(flag).fill_null(False))
        symbols=inst['symbol'].to_list()
        from app.services.index_const import CORE_INDEX_SYMBOLS
        symbols=list(dict.fromkeys(symbols+list(CORE_INDEX_SYMBOLS)))
        all_bars=self.repo.get_daily_batch(symbols,start,end)
        if all_bars.is_empty(): return {},pl.DataFrame()
        all_bars=all_bars.filter(pl.col('date')<=end).sort(['symbol','date'])
        u=params['universe']
        liquid=set(all_bars.filter(pl.col('date')>=end-timedelta(days=30)).group_by('symbol').agg([
            (pl.col('close')*pl.col('volume')).mean().alias('adv'),pl.col('close').last().alias('last')
        ]).filter((pl.col('adv')>=u['min_adv_usd'])&(pl.col('last')>=u['min_price']))['symbol'].to_list())
        universe={}
        for frame in all_bars.partition_by('symbol'):
            sym=frame['symbol'][0]
            if sym in liquid and sym not in CORE_INDEX_SYMBOLS and frame.height>=u['min_history_days']:
                universe[sym]=frame
        self.universe_diag = self._watchlist_pool_diag(all_bars, universe, set(symbols), u, end)
        return universe,all_bars.filter(pl.col('symbol')=='SPY')

    universe_diag: dict | None = None

    @staticmethod
    def _watchlist_pool_diag(all_bars, universe, listed, u, end) -> dict:
        """自选里没进流动性池的票, 逐只写明卡在哪条门槛 (只读诊断, 2026-10-05)。"""
        try:
            from app.services import watchlist as _wl
            members = {r['symbol'] for r in _wl.list_symbols() if r.get('symbol')}
        except Exception:  # noqa: BLE001
            return {}
        out = {}
        for sym in sorted(members - set(universe)):
            if sym not in listed:
                out[sym] = '不在股票主表（ETF/基金/非普通股或代码未同步）'
                continue
            f = all_bars.filter(pl.col('symbol') == sym)
            if f.is_empty():
                out[sym] = '无日K数据'
                continue
            recent = f.filter(pl.col('date') >= end - timedelta(days=30))
            last = float(f['close'][-1])
            adv = float((recent['close'] * recent['volume']).mean()) if not recent.is_empty() else 0.0
            if last < u['min_price']:
                out[sym] = f"价格 ${last:.2f} 低于门槛 ${u['min_price']}"
            elif adv < u['min_adv_usd']:
                out[sym] = f"近 30 日均成交额 ${adv/1e6:.1f}M 低于 ${u['min_adv_usd']/1e6:.0f}M"
            elif f.height < u['min_history_days']:
                out[sym] = f"历史 {f.height} 天少于 {u['min_history_days']} 天"
            else:
                out[sym] = '未进流动性池'
        return out
