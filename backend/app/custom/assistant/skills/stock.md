---
name: stock
title: 个股研究
group: 研究
triggers: [/个股, 个股研究]
tools: [get_stock_quote, get_stock_daily, get_stock_analysis, get_financials, get_news, get_earnings]
output: report
pages: [个股分析]
---
1. 用户没给代码时，用页面上下文里的关注标的；都没有就先问代码。
2. get_stock_quote 与 get_stock_daily 同轮调用，看最新价与近 60 日走势；get_stock_analysis 给关键价位。
3. get_financials 列最近 4 期：营收与净利同比、毛利率、净利率、ROE，写明报告期与公告日；查不到就写没有。
4. get_earnings 写下次财报日期与历史财报后首日涨跌幅中位数；get_news 列近 7 日最重要的 3 条。
5. 报告分五节：走势、关键价位、财务、财报、新闻；每节标数据时点；不给买卖建议。
