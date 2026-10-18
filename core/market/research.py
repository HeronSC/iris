# File: core/market/research.py

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date
from functools import partial
from typing import Any

from core.market.criteria import PROFILE_IDS, evaluate, facts_from, passes_price_volume, security_class
from core.market.headlines import adjust, tag_headlines
from core.market.models import AccountRead, Headline, MarketReport, Quote, RankedName, Sources
from core.market.report import company_name, render
from core.market.store import MarketStore
from core.market.symbols import normalize_symbol

logger = logging.getLogger(__name__)


class MarketResearch:
    def __init__(self, store: MarketStore, sources: Sources) -> None:
        self.store = store
        self.sources = sources

    def run(self, profile: str | None = None, on_progress: Any = None) -> MarketReport:
        settings = self.store.settings()
        profile_id = profile or str(settings["active_profile"])
        if profile_id not in settings["gates"]:
            return MarketReport(False, f"Unknown profile {profile_id}.", "", "", profile_id)
        if self.store.fresh(profile_id, self._day()):
            return self.report_saved(profile_id)
        if self._shared_stale():
            return self.refresh(on_progress=on_progress, focus=profile_id)
        return self.refresh((profile_id,), on_progress=on_progress)

    def refresh(self, profiles: tuple[str, ...] | None = None, on_progress: Any = None, focus: str | None = None) -> MarketReport:
        problems: list[str] = []
        step = partial(self._progress, on_progress, problems)
        try:
            report = self._refresh(profiles, step, focus)
        except Exception as error:
            logger.exception("Market refresh stopped")
            message = f"Market refresh stopped: {error.__class__.__name__}: {error}"
            step(message, "error")
            return MarketReport(False, message, message, message, focus or "", problems=tuple(problems))
        report = replace(report, problems=tuple(problems))
        if not report.ok:
            self.store.write_status(f"Refresh stopped: {report.message}")
        elif problems:
            self.store.write_status(f"Refresh finished with {len(problems)} problem(s). {report.provisional_count} names on the {report.profile} list.")
        else:
            self.store.write_status(f"Refresh finished. {report.provisional_count} names on the {report.profile} list.")
        return report

    def _refresh(self, profiles: tuple[str, ...] | None, step: Any, focus: str | None) -> MarketReport:
        settings = self.store.settings()
        chosen = profiles or PROFILE_IDS
        target = focus or (chosen[0] if profiles else str(settings["active_profile"]))
        if self.sources.history_days < 200:
            return MarketReport(False, "History request was shorter than 200 trading days.", "", "", target)
        account = self.sources.holdings.read()
        step("Reading the listed symbols.")
        symbols = self._universe(account, step)
        if not symbols:
            problem = str(getattr(self.sources.listings, "problem", "") or "No listed symbols were returned.")
            step(problem, "error")
            return MarketReport(False, problem, problem, problem, target)
        step(f"Listed {len(symbols)} symbols. Loading daily bars.")
        bars = self.sources.bars.daily(symbols, self.sources.history_days, on_progress=step)
        if not bars:
            problem = "No daily bars came back from Alpaca, so nothing can be screened."
            step(problem, "error")
            return MarketReport(False, problem, problem, problem, target)
        held = self._held(account)
        screened, band_unchecked = self._screen(symbols, bars, settings, held)
        step(f"Price and volume left {len(screened)} names. Reading fundamentals.")
        quotes = self.sources.quotes.fetch(screened, limit=len(screened), on_progress=step)
        reports = {
            profile_id: self._score(profile_id, settings, len(symbols), screened, band_unchecked, bars, quotes, account, held, step)
            for profile_id in chosen
        }
        return reports.get(target) or self.report_saved(target)

    def _score(
        self,
        profile_id: str,
        settings: dict[str, Any],
        universe: int,
        screened: list[str],
        band_unchecked: int,
        bars: dict[str, list],
        quotes: dict[str, Quote],
        account: AccountRead,
        held: set[str],
        step: Any,
    ) -> MarketReport:
        ranked, unchecked, candidate_count = self._rank(profile_id, settings, screened, band_unchecked, bars, quotes, held)
        day = self._day()
        if candidate_count == 0:
            sells = self._sells(profile_id, settings, account, bars, quotes)
            trims = self._trims(settings, account)
            self.store.save_run({
                "profile": profile_id,
                "gate_version": int(settings["versions"][profile_id]),
                "shared": self.store.shared_stamp(settings),
                "ranked": [],
                "day": day,
                "provisional": [],
                "sells": sells,
                "trims": trims,
                "candidate_count": 0,
                "unchecked": unchecked,
                "universe": universe,
            })
            return MarketReport(False, "No name could be scored.", "", "", profile_id, unchecked_count=unchecked, holdings_source=account.source)
        advanced = [row for row in ranked if row.advanced]
        adjusted = self._apply_news(advanced, settings, profile_id, step)
        by_symbol = {row.symbol: row for row in adjusted}
        merged = [by_symbol.get(row.symbol, row) for row in ranked]
        merged.sort(key=lambda row: (-row.strength, row.symbol))
        provisional = [row for row in merged if row.provisional][:10]
        self.store.remember_shown(profile_id, [self._shown(row) for row in provisional], day)
        chat, details = render(provisional, self.store.history(profile_id), day, self._also_on(profile_id))
        payload = {
            "profile": profile_id,
            "gate_version": int(settings["versions"][profile_id]),
            "shared": self.store.shared_stamp(settings),
            "holdings_source": account.source,
            "unchecked": unchecked,
            "universe": universe,
            "candidate_count": candidate_count,
            "ranked": [self._dump(row) for row in merged],
            "day": day,
            "provisional": [row.symbol for row in provisional],
            "sells": self._sells(profile_id, settings, account, bars, quotes),
            "trims": self._trims(settings, account),
        }
        self.store.save_run(payload)
        saved = self.report_saved(profile_id)
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

    def _progress(self, on_progress: Any, problems: list[str], text: str, role: str = "progress") -> None:
        self.store.write_status(text)
        if role == "error":
            problems.append(text)
        if callable(on_progress):
            on_progress(text, role)

    def _held(self, account: AccountRead) -> set[str]:
        held = {normalize_symbol(holding.symbol) for holding in account.holdings}
        held.discard(None)
        return held

    def _screen(self, symbols: list[str], bars: dict[str, list], settings: dict[str, Any], held: set[str]) -> tuple[list[str], int]:
        screened = []
        unchecked = 0
        for symbol in symbols:
            band = passes_price_volume(facts_from(bars.get(symbol) or [], None), settings)
            if symbol in held or band == "pass":
                screened.append(symbol)
            elif band == "unchecked":
                unchecked += 1
        return screened, unchecked

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
        dismissed = self.store.dismissed(profile, "buy")
        ranked = [item for item in latest.get("ranked") or [] if isinstance(item, dict) and item.get("advanced")]
        ranked.sort(key=lambda item: (-float(item.get("strength") or 0), str(item.get("symbol") or "")))
        visible = []
        for item in ranked:
            symbol = str(item.get("symbol") or "")
            if not symbol or symbol in exclusions or symbol in dismissed:
                continue
            if item.get("news_status") == "severe":
                continue
            visible.append(symbol)
            if len(visible) == 10:
                break
        return visible

    def _universe(self, account: AccountRead, step: Any) -> list[str]:
        listed = [normalize_symbol(symbol) for symbol in self.sources.listings.symbols()]
        listed = [symbol for symbol in listed if symbol]
        if listed and self.store.save_listings(listed):
            symbols = listed
        else:
            symbols = self.store.listings() or listed
            if not listed and symbols:
                problem = str(getattr(self.sources.listings, "problem", "") or "Alpaca returned no listed symbols.")
                step(f"{problem} Using the {len(symbols)} cached symbols instead.", "error")
            elif listed and symbols is not listed:
                step(f"Alpaca returned only {len(listed)} symbols against {len(symbols)} cached, so the cached list is used.", "error")
        held = [normalize_symbol(holding.symbol) for holding in account.holdings]
        for symbol in held:
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        return symbols

    def _rank(
        self,
        profile: str,
        settings: dict[str, Any],
        screened: list[str],
        unchecked: int,
        bars: dict[str, list],
        quotes: dict[str, Quote],
        held: set[str],
    ) -> tuple[list[RankedName], int, int]:
        dismissed = self.store.dismissed(profile, "buy")
        rows: list[tuple[str, float, bool]] = []
        for symbol in screened:
            facts = facts_from(bars.get(symbol) or [], quotes.get(symbol))
            status, strength, _failed = evaluate(profile, facts, settings["gates"][profile])
            if status == "unchecked":
                unchecked += 1
            if status != "pass":
                continue
            if symbol in dismissed:
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

    def _apply_news(self, advanced: list[RankedName], settings: dict[str, Any], profile: str, step: Any) -> list[RankedName]:
        exclusions = set(self.store.exclusions())
        adjusted: list[RankedName] = []
        missing = 0
        for row in advanced:
            fetched = self.sources.news.headlines(row.symbol, limit=5)
            excluded = row.symbol in exclusions
            if fetched is None and not excluded:
                if missing == 0:
                    step(f"Headlines could not be read for {row.symbol} ({profile}). Names are listed without a headline check.", "error")
                missing += 1
                adjusted.append(self._copy(row, row.strength, row.strength > 0, "unchecked", ()))
                continue
            headlines = tag_headlines(fetched or [])
            strength, status = adjust(row.strength, headlines, excluded=excluded)
            eligible = status != "severe" and strength > 0 and not excluded
            adjusted.append(self._copy(row, strength, eligible, status, headlines))
        if missing > 1:
            step(f"Headlines could not be read for {missing} of {len(advanced)} {profile} names.", "error")
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
        dismissed = self.store.dismissed(profile, "sell")
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
            if status != "fail" or symbol in dismissed:
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
        dismissed = self.store.dismissed("", "trim")
        trims = []
        for symbol, value in grouped.items():
            weight = value / float(account.net_value)
            if weight <= cap or symbol in dismissed:
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
        return date.today().isoformat()

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
