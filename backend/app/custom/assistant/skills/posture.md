---
name: posture
title: 持仓体检
group: 每日
triggers: [/持仓, 持仓体检]
tools: [get_portfolio, get_earnings, get_news]
output: chat
pages: [持仓]
---
1. get_portfolio 读模拟持仓与离场意向；先用一句话给出持仓只数、已跌破止损或待离场的只数。
2. 用表格列出：代码、股数、成本、收盘、止损、距止损、浮动盈亏；待离场的排最前，其余按距止损从近到远。
3. get_earnings 标出财报日历内有财报的持仓；get_news 只查距止损最近的两只，各列最重要的 1-2 条。
4. 每个数字带数据时点（收盘日、台账登记时刻、新闻发布时间）。
5. 已破止损只陈述台账事实：原因、登记时刻、下一交易日开盘按开盘价模拟成交；不给买卖建议。
