"""enriched 表计算流水线(§7.5 / §7.7 Step 2)。

存储层 (enriched parquet):
  仅存储基础行情窄表 (14 列), 指标和信号由各服务即时计算。

  存储列: symbol, date, OHLCV(前复权), volume, amount,
          raw_close, raw_high, raw_low, turnover_rate,

设计:
  - 100% Polars 表达式(SQL 窗口无法表达递归 EMA)
  - 每只标的独立计算(`.over("symbol")`)
  - 有 adj_factor 时先应用前复权再算指标;无因子时直接用 raw
  - streaming collect 控制内存
"""
from __future__ import annotations

import logging
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import polars as pl

from app.config import settings
from app.enriched_generation import (
    EnrichedPublication,
    enriched_publication_incomplete,
)
from app.market_time import cn_today
from app.parquet import scan_daily_parquet, scan_enriched_parquet, scan_parquet_compat
from app.share_capital import apply_historical_float_shares, load_share_history

logger = logging.getLogger(__name__)


# ── 自定义信号缓存 ─────────────────────────────────────
# 从 data/user_data/custom_signals/*.json 加载并编译为 Polars 表达式。
# 两套表达式分别用于全量路径 (allow_shift=True, 支持日期偏移条件)
# 和盘中增量热路径 (allow_shift=False, 跳过偏移条件)。
# 模块级缓存：首次调用时加载，invalidate_custom_signals() 后下次重载。
# 增量路径每秒级执行, 若不缓存则每轮 glob + 读所有 JSON + 重编译表达式。
_custom_signal_exprs: dict[str, pl.Expr] | None = None
_custom_signal_exprs_today: dict[str, pl.Expr] | None = None


def _get_custom_signal_exprs() -> dict[str, pl.Expr]:
    """懒加载自定义信号表达式（带模块级缓存，allow_shift=True）。"""
    global _custom_signal_exprs
    if _custom_signal_exprs is None:
        from app.strategy import custom_signals
        try:
            sigs = custom_signals.load_all(settings.data_dir)
            _custom_signal_exprs = custom_signals.build_expressions(sigs)
        except Exception as e:
            logger.warning("custom signals load failed: %s", e)
            _custom_signal_exprs = {}
    return _custom_signal_exprs


def _get_custom_signal_exprs_today() -> dict[str, pl.Expr]:
    """盘中增量热路径专用 (allow_shift=False, 跳过日期偏移条件)。

    与全量版分开缓存：盘中单日快照上 .shift 跨 symbol 语义不正确,
    build_expressions(allow_shift=False) 会跳过带偏移的信号, 结果集不同。
    """
    global _custom_signal_exprs_today
    if _custom_signal_exprs_today is None:
        from app.strategy import custom_signals
        try:
            sigs = custom_signals.load_all(settings.data_dir)
            _custom_signal_exprs_today = custom_signals.build_expressions(sigs, allow_shift=False)
        except Exception as e:
            logger.warning("custom signals load failed (today): %s", e)
            _custom_signal_exprs_today = {}
    return _custom_signal_exprs_today


def invalidate_custom_signals() -> None:
    """失效自定义信号缓存（保存/删除信号后调用，下次计算重新加载）。"""
    global _custom_signal_exprs, _custom_signal_exprs_today
    _custom_signal_exprs = None
    _custom_signal_exprs_today = None


# enriched parquet 仅存储的列 (14 列)
ENRICHED_STORAGE_COLS = [
    "symbol", "date",
    "open", "high", "low", "close",          # 前复权
    "volume", "amount",
    "raw_close", "raw_high", "raw_low",       # 不复权原始价
    "turnover_rate",                           # 依赖当时的 float_shares, 不可回推
    "quote_ts",                                # 行情时间戳(ms): 盘后校验/量比折算/跨天完整性
]


# ================================================================
# enriched 完整列清单 (存储 + 运行时计算)
# 供 AI 审查代码时参考: 策略/筛选/回测 可直接使用以下列名。
# 分类: 存储列 → 指标列 → 信号列 → JOIN 列
# ================================================================
ENRICHED_COLUMNS: dict[str, dict[str, str]] = {
    # ── 存储列 (parquet 持久化) ──────────────────────────
    "symbol":                  "股票代码",
    "date":                    "交易日期",
    "open":                    "前复权开盘价",
    "high":                    "前复权最高价",
    "low":                     "前复权最低价",
    "close":                   "前复权收盘价",
    "volume":                  "成交量",
    "amount":                  "成交额",
    "raw_close":               "原始收盘价(未复权)",
    "raw_high":                "原始最高价(未复权)",
    "raw_low":                 "原始最低价(未复权)",
    "turnover_rate":           "换手率",
    # ── 基础指标 ─────────────────────────────────────────
    "prev_close":              "前收盘价",
    "change_pct":              "日涨跌幅(小数, 如 0.05 = 5%)",
    "change_amount":           "日涨跌额",
    "amplitude":               "日振幅 (最高-最低)/昨收",
    # ── 均线 MA ──────────────────────────────────────────
    "ma5":                     "5日简单均线",
    "ma10":                    "10日简单均线",
    "ma20":                    "20日简单均线",
    "ma30":                    "30日简单均线",
    "ma60":                    "60日简单均线(季线)",
    # ── 指数均线 EMA ─────────────────────────────────────
    "ema5":                    "5日指数均线",
    "ema10":                   "10日指数均线",
    "ema20":                   "20日指数均线",
    "ema30":                   "30日指数均线",
    "ema60":                   "60日指数均线",
    # ── MACD ─────────────────────────────────────────────
    "macd_dif":                "MACD DIF线(快线-慢线)",
    "macd_dea":                "MACD DEA线(信号线)",
    "macd_hist":               "MACD柱状图 (DIF-DEA)×2",
    # ── 布林带 BOLL ──────────────────────────────────────
    "boll_upper":              "布林带上轨 MA20+2σ",
    "boll_lower":              "布林带下轨 MA20-2σ",
    # ── KDJ ──────────────────────────────────────────────
    "kdj_k":                   "KDJ K值",
    "kdj_d":                   "KDJ D值",
    "kdj_j":                   "KDJ J值 (3K-2D)",
    # ── ATR ──────────────────────────────────────────────
    "atr_14":                  "14日平均真实波幅",
    # ── 量价 ─────────────────────────────────────────────
    "vol_ma5":                 "5日成交均量",
    "vol_ma10":                "10日成交均量",
    "vol_ratio_5d":            "量比 (成交量/5日均量)",
    # ── 极值 ─────────────────────────────────────────────
    "high_60d":                "60日最高价",
    "low_60d":                 "60日最低价",
    # ── 动量 ─────────────────────────────────────────────
    "momentum_5d":             "5日动量(涨跌幅小数)",
    "momentum_10d":            "10日动量",
    "momentum_20d":            "20日动量",
    "momentum_30d":            "30日动量",
    "momentum_60d":            "60日动量",
    # ── 波动率 ───────────────────────────────────────────
    "annual_vol_20d":          "20日年化波动率",
    # ── RSI ──────────────────────────────────────────────
    "rsi_6":                   "6日相对强弱指标",
    "rsi_14":                  "14日相对强弱指标",
    "rsi_24":                  "24日相对强弱指标",
    # ── 信号列 (bool) ────────────────────────────────────
    "signal_ma_golden_5_20":   "MA5上穿MA20 (金叉)",
    "signal_ma_dead_5_20":     "MA5下穿MA20 (死叉)",
    "signal_ma_golden_20_60":  "MA20上穿MA60",
    "signal_macd_golden":      "MACD金叉 (DIF上穿DEA)",
    "signal_macd_dead":        "MACD死叉 (DIF下穿DEA)",
    "signal_ma20_breakout":    "收盘突破MA20上方",
    "signal_ma20_breakdown":   "收盘跌破MA20下方",
    "signal_ma5_breakout":     "收盘突破MA5上方",
    "signal_ma5_breakdown":    "收盘跌破MA5下方",
    "signal_ma10_breakout":    "收盘突破MA10上方",
    "signal_ma10_breakdown":   "收盘跌破MA10下方",
    "signal_n_day_high":       "创60日新高",
    "signal_n_day_low":        "创60日新低",
    "signal_boll_breakout_upper": "突破布林上轨",
    "signal_boll_breakdown_lower": "跌破布林下轨",
    "signal_volume_surge":     "放量 (量比≥2.0)",
    # ── JOIN 列 (由 repository 从 instruments 表补充) ───
    "name":                    "股票名称 (来自 instruments)",
    "total_shares":            "总股本 (来自 instruments)",
    "float_shares":            "流通股本 (来自 instruments)",
}

# 仅供 AI/开发者快速索引: 按类别的列名列表
ENRICHED_COLUMNS_BY_CATEGORY: dict[str, list[str]] = {
    "storage":  [k for k in ENRICHED_COLUMNS if k in ENRICHED_STORAGE_COLS],
    "basic":    ["prev_close", "change_pct", "change_amount", "amplitude"],
    "ma":       ["ma5", "ma10", "ma20", "ma30", "ma60"],
    "ema":      ["ema5", "ema10", "ema20", "ema30", "ema60"],
    "macd":     ["macd_dif", "macd_dea", "macd_hist"],
    "boll":     ["boll_upper", "boll_lower"],
    "kdj":      ["kdj_k", "kdj_d", "kdj_j"],
    "atr":      ["atr_14"],
    "volume":   ["vol_ma5", "vol_ma10", "vol_ratio_5d"],
    "extremes": ["high_60d", "low_60d"],
    "momentum": ["momentum_5d", "momentum_10d", "momentum_20d", "momentum_30d", "momentum_60d"],
    "volatility": ["annual_vol_20d"],
    "rsi":      ["rsi_6", "rsi_14", "rsi_24"],
    "signals":  [k for k in ENRICHED_COLUMNS if k.startswith("signal_")],
    "join":     ["name", "total_shares", "float_shares"],
}


def _ema_alpha(span: int) -> float:
    return 2.0 / (span + 1)


