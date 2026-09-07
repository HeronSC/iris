from __future__ import annotations

import ast
import csv
import json
import math
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib import error as urllib_error, request


class ProviderExecutionError(Exception):
    def __init__(self, message: str, *, unavailable: bool = True) -> None:
        super().__init__(message)
        self.unavailable = unavailable


@dataclass(frozen=True)
class GeneralKnowledgeResult:
    provider: str
    response: str | None = None
    fallback_notice: str | None = None
    detail_type: str = "text"
    detail_title: str | None = None
    detail_content: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    description: str
    request_schema: dict[str, Any]


@dataclass(frozen=True)
class WeatherRequest:
    location: str
    range_name: str
    start: str
    granularity: str
    period: str | None = None
    focus: str | None = None


@dataclass(frozen=True)
class NewsRequest:
    topic: str
    max_items: int = 5


@dataclass(frozen=True)
class StockQuoteRequest:
    ticker: str


@dataclass(frozen=True)
class TimeRequest:
    timezone: str


@dataclass(frozen=True)
class LookupRequest:
    query: str


class KnowledgeProvider:
    name = "provider"

    def can_handle(self, text: str) -> bool:
        raise NotImplementedError

    def can_handle_follow_up(self, text: str) -> bool:
        return False

    def execute(self, text: str) -> str:
        raise NotImplementedError

    def execute_detailed(self, text: str) -> GeneralKnowledgeResult | None:
        return None

    def execute_request(self, request_obj: Any) -> str:
        raise NotImplementedError

    def execute_detailed_request(self, request_obj: Any) -> GeneralKnowledgeResult | None:
        return None

    def definition(self) -> CapabilityDefinition | None:
        """Describe this capability to the orchestrator, or None to stay unadvertised."""
        return None

    def parse_request(self, payload: dict[str, Any]) -> Any | None:
        """Turn orchestrator-supplied arguments into this provider's request object."""
        return None


