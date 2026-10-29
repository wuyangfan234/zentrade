# 第三方声明（Third-Party Notices）

本文件列出 ZenTrade 中第三方代码、策略移植、依赖和外部服务的归属与许可边界。根目录 `LICENSE` 的 MIT 授权只适用于 ZenTrade 原创部分，不自动覆盖本文件列出的第三方内容。

## 1. 上游项目

| 项目 | 许可 | 说明 |
| --- | --- | --- |
| [TSP (tick-stock-panel)](https://github.com/shy3130/tick-stock-panel) @ `3391e34` | MIT，© tick-stock-panel contributors | 本项目基于其代码演进。上游版权与许可全文保留在 [LICENSE.upstream](LICENSE.upstream)。 |

依据 MIT 协议，再分发相关代码时必须保留上游版权声明与许可文本。

## 2. 策略与指标移植

下列内置策略参考或移植自 TradingView 上公开可见的 Pine 脚本，出处和原始声明记录在对应源文件头部。Python / NumPy 重写不意味着原脚本许可被替换。

| 文件（默认位于 `backend/app/strategy/builtin/`） | 原脚本 / 作者 | 许可或原始声明 |
| --- | --- | --- |
| `tv_supertrend.py` | Supertrend v3.0 · KivancOzbilgic | 未单独声明 |
| `tv_ut_bot.py` | UT Bot Alerts v1.0 · QuantNomad；思路来自 Yo_adriiiiaan / HPotter | 未单独声明 |
| `tv_chandelier_exit.py` | Chandelier Exit v8 · everget；可选过滤来自 Ultimate Pro v2.2 · Göktürk80 | GPL-3.0 |
| `tv_halftrend.py` | HalfTrend v3 · everget | GPL-3.0 |
| `tv_zlsma_cross.py` | ZLSMA - Zero Lag LSMA · veryfid | MPL-2.0 |
| `tv_smc_structure.py` | Smart Money Concepts · LuxAlgo；仅波段结构部分 | **CC BY-NC-SA 4.0（非商用、相同方式共享）** |
| `tv_squeeze_momentum.py` | Squeeze Momentum Indicator v2 · LazyBear | 未单独声明 |
| `tv_wavetrend.py` | WaveTrend [LazyBear] vX · dgtrd；原作 LazyBear | 未单独声明 |
| `tv_tke.py` | TKE v4.2 · gokturk80 | **源码写明非商用** |
| `tv_phantom_shift.py`、`tv_phantom_trail.py`、`backend/app/vendor/oasis_phantom/` | Phantom Flow Core 中文优化版（Pine v6） | 作者提供的私有脚本源码；授权范围以原始声明为准 |
| `ibs_mean_reversion.py` | [QuantifiedStrategies IBS 规则页](https://www.quantifiedstrategies.com/ibs-strategy/) | 公开规则转录；原页面条款与站外授权需另行核对 |
| `connors_rsi2.py` | Larry Connors & Cesar Alvarez《Short Term Trading Strategies That Work》；[QS 规则页](https://www.quantifiedstrategies.com/rsi2-strategy/) | 书籍规则与页面复测来源；表达与授权范围需另行核对 |

共用内核位于 `backend/app/backtest/tv_kernels.py`。同一文件中混合了不同来源和许可的函数，因此**不能把整个内核文件或后端 wheel 简单视为 MIT**；上表对应条目同时适用于内核中的相关实现。

Phantom 相关实现还涉及 `backend/app/services/phantom_4h.py`、`phantom_4h_pine.py`、`phantom_4h_source.py`、`kline_4h.py` 及交易候选链中的 4h 门。测试参照脚本位于 `backend/tests/tv_reference/`；部分对照答案由 [PyneCore](https://github.com/PyneSys/pynecore)（Apache-2.0）生成，PyneCore 不是运行时依赖，其许可证也不会覆盖原脚本及 fixtures 的权利边界。

### 许可边界

- GPL-3.0 和 MPL-2.0 文本分别见 [GPL-3.0.txt](LICENSES/GPL-3.0.txt) 与 [MPL-2.0.txt](LICENSES/MPL-2.0.txt)。相关文件的再分发须同时满足其对应义务；源文件未明确的 only/or-later 选择不作推测。
- CC BY-NC-SA 4.0 文本见 [CC-BY-NC-SA-4.0.txt](LICENSES/CC-BY-NC-SA-4.0.txt)。该许可含非商用限制，**不是 OSI 批准的开源许可证**。
- 标为「非商用」的内容不得用于商业目的。TKE 目前只有源文件对 v3 非商用约定的转述，公开再分发前还必须取得并保存对应版本的条款原文。标为「未单独声明」不等于自动获得再分发或商业使用授权；TradingView 的公开可见状态和 [House Rules](https://www.tradingview.com/support/solutions/43000590599/) 仍需单独核对。
- 私有脚本由作者提供不代表本项目可以为其额外授予许可。需要严格 OSI 开源、商业使用或二次再分发时，应先取得所需授权，或移除授权范围不满足要求的组件。
- 策略引擎按目录发现文件。移除某个策略文件及其引用后，可使其不再加载，但修改功能集后必须重新运行测试并核对文档。

## 3. 运行时与可选依赖

后端依赖及版本见 `backend/pyproject.toml` 与 `backend/uv.lock`，前端依赖及版本见 `frontend/package.json` 与 `frontend/pnpm-lock.yaml`。各依赖的版权和许可随其包元数据与源码分发；进行产品再分发或商业使用前，应使用实际安装产物复核依赖许可，而不是只根据依赖名称推断。

特别说明：

- 可选 extra `backtest` 中的 [vectorbt](https://github.com/polakowo/vectorbt) 使用 **Apache-2.0 + Commons Clause**，含额外销售限制；默认镜像不安装，只有显式启用该 extra 时才引入。
- Docker 构建会安装 OpenAI Codex CLI（Apache-2.0），用于可选的本机 Codex 登录态 AI 通道；主机凭据目录只有叠加 `docker-compose.codex.yml` 时才会挂载。
- 测试和构建依赖不一定进入最终运行镜像，应区分运行时、构建时和研究用途的许可义务。

## 4. 外部数据源与服务

ZenTrade 不附带第三方行情数据，也不附带任何真实 API Key。各服务须由使用者自行申请账户并遵守其条款；免 Key 公开端点不保证持续可用，也不代表数据可以再分发或商用。

| 数据源或服务 | 用途 | 鉴权 |
| --- | --- | --- |
| [Alpaca Market Data / Paper Trading](https://alpaca.markets/) | 日 K、分钟 K、IEX 实时、纸面镜像 | 用户自备 Key |
| [TickFlow](https://tickflow.org/) | 盘中实时行情 | 用户自备 Key |
| [Finnhub](https://finnhub.io/) | 财报核对、新闻、公司图标 | 用户自备 Key |
| [QVeris](https://qveris.ai/) | 期权、财报预期、电话会 | 用户自备 Key，按积分计费 |
| [SEC EDGAR](https://www.sec.gov/os/accessing-edgar-data) | 公司财务与申报文件 | 免 Key，须在 User-Agent 中提供联系标识 |
| Nasdaq、CBOE、FRED、ForexFactory | 财报保护、盘面情报 | 公开端点，条款以提供方为准 |
| Hyperliquid | 周末和盘前参考价 | 公开端点 |
| Google 翻译网页端点 | 新闻翻译兜底（默认关闭，`NEWS_GOOGLE_TRANSLATE_FALLBACK=1` 开启） | 非官方端点，可用性不保证 |
| CoinMarketCap、Parqet、FMP 静态图标 | 公司图标 | 公开静态资源，商标归各自权利人 |
| OpenAI 兼容 LLM 接口 | 个股分析、新闻打标、AI 策略生成 | 用户自备 |

## 5. 商标与无背书

ZenTrade、TradingView、Alpaca、Nasdaq、CBOE 及其他第三方名称和标识归各自权利人所有。本项目与上述公司或服务提供方无隶属关系，也不声称获得其背书。

如发现归属遗漏或许可标注错误，请提交不包含私密信息的 issue；涉及授权范围不确定时，以原作者的明确声明为准。
