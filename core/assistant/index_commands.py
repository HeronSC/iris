from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.assistant.prompting import PROMPT_CANCEL_TOKEN, PromptRequest, PromptType
from core.config.loader import ConfigLoader
from core.documents.catalog import DocumentCatalog
from core.documents.models import coerce_document_search_root, root_entry_path, root_entry_to_json
from core.documents.scanner import DocumentScanner


class IndexCommandHandler:
    def __init__(
        self,
        scanner: DocumentScanner,
        catalog: DocumentCatalog,
        config_path: Path | None = None,
        output: OutputSink | None = None,
        prompt_provider: Callable[[PromptRequest | str], str] | None = None,
    ) -> None:
        self.scanner = scanner
        self.catalog = catalog
        self.config_path = config_path
        self.output = output
        self.prompt_provider = prompt_provider
        self._progress_line_length = 0
        self._progress_last_update = 0.0
        self._active_progress_folder: str | None = None
        self._cancel_event: threading.Event | None = None
        self._root_decision_add = "add"
        self._root_decision_scan_once = "scan_once"
        self._root_decision_cancel = "cancel"

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        if not user_input.startswith("/index"):
            return False

        parts = user_input.strip().split()
        if len(parts) == 1:
            emit_output(self.output, "Usage: /index status|scan|scan <root>|errors")
            return True

        command = parts[1].lower()
        if command == "status":
            count = self.catalog.count_documents()
            last_scan = self.catalog.get_scan_state("last_scan_at") or "never"
            emit_output(self.output, f"Indexed documents: {count}")
            emit_output(self.output, f"Last scan: {last_scan}")
            return True

        if command == "scan":
            cancel_candidate = state.get("cancel_event")
            self._cancel_event = cancel_candidate if isinstance(cancel_candidate, threading.Event) else None
            root_filter = self._normalize_root_filter(" ".join(parts[2:]).strip()) if len(parts) > 2 else None
            if root_filter:
                selected_root = self._existing_directory(root_filter)
                if selected_root is not None and not self._is_configured_root(selected_root):
                    decision = self._confirm_add_root(selected_root)
                    if decision == self._root_decision_cancel:
                        emit_output(self.output, f"Scan cancelled: {selected_root}")
                        return True
                    if decision == self._root_decision_add:
                        if self._add_root_to_config(selected_root):
                            emit_output(self.output, f"Added root to config: {selected_root}")
                            self._reload_roots_from_config()
                        else:
                            emit_output(self.output, f"Could not persist root to config. Scanning folder anyway: {selected_root}")
                    else:
                        emit_output(self.output, f"Scanning without adding root to config: {selected_root}")
            try:
                command_label = user_input.strip()
                self._active_progress_folder = None
                emit_output(self.output, f"{command_label} is running...", role="progress")
                result = self.scanner.scan(
                    root_filter=root_filter if root_filter else None,
                    progress_callback=lambda current_root: self._report_scan_progress(command_label, current_root),
                )
            except ValueError as error:
                self._clear_progress_line()
                self._active_progress_folder = None
                emit_output(self.output, str(error))
                return True
            self._clear_progress_line()
            self._active_progress_folder = None
            emit_output(
                self.output,
                "Scan complete: "
                f"scanned={result.scanned_files}, indexed={result.indexed_files}, "
                f"updated={result.updated_files}, deleted={result.deleted_files}, errors={result.error_files}"
            )
            return True

        if command == "errors":
            errors = self.catalog.list_scan_errors(limit=20)
            if not errors:
                emit_output(self.output, "No scan errors recorded.")
                return True
            for item in errors:
                emit_output(self.output, f"- {item['created_at']} | {item['path']} | {item['error']}")
            return True

        emit_output(self.output, "Unknown /index command.")
        return True

    def _existing_directory(self, root_filter: str) -> Path | None:
        candidate = Path(root_filter).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved.exists() and resolved.is_dir():
            return resolved
        return None

    def _normalize_root_filter(self, root_filter: str | None) -> str | None:
        if root_filter is None:
            return None
        normalized = root_filter.strip()
        if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
            normalized = normalized[1:-1].strip()
        return normalized or None

    def _is_configured_root(self, root: Path) -> bool:
        root_value = str(root).lower()
        for configured_root in self.scanner.config.roots:
            configured_path = coerce_document_search_root(configured_root).path
            try:
                configured_resolved = configured_path.resolve()
            except OSError:
                configured_resolved = configured_path
            if str(configured_resolved).lower() == root_value:
                return True
        return False

    def _confirm_add_root(self, root: Path) -> str:
        provider = self.prompt_provider or input
        if provider is None:
            return self._root_decision_scan_once
        prompt = PromptRequest(
            prompt_id="add-document-root",
            prompt_type=PromptType.YES_NO_CANCEL,
            text=f"Root is not in document_search.roots in {self.config_path.name if self.config_path else 'config.json'}. Add it there and scan '{root}'?",
            choices=("yes", "no", "cancel"),
        )
        try:
            answer = provider(prompt).strip().lower()
        except TypeError:
            answer = provider(prompt.text).strip().lower()
        if answer in {PROMPT_CANCEL_TOKEN, "cancel"}:
            return self._root_decision_cancel
        if answer in {"y", "yes", "confirm", "go ahead", "proceed", "do it"}:
            return self._root_decision_add
        return self._root_decision_scan_once

    def _add_root_to_config(self, root: Path) -> bool:
        if self.config_path is None:
            return False

        try:
            with self.config_path.open("r", encoding="utf-8-sig") as handle:
                config = json.load(handle)
        except Exception:
            return False

        if not isinstance(config, dict):
            return False

        document_search = config.get("document_search", {})
        if not isinstance(document_search, dict):
            document_search = {}

        roots = document_search.get("roots", [])
        if not isinstance(roots, list):
            roots = []

        normalized = str(root)
        known_roots = {
            str(Path(path_text).expanduser()).lower()
            for item in roots
            for path_text in [root_entry_path(item)]
            if path_text is not None
        }
        if normalized.lower() not in known_roots:
            roots.append(root_entry_to_json(normalized))
            document_search["roots"] = roots
            config["document_search"] = document_search
            try:
                with self.config_path.open("w", encoding="utf-8") as handle:
                    json.dump(config, handle, indent=2)
                    handle.write("\n")
            except Exception:
                return False

        self.scanner.config.roots.append(root)
        return True

    def _reload_roots_from_config(self) -> None:
        if self.config_path is None:
            return

        try:
            loaded = ConfigLoader(self.config_path).load()
        except Exception:
            return

        document_search = loaded.get("document_search", {}) if isinstance(loaded, dict) else {}
        roots = document_search.get("roots", []) if isinstance(document_search, dict) else []
        if isinstance(roots, list):
            self.scanner.config.roots[:] = roots

    def _report_scan_progress(self, command_label: str, current_root: str) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise ValueError("Scan cancelled.")

        if current_root.startswith("ROOT:"):
            if self._active_progress_folder is not None:
                message = f"{command_label} is running... finished folder: {self._active_progress_folder}"
                self._write_progress_line(message)
                self._active_progress_folder = None
            root_path = current_root[len("ROOT:") :]
            message = f"{command_label} is running... scanning root: {root_path}"
            self._write_progress_line(message)
            self._progress_last_update = monotonic()
            return

        if current_root.startswith("ROOT_DONE:"):
            if self._active_progress_folder is not None:
                message = f"{command_label} is running... finished folder: {self._active_progress_folder}"
                self._write_progress_line(message)
                self._active_progress_folder = None
            root_path = current_root[len("ROOT_DONE:") :]
            message = f"{command_label} is running... completed root: {root_path}"
            self._write_progress_line(message)
            self._progress_last_update = monotonic()
            return

        if current_root == "CLEANUP_START":
            message = f"{command_label} is running... reconciling index (cleanup)"
            self._write_progress_line(message)
            self._progress_last_update = monotonic()
            return

        if current_root.startswith("CLEANUP_PATHS:"):
            parts = current_root.split(":")
            if len(parts) == 3:
                processed = parts[1]
                total = parts[2]
                message = f"{command_label} is running... cleanup catalog scan: {processed}/{total}"
                self._write_progress_line(message)
                self._progress_last_update = monotonic()
                return

        if current_root.startswith("CLEANUP_CATALOG_DONE:"):
            path_count = current_root[len("CLEANUP_CATALOG_DONE:") :]
            message = f"{command_label} is running... cleanup catalog scan complete: {path_count} tracked paths"
            self._write_progress_line(message)
            self._progress_last_update = monotonic()
            return

        if current_root == "CLEANUP_DIFF_START":
            message = f"{command_label} is running... cleanup diffing indexed paths"
            self._write_progress_line(message)
            self._progress_last_update = monotonic()
            return

        if current_root.startswith("CLEANUP_DELETE_TOTAL:"):
            total = current_root[len("CLEANUP_DELETE_TOTAL:") :]
            if total == "0":
                message = f"{command_label} is running... cleanup found no stale entries"
            else:
                message = f"{command_label} is running... cleanup deleting stale entries: 0/{total}"
            self._write_progress_line(message)
            self._progress_last_update = monotonic()
            return

        if current_root.startswith("CLEANUP_DELETE:"):
            parts = current_root.split(":")
            if len(parts) == 3:
                processed = parts[1]
                total = parts[2]
                message = f"{command_label} is running... cleanup deleting stale entries: {processed}/{total}"
                self._write_progress_line(message)
                self._progress_last_update = monotonic()
                return

        if current_root == "CLEANUP_DONE":
            message = f"{command_label} is running... cleanup complete"
            self._write_progress_line(message)
            self._progress_last_update = monotonic()
            return

        if current_root != self._active_progress_folder:
            if self._active_progress_folder is not None:
                message = f"{command_label} is running... finished folder: {self._active_progress_folder}"
                self._write_progress_line(message)
            self._active_progress_folder = current_root
            message = f"{command_label} is running... started folder: {current_root}"
            self._write_progress_line(message)
            self._progress_last_update = monotonic()

    def _write_progress_line(self, message: str) -> None:
        if self.output is not None:
            emit_output(self.output, message, role="progress")
            return
        padded = message
        if self._progress_line_length > len(message):
            padded = message + (" " * (self._progress_line_length - len(message)))
        self._progress_line_length = len(message)
        print(f"\r{padded}", end="", flush=True)

    def _clear_progress_line(self) -> None:
        if self._progress_line_length == 0:
            return
        print("\r" + (" " * self._progress_line_length) + "\r", end="", flush=True)
        self._progress_line_length = 0
        self._progress_last_update = 0.0

