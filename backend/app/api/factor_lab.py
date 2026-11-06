"""因子发现 + 公开采集 API — /api/factor-lab (0.47.0)。

  GET  /api/factor-lab/page                       发现页聚合 (本周候选 + 当前出卡引擎对照 + 近 4 周 + 采集概览; 只读文件)
  POST /api/factor-lab/candidates/decide          候选 保存 / 放弃 / 撤销 (保存 = 进候选方案的待定项; 不发布)
  POST /api/factor-lab/recalc                     单个候选 / 当前出卡引擎在自定股票池或区间上重算 (不落盘; 盘中拒绝)
  POST /api/factor-lab/run                        立即跑一次周度发现 (按最近一次周度挖掘; 盘中排到收盘后)
  POST /api/factor-lab/intake/run                 立即采集一轮 (抓取 + 模型 + 快筛)
  POST /api/factor-lab/intake/items/{id}/merge    采集公式并入因子库 (状态 = 观察, 下周五进入筛因子)
  POST /api/factor-lab/intake/items/{id}/ignore   忽略
  POST /api/factor-lab/intake/items/{id}/retry    失败重试 (不占每日模型额度)
  POST /api/factor-lab/intake/items/{id}/restore  恢复已归档条目
  POST /api/factor-lab/intake/submit              投递 (链接 / 正文 / Pine / 公式)
  POST /api/factor-lab/intake/sources             添加来源 (只允许公网 http(s))
  POST /api/factor-lab/intake/sources/{id}        开关来源
  DELETE /api/factor-lab/intake/sources/{id}      删除自建来源 (内置只能关)
  POST /api/factor-lab/intake/settings            每日模型调用上限
  GET  /api/factor-lab/rankers                    机会板排序因子: 候选池状态 + 已导入的排序因子 + ZenTrade 复核结果 (0.52.0)
  POST /api/factor-lab/rankers/import             导入一条排序因子 (研究投递密钥可调; 导入 ≠ 启用, 只写 research/rank_lab/)
  POST /api/factor-lab/rankers/{id}/remove        移除 (正在机会板做多 3 条里的拒绝)
  POST /api/factor-lab/rankers/rebuild            立即重导候选池并复核 (盘中拒绝; 平时每个交易日 17:40 ET 自动)
"""
from __future__ import annotations

import threading
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import factor_discover, factor_intake

router = APIRouter(prefix="/api/factor-lab", tags=["factor-lab"])


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


def _repo(request: Request):
    return request.app.state.repo


@router.get("/page")
def page(request: Request) -> dict:
    return factor_discover.page(_data_dir(request))


class DecideIn(BaseModel):
    week: str = Field(..., min_length=4, max_length=16)
    signature: str = Field(..., min_length=1, max_length=400)
    action: str = Field(..., pattern="^(save|discard|reset)$")


def _promoter(request: Request):
    from app.backtest.candidates import CandidateStore
    from app.services.mining_candidates import MiningCandidateService

    manager = getattr(request.app.state, "mining_manager", None)
    engine = getattr(request.app.state, "strategy_engine", None)
    if manager is None or engine is None:
        return None
    data_dir = _data_dir(request)
    svc = MiningCandidateService(data_dir, manager.store, CandidateStore(data_dir), engine)
    return svc.promote


