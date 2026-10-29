# 第三方声明（开源核心版）

本文件列出核心版使用的上游代码、依赖与外部服务。核心版不含任何非 OSI 许可的源代码。

## 1. 上游项目

| 项目 | 许可 | 说明 |
| --- | --- | --- |
| [TSP (tick-stock-panel)](https://github.com/shy3130/tick-stock-panel) @ `3391e34` | MIT | 本项目基于其代码演进，版权与许可全文见 [LICENSE.upstream](LICENSE.upstream)。 |

## 2. 运行时与构建依赖

依赖及精确版本见 `backend/pyproject.toml`、`backend/uv.lock`、`frontend/package.json` 与 `frontend/pnpm-lock.yaml`。导出核心版时逐包核对了实际安装产物的许可元数据：

- 后端运行时依赖：MIT、BSD、Apache-2.0、MPL-2.0（certifi）、PSF 等 OSI 许可。
- 前端依赖：MIT、ISC、BSD、Apache-2.0、0BSD 等 OSI 许可。
- 例外说明：`caniuse-lite` 的浏览器兼容数据以 CC-BY-4.0 发布，只在构建期由 autoprefixer / browserslist 读取，不进入运行产物。
- 完整版里带销售限制的可选回测依赖（vectorbt，Apache-2.0 + Commons Clause）**不在核心版的依赖声明中**。
- Docker 构建会安装 OpenAI Codex CLI（Apache-2.0），用于可选的本机 Codex 登录态 AI 通道。

依赖的版权与许可随各自包分发；再分发或商业使用前，请以实际安装产物为准复核。

## 3. 外部数据源与服务

ZenTrade 不附带第三方行情数据，也不附带任何真实 API Key。各服务由使用者自行申请账户并遵守其条款；免 Key 公开端点不保证持续可用，也不代表数据可以再分发或商用。

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

## 4. 商标与无背书

ZenTrade、Alpaca、Nasdaq、CBOE 及其他第三方名称和标识归各自权利人所有。本项目与上述公司或服务提供方无隶属关系，也不声称获得其背书。
