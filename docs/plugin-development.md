# 数据源插件开发指南

数据源插件是 ZenTrade 的美股行情数据来源,作为独立模块放在 `backend/app/plugins/` 下。
当前内置三个: **alpaca**(历史日K/分钟K/除权因子/官方开盘价)、**tickflow**(实时报价、纸面撮合
执行报价、5 秒热报价与分钟K拼接)、**hyperliquid**(盘前/周末参考价, 只用于跳空预警, 不声明任何数据集)。
services 层(kline_sync / quote_service / financial_sync)全部通过统一路由点分流: 插件声明了某数据集
且在「设置 → 数据源」的能力矩阵中被选为该数据集的生效源, 才走该插件; **未声明或未选中 → 该能力不授予
(fail-closed), 不会回退到任何内置厂商源**。
因此**一个合格的插件只需要正确实现契约,不需要改动任何 service / API 代码**;
反过来,插件也必须遵守内部数据契约(单位、代码格式、复权口径),框架不会替你转换。

> 无代码接入(纯 HTTP YAML 配置)请看 [custom-data-source.md](./custom-data-source.md),
> 两种方式遵循同一套内部数据契约。

## 快速上手

一个插件 = 一个目录 + 一个 `plugin.yaml` 清单:

```
backend/app/plugins/<your_plugin>/
├── plugin.yaml          # 清单(必需)
├── provider.py          # Provider 实现(必需)
├── ...                  # client/桥接/依赖文件(按需)
```

### plugin.yaml 字段

```yaml
name: my_source                          # 唯一标识, 只允许 [a-z0-9_], 也是 provider name
display_name: "我的数据源"                 # 设置页显示名
runtime: none                            # 运行时类型: node | python | none
entry: app.plugins.my_source.provider:MyProvider   # provider 类的导入路径
check: app.plugins.my_source.bridge:availability   # 可用性检测函数(可选)
datasets: [realtime]                     # 支持: daily/adj_factor/minute/full_minute/realtime/financial
api_key_env: MY_SOURCE_API_KEY           # (可选)声明后设置页提供 Key 输入框
hidden: false                            # (可选)true = 已加载但对设置页隐藏,不注册不展示
description: "数据源描述"
install_hint: "pip install xxx"          # 未装依赖时显示的安装提示
homepage: "https://example.com"          # (可选)官网/申请地址, 显示在设置页 Key 配置说明中
```

只声明真实提供的数据集; 未声明的数据集 `provider_has_dataset` 返回 False, 该能力不授予
(不回退)。不要声明做不了的数据集(粒度含义见下文"能力声明的粒度")。只做参考/预警的源
(如 hyperliquid 永续价)应声明 `datasets: []`, 防止被误选为实时行情或撮合价来源。

#### api_key_env(界面配置 API Key)

声明 `api_key_env` 的插件可以在设置页的数据源卡片中直接填写 Key,「先探后存」:

1. entry 模块需提供模块级 `probe_api_key(key) -> (ok, reason)`,
   后端用候选 Key 实探一次, **无效不落盘**
2. 有效则写入 `data/user_data/secrets.json` 的 `{name}_api_key` 字段
   (0600 权限, 优先级高于 `.env` / 环境变量)
3. 保存后自动重载数据源注册表, 插件即刻变为可切换
4. 插件取 Key 用 `secrets_store.get_env_backed_secret("{name}_api_key", api_key_env)`,
   保证 secrets.json 与 .env 两条配置路径一致

### runtime 字段说明

| runtime | 含义 | 典型场景 |
|---|---|---|
| `python` | 纯 Python 依赖, `pip install` | 第三方行情 SDK |
| `node` | 需要 Node.js 运行时 | 通过 Node.js 桥接的数据源 |
| `none` | 无额外依赖 | 纯 HTTP API 源 |

`runtime` 字段当前仅用于 UI 展示, 实际依赖检测由 `check` 函数负责。

### check 函数

插件自己负责检测依赖/Key 是否就绪。后端启动时会调用此函数:

