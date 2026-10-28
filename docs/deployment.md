# 部署指南

ZenTrade 的推荐部署形态是单容器 Docker。应用同时提供 API 和前端静态页面，运行时数据保存在宿主机 `./data`。

## 1. Docker 启动

```bash
cp .env.example .env
chmod 600 .env
```

至少配置 Alpaca Key；需要非本机访问时同时设置强 `AUTH_PASSWORD`。完整配置见 [configuration.md](configuration.md)。

```bash
docker compose up -d --build
docker compose ps
```

打开 <http://localhost:3018>。容器名为 `zentrade`，默认端口为 `3018`。

```bash
# 查看日志
docker compose logs -f app

# 停止
docker compose down
```

`docker compose down` 不会删除宿主机 `./data`。不要随手使用 `git clean -fdx`，它可能删除被 Git 忽略的运行数据。

## 2. 首次初始化

1. 在「设置 → 数据源」检查配置清单和能力路由。
2. 在「数据」页同步日 K，等待指标数据就绪。
3. 添加自选股，再检查机会、回测和系统状态页面。
4. 配置可选的新闻、AI、推送和增强数据源。

核心行情未配置时，相关功能会明确降级；AI 和推送不是核心交易环的启动依赖。

## 3. 更新与备份

更新前备份整个 `data/`；其中包含行情、配置、事件台账和密钥，备份文件也应按敏感资料保护。

```bash
# 在仓库目录更新源码
git pull --ff-only

# 重新构建
docker compose up -d --build
```

重点备份：

- `data/ledger/`：事件台账和净值重放依据。
- `data/rules/`：规则集与风险配置。
- `data/user_data/`：登录状态和用户配置相关文件。
- 自定义策略、数据源 YAML 和其他研究产物。

恢复前先停止容器，并核对目标目录，避免把旧数据覆盖到错误实例。

## 4. 访问密码

`AUTH_PASSWORD` 只在尚未设置密码时用于首次初始化；之后以页面「设置 → 修改密码」保存的值为准。仅修改 `.env` 不会覆盖已有密码。

如果忘记密码：

1. 停止容器。
2. 备份后移除 `data/user_data/auth.json`。
3. 在 `.env` 中设置新的 `AUTH_PASSWORD`。
4. 重启并完成首次初始化。

首次通过页面设置密码仅允许本机或内网请求。远程部署可使用环境变量预置密码，或通过 SSH 端口转发：

```bash
ssh -L 3018:127.0.0.1:3018 user@your-server
```

然后访问 <http://127.0.0.1:3018>。

## 5. 公网部署

- 使用 HTTPS 反向代理或受信隧道，不直接暴露 3018 端口。
- 设置强密码，限制管理入口的访问来源。
- 保持 `.env` 和 `data/` 权限最小化，不提交到 Git。
- 不把实例共享给不受信任的人；自定义策略和扩展数据配置具有服务端执行能力。
- 只运行一个应用进程。进程内研究任务锁不适合多 worker 部署。

详见 [SECURITY.md](../SECURITY.md)。

## 6. 构建选项

默认 Dockerfile 支持中国网络镜像。海外或官方源环境可关闭：

```bash
docker compose build --build-arg USE_CN_MIRROR=0
docker compose up -d
```

老 CPU 缺少 AVX2/FMA 时，在 `.env` 设置：

```ini
BACKEND_EXTRAS=legacy-cpu
```

可选 `backtest` extra 会安装带额外许可限制的 vectorbt；默认不安装。使用前请阅读 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

Compose 中的 Codex 主机目录挂载用于可选的本机 Codex 登录通道。它只读挂载；不使用该功能时，不要向不受信任的容器或镜像提供主机凭据目录。

## 7. 本地开发启动

```bash
cp .env.example .env
./dev.sh
```

Windows 使用 `dev.ps1`。默认后端为 `3018`、前端开发服务器为 `3011`：

```bash
BACKEND_PORT=8000 FRONTEND_PORT=5173 ./dev.sh
```

也可分别启动：

```bash
cd backend
uv sync --extra dev --frozen
uv run --frozen uvicorn app.main:app --reload --port 3018

cd ../frontend
pnpm install --frozen-lockfile
pnpm dev
```

## 8. 故障排查

- 页面功能缺失：先看「设置 → 数据源」的能力清单，再看「系统状态」。
- 数据未更新：检查交易日、供应商权限、数据同步日志和本地磁盘。
- 构建依赖失败：尝试切换 `USE_CN_MIRROR`，不要删除锁文件重新猜版本。
- 忘记密码：按第 4 节操作，不要在公开 issue 中提交 `auth.json`、`.env` 或会话 Cookie。
- 需要报告问题：提供版本、部署方式、复现步骤和脱敏日志，不附带任何 Key 或用户数据。
