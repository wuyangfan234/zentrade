"""AI 个股分析服务 — 技术面 / 基本面 / 财务面 / 消息面 四维综合分析。

职责:
  组合一只股票的 K 线(含已算好的技术指标)+ 财务表 + 关键价位 →
  拼装客观技术分析系统提示词 → 流式调用 LLM → 逐 chunk 吐给前端。

与 financial_analyzer.py 的区别(刻意区分,非复用):
  - 角色:客观技术分析师(非 CFA 财务分析师)
  - 数据源:K 线 + 技术指标为主,财务表为辅(财务分析以财务表为主)
  - 输出框架:技术面→基本面→财务面→消息面(四维),落点是客观技术状态与风险提示
    (财务分析的落点是财务质量评级)。注意:本服务不输出买卖建议、操作指令。

不知道: HTTP、前端、配置持久化。
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import polars as pl

from app.indicators.levels import compute_levels, summarize_levels
from app.market_time import cn_today

logger = logging.getLogger(__name__)

# 注入最近多少根日 K(技术面分析样本)
_KLINE_WINDOW = 90
# 注入财务表的最近期数
_MAX_PERIODS = 4


# ================================================================
# 数据加载
# ================================================================

def _load_kline(repo, symbol: str) -> pl.DataFrame:
    """读取该标的最近 N 根日 K(已含技术指标 / 信号)。

    repo: KlineRepository;走内存缓存,性能可控。
    """
    end = cn_today()
    start = end - timedelta(days=_KLINE_WINDOW * 2)  # 多取一些保证交易日够
    # 按资产类型分流: ETF/指数走独立 enriched 存储 (无财务数据, 提示词已有兜底)
    df = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)
    if df.is_empty():
        return df
    return df.tail(_KLINE_WINDOW)


def _clean_rows(df: pl.DataFrame, keep_cols: list[str]) -> list[dict]:
    """把 DataFrame 转成 JSON 安全的 dict 列表(只保留关键列 + 清洗 NaN/Inf + date→字符串)。

    polars 的 date 列会变成 Python datetime.date,json.dumps 无法直接序列化,
    必须转成 ISO 字符串,否则 json.dumps 抛 TypeError 让整个流静默失败。
    """
    import datetime
    import math
    cols = [c for c in keep_cols if c in df.columns]
    sub = df.select(cols)
    rows = []
    for rec in sub.to_dicts():
        clean = {}
        for k, v in rec.items():
            if isinstance(v, float):
                clean[k] = None if not math.isfinite(v) else round(v, 4)
            elif isinstance(v, (datetime.date, datetime.datetime)):
                clean[k] = v.isoformat()
            else:
                clean[k] = v
        rows.append(clean)
    return rows


_FIN_TABLES = ("metrics", "income")


def _load_financials(data_dir: Path, symbol: str, *, today=None) -> dict[str, list[dict]]:
    """财务 (0.16.2): 读 data/financials/{metrics,income} (SEC EDGAR 主源 / Finnhub 备源, fundamentals_sync 落盘)。

    只取申报日 announce_date ≤ today 的最近 _MAX_PERIODS 期 (未申报的期不提前看到), 每期带报告期 period_end、
    申报日与来源 source。读不到 / 无该标的 → 返回 {} (提示词退回「暂无财务数据」)。
    """
    if today is None:
        from app.market_clock import us_clock
        today = us_clock.today()
    cutoff = today.isoformat()
    out: dict[str, list[dict]] = {}
    for table in _FIN_TABLES:
        path = Path(data_dir) / "financials" / table / "part.parquet"
        if not path.exists():
            continue
        try:
            df = pl.read_parquet(path).filter(
                (pl.col("symbol") == symbol) & pl.col("announce_date").is_not_null()
                & (pl.col("announce_date").cast(pl.Utf8).str.slice(0, 10) <= cutoff))
        except Exception as e:  # noqa: BLE001
            logger.warning("读取财务表 %s 失败 (跳过): %s", table, type(e).__name__)
            continue
        if df.is_empty():
            continue
        df = df.sort(["period_end", "announce_date"]).unique(subset=["period_end", "period_type"], keep="last",
                                                              maintain_order=True)
        df = df.sort("period_end").tail(_MAX_PERIODS).drop("symbol")
        rows = _clean_rows(df, df.columns)
        if rows:
            out[table] = rows
    return out


# ================================================================
# 系统提示词 (美股口径; P1-5 裁剪时被连带删除致线上 NameError, 9-30 按美股重写)
# 重要原则: 只描述"指标/价位处于什么客观状态", 不输出"应该怎么操作"。
# ================================================================

_SYSTEM_PROMPT = """你是一位长期研究美股的技术分析师,擅长从日 K、量价、关键价位的交叉验证中客观解读个股的技术状态。你的任务是:基于提供的数据,产出一份**客观、中立、不包含任何买卖或操作建议**的美股技术分析报告。