class GeneralKnowledgeRouter:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if isinstance(config, dict) else {}
        self.timeout_seconds = float(cfg.get("timeout_seconds", 8.0))
        self.user_agent = str(cfg.get("user_agent", "Iris/1.0"))
        enabled = cfg.get("enabled", {})
        if not isinstance(enabled, dict):
            enabled = {}

        self.providers: list[KnowledgeProvider] = []
        self._last_provider_name: str | None = None
        self._weather_default_location: str = ""
        self._register(WeatherProvider(self._fetch_json), enabled, "weather")
        self._register(NewsProvider(self._fetch_text), enabled, "news")
        self._register(StockProvider(self._fetch_text), enabled, "stocks")
        self._register(CalculatorProvider(), enabled, "calculator")
        self._register(ConversionProvider(), enabled, "conversion")
        self._register(TimeProvider(self._fetch_json), enabled, "time")
        self._register(LookupProvider(self._fetch_json), enabled, "lookup")

    def route(self, user_text: str) -> GeneralKnowledgeResult | None:
        text = (user_text or "").strip()
        if not text:
            return None
        if self._looks_like_local_request(text):
            return None
        for provider in self.providers:
            explicit_match = provider.can_handle(text)
            follow_up_match = self._last_provider_name == provider.name and provider.can_handle_follow_up(text)
            if not explicit_match and not follow_up_match:
                continue
            result = self._execute_provider(provider, text)
            if result is not None:
                return result
        return None

    def route_provider(self, provider_name: str, user_text: str) -> GeneralKnowledgeResult | None:
        text = (user_text or "").strip()
        if not text:
            return None
        for provider in self.providers:
            if provider.name != provider_name:
                continue
            return self._execute_provider(provider, text)
        return None

    def execute_capability_request(self, provider_name: str, request_obj: Any) -> GeneralKnowledgeResult | None:
        for provider in self.providers:
            if provider.name != provider_name:
                continue
            return self._execute_provider_request(provider, request_obj)
        return None

    def capability_definitions(self) -> list[CapabilityDefinition]:
        """What the orchestrator may call. Each provider describes itself."""
        definitions = [provider.definition() for provider in self.providers]
        return [item for item in definitions if item is not None]

    def build_capability_request(self, provider_name: str, payload: dict[str, Any]) -> Any | None:
        if not isinstance(payload, dict):
            return None
        for provider in self.providers:
            if provider.name == provider_name:
                return provider.parse_request(payload)
        return None

    def register(self, provider: KnowledgeProvider) -> None:
        """Add a capability the router did not construct itself.

        Providers built here are gated by the general_knowledge.enabled config;
        one passed in has already been decided on by its caller.
        """
        if any(existing.name == provider.name for existing in self.providers):
            raise ValueError(f"A provider named {provider.name} is already registered")
        self.providers.append(provider)

    def set_active_provider(self, provider_name: str | None) -> None:
        normalized = str(provider_name or "").strip() or None
        self._last_provider_name = normalized

    def _execute_provider(self, provider: KnowledgeProvider, text: str) -> GeneralKnowledgeResult | None:
        try:
            detailed = provider.execute_detailed(text)
            if detailed is not None:
                if not isinstance(detailed, GeneralKnowledgeResult):
                    raise ProviderExecutionError(
                        f"{provider.name} provider returned invalid detailed result type: {type(detailed).__name__}",
                        unavailable=False,
                    )
                self._last_provider_name = provider.name
                metadata = self._normalize_result_metadata(provider.name, detailed.metadata, status="success")
                return GeneralKnowledgeResult(
                    provider=provider.name,
                    response=detailed.response,
                    fallback_notice=detailed.fallback_notice,
                    detail_type=detailed.detail_type,
                    detail_title=detailed.detail_title,
                    detail_content=detailed.detail_content,
                    metadata=metadata,
                )
            response = provider.execute(text)
        except ProviderExecutionError as exc:
            if provider.name == "weather":
                self._last_provider_name = provider.name
                metadata = self._normalize_result_metadata(
                    provider.name,
                    {
                        "capability_status": "failed",
                        "render_operation": "replace_section",
                        "warnings": [str(exc)],
                    },
                    status="failed",
                )
                return GeneralKnowledgeResult(
                    provider=provider.name,
                    response=f"I could not retrieve the current weather because the weather service failed: {exc}",
                    detail_type="text",
                    detail_title="Weather unavailable",
                    detail_content=f"I could not retrieve the current weather because the weather service failed: {exc}",
                    metadata=metadata,
                )
            if exc.unavailable:
                notice = f"{provider.name.capitalize()} service unavailable. Falling back to general knowledge..."
            else:
                notice = f"{provider.name.capitalize()} quick lookup could not answer that. Falling back to general knowledge..."
            return GeneralKnowledgeResult(
                provider=provider.name,
                fallback_notice=notice,
            )
        if response.strip():
            self._last_provider_name = provider.name
            metadata = self._normalize_result_metadata(provider.name, {}, status="success")
            return GeneralKnowledgeResult(provider=provider.name, response=response.strip(), metadata=metadata)
        return None

    def _execute_provider_request(self, provider: KnowledgeProvider, request_obj: Any) -> GeneralKnowledgeResult | None:
        try:
            detailed = provider.execute_detailed_request(request_obj)
            if detailed is not None:
                if not isinstance(detailed, GeneralKnowledgeResult):
                    raise ProviderExecutionError(
                        f"{provider.name} provider returned invalid detailed result type: {type(detailed).__name__}",
                        unavailable=False,
                    )
                self._last_provider_name = provider.name
                metadata = self._normalize_result_metadata(provider.name, detailed.metadata, status="success")
                return GeneralKnowledgeResult(
                    provider=provider.name,
                    response=detailed.response,
                    fallback_notice=detailed.fallback_notice,
                    detail_type=detailed.detail_type,
                    detail_title=detailed.detail_title,
                    detail_content=detailed.detail_content,
                    metadata=metadata,
                )
            response = provider.execute_request(request_obj)
        except ProviderExecutionError as exc:
            if provider.name == "weather":
                self._last_provider_name = provider.name
                metadata = self._normalize_result_metadata(
                    provider.name,
                    {
                        "capability_status": "failed",
                        "render_operation": "replace_section",
                        "warnings": [str(exc)],
                    },
                    status="failed",
                )
                return GeneralKnowledgeResult(
                    provider=provider.name,
                    response=f"I could not retrieve the current weather because the weather service failed: {exc}",
                    detail_type="text",
                    detail_title="Weather unavailable",
                    detail_content=f"I could not retrieve the current weather because the weather service failed: {exc}",
                    metadata=metadata,
                )
            if exc.unavailable:
                notice = f"{provider.name.capitalize()} service unavailable. Falling back to general knowledge..."
            else:
                notice = f"{provider.name.capitalize()} quick lookup could not answer that. Falling back to general knowledge..."
            return GeneralKnowledgeResult(
                provider=provider.name,
                fallback_notice=notice,
                metadata=self._normalize_result_metadata(
                    provider.name,
                    {
                        "capability_status": "fallback",
                        "render_operation": "replace_section",
                        "warnings": [str(exc)],
                    },
                    status="fallback",
                ),
            )
        if response.strip():
            self._last_provider_name = provider.name
            metadata = self._normalize_result_metadata(provider.name, {}, status="success")
            return GeneralKnowledgeResult(provider=provider.name, response=response.strip(), metadata=metadata)
        return None

    def _normalize_result_metadata(self, provider_name: str, metadata: dict[str, Any] | None, *, status: str) -> dict[str, Any]:
        source = str(provider_name).strip() or "unknown"
        normalized = dict(metadata or {})

        facts = normalized.get("facts") if isinstance(normalized.get("facts"), dict) else {}
        warnings_raw = normalized.get("warnings")
        warnings: list[str] = []
        if isinstance(warnings_raw, list):
            for item in warnings_raw:
                value = str(item).strip()
                if value:
                    warnings.append(value)

        coverage_raw = normalized.get("coverage")
        if isinstance(coverage_raw, dict):
            coverage: dict[str, Any] = coverage_raw
        else:
            coverage_text = str(coverage_raw).strip() if coverage_raw is not None else ""
            coverage = {"summary": coverage_text} if coverage_text else {"summary": "unknown"}

        normalized.setdefault("capability", source)
        normalized.setdefault("status", status)
        normalized.setdefault("source", source)
        normalized.setdefault("retrieved_at", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        normalized.setdefault("valid_until", None)
        normalized["coverage"] = coverage
        normalized["warnings"] = warnings
        normalized["facts"] = facts
        return normalized

    def set_weather_default_location(self, location: str | None) -> None:
        normalized = str(location or "").strip()
        self._weather_default_location = normalized
        for provider in self.providers:
            if isinstance(provider, WeatherProvider):
                provider.set_default_location(normalized)

    def _looks_like_local_request(self, text: str) -> bool:
        lowered = text.lower()
        if lowered.startswith("/"):
            return True
        if any(token in lowered for token in ["summarize this file", "read this file", "open this path", "search local", "local documents"]):
            return True
        if re.search(r"\b(?:read|summarize|open|search|find|locate)\b.*\b(?:file|folder|path|document)\b", lowered):
            return True
        if re.search(r"[a-zA-Z]:[\\/].+", text):
            return True
        return False

    def _register(self, provider: KnowledgeProvider, enabled: dict[str, Any], key: str) -> None:
        flag = enabled.get(key, True)
        if bool(flag):
            self.providers.append(provider)

    def _fetch_text(self, url: str) -> str:
        req = request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, application/xml, text/xml",
            },
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib_error.HTTPError, urllib_error.URLError, TimeoutError, OSError) as exc:
            raise ProviderExecutionError(str(exc)) from exc

    def _fetch_json(self, url: str) -> dict[str, Any]:
        payload = self._fetch_text(url)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProviderExecutionError(str(exc)) from exc
        if not isinstance(parsed, dict):
            raise ProviderExecutionError("Unexpected non-object JSON payload")
        return parsed


