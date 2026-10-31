# 配置规范

ZenTrade 用到的外部数据源、AI 模型和推送渠道，统一按本文的规则配置。页面「设置 → 数据源」顶部的**配置清单**列出同样的内容，并显示每项当前是否已配置、来源是哪里（接口 `GET /api/settings/setup-checklist`）。

## 1. 取值顺序（全系统同一条规则）

每个需要 Key 的服务，按下面顺序取第一个非空值：

1. **设置页**：页面填写的值写入 `data/user_data/secrets.json`（权限 600），立即生效。
2. **环境变量**：`.env` 经 `docker compose` 的 `env_file` 注入，或宿主机环境变量。改完需重启容器。
3. **本机 env 目录**（可选，仅开发机或宿主机脚本用）：设置 `ZENTRADE_ENV_DIR=/path/to/dir` 后，从该目录读 `alpaca.env`、`finnhub.env`、`polygon.env`。不设就不读任何文件。

实现集中在 `backend/app/credentials.py`。新增外部服务时，在该文件加一个 `SecretSpec`，并在 `backend/app/services/setup_checklist.py` 加一项清单条目，写清用途、不配的影响、去哪配。不要在业务模块里直接读 `os.environ` 或写死文件路径。

密钥永不打印、不写日志、不出接口：接口只返回「是否已配置」和脱敏串。

## 2. 配置项一览

| 项 | 级别 | 环境变量 | 设置页 | 不配的影响 |
|---|---|---|---|---|
| Alpaca | 必需 | `ALPACA_API_KEY_ID` + `ALPACA_API_SECRET_KEY` | 数据源 → Alpaca（格式 `KEY_ID:SECRET`） | 没有日K和交易日历，机会板、回测、纸面交易都无法运行 |
| Finnhub | 建议 | `FINNHUB_API_KEY` | 配置清单内直接填写 | 新闻只剩 Alpaca + SEC；财报日历只有 Nasdaq 单源，Nasdaq 不可用时暂停新开仓；财务兜底与公司图标缺失 |
| SEC EDGAR 联系邮箱 | 建议 | `SEC_USER_AGENT`（如 `ZenTrade you@example.com`） | 配置清单内直接填写 | SEC 要求请求头带联系邮箱；用占位标识可能被限流，财务与公告新闻缺失 |
| AI 模型（全局） | 建议 | `AI_PROVIDER` / `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` | AI 设置 | 新闻只有英文原标题（无利多/利空标签），个股分析、AI 策略、助手不可用；交易环不依赖 AI |
| 新闻翻译模型 | 建议 | — | AI 设置 → 模型库 + AI 模型分工 → 新闻翻译与打标 | 跟随全局模型时占掉大部分 AI 调用量，见第 3 节 |
| TickFlow | 可选 | `TICKFLOW_API_KEY` | 数据源 → TickFlow | 实时行情用 Alpaca IEX（单一交易所，成交量偏小） |
| QVeris | 可选 | `QVERIS_API_KEY` | 数据源 → QVeris | 个股弹窗无期权全链、财报预期、电话会；按积分计费，可设每日上限 |
| Massive / Polygon | 可选 | `MASSIVE_API_KEY` 或 `POLYGON_API_KEY` | — | 证据研究的退市股名单不更新（只影响研究数据，不影响交易） |
| 推送 | 可选 | `ZENTRADE_FEISHU_APP_ID` / `ZENTRADE_FEISHU_APP_SECRET`（飞书应用机器人） | 实时监控 → 推送渠道（飞书群 Webhook / 企业微信 / 自定义 Webhook / 邮件） | 提醒只在页面显示 |
| 访问密码 | 公网必需 | `AUTH_PASSWORD` | 系统设置 → 修改密码 | 仅首次启动生效；之后以页面修改为准 |

免 Key 的公开数据源（Nasdaq 财报日历、CBOE、FRED、ForexFactory、Hyperliquid）无需配置，单个源失败只影响对应卡片。请求统一使用如实标识本项目的 User-Agent（`Mozilla/5.0 (ZenTrade self-hosted)`），不伪装成具体浏览器。部分端点并非官方 API，可用性与使用条款以提供方为准，见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

