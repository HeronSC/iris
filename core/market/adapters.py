# File: core/market/adapters.py

from __future__ import annotations

import json
from pathlib import Path

import httpx

from core.market.criteria import as_fraction
from core.market.models import AccountRead, Bar, Headline, Holding, Quote
from core.market.symbols import normalize_symbol

US_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "NYSEARCA", "BATS"}


def _alpaca_credentials(secrets: object | None) -> tuple[str, str]:
    key = _first_secret(secrets, ("alpaca:key", "alpaca:api_key"))
    secret = _first_secret(secrets, ("alpaca:secret", "alpaca:secret_key"))
    return key, secret


def _first_secret(secrets: object | None, names: tuple[str, ...]) -> str:
    getter = getattr(secrets, "get", None)
    if not callable(getter):
        return ""
    for name in names:
        value = getter(name)
        if value:
            return str(value)
    return ""


class AlpacaListings:
    def __init__(self, secrets: object | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.secrets = secrets
        self.transport = transport
        self.problem = ""

    def symbols(self) -> list[str]:
        key, secret = _alpaca_credentials(self.secrets)
        if not key or not secret:
            self.problem = "Alpaca keys are not set. Use /secrets set alpaca:key <value> and /secrets set alpaca:secret <value>."
            return []
        self.problem = ""
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        with httpx.Client(timeout=30.0, headers=headers, transport=self.transport) as client:
            response = client.get("https://api.alpaca.markets/v2/assets", params={"status": "active", "asset_class": "us_equity"})
            response.raise_for_status()
            payload = response.json()
        found = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict) or item.get("tradable") is False:
                continue
            if str(item.get("exchange") or "").upper() not in US_EXCHANGES:
                continue
            symbol = normalize_symbol(item.get("symbol"))
            if symbol:
                found.append(symbol)
        return found


class AlpacaBars:
    def __init__(self, secrets: object | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.secrets = secrets
        self.transport = transport
        self.requested_days = 0

    def daily(self, symbols: list[str], trading_days: int) -> dict[str, list[Bar]]:
        self.requested_days = trading_days
        key, secret = _alpaca_credentials(self.secrets)
        if not key or not secret or not symbols:
            return {}
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        found: dict[str, list[Bar]] = {}
        with httpx.Client(timeout=30.0, headers=headers, transport=self.transport) as client:
            for index in range(0, len(symbols), 100):
                chunk = symbols[index : index + 100]
                response = client.get(
                    "https://data.alpaca.markets/v2/stocks/bars",
                    params={"symbols": ",".join(chunk), "timeframe": "1Day", "limit": trading_days},
                )
                if response.status_code >= 400:
                    continue
                bars = (response.json() or {}).get("bars") or {}
                for symbol, rows in bars.items():
                    parsed = []
                    for row in rows or []:
                        if not isinstance(row, dict):
                            continue
                        try:
                            parsed.append(Bar(float(row["c"]), float(row["v"])))
                        except (KeyError, TypeError, ValueError):
                            continue
                    if parsed:
                        normalized = normalize_symbol(symbol)
                        if normalized:
                            found[normalized] = parsed
        return found


class YahooQuotes:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    def fetch(self, symbols: list[str], *, limit: int) -> dict[str, Quote]:
        found: dict[str, Quote] = {}
        with httpx.Client(timeout=20.0, transport=self.transport, headers={"User-Agent": "Iris/1.0"}) as client:
            for symbol in symbols[:limit]:
                quote = self._one(client, symbol)
                if quote is not None:
                    found[symbol] = quote
        return found

    def _one(self, client: httpx.Client, symbol: str) -> Quote | None:
        response = client.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
            params={"modules": "price,summaryProfile,defaultKeyStatistics,financialData"},
        )
        if response.status_code >= 400:
            return None
        result = (((response.json() or {}).get("quoteSummary") or {}).get("result") or [None])[0]
        if not isinstance(result, dict):
            return None
        return quote_from_summary(symbol, result)


def quote_from_summary(symbol: str, result: dict) -> Quote:
    price = result.get("price") or {}
    profile = result.get("summaryProfile") or {}
    stats = result.get("defaultKeyStatistics") or {}
    financial = result.get("financialData") or {}
    debt = _raw(financial.get("debtToEquity"))
    if debt is not None:
        debt = debt / 100
    return Quote(
        symbol=symbol,
        long_name=_text(price.get("longName")),
        short_name=_text(price.get("shortName")),
        summary=_text(profile.get("longBusinessSummary")),
        industry=_text(profile.get("industry")),
        quote_type=_text(price.get("quoteType")),
        trailing_eps=_raw(stats.get("trailingEps")),
        revenue_growth=as_fraction(_raw(financial.get("revenueGrowth"))),
        return_on_equity=as_fraction(_raw(financial.get("returnOnEquity"))),
        profit_margin=as_fraction(_raw(financial.get("profitMargins"))),
        debt_to_equity=debt,
        free_cash_flow=_raw(financial.get("freeCashflow")),
        trailing_pe=_raw(stats.get("trailingPE")),
        dividend_yield=as_fraction(_raw(stats.get("dividendYield") or summary_yield(result))),
        payout_ratio=as_fraction(_raw(stats.get("payoutRatio"))),
    )


def summary_yield(result: dict) -> float | None:
    detail = result.get("summaryDetail") or {}
    return _raw(detail.get("dividendYield"))


def _raw(node: object) -> float | None:
    if isinstance(node, dict):
        node = node.get("raw")
    try:
        if node is None or isinstance(node, bool):
            return None
        return float(node)
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


class SearxNews:
    def __init__(self, search: object | None) -> None:
        self.search = search

    def headlines(self, symbol: str, *, limit: int = 5) -> list[Headline] | None:
        if self.search is None:
            return None
        try:
            response = self.search.search(f"{symbol} stock", category="news", time_range="week")
        except Exception:
            return None
        hits = getattr(response, "hits", ()) or ()
        return [Headline(hit.title, hit.url) for hit in hits[:limit]]


class FileHoldings:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> AccountRead:
        if not self.path.exists():
            return AccountRead(False, "file", None, message="No holdings file.")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AccountRead(False, "file", None, message="Holdings file could not be read.")
        if not isinstance(payload, dict):
            return AccountRead(False, "file", None, message="Holdings file could not be read.")
        holdings = []
        for row in payload.get("holdings") or []:
            if not isinstance(row, dict):
                continue
            symbol = normalize_symbol(row.get("symbol"))
            if not symbol:
                continue
            try:
                quantity = float(row.get("quantity"))
            except (TypeError, ValueError):
                continue
            market_value = row.get("market_value")
            try:
                market_value = None if market_value is None else float(market_value)
            except (TypeError, ValueError):
                market_value = None
            holdings.append(
                Holding(
                    symbol,
                    quantity,
                    market_value,
                    _text(row.get("security_class")),
                    str(row.get("account_id") or ""),
                    str(row.get("account_name") or ""),
                )
            )
        net = payload.get("net_value")
        try:
            net_value = None if net is None else float(net)
        except (TypeError, ValueError):
            net_value = None
        return AccountRead(True, "file", net_value, tuple(holdings))


class UnavailableHoldings:
    def read(self) -> AccountRead:
        return AccountRead(False, "none", None, message="Holdings were not read.")
