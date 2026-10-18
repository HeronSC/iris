# File: core/market/adapters.py

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from core.market.models import AccountRead, Bar, Headline, Holding, Quote
from core.market.symbols import normalize_symbol

US_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "NYSEARCA", "BATS"}
ALPACA_TRADING_HOSTS = ("api.alpaca.markets", "paper-api.alpaca.markets")
YAHOO_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

Reporter = Callable[[str, str], None]


def _say(on_progress: Reporter | None, text: str, role: str = "progress") -> None:
    if on_progress is not None:
        on_progress(text, role)


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
        if value and str(value).strip():
            return str(value).strip()
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
        try:
            with httpx.Client(timeout=30.0, headers=headers, transport=self.transport) as client:
                for host in ALPACA_TRADING_HOSTS:
                    response = client.get(f"https://{host}/v2/assets", params={"status": "active", "asset_class": "us_equity"})
                    if response.status_code not in {401, 403}:
                        break
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            if code in {401, 403}:
                self.problem = f"Alpaca rejected the keys (HTTP {code}) on both the live and paper endpoints. Check alpaca:key and alpaca:secret with /secrets."
            else:
                self.problem = f"Alpaca listings returned HTTP {code}."
            return []
        except (httpx.HTTPError, ValueError) as error:
            self.problem = f"Alpaca listings could not be read: {error.__class__.__name__}."
            return []
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

    def daily(self, symbols: list[str], trading_days: int, on_progress: Reporter | None = None) -> dict[str, list[Bar]]:
        self.requested_days = trading_days
        key, secret = _alpaca_credentials(self.secrets)
        if not symbols:
            return {}
        if not key or not secret:
            _say(on_progress, "Alpaca keys are not set, so daily bars cannot be read.", "error")
            return {}
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        found: dict[str, list[Bar]] = {}
        start = (datetime.now(timezone.utc) - timedelta(days=max(trading_days * 2, 400))).date().isoformat()
        with httpx.Client(timeout=30.0, headers=headers, transport=self.transport) as client:
            for index in range(0, len(symbols), 100):
                chunk = symbols[index : index + 100]
                rows_by_symbol, problem = self._chunk(client, chunk, start)
                if problem:
                    _say(on_progress, f"Daily bars for {chunk[0]} to {chunk[-1]} ({len(chunk)} symbols) failed: {problem}. Those symbols are skipped.", "error")
                for symbol, rows in rows_by_symbol.items():
                    found[symbol] = rows[-trading_days:]
                done = min(index + len(chunk), len(symbols))
                if done % 1000 == 0 or done == len(symbols):
                    _say(on_progress, f"Daily bars {done} of {len(symbols)}.")
        return found

    def _chunk(self, client: httpx.Client, chunk: list[str], start: str) -> tuple[dict[str, list[Bar]], str]:
        found: dict[str, list[Bar]] = {}
        page = None
        while True:
            params = {"symbols": ",".join(symbol.replace("-", ".") for symbol in chunk), "timeframe": "1Day", "start": start, "limit": 10000}
            if page:
                params["page_token"] = page
            try:
                response = client.get("https://data.alpaca.markets/v2/stocks/bars", params=params)
                if response.status_code >= 400:
                    return {}, f"HTTP {response.status_code}"
                payload = response.json() or {}
            except httpx.HTTPError as error:
                return {}, error.__class__.__name__
            except ValueError:
                return {}, "unreadable reply"
            for symbol, rows in (payload.get("bars") or {}).items():
                parsed = []
                for row in rows or []:
                    if not isinstance(row, dict):
                        continue
                    try:
                        parsed.append(Bar(float(row["c"]), float(row["v"])))
                    except (KeyError, TypeError, ValueError):
                        continue
                normalized = normalize_symbol(symbol)
                if normalized and parsed:
                    found.setdefault(normalized, []).extend(parsed)
            page = payload.get("next_page_token")
            if not page:
                return found, ""


