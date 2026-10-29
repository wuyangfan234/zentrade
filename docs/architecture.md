# 架构说明

ZenTrade 是一个单容器、单用户、自托管的美股波段研究与纸面交易应用。架构目标不是堆叠服务，而是在一条可回放、可审计的链路中保持时间、数据、信号和成交口径一致。

## 1. 运行形态

- 前端：React 18、TypeScript、Vite、TanStack Query、Tailwind CSS。
- 后端：Python 3.11+、FastAPI、Polars、DuckDB、PyArrow。
- 存储：本地 Parquet、DuckDB 和 JSON 文件，不依赖外部数据库。
- 调度：应用内 APScheduler，按美东市场时间运行。
- 部署：单个 Docker 容器，`./data` 挂载为 `/app/data`。
- 任务模型：部分研究任务使用进程内锁和单飞控制，因此生产环境只运行一个应用进程。

## 2. 六个核心模块

### 市场时钟

代码：`backend/app/market_clock/`

美东时区、NYSE 交易日、常规交易时段和持有交易日计数由同一时钟提供。业务逻辑不得依赖服务器本地时区。分钟 K 使用美东墙钟 naive 时间，常规时段为 `[09:30, 16:00)`。

### 数据能力总线

代码：`backend/app/data_providers/`、`backend/app/plugins/`

数据源按日 K、分钟 K、全量分钟、实时、复权和财务等数据集声明能力。只有已声明且被路由选中的能力才会授予上层；缺失时明确降级或拒绝，不静默换用不一致的数据。

### 策略引擎

代码：`backend/app/strategy/`、`backend/app/backtest/`、`backend/app/indicators/`

内置、自定义和研究发布的策略共享执行契约。信号生成与成交模拟分离，研究、回测和生产读取尽量复用同一数据与指标路径，避免同名不同口径。

### 机会协议

代码：`backend/app/opportunity/`、`backend/app/services/opp_page.py`

机会卡把策略输出转换为完整提案：触发区、失效点、目标区、方向和时间边界。页面聚合接口按区块隔离失败，某个情报或标注源失败不应拖垮整页。

机会板只在**方向门**同向时出卡。方向门是可插拔接口（`app/opportunity/direction_gate.py`）：盘后扫描时由当前生效的门给每只自选股一个方向（多 / 空 / 不放行），写进当日自选信号快照；机会板与纸面下单簿只读快照，不重算门。内置门为日线趋势门（收盘相对 50 日均线，数据不足不放行）；插件门调用 `direction_gate.register()` 注册，`rules/swing.json` 的 `gate` 字段可以显式选择已注册的门，未注册时自动回落到内置门。研究回放用同一个门的矩阵版本（只使用信号日之前已知的数据）。

### 证据与许可

代码：`backend/app/services/decision_desk.py`、`backend/app/services/watchlist_v4.py`、`backend/app/services/entry_guard.py`

研究命中、观察卡、可纸面执行和实际成交是不同状态。准入同时考虑来源证据、风险预算、入场窗口、组合约束和保护性数据可用性；证据不足不能被描述成已验证收益。

### 交易环与事件台账

代码：`backend/app/services/trading_loop.py`、`backend/app/trader/`、`backend/app/ledger/`

交易环处理入场、持仓管理、减仓和退出。状态变化写入只追加事件台账，持仓与净值可由台账重建。Alpaca 镜像仅允许 paper-api，不提供真实交易通道。

## 3. 主数据流

```text
数据插件 / 自定义 HTTP 源
  → 标准化、单位与时区校验
  → Parquet / DuckDB / KlineRepository
  → 指标与 enriched 数据
  → 策略、因子、回测和监控
  → 机会卡与证据许可
  → 纸面交易环
  → 事件台账 / 净值 / 持仓
  → FastAPI + SSE → 前端
```

页面不得绕过标准化仓库和业务服务直接读取供应商响应。新增数据能力应先定义输入、输出、单位、时区、空数据和失败语义，再接入 provider。

## 4. 不可混用的口径

- 成交量单位是股，不是手。
- 美股业务时间使用 `America/New_York`；服务器时区只影响日志显示。
- 日 K 原始价、前复权价和实时成交价必须明确区分。
- 交易日窗口不等于自然日窗口。
- 研究观察、纸面成交和真实收益必须分别表述。
- 缺失数据返回空值或明确错误，不用 0 或启发式换算伪装完整。

更完整的贡献约束见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## 5. 扩展边界

- 新策略：见 [策略指南](strategy.md)。
- 新数据源：优先使用 [YAML 自定义源](custom-data-source.md)，复杂协议再实现 [Python 插件](plugin-development.md)。
- 新页面或应用扩展：见 [二次开发指南](secondary-development.md)。
- 新外部服务：统一按 [配置规范](configuration.md) 管理凭据，不在业务模块写死密钥路径。

## 6. 安全与部署边界

ZenTrade 不是多租户 SaaS。自定义策略不是安全沙箱，扩展数据 URL 也不是受限网络环境。单进程、单用户和自托管是明确的设计边界。公开部署前应阅读 [SECURITY.md](../SECURITY.md) 与 [部署指南](deployment.md)。
