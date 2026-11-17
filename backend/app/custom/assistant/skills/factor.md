---
name: factor
title: 因子速查
group: 研究
triggers: [/因子, 因子速查]
tools: [list_factors, get_factor_values]
output: chat
pages: [因子]
---
1. 用户说的是因子中文名或描述时，先 list_factors 找到对应因子。
2. get_factor_values 取前 10 名（用户说「最低」就按升序取），写明数据时点（交易日）、有效股票数和计算方式。
3. 用表格列代码、名称、因子值、当日涨跌幅；解释这个因子衡量什么。
4. 不把因子排名说成买入理由。
