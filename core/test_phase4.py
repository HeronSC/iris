import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from core.actions.audit import ActionAuditLogger
from core.actions.executor import ActionExecutionContext, ActionExecutor, SystemAdapter
from core.actions.implementations.clipboard import ClipboardAction
from core.actions.implementations.add_document_root import AddDocumentRootAction
from core.actions.implementations.launch_application import LaunchApplicationAction
from core.actions.implementations.open_file import OpenFileAction
from core.actions.implementations.open_folder import OpenFolderAction, ShowInExplorerAction
from core.actions.implementations.open_url import OpenUrlAction
from core.actions.implementations.scan_document_root import ScanDocumentRootAction
from core.actions.implementations.update_config import UpdateConfigAction
from core.actions.models import ActionRequest, ApplicationConfig
from core.actions.policy import ActionPolicy
from core.actions.registry import ActionRegistry
from core.assistant.action_commands import ActionCommandHandler
from core.assistant.conversation_synonyms import ConversationSynonymStore
from core.assistant.pending_action_manager import PendingActionManager
from core.assistant.search_commands import SearchCommandHandler
from core.config.loader import ConfigLoader
from core.documents.catalog import DocumentCatalog
from core.documents.extractors.text_extractor import TextExtractor
from core.documents.models import DocumentSearchConfig
from core.documents.query_parser import FileSearchQueryParser, QueryParserConfig
from core.documents.scanner import DocumentScanner
from core.documents.search_service import DocumentSearchService
from core.state.search_result_context import SearchResultContext
from core.storage.sqlite_database import SQLiteDatabase


class FakeSystemAdapter(SystemAdapter):
    def __init__(self) -> None:
        self.opened_files: list[str] = []
        self.opened_folders: list[str] = []
        self.explorer_targets: list[str] = []
        self.launched_apps: list[str] = []
        self.opened_urls: list[str] = []
        self.clipboard_values: list[str] = []

    def open_file(self, path: str) -> None:
        self.opened_files.append(path)

    def open_folder(self, path: str) -> None:
        self.opened_folders.append(path)

    def show_in_explorer(self, path: str) -> None:
        self.explorer_targets.append(path)

    def launch_application(self, executable: str) -> None:
        self.launched_apps.append(executable)

    def open_url(self, url: str) -> None:
        self.opened_urls.append(url)

    def copy_text(self, text: str) -> None:
        self.clipboard_values.append(text)


class FakePendingExecutor:
    def __init__(self, *, has_pending: bool = True, expired_notice: bool = False) -> None:
        self.pending = has_pending
        self.expired_notice = expired_notice
        self.confirmed = 0
        self.cancelled = 0

    def has_pending_confirmation(self) -> bool:
        return self.pending

    def confirm_pending(self):
        self.pending = False
        self.confirmed = self.confirmed + 1
        return type("Result", (), {"message": "Confirmed pending action."})()

    def cancel_pending(self):
        self.pending = False
        self.cancelled = self.cancelled + 1
        return type("Result", (), {"message": "Pending action was cancelled."})()

    def consume_expired_confirmation_notice(self) -> bool:
        if not self.expired_notice:
            return False
        self.expired_notice = False
        return True

    def pending_description(self) -> str | None:
        if not self.pending:
            return None
        return "Remove D:\\HenryZuraw\\Documents from indexing"


