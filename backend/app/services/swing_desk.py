"""机会板出卡引擎 tight-v1 (0.24.0, 无限 2026-10-15 "完全重构, 卡区间太大没有交易价值")。

流水线: 自选 → 4h Phantom 门 (窗口内"条件齐备"事件 或 最后一根已完成 4h 同向状态)
→ 用户可改的 3 多 / 3 空策略打分 (命中条数) → 空头只在 SPY 收盘 < 50 日线
→ 紧几何卡 (一张卡 = 一张可挂的限价单):

    A = 信号日日线 ATR14, c = 信号日收盘 (多头; 空头镜像)
    入场区 = [c - 0.50A, c - 0.25A]      (限价回踩, 不追)
    止损   = 区上沿 - 1.0A               (收盘破位, 次日开盘离场; 交易员现有语义)
    目标   = 区上沿 + 2.0A               (盘中触及全平)
    每股风险 R = 区中值 - 止损 = 0.875A;  盈亏比 = (目标 - 区中值) / R = 2.125A / 0.875A ≈ 2.43
    有效期 = 信号日后 2 个交易日; 持有 ≤ 5 个交易日

证据: frozen tight-v1 replay specification (30m 逐根回放 2023-01~2026-09)。
结算合同 zt-settle-v1 不在这里改。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 默认 3+3 (无限 10-15: 可在规则集手动改; 初值按自选池超额 R 选) ──────────
# 证据: backend/config/watchlist_rule_history.json 2022-01~2026-09 同票随机日基线。
DEFAULT_LONG = (
    "tv_smc_structure",      # excess +0.40 (n=43, p=0.025)
    "volume_price_surge",    # excess +0.25 (n=105, p=0.085)
    "macd_golden",           # excess +0.19 (n=228, p=0.073)
)
DEFAULT_SHORT = (
    "tv_smc_structure",      # sell→short excess +0.08 (n=74)
    "tv_chandelier_exit",    # excess +0.06 (n=610)
    "tv_halftrend",          # excess +0.06 (n=955)
)
# 固定展示权重 (非日更 evo); 仅同分 tie-break
EXCESS_WEIGHT = {
    "tv_smc_structure": 0.40,
    "volume_price_surge": 0.25,
    "macd_golden": 0.19,
    "tv_chandelier_exit": 0.06,
    "tv_halftrend": 0.06,
}
HOLD_DAYS = 5
MAX_PER_SIDE = 5
CONTRACT = "swing-desk-tight-v1"
# 0.26.0: 规则面板“回测 N 笔”= 现在这张卡 (tight-v1) 的历史, 由 30 分钟K 回放算出 (见文件 source 字段);
# 旧宽卡历史 watchlist_rule_history.json 只留给旧规则表。只展示, 不进打分/排序 (排序权重仍是上面的 EXCESS_WEIGHT)。
RULE_HISTORY_PATH = Path(__file__).parents[2] / "config" / "swing_rule_history.json"
_RULE_HISTORY: dict = {}


def rule_history() -> dict:
    """tight-v1 规则历史 (只读, 进程内缓存)。读失败 = 空 (面板显示 —)。"""
    if "v" not in _RULE_HISTORY:
        try:
            d = json.loads(RULE_HISTORY_PATH.read_text(encoding="utf-8"))
            _RULE_HISTORY["v"] = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _RULE_HISTORY["v"] = {}
    return _RULE_HISTORY["v"]
RULE_ID = "swing"            # 纸面下单簿/许可里的规则名 (setup_type = strategy:swing)
ENTRY_DAYS = 2               # 信号日后 2 个交易日内入区, 否则作废
K_NEAR, K_FAR = 0.25, 0.50   # 入场区 = 收盘回踩 0.25A ~ 0.50A
STOP_A, TARGET_A = 1.0, 2.0  # 止损/目标距区上沿 (空头: 区下沿) 的 ATR 倍数
NAMES = {
    "tv_smc_structure": "SMC 结构",
    "volume_price_surge": "量价齐升",
    "macd_golden": "MACD 金叉放量",
    "ibs_mean_reversion": "IBS 均值回归",
    "tv_chandelier_exit": "吊灯翻空",
    "tv_halftrend": "HalfTrend 翻空",
}
# 空头用 TV 移植策略的卖出信号: 名字写“翻空”, 不用其 META 的“翻多”
SHORT_NAMES = {
    "tv_smc_structure": "SMC 结构翻空",
    "tv_chandelier_exit": "吊灯翻空",
    "tv_halftrend": "HalfTrend 翻空",
    "tv_supertrend": "Supertrend 翻空",
    "tv_ut_bot": "UT Bot 翻空",
    "tv_zlsma_cross": "ZLSMA 下穿",
    "tv_wavetrend": "WaveTrend 翻空",
    "tv_phantom_shift": "Phantom Shift 翻空",
    "tv_phantom_trail": "Phantom Trail 翻空",
    "tv_squeeze_momentum": "挤压动能转弱",
}
_META_NAMES: dict[str, str] = {}


def strategy_name(rule: str, side: str = "long") -> str:
    """卡上显示的策略名: 空头 → SHORT_NAMES; 多头 → NAMES; 再退到内置策略 META 的 name; 都没有才用 id。"""
    if side == "short" and rule in SHORT_NAMES:
        return SHORT_NAMES[rule]
    if rule in NAMES:
        return NAMES[rule]
    if rule not in _META_NAMES:
        name = rule
        try:
            import importlib
            meta = getattr(importlib.import_module(f"app.strategy.builtin.{rule}"), "META", None) or {}
            if isinstance(meta.get("name"), str) and meta["name"].strip():
                name = meta["name"].strip()
        except Exception:
            pass
        _META_NAMES[rule] = name
    return _META_NAMES[rule]


def watchlist_signal_path(data_dir: Path, day: date) -> Path:
    return Path(data_dir) / "opportunities" / "watchlist" / f"{day.isoformat()}.json"


def latest_watchlist_day(data_dir: Path) -> date | None:
    d = Path(data_dir) / "opportunities" / "watchlist"
    if not d.exists():
        return None
    days = []
    for f in d.glob("20*.json"):
        try:
            days.append(date.fromisoformat(f.stem))
        except ValueError:
            continue
    return max(days) if days else None


def load_watchlist_snapshot(data_dir: Path, day: date | None = None) -> dict | None:
    day = day or latest_watchlist_day(data_dir)
    if day is None:
        return None
    p = watchlist_signal_path(data_dir, day)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    raw["_day"] = day
    return raw


def swing_config_path(data_dir: Path) -> Path:
    """用户可改的 3+3 配置 (与 evo 时代 rules/current 解耦; 机会页/设置可写此文件)。"""
    return Path(data_dir) / "rules" / "swing.json"


def strategies_from_ruleset(data_dir: Path | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """读 data/rules/swing.json 的 long/short (各最多 3 条)。缺/坏/空 → 默认 3+3。

    不读 R0 默认勾选 (那是旧前向确认七条), 避免静默串台。用户在设置里改 3+3 即写 swing.json。
    """
    long_ids, short_ids = list(DEFAULT_LONG), list(DEFAULT_SHORT)
    if data_dir is None:
        return tuple(long_ids), tuple(short_ids)
    p = swing_config_path(data_dir)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return tuple(long_ids), tuple(short_ids)
    if not isinstance(raw, dict):
        return tuple(long_ids), tuple(short_ids)
    ban = {"evo", "phantom_4h", "phantom_4h_short", "phantom_4h_pine"}
    en = [x for x in (raw.get("long") or []) if isinstance(x, str) and x not in ban]
    sh = [x for x in (raw.get("short") or []) if isinstance(x, str) and x not in ban]
    if en:
        long_ids = en[:3]
    if sh:
        short_ids = sh[:3]
    return tuple(long_ids), tuple(short_ids)


def save_strategies(data_dir: Path, long_ids: list[str], short_ids: list[str]) -> dict:
    """设置页保存 3+3。"""
    from app.services.fs_utils import atomic_write_text
    ban = {"evo", "phantom_4h", "phantom_4h_short", "phantom_4h_pine"}
    out = {
        "long": [x for x in long_ids if isinstance(x, str) and x not in ban][:3],
        "short": [x for x in short_ids if isinstance(x, str) and x not in ban][:3],
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "swing-desk 3+3; 可手动改",
    }
    if not out["long"]:
        out["long"] = list(DEFAULT_LONG)
    if not out["short"]:
        out["short"] = list(DEFAULT_SHORT)
    path = swing_config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(out, ensure_ascii=False, indent=1))
    return out



def _phantom_maps(signals: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """从自选信号快照抽出 4h "条件齐备"事件。phantom_4h → 多; phantom_4h_short → 空。"""
    longs, shorts = {}, {}
    for s in signals:
        if not isinstance(s, dict):
            continue
        sym, rule = s.get("symbol"), s.get("rule")
        if not isinstance(sym, str):
            continue
        if rule == "phantom_4h" and s.get("side") != "short":
            longs[sym] = s
        elif rule == "phantom_4h_short" or (rule == "phantom_4h" and s.get("side") == "short"):
            shorts[sym] = s
    return longs, shorts


def _hits_by_symbol(signals: list[dict], rule_ids: tuple[str, ...], side: str) -> dict[str, list[dict]]:
    want = set(rule_ids)
    out: dict[str, list[dict]] = {}
    for s in signals:
        if not isinstance(s, dict):
            continue
        if s.get("rule") not in want:
            continue
        if (s.get("side") or "long") != side:
            continue
        out.setdefault(str(s["symbol"]), []).append(s)
    return out


def geometry(side: str, close: float, atr: float) -> dict | None:
    """紧几何 (纯函数)。close/atr 非正或非有限 → None。价位保留 2 位小数 (股价最小变动 0.01)。"""
    import math
    if not (isinstance(close, (int, float)) and isinstance(atr, (int, float))):
        return None
    if not (math.isfinite(close) and math.isfinite(atr) and close > 0 and atr > 0):
        return None
    if side == "long":
        lo, hi = close - K_FAR * atr, close - K_NEAR * atr
        stop, target = hi - STOP_A * atr, hi + TARGET_A * atr
    else:
        lo, hi = close + K_NEAR * atr, close + K_FAR * atr
        stop, target = lo + STOP_A * atr, lo - TARGET_A * atr
    lo, hi, stop, target = (round(x, 2) for x in (lo, hi, stop, target))
    if not (0 < lo < hi) or stop <= 0 or target <= 0:
        return None
    mid = (lo + hi) / 2
    risk = round(abs(mid - stop), 4)
    if risk <= 0:
        return None
    rr = round(abs(target - mid) / risk, 2)
    if side == "long" and not (stop < lo and target > hi):
        return None
    if side == "short" and not (stop > hi and target < lo):
        return None
    return {"entry_zone": [lo, hi], "stop": stop, "target": target, "risk": risk, "rr": rr,
            "atr": round(atr, 4), "stop_pct": round((stop / mid - 1) * 100, 2),
            "target_pct": round((target / mid - 1) * 100, 2)}


def _card(*, symbol: str, side: str, hits: list[dict], rule_ids: tuple[str, ...], close: float | None,
          atr: float | None, gate: str, as_of: date) -> dict[str, Any] | None:
    seen = {h.get("rule") for h in hits}
    hit_rules = [r for r in rule_ids if r in seen]
    geo = geometry(side, close, atr) if close and atr else None
    if geo is None:
        return None
    score = float(len(hit_rules)) + sum(EXCESS_WEIGHT.get(r, 0.05) * 0.1 for r in hit_rules)
    return {
        "contract": CONTRACT,
        "symbol": symbol,
        "side": side,
        "score": round(score, 3),
        "hit_count": len(hit_rules),
        "strategies": [{"id": r, "name": strategy_name(r, side)} for r in hit_rules],
        "gate": gate,                       # "event" = 4h 窗口内条件齐备; "state" = 4h 方向同向
        "close": close,
        **geo,
        # 兼容旧字段 (前端/下游读): target_zone 两端相同 = 单一目标价
        "target_zone": [geo["target"], geo["target"]],
        "entry_days": ENTRY_DAYS,
        "hold_days": HOLD_DAYS,
        # 兼容旧前端包 (部署时未刷新的页面读 invalidate_when[0])
        "invalidate_when": ["收盘破止损, 次日开盘离场", f"{ENTRY_DAYS} 日内未回到挂单区作废", f"持有满 {HOLD_DAYS} 日"],
        "as_of": as_of.isoformat(),
    }


def compose_boards(
    *,
    signals: list[dict],
    closes: dict[str, float],
    as_of: date,
    long_rules: tuple[str, ...] = DEFAULT_LONG,
    short_rules: tuple[str, ...] = DEFAULT_SHORT,
    max_per_side: int = MAX_PER_SIDE,
    atr: dict[str, float] | None = None,
    h4_state: dict[str, str] | None = None,
    short_ok: bool | None = True,
) -> dict[str, Any]:
    """纯函数: 信号快照 → LONG/SHORT 板。

    4h 门: 同向"条件齐备"事件 或 h4_state 同向。打分: 同向 3 策略命中数 ≥ 1。
    short_ok: SPY 收盘 < 50 日线 (True) / 不满足 (False) / 数据缺 (None, fail-closed 不出空)。
    atr 缺的票不出卡 (旧信号表没有 atr → 板为空, 等下一次盘后扫描)。
    """
    ph_l, ph_s = _phantom_maps(signals)
    h4_state = h4_state or {}
    atr = atr or {}
    hits_l = _hits_by_symbol(signals, long_rules, "long")
    hits_s = _hits_by_symbol(signals, short_rules, "short")
    skipped: list[dict] = []

    def side_cards(side: str, hits_by: dict, events: dict, rules: tuple[str, ...]) -> list[dict]:
        out = []
        for sym, hits in hits_by.items():
            if sym in events:
                gate = "event"
            elif h4_state.get(sym) == side:
                gate = "state"
            else:
                continue
            c = _card(symbol=sym, side=side, hits=hits, rule_ids=rules, close=closes.get(sym),
                      atr=atr.get(sym), gate=gate, as_of=as_of)
            if c is None:
                reason = "no_atr_or_close" if not (closes.get(sym) and atr.get(sym)) else "bad_geometry"
                skipped.append({"symbol": sym, "side": side, "reason": reason})
                continue
            out.append(c)
        out.sort(key=lambda c: (-c["score"], -c["hit_count"], c["gate"] != "event", c["symbol"]))
        return out

    long_all = side_cards("long", hits_l, ph_l, long_rules)
    short_all = side_cards("short", hits_s, ph_s, short_rules) if short_ok is True else []
    # 同票同日多空都有 → 方向冲突, 两边都不出 (先去冲突再截前 N 张)
    both = {c["symbol"] for c in long_all} & {c["symbol"] for c in short_all}
    if both:
        skipped += [{"symbol": s, "reason": "direction_conflict"} for s in sorted(both)]
    long_cards = [c for c in long_all if c["symbol"] not in both][:max_per_side]
    short_cards = [c for c in short_all if c["symbol"] not in both][:max_per_side]
    return {
        "contract": CONTRACT,
        "as_of": as_of.isoformat(),
        "long_rules": list(long_rules),
        "short_rules": list(short_rules),
        "long": long_cards,
        "short": short_cards,
        "meta": {
            "phantom_long_events": len(ph_l),
            "phantom_short_events": len(ph_s),
            "h4_long_state": sum(1 for v in h4_state.values() if v == "long"),
            "h4_short_state": sum(1 for v in h4_state.values() if v == "short"),
            "raw_long_hits": len(hits_l),
            "raw_short_hits": len(hits_s),
            "short_gate": short_ok,
            "skipped": skipped,
            "geometry": {"entry": [K_NEAR, K_FAR], "stop_atr": STOP_A, "target_atr": TARGET_A,
                         "entry_days": ENTRY_DAYS, "hold_days": HOLD_DAYS},
        },
    }


def rules_for_day(data_dir: Path, day: date) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """该信号日用哪套 3+3: 纸面下单簿已落盘 → 用簿里冻结的那套 (板与纸面始终同一批卡);
    还没落簿 (盘后到次日 08:00 前) → 当前 swing.json (改 3+3 即时预览)。"""
    from app.services import watchlist_book as wb
    try:
        raw = json.loads(wb.book_path(data_dir, day).read_text(encoding="utf-8"))
        fr = raw.get("swing_rules") if isinstance(raw, dict) else None
        if isinstance(fr, dict) and fr.get("long") and fr.get("short"):
            return tuple(fr["long"]), tuple(fr["short"])
    except (OSError, ValueError):
        pass
    return strategies_from_ruleset(data_dir)


def board_for_day(data_dir: Path, day: date, repo=None) -> dict[str, Any] | None:
    """某个信号日的板 (纯计算, 不写文件)。没有该日信号表 → None。repo=None → 空头门按数据缺处理 (不出空)。"""
    snap = load_watchlist_snapshot(data_dir, day)
    if snap is None:
        return None
    long_rules, short_rules = rules_for_day(data_dir, day)
    short_ok = None
    gate_info = None
    if repo is not None:
        try:
            from app.services import watchlist_v4 as wv
            gate_info = wv.short_gate(repo, day)
            short_ok = gate_info.get("ok")
        except Exception as e:
            logger.warning("swing_desk: short gate failed: %s", type(e).__name__)
    closes = {k: float(v) for k, v in (snap.get("closes") or {}).items() if isinstance(v, (int, float))}
    atr = {k: float(v) for k, v in (snap.get("atr") or {}).items() if isinstance(v, (int, float))}
    h4 = {k: v for k, v in (snap.get("h4_state") or {}).items() if v in ("long", "short")}
    boards = compose_boards(signals=list(snap.get("signals") or []), closes=closes, as_of=day,
                            long_rules=long_rules, short_rules=short_rules, atr=atr, h4_state=h4, short_ok=short_ok)
    if gate_info is not None:
        boards["meta"]["spy"] = {"close": gate_info.get("close"), "ma50": gate_info.get("ma50")}
    if not atr:
        boards["meta"]["reason"] = "snapshot_without_atr"
    return boards


def build(data_dir: Path, *, day: date | None = None, repo=None) -> dict[str, Any]:
    """读最近自选信号快照 + 3+3 → 两板 (严格只读, 不写 swing.json: 该文件存在与否决定纸面许可)。无快照 → 空板。"""
    day = day or latest_watchlist_day(data_dir)
    boards = board_for_day(data_dir, day, repo) if day else None
    if boards is None:
        long_rules, short_rules = strategies_from_ruleset(data_dir)
        return {"contract": CONTRACT, "available": False, "as_of": None, "long_rules": list(long_rules),
                "short_rules": list(short_rules), "long": [], "short": [], "meta": {"reason": "no_watchlist_snapshot"}}
    boards["available"] = True
    boards["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return boards


LIVE_TICK_MAX_AGE_S = 120   # 热报价超过 2 分钟不算“实时” (收盘后/盘前缓冲里留着昨天最后一笔)


def _today_bars(repo, today: date, symbols) -> dict[str, tuple[float, float, float, float]]:
    """今天这根还没收盘的日K (开/高/低/最新), 取自全市场行情快照 (TickFlow, 约 8 秒一轮, 内存缓存)。
    0.29.1 从旧 watchlist_cards 迁来, 只给 _live_prices 取热报价池外的现价。
    09:30 ET 前、快照不是今天、缺列、非有限值或高低不自洽 → 不给 (照旧只按已收盘日K)。"""
    import math

    import polars as pl

    from app.market_clock import US_SESSION_OPEN, us_clock
    try:
        now = us_clock.market_datetime(us_clock.now())
        getter = getattr(repo, "get_enriched_latest", None)
        if not symbols or getter is None or now.date() != today or now.time() < US_SESSION_OPEN \
                or not us_clock.is_trading_day(today):
            return {}
        df, d = getter()
        cols = ["symbol", "open", "high", "low", "close"]
        if d != today or df is None or df.is_empty() or not set(cols) <= set(df.columns):
            return {}
        if "date" in df.columns:
            df = df.filter(pl.col("date") == today)
        out = {}
        for sym, *v in df.filter(pl.col("symbol").is_in(sorted(symbols))).select(cols).iter_rows():
            try:
                o, h, lo, c = (float(x) for x in v)
            except (TypeError, ValueError):
                continue
            if all(math.isfinite(x) and x > 0 for x in (o, h, lo, c)) and lo <= min(o, c) and h >= max(o, c):
                out[sym] = (o, h, lo, c)
        return out
    except Exception as e:
        logger.warning("swing_desk: today snapshot unavailable: %s", type(e).__name__)
        return {}


def _live_prices(repo, symbols: set[str], today: date, now=None) -> dict[str, tuple[float, str]]:
    """现价 (只读): 2 分钟内的 5 秒热报价 = live → 全市场快照今日最新价 = snapshot →
    盘前 (04:00-09:45) Hyperliquid 永续中间价 = premarket → 都没有由调用方用信号日收盘 (close)。
    0.24.1: 旧实现不看热报价时间戳, 盘前/收盘后把昨天最后一笔标成“实时”。"""
    from app.market_clock import us_clock
    now = us_clock.market_datetime(now or us_clock.now())
    out: dict[str, tuple[float, str]] = {}
    try:
        from app.api.portfolio import _live_marks
        now_ms = now.timestamp() * 1000
        for s, v in _live_marks(sorted(symbols)).items():
            ts = v[1] if len(v) > 1 else None
            if isinstance(ts, (int, float)) and 0 <= now_ms - ts <= LIVE_TICK_MAX_AGE_S * 1000:
                out[s] = (float(v[0]), "live")
    except Exception:
        pass
    rest = symbols - set(out)
    if rest and repo is not None:
        try:
            for s, (_o, _h, _l, c) in _today_bars(repo, today, rest).items():
                out[s] = (float(c), "snapshot")
        except Exception:
            pass
    rest = symbols - set(out)
    if rest:
        try:
            from app.services import card_volume as cv
            if cv.phase(now)["phase"] in ("premarket", "open_wait"):
                for s, row in cv.premarket_prices(sorted(rest)).items():
                    out[s] = (float(row["price"]), "premarket")
        except Exception:
            pass
    return out


def _premarket_volume(symbols: set[str], now) -> dict:
    """盘前量比 (card_volume.premarket, 非阻塞: 返回缓存, 过期后台刷新)。04:00 ET 前 / 非交易日 = 空。测试里替换为桩。"""
    from app.services import card_volume as cv
    if not symbols or cv.phase(now)["phase"] not in ("premarket", "open_wait", "session", "after"):
        return {}
    return cv.premarket(sorted(symbols), now)


def _book_only_cards(book: dict | None, board_ids: set[str], d: date) -> list[dict]:
    """簿里落盘了、但当前信号表重算的板上没有的卡 (0.26.0: 落簿后被移出自选, 次日重扫的信号表不再含它)。
    用簿里冻结的价位还原成板卡的形状, 让它进持仓或“已结束”, 不再凭空消失。"""
    out = []
    for x in (book or {}).get("cards") or []:
        cid = x.get("id")
        if not cid or cid in board_ids or dd_rule(x) != RULE_ID:
            continue
        ev = x.get("evidence") or {}
        lo, hi = (x.get("entry_zone") or [None, None])[:2]
        tz = x.get("target_zone") or [None, None]
        risk = x.get("risk_per_share")
        out.append({"contract": CONTRACT, "symbol": x.get("symbol"), "side": x.get("side") or "long",
                    "score": ev.get("score") or 0.0, "hit_count": len((ev.get("decision") or {}).get("rules") or []),
                    "strategies": [{"id": r, "name": strategy_name(r, x.get("side") or "long")}
                                   for r in (ev.get("decision") or {}).get("rules") or []],
                    "gate": (ev.get("decision") or {}).get("gate_4h"), "close": ev.get("ref_close"),
                    "entry_zone": [lo, hi], "stop": x.get("invalidation"), "target": tz[0], "target_zone": tz,
                    "risk": risk, "rr": x.get("reward_risk"), "atr": ev.get("atr14"),
                    **_pcts(lo, hi, x.get("invalidation"), tz[0]),
                    "entry_days": ENTRY_DAYS, "hold_days": x.get("max_holding_days") or HOLD_DAYS,
                    "as_of": d.isoformat(), "signal_day": d.isoformat(), "card_id": cid})
    return out


def _pcts(lo, hi, stop, target) -> dict:
    try:
        mid = (float(lo) + float(hi)) / 2
        return {"stop_pct": round((float(stop) / mid - 1) * 100, 2), "target_pct": round((float(target) / mid - 1) * 100, 2)}
    except (TypeError, ValueError, ZeroDivisionError):
        return {}


def _watchlist_now() -> set[str]:
    """当前自选 (与交易环 entry_guard 同一来源, 读失败 = 空集, 与交易环一样 fail-closed)。"""
    from app.services import decision_scan_job as job
    return job._watchlist()


def dd_rule(card: dict) -> str | None:
    """簿卡 setup_type = strategy:<rule> → rule。"""
    st = str(card.get("setup_type") or "")
    return st.split(":", 1)[1] if st.startswith("strategy:") else None


def _last_closes(repo, symbols: set[str], today: date) -> dict[str, tuple[float, str]]:
    """最近一个已收盘交易日的收盘价 (只读日K)。休市/盘前没有热报价与快照时用它, 而不是信号日收盘
    (0.26.0: 周末 AAPL 卡显示 10-15 收盘 342.00, 实际最新收盘 10-16 是 343.46)。读不到 → 空 (调用方退回信号日收盘)。"""
    from datetime import timedelta
    getter = getattr(repo, "get_daily_batch", None)
    if not symbols or getter is None:
        return {}
    try:
        b = getter(sorted(symbols), today - timedelta(days=14), today, columns=["symbol", "date", "close"])
    except Exception as e:
        logger.warning("swing_desk: last close unavailable: %s", type(e).__name__)
        return {}
    if b is None or b.is_empty():
        return {}
    out = {}
    for g in b.sort("date").partition_by("symbol"):
        c = g["close"][-1]
        if isinstance(c, (int, float)) and c > 0:
            out[str(g["symbol"][0])] = (float(c), str(g["date"][-1]))
    return out


def _cooldown(events: list[dict], day: date) -> set[str]:
    """同票平仓后 N 个交易日内不开 (N = setups.json portfolio.same_symbol_cooldown_days, 与交易环同式)。"""
    from datetime import timedelta

    from app.market_clock import us_clock
    from app.opportunity.engine import load_params
    try:
        limit = int(load_params()["portfolio"]["same_symbol_cooldown_days"])
    except Exception:
        return set()
    out = set()
    for e in events:
        if e.get("kind") != "close" or not e.get("symbol"):
            continue
        try:
            d = date.fromisoformat(str(e.get("trade_date") or e["at"])[:10])
        except (KeyError, ValueError):
            continue
        if len(us_clock.trading_days_between(d + timedelta(days=1), day)) < limit:
            out.add(e["symbol"])
    return out


def _earnings_blocked(data_dir: Path, symbols: set[str], day: date) -> set[str]:
    """该交易日是财报前 1 个交易日 → 当天不开新仓 (earnings_guard.entry_blocked, 与交易环同式)。"""
    try:
        from app.services import earnings_guard
        from app.services.earnings_calendar import EarningsCalendar
        cal = EarningsCalendar(data_dir)
        return {s for s in symbols if earnings_guard.entry_blocked(cal.event_for(s), day)}
    except Exception:
        return set()


def _entry_day(now, today: date) -> date:
    """卡下一次能成交的交易日: 交易日入场截止 (15:30, 半日市 12:30) 前 = 今天; 否则下一交易日。"""
    from app.market_clock import us_clock
    if us_clock.is_trading_day(today) and not (now.date() == today and now.time() > us_clock.entry_close(today)):
        return today
    return us_clock.next_trading_day(today)


def _closed_rounds(events: list[dict]) -> dict[str, dict]:
    """已平仓回合 (按卡): 回合 R / 离场原因 / 离场日 (review.build_round_states, 与持仓页同一算式)。"""
    try:
        from app.review.review import build_round_states
        closed, _ = build_round_states(events)
    except Exception:
        return {}
    out = {}
    for st in closed:
        try:
            rt = st.closed(st.exits[-1])
        except Exception:
            continue
        if rt.card_id:
            out[rt.card_id] = {"r_final": None if rt.r_multiple is None else round(float(rt.r_multiple), 2),
                               "exit_reason": rt.exit_reason, "exit_date": rt.exit_date.isoformat(),
                               "exit_price": round(float(rt.avg_exit), 2), "entry_price": round(float(rt.avg_entry), 2)}
    return out


def _paper_states(events: list[dict]) -> dict[str, dict]:
    """交易环对每张卡的最新状态 (台账 opportunity 事件): {card_id: {state, reason, at}}。"""
    out = {}
    for e in events:
        if e.get("kind") == "opportunity" and e.get("card_id"):
            out[e["card_id"]] = {"state": e.get("state"), "reason": e.get("reason") or "", "at": e.get("at")}
    return out


def _last_entry_close(d: date):
    """信号日 d 的入场窗最后一刻 (第 ENTRY_DAYS 个交易日收盘, 美东)。"""
    from datetime import timedelta

    from app.market_clock import us_clock
    days = us_clock.trading_days_between(d + timedelta(days=1), d + timedelta(days=ENTRY_DAYS * 3 + 7))
    last = days[ENTRY_DAYS - 1] if len(days) >= ENTRY_DAYS else d
    return datetime.combine(last, us_clock.session_close(last), tzinfo=us_clock.tz)


def apply_pm_rank(cards: list[dict], view: dict | None) -> list[dict]:
    """盘前定稿 (pm_rank.view) -> 卡面 pm 一行 + 同组内排前。组 = (状态, 是否不下单), 组顺序不变; 不增减卡。"""
    if not view:
        return cards
    info = view.get("cards") or {}
    out = []
    for c in cards:
        v = info.get(c.get("card_id"))
        out.append({**c, "pm": {k: v.get(k) for k in ("gap_atr", "pvr", "news_n", "priority")}} if isinstance(v, dict) else c)
    grp = lambda c: (c.get("state"), bool(c.get("blocked")) or c.get("verdict") == "skip")
    order: list = []
    for c in out:
        if grp(c) not in order:
            order.append(grp(c))
    return sorted(out, key=lambda c: (order.index(grp(c)), 0 if (c.get("pm") or {}).get("priority") is True else 1))


def build_live(data_dir: Path, *, repo=None, loop=None, today: date | None = None) -> dict[str, Any]:
    """机会页 (只读): 最近信号日的板 + 实时状态 + 灰色历史。

    板 = 纸面下单簿 (0.24.1): 簿里每张仍在入场窗的卡都显示 (同票不同信号日 = 两张限价单, 都可能成交);
    簿已落盘的信号日只显示簿里的卡, 落簿时被排除的卡进历史并写原因。
    state: holding (已成交) / in_zone (现价在区内) / pending (待回踩)。
    blocked (仍显示, 但今天不会开): holding_other 同票已有持仓 / cooldown 平仓冷却 / earnings 财报前一日 /
    direction_conflict 跨日多空冲突 / paper_watch 交易环准入阻断 (paper_reason 原样给出)。
    history (灰): expired 入场窗结束未回到挂单区 / closed 已平仓 (回合 R) / void 落簿时排除 / paper_expired 交易环作废。
    """
    from app.market_clock import us_clock
    from app.services import watchlist_book as wb
    now = us_clock.market_datetime(us_clock.now())
    today = today or now.date()
    base = build(data_dir, repo=repo)
    base["history"] = []
    base["refresh"] = refresh_info(data_dir, today)
    if not base.get("available"):
        return base
    files = sorted({d for d in _signal_days(data_dir) if d <= today}, reverse=True)
    positions, events = {}, []
    lg_ref = None
    try:
        from app.services import desk_overview as ov
        lg, _acct = ov._account(loop, data_dir)
        lg_ref = lg
        if lg is not None:
            positions = {p.card_id: p for p in lg.open_positions if p.card_id}
            events = lg.read_events()
    except Exception as e:
        logger.warning("swing_desk live: ledger unavailable: %s", type(e).__name__)
    closed = _closed_rounds(events)
    closed_ids = {e.get("card_id") for e in events if e.get("kind") == "close" and e.get("card_id")}
    paper = _paper_states(events)
    days = files[: ENTRY_DAYS + HOLD_DAYS + 2]
    boards = {}
    for d in days:
        b = base if d.isoformat() == base["as_of"] else board_for_day(data_dir, d, repo)
        if b is not None:
            boards[d] = b
    cards: list[dict] = []
    history: list[dict] = []
    # 持仓卡优先 (任一信号日)
    held_syms: set[str] = set()
    for d, b in boards.items():
        board_ids = {wb.card_id(c["symbol"], RULE_ID, c["side"], d) for c in b["long"] + b["short"]}
        for c in b["long"] + b["short"] + _book_only_cards(wb.load_day(data_dir, d), board_ids, d):
            cid = wb.card_id(c["symbol"], RULE_ID, c["side"], d)
            pos = positions.get(cid)
            if pos is None or c["symbol"] in held_syms:
                continue
            held_syms.add(c["symbol"])
            cards.append(dict(c, signal_day=d.isoformat(), card_id=cid, entry_day_n=ENTRY_DAYS, state="holding",
                              entry_price=round(float(pos.avg_cost), 4), qty=abs(int(pos.qty)),
                              entry_date=str(pos.entry_date) if getattr(pos, "entry_date", None) else None))
    held_any = {p.symbol for p in positions.values()}
    # 0.33.0: 持仓票已移出自选 -> 收盘定版记离场意向, 下一交易日开盘平仓 (卡面提示; 读不到自选 = 不标)
    if any(c.get("state") == "holding" for c in cards):
        watch_held = _watchlist_now()
        if watch_held:
            for c in cards:
                if c.get("state") == "holding" and c["symbol"] not in watch_held:
                    c["watch_removed"] = True
    entry_day = _entry_day(now, today)
    from app.services import swing_pm_check as pmc
    pm_void: dict[str, dict] = {}
    pm_seen: dict[str, dict] = {}
    for d in boards:
        try:
            recs = pmc.load(data_dir, pmc.first_entry_day(d))
        except ValueError:
            continue
        for k, v in recs.items():
            pm_seen[k] = v
            if v.get("void") is True:
                pm_void[k] = v
    for d, b in boards.items():                      # 新信号日在前
        n_open = len(us_clock.trading_days_between(d, today)) - (1 if us_clock.is_trading_day(d) else 0)
        book = wb.load_day(data_dir, d)
        in_book = {c.get("id") for c in (book or {}).get("cards") or []}
        conflict_ids = set((book or {}).get("conflict_ids") or [])
        excluded = {(x.get("symbol"), x.get("side")): x.get("reason") for x in (book or {}).get("skipped") or []
                    if x.get("symbol")}
        board_ids = {wb.card_id(c["symbol"], RULE_ID, c["side"], d) for c in b["long"] + b["short"]}
        # 0.26.0: 簿里有、当前信号表重算的板上没有的卡也要显示 (板 = 簿): 票已移出自选 -> 已结束 (交易环同样
        # not_in_watchlist 拦下); 票仍在自选 (例如重扫后规则不再命中) -> 交易环照样可能成交, 照常上板。
        extra = _book_only_cards(book, board_ids, d)
        watch_now = _watchlist_now() if extra else set()
        for c, book_only in [(x, False) for x in b["long"] + b["short"]] + [(x, True) for x in extra]:
            cid = wb.card_id(c["symbol"], RULE_ID, c["side"], d)
            if cid in positions:
                continue
            row = dict(c, signal_day=d.isoformat(), card_id=cid, entry_day_n=min(max(n_open, 0), ENTRY_DAYS))
            if cid in closed or cid in closed_ids:
                history.append(dict(row, state="closed", **(closed.get(cid) or {})))
                continue
            if book_only and c["symbol"] not in watch_now:
                history.append(dict(row, state="void", void_reason="not_in_watchlist",
                                    voided_at=(paper.get(cid) or {}).get("at")))
                continue
            if book is not None and cid not in in_book:
                history.append(dict(row, state="void", void_reason=excluded.get((c["symbol"], c["side"])) or "not_in_book",
                                    voided_at=(book or {}).get("built_at")))
                continue
            ps = paper.get(cid) or {}
            # 0.27.0: 做多第 1 天 09:00 盘前已越过止损 -> 作废 (swing_pm_check 记录; 交易环同时写台账 expired)
            pmr = pm_void.get(cid)
            if pmr is not None:
                history.append(dict(row, state="paper_expired", void_reason=pmc.REASON, voided_at=pmr.get("at"),
                                    pm_price=pmr.get("price")))
                continue
            if ps.get("state") == "expired":
                history.append(dict(row, state="paper_expired", void_reason=ps.get("reason"), voided_at=ps.get("at")))
                continue
            if window_over(n_open, today, now):
                history.append(dict(row, state="expired", void_reason="entry_window_over",
                                    voided_at=_last_entry_close(d).isoformat()))
                continue
            row["state"], row["blocked"] = "pending", None
            if cid in conflict_ids:
                row["blocked"] = "direction_conflict"
            elif c["symbol"] in held_any:
                row["blocked"] = "holding_other"
            elif ps.get("state") == "watch" and str(ps.get("at") or "")[:10] == today.isoformat():
                row["blocked"], row["paper_reason"] = "paper_watch", ps.get("reason")
            cards.append(row)
    # 跨信号日同票反向 (都在入场窗): 交易环两边都不开 (entry_guard direction_conflict)
    sides: dict[str, set] = {}
    for c in cards:
        if c["state"] != "holding":
            sides.setdefault(c["symbol"], set()).add(c["side"])
    pend = [c for c in cards if c["state"] != "holding"]
    cool = _cooldown(events, entry_day)
    earn = _earnings_blocked(data_dir, {c["symbol"] for c in pend}, entry_day)
    for c in pend:
        if c.get("blocked"):
            continue
        if len(sides.get(c["symbol"], ())) > 1:
            c["blocked"] = "direction_conflict"
        elif c["symbol"] in cool:
            c["blocked"] = "cooldown"
        elif c["symbol"] in earn:
            c["blocked"] = "earnings"
    syms = {c["symbol"] for c in cards}
    # 0.27.0 盘前量角标 (只展示, 不进裁决): 04:00 起盘前累计量 / 前 10 日同时段均量 (SIP 延迟 16 分钟, 带截至时刻)
    try:
        vp = _premarket_volume(syms, now)
    except Exception as e:
        logger.warning("swing_desk: premarket volume unavailable: %s", type(e).__name__)
        vp = {}
    for c in cards:
        v = vp.get(c["symbol"])
        if isinstance(v, dict):
            c["vol_pre"] = {k: v.get(k) for k in ("ratio", "level", "label", "through")}
    px = _live_prices(repo, syms, today, now)
    last_close = _last_closes(repo, syms - set(px), today)
    for c in cards:
        if c["symbol"] in px:
            p, src = px[c["symbol"]]
        elif c["symbol"] in last_close:
            p, asof = last_close[c["symbol"]]
            src = "close"
            c["price_asof"] = asof
        else:
            p, src = c.get("close"), "close"
            c["price_asof"] = c.get("signal_day")
        c["price"], c["price_source"] = p, src
        if p and c["state"] == "pending":
            lo, hi = c["entry_zone"]
            if lo <= p <= hi:
                c["state"] = "in_zone"
                c["dist_pct"] = 0.0
            else:
                edge = hi if c["side"] == "long" else lo
                c["dist_pct"] = round((edge / p - 1) * 100, 2)
            # 0.27.0 起只有“做多第 1 天 09:00 盘前越过止损”会作废 (swing_pm_check); 其余越止损只是一个状态,
            # 卡面的回区率/期望已按“越止损”那一档给出 (盘中越止损那一格历史期望为正)
            beyond = (p < c["stop"]) if c["side"] == "long" else (p > c["stop"])
            c["beyond_stop"] = bool(beyond)
        if p and c["state"] == "holding" and c.get("entry_price"):
            sg = 1 if c["side"] == "long" else -1
            c["r_now"] = round(sg * (p - c["entry_price"]) / c["risk"], 2) if c["risk"] else None
    # 0.27.0 卡面裁决 + 两个数字 (回区率 / 期望美元): 裁决只看交易环会不会挂这张单; 数字按状态查回放表
    try:
        from app.services import swing_verdict as sv
        eq, rk = _equity_and_risk(data_dir, lg_ref)
        sv.annotate(cards, now, equity=eq, risk_pct=rk, pm_records=pm_seen)
        base["risk_pct"] = rk
        base["equity"] = None if eq is None else round(eq, 2)
    except Exception as e:
        logger.warning("swing_desk: verdict unavailable: %s", type(e).__name__)
    order = {"holding": 0, "in_zone": 1, "pending": 2}
    for side in ("long", "short"):
        base[side] = sorted([c for c in cards if c["side"] == side],
                            key=lambda c: (order.get(c["state"], 9), bool(c.get("blocked")) or c.get("verdict") == "skip",
                                           -c["score"],
                                           abs(c.get("dist_pct") or 0), c["symbol"], c.get("signal_day") or ""))
    # 0.31.0 盘前定稿 (无限 10-21 授权): 09:20 ET 起卡面带盘前一行; 同向放量卡在同组内排前 (只重排, 不增减卡)
    try:
        from app.services import pm_rank
        pmv = pm_rank.view(data_dir, now)
        if pmv:
            base["premarket_final"] = {"at": pmv["at"], "spy_chg": pmv.get("spy_chg")}
            for side in ("long", "short"):
                base[side] = apply_pm_rank(base[side], pmv)
    except Exception as e:
        logger.warning("swing_desk: pm rank unavailable: %s", type(e).__name__)
    history.sort(key=lambda c: (c.get("signal_day") or "", c["symbol"]), reverse=True)
    base["history"] = history[:HISTORY_MAX]
    base["today"] = today.isoformat()
    base["entry_day"] = entry_day.isoformat()
    # 0.26.0 卡面那一行: 同类卡的回放基础率 (回区/赢/目标/止损/到期), 多空各一份; 只展示, 不进打分/排序/下单
    try:
        from app.services import swing_settlement as ss
        cur_l, cur_s = strategies_from_ruleset(data_dir)
        base["base_rates"] = {"long": ss.card_line("long", cur_l), "short": ss.card_line("short", cur_s)}
    except Exception as e:
        logger.warning("swing_desk: base rates unavailable: %s", type(e).__name__)
        base["base_rates"] = {"long": None, "short": None}
    base["now"] = now.isoformat(timespec="seconds")
    return base


HISTORY_MAX = 40


def _equity_and_risk(data_dir: Path, lg) -> tuple[float | None, float | None]:
    """(纸面权益, 单笔风险小数), 用来把期望 R 折成本卡计划股数下的美元。
    权益 = 台账现金 + 持仓按成本估值 (卡面只用来算股数, 不需要逐秒估值); 风险 = 当前许可 (规则集 risk_per_trade_pct)。
    读不到 -> None (卡面显示 R, 不显示美元)。"""
    eq = rk = None
    try:
        if lg is not None:
            eq = float(lg.nav({}))
    except Exception as e:
        logger.warning("swing_desk: equity unavailable: %s", type(e).__name__)
    try:
        from app.services import watchlist_v4 as wv
        ok, risk, _tier = wv.rule_permission(wv.permission_cached(data_dir), RULE_ID, "long")
        rk = risk if ok else None
    except Exception as e:
        logger.warning("swing_desk: risk unavailable: %s", type(e).__name__)
    return eq, rk


# ── 每 30 分钟刷新 (无限 10-16: 盘前北京时间 20:00 起, 每 30 分钟更新卡; 作废的卡变灰并留在历史) ──
REFRESH_START_CN = (20, 0)       # 北京时间 20:00 (夏令时 = 美东 08:00, 冬令时 = 美东 07:00), 都在美股盘前


def live_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "swing_live"


def refresh_due(now) -> bool:
    """当前时刻是否在刷新窗: 美东交易日, 北京 20:00 (同一日历日) 起到美东收盘 (含; 半日市 13:00, 审计 L-4)。"""
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    from app.market_clock import us_clock
    et = us_clock.market_datetime(now)
    d = et.date()
    if not us_clock.is_trading_day(d):
        return False
    start = datetime.combine(d, dtime(*REFRESH_START_CN), tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(us_clock.tz)
    end = datetime.combine(d, us_clock.session_close(d), tzinfo=us_clock.tz)
    return start <= et <= end


def _brief(c: dict) -> dict:
    return {k: c.get(k) for k in ("card_id", "symbol", "side", "signal_day", "state", "blocked", "price",
                                   "price_source", "dist_pct", "r_now", "void_reason", "r_final")}


def refresh_info(data_dir: Path, day: date) -> dict:
    """今天 (美东) 的刷新记录摘要 (只读)。"""
    p = live_dir(data_dir) / f"{day.isoformat()}.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"count": 0, "last_at": None, "changes": []}
    snaps = raw.get("snapshots") if isinstance(raw, dict) else None
    snaps = snaps if isinstance(snaps, list) else []
    return {"count": len(snaps), "last_at": snaps[-1].get("at") if snaps else None,
            "changes": (raw.get("changes") or [])[-30:] if isinstance(raw, dict) else []}


def snapshot(data_dir: Path, *, repo=None, loop=None, now=None) -> dict:
    """落一次机会板快照到 opportunities/swing_live/<美东日>.json (只追加; 同一分钟重复调用不重复写)。
    与上一快照比较, 卡的状态变化 (进区/成交/作废/阻断) 记入 changes, 机会页历史区按时间显示。"""
    from app.market_clock import us_clock
    from app.services.fs_utils import atomic_write_text
    now = us_clock.market_datetime(now or us_clock.now())
    board = build_live(data_dir, repo=repo, loop=loop, today=now.date())
    at = now.isoformat(timespec="minutes")
    cur = {c["card_id"]: _brief(c) for c in board.get("long", []) + board.get("short", []) + board.get("history", [])
           if c.get("card_id")}
    path = live_dir(data_dir) / f"{now.date().isoformat()}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        raw = {}
    snaps = raw.get("snapshots") if isinstance(raw.get("snapshots"), list) else []
    changes = raw.get("changes") if isinstance(raw.get("changes"), list) else []
    if snaps and snaps[-1].get("at") == at:
        return {"written": False, "at": at, "count": len(snaps)}
    prev = {c["card_id"]: c for c in (snaps[-1].get("cards") or [])} if snaps else {}
    for cid, c in cur.items():
        p = prev.get(cid)
        before = (p or {}).get("state"), (p or {}).get("blocked")
        after = c.get("state"), c.get("blocked")
        if p is not None and before != after:
            changes.append({"at": at, "card_id": cid, "symbol": c["symbol"], "side": c["side"],
                            "from": before[0], "to": after[0], "blocked": after[1], "reason": c.get("void_reason")})
    snaps.append({"at": at, "as_of": board.get("as_of"), "cards": list(cur.values())})
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"day": now.date().isoformat(), "snapshots": snaps, "changes": changes},
                                       ensure_ascii=False))
    return {"written": True, "at": at, "count": len(snaps), "cards": len(cur)}


def window_over(n_open: int, today: date, now) -> bool:
    """入场窗是否已结束: 信号日后已开 n_open 个交易日 (含今天)。超过 ENTRY_DAYS, 或恰好第 ENTRY_DAYS 天且今天已收盘。"""
    from app.market_clock import us_clock
    if n_open > ENTRY_DAYS:
        return True
    if n_open < ENTRY_DAYS:
        return False
    # 恰好第 ENTRY_DAYS 个交易日: 今天收盘后, 或今天不是交易日 (周末/假日 = 最后入场日已收盘)
    return not us_clock.is_trading_day(today) or (now.date() == today and now.time() >= us_clock.session_close(today))


def _signal_days(data_dir: Path) -> list[date]:
    d = Path(data_dir) / "opportunities" / "watchlist"
    out = []
    for f in d.glob("20*.json") if d.exists() else []:
        try:
            day = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if f.stem == day.isoformat():
            out.append(day)
    return out