## 美股市场常识(写作时必须遵守)

- 常规交易时段为美东时间 09:30–16:00,连续交易、无午休;价格单位为美元(USD),成交量单位为股
- 美股**没有日内涨跌停制度**,只有个股波动暂停(LULD)与全市场熔断;不要使用"涨停/跌停/连板/封板"等说法
- 财报季、指引、并购公告、宏观数据(CPI/FOMC/非农)是常见的跳空来源;隔夜与盘前盘后可能大幅跳空
- 提供的日 K 为前复权价格;成交量为全市场合并口径

## 核心红线(务必遵守)

- **绝对不输出**"买入/卖出/加仓/减仓/观望/轻仓/重仓/止损/止盈/建议买入区间/操作建议"等任何交易指令或倾向性措辞
- 你的角色是**客观陈述**该股当前的技术状态、价位结构、量价特征与潜在风险,让读者自行判断
- 换成"一个中立财经记者能不能写出来"——能写就保留,不能写就删除

## 输出规范

用 **Markdown** 格式输出,严格遵循以下结构,不要输出 JSON 或代码块:

### 1. 🎯 一句话定调(1-2 句)
概括当前**技术状态**(如"价格在 50 日均线上方运行,均线多头排列,近期放量突破前高")。结尾用【当前状态:企稳 / 反弹 / 震荡 / 调整 / 走弱】客观描述,**不评价好坏、不下操作结论**。

### 2. 📈 技术面分析(核心维度)
只陈述客观事实,每条结论引用具体数值:
- **趋势**:均线排列、20/50 日均线方向、价格相对均线的位置
- **形态**:近期是否出现突破/破位/旗形/杯柄/双底/双顶等关键形态
- **指标**:MACD 金叉/死叉/背离、RSI 强弱、KDJ、布林通道位置、ATR 波动水平
- **量价**:放量上涨/缩量回调/量价背离,量比(当日量/5 日均量)的客观水平

### 3. 💰 关键价位(客观价位结构)
基于提供的关键价位数据逐档列出上方压力与下方支撑,标注来源(成交密集区/枢轴点/前高前低/布林带)与美元价位。**只列价位及其技术含义,不输出"建议买入区间""止损位""止盈位"等操作指令。**

### 4. 🏭 基本面(辅助)
若用户消息提供了「最新财务数据」(SEC EDGAR / Finnhub 同步的季报/年报指标与利润表): 只引用其中给出的数字, 每个数字写明报告期(period_end)与来源(source, 如 sec_edgar), 可描述营收/净利同比、毛利率、净利率、ROE、负债率、EPS 的趋势; 不推算估值、不补全缺失字段(为 null 的字段写"未提供")。
若用户消息提供了「财报一致预期」, 只陈述其中给出的共识数字、分析师家数与近 30 天修正方向(写明取数日期), 不推算估值。
若两者都未提供, 本节请输出:
> 📌 当前未取得该标的的财务数据,基本面维度暂不评估;以上技术面结论不依赖财务数据。
若标的为 ETF,改为说明"ETF 无上市公司财务报表,本维度不适用"。**绝对不要编造营收、EPS、市盈率等数字。**

### 5. 📰 消息面
- **若用户消息里提供了「近 7 日新闻」**:逐条只引用列表中真实存在的新闻(写明日期与来源),说明它与近期价量变化是否对得上(如新闻日前后是否放量/跳空);列表里的「事件类型/方向/重要度」是 AI 预判读,只作参考,请结合价量自行核对,对不上时直说。**不得引用列表之外的任何新闻或传闻。**
- **若未提供新闻**:仅基于价量异动做客观推断(如:放量跳空高开 → 可能存在财报或消息催化;放量长阴 → 可能存在未公开利空;价格长期贴着某一固定价位窄幅横盘且波动骤降 → 可能是已宣布的现金收购,股价被钉在收购价附近),明确标注"[推断]"。无明显异动则直说"近期价量平稳,无明显异动信号"。

