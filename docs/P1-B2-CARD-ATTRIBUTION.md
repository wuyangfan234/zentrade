# P1-B2 机会卡归因字段契约 + T1 状态切换卡 + 账本不可用 fail-closed

适用代码：`backend/app/services/swing_desk.py`（`_card` / `compose_boards` / `board_for_day` / `t1_plan` / `mark_headwind` / `build_live`）、
`backend/app/opportunity/strategy_ledger.py`（`ledger_for` / `lookup`）。全部直接生效，不设影子结算或双轨计算。

## 1. 卡归因五字段（只追加，旧键不变）

`board_for_day` / `compose_boards` 产出的每张板卡都带下面五个键。它们出现在机会页 `/api` 板载荷（`build` / `build_live`），
也会进入 `watchlist_book.build_swing_day` 读到的同一张板卡。`card_lineage` 侧按「板卡 dict → 键名」读取，类型以本表为准。

| 字段 | 类型 | 可空 | 语义 |
|---|---|---|---|
| `regime_state` | `str` ∈ {`进攻`, `震荡`, `防守`} | 是：regime 回滚开关关（`swing.json "regime": false`）或旧快照没有 regime 时为 `null` | 出卡那一刻生效的市场状态 = `regime_for_snapshot` 的 `state`（盘后快照与盘中整点取更防守者，只降不升） |
| `gate_id` | `str`，格式 `<方向门 id>+4h.<event\|state\|none>@tight-v1` | 否 | 放行这张卡的门及版本。方向门 id 取 `direction_gate.active(data_dir).id`（如 `multi_scale_v1`、`daily_ma50`），读不到写 `unknown`；`4h.event` = 4h 窗口内条件齐备，`4h.state` = 4h 方向同向；`@tight-v1` = 门与紧几何所属合同版本 |
| `ledger_tier` | `str` ∈ {`active`, `observe`} | 是：只有 `rank_*` 排序因子命中时为 `null` | 卡的账本档位（P1-B1 已有，本卡未改）：参与打分的非 rank 规则中有在用/复活档 → `active`；全是观察档 → `observe`（只上板，不进纸面下单簿） |
| `score_components` | `object` `{"rules": [...], "k": number, "total": number}` | 否 | 卡分逐规则明细。`rules` 每个元素：`{"strategy": str, "n": int, "excess": number\|null, "shrunk": number, "tier": str}`，`tier` ∈ {`active`, `observe`, `revived`, `rank`}（`rank` = 排序因子，分数恒 0）；`shrunk = excess × n / (n + k)`（四位小数）；`total` = 卡上 `score`（等于各 `shrunk` 之和，四舍五入误差 ≤ 1e-4）。只列参与打分的规则，待定/冷冻档不出现 |
| `timing_tier` | `str` ∈ {`T0`, `T1`} | 否 | 出卡时机：`T0` = 盘后快照出卡；`T1` = 盘中整点 regime 切换后新开方向的卡（见第 2 节）。`T2` 已被产品决策删除，不会出现 |

T1 卡另带（只追加）：`signal_close`（上一收盘，float）、`t1`（`{"day", "valid_until", "from", "to", "switched_at", "slot"}`）。
板 `meta` 在有切换时另带 `t1`（`sides` / `live_n` 等）。

## 2. T1 状态切换卡

