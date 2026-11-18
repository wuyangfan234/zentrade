# 手动策略开发指南

本文讲**手写一个策略文件**的完整流程：写到哪、怎么写、怎么保存、怎么验证，以及它到底能走到哪一步。

- 适用代码：本仓库 `main`（2026-11-18 核对，版本 0.57.0）。
- 只想查「必须 / 禁止」的规则清单，看 [策略语法标准](./strategy-standard.md)；本文是带示例的操作手册。
- 策略运行规范（字段、列名、信号列的完整清单）另见 [`backend/app/strategy/prompts/strategy-guide.md`](../backend/app/strategy/prompts/strategy-guide.md)；那份是给 AI 和人工共用的**文件规范**，本文是**人的操作手册**。
- 本文不涉及改系统源码。策略只能是一个 `.py` 文件，放进用户策略目录，由引擎自动发现。

## 0. 先分清四件事

同一个策略文件，在不同页面代表不同的东西，门槛也不同：

| 你要的 | 在哪用 | 吃什么数据 | 有没有“许可”概念 |
| :--- | :--- | :--- | :--- |
| 今天哪些票符合条件 | 选股页 · 全市场扫描 | 最新交易日 enriched 日线 | 没有，纯筛选 |
| 这套规则历史上赚不赚钱 | 回测页 · 策略回测 | 历史日线 / 分钟线 | 没有，纯研究 |
| 盘中触发提醒 | 监控中心 · 策略触发器 | 实时行情 | 没有，只通知 |
| 出机会卡、进纸面下单簿 | 机会页 · 3+3 规则 | 自选信号快照 + 方向门 | **有**，见 §7 |

一句硬话：**策略跑通、回测好看，都不等于允许开仓。** 能不能开仓由机会页的 3+3、方向门和规则集许可决定。

## 1. 最小可用策略（5 分钟）

### 1.1 文件放哪

| 来源 | 目录 | ID 约定 |
| :--- | :--- | :--- |
| 内置策略（只由项目维护者改） | `backend/app/strategy/builtin/` | 无前缀 |
| 手写 / 自定义 | `data/strategies/custom/` | `custom_` 开头（经保存接口新建时强制） |
| AI 生成 | `data/strategies/ai/` | `ai_` 开头（强制） |
| 叠加策略（声明式，无业务代码） | `data/strategies/composite/` | `composite_` 开头（强制） |

开发模式下 `data/` 在项目根目录；生产容器里是 `/app/data`。

引擎只扫目录**顶层**的 `*.py`，跳过 `_` 和 `.` 开头的文件；`_xxx.py` 会被当作可引用的辅助模块，但仍要过同一套安全校验。`.iterations/` 这类子目录不会被加载。

### 1.2 最小模板

下面这个策略已经在本地引擎里装载并跑过合成数据（`filter` 返回 `pl.Expr`）：

```python
"""均线回踩：强势股缩量回踩 10 日线，收盘站回。"""
import polars as pl

META = {
    "id": "custom_pullback_ma10",
    "name": "强势回踩 MA10",
    "description": "20 日动量为正、收盘在 MA20 上方，当日回踩 MA10 后收在 MA10 上方，且缩量。",
    "tags": ["回踩", "趋势", "缩量"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {"price_min": 5, "price_max": None, "amount_min": 2e7},
    "params": [
        {"id": "min_momentum", "label": "20 日最小涨幅", "type": "float",
         "default": 0.08, "min": 0.0, "max": 0.5, "step": 0.01},
        {"id": "touch_tolerance", "label": "回踩容差", "type": "float",
         "default": 0.01, "min": 0.0, "max": 0.05, "step": 0.005},
        {"id": "max_volume_ratio", "label": "最大量比", "type": "float",
         "default": 0.8, "min": 0.2, "max": 2.0, "step": 0.1},
    ],
    "scoring": {"momentum_20d": 0.5, "vol_ratio_5d": 0.3, "ma20_bias": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "polars_expr"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 7

RULES = """
1. 20 日动量不低于参数阈值，且收盘价在 MA20 上方。
2. 当日最低价回踩到 MA10 附近（容差内），收盘仍站在 MA10 上方。
3. 当日成交量不高于 5 日均量的设定倍数，回踩是缩量。
"""


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    min_momentum = float(params.get("min_momentum", 0.08))
    touch = float(params.get("touch_tolerance", 0.01))
    max_vol = float(params.get("max_volume_ratio", 0.8))
    return (
        (pl.col("momentum_20d") >= min_momentum)
        & (pl.col("close") > pl.col("ma20"))
        & (pl.col("low") <= pl.col("ma10") * (1 + touch))
        & (pl.col("close") >= pl.col("ma10"))
        & (pl.col("volume") <= pl.col("vol_ma5") * max_vol)
    ).fill_null(False)
```

