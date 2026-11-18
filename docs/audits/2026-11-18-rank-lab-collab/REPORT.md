# ZenTrade × oasis-rank 排序因子协作链路检查（ZenTrade 侧）

> 内部审计材料：本目录含生产只读快照与研究库统计，**不要提交或推送到公开仓库**。
> 日期：2026-11-18（美东）/ 11-19（北京）。方式：只读读码 + 生产只读快照 + 隔离临时目录复现。未改生产树、NAS、台账、3+3、策略许可与交易参数；未合并、未推送、未部署。

## 一、结论

1. **链路主干是通的。** 生产上 5 条已登记的排序因子（oasis-rank 导入 3 条、ZenTrade 公式挖掘 2 条）全部有复核结果，对账（parity，导入时两边用同一份候选池逐位比成绩）5/5 一致，分段口径两边相同（挖掘段止 2025-06-30、留出段起 2025-07-01）。打分代码三处逐字节一致：生产提交、NAS 源码目录（oasis-rank 只读挂载的那份）、运行中的 ZenTrade 容器；oasis-rank 记录的打分指纹 `0c8f0e22f498` 与按生产提交现算的指纹一致；两边容器的 numpy / polars / pyarrow 版本一致。
2. **没有会让钱、台账或下单出错的问题（无 P1）。** `rankers.json`（登记表）本身不存在并发丢写：唯一写入方是 `rank_lab`，所有读改写都在同一把跨进程文件锁里、原子替换；oasis-rank 那边是只读挂载。
3. **发现 7 条问题，全部已在隔离环境复现**（生产代码 `d14a2a9` 与本工作树 `7ce72ca` 各跑一遍，均 7 passed）：

| 编号 | 一句话 | 类别 | 严重度 |
|---|---|---|---|
| F3 | “导入 ≠ 启用”已不成立：自动选策略可以把外部导入的排序因子直接换进做多 3 条，不经齿轮勾选 | 合同口径不一致，需拍板 | 中（口径） |
| F1 | 夜间整份重算写回时，会抹掉计算期间新导入那条的复核结果 | 真缺陷（锁外读改写） | 低 P3 |
| F2 | 候选池三件套没有“整代”原子性，读者也不核 `pool_sha`；读到“新池 + 旧 meta”时不报错，还会记下一条假的“对账不一致” | 真缺陷（多文件非原子） | 低 P3 |
| F4 | 公式挖掘在搜索窗口内与 oasis-rank 撞同一条公式时，会把出处改回自己，之后还可能把它退席移除 | 防御缺口 + 归属口径待定 | 低 P3 |
| F6 | 已移除的排序因子被再次导入时静默复活，接口不提示 | 口径待定 | 低 P3 |
| F5 | 排序因子移除后，历史卡、回看页、齿轮预览里的名字回落成 `rank_xxxxxxxxxx` 编号 | 展示缺陷 | 低 P3 |
| F7 | 发布脚本先改 NAS 源码目录、后构建镜像，失败不回滚；这段时间 oasis-rank 读到的代码和生产实际运行的不是同一份 | 运维耦合 | 低 P3 |

4. **生产现状下这些问题都还没造成实际后果**：F1/F2 的窗口每个交易日只有约 1 秒 / 约 36 毫秒；F3 的自动换入首个评定日是 11-20（周五），但 3 条外部排序因子的前向样本只有 3–4 个有成交信号日（门槛 20），按每个交易日 +1 推算最早约 12 月中旬才可能达标（推算）；F4 目前双方 0 条撞车。

## 二、基线与版本

| 对象 | 版本 | 证据 |
|---|---|---|
| 本工作树（开源仓） | `7ce72ca`，0.57.0 | `git log -1`，`VERSION` |
| ZenTrade 生产（NAS） | `d14a2a9`（生产树标签 `deploy/2026-11-19-d14a2a9`），2026-11-18 13:40 ET 启动 | `.deployed-commit`、镜像 `org.opencontainers.image.revision`、容器启动时间三处一致 |
| oasis-rank 生产（NAS） | `bedf903`（v5.7），12:39 ET 启动 | `.deployed-commit`；`oasis/{engine,web,schedule,confirm,cache}.py` 在 Mac 工作仓、NAS `src/`、运行容器三处 sha256 一致 |

