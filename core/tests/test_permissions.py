# File: core/tests/test_permissions.py

"""Section 10: what Iris is allowed to do, to what, and on whose say-so."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.actions.audit import ActionAuditLogger
from core.actions.executor import ActionExecutionContext, ActionExecutor, SystemAdapter
from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.actions.policy import ActionPolicy
from core.actions.registry import ActionRegistry
from core.audit.stream import AuditCategory, AuditStream
from core.permissions.limits import RateLimit, RateLimiter
from core.permissions.models import Decision, PermissionLevel, PermissionRequest
from core.permissions.policy import OUTBOUND_BUCKET, PermissionPolicy
from core.permissions.secrets import SecretError, SecretStore, environment_name
from core.permissions.targets import hosts_in, paths_in
from core.knowledge import KnowledgeGraph, KnowledgeRetriever
from core.knowledge.hypotheses import HypothesisTracker
from core.knowledge.review import KnowledgeReviewWorkflow
from core.server.app import create_app
from core.server.auth import ApiAuthenticator, AuthSettings, TOKEN_HEADER, token_from_headers
from core.storage.sqlite_database import SQLiteDatabase
from core.tools.models import ToolDefinition
from core.tools.registry import ToolRegistry


class OpenPathAction:
    name = "open_path"
    definition = ToolDefinition(
        name="open_path",
        description="Open a path.",
        permission=PermissionLevel.EXECUTE,
    )

    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        return ValidationResult(ok=True, resolved_target=str(request.arguments.get("path", "")))

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        self.executed.append(dict(request.arguments))
        return ActionResult(status="success", message="opened", action=self.name)


class FetchAction:
    name = "fetch"
    definition = ToolDefinition(
        name="fetch",
        description="Fetch a URL.",
        permission=PermissionLevel.READ,
        outbound=True,
    )

    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        return ValidationResult(ok=True, resolved_target=str(request.arguments.get("url", "")))

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        self.executed.append(dict(request.arguments))
        return ActionResult(status="success", message="fetched", action=self.name)


class TargetTests(unittest.TestCase):
    def test_paths_and_hosts_are_picked_out_of_the_arguments(self) -> None:
        arguments = {"path": "C:\\Work\\notes.txt", "url": "https://Example.COM/feed", "count": 3, "note": "plain"}
        self.assertEqual(paths_in(arguments), ("C:\\Work\\notes.txt",))
        self.assertEqual(hosts_in(arguments), ("example.com",))

    def test_a_url_is_not_mistaken_for_a_path(self) -> None:
        self.assertEqual(paths_in({"target": "https://example.com/a/b"}), ())


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.stream = AuditStream(self.root)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _request(self, **fields: Any) -> PermissionRequest:
        return PermissionRequest(tool="open_path", **fields)

    def test_a_path_outside_the_allowed_folders_is_refused(self) -> None:
        allowed = self.root / "work"
        allowed.mkdir()
        policy = PermissionPolicy(allowed_paths=[allowed])

        inside = policy.evaluate(self._request(paths=(str(allowed / "notes.txt"),)))
        outside = policy.evaluate(self._request(paths=(str(self.root / "elsewhere.txt"),)))

        self.assertEqual(inside.decision, Decision.ALLOW)
        self.assertEqual(outside.decision, Decision.DENY)
        self.assertEqual(outside.rule, "allowed_paths")

    def test_a_denied_folder_wins_over_the_allowed_one(self) -> None:
        allowed = self.root / "work"
        secret = allowed / "keys"
        secret.mkdir(parents=True)
        policy = PermissionPolicy(allowed_paths=[allowed], denied_paths=[secret])

        decision = policy.evaluate(self._request(paths=(str(secret / "id_rsa"),)))
        self.assertEqual(decision.decision, Decision.DENY)
        self.assertEqual(decision.rule, "denied_paths")

    def test_hosts_are_scoped_the_same_way_as_paths(self) -> None:
        policy = PermissionPolicy(denied_hosts=["ads.example.com"])
        self.assertTrue(policy.evaluate(self._request(hosts=("example.com",))).allowed)
        self.assertTrue(policy.evaluate(self._request(hosts=("ads.example.com",))).denied)

    def test_a_subdomain_matches_its_parent_rule(self) -> None:
        policy = PermissionPolicy(allowed_hosts=["example.com"])
        self.assertTrue(policy.evaluate(self._request(hosts=("api.example.com",))).allowed)
        self.assertTrue(policy.evaluate(self._request(hosts=("example.com.evil.net",))).denied)

    def test_a_level_can_be_turned_off_or_made_to_ask(self) -> None:
        policy = PermissionPolicy(modes={PermissionLevel.EXECUTE: Decision.DENY, PermissionLevel.WRITE: Decision.CONFIRM})
        self.assertTrue(policy.evaluate(self._request(permission=PermissionLevel.EXECUTE)).denied)
        self.assertTrue(policy.evaluate(self._request(permission=PermissionLevel.WRITE)).requires_confirmation)
        self.assertTrue(policy.evaluate(self._request(permission=PermissionLevel.READ)).allowed)

    def test_the_shipped_defaults_leave_todays_behaviour_alone(self) -> None:
        """A permission layer that changes what already works would be a regression."""
        policy = PermissionPolicy()
        for level in PermissionLevel:
            self.assertEqual(policy.evaluate(self._request(permission=level)).decision, Decision.ALLOW)

    def test_outbound_work_is_capped(self) -> None:
        policy = PermissionPolicy(limiter=RateLimiter({OUTBOUND_BUCKET: RateLimit(limit=2, per_seconds=3600)}))
        request = self._request(outbound=True)

        self.assertTrue(policy.enforce(request).allowed)
        self.assertTrue(policy.enforce(request).allowed)
        capped = policy.enforce(request)

        self.assertTrue(capped.denied)
        self.assertEqual(capped.rule, "rate_limit")
        self.assertTrue(policy.enforce(self._request(outbound=False)).allowed)

    def test_the_cap_only_counts_a_window(self) -> None:
        now = [1000.0]
        limiter = RateLimiter({OUTBOUND_BUCKET: RateLimit(limit=1, per_seconds=60)}, clock=lambda: now[0])
        policy = PermissionPolicy(limiter=limiter)

        self.assertTrue(policy.enforce(self._request(outbound=True)).allowed)
        self.assertTrue(policy.enforce(self._request(outbound=True)).denied)
        now[0] += 61
        self.assertTrue(policy.enforce(self._request(outbound=True)).allowed)

    def test_a_refusal_is_written_to_the_audit_trail(self) -> None:
        policy = PermissionPolicy(denied_hosts=["ads.example.com"], audit=self.stream)
        policy.enforce(self._request(hosts=("ads.example.com",), target="https://ads.example.com/x"))

        events = self.stream.read(category=AuditCategory.PERMISSION)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, "denied")
        self.assertEqual(events[0].data["rule"], "denied_hosts")

    def test_an_allowed_call_does_not_fill_the_trail(self) -> None:
        policy = PermissionPolicy(audit=self.stream)
        policy.enforce(self._request())
        self.assertEqual(self.stream.read(category=AuditCategory.PERMISSION), [])

    def test_config_reads_the_rules_off_the_json(self) -> None:
        policy = PermissionPolicy.from_config(
            {
                "execute": "deny",
                "denied_hosts": ["ads.example.com"],
                "rate_limits": {"outbound": {"limit": 5, "per_seconds": 60}},
            }
        )
        self.assertTrue(policy.evaluate(self._request(permission=PermissionLevel.EXECUTE)).denied)
        self.assertEqual(policy.describe()["rate_limits"]["outbound"], "5 per 1m")


class ExecutorEnforcementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.allowed = self.root / "work"
        self.allowed.mkdir()
        self.action = OpenPathAction()
        self.fetch = FetchAction()
        registry = ActionRegistry(ToolRegistry())
        registry.register(self.action)
        registry.register(self.fetch)
        self.policy = PermissionPolicy(
            allowed_paths=[self.allowed],
            limiter=RateLimiter({OUTBOUND_BUCKET: RateLimit(limit=1, per_seconds=3600)}),
            audit=AuditStream(self.root),
        )
        self.executor = ActionExecutor(
            registry=registry,
            policy=ActionPolicy(),
            audit=ActionAuditLogger(self.root),
            context=ActionExecutionContext(
                catalog=None,  # type: ignore[arg-type]
                allowed_roots=[],
                applications={},
                app_alias_map={},
                web_shortcuts={},
                system=SystemAdapter(),
            ),
            permissions=self.policy,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_an_action_outside_the_allowed_folders_never_runs(self) -> None:
        result = self.executor.execute(
            ActionRequest(action="open_path", arguments={"path": str(self.root / "outside.txt")})
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "permission_denied")
        self.assertEqual(self.action.executed, [])

    def test_an_action_inside_them_runs(self) -> None:
        target = self.allowed / "notes.txt"
        result = self.executor.execute(ActionRequest(action="open_path", arguments={"path": str(target)}))

        self.assertEqual(result.status, "success")
        self.assertEqual(self.action.executed, [{"path": str(target)}])

    def test_the_outbound_cap_stops_the_second_call(self) -> None:
        first = self.executor.execute(ActionRequest(action="fetch", arguments={"url": "https://example.com/a"}))
        second = self.executor.execute(ActionRequest(action="fetch", arguments={"url": "https://example.com/b"}))

        self.assertEqual(first.status, "success")
        self.assertEqual(second.error, "permission_denied")
        self.assertEqual(len(self.fetch.executed), 1)

    def test_a_refused_action_is_still_audited_as_an_action(self) -> None:
        self.executor.execute(ActionRequest(action="open_path", arguments={"path": str(self.root / "outside.txt")}))
        entries = ActionAuditLogger(self.root).read_recent(limit=10)
        self.assertEqual(entries[-1]["error"], "permission_denied")


class SecretStoreTests(unittest.TestCase):
    class FakeKeyring:
        def __init__(self) -> None:
            self.values: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, name: str) -> str | None:
            return self.values.get((service, name))

        def set_password(self, service: str, name: str, value: str) -> None:
            self.values[(service, name)] = value

        def delete_password(self, service: str, name: str) -> None:
            del self.values[(service, name)]

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.index = Path(self.tempdir.name) / "secrets.json"
        self.backend = self.FakeKeyring()
        self.store = SecretStore(index_path=self.index, backend=self.backend, environ={})

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_secret_round_trips_through_the_credential_store(self) -> None:
        self.store.set("api_token:bot", "s3cret")
        self.assertEqual(self.store.get("api_token:bot"), "s3cret")
        self.assertEqual(self.store.names(), ["api_token:bot"])

    def test_the_index_keeps_names_and_never_values(self) -> None:
        """The list of what is stored is useful; the values are the thing being protected."""
        self.store.set("api_token:bot", "s3cret")
        self.assertNotIn("s3cret", self.index.read_text(encoding="utf-8"))

    def test_the_environment_answers_when_there_is_no_credential_store(self) -> None:
        store = SecretStore(backend=None, environ={environment_name("api_token:bot"): "from-env"})
        store._backend_loaded = True
        self.assertEqual(store.get("api_token:bot"), "from-env")

    def test_writing_without_a_store_says_what_to_do_instead(self) -> None:
        store = SecretStore(backend=None, environ={})
        store._backend_loaded = True
        with self.assertRaises(SecretError) as caught:
            store.set("api_token:bot", "s3cret")
        self.assertIn("IRIS_SECRET_API_TOKEN_BOT", str(caught.exception))

    def test_clearing_forgets_the_name_too(self) -> None:
        self.store.set("api_token:bot", "s3cret")
        self.store.delete("api_token:bot")
        self.assertEqual(self.store.names(), [])
        self.assertIsNone(self.store.get("api_token:bot"))


class HttpAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.stream = AuditStream(Path(self.tempdir.name))
        self.secrets = SecretStore(
            backend=SecretStoreTests.FakeKeyring(), index_path=Path(self.tempdir.name) / "secrets.json", environ={}
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _auth(self, **settings: Any) -> ApiAuthenticator:
        return ApiAuthenticator(self.secrets, AuthSettings(**settings), audit=self.stream)

    def test_the_surface_stays_open_until_a_token_exists(self) -> None:
        """Nobody gets locked out of a running Iris by installing this."""
        auth = self._auth()
        self.assertFalse(auth.enforcing)
        self.assertTrue(auth.authenticate(path="/recall", token=None, client_host="127.0.0.1").ok)
        self.assertIsNotNone(auth.startup_notice())

    def test_a_stored_token_turns_the_check_on(self) -> None:
        self.secrets.set("api_token:bot", "bot-token")
        auth = self._auth()

        self.assertTrue(auth.enforcing)
        self.assertFalse(auth.authenticate(path="/recall", token=None, client_host="127.0.0.1").ok)
        self.assertFalse(auth.authenticate(path="/recall", token="wrong", client_host="127.0.0.1").ok)
        accepted = auth.authenticate(path="/recall", token="bot-token", client_host="127.0.0.1")
        self.assertTrue(accepted.ok)
        self.assertEqual(accepted.client, "bot")

    def test_health_stays_answerable_without_a_token(self) -> None:
        self.secrets.set("api_token:bot", "bot-token")
        self.assertTrue(self._auth().authenticate(path="/health", token=None, client_host="127.0.0.1").ok)

    def test_another_machine_is_turned_away_before_the_token_is_read(self) -> None:
        self.secrets.set("api_token:bot", "bot-token")
        outcome = self._auth().authenticate(path="/recall", token="bot-token", client_host="192.168.1.50")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status_code, 403)

    def test_demanding_a_token_with_none_configured_fails_closed(self) -> None:
        outcome = self._auth(required=True).authenticate(path="/recall", token="anything", client_host="127.0.0.1")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status_code, 503)

    def test_a_refusal_is_audited_without_the_token(self) -> None:
        self.secrets.set("api_token:bot", "bot-token")
        self._auth().authenticate(path="/recall", token="guessed-token", client_host="127.0.0.1")

        events = self.stream.read(category=AuditCategory.PERMISSION)
        self.assertEqual(events[0].event, "http")
        self.assertNotIn("guessed-token", self.stream.path.read_text(encoding="utf-8"))

    def test_a_bearer_header_is_accepted_too(self) -> None:
        self.assertEqual(token_from_headers({"authorization": "Bearer abc"}), "abc")
        self.assertEqual(token_from_headers({TOKEN_HEADER: "xyz"}), "xyz")


class _Service:
    def __init__(self, root: Path, secrets: SecretStore, audit: AuditStream) -> None:
        database = SQLiteDatabase(root / "knowledge.db")
        self.knowledge = KnowledgeGraph(database)
        self.knowledge_retriever = KnowledgeRetriever(database)
        self.knowledge_review = KnowledgeReviewWorkflow(HypothesisTracker(self.knowledge))
        self.secrets = secrets
        self.audit_stream = audit
        self.config = {"assistant_name": "Iris", "http": {"clients": ["bot"]}}


class HttpSurfaceTests(unittest.TestCase):
    """The surface the trading bot calls (13.1) now has a door on it."""

    def setUp(self) -> None:
        #! @allow-local-import
        from fastapi.testclient import TestClient

        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.secrets = SecretStore(
            backend=SecretStoreTests.FakeKeyring(), index_path=root / "secrets.json", environ={}
        )
        self.service = _Service(root, self.secrets, AuditStream(root))
        self.client = TestClient(create_app(self.service))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_call_without_a_token_is_refused_once_one_is_set(self) -> None:
        self.assertEqual(self.client.post("/recall", json={"text": "anything"}).status_code, 200)

        self.secrets.set("api_token:bot", "bot-token")

        self.assertEqual(self.client.post("/recall", json={"text": "anything"}).status_code, 401)
        allowed = self.client.post("/recall", json={"text": "anything"}, headers={TOKEN_HEADER: "bot-token"})
        self.assertEqual(allowed.status_code, 200)

    def test_health_stays_open_for_monitoring(self) -> None:
        self.secrets.set("api_token:bot", "bot-token")
        self.assertEqual(self.client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