### 1.3 保存

推荐走页面：策略页 → 新建策略 → 「自定义编写」页签。左栏是模板库（6 个，五种写法都有），点一个就把完整源码放进编辑器；右栏在停笔约 1 秒后自动做分项校验（安全、META、入口、字段），未通过的会写明第几行并在编辑器里标出，点「标准 §N」可在弹窗内查对应条目。四项全过后保存（AI 页签生成的策略保存后是草稿，需在策略池「AI」页签发布）。保存时后端会：

1. AST 安全校验（import 白名单、危险调用、dunder 逃逸）；
2. 提取 `META`（必须是纯字面量，`ast.literal_eval` 可解析）；
3. 原子写文件；
4. 热重载全部策略，并核对这份文件确实加载成了那个 ID；
5. 失败则恢复上一版文件，并返回 `策略保存失败: ...`。

先校验再保存，可以少一次回滚：

```text
POST /api/strategies/code/validate   → {"valid": true, "meta": {...}}
POST /api/strategies/code/save       → {"ok": true, "path": "..."}
```

`code/save` 的请求体：

```json
{
  "code": "<策略文件全文>",
  "strategy_id": "custom_pullback_ma10",
  "target_source": "custom",
  "mode": "create",
  "name": "强势回踩 MA10",
  "description": "20 日动量为正、缩量回踩 MA10。"
}
```

直接用文件落盘也可以：写到 `data/strategies/custom/`，然后 `POST /api/strategies/reload`（或重启进程）。但这条路没有“失败自动回滚”，写错一个文件会让整次热重载失败，所有策略停留在上一版；优先用页面/接口保存。

### 1.4 确认装载

- 策略页能看到这一行（`GET /api/strategies`）；
- `GET /api/strategies/{id}` 的 `execution_backend`、`asset_types`、`timeframes` 与预期一致；
- 运行一次全市场扫描，看命中数与分布是否合理（0 命中不一定是 bug，先查门槛和数据窗口）。

## 2. 文件契约

### 2.1 META 字段

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `id` | 建议 | 唯一；缺省用文件名。重复 ID 会让保存失败（加载到非预期文件） |
| `name` / `description` | 建议 | 显示名与一句话说明 |
| `tags` | 否 | 分类标签 |
| `asset_types` | 内置必填 | `["stock"]` 或 `["stock", "etf"]`；缺省 `["stock"]`。内置策略不声明会直接拒绝加载 |
| `timeframes` | 否 | 缺省 `["1d"]`；分钟策略必须且只能是 `["1m"]`。机会板/规则目录只认 `1d` |
| `basic_filter` | 否 | 与默认门槛合并（见 §2.4） |
| `params` | 否 | 用户可调参数；会被归一化成 `list[dict]` |
| `scoring` | 否 | 排序评分权重；只能用真实数值列或 `ma20_bias` |
| `order_by` / `descending` / `limit` | 否 | 默认 `score` / `True` / `100` |
| `research_only` | 否 | `true` = 草稿：不进公开列表、不可运行、不进规则目录、不参加候选验收 |
| `children` | composite 必填 | 子策略列表（`strategy_id` + `weight`） |
| `daily_history_bars` | 分钟策略可选 | 0–250；大于 0 时 `filter_minute_history` 必须接受 `daily` 关键字 |
| `lookback_days` | 否 | 没有模块级 `LOOKBACK_DAYS` 时的兜底 |

`META` 在经保存接口写入时必须是纯字面量字典：不能引用变量、不能调用函数、不能写 `META = dict(...)`。保存接口用 AST 读取，不执行代码。直接落盘时引擎会执行整个模块（仍过安全校验），但仍建议保持纯字面量，便于校验、迁移和让 AI 读懂。

### 2.2 顶层常量