- Spec 里写“线上 `a30d863`”已过时：生产在 11-18 13:40 ET 又发了 `d14a2a9`。`a30d863..d14a2a9` 只有界面改动（`feda026`），链路文件未变。
- 本工作树（开源）与生产（生产树）不是同一份源码：`formula_miner / rule_auto_select / rule_forward / dsl / registry / fs_utils / strategy_panel` 逐字节相同；`rank_lab / factor_lab / worker / daily_pipeline / api_keys` 只差注释与因子库分组名（生产“绿洲排序”/开源“排序因子”），**行号相同**；`swing_candidates` 的 4h 门实现不同（生产直连 Phantom 4h，开源走方向门插件），`decision_scan_job / swing_desk / opportunities` 有可选插件改造，**行号有偏移**。下文 `文件:行` 按生产 `d14a2a9`，偏移处括注开源行号。
- 生产 12 个链路文件三处逐字节一致：见 `evidence/code_identity_prod.txt`。

## 三、链路逐段核对

### 3.1 导出（ZenTrade 写 `research/rank_lab/`）

- 调度：交易日 17:40 ET `zen_rank_lab`（`backend/app/jobs/daily_pipeline.py:1479-1500`）→ `rank_lab.run_nightly`（`backend/app/services/rank_lab.py:922-942`，09:00–17:00 ET 不跑）→ 子进程 `worker kind=rank_lab`（`backend/app/backtest/worker.py:159`、`:255`）→ `build_pool` 写三件套 + `evaluate_and_store` 全量复核（`rank_lab.py:905-912`）→ 同一任务里接着跑公式挖掘（`daily_pipeline.py:1490-1495`，顺序执行，不并发）。
- 生产 11-17 那次导出（只读快照 `evidence/prod_state_readonly_20261118.json`）：`contract=rank-lab-v2`，`pool_sha=63623aea1adae7b5`，`data_end=2026-11-16`，972 个交易日 × 47 只，13,870 格、成交 8,702 格，`split` 为现行分段。三件套 mtime：`pool.parquet` 22:41:57.523Z → `panel.parquet` .545Z → `meta.json` .559Z（相差 36 ms）。
- 问题：F2（三件套非整代原子）。

### 3.2 导入（oasis-rank → `POST /api/factor-lab/rankers/import`）

- 权限：研究投递密钥只能 GET 和这一个 POST（`backend/app/services/api_keys.py:52-54`、`:181-194`）；删除、重导、立即挖掘都不行。已有测试 `tests/test_rank_lab.py::test_research_scope_permissions` 覆盖。
- 处理：`import_ranker`（`rank_lab.py:640-678`）同公式同方向幂等（id = `rank_` + 指纹前 10 位）→ 写因子库草稿 → 用当前池复核 → `parity` 对账（`rank_lab.py:681-714`，先比分段再逐位比数字）→ API 层再补算最近一份信号表（`backend/app/api/factor_lab.py:310-324`、`rank_lab.py:853-883`）。oasis-rank 送来的 `source.confirm`、`members` 不在白名单里会被丢掉（`rank_lab.py:651-653`），oasis-rank 自己留底，无影响。
- 生产：oasis-rank 账本 `imports.json` 3 条，与 ZenTrade 登记一一对应、出处仍是 oasis-rank、均未移除；5 条登记 parity 全部 `checked=true, match=true, split_checked=true`。
- 问题：F1（复核结果被夜间写回抹掉）、F6（移除后重导静默复活）。

### 3.3 启用（进做多 3 条 → 出卡）

- 人工路径：齿轮保存 `POST /api/opportunities/settings`，写口核对已导入的排序因子算已知规则（`backend/app/api/opportunities.py:482`，开源 `:484`）；排序因子只进做多列表（`opportunities.py:638-646`，开源 `:640-648`）。每个信号日用下单簿冻结的那套 3+3（`swing_desk.py:410` `rules_for_day`，开源 `:439`）；盘后扫描把每条排序因子的当日值写进信号表 `rank_values`（`decision_scan_job.py:367-376`，开源 `:416-425`）；`compose_boards` 在“4h 门同向 + 能出紧几何卡”的自选里取前 5（`swing_desk.py:341`，开源 `:370`；`rank_lab.py:886-900`）。
- 生产：做多 3 条 = `ibs_mean_reversion / tv_zlsma_cross / rank_59ae3b44ef`（oasis-rank 导入的那条，`swing.json` 于 11-16 09:16 ET 写入）。11-17 信号表有 5 条排序值，其中公式挖掘的 2 条是 11-18 04:53 ET 补算的；该日格子卡 01:58 ET 已落盘、早于补算，所以两条新候补的前向记分从 11-18 信号日起算，符合“已记过不改写”的设计。
- 自动路径：见 F3。展示问题：F5。