class PhaseFourTests(unittest.TestCase):
    def _build_services(self, root: Path, confirmation_ttl_seconds: int = 120):
        docs = root / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        target = docs / "etrade.txt"
        target.write_text("etrade holdings", encoding="utf-8")

        catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
        scanner = DocumentScanner(
            config=DocumentSearchConfig(
                roots=[docs],
                excluded_directories=set(),
                supported_extensions={".txt"},
                max_file_size_mb=10,
            ),
            catalog=catalog,
            extractors=[TextExtractor()],
        )
        scanner.scan()

        parser = FileSearchQueryParser(QueryParserConfig(default_roots=[docs]))
        search_service = DocumentSearchService(catalog, parser)
        search_context = SearchResultContext(ttl_minutes=30)
        search_handler = SearchCommandHandler(search_service, search_context=search_context)

        app_path = root / "tools" / "Code.exe"
        app_path.parent.mkdir(parents=True, exist_ok=True)
        app_path.write_text("", encoding="utf-8")

        app = ApplicationConfig(
            id="vscode",
            display_name="Visual Studio Code",
            executable=str(app_path),
            aliases=["vs code", "code", "vscode"],
        )
        app_map = {"vscode": app}
        alias_map = {
            "vscode": "vscode",
            "vs code": "vscode",
            "code": "vscode",
            "visual studio code": "vscode",
        }

        adapter = FakeSystemAdapter()
        registry = ActionRegistry()
        registry.register(OpenFileAction())
        registry.register(OpenFolderAction())
        registry.register(ShowInExplorerAction())
        registry.register(LaunchApplicationAction())
        registry.register(OpenUrlAction())
        registry.register(ClipboardAction())
        registry.register(AddDocumentRootAction())
        registry.register(ScanDocumentRootAction())
        registry.register(UpdateConfigAction())

        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "assistant_name": "Iris",
                    "memory_path": str(root / "memory"),
                    "model": "qwen3:8b",
                    "llm_server": "http://localhost:11434",
                    "document_search": {
                        "roots": [str(docs)],
                        "excluded_directories": [],
                        "supported_extensions": [".txt"],
                        "max_file_size_mb": 10,
                    },
                    "applications": {
                        "vscode": {
                            "display_name": "Visual Studio Code",
                            "executable": str(app_path),
                            "aliases": ["vs code", "code", "vscode"],
                        }
                    },
                    "web_shortcuts": {
                        "bc-sandbox": "https://example.test/sandbox"
                    },
                    "action_audit_path": str(root / "audit"),
                }
            ),
            encoding="utf-8",
        )

        audit = ActionAuditLogger(root / "audit")
        executor = ActionExecutor(
            registry=registry,
            policy=ActionPolicy(),
            audit=audit,
            context=ActionExecutionContext(
                catalog=catalog,
                allowed_roots=[docs],
                applications=app_map,
                app_alias_map=alias_map,
                web_shortcuts={"bc-sandbox": "https://example.test/sandbox"},
                system=adapter,
                config_path=config_path,
                document_scanner=scanner,
            ),
            confirmation_ttl_seconds=confirmation_ttl_seconds,
        )
        action_handler = ActionCommandHandler(executor, audit, search_context)
        return target, search_handler, action_handler, adapter, audit, executor, search_context

    def test_config_loader_parses_phase4_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory_dir = root / "memory"
            memory_dir.mkdir(parents=True, exist_ok=True)
            docs_dir = root / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            app_path = root / "apps" / "App.exe"
            app_path.parent.mkdir(parents=True, exist_ok=True)
            app_path.write_text("", encoding="utf-8")

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "assistant_name": "Iris",
                        "memory_path": str(memory_dir),
                        "model": "qwen",
                        "llm_server": "http://localhost:11434",
                        "document_search": {
                            "roots": [str(docs_dir)],
                            "excluded_directories": [],
                            "supported_extensions": [".txt"],
                            "max_file_size_mb": 10,
                        },
                        "applications": {
                            "sample": {
                                "display_name": "Sample",
                                "executable": str(app_path),
                                "aliases": ["sample app"],
                            }
                        },
                        "web_shortcuts": {
                            "portal": "https://example.test"
                        },
                        "action_audit_path": str(root / "audit"),
                    }
                ),
                encoding="utf-8",
            )

            loaded = ConfigLoader(config_path).load()
            self.assertIn("sample", loaded["applications"])
            self.assertIn("portal", loaded["web_shortcuts"])

    def test_open_show_copy_launch_url_and_recent_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, search_handler, action_handler, adapter, _audit, _executor, _ctx = self._build_services(root)

            self.assertTrue(search_handler.handle("/search etrade", {}))
            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(action_handler.handle("/open 1", {}))
                self.assertTrue(action_handler.handle("/show 1", {}))
                self.assertTrue(action_handler.handle("/folder 1", {}))
                self.assertTrue(action_handler.handle("/copy path 1", {}))
                self.assertTrue(action_handler.handle("/launch vs code", {}))
                self.assertTrue(action_handler.handle("/open-url bc-sandbox", {}))
                self.assertTrue(action_handler.handle("/actions recent", {}))

            self.assertEqual(adapter.opened_files[-1], str(target.resolve()))
            self.assertEqual(adapter.explorer_targets[-1], str(target.resolve()))
            self.assertEqual(adapter.opened_folders[-1], str(target.resolve().parent))
            self.assertEqual(adapter.clipboard_values[-1], str(target.resolve()))
            self.assertTrue(adapter.launched_apps)
            self.assertEqual(adapter.opened_urls[-1], "https://example.test/sandbox")
            self.assertIn("success", output.getvalue().lower())

    def test_action_validation_failures_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, search_handler, action_handler, _adapter, _audit, executor, _ctx = self._build_services(root)

            result_unknown = executor.execute(ActionRequest(action="run_command", arguments={"command": "dir"}))
            self.assertEqual(result_unknown.status, "failed")

            result_url = executor.execute(ActionRequest(action="open_url", arguments={"url": "javascript:alert(1)"}))
            self.assertEqual(result_url.status, "failed")
            self.assertIn("unsafe", result_url.message.lower())

            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(action_handler.handle("/open 99", {}))
            self.assertIn("no recent search result", output.getvalue().lower())

    def test_pending_action_manager_confirms_natural_language_without_falling_through(self) -> None:
        executor = FakePendingExecutor(has_pending=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConversationSynonymStore(Path(tmpdir) / "Configuration" / "conversation_synonyms.json")
            manager = PendingActionManager(executor, synonyms=store, phrase_log_path=Path(tmpdir) / "audit" / "conversation_phrases.jsonl")

            output = StringIO()
            with redirect_stdout(output):
                handled = manager.handle("sounds good")

            self.assertTrue(handled)
            self.assertEqual(executor.confirmed, 1)
            self.assertEqual(executor.cancelled, 0)
            self.assertIn("confirmed pending action", output.getvalue().lower())

            log_path = Path(tmpdir) / "audit" / "conversation_phrases.jsonl"
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("sounds good", lines[0])
            self.assertIn('"mapped_to": "confirm"', lines[0])

    def test_pending_action_manager_cancels_natural_language_without_falling_through(self) -> None:
        executor = FakePendingExecutor(has_pending=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConversationSynonymStore(Path(tmpdir) / "Configuration" / "conversation_synonyms.json")
            manager = PendingActionManager(executor, synonyms=store)

            output = StringIO()
            with redirect_stdout(output):
                handled = manager.handle("not now")

            self.assertTrue(handled)
            self.assertEqual(executor.confirmed, 0)
            self.assertEqual(executor.cancelled, 1)
            self.assertIn("cancelled", output.getvalue().lower())

    def test_pending_action_manager_releases_modified_request_back_to_router(self) -> None:
        executor = FakePendingExecutor(has_pending=True)
        manager = PendingActionManager(executor)

        output = StringIO()
        with redirect_stdout(output):
            handled = manager.handle("Actually index D:\\Documents too")

        self.assertFalse(handled)
        self.assertEqual(executor.cancelled, 1)
        self.assertIn("interpreting your updated request", output.getvalue().lower())

    def test_pending_action_manager_reminds_user_about_pending_action(self) -> None:
        executor = FakePendingExecutor(has_pending=True)
        manager = PendingActionManager(executor)

        output = StringIO()
        with redirect_stdout(output):
            handled = manager.handle("what time is it")

        self.assertTrue(handled)
        self.assertIn("awaiting confirmation", output.getvalue().lower())
        self.assertIn("tell me what to change", output.getvalue().lower())

    def test_pending_confirmation_message_is_friendly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, _action_handler, _adapter, _audit, executor, _ctx = self._build_services(root)

            result = executor.execute(
                ActionRequest(
                    action="update_config",
                    arguments={"operation": "remove_document_root", "root": str(root / "docs")},
                    source="test",
                    reason="Remove test root",
                )
            )

            self.assertEqual(result.status, "pending_confirmation")
            self.assertIn("awaiting confirmation", result.message.lower())
            self.assertIn("reply naturally", result.message.lower())
            self.assertIn("tell me what to change", result.message.lower())
            self.assertIsNotNone(result.confirmation_preview)
            self.assertIn("document_search.roots", result.confirmation_preview.target)
            self.assertIn("After", "After")

    def test_natural_language_action_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, search_handler, action_handler, adapter, _audit, _executor, _ctx = self._build_services(root)

            self.assertTrue(search_handler.handle("/search etrade", {}))
            output = StringIO()
            with redirect_stdout(output), patch("builtins.input", return_value="n"):
                self.assertTrue(action_handler.handle_natural_language("Open number 1"))
                self.assertTrue(action_handler.handle_natural_language("Open the folder for number 1"))
                self.assertTrue(action_handler.handle_natural_language("Open bc-sandbox"))
                self.assertTrue(action_handler.handle_natural_language("Launch VS Code"))
                self.assertTrue(action_handler.handle_natural_language("Launch something unknown"))

            self.assertEqual(adapter.opened_files[-1], str(target.resolve()))
            self.assertEqual(adapter.opened_folders[-1], str(target.resolve().parent))
            self.assertTrue(adapter.launched_apps)
            self.assertEqual(adapter.opened_urls[-1], "https://example.test/sandbox")

    def test_launch_unknown_app_prompts_adds_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, action_handler, adapter, _audit, _executor, _ctx = self._build_services(root)

            app_path = root / "tools" / "NotepadPlusPlus.exe"
            app_path.parent.mkdir(parents=True, exist_ok=True)
            app_path.write_text("", encoding="utf-8")

            output = StringIO()
            with redirect_stdout(output), patch(
                "builtins.input",
                side_effect=["yes", str(app_path), "Notepad++", "notepad++, npp"],
            ):
                self.assertTrue(action_handler.handle("/launch notepad++", {}))

            self.assertIn(str(app_path), adapter.launched_apps)
            self.assertIn("added/updated application", output.getvalue().lower())

    def test_open_url_unknown_shortcut_prompts_adds_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, action_handler, adapter, _audit, _executor, _ctx = self._build_services(root)

            output = StringIO()
            with redirect_stdout(output), patch(
                "builtins.input",
                side_effect=["yes", "https://status.example.test"],
            ):
                self.assertTrue(action_handler.handle("/open-url status", {}))

            self.assertEqual(adapter.opened_urls[-1], "https://status.example.test")
            self.assertIn("updated web shortcut", output.getvalue().lower())

    def test_yes_is_not_consumed_without_pending_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, action_handler, _adapter, _audit, _executor, _ctx = self._build_services(root)

            self.assertFalse(action_handler.handle_natural_language("Yes"))

    def test_yes_confirms_when_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, action_handler, adapter, _audit, executor, _ctx = self._build_services(root)

            result = executor.execute(
                ActionRequest(
                    action="open_url",
                    arguments={"url": "https://pending-confirm.example"},
                    source="test",
                    reason="pending confirm",
                )
            )
            self.assertEqual(result.status, "pending_confirmation")
            self.assertTrue(action_handler.handle_natural_language("Yes"))
            self.assertEqual(adapter.opened_urls[-1], "https://pending-confirm.example")

    def test_okay_confirms_when_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, action_handler, adapter, _audit, executor, _ctx = self._build_services(root)

            result = executor.execute(
                ActionRequest(
                    action="open_url",
                    arguments={"url": "https://pending-okay.example"},
                    source="test",
                    reason="pending okay",
                )
            )
            self.assertEqual(result.status, "pending_confirmation")
            self.assertTrue(action_handler.handle_natural_language("okay"))
            self.assertEqual(adapter.opened_urls[-1], "https://pending-okay.example")

    def test_add_document_root_followed_by_scan_runs_after_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, _action_handler, _adapter, _audit, executor, _ctx = self._build_services(root)

            extra_docs = root / "extra-docs"
            extra_docs.mkdir(parents=True, exist_ok=True)
            (extra_docs / "outside.txt").write_text("outside root", encoding="utf-8")

            pending = executor.execute(
                ActionRequest(
                    action="add_document_root",
                    arguments={"root": str(extra_docs)},
                    source="test",
                    reason="add and scan",
                    workflow_goal="make_directory_searchable",
                    workflow_parameters={"root": str(extra_docs)},
                    follow_up=ActionRequest(
                        action="scan_document_root",
                        arguments={"root": str(extra_docs)},
                        source="test-follow-up",
                        reason="scan after add",
                        workflow_goal="make_directory_searchable",
                        workflow_parameters={"root": str(extra_docs)},
                    ),
                )
            )
            self.assertEqual(pending.status, "pending_confirmation")
            self.assertEqual(executor._pending_action.validated_request.workflow_goal, "make_directory_searchable")
            self.assertEqual(executor._pending_action.validated_request.workflow_parameters, {"root": str(extra_docs)})

            confirmed = executor.confirm_pending()
            self.assertEqual(confirmed.status, "success")
            self.assertIn("Added document root", confirmed.message)
            self.assertIn("Scan complete", confirmed.message)
            expected_root = str(extra_docs.resolve()).lower()
            configured_roots = {str(path.resolve()).lower() for path in executor.context.allowed_roots}
            self.assertIn(expected_root, configured_roots)

            payload = json.loads(Path(executor.context.config_path).read_text(encoding="utf-8"))
            stored_roots = payload.get("document_search", {}).get("roots", [])
            self.assertTrue(any(isinstance(item, dict) and item.get("path", "").lower() == expected_root for item in stored_roots))

    def test_yes_reports_expired_confirmation_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, action_handler, adapter, _audit, executor, _ctx = self._build_services(root, confirmation_ttl_seconds=-1)

            pending = executor.execute(
                ActionRequest(
                    action="open_url",
                    arguments={"url": "https://expired-via-yes.example"},
                    source="test",
                    reason="expired via yes",
                )
            )
            self.assertEqual(pending.status, "pending_confirmation")

            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(action_handler.handle_natural_language("Yes"))
            self.assertIn("expired", output.getvalue().lower())
            self.assertEqual(adapter.opened_urls, [])

            self.assertFalse(action_handler.handle_natural_language("Yes"))

    def test_stale_search_context_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, search_handler, action_handler, _adapter, _audit, _executor, context = self._build_services(root)

            self.assertTrue(search_handler.handle("/search etrade", {}))
            context._created_at = datetime.now(timezone.utc) - timedelta(hours=2)

            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(action_handler.handle("/open 1", {}))
            self.assertIn("no recent search result", output.getvalue().lower())

    def test_unconfigured_https_requires_confirmation_and_confirm_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, action_handler, adapter, _audit, executor, _ctx = self._build_services(root)

            result = executor.execute(
                ActionRequest(
                    action="open_url",
                    arguments={"url": "https://untrusted.example/path", "configured": True},
                    source="test",
                    reason="direct url",
                )
            )
            self.assertEqual(result.status, "pending_confirmation")
            self.assertTrue(executor.has_pending_confirmation())
            self.assertEqual(adapter.opened_urls, [])

            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(action_handler.handle("/confirm", {}))
            self.assertIn("opened", output.getvalue().lower())
            self.assertEqual(adapter.opened_urls, ["https://untrusted.example/path"])

    def test_malformed_https_values_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, _action_handler, _adapter, _audit, executor, _ctx = self._build_services(root)

            malformed_urls = ["https:example.com", "https:///example", "https:"]
            for value in malformed_urls:
                result = executor.execute(
                    ActionRequest(
                        action="open_url",
                        arguments={"url": value},
                        source="test",
                        reason="malformed url",
                    )
                )
                self.assertEqual(result.status, "failed")
                self.assertIn("valid host", result.message.lower())

            credentialed = executor.execute(
                ActionRequest(
                    action="open_url",
                    arguments={"url": "https://user:pass@example.com"},
                    source="test",
                    reason="credentialed url",
                )
            )
            self.assertEqual(credentialed.status, "failed")
            self.assertIn("credentials", credentialed.message.lower())

    def test_second_pending_action_is_rejected_and_does_not_replace_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, action_handler, adapter, _audit, executor, _ctx = self._build_services(root)

            first = executor.execute(
                ActionRequest(
                    action="open_url",
                    arguments={"url": "https://first.example"},
                    source="test",
                    reason="first pending",
                )
            )
            self.assertEqual(first.status, "pending_confirmation")

            second = executor.execute(
                ActionRequest(
                    action="open_url",
                    arguments={"url": "https://second.example"},
                    source="test",
                    reason="second pending",
                )
            )
            self.assertEqual(second.status, "rejected")
            self.assertEqual(second.error, "confirmation_already_pending")

            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(action_handler.handle("/confirm", {}))
            self.assertEqual(adapter.opened_urls, ["https://first.example"])

    def test_cancel_clears_pending_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, action_handler, adapter, _audit, executor, _ctx = self._build_services(root)

            pending = executor.execute(
                ActionRequest(
                    action="open_url",
                    arguments={"url": "https://cancel.example"},
                    source="test",
                    reason="cancel pending",
                )
            )
            self.assertEqual(pending.status, "pending_confirmation")
            self.assertTrue(executor.has_pending_confirmation())

            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(action_handler.handle("/cancel", {}))
            self.assertIn("cancelled", output.getvalue().lower())
            self.assertFalse(executor.has_pending_confirmation())

            output2 = StringIO()
            with redirect_stdout(output2):
                self.assertTrue(action_handler.handle("/confirm", {}))
            self.assertIn("no pending action", output2.getvalue().lower())
            self.assertEqual(adapter.opened_urls, [])

    def test_configured_shortcut_does_not_require_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, _action_handler, adapter, _audit, executor, _ctx = self._build_services(root)

            result = executor.execute(
                ActionRequest(
                    action="open_url",
                    arguments={"shortcut": "bc-sandbox"},
                    source="test",
                    reason="shortcut",
                )
            )
            self.assertEqual(result.status, "success")
            self.assertFalse(executor.has_pending_confirmation())
            self.assertEqual(adapter.opened_urls[-1], "https://example.test/sandbox")

    def test_remove_document_root_persists_with_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, _action_handler, _adapter, _audit, executor, _ctx = self._build_services(root)

            existing_root = str(executor.context.allowed_roots[0])
            pending = executor.execute(
                ActionRequest(
                    action="update_config",
                    arguments={"operation": "remove_document_root", "root": existing_root},
                    source="test",
                    reason="remove root",
                )
            )
            self.assertEqual(pending.status, "pending_confirmation")

            confirmed = executor.confirm_pending()
            self.assertEqual(confirmed.status, "success")
            self.assertEqual(len(executor.context.allowed_roots), 0)

            payload = json.loads(Path(executor.context.config_path).read_text(encoding="utf-8"))
            self.assertEqual(payload.get("document_search", {}).get("roots", []), [])

    def test_remove_shortcut_and_application_persist_with_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, _action_handler, _adapter, _audit, executor, _ctx = self._build_services(root)

            pending_shortcut = executor.execute(
                ActionRequest(
                    action="update_config",
                    arguments={"operation": "remove_web_shortcut", "name": "bc-sandbox"},
                    source="test",
                    reason="remove shortcut",
                )
            )
            self.assertEqual(pending_shortcut.status, "pending_confirmation")
            confirmed_shortcut = executor.confirm_pending()
            self.assertEqual(confirmed_shortcut.status, "success")
            self.assertNotIn("bc-sandbox", executor.context.web_shortcuts)

            pending_app = executor.execute(
                ActionRequest(
                    action="update_config",
                    arguments={"operation": "remove_application", "app_id": "vscode"},
                    source="test",
                    reason="remove app",
                )
            )
            self.assertEqual(pending_app.status, "pending_confirmation")
            confirmed_app = executor.confirm_pending()
            self.assertEqual(confirmed_app.status, "success")
            self.assertNotIn("vscode", executor.context.applications)
            self.assertNotIn("vscode", executor.context.app_alias_map)

            payload = json.loads(Path(executor.context.config_path).read_text(encoding="utf-8"))
            self.assertNotIn("bc-sandbox", payload.get("web_shortcuts", {}))
            self.assertNotIn("vscode", payload.get("applications", {}))

    def test_expired_confirmation_cannot_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _target, _search_handler, action_handler, adapter, _audit, executor, _ctx = self._build_services(root, confirmation_ttl_seconds=-1)

            result = executor.execute(
                ActionRequest(
                    action="open_url",
                    arguments={"url": "https://expired.example"},
                    source="test",
                    reason="expired",
                )
            )
            self.assertEqual(result.status, "pending_confirmation")

            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(action_handler.handle("/confirm", {}))
            self.assertIn("expired", output.getvalue().lower())
            self.assertEqual(adapter.opened_urls, [])

    def test_show_deleted_file_fails_when_folder_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, search_handler, action_handler, adapter, _audit, _executor, _ctx = self._build_services(root)

            self.assertTrue(search_handler.handle("/search etrade", {}))
            target.unlink()

            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(action_handler.handle("/show 1", {}))
            self.assertIn("no longer exists", output.getvalue().lower())
            self.assertEqual(adapter.explorer_targets, [])

    def test_registry_rejects_duplicate_registration_without_replace(self) -> None:
        registry = ActionRegistry()
        registry.register(OpenFileAction())
        with self.assertRaises(ValueError):
            registry.register(OpenFileAction())
        registry.register(OpenFileAction(), replace=True)

    def test_actions_recent_skips_malformed_audit_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            audit = ActionAuditLogger(root / "audit")
            audit.log({"action": "open_file", "status": "success"})
            with audit.log_path.open("a", encoding="utf-8") as handle:
                handle.write("{bad json\n")
            audit.log({"action": "open_url", "status": "success"})

            entries = audit.read_recent(limit=10)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0].get("action"), "open_file")
            self.assertEqual(entries[1].get("action"), "open_url")


if __name__ == "__main__":
    unittest.main()

