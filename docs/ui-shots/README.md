# Todo #10 P1 UI 检查截图

真浏览器渲染 (Playwright + Chromium headless, Vite dev 实挂载), 拦截 `/api/**` 喂虚拟数据。
数据字段形状对齐后端真实返回 ([P1-B2 归因字段契约](../P1-B2-CARD-ATTRIBUTION.md)), 来源
`frontend/src/components/opportunities/__fixtures__/p1scenes.ts`, 用 [tools/dump-scenes.mjs](tools/dump-scenes.mjs) 导成 JSON,
[tools/shoot.py](tools/shoot.py) 截图并自检。全部为虚拟数据, 非实时行情。

## 主截图 (1440×900 桌面 / 390 宽手机)

| 页面 | 桌面 | 手机 |
|---|---|---|
| 机会页 (满: 5 张可下单卡 + 观察级 1 + T1 1, 账本 7 条分五个档位, 归因展开) | [opp-desktop.png](opp-desktop.png) | [opp-mobile.png](opp-mobile.png) |
| 因子页 (满: 在用 3 + 挑战 4, 理由行 7, 闭环展开) | [factor-desktop.png](factor-desktop.png) | [factor-mobile.png](factor-mobile.png) |

## 数据密度

- 稀疏: [机会页单卡](states/opp-sparse-desktop.png) · [机会页 0 候选](states/opp-empty-desktop.png) · [因子页只在用 1 条](states/factor-sparse-desktop.png)
- 异常 (观察级卡 + T1 卡 + 无 regime 降级 + 账本 unavailable):
  [机会页桌面](states/opp-anomaly-desktop.png) · [机会页手机](states/opp-anomaly-mobile.png) ·
  [因子页桌面](states/factor-anomaly-desktop.png) · [因子页手机](states/factor-anomaly-mobile.png)

## 用户指定三处细节: 修前 / 修后

修前 = 基线 `1a57c1c` 同一份数据同一脚本; 用户描述的原状见参考稿 [prototype-factor-desktop.png](compare/prototype-factor-desktop.png)。

| 细节 | 修前 | 修后 |
|---|---|---|
| 10 卡片网格右缘留白 (1920 宽, 2 张卡) | [10-grid-before.png](compare/10-grid-before.png) 2 张卡占 3 列, 右侧空一列 | [10-grid-after.png](compare/10-grid-after.png) `auto-fit` 收掉空列 + 末行最后一张补齐剩余列 |
| 11 95% 区间条区分度 | [11-12-ci-reason-before.png](compare/11-12-ci-reason-before.png) 固定 −0.15~+0.25 全幅, 虚线 0, 无端点数 | [11-12-ci-reason-after.png](compare/11-12-ci-reason-after.png) 刻度按数据收紧 (全表同一把), 0 基准实线贯穿, 跨 0 负段琥珀斜纹, 端点标数值, 最小宽 8px |
| 12 淘汰理由警示度 | 同上 (原页面无理由列) | 同上: 换下 / 待定·不出卡 行浅琥珀底 + 加粗, 触发条件 (如「连续 2 次月度复核 < 0」「区间上限 −0.01R < 0」) 高亮 + 下划线 |

## 自检结果

[check.json](check.json) (12 张) 与 [check-wide.json](check-wide.json) (宽屏 2 张): JS 报错 / 横向溢出 / 新组件文字截断 / 文字重叠 /
对比度 (WCAG AA) / 计数断言 (账本总数与各档、今日机会、观察级、闭环今日卡数 = 实际行/卡数; 网格末行不留白; 同表 0 基准对齐) /
两页 regime 指纹一致 —— 全部 0。

复跑: 前端起 Vite dev (`BACKEND_PORT` 指向不存在的端口即可), 然后
`node docs/ui-shots/tools/dump-scenes.mjs <scenes.json>` (在 frontend 目录) →
`PW_CHROMIUM=<chrome-headless-shell 路径> python3 docs/ui-shots/tools/shoot.py http://127.0.0.1:<端口> <scenes.json> <输出目录>`。