| 常量 | 作用 |
| :--- | :--- |
| `EXECUTION_BACKEND` | 执行后端，见 §2.3；不写时按是否声明 `filter_history` 推断 |
| `filter(df, params)` | 单日截面过滤，返回 `pl.Expr` |
| `filter_history(df, params)` | 历史窗口过滤，返回 `pl.DataFrame` |
| `filter_minute_history(df, params, *, daily=None)` | 分钟形态过滤，返回 `pl.DataFrame` |
| `MATRIX_STRATEGY` | 矩阵策略对象，必须实现 `app.backtest.matrix.MatrixStrategy` |
| `ENTRY_SIGNALS` / `EXIT_SIGNALS` | 信号列名列表，回测/监控用；要用 `.fill_null(False)` 的信号列 |
| `STOP_LOSS` | 负数比例（`-0.06` = 亏 6% 止损）；策略回测与决策台卡读取 |
| `TAKE_PROFIT` | 可选，(0, 5] 的有限比例；越界按未设置处理 |
| `TRAILING_STOP` / `TRAILING_TAKE_PROFIT_ACTIVATE` / `TRAILING_TAKE_PROFIT_DRAWDOWN` | 可选，引擎读取并在策略详情暴露；纸面出场以卡和交易环为准 |
| `MAX_HOLD_DAYS` | 策略回测的最长持有交易日；机会板的紧卡固定 5 日，不受它影响 |
| `LOOKBACK_DAYS` | `filter_history` 需要加载的历史交易日数；不声明时引擎会尝试读 `params` 里的 `lookback_days` 默认值，再退回 1 |
| `REQUIRED_FEATURES` | 静态声明用到的公开字段（如 `{"ma20", "momentum_20d"}`）；不声明会回退全量特征计算并告警 |
| `RULES` | 人类可读规则说明，不参与执行 |

### 2.3 五种执行后端

引擎只接受这五种，写错会直接拒绝加载：

| 后端 | 必须声明 | 禁止 | 适用 |
| :--- | :--- | :--- | :--- |
| `polars_expr` | 只声明 `filter`，返回 `pl.Expr` | `filter_history` | 单日指标比较，最常用 |
| `python_history_legacy` | 只声明 `filter_history`，返回 `DataFrame` | `filter` | 多日窗口、事件后第 N 天、前高前低 |
| `matrix_native` | `MATRIX_STRATEGY` 实现 `MatrixStrategy` | `filter` / `filter_history` | 全市场矩阵、截面排序；只用 NumPy 与 `app.backtest.matrix` |
| `composite` | `META["children"]` 引用其他策略 | `filter` / `filter_history` / `MATRIX_STRATEGY` | 叠加策略；不能嵌套叠加；子策略的 `asset_types` / `timeframes` 必须覆盖父策略 |
| `minute_filter` | 只声明 `filter_minute_history`，`timeframes == ["1m"]` | 日线 `filter` / `filter_history` | 盘中分钟形态；数据来自当日分钟分区 |

矩阵策略的评分必须全是有限值：预热区和 `valid_shift` 之前的格子填 0，不要留 `NaN`。写法见 §3.5。

内置策略全部是矩阵写法。全市场扫描和回测都直接在「时间 × 股票」的数组上算，速度最快；单日比较用 `polars_expr` 更省事，两种都受支持。

### 2.4 params / basic_filter / scoring

**params**：只放用户可能调的阈值，公式常数不必参数化。

```python
{"id": "min_momentum", "label": "20 日最小涨幅", "type": "float",
 "default": 0.08, "min": 0.0, "max": 0.5, "step": 0.01}
```

- `type` 支持 `float` / `int` / `bool` / `select`（带 `options`）/ `date`（`default` 写 `"YYYY-MM-DD"` 字符串）。
- 读取统一用 `params.get("id", 默认值)`；用户在前端改参数后经覆盖配置传入，不需要改代码。
- `date` 类型从 JSON 传来是字符串，和 `date` 列比较前必须先 `date.fromisoformat()`。

**basic_filter**：默认门槛是 `price_min=5`、`amount_min=2e7`（2000 万美元）、`price_max=None`；`market_cap_*` / `float_cap_*` / `turnover_*` / `exclude_new_days` 没有数据源，不要依赖。覆盖优先级：默认值 < 模块级 `BASIC_FILTER` < `META["basic_filter"]`。

**scoring**：权重只能用真实数值字段或受控虚拟字段 `ma20_bias`，不能写信号列或条件名；权重必须是非负有限数，**合计必须等于 1.0**，否则校验不通过；不需要评分就写空字典 `{}`。股本类字段（市值、换手率）和已停用的 `deviate_*` 会被保存接口拒绝。

### 2.5 多文件与辅助模块

- 一个策略 = 一个 `.py`；**不要**为了“拆文件”去改项目源码。
- 真要复用函数，放同目录 `_helper.py`，策略里 `import _helper`；引擎会把 `_*.py` 作为依赖加载，并同样跑安全校验。
- 辅助模块允许 `collections.abc`、`types`、`typing` 和 `vars()`（仅依赖文件有这份放宽），策略文件本身没有这份放宽。

## 3. 写表达式

### 3.1 filter：单日截面

