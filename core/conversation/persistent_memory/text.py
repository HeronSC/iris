from __future__ import annotations

import re
from difflib import SequenceMatcher

def _slugify_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return normalized or "item"


def _split_sentences(text: str) -> list[str]:
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+|\n+", text or "") if item.strip()]
    return sentences if sentences else [str(text or "").strip()]


def _extract_price_value(text: str) -> str | None:
    patterns = [
        r"\$\d[\d,]*(?:\s*(?:-|to|and)\s*\$?\d[\d,]*)",
        r"\$\d[\d,]*",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match is not None:
            return re.sub(r"\s+", " ", match.group(0)).strip()
    return None


def _extract_action_target(text: str, *, action: str) -> str:
    lowered = (text or "").strip()
    if not lowered:
        return ""
    patterns = {
        "reject": [r"don't want\s+(.+)$", r"do not want\s+(.+)$", r"remove\s+(.+)$", r"reject\s+(.+)$", r"eliminate\s+(.+)$"],
        "restore": [r"put\s+(.+)\s+back", r"restore\s+(.+)$", r"bring\s+(.+)\s+back"],
    }
    for pattern in patterns.get(action, []):
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if match is not None:
            return match.group(1).strip(" .?!")
    return ""


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / 4))


def _semantic_similarity(text_a: str, text_b: str) -> float:
    tokens_a = _token_set(text_a)
    tokens_b = _token_set(text_b)
    overlap = _token_overlap_score(tokens_a, tokens_b)
    query_coverage = _query_coverage_score(tokens_a, tokens_b)
    sequence = SequenceMatcher(None, (text_a or "").lower(), (text_b or "").lower()).ratio()
    blended = (query_coverage * 0.65) + (overlap * 0.2) + (sequence * 0.15)
    return max(0.0, min(1.0, blended))


def _token_overlap_score(tokens_a: set[str], tokens_b: set[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = len(tokens_a.intersection(tokens_b))
    union = len(tokens_a.union(tokens_b))
    if union == 0:
        return 0.0
    return float(intersection) / float(union)


def _query_coverage_score(query_tokens: set[str], target_tokens: set[str]) -> float:
    if not query_tokens or not target_tokens:
        return 0.0
    intersection = len(query_tokens.intersection(target_tokens))
    return float(intersection) / float(len(query_tokens))


def _token_set(text: str) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "did",
        "do",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "much",
        "me",
        "of",
        "on",
        "or",
        "our",
        "this",
        "these",
        "that",
        "the",
        "their",
        "them",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "you",
        "your",
    }

    tokens: set[str] = set()
    for raw in re.findall(r"[a-z0-9]{2,}", (text or "").lower()):
        if raw in stopwords:
            continue
        token = _normalize_token(raw)
        if not token or token in stopwords:
            continue
        tokens.add(token)
    return tokens


def _normalize_token(token: str) -> str:
    value = (token or "").strip().lower()
    if not value:
        return ""
    typo_map = {
        "reccommended": "recommended",
        "recomend": "recommend",
        "recomended": "recommended",
    }
    mapped = typo_map.get(value)
    if mapped is not None:
        value = mapped
    if value.endswith("ies") and len(value) > 4:
        return value[:-3] + "y"
    if value.endswith("es") and len(value) > 4:
        return value[:-2]
    if value.endswith("s") and len(value) > 3:
        return value[:-1]
    return value


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def _extract_requirements(items: list[str]) -> list[str]:
    output: list[str] = []
    for text in items:
        for line in re.split(r"[\n\r]+", text):
            candidate = line.strip(" -\t")
            lowered = candidate.lower()
            if not candidate:
                continue
            if any(token in lowered for token in ("need", "require", "must", "should", "want", "looking for")):
                output.append(candidate)
    return _dedupe_keep_order(output)


def _extract_spec_facts(items: list[str]) -> list[str]:
    facts: list[str] = []
    patterns = [
        (r"\b32\s*gb\s*ram\b", "32 GB RAM"),
        (r"\b16\s*gb\s*ram\b", "16 GB RAM"),
        (r"\b64\s*gb\s*ram\b", "64 GB RAM"),
        (r"\b1\s*tb\s*ssd\b", "1 TB SSD"),
        (r"\b2\s*tb\s*ssd\b", "2 TB SSD"),
        (r"\btouch\s*screen|touchscreen\b", "Touchscreen"),
        (r"\bflip\s*screen\b|\b360[- ]degree\b|\bconvertible\b|\b2[- ]in[- ]1\b", "Convertible or flip-screen design"),
    ]
    for text in items:
        lowered = (text or "").lower()
        for pattern, label in patterns:
            if re.search(pattern, lowered):
                facts.append(label)
    return _dedupe_keep_order(facts)


def _extract_model_mentions(items: list[str]) -> list[str]:
    brands = "HP|Lenovo|Dell|Microsoft|Asus|Acer|MSI|Razer|Samsung|LG|Framework|Alienware"
    brand_values = {"hp", "lenovo", "dell", "microsoft", "asus", "acer", "msi", "razer", "samsung", "lg", "framework", "alienware"}
    families = "Spectre|ThinkPad|Yoga|XPS|Surface|EliteBook|Latitude|OmniBook|Zenbook|ProBook|Pavilion|ZBook"
    pattern = re.compile(
        rf"\b(?:{brands})\s+(?:[A-Z][a-zA-Z0-9-]*\s+){{0,4}}(?:{families}|[A-Z][a-zA-Z0-9-]+)(?:\s+[A-Za-z0-9][a-zA-Z0-9-]*){{0,4}}",
    )
    matches: list[str] = []
    for text in items:
        for match in pattern.findall(text or ""):
            cleaned = re.sub(r"\s+", " ", match).strip(" ,.;:-")
            cleaned = re.sub(r"\b(starts?\s+at|starting\s+at|around|about|from)\b.*$", "", cleaned, flags=re.IGNORECASE).strip(" ,.;:-")
            cleaned = re.sub(r"\b(is|are|can|could|may|might|with|for|to)$", "", cleaned, flags=re.IGNORECASE).strip(" ,.;:-")
            if len(cleaned) >= 4:
                if " and " in cleaned.lower():
                    parts = [part.strip(" ,.;:-") for part in re.split(r"\s+and\s+", cleaned, flags=re.IGNORECASE) if part.strip()]
                    for part in parts:
                        if len(part) >= 4 and part.lower() not in brand_values:
                            matches.append(part)
                    continue
                if cleaned.lower() in brand_values:
                    continue
                matches.append(cleaned)
    return _dedupe_keep_order(matches)


def _clean_excerpt(text: str, maximum_chars: int) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) <= maximum_chars:
        return compact
    return compact[: maximum_chars - 3].rstrip() + "..."


