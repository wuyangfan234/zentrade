# Todo #10 P1 UI 检查截图

真浏览器渲染 (Playwright + Chromium headless, Vite dev 实挂载), 拦截 `/api/**` 喂虚拟数据。
数据字段形状对齐后端真实返回 ([P1-B2 归因字段契约](../P1-B2-CARD-ATTRIBUTION.md)), 来源
`frontend/src/components/opportunities/__fixtures__/p1scenes.ts`, 用 [tools/dump-scenes.mjs](tools/dump-scenes.mjs) 导成 JSON,
[tools/shoot.py](tools/shoot.py) 截图并自检。全部为虚拟数据, 非实时行情。

## 未交付项 (如实说明)

- **机会页账本的 95% 区间: 真实数据下未交付。** 区间依赖后端透传 `ci_low` / `ci_high`, 现行后端
  `strategy_ledger.ledger_view` 只给 `{n, excess, tier}`; 本卡禁改后端, 已另开后端卡 #11。
  前端已做成向前兼容: 账本格一旦带这两个字段就按全表同一刻度画区间条, 缺值的行写「区间未返回」且条宽不变;
  #11 合并后前端无需再改。模拟 #11 合并后的效果见 [states/ledger-ci-forward-compat.png](states/ledger-ci-forward-compat.png)。
  主截图里账本各行写「区间未返回」, 即现行后端下的真实样子。
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

## 用户指定三处细节: 修前 / 修后

修前 = 基线 `1a57c1c` 同一份数据同一脚本。

**细节 10 的出处**: 用户说的「选股槽位只有 2 张卡却占 3 列」出自参考稿 `docs/prototype/factor-next.html`
([prototype-factor-desktop.png](compare/prototype-factor-desktop.png)); **现行因子页没有这种卡片网格**。
同一问题在现行代码里出现在机会页卡片网格 (`Cards.tsx` 的 `GRID`, `auto-fill`), 本卡修的是这里;
因子页新增的闭环卡片小网格也用同一套规则。持仓卡网格与今日机会共用 `GRID`, 也一并换成末行补齐 (不在任务明确范围内, 特此说明)。

| 细节 | 修前 | 修后 |
|---|---|---|
| 10 卡片网格右缘留白 (1920 宽, 2 张卡) | [10-grid-before.png](compare/10-grid-before.png) 2 张卡占 3 列, 右侧空一列 | [10-grid-after.png](compare/10-grid-after.png) `auto-fit` 收掉空列 |
| 10 多行末行 (1920 宽, 3 列 5 张) | — | [10-grid-wide-5cards-after.png](compare/10-grid-wide-5cards-after.png) 末行卡按剩余列加宽, 每张最多跨 2 列, 仍有空列则居中 |
| 11 95% 区间条区分度 | [11-12-ci-reason-before.png](compare/11-12-ci-reason-before.png) 固定 −0.15~+0.25 全幅, 虚线 0, 无端点数 | [11-12-ci-reason-after.png](compare/11-12-ci-reason-after.png) 刻度按数据收紧 (全表同一把), 0 基准实线贯穿, 跨 0 负段琥珀斜纹, 端点标数值, 最小宽 8px |
| 12 淘汰理由警示度 | 同上 (原页面无理由列) | 同上: 换下 / 待定·不出卡 行浅琥珀底 + 加粗, 触发条件 (如「超额 −0.11R ≤ 0」「区间上限 −0.01R < 0」) 高亮 + 下划线 |

## 自检结果

[check.json](check.json) (15 张, 含宽屏与区间兼容场景): JS 报错 / 横向溢出 / 新组件文字截断 / 文字重叠 /
对比度 (WCAG AA) / 计数断言 (账本总数与各档、今日机会、观察级、槽位 1 闭环今日卡数、槽位 3 闭环引用策略数 = 实际行/卡数;
网格末行左右空白相差 ≤ 2px 且单卡不超过 2 列宽; 同表 0 基准对齐) / 两页 regime 指纹一致 —— 全部 0。

复跑: 前端起 Vite dev (`BACKEND_PORT` 指向不存在的端口即可), 然后
`node docs/ui-shots/tools/dump-scenes.mjs <scenes.json>` (在 frontend 目录) →
`PW_CHROMIUM=<chrome-headless-shell 路径> python3 docs/ui-shots/tools/shoot.py http://127.0.0.1:<端口> <scenes.json> <输出目录>`。