- 入参 `df` 是已经过基础门槛的 enriched 数据；`params` 是合并后的参数。
- 返回 `pl.Expr`，用 `&` `|` `~` 组合；布尔表达式末尾统一 `.fill_null(False)`。
- 不要在这里取“最近 N 天”——那是 `filter_history` 的事。

### 3.2 filter_history：历史窗口

必须遵守三条：

1. **返回所有匹配行，不要只取最新一天**。选股引擎自己取最新日，回测引擎要全区间命中；只留最新日会让回测只剩一天有信号。
2. 声明 `LOOKBACK_DAYS`（或 `params` 里的 `lookback_days`），否则引擎只加载 1 天历史，运行时报 `requires history data`。
3. 先 `sort(["symbol", "date"])`，再用 `shift(n).over("symbol")`；优先向量化，不要在 Python 里逐股票循环。

### 3.3 历史窗口示例

“20 日新高后 5 日内缩量回踩 MA10”，已在本地引擎装载并用合成数据跑通：

```python
"""突破后回踩：20 日新高后 5 日内缩量回踩 MA10。"""
import polars as pl

META = {
    "id": "custom_breakout_pullback",
    "name": "突破后回踩 MA10",
    "description": "过去 5 个交易日内收盘创 20 日新高，随后缩量回踩 MA10 并收盘站回。",
    "tags": ["突破", "回踩", "缩量"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {"price_min": 5, "amount_min": 2e7},
    "params": [
        {"id": "breakout_window", "label": "突破后有效天数", "type": "int",
         "default": 5, "min": 1, "max": 20},
        {"id": "max_volume_ratio", "label": "最大量比", "type": "float",
         "default": 0.8, "min": 0.2, "max": 2.0, "step": 0.1},
    ],
    "scoring": {"momentum_20d": 0.6, "vol_ratio_5d": 0.4},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

LOOKBACK_DAYS = 30
EXECUTION_BACKEND = "python_history_legacy"
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 7

RULES = """
1. 前一交易日收盘价创 20 日新高（用截至前一日的 20 日最高收盘比较）。
2. 该次突破发生在最近 N 个交易日内。
3. 当日最低价回踩 MA10，收盘仍在 MA10 上方，成交量低于前一日为止的 5 日均量。
"""


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    if df.is_empty() or "date" not in df.columns:
        return df
    window = int(params.get("breakout_window", 5))
    max_vol = float(params.get("max_volume_ratio", 0.8))
    hist = (
        df.sort(["symbol", "date"])
        .with_columns([
            pl.col("close").rolling_max(20, min_samples=20).shift(1).over("symbol").alias("_hh20_prev"),
            pl.col("volume").rolling_mean(5, min_samples=5).shift(1).over("symbol").alias("_volma5_prev"),
        ])
        .with_columns(
            (pl.col("close").shift(1).over("symbol") >= pl.col("_hh20_prev")).alias("_breakout_prev")
        )
        .with_columns(
            pl.col("_breakout_prev").cast(pl.Int8)
            .rolling_max(window, min_samples=1).shift(1).over("symbol").alias("_broke_recently")
        )
    )
    return hist.filter(
        (pl.col("_broke_recently") == 1)
        & (pl.col("low") <= pl.col("ma10") * 1.01)
        & (pl.col("close") >= pl.col("ma10"))
        & (pl.col("volume") <= pl.col("_volma5_prev") * max_vol)
    )
```

要点：所有“创新高 / 5 日均量”都先 `shift(1)` 再参与当日判断，避免把当日收盘算进信号条件（未来函数）。

### 3.5 矩阵写法（matrix_native）

矩阵写法不写 `filter`。你提供一个对象 `MATRIX_STRATEGY`，引擎把全市场行情整理成二维数组交给它：**行是时间，列是股票**，`market.close[t, i]` 就是第 `i` 只股票在第 `t` 根 K 线的收盘价。停牌、未上市的格子是 `NaN`。

对象要有三个方法：

| 方法 | 作用 |
| :--- | :--- |
| `required_fields()` | 返回用到的行情字段名，如 `frozenset({"close", "volume"})` |
| `required_warmup_bars(params)` | 出第一个可信信号前需要多少根历史。20 日窗口就至少返回 20 多 |
| `compute_signals(market, params)` | 算出买入、卖出、分数三个数组，用 `make_signal_matrix` 打包返回 |

下面这个策略已在本地引擎装载，并用合成数据跑通（放量突破当天命中，分数全部有限）：

