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

    def daily(self, symbols: list[str], trading_days: int) -> dict[str, list[Bar]]:
        self.requested_days = trading_days
        return {symbol: self.rows[symbol] for symbol in symbols if symbol in self.rows}


class MemoryQuotes:
    def __init__(self, rows: dict[str, Quote], limit: int | None = None) -> None:
        self.rows = rows
        self.limit = limit
        self.calls: list[str] = []

    def fetch(self, symbols: list[str], *, limit: int) -> dict[str, Quote]:
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


def research(root: Path, symbols: list[str], quotes: dict[str, Quote], news: dict[str, list[Headline] | None] | None = None, account: AccountRead | None = None, bar_rows: dict[str, list[Bar]] | None = None, refresh_limit: int = 40) -> MarketResearch:
    rows = bar_rows or {symbol: bars(100) for symbol in symbols}
    sources = Sources(
        listings=MemoryListings(symbols),
        bars=MemoryBars(rows),
        quotes=MemoryQuotes(quotes),
        news=MemoryNews(news or {symbol: [Headline(f"{symbol} quiet", "https://example.test/quiet")] for symbol in symbols}),
        holdings=MemoryHoldings(account or AccountRead(True, "fake", 1000.0)),
        refresh_limit=refresh_limit,
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
        tool = research(self.root, symbols, quotes, refresh_limit=60)
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
        tool = research(self.root, names, {name: quote(name) for name in names}, refresh_limit=3)
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


def tool_gates(profile: str) -> dict[str, float]:
    from core.market.criteria import default_gates

    return default_gates(profile)