class YahooQuotes:
    def __init__(self, transport: httpx.BaseTransport | None = None, *, give_up_after: int = 25) -> None:
        self.transport = transport
        self.give_up_after = give_up_after

    def fetch(self, symbols: list[str], *, limit: int, on_progress: Reporter | None = None) -> dict[str, Quote]:
        found: dict[str, Quote] = {}
        chosen = symbols[:limit]
        if not chosen:
            return found
        failures: dict[str, int] = {}
        in_a_row = 0
        with httpx.Client(timeout=20.0, transport=self.transport, headers={"User-Agent": YAHOO_AGENT}, follow_redirects=True) as client:
            crumb = self._crumb(client)
            if not crumb:
                _say(on_progress, "Yahoo did not hand out a session crumb, so fundamentals cannot be read.", "error")
                return found
            for index, symbol in enumerate(chosen, start=1):
                quote, problem = self._one(client, symbol, crumb)
                if problem == "HTTP 401":
                    crumb = self._crumb(client) or crumb
                    quote, problem = self._one(client, symbol, crumb)
                if quote is not None:
                    found[symbol] = quote
                    in_a_row = 0
                elif problem:
                    if problem not in failures and problem != "HTTP 404":
                        _say(on_progress, f"Yahoo fundamentals failed for {symbol}: {problem}.", "error")
                    failures[problem] = failures.get(problem, 0) + 1
                    in_a_row = 0 if problem == "HTTP 404" else in_a_row + 1
                if in_a_row >= self.give_up_after:
                    _say(on_progress, f"Yahoo failed {in_a_row} times in a row, so fundamentals stopped at {index} of {len(chosen)}.", "error")
                    break
                if index % 100 == 0 or index == len(chosen):
                    _say(on_progress, f"Fundamentals {index} of {len(chosen)}.")
        if failures:
            counts = ", ".join(f"{problem}: {count}" for problem, count in sorted(failures.items()))
            serious = any(problem != "HTTP 404" for problem in failures)
            _say(on_progress, f"Yahoo had no fundamentals for {sum(failures.values())} of {len(chosen)} names ({counts}).", "error" if serious else "progress")
        return found

    def _crumb(self, client: httpx.Client) -> str:
        try:
            client.get("https://fc.yahoo.com")
        except httpx.HTTPError:
            pass
        try:
            response = client.get("https://query1.finance.yahoo.com/v1/test/getcrumb")
        except httpx.HTTPError:
            return ""
        text = response.text.strip() if response.status_code == 200 else ""
        return "" if not text or "<" in text or " " in text else text

    def _one(self, client: httpx.Client, symbol: str, crumb: str) -> tuple[Quote | None, str]:
        try:
            response = client.get(
                f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
                params={"modules": "price,summaryProfile,summaryDetail,defaultKeyStatistics,financialData", "crumb": crumb},
            )
            if response.status_code >= 400:
                return None, f"HTTP {response.status_code}"
            payload = response.json() or {}
        except httpx.HTTPError as error:
            return None, error.__class__.__name__
        except ValueError:
            return None, "unreadable reply"
        result = ((payload.get("quoteSummary") or {}).get("result") or [None])[0]
        if not isinstance(result, dict):
            return None, "no result"
        return quote_from_summary(symbol, result), ""


def quote_from_summary(symbol: str, result: dict) -> Quote:
    price = result.get("price") or {}
    profile = result.get("summaryProfile") or {}
    detail = result.get("summaryDetail") or {}
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
        revenue_growth=_raw(financial.get("revenueGrowth")),
        return_on_equity=_raw(financial.get("returnOnEquity")),
        profit_margin=_raw(financial.get("profitMargins")),
        debt_to_equity=debt,
        free_cash_flow=_raw(financial.get("freeCashflow")),
        trailing_pe=_first_raw(detail, stats, "trailingPE"),
        dividend_yield=_first_raw(detail, stats, "dividendYield"),
        payout_ratio=_first_raw(detail, stats, "payoutRatio"),
    )


def _first_raw(primary: dict, fallback: dict, key: str) -> float | None:
    value = _raw(primary.get(key))
    return value if value is not None else _raw(fallback.get(key))


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