```python
"""矩阵写法：放量突破 20 日最高收盘。"""
import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
    valid_shift,
)

META = {
    "id": "custom_matrix_breakout",
    "name": "放量突破 20 日高点",
    "description": "收盘价高于此前 20 个交易日的最高收盘，且成交量不低于 5 日均量的设定倍数。",
    "tags": ["突破", "放量", "矩阵"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {"price_min": 5, "amount_min": 2e7},
    "params": [
        {"id": "window", "label": "突破窗口", "type": "int",
         "default": 20, "min": 5, "max": 120},
        {"id": "min_volume_ratio", "label": "最小量比", "type": "float",
         "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_custom_matrix_breakout"]
EXIT_SIGNALS = []
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 7

RULES = """
1. 收盘价高于截至前一交易日的 N 日最高收盘。
2. 当日成交量不低于 5 日均量的设定倍数。
3. 按 20 日动量从高到低排序。
"""


class CustomMatrixBreakout:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        return int(params.get("window", 20)) + 5

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        window = int(params.get("window", 20))
        min_ratio = float(params.get("min_volume_ratio", 1.5))
        close = market.close
        valid = np.isfinite(close)

        # 先算「截至当日」的 N 日最高收盘, 再后移一根: 当日收盘不参与自己的突破线
        prior_high = valid_shift(valid_rolling_max(close, valid, window), 1, valid)
        vol_ratio = matrix_feature(market, "vol_ratio_5d")
        momentum = matrix_feature(market, "momentum_20d")

        entry = (
            np.isfinite(prior_high) & (close > prior_high)
            & np.isfinite(vol_ratio) & (vol_ratio >= min_ratio)
        )
        # 分数必须全部是有限值: 预热区和缺数据的格子填 0
        score = np.where(np.isfinite(momentum), momentum, 0.0).astype(np.float32)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_custom_matrix_breakout",),
        )


MATRIX_STRATEGY = CustomMatrixBreakout()
```

写矩阵策略要记住四件事：

1. **取指标用 `matrix_feature(market, "名字")`**：均线 `ma20`、`ema10`，`rsi_14`，`momentum_20d`，`vol_ratio_5d`，`atr_14`，布林、MACD、KDJ 等都按需现算并缓存，不用自己写。
2. **窗口和后移用带 `valid_` 前缀的函数**（`valid_rolling_max`、`valid_rolling_mean`、`valid_shift` 等）。它们按「有效 K 线」数根数，会跳过停牌留下的空格；不带前缀的版本按行数，停牌票会算错。
3. **不用未来数据**：「此前 N 日最高」必须先算窗口、再 `valid_shift(..., 1, ...)` 后移一根，当日收盘不能进自己的比较线。这一条机器不检查。
4. **分数不能有 `NaN`**：用 `np.where(np.isfinite(x), x, 0.0)` 兜底，类型转成 `float32`。买入、卖出数组转成 `uint8`。漏了会在运行时报 `SignalMatrix.score must contain only finite values`。

矩阵策略只能 import `numpy` 和 `app.backtest.matrix`（以及 §5.1 清单里的其他模块），不要 import `polars` 去逐票处理。

### 3.6 常见写法与坑

- 布尔表达式：`.fill_null(False)`，否则新上市/停牌会产生空值行。
- 窗口：`rolling_max(20, min_samples=20)`；样本不足宁可不出信号，不要放宽 `min_samples` 凑数。
- `over("symbol")` 之前一定先按 `symbol, date` 排序。
- 不要写 `df.to_dicts()` 逐股循环；先用 Polars 表达式，确实写不出来才 `partition_by("symbol")`。
- 不要在策略里 `open()` 读文件、发网络请求或 import 数据源；策略只接收引擎给的数据。

## 4. 能用什么数据

### 4.1 可用列（简表）

| 类别 | 列 |
| :--- | :--- |
| 标识 | `symbol`、`date`、`name`（运行时 JOIN） |
| 价格 | `open`、`high`、`low`、`close`（前复权）、`raw_close`、`raw_high`、`raw_low`、`prev_close`、`change_pct`、`change_amount`、`amount`、`amplitude` |
| 均线 | `ma5/10/20/30/60`、`ema5/10/20/30/60` |
| 技术 | `macd_dif/dea/hist`、`boll_upper/lower`、`kdj_k/d/j`、`rsi_6/14/24`、`atr_14` |
| 量能 | `volume`（股）、`vol_ma5`、`vol_ma10`、`vol_ratio_5d` |
| 动量/波动 | `momentum_5d/10d/20d/30d/60d`、`annual_vol_20d`、`high_60d`、`low_60d` |
| 虚拟评分 | `ma20_bias`（`close / ma20 - 1`，只存在于内存） |

完整清单和信号列见 `backend/app/strategy/prompts/strategy-guide.md` §3–4。