def _apply_adj_factor(raw: pl.DataFrame, factors: pl.DataFrame) -> pl.DataFrame:
    """对 raw K 线应用前复权 (forward adjustment)。

    adj_factor 结构: symbol, trade_date, ex_factor
    ex_factor 含义: 每次除权事件的 pre/post 比值(个股级,非累积)。

    前复权原理:
      - 保持最新价格不变,将历史价格向下调整以消除除权缺口
      - adjusted = raw × cumprod_at_D / total_cumprod
      - 等价于: adjusted = raw / (该日期之后所有事件的 ex_factor 乘积)
    """
    if factors.is_empty():
        return raw

    # 确保类型一致
    has_vol = "vol_factor" in factors.columns
    factors = factors.with_columns(
        pl.col("trade_date").cast(pl.Date, strict=False),
        pl.col("ex_factor").cast(pl.Float64, strict=False),
        (pl.col("vol_factor").cast(pl.Float64, strict=False).fill_null(1.0) if has_vol else pl.lit(1.0)).alias("vol_factor"),
    ).select("symbol", "trade_date", "ex_factor", "vol_factor").drop_nulls(["symbol", "trade_date", "ex_factor"])

    if factors.is_empty():
        return raw

    # 去重 + 排序 + 累积乘积 (一趟完成)
    factors_sorted = (
        factors.sort(["symbol", "trade_date"])
        .unique(subset=["symbol", "trade_date"])
        .sort(["symbol", "trade_date"])
        .with_columns(
            pl.col("ex_factor").cum_prod().over("symbol").alias("cum_factor"),
            pl.col("vol_factor").cum_prod().over("symbol").alias("cum_vol"),
        )
    )

    # 每个 symbol 的总累积因子
    total_factors = (
        factors_sorted
        .group_by("symbol")
        .agg(pl.col("cum_factor").last().alias("total_factor"),
             pl.col("cum_vol").last().alias("total_vol"))
    )
    if not has_vol:
        factors_sorted = factors_sorted.drop("cum_vol")
        total_factors = total_factors.drop("total_vol")

    raw_sorted = raw.sort(["symbol", "date"])

    # join_asof backward: 每根 K 线取 <= 其 date 的最新累积因子
    # 同时带 trade_date 列用于判断除权日标记
    right_cols = ["symbol", "trade_date", "cum_factor"] + (["cum_vol"] if has_vol else [])
    df = raw_sorted.join_asof(
        factors_sorted.select(right_cols),
        left_on="date",
        right_on="trade_date",
        by="symbol",
        strategy="backward",
        check_sortedness=False,
    )

    # 补充 total_factor + 前复权 + 除权标记,一次 with_columns 完成
    df = df.join(total_factors, on="symbol", how="left")

    is_ex = pl.col("trade_date") == pl.col("date")
    ratio = pl.col("cum_factor").fill_null(1.0) / pl.col("total_factor").fill_null(1.0)
    price_cols = [c for c in ("open", "high", "low", "close") if c in df.columns]
    exprs = [pl.col(c) * ratio for c in price_cols] + [is_ex.alias("ex_rights")]
    drop = ["trade_date", "cum_factor", "total_factor"]
    # 拆股/合股同时改变成交量口径: 事件前成交量乘以 (事件后累计 ÷ 事件前累计) 的倒数链,
    # 与价格同源前复权, 保证量比/均量跨拆股连续 (成交额不变, 不调整 amount)。
    if "cum_vol" in df.columns and "volume" in df.columns:
        vol_ratio = pl.col("total_vol").fill_null(1.0) / pl.col("cum_vol").fill_null(1.0)
        exprs.append(pl.col("volume") * vol_ratio)
        drop += ["cum_vol", "total_vol"]
    return df.with_columns(exprs).drop(drop)


# ================================================================
# 技术指标计算 (从 OHLCV 计算)
# ================================================================

# ── compute_indicators 的列依赖关系 (供 needed 裁剪时求闭包) ────────────
# target -> 其计算所依赖的中间/指标列 (仅列出依赖非原始 OHLCV 的项)
_INDICATOR_DEPS: dict[str, set[str]] = {
    "macd_dif": {"_ema12", "_ema26"},
    "boll_upper": {"ma20", "_boll_std"},
    "boll_lower": {"ma20", "_boll_std"},
    "macd_dea": {"macd_dif"},
    "macd_hist": {"macd_dif", "macd_dea"},
    "kdj_k": {"_kdj_ln", "_kdj_hn"},
    "kdj_d": {"kdj_k"},
    "kdj_j": {"kdj_k", "kdj_d"},
    "atr_14": {"_tr"},
    "vol_ratio_5d": {"_vol_ma5"},
    "annual_vol_20d": {"_daily_pct"},
    "rsi_6": {"_delta", "_gain", "_loss"},
    "rsi_14": {"_delta", "_gain", "_loss"},
    "rsi_24": {"_delta", "_gain", "_loss"},
}

# compute_indicators 可产出的全部指标/临时列 (needed=None 时即为此全集, 行为不变)
_ALL_INDICATOR_COLS: frozenset[str] = frozenset({
    "prev_close", "ma5", "ma10", "ma20", "ma30", "ma60",
    "ema5", "ema10", "ema20", "ema30", "ema60", "_ema12", "_ema26",
    "_boll_std", "_kdj_ln", "_kdj_hn", "_tr", "vol_ma5", "vol_ma10",
    "_vol_ma5", "high_60d", "low_60d",
    "macd_dif", "boll_upper", "boll_lower", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "atr_14", "vol_ratio_5d",
    "momentum_5d", "momentum_10d", "momentum_20d", "momentum_30d", "momentum_60d",
    "change_pct", "change_amount", "amplitude", "_daily_pct", "annual_vol_20d",
    "rsi_6", "rsi_14", "rsi_24",
})


def _resolve_needed(needed: set[str] | None) -> set[str]:
    """把 needed 展开为闭包 (含所依赖的中间列)。needed=None → 全集。"""
    if needed is None:
        return set(_ALL_INDICATOR_COLS)
    want = set(needed)
    changed = True
    while changed:
        changed = False
        for target in list(want):
            deps = _INDICATOR_DEPS.get(target)
            if deps and not deps <= want:
                want |= deps
                changed = True
    return want


