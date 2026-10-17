# File: core/market/research.py

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.market.criteria import PROFILE_IDS, evaluate, facts_from, passes_price_volume, security_class
from core.market.headlines import adjust, tag_headlines
from core.market.models import AccountRead, Headline, MarketReport, Quote, RankedName, Sources
from core.market.report import company_name, render
from core.market.store import MarketStore
from core.market.symbols import normalize_symbol


class MarketResearch:
    def __init__(self, store: MarketStore, sources: Sources) -> None:
        self.store = store
        self.sources = sources

    def run(self, profile: str | None = None) -> MarketReport:
        settings = self.store.settings()
        profile_id = profile or str(settings["active_profile"])
        if profile_id not in settings["gates"]:
            return MarketReport(False, f"Unknown profile {profile_id}.", "", "", profile_id)
        if profile:
            self.store.set_active_profile(profile_id)
            settings = self.store.settings()
        if self.store.fresh(profile_id):
            return self.report_saved(profile_id)
        if self._shared_stale():
            return self.refresh()
        return self.refresh((profile_id,))

    def refresh(self, profiles: tuple[str, ...] | None = None) -> MarketReport:
        settings = self.store.settings()
        chosen = profiles or PROFILE_IDS
        account = self.sources.holdings.read()
        symbols = self._universe(account)
        if not symbols:
            problem = str(getattr(self.sources.listings, "problem", "") or "No listed symbols were returned.")
            return MarketReport(False, problem, problem, problem, str(settings["active_profile"]))
        bars = self.sources.bars.daily(symbols, self.sources.history_days)
        if self.sources.history_days < 200:
            return MarketReport(False, "History request was shorter than 200 trading days.", "", "", str(settings["active_profile"]))
        quotes = self.sources.quotes.fetch(symbols, limit=max(len(symbols), 1))
        last = MarketReport(False, "No profile was refreshed.", "", "", str(settings["active_profile"]))
        for profile_id in chosen:
            last = self._score(profile_id, settings, symbols, bars, quotes, account)
        active = str(settings["active_profile"])
        if self.store.latest(active):
            return self.report_saved(active)
        return last

    def _score(self, profile_id: str, settings: dict[str, Any], symbols: list[str], bars: dict[str, list], quotes: dict[str, Quote], account: AccountRead) -> MarketReport:
        ranked, unchecked, candidate_count = self._rank(profile_id, settings, symbols, bars, quotes, account)
        if candidate_count == 0:
            sells = self._sells(profile_id, settings, account, bars, quotes)
            trims = self._trims(settings, account)
            self.store.save_run({
                "profile": profile_id,
                "gate_version": int(settings["versions"][profile_id]),
                "shared": self.store.shared_stamp(settings),
                "ranked": [],
                "provisional": [],
                "sells": sells,
                "trims": trims,
                "candidate_count": 0,
                "unchecked": unchecked,
                "universe": len(symbols),
            })
            return MarketReport(False, "No name could be scored.", "", "", profile_id, unchecked_count=unchecked, holdings_source=account.source)
        advanced = [row for row in ranked if row.advanced]
        adjusted = self._apply_news(advanced, settings, profile_id)
        by_symbol = {row.symbol: row for row in adjusted}
        merged = [by_symbol.get(row.symbol, row) for row in ranked]
        merged.sort(key=lambda row: (-row.strength, row.symbol))
        provisional = [row for row in merged if row.provisional][:10]
        day = self._day()
        self.store.remember_shown(profile_id, [self._shown(row) for row in provisional], day)
        chat, details = render(provisional, self.store.history(profile_id), day, self._also_on(profile_id))
        payload = {
            "profile": profile_id,
            "gate_version": int(settings["versions"][profile_id]),
            "shared": self.store.shared_stamp(settings),
            "holdings_source": account.source,
            "unchecked": unchecked,
            "universe": len(symbols),
            "candidate_count": candidate_count,
            "ranked": [self._dump(row) for row in merged],
            "day": day,
            "provisional": [row.symbol for row in provisional],
            "sells": self._sells(profile_id, settings, account, bars, quotes),
            "trims": self._trims(settings, account),
        }
        self.store.save_run(payload)
        saved = self.report_saved()
        return MarketReport(
            saved.ok,
            saved.message,
            chat,
            details,
            profile_id,
            candidate_count=candidate_count,
            advanced_count=saved.advanced_count,
            provisional_count=len(provisional),
            unchecked_count=unchecked,
            holdings_source=account.source,
            ranked=saved.ranked,
        )

    def report_saved(self, profile: str | None = None) -> MarketReport:
        settings = self.store.settings()
        profile_id = profile or str(settings["active_profile"])
        latest = self.store.latest(profile_id)
        if not latest:
            return MarketReport(False, "No saved research run.", "", "", "")
        symbols = self._visible_top(latest, profile_id)
        rows = {str(item.get("symbol")): item for item in latest.get("ranked") or [] if isinstance(item, dict)}
        names = []
        for symbol in symbols:
            item = rows.get(symbol) or {"symbol": symbol}
            headlines = tuple(
                Headline(str(hit.get("title") or ""), str(hit.get("url") or ""), str(hit.get("theme") or ""))
                for hit in item.get("headlines") or []
                if isinstance(hit, dict)
            )
            names.append(
                RankedName(
                    symbol=symbol,
                    strength=float(item.get("strength") or 0),
                    held=bool(item.get("held")),
                    advanced=bool(item.get("advanced")),
                    provisional=True,
                    company_name=str(item.get("company_name") or symbol),
                    industry=str(item.get("industry") or ""),
                    summary=str(item.get("summary") or ""),
                    headlines=headlines,
                    news_status=str(item.get("news_status") or ""),
                )
            )
        chat, details = render(names, self.store.history(profile_id), str(latest.get("day") or ""), self._also_on(profile_id))
        return MarketReport(
            True,
            "saved",
            chat,
            details,
            str(latest.get("profile") or ""),
            provisional_count=len(names),
            advanced_count=sum(1 for item in rows.values() if item.get("advanced")),
            ranked=tuple(names),
        )

    def _shared_stale(self) -> bool:
        settings = self.store.settings()
        stamp = self.store.shared_stamp(settings)
        for profile in PROFILE_IDS:
            saved = self.store.latest(profile)
            if isinstance(saved, dict) and saved.get("shared") != stamp:
                return True
        return False

    def _visible_top(self, latest: dict[str, Any], profile: str) -> list[str]:
        exclusions = set(self.store.exclusions())
        ranked = [item for item in latest.get("ranked") or [] if isinstance(item, dict) and item.get("advanced")]
        ranked.sort(key=lambda item: (-float(item.get("strength") or 0), str(item.get("symbol") or "")))
        visible = []
        for item in ranked:
            symbol = str(item.get("symbol") or "")
            if not symbol or symbol in exclusions or self.store.active_dismissal(profile, "buy", symbol):
                continue
            if item.get("news_status") in {"severe", "missing"}:
                continue
            visible.append(symbol)
            if len(visible) == 10:
                break
        return visible

    def _universe(self, account: AccountRead) -> list[str]:
        listed = [normalize_symbol(symbol) for symbol in self.sources.listings.symbols()]
        listed = [symbol for symbol in listed if symbol]
        if listed and self.store.save_listings(listed):
            symbols = listed
        else:
            symbols = self.store.listings() or listed
        held = [normalize_symbol(holding.symbol) for holding in account.holdings]
        for symbol in held:
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        return symbols

    def _rank(
        self,
        profile: str,
        settings: dict[str, Any],
        symbols: list[str],
        bars: dict[str, list],
        quotes: dict[str, Quote],
        account: AccountRead,
    ) -> tuple[list[RankedName], int, int]:
        held = {normalize_symbol(holding.symbol) for holding in account.holdings}
        held.discard(None)
        rows: list[tuple[str, float, bool]] = []
        unchecked = 0
        for symbol in symbols:
            quote = quotes.get(symbol)
            facts = facts_from(bars.get(symbol) or [], quote)
            band = passes_price_volume(facts, settings)
            if symbol not in held and band != "pass":
                if band == "unchecked":
                    unchecked += 1
                continue
            status, strength, _failed = evaluate(profile, facts, settings["gates"][profile])
            if status == "unchecked":
                unchecked += 1
            if status != "pass":
                continue
            if self.store.active_dismissal(profile, "buy", symbol):
                continue
            rows.append((symbol, float(strength or 0), symbol in held))
        rows.sort(key=lambda item: (-item[1], item[0]))
        advance_left = int(settings["advance_n"])
        ranked: list[RankedName] = []
        for symbol, strength, is_held in rows:
            advanced = False
            if not is_held and advance_left > 0:
                advanced = True
                advance_left -= 1
            quote = quotes.get(symbol)
            ranked.append(self._name(symbol, strength, is_held, advanced, False, quote, (), "pending"))
        return ranked, unchecked, len(rows)

    def _apply_news(self, advanced: list[RankedName], settings: dict[str, Any], profile: str) -> list[RankedName]:
        exclusions = set(self.store.exclusions())
        adjusted: list[RankedName] = []
        for row in advanced:
            fetched = self.sources.news.headlines(row.symbol, limit=5)
            excluded = row.symbol in exclusions
            if fetched is None and not excluded:
                adjusted.append(self._copy(row, row.strength, False, "missing", ()))
                continue
            headlines = tag_headlines(fetched or [])
            strength, status = adjust(row.strength, headlines, excluded=excluded)
            eligible = status == "applied" and strength > 0 and not excluded
            adjusted.append(self._copy(row, strength, eligible, status, headlines))
        adjusted.sort(key=lambda row: (-row.strength, row.symbol))
        marked: list[RankedName] = []
        remaining = 10
        for row in adjusted:
            provisional = row.provisional and remaining > 0
            if provisional:
                remaining -= 1
            marked.append(self._copy(row, row.strength, provisional, row.news_status, row.headlines))
        return marked

    def _sells(self, profile: str, settings: dict[str, Any], account: AccountRead, bars: dict, quotes: dict) -> list[dict[str, str]]:
        sells = []
        seen: set[str] = set()
        for holding in account.holdings:
            symbol = normalize_symbol(holding.symbol)
            if not symbol or symbol in seen or holding.quantity < 0:
                continue
            seen.add(symbol)
            quote = quotes.get(symbol)
            kind = security_class(quote.quote_type if quote else None, holding.security_class)
            if kind != "stock":
                continue
            status, _strength, failed = evaluate(profile, facts_from(bars.get(symbol) or [], quote), settings["gates"][profile])
            if status != "fail" or self.store.active_dismissal(profile, "sell", symbol):
                continue
            sells.append({"symbol": symbol, "account": holding.account_name, "gates": ",".join(failed)})
        return sells

    def _trims(self, settings: dict[str, Any], account: AccountRead) -> list[dict[str, object]]:
        if not account.ok or account.failed_accounts or not account.net_value:
            return []
        grouped: dict[str, float] = {}
        accounts: dict[str, list[str]] = {}
        for holding in account.holdings:
            symbol = normalize_symbol(holding.symbol)
            if not symbol or holding.quantity < 0 or holding.market_value is None:
                continue
            grouped[symbol] = grouped.get(symbol, 0.0) + holding.market_value
            accounts.setdefault(symbol, []).append(holding.account_name)
        cap = float(settings["max_weight"])
        trims = []
        for symbol, value in grouped.items():
            weight = value / float(account.net_value)
            if weight <= cap or self.store.active_dismissal("", "trim", symbol):
                continue
            trims.append({"symbol": symbol, "weight": weight, "accounts": accounts[symbol], "target": cap})
        return trims

    def _name(
        self,
        symbol: str,
        strength: float,
        held: bool,
        advanced: bool,
        provisional: bool,
        quote: Quote | None,
        headlines: tuple[Headline, ...],
        news_status: str,
    ) -> RankedName:
        return RankedName(
            symbol,
            strength,
            held,
            advanced,
            provisional,
            company_name(symbol, quote.long_name if quote else None, quote.short_name if quote else None),
            (quote.industry if quote and quote.industry else ""),
            (quote.summary if quote and quote.summary else ""),
            headlines,
            news_status,
        )

    def _copy(self, row: RankedName, strength: float, provisional: bool, status: str, headlines: tuple) -> RankedName:
        return RankedName(
            row.symbol,
            strength,
            row.held,
            row.advanced,
            provisional,
            row.company_name,
            row.industry,
            row.summary,
            headlines,
            status,
        )

    def _also_on(self, profile: str) -> dict[str, list[str]]:
        labels = {"compounder": "Compounder", "dividend": "Dividend", "short_term": "Short-term"}
        found: dict[str, list[str]] = {}
        for other in PROFILE_IDS:
            if other == profile:
                continue
            saved = self.store.latest(other) or {}
            for symbol in saved.get("provisional") or []:
                found.setdefault(str(symbol), []).append(labels[other])
        return found

    def _day(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _shown(self, row: RankedName) -> dict[str, Any]:
        return {
            "symbol": row.symbol,
            "company_name": row.company_name,
            "industry": row.industry,
            "summary": row.summary,
            "headlines": [{"title": item.title, "url": item.url, "theme": item.theme} for item in row.headlines],
        }

    def _dump(self, row: RankedName) -> dict[str, Any]:
        return {
            "symbol": row.symbol,
            "strength": row.strength,
            "held": row.held,
            "advanced": row.advanced,
            "provisional": row.provisional,
            "company_name": row.company_name,
            "industry": row.industry,
            "summary": row.summary,
            "news_status": row.news_status,
            "headlines": [{"title": item.title, "url": item.url, "theme": item.theme} for item in row.headlines],
        }
