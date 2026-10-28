# TradingView 参照脚本（PyneCore）

逐行转写自用户提供的 Pine 源码，由 PyneCore（Apache-2.0，对拍 TradingView 的 Pine 运行时，
https://github.com/PyneSys/pynecore @9249a0e）逐根执行，作为 `app/backtest/tv_kernels.py` 的对照答案。

- 生成：`python gen_fixtures.py <pyne 解释器>`（需另装 pynecore；生产环境不依赖它）
- 产物：`fixtures.json.gz`（每个脚本 × 若干真实/合成序列的逐根 buy/sell）
- 测试：`tests/test_tv_strategies.py` 读取 fixtures，逐根断言 0 差异

这些参照脚本、逐行转写和 fixtures 不因 PyneCore 的 Apache-2.0 而自动改换许可；各原脚本的作者、许可和使用限制仍适用，详见根目录 `THIRD_PARTY_NOTICES.md`。
