"""机会板卡自动结算 + 每日对账 (0.26.0 第③批, 10-18 产品决策“机会页折中方案”与 Q2“每日自动对账”)。

一、每张上过板的卡都自动出结局 (成交和没成交都算), 结算规则与纸面账户同一套, 用系统已有的 30 分钟K:
  - 入场: 信号日后 ENTRY_DAYS (2) 个交易日内, 与纸面同一个入场时窗 (us_clock.in_entry_window: 09:45 起, 收盘前
    30 分钟止) 内价格碰到挂单区即成交: 09:45-10:00 看那根 15 分钟K (kline_4h open15; 缺 = 那天从 10:00 起判),
    10:00 起逐根 30 分钟K, 当天最后一根 (15:30-16:00, 半日市 12:30-13:00) 不再入场。
    成交价 = 该根开盘价夹到区内, 滑点 5 bps (与 ledger Account.slippage_bps 同值)。没碰到 = no_fill。
  - 持有: 盘中碰到目标 -> 目标价离场 (开盘就越过目标 -> 开盘价); 收盘破止损 -> 次日开盘离场;
    浮盈 >= 1R 止损移到保本, >= 2R 锁 1R (与 trader._move_stop 同式); 第 HOLD_DAYS (5) 个持有日 15:30 离场。
  - R = 每股盈亏 / 计划每股风险 (挂单区中值到止损), 含滑点, 不含佣金, 不经账户约束 (被仓位/板块上限挡掉的卡也照算)。
  - 与回放研究内核 (oppfit-1016 core.sim) 逐根同式; 与纸面账户的差别只有两处并已知: 纸面要两帧报价在区内才成交、
    纸面目标是区间 (远端全平/近端减半), 本卡目标单一价位。实际 vs 回放区间不重叠 = 撮合模型要修 (失败判据)。
  结局行只追加, 唯一键 = (卡号, contract); 已写的行永不改写。口径变了升 CONTRACT 写新文件 (改版不清零)。
  v2 (0.27.0): 做多卡第 1 天 09:00 盘前价已低于止损 -> status=void (作废, 第 2 天也不挂), 与纸面同一条规则。
  v3 (2026-11-10, 产品决策「回放改成 09:45-15:30」): 入场时窗与纸面对齐 (v2 是 10:00 起、含最后一根 30 分钟K:
    一个运行实例的 21 笔纸面成交 12 笔在 10:00 前, 同卡纸面与回放平均绝对差 0.22R); 持有期K线换回信号日原始价口径
    (拆股 / 分红后日K整体回溯, 卡上价位是信号日当晚的价: 不换算时除权后结算会让真实成交消失或算出 -19R)。

二、随时有效的置信区间 (不设“攒够多少笔才算数”, 也不拦任何交易):
  比例 (回区率/赢/目标/止损/到期) = 伯努利 Robbins 正态混合置信序列; 平均 R = 把 R 截到 [-R_CAP, R_CAP] 后
  归一到 [0,1] 的同一置信序列 (Howard, Ramdas, McAuliffe, Sekhon 2021, eq. (14), 混合参数 rho 固定)。
  任何时候看都成立 (anytime-valid), 样本越多区间越窄; 0 笔时区间 = 全范围。

三、每日对账 (Q2: 核对“生产里实际出的信号”和“回放用同样数据算出的信号”是否一致), 每天收盘推送之后:
  信号层 = 当天 3+3 规则, 生产信号表的命中 vs 回测矩阵路径在同一批自选上重算的命中, 逐票比 (0.25.0 才发现的
  “connors_rsi2 生产 0 命中 / SMC 召回 61%”就是这一层); 板层 = 最近一本已落盘的下单簿 vs 生产同一入口重算的板
  (票 + 方向 + 价位); 连续 0 命中只在超过该规则回放里的历史最长时列出。不一致写 research/swing_settle/reconcile.jsonl
  并推送一次提醒 (同一天一次)。
"""
from __future__ import annotations

import json
import logging
import math
from collections import Counter
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

logger = logging.getLogger(__name__)

# 结算口径 (改口径升版本号, 新旧两版并行写各自的文件)。v2 (0.27.0) = v1 + 做多卡第 1 天 09:00 盘前越过止损作废
# (status=void, 不计入回区/赢/目标/止损/到期, 与纸面同一条规则, 见 swing_pm_check)。
CONTRACT = "swing-settle-v3"
SLIP = 0.0005                         # 5 bps, 与 ledger Account.slippage_bps 同值
ALPHA = 0.05                          # 置信序列 1 - alpha = 95%
R_CAP = 3.0                           # 平均 R 的置信序列把单笔 R 截到 [-3, 3] (5 日卡几乎不超出)
N_OPT = 100                           # 区间在约 100 张卡时最紧 (固定常数, 不随样本调)
# 正态混合参数 rho (单位 = 方差时间 v = n/4), 按 Howard et al. 2021 的近似最优式取在 v = N_OPT/4 处最紧; 固定不变,
# 这是“随时有效”成立的前提 (rho 若随 n 变, 就退化成每个 n 各算一次的普通区间)。
RHO = (N_OPT / 4) / (2 * math.log(1 / ALPHA) + math.log(1 + 2 * math.log(1 / ALPHA)))


def settle_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "swing_settle"


def outcomes_path(data_dir: Path, contract: str | None = None) -> Path:
    """结局文件 (按结算口径分文件: 改口径 = 新文件, 旧文件原样保留, 改版不清零)。contract=None -> 现行 CONTRACT。"""
    return settle_dir(data_dir) / f"outcomes.{contract or CONTRACT}.jsonl"


def summary_path(data_dir: Path) -> Path:
    return settle_dir(data_dir) / "summary.json"


def reconcile_path(data_dir: Path) -> Path:
    return settle_dir(data_dir) / "reconcile.jsonl"


# ── 1. 纯函数回放 (一张卡) ────────────────────────────────────────────────

