# API 密钥与 MCP 接入

ZenTrade 可以把自己的全部功能开放给其他 agent 或脚本：Claude Desktop、Claude Code、Cursor 这类支持 MCP 的客户端，以及任何能发 HTTP 请求的程序。

## 1. 生成密钥

设置 → **API 与 MCP** → 填名称、选权限和有效期 → 生成密钥。

- 明文密钥（`zt_` 开头）**只在生成后显示一次**。服务端只保存它的 SHA-256 摘要（`data/user_data/api_keys.json`，权限 0600），丢了只能吊销后重新生成。
- 列表只显示前缀、创建时间、最近使用时间和过期时间；吊销立即生效。

| 权限 | 能做什么 |
|---|---|
| 完全控制 | 与网页登录后相同：回测，新建 / 编辑 / 删除策略，自选增删，规则集，纸面交易设置，数据源配置…… |
| 只读 | 所有 GET 查询，加上不改数据的计算：策略回测、因子检验、选股、策略代码校验、批量取 K 线 |

两档都**不能**访问 `/api/auth/*`（登录、改密码）和 `/api/settings/api-keys*`（生成、查看、吊销密钥）。这两类只认网页登录会话，所以一把泄露的密钥既不能给自己续命，也不能把主人锁在门外。

## 2. 用法

所有 `/api/*` 接口都认下面两种请求头，任选其一：

```bash
curl -s https://your-host/api/watchlist -H "Authorization: Bearer zt_xxx"
curl -s https://your-host/api/watchlist -H "X-API-Key: zt_xxx"
```

规则：请求一旦带了密钥头，就只按密钥判定，不再看登录 Cookie。密钥无效、已吊销或已过期返回 401；权限不够返回 403，并附中文原因。

全部接口和参数在 `/openapi.json`。

## 3. MCP

- 地址：`https://your-host/api/mcp`
- 传输：Streamable HTTP（无状态，只用 POST，不推送 SSE）
- 认证：`Authorization: Bearer zt_xxx`

Claude Code：

```bash
claude mcp add --transport http zentrade https://your-host/api/mcp --header "Authorization: Bearer zt_xxx"
```

Claude Desktop / Cursor 等用 `mcpServers` 配置的客户端：

```json
{
  "mcpServers": {
    "zentrade": {
      "type": "http",
      "url": "https://your-host/api/mcp",
      "headers": { "Authorization": "Bearer zt_xxx" }
    }
  }
}
```

设置页「接入 MCP」会自动用当前站点地址和刚生成的密钥填好这两段，复制即可。

### 工具

| 工具 | 作用 |
|---|---|
| `watchlist_list` / `watchlist_add` / `watchlist_remove` / `watchlist_groups` | 自选与分组 |
| `strategy_list` / `strategy_get_source` | 策略摘要与源码 |
| `strategy_validate` / `strategy_save` / `strategy_save_params` / `strategy_delete` | 校验、新建 / 修改（`custom_` 开头的 ID）、参数覆盖、删除自定义策略 |
| `strategy_screen` | 用策略对最新收盘数据选股 |
| `backtest_strategy` | 策略回测（日 K，已收盘数据） |
| `opportunities_page` / `portfolio_overview` / `kline_daily` | 机会板、纸面账户、日 K |
| `zentrade_endpoints` | 按关键词列出全部 HTTP 接口 |
| `zentrade_api` | 调用任意 `/api` 接口：规则集、因子、挖掘、数据源、推送……全系统操作的通用入口 |

工具执行的是本服务自己的 `/api` 路由，并且用同一把密钥，所以权限只在一处判定，行为也和网页按钮完全一致。只读密钥调用写操作的工具时，返回的是工具错误（`isError: true`，内容为 403 原因），不会中断 MCP 会话。结果超过 6 万字符时会截断，截断后仍是合法 JSON（`truncated: true`）。

## 4. 安全建议

- ZenTrade 是单用户自托管应用。「完全控制」密钥等同于你的登录权限，包括写自定义 Python 策略（AST 校验不是安全沙箱）。只发给你信任的 agent。
- 经公网隧道开放时，优先发「只读」密钥，并设置有效期。
- 每个 agent 各用一把密钥，便于按「最近使用」排查，也便于单独吊销。