```python
# app/plugins/my_source/provider.py (或 bridge.py)
def availability() -> tuple[bool, str]:
    """返回 (是否可用, 原因)。不抛异常。"""
    if not get_api_key():
        return False, "未配置 MY_SOURCE_API_KEY(可在设置页数据源卡片中直接填写)"
    return True, "ok"
```

- **可用** → 插件注册进路由表, 设置页可切换
- **不可用** → 设置页显示插件卡片但灰显, 展示原因/`install_hint`

## 内部数据契约(所有数据集必须遵守)

以下口径是全项目红线(详见 CONTRIBUTING §3)。金融数据错误往往不抛异常,而是生成
**看似合理的错误结果**——单位、代码格式、复权口径错了,页面照样能渲染,只是数字全错。
插件必须在 provider 内完成适配。

### 代码格式

- symbol 为**美股纯代码**, 无交易所后缀: `NVDA` / `AAPL` / `BRK.B`(股份类别保留点号); ETF 同格式。
- 接口返回带后缀或异构格式时(如 TickFlow 的 `NVDA.US`), 在 provider 内归一, 并丢弃非美股行
  (如 `600000.SH`), 不要直接透传。

### 单位制

| 字段 | 契约 | 说明 |
| --- | --- | --- |
| `change_pct` | **小数制**, `0.0366` = 3.66% | 接口给百分数(3.66)时必须在 provider 内显式 /100 |
| `turnover_rate`(realtime 入口) | **小数制**, `0.05` = 5% | 下游 enriched 管道统一转百分数值存储 |
| `volume` | **股数** | 美股无“手”的概念, 日K/分钟K/实时快照一律为股数 (自验: amount÷volume≈当日均价) |
| `amount` / `turnover` | **美元** | |
| 成交量口径 | **必须声明 feed** | 实时行情行带 `feed`/`quote_feed`/`volume_feed`: 全市场合并口径 (`sip`/`delayed_sip`/`tickflow`) 的量才能与 SIP 历史均量比较算量比; 单交易所口径 (`iex`) 的量会被契约置空 (`iex_volume_disabled`) |
| 日K OHLC | **不复权原始价** | 复权由 adj_factor + enriched 管道处理, provider 不得自行复权 |

### 缺字段与空数据

- 接口不提供的字段返回 `None`,禁止"数值小于 1 就乘 100"之类启发式补全——那会掩盖
  真实的数据错误。
- 可推导字段按固定口径推导: `change_pct = change_amount / prev_close`(小数制,不乘 100)。
- 接口结构整体变化(如所有行都识别不出 symbol)要打明确告警日志,不要静默返回空数据。

## 能力声明的粒度(重要)

`datasets` 声明是**数据集级**的,不是资产类型级的:声明了 `realtime`,整个全市场实时
轮询周期(含指数与 ETF 部分)就全部路由给插件。若你的快照只覆盖普通股:

- 指数行情自动降级为日线推导值(非实时),不报错;
- ETF 实时计数为 0。

这是当前框架的设计行为。要么在数据里尽量覆盖指数/ETF,要么接受降级并在
`description` 里向用户说明覆盖范围。

## Provider 接口契约

Provider 是普通 Python 类(无需继承基类),方法签名对齐 `GenericHTTPProvider`,
services 层零改动即可路由。只实现已声明数据集对应的方法,其余可缺省。

