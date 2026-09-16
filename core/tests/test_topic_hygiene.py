# File: core/tests/test_topic_hygiene.py

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.assistant.topic_commands import TopicCommandHandler
from core.conversation.persistent_memory import TopicMemoryService
from core.conversation.persistent_memory.models import MemoryConfig
from core.conversation.persistent_memory.naming import derive_topic_title, parse_model_title
from core.conversation.persistent_memory.text import _generate_topic_name, _is_transient_message, _is_transient_topic_name


class TransientTests(unittest.TestCase):
    def test_greetings_weather_time_help_and_commands_are_transient(self) -> None:
        for message in (
            "good morning",
            "Goood afternoon!",
            "good evening iris",
            "what's the weather like today",
            "3 day weather forecast",
            "what is the current time",
            "what time is it",
            "help",
            "list saved topics",
            "/list topics",
            "my location is 29626",
            "",
        ):
            with self.subTest(message=message):
                self.assertTrue(_is_transient_message(message))

    def test_real_requests_are_not_transient(self) -> None:
        for message in (
            "I need to buy a laptop with a touchscreen",
            "create a 500 word story about joe and julie on the beach",
            "write a lookup function for the customer field",
            "tell me about the platypus",
            "good morning, can you review my trading bot exits",
        ):
            with self.subTest(message=message):
                self.assertFalse(_is_transient_message(message))

    def test_transient_topic_names(self) -> None:
        for name in ("Good morning", "Goood afternoon", "Good morningh", "Weather", "Weather like today", "Help", "Today", "S current time", "List saved topic", "General"):
            with self.subTest(name=name):
                self.assertTrue(_is_transient_topic_name(name))
        for name in ("Convertible laptop research", "Make up 500 word story", "Tell me platypu"):
            with self.subTest(name=name):
                self.assertFalse(_is_transient_topic_name(name))

    def test_topic_name_collapses_repeated_letters(self) -> None:
        self.assertEqual(_generate_topic_name("Goood afternoon"), "Good afternoon")


class NamingTests(unittest.TestCase):
    def test_code_requests_get_domain_and_subject(self) -> None:
        self.assertEqual(derive_topic_title("write a BC function for customer lookup", "codeunit 50100 ..."), "BC: Customer Lookup Function")
        self.assertEqual(derive_topic_title("can you write me a python script that parses csv files", "import csv"), "Python: Parses Csv Files Script")
        self.assertEqual(derive_topic_title("write a lookup function for the customer field in Business Central", "..."), "BC: Lookup for the Customer Field Function")

    def test_creative_work_is_named_after_its_title(self) -> None:
        story = '**"Sunset Serendipity"**\n\n' + "The sun dipped low over the horizon. " * 20
        self.assertEqual(derive_topic_title("create a 500 word story about joe and julie meeting on the beach", story), "Story: Sunset Serendipity")
        untitled = "Joe walked along the sandy shore. " * 20
        self.assertEqual(derive_topic_title("create a story about joe and julie meeting on the beach", untitled), "Story: Joe and Julie Meeting on the Beach")

    def test_general_requests_keep_the_subject_only(self) -> None:
        self.assertEqual(derive_topic_title("tell me about the platypus", "The platypus is..."), "Platypus")
        self.assertEqual(derive_topic_title("I need to buy a laptop with a touchscreen", "Sure."), "Buy a Laptop with a Touchscreen")
        self.assertEqual(derive_topic_title("does hulu have a plan that has no ads", "Yes."), "Does Hulu Have a Plan That Has")

    def test_model_title_parsing(self) -> None:
        self.assertEqual(parse_model_title('"BC: Customer Lookup Function".'), "BC: Customer Lookup Function")
        self.assertEqual(parse_model_title("Topic: Story: Sunset Serendipity\nbecause..."), "Story: Sunset Serendipity")
        self.assertIsNone(parse_model_title(""))
        self.assertIsNone(parse_model_title("this is a very long sentence that is clearly not a title at all"))


class MemoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config = MemoryConfig.from_config({"memory": {"enabled": True, "database_path": str(Path(self._tmp.name) / "conversations.db")}})
        self.service = TopicMemoryService(config)
        self.service.namer_in_background = False
        self.lines: list[str] = []
        self.handler = TopicCommandHandler(self.service, output=lambda text, role=None: self.lines.append(text))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _turn(self, conversation: str, message: str, reply: str = "ok") -> int:
        prepared = self.service.prepare_user_turn(conversation_id=conversation, conversation_title=None, user_message=message)
        self.service.finalize_assistant_turn(prepared, reply)
        return prepared.topic_id

    def test_misspelled_repeat_reuses_existing_topic(self) -> None:
        first = self._turn("c1", "I want to research convertible laptops with a touchscreen")
        second = self._turn("c2", "I want to reserch convertible laptopss with a touchscreen")
        self.assertEqual(first, second)
        self.assertEqual(len(self.service.list_topics()), 1)

    def test_merge_moves_messages_and_archives_source(self) -> None:
        keep = self._turn("c1", "plan a trip to tasmania in march", "Sure, March is autumn there.")
        dupe = self._turn("c2", "help me organise a fishing weekend on the lake", "Happy to.")
        self.assertNotEqual(keep, dupe)
        ok, message = self.service.merge_topics(str(dupe), str(keep))
        self.assertTrue(ok, message)
        self.assertIn("2 message(s) moved", message)
        names = [topic.id for topic in self.service.list_topics()]
        self.assertEqual(names, [keep])
        contents = [item["content"] for item in self.service.topic_messages(keep)]
        self.assertIn("help me organise a fishing weekend on the lake", contents)
        self.assertIn("plan a trip to tasmania in march", contents)
        bad_ok, bad_message = self.service.merge_topics(str(keep), str(keep))
        self.assertFalse(bad_ok)

    def test_archive_and_prune(self) -> None:
        real = self._turn("c1", "compare the three lake fishing boats we shortlisted")
        junk = self.service.topics.create_topic("Good morningh")
        junk2 = self.service.topics.create_topic("Weather like today")
        pruned = self.service.prune_transient_topics()
        self.assertEqual({topic.id for topic in pruned}, {junk.id, junk2.id})
        self.assertEqual([topic.id for topic in self.service.list_topics()], [real])
        ok, _message = self.service.archive_topic(str(real))
        self.assertTrue(ok)
        self.assertEqual(self.service.list_topics(), [])

    def test_new_topics_are_named_after_the_answer(self) -> None:
        story = '**"Sunset Serendipity"**\n\n' + "The sun dipped low over the horizon. " * 20
        topic_id = self._turn("c1", "create a 500 word story about joe and julie meeting on the beach", story)
        self.assertEqual(self.service.current_topic_name(topic_id), "Story: Sunset Serendipity")
        prompts: list[str] = []

        def namer(system_prompt: str, prompt: str) -> str:
            prompts.append(prompt)
            return '"BC: Customer Lookup Function"'

        self.service.namer = namer
        code_topic = self._turn("c2", "write a BC function for customer lookup", "codeunit 50100 CustomerLookup { }")
        self.assertEqual(self.service.current_topic_name(code_topic), "BC: Customer Lookup Function")
        self.assertEqual(len(prompts), 1)
        self.assertIn("customer lookup", prompts[0])
        follow_up = self._turn("c2", "add a filter on the customer posting group to that function", "Done.")
        self.assertEqual(follow_up, code_topic)
        self.assertEqual(len(prompts), 1)
        ok, message = self.service.rename_topic(str(code_topic), "BC: Customer Lookup")
        self.assertTrue(ok, message)
        self.assertEqual(self.service.current_topic_name(code_topic), "BC: Customer Lookup")
        self.service.namer = None
        changes = self.service.retitle_topics()
        self.assertIn(("BC: Customer Lookup", "BC: Customer Lookup Function"), changes)

    def test_refused_first_answer_does_not_fix_the_name(self) -> None:
        story = '**"Sunset Serendipity"**\n\n' + "The sun dipped low over the horizon. " * 20
        topic_id = self._turn("c1", "create a 500 word story about an explicit encounter on a beach", "I'm unable to assist with that request. Let me know if you'd like help with something else.")
        provisional_name = self.service.current_topic_name(topic_id)
        self.assertNotIn("Story:", provisional_name)
        retry = self._turn("c1", "create an explicit story with 500 words that tells about joe and julie meeting on the beach", story)
        self.assertEqual(retry, topic_id)
        self.assertEqual(self.service.current_topic_name(topic_id), "Story: Sunset Serendipity")
        self.assertFalse(self.service.topics.get_state(topic_id).get("title_provisional"))
        self.service.topics.rename_topic(topic_id, "Something else")
        changes = self.service.retitle_topics()
        self.assertIn(("Something else", "Story: Sunset Serendipity"), changes)

    def test_commands_produce_clickable_details(self) -> None:
        topic_id = self._turn("c1", "compare the three lake fishing boats we shortlisted", "The Tracker is cheapest.")
        state: dict[str, object] = {}
        self.assertTrue(self.handler.handle("/topics", state))
        detail = state["command_detail"]
        self.assertEqual(detail["title"], "Topics")
        self.assertIn(f"iris://run?cmd=%2Ftopic%20{topic_id}", detail["content"])
        self.assertTrue(detail["content"].startswith(f"{topic_id}\\. ["))
        self.assertNotIn("delete", detail["content"].split("\n\n")[0])
        state = {}
        self.assertTrue(self.handler.handle(f"/topic {topic_id}", state))
        detail = state["command_detail"]
        self.assertIn("**Conversation**", detail["content"])
        self.assertIn("The Tracker is cheapest.", detail["content"])
        self.assertIn("back to all topics", detail["content"])
        state = {}
        other = self._turn("c2", "which trailer hitch fits the truck")
        self.assertTrue(self.handler.handle(f"/topic merge {other} into {topic_id}", state))
        self.assertTrue(any("Merged" in line for line in self.lines))
        self.assertEqual(len(self.service.list_topics()), 1)
        state = {}
        self.assertTrue(self.handler.handle(f"/topic delete {topic_id}", state))
        self.assertEqual(self.service.list_topics(), [])
        self.assertTrue(self.handler.handle("/topics prune", {}))


if __name__ == "__main__":
    unittest.main()
