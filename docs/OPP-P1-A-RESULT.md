# P1-A 状态与门层：结果与复现

> 范围：方案书 [OPP-FACTOR-NEXT-20261126.md](OPP-FACTOR-NEXT-20261126.md) P1 阶段的「状态与门层」（regime 三态、多尺度方向门、滞后/假翻转指标、空头条件替换）。账本/打分（P1-B）与 UI 另卡。
> 本卡未部署、未动生产、未引入任何影子机制。

## 1. 结论

1. **regime 三态已直接生效**（带开关可回滚）：盘后慢层随 `decision_scan_job` 跑一次并写进当日自选信号快照（只追加 `regime_state` / `regime_as_of` / `regime_reasons` / `regime_contract`）；盘中快层挂在已有的 09:25 + 10:00–15:00 整点大盘重判任务里，没有新定时器。
2. **空头只在「防守」开**，取代「SPY 收盘 < MA50」单条件；过渡期空头卡一律观察级，不进纸面下单簿。`tv_halftrend` 留在默认空头三条里，权重与名单都没动。
3. **`multi_scale_v1` 已注册但默认不生效**（`preferred=False`），只有 `rules/swing.json` 写 `"gate": "multi_scale_v1"` 才启用。
4. **滞后 / 假翻转（44 只自选，2023-01-03 至 2026-10-13）**：多尺度门的假翻转比 `daily_ma50` 少约 20%–24%，停止放行旧方向快 1 天，翻到新方向慢 1 天。门内格子少 18%，每笔 R 0.163 对 0.154，差值落在自助区间内，不显著。
5. **需要拍板的风险**：同一份回放里，按 regime 分组的每笔 R 是「震荡」最高（0.21）、「进攻」次之（0.13）、「防守」最低（0.05）。所以按任务书写的上限「进攻 5 / 震荡 2」与证据方向不一致，防守关多头有证据支持。另外，只用收盘的回放里 regime 平均约每 5 个交易日切换一次。详见 §4。

## 2. 实现与开关

| 项 | 位置 | 说明 |
| --- | --- | --- |
| regime 三态 | `backend/app/opportunity/regime.py` | 慢层：SPY 收盘对 MA50、MA50 的 20 日斜率、自选宽度（收盘 > MA20 占比）。快层：SPY 对 10 日 EMA、前 20 日高低点突破、5/60 日已实现波动比。 |
| 滞回 | `regime.step` | 降级：任一层单次触发即降。升级：慢层同意，或快层同一交易日连续 2 个整点确认。盘后升级不越过同时刻的收盘快层读数。 |
| 状态 / 审计 | `data/rules/regime_state.json`、`data/rules/regime_audit.jsonl` | 审计只追加；每次切换一行，记录时间、触发层、前后状态、触发条件和依据数值。不写结算台账。 |
| 盘后接入 | `decision_scan_job._regime_close` → `watchlist_v4.save_signals(regime=…)` | 快照只追加新键；旧快照没有这些键时仍可读，机会板按 tight-v1 旧口径处理。 |
| 盘中接入 | `daily_pipeline._market_mood_update` → `regime.evaluate_hourly` | 与大盘整点重判同一任务；只在 `market_mood.due_slot` 窗口内执行，同一 slot 只评估一次。没有实时现价时不评估。 |
| 作用面 | `swing_desk.board_for_day` / `compose_boards` | 多头上限：进攻 5、震荡 2、防守 0。空头：只在防守开，上限 5，卡带 `grade: "observe"`。 |
| 观察级不进簿 | `watchlist_book.build_swing_day` | `grade == "observe"` 的卡记 `skipped: observe_only`，不进 `RULE_ID = swing` 的纸面下单簿。 |
| 多尺度门 | `backend/app/opportunity/multi_scale_gate.py` | 慢：MA50；中：MA20 + 5 日斜率；快：4h Phantom 方向。慢中同意 → 放行；慢中冲突且快层与其一同向 → 只放行观察级；其余 → 不放行（fail-closed）。 |
| 指标 | `backend/app/opportunity/gate_metrics.py` | ZigZag、翻转滞后、停放行滞后、20 日内假翻转。 |
| 研究脚本 | `backend/scripts/p1a_gate_lag.py` | 只读输入文件，只写 `--out`。 |

**快层选 4h 而不是 1h**：盘后扫描里 Phantom 4h 来源已经算好自选的 4h 方向（`meta["phantom_4h_state"]`），门直接复用，不重算；研究侧也有现成的 4h K 线（44/46 只自选覆盖）。本机没有 1h 数据，所以 1h 结构没有实现。开源核心版没有 Phantom 插件时，快层视为缺失，慢中冲突的票一律不放行。