def _contains_price_signal(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    if "$" in lowered:
        return True
    if re.search(r"\b\d{3,5}\b", lowered) and any(token in lowered for token in ("price", "cost", "usd", "dollar", "starting at", "around", "approximately")):
        return True
    return False


def _looks_like_recommendation_or_price(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    if any(token in lowered for token in ("recommend", "suggest", "option", "model", "configuration", "spec", "ram", "ssd", "price", "cost")):
        return True
    if _contains_price_signal(text):
        return True
    return False


def _is_anaphoric_query(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    markers = (
        "those ",
        "that ",
        "these ",
        "earlier",
        "before",
        "previous",
        "recommended",
        "mentioned",
        "we discussed",
        "you suggested",
    )
    return any(marker in lowered for marker in markers)


def _looks_like_topic_mutation(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    markers = (
        "remove ",
        "reject ",
        "eliminate ",
        "don't want ",
        "do not want ",
        "restore ",
        "put ",
        "bring ",
        "show me the pricing",
        "what models",
        "which has",
    )
    return any(marker in lowered for marker in markers)


def _looks_like_topic_enrichment(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    markers = (
        "price",
        "pricing",
        "cost",
        "battery",
        "display",
        "compare",
        "configuration",
        "ram",
        "ssd",
        "spec",
        "details",
        "for those",
        "for the remaining",
    )
    return any(marker in lowered for marker in markers)


def _generate_topic_name(user_message: str) -> str:
    cleaned = re.sub(r"\s+", " ", (user_message or "").strip())
    if not cleaned:
        return "General"
    lowered = cleaned.lower()
    if "azure" in lowered and "function" in lowered:
        return "Azure Function deployment"
    if "laptop" in lowered or any(token in lowered for token in ("touchscreen", "flip screen", "convertible", "spectre", "thinkpad", "xps", "elitebook", "latitude")):
        if any(token in lowered for token in ("price", "pricing", "cost")):
            return "Laptop pricing"
        return "Convertible laptop research"
    lowered = re.sub(r"^(please|can you|could you|help me|now|and)\s+", "", lowered)
    stopwords = {
        "the",
        "a",
        "an",
        "to",
        "for",
        "with",
        "about",
        "how",
        "what",
        "why",
        "when",
        "where",
        "were",
        "was",
        "is",
        "are",
        "those",
        "these",
        "this",
        "that",
        "you",
        "your",
        "our",
        "much",
    }
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9]+", lowered):
        normalized = _normalize_token(token)
        if not normalized or normalized in stopwords:
            continue
        tokens.append(normalized)
    if not tokens:
        return "General"
    candidate = " ".join(tokens[:5]).strip()
    return candidate[:1].upper() + candidate[1:]