class WeatherProvider(KnowledgeProvider):
    name = "weather"

    def __init__(self, fetch_json: Callable[[str], dict[str, Any]], default_location: str = "") -> None:
        self.fetch_json = fetch_json
        self._last_location = ""
        self._default_location = str(default_location).strip()

    def set_default_location(self, location: str | None) -> None:
        self._default_location = str(location or "").strip()

    def definition(self) -> CapabilityDefinition:
        return CapabilityDefinition(
            name=self.name,
            description="Use for live weather conditions and forecasts using a structured weather request.",
            request_schema={
                "location": "string",
                "range_name": "current|today|tomorrow|week|next_week|weekend",
                "start": "today|tomorrow|next_week",
                "granularity": "current|hourly|daily",
                "period": "morning|afternoon|evening|null",
                "focus": "rain|null",
            },
        )

    def parse_request(self, payload: dict[str, Any]) -> WeatherRequest | None:
        location = str(payload.get("location", "")).strip() or self._default_location
        range_name = str(payload.get("range_name", "current")).strip().lower() or "current"
        if range_name not in {"current", "today", "tomorrow", "week", "next_week", "weekend"}:
            return None
        start = str(payload.get("start", "today")).strip().lower() or "today"
        granularity = str(payload.get("granularity", "current")).strip().lower() or "current"
        period_value = payload.get("period")
        period = str(period_value).strip().lower() if period_value is not None and str(period_value).strip() else None
        if period not in {None, "morning", "afternoon", "evening"}:
            return None
        focus_value = payload.get("focus")
        focus = str(focus_value).strip().lower() if focus_value is not None and str(focus_value).strip() else None
        if focus not in {None, "rain"}:
            return None
        return WeatherRequest(
            location=location,
            range_name=range_name,
            start=start,
            granularity=granularity,
            period=period,
            focus=focus,
        )

    def can_handle(self, text: str) -> bool:
        lowered = text.lower()
        explicit_patterns = [
            r"\bweather\b",
            r"\bforecast\b",
            r"\bwhat(?:'s| is) it like outside(?: today| tonight| this morning| this afternoon)?\b",
            r"\btemperature outside\b",
            r"\btemp outside\b",
            r"\bwill it rain(?: today| tonight| tomorrow)?\b",
            r"\bhow hot is it(?: outside| today)?\b",
            r"\bdo i need (?:a )?jacket\b",
            r"\bdo i need (?:an )?umbrella\b",
            r"\bwhat will it be like tomorrow\b",
        ]
        return any(re.search(pattern, lowered) for pattern in explicit_patterns)

    def can_handle_follow_up(self, text: str) -> bool:
        if not self._last_location:
            return False
        lowered = text.lower()
        follow_up_markers = [
            "how about",
            "afternoon",
            "morning",
            "tonight",
            "evening",
            "later today",
            "tomorrow",
            "what about",
            "hourly",
            "weekend",
            "rest of the day",
            "rest of today",
            "rest of the week",
            "rest of week",
            "outlook",
            "rain chances",
        ]
        return any(marker in lowered for marker in follow_up_markers)

    def execute(self, text: str) -> str:
        result = self.execute_detailed(text)
        if result is None:
            raise ProviderExecutionError("Weather result was unavailable", unavailable=False)
        return str(result.response or "").strip()

    def execute_detailed(self, text: str) -> GeneralKnowledgeResult | None:
        weather_request = self._legacy_parse_weather_request(text)
        return self.execute_detailed_request(weather_request)

    def execute_request(self, request_obj: Any) -> str:
        result = self.execute_detailed_request(request_obj)
        if result is None:
            raise ProviderExecutionError("Weather result was unavailable", unavailable=False)
        return str(result.response or "").strip()

    def execute_detailed_request(self, request_obj: Any) -> GeneralKnowledgeResult | None:
        if not isinstance(request_obj, WeatherRequest):
            raise ProviderExecutionError("weather provider requires a WeatherRequest", unavailable=False)
        weather_request = request_obj
        location = weather_request.location
        encoded = urllib.parse.quote(location) if location else ""
        url = f"https://wttr.in/{encoded}?format=j1"
        data = self.fetch_json(url)
        current = data.get("current_condition")
        if not isinstance(current, list) or not current:
            raise ProviderExecutionError("Missing weather data")
        current_now = current[0] if isinstance(current[0], dict) else {}
        if location:
            self._last_location = location
        temp_c = str(current_now.get("temp_C", "")).strip()
        feels_like_c = str(current_now.get("FeelsLikeC", "")).strip()
        desc_list = current_now.get("weatherDesc", [])
        description = ""
        if isinstance(desc_list, list) and desc_list and isinstance(desc_list[0], dict):
            description = str(desc_list[0].get("value", "")).strip()
        humidity = str(current_now.get("humidity", "")).strip()
        wind_mph = str(current_now.get("windspeedMiles", "")).strip()

        temp_f_text = "unknown"
        temp_c_value = self._safe_float(temp_c)
        if temp_c_value is not None:
            temp_f_text = f"{self._c_to_f(temp_c_value):.1f}F"
        feels_like_text = ""
        feels_like_value = self._safe_float(feels_like_c)
        if feels_like_value is not None:
            feels_like_text = f"{self._c_to_f(feels_like_value):.1f}F"

        forecast_today = self._select_forecast_day(data, day_offset=0)
        forecast_tomorrow = self._select_forecast_day(data, day_offset=1)
        today_minmax = self._daily_min_max_text(forecast_today)
        rain_summary = self._format_rain_forecast(forecast_today, label="today")

        if location:
            self._last_location = location

        if weather_request.range_name == "current":
            if weather_request.focus == "rain":
                rain_text = self._format_rain_forecast(forecast_today, label="today")
                if rain_text:
                    location_text = f" in {location}" if location else ""
                    response = f"Rain outlook{location_text}: {rain_text}"
                    return self._weather_result(
                        weather_request,
                        response=response,
                        headline=rain_text,
                        description=description,
                        temp_f_text=temp_f_text,
                        feels_like_text=feels_like_text,
                        today_minmax=today_minmax,
                        rain_summary=rain_summary,
                        wind_mph=wind_mph,
                        humidity=humidity,
                        extra_lines=[],
                    )
            location_text = f" in {location}" if location else ""
            response = f"Current weather{location_text}: {description or 'conditions unavailable'}, {temp_f_text}."
            detail_intro = f"{description or 'Conditions unavailable'} with a current temperature of {temp_f_text}."
            return self._weather_result(
                weather_request,
                response=response,
                headline=detail_intro,
                description=description,
                temp_f_text=temp_f_text,
                feels_like_text=feels_like_text,
                today_minmax=today_minmax,
                rain_summary=rain_summary,
                wind_mph=wind_mph,
                humidity=humidity,
                extra_lines=[],
            )

        if weather_request.range_name == "today":
            if weather_request.period is not None:
                period_text = self._format_named_period_forecast(forecast_today, weather_request.period)
                if period_text:
                    response = f"Forecast in {location} {period_text}" if location else f"Forecast {period_text}"
                    return self._weather_result(
                        weather_request,
                        response=response,
                        headline=period_text,
                        description=description,
                        temp_f_text=temp_f_text,
                        feels_like_text=feels_like_text,
                        today_minmax=today_minmax,
                        rain_summary=rain_summary,
                        wind_mph=wind_mph,
                        humidity=humidity,
                        extra_lines=[],
                    )
            today_summary = self._format_daily_forecast(forecast_today, label="Today")
            response = f"The outlook for the rest of today in {location}: {today_summary}" if location else f"The outlook for the rest of today: {today_summary}"
            return self._weather_result(
                weather_request,
                response=response,
                headline=today_summary,
                description=description,
                temp_f_text=temp_f_text,
                feels_like_text=feels_like_text,
                today_minmax=today_minmax,
                rain_summary=rain_summary,
                wind_mph=wind_mph,
                humidity=humidity,
                extra_lines=[f"- Outlook: {today_summary}"],
            )

        if weather_request.range_name == "tomorrow":
            tomorrow_summary = self._format_daily_forecast(forecast_tomorrow, label="Tomorrow")
            response = f"Tomorrow in {location}: {tomorrow_summary}" if location else f"Tomorrow: {tomorrow_summary}"
            return self._weather_result(
                weather_request,
                response=response,
                headline=tomorrow_summary,
                description=description,
                temp_f_text=temp_f_text,
                feels_like_text=feels_like_text,
                today_minmax=today_minmax,
                rain_summary=rain_summary,
                wind_mph=wind_mph,
                humidity=humidity,
                extra_lines=[f"- Tomorrow: {tomorrow_summary}"],
            )

        if weather_request.range_name in {"week", "weekend"}:
            daily_lines, coverage_note = self._format_multi_day_forecast(data, weather_request.range_name)
            if not daily_lines:
                raise ProviderExecutionError("forecast data is not available for the requested range", unavailable=False)
            range_label = "rest of the week" if weather_request.range_name == "week" else "this weekend"
            response = self._compose_multi_day_response(location, daily_lines, coverage_note)
            extra_lines = ["- Daily outlook:"] + [f"  - {line}" for line in daily_lines]
            if coverage_note:
                extra_lines.append("")
                extra_lines.append(f"Coverage note: {coverage_note}")
            return self._weather_result(
                weather_request,
                response=response,
                headline=f"Outlook for {range_label}.",
                description=description,
                temp_f_text=temp_f_text,
                feels_like_text=feels_like_text,
                today_minmax=today_minmax,
                rain_summary=rain_summary,
                wind_mph=wind_mph,
                humidity=humidity,
                extra_lines=extra_lines,
            )

        if weather_request.range_name == "next_week":
            daily_lines, coverage_note = self._format_multi_day_forecast(data, "week")
            available_window = " ".join(daily_lines[:3]).strip()
            response = (
                f"Next week's forecast for {location} is not available through this source. "
                if location
                else "Next week's forecast is not available through this source. "
            )
            if coverage_note:
                response = response + coverage_note
            extra_lines = []
            if daily_lines:
                extra_lines.append("- Currently available forecast window:")
                extra_lines.extend([f"  - {line}" for line in daily_lines])
            if coverage_note:
                extra_lines.append("")
                extra_lines.append(f"Coverage note: {coverage_note}")
            return self._weather_result(
                weather_request,
                response=response.strip(),
                headline=response.strip(),
                description=description,
                temp_f_text=temp_f_text,
                feels_like_text=feels_like_text,
                today_minmax=today_minmax,
                rain_summary=rain_summary,
                wind_mph=wind_mph,
                humidity=humidity,
                extra_lines=extra_lines,
            )

        raise ProviderExecutionError(f"unsupported weather range: {weather_request.range_name}", unavailable=False)

    def _legacy_parse_weather_request(self, text: str) -> WeatherRequest:
        lowered = text.lower().strip()
        location = self._extract_location(text)
        if not location and self._last_location and self._is_follow_up_weather_question(text):
            location = self._last_location
        if not location and self._default_location:
            location = self._default_location

        if "next week" in lowered:
            return WeatherRequest(location=location, range_name="next_week", start="next_week", granularity="daily")
        if "this week" in lowered:
            return WeatherRequest(location=location, range_name="week", start="today", granularity="daily")
        if "rest of the week" in lowered or "rest of week" in lowered:
            return WeatherRequest(location=location, range_name="week", start="today", granularity="daily")
        if "weekend" in lowered:
            return WeatherRequest(location=location, range_name="weekend", start="today", granularity="daily")
        if "tomorrow" in lowered:
            focus = "rain" if "rain" in lowered else None
            return WeatherRequest(location=location, range_name="tomorrow", start="tomorrow", granularity="daily", focus=focus)
        if "rest of the day" in lowered or "rest of today" in lowered or "later today" in lowered:
            focus = "rain" if "rain" in lowered else None
            return WeatherRequest(location=location, range_name="today", start="today", granularity="hourly", focus=focus)
        if "afternoon" in lowered:
            return WeatherRequest(location=location, range_name="today", start="today", granularity="hourly", period="afternoon")
        if "morning" in lowered:
            return WeatherRequest(location=location, range_name="today", start="today", granularity="hourly", period="morning")
        if "tonight" in lowered or "evening" in lowered:
            return WeatherRequest(location=location, range_name="today", start="today", granularity="hourly", period="evening")
        if "hourly" in lowered:
            return WeatherRequest(location=location, range_name="today", start="today", granularity="hourly")
        focus = "rain" if "rain" in lowered or "supposed to rain" in lowered else None
        return WeatherRequest(location=location, range_name="current", start="today", granularity="current", focus=focus)

    def _extract_location(self, text: str) -> str:
        match = re.search(r"\b(?:in|for|at)\s+([A-Za-z][A-Za-z\s,-]{1,40})$", text.strip(), re.IGNORECASE)
        if match is None:
            if re.search(r"\bhere\b", text, re.IGNORECASE):
                return self._last_location or self._default_location
            return ""
        return match.group(1).strip(" .")

    def _is_follow_up_weather_question(self, text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in ["what about", "how about", "afternoon", "morning", "tonight", "evening", "tomorrow", "later", "rest of the day", "rest of today", "rest of the week", "this week", "next week", "weekend", "outlook"]) 

    def _select_forecast_day(self, payload: dict[str, Any], day_offset: int) -> dict[str, Any] | None:
        weather = payload.get("weather")
        if not isinstance(weather, list):
            return None
        if len(weather) <= day_offset:
            return None
        day = weather[day_offset]
        if not isinstance(day, dict):
            return None
        return day

    def _format_period_forecast(self, day: dict[str, Any] | None, start_hour: int, end_hour: int, label: str) -> str:
        if day is None:
            return ""
        hourly = day.get("hourly")
        if not isinstance(hourly, list):
            return ""
        selected: list[dict[str, Any]] = []
        for item in hourly:
            if not isinstance(item, dict):
                continue
            raw_time = str(item.get("time", "")).strip()
            try:
                hour = int(raw_time) // 100
            except ValueError:
                continue
            if start_hour <= hour < end_hour:
                selected.append(item)
        if not selected:
            return ""

        avg_temp_c = self._average([self._safe_float(item.get("tempC")) for item in selected])
        max_rain = self._maximum([self._safe_float(item.get("chanceofrain")) for item in selected])
        summary_desc = self._first_description(selected)
        temp_text = "temperature unavailable"
        if avg_temp_c is not None:
            temp_text = f"around {self._c_to_f(avg_temp_c):.1f}F"
        rain_text = "rain chance unavailable"
        if max_rain is not None:
            rain_text = f"up to {int(round(max_rain))}% chance of rain"
        return f"{label}: {summary_desc}, {temp_text}, {rain_text}."

    def _format_rain_forecast(self, day: dict[str, Any] | None, label: str) -> str:
        if day is None:
            return ""
        hourly = day.get("hourly")
        if not isinstance(hourly, list):
            return ""
        rain_values = [self._safe_float(item.get("chanceofrain")) for item in hourly if isinstance(item, dict)]
        max_rain = self._maximum(rain_values)
        if max_rain is None:
            return ""
        if max_rain >= 60:
            level = "likely"
        elif max_rain >= 30:
            level = "possible"
        else:
            level = "unlikely"
        return f"Rain is {level} {label} (peak chance around {int(round(max_rain))}%)."

    def _format_daily_forecast(self, day: dict[str, Any] | None, *, label: str) -> str:
        if day is None:
            return f"{label}: forecast unavailable."
        minmax = self._daily_min_max_text(day)
        summary = self._first_description(day.get("hourly", []) if isinstance(day.get("hourly"), list) else [])
        rain = self._format_rain_forecast(day, label=label.lower())
        parts = [summary]
        if minmax:
            parts.append(minmax)
        if rain:
            parts.append(rain)
        return f"{label}: " + "; ".join(parts) + "."

    def _format_named_period_forecast(self, day: dict[str, Any] | None, period: str) -> str:
        mapping = {
            "morning": (6, 12, "this morning"),
            "afternoon": (12, 18, "this afternoon"),
            "evening": (18, 24, "this evening"),
        }
        start_hour, end_hour, label = mapping.get(period, (12, 18, "this afternoon"))
        return self._format_period_forecast(day, start_hour=start_hour, end_hour=end_hour, label=label)

    def _format_multi_day_forecast(self, payload: dict[str, Any], range_name: str) -> tuple[list[str], str | None]:
        weather = payload.get("weather")
        if not isinstance(weather, list) or not weather:
            return [], None

        if range_name == "weekend":
            selected = [item for item in weather if isinstance(item, dict) and self._is_weekend_day(str(item.get("date", "")).strip())]
            if not selected:
                return [], "The current provider response does not include weekend dates in its forecast window."
            lines = [self._format_daily_forecast(day, label=self._day_label(day, index)) for index, day in enumerate(selected)]
            return lines, None

        selected_days = [item for item in weather if isinstance(item, dict)]
        lines = [self._format_daily_forecast(day, label=self._day_label(day, index)) for index, day in enumerate(selected_days)]
        coverage_note = None
        if len(selected_days) < 5:
            coverage_note = f"Provider coverage is limited to the next {len(selected_days)} day(s)."
        return lines, coverage_note

    def _day_label(self, day: dict[str, Any], index: int) -> str:
        raw_date = str(day.get("date", "")).strip()
        if raw_date:
            try:
                stamp = datetime.strptime(raw_date, "%Y-%m-%d")
                return stamp.strftime("%A")
            except ValueError:
                pass
        if index == 0:
            return "Today"
        if index == 1:
            return "Tomorrow"
        return f"Day {index + 1}"

    def _is_weekend_day(self, raw_date: str) -> bool:
        if not raw_date:
            return False
        try:
            stamp = datetime.strptime(raw_date, "%Y-%m-%d")
        except ValueError:
            return False
        return stamp.weekday() >= 5

    def _compose_multi_day_response(self, location: str, daily_lines: list[str], coverage_note: str | None) -> str:
        fragments: list[str] = []
        for line in daily_lines:
            cleaned = str(line).strip()
            if not cleaned:
                continue
            fragments.append(cleaned)
        response = " ".join(fragments)
        if location:
            response = f"For {location}, {response[:1].lower() + response[1:] if response else ''}".strip()
        if coverage_note:
            response = f"{response} {coverage_note}".strip()
        return response

    def _first_description(self, hourly_items: list[dict[str, Any]]) -> str:
        for item in hourly_items:
            desc_list = item.get("weatherDesc")
            if isinstance(desc_list, list) and desc_list and isinstance(desc_list[0], dict):
                text = str(desc_list[0].get("value", "")).strip()
                if text:
                    return text
        return "conditions"

    def _average(self, values: list[float | None]) -> float | None:
        usable = [value for value in values if value is not None]
        if not usable:
            return None
        return sum(usable) / len(usable)

    def _maximum(self, values: list[float | None]) -> float | None:
        usable = [value for value in values if value is not None]
        if not usable:
            return None
        return max(usable)

    def _safe_float(self, value: Any) -> float | None:
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _c_to_f(self, value_c: float) -> float:
        return (value_c * 9.0 / 5.0) + 32.0

    def _daily_min_max_text(self, day: dict[str, Any] | None) -> str:
        if day is None:
            return ""
        min_c = self._safe_float(day.get("mintempC"))
        max_c = self._safe_float(day.get("maxtempC"))
        if min_c is None or max_c is None:
            return ""
        return f"{self._c_to_f(min_c):.1f}F to {self._c_to_f(max_c):.1f}F"

    def _weather_result(
        self,
        weather_request: WeatherRequest,
        response: str,
        headline: str,
        description: str,
        temp_f_text: str,
        feels_like_text: str,
        today_minmax: str,
        rain_summary: str,
        wind_mph: str,
        humidity: str,
        extra_lines: list[str],
    ) -> GeneralKnowledgeResult:
        retrieved_at = datetime.now(timezone.utc).replace(microsecond=0)
        retrieved_at_iso = retrieved_at.isoformat()
        title_location = weather_request.location.title() if weather_request.location else "Current location"
        title = self._weather_title(weather_request, title_location)
        lines: list[str] = []
        lines.append(f"## Weather in {title_location}")
        if description:
            lines.append(f"- Conditions: {description}")
        lines.append(f"- Current temperature: {temp_f_text}")
        if feels_like_text:
            lines.append(f"- Feels like: {feels_like_text}")
        if today_minmax:
            lines.append(f"- Today's range: {today_minmax}")
        if rain_summary:
            lines.append(f"- Rain outlook: {rain_summary}")
        if wind_mph:
            lines.append(f"- Wind: {wind_mph} mph")
        if humidity:
            lines.append(f"- Humidity: {humidity}%")
        lines.append("")
        lines.append(f"Summary: {headline}")
        if extra_lines:
            lines.append("")
            lines.extend(extra_lines)
        lines.append("")
        lines.append(f"Retrieved: {retrieved_at.strftime('%Y-%m-%d %I:%M %p UTC')}")
        lines.append("Source: wttr.in")

        facts: dict[str, Any] = {
            "location": weather_request.location or "default",
            "range": weather_request.range_name,
            "granularity": weather_request.granularity,
            "period": weather_request.period,
            "focus": weather_request.focus,
            "condition": description or None,
            "current_temp_f": temp_f_text,
            "feels_like_f": feels_like_text or None,
            "today_range_f": today_minmax or None,
            "rain_summary": rain_summary or None,
            "wind_mph": wind_mph or None,
            "humidity_percent": humidity or None,
            "headline": headline,
            "details": [str(item).strip() for item in extra_lines if str(item).strip()],
        }
        warnings: list[str] = []
        if weather_request.range_name == "next_week":
            warnings.append("Requested next_week is outside provider forecast window.")
        coverage_summary = "full"
        if weather_request.range_name in {"week", "weekend", "next_week"}:
            coverage_summary = "provider_limited_window"

        return GeneralKnowledgeResult(
            provider=self.name,
            response=response,
            detail_type="markdown",
            detail_title=title,
            detail_content="\n".join(lines).strip(),
            metadata={
                "capability_status": "success",
                "render_operation": "replace_section",
                "capability": "weather",
                "weather_range": weather_request.range_name,
                "weather_granularity": weather_request.granularity,
                "facts": facts,
                "source": "wttr.in",
                "retrieved_at": retrieved_at_iso,
                "valid_until": None,
                "coverage": {"summary": coverage_summary},
                "warnings": warnings,
            },
        )

    def _weather_title(self, weather_request: WeatherRequest, title_location: str) -> str:
        mapping = {
            "current": "Current Weather",
            "today": "Today's Weather",
            "tomorrow": "Tomorrow's Forecast",
            "next_week": "Next Week Forecast",
            "week": "Weekly Outlook",
            "weekend": "Weekend Forecast",
        }
        prefix = mapping.get(weather_request.range_name, "Weather")
        return f"{prefix} - {title_location}"


