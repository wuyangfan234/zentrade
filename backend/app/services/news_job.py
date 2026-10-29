"""新闻调度任务: 覆盖范围 (自选 ∪ 持仓 ∪ 机会卡候选) + 采集 → 翻译打标 → 持仓高重要度利空推送。

节奏 (美东):
  盘中 (09:30-16:00) 与盘前 (04:00-09:30): 每 5 分钟采集 Alpaca + Finnhub;
  其余时段: 每 30 分钟采集一次;
  SEC 8-K: 每 30 分钟 (官方限速 + 文件较大, 无需高频)。
翻译打标紧随每次采集之后 (每轮最多 120 条, 积压分摊到后续轮)。
2026-10-02 产品决策覆盖范围「24 只池 + 自选 + 持仓 + 机会卡候选」; 代码里无固定 24 只名单,
按推荐项落为三者并集 (方案文档 · 已拍板 / 实施笔记)。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PUSH_COOLDOWN_H = 12
FAST_SECONDS = 5 * 60
SLOW_SECONDS = 30 * 60
TRANSLATE_FAIL_WARN_STREAK = 2  # 0.41.1: 连续 N 轮翻译有失败批次才标黄 (单轮偶发超时下一轮自愈)
SEC_PER_ROUND = 40  # 实测 SEC submissions 约 0.75 秒/只 (2026-10-02 生产 37 只 28 秒); 大池轮转, 每轮最多 40 只
_lock = threading.Lock()
_last = {"fast": 0.0, "sec": 0.0}
_sec_cursor = {"i": 0, "core": 0}


UNIVERSE_MAX = 400
_CORE_CARD_STATES = ("open", "active", "filled")


def universe(loop=None, *, core_only: bool = False) -> list[str]:
    """自选 ∪ 持仓 ∪ 未终态机会卡 (含观察卡, 即已拍板的「机会卡候选」)。各来源失败互不影响。

    core_only=True: 自选 ∪ 持仓 ∪ 可入场/已入场卡 (不含观察卡)。供逐股限速的 Finnhub 补源用:
    实测 (2026-10-02 生产首轮) 非终态卡 175 张中 163 张是观察卡, 覆盖 136 只自选外小票,
    全量逐股拉 Finnhub 撞 60 次/分限速。Alpaca 批量、SEC 低成本, 仍覆盖全量 (影子记账要求观察卡也有新闻覆盖)。

    上限 UNIVERSE_MAX 只裁观察卡: 核心集 (自选/持仓/可入场卡) 永远保留, 剩余名额按观察卡生成日由新到旧补满。
    (审计 2026-10-26 H-1: 旧实现对并集按字母序截断, 观察卡累积到 800+ 只后 I 之后的自选与持仓全被裁掉。)
    """
    core: set[str] = set()
    extra: dict[str, str] = {}  # 观察卡 symbol -> 最新生成日 (ISO, 便于排序)
    try:
        from app.services import watchlist
        core |= {str(r["symbol"]).upper() for r in watchlist.list_symbols() if r.get("symbol")}
    except Exception as e:  # noqa: BLE001
        logger.debug("news universe: watchlist unavailable: %s", type(e).__name__)
    if loop is not None:
        try:
            core |= {str(p.symbol).upper() for p in loop.ledger.open_positions if p.symbol}
        except Exception as e:  # noqa: BLE001
            logger.debug("news universe: positions unavailable: %s", type(e).__name__)
        try:
            from app.market_clock import us_clock
            from app.opportunity.card import TERMINAL_STATES
            for c in loop.opp_svc.all_cards(us_clock.today()):
                if c.state in TERMINAL_STATES or not c.symbol:
                    continue
                sym = str(c.symbol).upper()
                if str(getattr(c.state, "value", c.state)) in _CORE_CARD_STATES:
                    core.add(sym)
                elif not core_only:
                    day = str(getattr(c, "created_date", "") or "")
                    if day > extra.get(sym, ""):
                        extra[sym] = day
                    else:
                        extra.setdefault(sym, day)
        except Exception as e:  # noqa: BLE001
            logger.debug("news universe: cards unavailable: %s", type(e).__name__)
    core.discard("")
    room = max(0, UNIVERSE_MAX - len(core))
    fresh = sorted((s for s in extra if s not in core), key=lambda s: (extra[s], s), reverse=True)[:room]
    return sorted(core | set(fresh))


def sec_batch(symbols: list[str], core: list[str], held: set[str] | None = None) -> list[str]:
    """本轮 SEC 查询集 (每轮最多 SEC_PER_ROUND 只)。

    核心集 <= SEC_PER_ROUND 时每轮全查 (与旧行为一致), 剩余名额轮转观察票;
    核心集超额时: 持仓每轮必查, 其余核心轮转, 并给观察票留 1/4 名额。
    (审计 2026-10-26 H-1: 旧实现核心 44 只 > 40 时 take=0, 游标永不前进, 末尾 4 只核心与全部观察票永远查不到。)
    """
    core_set = set(core)
    core_l = [x for x in symbols if x in core_set] or list(core)
    rest = [x for x in symbols if x not in core_set]
    if len(core_l) <= SEC_PER_ROUND:
        core_take = list(core_l)
    else:
        must = [x for x in core_l if held and x in held][:SEC_PER_ROUND]
        cap = SEC_PER_ROUND if not rest else max(len(must), SEC_PER_ROUND * 3 // 4)
        others = [x for x in core_l if x not in set(must)]
        room = max(0, cap - len(must))
        take_o: list[str] = []
        if others and room:
            j = _sec_cursor.get("core", 0) % len(others)
            take_o = (others[j:] + others[:j])[:room]
            _sec_cursor["core"] = j + room
        core_take = must + take_o
    take = max(0, SEC_PER_ROUND - len(core_take))
    rest_take: list[str] = []
    if rest and take:
        i = _sec_cursor.get("i", 0) % len(rest)
        rest_take = (rest[i:] + rest[:i])[:take]
        _sec_cursor["i"] = i + take
    return sorted(set(core_take) | set(rest_take))


def held_symbols(loop=None) -> set[str]:
    if loop is None:
        return set()
    try:
        return {p.symbol.upper() for p in loop.ledger.open_positions}
    except Exception:  # noqa: BLE001
        return set()


def _fast_interval(now_et) -> int:
    t = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday() < 5
    return FAST_SECONDS if weekday and 4 * 60 <= t < 16 * 60 else SLOW_SECONDS


def tick(data_dir: Path, loop=None, repo=None, *, now: datetime | None = None, force: bool = False) -> dict | None:
    """调度器每分钟调用; 内部按节奏决定是否真跑。返回本次结果 (未到点返回 None)。"""
    from app.market_clock import us_clock
    from app.services import news_ingest, news_store, news_translate

    now = now or datetime.now(timezone.utc)
    now_et = now.astimezone(us_clock.tz)
    mono = time.monotonic()
    if not _lock.acquire(blocking=False):
        return None  # 上一轮还在跑
    try:
        due_fast = force or mono - _last["fast"] >= _fast_interval(now_et) - 5
        due_sec = force or mono - _last["sec"] >= SLOW_SECONDS - 5
        symbols = universe(loop)
        core = universe(loop, core_only=True)
        result = {"raw": 0, "kept": 0, "added": 0, "errors": []}
        sources: set[str] = set()
        if due_fast or due_sec:
            names = {}
            if repo is not None and symbols:
                try:
                    names = repo.get_name_map(symbols)
                except Exception as e:  # noqa: BLE001
                    logger.debug("news names unavailable: %s", type(e).__name__)
            if due_fast:
                _last["fast"] = mono  # 先盖戳: 本阶段失败/超时也不会下一分钟立刻重跑
                sources |= {"alpaca", "finnhub"}
                r = news_ingest.ingest(data_dir, symbols, now=now, sources={"alpaca", "finnhub"}, names=names,
                                       finnhub_symbols=core)
                for k in ("raw", "kept", "added"):
                    result[k] += r.get(k, 0)
                result["errors"] += r.get("errors", [])
            if due_sec and symbols:
                _last["sec"] = mono
                sources.add("sec")
                # 大池轮转: 每轮最多 SEC_PER_ROUND 只; 核心集不超额时每轮全查, 超额时持仓每轮必查、其余核心轮转
                batch = sec_batch(symbols, core, held_symbols(loop))
                r = news_ingest.ingest(data_dir, batch, now=now, sources={"sec"}, names=names)
                for k in ("raw", "kept", "added"):
                    result[k] += r.get(k, 0)
                result["errors"] += r.get("errors", [])
        # 翻译每次唤醒都跑 (自带每轮批次数与墙钟预算), 不依赖采集是否到点
        tr = news_translate.translate_pending_sync(data_dir, now=now, priority=set(core))
        if not sources and not tr.get("llm") and not tr.get("fallback"):
            return None
        pushed = notify_holdings(data_dir, held_symbols(loop), now=now)
        # 0.41.1: 翻译失败连续轮数。单批偶发超时 (网关 120s 超时, 下一轮自动重译) 不算故障;
        # 连续 TRANSLATE_FAIL_WARN_STREAK 轮都失败才在状态页标黄。
        prev = (news_store.status_state(data_dir).get("translate_fail_streak") or 0)
        streak = int(prev) + 1 if tr.get("failed_batches") else 0
        news_store.write_state(data_dir, last_run_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), universe=len(symbols),
                               translate_fail_streak=streak,
                               core=len(core),
                               sources=sorted(sources), ingest={k: result[k] for k in ("raw", "kept", "added")},
                               ingest_errors=result["errors"][:10], translate=tr, pushed=pushed)
        return {"universe": len(symbols), "ingest": result, "translate": tr, "pushed": pushed}
    except Exception as e:  # noqa: BLE001
        logger.warning("news tick failed: %s", type(e).__name__)
        try:
            from app.services import news_store as ns
            ns.write_state(data_dir, last_error=f"{type(e).__name__}: {e}"[:200],
                           last_error_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        except Exception:  # noqa: BLE001
            pass
        return {"error": type(e).__name__}
    finally:
        _lock.release()


def notify_holdings(data_dir: Path, held: set[str], *, now: datetime | None = None) -> int:
    """持仓 + 重要度高 + 利空 + 主角 + 48 小时内 → 入队飞书 (按事件 id 去重)。

    2026-10-02 产品决策「开启：仅持仓 + 重要度高时推送」。只推利空 (方案: 持仓重大利空);
    只推模型/规则打过标的 (未打标的兜底译文不推, 避免无判读的噪音)。
    """
    if not held:
        return 0
    import json as _json
    from app.services import news_store
    from app.services.fs_utils import atomic_write_text
    from app.services.notify_opportunity import enqueue_news_alert
    now = now or datetime.now(timezone.utc)
    state_p = Path(data_dir) / "news" / "push_state.json"
    try:
        state = _json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
    except (OSError, ValueError):
        state = {}
    rows = news_store.load_recent(data_dir, days=2, now=now)
    n = 0
    for sym in sorted(held):
        last = state.get(sym)
        try:
            if last and (now - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() < PUSH_COOLDOWN_H * 3600:
                continue  # 同一持仓 12 小时内最多推一次 (多家媒体报道同一事件, 标题不同聚不成簇)
        except ValueError:
            pass
        hits = [v for v in news_store.for_symbol(data_dir, sym, rows=rows, now=now, limit=200)
                if v["tagged"] and v["importance"] == "high" and v["direction"] == "bearish" and v["relevance"] == "primary"
                and not v["rehash"] and (v["age_hours"] or 1e9) <= 48]
        if not hits:
            continue
        first = min(hits, key=lambda v: v["published_at"] or "")  # 最早一条 = 事件首发
        if enqueue_news_alert(data_dir, {**first, "related_count": len(hits)}):
            n += 1
            state[sym] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if n:
        state_p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(state_p, _json.dumps(state, ensure_ascii=False))
    return n
