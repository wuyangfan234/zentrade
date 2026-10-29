"""机会 API — /api/opportunities (P3)。

端点:
  GET /api/opportunities/today     今日机会卡 (含 watch)
  GET /api/opportunities/history   历史开卡日清单 + 指定日
  GET /api/opportunities/state     状态总览 (活跃卡/持仓联动)
"""
from __future__ import annotations

from pathlib import Path

import logging
from datetime import date
from app.market_clock import us_clock

from fastapi import APIRouter, HTTPException, Query, Request

from app.services.opportunity_service import OpportunityService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/opportunities", tags=["opportunities"])


def _svc(request: Request) -> OpportunityService:
    svc = getattr(request.app.state, "opportunity_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="机会服务未初始化")
    return svc


@router.get("/today")
def today_cards(request: Request) -> dict:
    """今日机会卡 (含 watch 降级)。无当日数据 → 最近一个开卡日。"""
    svc = _svc(request)
    today = us_clock.today()
    payload = svc.load_cards(today)
    if payload is None:
        days = svc.list_card_days()
        if not days:
            return {"as_of": None, "gate": None, "cards": [], "watch": [],
                    "note": "尚未开卡 (盘后管线未跑或无数据)"}
        payload = svc.load_cards(date.fromisoformat(days[-1]))
    assert payload is not None
    return payload


@router.get("/history")
def history(request: Request, day: str | None = Query(None)) -> dict:
    """历史: 不带 day 返回开卡日清单; 带 day 返回当日全部卡。"""
    svc = _svc(request)
    if day is None:
        return {"days": svc.list_card_days()}
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD") from None
    payload = svc.load_cards(d)
    if payload is None:
        raise HTTPException(404, f"{day} 无开卡记录")
    return payload


@router.get("/state")
def state(request: Request) -> dict:
    """状态总览: 最近开卡日的活跃卡 + 台账持仓联动。"""
    svc = _svc(request)
    days = svc.list_card_days()
    latest = svc.load_cards(date.fromisoformat(days[-1])) if days else None
    active = [c.to_dict() for c in svc.all_cards(us_clock.today()) if c.state in ("open", "active", "filled")]
    return {
        "latest_card_day": days[-1] if days else None,
        "active_count": len(active),
        "active": active,
    }


# ── 决策台: 来源证据 / 人工管理 / 最近一次运行 (2026-10-02) ─────────────

from pydantic import BaseModel, Field  # noqa: E402


class SourceStatusIn(BaseModel):
    status: str = Field(..., pattern="^(enabled|observe|disabled)$")
    note: str = Field("", max_length=200)


class SourceCapIn(BaseModel):
    cap: float | None = Field(None, ge=0)
    note: str = Field("", max_length=200)


def _desk_registry(request: Request):
    from app.services import decision_desk as dd
    engine = getattr(request.app.state, "strategy_engine", None)
    ids = {m["id"] for m in engine.list_strategies() if not m.get("research_only")} if engine else set()
    reg = dd.SourceRegistry(request.app.state.repo.store.data_dir, builtin=set(dd.BUILTIN_SETUPS))
    reg.seed(ids | set(dd.BUILTIN_SETUPS))
    return reg, ids, engine


@router.get("/desk/sources")
def desk_sources(request: Request) -> dict:
    """每个候选来源: 名称、状态 (启用/只观察/停用)、统一口径证据、当前权重与原因、人工上限、最近选股数。"""
    from app.services import decision_desk as dd
    from app.services import decision_scan_job, strategy_evaluation
    data_dir = request.app.state.repo.store.data_dir
    reg, ids, engine = _desk_registry(request)
    evaluation = strategy_evaluation.load_evaluation(data_dir) or {}
    ev = evaluation.get("sources", {})
    latest = decision_scan_job.latest_run(data_dir) or {}
    by_source = (latest.get("summary") or {}).get("by_source", {})
    names = {m["id"]: m.get("name", m["id"]) for m in engine.list_strategies()} if engine else {}
    names.update({"breakout": "S1 突破形态", "pullback": "S2 回踩形态"})
    from app import optional
    phantom_4h_source = optional.load("app.services.phantom_4h_source")   # 可选来源 (开源核心版不含): 有才列出
    if phantom_4h_source is not None:
        names[phantom_4h_source.SOURCE_ID] = phantom_4h_source.SOURCE_NAME
        names[phantom_4h_source.SHORT_SOURCE_ID] = phantom_4h_source.SHORT_SOURCE_NAME
        names[phantom_4h_source.PINE_SOURCE_ID] = phantom_4h_source.PINE_SOURCE_NAME
        ids = set(ids) | {phantom_4h_source.SOURCE_ID, phantom_4h_source.SHORT_SOURCE_ID, phantom_4h_source.PINE_SOURCE_ID}
    rows = []
    for sid in sorted(ids | set(dd.BUILTIN_SETUPS) | set(ev)):
        w = reg.effective(sid, dd.source_weight(ev.get(sid)))
        e = ev.get(sid) or {}
        rows.append({"id": sid, "name": names.get(sid, sid), "status": reg.status(sid), "manual": reg.row(sid),
                     "tier": w.get("tier"), "weight": w.get("weight"), "risk_pct": w.get("risk_pct"), "reason": w.get("reason"),
                     "evidence": {k: e.get(k) for k in ("n", "mean", "ci_low", "ci_high", "win_rate", "profit_factor", "days",
                                                        "method", "error")} if e else None,
                     "by_bucket": e.get("by_bucket"), "exit_reasons": e.get("exit_reasons"),
                     "latest": by_source.get(sid)})
    rank = {"proven": 0, "explore": 1, "negative": 2}
    rows.sort(key=lambda r: (r["status"] != "enabled", rank.get(r["tier"], 9), -(r["weight"] or 0)))
    # 新闻催化 (2026-10-02 产品决策): 固定只观察, 不进排序、不开仓; 只展示影子证据进度
    try:
        from app.services import news_shadow
        nev = news_shadow.evidence(data_dir)
        rows.append({"id": news_shadow.SOURCE_ID, "name": news_shadow.SOURCE_NAME, "status": "observe",
                     "manual": {"status": "observe", "locked": True}, "tier": None, "weight": None, "risk_pct": None,
                     "reason": nev["verdict"], "evidence": None, "by_bucket": None, "exit_reasons": None, "latest": None,
                     "news_shadow": nev})
    except Exception as e:  # noqa: BLE001
        logger.warning("news shadow evidence unavailable: %s", type(e).__name__)
    return {"sources": rows, "evaluation": {k: evaluation.get(k) for k in ("generated_at", "window", "universe", "rules", "picks_per_day")},
            "audit": reg.audit()[-30:],
            "rules": {"weight": "统一 7 日纪律重放的平均 R，按入场日聚类 95% 置信下界；样本越少越向 0 收缩；每周更新、单次变化 ≤50%",
                      "negative": "样本 ≥30 且置信下界 ≤0：权重 0，只展示不开仓",
                      "explore": "样本 <30：探索额度（单笔风险 0.25%，每天最多 1 张）",
                      "manual": "新来源默认“只观察”；启用 / 停用 / 权重上限由你手动决定，每次操作记审计"}}


@router.post("/desk/sources/{source_id}/status")
def desk_set_status(source_id: str, body: SourceStatusIn, request: Request) -> dict:
    reg, ids, _ = _desk_registry(request)
    from app.services import decision_desk as dd
    from app.services import strategy_evaluation
    known = ids | set(dd.BUILTIN_SETUPS) | set((strategy_evaluation.load_evaluation(request.app.state.repo.store.data_dir) or {}).get("sources", {}))
    if source_id not in known:
        raise HTTPException(404, f"未知来源 {source_id}")
    return {"ok": True, "source": source_id, **reg.set_status(source_id, body.status, body.note)}


@router.post("/desk/sources/{source_id}/cap")
def desk_set_cap(source_id: str, body: SourceCapIn, request: Request) -> dict:
    reg, ids, _ = _desk_registry(request)
    from app.services import decision_desk as dd
    if source_id not in ids | set(dd.BUILTIN_SETUPS):
        raise HTTPException(404, f"未知来源 {source_id}")
    return {"ok": True, "source": source_id, **reg.set_cap(source_id, body.cap, body.note)}


class EvidenceModeIn(BaseModel):
    mode: str = Field(..., pattern="^(v1|v2|v3|v4)$")
    note: str = Field("", max_length=200)


@router.get("/desk/evidence")
def desk_evidence(request: Request) -> dict:
    """新旧证据并排 (拍板 3: 并行核对, 确认后再切换): 现行模式、v2 元数据、逐来源对照、许可是否会变。"""
    from app.services import evaluation_schedule, strategy_evaluation as se
    from app.services.strategy_evaluation_runner import compare_versions
    data_dir = request.app.state.repo.store.data_dir
    v1, v2 = se.load_evaluation_file(data_dir, "v1"), se.load_evaluation_file(data_dir, "v2")
    cmp = compare_versions(v1, v2) if v2 else {}
    current = se.current_fingerprints(data_dir, [k for k, v in cmp.items() if v.get("fingerprint")])
    for k, row in cmp.items():
        fp = row.get("fingerprint") or {}
        row["version"] = ("unverified" if not fp else "unknown" if current.get(k) is None
                          else "match" if current[k] == fp.get("hash") else "changed")
        row.pop("fingerprint", None)
    meta = lambda e: {k: (e or {}).get(k) for k in ("generated_at", "window", "rules", "contract", "elapsed_s")}  # noqa: E731
    v3 = se.load_evaluation_file(data_dir, "v3")
    return {"mode": se.evidence_mode(data_dir), "v1": meta(v1), "v2": meta(v2) if v2 else None, "comparison": cmp,
            "v2_job": evaluation_schedule.v2_status(), "v3_job": evaluation_schedule.v3_status(),
            "v3": _v3_view(data_dir, v3) if v3 else None,
            "permission_changes": sorted(k for k, v in cmp.items() if v.get("changes_permission"))}


def _v3_view(data_dir, v3: dict) -> dict:
    """来源体检板: 每个来源的 历史(v3.1) / 前向(影子账本) / 真实成交 三套成绩, 与随机挑选对比。"""
    from app.services import forward_confirmation, shadow_forward
    from app.services.event_sources import EVENT_SOURCES
    fwd = forward_confirmation.status(data_dir, v3)
    shadow = shadow_forward.summary(data_dir).get("sources", {})
    real = _real_by_source(data_dir)
    rows = []
    for sid, v in (v3.get("sources") or {}).items():
        if not isinstance(v, dict) or "excess" not in v:
            continue
        rows.append({"source": sid, "name": EVENT_SOURCES.get(sid), "kind": v.get("kind"), "n": v.get("n"),
                     "mean": v.get("mean"), "median": v.get("median"), "baseline": v.get("baseline_mean"),
                     "excess": v.get("excess"), "discovery": v.get("discovery"), "holdout": v.get("holdout"),
                     "by_year": v.get("by_year"), "concentration": v.get("concentration"),
                     "delisted_trades": v.get("delisted_trades"), "holm_pass": v.get("holm_pass"),
                     "tier": v.get("tier_v3"), "reason": v.get("reason_v3"), "forward": fwd.get(sid),
                     "shadow": shadow.get(sid), "real": real.get(sid)})
    rows.sort(key=lambda r: -((r.get("excess") or {}).get("mean") or -9))
    return {k: v3.get(k) for k in ("contract", "window", "generated_at", "holdout_start", "cluster_unit",
                                   "calibration", "with_delisted", "hypotheses_tested", "pool", "baseline")} | {
        "sources": rows, "forward_rule": {"start": str(forward_confirmation.FORWARD_START), "min_n": forward_confirmation.MIN_N,
                                          "min_months": forward_confirmation.MIN_MONTHS,
                                          "candidates": forward_confirmation.candidates(v3)},
        "baseline_shadow": shadow.get(shadow_forward.BASELINE_ID)}


def _real_by_source(data_dir) -> dict:
    """真实纸面成交按来源汇总: 已开回合数、已平回合数、已平回合平均 R。读失败返回空。"""
    try:
        from app.ledger.core import Ledger
        from app.review.review import build_round_states
        from app.services import decision_desk as dd
        events = Ledger(Path(data_dir) / "ledger", read_only=True).read_events()
        closed, opens = build_round_states(events)
        out: dict[str, dict] = {}
        for st in closed:
            trip = st.closed(st.exits[-1])
            o = out.setdefault(dd.source_of(trip.setup_type), {"opened": 0, "closed": 0, "r": []})
            o["opened"] += 1
            o["closed"] += 1
            if trip.r_multiple is not None:
                o["r"].append(trip.r_multiple)
        for st in opens.values():
            o = out.setdefault(dd.source_of(str(st.first.get("setup_type") or "unknown")), {"opened": 0, "closed": 0, "r": []})
            o["opened"] += 1
        return {k: {"opened": v["opened"], "closed": v["closed"],
                    "mean_r": round(sum(v["r"]) / len(v["r"]), 4) if v["r"] else None} for k, v in out.items()}
    except Exception:  # noqa: BLE001
        return {}


@router.post("/desk/evidence/mode")
def desk_evidence_mode(body: EvidenceModeIn, request: Request) -> dict:
    """切换许可证据版本。先写审计 (fsync) 再原子替换模式文件; 切到 v2 前必须已有成功的 v2 评估。"""
    import json as _json
    import os
    from datetime import datetime
    from app.services import decision_desk as dd, strategy_evaluation as se
    from app.services.fs_utils import atomic_write_text
    data_dir = request.app.state.repo.store.data_dir
    if body.mode in ("v2", "v3"):
        ev = se.load_evaluation_file(data_dir, body.mode)
        if not ev or not ev.get("generated_at") or not ev.get("evaluated"):
            raise HTTPException(409, f"还没有完成的 {body.mode} 评估，不能切换")
    reg = dd.SourceRegistry(data_dir)
    reg.dir.mkdir(parents=True, exist_ok=True)
    prev = se.evidence_mode(data_dir)
    with open(reg.audit_path, "a", encoding="utf-8") as f:
        f.write(_json.dumps({"action": "evidence_mode", "source": "*", "value": body.mode, "previous": prev,
                             "note": body.note, "at": datetime.now().astimezone().isoformat()}, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    atomic_write_text(se.evidence_mode_path(data_dir), _json.dumps({"mode": body.mode, "previous": prev,
                                                                   "at": datetime.now().astimezone().isoformat()}))
    return {"ok": True, "mode": se.evidence_mode(data_dir), "previous": prev}


@router.post("/desk/evidence/v3/run")
def desk_evidence_v3_run(request: Request) -> dict:
    """后台跑一次 v3.1 评估 (含研究数据增量刷新; 只写研究目录与并行文件)。交易时段内拒绝; 已在跑则不重复。"""
    import threading
    from app.services import evaluation_schedule
    if us_clock.in_session():
        raise HTTPException(409, "交易时段内不运行评估")
    if evaluation_schedule.v3_status().get("running"):
        return {"started": False, "reason": "已在运行"}
    state = request.app.state
    threading.Thread(target=evaluation_schedule.run_v3, args=(state,), name="evaluation-v3", daemon=True).start()
    return {"started": True}


@router.post("/desk/evidence/v2/run")
def desk_evidence_v2_run(request: Request) -> dict:
    """后台跑一次 v2 评估 (只写并行文件)。交易时段内拒绝; 已在跑则不重复。"""
    import threading
    from app.services import evaluation_schedule
    if us_clock.in_session():
        raise HTTPException(409, "交易时段内不运行评估")
    if evaluation_schedule.v2_status().get("running"):
        return {"started": False, "reason": "已在运行"}
    state = request.app.state
    threading.Thread(target=evaluation_schedule.run_v2, args=(state,), name="evaluation-v2", daemon=True).start()
    return {"started": True}


class RulesetIn(BaseModel):
    enabled: list[str] = Field(default_factory=list, max_length=200)
    short: list[str] | None = Field(None, max_length=50)
    evo: bool | None = None          # 0.15.0 自进化机会卡开关; None = 沿用当前版本
    params: dict = Field(default_factory=dict)
    note: str = Field("", max_length=200)


# 0.29.1: 旧机会卡 /cards、09:44 首选定格 /top-snapshot、旧自选表 /watchlist(/{symbol}) 退役 (前端 0.29.0 起零调用)。


@router.get("/swing-settlement")
def swing_settlement_table(request: Request) -> dict:
    """0.26.0 机会板卡结局 (只读): 每张上过板的卡的自动结局 (实际) vs 30 分钟K 回放基础率, 多空分开,
    每个数带随时有效 95% 区间; mismatch = 区间不重叠的项 (撮合模型要修)。另附最近 10 次每日对账。"""
    from app.services import swing_settlement as ss
    return ss.table(request.app.state.repo.store.data_dir)


@router.get("/settlement")
def settlement_table(request: Request) -> dict:
    """方向 A 卡结局表 (只读): 读 data/research/settlement/table.json; 缺文件 = 尚未结算, 不触发计算。"""
    from app.services import card_settlement as cs
    d = cs.read_table(request.app.state.repo.store.data_dir)
    if d is None:
        return {"available": False, "contract": cs.CONTRACT, "tables": None,
                "thresholds": {"gray_below": cs.N_GRAY, "color_from": cs.N_COLOR, "min_blocks": cs.MIN_BLOCKS, "min_age": cs.MIN_AGE}}
    return {"available": True, **d}


def _card_exit_reason(r: dict) -> str | None:
    """逐卡明细的退出原因: pass_signal 行 (信号口径) 写在 signal_exit_reason, 其余行读 exit_reason"""
    return r.get("signal_exit_reason") if r.get("record_kind") == "pass_signal" else r.get("exit_reason")


@router.get("/settlement/cards")
def settlement_cards(request: Request, rule: str | None = None, exit_reason: str | None = None) -> dict:
    """0.18.0 逐卡明细 (只读): 原样读 cards.zt-settle-v1.jsonl; 缺文件不触发结算、不建目录。total = 文件行数。"""
    import json as _json
    from app.services import card_settlement as cs
    f = cs.cards_path(request.app.state.repo.store.data_dir)
    if not f.exists():
        return {"available": False, "contract": cs.CONTRACT, "total": 0, "rows": [], "rules": [], "exit_reasons": []}
    rows = []
    with f.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(_json.loads(line))
                except ValueError:
                    logger.warning("settlement cards: bad line skipped")
    keep = [r for r in rows if (rule is None or r.get("rule") == rule)
            and (exit_reason is None or _card_exit_reason(r) == exit_reason)]
    keep.sort(key=lambda r: (r.get("signal_day") or "", r.get("symbol") or ""), reverse=True)
    return {"available": True, "contract": cs.CONTRACT, "total": len(rows), "rows": keep,
            "rules": sorted({r["rule"] for r in rows if r.get("rule")}),
            # pass_signal 行的退出原因在 signal_exit_reason, 一并列入筛选项
            "exit_reasons": sorted({x for x in map(_card_exit_reason, rows) if x})}



# 0.29.1: POST /swing/strategies 退役, swing.json 只经 POST /settings 写 (一个写口)。


@router.get("/intraday")
def intraday_board(request: Request) -> dict:
    """0.28.0 盘中强势卡 (只读, 不出网): 在途卡 + 已结束卡 + 自然战绩 + 回放成绩。只记录不下单。"""
    from app.services import desk_overview as ov
    from app.services import intraday_strength as ids
    return ov.to_json_safe(ids.build_live(request.app.state.repo.store.data_dir, repo=request.app.state.repo,
                                          loop=getattr(request.app.state, "trading_loop", None)))


@router.get("/page")
def opportunities_page(request: Request) -> dict:
    """0.29.0 机会页一次取齐 (只读): 机会板 + 盘中强势 + 大盘条 + 时段 + 板卡标注。某块失败只空那一块 (errors)。"""
    from app.services import desk_overview as ov
    from app.services import opp_page
    return ov.to_json_safe(opp_page.build(request.app.state.repo.store.data_dir, repo=request.app.state.repo,
                                          loop=getattr(request.app.state, "trading_loop", None)))


class RadarPushIn(BaseModel):
    """0.35.0 开盘放量榜飞书推送: 开关 + 门槛 (只收档位 1 / 2 / 3 / 5)。strict: 不把 "true" / "3" 宽松转换。"""
    enabled: bool = Field(..., strict=True)
    min_rvol: float = Field(..., strict=True)


class OppSettingsIn(BaseModel):
    long: list[str] = Field(default_factory=list, max_length=10)
    short: list[str] = Field(default_factory=list, max_length=10)
    # 审查 #5: strict = 只收 JSON 数字 (不把 true / "0.5" 宽松转成数字); int 也收 (1 = 1%)
    risk_per_trade_pct: float | None = Field(None, strict=True)
    note: str = Field("", max_length=200)
    radar_push: RadarPushIn | None = None


@router.post("/settings")
def opportunities_settings(body: OppSettingsIn, request: Request) -> dict:
    """0.29.0 规则齿轮唯一保存口: 3+3 + 单笔风险 (风险变了才建规则集新版本, 勾选原样沿用)。"""
    from app.services import opp_page
    risk = body.risk_per_trade_pct
    if risk is not None:
        if not (risk == risk and 0.1 <= risk <= 2.0):
            raise HTTPException(422, "risk_per_trade_pct must be within 0.1-2.0")
        risk = round(float(risk), 3)
    push = None
    if body.radar_push is not None:
        from app.services import open_radar
        if float(body.radar_push.min_rvol) not in open_radar.PUSH_LEVELS:
            raise HTTPException(422, f"radar_push.min_rvol must be one of {list(open_radar.PUSH_LEVELS)}")
        push = {"enabled": body.radar_push.enabled, "min_rvol": float(body.radar_push.min_rvol)}
    return opp_page.save_settings(request.app.state.repo.store.data_dir, long_ids=body.long, short_ids=body.short,
                                  risk_pct=risk, note=body.note, radar_push=push)


@router.get("/swing")
def swing_boards(request: Request) -> dict:
    """第一性原理机会板: LONG | SHORT (4h 门 + 3+3 打分)。只读最近自选信号快照。"""
    from app.services import desk_overview as ov
    from app.services import swing_desk as sd
    return ov.to_json_safe(sd.build_live(request.app.state.repo.store.data_dir, repo=request.app.state.repo,
                                         loop=getattr(request.app.state, "trading_loop", None)))


def _rule_history_for_panel() -> dict:
    """规则面板“回测 N 笔”(0.26.0): 机会板现在这张卡 (tight-v1) 的历史, 不再用旧宽卡 7 日回放。
    键 = 规则 id (做多) / <id>:short (卖出当做空)。只展示, 不进打分、排序与许可。"""
    from app.services import swing_desk as sd
    return dict(sd.rule_history().get("rules") or {})


def _rule_history_meta() -> dict:
    """0.30.1: 齿轮里回放数字的来源写成文件自己的元数据, 不再写死日期与名单 (重算后自动跟上)。
    文件读不到 = 空 dict, 前端退回「无来源」。"""
    from app.services import swing_desk as sd
    h = sd.rule_history()
    win = h.get("window") or []
    return {"window": [str(x) for x in win][:2] or None, "universe": h.get("universe"),
            "data_end": h.get("data_end"), "generated": h.get("generated")}


@router.get("/rules")
def rules_get(request: Request) -> dict:
    """规则控制台: 当前规则集 + 可选规则目录 (含每条规则的自选前向成绩) + 前向确认状态 + 版本列表。"""
    from app.services import swing_desk as sd
    from app.services import watchlist_table as wt
    from app.services import watchlist_v4 as wv
    data_dir = request.app.state.repo.store.data_dir
    pm = wv.permission(data_dir)
    rs, st = pm["ruleset"], pm["status"]
    per_rule = st.get("per_rule") or {}
    hist = _rule_history_for_panel()
    from app.services import swing_candidates as _sc
    cand_rules = _sc.read_candidates(data_dir).get("rules") or {}
    catalog = [{**r, "enabled": r["id"] in set(rs.get("enabled") or []), "forward": per_rule.get(r["id"]),
                "history": hist.get(r["id"])}
               for r in wt.rule_catalog(getattr(request.app.state, "strategy_engine", None))]
    from app.services import decision_desk as dd
    names = wt._names(getattr(request.app.state, "strategy_engine", None))
    disp = wv.display_rules(data_dir)
    for r in catalog:
        r["show"] = r["id"] in disp["long"] or r["enabled"]
        r["short_capable"] = wv.short_capable(r["id"])
        r["show_short"] = r["short_capable"] and r["id"] in disp["short"]
        r["history_short"] = hist.get(f"{r['id']}:short") if r["id"] in wv.SELL_IS_SHORT else None
        # 0.30.0: short list shows the short-side name the board cards use (META name is the long one, e.g. 翻多)
        r["name_short"] = sd.strategy_name(r["id"], "short") if r["short_capable"] else None
        # 0.26.0: 系统发现的策略按机会板卡回放赢过“4h 门内随便挑” -> 标候选 (只标记, 勾不勾由人决定)
        r["candidate"] = _candidate_view(cand_rules.get(r["id"]))
    # 空头规则 (生来做空): 不进看多规则集, 可在“看空”列勾选进 short 列表, 过 SPY 50 日线门槛后出卡下单
    short_rules = [{"id": sid, "name": names.get(sid, sid), "history": hist.get(sid), "show_short": sid in disp["short"]}
                   for sid in sorted(dd.SHORT_SOURCES)]
    return {"ruleset": rs, "catalog": catalog, "short_rules": short_rules, "versions": wv.versions(data_dir),
            "display": {"long": sorted(disp["long"]), "short": sorted(disp["short"]), "saved": disp["saved"]},
            "forward": {**{k: st.get(k) for k in ("n", "months", "excess", "abs_mean", "p", "threshold", "promoted",
                                                  "ledger_corrupt")},
                        "tradable_rules": sorted(pm["tradable"])},
            # 规则集勾选的看多规则触发即可下单, 风险 = 规则集 risk_per_trade_pct;
            # 做空 = 规则集 short 列表里的 short_capable 规则, 且信号日 SPY 收盘 < 50 日均线 (short_gate) 才出卡下单
            "trading": {"long": sorted(pm["tradable"]), "short": sorted(pm.get("short_tradable") or []),
                        "risk_pct": round((pm.get("risk_pct") or 0.0) * 100, 3)},
            "rule": {"start": str(wv.FORWARD_START), "min_n": wv.MIN_N, "min_months": wv.MIN_MONTHS},
            "evo": _evo_panel(data_dir, rs, names),
            "candidates": _sc.status(data_dir),
            "history_meta": _rule_history_meta(),
            "params_default": wv.DEFAULT_PARAMS}


def _candidate_view(cand) -> dict | None:
    """Card-replay check result for one discovered strategy. 0.30.0 also passes mean_r, and failed checks
    (error + detail) so the gear can say why a strategy has no number."""
    if not isinstance(cand, dict):
        return None
    if "passed" in cand:
        return {k: cand.get(k) for k in ("passed", "excess", "excess_ci", "n", "mean_r", "r_per_year",
                                         "base_r_per_year", "first_passed")}
    if cand.get("error"):
        return {"passed": False, "error": str(cand["error"]), "detail": str(cand.get("detail") or "")[:200]}
    return None


@router.post("/rules/candidates/run")
def rules_candidates_run(request: Request) -> dict:
    """0.30.0 gear button: re-run the card-replay check for mined / AI strategies. Off-hours it starts in the
    background; 09:00-17:00 ET on trading days it is queued and runs after the post-close pipeline.
    Writes only research/; trading, 3+3 and the ruleset are untouched."""
    from app.services import swing_candidates as sc
    repo = request.app.state.repo
    return sc.request_check(repo.store.data_dir, repo, reason="manual", if_idle=True)


def _evo_panel(data_dir, rs, names: dict | None = None) -> dict:
    """0.22+ 自进化已退役: 不再算权重/出卡; 仅返回 retired 标记 (兼容旧前端字段)。"""
    _ = (data_dir, names)
    return {"retired": True, "on": False, "as_of": None, "hold_days": 5,
            "train": None, "bear": None, "index_close": None, "index_ma50": None, "index_ma20": None,
            "short_gate": None, "long_picks": None, "long": [], "short": [], "cards": []}


@router.post("/rules")
def rules_save(body: RulesetIn, request: Request) -> dict:
    """保存为新版本 (冻结); 与当前相同则不建新版本。只允许目录里存在的规则。"""
    from app.services import watchlist_table as wt
    from app.services import watchlist_v4 as wv
    known = {r["id"] for r in wt.rule_catalog(getattr(request.app.state, "strategy_engine", None))}
    bad = sorted(set(body.enabled) - known)
    if bad:
        raise HTTPException(422, f"未知规则: {', '.join(bad[:5])}")
    if body.short is not None:
        bad_short = sorted({x for x in body.short if not wv.short_capable(x)})
        if bad_short:
            raise HTTPException(422, f"不能做空的规则: {', '.join(bad_short[:5])}")
    params = {}
    if "risk_per_trade_pct" in (body.params or {}):
        try:
            v = float(body.params["risk_per_trade_pct"])
        except (TypeError, ValueError):
            raise HTTPException(422, "risk_per_trade_pct 不是数字") from None
        if not (v == v and 0.1 <= v <= 2.0):
            raise HTTPException(422, "risk_per_trade_pct 需在 0.1–2.0")
        params["risk_per_trade_pct"] = round(v, 3)
    return wv.save_ruleset(request.app.state.repo.store.data_dir, body.enabled, params, body.note, short=body.short,
                           evo=body.evo)


# 0.12.0: POST /rules/display 下线 —— 显示勾选早已不参与出卡 (卡只按规则集出), 面板改只读; display.json 保留存档。


@router.get("/desk/overview")
def desk_overview(request: Request) -> dict:
    """机会页总览 (只读): 能不能出手 / 跨日在窗可执行卡 + 卡在哪 / 账户容量 / 自选总表。"""
    from app.services import desk_overview as ov
    svc = _svc(request)
    out = ov.build(svc=svc, repo=request.app.state.repo, loop=getattr(request.app.state, "trading_loop", None),
                   engine=getattr(request.app.state, "strategy_engine", None),
                   data_dir=request.app.state.repo.store.data_dir)
    return ov.to_json_safe(out)


@router.get("/desk/latest")
def desk_latest(request: Request) -> dict:
    from app.services import decision_scan_job
    return decision_scan_job.latest_run(request.app.state.repo.store.data_dir) or {"summary": None, "ranked": []}


@router.post("/desk/rescan")
def desk_rescan(request: Request) -> dict:
    """在应用进程内对最近开卡日重跑盘后扫描 (决策台)。台账单写者: 必须在本进程执行, 不允许外部脚本写台账。

    只在非交易时段、且该日官方收盘已确认时允许; 已存在的提案保留 (save_scan 既有提案优先),
    决策台新增的卡加入当日卡; 旧来源证据为负的卡在入场前被拦截。不重复推送当日机会。
    """
    loop = getattr(request.app.state, "trading_loop", None)
    if loop is None:
        raise HTTPException(503, "交易编排未初始化")
    if us_clock.in_session():
        raise HTTPException(409, "交易时段内不重跑盘后扫描")
    days = loop.opp_svc.list_card_days()
    if not days:
        raise HTTPException(404, "尚无开卡日")
    day = date.fromisoformat(days[-1])
    qs = getattr(request.app.state, "quote_service", None)
    if qs is None or not qs.is_official_close(day):
        raise HTTPException(409, f"{day} 官方收盘未确认")
    result = loop.opp_svc.run_daily_scan(day)
    loop._sync_card_book()
    return {"day": day.isoformat(), "cards": len(result.cards), "watch": len(result.watch),
            "desk": getattr(loop.opp_svc, "desk_summary", None)}


@router.get("/market-mood")
def market_mood_view(request: Request):
    """盘前大盘情绪 (只读): 最新判断 + 是否今日 + 是否最新 + 上一日结果 + 近 20 日命中。"""
    from app.services import market_mood
    return market_mood.view(request.app.state.repo.store.data_dir)