def replay_card(side: str, zone: tuple[float, float], stop: float, target: float, days: list[list[tuple]],
                *, entry_days: int = 2, hold_days: int = 5, open15: list[tuple | None] | None = None) -> dict:
    """days = 信号日之后的交易日 (按日期排序), 每天 = 常规时段 30 分钟K [(开, 高, 低, 收), ...] (09:30 起)。
    open15 = 与 days 对齐的 09:45-10:00 那根 15 分钟K (开, 高, 低, 收); 没给 / 那天是 None -> 那天从 10:00 起判。
    返回 {"status": "no_fill" | "closed" | "pending" | "holding", ...}。
      pending = 入场窗还没走完 (数据不够判定); holding = 已成交但持有期还没走完。
      fill_bar = 成交在当天第几根 30 分钟K (0 = 09:45-10:00 那一段)。
    规则见模块说明。纯函数, 不读文件。"""
    zlo, zhi = float(min(zone)), float(max(zone))
    sg = 1 if side == "long" else -1
    rc = abs((zlo + zhi) / 2 - stop)
    if not (rc > 0 and math.isfinite(rc)):
        return {"status": "invalid"}
    fill_d = entry = b0 = None
    for w in range(entry_days):
        if w >= len(days):
            return {"status": "pending", "days_seen": len(days)}
        bars = days[w]
        sub = open15[w] if open15 is not None and w < len(open15) else None
        if sub is not None:                              # 09:45-10:00 (纸面 09:45 起可成交)
            o, h, lo, _c = sub
            if h >= zlo and lo <= zhi:
                px = min(max(o, zlo), zhi)
                fill_d, entry, b0 = w, px * (1 + sg * SLIP), 0
                break
        for b in range(1, len(bars) - 1):                # 10:00 起; 最后一根 (收盘前 30 分钟) 纸面已过入场截止
            o, h, lo, _c = bars[b]
            if h >= zlo and lo <= zhi:
                px = min(max(o, zlo), zhi)
                fill_d, entry, b0 = w, px * (1 + sg * SLIP), b
                break
        if fill_d is not None:
            break
    if fill_d is None or entry is None or b0 is None:
        return {"status": "no_fill"}
    entry = float(entry)
    stop_now, ext, pending_exit = stop, entry, False

    def done(px: float, k: int, why: str) -> dict:
        xp = px * (1 - sg * SLIP)
        return {"status": "closed", "fill_day": fill_d, "fill_bar": b0, "exit_day": fill_d + k, "entry": round(entry, 4),
                "exit": round(xp, 4), "why": why, "r": round(sg * (xp - entry) / rc, 4)}

    for k in range(hold_days):
        t = fill_d + k
        if t >= len(days):
            return {"status": "holding", "fill_day": fill_d, "fill_bar": b0, "entry": round(entry, 4)}
        bars = days[t]
        if not bars:
            return {"status": "holding", "fill_day": fill_d, "fill_bar": b0, "entry": round(entry, 4)}
        if pending_exit:
            return done(bars[0][0], k, "stop")
        last = len(bars) - 1
        for b in range(b0 + 1 if k == 0 else 0, len(bars)):
            o, h, lo, _c = bars[b]
            fav = h if sg == 1 else lo
            if sg * (o - target) >= 0:
                return done(o, k, "target")
            if sg * (fav - target) >= 0:
                return done(target, k, "target")
            if k == hold_days - 1 and b == last:
                break
            ext = max(ext, fav) if sg == 1 else min(ext, fav)
            gain = sg * (ext - entry) / rc
            if gain >= 2:
                stop_now = max(stop_now, entry + rc) if sg == 1 else min(stop_now, entry - rc)
            elif gain >= 1:
                stop_now = max(stop_now, entry) if sg == 1 else min(stop_now, entry)
        if k == hold_days - 1:
            return done(bars[-2][3] if len(bars) > 1 else bars[-1][3], k, "time")   # 15:30 离场 = 倒数第二根收盘
        if sg * (bars[-1][3] - stop_now) < 0:
            pending_exit = True
    return {"status": "holding", "fill_day": fill_d, "fill_bar": b0, "entry": round(entry, 4)}


# ── 2. 随时有效置信区间 ─────────────────────────────────────────────────

def _mix_radius(v: float, rho: float = RHO, alpha: float = ALPHA) -> float:
    """Robbins 正态混合 (双侧): 对方差时间 v 的 sub-Gaussian 累计和 S, 以 >= 1-alpha 的概率对所有 n 同时有
    |S_n| <= sqrt((v + rho) * log((v + rho) / (rho * alpha^2)))  (Howard, Ramdas, McAuliffe, Sekhon 2021)。
    v = n/4 ([0,1] 有界变量的方差上界)。返回 S_n 的半宽, 调用方再除以 n 得到均值半宽。"""
    return math.sqrt((v + rho) * math.log((v + rho) / (rho * alpha * alpha)))


def cs_proportion(k: int, n: int, *, alpha: float = ALPHA) -> tuple[float, float]:
    """k / n 的随时有效 95% 置信区间。n = 0 -> (0, 1)。注意: 同一天的卡彼此相关, 区间按独立样本算, 偏乐观。"""
    if n <= 0:
        return (0.0, 1.0)
    half = _mix_radius(n / 4.0, RHO, alpha) / n
    p = k / n
    return (round(max(0.0, p - half), 4), round(min(1.0, p + half), 4))


def cs_mean_r(rs: list[float], *, alpha: float = ALPHA, cap: float = R_CAP) -> tuple[float, float] | None:
    """平均 R 的随时有效置信区间: R 截到 [-cap, cap] 后按 [0,1] 有界变量求区间再换回。空 -> None。"""
    n = len(rs)
    if n == 0:
        return None
    x = [(min(max(r, -cap), cap) + cap) / (2 * cap) for r in rs]
    m = sum(x) / n
    half = _mix_radius(n / 4.0, RHO, alpha) / n
    lo, hi = max(0.0, m - half), min(1.0, m + half)
    return (round(lo * 2 * cap - cap, 4), round(hi * 2 * cap - cap, 4))


def cs_bounds(k: int, n: int) -> list[float]:
    return list(cs_proportion(k, n))


def summarize_rows(rows: list[dict]) -> dict:
    """结局行 -> 卡面那一行数字 + 区间。rows: {"status": "no_fill" | "closed", "why", "r"}。"""
    shown = [r for r in rows if r.get("status") in ("no_fill", "closed")]
    n = len(shown)
    filled = [r for r in shown if r.get("status") == "closed"]
    f = len(filled)
    why = Counter(r.get("why") for r in filled)
    rs = [float(r["r"]) for r in filled if isinstance(r.get("r"), (int, float))]
    wins = sum(1 for x in rs if x > 0)

    def prop(k, m):
        return {"p": round(k / m, 4) if m else None, "ci": cs_bounds(k, m), "k": k, "n": m}
    return {"cards": n, "filled": f,
            "fill": prop(f, n), "win": prop(wins, f), "target": prop(why.get("target", 0), f),
            "stop": prop(why.get("stop", 0), f), "time": prop(why.get("time", 0), f),
            "mean_r": {"v": round(sum(rs) / len(rs), 4) if rs else None, "ci": cs_mean_r(rs), "n": len(rs)}}


def overlap(a: list | tuple | None, b: list | tuple | None) -> bool | None:
    """两个区间是否重叠; 任一缺 -> None (不判)。"""
    if not a or not b:
        return None
    return not (a[1] < b[0] or b[1] < a[0])


