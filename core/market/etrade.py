# File: core/market/etrade.py

from __future__ import annotations

from core.market.models import AccountRead


class EtradeHoldings:
    def __init__(self, key: str = "", secret: str = "") -> None:
        self.key = key
        self.secret = secret

    def read(self) -> AccountRead:
        if not self.key or not self.secret:
            return AccountRead(False, "etrade", None, message="E*TRADE is not connected.")
        return AccountRead(False, "etrade", None, message="E*TRADE holdings were not read.")
