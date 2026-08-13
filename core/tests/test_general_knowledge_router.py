from __future__ import annotations

import unittest

from core.assistant.general_knowledge_router import (
    CalculatorProvider,
    ConversionProvider,
    GeneralKnowledgeRouter,
    KnowledgeProvider,
    LookupRequest,
    NewsRequest,
    ProviderExecutionError,
    StockQuoteRequest,
    TimeRequest,
    WeatherProvider,
)


class _FailingProvider(KnowledgeProvider):
    name = "weather"

    def can_handle(self, text: str) -> bool:
        lowered = text.lower()
        return "weather" in lowered or "outside" in lowered

    def execute(self, text: str) -> str:
        raise ProviderExecutionError("service down")


class _NoResultProvider(KnowledgeProvider):
    name = "lookup"

    def can_handle(self, text: str) -> bool:
        return text.lower().startswith("what")

    def execute(self, text: str) -> str:
        raise ProviderExecutionError("no result", unavailable=False)


class GeneralKnowledgeRouterTests(unittest.TestCase):
    def test_calculator_provider_handles_expression(self) -> None:
        provider = CalculatorProvider()

        self.assertTrue(provider.can_handle("15 * 82"))
        response = provider.execute("15 * 82")

        self.assertIn("= 1230.0", response)

    def test_conversion_provider_miles_to_km(self) -> None:
        provider = ConversionProvider()

        self.assertTrue(provider.can_handle("10 miles in km"))
        response = provider.execute("10 miles in km")

        self.assertIn("10 miles = 16.0934 km", response)

    def test_weather_provider_formats_current_conditions(self) -> None:
        sample_payload = {
            "current_condition": [
                {
                    "temp_C": "21",
                    "weatherDesc": [{"value": "Partly cloudy"}],
                }
            ],
            "weather": [
                {
                    "hourly": [
                        {"time": "1200", "tempC": "22", "chanceofrain": "20", "weatherDesc": [{"value": "Cloudy"}]},
                        {"time": "1500", "tempC": "24", "chanceofrain": "35", "weatherDesc": [{"value": "Light rain"}]},
                    ]
                },
                {
                    "mintempC": "19",
                    "maxtempC": "28",
                    "hourly": [],
                },
            ],
        }

        provider = WeatherProvider(lambda url: sample_payload)
        response = provider.execute("what's the weather in London")

        self.assertIn("Current weather in London", response)
        self.assertIn("69.8F", response)

    def test_weather_provider_reports_rain_outlook_for_today(self) -> None:
        sample_payload = {
            "current_condition": [
                {
                    "temp_C": "27",
                    "weatherDesc": [{"value": "Sunny"}],
                }
            ],
            "weather": [
                {
                    "hourly": [
                        {"time": "0900", "chanceofrain": "10"},
                        {"time": "1200", "chanceofrain": "45"},
                        {"time": "1500", "chanceofrain": "65"},
                    ]
                }
            ],
        }

        provider = WeatherProvider(lambda url: sample_payload)
        response = provider.execute("is it supposed to rain here today?")

        self.assertIn("Rain outlook", response)
        self.assertIn("likely today", response)
        self.assertIn("65%", response)

    def test_weather_provider_uses_last_location_for_afternoon_follow_up(self) -> None:
        sample_payload = {
            "current_condition": [
                {
                    "temp_C": "26",
                    "weatherDesc": [{"value": "Partly cloudy"}],
                }
            ],
            "weather": [
                {
                    "hourly": [
                        {"time": "1200", "tempC": "27", "chanceofrain": "25", "weatherDesc": [{"value": "Cloudy"}]},
                        {"time": "1500", "tempC": "28", "chanceofrain": "30", "weatherDesc": [{"value": "Cloudy"}]},
                    ]
                }
            ],
        }

        router = GeneralKnowledgeRouter({"enabled": {}})
        provider = WeatherProvider(lambda url: sample_payload)
        router.providers = [provider]

        first = router.route("what's the weather in bluffton sc")
        self.assertIsNotNone(first)
        self.assertTrue(provider.can_handle_follow_up("what about this afternoon?"))
        follow_up_result = router.route("what about this afternoon?")

        self.assertIsNotNone(follow_up_result)
        follow_up = str(follow_up_result.response if follow_up_result is not None else "")
        self.assertIn("Forecast in bluffton sc this afternoon", follow_up)

    def test_weather_provider_handles_outside_phrase_with_default_location(self) -> None:
        sample_payload = {
            "current_condition": [
                {
                    "temp_C": "28",
                    "FeelsLikeC": "30",
                    "humidity": "65",
                    "windspeedMiles": "6",
                    "weatherDesc": [{"value": "Partly cloudy"}],
                }
            ],
            "weather": [{"mintempC": "24", "maxtempC": "31", "hourly": [{"time": "1200", "chanceofrain": "20"}]}],
        }

        provider = WeatherProvider(lambda url: sample_payload, default_location="Anderson, SC")

        self.assertTrue(provider.can_handle("what is it like outside today?"))
        response = provider.execute("what is it like outside today?")

        self.assertIn("Current weather in Anderson, SC", response)

    def test_weather_provider_formats_rest_of_day_request(self) -> None:
        sample_payload = {
            "current_condition": [{"temp_C": "30", "weatherDesc": [{"value": "Sunny"}]}],
            "weather": [
                {
                    "date": "2026-08-03",
                    "mintempC": "24",
                    "maxtempC": "33",
                    "hourly": [
                        {"time": "1200", "tempC": "31", "chanceofrain": "10", "weatherDesc": [{"value": "Sunny"}]},
                        {"time": "1500", "tempC": "32", "chanceofrain": "15", "weatherDesc": [{"value": "Sunny"}]},
                    ],
                }
            ],
        }

        provider = WeatherProvider(lambda url: sample_payload, default_location="Anderson, SC")
        response = provider.execute("what's the outlook for the rest of the day today?")

        self.assertIn("rest of today", response.lower())
        detailed = provider.execute_detailed("what's the outlook for the rest of the day today?")
        self.assertIsNotNone(detailed)
        assert detailed is not None
        self.assertIn("Today's Weather", str(detailed.detail_title))

    def test_weather_provider_formats_week_request_with_coverage_note(self) -> None:
        sample_payload = {
            "current_condition": [{"temp_C": "30", "weatherDesc": [{"value": "Sunny"}]}],
            "weather": [
                {"date": "2026-08-03", "mintempC": "24", "maxtempC": "33", "hourly": [{"time": "1200", "chanceofrain": "10", "weatherDesc": [{"value": "Sunny"}]}]},
                {"date": "2026-08-04", "mintempC": "23", "maxtempC": "31", "hourly": [{"time": "1200", "chanceofrain": "20", "weatherDesc": [{"value": "Partly cloudy"}]}]},
                {"date": "2026-08-05", "mintempC": "22", "maxtempC": "30", "hourly": [{"time": "1200", "chanceofrain": "35", "weatherDesc": [{"value": "Light rain"}]}]},
            ],
        }

        router = GeneralKnowledgeRouter({"enabled": {}})
        provider = WeatherProvider(lambda url: sample_payload, default_location="Anderson, SC")
        router.providers = [provider]

        first = router.route("what's it like outside today?")
        self.assertIsNotNone(first)
        result = router.route("how about the rest of the week?")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("monday:", str(result.response).lower())
        self.assertIn("tuesday:", str(result.response).lower())
        self.assertIn("wednesday:", str(result.response).lower())
        self.assertIn("provider coverage is limited", str(result.response).lower())
        self.assertIn("coverage is limited", str(result.detail_content).lower())

    def test_weather_provider_treats_this_week_as_week_range(self) -> None:
        sample_payload = {
            "current_condition": [{"temp_C": "30", "weatherDesc": [{"value": "Sunny"}]}],
            "weather": [
                {"date": "2026-08-03", "mintempC": "24", "maxtempC": "33", "hourly": [{"time": "1200", "chanceofrain": "10", "weatherDesc": [{"value": "Sunny"}]}]},
                {"date": "2026-08-04", "mintempC": "23", "maxtempC": "31", "hourly": [{"time": "1200", "chanceofrain": "20", "weatherDesc": [{"value": "Partly cloudy"}]}]},
            ],
        }

        provider = WeatherProvider(lambda url: sample_payload, default_location="Anderson, SC")
        response = provider.execute("what is the weather this week?")

        self.assertIn("monday:", response.lower())
        self.assertNotIn("current weather", response.lower())

    def test_weather_provider_reports_next_week_as_provider_coverage_limit(self) -> None:
        sample_payload = {
            "current_condition": [{"temp_C": "30", "weatherDesc": [{"value": "Sunny"}]}],
            "weather": [
                {"date": "2026-08-03", "mintempC": "24", "maxtempC": "33", "hourly": [{"time": "1200", "chanceofrain": "10", "weatherDesc": [{"value": "Sunny"}]}]},
                {"date": "2026-08-04", "mintempC": "23", "maxtempC": "31", "hourly": [{"time": "1200", "chanceofrain": "20", "weatherDesc": [{"value": "Partly cloudy"}]}]},
                {"date": "2026-08-05", "mintempC": "22", "maxtempC": "30", "hourly": [{"time": "1200", "chanceofrain": "35", "weatherDesc": [{"value": "Light rain"}]}]},
            ],
        }

        provider = WeatherProvider(lambda url: sample_payload, default_location="Anderson, SC")
        response = provider.execute("how about next week?")

        self.assertIn("next week's forecast", response.lower())
        self.assertIn("not available through this source", response.lower())
        self.assertNotIn("current weather", response.lower())

    def test_weather_result_metadata_contains_facts_and_provenance(self) -> None:
        sample_payload = {
            "current_condition": [
                {
                    "temp_C": "27",
                    "FeelsLikeC": "29",
                    "humidity": "55",
                    "windspeedMiles": "8",
                    "weatherDesc": [{"value": "Partly cloudy"}],
                }
            ],
            "weather": [
                {
                    "date": "2026-08-03",
                    "mintempC": "22",
                    "maxtempC": "31",
                    "hourly": [
                        {"time": "1200", "tempC": "28", "chanceofrain": "20", "weatherDesc": [{"value": "Cloudy"}]},
                    ],
                }
            ],
        }

        router = GeneralKnowledgeRouter({"enabled": {}})
        provider = WeatherProvider(lambda url: sample_payload, default_location="Anderson, SC")
        router.providers = [provider]

        result = router.route("what is it like outside today?")

        self.assertIsNotNone(result)
        assert result is not None
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        facts = metadata.get("facts") if isinstance(metadata.get("facts"), dict) else {}

        self.assertEqual(metadata.get("source"), "wttr.in")
        self.assertEqual(metadata.get("status"), "success")
        self.assertTrue(bool(metadata.get("retrieved_at")))
        self.assertIn("coverage", metadata)
        self.assertIn("warnings", metadata)
        self.assertEqual(facts.get("location"), "Anderson, SC")
        self.assertEqual(facts.get("range"), "current")
        self.assertEqual(facts.get("granularity"), "current")
        self.assertEqual(facts.get("condition"), "Partly cloudy")

    def test_non_weather_capabilities_include_facts_and_provenance_metadata(self) -> None:
        router = GeneralKnowledgeRouter({"enabled": {}})

        def fake_fetch_text(url: str) -> str:
            if "news.google.com" in url:
                return (
                    "<rss><channel>"
                    "<item><title>Alpha headline</title></item>"
                    "<item><title>Beta headline</title></item>"
                    "</channel></rss>"
                )
            if "stooq.com" in url:
                return "Symbol,Date,Time,Open,High,Low,Close,Volume\nNVDA.US,2026-08-03,22:00:00,100,105,99,102,123\n"
            return ""

        def fake_fetch_json(url: str) -> dict[str, object]:
            if "worldtimeapi" in url:
                return {"datetime": "2026-08-03T20:30:00+00:00"}
            if "duckduckgo" in url:
                return {"AbstractText": "A platypus is a semiaquatic mammal."}
            return {}

        for idx, provider in enumerate(router.providers):
            if provider.name == "news":
                router.providers[idx] = provider.__class__(fake_fetch_text)
            if provider.name == "stocks":
                router.providers[idx] = provider.__class__(fake_fetch_text)
            if provider.name == "time":
                router.providers[idx] = provider.__class__(fake_fetch_json)
            if provider.name == "lookup":
                router.providers[idx] = provider.__class__(fake_fetch_json)

        news = router.execute_capability_request("news", NewsRequest(topic="general", max_items=2))
        stocks = router.execute_capability_request("stocks", StockQuoteRequest(ticker="NVDA"))
        time_result = router.execute_capability_request("time", TimeRequest(timezone="Etc/UTC"))
        lookup = router.execute_capability_request("lookup", LookupRequest(query="what is platypus"))

        for result in [news, stocks, time_result, lookup]:
            self.assertIsNotNone(result)
            assert result is not None
            metadata = result.metadata if isinstance(result.metadata, dict) else {}
            facts = metadata.get("facts") if isinstance(metadata.get("facts"), dict) else {}
            self.assertTrue(bool(facts))
            self.assertTrue(bool(metadata.get("source")))
            self.assertTrue(bool(metadata.get("retrieved_at")))
            self.assertIn("coverage", metadata)
            self.assertIn("warnings", metadata)

    def test_weather_provider_does_not_claim_generic_today_follow_up_without_context(self) -> None:
        provider = WeatherProvider(lambda url: {"current_condition": [{"temp_C": "21", "weatherDesc": [{"value": "Cloudy"}]}]})

        self.assertFalse(provider.can_handle("what about today?"))
        self.assertFalse(provider.can_handle_follow_up("what about today?"))

    def test_router_returns_direct_weather_failure_response_without_llm_fallback(self) -> None:
        router = GeneralKnowledgeRouter({"enabled": {}})
        router.providers = [_FailingProvider()]

        result = router.route("what is it like outside today?")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.provider, "weather")
        self.assertIsNotNone(result.response)
        self.assertIsNone(result.fallback_notice)
        self.assertIn("could not retrieve", str(result.response).lower())

    def test_router_returns_fallback_notice_when_provider_fails(self) -> None:
        router = GeneralKnowledgeRouter({"enabled": {}})
        router.providers = [_FailingProvider()]

        result = router.route("weather in tokyo")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.provider, "weather")
        self.assertIsNotNone(result.response)
        self.assertIsNone(result.fallback_notice)
        self.assertIn("could not retrieve", str(result.response).lower())

    def test_router_returns_non_service_fallback_notice_for_no_result(self) -> None:
        router = GeneralKnowledgeRouter({"enabled": {}})
        router.providers = [_NoResultProvider()]

        result = router.route("what is that")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.provider, "lookup")
        self.assertIsNone(result.response)
        self.assertIn("quick lookup could not answer", str(result.fallback_notice).lower())

    def test_lookup_capability_request_related_topics_returns_structured_result(self) -> None:
        router = GeneralKnowledgeRouter({"enabled": {}})

        def fake_fetch_json(url: str) -> dict[str, object]:
            _ = url
            return {
                "RelatedTopics": [
                    {
                        "Text": "Platypus is a semiaquatic egg-laying mammal native to eastern Australia.",
                    }
                ]
            }

        for idx, provider in enumerate(router.providers):
            if provider.name == "lookup":
                router.providers[idx] = provider.__class__(fake_fetch_json)

        result = router.execute_capability_request("lookup", LookupRequest(query="tell me about the platypus"))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.provider, "lookup")
        self.assertIn("platypus", str(result.response).lower())
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        self.assertEqual(metadata.get("status"), "success")
        self.assertIn("facts", metadata)

    def test_lookup_capability_request_no_result_returns_fallback_result(self) -> None:
        class _NoResultTypedProvider(KnowledgeProvider):
            name = "lookup"

            def can_handle(self, text: str) -> bool:
                _ = text
                return True

            def execute(self, text: str) -> str:
                _ = text
                raise ProviderExecutionError("no result", unavailable=False)

            def execute_request(self, request_obj: object) -> str:
                _ = request_obj
                raise ProviderExecutionError("no result", unavailable=False)

        router = GeneralKnowledgeRouter({"enabled": {}})
        router.providers = [_NoResultTypedProvider()]

        result = router.execute_capability_request("lookup", LookupRequest(query="what is that"))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.provider, "lookup")
        self.assertIsNone(result.response)
        self.assertIn("quick lookup could not answer", str(result.fallback_notice).lower())
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        self.assertEqual(metadata.get("status"), "fallback")


if __name__ == "__main__":
    unittest.main()
