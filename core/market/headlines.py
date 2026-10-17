# File: core/market/headlines.py

from __future__ import annotations

from core.market.models import Headline

SEVERE = ("fraud", "sec charge", "sec charges", "bankruptcy", "restatement", "criminal")
REPORTED = ("beat estimates", "beats estimates", "earnings beat", "raised guidance", "raises guidance")
PATENT = ("patent",)
PRODUCT = ("new technology", "product launch", "unveils", "launches")
FORECAST = ("expects", "forecast", "profitable quarter")
BUMPS = {
    "reported_result": 0.05,
    "company_forecast": 0.02,
    "product": 0.02,
    "patent": 0.01,
}
BUMP_CAP = 0.08


def tag_title(title: str) -> str:
    lowered = title.lower()
    if any(term in lowered for term in SEVERE):
        return "severe"
    if any(term in lowered for term in REPORTED):
        return "reported_result"
    if any(term in lowered for term in PATENT):
        return "patent"
    if any(term in lowered for term in PRODUCT):
        return "product"
    if any(term in lowered for term in FORECAST):
        return "company_forecast"
    return ""


def tag_headlines(headlines: list[Headline]) -> tuple[Headline, ...]:
    tagged: list[Headline] = []
    for headline in headlines[:5]:
        tagged.append(Headline(headline.title, headline.url, tag_title(headline.title)))
    return tuple(tagged)


def adjust(strength: float, headlines: tuple[Headline, ...], *, excluded: bool) -> tuple[float, str]:
    if excluded or any(item.theme == "severe" for item in headlines):
        return 0.0, "severe"
    if not headlines:
        return strength, "missing"
    seen: set[str] = set()
    bump = 0.0
    for headline in headlines:
        if not headline.theme or headline.theme in seen:
            continue
        seen.add(headline.theme)
        bump += BUMPS.get(headline.theme, 0.0)
    return strength + min(bump, BUMP_CAP), "applied"