### 3.4 并存（三个登记方：外部导入、公式挖掘、人工移除）

- 写入方：只有 `rank_lab` 的 `import_ranker / remove_ranker / evaluate_and_store` 写 `rankers.json`、`results.json`（全仓 rg 无其他写入点），都走 `_locked`（线程锁 + `fcntl.flock`，`rank_lab.py:95-114`）+ 原子替换；oasis-rank 两个容器对 `rank_lab` 目录 `rw=false`。
- 公式挖掘（`formula_miner.py`）只认 `source.system=zentrade-miner` 的候补；外部导入同一条公式 → 记 `released` 让出席位（`formula_miner.py:436-440`）。搜索开始时把全部已登记 id（含已移除）放进“试过”集合，不会重复登记（`formula_miner.py:330-332`、`:351-354`），对照探针 `test_f4_control_without_race_the_miner_skips_known_ids` 通过。
- **Spec 更正**：`rule_auto_select.py` 并不登记 `rank_*`，它只从已登记的排序因子里挑选、改写 `rules/swing.json`（`rule_auto_select.py:89-103`、`:218-219`）。登记方只有导入接口和公式挖掘。
- oasis-rank v5.4 的“外部登记即已消费”（`engine.py:816-850`，`confirm.py:150`）：ZenTrade 公式挖掘登记的同款公式会让 oasis-rank 对应行记 `consumed.zentrade_external`、在组的作废。生产现状 0 条。挖掘模板空间（不含窗口变异，抽样 4000 轮 × 120 条，得到至少 18,035 条不同公式，尚未饱和）与 oasis-rank 公式库 1,891 行的重合：同 id 9 条、规范化同指纹 29 条，全部是未入组的普通行；oasis-rank 两条在组公式（`rank_af8c07b0db` 用成交额 amount，挖掘积木里没有；`rank_88e88c5b6e` 是多条件 if_else）都不在挖掘模板空间（`evidence/miner_vs_oasis_library_overlap.json`）。
- 问题：F4（撞车竞态与归属）、F7（共享源码目录与发布顺序）。

## 四、问题清单（按影响排序）

核实方式全部为“亲验”：回读源码 + 隔离复现（探针 `probes/test_rank_collab_probes.py`，断言写的是**现行行为**，修复后对应断言应翻转）。

### F3 “导入 ≠ 启用”已不成立（合同口径不一致，需拍板）

- 是什么：外部系统导入的排序因子，现在不需要任何人在齿轮里勾选，也可能被自动换进做多 3 条，从下一个信号日起按它出纸面卡。
- 位置：`rule_auto_select.py:55-58`（没有开关文件 = 开）、`:89-103`（候补 = 全部在册排序因子，不分出处）、`:218-219`（写 `rules/swing.json`）；每周最后一个交易日盘后调用（`daily_pipeline.py:719-725`）。仍写着“导入 ≠ 启用 / 勾不勾由人决定”的地方：`rank_lab.py:25`、`factor_lab.py:18`、`api_keys.py:52-53`、`rule_forward.py:11-12`；oasis-rank 侧 `oasis/web.py:497`、`oasis/engine.py:655`。
- 复现：`test_f3_auto_select_enables_an_imported_ranker_without_the_gear` → 实际输出 `换入记录={'action': 'swap', 'out': 'c', 'in': 'rank_a1e2ee59af', ... 'applied': True} 做多 3 条=('a', 'b', 'rank_a1e2ee59af')`。
- 生产现状：`rules/auto_select.json` 不存在（= 开），评定与换入记录都为空；前向记分摘要 `as_of=2026-11-17`、22 个信号日，全部 52 条规则只有 `early/thin`；3 条外部排序因子在“全部格子”只有 3–4 个有成交信号日、9–11 笔，门槛是 20 个信号日、15 笔且区间整体高于 0。11-20 的首次评定不会换入。
- 影响：纸面期不亏真钱；但与 Spec 和 oasis-rank 文档里“导入不替用户勾 3+3”的约定相反，oasis-rank 的“只导入前向确认通过的公式”事实上成了进入纸面出卡的唯一一道人工闸门。
- 选项见第七节。

