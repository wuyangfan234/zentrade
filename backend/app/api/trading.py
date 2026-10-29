# ruff: noqa: RUF001
"""Read-only, user-visible status of the paper production chain."""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from app.market_clock import us_clock
from app.services.setup_status import SETUPS, SetupStatusStore

router=APIRouter(prefix='/api/trading',tags=['paper-trading'])

@router.post('/reconcile')
def reconcile(request: Request):
    """只复验未成交机会与过期状态，禁止撮合、禁止历史台账篡改。"""
    loop = getattr(request.app.state, 'trading_loop', None)
    if loop is None:
        raise HTTPException(503, '交易编排未初始化')
    now = us_clock.now()
    loop.opp_svc.advance_lifecycle(now, {})
    eligible = loop.opp_svc.revalidate_unfilled(now)
    return {'executed_trades': 0, 'eligible_symbols': sorted(eligible), 'status': loop.status()}


@router.get('/status')
def status(request:Request):
    loop=getattr(request.app.state,'trading_loop',None)
    scheduler=getattr(request.app.state,'scheduler',None)
    base=loop.status() if loop is not None else {'mode':'paper_only','booted':False,'last_error':'交易编排未初始化'}
    from app.services.notify_opportunity import notification_status
    from app.services.earnings_calendar import EarningsCalendar
    from app.services.corporate_actions import CorporateActionService, TYPE_LABEL
    data_dir = request.app.state.repo.store.data_dir
    earnings = EarningsCalendar(data_dir).status()
    corp_svc = CorporateActionService(data_dir)
    corp = corp_svc.status(us_clock.now())
    corp['flagged'] = [{'symbol': a['symbol'], 'type': a['type'], 'label': TYPE_LABEL.get(a['type'], a['type']),
                        'ex_date': a.get('ex_date')} for a in corp_svc.actions() if not a.get('auto')]
    return {**base,'scheduled':bool(scheduler and scheduler.get_job('zen_paper_tick')),
        'market_time':us_clock.now().isoformat(),'in_session':us_clock.in_session(),
        'notifications':notification_status(data_dir),
        'protections':{'source_timestamp_required':True,'max_quote_age_seconds':120,
            'earnings_auto':earnings['status'],'earnings_calendar':earnings,
            'earnings_rules':{'entry_block':'仅财报前 1 个交易日不开新仓（财报当天可开）',
                              'reduce':'持仓期内有财报: 盘前/未知在前一交易日、盘后在当日 15:30-16:00 ET 减半',
                              'calendar_unavailable':'暂停一切新开仓，持仓照常管理'},
            'corporate_actions_auto':corp['status'],'corporate_actions':corp,
            'paper_only':True},
        'note':'仅纸面撮合，无真实下单接口；数据源失效时不伪造成交。'}



class RestoreBody(BaseModel):
    note: str = Field('', max_length=200)


@router.get('/setup-status')
def setup_status(request: Request):
    """各 setup 当前状态 (active / degraded) 与降级证据、审计流水。"""
    store = SetupStatusStore(request.app.state.repo.store.data_dir)
    return {'setups': store.summary(), 'audit': store.audit()[-20:],
            'rule': '近 28 个自然日闭合回合有效 R ≥10 笔且平均 R<0 → 自动降级为只观察；只能人工恢复'}


@router.post('/setup-status/{setup}/restore')
def restore_setup(setup: str, body: RestoreBody, request: Request):
    """人工恢复一个已降级的 setup (立即生效)。"""
    store = SetupStatusStore(request.app.state.repo.store.data_dir)
    if setup not in SETUPS and setup not in store.load():
        raise HTTPException(404, '未知的形态')
    store.restore(setup, note=body.note, at=us_clock.now().isoformat())
    return {'setup': next(s for s in store.summary() if s['setup'] == setup)}


