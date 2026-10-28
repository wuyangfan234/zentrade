"""自选机会 v4 (2026-10-06): 规则集版本 + 自选影子账本 (同票基线) + 前向确认。预登记 = design/watchlist-v4-preregistration.md。

- 规则集: data/rules/R{n}.json 一版一文件 (冻结) + data/rules/current.json 指针; 改勾选/参数 = 新版本。
- 自选影子账本: data/decision_desk/shadow_watchlist.jsonl。盘后扫描时写入: 全部规则在自选上的全部 +1 命中 (规则卡)
  + 每只自选每档止损一张同票基线卡 (__same_symbol__)。结算与 v3.1 影子账本同一回放。
- 前向确认: 勾选规则合并 n≥100、≥3 个月、按月单侧 p < 0.05/V、绝对 R>0 → 可下单; 单条规则前向 n≥30 且超额<0 剔除;
  最近 3 个月合并超额 ≤0 → 回落。全部自动, 读失败 = 不可下单 (fail-closed)。
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import threading
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BASELINE_ID = "__same_symbol__"
SHORT_BASELINE_ID = "__same_symbol_short__"
FORWARD_START = date(2026, 10, 6)
MIN_N = 100
MIN_MONTHS = 3
ALPHA = 0.05
RULE_MIN_N = 30
RECENT_MONTHS = 3
DEFAULT_STOP = -0.06
NATIVE = "native"
DEFAULT_ENABLED = ("volume_price_surge", "macd_golden", "ma_golden_cross", "ma_convergence_breakout",
                   "trend_breakout", "breakout_new_high_60d", "pullback_ma20_bounce")
DEFAULT_PARAMS = {"risk_per_trade_pct": 1.0}   # 只放真正接进交易链路的参数; 入场窗/跳空作废/财报/7 日沿用交易员现行规则
_LOCK = threading.Lock()


# ── 规则集 ────────────────────────────────────────────────────

def rules_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "rules"


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def risk_pct_of(rs) -> float | None:
    """规则集的单笔风险 (%): 有限数且 0 < x ≤ 2 才有效, 否则 None (= 不可下单)。"""
    try:
        v = (rs.get("params") or {}).get("risk_per_trade_pct", DEFAULT_PARAMS["risk_per_trade_pct"])
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        v = float(v)
    except (AttributeError, TypeError, ValueError):
        return None
    return v if math.isfinite(v) and 0 < v <= 2 else None


def _valid(rs, version: int) -> bool:
    return (isinstance(rs, dict) and rs.get("version") == version and isinstance(rs.get("enabled"), list)
            and all(isinstance(x, str) for x in rs["enabled"]) and isinstance(rs.get("params", {}), dict)
            and ("short" not in rs or (isinstance(rs["short"], list)
                                       and all(isinstance(x, str) for x in rs["short"])))
            and ("evo" not in rs or isinstance(rs["evo"], bool))
            and risk_pct_of(rs) is not None)


def current_ruleset(data_dir: Path) -> dict:
    """当前规则集。从未保存过任何版本 → 默认 R0 (未落盘: 只展示/影子记账, 永不下单)。
    指针或版本文件损坏/缺失/字段非法 → invalid (什么都不勾选, 不可下单; 不回退默认, 重新保存即恢复)。"""
    d = rules_dir(data_dir)
    ptr_path = d / "current.json"
    if not ptr_path.exists() and not versions(data_dir):
        return {"version": 0, "enabled": list(DEFAULT_ENABLED), "short": [], "params": dict(DEFAULT_PARAMS),
                "created_at": None}
    ptr = _read(ptr_path)
    v = ptr.get("version") if isinstance(ptr, dict) else None
    if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
        rs = _read(d / f"R{v}.json")
        if _valid(rs, v):
            return rs
    return {"version": None, "enabled": [], "short": [], "params": dict(DEFAULT_PARAMS), "created_at": None,
            "invalid": True}


def versions(data_dir: Path) -> list[int]:
    d = rules_dir(data_dir)
    out = []
    for p in d.glob("R*.json") if d.exists() else []:
        if p.stem[1:].isdigit():
            out.append(int(p.stem[1:]))
    return sorted(out)


def save_ruleset(data_dir: Path, enabled: list[str], params: dict, note: str = "",
                 short: list[str] | None = None, evo: bool | None = None) -> dict:
    """新建一个版本 (冻结) 并切换指针。与当前完全相同则不建新版本。
    short=None = 沿用当前版本的看空列表 (界面只改看多时不动它); 给了就只留 short_capable 的规则。"""
    from app.services.fs_utils import atomic_write_text
    with _LOCK:
        cur = current_ruleset(data_dir)
        clean = {**DEFAULT_PARAMS, **{k: v for k, v in (params or {}).items() if k in DEFAULT_PARAMS}}
        en = sorted(dict.fromkeys(str(x) for x in enabled))
        sh = sorted({s for s in (str(x) for x in short) if short_capable(s)}) if short is not None \
            else sorted(cur.get("short") or [])
        ev = bool(cur.get("evo")) if evo is None else bool(evo)
        if cur.get("version") and sorted(cur.get("enabled") or []) == en and cur.get("params") == clean \
                and sorted(cur.get("short") or []) == sh and bool(cur.get("evo")) == ev:
            return cur
        n = (max(versions(data_dir)) if versions(data_dir) else 0) + 1
        rs = {"version": n, "enabled": en, "short": sh, "evo": ev, "params": clean, "note": note[:200],
              "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        d = rules_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"R{n}.json"
        if p.exists():
            raise FileExistsError(p)
        atomic_write_text(p, json.dumps(rs, ensure_ascii=False, indent=1))
        atomic_write_text(d / "current.json", json.dumps({"version": n}))
        return rs


# ── 做空大盘门槛: 信号日 SPY 收盘 < 50 日均线才允许开空 ──
SHORT_GATE_INDEX = "SPY"
SHORT_GATE_MA = 50
_SHORT_GATE_CACHE: dict = {}


def short_gate(repo, day: date) -> dict:
    """{"day", "ok": True|False|None, "close", "ma50"}。ok=True 当且仅当 day 当天 SPY 已收盘日K存在、≥50 根、
    收盘 < 最近 50 根收盘均值 (含当天)。ok=False = 数据齐但不满足; ok=None = 数据缺/读失败 (调用方 fail-closed)。
    只缓存数据齐的结果 (10 分钟)。"""
    import time
    from datetime import timedelta
    out = {"day": day.isoformat(), "ok": None, "close": None, "ma50": None}
    key = (day, id(repo))
    hit = _SHORT_GATE_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return dict(hit[1])
    if repo is None:
        return out
    try:
        b = repo.get_daily_batch([SHORT_GATE_INDEX], day - timedelta(days=130), day,
                                 columns=["symbol", "date", "close"])
        if b is None or b.is_empty():
            return out
        g = b.filter(b["symbol"] == SHORT_GATE_INDEX).sort("date")
        g = g.filter(g["date"] <= day)
        if g.height < SHORT_GATE_MA or g["date"][-1] != day:
            return out
        cl = [float(x) for x in g["close"].to_list()]
        if any(not math.isfinite(x) or x <= 0 for x in cl):
            return out
        ma = statistics.fmean(cl[-SHORT_GATE_MA:])
        out.update(ok=cl[-1] < ma, close=round(cl[-1], 4), ma50=round(ma, 4))
    except Exception as e:
        logger.warning("short gate %s: daily bars unavailable: %s", day, type(e).__name__)
        return out
    if len(_SHORT_GATE_CACHE) > 64:
        _SHORT_GATE_CACHE.clear()
    _SHORT_GATE_CACHE[key] = (time.monotonic(), dict(out))
    return out


# ── 机会卡显示 (看) ──────────────────────────────────────────
# 2026-10-09 无限: “加入到策略里面, 同步到机会卡 … 我用来分析行情使用”。显示 (看) 与交易 (规则集 R{n}) 分开:
# 看 = 哪些规则的信号出卡 (多/空各一组), 只影响展示, 不建版本、不进下单许可; 交易仍只看规则集 + 前向确认。
# 规则集里勾选的规则其多头信号永远出卡 (交易的规则必须看得见)。
TV_RULES = ("tv_supertrend", "tv_ut_bot", "tv_chandelier_exit", "tv_halftrend", "tv_zlsma_cross", "tv_squeeze_momentum",
            "tv_wavetrend", "tv_smc_structure", "tv_phantom_shift", "tv_phantom_trail")
# 卖出信号按源脚本即反向信号 (TV 图上的 Sell / 翻空 / 空头结构) → 可出看空卡。挤压动能的“卖”是动能转弱 (自定), 默认不出看空。
SELL_IS_SHORT = frozenset(TV_RULES)
DEFAULT_DISPLAY = {"long": [*TV_RULES, "phantom_4h_pine"],
                   "short": [r for r in TV_RULES if r != "tv_squeeze_momentum"] + ["phantom_4h_short"]}


def display_path(data_dir: Path) -> Path:
    return rules_dir(data_dir) / "display.json"


def display_rules(data_dir: Path) -> dict:
    """{"long": set, "short": set, "saved": bool}。文件缺失 → 默认; 损坏 → 默认 (展示层, 不影响交易)。"""
    d = _read(display_path(data_dir))
    if isinstance(d, dict) and isinstance(d.get("long"), list) and isinstance(d.get("short"), list):
        return {"long": {x for x in d["long"] if isinstance(x, str)}, "short": {x for x in d["short"] if isinstance(x, str)},
                "saved": True}
    return {"long": set(DEFAULT_DISPLAY["long"]), "short": set(DEFAULT_DISPLAY["short"]), "saved": False}


EVO_RULE = "evo"   # 历史自进化卡规则名（出卡已退役，纸面名额识别仍可能遇到）
SWING_RULE = "swing"   # tight-v1 机会板卡 (= swing_desk.RULE_ID)


def short_capable(rule: str) -> bool:
    from app.services import decision_desk as dd
    return rule in dd.SHORT_SOURCES or rule in SELL_IS_SHORT     # evo 不在这里: 只能经规则集 evo 开关放行 (审查 #2)


def save_display(data_dir: Path, long: list[str], short: list[str]) -> dict:
    from app.services.fs_utils import atomic_write_text
    with _LOCK:
        out = {"long": sorted(dict.fromkeys(str(x) for x in long)),
               "short": sorted(dict.fromkeys(str(x) for x in short if short_capable(str(x)))),
               "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        rules_dir(data_dir).mkdir(parents=True, exist_ok=True)
        atomic_write_text(display_path(data_dir), json.dumps(out, ensure_ascii=False, indent=1))
        return out


# ── 影子账本 ──────────────────────────────────────────────────

def path(data_dir: Path) -> Path:
    return Path(data_dir) / "decision_desk" / "shadow_watchlist.jsonl"


def _row_ok(r) -> bool:
    if not (isinstance(r, dict) and isinstance(r.get("rule"), str) and isinstance(r.get("symbol"), str)):
        return False
    try:
        date.fromisoformat(r.get("as_of"))
    except (TypeError, ValueError):
        return False
    return r.get("outcome") is None or isinstance(r.get("outcome"), dict)


def load_checked(data_dir: Path) -> tuple[list[dict], int]:
    """(合法行, 损坏行数)。损坏 = 非 JSON / 非对象 / 缺关键字段。有损坏时: 前向确认不晋级, 记账与结算拒写 (不静默丢行)。"""
    p = path(data_dir)
    if not p.exists():
        return [], 0
    out, bad = [], 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if _row_ok(r):
            out.append(r)
        else:
            bad += 1
    return out, bad


def load(data_dir: Path) -> list[dict]:
    return load_checked(data_dir)[0]


def _save(data_dir: Path, rows: list[dict]) -> None:
    from app.services.fs_utils import atomic_write_text
    path(data_dir).parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    atomic_write_text(path(data_dir), text + ("\n" if text else ""))


def _last_close(b) -> float | None:
    try:
        v = float(b["close"][-1])
    except Exception:  # noqa: BLE001
        return None
    return v if math.isfinite(v) and v > 0 else None


def _row(rule: str, sym: str, card, as_of: date, stop, ref, now_s: str, extra: dict | None = None) -> dict:
    return {"as_of": as_of.isoformat(), "rule": rule, "symbol": sym, "stop": stop,
            "entry_zone": [round(float(x), 4) for x in card.entry_zone], "invalidation": float(card.invalidation),
            "target_zone": [round(float(x), 4) for x in card.target_zone], "risk": float(card.risk_per_share),
            "ref_close": ref, "snapshot_at": now_s, "outcome": None, **(extra or {})}


def signals_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "watchlist"


def save_signals(data_dir: Path, as_of: date, signals: dict[str, list[dict]], *, watchlist: set[str], frames: dict,
                 lineage: dict | None = None, row_extra=None, extra_rows: list[dict] | None = None,
                 h4_state: dict | None = None, atr: dict | None = None) -> None:
    """当日自选信号表 (机会页读这份): 每条规则命中的票与卡几何 (side: long/short; 旧文件无此字段 = long),
    以及扫描时的自选名单与收盘价。不含许可 (读时现算)。看空行只供机会卡展示, 自选表/盘前推送只读看多行。
    lineage / row_extra (2026-10-11 方向 A 谱系, card_lineage.stamp): 只追加新键 (顶层 lineage, 每行 mode/params_hash),
    不改任何已有键; 读方 (机会卡/下单簿/盘前推送) 不读这些键 (黄金样本测试锁定)。"""
    from app.services.fs_utils import atomic_write_text
    rows = []
    for rule, items in signals.items():
        for it in items:
            c = it["card"]
            rows.append({"rule": rule, "symbol": it["symbol"], "side": getattr(c, "side", "long") or "long",
                         "stop": it.get("stop"), "rank": it.get("rank"),
                         "total": it.get("total"), "entry_zone": [float(x) for x in c.entry_zone],
                         "invalidation": float(c.invalidation), "target_zone": [float(x) for x in c.target_zone],
                         "risk": float(c.risk_per_share), "rr": float(c.reward_risk), "card_id": c.id})
    if row_extra is not None:
        for r in rows:
            try:
                r.update({k: v for k, v in (row_extra(r) or {}).items() if k not in r})
            except Exception:  # noqa: BLE001 - 谱系失败不影响落表
                pass
    # 历史自进化卡行: 若信号表仍带 evo 元数据则原样追加（新扫描不再注入）
    for r in extra_rows or []:
        if isinstance(r, dict) and isinstance(r.get("symbol"), str) and isinstance(r.get("rule"), str) \
                and r["symbol"] in watchlist:
            rows.append(dict(r))
    closes = {s: _last_close(frames.get(s)) for s in watchlist}
    payload = {"as_of": as_of.isoformat(), "watchlist": sorted(watchlist), "closes": closes, "signals": rows,
               "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    if lineage:
        payload["lineage"] = lineage
    # tight-v1 (只追加新键): 自选 4h 方向状态 + 日线 ATR14, 机会板出卡用; 旧读方不读
    if h4_state:
        payload["h4_state"] = {s: v for s, v in h4_state.items() if s in watchlist and v in ("long", "short")}
    if atr:
        payload["atr"] = {s: v for s, v in atr.items() if s in watchlist}
    d = signals_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(d / f"{as_of.isoformat()}.json", json.dumps(payload, ensure_ascii=False))


def signal_files(data_dir: Path) -> list[tuple[date, Path]]:
    """自选信号表文件 (只认 YYYY-MM-DD.json), 按日期升序。"""
    d = signals_dir(data_dir)
    out = []
    for f in d.glob("20*.json") if d.exists() else []:
        try:
            day = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if f.stem == day.isoformat():
            out.append((day, f))
    return sorted(out)


def latest_signals(data_dir: Path) -> dict | None:
    files = signal_files(data_dir)
    return _read(files[-1][1]) if files else None


def record(data_dir: Path, as_of: date, *, watchlist: set[str], frames: dict, signals: dict[str, list[dict]],
           baseline_stops: set, now: datetime | None = None, short_signals: dict | None = None) -> dict:
    """signals: {rule: [{symbol, card(OpportunityCard), stop}]} = 当日自选上全部 +1 命中 (已成卡的)。
    short_signals: {rule: [{symbol, card(空头卡), stop}]} = 当日看空命中, 行记 rule="<id>:short" + side="short",
    并给每只有日K的自选补一张无止损的同票空头基线卡 (__same_symbol_short__)。short_signals=None = 不记空头。
    基线: 每只有日K的自选 × 每档止损一张 card_from_pick 卡。同一 (as_of, rule, symbol, stop) 只记一次。失败不抛。"""
    now_s = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _LOCK:
            from app.services import decision_desk as dd
            rows, bad = load_checked(data_dir)
            if bad:
                logger.error("watchlist shadow ledger has %d corrupt lines: record refused (fail-closed)", bad)
                return {}
            seen = {(r["as_of"], r["rule"], r["symbol"], str(r.get("stop")) if r["rule"] in (BASELINE_ID, SHORT_BASELINE_ID) else "*")
                    for r in rows}
            added = defaultdict(int)
            day = as_of.isoformat()
            for rule, items in (signals or {}).items():
                for it in items:
                    sym, card, stop = it["symbol"], it["card"], it.get("stop")
                    key = (day, rule, sym, "*")   # 规则卡: 同日同规则同票只记一次 (止损改了也不重复记)
                    if sym not in watchlist or key in seen or card is None:
                        continue
                    rows.append(_row(rule, sym, card, as_of, stop, _last_close(frames.get(sym)), now_s,
                                     {"per_symbol_stop": True} if it.get("per_symbol_stop") else None))
                    seen.add(key)
                    added[rule] += 1
            for rule, items in (short_signals or {}).items():
                rid = f"{rule}:short"
                for it in items:
                    sym, card, stop = it["symbol"], it["card"], it.get("stop")
                    key = (day, rid, sym, "*")
                    if sym not in watchlist or key in seen or card is None:
                        continue
                    rows.append(_row(rid, sym, card, as_of, stop, _last_close(frames.get(sym)), now_s,
                                     {"side": "short", **({"per_symbol_stop": True} if it.get("per_symbol_stop") else {})}))
                    seen.add(key)
                    added[rid] += 1
            stops = sorted({s for s in baseline_stops if s != NATIVE} | {DEFAULT_STOP}, key=lambda x: (x is None, x or 0))
            for sym in sorted(watchlist):
                b = frames.get(sym)
                if b is None:
                    continue
                for stop in stops:
                    key = (day, BASELINE_ID, sym, str(stop))
                    if key in seen:
                        continue
                    card = dd.card_from_pick(BASELINE_ID, sym, b, as_of=as_of, stop_loss=stop)
                    if card is None:
                        continue
                    rows.append(_row(BASELINE_ID, sym, card, as_of, stop, _last_close(b), now_s))
                    seen.add(key)
                    added[BASELINE_ID] += 1
                if short_signals is not None:               # 空头同票基线: 无止损 (空头统一卡止损 = 2ATR)
                    key = (day, SHORT_BASELINE_ID, sym, str(None))
                    if key not in seen:
                        card = dd.card_from_pick(SHORT_BASELINE_ID, sym, b, as_of=as_of, stop_loss=None, side="short")
                        if card is not None:
                            rows.append(_row(SHORT_BASELINE_ID, sym, card, as_of, None, _last_close(b), now_s,
                                             {"side": "short"}))
                            seen.add(key)
                            added[SHORT_BASELINE_ID] += 1
            if added:
                _save(data_dir, rows)
            return dict(added)
    except Exception as e:  # noqa: BLE001
        logger.warning("watchlist shadow record failed (ignored): %s", type(e).__name__)
        return {}


def settle(data_dir: Path, repo, *, today: date) -> int:
    """结算满期影子卡 (复用 v3.1 影子账本的结算逻辑: 拆并股换基准、停止交易按最后收盘平仓)。"""
    from app.services import shadow_forward as sf
    with _LOCK:
        rows, bad = load_checked(data_dir)
        if bad:
            logger.error("watchlist shadow ledger has %d corrupt lines: settle refused (fail-closed)", bad)
            return 0
        tmp = [dict(r, source=r["rule"], stop_loss=r.get("stop")) for r in rows]
        n = sf.settle_rows(tmp, repo, today=today)
        if n:
            for r, t in zip(rows, tmp):
                r["outcome"] = t.get("outcome")
            _save(data_dir, rows)
        return n


# ── 前向统计 ──────────────────────────────────────────────────

def _closed(rows):
    return [r for r in rows if (r.get("outcome") or {}).get("status") == "closed" and isinstance(r["outcome"].get("r"), (int, float))
            and date.fromisoformat(r["as_of"]) >= FORWARD_START]


def excess_rows(data_dir: Path, rows: list[dict] | None = None) -> list[dict]:
    """规则卡 → {rule, symbol, day, r, ex}; ex = r − 该票同止损档全部已结算基线卡均 R (原生/逐股止损按 −0.06 档)。"""
    rows = load(data_dir) if rows is None else rows
    cl = _closed(rows)
    base: dict[tuple, list[float]] = defaultdict(list)
    base_short: dict[str, list[float]] = defaultdict(list)
    for r in cl:
        if r["rule"] == BASELINE_ID:
            base[(r["symbol"], str(r.get("stop")))].append(r["outcome"]["r"])
        elif r["rule"] == SHORT_BASELINE_ID:
            base_short[r["symbol"]].append(r["outcome"]["r"])          # 空头基线无止损 (stop=None)
    out = []
    for r in cl:
        if r["rule"] in (BASELINE_ID, SHORT_BASELINE_ID):
            continue
        if r.get("side") == "short":
            b = base_short.get(r["symbol"])                            # 空头规则卡一律对无止损空头基线
            out.append({"rule": r["rule"], "symbol": r["symbol"], "day": r["as_of"], "signal_day": r["as_of"],
                        "r": r["outcome"]["r"],
                        "ex": (r["outcome"]["r"] - statistics.fmean(b)) if b else None})
            continue
        stop = r.get("stop")
        key = (r["symbol"], str(stop if isinstance(stop, (int, float)) or stop is None else DEFAULT_STOP))
        if r.get("per_symbol_stop") or stop == NATIVE:
            key = (r["symbol"], str(DEFAULT_STOP))
        b = base.get(key)
        out.append({"rule": r["rule"], "symbol": r["symbol"], "day": r["as_of"], "signal_day": r["as_of"], "r": r["outcome"]["r"],
                    "ex": (r["outcome"]["r"] - statistics.fmean(b)) if b else None})
    return out


_STATUS_CACHE: dict = {}


def _sig(p: Path):
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def status(data_dir: Path, ruleset: dict | None = None) -> dict:
    """带缓存 (影子账本/规则指针/版本列表的文件签名; 机会页每 15 秒刷新)。返回副本。"""
    import copy
    rs = ruleset or current_ruleset(data_dir)
    key = (str(Path(data_dir).resolve()), _sig(path(data_dir)), _sig(rules_dir(data_dir) / "current.json"),
           tuple(versions(data_dir)), json.dumps(rs, sort_keys=True, default=str))
    hit = _STATUS_CACHE.get(key[0])
    if hit and hit[0] == key:
        return copy.deepcopy(hit[1])
    out = _status_uncached(data_dir, rs)
    _STATUS_CACHE[key[0]] = (key, out)
    return copy.deepcopy(out)


def _status_uncached(data_dir: Path, ruleset: dict | None = None) -> dict:
    """当前规则集的前向确认状态。任何异常 → promoted=False (不可下单)。"""
    from app.services import evidence_v3 as ev3
    rs = ruleset or current_ruleset(data_dir)
    out = {"version": rs.get("version"), "enabled": rs.get("enabled") or [], "promoted": False, "tradable_rules": [],
           "n": 0, "months": 0, "excess": None, "abs_mean": None, "p": None, "threshold": None, "per_rule": {},
           "per_symbol_rule": {}, "ledger_corrupt": 0, "ruleset_invalid": bool(rs.get("invalid"))}
    try:
        rows, bad = load_checked(data_dir)
        out["ledger_corrupt"] = bad
        ex = [x for x in excess_rows(data_dir, rows) if x["ex"] is not None]
        V = max(versions(data_dir) + [rs.get("version") if isinstance(rs.get("version"), int) else 0, 1])
        out["threshold"] = round(ALPHA / V, 5)
        en = set(out["enabled"])
        per_rule = defaultdict(list)
        per_sym = defaultdict(list)
        for x in ex:
            per_rule[x["rule"]].append(x)
            per_sym[(x["symbol"], x["rule"])].append(x["r"])
        out["per_rule"] = {k: {"n": len(v), "mean_r": round(statistics.fmean(y["r"] for y in v), 4),
                               "excess": round(statistics.fmean(y["ex"] for y in v), 4)} for k, v in per_rule.items()}
        out["per_symbol_rule"] = {f"{s}|{k}": {"n": len(v), "mean_r": round(statistics.fmean(v), 4)}
                                  for (s, k), v in per_sym.items()}
        pooled = [x for x in ex if x["rule"] in en]
        months = sorted({x["day"][:7] for x in pooled})
        st = ev3.cluster_boot(pooled, "ex", unit="month") if pooled else {"n": 0, "mean": None, "p": None}
        abs_mean = statistics.fmean(x["r"] for x in pooled) if pooled else None
        out.update(n=len(pooled), months=len(months), excess=st.get("mean"), p=st.get("p"),
                   abs_mean=round(abs_mean, 4) if abs_mean is not None else None)
        ok = (len(pooled) >= MIN_N and len(months) >= MIN_MONTHS and st.get("p") is not None
              and st["p"] < ALPHA / V and (abs_mean or 0) > 0)
        if ok:
            recent = set(months[-RECENT_MONTHS:])
            rec = [x["ex"] for x in pooled if x["day"][:7] in recent]
            if rec and statistics.fmean(rec) <= 0:
                ok = False
        ok = ok and not bad and not rs.get("invalid")
        out["promoted"] = bool(ok)
        if ok:
            out["tradable_rules"] = sorted(k for k in en if not (out["per_rule"].get(k, {}).get("n", 0) >= RULE_MIN_N
                                                                  and (out["per_rule"][k]["excess"] or 0) < 0))
    except Exception as e:  # noqa: BLE001
        logger.warning("watchlist forward status failed (fail-closed): %s", type(e).__name__)
        out["promoted"], out["tradable_rules"] = False, []
    return out


def permission(data_dir: Path) -> dict:
    """下单许可的唯一出处 (v4, 0.11.0 单一口径)。任何异常/非法 → 全空 (fail-closed)。

    0.11.0 (无限 2026-10-13 拍板, 退役 试用/正式/观察 分档): 已保存规则集 (R1 起) 里勾选的看多规则 = 可下单,
    单笔风险 = 规则集 risk_per_trade_pct (默认 1%); 规则集外 = 不可。做空按规则集 short 列表 + 大盘门槛
    (short_gate: 信号日 SPY 收盘 < 50 日均线)。前向确认 status() 仅作只读统计, 不参与许可。"""
    try:
        rs = current_ruleset(data_dir)
        st = status(data_dir, rs)
        risk = risk_pct_of(rs)
        ver = rs.get("version")
        saved = isinstance(ver, int) and not isinstance(ver, bool) and ver >= 1 and not rs.get("invalid")
        enabled = set(x for x in rs.get("enabled") or [] if isinstance(x, str))
        tradable = enabled if (saved and risk) else set()
        short_tradable = {x for x in rs.get("short") or [] if isinstance(x, str) and short_capable(x)} \
            if (saved and risk) else set()
        if saved and risk and rs.get("evo") is True:      # 0.15.0 自进化卡: 开关开 = 只交易 evo 卡, 多空都放行 (空头再过 SPY<50 日线)
            tradable, short_tradable = {EVO_RULE}, {EVO_RULE}
        # tight-v1 (0.24.0, 无限 10-15 "完全重构"): 机会板模式 (rules/swing.json 存在) = 纸面只交易机会板卡 (规则名 swing),
        # 板与纸面同一张卡; 旧规则集勾选不再开新仓 (持仓照常管理)。风险仍取规则集 risk_per_trade_pct。
        if saved and risk and (Path(data_dir) / "rules" / "swing.json").exists():
            tradable, short_tradable = {SWING_RULE}, {SWING_RULE}
        return {"ruleset": rs, "status": st, "tradable": tradable,
                "risk_pct": (risk or 0.0) / 100 if (tradable or short_tradable) else 0.0,
                "short_tradable": short_tradable}
    except Exception as e:  # noqa: BLE001
        logger.warning("watchlist v4 permission failed (fail-closed): %s", type(e).__name__)
        return {"ruleset": {"version": None, "enabled": [], "params": dict(DEFAULT_PARAMS), "invalid": True},
                "status": {"promoted": False, "tradable_rules": [], "per_rule": {}, "per_symbol_rule": {}},
                "tradable": set(), "risk_pct": 0.0, "short_tradable": set()}


TIER = "rule"   # 0.11.0 起只有一档; 写进卡/纸面台账的 tier/entry_tier 字段值 (字段与格式不变)


def rule_permission(pm: dict, rule: str, side: str) -> tuple[bool, float, str]:
    """(可下单, 单笔风险小数, 档位 'rule'|'none'): 一条规则在一个方向上的当前许可。做空 = 规则集 short 列表。"""
    if side == "short":
        ok, risk = rule in (pm.get("short_tradable") or set()), pm.get("risk_pct")
    else:
        ok, risk = rule in (pm.get("tradable") or set()), pm.get("risk_pct")
    if not (ok and type(risk) in (int, float) and math.isfinite(risk) and 0 < risk <= 0.02):
        return False, 0.0, "none"
    return True, float(risk), TIER


_PM_CACHE: dict = {}
PM_CACHE_MAX_AGE_S = 30


def permission_cached(data_dir: Path) -> dict:
    """盘中每帧每卡都要问许可: 按相关文件签名缓存, 最长 30 秒。返回深拷贝。"""
    import copy
    import time
    rd = rules_dir(data_dir)
    key = (str(Path(data_dir).resolve()), _sig(path(data_dir)), _sig(rd / "current.json"), _sig(display_path(data_dir)),
           _sig(Path(data_dir) / "ledger" / "events.jsonl"),
           tuple((v, _sig(rd / f"R{v}.json")) for v in versions(data_dir)))
    now = time.monotonic()
    hit = _PM_CACHE.get(key[0])
    if hit and hit[0] == key and now - hit[1] < PM_CACHE_MAX_AGE_S:
        return copy.deepcopy(hit[2])
    out = permission(data_dir)
    _PM_CACHE[key[0]] = (key, now, out)
    return copy.deepcopy(out)


def _short_capable_ids() -> set[str]:
    from app.services import decision_desk as dd
    return set(dd.SHORT_SOURCES) | set(SELL_IS_SHORT)