### F1 夜间整份重算抹掉并发导入的复核结果（真缺陷，P3）

- 位置：`rank_lab.py:750-780`。`evaluate_and_store(ids=None)` 先在锁外读登记（`:753`）、锁外逐条重算（`:756-768`），写回时 `if ids is None: allres = {}`（`:773-774`）整份覆盖。
- 触发：17:40 ET 夜间重导或手动“重导”（`factor_lab.py:341-358`）的复核阶段里，正好有一条导入进来（oasis-rank 导入接口无时段限制；“立即挖掘”`factor_lab.py:380-397` 也不检查重导是否在跑）。生产 11-17 复核 3 条约 1 秒，窗口随条数线性增长。
- 复现：`test_f1_full_rescore_drops_result_of_ranker_imported_meanwhile` → `登记=[A, B] 导入 B 后复核结果=[A, B] 夜间写回后=[A]`。
- 影响：B 仍在册、照常进扫描和前向记分，只是齿轮右侧数字、因子页“排序因子”行与公式挖掘页的回放数字为空，直到下一个交易日 17:40 ET 重导自愈。不进交易决策。parity 存在登记里，不受影响。
- 最小补丁思路：写回时不清空，改为锁内重读登记、只丢掉已不在册的 id（`allres = {k: v for k, v in allres.items() if k in read_rankers(data_dir)}` 再 `update(fresh)`）。

### F2 三件套没有“整代”原子性，读者不核 `pool_sha`（真缺陷，P3）

- 位置：写侧 `rank_lab.py:599-620`：`pool.parquet`、`panel.parquet`、`meta.json` 各自原子替换，但三者之间没有整体切换；读侧 `load_pool`（`rank_lab.py:331-361`）先读 meta 再读两份 parquet，`pool.sha` 直接取 meta 里的值（`:326-328`），不按字节复核。oasis-rank 的 `Lab` 走同一个 `load_pool`（`oasis/engine.py:341-347`）；它只在存档候选池时复核 sha（`oasis/cache.py:164`）。
- 触发：读者恰好落在“新 pool 已替换、meta 还没替换”的窗口里。生产 11-17 实测这个窗口约 36 ms/交易日；ZenTrade 导入时的复核、oasis-rank 每轮任务都会读池。
- 复现：`test_f2_mixed_generation_read_is_silent_and_fakes_a_parity_mismatch` → `meta.pool_sha=4765f8ad87f1d507 实际字节 sha=b2e9603a81cef1b3`，`parity={'checked': True, 'match': False, 'diff': ['holdout.n: 挖掘 153 / ZenTrade 155', ...]}`。
- 影响：不报错，混合数据被打上旧 `pool_sha` 的标签。落在 ZenTrade 导入那一刻 → 这条登记永久记下一条假的“数字对不上”（parity 是导入时快照，之后不更新）；落在 oasis-rank → 研究数字按错误的池标签入库。不进交易决策。
- 最小补丁思路：`load_pool` 读完两份 parquet 后按 `build_pool` 同式重算 sha，与 meta 不符就返回 None（调用方已有 None 分支：ZenTrade 记 `no_pool`、oasis-rank 抛错下一轮重试）。约 2.3 MB 的哈希，毫秒级。

### F4 公式挖掘在搜索窗口里与外部导入撞同一 id（防御缺口 + 归属口径，P3）