# ── 3. 生产结算 (每天收盘后, 只写 research/swing_settle/) ────────────────────

SPLIT_MIN = 0.15      # 日K复权系数相对信号日变了 15% 以上才当作拆股 (5:4 拆股 = 25%); 普通分红不换算


def _basis_factors(data_dir: Path, repo, symbol: str, start: date, end: date, basis_day: date | None):
    """-> f(日期): 那天的原始价 x f = 目标口径的价。
    basis_day=None: 系统日K现行复权口径 (f = 当日 close/raw_close; 研究回放的卡价位也按现行日K算, 两边同口径)。
    basis_day = 信号日: 信号日原始价口径 (生产结算: 卡上价位冻结在信号日当晚; 之后拆股, 日K整体回溯, 30 分钟K是拆后原始价)。
      先按公司行动里 (信号日, 那天] 的拆股比例换算 (不依赖日K有没有回溯完); 公司行动里没有、但日K复权系数相对信号日
      变了 SPLIT_MIN 以上 (缓存缺这条拆股) -> 用日K系数之比。普通现金分红不换算: 纸面成交看的也是原始报价。"""
    import polars as pl

    from app.services import kline_4h
    lo = min(start, basis_day) if basis_day is not None else start
    adj = kline_4h._adj_factors(repo, [symbol], lo, end)
    by: dict[date, float] = {}
    if adj.height:
        for d, a in adj.filter(pl.col("symbol") == symbol).select(["date", "adj"]).iter_rows():
            if isinstance(a, (int, float)) and math.isfinite(a) and a > 0:
                by[d] = float(a)
    if basis_day is None:
        return lambda d: by.get(d, 1.0)
    try:
        from app.services.corporate_actions import CorporateActionService, split_factor
        actions = CorporateActionService(data_dir).actions()
    except Exception as e:
        logger.warning("swing settle: corporate actions unavailable: %s", type(e).__name__)
        actions, split_factor = [], None
    ab = by.get(basis_day)
    cache: dict[date, float] = {}

    def f(d: date) -> float:
        if d not in cache:
            k = split_factor(actions, symbol, basis_day, d) if split_factor is not None else 1.0
            if k == 1.0 and ab and by.get(d) and abs(by[d] / ab - 1.0) >= SPLIT_MIN:
                k = by[d] / ab
            cache[d] = k
        return cache[d]
    return f


def _open15_days(data_dir: Path, repo, symbol: str, start: date, end: date, *,
                 basis_day: date | None = None) -> dict[date, tuple]:
    """一只票 [start, end] 每天 09:45 那根 15 分钟K (开, 高, 低, 收), 口径与 _m30_days 相同。没有数据 = 空。"""
    import polars as pl

    from app.services import kline_4h
    raw = kline_4h.load_open15(data_dir, [symbol], start, end)
    if raw.is_empty():
        return {}
    raw = raw.with_columns(pl.col("t").str.to_datetime(time_zone="UTC").dt.convert_time_zone("America/New_York").alias("ts"))
    raw = raw.with_columns(pl.col("ts").dt.date().alias("date"),
                           (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("mm"))
    raw = raw.filter((pl.col("mm") == kline_4h.OPEN15_START[0] * 60 + kline_4h.OPEN15_START[1])
                     & (pl.col("date") >= start) & (pl.col("date") <= end)).sort("ts")
    fac = _basis_factors(data_dir, repo, symbol, start, end, basis_day)
    out: dict[date, tuple] = {}
    for d, o, h, lo, c in raw.select(["date", "open", "high", "low", "close"]).iter_rows():
        a = fac(d)
        out[d] = (o * a, h * a, lo * a, c * a)
    return out


def _m30_days(data_dir: Path, repo, symbol: str, start: date, end: date, *,
              basis_day: date | None = None) -> dict[date, list[tuple]]:
    """一只票 [start, end] 的常规时段 30 分钟K (复权到系统日K口径, 与 4h 数据层同式), 按日分组。
    basis_day 给了 -> 换成那一天 (信号日) 的原始价口径 (见 _basis_factors)。"""
    import polars as pl

    from app.services import kline_4h
    raw = kline_4h.load_m30(data_dir, [symbol], start, end)
    if raw.is_empty():
        return {}
    raw = raw.with_columns(pl.col("t").str.to_datetime(time_zone="UTC").dt.convert_time_zone("America/New_York").alias("ts"))
    raw = raw.with_columns(pl.col("ts").dt.date().alias("date"),
                           (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("mm"))
    # 复审: 半日市 (13:00 收盘) 13:00 后的 30 分钟K是盘后, 按当日日历收盘截 (与交易环 / 分钟K同口径)
    from app.services.kline_sync import _early_close_minutes
    early = _early_close_minutes(raw["date"].unique().to_list())
    close_mm = (pl.col("date").replace_strict(list(early), list(early.values()), default=960, return_dtype=pl.Int32)
                if early else pl.lit(960))
    raw = raw.filter((pl.col("mm") >= 570) & (pl.col("mm") < close_mm)).sort("ts")
    if raw.is_empty():
        return {}
    fac = _basis_factors(data_dir, repo, symbol, start, end, basis_day)
    out: dict[date, list[tuple]] = {}
    for d, o, h, lo, c in raw.select(["date", "open", "high", "low", "close"]).iter_rows():
        a = fac(d)
        out.setdefault(d, []).append((o * a, h * a, lo * a, c * a))
    return out


def card_days(data_dir: Path, repo, symbol: str, after: list[date], *,
              basis_day: date | None = None) -> tuple[list[list[tuple]], list[tuple | None]]:
    """一张卡信号日之后的交易日 after -> (逐日 30 分钟K, 对齐的 09:45 那根)。某天缺 30 分钟K: 只用缺口之前的天
    (不把缺数据当没碰到)。replay_card(..., days, open15=open15) 直接用。"""
    if not after:
        return [], []
    bars = _m30_days(data_dir, repo, symbol, after[0], after[-1], basis_day=basis_day)
    subs = _open15_days(data_dir, repo, symbol, after[0], after[-1], basis_day=basis_day)
    days: list[list[tuple]] = []
    open15: list[tuple | None] = []
    for x in after:
        if x not in bars:
            break
        days.append(bars[x])
        open15.append(subs.get(x))
    return days, open15


def _board_cards(data_dir: Path, day: date) -> list[dict]:
    """某信号日上过板的卡 = 下单簿 (板 = 簿)。缺簿 -> []。"""
    from app.services import watchlist_book as wb
    book = wb.load_day(data_dir, day)
    out = []
    for c in (book or {}).get("cards") or []:
        if "-book-swing" not in str(c.get("id") or ""):
            continue
        zone = c.get("entry_zone") or []
        tz = c.get("target_zone") or []
        if len(zone) != 2 or not tz or c.get("invalidation") is None:
            continue
        out.append({"card_id": c["id"], "symbol": c["symbol"], "side": c.get("side") or "long",
                    "signal_day": day.isoformat(), "zone": [float(zone[0]), float(zone[1])],
                    "stop": float(c["invalidation"]), "target": float(tz[0]),
                    "rules": ((c.get("evidence") or {}).get("decision") or {}).get("rules") or []})
    return out


def _done_ids(data_dir: Path, contract: str | None = None) -> set[str]:
    p = outcomes_path(data_dir, contract)
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line)["card_id"])
        except (ValueError, KeyError, TypeError):
            continue
    return out