### 4.2 不可用

- 财务数据（PE/PB/ROE/营收/利润/资产负债等）——当前链路没有。
- 市值、流通市值、换手率——股本数据缺失，不要写这类过滤。
- 行业/概念/人气排名/资金流向——`ext_data` 未 JOIN。
- 盘中实时价、盘口——策略日线上下文里没有。

需要这些条件，先把数据接进系统，不要在策略里“假装有”。

### 4.3 美股口径

- 价格与成交额是美元，成交量是**股**，没有“手”的换算。
- 常规时段 09:30–16:00 ET，连续交易、无午休；分钟根不含 16:00，半日市按交易日历。
- 日线是前复权；跨拆股/代码换主要用系统提供的复权序列，不要自己按 `raw_close` 拼。

## 5. 安全与加载

### 5.1 import 白名单

只允许：

```text
polars
numpy
datetime
__future__
app.backtest.matrix
app.backtest.tv_kernels
app.strategy.market_data
app.strategy.builtin.factor_rank_research
```

以及同目录的 `_helper` 模块。其余一律拒绝，报 `禁止 import xxx (不在策略安全白名单)`。

### 5.2 禁止

- 调用 `open` / `exec` / `eval` / `compile` / `__import__` / `globals` / `locals` / `vars` / `dir` / `getattr` / `setattr` / `delattr` / `type` / `input` / `breakpoint`。
- 访问 `__globals__`、`__builtins__`、`__class__`、`__subclasses__`、`__mro__`、`__dict__`、`__code__` 等 dunder 属性。
- 用字符串下标取 `__builtins__` / `__import__` / `__globals__`。

AST 校验不是完整沙箱，只能拦常见逃逸；不要试图绕过，绕过了也会在 `exec_module` 前被同一套校验再拦一次。

### 5.3 加载规则

- 目录顶层 `*.py` 全量加载；`_` / `.` 开头跳过。
- **热重载是 all-or-nothing**：任一策略文件出错，整次 reload 失败，注册表保留上一版。所以一个坏文件会卡住所有保存/发布，先在 `code/validate` 里过一遍。
- `META.id` 重复时，保存会报 `策略加载到了非预期文件，请检查是否存在重复 strategy_id`。
- 内置策略不可覆盖、不可删除；要改就先另存为 `custom_` 策略。

## 6. 保存、发布、可见性

### 6.1 三条保存路径

| 路径 | 写入位置 | 默认可见性 |
| :--- | :--- | :--- |
| 页面「自定义」页签 / `POST /code/save` + `target_source=custom` | `data/strategies/custom/` | 公开（除非 META 写了 `research_only: true`） |
| AI 生成保存 `/ai/save` 或 `target_source=ai` | `data/strategies/ai/` | 草稿（后端自动写 `research_only: true`） |
| 叠加策略 `/composite/save` | `data/strategies/composite/` | 公开 |

### 6.2 草稿与发布

- `research_only: true` 的策略：不出现在公开策略列表、不能运行、不进规则目录、不参加机会板候选验收；只在策略池的草稿分区可见。
- AI 草稿发布：`POST /api/strategies/{id}/publish`，只对 `source=ai` 且仍为草稿的策略有效；发布后进入公开列表、可运行、可监控。
- 手写策略没有“发布”接口：把 META 里的 `research_only` 改成 `false`（或删掉）再保存即可；想收回就改回 `true`。

### 6.3 删除

`DELETE /api/strategies/{id}`：

- 内置策略拒绝（403）；被叠加策略引用的拒绝（409），先解除引用。
- 会尽力清理覆盖配置、监控偏好和关联监控规则（失败只记 warning）。
- **不会**自动把它从机会页 3+3 里移除。删之前先去齿轮取消勾选，否则那个位置会一直空着、永远不出卡，页面只在下次保存时提示“未知规则”。

### 6.4 参数覆盖

用户在策略页改的参数/基础过滤/评分存为覆盖配置，不改源文件。覆盖只影响该策略的运行，不会反向写入 `.py`；要固化就改源码。删除策略会连带删除覆盖配置。

## 7. 从策略到机会板（最重要的边界）

### 7.1 使用路径

| 目标 | 操作 | 门槛 |
| :--- | :--- | :--- |
| 当日选股 | 策略页运行 / 全市场扫描 | 只受 `basic_filter` 和策略逻辑 |
| 回测 | 回测页 · 策略回测 | 需要历史数据；正式成绩按 2023-01-03 起的牛市窗口，熊市年份只作参照 |
| 盘中提醒 | 监控中心 · 策略触发器 | 只通知，不下单 |
| 机会卡 + 纸面 | 机会页齿轮 · 3+3 | 见下 |