- 位置：`formula_miner.py:330-332`（只在搜索开始时取“已登记”快照），搜索在子进程里跑（预算最长 900 秒），`admit` 直接调 `import_ranker`（`formula_miner.py:480-486`），而 `import_ranker` 的出处是“后写者赢”（`rank_lab.py:660-663`：`"source": src or prev.get("source")`）。之后 `retire` 把它当自己的候补，前向垫底或坐满 60 个交易日就移除（`formula_miner.py:441-462`）。
- 触发：搜索进行中，oasis-rank 恰好导入了同一条公式（去空白后字面相同 + 同方向）。这不是假想的公式：例子 `ts_rsi(close,14)` 低值优先，既是 oasis-rank 种子“RSI 低”（id `rank_f8fad1581a`，库里现存一行），也在挖掘 rsi 模板的生成范围内。
- 复现：`test_f4_miner_admit_takes_over_an_oasis_import_and_can_retire_it` → `出处=zentrade-miner 退席=[{'id': 'rank_f8fad1581a', 'name': 'RSI 14 低', 'reason': 'below'}] removed_at=...`；对照组（无竞态）通过。
- 影响：oasis-rank 导入的那条在 ZenTrade 里改挂公式挖掘名下，可能被自动移除；oasis-rank 账本只会在对账时看到 `removed_at`。生产现状 0 次。
- 最小补丁思路：`admit` 在登记前于 `rank_lab` 锁内复查该 id 是否已在册（含已移除），已在就跳过并在试验记录里写 `taken`。

### F6 已移除的排序因子再次导入 = 静默复活（口径待定，P3）

- 位置：`rank_lab.py:660-662` 每次导入都写 `removed_at: None`；返回的 `created` 只区分“以前有没有这条”，不区分“以前是否已移除”。oasis-rank 的导入门对“已导入过”直接放行（`oasis/engine.py:678-690`），对账虽会记下 ZenTrade 的 `removed_at`（`engine.py:909-912`）但不拦截重导。
- 复现：`test_f6_reimport_silently_revives_a_removed_ranker` → `created=False removed_at=None`。
- 影响：用户在 ZenTrade 移除的排序因子，被 oasis-rank“重导刷新对账”后回到齿轮目录、夜间扫描、前向记分，并因 F3 重新成为自动换入候选；双方界面都没有提示。公式挖掘不受影响（它把已移除的也算“试过”）。
- 选项见第七节。

### F5 移除后名字回落成编号（展示缺陷，P3）

- 位置：`swing_desk.py:103-109` 取排序因子名字走 `rank_lab.ranker_names`，后者只读在册的（`rank_lab.py:626-637`）；名字在渲染时现取：卡上命中策略 `swing_desk.py:291`、簿内卡还原 `:578`、过去交易日回看 `opp_asof.py:67`、今日多空 `ls_rank.py:380`、齿轮两步保存预览 `opp_page.py:454-455`。
- 触发：一条排序因子出过卡后被移除（人工移除，或被自动换出后再被公式挖掘退席）；之后 7 个交易日内的持仓、灰色历史和回看页。
- 复现：`test_f5_removed_ranker_name_falls_back_to_raw_id` → `移除前=排序 · 外部 移除后=rank_a1e2ee59af`。
- 影响：违反“内部编号不上屏”的界面约定，不影响交易。
- 最小补丁思路：`ranker_names` 读含已移除的登记（名字后可加“已移除”）。

### F7 发布先改共享源码目录、后构建镜像，失败不回滚（运维耦合，P3）

- 位置：生产树 `scripts/deploy-nas.sh:26-33`：先 `rsync --delete` 受管源码树（含 `backend/app`），再 `docker compose build`；构建或健康检查失败直接退出，不还原源码树。ZenTrade 容器用镜像内代码（不挂源码目录），oasis-rank 两个容器只读挂的正是这份 `backend/app`，并按它算打分指纹；指纹一变就触发重算检查：先跑金丝雀，数字逐位不变就沿用旧结果，变了才整族重算（`oasis/engine.py:204-213`、`oasis/schedule.py:56-60`、`oasis/engine.py:1927-1987`）。
- 触发：每次发布的构建期间（分钟级，未实测时长）；或某次发布在构建 / 健康检查失败后，一直持续到下一次成功发布。
- 影响：新代码若改了打分结果，oasis-rank 会按“还没上线、甚至上线失败”的代码整族重算并据此过研究门；这期间导入的公式，ZenTrade 按旧代码复核，parity 会报不一致（能被发现，不是静默错误）。只改注释等不影响数字的发布，金丝雀通过，没有实际后果。现行靠人工流程规避（oasis-nas 手册：发 ZenTrade 打分代码前先暂停 oasis-rank），脚本本身不保证。
- 选项见第七节。

## 五、已核实无问题