def read_outcomes(data_dir: Path, contract: str | None = None) -> list[dict]:
    p = outcomes_path(data_dir, contract)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _pm_void(data_dir: Path, repo, c: dict, first_day: date, *, basis_day: date | None = None) -> dict | None:
    """v2 作废规则 (与纸面同一条): 做多卡第 1 天 09:00 盘前价已低于止损 -> {"price", "source"}; 否则 None。
    先读生产检查记录 (swing_pm_check, 0.27.0 起每张卡都有); 没有记录 (上线前的卡) -> 用已存 30 分钟K 的盘前最后一根重放;
    30 分钟K 也缺 -> 不作废 (与生产取不到价时相同)。"""
    if c.get("side", "long") != "long":
        return None
    from app.services import swing_pm_check as pmc
    rec = pmc.load(data_dir, first_day).get(c["card_id"])
    if rec is not None:
        return {"price": rec.get("price"), "source": rec.get("source")} if rec.get("void") is True else None
    try:
        px = pmc.premarket_close_m30(data_dir, repo, c["symbol"], first_day, basis_day=basis_day)
    except Exception as e:
        logger.warning("swing settle: premarket replay %s unavailable: %s", c["symbol"], type(e).__name__)
        return None
    if px is not None and px < float(c["stop"]):
        return {"price": round(px, 4), "source": "m30_replay"}
    return None


def _settle_one(data_dir: Path, repo, c: dict, after: list[date], as_of_close: date) -> tuple[dict | None, bool]:
    """一张卡 (板卡或影子卡) -> (结局行 | None = 现在还定不了, 是否缺 30 分钟K)。after = 信号日之后到 as_of_close 的交易日。"""
    try:
        sday = date.fromisoformat(str(c["signal_day"])[:10])
    except ValueError:
        sday = None
    head = {"card_id": c["card_id"], "contract": CONTRACT, "symbol": c["symbol"], "side": c["side"],
            "signal_day": c["signal_day"], "rules": c["rules"], "zone": c["zone"], "stop": c["stop"],
            "target": c["target"], "settled_on": as_of_close.isoformat()}
    void = _pm_void(data_dir, repo, c, after[0], basis_day=sday)
    if void is not None:
        return {**head, "status": "void", "why": "premarket_beyond_stop", "pm_price": void["price"],
                "pm_source": void["source"], "void_date": after[0].isoformat()}, False
    days, open15 = card_days(data_dir, repo, c["symbol"], after, basis_day=sday)
    missing = len(days) < len(after)
    res = replay_card(c["side"], c["zone"], c["stop"], c["target"], days, open15=open15)
    if res["status"] in ("pending", "holding", "invalid"):
        return None, missing
    row = {**head, **res}
    for key in ("fill_day", "exit_day"):
        if isinstance(row.get(key), int):
            row[key.replace("_day", "_date")] = after[row[key]].isoformat()
            row.pop(key)
    return row, missing


def _append_rows(p: Path, rows: list[dict]) -> None:
    if not rows:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def settle(data_dir: Path, repo, *, as_of_close: date, lookback_days: int = 40) -> dict:
    """结算截至 as_of_close (已收盘) 能定结局的全部板卡, 只追加新行。返回 {written, pending, missing}。"""
    from app.market_clock import us_clock
    from app.services import watchlist_book as wb
    done = _done_ids(data_dir)
    written, pending, missing = [], 0, 0
    for d in wb.book_days(data_dir):
        if d > as_of_close or d < as_of_close - timedelta(days=lookback_days):
            continue
        after = us_clock.trading_days_between(d + timedelta(days=1), as_of_close)
        for c in _board_cards(data_dir, d):
            if c["card_id"] in done:
                continue
            if not after:
                pending += 1
                continue
            row, miss = _settle_one(data_dir, repo, c, after, as_of_close)
            missing += 1 if miss else 0
            if row is None:
                pending += 1
                continue
            written.append(row)
            done.add(c["card_id"])
    _append_rows(outcomes_path(data_dir), written)
    return {"written": len(written), "pending": pending, "missing": missing}


# ── 3b. 影子记录 (2026-11-10): 没上板的同几何候选只结算、不下单 ─────────────────────────────
# 被每边前 5 张截掉的 (cut) 和 3+3 命中但 4h 门不同向的 (gate_4h) 候选, 按上板卡同一套价位记一份, 走同一个回放。
# 用来回答“被砍的卡是不是更差”“4h 门挡掉的值不值”。只写 opportunities/shadow/ 与 research/swing_settle/shadow.*.jsonl;
# 不进下单簿、台账、交易环、推送, 页面也不把它们当卡显示。

SHADOW_CONTRACT = "swing-shadow-v1"
SHADOW_REASONS = ("cut", "gate_4h")


def shadow_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "shadow"


def shadow_path(data_dir: Path, day: date) -> Path:
    return shadow_dir(data_dir) / f"{day.isoformat()}.json"


def shadow_outcomes_path(data_dir: Path, contract: str | None = None) -> Path:
    return settle_dir(data_dir) / f"shadow.{contract or CONTRACT}.jsonl"


def shadow_card_id(day: date, symbol: str, side: str) -> str:
    return f"shadow-{day.isoformat()}-{symbol}-{side}"


