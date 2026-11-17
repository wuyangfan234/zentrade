---
name: earn
title: 财报前检查
group: 研究
triggers: [/财报, 财报前检查]
tools: [get_earnings, get_financials, get_stock_daily, get_portfolio]
output: chat
pages: [自选, 持仓]
---
1. get_earnings（不传代码 = 自选加持仓）列出财报日历内要发财报的股票、日期、盘前或盘后。
2. 对其中的模拟持仓（get_portfolio）单独标出，写当前浮动盈亏和距止损。
3. 每只写历史财报后首日涨跌幅的中位数与最大值，以及最近一次是否超预期。
4. 最多挑 3 只调用 get_financials 看最近一期营收和净利同比。
5. 写明数据时点：财报日历的覆盖区间和更新时刻；不给买卖建议。
