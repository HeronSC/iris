# File: core/market/criteria.py

from __future__ import annotations

from dataclasses import dataclass

from core.market.models import Bar, Quote


PROFILE_IDS = ("compounder", "dividend", "short_term")
SHARED_KEYS = ("min_price", "max_price", "min_avg_share_volume_5", "max_weight", "advance_n")


@dataclass(frozen=True)
class Facts:
    last_close: float | None
    avg_volume_5: float | None
    sma_50: float | None
    sma_200: float | None
    return_20: float | None
    quote: Quote | None


def facts_from(bars: list[Bar], quote: Quote | None) -> Facts:
    closes = [bar.close for bar in bars if bar.close > 0]
    volumes = [bar.volume for bar in bars if bar.volume >= 0]
    last = closes[-1] if closes else None
    avg_volume = sum(volumes[-5:]) / 5 if len(volumes) >= 5 and len(closes) >= 5 else None
    sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
    sma_200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
    return_20 = None
    if len(closes) >= 21 and closes[-21] > 0 and last is not None:
        return_20 = (last / closes[-21]) - 1
    return Facts(last, avg_volume, sma_50, sma_200, return_20, quote)


def clearance(value: float | None, gate: float, better: float, *, higher_is_better: bool) -> float | None:
    if value is None:
        return None
    span = better - gate
    if span == 0:
        return 1.0 if value == gate else None
    if higher_is_better:
        if value < gate:
            return None
        return max(0.0, min(1.0, (value - gate) / span))
    if value > gate:
        return None
    return max(0.0, min(1.0, (gate - value) / (gate - better)))


@dataclass(frozen=True)
class GateResult:
    name: str
    status: str
    clearance: float | None


def _result(name: str, value: float | None, gate: float, better: float, *, higher_is_better: bool) -> GateResult:
    if value is None:
        return GateResult(name, "unchecked", None)
    passed = value >= gate if higher_is_better else value <= gate
    if not passed:
        return GateResult(name, "fail", None)
    return GateResult(name, "pass", clearance(value, gate, better, higher_is_better=higher_is_better))


def evaluate(profile: str, facts: Facts, gates: dict[str, float]) -> tuple[str, float | None, tuple[str, ...]]:
    quote = facts.quote
    rows: list[GateResult] = []
    if profile == "compounder":
        rows = [
            _flag("earnings", quote.trailing_eps if quote else None, positive=True),
            _result("revenue_growth", quote.revenue_growth if quote else None, gates["min_revenue_growth"], 0.25, higher_is_better=True),
            _result("roe", quote.return_on_equity if quote else None, gates["min_roe"], 0.30, higher_is_better=True),
            _result("margin", quote.profit_margin if quote else None, gates["min_profit_margin"], 0.25, higher_is_better=True),
            _result("debt", quote.debt_to_equity if quote else None, gates["max_debt_to_equity"], 0.0, higher_is_better=False),
            _flag("free_cash_flow", quote.free_cash_flow if quote else None, positive=True),
            _result("pe", quote.trailing_pe if quote else None, gates["max_trailing_pe"], 15.0, higher_is_better=False),
            _above("sma_200", facts.last_close, facts.sma_200, 0.15),
        ]
    elif profile == "dividend":
        rows = [
            _result("yield", quote.dividend_yield if quote else None, gates["min_yield"], 0.06, higher_is_better=True),
            _result("payout", quote.payout_ratio if quote else None, gates["max_payout"], 0.30, higher_is_better=False),
            _flag("free_cash_flow", quote.free_cash_flow if quote else None, positive=True),
            _flag("earnings", quote.trailing_eps if quote else None, positive=True),
            _result("debt", quote.debt_to_equity if quote else None, gates["max_debt_to_equity"], 0.0, higher_is_better=False),
            _floor("sma_200", facts.last_close, facts.sma_200, 0.20),
        ]
    elif profile == "short_term":
        rows = [
            _flag("earnings", quote.trailing_eps if quote else None, positive=True),
            _result("revenue_growth", quote.revenue_growth if quote else None, gates["min_revenue_growth"], 0.40, higher_is_better=True),
            _above("sma_50", facts.last_close, facts.sma_50, 0.15),
            _result("return_20", facts.return_20, gates["min_return_20"], 0.25, higher_is_better=True),
        ]
    else:
        return "fail", None, ("profile",)
    if any(row.status == "fail" for row in rows):
        return "fail", None, tuple(row.name for row in rows if row.status == "fail")
    if any(row.status == "unchecked" for row in rows):
        return "unchecked", None, tuple(row.name for row in rows if row.status == "unchecked")
    scored = [row.clearance for row in rows if row.clearance is not None]
    strength = sum(scored) / len(scored) if scored else 0.0
    return "pass", strength, ()