- **触发**：盘中整点重判把 regime 降了一档以上（`regime_for_snapshot` 的 `source == "intraday"`），并且某个方向在快照态下上限为 0、切换后 > 0（`t1_plan`）。按现行上限（进攻/震荡/防守 多头 5/5/0、空头 0/0/5），实际只有「切到防守 → 空头新开」这一种情况。
- **几何**：公式不改（`[c−0.50A, c−0.25A]`、止损 = 区上沿 −1.0A、目标 = 区上沿 +2.0A，空头镜像；`ENTRY_DAYS=2`、`HOLD_DAYS=5`），只把 `c` 从信号日收盘换成现价。现价取 `_live_prices`（热报价 → 当日快照 → 盘前），某只票取不到现价就不出卡（`skipped` 原因 `t1_no_live_price`）。
- **有效期**：切换当日（快照日的下一个交易日）+ 次日，与 `ENTRY_DAYS=2` 是同一个窗口。
- **等级**：T1 卡一律观察级（`grade="observe"`），纸面下单簿不收；机会页把它显示为待回踩、`blocked="observe_only"`（交易环不挂单）。空头仍只在防守态开放。
- **读点**：只有 `build_live`（机会页实时板）给 `board_for_day` 传 `live_prices`。落簿、对账、推送不传，这些路径在切换期间不出新开方向的卡。
- **旧方向「逆风」**：切换后上限为 0 的方向上还没成交的卡（`pending` / `in_zone`，不含 T1 卡）在 `build_live` 中标 `headwind=true`，并在同组内排到后面。不撕单，不改写已落盘的簿卡。

## 3. 账本不可用 → fail-closed

`strategy_ledger.ledger_for` 不再返回 `None`，统一返回一个切片，带 `status` 字段：

| status | 场景 | 效果 |
|---|---|---|
| `ok` | 该月有复核记录（当月第一次出板时会写入审计） | 按档位过滤 + 收缩超额打分 |
| `disabled` | 回滚开关 `swing.json "ledger": false` | 不按档位过滤（档位一律当在用，分数取产物汇总），等同 P1-B1 开关关时的行为 |
| `unavailable` | 查历史或未来月份，且该月没有复核记录 | **所有规则（含 `rank_*`）按待定处理**：不打分、不出卡、不进簿；`skipped` 原因 `ledger_inactive` |

新簿冻结的总是这个切片，不会再冻结 `None`。`None` 只会出现在 P1-B1 之前落盘的旧簿上（保持它们当初的出卡结果，对账照旧跳过板层）。

## 4. 默认 3+3（`DEFAULT_LONG` / `DEFAULT_SHORT`）

规则：默认值只收「按账本过 A4 门槛（n ≥ 30 且 CI 下限 > 0），且该结论在留出段（2025-07-01 起）成立」的规则；账本判待定/冷冻的规则不得进默认值。

- 账本唯一来源 `backend/config/swing_rule_history.json`（tight-v1，窗口 2023-01-03 ~ 2026-10-30）只有全窗口汇总，没有按留出段拆分，所以任何多头规则的留出段结论都无法验证。评估协议（[OPP-FACTOR-NEXT-20261126.md](OPP-FACTOR-NEXT-20261126.md) A6-0 第 3、8 条）又禁止用全窗超额选规则。
- 全窗口里唯一过 A4 的多头是 `tv_zlsma_cross`（n=601，超额 +0.127，CI [+0.001, +0.252]），但没有留出段证据 → **`DEFAULT_LONG = ()`（不出多头卡）**。
- 旧默认 `tv_smc_structure` / `volume_price_surge` / `macd_golden` 在 tight-v1 下超额分别为 −0.148 / −0.203 / −0.112，账本全判待定，已删除；旧核心版补位（量价齐升 / MACD 金叉 / trend_breakout，全是待定）同时删除。
- `DEFAULT_SHORT = ("tv_halftrend",)`：它是全库 39 条中唯一通过 BH q=0.10 的规则（n=164，超额 +0.249，CI [+0.147, +0.352]，账本判在用），优先级不低于原来，并排第一。吊灯（CI 跨 0，观察档）和 SMC 结构（n=29，待定档）不过 A4，已移出。
- 生产环境用 `data/rules/swing.json` 配置 3+3，不读默认值，所以生产出卡不受这里影响。默认值只作用于没有 `swing.json` 的新部署和开源用户。

## 5. 回滚

- 账本过滤：`swing.json "ledger": false`（status=disabled）。
- regime / T1：`swing.json "regime": false` → `regime_for_snapshot` 返回 None，不会触发 T1，也不会标逆风。
- 彻底回滚：`git revert` 本卡的提交。
