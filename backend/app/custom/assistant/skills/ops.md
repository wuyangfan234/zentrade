---
name: ops
title: 助手体检
group: 系统
triggers: [/体检, 助手体检]
tools: [list_data_capabilities, get_indices, get_portfolio, get_market_overview]
output: chat
pages: [系统状态]
---
1. list_data_capabilities 列出每项数据能力是否已配置可用。
2. get_indices 看参考 ETF 是实时还是收盘回退；get_market_overview 看最新交易日与样本覆盖。
3. get_portfolio 看模拟台账是否可读、最近一条台账记录的时刻。
4. 用一张表写每项的状态和数据时点；发现异常写在哪里查看（系统状态页对应的行）。