@router.get('/premarket-gap')
def premarket_gap_status(request: Request):
    """最近一次盘前跳空检查 (美东 09:00 运行; HL 永续价, 只提醒)。"""
    from app.services import premarket_gap
    return {'latest': premarket_gap.latest(request.app.state.repo.store.data_dir),
            'rule': '美东 09:00（北京夏令时 21:00 / 冬令时 22:00）检查 HL 有合约的持仓、机会卡和自选股；'
                    '较昨收 ±3% 合并推送一条；无触发不推送；只提醒，不触发任何交易',
            'threshold_pct': premarket_gap.THRESHOLD_PCT}


class MirrorToggle(BaseModel):
    enabled: bool
    note: str = Field('', max_length=200)


@router.get('/alpaca-mirror')
def alpaca_mirror_status(request: Request):
    """Alpaca 纸面镜像: 开关、逐笔镜像单 (内部价 vs Alpaca 成交价 / 偏差 bps)、镜像持仓。"""
    from app.services.alpaca_mirror import AlpacaMirror
    m = AlpacaMirror(request.app.state.repo.store.data_dir)
    return {'summary': m.summary(), 'orders': m.orders()[-100:]}


@router.post('/alpaca-mirror/enabled')
def alpaca_mirror_toggle(body: MirrorToggle, request: Request):
    """一键停用 / 恢复镜像下单 (只影响 Alpaca 纸面账户, 不影响内部纸面台账)。"""
    from app.services.alpaca_mirror import AlpacaMirror
    m = AlpacaMirror(request.app.state.repo.store.data_dir)
    m.set_enabled(body.enabled, body.note)
    return m.summary()


def _et(value) -> str:
    """ISO 时间 → 'YYYY-MM-DD HH:MM ET' (美东); 空/坏值 → '—'。"""
    if not value:
        return '—'
    try:
        return us_clock.market_datetime(str(value)).strftime('%Y-%m-%d %H:%M ET')
    except (TypeError, ValueError):
        return str(value)[:16]


def _et_epoch(ts) -> str:
    """epoch 秒 → 'YYYY-MM-DD HH:MM ET'; 空/坏值 → '—'。"""
    try:
        from datetime import UTC, datetime
        return _et(datetime.fromtimestamp(float(ts), tz=UTC).isoformat()) if ts else '—'
    except (TypeError, ValueError, OverflowError, OSError):
        return '—'


# 系统状态页展示用: 状态码 → 中文 (只改文案, 不参与任何判定)
_STATUS_CN = {'connected': '已接入', 'stale': '已过期', 'unavailable': '不可用', 'missing': '无数据', 'error': '出错',
              'succeeded': '成功', 'failed': '失败', 'skipped': '跳过', 'running': '运行中'}


def _cn(code) -> str:
    return _STATUS_CN.get(str(code), str(code)) if code else '—'


def _v4_evidence_row(data_dir, ev: dict, weekly: str) -> tuple[str, str, str]:
    """v4 (自选前向确认) 没有评估窗口: 显示现行口径的真实字段 —— 规则集版本/保存时间、勾选与可下单条数、
    前向确认样本与是否通过、影子前向数据更新时刻。只读展示, 不碰许可与证据计算。"""
    from app.services import watchlist_v4 as wv
    rs = ev.get('ruleset') or {}
    fwd = ev.get('forward') or {}
    srcs = ev.get('sources') or {}
    ver = rs.get('version')
    enabled = sum(1 for v in srcs.values() if v.get('enabled'))
    tradable = sum(1 for v in srcs.values() if v.get('tradable'))
    shadow = wv.path(data_dir)
    try:
        from datetime import datetime
        shadow_at = _et(datetime.fromtimestamp(shadow.stat().st_mtime, tz=us_clock.tz).isoformat())
    except OSError:
        shadow_at = '—'
    passed = '已通过' if fwd.get('promoted') else '未通过'
    thr = f"，门槛 {fwd['threshold']}" if fwd.get('threshold') is not None else ''
    summary = (f"现行算法 v4（自选前向确认）：规则集 R{ver if ver is not None else '—'}（保存于 {_et(rs.get('created_at'))}），"
               f"勾选 {enabled} 条、前向确认档可下单 {tradable} 条；前向确认样本 {fwd.get('n') or 0}，{passed}{thr}；"
               f"影子前向数据更新于 {shadow_at}" + weekly)
    source = ' · '.join(['data/rules/current.json'] + ([f'data/rules/R{ver}.json'] if ver is not None else [])
                        + ['data/decision_desk/shadow_watchlist.jsonl'])
    return ('ok' if ver is not None else 'warn'), summary, source


