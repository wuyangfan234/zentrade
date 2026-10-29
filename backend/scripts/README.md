# 后端维护脚本

这些脚本面向维护者，用于离线研究、数据修复和压测，不在应用运行时自动执行。除非特别说明，均在 `backend/` 目录下运行，并显式指定数据目录；不要直接指向正在运行实例的生产数据目录，先复制一份再操作。

| 脚本 | 用途 | 写入范围 |
| --- | --- | --- |
| `backfill_daily_isolated.py` | 在隔离目录回填原始日 K 与除权事件，用于回放或回填前的耗时、内存预演 | 仅 `--out` 指定目录 |
| `cleanup_halt_days.py` | 清理历史停牌脏数据（OHLC 填充前收盘价的停牌日），幂等，可 `--dry-run` | 数据目录内日 K 分区 |
| `decision_desk_dryrun.py` | 按生产路径干跑一次决策台，打印结果 | 临时目录 |
| `evaluate_strategies.py` | 统一口径评估全部策略，生成决策台证据文件；与应用内周度任务同一份代码 | `--data` 下 `decision_desk/` |
| `rebuild_swing_history.py` | 用生产函数重算机会板内置规则的回放统计 | `config/swing_rule_history.json` |
| `repair_evaluation_v1_0392.py` | 一次性修复 0.39.x 之前被写入视图行的 v1 评估文件；默认 dry-run，`--apply` 前自动备份 | `--data-dir` 下 `decision_desk/evaluation.json` |
| `replay_setups_realistic.py` | 机会引擎 S1/S2 形态的真实规则重放（时点复权、生产撮合） | `--out` 指定目录 |
| `research_desk_v2.py` | 决策台名次与扎堆效应的只读研究 | `--out` 指定文件 |
| `stress_polars_concurrency.py` | Polars 并发 collect 死锁回归压测；升级 Polars 前必须运行 | 临时目录 |
| `verify_minute_chain.py` | 分钟 K 链路端到端核对（时区、窗口、盘前盘后过滤） | `--out` 指定目录 |
| `verify_paper_chain.py` | 纸面交易链路端到端核对 | 临时目录 |

示例：

```bash
cd backend
uv run --frozen python -m scripts.evaluate_strategies --data /path/to/data-copy --start 2022-01-13 --end 2026-09-22
```

研究类脚本的输出是历史模拟，不代表未来表现，也不会自动改变应用的排序、许可或交易参数。