def compute_indicators(
    df: pl.DataFrame,
    needed: set[str] | None = None,
    *,
    assume_sorted: bool = False,
) -> pl.DataFrame:
    """从 OHLCV 数据计算全套技术指标。

    输入必须包含: symbol, date, open, high, low, close, volume
    返回添加了所有指标列的 DataFrame。

    needed:
        None (默认) — 计算全部指标, 行为与历史逐位一致 (所有 gate 为真, 表达式/顺序不变)。
        列名集合   — 仅计算这些列及其依赖闭包, 跳过无关的 EMA/KDJ/RSI 等 pass;
                     输出保留输入列 + 所需指标列。被保留列的数值与全量计算逐位一致
                     (逐列 window/rolling 相互独立, 跳过其它列不影响保留列)。
    """
    if df.is_empty():
        return df

    import time as _time
    _t0 = _time.perf_counter()

    want = _resolve_needed(needed)

    df = df if assume_sorted else df.sort(["symbol", "date"])

    # Pass 1: 均线 + EMA + MACD 基础 + BOLL 基础 + KDJ 基础 + ATR 基础 + 量价 + 极值
    prev_close = pl.col("close").shift(1).over("symbol")
    _p1: list[pl.Expr] = []
    if "prev_close" in want:
        _p1.append(prev_close.alias("prev_close"))
    if "ma5" in want:
        _p1.append(pl.col("close").rolling_mean(5).over("symbol").alias("ma5"))
    if "ma10" in want:
        _p1.append(pl.col("close").rolling_mean(10).over("symbol").alias("ma10"))
    if "ma20" in want:
        _p1.append(pl.col("close").rolling_mean(20).over("symbol").alias("ma20"))
    if "ma30" in want:
        _p1.append(pl.col("close").rolling_mean(30).over("symbol").alias("ma30"))
    if "ma60" in want:
        _p1.append(pl.col("close").rolling_mean(60).over("symbol").alias("ma60"))
    if "ema5" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(5), adjust=False).over("symbol").alias("ema5"))
    if "ema10" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(10), adjust=False).over("symbol").alias("ema10"))
    if "ema20" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(20), adjust=False).over("symbol").alias("ema20"))
    if "ema30" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(30), adjust=False).over("symbol").alias("ema30"))
    if "ema60" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(60), adjust=False).over("symbol").alias("ema60"))
    if "_ema12" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(12), adjust=False).over("symbol").alias("_ema12"))
    if "_ema26" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(26), adjust=False).over("symbol").alias("_ema26"))
    if "_boll_std" in want:
        _p1.append(pl.col("close").rolling_std(20).over("symbol").alias("_boll_std"))
    if "_kdj_ln" in want:
        _p1.append(pl.col("low").rolling_min(9).over("symbol").alias("_kdj_ln"))
    if "_kdj_hn" in want:
        _p1.append(pl.col("high").rolling_max(9).over("symbol").alias("_kdj_hn"))
    if "_tr" in want:
        _p1.append(pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - prev_close).abs(),
            (pl.col("low") - prev_close).abs(),
        ).alias("_tr"))
    if "vol_ma5" in want:
        _p1.append(pl.col("volume").rolling_mean(5).over("symbol").alias("vol_ma5"))
    if "vol_ma10" in want:
        _p1.append(pl.col("volume").rolling_mean(10).over("symbol").alias("vol_ma10"))
    if "_vol_ma5" in want:
        _p1.append(pl.col("volume").rolling_mean(5).over("symbol").alias("_vol_ma5"))
    if "vol_ratio_5d" in want:
        # 前5日平均成交量(不含当天), 标准量比分母: volume.shift(1).rolling_mean(5)
        _p1.append(pl.col("volume").shift(1).rolling_mean(5).over("symbol").alias("_vol_ma5_prev"))
    if "high_60d" in want:
        _p1.append(pl.col("close").rolling_max(60).over("symbol").alias("high_60d"))
    if "low_60d" in want:
        _p1.append(pl.col("close").rolling_min(60).over("symbol").alias("low_60d"))
    if _p1:
        df = df.with_columns(_p1)

    # Pass 2: MACD + BOLL (基于 Pass 1 基础列)
    _p2: list[pl.Expr] = []
    if "macd_dif" in want:
        _p2.append((pl.col("_ema12") - pl.col("_ema26")).alias("macd_dif"))
    if "boll_upper" in want:
        _p2.append((pl.col("ma20") + 2 * pl.col("_boll_std")).alias("boll_upper"))
    if "boll_lower" in want:
        _p2.append((pl.col("ma20") - 2 * pl.col("_boll_std")).alias("boll_lower"))
    if _p2:
        df = df.with_columns(_p2)
    if "macd_dea" in want:
        df = df.with_columns(
            pl.col("macd_dif").ewm_mean(alpha=_ema_alpha(9), adjust=False).over("symbol").alias("macd_dea"),
        )
    if "macd_hist" in want:
        df = df.with_columns(
            ((pl.col("macd_dif") - pl.col("macd_dea")) * 2).alias("macd_hist"),
        )

    # Pass 3: KDJ
    if "kdj_k" in want:
        # 9 日内最高价=最低价 (场内货币 ETF、长期无成交标的) 时分母是 0 而不是空值,
        # fill_null 拦不住: 0/0 得到 NaN, 再被 ewm 递推永久传染。与矩阵路径口径一致 ——
        # 该日 RSV 置空, EWM 跳过空值后继续递推。
        _kdj_range = pl.col("_kdj_hn") - pl.col("_kdj_ln")
        _kdj_rsv = pl.when(_kdj_range > 0).then(
            100 * (pl.col("close") - pl.col("_kdj_ln")) / _kdj_range
        )
        df = df.with_columns([
            _kdj_rsv.ewm_mean(alpha=1.0 / 3, adjust=False, ignore_nulls=True)
            .over("symbol").alias("kdj_k"),
        ])
    if "kdj_d" in want:
        df = df.with_columns([
            pl.col("kdj_k").ewm_mean(alpha=1.0 / 3, adjust=False, ignore_nulls=True)
            .over("symbol").alias("kdj_d"),
        ])
    if "kdj_j" in want:
        df = df.with_columns([
            (3 * pl.col("kdj_k") - 2 * pl.col("kdj_d")).alias("kdj_j"),
        ])

    # Pass 4: ATR + 量比 + 动量 + 波动 + 涨跌幅 + 涨跌额 + 振幅
    if "atr_14" in want:
        df = df.with_columns(
            pl.col("_tr").ewm_mean(alpha=1.0 / 14, adjust=False).over("symbol").alias("atr_14"),
        )
    if "vol_ratio_5d" in want:
        # 标准量比: 今日成交量 / 前5日均量(不含当天)
        # 盘后全量路径: 当日 volume 是完整全天量, 无需时间折算
        df = df.with_columns(
            (pl.col("volume") / pl.col("_vol_ma5_prev")).alias("vol_ratio_5d"),
        )
    _p4mom: list[pl.Expr] = []
    if "momentum_5d" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(5).over("symbol") - 1).alias("momentum_5d"))
    if "momentum_10d" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(10).over("symbol") - 1).alias("momentum_10d"))
    if "momentum_20d" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("momentum_20d"))
    if "momentum_30d" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(30).over("symbol") - 1).alias("momentum_30d"))
    if "momentum_60d" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(60).over("symbol") - 1).alias("momentum_60d"))
    if "change_pct" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("change_pct"))
    if _p4mom:
        df = df.with_columns(_p4mom)
    if "change_amount" in want:
        df = df.with_columns(
            (pl.col("close") - pl.col("close").shift(1).over("symbol")).alias("change_amount"),
        )
    if "amplitude" in want:
        df = df.with_columns(
            pl.when(pl.col("close").shift(1).over("symbol") > 0)
              .then((pl.col("high") - pl.col("low")) / pl.col("close").shift(1).over("symbol"))
              .otherwise(None)
              .alias("amplitude"),
        )
    if "_daily_pct" in want:
        df = df.with_columns(
            pl.col("close").pct_change().over("symbol").alias("_daily_pct"),
        )
    if "annual_vol_20d" in want:
        df = df.with_columns(
            (pl.col("_daily_pct").rolling_std(20).over("symbol") * (252 ** 0.5))
                .alias("annual_vol_20d"),
        )

    # Pass 5: RSI
    if want & {"rsi_6", "rsi_14", "rsi_24"}:
        df = df.with_columns(
            pl.col("close").diff().over("symbol").alias("_delta"),
        ).with_columns([
            pl.when(pl.col("_delta") > 0).then(pl.col("_delta")).otherwise(0.0).alias("_gain"),
            pl.when(pl.col("_delta") < 0).then(-pl.col("_delta")).otherwise(0.0).alias("_loss"),
        ])
        for n in (6, 14, 24):
            if f"rsi_{n}" not in want:
                continue
            a = 1.0 / n
            df = df.with_columns([
                pl.col("_gain").ewm_mean(alpha=a, adjust=False).over("symbol").alias(f"_rsi_avg_gain_{n}"),
                pl.col("_loss").ewm_mean(alpha=a, adjust=False).over("symbol").alias(f"_rsi_avg_loss_{n}"),
            ]).with_columns(
                (100 - 100 / (1 + pl.col(f"_rsi_avg_gain_{n}") /
                             pl.when(pl.col(f"_rsi_avg_loss_{n}") == 0)
                               .then(1e-12)
                               .otherwise(pl.col(f"_rsi_avg_loss_{n}"))
                             )).alias(f"rsi_{n}"),
            )

    # Pass 6: 换手率 (需要 float_shares, 后续在 compute_all 中 JOIN instruments 后补充)

    # 清理临时列 (只丢弃实际存在的临时列)
    _temp_cols = ["_boll_std", "_tr", "_ema12", "_ema26",
                  "_kdj_ln", "_kdj_hn", "_vol_ma5", "_vol_ma5_prev", "_daily_pct",
                  "_delta", "_gain", "_loss",
                  "_rsi_avg_gain_6", "_rsi_avg_loss_6",
                  "_rsi_avg_gain_14", "_rsi_avg_loss_14",
                  "_rsi_avg_gain_24", "_rsi_avg_loss_24"]
    df = df.drop([c for c in _temp_cols if c in df.columns])

    _elapsed = (_time.perf_counter() - _t0) * 1000
    import logging as _logging
    _logging.getLogger(__name__).debug("compute_indicators: %.1fms, %d rows", _elapsed, len(df))

    return df


SIGNAL_DEPENDENCIES: dict[str, frozenset[str]] = {
    "signal_ma_golden_5_20": frozenset({"ma5", "ma20"}),
    "signal_ma_dead_5_20": frozenset({"ma5", "ma20"}),
    "signal_ma_golden_20_60": frozenset({"ma20", "ma60"}),
    "signal_macd_golden": frozenset({"macd_dif", "macd_dea"}),
    "signal_macd_dead": frozenset({"macd_dif", "macd_dea"}),
    "signal_ma20_breakout": frozenset({"close", "ma20"}),
    "signal_ma20_breakdown": frozenset({"close", "ma20"}),
    "signal_ma5_breakout": frozenset({"close", "ma5"}),
    "signal_ma5_breakdown": frozenset({"close", "ma5"}),
    "signal_ma10_breakout": frozenset({"close", "ma10"}),
    "signal_ma10_breakdown": frozenset({"close", "ma10"}),
    "signal_n_day_high": frozenset({"close", "high_60d"}),
    "signal_n_day_low": frozenset({"close", "low_60d"}),
    "signal_boll_breakout_upper": frozenset({"close", "boll_upper"}),
    "signal_boll_breakdown_lower": frozenset({"close", "boll_lower"}),
    "signal_volume_surge": frozenset({"vol_ratio_5d"}),
}

LIMIT_SIGNAL_OUTPUTS: frozenset[str] = frozenset({
    "turnover_rate",
})

INDICATOR_COLUMNS: frozenset[str] = frozenset(
    col for col in _ALL_INDICATOR_COLS if not col.startswith("_")
)


def get_signal_dependencies() -> dict[str, frozenset[str]]:
    """返回内置与 JSON 自定义信号的唯一依赖映射。"""
    from app.strategy import custom_signals

    return {
        **SIGNAL_DEPENDENCIES,
        **custom_signals.expression_dependencies(_get_custom_signal_exprs()),
    }