```python
class MyProvider:
    name = "my_source"
    builtin = True  # 标记为内置(不可被用户编辑/删除)

    def __init__(self):
        self.config = MyConfig()  # 需有 .datasets 属性(dict, key 是数据集名)

    def close(self) -> None:
        """清理资源(load_all 重建注册表时会调)。"""

    def get_daily(self, symbols, start_time, end_time, asset_type="stock",
                  on_chunk_done=None) -> pl.DataFrame:
        """日K: [symbol, date, open, high, low, close, volume, amount]; 不复权"""

    def iter_daily(self, symbols, start_time, end_time, asset_type="stock",
                   on_chunk_done=None) -> Iterator[pl.DataFrame]:
        """(可选)有界分批返回与 get_daily 同形的日K; 全市场历史同步优先消费。"""

    def get_adj_factors(self, symbols, start_time, end_time, asset_type="stock",
                        on_chunk_done=None) -> pl.DataFrame:
        """除权因子: [symbol, trade_date, ex_factor]"""

    def get_minute(self, symbols, start_time, end_time, asset_type="stock",
                   on_chunk_done=None, freq="1m") -> pl.DataFrame:
        """分钟K: [symbol, datetime(美东墙钟 naive), open, high, low, close, volume(股), amount(美元, 可空)]"""

    def get_intraday_batch(self, symbols, count=300, asset_type="stock") -> pl.DataFrame:
        """(声明 full_minute 数据集时实现) 全量分钟修复轮: 给定标的当日 1 分钟K,
        canonical 8 列同 get_minute; 内部自行分块/限速。"""

    def get_intraday_latest(self, symbols=None, count=3) -> pl.DataFrame:
        """(可选, full_minute 稳态增量轮) 尽量单请求返回全市场每只最新 count 根;
        未实现则服务降级为仅修复轮 (节奏下限 60s)。"""

    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → list[dict]。失败软返回 [], 不抛异常(不阻断轮询线程)。"""

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """(可选)指数实时快照 → list[dict], 行字段与 get_realtime 一致。
        核心指数为 SPY/QQQ/DIA/IWM (ETF 形式, 挂 NYSE Arca, 可能不在普通股 universe);
        声明 realtime 的源强烈建议实现本方法, 否则指数行情冻结在本地日K兜底。失败返回 None,
        成功但无数据返回 []。"""

    def get_execution_quotes(self, symbols, *, max_age_seconds=90.0) -> list[dict]:
        """(纸面撮合主源实现) ≤50 只小池报价: {symbol, price, timestamp(ms, 源时间), open,
        open_timestamp, feed}; open 只能是 09:30 官方开盘 (分钟根), 不能用快照里的 open 或最新价。"""

    def get_financials(self, table, symbols, latest_only=False) -> pl.DataFrame:
        """财务数据(声明 financial 数据集时实现, table 见 financial_sync 调用)。"""

    def get_instruments(self, asset_type="stock") -> list[dict]:
        """(可选)标的维表: 与 alpaca.get_instruments 同形的行, 供 instrument_sync 使用"""

    def test_dataset(self, dataset: str, symbols=None) -> dict:
        """(强烈建议)设置页"试拉"按钮。
        返回 {provider, dataset, rows, columns, preview, error?}; 未支持的数据集
        返回 error 字段说明。"""
```

### get_minute 的 datetime 时区契约

`datetime` 是**美东墙钟 naive**(如 `2026-10-01 09:35:00`), 只含常规时段 `[09:30, 16:00)`
ET (一个正常交易日最多 390 根, 16:00 只是右边界)。不要返回北京时间; 源是 UTC 时必须提供
tz-aware 字段或 epoch 由映射层换算。入口守卫 `kline_sync._enforce_minute_market_wallclock`
只做显式时区转换, 绝不按小时分布猜测平移 (盘后 17:00 ET 与 UTC 墙钟无法靠数值区分)。

`amount` 单位为美元; 数据源无法提供可靠的分钟成交额时应返回 `null`, 不得伪造。

可选类属性: `minute_history_days`(1 分钟历史深度, 交易日; None=深历史)、
`minute_delay_minutes`(右端延迟, Alpaca 免费 SIP = 16)。