### 6. ⚖️ 综合研判与风险提示
2-3 段,只做客观描述:当前所处技术阶段;距上方压力与下方支撑的空间不对称性;后续值得关注的量价信号与潜在跳空风险(财报、宏观数据)。**不附任何操作结论。**

## 分析准则

1. 技术面优先,数据说话,每个判断引用具体数值,禁止空泛套话
2. 客观中立,数据不支持时直言无法判断
3. 价位精确到美元,基于提供的关键价位数据陈述
4. 总字数 1000-1800 字,重在信息密度

## 重要免责
报告末尾附一行:"> ⚠️ 本内容由 AI 基于公开行情数据生成,仅客观陈述技术状态,不构成任何投资建议或买卖指令。"

现在请基于下方数据进行分析。"""



def _ledger_positions(data_dir, symbol: str) -> list[dict]:
    """ZenTrade 唯一纸面账户 (append-only 台账) 中该标的的持仓; 无持仓返回 []。只读重放, 不写物化。"""
    from pathlib import Path

    from app.ledger.core import Account, Ledger
    ledger_dir = Path(data_dir) / "ledger"
    if not (ledger_dir / "account.json").exists() and not (ledger_dir / "events.jsonl").exists():
        return []
    led = Ledger(data_dir=ledger_dir, read_only=True)
    acct = Account.from_file(ledger_dir / "account.json") if (ledger_dir / "account.json").exists() else Account()
    led.replay(acct, persist=False)
    pos = led.position(symbol)
    if pos is None or pos.qty == 0:
        return []
    row = {"account_id": "ZenTrade 纸面账户", "qty": pos.qty, "avg_cost": round(pos.avg_cost, 4)}
    if pos.qty < 0:
        row.update(qty=-pos.qty, side="short")  # 空头: 股数取正 + 标方向 (多头保持原结构)
    return [row]


def _paper_position_part(positions: list[dict], close: float | None, raw_close: float | None) -> str:
    """虚拟持仓上下文 (V3): 模拟盘持有该标的时注入提示词, 让 AI 结合仓位给建议。

    positions 为多账户列表 [{account_id, qty, avg_cost}, ...] (V2 账户隔离),
    只收 qty>0 的账户, 逐账户一行; 全空则返回空串 (不注入"空仓"之类误导性表述)。
    浮盈用 raw 口径: 持仓 avg_cost 是不复权 raw 成本, 与前复权 close 混算会在
    除权后产生虚假盈亏, 故优先用 raw_close (缺列时退化用 close 并接受口径误差)。
    """
    lines: list[str] = []
    for item in positions or []:
        qty = item.get("qty") or 0
        avg_cost = item.get("avg_cost") or 0
        if qty <= 0 or avg_cost <= 0:
            continue
        short = item.get("side") == "short"
        kind = "空头持仓" if short else "持仓"
        cost_label = "卖空均价" if short else "平均成本"
        line = f"账户 {item.get('account_id') or 'default'}: {kind} {qty} 股, {cost_label} ${avg_cost:.3f} (不复权)"
        ref = raw_close if (raw_close is not None and raw_close > 0) else close
        if ref is not None and ref > 0:
            pnl = (avg_cost - ref) / avg_cost if short else (ref / avg_cost - 1)
            line += f", 现价 ${ref:.2f}, 浮动盈亏 {pnl * 100:+.2f}%"
        lines.append(line)
    if not lines:
        return ""
    return (
        "\n以下是该标的在模拟盘 (虚拟账户) 的当前持仓:\n"
        + "\n".join(lines)
        + "\n请在结论中结合该虚拟持仓状态给出持有/减仓/加仓的可执行参考,"
        "并明确这是基于模拟盘虚拟持仓的建议。\n"
    )


_NEWS_MAX = 15


def _pct(x) -> str:
    return f"{x * 100:.1f}%" if isinstance(x, (int, float)) else "—"


def _paid_part(paid: dict) -> str:
    """QVeris 已缓存数据 (财报一致预期 / 期权摘要 / 电话会要点) → 紧凑文本。只用缓存, 由调用方保证不收费。"""
    lines: list[str] = []
    est = (paid.get("estimates") or {}).get("estimates") or {}
    nxt = est.get("next")
    if nxt and nxt.get("eps_avg") is not None:
        rev = nxt.get("rev_avg")
        lines.append(
            f"- 财报一致预期（{(paid['estimates'].get('fetched_at') or '')[:10]} 取数）: 期末 {nxt.get('date')} 的财季 EPS 共识 "
            f"{nxt['eps_avg']:.4g}（区间 {nxt.get('eps_low')}–{nxt.get('eps_high')}，{int(nxt.get('eps_analysts') or 0)} 家）"
            + (f"，营收共识 {rev / 1e8:.2f} 亿美元" if isinstance(rev, (int, float)) else "")
            + f"；近 30 天上调 {int(nxt.get('eps_up_30d') or 0)} 家、下调 {int(nxt.get('eps_down_30d') or 0)} 家")
    opt = (paid.get("options") or {}).get("analysis") or {}
    if opt.get("ok"):
        term = [t for t in opt.get("term") or [] if t.get("atm_iv") is not None][:4]
        pcr = (opt.get("pcr") or {}).get("all") or {}
        skew = opt.get("skew") or {}
        walls = opt.get("oi_walls") or {}
        seg = [f"- 期权（数据日 {opt.get('as_of')}）: ATM 隐含波动率 " + "、".join(
            f"{t['dte']}天 {_pct(t['atm_iv'])}（预期波动 ±{_pct(t['expected_move_pct'])}）" for t in term)]
        if pcr.get("volume") is not None:
            seg.append(f"认沽/认购比 成交量 {pcr['volume']}、持仓量 {pcr.get('open_interest')}")
        if skew.get("risk_reversal") is not None:
            seg.append(f"25-delta 看跌减看涨 IV 差 {skew['risk_reversal'] * 100:+.1f} 个百分点")
        if walls.get("calls_above"):
            seg.append("上方看涨持仓集中 " + "/".join(str(w["strike"]) for w in walls["calls_above"][:2]))
        if walls.get("puts_below"):
            seg.append("下方看跌持仓集中 " + "/".join(str(w["strike"]) for w in walls["puts_below"][:2]))
        if opt.get("earnings_move"):
            em = opt["earnings_move"]
            seg.append(f"财报日 {em['earnings_date']} 后首个到期的预期波动 ±{_pct(em['expected_move_pct'])}")
        lines.append("；".join(seg))
    dig = (paid.get("transcript") or {}).get("digest") or {}
    if dig.get("points"):
        lines.append(f"- 最近一次财报电话会（{paid['transcript'].get('quarter')}）要点: " + "；".join(dig["points"][:6]))
    if not lines:
        return ""
    return ("以下为付费数据源缓存（取数时点见括号; 期权与预期是市场定价/分析师观点, 不是事实结论, 请与价量交叉核对, "
            "引用时写明来源; 不得据此给出买卖指令）:\n" + "\n".join(lines))


def _news_part(news: list[dict]) -> str:
    """近 7 日新闻 (news_store.view 视图, 已是中文优先) → 紧凑列表; 主角新闻优先, 最多 15 条。"""
    if not news:
        return ""
    from app.services.news_store import is_main
    main = [v for v in news if is_main(v)]
    rest = [v for v in news if not is_main(v)]
    picked = (main + rest)[:_NEWS_MAX]
    lines = [f"近 7 日新闻(共 {len(news)} 条, 列出 {len(picked)} 条; 中文为机器翻译, 事件类型/方向/重要度为 AI 预判读、未经验证):"]
    for v in picked:
        day = str(v.get("published_at") or "")[:10]
        tag = ""
        if v.get("tagged"):
            dirn = {"bullish": "偏利好", "bearish": "偏利空", "neutral": "中性"}.get(v.get("direction") or "", "")
            imp = {"high": "重要度高", "medium": "重要度中", "low": "重要度低"}.get(v.get("importance") or "", "")
            tag = f" [{v.get('event_label') or '其他'}/{dirn}/{imp}{'/旧闻重炒' if v.get('rehash') else ''}]"
        src = "、".join(v.get("sources") or [])
        lines.append(f"- {day} {v.get('headline')}{tag}(来源: {src})")
        if v.get("summary"):
            lines.append(f"  摘要: {v['summary']}")
    return "\n".join(lines)


def _build_user_prompt(
    kline_tail: list[dict],
    fins: dict[str, list[dict]],
    levels: dict[str, list[dict]],
    close: float | None,
    symbol: str,
    focus: str,
    asset_type: str = "stock",
    paper_positions: list[dict] | None = None,
    raw_close: float | None = None,
    news: list[dict] | None = None,
    paid: dict | None = None,
) -> str:
    """构建用户消息:标的 + 价位摘要 + 技术指标 JSON + 财务摘要 + 近 7 日新闻 + 关注点 (+ 虚拟持仓)。

    asset_type 用于区分无财务数据时的文案:指数/ETF 无财务是常态,不走 Free 文案。
    """
    parts: list[str] = [
        f"标的标准代码: {symbol}",
        f"关键价位概览: {summarize_levels(levels, close)}",
        "",
        "以下是该标的最近日 K 数据(JSON,含 OHLCV 与已计算的技术指标。"
        f"最近 {_KLINE_WINDOW} 个交易日,升序):",
        "```json",
        json.dumps(kline_tail, ensure_ascii=False),
        "```",
    ]

    has_fin = any(fins.values())
    if has_fin:
        parts.extend([
            "",
            "以下是该标的最新财务数据(JSON, metrics=核心指标, income=利润表; 每期含报告期 period_end、"
            "申报日 announce_date、来源 source; 金额单位为美元, 比率为小数。引用时写明报告期与来源):",
            "```json",
            json.dumps(fins, ensure_ascii=False),
            "```",
        ])
    elif asset_type == "index":
        parts.extend([
            "",
            "(该标的为指数: 无财务与股本数据。请按系统提示词第 4 节的说明处理基本面维度,"
            "不要编造数据; 消息面维度基于价量异动推断即可。)",
        ])
    elif asset_type == "etf":
        parts.extend([
            "",
            "(该标的为 ETF: 无上市公司财务报表。请按系统提示词第 4 节的说明,"
            "在基本面维度明确这是 ETF 常态, 不要编造公司财务数字;"
            "消息面维度基于价量异动推断即可。)",
        ])
    else:
        parts.extend([
            "",
            "(该标的暂无财务数据: 本地财务同步 (SEC EDGAR) 尚未覆盖该标的或无已申报期。"
            "请按系统提示词第 4 节的说明处理基本面维度, 不要编造数据。)",
        ])

    news_part = _news_part(news or [])
    if news_part:
        parts.extend(["", news_part])
    paid_part = _paid_part(paid or {})
    if paid_part:
        parts.extend(["", paid_part])

    from app.services.ai_provider import build_focus_instruction
    focus_instruction = build_focus_instruction(focus, report_name="个股分析报告")
    if focus_instruction:
        parts.extend(["", focus_instruction])
    paper_part = _paper_position_part(paper_positions or [], close, raw_close)
    if paper_part:
        parts.extend(["", paper_part])
    return "\n".join(parts)


# ================================================================
# 关键列筛选(控制上下文体积)
# ================================================================

_KLINE_KEEP_COLS = [
    "date", "open", "high", "low", "close", "volume", "change_pct",
    "ma5", "ma10", "ma20", "ma60",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "rsi_6", "rsi_14", "rsi_24",
    "boll_upper", "boll_mid", "boll_lower",
    "atr_14", "vol_ratio_5d",
    # 信号类(布尔)——只挑对消息面推断有用的几个
    "signal_macd_golden",
    "signal_macd_death", "signal_ma_golden_5_20", "signal_volume_surge",
    "signal_boll_breakout_upper", "signal_boll_breakout_lower",
]


# ================================================================
# 流式分析入口
# ================================================================

async def analyze_stock_stream(
    repo,
    data_dir: Path,
    symbol: str,
    focus: str = "",
) -> AsyncIterator[str]:
    """流式个股分析:yield 出每个 NDJSON 事件。

    协议(与 financial_analyzer 一致,前端解析无差异):
      {"type":"meta","symbol","summary","levels"}  数据 + 价位摘要
      {"type":"delta","content":"..."}             逐 chunk 文本
      {"type":"error","message":"..."}
      {"type":"done"}
    """
    # 1. 加载 K 线
    df = _load_kline(repo, symbol)
    if df.is_empty():
        yield json.dumps({
            "type": "error",
            "message": f"标的 {symbol} 暂无日 K 数据,请先同步",
        }, ensure_ascii=False)
        return

    # 2. 价位计算(基于 K 线)
    levels = compute_levels(df)
    close = float(df.tail(1)["close"][0]) if "close" in df.columns else None

    # 3. 财务(辅助)。ETF/指数无公司报表, 跳过扫描, 由提示词走对应常态文案。
    asset_type = repo.resolve_asset_type(symbol)
    fins = {} if asset_type in {"etf", "index"} else _load_financials(data_dir, symbol)

    # 4. meta
    yield json.dumps({
        "type": "meta",
        "symbol": symbol,
        "summary": summarize_levels(levels, close),
        "levels": levels,
        "close": close,
    }, ensure_ascii=False)

    # 5+6. 构建提示词 + 流式调用 LLM(整体 try-except,任何异常都 yield error,避免前端卡死)
    try:
        from app.services.ai_provider import stream_ai_text

        # 虚拟持仓上下文 (V3): 逐账户 (V2 多账户) 收集持有该标的的持仓注入
        # (任何异常静默跳过, 不影响分析)。
        try:
            paper_positions = _ledger_positions(data_dir, symbol)
        except Exception as e:  # noqa: BLE001
            paper_positions = []
            logger.warning("读取纸面台账持仓失败 (跳过注入): %s", e)
        # 浮盈口径用 raw: 持仓成本不复权, 与前复权 close 混算会在除权后失真
        raw_close = float(df.tail(1)["raw_close"][0]) if "raw_close" in df.columns else None

        kline_tail = _clean_rows(df, _KLINE_KEEP_COLS)
        news: list[dict] = []
        if asset_type == "stock":
            try:
                from app.services import news_store
                news = news_store.for_symbol(data_dir, symbol, days=7, limit=40)
            except Exception as e:  # noqa: BLE001
                logger.warning("读取个股新闻失败 (跳过注入): %s", type(e).__name__)
        paid: dict = {}
        if asset_type == "stock":
            try:
                from app.services import qveris_data
                est = qveris_data.estimates(data_dir, symbol, refresh=False)
                if est.get("available"):
                    paid["estimates"] = est
                opt = qveris_data.options_summary(data_dir, repo, symbol, refresh=False)
                if opt.get("available"):
                    paid["options"] = opt
                from app.market_clock import us_clock as _uc
                tr = qveris_data.transcript(data_dir, symbol, qveris_data.latest_quarter(_uc.today()), refresh=False)
                if tr.get("digest"):
                    paid["transcript"] = tr
            except Exception as e:  # noqa: BLE001
                logger.warning("读取 QVeris 缓存失败 (跳过注入): %s", type(e).__name__)
        user_prompt = _build_user_prompt(kline_tail, fins, levels, close, symbol, focus,
                                         asset_type=asset_type, paper_positions=paper_positions,
                                         raw_close=raw_close, news=news, paid=paid)
        got_content = False
        async for delta in stream_ai_text(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
            # 不限制输出: 推理模型(deepseek reasoner 系)思考 token 计入 max_tokens
            # 预算, 固定上限会把正文挤光(实测 4500 全被推理吃掉 → 正文 0 字)。
            max_tokens=None,
            prefer_final_answer=True,
            task="stock_analysis",
        ):
            got_content = True
            yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)

    except Exception as e:
        logger.exception("AI stock analysis failed for %s: %s", symbol, e)
        yield json.dumps({"type": "error", "message": f"AI 分析失败: {e}"}, ensure_ascii=False)
        return

    if not got_content:
        # 流正常结束但一个正文块都没有(典型: 输出上限被思考吃光后静默截断)
        logger.warning("AI stock analysis ended with empty content for %s", symbol)
        yield json.dumps({"type": "error", "message": "AI 未返回正文(输出被截断), 请重试"}, ensure_ascii=False)
        return
    yield json.dumps({"type": "done"}, ensure_ascii=False)