class NewsProvider(KnowledgeProvider):
    name = "news"

    def __init__(self, fetch_text: Callable[[str], str]) -> None:
        self.fetch_text = fetch_text

    def definition(self) -> CapabilityDefinition:
        return CapabilityDefinition(
            name=self.name,
            description="Use for current news and headline requests.",
            request_schema={"topic": "string", "max_items": "integer"},
        )

    def parse_request(self, payload: dict[str, Any]) -> NewsRequest:
        topic = str(payload.get("topic", "")).strip() or "general"
        try:
            max_items = max(1, min(10, int(payload.get("max_items", 5))))
        except (TypeError, ValueError):
            max_items = 5
        return NewsRequest(topic=topic, max_items=max_items)

    def can_handle(self, text: str) -> bool:
        lowered = text.lower()
        return "news" in lowered or "headline" in lowered or "headlines" in lowered

    def execute(self, text: str) -> str:
        request_obj = NewsRequest(topic=str(text or "").strip() or "general")
        return self.execute_request(request_obj)

    def execute_detailed(self, text: str) -> GeneralKnowledgeResult | None:
        request_obj = NewsRequest(topic=str(text or "").strip() or "general")
        return self.execute_detailed_request(request_obj)

    def execute_request(self, request_obj: Any) -> str:
        detailed = self.execute_detailed_request(request_obj)
        if detailed is None:
            raise ProviderExecutionError("No news headlines were returned", unavailable=False)
        return str(detailed.response or "").strip()

    def execute_detailed_request(self, request_obj: Any) -> GeneralKnowledgeResult | None:
        if not isinstance(request_obj, NewsRequest):
            raise ProviderExecutionError("news provider requires a NewsRequest", unavailable=False)
        xml_payload = self.fetch_text("https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en")
        try:
            root = ET.fromstring(xml_payload)
        except ET.ParseError as exc:
            raise ProviderExecutionError(str(exc)) from exc

        titles: list[str] = []
        for item in root.findall("./channel/item/title"):
            if item.text:
                cleaned = item.text.strip()
                if cleaned:
                    titles.append(cleaned)
            if len(titles) >= 5:
                break

        if not titles:
            raise ProviderExecutionError("No news headlines were returned")

        lines = ["Top headlines:"]
        selected = titles[: request_obj.max_items]
        for idx, title in enumerate(selected, start=1):
            lines.append(f"{idx}. {title}")
        response = "\n".join(lines)
        detail_lines = ["## Top headlines"] + [f"- {item}" for item in selected]
        return GeneralKnowledgeResult(
            provider=self.name,
            response=response,
            detail_type="markdown",
            detail_title="News Headlines",
            detail_content="\n".join(detail_lines),
            metadata={
                "capability_status": "success",
                "render_operation": "replace_section",
                "capability": "news",
                "facts": {"topic": request_obj.topic, "headlines": selected, "max_items": request_obj.max_items},
                "source": "news.google.com/rss",
                "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "valid_until": None,
                "coverage": {"summary": "top_headlines"},
                "warnings": [],
            },
        )


