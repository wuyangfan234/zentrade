"""机会卡下单裁决 + 状态赔率 (0.27.0, 2026-10-19 产品决策 B “折中”: 期望写成美元, 盘前量/新闻角标保留)。

卡面只回答一件事: 系统此刻对这张卡下单还是不下单, 再给两个量化数字:
  回区 = 从当前状态起, 历史上同类卡最终回到挂单区 (成交) 的比例;
  期望 = 从当前状态起继续挂单, 每张卡最终拿到的平均盈亏 (没成交记 0), 按本卡计划股数折成美元。
状态 = 方向 x 入场窗第几天 x 时段 (盘前 / 开盘 / 上午 / 下午) x 现价离挂单区几个 ATR (7 档)。
数字查 config/swing_state_odds.json: 2023-01 ~ 2026-09 自选 58 只 30 分钟K 回放, 与卡结局同一结算规则,
已含“做多第 1 天盘前越过止损作废”; 样本 < 20 的格子在生成时已退到 (方向, 第几天, 距离), 再退到 (方向, 距离)。
期望只在 95% 区间 (按周聚类自助法) 不跨 0 时着色 (sig = +1 / -1); 跨 0 = 历史上分不出好坏 (sig = 0)。

裁决 (verdict) 只看纸面交易员此刻会不会挂这张单, 数字不参与裁决 (回放: 用期望表直接决定挂不挂, 留出段更差):
  holding = 已成交; order = 下单;
  skip = 不下单, reason = holding_other / cooldown / earnings / direction_conflict / paper_watch (交易环同式) /
         premarket_beyond_stop (做多第 1 天 09:00 检查之前, 现价已越过止损的预判; 09:00 检查后作废卡进灰色历史) /
         entry_closed (今天 15:30 后已不能成交, 且明天已过入场窗)。
回放证据: frozen offline replay (2026-10)。只读, 不写任何文件。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from app.services import swing_pm_check as pmc

logger = logging.getLogger(__name__)

ODDS_PATH = Path(__file__).parents[2] / "config" / "swing_state_odds.json"
_ODDS: dict = {}
OPEN_AT, AM_AT, PM_AT = dtime(9, 30), dtime(10, 30), dtime(12, 30)
BINS = ((1.0, "gt1"), (0.5, "0.5-1"), (0.25, "0.25-0.5"), (0.0, "0-0.25"))
NOTIONAL_CAP = 0.25          # 单票名义 <= 权益 25% (trader.sizing.size_position 同式)


def odds_table() -> dict:
    """状态赔率表 (只读, 进程内缓存)。读失败 = 空 (卡面不显示数字, 裁决照常)。"""
    if "v" not in _ODDS:
        try:
            d = json.loads(ODDS_PATH.read_text(encoding="utf-8"))
            _ODDS["v"] = d if isinstance(d, dict) and isinstance(d.get("cells"), dict) else {}
        except (OSError, ValueError):
            _ODDS["v"] = {}
    return _ODDS["v"]


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def dist_atr(side: str, price, zone, atr) -> float | None:
    """现价离挂单区几个 ATR (多: (现价 - 区上沿) / ATR; 空: (区下沿 - 现价) / ATR)。>0 = 还在等回踩, 区内 [-0.25, 0]。"""
    p, a = _num(price), _num(atr)
    try:
        lo, hi = (_num(x) for x in zone)
    except (TypeError, ValueError):
        return None
    if p is None or a is None or a <= 0 or lo is None or hi is None:
        return None
    return round(((p - hi) if side == "long" else (lo - p)) / a, 2)


def dist_bin(d: float) -> str:
    for th, name in BINS:
        if d > th:
            return name
    return "in_zone" if d >= -0.25 else ("below_zone" if d >= -1.0 else "beyond_stop")


def phase_of(t: dtime) -> str:
    if t < OPEN_AT:
        return "pre"
    if t < AM_AT:
        return "open"
    return "am" if t < PM_AT else "pm"


def entry_day(now_et: datetime) -> date:
    """卡下一次能成交的交易日: 交易日入场截止 (15:30, 半日市 12:30) 前 = 今天; 否则下一交易日 (swing_desk._entry_day 同式)。"""
    from app.market_clock import us_clock
    today = now_et.date()
    if us_clock.is_trading_day(today) and now_et.time() <= us_clock.entry_close(today):
        return today
    return us_clock.next_trading_day(today)


def state_key(side: str, signal_day: date, now_et: datetime, *, price_source: str | None = None,
              entry_days: int = 2) -> dict | None:
    """{day, phase, birth}; None = 入场窗已过 (下一次能成交的那天已超出入场窗)。
    birth = 出卡后还没有任何新价格: 第 1 天开盘前, 现价仍是信号日收盘 (收盘价 / 收盘后快照, 不是盘前或实时报价),
    用该方向第 1 天全部卡的结局。"""
    from app.market_clock import us_clock
    ed = entry_day(now_et)
    w = len(us_clock.trading_days_between(signal_day + timedelta(days=1), ed))
    if w > entry_days:
        return None
    w = max(w, 1)
    ph = "pre" if ed > now_et.date() else phase_of(now_et.time())
    birth = w == 1 and ph == "pre" and price_source in (None, "close", "snapshot")
    return {"day": w, "phase": ph, "birth": birth}


def plan_qty(equity, risk_pct, rps, mid) -> int | None:
    """计划股数 = 权益 x 单笔风险 / 每股风险, 名义 <= 权益 25% (trader.sizing.size_position 同式)。缺数 -> None。"""
    e, r, s, m = _num(equity), _num(risk_pct), _num(rps), _num(mid)
    if e is None or r is None or s is None or m is None or e <= 0 or r <= 0 or s <= 0 or m <= 0:
        return None
    return max(min(int(e * r / s), int(e * NOTIONAL_CAP / m)), 0)


def odds_for(card: dict, now_et: datetime, *, equity=None, risk_pct=None, table: dict | None = None) -> dict | None:
    """一张卡此刻的赔率 (只读)。key 算不出 (入场窗已过) -> {"closed": True}; 表里没有该格 -> None。"""
    side = card.get("side") or "long"
    try:
        sday = date.fromisoformat(str(card.get("signal_day") or card.get("as_of"))[:10])
    except ValueError:
        return None
    key = state_key(side, sday, now_et, price_source=card.get("price_source"), entry_days=int(card.get("entry_days") or 2))
    if key is None:
        return {"closed": True}
    d = dist_atr(side, card.get("price"), card.get("entry_zone") or (), card.get("atr"))
    cells = (table if table is not None else odds_table()).get("cells") or {}
    if key["birth"]:
        cell_key, bin_ = f"{side}|1|birth", "birth"
    else:
        if d is None:
            return None
        bin_ = dist_bin(d)
        cell_key = f"{side}|{key['day']}|{key['phase']}|{bin_}"
    cell = cells.get(cell_key)
    ev, fill = _num((cell or {}).get("ev")), _num((cell or {}).get("fill"))
    if cell is None or ev is None or fill is None:
        return None
    raw_ci = cell.get("ci") if isinstance(cell.get("ci"), list) and len(cell["ci"]) == 2 else None
    lo_ci, hi_ci = (_num(raw_ci[0]), _num(raw_ci[1])) if raw_ci else (None, None)
    ci = [lo_ci, hi_ci] if lo_ci is not None and hi_ci is not None else None
    sig = 0
    if lo_ci is not None and hi_ci is not None and lo_ci > 0:
        sig = 1
    elif lo_ci is not None and hi_ci is not None and hi_ci < 0:
        sig = -1
    rps = _num(card.get("risk"))
    try:
        mid = (float(card["entry_zone"][0]) + float(card["entry_zone"][1])) / 2
    except (KeyError, IndexError, TypeError, ValueError):
        mid = None
    qty = plan_qty(equity, risk_pct, rps, mid)
    out = {"fill": fill, "ev_r": ev, "ci_r": ci, "sig": sig, "n": cell.get("n"), "level": cell.get("level"),
           "day": key["day"], "phase": key["phase"], "bin": bin_, "dist_atr": d, "qty": qty,
           "risk_usd": None, "ev_usd": None, "ci_usd": None}
    if qty and rps:
        usd = qty * rps
        out["risk_usd"] = round(usd)
        out["ev_usd"] = round(ev * usd)
        out["ci_usd"] = [round(lo_ci * usd), round(hi_ci * usd)] if lo_ci is not None and hi_ci is not None else None
    return out


def verdict(card: dict, odds: dict | None, *, pm_record: dict | None, now_et: datetime) -> tuple[str, str | None]:
    """(verdict, reason)。只看交易环会不会挂这张单 (见模块说明); 赔率数字不参与。"""
    if card.get("state") == "holding":
        return "holding", None
    if card.get("blocked"):
        return "skip", str(card["blocked"])
    if odds and odds.get("closed"):
        return "skip", "entry_closed"
    # 做多第 1 天: 09:00 检查前, 盘前现价已越过止损 = 预判作废 (检查后作废的卡已进灰色历史, 不会走到这里)
    if card.get("side", "long") == "long" and pm_record is None:
        try:
            sday = date.fromisoformat(str(card.get("signal_day"))[:10])
            first = pmc.first_entry_day(sday)
        except (ValueError, TypeError):
            first = None
        p, stop = _num(card.get("price")), _num(card.get("stop"))
        ed = entry_day(now_et)
        if first is not None and ed == first and (ed > now_et.date() or now_et.time() < OPEN_AT) \
                and p is not None and stop is not None and p < stop:
            return "skip", "premarket_beyond_stop"
    return "order", None


def annotate(cards: list[dict], now_et: datetime, *, equity=None, risk_pct=None, pm_records: dict | None = None) -> None:
    """就地写入每张卡的 verdict / verdict_reason / odds。任何一张出错只是该卡不带数字, 裁决按交易环口径照常。"""
    table = odds_table()
    pm_records = pm_records or {}
    for c in cards:
        try:
            o = None if c.get("state") == "holding" else odds_for(c, now_et, equity=equity, risk_pct=risk_pct, table=table)
        except Exception as e:
            logger.warning("swing verdict odds %s failed: %s", c.get("symbol"), type(e).__name__)
            o = None
        v, why = verdict(c, o, pm_record=pm_records.get(c.get("card_id")), now_et=now_et)
        c["verdict"], c["verdict_reason"] = v, why
        c["odds"] = None if (o is None or o.get("closed")) else o