@router.post("/candidates/decide")
def decide(request: Request, body: DecideIn) -> dict:
    from app.backtest.candidates import CandidateStore

    try:
        return factor_discover.decide(
            _data_dir(request), body.week, body.signature, body.action,
            promoter=_promoter(request), remover=CandidateStore(_data_dir(request)).delete,
        )
    except factor_discover.PromoteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except KeyError:
        raise HTTPException(status_code=404, detail="候选不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


class RecalcIn(BaseModel):
    signature: str = Field(..., min_length=1, max_length=400)
    pool: str = Field("watchlist", pattern="^(watchlist|theme|custom)$")
    symbols: list[str] = Field(default_factory=list, max_length=400)
    start: date
    end: date


@router.post("/recalc")
def recalc(request: Request, body: RecalcIn) -> dict:
    if body.end <= body.start:
        raise HTTPException(status_code=422, detail="结束日要晚于开始日")
    if body.pool == "watchlist":
        symbols = factor_discover.watchlist_symbols()
    elif body.pool == "theme":
        from app.services.mining_pool import build_mining_pool
        symbols = list(build_mining_pool(_data_dir(request)).symbols)
    else:
        symbols = [s.strip().upper() for s in body.symbols if s.strip()]
    if not symbols:
        raise HTTPException(status_code=422, detail="股票池为空")
    try:
        return factor_discover.recalc(_data_dir(request), _repo(request), body.signature, symbols=symbols, start=body.start, end=body.end)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except KeyError:
        raise HTTPException(status_code=404, detail="候选不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _background(name: str, fn) -> None:
    def run():
        try:
            fn()
        except Exception as exc:  # background job: log only
            import logging
            logging.getLogger(__name__).warning("factor-lab %s failed: %s", name, type(exc).__name__)
    threading.Thread(target=run, name=f"factor-lab-{name}", daemon=True).start()


@router.post("/run")
def run_discover(request: Request) -> dict:
    if not factor_discover.can_run_heavy():
        raise HTTPException(status_code=409, detail="盘中不跑, 收盘后 17:00 ET 起可运行")
    data_dir, repo = _data_dir(request), _repo(request)
    _background("discover", lambda: factor_discover.run_weekly(data_dir, repo))
    return {"started": True}


@router.post("/intake/run")
def run_intake(request: Request) -> dict:
    if factor_intake.running():
        raise HTTPException(status_code=409, detail="上一轮还没结束")
    data_dir, repo = _data_dir(request), _repo(request)
    _background("intake", lambda: factor_intake.tick(data_dir, repo, force=True))
    return {"started": True}


@router.post("/intake/process")
def process_intake(request: Request) -> dict:
    """只跑模型队列 + 快筛, 不抓源 (投递「立即处理」)。"""
    if factor_intake.running():
        raise HTTPException(status_code=409, detail="上一轮还没结束")
    data_dir, repo = _data_dir(request), _repo(request)
    _background("intake-process", lambda: factor_intake.process_only(data_dir, repo))
    return {"started": True}


@router.post("/intake/items/{item_id}/merge")
def merge(request: Request, item_id: str, formula: int = 0) -> dict:
    try:
        return factor_intake.merge(_data_dir(request), item_id, formula)
    except KeyError:
        raise HTTPException(status_code=404, detail="条目不存在或已处理") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/intake/items/{item_id}/ignore")
def ignore(request: Request, item_id: str) -> dict:
    try:
        return factor_intake.ignore(_data_dir(request), item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="条目不存在或已处理") from None


@router.post("/intake/items/{item_id}/restore")
def restore(request: Request, item_id: str) -> dict:
    try:
        return factor_intake.restore(_data_dir(request), item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="条目不存在或不可恢复") from None


@router.post("/intake/items/{item_id}/retry")
def retry_item(request: Request, item_id: str) -> dict:
    """0.47.3: re-queue a failed 投递. The call it needs is exempt from the daily model quota."""
    try:
        return factor_intake.retry(_data_dir(request), item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="条目不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


class SubmitIn(BaseModel):
    url: str = Field("", max_length=2000)
    text: str = Field("", max_length=60_000)
    title: str = Field("", max_length=200)


@router.post("/intake/submit")
def submit(request: Request, body: SubmitIn) -> dict:
    try:
        return factor_intake.submit(_data_dir(request), url=body.url, text=body.text, title=body.title)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:  # remote fetch failed
        raise HTTPException(status_code=502, detail=f"抓取失败: {type(exc).__name__}") from None


class SourceIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    url: str = Field(..., min_length=8, max_length=2000)
    kind: str = Field(..., pattern="^(person|site|aggregator|paper)$")


@router.post("/intake/sources")
def add_source(request: Request, body: SourceIn) -> dict:
    try:
        return factor_intake.add_source(_data_dir(request), name=body.name, url=body.url, kind=body.kind)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


class SourceToggleIn(BaseModel):
    enabled: bool = Field(..., strict=True)


@router.post("/intake/sources/{source_id}")
def toggle_source(request: Request, source_id: str, body: SourceToggleIn) -> dict:
    try:
        return factor_intake.set_source_enabled(_data_dir(request), source_id, body.enabled)
    except KeyError:
        raise HTTPException(status_code=404, detail="来源不存在") from None


@router.delete("/intake/sources/{source_id}")
def delete_source(request: Request, source_id: str) -> dict:
    try:
        factor_intake.remove_source(_data_dir(request), source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="来源不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True}


class IntakeSettingsIn(BaseModel):
    daily_limit: int = Field(..., strict=True, ge=0, le=1000)


@router.post("/intake/settings")
def intake_settings(request: Request, body: IntakeSettingsIn) -> dict:
    try:
        return factor_intake.save_settings(_data_dir(request), daily_limit=body.daily_limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


# ── 0.52.0 机会板排序因子 (rank lab) ────────────────────────────────────────

class RankerImportIn(BaseModel):
    name: str = Field("", max_length=80)
    formula: str = Field(..., min_length=1, max_length=600)
    direction: str = Field(..., pattern="^(high|low)$")
    idea: str = Field("", max_length=1000)
    source: dict | None = None


def _ranker_rows(data_dir) -> list[dict]:
    from app.services import rank_lab
    from app.services import swing_desk as sd
    long_ids, _ = sd.strategies_from_ruleset(data_dir)
    results = (rank_lab.read_results(data_dir).get("rankers") or {})
    out = []
    for rid, rk in sorted(rank_lab.read_rankers(data_dir).items(), key=lambda x: x[1].get("imported_at") or ""):
        res = results.get(rid)
        out.append({**rk, "enabled_long": rid in long_ids, "result": res, "verdict": rank_lab.verdict(res)
                    if isinstance(res, dict) and not res.get("error") else {"passed": False}})
    return out


@router.get("/rankers")
def rankers(request: Request) -> dict:
    from app.services import rank_lab
    data_dir = _data_dir(request)
    return {"status": rank_lab.status(data_dir), "rankers": _ranker_rows(data_dir)}


@router.post("/rankers/import")
def rankers_import(request: Request, body: RankerImportIn) -> dict:
    from app.services import rank_lab
    try:
        out = rank_lab.import_ranker(_data_dir(request), name=body.name, formula=body.formula,
                                     direction=body.direction, idea=body.idea, source=body.source)
    except rank_lab.RankFormulaError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": exc.message}) from None
    res = out.get("result") or {}
    out["verdict"] = rank_lab.verdict(res) if not res.get("error") else {"passed": False}
    try:   # 补算最近一份信号表的当日值 (勾上后当天预览就能出卡); 失败不影响导入
        out["backfill"] = rank_lab.backfill_latest(_data_dir(request), _repo(request))
    except Exception as e:
        out["backfill"] = {"error": type(e).__name__}
    return out


@router.post("/rankers/{ranker_id}/remove")
def rankers_remove(request: Request, ranker_id: str) -> dict:
    from app.services import rank_lab
    from app.services import swing_desk as sd
    data_dir = _data_dir(request)
    long_ids, _ = sd.strategies_from_ruleset(data_dir)
    try:
        return {"ok": True, "ranker": rank_lab.remove_ranker(data_dir, ranker_id, enabled_long=list(long_ids))}
    except KeyError:
        raise HTTPException(status_code=404, detail="排序因子不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/rankers/rebuild")
def rankers_rebuild(request: Request) -> dict:
    from app.services import rank_lab
    from app.services import swing_candidates as sc
    if not sc.can_run_now():
        raise HTTPException(status_code=409, detail="交易日 09:00-17:00 (美东) 不重导候选池; 收盘后 17:40 自动导出")
    if rank_lab.is_running():
        return {"started": False, "running": True}
    data_dir, repo = _data_dir(request), _repo(request)

    def run():
        try:
            rank_lab.run_nightly(data_dir, repo)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("rank lab rebuild failed: %s", type(e).__name__)
    threading.Thread(target=run, name="rank-lab-rebuild", daemon=True).start()
    return {"started": True, "running": True}
