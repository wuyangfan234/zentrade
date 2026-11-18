# 策略语法标准

本页只写**规则**：一个策略文件必须怎样、禁止怎样、可以怎样。怎么一步步写，见 [手动策略开发指南](./strategy-development.md)。

- 每条规则后面标了代码出处。规则以代码为准；本页与代码不一致时，是本页的错。
- 带 `<!-- std:... -->` 标记的清单由测试 `backend/tests/test_strategy_docs.py` 与代码常量逐项比对，代码改了本页没改，测试不通过。
- 「机器检查」一栏写明这条规则由谁拦：**校验** = 保存前的静态检查；**加载** = 引擎装载时；**运行** = 出信号时；**不检查** = 只是约定，写错了系统不会报。

## 1. 文件

| 规则 | 机器检查 | 出处 |
| :--- | :--- | :--- |
| 一个策略 = 一个 `.py` 文件，放在策略目录顶层 | 加载 | `strategy/engine.py` |
| 文件名以 `_` 或 `.` 开头的不当作策略；`_xxx.py` 可作同目录辅助模块 | 加载 | `strategy/engine.py` |
| 手写策略的编号以 `custom_` 开头，AI 生成以 `ai_` 开头，叠加策略以 `composite_` 开头 | 保存 | `api/strategy.py` |
| 内置策略不可覆盖、不可删除 | 保存 | `api/strategy.py` |
| 编号不得与其他策略重复 | 保存 | `api/strategy.py` |

## 2. META

| 规则 | 机器检查 | 出处 |
| :--- | :--- | :--- |
| 模块顶层必须有 `META`，且是**纯字面量字典**：不引用变量、不调用函数、不写 `dict(...)` | 校验 | `ai_generator._extract_meta` |
| `asset_types`、`timeframes` 若声明，必须是非空字符串列表；内置策略必须声明 `asset_types` | 加载 | `strategy/engine.py` |
| `params` 每一项必须有非空 `id` | 校验 | `ai_generator._validate_meta_semantics` |
| `scoring` 必须是字典；非空时每个权重是非负有限数，**权重合计必须等于 1.0** | 校验 | 同上 |
| `scoring` 的字段名只能是真实数值字段或 `ma20_bias`（历史窗口写法不查字段名） | 校验 | 同上 |
| `basic_filter` 不得设置下列股本类门槛（美股没有这份数据） | 校验 | 同上 |
| 代码里不得引用已停用字段：`deviate_3d` / `deviate_10d` / `deviate_30d`，以及 `signal_deviate_`、`signal_limit_`、`signal_broken_limit_`、`consecutive_limit_` 开头的列 | 校验 | 同上 |

不可用的股本类门槛：

<!-- std:capital-filter-fields -->
```text
float_cap_max
float_cap_min
market_cap_max
market_cap_min
```

不可用的股本类字段：

<!-- std:share-capital-fields -->
```text
float_market_cap
float_shares
log_float_mv
market_cap
total_shares
turnover_mean_20d
turnover_rate
turnover_ratio_5d
turnover_std_20d
turnover_z_60d
```

## 3. 执行方式（五选一，不得混用）

`EXECUTION_BACKEND` 只接受下面五个值。不写时：有 `filter_history` 按历史窗口，否则按单日过滤。

<!-- std:execution-backends -->
```text
composite
matrix_native
minute_filter
polars_expr
python_history_legacy
```

| 执行方式 | 必须 | 禁止 |
| :--- | :--- | :--- |
| `polars_expr` 单日过滤 | 顶层函数 `filter(df, params)`，返回 `pl.Expr` | `filter_history` |
| `python_history_legacy` 历史窗口 | 顶层函数 `filter_history(df, params)`，返回 `pl.DataFrame` | `filter` |
| `matrix_native` 矩阵 | 顶层对象 `MATRIX_STRATEGY`，实现 §4 的三个方法 | `filter`、`filter_history` |
| `composite` 叠加 | `META["children"]`：非空列表，每项有 `strategy_id` 与非负 `weight`，不重复 | `filter`、`filter_history`、`MATRIX_STRATEGY`；不得嵌套叠加 |
| `minute_filter` 分钟 | 顶层函数 `filter_minute_history`；`timeframes` 必须恰好是 `["1m"]`；`daily_history_bars` 在 0–250，大于 0 时函数必须接受 `daily` 关键字 | `filter`、`filter_history`、`MATRIX_STRATEGY` |

机器检查：加载（`strategy/engine.py`）。保存前的校验只查入口是否存在：矩阵写法查 `MATRIX_STRATEGY`，其余查 `filter` 或 `filter_history`。

