# File: core/market/models.py

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Bar:
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    long_name: str | None = None
    short_name: str | None = None
    summary: str | None = None
    industry: str | None = None
    quote_type: str | None = None
    trailing_eps: float | None = None
    revenue_growth: float | None = None
    return_on_equity: float | None = None
    profit_margin: float | None = None
    debt_to_equity: float | None = None
    free_cash_flow: float | None = None
    trailing_pe: float | None = None
    dividend_yield: float | None = None
    payout_ratio: float | None = None


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: float
    market_value: float | None
    security_class: str | None
    account_id: str
    account_name: str


@dataclass(frozen=True)
class AccountRead:
    ok: bool
    source: str
    net_value: float | None
    holdings: tuple[Holding, ...] = ()
    failed_accounts: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class Headline:
    title: str
    url: str
    theme: str = ""


@dataclass(frozen=True)
class RankedName:
    symbol: str
    strength: float
    held: bool
    advanced: bool
    provisional: bool
    company_name: str
    industry: str
    summary: str
    headlines: tuple[Headline, ...] = ()
    news_status: str = "missing"


@dataclass(frozen=True)
class MarketReport:
    ok: bool
    message: str
    chat: str
    details: str
    profile: str
    candidate_count: int = 0
    advanced_count: int = 0
    provisional_count: int = 0
    unchecked_count: int = 0
    holdings_source: str = ""
    ranked: tuple[RankedName, ...] = ()
    problems: tuple[str, ...] = ()


class ListingsSource(Protocol):
    def symbols(self) -> list[str]: ...


class BarsSource(Protocol):
    def daily(self, symbols: list[str], trading_days: int, on_progress: Callable[[str, str], None] | None = None) -> dict[str, list[Bar]]: ...


class QuoteSource(Protocol):
    def fetch(self, symbols: list[str], *, limit: int, on_progress: Callable[[str, str], None] | None = None) -> dict[str, Quote]: ...


class NewsSource(Protocol):
    def headlines(self, symbol: str, *, limit: int = 5) -> list[Headline] | None: ...


class HoldingsSource(Protocol):
    def read(self) -> AccountRead: ...


@dataclass
class Sources:
    listings: ListingsSource
    bars: BarsSource
    quotes: QuoteSource
    news: NewsSource
    holdings: HoldingsSource
    history_days: int = 200
