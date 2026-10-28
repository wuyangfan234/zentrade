"""Hyperliquid xyz 盘前参考价插件 (无 Key, 只读, 不声明任何交易数据集)。

只提供两件事: 当前在交易的美股永续代码清单、指定代码的中间价。用于盘前/周末跳空预警。
永续价 ≠ 股票成交价 (含基差与资金费、部署方自营预言机单点), 永不进入撮合、日K、实时行情链。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request

from app.data_providers.base import ProviderCapabilities

logger = logging.getLogger(__name__)

INFO_URL = "https://api.hyperliquid.xyz/info"
DEX = "xyz"
_LISTED_TTL = 3600


def _post(body: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(INFO_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "zentrade/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def availability() -> tuple[bool, str]:
    return True, "ok"


class HyperliquidProvider:
    name = "hyperliquid"
    capabilities = ProviderCapabilities()

    def __init__(self) -> None:
        from types import SimpleNamespace
        self.config = SimpleNamespace(display_name="Hyperliquid xyz (盘前参考价)", path=None, datasets={})
        self._listed: set[str] = set()
        self._listed_at = 0.0
        self.last_error: str | None = None

    def close(self) -> None:
        return None

    def listed(self) -> set[str]:
        if self._listed and time.time() - self._listed_at < _LISTED_TTL:
            return set(self._listed)
        meta = _post({"type": "meta", "dex": DEX})
        names = {u["name"].split(":", 1)[-1] for u in meta.get("universe", []) if not u.get("isDelisted")}
        self._listed, self._listed_at = names, time.time()
        return set(names)

    def mids(self, symbols: list[str]) -> dict[str, float]:
        raw = _post({"type": "allMids", "dex": DEX})
        out = {}
        live = self.listed()
        for s in symbols:
            v = raw.get(f"{DEX}:{s}")
            if s in live and v is not None:
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if f > 0:
                    out[s] = f
        return out