**回滚（都只改 `data/rules/swing.json`，不改代码）**

- 方向门：删掉 `"gate"` 字段，或改回原值（`"daily_ma50"` / `"phantom_4h"`）。
- regime：写 `"regime": false`。机会板回到 tight-v1：空头只看 SPY < MA50，每边 5 张，不打观察级。
- 齿轮保存 3+3（`save_strategies`）会保留 `gate` 和 `regime` 两个字段。这次顺带修了一处：修复前保存 3+3 会把 `gate` 字段抹掉。

**现网默认门说明**：私有完整版注册了 `phantom_4h`（`preferred=True`），所以没有配置 `gate` 时生效的是 `phantom_4h`；开源核心版没有该插件，生效的是 `daily_ma50`。`multi_scale_v1` 为 `preferred=False`，加进来后两种版本的默认门都不变（单测锁定）。任务书写的是 `preferred=True`，但如果照写，没有配置 `gate` 的实例会直接换门，违反「默认仍走现行门」，所以这里取 `False`。

**已知边界**

- 盘中快层降级只改 regime 状态与审计，作用到下一次盘后快照（下一张板）。当日已落簿的卡和交易环准入不受影响，本卡不改交易环。
- regime 读不到 SPY 当日收盘时，快照不写 regime 字段，当天按 tight-v1 旧口径处理；空头仍受 SPY < MA50 及数据缺失 fail-closed 约束。

## 3. 滞后与假翻转（一级指标）

- 数据窗口：2023-01-03 至 2026-10-13（终点 = 4h K 线最后一天）。
- 股票池：自选 46 只 ∩ 日K ∩ 4h K = 44 只，共 35,251 个票日。
- 拐点：收盘价 ZigZag。只统计拐点当天门仍在旧方向的拐点（「可测」），已提前对齐的不进分布。
- 翻转滞后：拐点 → 门首次给出新方向的交易日数。停放行滞后：拐点 → 门首次不再放行旧方向。
- 漏翻：到下一个拐点仍未翻。假翻转：翻到新方向后 20 个交易日内又回到旧方向的次数，按全部拐点数平均。

| 门 | ZigZag | 拐点 | 可测 | 翻转滞后 中位 / P80 | 停放行滞后 中位 / P80 | 漏翻 | 假翻转 / 拐点 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| daily_ma50（现行内置） | 7% | 3359 | 2358 | 4 / 9 | 4 / 9 | 1174 | 0.402 |
| ma20_single（A6-5 对照） | 7% | 3359 | 2758 | 3 / 6 | 3 / 6 | 928 | 0.744 |
| multi_scale_v1 | 7% | 3359 | 2270 | 5 / 10 | 3 / 6 | 1172 | **0.307** |
| phantom_4h（私有版现行，参照） | 7% | 3359 | 2364 | 4 / 6 | 2 / 3 | 864 | 0.625 |
| daily_ma50（现行内置） | 10% | 2189 | 1642 | 5 / 12 | 5 / 12 | 640 | 0.520 |
| ma20_single（A6-5 对照） | 10% | 2189 | 1914 | 4 / 7 | 4 / 7 | 453 | 0.913 |
| multi_scale_v1 | 10% | 2189 | 1615 | 6 / 11 | 4 / 7 | 629 | **0.415** |
| phantom_4h（私有版现行，参照） | 10% | 2189 | 1663 | 4 / 6 | 2 / 3 | 421 | 0.776 |

每票每年翻转次数：daily_ma50 16.4、ma20_single 27.9、multi_scale_v1 23.5、phantom_4h 40.0。

**读法**：MA20 单尺度翻得最早，但假翻转几乎翻倍。多尺度门假翻转最少；它比 MA50 早一天停止放行旧方向（慢中一冲突就转为观察或不放行），但确认新方向要等慢中同意，所以翻转滞后多一天。单看日线门，A4-6 的「滞后中位数缩短」只在停放行口径上成立，翻转口径不成立。

## 4. 现行门 vs 多尺度门：卡量、成交、每笔 R

**口径**：门内全部做多格子（同日同侧、同一张紧卡，与 S1 `swing_rule_history.json` 的基线「same-day same-side gated cells, same tight card」同一定义），用日K近似结算：