> **全量分钟 (full_minute) 数据集契约**:声明 `full_minute` 数据集并把
> `full_minute_data_provider` 路由到你的源,即接入「全量分钟」能力(盘中全市场
> 当日分钟K增量落盘,由内置服务 `minute_refresh` 调度,能力键 `intraday.universe`)。需实现:
>
> - `get_intraday_batch(symbols, count=300, asset_type="stock") -> pl.DataFrame`
>   — **必须**(或已有 `get_minute` 自动回退,但强烈建议实现批量端点)。
>   返回给定标的当日 1 分钟K,canonical 8 列
>   `[symbol, datetime(美东墙钟 naive), open, high, low, close, volume, amount]`,
>   内部自行分块/限速。服务在冷启动、覆盖断档、连续空轮时调用(修复轮)。
> - `get_intraday_latest(symbols=None, count=3) -> pl.DataFrame` — **可选**,
>   稳态增量轮专用:尽量单请求返回全市场每只标的最新 `count` 根。未实现时
>   服务自动降级为仅修复轮,节奏下限抬到 60s(全天批量打不住 6s 节奏)。
>
> 两个方法的返回帧都过 `_enforce_minute_market_wallclock` 时区守卫(与
> `get_minute` 同纪律);失败抛异常或返回空 df 均按空轮处理,连续空轮触发
> 修复轮自愈。声明方式:插件在 `plugin.yaml` 的 `datasets:` 列表加入
> `full_minute`。YAML 声明式源同样支持(数据集配置与 `minute` 同形,仅提供
> 修复轮语义,见 [custom-data-source.md](./custom-data-source.md))。

### 异常语义

| 方法 | 失败行为 |
| --- | --- |
| `get_realtime` | **软失败**: 返回 `[]` + warning 日志, 保证轮询线程不中断 |
| `get_realtime_indices` | **软失败**: 返回 `None` + warning 日志, 保留上轮有效缓存; 成功无数据返回 `[]` |
| `get_depth_batch` | 单批异常由服务隔离并保留其他批次; 不跨数据源回退 |
| `get_minute` | 抛异常时本轮记为失败, 不跨源回退 |
| `get_daily` / `get_adj_factors` / `get_financials` | 异常由上层同步流程捕获记录; 无数据返回空 DataFrame |
| `iter_daily` | 可选; 每批必须符合 `get_daily` 契约。流正常结束后才提交 staging; 未捕获异常会丢弃 staging。provider 内已定义的单标的软失败语义保持不变 |

`iter_daily` 用于避免大范围日K同步在 provider 内累积完整 DataFrame。实现该方法后,
`kline_sync` 会优先消费它; 未实现的 provider 继续调用 `get_daily`,保持兼容。批次大小应有
明确上界,不得先把全部结果放入列表再 `concat`。`on_chunk_done(cur, total)` 必须覆盖空批次,
确保最终 `cur == total`。

### get_realtime 行字段

| 字段 | 必需 | 契约 |
| --- | --- | --- |
| `symbol` | ✅ | 美股纯代码 (无后缀) |
| `last_price` | ✅ | 最新价 |
| `prev_close` | ✅ | 昨收, 涨跌幅推导基准 |
| `open` / `high` / `low` | ✅ | 当日 OHLC |
| `volume` | ✅ | 股数 (当日累计) |
| `amount` | 建议 | 成交额(美元) |
| `change_pct` | 建议 | **小数制**; 缺失时下游按 change_amount/prev_close 推导 |
| `change_amount` | 建议 | 涨跌额(美元) |
| `timestamp` | ✅ | 毫秒, **源报价时间** (不是请求时间); 声明 feed 的行缺失或非当日会被契约丢弃 |
| `feed` / `quote_feed` / `volume_feed` / `volume_ts` / `volume_status` | 建议 | 成交量口径与时间, 见上文“成交量口径” |
| `name` | 可选 | 快照无名称时置 None, 下游用标的维表关联 |
| `amplitude` / `turnover_rate` / `session` | 可选 | 缺失置 None, 不启发式伪造; turnover_rate 入口为小数制 |

### config.datasets 的作用

`provider_has_dataset(name, dataset)` 通过 `dataset in provider.config.datasets` 判断。
这是 services 层路由的关键: 用户在设置页选了插件, 但某数据集未声明时, 该能力不授予
(不回退)。`current_capability_set` 还会读 `config.datasets[ds].rpm/batch` 作为限速声明,
**插件对象必须有 `.config.datasets`**, 否则保存数据源选择时能力重建会报错。