def record_shadow(data_dir: Path, repo, day: date, *, now: datetime | None = None) -> dict | None:
    """信号日 day 的影子名单。已记过不改写; 簿还没落盘 (那天的 3+3 与同分口径还没冻结) / 没有信号表 / 大盘门读不到 -> 不记
    (下次再试)。返回写入的记录, 没写 = None。"""
    from app.market_clock import us_clock
    from app.services import swing_desk as sd
    from app.services import watchlist_book as wb
    from app.services.fs_utils import atomic_write_text
    p = shadow_path(data_dir, day)
    if p.exists():
        return None
    book = wb.load_day(data_dir, day)
    if not (isinstance(book, dict) and isinstance(book.get("swing_rules"), dict) and book["swing_rules"].get("long")):
        return None
    board = sd.board_for_day(data_dir, day, repo, shadow=True)
    if board is None:
        return None
    if board["meta"].get("short_gate") is None and board["meta"].get("raw_short_hits"):
        return None
    now = us_clock.market_datetime(now or us_clock.now())
    cards = []
    for c in board.get("shadow") or []:
        cards.append({"card_id": shadow_card_id(day, c["symbol"], c["side"]), "symbol": c["symbol"], "side": c["side"],
                      "signal_day": day.isoformat(), "reason": c["shadow"], "zone": list(c["entry_zone"]),
                      "stop": c["stop"], "target": c["target"], "risk": c["risk"], "atr": c["atr"], "close": c["close"],
                      "score": c["score"], "gate": c["gate"], "rules": [s["id"] for s in c["strategies"]]})
    rec = {"contract": SHADOW_CONTRACT, "card_contract": sd.CONTRACT, "day": day.isoformat(),
           "rules": {"long": board["long_rules"], "short": board["short_rules"]}, "tiebreak": board.get("tiebreak"),
           "per_side": sd.MAX_PER_SIDE, "short_gate": board["meta"].get("short_gate"),
           "board": [{"symbol": c["symbol"], "side": c["side"]} for c in board["long"] + board["short"]],
           "cards": cards, "built_at": now.isoformat(timespec="seconds"),
           # late = 入场窗第 1 天过后才补记的 (上线前的信号日按当时的信号表与冻结规则重算)
           "late": us_clock.next_trading_day(day) < now.date()}
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