### 7.2 3+3 出卡

- 可勾选列表 = S1/S2 +（可选）Phantom 4h + 引擎里所有**非草稿、`stock`、`1d`** 的策略（含手写 `custom_` 和已发布 AI 策略）。
- 多头最多 3 条、空头最多 3 条；保存走 `POST /api/opportunities/settings`，会核对规则是否仍存在、空头规则是否真的能做空，未知规则返回 422。
- 卡片只对**自选股**生成；票不在自选、或信号日之后被移出自选，就不出卡。
- 规则改动对**新的信号日**生效；已经落进纸面下单簿的信号日仍用簿里冻结的那一套（板和纸面始终同一批卡）。

### 7.3 方向门

3+3 命中只是候选，出卡还要过方向门：

- 装了 4h 门插件的部署：4h 门（同向“条件齐备”事件，或 4h 状态同向）。
- 开源核心版没装 4h 插件时：日线 MA50 门（信号日收盘在 MA50 上方 = 多头放行，下方 = 空头放行）。
- 数据不足/缺失：不放行（fail-closed），不会“默认通过”。

4h 门不同向的卡不会显示在板上，只记在当天的 `gate_blocked` 统计里。

### 7.4 候选标记

系统每周（挖掘结束后，以及 AI / 挖掘策略发布之后）会对**系统发现的策略**做一次卡回放；齿轮里也可以手动触发一次重新验收。

- 对象：`source` 为 `custom` 或 `ai`、非草稿、`stock` + `1d`、后端是 `polars_expr` / `python_history_legacy` / `matrix_native`。
- 方法：全市场出信号 → 按分数每天取前 5 张卡（与机会板同一套紧几何、同一套 30 分钟 K 结算）→ 和“同一天门内随机挑 5 张”比，周聚类自助法 95% 区间下限 > 0 才算过。
- 结果只写 `research/swing_candidates.json`，在齿轮里显示“候选”标记；**系统不会自己把它勾进 3+3**，勾不勾由人决定。

所以手写策略也会被自动验收，但验收通过只是“有资格被考虑”，不是自动上线。

### 7.5 纸面许可

- 机会板模式（存在 `data/rules/swing.json`）下，纸面只交易机会板卡，规则名统一为 `swing`。
- 前提是规则集已保存且单笔风险 > 0；两者缺一，`permission` 返回空，卡片不会进下单簿。
- 3+3 决定“出哪些卡”，规则集风险决定“能不能下单、下多大”，两者都成立才可能开仓。

### 7.6 不成立的推论

- “回测夏普高” ≠ 可以开仓。
- “进了公开策略池” ≠ 有许可；许可只来自机会页规则集与 3+3。
- “候选标记” ≠ 自动上线。
- “策略能跑” ≠ 盘中会成交；还要过方向门、入场窗、准入和账户约束。

## 8. 验证清单

### 8.1 保存前

- [ ] `POST /api/strategies/code/validate` 返回 `valid: true`。
- [ ] `META` 是纯字面量；`id` 与文件名一致、不与其他策略重复。
- [ ] `asset_types` / `timeframes` 与实际数据一致（日线不要写 `1m`）。
- [ ] 只声明了一种执行后端，且没有同时写 `filter` 和 `filter_history`。
- [ ] `scoring` 为空或权重合计 1.0；矩阵写法的分数没有 `NaN`。
- [ ] 所有布尔信号列都 `.fill_null(False)`。
- [ ] `filter_history` 返回全部匹配行，没有 `tail(1)` / `latest` 过滤。
- [ ] 历史窗口声明了 `LOOKBACK_DAYS`（或 `lookback_days` 参数），且不超过实际可用历史。
- [ ] 没有财务/市值/换手率/行业资金流字段。
- [ ] 没有 `open` / `exec` / `eval` / 网络请求 / 文件读取。

### 8.2 上线前（进 3+3 前）

- [ ] 全市场扫描的命中数在同一区间内稳定，不是个位数也不是全市场。
- [ ] 回测分年看：不是只有一年赚钱；参数 ±20% 不出现断崖。
- [ ] 留出集只跑一次，不参与调参。
- [ ] 检查未来函数：所有“前 N 日最高/最低/均量”都 `shift(1)`。
- [ ] 检查拆股/代码换主：复权序列由系统提供，不自己拼 `raw_close`。
- [ ] 检查半日市、停牌、新上市：窗口不足时不出信号，不报错。
- [ ] 先在齿轮里看候选回放的 `excess` / `excess_ci`；区间跨零就不要急着占 3+3 名额。
- [ ] 进 3+3 后先观察 1–2 周卡片质量，再考虑纸面跟单；不要用单日结果放宽门槛。