def _redact(text):
    from app.services.ops_watch import redact
    return redact(text)



def _news_translate_failing(nw: dict) -> bool:
    """0.41.1: 翻译失败只在连续多轮时算故障。旧 state 没有 streak 字段 → 按旧口径 (本轮有失败即算)。"""
    from app.services.news_job import TRANSLATE_FAIL_WARN_STREAK
    tr = nw.get('translate') or {}
    if not tr.get('failed_batches'):
        return False
    streak = nw.get('translate_fail_streak')
    return streak is None or int(streak) >= TRANSLATE_FAIL_WARN_STREAK


def _pipeline_session_finalized(done: dict, now=None) -> bool:
    """0.41.1: 最近一次收盘定版 (zen_trade=completed) 是否已覆盖「最近一个应定版的交易日」。

    盘中重启触发的数据修复管道会跳过 zen_trade (regular_session_not_closed), 它的完成日期是今天,
    而上一次定版在昨天 → 旧判据按日期比较失败, 状态页误报「注意」(10-27 实例)。
    应定版日 = 今天已过盘后调度时刻则今天, 否则上一交易日; 定版运行须在该日 16:00 ET 之后开始。"""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import time as _t
    from datetime import timedelta as _tdl

    from app.services import preferences as _p
    now = now or us_clock.now()
    sched = _p.get_pipeline_schedule()
    today = now.date()
    due = today if us_clock.is_trading_day(today) and now.time() >= _t(sched["hour"], sched["minute"]) \
        else us_clock.previous_trading_day(today)
    started = _dt.fromisoformat(str(done.get('started_at')).replace('Z', '+00:00'))
    if started.tzinfo is None:
        started = started.replace(tzinfo=_UTC)
    return started >= _dt.combine(due, _t(16, 0), tzinfo=us_clock.tz) - _tdl(seconds=1)