- 入场区 [c−0.50A, c−0.25A] 限价，2 日入场窗；
- 止损 = 区上沿 −1.0A，目标 = 区上沿 +2.0A，持有 5 日；
- 同一天同时触及止损与目标按止损计（保守）；跳空穿越按开盘价成交。

这不是 `swing-settle-v3`（该合同用 30 分钟K，本机没有），也没有经过 3+3 命中筛选，所以绝对水平不能和生产对比，只能做门与门之间的相对比较。每笔 R 的 95% 区间按周聚类自助法计算（`rank_lab.week_boot`）。

| 门 | 门内格子（卡量） | 成交 | 成交率 | 每笔 R | 95% 区间（周聚类） |
| --- | --- | --- | --- | --- | --- |
| daily_ma50 | 21,210 | 14,859 | 70.1% | 0.154 | [0.080, 0.222] |
| ma20_single | 20,404 | 14,324 | 70.2% | 0.151 | [0.078, 0.227] |
| multi_scale_v1（含观察级） | 17,395 | 12,208 | 70.2% | 0.163 | [0.087, 0.235] |
| multi_scale_v1（仅正常级，进下单簿的部分） | 15,664 | 10,976 | 70.1% | 0.169 | [0.093, 0.237] |
| phantom_4h | 12,365 | 8,784 | 71.0% | 0.161 | [0.086, 0.242] |

**生产回放锚点（S2 `swing_base_rates.json`，只读引用，未重算）**：

- 窗口：2023-01-03 至 2026-10-30，现行 3+3 + 4h 门，结算合同 swing-settle-v3；
- 多头：卡 2,594 张，成交 1,626 笔，每笔 R 0.295 [0.027, 0.519]；
- 空头：卡 357 张，成交 240 笔，每笔 R 0.117 [−0.493, 0.698]。

S1/S2 只存了汇总数，没有逐卡明细，没法在同一批卡上换门重算。所以门与门的对比只能用上表这种同口径近似；接入 3+3 后的逐卡对比，要等能拿到 30 分钟K的环境用 `rebuild_swing_history.py` 重跑。

**regime 回放（只用收盘：慢层 + 收盘快层；盘中整点没有历史数据，不回放）**：

- 948 个交易日里，进攻 404 天、震荡 349 天、防守 195 天，切换 190 次。
- 门内成交按信号日 regime 分组的每笔 R：

| 门 | 进攻 | 震荡 | 防守 |
| --- | --- | --- | --- |
| daily_ma50 | 0.133（n=7,928） | 0.213（n=5,446） | 0.046（n=1,485） |
| multi_scale_v1 | 0.132（n=7,067） | 0.235（n=4,195） | 0.075（n=946） |

防守态关多头与数据方向一致。「震荡只出 2 张」与数据方向相反：震荡态每笔 R 最高，和以前「日线大盘状态描述的是已经涨过，对一周持有反而不利」的结论吻合。上限是任务书规定值，本卡照写；要改的话，下一张卡可以把上限做成 `swing.json` 配置项，不改代码。

## 5. 复现

数据（只读，均为已有研究快照，不写 NAS）：

| 文件 | 内容 | sha256 |
| --- | --- | --- |
| `~/.hermes/workspace/zentrade-lsrank-1105/snap/zt_daily.parquet` | 397 只复权日K（含 SPY），2021-01-04 至 2026-11-04 | `e5fbab94…626313` |
| `~/.hermes/workspace/zentrade-mlst-1019/data/bars4h.parquet` | 67 只 4h K，2021-09-01 至 2026-10-13 | `4c80a299…b4b3c8` |
| `~/.hermes/workspace/zentrade-chananchor-1120/out/daily_wl.parquet` | 自选 46 只名单（取 symbol 列） | `81ef2c44…bd5912` |

工作树 `data/kline_daily` 为空，以上文件是本机已有的数据快照。重新拉数据：用项目自带的隔离回填 `backend/scripts/backfill_daily_isolated.py --out <隔离目录>`，不要指向生产数据目录。

```bash
cd backend
env -u PYTHONPATH -u VIRTUAL_ENV .venv/bin/python -m scripts.p1a_gate_lag \
  --daily ~/.hermes/workspace/zentrade-lsrank-1105/snap/zt_daily.parquet \
  --bars4h ~/.hermes/workspace/zentrade-mlst-1019/data/bars4h.parquet \
  --universe ~/.hermes/workspace/zentrade-chananchor-1120/out/daily_wl.parquet \
  --start 2023-01-03 --out /tmp/p1a_gate_lag.json
```

单机耗时约 12 秒，结果确定（无随机种子差异；自助区间固定 seed=7）。
