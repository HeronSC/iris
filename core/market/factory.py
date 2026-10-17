# File: core/market/factory.py

from __future__ import annotations

from pathlib import Path

from core.market.adapters import AlpacaBars, AlpacaListings, FileHoldings, SearxNews, UnavailableHoldings, YahooQuotes
from core.market.models import Sources
from core.market.research import MarketResearch
from core.market.store import MarketStore


def build_market_research(root: str | Path, secrets: object | None = None, search: object | None = None, holdings_path: str | Path | None = None) -> MarketResearch:
    holdings = FileHoldings(holdings_path) if holdings_path and Path(holdings_path).exists() else UnavailableHoldings()
    sources = Sources(
        listings=AlpacaListings(secrets),
        bars=AlpacaBars(secrets),
        quotes=YahooQuotes(),
        news=SearxNews(search),
        holdings=holdings,
    )
    return MarketResearch(MarketStore(root), sources)