def compute_signals(df: pl.DataFrame, needed: set[str] | None = None) -> pl.DataFrame:
    """从已有指标列计算原子信号布尔列。

    输入必须包含 compute_indicators() 产出的指标列。
    """
    if df.is_empty():
        return df

    want = set(SIGNAL_DEPENDENCIES) if needed is None else set(needed) & set(SIGNAL_DEPENDENCIES)
    expressions: dict[str, pl.Expr] = {
        "signal_ma_golden_5_20": ((pl.col("ma5") > pl.col("ma20")) &
         (pl.col("ma5").shift(1).over("symbol") <= pl.col("ma20").shift(1).over("symbol")))
            .alias("signal_ma_golden_5_20"),
        "signal_ma_dead_5_20": ((pl.col("ma5") < pl.col("ma20")) &
         (pl.col("ma5").shift(1).over("symbol") >= pl.col("ma20").shift(1).over("symbol")))
            .alias("signal_ma_dead_5_20"),
        "signal_ma_golden_20_60": ((pl.col("ma20") > pl.col("ma60")) &
         (pl.col("ma20").shift(1).over("symbol") <= pl.col("ma60").shift(1).over("symbol")))
            .alias("signal_ma_golden_20_60"),
        "signal_macd_golden": ((pl.col("macd_dif") > pl.col("macd_dea")) &
         (pl.col("macd_dif").shift(1).over("symbol") <= pl.col("macd_dea").shift(1).over("symbol")))
            .alias("signal_macd_golden"),
        "signal_macd_dead": ((pl.col("macd_dif") < pl.col("macd_dea")) &
         (pl.col("macd_dif").shift(1).over("symbol") >= pl.col("macd_dea").shift(1).over("symbol")))
            .alias("signal_macd_dead"),
        "signal_ma20_breakout": ((pl.col("close") > pl.col("ma20")) &
         (pl.col("close").shift(1).over("symbol") <= pl.col("ma20").shift(1).over("symbol")))
            .alias("signal_ma20_breakout"),
        "signal_ma20_breakdown": ((pl.col("close") < pl.col("ma20")) &
         (pl.col("close").shift(1).over("symbol") >= pl.col("ma20").shift(1).over("symbol")))
            .alias("signal_ma20_breakdown"),
        "signal_ma5_breakout": ((pl.col("close") > pl.col("ma5")) &
         (pl.col("close").shift(1).over("symbol") <= pl.col("ma5").shift(1).over("symbol")))
            .alias("signal_ma5_breakout"),
        "signal_ma5_breakdown": ((pl.col("close") < pl.col("ma5")) &
         (pl.col("close").shift(1).over("symbol") >= pl.col("ma5").shift(1).over("symbol")))
            .alias("signal_ma5_breakdown"),
        "signal_ma10_breakout": ((pl.col("close") > pl.col("ma10")) &
         (pl.col("close").shift(1).over("symbol") <= pl.col("ma10").shift(1).over("symbol")))
            .alias("signal_ma10_breakout"),
        "signal_ma10_breakdown": ((pl.col("close") < pl.col("ma10")) &
         (pl.col("close").shift(1).over("symbol") >= pl.col("ma10").shift(1).over("symbol")))
            .alias("signal_ma10_breakdown"),
        "signal_n_day_high": (pl.col("close") >= pl.col("high_60d")).alias("signal_n_day_high"),
        "signal_n_day_low": (pl.col("close") <= pl.col("low_60d")).alias("signal_n_day_low"),
        "signal_boll_breakout_upper": (pl.col("close") > pl.col("boll_upper")).alias("signal_boll_breakout_upper"),
        "signal_boll_breakdown_lower": (pl.col("close") < pl.col("boll_lower")).alias("signal_boll_breakdown_lower"),
        "signal_volume_surge": (pl.col("vol_ratio_5d") >= 2.0).alias("signal_volume_surge"),
    }
    if want:
        df = df.with_columns([expressions[name] for name in SIGNAL_DEPENDENCIES if name in want])

    # 自定义信号（用户配置的字段+运算符+值组合，编译为布尔列）。
    # 扩展表数值列先行 join (ext_ 因子列 = 帧上已有列): 信号条件与评分引用
    # 都按列存在性解析。历史多日帧仅注入时序模式 —— 快照代表"最新值",
    # 历史回看注入会引入未来数据 (CONTRIBUTING §5.3)。
    from app.factors import ext_factors
    df = ext_factors.attach_ext_columns(df, include_snapshot=False)
    # 条件引用的注册表因子列先复用评分物化管线补算 (虚拟/自定义/复合均可)。
    from app.strategy import custom_signals
    exprs = _get_custom_signal_exprs()
    df = custom_signals.materialize_factor_columns(df, exprs, needed=needed)
    df = custom_signals.inject(df, exprs, needed=needed)

    return df