### 8.3 迭代纪律

- 动手前把门槛写下来（夏普/回撤/胜率/命中数），看完结果再改门槛等于自欺。
- 粗调期一轮一个主题；精调期一轮只改一处。
- 留出集封存，只在定型时跑一次。
- 负知识（什么没用、为什么）比“这次赚了多少”更值得记；快照和台账放 `data/strategies/custom/.iterations/<id>/`，不会被引擎加载。

## 9. 报错对照表

| 报错 | 原因 | 处理 |
| :--- | :--- | :--- |
| `polars_expr strategy must declare only filter` | 同时写了 `filter_history` 或没写 `filter` | 只留一个入口函数 |
| `python_history_legacy strategy must declare only filter_history` | 同时写了 `filter` | 同上 |
| `matrix_native strategy must declare MATRIX_STRATEGY` / `找不到 Matrix 策略入口 MATRIX_STRATEGY` | 后端写了矩阵但没提供对象 | 补 `MATRIX_STRATEGY` 或改后端 |
| `MATRIX_STRATEGY must implement MatrixStrategy` | 对象缺三个方法之一 | 补齐 `required_fields` / `required_warmup_bars` / `compute_signals` |
| `SignalMatrix.score must contain only finite values` | 分数里有 `NaN` 或无穷 | 用 `np.where(np.isfinite(x), x, 0.0)` 兜底 |
| `SignalMatrix.entry must use uint8, got bool` | 数组类型不对 | `entry.astype(np.uint8)`，分数 `astype(np.float32)` |
| `META.scoring 权重总和必须为 1.0` | 评分权重没凑满 1 | 调整权重，或写空字典 |
| `找不到策略入口函数 filter() 或 filter_history()` | 没写入口函数，或写成了别的名字（如 `basic_filter`） | 入口函数名必须是 `filter` 或 `filter_history` |
| `composite strategy must not declare filter...` | 叠加策略里写了业务代码 | 叠加策略只写 `META.children` |
| `minute_filter strategy must declare timeframes == ['1m']` | 分钟策略写了日线周期 | 改成 `["1m"]` |
| `minute_filter daily_history_bars requires ... 'daily' keyword` | 声明了日线历史但函数签名不接 | 加 `daily=None` 关键字 |
| `禁止 import xxx (不在策略安全白名单)` | 引用了白名单外模块 | 删掉或换成允许的模块 |
| `禁止调用 open()` | 用了危险内建 | 策略不能读文件/执行代码 |
| `META 必须是纯字面量字典` | `META` 里有变量/函数调用 | 改成纯字面量 |
| `策略保存失败: ...` | 校验/加载失败，已自动回滚 | 看冒号后的真实原因，先 validate |
| `策略加载到了非预期文件...` | ID 重复或来源目录不对 | 换唯一 ID，检查目录 |
| `策略引用了未定义的自定义信号: csg_...` | `REQUIRED_FEATURES` 里的自定义信号没建 | 先建信号，或删掉该依赖 |
| `requires history data` | `filter_history` 没声明足够 `LOOKBACK_DAYS` | 声明 `LOOKBACK_DAYS` 或 `lookback_days` 参数 |
| `自定义策略 ID 必须以 custom_ 开头` | 新建时 ID 前缀不对 | 换 `custom_` / `ai_` / `composite_` |
| `内置策略不可覆盖，请另存为自定义策略` | 想改内置策略 | 新建 `custom_` 策略 |
| `未知规则 (已删除或改名, 先取消勾选)` | 3+3 里残留了已删除的规则 | 打开齿轮取消勾选再保存 |

## 10. 参考

- 规则清单（必须 / 禁止 / 机器查不查）：[策略语法标准](./strategy-standard.md)
- 文件规范（字段、列名、信号列的完整清单）：`backend/app/strategy/prompts/strategy-guide.md`
- 策略体系与三种扩展方式：`docs/strategy.md`
- 因子与挖掘：`docs/mining.md`
- 数据源插件：`docs/plugin-development.md`
- 矩阵接口与指标函数：`backend/app/backtest/matrix.py`
- 现行引擎与加载校验：`backend/app/strategy/engine.py`
- 安全校验：`backend/app/strategy/ai_generator.py`
- 保存 / 发布 / 删除接口：`backend/app/api/strategy.py`
- 机会页 3+3 与方向门：`backend/app/services/swing_desk.py`、`backend/app/opportunity/direction_gate.py`