@router.get('/system-status')
def system_status(request: Request):
    """系统状态: 每个部件的状态、一句话摘要和证据出处 (需求: 不制造黑箱)。只读。"""
    from app.services import decision_scan_job, evaluation_schedule, hot_quotes, premarket_gap, strategy_evaluation
    from app.services.alpaca_mirror import AlpacaMirror
    from app.services.earnings_calendar import EarningsCalendar
    from app.services.corporate_actions import CorporateActionService
    from app.services.notify_opportunity import notification_status
    data_dir = request.app.state.repo.store.data_dir
    loop = getattr(request.app.state, 'trading_loop', None)
    st = loop.status() if loop is not None else {}
    comps = []

    def add(cid, label, state, summary, source=''):
        comps.append({'id': cid, 'label': label, 'state': state, 'summary': summary, 'source': source})

    def safe(fn, default=None):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            return {'_error': f'{type(e).__name__}: {e}'[:160]} if default is None else default

    ex = st.get('execution_source') or {}
    add('execution_quotes', '纸面撮合报价', 'ok' if ex.get('active') == 'tickflow' else ('warn' if ex.get('active') else 'idle'),
        f"当前来源 {ex.get('active') or '—'}；IEX 交叉核对 {ex.get('cross_checked', 0)} 次、偏差拒用 {len(ex.get('deviations') or [])} 次"
        + (f"；主源错误 {ex.get('primary_error')}" if ex.get('primary_error') else ''), '/api/trading/status')
    hq = hot_quotes.status()
    add('hot_quotes', '5 秒热报价', 'error' if hq.get('error') else ('ok' if hq.get('at') else 'idle'),
        f"池 {hq.get('pool', 0)} 只，最近一次 {hq.get('count', 0)} 只"
        + (f"；错误 {_redact(hq['error'])}" + (f"（{_et_epoch(hq.get('error_at'))}）" if hq.get('error_at') else '')
           if hq.get('error') else ''),
        '持仓页 / 分时实时拼接')
    try:
        from app.services.preferences import get_realtime_data_provider
        rt = get_realtime_data_provider()
    except Exception:  # noqa: BLE001
        rt = ''
    add('realtime_source', '实时行情源', 'ok' if rt == 'tickflow' else 'warn', f"实时行情来源：{rt or '未配置'}",
        '设置 → 数据源')
    cb = st.get('circuit_breaker') or {}
    add('circuit_breaker', '熔断', 'warn' if cb.get('block_new_entries') or (cb.get('risk_multiplier') or 1) < 1 else 'ok',
        f"当日 {cb.get('day_change_pct', '—')}% · 回撤 {cb.get('drawdown_pct', '—')}% · "
        f"{'停新开仓' if cb.get('block_new_entries') else '允许开仓'} · 风险倍数 {cb.get('risk_multiplier', 1.0)}", 'data/circuit_breaker/')
    latest = safe(lambda: decision_scan_job.latest_run(data_dir), {}) or {}
    s = latest.get('summary') or {}
    add('decision_desk', '决策台', 'ok' if s else 'idle',
        f"{s.get('as_of', '—')}：候选 {s.get('candidates', 0)} · 开卡 {s.get('opened', 0)} · 只观察 {s.get('watched', 0)}",
        '机会页 → 来源管理')
    ev = safe(lambda: strategy_evaluation.load_evaluation(data_dir), {}) or {}
    srcs = ev.get('sources') or {}
    runs = safe(lambda: evaluation_schedule.last_runs(data_dir, 1), []) or []
    weekly = f"；最近周更 {_cn(runs[-1].get('status'))}" if runs else '；周更：每周六 10:00 ET'
    mode = safe(lambda: strategy_evaluation.evidence_mode(data_dir), '') or ''
    if mode == 'v4':
        add('evidence', '统一算法证据', *_v4_evidence_row(data_dir, ev, weekly))
    else:
        pos = sum(1 for v in srcs.values() if (v.get('ci_low') or -1) > 0 and (v.get('n') or 0) >= 30)
        src_file = {'v3': 'evaluation_v3.json', 'v2': 'evaluation_v2.json'}.get(mode, 'evaluation.json')
        add('evidence', '统一算法证据', 'ok' if srcs else 'warn',
            f"{len(srcs)} 个来源，{pos} 个证据为正；窗口 {' ~ '.join(ev.get('window') or ['—'])}；生成于 {ev.get('generated_at') or '—'}"
            + weekly, f'data/decision_desk/{src_file}')
    mir = safe(lambda: AlpacaMirror(data_dir).summary(), {}) or {}
    add('alpaca_mirror', 'Alpaca 纸面镜像', 'error' if mir.get('last_error') else ('ok' if mir.get('enabled') else 'warn'),
        f"{'运行中' if mir.get('enabled') else '已停用'} · 镜像 {mir.get('orders', 0)} 笔 · 成交 {mir.get('filled', 0)} 笔 · "
        f"平均偏差 {mir.get('avg_deviation_bps') if mir.get('avg_deviation_bps') is not None else '—'} 基点", '持仓页')
    pg = safe(lambda: premarket_gap.latest(data_dir), {}) or {}
    add('premarket_gap', '盘前跳空预警', 'ok' if pg else 'idle',
        f"最近 {pg.get('day', '—')}：检查 {pg.get('checked', 0)} 只，触发 {len(pg.get('alerts') or [])} 只" if pg else '美东 09:00 首次运行后显示',
        '/api/trading/premarket-gap')
    from app.services import intraday_strength
    ist = safe(lambda: intraday_strength.status(data_dir), {}) or {}
    _ist_unres = ist.get('unresolved', ist.get('errors'))
    add('intraday_strength', '盘中强势卡', 'warn' if _ist_unres else ('ok' if ist.get('checks') else 'idle'),
        f"今天 {ist.get('day', '—')}：检查 {ist.get('checks', 0)} 次，出卡 {ist.get('fired', 0)} 张，在途 {ist.get('open', 0)} 张"
        + ((f"，取数失败 {ist.get('errors')} 次（最近 {ist.get('last_error')} ET，下个检查点自动重试）" if _ist_unres else
            f"，取数失败 {ist.get('errors')} 次（最近 {ist.get('last_error')} ET，{str(ist.get('recovered_at') or '')[11:16]} ET 已恢复）")
           if ist.get('errors') else '')
        + "（10:00–15:00 ET 每 30 分钟；只记录不下单）", '/api/opportunities/intraday')
    from app.services import open_radar
    orr = safe(lambda: open_radar.status(data_dir), {}) or {}
    add('open_radar', '开盘放量榜', 'warn' if orr.get('errors') and not orr.get('ready') and not orr.get('ran')
        else ('ok' if orr.get('ready') or orr.get('ran') else 'idle'),
        (f"今天 {orr.get('day', '—')}：{orr.get('eligible', 0)} 只有资格，点名 {'、'.join(orr.get('picks') or []) or '无'}"
         if orr.get('ready') or orr.get('ran') else f"今天 {orr.get('day', '—')}：未出榜" + (f"，取数失败 {orr.get('errors')} 次" if orr.get('errors') else ''))
        + (f"；推送 {'、'.join(orr['pushed'])}" if orr.get('pushed') else '')
        + "（09:51 ET 出榜，只点名不下单；外部推送"
        + (f"开 · 相对量 ≥{(orr.get('push') or {}).get('min_rvol', 3):g}x）" if (orr.get('push') or {}).get('enabled', True) else "关）"),
        '/api/opportunities/page')
    from app.services import opp_radar
    rf = safe(lambda: opp_radar.status(data_dir), {}) or {}
    add('opp_radar', '机会页雷达', 'ok' if rf.get('frames') else 'idle',
        (f"今天 {rf.get('day', '—')}：{rf.get('frames', 0)} 帧，最近 {str(rf.get('last_at') or '')[11:19]} ET，推送 {rf.get('pushed', 0)} 次"
         if rf.get('frames') else f"今天 {rf.get('day', '—')}：{'等待首帧' if rf.get('window') else '不在记录时段'}")
        + "（交易日北京 20:00 起至收盘后 10 分钟，15 秒探测，状态变化即推送；只展示）",
        'data/opportunities/radar_frames/')
    ns = safe(lambda: notification_status(data_dir), {}) or {}
    _nc = ns.get('counts') or {}
    # 0.35.3: 以「最近一次成功发送之后还有没有失败」判通道 (unresolved_failed); 更早的失败只在摘要里留痕
    _unres = ns.get('unresolved_failed', _nc.get('failed', 0))
    _old_failed = (_nc.get('failed') or 0) - (_unres or 0)
    add('notifications', '飞书推送', 'ok' if ns.get('configured') and not _unres else 'warn',
        f"{ns.get('label', '—')} · " + ' · '.join(f"{(ns.get('labels') or {}).get(k, k)} {v}" for k, v in _nc.items())
        + (f"（其中 {_old_failed} 条失败早于最近一次成功发送 {_et_epoch(ns.get('last_sent_at'))}，通道已恢复；过期提醒不补发）"
           if _old_failed > 0 and not _unres else ''),
        'data/notifications/outbox.sqlite3')
    er = safe(lambda: EarningsCalendar(data_dir).status(), {}) or {}
    add('earnings', '财报保护', 'ok' if er.get('status') == 'connected' else 'error',
        f"{_cn(er.get('status'))} · 来源 {er.get('source') or '—'} · 更新 {_et(er.get('fetched_at'))}"
        f" · 按交易日 08:30 / 17:00 ET 刷新，须覆盖最近应刷新时点 {_et(er.get('due_at'))}（宽限 60 分钟，周末/休市不过期）", '持仓页')
    ca = safe(lambda: CorporateActionService(data_dir).status(us_clock.now()), {}) or {}
    add('corporate_actions', '公司行动保护', 'ok' if ca.get('status') == 'connected' else 'error',
        f"{_cn(ca.get('status'))} · 覆盖 {len(ca.get('symbols') or [])} 只 · 更新 {_et(ca.get('fetched_at'))}"
        f" · 按交易日 08:00 ET 刷新，须覆盖最近应刷新时点 {_et(ca.get('due_at'))}（宽限 60 分钟，周末/休市不过期）", '持仓页')
    from app.services import news_store
    nw = safe(lambda: news_store.status(data_dir), {}) or {}
    tr = nw.get('translate') or {}
    ing = nw.get('ingest') or {}
    nw_state = ('idle' if not nw.get('last_run_at') else
                'error' if nw.get('last_error') and str(nw.get('last_error_at') or '') >= str(nw.get('last_run_at') or '') else
                'warn' if nw.get('ingest_errors') or _news_translate_failing(nw) else 'ok')
    add('news', '个股新闻', nw_state,
        (f"最近 {_et(nw.get('last_run_at'))} · 覆盖 {nw.get('universe', 0)} 只 · "
         f"24h {nw.get('events_24h', 0)} 条（已译 {nw.get('translated_24h', 0)}，已打标 {nw.get('tagged_24h', 0)}）"
         f" · 本轮新增 {ing.get('added', 0)} · 待打标 {tr.get('pending_after', 0)}"
         + (f"；源错误 {len(nw.get('ingest_errors') or [])} 项" if nw.get('ingest_errors') else '')
         + (f"；翻译连续 {nw.get('translate_fail_streak')} 轮有失败批次" if _news_translate_failing(nw)
            else f"；本轮翻译 {tr.get('failed_batches')} 批失败，下一轮自动重译" if tr.get('failed_batches') else ''))
        if nw.get('last_run_at') else '首次采集后显示（每分钟唤醒，盘前盘中每 5 分钟）', '/api/news/status')
    from app.services import qveris_data, qveris_gate
    qv = safe(lambda: qveris_gate.usage(data_dir), {}) or {}
    qa = safe(lambda: qveris_data.auto_state(data_dir), {}) or {}
    if qv.get('configured'):
        bal = (qv.get('balance') or {})
        near_cap = qv.get('spent_today', 0) >= 0.9 * (qv.get('daily_limit') or 0) > 0
        # 0.22.5: 上限/失败只看「今天 (美东) 那次自动运行」; 往日旧事件不再一直挂告警
        try:
            qa_today = bool(qa.get('at')) and us_clock.market_datetime(str(qa['at'])).date() == us_clock.today()
        except (TypeError, ValueError):
            qa_today = False
        qv_state = 'error' if bal.get('error') else ('warn' if near_cap or (qa_today and (qa.get('blocked') or qa.get('failed'))) else 'ok')
        add('qveris', 'QVeris 付费数据', qv_state,
            f"余额 {bal.get('remaining') if bal.get('remaining') is not None else '—'} 积分 · 今日 {qv.get('spent_today', 0):g}/{qv.get('daily_limit', 0):g}"
            + (f" · 自动财报预期 {_et(qa.get('at'))}：目标 {qa.get('targets', 0)}，新取 {len(qa.get('fetched') or [])}，缓存 {len(qa.get('cached') or [])}"
               if qa else ' · 自动财报预期：美东 08:45 首次运行后显示')
            # bughunt-1015: 拒绝/失败文案也只对今天那次运行显示 (往日的「今日积分已用 100/100…已拒绝」与本行「今日 0/150」矛盾)
            + (f"；{qa['blocked']}" if qa_today and qa.get('blocked') else '')
            + (f"；失败 {len(qa['failed'])}" if qa_today and qa.get('failed') else '')
            + (f"；余额查询失败 {bal['error']}" if bal.get('error') else ''), '/api/qveris/usage')
    else:
        add('qveris', 'QVeris 付费数据', 'idle', '未配置 Key（设置 → 数据源）', '/api/qveris/usage')
    from app.services import fundamentals_sync as _fs
    _fst, _fsum = safe(lambda: _fs.status_component(data_dir), ('error', '读取同步状态失败'))
    add('fundamentals', '美股财务同步', _fst, _fsum, 'data/financials/_last_run.json')
    add('paper_loop', '纸面交易员', 'error' if st.get('last_error') else ('ok' if st.get('booted') else 'idle'),
        f"最近运行 {_et(st.get('last_run_at'))} · 报价 {st.get('last_quote_count', 0)} 只 · 入场拦截 {len(st.get('entry_blockers') or [])} 条"
        + (f"；错误 {_redact(st['last_error'])}" if st.get('last_error') else ''), '/api/trading/status')
    # 盘后管道最近一次 (job_store): 失败必须可见 (0.22.3; 此前吞异常 + jobs_run 假 ok)
    from app.services.pipeline_jobs import job_store as _job_store
    _pj = None
    for _j in safe(lambda: _job_store.list_recent(40), []) or []:
        _res = _j.get('result') or {}
        _stages = {e.get('stage') for e in (_j.get('log') or []) if isinstance(e, dict)}
        if 'daily_days' in _res or _stages & {'sync_daily', 'zen_trade', 'compute_enriched', 'sync_adj'}:
            _pj = _j
            break
    # 0.22.5: 同一盘后之后又跑了一次 (开机 catchup 等) 且 zen_trade 被跳过 → 以最近一次完成的 zen_trade 为准
    _done = None
    if _pj is not None and ((_pj.get('result') or {}).get('zen_trade') or {}).get('status') == 'skipped':
        for _j in safe(lambda: _job_store.list_recent(40), []) or []:
            _z = ((_j.get('result') or {}).get('zen_trade') or {})
            if _j.get('status') == 'succeeded' and _z.get('status') == 'completed':
                _done = _j
                break
    if _done is not None and (str(_done.get('finished_at') or '')[:10] >= str(_pj.get('finished_at') or '')[:10]
                              or safe(lambda: _pipeline_session_finalized(_done), False)):
        _zd = (_done.get('result') or {}).get('zen_trade') or {}
        _why = ('盘中数据修复，不做收盘定版，正常' if ((_pj.get('result') or {}).get('zen_trade') or {}).get('reason')
                == 'regular_session_not_closed' else '重复运行已跳过，正常')
        add('daily_pipeline', '盘后管道', 'ok',
            f"最近完成 {_et(_done.get('finished_at'))} · 卡 {_zd.get('cards', '—')} · 观察 {_zd.get('watch', '—')}"
            f"（之后 {_et(_pj.get('finished_at'))} 的运行为{_why}）",
            f"data/job_store/{_done.get('id')}.json")
    elif _pj is None:
        add('daily_pipeline', '盘后管道', 'idle', '尚无盘后管道记录；交易日 16:35 ET 自动跑', 'data/job_store/')
    elif _pj.get('status') == 'succeeded':
        _zen = ((_pj.get('result') or {}).get('zen_trade') or {})
        add('daily_pipeline', '盘后管道', 'ok' if _zen.get('status') == 'completed' else 'warn',
            f"最近成功 {_et(_pj.get('finished_at'))} · zen_trade={_zen.get('status') or '—'}"
            + (f" · 卡 {_zen.get('cards', '—')} · 观察 {_zen.get('watch', '—')}" if _zen else ''),
            f"data/job_store/{_pj.get('id')}.json")
    elif _pj.get('status') in ('running', 'pending'):
        add('daily_pipeline', '盘后管道', 'warn',
            f"进行中 stage={_pj.get('stage')} · {_pj.get('progress')}%",
            f"data/job_store/{_pj.get('id')}.json")
    else:
        add('daily_pipeline', '盘后管道', 'error',
            f"最近失败 {_et(_pj.get('finished_at'))} · stage={_pj.get('stage')} · {_redact(_pj.get('error') or '未知错误')}",
            f"data/job_store/{_pj.get('id')}.json")
    from app.services import data_freshness as _dfr
    def _fresh():
        from app.services import watchlist as _wl
        return _dfr.check(data_dir, request.app.state.repo, [e.get('symbol') for e in _wl.list_symbols()],
                          today=us_clock.today())
    _fr = safe(_fresh, {}) or {}
    if _fr.get('_error'):
        add('data_freshness', '自选数据新鲜度', 'error', f"检查失败 {_redact(_fr['_error'])}", 'kline_daily · kline_4h_src/m30')
    else:
        _fs_state, _fs_sum = _dfr.summary(_fr)
        add('data_freshness', '自选数据新鲜度', _fs_state, _fs_sum, 'kline_daily · kline_4h_src/m30')
    from app.services import data_quality
    dq = safe(lambda: data_quality.latest(data_dir), {}) or {}
    if dq.get('day'):
        add('data_quality', '日K数据质量', 'ok' if dq.get('ok') else 'error', data_quality.summary(dq),
            'data/ops/data_quality.json')
    else:
        add('data_quality', '日K数据质量', 'idle', '盘后管道成功后自动检查', 'data/ops/data_quality.json')
    sched = getattr(request.app.state, 'scheduler', None)
    from app.services import ops_watch
    ops = safe(lambda: ops_watch.status(data_dir, sched, loop), {})
    if ops.get('trading_day'):
        bad = [c for c in ops.get('checklist', []) if c['state'] in ('missing', 'error')]
        probs = ops.get('problems') or []
        okn = sum(1 for c in ops.get('checklist', []) if c['state'] == 'ok')
        add('ops_watch', '盘中盯盘', 'error' if (bad or probs) else 'ok',
            f"今日应跑 {len(ops.get('checklist', []))} 项，已完成 {okn}"
            + (''.join(f"；{c['label']}{'未运行' if c['state'] == 'missing' else '失败'}" for c in bad))
            + (''.join(f"；{ops_watch.REASON_LABEL.get(p['reason'], p['reason'])}" for p in probs)),
            'data/ops/jobs_run.jsonl')
    else:
        add('ops_watch', '盘中盯盘', 'idle', '非交易日', 'data/ops/jobs_run.jsonl')
    jobs = []
    if sched is not None:
        for j in sched.get_jobs():
            if str(j.id).startswith(('zen_', 'daily_pipeline', 'pre_market')):
                nrt = getattr(j, 'next_run_time', None)
                jobs.append({'id': j.id, 'next_run': nrt.astimezone(us_clock.tz).isoformat() if nrt else None})  # 统一美东
    from app.services import system_status_view
    now = us_clock.now()
    jobs = sorted(jobs, key=lambda j: j['id'])
    mk = system_status_view.enrich(comps, jobs, now=now, loop_status=st, ops=ops if isinstance(ops, dict) else {},
                                   dq=dq or None, pg=pg or None)
    return {'components': comps, 'jobs': jobs, 'market': mk, 'at': now.isoformat()}