- 分段：两边常量同为 2025-06-30 / 2025-07-01；`meta.json`、`results.json`、状态接口的 `split` 一致；分段不同的结果 `verdict` 给 `stale_split`、不给候选（`rank_lab.py:536-556`），parity 先比分段（`:694-702`）。oasis-rank 在进程内覆盖分段常量，两边值相同时是空操作（`oasis/engine.py:349-353`）。
- 对账：生产 5/5 一致；oasis-rank 账本 3 条与 ZenTrade 登记一致，无 `zentrade_missing`、无冲突。
- 复核结果完整：5 条在册均有结果，无多余结果；`pool_sha` 与 meta 一致。
- 打分指纹与依赖：oasis-rank 已检查的 `code_sha=0c8f0e22f498` = 按 `d14a2a9` 四个打分文件现算；打分路径的 import（`rank_lab → swing_candidates / dsl → registry`）都在这四个文件内；两容器 numpy 2.4.6 / polars 1.44.1 / pyarrow 24.0.0，Python 3.11.14 / 3.11.15。
- `rankers.json` 无丢写：唯一写入方、同一把跨进程锁、原子替换；oasis-rank 挂载 `rw=false`（`docker inspect` 实读）。
- 移除保护：在 3+3 里的排序因子移除返回 409（`factor_lab.py:327-338`）；公式挖掘退席跳过已入选的（`formula_miner.py:441-443`）。
- 补算不看未来：`backfill_latest` 只补缺的键、锁内重读后合并、用信号表当时冻结的名单、数据截至该信号日收盘（`rank_lab.py:853-883`）；下单簿已冻结时不改当天的板。
- 盘中导入：导入接口无时段限制（生产 11-10 有 3 次导入 / 重导发生在盘中：12:48 ET 两次、14:08 ET 一次），在 API 线程里做一次复核和一次补算；只写 `research/rank_lab/` 与最近一份信号表的新键，下单簿冻结后不改当天的板，未发现正确性影响。

## 六、Spec 待定问题与更正

- **是否检查写入竞争**：检查了。`rankers.json` 无丢写（见第五节）；`results.json` 有丢写（F1）；登记出处有“后写者赢”的竞态（F4）；“盘后扫描整份写信号表”与“导入补算往同一份信号表加键”之间有毫秒级理论窗口（扫描写信号表不拿 `rank_lab` 的锁），未复现，只列防御缺口。
- **是否补回归测试**：本轮只交报告。复现探针放在本目录 `probes/`，没有放进 `backend/tests/`，没有提交。修复时把对应断言翻转即可作为回归测试。
- 对 Spec 的更正（以代码与实测为准）：
  - “线上 `a30d863`” → 线上 `d14a2a9`（链路文件未变，见第二节）。
  - “导入不等于启用，需在齿轮勾进做多并保存才出卡” → 自动选策略上线后不再成立（F3）。
  - “`rule_auto_select.py` 也会登记 `rank_*` 候补” → 它不登记，只挑选（第 3.4 节）。
  - “`worker.py:159` 承载导入” → `worker.py:159/255` 只是夜间重导子进程入口；导入在 API 进程内同步完成（`factor_lab.py:310-324`）。
  - “`decision_scan_job.py:417`” 是开源树行号，生产为 `:367-369`。

## 七、需要你拍板的 3 件事

纸面期，以下选项都不涉及真钱。

1. **外部导入的排序因子能不能被自动换进做多 3 条（F3）**
   - 现状：能。生产自动选策略默认开着；按前向样本推算，最早约 12 月中旬才可能出现第一次换入。
   - A 维持全自动，只把两边合同文字改成“导入 = 进入前向记分与自动换入候选”。oasis-rank 的“确认通过才导入”就是实际闸门。
   - B 自动换入只从 ZenTrade 自己的候补和内置规则里挑，外部导入仍需人工在齿轮勾选。改 `rule_auto_select.eligible` 一处，按出处过滤。代价：外部公式前向再好也要等人操作。
   - C 外部导入单独一个“允许自动换入”开关，默认关。
   - 建议 A：符合你 11-18 定的“直接全自动”和“不建需要等人操作的开关”，前向门槛本身较严；只需同步两边文字。

2. **同一条公式被两方登记时归谁，移除后再导入是否复活（F4、F6）**
   - A 维持“后写者赢、重导即复活”，只写进合同。
   - B 出处只增不改：保留首个登记方并记出处历史；移除后重导须显式声明复活，否则 409。
   - C 只修竞态：公式挖掘登记前锁内复查 id，已在册就跳过；重导复活保留，但返回里加 `revived: true`，让 oasis-rank 账本记一笔。
   - 建议 C：改动最小，只往更保守的方向走，不改现有导入语义。