def load_shadow(data_dir: Path, day: date) -> dict | None:
    try:
        d = json.loads(shadow_path(data_dir, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("cards"), list) else None


def read_shadow_outcomes(data_dir: Path, contract: str | None = None) -> list[dict]:
    p = shadow_outcomes_path(data_dir, contract)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def settle_shadow(data_dir: Path, repo, *, as_of_close: date, lookback_days: int = 40) -> dict:
    """补记窗口内各信号日的影子名单, 再结算能定结局的影子卡 (与板卡同一个回放与作废规则), 只追加新行。"""
    from app.market_clock import us_clock
    from app.services import watchlist_book as wb
    done = {r.get("card_id") for r in read_shadow_outcomes(data_dir)}
    written, pending, recorded = [], 0, 0
    for d in wb.book_days(data_dir):
        if d > as_of_close or d < as_of_close - timedelta(days=lookback_days):
            continue
        try:
            recorded += 1 if record_shadow(data_dir, repo, d) is not None else 0
        except Exception as e:
            logger.warning("swing shadow record %s failed: %s", d, type(e).__name__)
        rec = load_shadow(data_dir, d)
        if rec is None:
            continue
        after = us_clock.trading_days_between(d + timedelta(days=1), as_of_close)
        for c in rec["cards"]:
            if c.get("card_id") in done or c.get("reason") not in SHADOW_REASONS:
                continue
            if not after:
                pending += 1
                continue
            row, _miss = _settle_one(data_dir, repo, c, after, as_of_close)
            if row is None:
                pending += 1
                continue
            written.append({**row, "reason": c["reason"], "score": c.get("score"), "gate": c.get("gate")})
            done.add(c["card_id"])
    _append_rows(shadow_outcomes_path(data_dir), written)
    return {"recorded": recorded, "written": len(written), "pending": pending}


def cluster_ci(groups: dict, *, n_boot: int = 2000, seed: int = 7, min_clusters: int = 8) -> list[float] | None:
    """均值的聚类自助法 95% 区间: 整组重抽 (组 = 出卡日, 同一天的卡彼此相关)。有数的组不足 min_clusters -> None (不给区间)。"""
    import numpy as np
    ks = [k for k, v in groups.items() if v]
    if len(ks) < min_clusters:
        return None
    sums = np.array([float(sum(groups[k])) for k in ks])
    cnts = np.array([len(groups[k]) for k in ks])
    pick = np.random.default_rng(seed).integers(0, len(ks), (n_boot, len(ks)))
    m = sums[pick].sum(1) / np.maximum(cnts[pick].sum(1), 1)
    return [round(float(np.percentile(m, 2.5)), 3), round(float(np.percentile(m, 97.5)), 3)]


def live_vs_replay(groups: dict, replay_mean) -> dict:
    """上线后的实际成绩 vs 回放值 (只展示, 不拦任何交易): 笔数 / 出卡日数 / 每笔 R / 区间 (出卡日聚类自助法 95%)。
    diverged = 区间不含回放值 (回放数字与实际背离); 出卡日不足 8 个不给区间, 也不判背离。"""
    rs = [float(x) for v in groups.values() for x in v]
    rm = float(replay_mean) if isinstance(replay_mean, (int, float)) and not isinstance(replay_mean, bool) else None
    ci = cluster_ci(groups)
    return {"n": len(rs), "days": sum(1 for v in groups.values() if v),
            "mean_r": round(sum(rs) / len(rs), 3) if rs else None, "ci": ci, "replay_mean_r": rm,
            "diverged": bool(ci is not None and rm is not None and not (ci[0] <= rm <= ci[1]))}


def _by_day(rows: list[dict]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in rows:
        if r.get("status") == "closed" and isinstance(r.get("r"), (int, float)):
            out.setdefault(str(r.get("signal_day")), []).append(float(r["r"]))
    return out


def live_line(data_dir: Path, long_rules=None, short_rules=None) -> dict:
    """机会板页头那一行: 多空各一份 上线后实际 (已结算的板卡: 张数 / 回区率 / 每笔 R / 区间) 并排 回放值。
    replay_stale = 回放基础率不是现行 3+3 那一套 (不拿来判背离)。"""
    rows = read_outcomes(data_dir)
    base = base_rates()
    out = {}
    for side, cur in (("long", long_rules), ("short", short_rules)):
        xs = [r for r in rows if r.get("side") == side and r.get("status") in ("no_fill", "closed")]
        ref = base.get(side) or {}
        line = card_line(side, cur)
        stale = bool(line and line.get("stale")) or not ref
        v = live_vs_replay(_by_day(xs), None if stale else (ref.get("mean_r") or {}).get("v"))
        out[side] = {**v, "cards": len(xs), "fill": round(v["n"] / len(xs), 3) if xs else None,
                     "replay_fill": None if stale else (ref.get("fill") or {}).get("p"), "replay_stale": stale}
    return {"contract": CONTRACT, **out}


def shadow_table(data_dir: Path) -> dict:
    """影子对照 (只读): 每类 (被砍 / 被 4h 门挡) x 方向 = 影子卡的成绩, 并排同一批信号日里上了板的卡。"""
    rows = read_shadow_outcomes(data_dir)
    board = read_outcomes(data_dir)
    groups = {}
    for reason in SHADOW_REASONS:
        for side in ("long", "short"):
            xs = [r for r in rows if r.get("reason") == reason and r.get("side") == side]
            days = {r.get("signal_day") for r in xs}
            ref = [r for r in board if r.get("side") == side and r.get("signal_day") in days]
            diff = None
            a, b = summarize_rows(xs), summarize_rows(ref)
            if a["mean_r"]["v"] is not None and b["mean_r"]["v"] is not None:
                diff = round(a["mean_r"]["v"] - b["mean_r"]["v"], 4)
            groups[f"{reason}:{side}"] = {"shadow": a, "board": b, "days": len(days), "ci": cluster_ci(_by_day(xs)),
                                          "diff_mean_r": diff}
    return {"contract": SHADOW_CONTRACT, "settle_contract": CONTRACT, "groups": groups, "n_total": len(rows)}


# ── 4. 读数 (卡面一行 + 回测页卡结局) ────────────────────────────────────

BASE_RATES_PATH = Path(__file__).parents[2] / "config" / "swing_base_rates.json"
_BASE: dict = {}


def base_rates() -> dict:
    """回放基础率 (config/swing_base_rates.json, backend/scripts/rebuild_swing_odds.py 生成; 窗口 / 名单 / 规则写在文件里)。"""
    if "v" not in _BASE:
        try:
            d = json.loads(BASE_RATES_PATH.read_text(encoding="utf-8"))
            _BASE["v"] = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _BASE["v"] = {}
    return _BASE["v"]


def card_line(side: str, current_rules: tuple[str, ...] | list[str] | None = None) -> dict | None:
    """卡面那一行: 回放基础率 (多/空各一行)。缺文件 -> None (卡面不显示)。
    基础率是某一套 3+3 在回放里的成绩; current_rules 给了且与那套不同 -> stale=True (卡面标“旧 3+3”, 不重算)。"""
    b = (base_rates() or {}).get(side)
    if not isinstance(b, dict):
        return None
    pick = {k: (b.get(k) or {}).get("p") for k in ("fill", "win", "target", "stop", "time")}
    if any(not isinstance(v, (int, float)) for v in pick.values()):
        return None
    built_for = ((base_rates().get("rules") or {}).get(side)) or []
    stale = current_rules is not None and sorted(current_rules) != sorted(built_for)
    return {**{k: round(float(v), 3) for k, v in pick.items() if v is not None}, "contract": base_rates().get("contract"),
            "rules": list(built_for), "stale": bool(stale)}


def table(data_dir: Path) -> dict:
    """回测页“卡结局”: 实际 (生产结算) vs 回放, 多空分开, 每个数并排给区间; 并给失败判据 (区间不重叠)。"""
    rows = read_outcomes(data_dir)
    base = base_rates()
    out = {"contract": CONTRACT, "card_contract": "swing-desk-tight-v1", "alpha": ALPHA,
           "base_window": base.get("window"), "sides": {}}
    for side in ("long", "short"):
        actual = summarize_rows([r for r in rows if r.get("side") == side])
        ref = base.get(side) or {}
        flags = []
        for k in ("fill", "win", "target", "stop", "time"):
            ov = overlap((actual.get(k) or {}).get("ci"), (ref.get(k) or {}).get("ci"))
            if ov is False:
                flags.append(k)
        ov_r = overlap((actual.get("mean_r") or {}).get("ci"), (ref.get("mean_r") or {}).get("ci"))
        if ov_r is False:
            flags.append("mean_r")
        out["sides"][side] = {"actual": actual, "replay": ref or None, "mismatch": flags,
                              "void": sum(1 for r in rows if r.get("side") == side and r.get("status") == "void")}
    out["recent"] = sorted(rows, key=lambda r: (r.get("signal_day") or "", r.get("symbol") or ""), reverse=True)[:60]
    out["n_total"] = len(rows)
    rec = read_reconcile(data_dir, limit=10)
    out["reconcile"] = rec
    try:
        out["shadow"] = shadow_table(data_dir)
    except Exception as e:
        logger.warning("swing settle: shadow table failed: %s", type(e).__name__)
        out["shadow"] = None
    return out


# ── 5. 每日对账 (Q2) ────────────────────────────────────────────────────

def read_reconcile(data_dir: Path, limit: int = 10) -> list[dict]:
    p = reconcile_path(data_dir)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(rows))


def _rule_hits(snap: dict, rules: tuple[str, ...], side: str) -> dict[str, int]:
    want = set(rules)
    c: Counter = Counter()
    for s in snap.get("signals") or []:
        if isinstance(s, dict) and s.get("rule") in want and (s.get("side") or "long") == side:
            c[s["rule"]] += 1
    return {r: c.get(r, 0) for r in rules}


RULE_HIT_RATES_PATH = Path(__file__).parents[2] / "config" / "swing_rule_hit_rates.json"
_HIT_RATES: dict = {}
ZERO_HIT_MAX = 120        # 连续 0 命中最多往回数这么多个信号日 (要能超过 SMC 空的历史最长 80 天; 有命中即停, 平时只读几份)


def rule_hit_rates() -> dict:
    """3+3 规则在回放里的命中频率 (config/swing_rule_hit_rates.json, 自选 58 只 2023-01~2026-09):
    {rule 或 rule:short: {mean_per_day, p_zero_day, max_zero_run}}。读失败 = 空 (不列连续 0 命中)。"""
    if "v" not in _HIT_RATES:
        try:
            d = json.loads(RULE_HIT_RATES_PATH.read_text(encoding="utf-8"))
            _HIT_RATES["v"] = (d.get("rules") or {}) if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _HIT_RATES["v"] = {}
    return _HIT_RATES["v"]


SIGNAL_WARM_DAYS = 900    # 信号层对账的预热 (结构类策略从序列开头累积; 实测 10-16 这么长时 5 条 3+3 与生产逐票一致)


def replay_rule_hits(data_dir: Path, repo, day: date, symbols: list[str], rules: dict[str, tuple[str, ...]],
                     *, engine=None) -> dict[str, set[str]]:
    """回放信号: 用回测矩阵路径 (strategy_evaluation_runner._signal_matrix, 与回测页同一条信号路径) 在自选上
    重算 day 当天 3+3 的命中。多 = 入场信号; 空 = 卖出当做空 (TradingView 移植规则, 与生产 sell_hits 同口径)。
    返回 {"<rule>" / "<rule>:short": {symbol}}; 算不了的规则 (非矩阵策略、引擎里没有) 不出现在结果里。"""
    import numpy as np

    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.services import strategy_evaluation_runner as runner
    from app.services import watchlist_v4 as wv
    engine = engine or runner._strategy_engine(Path(data_dir))
    svc = StrategyBacktestService(BacktestEngine(repo), engine)
    want: dict[str, set[str]] = {}
    for side, ids in rules.items():
        for r in ids:
            if side == "short" and r not in wv.SELL_IS_SHORT:
                continue
            if str(r).startswith("rank_"):     # 0.52.0 排序因子: 不是信号策略, 板层对账照常覆盖它出的卡
                continue
            want.setdefault(r, set()).add(side)
    out: dict[str, set[str]] = {}
    for sid, sides in sorted(want.items()):
        try:
            s = engine.get(sid)
            if s.execution_backend != "matrix_native":
                continue
            # 截面策略 (META.cross_sectional, 如动量排名前 20%): 排名分母是全池, 只在自选上算会换掉名次 ->
            # 在全市场矩阵上算 (预热按策略自己, 不用 900 天长预热), 再取自选列。其余策略照旧只算自选。
            cross = bool((s.meta or {}).get("cross_sectional"))
            md, sig, _stop = runner._signal_matrix(sid, svc, engine, day, day,
                                                   symbols=None if cross else sorted(symbols),
                                                   warm_days=None if cross else SIGNAL_WARM_DAYS, disk_cache=False)
        except Exception as e:
            logger.warning("swing reconcile: replay %s failed: %s", sid, type(e).__name__)
            continue
        labels = [x[:10] for x in md.timestamp_labels]
        if day.isoformat() not in labels:
            continue
        t = labels.index(day.isoformat())
        for side in sides:
            mask = np.asarray(sig.entry if side == "long" else sig.exit, bool)
            hit = {md.symbols[j] for j in np.flatnonzero(mask[t]) if md.symbols[j] in set(symbols)}
            out[sid if side == "long" else f"{sid}:short"] = hit
    return out


def _rule_stop(engine, rule: str, side: str):
    """生产写信号行时给 card_from_pick 的止损 (decision_scan_job.strategy_picks / watchlist_*_signals 同式):
    多 = 策略 stop_loss (用户覆盖优先); 空 (卖出当做空) = |stop_loss| 且须在 (-0.5, 0)。读不到 -> None。"""
    try:
        eng = engine()
        s = eng.get(rule)
        ov = (eng._override_loader(rule) if eng._override_loader else {}) or {}
        st = ov.get("stop_loss", s.stop_loss)
    except Exception:
        return None
    if not isinstance(st, (int, float)):
        return None
    if side == "short":
        return abs(st) if -0.5 < st < 0 else None
    return st


def _cardable(data_dir: Path, repo, day: date, symbols: set[str], side: str, stop_loss=None) -> set[str]:
    """生产信号表只写“出得了卡”的命中 (decision_desk.card_from_pick 返回卡)。回放有、生产无的票先按同一函数、
    同一止损判一次: 出不了卡的不算不一致。读日K失败 -> 全部当作出得了卡 (宁可多报)。"""
    from datetime import timedelta

    from app.services import decision_desk as dd
    if not symbols:
        return set()
    try:
        b = repo.get_daily_batch(sorted(symbols), day - timedelta(days=200), day,
                                 columns=["symbol", "date", "open", "high", "low", "close", "volume"])
    except Exception:
        return set(symbols)
    if b is None or b.is_empty():
        return set(symbols)
    ok = set()
    for g in b.sort("date").partition_by("symbol"):
        sym = str(g["symbol"][0])
        if dd.card_from_pick("reconcile", sym, g, as_of=day, stop_loss=stop_loss, side=side) is not None:
            ok.add(sym)
    return ok


def reconcile(data_dir: Path, repo, *, day: date, notify: bool = True, replay=None) -> dict:
    """每日对账 (10-18 产品决策 Q2: 核对“生产里实际出的信号”和“回放用同样数据算出的信号”是否一致):
    1) 信号层: 当天 3+3 规则, 生产信号表的命中 vs 回测矩阵路径在同一批自选上重算的命中, 逐票比 —— 0.25.0 才发现的
       “两条规则生产 0 命中 / SMC 只抓到 61%”就是这一层。回放有、生产无 = signal_missing; 生产有、回放无 = signal_extra。
    2) 板层: 生产信号表经生产同一入口 (swing_desk.board_for_day) 重算的板 vs 当天下单簿 (票 + 方向 + 价位)。
    3) 连续 0 命中: 只在当前连续天数超过该规则回放里的历史最长 (swing_rule_hit_rates.json) 时列出, 不报警。
    写 reconcile.jsonl 一行; 有不一致且 notify -> 推送一次提醒。只读生产数据, 只写 research/swing_settle/。
    replay = 测试注入 (day, symbols, rules) -> {key: set}; None = replay_rule_hits。"""
    from app.services import swing_desk as sd
    from app.services import watchlist_book as wb
    out: dict = {"day": day.isoformat(), "at": datetime.now().astimezone().isoformat(timespec="seconds"), "issues": []}
    box: dict = {}

    def engine():                                         # 策略引擎只建一次 (读全部策略文件)
        if "e" not in box:
            from app.services import strategy_evaluation_runner as runner
            box["e"] = runner._strategy_engine(Path(data_dir))
        return box["e"]
    snap = sd.load_watchlist_snapshot(data_dir, day)
    if snap is None:
        out["issues"].append({"kind": "no_signal_file"})
    else:
        lr, srules = sd.rules_for_day(data_dir, day)
        uni = sorted(s for s, c in (snap.get("closes") or {}).items() if c is not None)
        # ── 1. 信号层 ──
        try:
            rep = (replay or (lambda d, syms, rules: replay_rule_hits(data_dir, repo, d, syms, rules, engine=engine())))(
                day, uni, {"long": tuple(lr), "short": tuple(srules)})
        except Exception as e:
            logger.warning("swing reconcile: signal replay failed: %s", type(e).__name__)
            rep = None
        if rep is None:
            out["signal_check"] = "unavailable"
        else:
            checked = {}
            for key, rhits in sorted(rep.items()):
                rule, side = (key.split(":", 1)[0], "short") if key.endswith(":short") else (key, "long")
                prod = {x["symbol"] for x in snap.get("signals") or [] if isinstance(x, dict) and x.get("rule") == rule
                        and (x.get("side") or "long") == side}
                missing = set(rhits) - prod
                if missing:
                    missing &= _cardable(data_dir, repo, day, missing, side, _rule_stop(engine, rule, side))
                extra = prod - set(rhits)
                checked[key] = {"prod": len(prod), "replay": len(rhits)}
                for sym in sorted(missing):
                    out["issues"].append({"kind": "signal_missing", "rule": rule, "symbol": sym, "side": side})
                for sym in sorted(extra):
                    out["issues"].append({"kind": "signal_extra", "rule": rule, "symbol": sym, "side": side})
            out["signal_check"] = checked
        # ── 2. 板层 ── 当天的下单簿要到下一个交易日北京 20:00 才落; 收盘后对账核“已落盘的最近一本簿”(通常是
        # 上一信号日、今早落的那本)。推送键按对账日 (swing_reconcile:<日>), 同一天只推一次 (outbox 去重)。
        bday = max((x for x in wb.book_days(data_dir) if x <= day), default=None)
        bsnap = sd.load_watchlist_snapshot(data_dir, bday) if bday is not None else None
        if bday is not None and bsnap is not None:
            out["board_day"] = bday.isoformat()
            board = sd.board_for_day(data_dir, bday, repo) or {}
            recomputed = {(c["symbol"], c["side"]): (c["entry_zone"], c["stop"], c["target"])
                          for c in (board.get("long") or []) + (board.get("short") or [])}
            out["recomputed"] = len(recomputed)
            book = wb.load_day(data_dir, bday)
            if book is not None and book.get("swing_rules"):
                booked = {(c["symbol"], c.get("side") or "long"): (list(c.get("entry_zone") or []), c.get("invalidation"),
                                                                   (c.get("target_zone") or [None])[0])
                          for c in book.get("cards") or [] if "-book-swing" in str(c.get("id") or "")}
                watch = set(bsnap.get("watchlist") or [])
                for k, v in booked.items():
                    if k[0] not in watch:
                        continue                         # 落簿后移出自选: 已知差异, 不算对账失败
                    r = recomputed.get(k)
                    if r is None:
                        out["issues"].append({"kind": "book_card_not_recomputed", "symbol": k[0], "side": k[1],
                                              "day": bday.isoformat()})
                    elif not _same_levels(r, v):
                        out["issues"].append({"kind": "levels_differ", "symbol": k[0], "side": k[1], "book": v,
                                              "recomputed": r, "day": bday.isoformat()})
                out["booked"] = len(booked)
        # ── 3. 命中数 + 连续 0 命中 (超过历史最长才列) ──
        hits = {**{f"{r}": n for r, n in _rule_hits(snap, lr, "long").items()},
                **{f"{r}:short": n for r, n in _rule_hits(snap, srules, "short").items()}}
        out["rule_hits"] = hits
        streak = _zero_streak(data_dir, day, lr, srules)
        rates = rule_hit_rates()
        long_gap = {k: n for k, n in streak.items()
                    if isinstance((rates.get(k) or {}).get("max_zero_run"), int) and n > rates[k]["max_zero_run"]}
        if long_gap:
            out["zero_hit_rules"] = {k: {"days": n, "history_max": rates[k]["max_zero_run"]} for k, n in sorted(long_gap.items())}
    out["ok"] = not out["issues"]
    p = reconcile_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False) + "\n")
    if notify and out["issues"]:
        try:
            from app.services import notify_opportunity as no
            kinds = Counter(x["kind"] for x in out["issues"])
            label = {"signal_missing": "回放有、生产没出", "signal_extra": "生产出了、回放没有",
                     "book_card_not_recomputed": "簿里有、重算板上没有", "levels_differ": "价位对不上",
                     "no_signal_file": "当天没有信号表"}
            lines = [f"机会板对账 {day.isoformat()}: {len(out['issues'])} 处不一致"]
            lines += [f"- {label.get(k, k)} {n} 处" for k, n in kinds.most_common()]
            for x in out["issues"][:6]:
                lines.append(f"  {x.get('rule', '')} {x.get('symbol', '*')} {'空' if x.get('side') == 'short' else '多'}")
            no._enqueue(data_dir, f"swing_reconcile:{day.isoformat()}",
                        {"title": "ZenTrade · 机会板对账", "subtitle": day.isoformat(), "body": "\n".join(lines)})
        except Exception as e:
            logger.warning("swing reconcile notify failed (soft): %s", type(e).__name__)
    return out