def compute_turnover(
    df: pl.DataFrame,
    instruments: pl.DataFrame,
    historical_shares: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """计算换手率(%) = volume(股) * 100 / float_shares(股)。

    美股 volume 单位是股 (Alpaca), 不是「手」— 原 ×10000 公式会放大 100 倍 (9-29 审计)。
    注: Alpaca instruments 不含 float_shares, 当前全量为 None, 本计算整体惰性;
    公式先修对, 待股本数据源接入即生效。
    """
    if df.is_empty():
        return df
    inst_cols = ["symbol"]
    if "float_shares" in instruments.columns:
        inst_cols.append("float_shares")
    inst_subset = instruments.select(inst_cols).unique(subset=["symbol"])
    df = df.join(inst_subset, on="symbol", how="left", suffix="_inst")
    df = apply_historical_float_shares(df, historical_shares, today=cn_today())
    if "float_shares" in df.columns and "volume" in df.columns:
        df = df.with_columns(
            pl.when(pl.col("float_shares") > 0)
              .then(pl.col("volume") * 100.0 / pl.col("float_shares"))
              .otherwise(None)
              .alias("turnover_rate")
        )
    elif "turnover_rate" not in df.columns:
        df = df.with_columns(pl.lit(None).cast(pl.Float64).alias("turnover_rate"))
    return df



def compute_all(
    df: pl.DataFrame,
    instruments: pl.DataFrame | None = None,
    historical_shares: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """从 OHLCV 计算全套指标 + 信号。一站式调用。

    输入: symbol, date, open, high, low, close, volume, amount, raw_close
    """
    df = compute_indicators(df)
    df = compute_signals(df)
    if instruments is not None and not instruments.is_empty():
        df = compute_turnover(df, instruments, historical_shares=historical_shares)

    # 清理 NaN / Inf
    float_cols = [c for c in df.columns if df[c].dtype.is_float()]
    if float_cols:
        df = df.with_columns([
            pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite())
              .then(None)
              .otherwise(pl.col(c))
              .alias(c)
            for c in float_cols
        ])

    return df


def filter_halt_days(df: pl.DataFrame) -> pl.DataFrame:
    """过滤停牌日。

    停牌日的 open/high 必然为 0 (无集合竞价)。注意 close 可能被数据源
    填充为前收盘价而非 0, 因此不能用 "OHLC 全零" 判断, 否则会漏过这类
    停牌记录 (如 *ST 撤销风险警示的停牌日), 污染 MA/ATR 等指标。旧版实时
    落盘还会先把 open/high=0 填成 close, 对这类历史数据用零成交量和零成交额
    作为兼容判据。
    """
    if df.is_empty() or "open" not in df.columns or "high" not in df.columns:
        return df
    halted = (pl.col("open") == 0) & (pl.col("high") == 0)
    if "volume" in df.columns and "amount" in df.columns:
        halted = halted | ((pl.col("volume") == 0) & (pl.col("amount") == 0))
    # 缺失成交量不是零成交：IEX价格降级会显式清空全市场量额。
    return df.filter(~halted.fill_null(False))


# ================================================================
# Pipeline: 盘后全量计算 + 写入
# ================================================================

def compute_enriched(
    raw: pl.DataFrame,
    factors: pl.DataFrame | None = None,
    instruments: pl.DataFrame | None = None,
    historical_shares: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """对原始日 K 应用前复权 + 全量计算指标 + 信号, 产出完整 enriched (含全部指标列)。

    输入应包含至少: symbol, date, open, high, low, close, volume (可选 amount)。
    如果提供了 factors, 先应用前复权再算指标。
    如果提供了 instruments, 计算换手率。
    """
    if raw.is_empty():
        return raw

    # 过滤停牌日 (会污染指标计算)
    raw = filter_halt_days(raw)

    if raw.is_empty():
        return raw

    # 保留不复权原始价格 (历史列)
    raw = raw.with_columns(
        pl.col("close").alias("raw_close"),
        pl.col("high").alias("raw_high"),
        pl.col("low").alias("raw_low"),
    )

    # 应用前复权（只改 open/high/low/close，raw_close 不受影响）
    if factors is not None and not factors.is_empty():
        raw = _apply_adj_factor(raw, factors)

    # 排序
    df = raw.sort(["symbol", "date"])

    # 全量计算指标 + 信号
    df = compute_all(
        df,
        instruments=instruments,
        historical_shares=historical_shares,
    )

    return df


def _select_storage_cols(df: pl.DataFrame) -> pl.DataFrame:
    """写入 parquet 前裁剪到存储列 (14 列)。"""
    cols = [c for c in ENRICHED_STORAGE_COLS if c in df.columns]
    return df.select(cols)


# ================================================================
# 全量重建流式暂存 + 自适应批次 (#208/#174)
#
# 旧全量模式把所有批次结果累积在内存 date_buffers 直到统一写盘:
# 延长历史后 (5年 × 5500 只 ≈ 800 万行) 「全表驻留 + 单批宽表」双双
# 超出小内存机器上限, 重建必然 OOM。现改为:
#   - 每批结果立即写暂存文件 (enriched 树外的隐藏目录 —— polars/duckdb
#     的 **/*.parquet glob 均会匹配点目录, 树内暂存会被业务读取扫到),
#     最后按日期分块流式合并、逐分区原子替换;
#   - 批次大小按单批目标行数自适应收缩 (指标/信号全部 over("symbol")
#     分组, symbol 级分批不改变计算结果, 只约束单批宽表峰值)。
# 任一时刻峰值内存 = 单批计算 + 单个日期块合并, 与总历史长度无关。
# ================================================================

_STAGING_ROOT = Path(".staging") / "enriched_rebuild"
_STALE_STAGING_MAX_AGE_S = 24 * 3600
_RAM_LARGE_BYTES = 8 * 1024 ** 3      # ≥8GB 视为内存充裕, 批次保持用户设置
_BATCH_TARGET_ROWS = 150_000          # 小内存单批目标行数 (宽表 ~60-80MB)
_BATCH_MIN_SYMBOLS = 50
_MERGE_DATE_CHUNKS = 15               # 最终合并按日期切 15 块流式执行

_ram_bytes_cache: int | None | bool = False  # False = 未探测


def _total_ram_bytes() -> int | None:
    global _ram_bytes_cache
    if _ram_bytes_cache is False:
        try:
            import psutil
            _ram_bytes_cache = psutil.virtual_memory().total
        except Exception:
            _ram_bytes_cache = None
    return _ram_bytes_cache  # type: ignore[return-value]


def _adaptive_sym_batch(default_batch: int, rows_per_symbol: int) -> int:
    """小内存机器按单批目标行数收缩批次; 大内存机器保持原值 (#208)。"""
    if (_total_ram_bytes() or 0) >= _RAM_LARGE_BYTES:
        return default_batch
    return max(
        _BATCH_MIN_SYMBOLS,
        min(default_batch, _BATCH_TARGET_ROWS // max(rows_per_symbol, 1)),
    )


def _sweep_stale_staging(data_dir: Path) -> None:
    """清理崩溃/取消运行残留的暂存目录 (按 mtime 判定, 不碰活跃目录)。"""
    root = data_dir / _STAGING_ROOT
    if not root.exists():
        return
    cutoff = time.time() - _STALE_STAGING_MAX_AGE_S
    for run_dir in root.iterdir():
        try:
            if run_dir.is_dir() and run_dir.stat().st_mtime < cutoff:
                shutil.rmtree(run_dir, ignore_errors=True)
        except OSError:
            pass


def compute_enriched_history_window(
    df_hist: pl.DataFrame,
    data_dir: Path,
    instruments: pl.DataFrame | None = None,
    historical_shares: pl.DataFrame | None = None,
    sym_batch: int | None = None,
    *,
    include_instrument_metadata: bool = False,
) -> pl.DataFrame:
    """按 symbol 分批执行历史窗口计算: 指标 → 信号 → 换手率（历史兼容）。

    分批约束计算中间表, 最终完整历史仍常驻内存。排序和可选元数据关联
    均在单批完成, 避免合并后再复制整张宽表。
    sym_batch 显式传入时跳过自适应 (测试用)。
    """
    if df_hist.is_empty() or "symbol" not in df_hist.columns:
        return df_hist
    symbols = df_hist["symbol"].unique().sort().to_list()
    if sym_batch is None:
        rows_per_sym = max(1, df_hist.height // max(len(symbols), 1))
        sym_batch = _adaptive_sym_batch(2000, rows_per_sym)
    parts: list[pl.DataFrame] = []
    for bs in range(0, len(symbols), sym_batch):
        batch = symbols[bs:bs + sym_batch]
        part = df_hist.filter(pl.col("symbol").is_in(batch)).sort(["symbol", "date"])
        part = compute_indicators(part)
        part = compute_signals(part)
        if instruments is not None and not instruments.is_empty():
            inst_batch = instruments.filter(pl.col("symbol").is_in(batch))
            shares_batch = (
                historical_shares.filter(pl.col("symbol").is_in(batch))
                if historical_shares is not None and not historical_shares.is_empty()
                else historical_shares
            )
            part = compute_turnover(part, inst_batch, historical_shares=shares_batch)
            if include_instrument_metadata:
                inst_cols = [c for c in ("name", "total_shares", "float_shares")
                             if c in inst_batch.columns and c not in part.columns]
                if inst_cols:
                    part = part.join(
                        inst_batch.select("symbol", *inst_cols).unique(subset=["symbol"]),
                        on="symbol", how="left",
                    )
        # 连续、互不重叠的已排序 symbol 批次, 拼接后天然有序。
        parts.append(part.sort(["symbol", "date"]))
    return parts[0] if len(parts) == 1 else pl.concat(parts, how="diagonal_relaxed", rechunk=False)


def _compute_storage_batches(
    raw: pl.DataFrame,
    *,
    factors: pl.DataFrame,
    instruments: pl.DataFrame,
    historical_shares: pl.DataFrame,
) -> pl.DataFrame:
    """保留完整标的历史输入, 单批计算宽表后仅累积落盘窄表。"""
    from app.services import preferences

    if raw.is_empty():
        return _select_storage_cols(raw)
    symbols = raw["symbol"].unique().sort().to_list()
    rows_per_sym = max(1, -(-raw.height // len(symbols)))
    batch_size = _adaptive_sym_batch(preferences.get_enriched_batch_size(), rows_per_sym)
    parts = []
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start:start + batch_size]
        part = compute_enriched(
            raw.filter(pl.col("symbol").is_in(batch)),
            factors=factors.filter(pl.col("symbol").is_in(batch)) if not factors.is_empty() else factors,
            instruments=(instruments.filter(pl.col("symbol").is_in(batch))
                         if not instruments.is_empty() else instruments),
            historical_shares=(historical_shares.filter(pl.col("symbol").is_in(batch))
                               if not historical_shares.is_empty() else historical_shares),
        )
        # 下一批开始前释放宽表; 分区发布仍在所有计算批次成功之后。
        if not part.is_empty():
            parts.append(_select_storage_cols(part))
        del part
    if not parts:
        return _select_storage_cols(raw.head(0))
    return pl.concat(parts, how="diagonal_relaxed", rechunk=False)


def run_pipeline(data_dir: Path | None = None,
                 symbols: list[str] | None = None,
                 new_dates_only: bool = False,
                 on_batch_done: Callable[[int, int], None] | None = None) -> int:
    """运行盘后管道:读 kline_daily + adj_factor → 前复权 + 计算存储列 → 写 enriched。

    enriched 表仅存储 14 列基础行情窄表 (OHLCV + raw_close/high/low + turnover_rate 等)。

    模式:
      - 全量 (symbols=None, new_dates_only=False):
          读全部 kline_daily, 全部重写 enriched 分区。
          用于首次同步、往前扩展历史。
      - 向后增量 (new_dates_only=True):
          只读 enriched 中尚不存在的日期分区对应的 daily 数据,
          为所有标的生成新的 enriched 分区;
          若同时传 symbols, 还会对这些个股的全部已有日期做重算
          (因为除权因子链变了,历史数据的复权比例也要更新)。
      - 除权因子增量 (symbols 指定, new_dates_only=False):
          只对指定 symbol 做局部重算并合并回已有 enriched。
          用于无新日K数据、仅除权因子变更的场景。
    返回写入的行数。
    """
    import time as _t
    t0 = _t.perf_counter()

    d = Path(data_dir or settings.data_dir)
    if enriched_publication_incomplete(d, "stock"):
        logger.warning("检测到未完成的 enriched 发布,改为全量重建")
        symbols = None
        new_dates_only = False
    publication = EnrichedPublication(d, "stock", recover=True)
    daily_dir = d / "kline_daily"
    enriched_base = d / "kline_daily_enriched"
    factor_path = d / "adj_factor" / "all.parquet"
    inst_glob = str(d / "instruments" / "**" / "*.parquet")

    if not daily_dir.exists() or not any(daily_dir.rglob("*.parquet")):
        logger.info("无日K数据, 跳过管道")
        return 0

    daily_glob = (daily_dir / "**" / "*.parquet").as_posix()
    _cast = pl.ScanCastOptions(integer_cast="allow-float")
    written = 0

    # 加载 instruments (换手率需要)
    instruments = pl.DataFrame()
    try:
        instruments = scan_parquet_compat(inst_glob, cast_options=_cast).collect()
    except Exception as e:  # noqa: BLE001
        logger.warning("instruments 读取失败: %s", e)
    historical_shares = load_share_history(d)

    if new_dates_only:
        # ── 向后增量模式 ──
        # 1. 找出 daily 有但 enriched 还没有的日期
        enriched_dates = set()
        if enriched_base.exists():
            enriched_dates = {p.stem.split("=")[1] for p in enriched_base.glob("date=*")}

        # 读新增日期的 daily 数据 (所有标的)
        new_date_dirs = sorted(
            p for p in daily_dir.glob("date=*")
            if p.stem.split("=")[1] not in enriched_dates
        )
        if not new_date_dirs and not symbols:
            logger.info("增量模式: 无新日期, 无需重算")
            return 0

        # 加载复权因子 (全量,因为所有标的都可能需要)
        factors = _load_factors(factor_path)

        # 2. 为新日期计算 enriched (所有标的)
        if new_date_dirs:
            raw_new = scan_daily_parquet(new_date_dirs[0] / "*.parquet", cast_options=_cast)
            for nd in new_date_dirs[1:]:
                raw_new = pl.concat([raw_new, scan_daily_parquet(nd / "*.parquet", cast_options=_cast)], how="diagonal_relaxed")
            raw_new = raw_new.sort(["symbol", "date"]).collect(streaming=True)

            # 增量模式: 只算新日期, 但指标需要历史窗口
            # 读已有 enriched 最近 60 天作为历史前缀
            sym_list = raw_new["symbol"].unique().to_list()
            hist_df = _load_recent_history(enriched_base, sym_list, days=60)

            # 合并历史 + 新数据
            if not hist_df.is_empty():
                # 只取基础行情列做历史前缀
                hist_cols = [c for c in ["symbol", "date", "open", "high", "low", "close",
                                         "volume", "amount", "raw_close", "raw_high", "raw_low"]
                             if c in hist_df.columns]
                raw_full = pl.concat([hist_df.select(hist_cols), raw_new], how="diagonal_relaxed")
            else:
                raw_full = raw_new

            enriched_new = _compute_storage_batches(
                raw_full,
                factors=factors,
                instruments=instruments,
                historical_shares=historical_shares,
            )

            # 只保留新日期的行
            new_date_set = set()
            for nd in new_date_dirs:
                ds = nd.stem.split("=")[1]
                new_date_set.add(ds)
            enriched_new = enriched_new.filter(
                pl.col("date").map_elements(lambda x: x.isoformat(), return_dtype=pl.Utf8).is_in(list(new_date_set))
            )

            t_new = _t.perf_counter()
            logger.info("增量计算: %d 个新日期, %d 行, 耗时 %.2fs",
                        len(new_date_dirs), enriched_new.height, t_new - t0)

            if not enriched_new.is_empty():
                for date_df in enriched_new.partition_by("date"):
                    dt = date_df["date"][0]
                    ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
                    out = enriched_base / f"date={ds}" / "part.parquet"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    date_df = _select_storage_cols(date_df).sort(["symbol"])
                    publication.write_parquet(date_df, out)
                    written += date_df.height
                t_write_new = _t.perf_counter()
                logger.info("增量写入: %.2fs, %d 行", t_write_new - t_new, written)
            del raw_new, hist_df, raw_full, enriched_new

        # 3. 受除权因子影响的个股: 重算全部已有日期 (累积因子链变了)
        if symbols:
            sym_set = set(symbols)
            raw_sym = scan_daily_parquet(daily_glob, cast_options=_cast).sort(["symbol", "date"])
            raw_sym = raw_sym.filter(pl.col("symbol").is_in(list(sym_set)))
            raw_sym = raw_sym.collect(streaming=True)
            if not raw_sym.is_empty():
                factors_sym = factors.filter(pl.col("symbol").is_in(list(sym_set))) if not factors.is_empty() else factors
                inst_sym = instruments.filter(pl.col("symbol").is_in(list(sym_set))) if not instruments.is_empty() else instruments
                shares_sym = historical_shares.filter(pl.col("symbol").is_in(list(sym_set))) if not historical_shares.is_empty() else historical_shares
                enriched_sym = _compute_storage_batches(
                    raw_sym,
                    factors=factors_sym,
                    instruments=inst_sym,
                    historical_shares=shares_sym,
                )
                for date_df in enriched_sym.partition_by("date"):
                    dt = date_df["date"][0]
                    ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
                    out = enriched_base / f"date={ds}" / "part.parquet"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    date_df_storage = _select_storage_cols(date_df)
                    if out.exists():
                        existing = pl.read_parquet(out)
                        existing = existing.filter(~pl.col("symbol").is_in(list(sym_set)))
                        date_df_storage = pl.concat([existing, date_df_storage], how="diagonal_relaxed")
                    date_df_storage = date_df_storage.sort(["symbol"])
                    publication.write_parquet(date_df_storage, out)
                    written += date_df.height
                logger.info("除权重算: %d 只, 共写入 %d 行", len(sym_set), written)

        publication.commit()
        t_done = _t.perf_counter()
        logger.info("增量管道完成: %.2fs, %d 行", t_done - t0, written)
        return written

    # ── 全量 或 除权因子增量 模式 ──
    mode = f"incremental ({len(symbols)} symbols)" if symbols else "full"
    base = d / "kline_daily_enriched"

    # 加载复权因子 (全量加载一次,每批复用)
    factors = _load_factors(factor_path)

    # 局部模式: 过滤 instruments
    inst_use = instruments

    import gc

    # ── 按 symbol 分批处理: 指标全部 over("symbol") 分组, 分批不改变结果 ──
    # 文件列表只收集一次, 批间复用 (避免每批重新展开 glob)
    daily_files = sorted(str(p) for p in daily_dir.rglob("*.parquet"))
    lf_all = scan_daily_parquet(daily_files, cast_options=_cast)
    if symbols:
        sym_set = set(symbols)
        lf_all = lf_all.filter(pl.col("symbol").is_in(list(sym_set)))

    all_symbols = (
        lf_all.select("symbol").unique().sort("symbol")
        .collect(streaming=True)["symbol"].to_list()
    )
    if not all_symbols:
        logger.info("无日K数据, 跳过管道")
        return 0

    total_syms = len(all_symbols)

    if not factors.is_empty() and symbols:
        factors = factors.filter(pl.col("symbol").is_in(list(sym_set)))
    if not factors.is_empty():
        logger.info("读取复权因子: %d 行", factors.height)
    if not instruments.is_empty() and symbols:
        inst_use = instruments.filter(pl.col("symbol").is_in(list(sym_set)))

    from app.services import preferences as prefs_mod
    # 自适应批次 (#208): 单批体积按目标行数恒定, 与总历史长度解耦;
    # 小内存机器自动收缩, 大内存机器保持用户设置
    total_rows = lf_all.select(pl.len()).collect(streaming=True).item()
    rows_per_sym = max(1, -(-int(total_rows) // total_syms))
    SYM_BATCH = _adaptive_sym_batch(prefs_mod.get_enriched_batch_size(), rows_per_sym)
    total_batches = (total_syms + SYM_BATCH - 1) // SYM_BATCH
    logger.info("全量计算: %d 只标的 (%d 行, ~%d 行/只), symbol 分批 %d 只/批, %d 批 [%s]",
                total_syms, total_rows, rows_per_sym, SYM_BATCH, total_batches, mode)

    # 全量模式: 流式暂存发布 (#208) —— 每批落盘暂存文件, 不再内存累积;
    # 暂存目录在 enriched 树外, 不会被任何 **/*.parquet 业务 glob 扫到
    staging_dir: Path | None = None
    staging_files: list[str] = []
    if not symbols:
        _sweep_stale_staging(d)
        staging_dir = d / _STAGING_ROOT / uuid.uuid4().hex
        staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        for batch_start in range(0, total_syms, SYM_BATCH):
            batch_end = min(batch_start + SYM_BATCH, total_syms)
            batch_syms = all_symbols[batch_start:batch_end]

            # 只读取本批 symbol 的数据
            lf_batch = scan_daily_parquet(daily_files, cast_options=_cast)
            lf_batch = lf_batch.filter(pl.col("symbol").is_in(batch_syms))
            raw = lf_batch.sort(["symbol", "date"]).collect(streaming=True)

            if raw.is_empty():
                continue

            # 本批的 factors / instruments
            batch_factors = (
                factors.filter(pl.col("symbol").is_in(batch_syms))
                if not factors.is_empty() else factors
            )
            batch_inst = (
                inst_use.filter(pl.col("symbol").is_in(batch_syms))
                if not inst_use.is_empty() else inst_use
            )
            batch_shares = (
                historical_shares.filter(pl.col("symbol").is_in(batch_syms))
                if not historical_shares.is_empty() else historical_shares
            )

            # 计算
            enriched = compute_enriched(
                raw,
                factors=batch_factors,
                instruments=batch_inst,
                historical_shares=batch_shares,
            )

            if not enriched.is_empty():
                if symbols:
                    # 局部模式: 直接按日期合并写入
                    for date_df in _select_storage_cols(enriched).partition_by("date"):
                        dt = date_df["date"][0]
                        ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
                        out = base / f"date={ds}" / "part.parquet"
                        out.parent.mkdir(parents=True, exist_ok=True)
                        date_df_storage = _select_storage_cols(date_df)
                        if out.exists():
                            existing = pl.read_parquet(out)
                            existing = existing.filter(~pl.col("symbol").is_in(batch_syms))
                            date_df_storage = pl.concat([existing, date_df_storage], how="diagonal_relaxed")
                        date_df_storage = date_df_storage.sort(["symbol"])
                        publication.write_parquet(date_df_storage, out)
                        written += date_df_storage.height
                else:
                    # 全量模式: 写单批暂存文件 (按 date,symbol 排序 →
                    # 合并期 parquet 行组统计可按日期裁剪), 随即释放本批内存
                    out = staging_dir / f"batch-{batch_start // SYM_BATCH:04d}.parquet"
                    _select_storage_cols(enriched).sort(["date", "symbol"]).write_parquet(out)
                    staging_files.append(str(out))
                    written += enriched.height

            del raw, enriched, batch_factors, batch_inst, batch_shares
            gc.collect()

            logger.info("symbol 批次 %d/%d (%s ~ %s), 已处理 %d 行",
                         batch_start // SYM_BATCH + 1,
                         total_batches,
                         batch_syms[0], batch_syms[-1], written)

            # 通知进度
            if on_batch_done:
                on_batch_done(batch_start // SYM_BATCH + 1, total_batches)

        # 全量模式: 日期覆盖校验 → 按日期分块流式合并 → 逐分区原子替换
        if not symbols and staging_files:
            existing_dates = {
                p.name.removeprefix("date=")
                for p in base.glob("date=*")
                if p.is_dir()
            }
            unique_dates = sorted(
                scan_enriched_parquet(staging_files).select("date").unique()
                .collect()["date"].to_list()
            )
            rebuilt_dates = {
                ds.isoformat() if hasattr(ds, "isoformat") else str(ds)
                for ds in unique_dates
            }
            missing_dates = existing_dates - rebuilt_dates
            if missing_dates:
                sample = ", ".join(sorted(missing_dates)[:5])
                raise RuntimeError(f"全量重建结果缺少已有日期分区,拒绝覆盖: {sample}")

            base.mkdir(parents=True, exist_ok=True)

            chunk = max(1, -(-len(unique_dates) // _MERGE_DATE_CHUNKS))
            for ci in range(0, len(unique_dates), chunk):
                lo = unique_dates[ci]
                hi = unique_dates[min(ci + chunk, len(unique_dates)) - 1]
                block = (
                    scan_enriched_parquet(staging_files)
                    .filter((pl.col("date") >= lo) & (pl.col("date") <= hi))
                    .sort(["date", "symbol"])
                    .collect(streaming=True)
                )
                for date_df in block.partition_by("date"):
                    ds = date_df["date"][0]
                    ds_str = ds.isoformat() if hasattr(ds, "isoformat") else str(ds)
                    out = base / f"date={ds_str}" / "part.parquet"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    publication.write_parquet(date_df.sort(["symbol"]), out)
            gc.collect()
            logger.info("全量暂存合并完成: %d 个日期分区", len(unique_dates))
    finally:
        # 无论成功/失败/取消都清掉本次暂存 (历史残留由 _sweep_stale_staging 兜底)
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)

    publication.commit()
    t_done = _t.perf_counter()
    adj_label = "含复权" if not factors.is_empty() else "无复权"
    logger.info("enriched 完成 [%s]: %.2fs, 共 %d 行, %s",
                mode, t_done - t0, written, adj_label)
    return written


def _load_factors(factor_path: Path) -> pl.DataFrame:
    """加载复权事件表。无 event_v2 口径标记的旧表 (逐日水平值) 不可用, 按无复权处理。"""
    if not factor_path.exists():
        return pl.DataFrame()
    from app.services.kline_sync import ADJ_EVENT_BASIS, adj_factor_basis
    if adj_factor_basis(factor_path.parent) != ADJ_EVENT_BASIS:
        logger.warning("复权因子表为旧口径 (逐日水平), 已忽略; 需完整历史重建除权事件")
        return pl.DataFrame()
    try:
        return pl.read_parquet(factor_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("复权因子读取失败: %s", e)
        return pl.DataFrame()


def _load_recent_history(enriched_base: Path, symbols: list[str], days: int) -> pl.DataFrame:
    """从已有 enriched parquet 加载最近 N 天的历史数据(用于增量模式的指标计算窗口)。

    只读基础行情列, 作为指标计算的历史前缀。
    """
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=days + 30)  # 多读 30 天余量

    cast_options = pl.ScanCastOptions(integer_cast="allow-float")
    try:
        lf = (
            scan_enriched_parquet(
                str(enriched_base / "**" / "*.parquet"),
                cast_options=cast_options,
            )
            .filter(
                (pl.col("symbol").is_in(symbols))
                & (pl.col("date") >= cutoff)
            )
            .sort(["symbol", "date"])
        )
        hist_cols = [c for c in ["symbol", "date", "open", "high", "low", "close",
                                 "volume", "amount", "raw_close", "raw_high", "raw_low"]
                    if c in lf.schema]
        return lf.select(hist_cols).collect()
    except Exception as e:  # noqa: BLE001
        logger.warning("历史数据加载失败: %s", e)
        return pl.DataFrame()


# ================================================================
# 盘中增量计算: 只算今天 5500 行 (不复算历史)
# ================================================================

def compute_enriched_today(
    live_agg: pl.DataFrame,
    prev_enriched: pl.DataFrame,
    today_ohlcv: pl.DataFrame,
    instruments: pl.DataFrame | None = None,
    elapsed_minutes: float | None = None,
) -> pl.DataFrame:
    """用昨天的递推状态 + 今天的 OHLCV 增量计算今天的 enriched 数据。

    只处理 ~5500 行, 耗时 ~10-50ms (替代全量 compute_enriched 的 1.5-2s)。

    参数:
        live_agg:       repo.get_live_agg() — 包含所有递推状态 + 窗口聚合
        prev_enriched:  repo.get_enriched_latest() — 昨天的完整 enriched (用于信号交叉判断)
        today_ohlcv:    今天的 OHLCV (symbol, date, open, high, low, close, volume, amount)
        instruments:    维表 (换手率需要)
        elapsed_minutes: 当日已交易分钟数(用于标准量比的时间折算)。
            None 或 0 表示不折算(盘后或时间不可用, 此时 volume 已是全天量)。

    返回:
        今天的 enriched DataFrame (~5500 行, 64 列)
    """
    if today_ohlcv.is_empty() or live_agg.is_empty():
        return pl.DataFrame()

    alpha = _ema_alpha

    # ---- JOIN: 今天的 OHLCV + 各股票最后一个有效交易日的递推状态 ----
    # 当日行情是主表, 复牌或新上市股票不能因为没有历史状态而被静默删除。
    live_state = live_agg.with_columns(pl.lit(True).alias("_has_history_state"))
    df = today_ohlcv.join(live_state, on="symbol", how="left")
    has_history_state = pl.col("_has_history_state").fill_null(False)

    # ---- 前复权: 保存原始价 → 调整 OHLCV ----
    df = df.with_columns([
        pl.col("close").alias("raw_close"),
        pl.col("high").alias("raw_high"),
        pl.col("low").alias("raw_low"),
    ])
    if "_adj_factor" in df.columns:
        af = pl.col("_adj_factor").fill_null(1.0)
        df = df.with_columns([
            (pl.col("open") * af).alias("open"),
            (pl.col("high") * af).alias("high"),
            (pl.col("low") * af).alias("low"),
            (pl.col("close") * af).alias("close"),
        ])

    # ---- volume 统一 Float64 ----
    df = df.with_columns(pl.col("volume").cast(pl.Float64))

    # ---- ex_rights: 盘中除权极罕见, 直接 false ----
    df = df.with_columns(pl.lit(False).alias("ex_rights"))

    # ---- 基础涨跌 ----
    # prev_close: 有则直接用 (来自 API quote_extra, raw), 需要乘 adj_factor 对齐复权价
    if "prev_close" not in df.columns:
        prev_close = pl.col("close_right") if "close_right" in df.columns else pl.col("close")
        df = df.with_columns(prev_close.alias("prev_close"))
    elif "_adj_factor" in df.columns:
        # 保存 API 原始前收盘价
        df = df.with_columns(pl.col("prev_close").alias("_prev_close_raw"))
        # API 返回的 prev_close 是原始价, 乘复权因子对齐复权价 (用于 change_pct)
        df = df.with_columns((pl.col("prev_close") * pl.col("_adj_factor").fill_null(1.0)).alias("prev_close"))

    # change_pct / change_amount / amplitude: 有则直接用, 无则计算
    if "change_pct" not in df.columns:
        df = df.with_columns((pl.col("close") / pl.col("prev_close") - 1).alias("change_pct"))
    if "change_amount" not in df.columns:
        df = df.with_columns((pl.col("close") - pl.col("prev_close")).alias("change_amount"))
    if "amplitude" not in df.columns:
        df = df.with_columns(
            pl.when(pl.col("prev_close") > 0)
              .then((pl.col("high") - pl.col("low")) / pl.col("prev_close"))
              .otherwise(None)
              .alias("amplitude"),
        )

    # ---- EMA (递推) ----
    df = df.with_columns([
        (alpha(5)  * pl.col("close") + (1 - alpha(5))  * pl.col("ema5")).alias("ema5"),
        (alpha(10) * pl.col("close") + (1 - alpha(10)) * pl.col("ema10")).alias("ema10"),
        (alpha(20) * pl.col("close") + (1 - alpha(20)) * pl.col("ema20")).alias("ema20"),
        (alpha(30) * pl.col("close") + (1 - alpha(30)) * pl.col("ema30")).alias("ema30"),
        (alpha(60) * pl.col("close") + (1 - alpha(60)) * pl.col("ema60")).alias("ema60"),
    ])

    # ---- MACD (递推) ----
    ema12 = alpha(12) * pl.col("close") + (1 - alpha(12)) * pl.col("_ema12")
    ema26 = alpha(26) * pl.col("close") + (1 - alpha(26)) * pl.col("_ema26")
    dif = ema12 - ema26
    dea = alpha(9) * dif + (1 - alpha(9)) * pl.col("macd_dea")
    df = df.with_columns([
        dif.alias("macd_dif"),
        dea.alias("macd_dea"),
        ((dif - dea) * 2).alias("macd_hist"),
    ])

    # ---- MA (用部分和) ----
    df = df.with_columns([
        ((pl.col("_ma5_partial_sum") + pl.col("close")) / 5).alias("ma5"),
        ((pl.col("_ma10_partial_sum") + pl.col("close")) / 10).alias("ma10"),
        ((pl.col("_ma20_partial_sum") + pl.col("close")) / 20).alias("ma20"),
        ((pl.col("_ma30_partial_sum") + pl.col("close")) / 30).alias("ma30"),
        ((pl.col("_ma60_partial_sum") + pl.col("close")) / 60).alias("ma60"),
    ])

    # ---- Bollinger ----
    boll_sum = pl.col("_boll_partial_sum") + pl.col("close")
    boll_sq_sum = pl.col("_boll_partial_sq_sum") + pl.col("close") ** 2
    boll_ma = boll_sum / 20
    # 样本方差 (ddof=1), 与全量 rolling_std(20) / 回测矩阵 ddof=1 同口径
    boll_var = (boll_sq_sum - boll_sum ** 2 / 20) / 19
    boll_std = pl.when(boll_var > 0).then(boll_var.sqrt()).otherwise(0.0)
    df = df.with_columns([
        (boll_ma + 2 * boll_std).alias("boll_upper"),
        (boll_ma - 2 * boll_std).alias("boll_lower"),
    ])

    # ---- KDJ (递推) ----
    kdj_ln = pl.min_horizontal(pl.col("_kdj_8d_low"), pl.col("low"))
    kdj_hn = pl.max_horizontal(pl.col("_kdj_8d_high"), pl.col("high"))
    rsv = (pl.col("close") - kdj_ln) / (kdj_hn - kdj_ln).fill_null(1e-12) * 100
    k_today = rsv / 3 + pl.col("kdj_k") * 2 / 3
    d_today = k_today / 3 + pl.col("kdj_d") * 2 / 3
    df = df.with_columns([
        k_today.alias("kdj_k"),
        d_today.alias("kdj_d"),
        (3 * k_today - 2 * d_today).alias("kdj_j"),
    ])

    # ---- ATR (递推) ----
    tr = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - pl.col("prev_close")).abs(),
        (pl.col("low") - pl.col("prev_close")).abs(),
    )
    df = df.with_columns(
        (tr / 14 + pl.col("atr_14") * 13 / 14).alias("atr_14"),
    )

    # ---- RSI (递推, n=6,14,24) ----
    delta = pl.col("close") - pl.col("prev_close")
    gain = pl.when(delta > 0).then(delta).otherwise(0.0)
    loss = pl.when(delta < 0).then(-delta).otherwise(0.0)
    for n in (6, 14, 24):
        a = 1.0 / n
        avg_gain = (1 - a) * pl.col(f"_rsi_avg_gain_{n}") + a * gain
        avg_loss = (1 - a) * pl.col(f"_rsi_avg_loss_{n}") + a * loss
        df = df.with_columns([
            avg_gain.alias(f"_rsi_avg_gain_{n}"),
            avg_loss.alias(f"_rsi_avg_loss_{n}"),
            (100 - 100 / (1 + avg_gain / pl.when(avg_loss == 0).then(1e-12).otherwise(avg_loss)))
            .alias(f"rsi_{n}"),
        ])

    # ---- 量比 ----
    # vol_ma5/vol_ma10 保留原语义(含当天的均量), 其他地方在用
    vol_ma5 = (pl.col("_vol_ma5_partial_sum") + pl.col("volume")) / 5
    vol_ma10 = (pl.col("_vol_ma10_partial_sum") + pl.col("volume")) / 10
    # 标准量比: 今日累计成交量 / (前5日均量 × 已交易分钟数/240)
    # _vol_ma5_prev_sum 是前5个交易日成交量之和(tail(5)), 不含当天
    # 盘中 volume 是部分量, 按 elapsed_minutes 折算到全天量级
    vol_ma5_prev = pl.col("_vol_ma5_prev_sum") / 5  # 前5日均量(不含当天)
    time_factor: float
    if elapsed_minutes and elapsed_minutes > 0:
        from app.market_clock import us_clock
        time_factor = float(us_clock.total_minutes) / elapsed_minutes  # 盘中折算: 部分量 → 全天量级
    else:
        time_factor = 1.0  # 盘后/无效时间: 不折算(此时 volume 已是全天量)
    df = df.with_columns([
        vol_ma5.alias("vol_ma5"),
        vol_ma10.alias("vol_ma10"),
        ((pl.col("volume") * time_factor) / vol_ma5_prev).alias("vol_ratio_5d"),
    ])

    # ---- 极值 60 日 (收盘价口径, 与全量 close.rolling_max/min(60) 一致) ----
    df = df.with_columns([
        pl.when(has_history_state)
          .then(pl.max_horizontal(pl.col("_high_59d"), pl.col("close")))
          .otherwise(None)
          .alias("high_60d"),
        pl.when(has_history_state)
          .then(pl.min_horizontal(pl.col("_low_59d"), pl.col("close")))
          .otherwise(None)
          .alias("low_60d"),
    ])

    # ---- 动量 (5d/10d/20d/30d/60d) ----
    df = df.with_columns([
        (pl.col("close") / pl.col("_close_5d_ago") - 1).alias("momentum_5d"),
        (pl.col("close") / pl.col("_close_10d_ago") - 1).alias("momentum_10d"),
        (pl.col("close") / pl.col("_close_20d_ago") - 1).alias("momentum_20d"),
        (pl.col("close") / pl.col("_close_30d_ago") - 1).alias("momentum_30d"),
        (pl.col("close") / pl.col("_close_60d_ago") - 1).alias("momentum_60d"),
    ])

    # ---- 年化波动率 20d (递推) ----
    # 用 Welford 简化: sum + sum_sq of 19 historical returns + today's return
    today_ret = pl.col("close") / pl.col("prev_close") - 1
    total_sum = pl.col("_vol_19d_pct_sum").fill_null(0.0) + today_ret
    total_sq_sum = pl.col("_vol_19d_pct_sq_sum").fill_null(0.0) + today_ret ** 2
    # 样本方差 (ddof=1), 与全量 _daily_pct.rolling_std(20) / 回测矩阵 ddof=1 同口径
    vol_var = (total_sq_sum - total_sum ** 2 / 20) / 19
    df = df.with_columns(
        pl.when(has_history_state & (vol_var > 0))
          .then(vol_var.sqrt() * (252 ** 0.5))
          .otherwise(None)
          .alias("annual_vol_20d"),
    )

    # ---- 窗口不满置空 ----
    # live_agg 的部分和 / 极值 / N 日前收盘用 tail(N) 取, 历史 K 线不足 N 根 (次新股) 时
    # 取到的是残缺窗口; 全量 rolling_*(N) / shift(N) 窗口不满为空, 按窗口内实际根数同口径置空。
    min_history_bars = {
        "ma5": 4, "ma10": 9, "ma20": 19, "ma30": 29, "ma60": 59,
        "vol_ma5": 4, "vol_ma10": 9, "vol_ratio_5d": 5,
        "boll_upper": 19, "boll_lower": 19, "high_60d": 59, "low_60d": 59,
        "momentum_5d": 5, "momentum_10d": 10,
        "momentum_20d": 20, "momentum_30d": 30, "momentum_60d": 60,
        "annual_vol_20d": 20,
    }
    if "_window_len" in df.columns:
        df = df.with_columns([
            pl.when(pl.col("_window_len") >= need).then(pl.col(column)).otherwise(None).alias(column)
            for column, need in min_history_bars.items()
            if column in df.columns
        ])

    # ---- 信号 (需要昨天的指标值判断交叉) ----
    if not prev_enriched.is_empty():
        sig_prev = prev_enriched.select(
            "symbol",
            pl.col("ma5").alias("_prev_ma5"),
            pl.col("ma10").alias("_prev_ma10"),
            pl.col("ma20").alias("_prev_ma20"),
            pl.col("ma60").alias("_prev_ma60"),
            pl.col("macd_dif").alias("_prev_dif"),
            pl.col("macd_dea").alias("_prev_dea"),
            pl.col("boll_upper").alias("_prev_boll_upper"),
            pl.col("boll_lower").alias("_prev_boll_lower"),
            pl.col("close").alias("_prev_close_enriched"),
        )
        df = df.join(sig_prev, on="symbol", how="left")

        df = df.with_columns([
            # MA 金叉/死叉
            ((pl.col("ma5") > pl.col("ma20")) & (pl.col("_prev_ma5") <= pl.col("_prev_ma20")))
                .alias("signal_ma_golden_5_20"),
            ((pl.col("ma5") < pl.col("ma20")) & (pl.col("_prev_ma5") >= pl.col("_prev_ma20")))
                .alias("signal_ma_dead_5_20"),
            ((pl.col("ma20") > pl.col("ma60")) & (pl.col("_prev_ma20") <= pl.col("_prev_ma60")))
                .alias("signal_ma_golden_20_60"),
            # MACD 金叉/死叉
            ((pl.col("macd_dif") > pl.col("macd_dea")) & (pl.col("_prev_dif") <= pl.col("_prev_dea")))
                .alias("signal_macd_golden"),
            ((pl.col("macd_dif") < pl.col("macd_dea")) & (pl.col("_prev_dif") >= pl.col("_prev_dea")))
                .alias("signal_macd_dead"),
            # MA20 突破/跌破
            ((pl.col("close") > pl.col("ma20")) & (pl.col("_prev_close_enriched") <= pl.col("_prev_ma20")))
                .alias("signal_ma20_breakout"),
            ((pl.col("close") < pl.col("ma20")) & (pl.col("_prev_close_enriched") >= pl.col("_prev_ma20")))
                .alias("signal_ma20_breakdown"),
            # MA5 突破/跌破
            ((pl.col("close") > pl.col("ma5")) & (pl.col("_prev_close_enriched") <= pl.col("_prev_ma5")))
                .alias("signal_ma5_breakout"),
            ((pl.col("close") < pl.col("ma5")) & (pl.col("_prev_close_enriched") >= pl.col("_prev_ma5")))
                .alias("signal_ma5_breakdown"),
            # MA10 突破/跌破
            ((pl.col("close") > pl.col("ma10")) & (pl.col("_prev_close_enriched") <= pl.col("_prev_ma10")))
                .alias("signal_ma10_breakout"),
            ((pl.col("close") < pl.col("ma10")) & (pl.col("_prev_close_enriched") >= pl.col("_prev_ma10")))
                .alias("signal_ma10_breakdown"),
            # BOLL 突破
            (pl.col("close") >= pl.col("boll_upper")).alias("signal_boll_breakout_upper"),
            (pl.col("close") <= pl.col("boll_lower")).alias("signal_boll_breakdown_lower"),
        ])

        df = df.drop([c for c in df.columns if c.startswith("_prev_")])

    # N日新高/新低 + 放量
    df = df.with_columns([
        (pl.col("close") >= pl.col("high_60d")).alias("signal_n_day_high"),
        (pl.col("close") <= pl.col("low_60d")).alias("signal_n_day_low"),
        (pl.col("vol_ratio_5d") >= 2.0).alias("signal_volume_surge"),
    ])

    # ---- 换手率 ----
    if instruments is not None and not instruments.is_empty():
        df = compute_turnover(df, instruments)

    # ---- 清理内部列 ----
    drop_cols = [
        "close_right", "high_right", "low_right", "_prev_close_raw",
        "_ma5_partial_sum", "_ma10_partial_sum", "_ma20_partial_sum",
        "_ma30_partial_sum", "_ma60_partial_sum",
        "_boll_partial_sum", "_boll_partial_sq_sum",
        "_high_59d", "_low_59d",
        "_close_5d_ago", "_close_10d_ago", "_close_20d_ago",
        "_close_30d_ago", "_close_60d_ago",
        "_vol_ma5_partial_sum", "_vol_ma10_partial_sum", "_vol_ma5_prev_sum",
        "_kdj_8d_low", "_kdj_8d_high",
        "_window_len",
        "_rsi_avg_gain_6", "_rsi_avg_loss_6",
        "_rsi_avg_gain_14", "_rsi_avg_loss_14",
        "_rsi_avg_gain_24", "_rsi_avg_loss_24",
        "_ema12", "_ema26",
        "_adj_factor",
        "_vol_19d_pct_sum", "_vol_19d_pct_sq_sum",
        "_has_history_state",
    ]
    df = df.drop([c for c in drop_cols if c in df.columns])

    # 扩展表数值列注入: 当日单日帧, 时序按当日分区对齐 + 快照最新值
    # (include_snapshot 仅此处为 True —— 单日帧不存在"回看历史"的未来函数问题)。
    # 帧缓存由 ext_factors 按分区/文件签名管理, 写入端变更自动失效。
    from app.factors import ext_factors
    df = ext_factors.attach_ext_columns(df, include_snapshot=True)

    # 自定义信号（日级实时路径同样注入, 但不支持日期偏移条件 → allow_shift=False）
    # 复用模块级缓存 _custom_signal_exprs_today: 增量热路径每秒级执行,
    # 不缓存则每轮 glob + 读所有 JSON + 重编译表达式。失效由 invalidate_custom_signals 统一管理。
    from app.strategy import custom_signals
    df = custom_signals.inject(df, _get_custom_signal_exprs_today())

    # 清理 NaN / Inf
    float_cols = [c for c in df.columns if df[c].dtype.is_float()]
    if float_cols:
        df = df.with_columns([
            pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite())
              .then(None)
              .otherwise(pl.col(c))
              .alias(c)
            for c in float_cols
        ])

    return df
