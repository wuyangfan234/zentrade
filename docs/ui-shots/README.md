# Todo #10 P1 UI 检查截图

真浏览器渲染 (Playwright + Chromium headless, Vite dev 实挂载), 拦截 `/api/**` 喂虚拟数据。
数据字段形状对齐后端真实返回 ([P1-B2 归因字段契约](../P1-B2-CARD-ATTRIBUTION.md)), 来源
`frontend/src/components/opportunities/__fixtures__/p1scenes.ts`, 用 [tools/dump-scenes.mjs](tools/dump-scenes.mjs) 导成 JSON,
[tools/shoot.py](tools/shoot.py) 截图并自检。全部为虚拟数据, 非实时行情。

## 交付边界 (如实说明)

- **机会页账本的 95% 区间 = #10 (前端画条) + #11 (后端给数) 合起来交付, 只靠 #10 不生效。**
  - 后端卡 #11 已合入 main (`2faa44e`, `strategy_ledger._cell_view` 输出 `ci_low` / `ci_high`)。
  - 本分支基于 `1a57c1c`, 自身不含 #11; 与 main 试合 (`git merge-tree`) 无冲突。本分支合入 main 后两卡同时在, 账本按区间条显示。
  - 在本分支上单独运行 (不含 #11), 或读到 #11 之前落盘、缺这两个字段的历史旧簿时, 对应行显示「区间未返回」, 条宽不变不错位。
- **账本区间截图为 #11 合并后的预期效果**: [states/ledger-ci-forward-compat.png](states/ledger-ci-forward-compat.png)
  用的是 `__fixtures__/p1scenes.ts` 里字段形状对齐 (`ci_low` / `ci_high`) 的模拟值, 不是真实后端数据。
  主截图的模拟账本数据不带这两个字段 (等同 #11 之前的载荷或历史旧簿), 所以各行写「区间未返回」。
- **冷冻理由**: 账本切片不带「哪条触发冷冻」。只有拿到区间上限且 < 0 时才写「区间上限 X < 0」, 否则写
  「冷冻 · 触发条件未返回 (复核连负 / 区间上限为负 / 延续上月冷冻 之一)」, 不猜具体原因。

## 主截图 (1440×900 桌面 / 390 宽手机)

| 页面 | 桌面 | 手机 |
|---|---|---|
| 机会页 (满: 5 张可下单卡 + 观察级 1 + T1 1, 账本 7 条分五个档位, 归因展开) | [opp-desktop.png](opp-desktop.png) | [opp-mobile.png](opp-mobile.png) |
| 因子页 (满: 在用 3 + 挑战 4, 理由行 7, 槽位 1 与槽位 3 闭环展开) | [factor-desktop.png](factor-desktop.png) | [factor-mobile.png](factor-mobile.png) |

因子页理由行开头写出处「账本·<当前 regime>格 <月份>」, 与左侧「回放」超额列不同源, 不混用。
槽位 3 (策略打分) 闭环写明「不在策略账本 · 口径 = 周度 IC · 打分只给选股结果排序 · 不出机会卡」;
截图数据里打分策略列表为空, 所以展开处显示「暂无打分因子」, 有数据时的渲染由 vitest 用例守着。

## 数据密度

- 稀疏: [机会页单卡](states/opp-sparse-desktop.png) · [机会页 0 候选](states/opp-empty-desktop.png) · [因子页只在用 1 条](states/factor-sparse-desktop.png)
- 异常 (观察级卡 + T1 卡 + 无 regime 降级 + 账本 unavailable):
  [机会页桌面](states/opp-anomaly-desktop.png) · [机会页手机](states/opp-anomaly-mobile.png) ·
  [因子页桌面](states/factor-anomaly-desktop.png) · [因子页手机](states/factor-anomaly-mobile.png)
- 宽屏 1920 满卡: [states/opp-full-wide.png](states/opp-full-wide.png)
- 末行补齐分支 (自检断言网格实际 (列数, 末行张数) 等于期望, 确认分支真跑到):
  - 3 列 4 张 (末行剩 1, 1920 宽): [states/grid-3col-4cards.png](states/grid-3col-4cards.png) 末行一张跨满 3 列
  - 5 列 7 张 (末行剩 2): [states/grid-5col-7cards.png](states/grid-5col-7cards.png) 末行两张跨 3 + 2 列补满。
    机会页容器最大宽 1440px, 真实布局下今日机会网格最多 3 列; 这张是在页面加载前注入 CSS 把卡最小宽度收窄到 180px 模拟出的 5 列,
    只为在真浏览器里跑一遍 (5 列, 末行 2 张) 分支。「5 列 4 张」没有末行可补 (`auto-fit` 直接收成 4 列), 所以用 7 张。

## 用户指定三处细节: 修前 / 修后

修前 = 基线 `1a57c1c` 同一份数据同一脚本。

**细节 10 的出处**: 用户说的「选股槽位只有 2 张卡却占 3 列」出自参考稿 `docs/prototype/factor-next.html`
([prototype-factor-desktop.png](compare/prototype-factor-desktop.png)); **现行因子页没有这种卡片网格**。
同一问题在现行代码里出现在机会页卡片网格 (`Cards.tsx` 的 `GRID`, `auto-fill`), 本卡修的是这里;
因子页新增的闭环卡片小网格也用同一套规则。持仓卡网格与今日机会共用 `GRID`, 也一并换成末行补齐 (不在任务明确范围内, 特此说明)。

| 细节 | 修前 | 修后 |
|---|---|---|
| 10 卡片网格右缘留白 (1920 宽, 2 张卡) | [10-grid-before.png](compare/10-grid-before.png) 2 张卡占 3 列, 右侧空一列 | [10-grid-after.png](compare/10-grid-after.png) `auto-fit` 收掉空列 |
| 10 多行末行 (1920 宽, 3 列 5 张) | — | [10-grid-wide-5cards-after.png](compare/10-grid-wide-5cards-after.png) 末行两张跨 2 + 1 列补满 |

末行规则: 3 列及以下直接补满 (不设跨度上限); 4 列及以上每张最多跨 2 列, 剩余空列为奇数时第一张再加 1 列, 其余空列左右平分居中 ——
末行永远补满或左右对称, 不会出现「右侧空一整列」。
| 11 95% 区间条区分度 | [11-12-ci-reason-before.png](compare/11-12-ci-reason-before.png) 固定 −0.15~+0.25 全幅, 虚线 0, 无端点数 | [11-12-ci-reason-after.png](compare/11-12-ci-reason-after.png) 刻度按数据收紧 (全表同一把), 0 基准实线贯穿, 跨 0 负段琥珀斜纹, 端点标数值, 最小宽 8px |
| 12 淘汰理由警示度 | 同上 (原页面无理由列) | 同上: 换下 / 待定·不出卡 行浅琥珀底 + 加粗, 触发条件 (如「超额 −0.11R ≤ 0」「区间上限 −0.01R < 0」) 高亮 + 下划线 |

## 自检结果

[check.json](check.json) (17 张, 含宽屏、末行补齐分支与区间兼容场景): JS 报错 / 横向溢出 / 新组件文字截断 / 文字重叠 /
对比度 (WCAG AA) / 计数断言 (账本总数与各档、今日机会、观察级、槽位 1 闭环今日卡数、槽位 3 闭环引用策略数 = 实际行/卡数;
网格末行左右空白相差 ≤ 2px, 单卡跨度不超过规则上限 (≤3 列时为列数, ≥4 列时为 3), 末行补齐场景的实际 (列数, 末行张数) 等于期望; 同表 0 基准对齐) / 两页 regime 指纹一致 —— 全部 0。

复跑: 前端起 Vite dev (`BACKEND_PORT` 指向不存在的端口即可), 然后
`node docs/ui-shots/tools/dump-scenes.mjs <scenes.json>` (在 frontend 目录) →
`PW_CHROMIUM=<chrome-headless-shell 路径> python3 docs/ui-shots/tools/shoot.py http://127.0.0.1:<端口> <scenes.json> <输出目录>`。