def _same_levels(a, b, tol: float = 0.011) -> bool:
    """价位一致 (容差 1 分钱多一点: 两边都按 2 位小数取整)。"""
    try:
        (alo, ahi), astop, atgt = a
        (blo, bhi), bstop, btgt = b
        return all(abs(float(x) - float(y)) <= tol for x, y in ((alo, blo), (ahi, bhi), (astop, bstop), (atgt, btgt)))
    except (TypeError, ValueError):
        return False


def _zero_streak(data_dir: Path, day: date, long_rules, short_rules) -> dict[str, int]:
    """每条 3+3 规则截至 day 连续几个信号日 0 命中 (最多看 ZERO_HIT_MAX 个信号日)。"""
    from app.services import swing_desk as sd
    days = sorted((d for d in sd._signal_days(data_dir) if d <= day), reverse=True)[:ZERO_HIT_MAX]
    keys = [(r, "long", r) for r in long_rules] + [(r, "short", f"{r}:short") for r in short_rules]
    streak = {k: 0 for _, _, k in keys}
    alive = set(streak)
    for d in days:
        snap = sd.load_watchlist_snapshot(data_dir, d) or {}
        for r, side, k in keys:
            if k not in alive:
                continue
            if _rule_hits(snap, (r,), side)[r] == 0:
                streak[k] += 1
            else:
                alive.discard(k)
        if not alive:
            break
    return streak


