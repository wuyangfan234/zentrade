"""统一凭据解析 (0.42.0): 所有「需要填 key」的外部服务只从这里取值。

取值顺序 (与 secrets_store.get_env_backed_secret 一致, 全系统同一条规则):
  1. 设置页写入的 data/user_data/secrets.json (字段名 = SecretSpec.field)
  2. 进程环境变量 (docker compose 的 env_file=.env 注入; 名字 = SecretSpec.env)
  3. 可选的本机 env 目录 ZENTRADE_ENV_DIR/<file> (开发机或宿主机脚本用; 未设置则不读任何文件)

不再有写死的本机路径。值永不打印、不落盘、不出接口 (接口只给 mask 与来源)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_DIR_VAR = "ZENTRADE_ENV_DIR"


@dataclass(frozen=True)
class SecretSpec:
    env: str                 # 环境变量名 (.env)
    field: str = ""          # secrets.json 字段名; 空 = 只认环境变量
    env_file: str = ""       # ZENTRADE_ENV_DIR 下的文件名; 空 = 不读文件


ALPACA_PACKED = SecretSpec("ZENTRADE_ALPACA_KEY", "alpaca_api_key")      # KEY_ID:SECRET (设置页写入)
ALPACA_KEY_ID = SecretSpec("ALPACA_API_KEY_ID", env_file="alpaca.env")
ALPACA_SECRET = SecretSpec("ALPACA_API_SECRET_KEY", env_file="alpaca.env")
FINNHUB = SecretSpec("FINNHUB_API_KEY", "finnhub_api_key", "finnhub.env")
MASSIVE = SecretSpec("MASSIVE_API_KEY", "massive_api_key", "polygon.env")
POLYGON = SecretSpec("POLYGON_API_KEY", "", "polygon.env")
SEC_UA = SecretSpec("SEC_USER_AGENT", "sec_user_agent")


def _from_env_dir(spec: SecretSpec) -> str:
    base = os.environ.get(ENV_DIR_VAR, "").strip()
    if not base or not spec.env_file:
        return ""
    try:
        for line in (Path(base).expanduser() / spec.env_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == spec.env:
                return v.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def lookup(spec: SecretSpec) -> tuple[str, str]:
    """返回 (值, 来源)。来源 = 'ui' / 'env' / 'env_dir' / ''。"""
    if spec.field:
        from app import secrets_store
        val = str(secrets_store.load().get(spec.field) or "").strip()
        if val:
            return val, "ui"
    val = os.environ.get(spec.env, "").strip()
    if val:
        return val, "env"
    val = _from_env_dir(spec)
    return (val, "env_dir") if val else ("", "")


def get(spec: SecretSpec) -> str:
    return lookup(spec)[0]


def alpaca() -> tuple[str, str, str]:
    """(key_id, secret, 来源)。设置页的 KEY_ID:SECRET 优先, 其次两个环境变量 / env 目录。"""
    packed, src = lookup(ALPACA_PACKED)
    if packed and ":" in packed:
        k, s = packed.split(":", 1)
        if k.strip() and s.strip():
            return k.strip(), s.strip(), src
    k, ks = lookup(ALPACA_KEY_ID)
    s, _ = lookup(ALPACA_SECRET)
    return (k, s, ks) if k and s else ("", "", "")


def finnhub() -> str | None:
    return get(FINNHUB) or None


def massive() -> str | None:
    return get(MASSIVE) or get(POLYGON) or None


def sec_user_agent() -> str:
    """SEC EDGAR 要求 User-Agent 带可联系的邮箱 (https://www.sec.gov/os/accessing-edgar-data)。

    取 SEC_USER_AGENT 环境变量或设置页字段 sec_user_agent; 都没有时用中性占位,
    SEC 对占位 UA 可能限流 —— 设置清单会把它标成「建议配置」。
    """
    val = get(SEC_UA)
    return val or DEFAULT_SEC_USER_AGENT


DEFAULT_SEC_USER_AGENT = "ZenTrade self-hosted (set SEC_USER_AGENT)"