class AccountResetIn(BaseModel):
    confirm: str = Field(..., pattern='^RESET$')
    note: str = Field('', max_length=200)


@router.post('/account/reset')
def account_reset(body: AccountResetIn, request: Request):
    """重置纸面账户: 归档旧台账/卡册/熔断, 新台账同参数从初始权益起步; Alpaca 镜像换前缀并平旧仓。交易时段拒绝。"""
    from app.services import account_reset as ar
    loop = getattr(request.app.state, 'trading_loop', None)
    if loop is None:
        raise HTTPException(503, '交易编排未初始化')
    try:
        return ar.reset(request.app.state.repo.store.data_dir, loop, note=body.note)
    except ar.BrokerFlattenError as e:
        raise HTTPException(502, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None


@router.get('/funnel')
def entry_funnel_view(request: Request, days: int = 10):
    """准入漏斗 (只读): 最近 N 个交易日 激活 → 触价 → 被拦 → 成交, 回补成交单列。"""
    from app.services import entry_funnel
    return entry_funnel.summarize(request.app.state.repo.store.data_dir, days=max(1, min(days, 60)))


@router.get('/mood-breakdown')
def mood_breakdown_view(request: Request):
    """已闭合实时回合按开仓日盘前大盘情绪 / 当日实际走势分组 (只读)。"""
    import json as _json

    from app.review.review import build_round_trips, mood_breakdown
    data_dir = request.app.state.repo.store.data_dir
    p = data_dir / 'ledger' / 'events.jsonl'
    events = [_json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()] if p.exists() else []
    trips = build_round_trips(events)
    return {'trips': sum(1 for t in trips if not t.backfill), 'backfill_excluded': sum(1 for t in trips if t.backfill),
            **mood_breakdown(data_dir, trips)}
