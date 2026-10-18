# File: core/tests/test_market.py

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.market.etrade import EtradeHoldings
from core.market.criteria import evaluate, facts_from
from core.market.headlines import adjust, tag_headlines
from core.market.models import AccountRead, Bar, Headline, Holding, Quote, Sources
from core.market.report import days_on_list, news_note
from core.market.research import MarketResearch
from core.market.store import MarketStore
from core.market.symbols import normalize_symbol


def sym(index: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return alphabet[index // 26] + alphabet[index % 26] + "X"


def bars(close: float, volume: float = 800_000, days: int = 210, older: float | None = None) -> list[Bar]:
    start = older if older is not None else close
    step = (close - start) / (days - 1)
    return [Bar(start + step * index, volume) for index in range(days)]


def quote(symbol: str, **overrides: object) -> Quote:
    payload = dict(
        symbol=symbol,
        long_name=f"{symbol} Inc",
        short_name=symbol,
        summary=f"{symbol} makes things. It sells them.",
        industry="Software",
        quote_type="EQUITY",
        trailing_eps=2.0,
        revenue_growth=0.20,
        return_on_equity=0.25,
        profit_margin=0.20,
        debt_to_equity=0.2,
        free_cash_flow=10.0,
        trailing_pe=18.0,
        dividend_yield=0.04,
        payout_ratio=0.40,
    )
    payload.update(overrides)
    return Quote(**payload)


class MemoryListings:
    def __init__(self, symbols: list[str]) -> None:
        self._symbols = symbols

    def symbols(self) -> list[str]:
        return list(self._symbols)


class MemoryBars:
    def __init__(self, rows: dict[str, list[Bar]]) -> None:
        self.rows = rows
        self.requested_days = 0

    def daily(self, symbols: list[str], trading_days: int, on_progress: object = None) -> dict[str, list[Bar]]:
        self.requested_days = trading_days
        return {symbol: self.rows[symbol] for symbol in symbols if symbol in self.rows}


class MemoryQuotes:
    def __init__(self, rows: dict[str, Quote], limit: int | None = None) -> None:
        self.rows = rows
        self.limit = limit
        self.calls: list[str] = []

    def fetch(self, symbols: list[str], *, limit: int, on_progress: object = None) -> dict[str, Quote]:
        cap = self.limit if self.limit is not None else limit
        chosen = symbols[:cap]
        self.calls.extend(chosen)
        return {symbol: self.rows[symbol] for symbol in chosen if symbol in self.rows}


class MemoryNews:
    def __init__(self, rows: dict[str, list[Headline] | None]) -> None:
        self.rows = rows

    def headlines(self, symbol: str, *, limit: int = 5) -> list[Headline] | None:
        found = self.rows.get(symbol)
        if found is None and symbol not in self.rows:
            return []
        if found is None:
            return None
        return found[:limit]


class MemoryHoldings:
    def __init__(self, account: AccountRead) -> None:
        self.account = account

    def read(self) -> AccountRead:
        return self.account


def research(root: Path, symbols: list[str], quotes: dict[str, Quote], news: dict[str, list[Headline] | None] | None = None, account: AccountRead | None = None, bar_rows: dict[str, list[Bar]] | None = None) -> MarketResearch:
    rows = bar_rows or {symbol: bars(100) for symbol in symbols}
    sources = Sources(
        listings=MemoryListings(symbols),
        bars=MemoryBars(rows),
        quotes=MemoryQuotes(quotes),
        news=MemoryNews(news or {symbol: [Headline(f"{symbol} quiet", "https://example.test/quiet")] for symbol in symbols}),
        holdings=MemoryHoldings(account or AccountRead(True, "fake", 1000.0)),
    )
    return MarketResearch(MarketStore(root), sources)


class MarketResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_price_band_and_held_sell(self) -> None:
        cheap = quote("CHEAP")
        rich = quote("RICH")
        held = quote("HELD", revenue_growth=0.01)
        tool = research(
            self.root,
            ["CHEAP", "RICH", "HELD"],
            {"CHEAP": cheap, "RICH": rich, "HELD": held},
            bar_rows={
                "CHEAP": bars(1.5),
                "RICH": bars(500),
                "HELD": bars(50),
            },
            account=AccountRead(True, "fake", 1000, (Holding("HELD", 10, 100, "stock", "1", "Brokerage"),)),
        )
        report = tool.run()
        self.assertNotIn("CHEAP", report.chat)
        self.assertNotIn("RICH", report.chat)
        latest = tool.store.latest()
        self.assertEqual(latest["sells"][0]["symbol"], "HELD")

    def test_missing_field_is_unchecked_not_a_sell(self) -> None:
        held = quote("HELD", trailing_eps=None)
        tool = research(
            self.root,
            ["HELD"],
            {"HELD": held},
            account=AccountRead(True, "fake", 1000, (Holding("HELD", 1, 10, "stock", "1", "Brokerage"),)),
        )
        tool.run()
        self.assertEqual(tool.store.latest("compounder")["sells"], [])

    def test_etf_trim_not_sell(self) -> None:
        fund = quote("FUND", quote_type="ETF", trailing_eps=None)
        tool = research(
            self.root,
            ["FUND"],
            {"FUND": fund},
            account=AccountRead(True, "fake", 1000, (Holding("FUND", 1, 120, "etf", "1", "Brokerage"),)),
        )
        tool.run()
        latest = tool.store.latest()
        self.assertEqual(latest["sells"], [])
        self.assertEqual(latest["trims"][0]["symbol"], "FUND")

    def test_pass_order_and_advance_cap(self) -> None:
        symbols = [sym(index) for index in range(60)]
        quotes = {symbol: quote(symbol, revenue_growth=0.10 + index / 1000) for index, symbol in enumerate(symbols)}
        tool = research(self.root, symbols, quotes)
        report = tool.run()
        latest = tool.store.latest()
        ranked = latest["ranked"]
        self.assertEqual(len(ranked), 60)
        self.assertEqual(sum(1 for row in ranked if row["advanced"]), 50)
        self.assertLessEqual(report.provisional_count, 10)
        self.assertEqual(tool.sources.bars.requested_days, 200)

    def test_nine_passers_advance_nine(self) -> None:
        symbols = [sym(index) for index in range(9)]
        tool = research(self.root, symbols, {symbol: quote(symbol) for symbol in symbols})
        tool.run()
        ranked = tool.store.latest()["ranked"]
        self.assertEqual(sum(1 for row in ranked if row["advanced"]), 9)

    def test_dismissal_is_per_profile(self) -> None:
        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL")})
        tool.store.dismiss("compounder", "buy", "AAPL")
        tool.run()
        self.assertEqual(tool.store.latest("compounder")["provisional"], [])
        tool.store.set_active_profile("short_term")
        tool.sources.quotes.rows["AAPL"] = quote("AAPL", revenue_growth=0.20)
        tool.sources.bars.rows["AAPL"] = [Bar(100, 800_000) for _ in range(189)] + [Bar(100, 800_000) for _ in range(20)] + [Bar(110, 800_000)]
        report = tool.run(profile="short_term")
        self.assertTrue(report.ok)
        self.assertIn("AAPL", report.chat)

    def test_profile_gates(self) -> None:
        dividend = facts_from(bars(100), quote("DV", dividend_yield=0.02))
        self.assertEqual(evaluate("dividend", dividend, tool_gates("dividend"))[0], "fail")
        short = facts_from(bars(110, older=90), quote("ST", revenue_growth=0.10))
        self.assertEqual(evaluate("short_term", short, tool_gates("short_term"))[0], "fail")
        trend_bars = [Bar(130, 800_000) for _ in range(160)] + [Bar(100, 800_000) for _ in range(49)] + [Bar(115, 800_000)]
        trend = facts_from(trend_bars, quote("TR", revenue_growth=0.20))
        self.assertEqual(evaluate("short_term", trend, tool_gates("short_term"))[0], "pass")
        self.assertEqual(evaluate("compounder", trend, tool_gates("compounder"))[0], "fail")

    def test_severe_headline_and_bump_cap(self) -> None:
        severe = tag_headlines([Headline("SEC charges the company", "https://example.test/sec"), Headline("New patent", "https://example.test/patent")])
        strength, status = adjust(0.9, severe, excluded=False)
        self.assertEqual((strength, status), (0.0, "severe"))
        patents = tag_headlines([Headline(f"Patent {index}", "https://example.test/p") for index in range(5)])
        bumped, _status = adjust(0.5, patents, excluded=False)
        self.assertAlmostEqual(bumped, 0.51)
        every = tag_headlines(
            [
                Headline("Company beats estimates", "https://example.test/beat"),
                Headline("Company expects a profitable quarter", "https://example.test/forecast"),
                Headline("Company unveils new technology", "https://example.test/product"),
                Headline("Company files a patent", "https://example.test/patent"),
            ]
        )
        capped, _status = adjust(0.5, every, excluded=False)
        self.assertAlmostEqual(capped, 0.58)
        self.assertLess(0.80 + 0.08, 0.90)

    def test_combined_trim_and_failed_account(self) -> None:
        tool = research(
            self.root,
            ["AAPL"],
            {"AAPL": quote("AAPL")},
            account=AccountRead(
                True,
                "fake",
                1000,
                (
                    Holding("AAPL", 1, 60, "stock", "1", "Brokerage"),
                    Holding("AAPL", 1, 60, "stock", "2", "IRA"),
                ),
            ),
        )
        tool.run()
        self.assertAlmostEqual(tool.store.latest()["trims"][0]["weight"], 0.12)
        tool.sources.holdings.account = AccountRead(False, "fake", 1000, tool.sources.holdings.account.holdings, ("IRA",))
        tool.refresh(("compounder",))
        self.assertEqual(tool.store.latest("compounder")["trims"], [])

    def test_alpaca_reads_secrets_when_called(self) -> None:
        from core.market.adapters import AlpacaListings, _alpaca_credentials

        class Store:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}

            def get(self, name: str) -> str:
                return self.values.get(name, "")

        store = Store()
        listings = AlpacaListings(store)
        self.assertIn("not set", listings.symbols() or "not set")
        self.assertIn("not set", listings.problem)
        store.values["alpaca:key"] = "key"
        store.values["alpaca:secret"] = "secret"
        self.assertEqual(_alpaca_credentials(store), ("key", "secret"))

    def test_symbol_alias_and_no_order_path(self) -> None:
        self.assertEqual(normalize_symbol("brk.b"), "BRK-B")
        source = Path(__file__).resolve().parents[1].joinpath("market", "etrade.py").read_text(encoding="utf-8")
        self.assertNotIn("/orders", source)
        self.assertFalse(hasattr(EtradeHoldings, "place"))

    def test_report_shape(self) -> None:
        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL", long_name="Apple Inc", summary="Apple designs devices. It sells services.")})
        report = tool.run()
        self.assertTrue(report.chat.startswith("1. AAPL Apple Inc"))
        self.assertIn("Industry: Software", report.details)
        self.assertNotIn("chart", report.details.lower())
        self.assertNotIn("```", report.details)

    def test_tiny_listings_do_not_replace_cache(self) -> None:
        names = [sym(index) for index in range(120)]
        tool = research(self.root, names, {name: quote(name) for name in names})
        tool.run()
        tool.sources.listings._symbols = ["ONLY"]
        tool.sources.quotes.rows["ONLY"] = quote("ONLY")
        tool.sources.bars.rows["ONLY"] = bars(100)
        tool.run()
        self.assertGreater(len(tool.store.listings()), 50)

    def test_daytime_run_reads_saved_profiles(self) -> None:
        names = [sym(index) for index in range(3)]
        tool = research(self.root, names, {name: quote(name) for name in names})
        tool.refresh()
        self.assertIsNotNone(tool.store.latest("compounder"))
        self.assertIsNotNone(tool.store.latest("dividend"))
        self.assertIsNotNone(tool.store.latest("short_term"))
        tool.sources.quotes.calls.clear()
        report = tool.run(profile="dividend")
        self.assertEqual(tool.sources.quotes.calls, [])
        self.assertTrue(report.chat.startswith("1. "))
        settings = tool.store.settings()
        settings["versions"]["dividend"] = int(settings["versions"]["dividend"]) + 1
        tool.store.save_settings(settings)
        tool.run(profile="dividend")
        self.assertGreater(len(tool.sources.quotes.calls), 0)

    def test_shared_profile_is_named(self) -> None:
        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL")})
        tool.store.save_run({"profile": "dividend", "provisional": ["AAPL"], "ranked": [], "gate_version": 1, "shared": tool.store.shared_stamp()})
        report = tool.run()
        self.assertIn("Also on Dividend.", report.chat)

    def test_screen_shows_streak_and_conflict(self) -> None:
        history = [
            {"day": "2026-10-16", "shown": [{"symbol": "AAPL", "headlines": [{"title": "Apple files a patent", "theme": "patent"}]}]},
            {"day": "2026-10-17", "shown": [{"symbol": "AAPL", "headlines": [{"title": "Apple patent was registered", "theme": "patent"}]}]},
        ]
        self.assertEqual(days_on_list(history, "AAPL", "2026-10-17"), 2)
        note = news_note(history, "AAPL", (Headline("Apple patent was registered", "https://example.test/reg", "patent"),), "2026-10-17")
        self.assertIn("registered", note)
        fell = news_note(history, "AAPL", (Headline("Apple patent fell through", "https://example.test/no", "patent"),), "2026-10-17")
        self.assertIn("fell through", fell)

    def test_history_keeps_each_day(self) -> None:
        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL")})
        tool.run()
        tool.store.remember_shown("compounder", [{"symbol": "AAPL", "company_name": "Apple Inc", "industry": "Software", "summary": "Apple designs devices.", "headlines": []}], "2026-10-16")
        days = [row["day"] for row in tool.store.history("compounder")]
        self.assertIn("2026-10-16", days)
        self.assertGreaterEqual(len(days), 2)
        self.assertEqual(tool.store.history("compounder")[-1]["shown"][0]["symbol"], "AAPL")

    def test_empty_universe_does_not_write_top_10(self) -> None:
        tool = research(self.root, [], {})
        report = tool.run()
        self.assertFalse(report.ok)
        self.assertIn("No listed symbols", report.message)
        self.assertIsNone(tool.store.latest("compounder"))

    def test_summary_detail_fields_are_read_as_fractions(self) -> None:
        from core.market.adapters import quote_from_summary

        result = {
            "price": {"quoteType": "EQUITY"},
            "summaryDetail": {"trailingPE": {"raw": 28.5}, "dividendYield": {"raw": 0.031}, "payoutRatio": {"raw": 0.45}},
            "financialData": {"revenueGrowth": {"raw": 1.5}, "returnOnEquity": {"raw": 1.6}, "debtToEquity": {"raw": 150.0}},
        }
        parsed = quote_from_summary("AAPL", result)
        self.assertEqual((parsed.trailing_pe, parsed.dividend_yield, parsed.payout_ratio), (28.5, 0.031, 0.45))
        self.assertEqual((parsed.revenue_growth, parsed.return_on_equity), (1.5, 1.6))
        self.assertEqual(parsed.debt_to_equity, 1.5)

    def test_newer_dismissal_counts_after_gate_change(self) -> None:
        store = MarketStore(self.root)
        store.dismiss("compounder", "buy", "AAPL")
        settings = store.settings()
        settings["versions"]["compounder"] = 2
        store.save_settings(settings)
        self.assertNotIn("AAPL", store.dismissed("compounder", "buy"))
        store.dismiss("compounder", "buy", "AAPL")
        self.assertIn("AAPL", store.dismissed("compounder", "buy"))

    def test_named_profile_report_and_active_profile_kept(self) -> None:
        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL")})
        report = tool.run(profile="dividend")
        self.assertEqual(report.profile, "dividend")
        self.assertTrue(report.ok)
        self.assertEqual(tool.store.settings()["active_profile"], "compounder")
        self.assertIsNone(tool.store.latest("compounder"))

    def test_saved_run_from_another_day_is_refreshed(self) -> None:
        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL")})
        tool.run()
        saved = tool.store.latest("compounder")
        saved["day"] = "2000-01-01"
        tool.store.save_run(saved)
        tool.sources.quotes.calls.clear()
        tool.run()
        self.assertEqual(tool.sources.quotes.calls, ["AAPL"])
        status = (self.root / "status.txt").read_text(encoding="utf-8")
        self.assertIn("Refresh finished", status)

    def test_failed_bars_page_drops_the_whole_chunk(self) -> None:
        import httpx

        from core.market.adapters import AlpacaBars

        def handler(request: httpx.Request) -> httpx.Response:
            if "page_token" in request.url.params:
                return httpx.Response(429)
            return httpx.Response(200, json={"bars": {"AAPL": [{"c": 1, "v": 1}]}, "next_page_token": "next"})

        class Secrets:
            def get(self, name: str) -> str:
                return "value"

        source = AlpacaBars(Secrets(), httpx.MockTransport(handler))
        seen: list[tuple[str, str]] = []
        self.assertEqual(source.daily(["AAPL"], 200, on_progress=lambda text, role: seen.append((text, role))), {})
        self.assertEqual(seen[0][1], "error")
        self.assertIn("HTTP 429", seen[0][0])
        self.assertEqual(seen[1], ("Daily bars 1 of 1.", "progress"))

    def test_bars_ask_alpaca_for_class_shares_with_a_dot(self) -> None:
        import httpx

        from core.market.adapters import AlpacaBars

        asked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(request.url.params["symbols"])
            return httpx.Response(200, json={"bars": {"BRK.B": [{"c": 1, "v": 1}]}})

        class Secrets:
            def get(self, name: str) -> str:
                return "value"

        found = AlpacaBars(Secrets(), httpx.MockTransport(handler)).daily(["AAPL", "BRK-B"], 200)
        self.assertEqual(asked, ["AAPL,BRK.B"])
        self.assertEqual(list(found), ["BRK-B"])

    def test_listings_network_error_sets_problem(self) -> None:
        import httpx

        from core.market.adapters import AlpacaListings

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        class Secrets:
            def get(self, name: str) -> str:
                return "value"

        listings = AlpacaListings(Secrets(), httpx.MockTransport(handler))
        self.assertEqual(listings.symbols(), [])
        self.assertIn("ConnectError", listings.problem)

    def test_listings_fall_back_to_paper_endpoint(self) -> None:
        import httpx

        from core.market.adapters import AlpacaListings

        hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            hosts.append(request.url.host)
            if request.url.host == "api.alpaca.markets":
                return httpx.Response(401)
            return httpx.Response(200, json=[{"symbol": "AAPL", "exchange": "NASDAQ", "tradable": True}])

        class Secrets:
            def get(self, name: str) -> str:
                return " value "

        listings = AlpacaListings(Secrets(), httpx.MockTransport(handler))
        self.assertEqual(listings.symbols(), ["AAPL"])
        self.assertEqual(hosts, ["api.alpaca.markets", "paper-api.alpaca.markets"])

    def test_refresh_error_is_reported(self) -> None:
        from core.assistant.market_command import MarketCommandHandler

        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL")})

        def broken(*args: object, **kwargs: object) -> dict:
            raise RuntimeError("boom")

        tool.sources.bars.daily = broken
        state: dict = {}
        MarketCommandHandler(tool).handle("/market refresh", state)
        self.assertTrue(state["command_failed"])
        self.assertIn("RuntimeError: boom", state["command_conversation"])

    def test_refresh_problems_are_reported_as_they_happen(self) -> None:
        from core.assistant.market_command import MarketCommandHandler

        tool = research(self.root, ["AAPL", "MSFT"], {"AAPL": quote("AAPL"), "MSFT": quote("MSFT")}, news={"AAPL": None, "MSFT": None})
        lines: list[tuple[str, str | None]] = []
        state: dict = {}
        MarketCommandHandler(tool, output=lambda text, role=None: lines.append((text, role))).handle("/market refresh", state)
        errors = [text for text, role in lines if role == "error"]
        self.assertTrue(any("Headlines could not be read for AAPL" in text for text in errors))
        first_error = next(index for index, (_text, role) in enumerate(lines) if role == "error")
        self.assertLess(first_error, len(lines) - 1)
        self.assertIn("problem", lines[-1][0])
        self.assertIn("AAPL", state["command_conversation"])
        self.assertIn("Headlines not checked.", state["command_conversation"])
        self.assertIn("Headlines: not checked", state["command_detail"]["content"])

    def test_switching_profile_shows_its_saved_list(self) -> None:
        from core.assistant.market_command import MarketCommandHandler

        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL")})
        handler = MarketCommandHandler(tool, output=lambda text, role=None: None)
        state: dict = {}
        handler.handle("/market profile dividend", state)
        self.assertIn("no saved dividend list yet", state["command_conversation"])
        tool.refresh()
        state = {}
        handler.handle("/market profile Dividend", state)
        self.assertTrue(state["command_conversation"].startswith("Active profile: dividend.\n1. AAPL"))
        self.assertEqual(state["command_detail"]["title"], "Market research")
        self.assertEqual(tool.store.settings()["active_profile"], "dividend")

    def test_every_market_command_offers_profile_buttons(self) -> None:
        from core.application.service import command_suggestions
        from core.assistant.market_command import PROFILE_BUTTONS, MarketCommandHandler

        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL")})
        handler = MarketCommandHandler(tool, output=lambda text, role=None: None)
        for command in ("/market", "/market criteria", "/market nonsense", "/market run bogus"):
            state: dict = {}
            handler.handle(command, state)
            self.assertEqual(state["command_actions"], list(PROFILE_BUTTONS))
        suggestions = command_suggestions(PROFILE_BUTTONS)
        self.assertEqual([item.label for item in suggestions], ["Compounder top 10", "Dividend top 10", "Short-term top 10"])
        self.assertEqual(suggestions[1].payload, {"command": "/market run dividend"})

    def test_run_button_shows_that_profile_without_switching(self) -> None:
        from core.assistant.market_command import MarketCommandHandler

        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL")})
        tool.refresh()
        state: dict = {}
        MarketCommandHandler(tool, output=lambda text, role=None: None).handle("/market run short_term", state)
        self.assertNotIn("command_failed", state)
        self.assertEqual(tool.store.settings()["active_profile"], "compounder")
        self.assertEqual(state["command_conversation"], tool.report_saved("short_term").chat)
        self.assertNotEqual(state["command_conversation"], tool.report_saved("compounder").chat)

    def test_name_with_no_headlines_this_week_is_listed(self) -> None:
        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL")}, news={"MSFT": []})
        report = tool.run()
        self.assertIn("AAPL", report.chat)
        self.assertNotIn("Headlines not checked.", report.chat)
        self.assertIn("Headlines: none found this week", report.details)

    def test_no_bars_fails_with_a_reason(self) -> None:
        tool = research(self.root, ["AAPL"], {"AAPL": quote("AAPL")}, bar_rows={"ZZZ": bars(100)})
        report = tool.refresh()
        self.assertFalse(report.ok)
        self.assertIn("No daily bars", report.message)
        self.assertIn(report.message, report.problems)
        self.assertIn("Refresh stopped", (self.root / "status.txt").read_text(encoding="utf-8"))

    def test_yahoo_uses_crumb_and_reports_failures(self) -> None:
        import httpx

        from core.market.adapters import YahooQuotes

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "fc.yahoo.com":
                return httpx.Response(404)
            if request.url.path == "/v1/test/getcrumb":
                return httpx.Response(200, text="crumb123")
            if request.url.params.get("crumb") != "crumb123":
                return httpx.Response(401)
            if request.url.path.endswith("/BAD"):
                return httpx.Response(500)
            return httpx.Response(200, json={"quoteSummary": {"result": [{"price": {"quoteType": "EQUITY"}, "summaryDetail": {"trailingPE": {"raw": 20.0}}}]}})

        seen: list[tuple[str, str]] = []
        found = YahooQuotes(httpx.MockTransport(handler)).fetch(["AAPL", "BAD"], limit=2, on_progress=lambda text, role: seen.append((text, role)))
        self.assertEqual(list(found), ["AAPL"])
        self.assertEqual(found["AAPL"].trailing_pe, 20.0)
        errors = [text for text, role in seen if role == "error"]
        self.assertIn("Yahoo fundamentals failed for BAD: HTTP 500.", errors)

    def test_yahoo_gives_up_after_repeated_failures(self) -> None:
        import httpx

        from core.market.adapters import YahooQuotes

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/test/getcrumb":
                return httpx.Response(200, text="crumb123")
            if "quoteSummary" in request.url.path:
                calls.append(request.url.path)
                return httpx.Response(503)
            return httpx.Response(404)

        seen: list[tuple[str, str]] = []
        YahooQuotes(httpx.MockTransport(handler), give_up_after=3).fetch([f"S{index}" for index in range(10)], limit=10, on_progress=lambda text, role: seen.append((text, role)))
        self.assertEqual(len(calls), 3)
        self.assertTrue(any("stopped at 3 of 10" in text for text, _role in seen))

    def test_morning_job_failure_notifies(self) -> None:
        from core.scheduler.jobs import market_morning

        tool = research(self.root, [], {})
        result = market_morning(tool)({})
        self.assertFalse(result.ok)
        self.assertTrue(result.notify)
        self.assertIn("No listed symbols", result.summary)

    def test_failed_scheduled_run_is_shown_once_on_open(self) -> None:
        from types import SimpleNamespace

        from core.application.service import IrisApplication
        from core.scheduler.models import JobDefinition, JobState

        definition = JobDefinition(job="market_research", id="market-morning")
        state = JobState(last_run="2026-10-18T10:00:00+00:00", last_error="Alpaca keys are not set.")
        schedules = SimpleNamespace(definitions=lambda: [definition], state=lambda job_id: state)
        app = SimpleNamespace(schedules=schedules, market_research=SimpleNamespace(store=MarketStore(self.root)))
        first = IrisApplication._market_failure_notices(app)
        self.assertEqual(len(first), 1)
        self.assertIn("Alpaca keys are not set.", first[0].text)
        self.assertEqual(IrisApplication._market_failure_notices(app), [])


def tool_gates(profile: str) -> dict[str, float]:
    from core.market.criteria import default_gates

    return default_gates(profile)