class StockProvider(KnowledgeProvider):
    name = "stocks"

    def __init__(self, fetch_text: Callable[[str], str]) -> None:
        self.fetch_text = fetch_text
        self.company_map = {
            "apple": "AAPL",
            "nvidia": "NVDA",
            "tesla": "TSLA",
            "microsoft": "MSFT",
            "google": "GOOGL",
            "alphabet": "GOOGL",
            "amazon": "AMZN",
            "meta": "META",
        }

    def definition(self) -> CapabilityDefinition:
        return CapabilityDefinition(
            name=self.name,
            description="Use for stock quotes and price lookups.",
            request_schema={"ticker": "string"},
        )

    def parse_request(self, payload: dict[str, Any]) -> StockQuoteRequest | None:
        ticker = str(payload.get("ticker", "")).strip().upper()
        return StockQuoteRequest(ticker=ticker) if ticker else None

    def can_handle(self, text: str) -> bool:
        lowered = text.lower().strip()
        if "stock" in lowered or "trading" in lowered:
            return True
        if re.fullmatch(r"[A-Za-z]{1,5}", lowered):
            return True
        return any(name in lowered for name in self.company_map.keys())

    def execute(self, text: str) -> str:
        ticker = self._extract_ticker(text)
        return self.execute_request(StockQuoteRequest(ticker=ticker))

    def execute_detailed(self, text: str) -> GeneralKnowledgeResult | None:
        ticker = self._extract_ticker(text)
        return self.execute_detailed_request(StockQuoteRequest(ticker=ticker))

    def execute_request(self, request_obj: Any) -> str:
        detailed = self.execute_detailed_request(request_obj)
        if detailed is None:
            raise ProviderExecutionError("No stock payload", unavailable=False)
        return str(detailed.response or "").strip()

    def execute_detailed_request(self, request_obj: Any) -> GeneralKnowledgeResult | None:
        if not isinstance(request_obj, StockQuoteRequest):
            raise ProviderExecutionError("stock provider requires a StockQuoteRequest", unavailable=False)
        ticker = request_obj.ticker
        if not ticker:
            raise ProviderExecutionError("Could not determine ticker symbol")
        csv_text = self.fetch_text(f"https://stooq.com/q/l/?s={ticker.lower()}.us&i=d")
        rows = list(csv.DictReader(csv_text.splitlines()))
        if not rows:
            raise ProviderExecutionError("No stock payload")
        row = rows[0]
        close_value = self._safe_float(row.get("Close"))
        open_value = self._safe_float(row.get("Open"))
        if close_value is None:
            raise ProviderExecutionError("No price available")

        output = f"{ticker}: ${close_value:.2f}"
        change_value: float | None = None
        percent_value: float | None = None
        if open_value is not None and open_value != 0:
            change = close_value - open_value
            percent = (change / open_value) * 100
            change_value = change
            percent_value = percent
            output = output + f" ({change:+.2f}, {percent:+.2f}%)"
        detail_lines = [
            f"## Stock quote: {ticker}",
            f"- Last price: ${close_value:.2f}",
        ]
        if open_value is not None:
            detail_lines.append(f"- Open: ${open_value:.2f}")
        if change_value is not None and percent_value is not None:
            detail_lines.append(f"- Change: {change_value:+.2f} ({percent_value:+.2f}%)")
        return GeneralKnowledgeResult(
            provider=self.name,
            response=output,
            detail_type="markdown",
            detail_title=f"Stock Quote - {ticker}",
            detail_content="\n".join(detail_lines),
            metadata={
                "capability_status": "success",
                "render_operation": "replace_section",
                "capability": "stocks",
                "facts": {
                    "ticker": ticker,
                    "price": close_value,
                    "open": open_value,
                    "change": change_value,
                    "percent_change": percent_value,
                },
                "source": "stooq.com",
                "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "valid_until": None,
                "coverage": {"summary": "single_quote"},
                "warnings": [],
            },
        )

    def _extract_ticker(self, text: str) -> str:
        lowered = text.lower()
        for company, ticker in self.company_map.items():
            if company in lowered:
                return ticker
        bare_match = re.search(r"\b([A-Za-z]{1,5})\b", text)
        if bare_match is None:
            return ""
        return bare_match.group(1).upper()

    def _safe_float(self, value: Any) -> float | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.upper() == "N/D":
            return None
        try:
            return float(text)
        except ValueError:
            return None


