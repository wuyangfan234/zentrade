"""策略加载器必须跳过隐藏文件 (含 macOS AppleDouble 伴生文件 ``._xxx.py``)。

复现 2026-09-29 线上故障: 在 Mac 上用 bsdtar 打包推送到部署主机时, 每个带扩展属性的文件
都会多出一个 ``._<原名>`` 伴生文件 (二进制资源叉, 不是 Python 源码)。它们经 Docker
构建上下文 COPY 进镜像后, 被 ``glob("*.py")`` 当成策略加载, 每个都报
``'utf-8' codec can't decode byte 0xa3 in position 45``: 策略页连弹 22 条红色报错,
且 ``reload()`` 按"任一失败即整体失败"语义让保存/发布/热重载全部 400。
"""
from __future__ import annotations

from app.strategy.engine import StrategyEngine

# 部署主机上实取的伴生文件原样字节: 163 B, 魔数 00 05 16 07 + "Mac OS X",
# 第 45 字节 0xa3 即报错位置, 载荷是 com.apple.provenance 扩展属性。
APPLEDOUBLE_SIDECAR = bytes.fromhex(
    "00051607000200004d6163204f53205820202020202020200002000000090000"
    "00320000007100000002000000a3000000000000000000000000000000000000"
    "00000000000000000000000000000000000000004154545200000000000000a3"
    "000000980000000b00000000000000000000000000000001000000980000000b"
    "000015636f6d2e6170706c652e70726f76656e616e6365000102009f4b3027e5"
    "9e956e"
)


def _strategy_code(strategy_id: str) -> str:
    return f'''import polars as pl
META = {{
    "id": "{strategy_id}",
    "name": "{strategy_id}",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.lit(True)
'''


def test_fixture_is_real_appledouble_sidecar():
    assert APPLEDOUBLE_SIDECAR[:4] == bytes.fromhex("00051607")
    assert APPLEDOUBLE_SIDECAR[45] == 0xA3


def test_appledouble_sidecars_are_not_loaded_as_strategies(tmp_path):
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "real.py").write_text(_strategy_code("real"), encoding="utf-8")
    (tmp_path / "._real.py").write_bytes(APPLEDOUBLE_SIDECAR)
    (tmp_path / ".___init__.py").write_bytes(APPLEDOUBLE_SIDECAR)

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    assert engine.has("real")
    assert engine.load_errors() == []


def test_hidden_python_file_is_not_a_strategy(tmp_path):
    # 合法策略 ID 只允许 [A-Za-z0-9_-], 以 "." 开头的文件只可能是隐藏/临时文件
    (tmp_path / "real.py").write_text(_strategy_code("real"), encoding="utf-8")
    (tmp_path / ".draft.py").write_text(_strategy_code("draft"), encoding="utf-8")

    engine = StrategyEngine(strategy_dirs=[tmp_path])

    assert engine.has("real")
    assert not engine.has("draft")
    assert engine.load_errors() == []


def test_reload_succeeds_when_sidecars_appear_after_start(tmp_path):
    (tmp_path / "real.py").write_text(_strategy_code("real"), encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[tmp_path])
    (tmp_path / "._real.py").write_bytes(APPLEDOUBLE_SIDECAR)

    engine.reload()

    assert engine.has("real")
    assert engine.load_errors() == []
