---
name: bt
title: 策略回测报告
group: 策略
triggers: [/回测, 策略回测]
tools: [list_strategies, run_backtest]
output: report
pages: [回测, 策略]
---
1. 用户没指定策略时先 list_strategies，跳过标为不可用的，请用户挑一个或按名称匹配。
2. run_backtest 默认最近 180 天；写明起止日期作为数据时点。
3. 报告列收益、年化、最大回撤、夏普、胜率、盈亏比、交易次数，交易次数少于 30 笔要特别说明样本少。
4. 写明这是日K收盘口径的历史回测，不代表未来表现。
