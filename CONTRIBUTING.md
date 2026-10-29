# 贡献指南

感谢参与 ZenTrade。一个可合并的改动应当聚焦、可复现、可审查，并保持金融数据、失败语义和用户数据边界清晰。参与项目即表示同意遵守 [行为准则](CODE_OF_CONDUCT.md)。

## 1. 开发环境

- Python 3.11+
- Node.js 20+
- pnpm 9
- uv
- Docker（用于容器验证）

```bash
git clone https://github.com/wuyangfan234/zentrade.git
cd zentrade

cd backend
uv sync --extra dev --frozen

cd ../frontend
pnpm install --frozen-lockfile
```

不要提交 `.venv`、`node_modules`、构建产物或 `data/`。

可选：安装 [pre-commit](https://pre-commit.com/) 钩子，在提交前自动检查合并冲突标记、私钥、密钥泄露和未定义名称：

```bash
pipx install pre-commit
pre-commit install
```

仓库根目录的 `.editorconfig` 约定 UTF-8、LF 换行和缩进；Node.js 版本见 `.nvmrc`。

## 2. 工作流程

1. 从最新 `main` 创建聚焦的分支。
2. 先阅读真实调用链、相邻实现和现有测试，再确定最小改动。
3. 新功能覆盖正常路径、关键边界和失败路径；bug 修复先添加能复现问题的测试。
4. 更新相关用户文档、配置示例和第三方声明。
5. 执行适用的测试与构建，检查最终 diff。
6. 提交 Pull Request，填写动机、行为变化、兼容性、验证结果和剩余风险。

提交信息推荐使用 Conventional Commits，例如 `feat: add provider contract test`、`fix: align minute bar timezone`。

## 3. 验证命令

后端：

```bash
cd backend
uv run --frozen pytest tests -q --tb=short -p no:cacheprovider
uvx ruff check app --select F821 --no-cache --isolated
```

前端：

```bash
cd frontend
pnpm exec tsc -b
pnpm test
pnpm build
```

文档与最终检查：

```bash
python3 scripts/check_docs.py
git diff --check
```

也可在依赖安装完成后一次运行：

```bash
bash scripts/gate.sh
```

根据改动范围增加 API、数据源、并发、缓存、响应式界面或 Docker 验证。无法执行的检查必须在 PR 中明确说明，不能把未运行的验证写成已通过。

## 4. 金融数据契约

### 时间与交易日

- 美股业务时间统一使用 `America/New_York` 和 `app.market_clock.us_clock`。
- 分钟 K 使用美东墙钟 naive 时间，常规时段为 `[09:30, 16:00)`。
- 交易日窗口不得用自然日替代；持有期按实际交易日计算。
- 信号时间、可成交时间和使用价格必须分开定义，禁止未来函数。

### 单位与价格

- 成交量单位是股；成交额单位是美元。
- 比例字段是小数制还是百分数值必须显式标注，不用数值大小猜测。
- 原始价、前复权价和实时成交价不得混用。
- 缺失数据使用空值或明确错误，不填 0 伪装完整。

### 研究、纸面与真实结果

- 研究观察、信号命中、纸面成交和实际收益是不同状态。
- 回测应说明样本区间、成本、成交假设和数据缺口。
- 不用单日结果或同一数据的重复回放宽证据门槛。
- 样本内选择后的结果不能再次冒充未见样本外验证。

## 5. 代码边界

- 通用数据访问经过 provider 能力路由，不在页面或策略中硬编码供应商响应。
- 新能力先定义输入、输出、单位、空数据和失败语义。
- 写入数据时同时检查持久化、内存缓存、版本/generation、SSE 和前端查询失效。
- 不在持锁期间执行网络请求或全量重算；取消、超时和退出路径释放资源。
- 保持历史配置和用户数据可读；破坏性变化需要迁移与回滚说明。
- 不做与当前需求无关的全仓格式化、重命名或重构。

架构入口见 [docs/architecture.md](docs/architecture.md)，扩展开发见 [docs/secondary-development.md](docs/secondary-development.md)。

## 6. 安全与用户数据

- 不提交 Key、Token、Cookie、Webhook、`.env`、`auth.json`、真实账户或私有地址。
- 日志、测试夹具和截图必须脱敏；占位密钥使用明显的 `placeholder` 或 `example`。
- 不把自定义策略 AST 校验当作安全沙箱，不扩大未授权代码执行面。
- 文件删除、覆盖、迁移和远程拉取必须限制目标范围并提供失败路径。
- 引入第三方代码、数据或许可证前，更新 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；根目录与 `backend/` 中用于 Wheel 的许可副本必须同步，`scripts/check_docs.py` 会逐字节校验。

安全问题不要直接发公开 issue，按 [SECURITY.md](SECURITY.md) 报告。

## 7. 前端与文档

- API 类型、调用封装和查询键保持集中，不复制平行客户端。
- 新交互覆盖加载、空数据、错误、禁用和无权限状态。
- 可见改动检查深色/浅色主题及窄屏布局，并附脱敏截图或录屏。
- README 和文档中的命令、相对链接、配置名和代码路径必须与当前仓库一致。
- 不写难以持续维护的固定测试数、策略数或未经验证的性能结论。

## 8. Pull Request 自检

- [ ] 改动只解决一个明确问题。
- [ ] 已核对时间、单位、复权、资产类型和成交口径。
- [ ] 已覆盖失败路径、兼容性和相关缓存。
- [ ] 已执行并记录适用的测试、构建和文档检查。
- [ ] 最终 diff 不包含敏感数据、本机路径、调试输出或生成文件。
- [ ] 第三方归属和许可仍然准确。