class CalculatorProvider(KnowledgeProvider):
    name = "calculator"

    def can_handle(self, text: str) -> bool:
        lowered = text.strip().lower()
        if re.search(r"[a-zA-Z]:[\\/].+", text):
            return False
        if re.search(r"\b[a-z0-9_.-]+\.(md|txt|pdf|xlsx|xls|json|py|ps1|cs|jsonl|yml|yaml|xml|toml)\b", lowered):
            return False
        if lowered.startswith("calc ") or lowered.startswith("calculate "):
            return True
        if "sqrt" in lowered:
            return True
        if re.search(r"\d", lowered) and re.search(r"[+\-*/^()]", lowered):
            return True
        return False

    def execute(self, text: str) -> str:
        expression = text.strip().lower()
        expression = re.sub(r"^(calc|calculate)\s+", "", expression)
        expression = expression.replace("^", "**")
        result = self._safe_eval(expression)
        return f"{text.strip()} = {result}"

    def _safe_eval(self, expression: str) -> float:
        allowed_binary_ops = {
            ast.Add: lambda a, b: a + b,
            ast.Sub: lambda a, b: a - b,
            ast.Mult: lambda a, b: a * b,
            ast.Div: lambda a, b: a / b,
            ast.Pow: lambda a, b: a**b,
        }
        allowed_unary_ops = {
            ast.UAdd: lambda x: x,
            ast.USub: lambda x: -x,
        }

        def eval_node(node: ast.AST) -> float:
            if isinstance(node, ast.Expression):
                return eval_node(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return float(node.value)
            if isinstance(node, ast.BinOp) and type(node.op) in allowed_binary_ops:
                left = eval_node(node.left)
                right = eval_node(node.right)
                return float(allowed_binary_ops[type(node.op)](left, right))
            if isinstance(node, ast.UnaryOp) and type(node.op) in allowed_unary_ops:
                operand = eval_node(node.operand)
                return float(allowed_unary_ops[type(node.op)](operand))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sqrt" and len(node.args) == 1:
                return float(math.sqrt(eval_node(node.args[0])))
            raise ProviderExecutionError("Unsupported expression")

        try:
            parsed = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ProviderExecutionError(str(exc)) from exc
        return eval_node(parsed)


class ConversionProvider(KnowledgeProvider):
    name = "conversion"

    def can_handle(self, text: str) -> bool:
        lowered = text.lower()
        return bool(re.search(r"\b\d+(?:\.\d+)?\s*[a-zA-Z]+\s+(?:in|to)\s+[a-zA-Z]+\b", lowered))

    def execute(self, text: str) -> str:
        match = re.search(r"\b(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s+(?:in|to)\s+([a-zA-Z]+)\b", text.lower())
        if match is None:
            raise ProviderExecutionError("Could not parse conversion")

        value = float(match.group(1))
        source = match.group(2)
        target = match.group(3)

        temp = self._convert_temperature(value, source, target)
        if temp is not None:
            return f"{value:g} {source} = {temp:.2f} {target}"

        converted = self._convert_units(value, source, target)
        if converted is None:
            raise ProviderExecutionError("Unsupported conversion units")
        return f"{value:g} {source} = {converted:.4f} {target}"

    def _convert_temperature(self, value: float, source: str, target: str) -> float | None:
        src = self._normalize_unit(source)
        dst = self._normalize_unit(target)
        if src not in {"c", "f"} or dst not in {"c", "f"}:
            return None
        if src == dst:
            return value
        if src == "c" and dst == "f":
            return (value * 9 / 5) + 32
        if src == "f" and dst == "c":
            return (value - 32) * 5 / 9
        return None

    def _convert_units(self, value: float, source: str, target: str) -> float | None:
        length = {
            "m": 1.0,
            "meter": 1.0,
            "meters": 1.0,
            "km": 1000.0,
            "kilometer": 1000.0,
            "kilometers": 1000.0,
            "mile": 1609.344,
            "miles": 1609.344,
            "mi": 1609.344,
        }
        weight = {
            "kg": 1.0,
            "kilogram": 1.0,
            "kilograms": 1.0,
            "lb": 0.45359237,
            "lbs": 0.45359237,
            "pound": 0.45359237,
            "pounds": 0.45359237,
        }
        volume = {
            "l": 1.0,
            "liter": 1.0,
            "liters": 1.0,
            "gallon": 3.785411784,
            "gallons": 3.785411784,
            "gal": 3.785411784,
        }
        groups = [length, weight, volume]

        src = self._normalize_unit(source)
        dst = self._normalize_unit(target)
        for mapping in groups:
            if src in mapping and dst in mapping:
                base_value = value * mapping[src]
                return base_value / mapping[dst]
        return None

    def _normalize_unit(self, value: str) -> str:
        normalized = value.strip().lower()
        aliases = {
            "celsius": "c",
            "celcius": "c",
            "fahrenheit": "f",
            "farenheit": "f",
            "farenheight": "f",
        }
        return aliases.get(normalized, normalized)


class TimeProvider(KnowledgeProvider):
    name = "time"

    def __init__(self, fetch_json: Callable[[str], dict[str, Any]]) -> None:
        self.fetch_json = fetch_json
        self.timezone_aliases = {
            "tokyo": "Asia/Tokyo",
            "london": "Europe/London",
            "california": "America/Los_Angeles",
            "new york": "America/New_York",
            "paris": "Europe/Paris",
            "berlin": "Europe/Berlin",
            "sydney": "Australia/Sydney",
            "utc": "Etc/UTC",
        }

    def definition(self) -> CapabilityDefinition:
        return CapabilityDefinition(
            name=self.name,
            description="Use for current time requests by timezone or location alias.",
            request_schema={"timezone": "string"},
        )

    def parse_request(self, payload: dict[str, Any]) -> TimeRequest | None:
        timezone = str(payload.get("timezone", "")).strip()
        return TimeRequest(timezone=timezone) if timezone else None

    def can_handle(self, text: str) -> bool:
        lowered = text.lower()
        return "time in" in lowered or "current time" in lowered

    def execute(self, text: str) -> str:
        timezone = self._extract_timezone(text)
        return self.execute_request(TimeRequest(timezone=timezone))

    def execute_detailed(self, text: str) -> GeneralKnowledgeResult | None:
        timezone = self._extract_timezone(text)
        return self.execute_detailed_request(TimeRequest(timezone=timezone))

    def execute_request(self, request_obj: Any) -> str:
        detailed = self.execute_detailed_request(request_obj)
        if detailed is None:
            raise ProviderExecutionError("Missing datetime payload", unavailable=False)
        return str(detailed.response or "").strip()

    def execute_detailed_request(self, request_obj: Any) -> GeneralKnowledgeResult | None:
        if not isinstance(request_obj, TimeRequest):
            raise ProviderExecutionError("time provider requires a TimeRequest", unavailable=False)
        timezone_name = request_obj.timezone
        if not timezone_name:
            raise ProviderExecutionError("Could not determine timezone")
        data = self.fetch_json(f"https://worldtimeapi.org/api/timezone/{urllib.parse.quote(timezone_name)}")
        date_time_raw = data.get("datetime")
        if not isinstance(date_time_raw, str) or not date_time_raw.strip():
            raise ProviderExecutionError("Missing datetime payload")
        dt = datetime.fromisoformat(date_time_raw.replace("Z", "+00:00"))
        response = f"Current time in {timezone_name}: {dt.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        detail_lines = [
            f"## Current time in {timezone_name}",
            f"- Local time: {dt.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"- ISO: {dt.isoformat()}",
        ]
        return GeneralKnowledgeResult(
            provider=self.name,
            response=response,
            detail_type="markdown",
            detail_title=f"Time - {timezone_name}",
            detail_content="\n".join(detail_lines),
            metadata={
                "capability_status": "success",
                "render_operation": "replace_section",
                "capability": "time",
                "facts": {
                    "timezone": timezone_name,
                    "datetime": dt.isoformat(),
                },
                "source": "worldtimeapi.org",
                "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "valid_until": None,
                "coverage": {"summary": "single_timezone"},
                "warnings": [],
            },
        )

    def _extract_timezone(self, text: str) -> str:
        lowered = text.lower().strip()
        for name, zone in self.timezone_aliases.items():
            if name in lowered:
                return zone
        explicit = re.search(r"\b([A-Za-z_]+/[A-Za-z_]+)\b", text)
        if explicit is not None:
            return explicit.group(1)
        return ""