def run_daily(data_dir: Path, repo, *, day: date) -> dict:
    """收盘后管道调用: 结算 + 摘要 + 对账。任一步失败只记日志。"""
    res: dict = {}
    try:
        res["settle"] = settle(data_dir, repo, as_of_close=day)
    except Exception as e:
        logger.warning("swing settle failed (soft): %s", type(e).__name__)
        res["settle_error"] = type(e).__name__
    try:
        res["shadow"] = settle_shadow(data_dir, repo, as_of_close=day)
    except Exception as e:
        logger.warning("swing shadow settle failed (soft): %s", type(e).__name__)
        res["shadow_error"] = type(e).__name__
    try:
        from app.services.fs_utils import atomic_write_text
        t = table(data_dir)
        p = summary_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps({**t, "built_at": datetime.now().astimezone().isoformat(timespec="seconds")},
                                        ensure_ascii=False))
    except Exception as e:
        logger.warning("swing settle summary failed (soft): %s", type(e).__name__)
    try:
        res["reconcile"] = {k: v for k, v in reconcile(data_dir, repo, day=day).items() if k in ("ok", "issues")}
    except Exception as e:
        logger.warning("swing reconcile failed (soft): %s", type(e).__name__)
        res["reconcile_error"] = type(e).__name__
    return res


_ = dtime  # 预留: 半日市 15:30 离场由 30 分钟K 的倒数第二根近似 (与回放内核一致)