3. **发布与 oasis-rank 共享源码目录（F7）**
   - A 维持人工流程（发打分代码前先暂停 oasis-rank），在发布脚本里加一行提醒。
   - B 发布脚本失败时还原受管源码树（同步前留一份旧树，失败的 trap 里还原）。构建期间的分钟级窗口仍在。
   - C oasis-rank 改挂“已上线快照”目录，发布在健康检查通过后才原子切换。代价：两边 compose 都要改。
   - 建议 B：一处脚本改动，消除“失败后长期不一致”这一档；构建窗口继续靠人工暂停覆盖。

## 八、交给 oasis-rank 侧自查的接口项

- `Lab` 读池不复核 `pool_sha`（`oasis/engine.py:341-347`），同 F2；可在读侧加按字节复核。
- 打分指纹 `CODE_FILES` 不含依赖版本（`oasis/engine.py:117-118`）：现在两边版本一致，ZenTrade 升级 numpy / polars / pyarrow 时需同步 `requirements.txt`，否则不会触发重算，只能靠导入时的 parity 发现。
- 文档中的“导入 ≠ 启用，不替用户勾 3+3”（`oasis/web.py:497`、`oasis/engine.py:655`）需随第七节第 1 项更新。
- 对账记下 ZenTrade 的 `removed_at` 但不拦截重导（`oasis/engine.py:678-690`、`:909-912`），与第七节第 2 项同口径。
- `consumed.zentrade_external` 与公式挖掘模板空间有 29 条规范化重合（均未入组），若将来把这些公式送去前向确认，可能被 ZenTrade 公式挖掘的登记作废。

## 九、复现方法与证据

- 探针：`docs/audits/2026-11-18-rank-lab-collab/probes/test_rank_collab_probes.py`（7 条，全在 pytest 临时目录跑，不读生产、不出网）。
  ```bash
  # 生产代码
  git -C <zentrade-prod> archive d14a2a9 backend/app backend/config backend/tests/conftest.py | tar -x -C <scratch>/prodcode
  cp probes/test_rank_collab_probes.py <scratch>/prodcode/backend/tests/
  cd <scratch>/prodcode/backend
  env -u VIRTUAL_ENV PYTHONPATH= PYTHONDONTWRITEBYTECODE=1 <zentrade 主工作区>/backend/.venv/bin/python \
    -m pytest tests/test_rank_collab_probes.py -q -s -p no:cacheprovider
  # 开源树: 把 archive 的提交换成 7ce72ca
  ```
  结果：生产 `d14a2a9` 7 passed（4.99 s，输出见 `evidence/probe_run_prod_d14a2a9.log`）；开源 `7ce72ca` 7 passed（4.31 s，`evidence/probe_run_oss_7ce72ca.log`）。
- 生产只读快照：在 NAS 宿主上用标准库 `python3 -` 读 JSON 与 `stat`，只读；输出见 `evidence/prod_state_readonly_20261118.json`、`evidence/oasis_confirm_state_20261118.json`。
- 代码一致性：`evidence/code_identity_prod.txt`（12 个文件 × 3 处 sha256）。
- 挖掘 × oasis-rank 公式库重合：`evidence/miner_vs_oasis_library_overlap.json`（只放计数和 id，不放公式原文）。

## 十、未覆盖与边界

- 没有在生产上真的触发导入、重导、立即挖掘、齿轮保存或自动换入（受约束）；F1/F2/F4 的生产窗口长度来自文件时间戳和代码顺序，竞态本身只在隔离环境复现。
- F7 的构建窗口时长未实测（NAS 上源码文件的 ctime 被权限守护改写，不能用来定时间）。
- 没有审 oasis-rank 的研究统计口径（κ、IC、确认门）；只核了它与 ZenTrade 的接口点。
- 没有走 HTTP 层端到端调导入接口；权限由现有测试 `test_research_scope_permissions` 覆盖，导入语义在函数层复现。
- 齿轮里“每侧最多 10 条、保存只取前 3 条”（`opportunities.py:452`、`swing_desk.py:181/192`）属通用配置口径，不是本链路特有，未展开。