数据源的能力路由（日K / 分钟K / 实时 / 复权 / 财务各由哪个源提供）在「设置 → 数据源 → 能力路由」切换；接入自有数据源见 [custom-data-source.md](./custom-data-source.md)（YAML 声明）与 [plugin-development.md](./plugin-development.md)（Python 插件）。

## 3. AI 模型选择

AI 按任务分工。先在「设置 → AI 设置 → 模型库」登记要用的模型（可以同时登记多个，例如 DeepSeek 和 Claude，各自填写地址、Key 和模型名；地址或 Key 留空时沿用全局），再到「AI 模型分工」给每个任务选一个：新闻翻译用 DeepSeek，助手对话用 Claude。每个任务还能单独设思考档位。任务也可以不选库模型、直接单独填写，或者都不填，沿用全局。被任务引用的库模型不能删除，要先在分工里改掉。

| 任务 | 调用频率 | 建议 |
|---|---|---|
| 新闻翻译与打标 | **最高**：约 600 条/交易日（峰值超过 1000），每 20 条一批 | 低价、关思考的模型（DeepSeek / Kimi / 通义千问轻量档）。关思考时粗估约 12 万 token/日、260 万 token/月；推理模型开思考会翻数倍，并可能把正文挤空 |
| 盘前大盘情绪 | 每交易日 1 次 | 关思考即可 |
| 个股分析 | 手动触发 | 可用旗舰模型 |
| 策略生成、助手对话 | 手动触发 | 可用旗舰模型 |

AI 不可用时，新闻保持英文原标题，下一轮继续重试打标。可选设置 `NEWS_GOOGLE_TRANSLATE_FALLBACK=1`，让失败批次改用 Google 翻译网页端点只翻译标题（非官方端点、无 SLA，默认关闭）。

## 4. 服务与数据

```ini
HOST=127.0.0.1        # 监听地址 / Docker 主机绑定地址；局域网访问设 0.0.0.0
PORT=3018             # 端口
LOG_LEVEL=INFO        # DEBUG | INFO | WARNING | ERROR
DATA_DIR=./data       # 数据目录（容器内固定为 /app/data）
CORS_ALLOW_ORIGINS=   # 跨域来源白名单（逗号分隔）；留空 = 只允许同源
COOKIE_SECURE=auto    # 登录 Cookie 的 Secure 标志：auto | true | false
NEWS_GOOGLE_TRANSLATE_FALLBACK=0  # 新闻翻译兜底（非官方端点），默认关闭
UPDATE_CHECK_ENABLED=true          # 新版本提醒：服务端每 6 小时查一次上游仓库；离线 / 内网部署可关
UPDATE_CHECK_REPO=wuyangfan234/zentrade  # 自己的 fork 想跟自己的仓库时改这里
BACKEND_EXTRAS=       # legacy-cpu（老 CPU 无 AVX2/FMA）/ backtest（vectorbt，Apache-2.0 + Commons Clause，含销售限制）
USE_CN_MIRROR=0       # Docker 构建：1 = 使用国内 npm / PyPI / Debian 镜像
PLUGIN_PIP_INDEX_URL= # 可选：安装 Python 数据源插件依赖时使用的 PyPI 镜像
```

`COOKIE_SECURE=auto` 时，请求本身是 HTTPS，或直连地址是本机/内网反向代理且带 `X-Forwarded-Proto: https`，登录 Cookie 才会加 `Secure`。公网直连时不采信转发头。

整个 `data/` 目录不纳入 git：行情、台账、规则集、密钥（`user_data/secrets.json`）都在里面。迁移时整体拷贝，备份时注意它含密钥。

访问密码的公网部署步骤与重置方法见 [deployment.md](./deployment.md)。让其他 agent 用密钥或 MCP 接入，见 [api-and-mcp.md](./api-and-mcp.md)。

## 5. 首次部署检查

1. `cp .env.example .env && chmod 600 .env`，至少填 Alpaca 与 `AUTH_PASSWORD`。
2. `docker compose up -d --build`，打开页面完成向导。向导最后一步列出未配置的必需与建议项。
3. 必需项缺失时，各页面顶部常驻红色提示；只缺建议项时提示可在非首页关闭。
4. 在「数据」页同步日K，之后机会板按美东交易日自动运行。
