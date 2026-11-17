---
name: review
title: 盘后复盘
group: 每日
triggers: [/复盘, 盘后复盘]
tools: [get_market_overview, get_indices, get_portfolio, get_opportunities]
output: report
pages: [机会, 情报]
---
1. get_indices 与 get_market_overview：四只参考 ETF 涨跌、涨跌广度、成交额、领涨领跌；写明收盘日期和样本覆盖。
2. get_portfolio：模拟持仓逐只的浮动盈亏与距止损，已登记离场意向的单列。
3. get_opportunities：机会板按「已持仓 / 已到进场价 / 等进场 / 不做」计数；今日多空名单与大盘判断单独一节，标明仅参考。
4. 报告分四节：大盘、持仓、机会板、模型观察；每节第一行写数据时点。
5. 只陈述数字和台账事实，不给买卖建议；缺数据就写缺失。