```python
class MyConfig:
    datasets = {"daily": ..., "realtime": ...}  # key 是数据集名, value 任意
```

## 限频与性能

- 先实测服务端的批量上限与频率限制, 在 provider 内做每接口限速并留余量 (tickflow 用 80% 额度:
  按标的报价 60 次/分 ≤50 只、标的池 20 次/分、日K批量 30 次/分 ≤100 只、除权因子 60 次/分 ≤200 只)。
- 全市场快照优先用“一次调用拿全市场”的接口 (tickflow 标的池 1.3 秒 / 12,381 只, gzip 约 1.5 MB)。
- 拉取由 fetch 锁串行化, 慢不会并发重叠; 实际刷新周期 = 轮询间隔 + 拉取耗时。
- 分页必须有页数上限和空页终止条件。

## 测试要求

插件 PR 必须带契约测试(CONTRIBUTING §9), **不依赖真实网络与 API Key**——用假
Client/桥接注入。以 `backend/tests/test_tickflow_plugin.py` 为范本, 至少覆盖:

1. 字段映射与单位转换: 代码归一 (去后缀、丢非美股)、百分数→小数制、源时间戳与时区、缺失字段按口径推导、缺失字段置 None 不伪造
2. 接口响应结构变体: 实测结构 vs 官方文档示例双兼容(供应商文档与实际不一致是常态)
3. 分页: 多页合并、空页终止、页数上限
4. 软失败: 接口报错返回 []; 整页 schema 变化有告警而非静默空数据
5. 能力声明: 未声明数据集 `provider_has_dataset` 为 False
6. Key 语义: 先探后存(无效不落盘)、secrets.json > .env 优先级、availability 两态
7. loader 集成: 清单解析后正确注册(或 hidden 时正确跳过)

```bash
cd backend && uv run --extra dev python -m pytest tests/test_<your_plugin>_provider.py -q
uv run --extra dev python -m ruff check app/plugins/<your_plugin>/ tests/test_<your_plugin>_provider.py
```

## 现有插件参考

- **`backend/app/plugins/alpaca/`** — 历史日K/分钟K (feed=sip, 右端 now−16 分钟)、除权因子 (raw/all 双拉按日比值,
  过 `ticker_handoff` 剔除代码换主污染)、标的维表、09:30 官方开盘价 `official_opens` (先 IEX, 09:46 后补 SIP)、
  IEX 执行报价 (TickFlow 不可用时的回退)。测试: `tests/test_alpaca_snapshot_contract.py`。
- **`backend/app/plugins/tickflow/`** — 声明 `realtime`。全市场快照 (按本地维表过滤)、纸面撮合执行报价
  (官方开盘价向 Alpaca 取, 不用报价里的 open)、5 秒热报价与分钟K拼接 (`stitched_minutes`, 只用于显示),
  日K/除权因子 (交叉核对用)。Key 在设置页卡片先探后存。测试: `tests/test_tickflow_plugin.py`。
- **`backend/app/plugins/hyperliquid/`** — `datasets: []`, 无 Key。`listed()` / `mids()` 供
  `services/premarket_gap` 在美东 09:00 做跳空预警; 永续价永不进入撮合、日K或实时行情。

## 路由机制(无需关心, 仅参考)

后端启动时, `loader.py` 扫描 `plugins/` 目录:
1. 读每个子目录的 `plugin.yaml`
2. `hidden: true` → 跳过(不注册不展示); 否则调 `check` 函数检测可用性
3. 可用 → 动态 import `entry` 指向的 Provider 类 → 注册进 `_PROVIDERS`
4. 不可用 → 记录状态, 设置页显示但不可切换

注册后, 插件和用户 YAML 自定义源走**完全相同的路由路径**(services 层的
`provider_has_dataset` / `get_provider` 调用), 无需额外集成代码。