## 4. 矩阵写法的契约

`MATRIX_STRATEGY` 必须实现 `app.backtest.matrix.MatrixStrategy`：

| 方法 | 返回 | 说明 |
| :--- | :--- | :--- |
| `required_fields(self)` | `frozenset[str]` | 用到的行情字段名 |
| `required_warmup_bars(self, params)` | `int` | 出第一个可信信号前需要的历史根数 |
| `compute_signals(self, market, params)` | `SignalMatrix` | 用 `make_signal_matrix(market.shape, ...)` 构造 |

`SignalMatrix` 的每个数组形状必须等于 `market.shape`（行 = 时间，列 = 股票），类型固定，且只读：

<!-- std:signal-matrix-dtypes -->
```text
entry uint8
entry_signal_code int16
exit uint8
exit_signal_code int16
score float32
```

| 规则 | 机器检查 | 出处 |
| :--- | :--- | :--- |
| `score` **全部是有限值**：预热区、停牌、缺数据的格子填 0，不留 `NaN` / `inf` | 运行 | `backtest/matrix.validate_signal_matrix` |
| 形状、类型、只读不符即报错；用 `make_signal_matrix` 构造可自动满足类型与只读 | 运行 | 同上 |
| `scoring` 的字段名只能来自矩阵可用评分字段 | 校验 | `ai_generator._MATRIX_SCORING_FIELDS` |

## 5. 允许的 import

只允许下面这些模块（含其子模块）。同目录的 `_xxx.py` 辅助模块另外放行。

<!-- std:allowed-imports -->
```text
__future__
app.backtest.matrix
app.backtest.tv_kernels
app.strategy.builtin.factor_rank_research
app.strategy.market_data
datetime
numpy
polars
```

机器检查：校验，并在加载时再查一次（`ai_generator._validate_safety`）。

## 6. 禁止的调用与访问

禁止调用：

<!-- std:forbidden-calls -->
```text
__import__
breakpoint
compile
delattr
dir
eval
exec
getattr
globals
input
locals
open
setattr
type
vars
```

禁止访问的属性：

<!-- std:forbidden-attrs -->
```text
__base__
__bases__
__builtins__
__class__
__code__
__dict__
__globals__
__import__
__loader__
__mro__
__spec__
__subclasses__
__wrapped__
```

禁止用字符串下标取：

<!-- std:forbidden-subscripts -->
```text
__builtins__
__globals__
__import__
```

机器检查：校验（`ai_generator._validate_safety`）。这是静态检查，不是完整沙箱。辅助模块 `_xxx.py` 额外允许 `collections.abc`、`types`、`typing` 与 `vars()`，策略文件本身不允许。

## 7. 只是约定、机器不检查的

下面几条写错了系统**不会报错**，但结果会失真。保存前自己核对。

| 约定 | 写错的后果 |
| :--- | :--- |
| **不用未来数据**：「前 N 日最高 / 最低 / 均量」先后移一根再和当日比较（Polars 用 `shift(1).over("symbol")`，矩阵用 `valid_shift(..., 1, ...)`） | 回测虚高，实盘做不到 |
| `filter_history` 返回**全部**命中行，不只留最新一天 | 回测只剩一天有信号 |
| `filter_history` 声明 `LOOKBACK_DAYS` | 只加载 1 天历史，运行时报缺历史数据 |
| 布尔表达式末尾 `.fill_null(False)` | 新上市、停牌的票产生空值行 |
| `over("symbol")` 之前先按 `symbol`、`date` 排序 | 窗口错位 |
| 窗口不足时不出信号，不放宽 `min_samples` 凑数 | 预热期出假信号 |
| 不自己拿 `raw_close` 拼复权序列 | 拆股日信号错 |

## 8. 保存前自检

- [ ] 校验接口返回通过。
- [ ] `META` 是纯字面量；编号唯一，前缀正确。
- [ ] 只用了一种执行方式，没有混写两种入口。
- [ ] `scoring` 为空，或权重合计 1.0。
- [ ] 没有股本、财务、行业类字段。
- [ ] import 都在 §5 清单内；没有 §6 的调用。
- [ ] §7 的每条约定都核对过，尤其是「不用未来数据」。
- [ ] 矩阵写法：`score` 没有 `NaN`，三个方法齐全。

## 9. 这份标准不管的

策略通过本标准只说明**文件合法、能加载**。它不说明策略有效，也不等于允许开仓：能不能出机会卡、能不能进纸面下单，由机会页的规则设置、方向门和风险许可决定，见 [手动策略开发指南 §7](./strategy-development.md#7-从策略到机会板最重要的边界)。