class LookupProvider(KnowledgeProvider):
    name = "lookup"

    def __init__(self, fetch_json: Callable[[str], dict[str, Any]]) -> None:
        self.fetch_json = fetch_json

    def definition(self) -> CapabilityDefinition:
        return CapabilityDefinition(
            name=self.name,
            description="Use for quick factual lookups.",
            request_schema={"query": "string"},
        )

    def parse_request(self, payload: dict[str, Any]) -> LookupRequest | None:
        query = str(payload.get("query", "")).strip()
        return LookupRequest(query=query) if query else None

    def can_handle(self, text: str) -> bool:
        lowered = text.lower().strip()
        if any(token in lowered for token in ["fahrenheit", "farenheit", "farenheight", "celsius", "celcius"]):
            return False
        if "that in" in lowered:
            return False
        prefixes = ["who", "what", "when", "where", "how old", "capital of", "population of"]
        return any(lowered.startswith(prefix) for prefix in prefixes)

    def execute(self, text: str) -> str:
        return self.execute_request(LookupRequest(query=text))

    def execute_detailed(self, text: str) -> GeneralKnowledgeResult | None:
        return self.execute_detailed_request(LookupRequest(query=text))

    def execute_request(self, request_obj: Any) -> str:
        detailed = self.execute_detailed_request(request_obj)
        if detailed is None:
            raise ProviderExecutionError("No lookup result", unavailable=False)
        return str(detailed.response or "").strip()

    def execute_detailed_request(self, request_obj: Any) -> GeneralKnowledgeResult | None:
        if not isinstance(request_obj, LookupRequest):
            raise ProviderExecutionError("lookup provider requires a LookupRequest", unavailable=False)
        encoded = urllib.parse.quote(request_obj.query)
        payload = self.fetch_json(f"https://api.duckduckgo.com/?q={encoded}&format=json&no_redirect=1&no_html=1")
        abstract = payload.get("AbstractText")
        if isinstance(abstract, str) and abstract.strip():
            return self._build_lookup_result(request_obj.query, abstract.strip())

        related = payload.get("RelatedTopics")
        if isinstance(related, list):
            for item in related:
                if isinstance(item, dict):
                    text_value = item.get("Text")
                    if isinstance(text_value, str) and text_value.strip():
                        return self._build_lookup_result(request_obj.query, text_value.strip())
                    nested = item.get("Topics")
                    if isinstance(nested, list):
                        for nested_item in nested:
                            if isinstance(nested_item, dict):
                                nested_text = nested_item.get("Text")
                                if isinstance(nested_text, str) and nested_text.strip():
                                    return self._build_lookup_result(request_obj.query, nested_text.strip())
        raise ProviderExecutionError("No lookup result", unavailable=False)

    def _build_lookup_result(self, query: str, answer: str) -> GeneralKnowledgeResult:
        return GeneralKnowledgeResult(
            provider=self.name,
            response=answer,
            detail_type="markdown",
            detail_title="Lookup Result",
            detail_content=f"## Lookup\n\n- Query: {query}\n- Result: {answer}",
            metadata={
                "capability_status": "success",
                "render_operation": "replace_section",
                "capability": "lookup",
                "facts": {"query": query, "answer": answer},
                "source": "duckduckgo",
                "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "valid_until": None,
                "coverage": {"summary": "single_fact"},
                "warnings": [],
            },
        )