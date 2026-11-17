---
name: watch
title: 自选巡检
group: 每日
triggers: [/自选, 自选巡检]
tools: [get_watchlist, get_stock_quote, get_earnings]
output: chat
pages: [自选]
---
1. get_watchlist 取自选与最新涨跌幅，按涨跌幅排序，列出最强 5 只与最弱 5 只。
2. 对最强与最弱各 2 只调用 get_stock_quote，补充成交额与振幅。
3. get_earnings 列出自选里财报日历内要发财报的，带日期和盘前/盘后。
4. 开头写数据时点（盘中快照或收盘日）；不给买卖建议。
