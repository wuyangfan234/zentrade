"""Shared new-entry admission for lifecycle promotion and BOTH execution frames.

Call under the ledger lock. Frozen hard vetoes never become capacity watches;
live source evidence and borrow permission are deliberately not cached here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from app.market_clock import us_clock
from app.services import decision_desk as dd, decision_scan_job, shortable


@dataclass(frozen=True)
class Admission:
    blockers: tuple[str, ...]
    risk_pct: float
    explore: bool
    tier: str | None = None        # 实际执行档: proven / explore / trial (自选机会卡试用档)
    book: bool = False             # 自选机会卡下单簿的卡 (合约式核算)
    segregated_rule: str | None = None   # evo / swing: position_cap 只数该类仓 (旧仓风险仍计入组合风险上限)
    evo: bool = False              # 自进化卡 (strategy:evo) / tight-v1 机会板卡 (strategy:swing): 持仓数与板块名额只数本类仓, 见 position_cap

    @property
    def allowed(self):
        return not self.blockers


def decision(card):
    return (card.evidence or {}).get('decision') or {}


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def missing_valuation(ledger, quotes):
    """Only accepted fresh execution quotes, never cost/previous-close substitutes.

    只有股票式空头 (卖空所得入现金, 回补负债随现价变化) 才要求全部持仓有新鲜报价; 合约式空头不收卖空所得,
    缺报价只影响浮盈估值, 不卡死全账户的新开仓 (2026-10-10 审查)。"""
    positions = ledger.open_positions
    if not any(p.qty < 0 and not getattr(p, 'contract', False) for p in positions):
        return []
    return sorted(p.symbol for p in positions if not _positive((quotes.get(p.symbol) or {}).get('price')))


def _day(event):
    # Event at is authoritative market time (not the proposal's scan date).
    try:
        return us_clock.market_datetime(event['at']).date().isoformat()
    except (KeyError, ValueError, TypeError):
        return event.get('trade_date')


def exploration_claims(ledger, cards, now):
    """Unique filled cards + current-day pending intents. Replayed after restart.

    Closing a position cannot refund the daily slot. Blocked/old-day intents do
    not reserve a new day's quota, and repeated intents never double-count.
    """
    if ledger is None:
        return set()
    by_id = {c.id: c for c in cards}
    history = ledger.read_events()
    latest, proposals, opened = {}, {}, set()
    for e in history:
        cid = e.get('card_id')
        if e['kind'] == 'entry_intent':
            latest[cid] = e
            proposals[cid] = e.get('proposal') or {}
        if e['kind'] == 'fill' and e.get('side') in ('buy', 'short'):
            opened.add(cid)
    def explore(cid):
        dec = (proposals.get(cid, {}).get('evidence') or {}).get('decision') or {}
        if not dec and cid in by_id:
            dec = decision(by_id[cid])
        return dec.get('tier') == 'explore' or dec.get('entry_tier') == 'explore'
    today = now.date().isoformat()
    claims = {e.get('card_id') for e in history if e['kind'] == 'fill' and e.get('side') in ('buy', 'short')
              and _day(e) == today and (e.get('entry_tier') == 'explore'
                                        or (e.get('entry_tier') is None and explore(e.get('card_id'))))}
    for cid, e in latest.items():
        if cid not in opened and _day(e) == today and e.get('status') == 'pending' and explore(cid):
            claims.add(cid)
    return claims


class IntentIndex:
    """一轮生命周期内的入场意向索引: 台账只读一次 (旧实现每张卡读一遍全台账, 1100+ 张观察卡 ≈ 5.4 秒/轮,
    撮合一轮 15 秒 > 调度间隔, 每隔一帧被跳过)。cancel_pending 追加事件后同步更新索引。"""

    def __init__(self, ledger):
        self.filled: set[str] = set()
        self.latest: dict[str, dict] = {}
        for e in ledger.read_events() if ledger is not None else ():
            cid = e.get('card_id')
            if not cid:
                continue
            if e['kind'] == 'fill':
                self.filled.add(cid)
            elif e['kind'] == 'entry_intent':
                self.latest[cid] = e


def cancel_pending(ledger, card, now, reason, index: IntentIndex | None = None):
    """A veto consumes the prior intent: later recovery needs a new first frame."""
    if ledger is None:
        return
    if index is None:
        index = IntentIndex(ledger)
    if card.id in index.filled:
        return
    latest = index.latest.get(card.id)
    if latest and latest.get('status') == 'pending':
        payload = {k: v for k, v in latest.items() if k not in ('seq', 'kind', 'at')}
        index.latest[card.id] = ledger.append('entry_intent', {**payload, 'status': 'blocked', 'reason': reason},
                                              at=now.isoformat())


# 自选机会卡下单簿的开仓节奏 (2026-10-12 产品决策): 单日最多开 5 笔; 同一板块同一方向最多同时持有 2 仓。
# 10-09 曾在 20 分钟内开 19 笔 (半导体空单扎堆), 组合风险上限挡不住同向集中。
# 0.26.0 (10-18 产品决策 A): 上限按金额折算, 不按笔数 —— 单日新开总风险 <= 权益 5%, 同板块同方向 <= 权益 2%,
# 笔数 = 金额 / 每笔风险: 每笔 1% -> 5 笔 / 2 仓 (10-12 原值), 每笔 0.5% -> 10 笔 / 4 仓。
BOOK_DAILY_OPEN_CAP = 5          # 每笔 1% 时的笔数 (也是读不到风险时的兜底)
BOOK_SECTOR_SIDE_CAP = 2
BOOK_DAILY_RISK_BUDGET = 0.05
BOOK_SECTOR_RISK_BUDGET = 0.02


def book_caps(risk_pct) -> tuple[int, int]:
    """(单日开仓笔数上限, 同板块同方向仓数上限) = 金额上限 / 每笔风险。风险无效 -> 10-12 原值 5 / 2。"""
    if not _positive(risk_pct):
        return BOOK_DAILY_OPEN_CAP, BOOK_SECTOR_SIDE_CAP
    return (max(1, int(BOOK_DAILY_RISK_BUDGET / risk_pct + 1e-9)),
            max(1, int(BOOK_SECTOR_RISK_BUDGET / risk_pct + 1e-9)))


def opens_today(ledger, now) -> int:
    """今天 (美东) 已成交的开仓笔数。平仓不退还额度。"""
    if ledger is None:
        return 0
    today = now.date().isoformat()
    return sum(1 for e in ledger.read_events()
               if e.get('kind') == 'fill' and e.get('side') in ('buy', 'short') and _day(e) == today)


def sector_side_count(ledger, sectors, sector, side, *, evo_only: bool = False, rule: str = 'evo') -> int:
    """当前持仓里与 (板块, 方向) 相同的仓数。evo_only = 只数 rule 类下单簿仓 (与旧试用档仓位分开算板块名额)。"""
    if ledger is None:
        return 0
    want_short = side == 'short'
    return sum(1 for p in ledger.open_positions
               if (p.qty < 0) == want_short and sectors.get(p.symbol) == sector
               and (not evo_only or is_evo_position(p, rule)))


def is_book(card) -> bool:
    return decision(card).get('book') is True


def is_evo_position(pos, rule: str = 'evo') -> bool:
    """台账持仓是否来自该类下单簿卡 (卡 id = opp-<日>-<票>-book-<rule>[-short]-<hash>)。rule = evo / swing。"""
    return f'-book-{rule}-' in str(getattr(pos, 'card_id', None) or '')


def position_cap(account, admission, portfolio_risk_pct: float, ledger=None) -> int:
    """持仓数上限: 自选机会卡按“组合风险上限 ÷ 单笔风险”放宽 (0.25% → 24 只, 1% → 6 只, 黄灯 3% → 12/3 只),
    不低于账户设定; 其余卡沿用账户设定。
    自进化卡 (0.15.1): 名额只数自进化仓 —— 撮合比的是“全部持仓数 ≥ 上限”, 所以把非自进化旧仓数加到上限上。
    旧仓 (试用档 0.25%) 的风险仍计入组合风险上限 (6%), 总敞口照样封顶; 只是不让 21 个旧仓把 6 个名额占满一周。"""
    base = int(account.max_positions)
    if not getattr(admission, 'book', False) or not _positive(admission.risk_pct):
        return base
    cap = max(base, int(portfolio_risk_pct / admission.risk_pct + 1e-9))
    if getattr(admission, 'evo', False) and ledger is not None:
        rule = getattr(admission, 'segregated_rule', None) or 'evo'
        cap += sum(1 for p in ledger.open_positions if not is_evo_position(p, rule))
    return cap


def _evaluate_book(card, dec, *, data_dir, now, cards=(), ledger=None, sectors=None):
    """自选机会卡下单簿 (v4, 0.11.0 单一口径) 的准入: 只认当前许可 (watchlist_v4.permission)、单档落簿 (tier='rule') 与当前自选。
    合约式核算: 空头不查借券 (按合约做空, 无借券概念); 不占探索额度。风险取当前许可 (规则集 risk_per_trade_pct)。"""
    from app.services import strategy_evaluation as _se
    from app.services import watchlist_v4 as wv
    source = dd.source_of(card.setup_type)
    blockers = []
    if _se.evidence_mode(data_dir) != 'v4':
        blockers.append('book_requires_v4')
    ok, risk, tier = wv.rule_permission(wv.permission_cached(data_dir), source, card.side)
    if not ok:
        blockers.append(f'source_not_tradable:{source}')
    # 0.12.0 去掉 MA50 门槛后仍只认单档 (tier='rule') 落簿的卡: 0.11.0 前的试用档旧卡 (tier='trial'/缺) 不开, 等入场窗自然过期
    if dec.get('tier') != wv.TIER:
        blockers.append('legacy_card')
    if card.symbol not in decision_scan_job._watchlist() or dec.get('tab') != 'watchlist':
        blockers.append('not_in_watchlist')
    if card.side not in (dec.get('sides') or [card.side]) or 'direction_conflict' in (dec.get('hard_blockers') or []):
        blockers.append('direction_conflict')
    # 0.27.0: 做多机会板卡第 1 天盘前已越过止损 -> 作废 (交易环会把卡转 EXPIRED; 这里是第二道闸, 记录在即拦)。
    # 第 1 天还没查过 (09:20 定时任务与交易环每轮补查之前) -> 先不开 (fail-closed; 纸面 09:45 才成交, 正常不会等)。
    if source == wv.SWING_RULE and not card.is_short:
        from app.services import swing_pm_check as _pmc
        st = _pmc.guard_state(data_dir, card.id, card.created_date, now)
        if st == 'void':
            blockers.append(_pmc.REASON)
        elif st == 'pending':
            blockers.append(_pmc.PENDING)
    # 同一只票另有仍在入场窗的反方向机会卡 (不同信号日, 含被拦成观察的) → 多空分歧, 双方都不开
    # (只看 OPEN/ACTIVE 会在双方都被打回 WATCH 后放行先入区的一方)
    from app.opportunity.card import CardState as _CS
    if any(c.symbol == card.symbol and c.side != card.side and is_book(c) and c.id != card.id
           and c.state in (_CS.OPEN, _CS.ACTIVE, _CS.WATCH) for c in (cards or ())):
        blockers.append('direction_conflict')
    day_cap, sector_cap = book_caps(risk if ok else None)
    if ledger is not None and ledger.position(card.symbol) is None:
        if opens_today(ledger, now) >= day_cap:
            blockers.append('daily_open_cap')
        # sectors=None = 调用方未提供板块表 (离线/旧调用); 生产两处调用都传。提供了却查不到 = 不开 (fail-closed)
        if sectors is not None:
            sector = sectors.get(card.symbol)
            if not sector:
                blockers.append('sector_unknown')
            elif sector_side_count(ledger, sectors, sector, card.side,
                                   evo_only=source in (wv.EVO_RULE, wv.SWING_RULE),
                                   rule=source) >= sector_cap:
                blockers.append('sector_side_cap')
    return Admission(tuple(dict.fromkeys(blockers)), risk if ok else 0.0, False, tier=tier if ok else None, book=True,
                     evo=source in (wv.EVO_RULE, wv.SWING_RULE),
                     segregated_rule=source if source in (wv.EVO_RULE, wv.SWING_RULE) else None)


def evaluate(card, *, data_dir, now: datetime, ledger=None, cards=(), claimed=(),
             default_risk=dd.PROVEN_RISK_PCT, check_borrow=False, refresh_borrow=False, sectors=None):
    dec = decision(card)
    if dec.get('book') is True:
        return _evaluate_book(card, dec, data_dir=data_dir, now=now, cards=cards, ledger=ledger, sectors=sectors)
    source = dd.source_of(card.setup_type)
    blockers = list(dec.get('hard_blockers') or [])
    sides = set(dec.get('sides') or [dd.side_of(s) for s in dec.get('sources', [])])
    if dec.get('direction_conflict') or len(sides) > 1:
        blockers.append('direction_conflict')
    if dec.get('tier') == 'negative' or ('tradable_sources' in dec and not dec['tradable_sources']):
        blockers.append(f'source_not_tradable:{source}')
    # Explicit zero/None/NaN is never a request to use default risk.
    risk = dec.get('risk_pct', default_risk)
    if not _positive(risk):
        blockers.append('risk_zero')
        risk = 0.0
    permission = decision_scan_job.source_permission(data_dir, source)
    if permission.get('tradable') is not True:
        blockers.append(f'source_not_tradable:{source}')
    else:
        risk = min(risk, permission['risk_pct'])
    explore = dec.get('tier') == 'explore' or permission.get('tier') == 'explore'
    if explore:
        risk = min(risk, dd.EXPLORE_RISK_PCT)
        claims = exploration_claims(ledger, cards, now) | set(claimed)
        if len(claims - {card.id}) >= dd.EXPLORE_DAILY_CAP:
            blockers.append('explore_daily_quota')
    # 自选独立选股 (2026-10-05): 只经自选通道入选的卡, 自选移出后停止新开; 读不到名单同样禁入 (fail-closed)。
    from app.services import strategy_evaluation as _se
    v4 = _se.evidence_mode(data_dir) == 'v4'
    if v4:
        # 自选机会 v4 (2026-10-06): 只交易扫描时与当前都在自选里的票; 全市场扫描只记账 (读不到名单 = 禁入)
        if card.symbol not in decision_scan_job._watchlist() or dec.get('tab') != 'watchlist':
            blockers.append('not_in_watchlist')
        # 2026-10-10: v4 的自选开仓只走自选机会卡下单簿 (watchlist_book, 两档同一来源); 决策台卡只观察, 不重复开仓
        blockers.append('v4_book_only')
    elif dec.get('lane_only'):
        if card.symbol not in decision_scan_job._watchlist():
            blockers.append('watchlist_removed')
        risk = min(risk, dd.EXPLORE_RISK_PCT)  # 证据只覆盖全池前 10, 自选通道票按探索档风险
    if not blockers and check_borrow and card.is_short:
        if shortable.checker(data_dir)(card.symbol, refresh=refresh_borrow) is not True:
            blockers.append('borrow_unavailable')
    return Admission(tuple(dict.fromkeys(blockers)), risk, explore,
                     tier='explore' if explore else 'proven')
