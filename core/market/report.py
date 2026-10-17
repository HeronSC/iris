# File: core/market/report.py

from __future__ import annotations

from datetime import date, timedelta

from core.market.models import RankedName


def company_name(symbol: str, long_name: str | None, short_name: str | None) -> str:
    return (long_name or short_name or symbol).strip() or symbol


def brief(summary: str | None) -> str:
    text = " ".join((summary or "").split())
    if not text:
        return "missing"
    sentences = []
    start = 0
    for index, character in enumerate(text):
        if character == "." and index + 1 < len(text) and text[index + 1] == " ":
            sentences.append(text[start : index + 1])
            start = index + 2
            if len(sentences) == 2:
                break
    if not sentences:
        return text
    return " ".join(sentences)


def render(names: list[RankedName], history: list[dict] | None = None, day: str = "", also: dict[str, list[str]] | None = None) -> tuple[str, str]:
    if not names:
        return "No names reached the top 10.", "No names reached the top 10."
    saved = history or []
    shared = also or {}
    chat = []
    details = []
    for index, name in enumerate(names, start=1):
        days = days_on_list(saved, name.symbol, day)
        note = news_note(saved, name.symbol, name.headlines, day)
        others = shared.get(name.symbol) or []
        shared_line = f" Also on {', '.join(others)}." if others else ""
        chat.append(f"{index}. {name.symbol} {name.company_name}. On the list {days} {day_word(days)}.{shared_line}")
        lines = [
            f"{index}. {name.symbol} {name.company_name}",
            f"On the list {days} {day_word(days)}.",
        ]
        if others:
            lines.append(f"Also on {', '.join(others)}.")
        lines.extend([
            f"Industry: {name.industry or 'missing'}",
            brief(name.summary),
        ])
        if note:
            lines.append(note)
        if name.headlines:
            lines.extend(f"- {headline.title} ({headline.url})" for headline in name.headlines[:5])
        else:
            lines.append("Headlines: missing")
        details.append("\n".join(lines))
    return "\n".join(chat), "\n\n".join(details)


def day_word(days: int) -> str:
    return "day" if days == 1 else "days"


def days_on_list(history: list[dict], symbol: str, day: str) -> int:
    present = {str(row.get("day")) for row in history if _shown(row, symbol)}
    if day:
        present.add(day)
    if not present:
        return 1
    cursor = day or max(present)
    count = 0
    while cursor in present:
        count += 1
        cursor = _previous(cursor)
        if count > len(present):
            break
    return max(count, 1)


def news_note(history: list[dict], symbol: str, headlines: tuple, day: str) -> str:
    prior = []
    for row in history:
        if day and str(row.get("day")) == day:
            continue
        item = _shown(row, symbol)
        if item:
            prior.extend(item.get("headlines") or [])
    prior_text = " ".join(str(hit.get("title") or "") for hit in prior if isinstance(hit, dict)).lower()
    prior_themes = {str(hit.get("theme") or "") for hit in prior if isinstance(hit, dict)}
    today = " ".join(headline.title for headline in headlines).lower()
    mentioned_patent = "patent" in prior_themes or "patent" in prior_text
    if mentioned_patent and any(word in today for word in ("fell through", "rejected", "abandoned", "invalidated", "denied")):
        return "Earlier headline mentioned a patent. A later one says it fell through."
    if mentioned_patent and any(word in today for word in ("registered", "granted", "issued")):
        return "Earlier headline mentioned a patent. A later one says it was registered."
    expected = "company_forecast" in prior_themes or "profitable quarter" in prior_text
    if expected and any(word in today for word in ("missed", "misses", "loss", "fell short")):
        return "Earlier headline expected a stronger quarter. A later one conflicts with that."
    return ""


def _shown(row: dict, symbol: str) -> dict | None:
    for item in row.get("shown") or []:
        if isinstance(item, dict) and item.get("symbol") == symbol:
            return item
    return None


def _previous(day: str) -> str:
    year, month, value = (int(part) for part in day.split("-"))
    return (date(year, month, value) - timedelta(days=1)).isoformat()
