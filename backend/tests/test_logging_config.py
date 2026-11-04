"""日志配置回归: APScheduler 的例行心跳不得占用 INFO 输出。

APScheduler 每执行一次任务都会在 INFO 记录 "Running job" / "executed successfully"。
其中 _hot_quotes_tick 是 5 秒一次, 长期运行实例里这些心跳占掉约 86% 的日志量,
把同步失败、数据源降级这类真正的信号淹掉。

断言的是 apscheduler 这一支上的**显式**级别, 而不是 getEffectiveLevel():
pytest 的 logging 插件会接管根 logger, 而根 logger 默认就是 WARNING,
用生效级别判断会让本测试在缺少实现时也变绿 (假绿)。
"""

from __future__ import annotations

import logging

import app.main  # noqa: F401  导入即执行模块级日志配置


def test_apscheduler_logger_level_is_pinned_to_warning() -> None:
    aps = logging.getLogger("apscheduler")

    # 显式设置在 apscheduler 这一支上, 不依赖根 logger 当前是什么级别
    assert aps.level == logging.WARNING
    # 例行心跳(INFO)被挡在门外
    assert not aps.isEnabledFor(logging.INFO)
    # 真实异常(WARNING/ERROR)仍然输出
    assert aps.isEnabledFor(logging.WARNING)
    assert aps.isEnabledFor(logging.ERROR)