def _flag(name: str, value: float | None, *, positive: bool) -> GateResult:
    if value is None:
        return GateResult(name, "unchecked", None)
    if positive and value > 0:
        return GateResult(name, "pass", 1.0)
    return GateResult(name, "fail", None)


def _above(name: str, price: float | None, average: float | None, better: float) -> GateResult:
    if price is None or average is None or average <= 0:
        return GateResult(name, "unchecked", None)
    distance = (price / average) - 1
    if distance < 0:
        return GateResult(name, "fail", None)
    return GateResult(name, "pass", max(0.0, min(1.0, distance / better)))


def _floor(name: str, price: float | None, average: float | None, allowed_below: float) -> GateResult:
    if price is None or average is None or average <= 0:
        return GateResult(name, "unchecked", None)
    distance = (price / average) - 1
    if distance < -allowed_below:
        return GateResult(name, "fail", None)
    span = allowed_below
    score = 1.0 if distance >= 0 else max(0.0, min(1.0, (distance + allowed_below) / span))
    return GateResult(name, "pass", score)


def default_gates(profile: str) -> dict[str, float]:
    if profile == "dividend":
        return {
            "min_yield": 0.025,
            "max_payout": 0.60,
            "max_debt_to_equity": 1.5,
        }
    if profile == "short_term":
        return {
            "min_revenue_growth": 0.15,
            "min_return_20": 0.08,
        }
    return {
        "min_revenue_growth": 0.08,
        "min_roe": 0.15,
        "min_profit_margin": 0.10,
        "max_debt_to_equity": 1.0,
        "max_trailing_pe": 35.0,
    }


def default_settings() -> dict[str, object]:
    return {
        "active_profile": "compounder",
        "advance_n": 50,
        "max_weight": 0.10,
        "min_price": 2.0,
        "max_price": 400.0,
        "min_avg_share_volume_5": 700_000.0,
        "versions": {profile: 1 for profile in PROFILE_IDS},
        "gates": {profile: default_gates(profile) for profile in PROFILE_IDS},
    }


def passes_price_volume(facts: Facts, settings: dict[str, object]) -> str:
    if facts.last_close is None or facts.avg_volume_5 is None:
        return "unchecked"
    if not float(settings["min_price"]) <= facts.last_close <= float(settings["max_price"]):
        return "fail"
    if facts.avg_volume_5 < float(settings["min_avg_share_volume_5"]):
        return "fail"
    return "pass"


def security_class(quote_type: str | None, broker_class: str | None) -> str | None:
    yahoo = (quote_type or "").upper()
    if yahoo == "EQUITY":
        return "stock"
    if yahoo in {"ETF", "MUTUALFUND"}:
        return "fund"
    broker = (broker_class or "").lower()
    if broker in {"stock", "equity", "eq"}:
        return "stock"
    if broker in {"fund", "etf", "mutualfund", "mf"}:
        return "fund"
    if broker in {"option", "optn"}:
        return "option"
    if broker == "bond":
        return "bond"
    if broker == "cash":
        return "cash"
    return None
