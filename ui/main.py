from __future__ import annotations

import argparse
import os
import re
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

workspace_root = Path(__file__).resolve().parents[1]
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

from core.application import IrisApplication, IrisEvent, IrisStatus, MessageRole
from core.application.contracts import ActionSuggestion, ConversationContent, DetailContent, TopicContext
from core.assistant.prompting import PROMPT_CANCEL_TOKEN, PromptRequest, PromptType
from core.config.loader import ConfigError
from core.profile.loader import AssistantMemoryError


class ChatInput(QTextEdit):
    submitRequested = Signal()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.insertPlainText("\n")
            event.accept()
            return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            event.accept()
            self.submitRequested.emit()
            return
        super().keyPressEvent(event)


class ProcessThread(QThread):
    eventSignal = Signal(object)
    finishedSignal = Signal(object)
    failedSignal = Signal(str)
    promptSignal = Signal(object)

    def __init__(self, app_service: IrisApplication, text: str) -> None:
        super().__init__()
        self.app_service = app_service
        self.text = text
        self.cancel_event = threading.Event()
        self._prompt_wait = threading.Event()
        self._prompt_response = ""

    def run(self) -> None:
        response = None
        try:
            def _handler(event: IrisEvent) -> None:
                self.eventSignal.emit(event)

            def _prompt(prompt_text: PromptRequest | str) -> str:
                self._prompt_response = ""
                self._prompt_wait.clear()
                self.promptSignal.emit(prompt_text)
                while not self._prompt_wait.wait(timeout=0.1):
                    if self.cancel_event.is_set():
                        return PROMPT_CANCEL_TOKEN
                return self._prompt_response

            response = self.app_service.process_message(
                self.text,
                cancel_event=self.cancel_event,
                event_handler=_handler,
                prompt_provider=_prompt,
            )
        except (RuntimeError, OSError, ValueError, AssertionError) as error:
            self.failedSignal.emit(str(error))
            return
        self.finishedSignal.emit(response)

    def provide_prompt_response(self, response_text: str) -> None:
        self._prompt_response = response_text
        self._prompt_wait.set()

    def cancel_prompt(self) -> None:
        self._prompt_response = PROMPT_CANCEL_TOKEN
        self._prompt_wait.set()


class InitializeThread(QThread):
    initializedSignal = Signal()
    failedSignal = Signal(str)

    def __init__(self, app_service: IrisApplication) -> None:
        super().__init__()
        self.app_service = app_service

    def run(self) -> None:
        try:
            self.app_service.initialize()
        except (ConfigError, AssistantMemoryError) as error:
            self.failedSignal.emit(str(error))
            return
        except (RuntimeError, OSError, ValueError, AssertionError) as error:
            self.failedSignal.emit(f"Unexpected startup error: {error}")
            return
        self.initializedSignal.emit()


class FileOperationsPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode = "idle"
        self._path_nodes: dict[str, QTreeWidgetItem] = {}

        self.summary_label = QLabel("No file operations yet.")
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Item", "Type", "Info"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        layout = QVBoxLayout()
        layout.addWidget(self.summary_label)
        layout.addWidget(self.tree, 1)
        self.setLayout(layout)

    def show_search_results(self, payload: dict[str, object]) -> None:
        query = str(payload.get("query", "")).strip()
        total_matches = int(payload.get("total_matches", 0) or 0)
        searched_roots = payload.get("searched_roots", [])
        failed_roots = payload.get("failed_roots", [])
        matches = payload.get("matches", [])

        searched_count = len(searched_roots) if isinstance(searched_roots, list) else 0
        failed_count = len(failed_roots) if isinstance(failed_roots, list) else 0
        summary = f"Query: {query or '(unknown)'} | Matches: {total_matches} | Searched roots: {searched_count}"
        if failed_count > 0:
            summary = summary + f" | Failed roots: {failed_count}"

        self._mode = "search"
        self._path_nodes = {}
        self.tree.clear()
        self.summary_label.setText(summary)

        if isinstance(matches, list):
            for entry in matches:
                if not isinstance(entry, dict):
                    continue
                path = str(entry.get("path", "")).strip()
                if not path:
                    continue
                classification = str(entry.get("classification", "partial")).strip() or "partial"
                self._add_search_path(path, classification)

        self.tree.collapseAll()

    def update_index_progress(self, text: str) -> None:
        message = text.strip()
        if not message:
            return
        if self._mode != "index":
            self._mode = "index"
            self._path_nodes = {}
            self.tree.clear()
        self.summary_label.setText(message)

        root_match = re.search(r"scanning root:\s*(.+)$", message, re.IGNORECASE)
        if root_match is not None:
            root_path = root_match.group(1).strip()
            node = self._ensure_folder_node(root_path)
            node.setText(2, "Scanning root")
            self.tree.collapseAll()
            return

        start_folder_match = re.search(r"started folder:\s*(.+)$", message, re.IGNORECASE)
        if start_folder_match is not None:
            folder_path = start_folder_match.group(1).strip()
            node = self._ensure_folder_node(folder_path)
            node.setText(2, "Scanning")
            self.tree.collapseAll()
            return

        finish_folder_match = re.search(r"finished folder:\s*(.+)$", message, re.IGNORECASE)
        if finish_folder_match is not None:
            folder_path = finish_folder_match.group(1).strip()
            node = self._ensure_folder_node(folder_path)
            node.setText(2, "Done")
            return

        complete_root_match = re.search(r"completed root:\s*(.+)$", message, re.IGNORECASE)
        if complete_root_match is not None:
            root_path = complete_root_match.group(1).strip()
            node = self._ensure_folder_node(root_path)
            node.setText(2, "Root complete")
            return

        if "cleanup" in message.lower():
            cleanup_root = self._ensure_folder_node("[cleanup]")
            cleanup_root.setText(2, message)

    def _add_search_path(self, path: str, classification: str) -> None:
        normalized = path.replace("\\", "/").strip()
        if not normalized:
            return
        path_obj = Path(normalized)
        folder = str(path_obj.parent).replace("\\", "/")
        file_name = path_obj.name or normalized
        parent = self._ensure_folder_node(folder)
        item = QTreeWidgetItem([file_name, "File", classification])
        item.setToolTip(0, normalized)
        item.setToolTip(2, classification)
        parent.addChild(item)
        current = parent
        while current is not None:
            count_value = current.data(2, Qt.ItemDataRole.UserRole)
            count = int(count_value) if isinstance(count_value, int) else 0
            count = count + 1
            current.setData(2, Qt.ItemDataRole.UserRole, count)
            label = "match" if count == 1 else "matches"
            current.setText(2, f"{count} {label}")
            current = current.parent()

    def _ensure_folder_node(self, path: str) -> QTreeWidgetItem:
        normalized = path.replace("\\", "/").strip()
        if not normalized:
            normalized = "."
        segments = [segment for segment in normalized.split("/") if segment]
        if not segments:
            segments = ["."]
        parent_item: QTreeWidgetItem | None = None
        cumulative = ""
        for segment in segments:
            cumulative = f"{cumulative}/{segment}" if cumulative else segment
            key = cumulative.lower()
            node = self._path_nodes.get(key)
            if node is None:
                node = QTreeWidgetItem([segment, "Folder", ""])
                node.setToolTip(0, cumulative)
                if parent_item is None:
                    self.tree.addTopLevelItem(node)
                else:
                    parent_item.addChild(node)
                self._path_nodes[key] = node
            parent_item = node
        return parent_item


class IrisWindow(QMainWindow):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.setWindowTitle("Iris")

        self.settings = QSettings("Iris", "IrisUI")
        self.app_service = IrisApplication(config_path, prompt_provider=None)
        self.config_path = config_path
        self.init_worker: InitializeThread | None = None
        self.worker: ProcessThread | None = None
        self.pending_text: str | None = None
        self.engine_available = False
        self._pending_close_request = False
        self._close_requested = False
        self._current_response = None
        self._start_maximized = True
        self._topic_workspaces: dict[str, dict[str, object]] = {}
        self._topic_order: list[str] = []
        self._active_topic_id: str | None = None
        self._live_detail_lines: list[str] = []
        self._live_details_active = False
        self._active_input_text = ""

        self.status_label = QLabel("Starting")
        self.conversation_view = QTextBrowser()
        self.conversation_view.setOpenExternalLinks(False)
        self.conversation_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.history = self.conversation_view

        self.details_title = QLabel("Details")
        self.details_view = QTextBrowser()
        self.details_view.setOpenExternalLinks(False)
        self.details_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.details_actions_row = QWidget()
        self.details_actions_layout = QVBoxLayout()
        self.details_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.details_actions_row.setLayout(self.details_actions_layout)
        self.approve_button = QPushButton("Approve")
        self.approve_button.clicked.connect(self._approve_pending_action)
        self.cancel_confirmation_button = QPushButton("Cancel")
        self.cancel_confirmation_button.clicked.connect(self._cancel_pending_action)
        self.details_toggle = QPushButton("Hide Details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setChecked(True)
        self.details_toggle.toggled.connect(self._set_details_visible)

        self.input_box = ChatInput()
        self.input_box.setPlaceholderText("Type a message...")
        self.input_box.submitRequested.connect(self.send_message)

        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self.send_message)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_request)
        self.stop_button.setEnabled(False)

        header = QHBoxLayout()
        header.addWidget(QLabel("Iris"))
        header.addStretch()
        header.addWidget(self.details_toggle)
        header.addWidget(self.status_label)

        left_panel = QWidget()
        left_layout = QVBoxLayout()
        left_layout.addWidget(self.conversation_view, 1)
        left_panel.setLayout(left_layout)

        right_panel = QWidget()
        right_layout = QVBoxLayout()
        right_layout.addWidget(self.details_title)
        self.file_ops_panel = FileOperationsPanel(self)
        self.details_stack = QStackedWidget()
        self.details_stack.addWidget(self.details_view)
        self.details_stack.addWidget(self.file_ops_panel)
        right_layout.addWidget(self.details_stack, 1)
        right_layout.addWidget(self.details_actions_row)
        right_layout.addWidget(self.approve_button)
        right_layout.addWidget(self.cancel_confirmation_button)
        right_panel.setLayout(right_layout)

        self.details_view.setMinimumWidth(280)
        self.details_panel = right_panel
        self.details_action_buttons: list[QPushButton] = []

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left_panel)
        self.splitter.addWidget(right_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)

        composer = QHBoxLayout()
        composer.addWidget(self.input_box, 1)

        buttons = QVBoxLayout()
        buttons.addWidget(self.send_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch()
        composer.addLayout(buttons)

        body = QVBoxLayout()
        body.addLayout(header)
        body.addWidget(self.splitter, 1)
        body.addLayout(composer)

        container = QWidget()
        container.setLayout(body)
        self.setCentralWidget(container)

        self._restore_window_state()
        self._set_controls_enabled(False)
        self._set_confirmation_controls_visible(False)
        self._start_engine_initialization()

    def _start_engine_initialization(self) -> None:
        if not self.config_path.exists():
            QMessageBox.critical(self, "Startup Error", f"Configuration file not found: {self.config_path}")
            self.set_status("Error")
            self._disable_controls_after_startup_failure()
            return

        self.init_worker = InitializeThread(self.app_service)
        self.init_worker.initializedSignal.connect(self._on_initialize_finished)
        self.init_worker.failedSignal.connect(self._on_initialize_failed)
        self.init_worker.finished.connect(self._on_initialize_thread_stopped)
        self.init_worker.start()

    def _on_initialize_finished(self) -> None:
        for message in self.app_service.startup_messages:
            self._append_message(message.role, message.text)
        self.engine_available = True
        self.set_status("Ready")
        self._set_controls_enabled(True)
        QTimer.singleShot(0, self._focus_input_box)

    def _focus_input_box(self) -> None:
        if self.input_box.isEnabled():
            self.input_box.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _on_initialize_failed(self, error_text: str) -> None:
        QMessageBox.critical(self, "Startup Error", error_text)
        self.set_status("Error")
        self._disable_controls_after_startup_failure()

    def _on_initialize_thread_stopped(self) -> None:
        worker = self.init_worker
        if worker is not None:
            worker.deleteLater()
        self.init_worker = None
        if self._close_requested and self.worker is None:
            self._close_requested = False
            QTimer.singleShot(0, self.close)

    def _disable_controls_after_startup_failure(self) -> None:
        self.engine_available = False
        self._set_controls_enabled(False)
        self.stop_button.setEnabled(False)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.input_box.setEnabled(enabled)
        self.send_button.setEnabled(enabled)

    def _handle_event(self, event: IrisEvent) -> None:
        status_text = event.status.value.replace("_", " ").title()
        if event.progress_current is not None and event.progress_total is not None:
            status_text = f"{status_text} {event.progress_current} / {event.progress_total}"
        self.set_status(status_text)
        if event.message is not None:
            if event.message.role == MessageRole.ASSISTANT:
                return
            self._append_live_detail_event(event.message.role, event.message.text)
            if event.message.role == MessageRole.PROGRESS:
                return
            self._append_message(event.message.role, event.message.text)

    def send_message(self) -> None:
        if not self.engine_available:
            return
        if self.worker is not None and self.worker.isRunning():
            return
        text = self.input_box.toPlainText().strip()
        if not text:
            return

        self._append_message(MessageRole.USER, text)
        assistant_name = self.app_service.config.get("assistant_name", "Iris") if isinstance(self.app_service.config, dict) else "Iris"
        self._append_message(MessageRole.ASSISTANT, "Thinking...", label=assistant_name)
        self.pending_text = text
        self._active_input_text = text
        self.input_box.clear()
        self.set_status("Thinking")
        self.send_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.input_box.setEnabled(False)

        self._live_detail_lines = []
        self._live_details_active = text.lstrip().startswith("/")
        if self._live_details_active:
            self.details_title.setText("Live command details")
            self.details_view.setPlainText("Waiting for updates...")

        self.worker = ProcessThread(self.app_service, text)
        self.worker.eventSignal.connect(self._on_worker_event)
        self.worker.finishedSignal.connect(self._on_worker_finished)
        self.worker.failedSignal.connect(self._on_worker_failed)
        self.worker.promptSignal.connect(self._on_worker_prompt)
        self.worker.finished.connect(self._on_worker_thread_stopped)
        self.worker.start()

    def stop_request(self) -> None:
        if self.worker is None:
            return
        self.worker.cancel_event.set()
        self.worker.cancel_prompt()
        self.stop_button.setEnabled(False)
        self.set_status("Stopping")

    def _on_worker_event(self, event: IrisEvent) -> None:
        self._handle_event(event)

    def _on_worker_finished(self, response) -> None:
        self._live_details_active = False
        self._active_input_text = ""
        self._current_response = response
        self._render_response(response)
        status_text = response.status.value.replace("_", " ").title()
        self.set_status(status_text)
        self.pending_text = None
        if bool(self._response_metadata(response).get("close_application", False)):
            self._pending_close_request = True
        if response.status in {IrisStatus.COMPLETE, IrisStatus.CANCELLED}:
            QTimer.singleShot(1200, self._set_ready_if_idle)

    def _on_worker_failed(self, error_text: str) -> None:
        self._live_details_active = False
        self._active_input_text = ""
        self._append_message(MessageRole.ERROR, error_text)
        self.set_status("Error")
        if self.pending_text is not None and not self.input_box.toPlainText().strip():
            self.input_box.setPlainText(self.pending_text)
        self.pending_text = None

    def _on_worker_prompt(self, prompt_request: PromptRequest | str) -> None:
        if self.worker is None:
            return

        request = prompt_request if isinstance(prompt_request, PromptRequest) else PromptRequest(
            prompt_id="legacy-prompt",
            prompt_type=PromptType.TEXT,
            text=prompt_request,
        )

        if request.prompt_type == PromptType.YES_NO_CANCEL:
            button = QMessageBox.question(
                self,
                "Iris Prompt",
                request.text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.No,
            )
            if button == QMessageBox.StandardButton.Yes:
                response_text = "yes"
            elif button == QMessageBox.StandardButton.No:
                response_text = "no"
            else:
                response_text = PROMPT_CANCEL_TOKEN
        else:
            dialog = QInputDialog(self)
            dialog.setInputMode(QInputDialog.InputMode.TextInput)
            dialog.setWindowTitle("Iris Prompt")
            dialog.setLabelText(request.text)
            if request.sensitive:
                dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            if not accepted:
                response_text = PROMPT_CANCEL_TOKEN
            else:
                response_text = dialog.textValue()
        self.worker.provide_prompt_response(response_text)

    def _on_worker_thread_stopped(self) -> None:
        worker = self.worker
        if worker is not None:
            worker.deleteLater()
        self._reset_input_state()
        if self._pending_close_request:
            self._pending_close_request = False
            self.close()
            return
        if self._close_requested and self.init_worker is None:
            self._close_requested = False
            self.close()

    def _reset_input_state(self) -> None:
        self._set_controls_enabled(self.engine_available)
        self.stop_button.setEnabled(False)
        self.input_box.setFocus()
        self.worker = None

    def _set_ready_if_idle(self) -> None:
        if self.worker is None and self.engine_available:
            self.set_status("Ready")

    def _append_message(self, role: MessageRole, text: str, label: str | None = None) -> None:
        role_label = label or {
            MessageRole.USER: "You",
            MessageRole.ASSISTANT: "Iris",
            MessageRole.SYSTEM: "System",
            MessageRole.PROGRESS: "Progress",
            MessageRole.ERROR: "Error",
            MessageRole.CONFIRMATION: "Confirmation",
        }[role]
        self.history.append(f"{role_label}\n{text}\n")
        cursor = self.history.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.history.setTextCursor(cursor)

    def _append_live_detail_event(self, role: MessageRole, text: str) -> None:
        if not self._live_details_active:
            return
        role_label = {
            MessageRole.USER: "You",
            MessageRole.ASSISTANT: "Iris",
            MessageRole.SYSTEM: "System",
            MessageRole.PROGRESS: "Progress",
            MessageRole.ERROR: "Error",
            MessageRole.CONFIRMATION: "Confirmation",
        }[role]
        entry = f"{role_label}: {text.strip()}" if text.strip() else role_label
        self._live_detail_lines.append(entry)
        if len(self._live_detail_lines) > 2000:
            self._live_detail_lines = self._live_detail_lines[-2000:]
        self.details_view.setPlainText("\n".join(self._live_detail_lines))
        cursor = self.details_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.details_view.setTextCursor(cursor)
        if role == MessageRole.PROGRESS and self._active_input_text.lower().startswith("/index"):
            self.file_ops_panel.update_index_progress(text)
            self._show_file_operations_panel("Indexing")

    def _render_response(self, response) -> None:
        assistant_name = self.app_service.config.get("assistant_name", "Iris") if isinstance(self.app_service.config, dict) else "Iris"
        conversation = self._response_conversation(response)
        if conversation is not None and conversation.message.strip():
            self._append_message(MessageRole.ASSISTANT, conversation.message.strip(), label=assistant_name)
        else:
            for message in response.messages:
                if message.role == MessageRole.USER:
                    continue
                if message.role == MessageRole.ASSISTANT:
                    self._append_message(MessageRole.ASSISTANT, message.text, label=assistant_name)
                else:
                    self._append_message(message.role, message.text)

        self._append_details_section(response)
        self._set_confirmation_controls_visible(response.status == IrisStatus.AWAITING_CONFIRMATION)

    def _append_details_section(self, response) -> None:
        details = self._response_details(response)
        topic = self._response_topic(response)
        topic_id, topic_title, relationship = self._unpack_topic(topic)
        if not topic_id:
            topic_id = self._active_topic_id or "topic-default"
        if relationship == "new_topic" or self._active_topic_id is None:
            self._active_topic_id = topic_id
            if topic_id not in self._topic_workspaces:
                self._topic_workspaces[topic_id] = {
                    "title": topic_title or "Details",
                    "sections": [],
                    "explore_actions": [],
                    "explore_seen": set(),
                }
                self._topic_order.append(topic_id)
            else:
                self._topic_workspaces[topic_id]["title"] = topic_title or self._topic_workspaces[topic_id].get("title") or "Details"
        else:
            if topic_id not in self._topic_workspaces:
                self._topic_workspaces[topic_id] = {
                    "title": topic_title or "Details",
                    "sections": [],
                    "explore_actions": [],
                    "explore_seen": set(),
                }
                self._topic_order.append(topic_id)
            self._active_topic_id = topic_id

        if self._active_topic_id is None:
            self._active_topic_id = topic_id

        section_html = self._build_details_section_html(details)
        detail_type, section_id, _title, _content, _summary, detail_items, _actions, metadata = self._unpack_detail(details)
        render_operation = "append"
        if isinstance(metadata, dict):
            render_operation = str(metadata.get("render_operation", render_operation)).strip() or "append"
        if detail_type == "topic_state" and render_operation == "append":
            render_operation = "replace_workspace"
        resolved_section_id = str(section_id).strip() if section_id else ""
        if not resolved_section_id:
            resolved_section_id = f"generated-{len(self._topic_workspaces)}-{len(self._topic_order)}"

        if section_html:
            workspace = self._topic_workspaces.setdefault(
                self._active_topic_id,
                {"title": topic_title or "Details", "sections": [], "explore_actions": [], "explore_seen": set()},
            )
            sections = workspace.setdefault("sections", [])
            if isinstance(sections, list):
                normalized_sections = self._normalize_sections(sections)
                if render_operation == "replace_workspace":
                    normalized_sections = [{"id": resolved_section_id, "html": section_html}]
                elif render_operation == "replace_section":
                    replaced = False
                    for entry in normalized_sections:
                        if str(entry.get("id", "")).strip() == resolved_section_id:
                            entry["html"] = section_html
                            replaced = True
                            break
                    if not replaced:
                        normalized_sections.append({"id": resolved_section_id, "html": section_html})
                elif render_operation == "remove_section":
                    normalized_sections = [entry for entry in normalized_sections if str(entry.get("id", "")).strip() != resolved_section_id]
                elif render_operation == "no_change":
                    pass
                else:
                    normalized_sections.append({"id": resolved_section_id, "html": section_html})
                workspace["sections"] = normalized_sections
            if topic_title:
                workspace["title"] = topic_title

        self._accumulate_explore_actions(self._active_topic_id, details)

        active_workspace = self._topic_workspaces.get(
            self._active_topic_id,
            {"title": "Details", "sections": [], "explore_actions": [], "explore_seen": set()},
        )
        sections = active_workspace.get("sections", [])
        rendered_sections = self._normalize_sections(sections if isinstance(sections, list) else [])
        self.details_title.setText(str(active_workspace.get("title") or "Details"))
        if rendered_sections:
            container_html = "<hr>".join(str(block.get("html", "")) for block in rendered_sections if str(block.get("html", "")).strip())
            self.details_view.setHtml(container_html)
            cursor = self.details_view.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.details_view.setTextCursor(cursor)
        else:
            self.details_view.setHtml("<i>No details available.</i>")
        self.details_stack.setCurrentWidget(self.details_view)
        self._render_explore_actions_for_topic(self._active_topic_id)
        self._sync_file_operations_panel(detail_type, detail_items, metadata)

    def _normalize_sections(self, sections: list[object]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for index, entry in enumerate(sections):
            if isinstance(entry, dict):
                section_id = str(entry.get("id", "")).strip() or f"section-{index + 1}"
                html = str(entry.get("html", ""))
                normalized.append({"id": section_id, "html": html})
                continue
            normalized.append({"id": f"section-{index + 1}", "html": str(entry)})
        return normalized

    def _build_details_section_html(self, details) -> str:
        if details is None:
            return ""

        detail_type, _section_id, title, content, summary, items, _actions, metadata = self._unpack_detail(details)
        body = str(content or summary or "").strip()
        display_title = str(title or "Details").strip()

        if detail_type == "topic_state":
            return self._render_topic_state_section(display_title, items)

        if detail_type == "markdown" and body:
            document = QTextDocument()
            document.setMarkdown(body)
            rendered_body = document.toHtml()
            rendered_body = re.sub(r"^.*<body[^>]*>", "", rendered_body, flags=re.DOTALL)
            rendered_body = re.sub(r"</body>.*$", "", rendered_body, flags=re.DOTALL)
            return f"<section><h3>{self._escape_html(display_title)}</h3>{rendered_body}</section>"

        parts: list[str] = []
        parts.append(f"<h3>{self._escape_html(display_title)}</h3>")
        if body:
            parts.append(f"<p>{self._escape_html(body)}</p>")
        if detail_type in {"text", "markdown"}:
            return "".join(parts) if body else "<i>No details available.</i>"
        if detail_type in {"file_results", "file_list", "search_results"}:
            parts.append(self._render_result_cards(items))
        elif items:
            parts.append("<ul>")
            for item in items:
                parts.append(f"<li>{self._escape_html(self._format_detail_item(item))}</li>")
            parts.append("</ul>")
        if metadata:
            parts.append("<hr>")
            parts.append("<pre>")
            parts.append(self._escape_html(self._format_metadata(metadata)))
            parts.append("</pre>")
        return "".join(parts) if parts else "<i>No details available.</i>"

    def _render_topic_state_section(self, title: str, items) -> str:
        if not isinstance(items, list) or not items:
            return "<i>No details available.</i>"
        payload = items[0] if isinstance(items[0], dict) else {}
        if not isinstance(payload, dict):
            payload = {}

        lines: list[str] = []
        lines.append(f"<h3>{self._escape_html(title)}</h3>")

        goal = str(payload.get("goal", "")).strip()
        if goal:
            lines.append("<h4>Goal</h4>")
            lines.append(f"<p>{self._escape_html(goal)}</p>")

        requirements = payload.get("requirements", []) if isinstance(payload.get("requirements"), list) else []
        if requirements:
            lines.append("<h4>Requirements</h4><ul>")
            for entry in requirements:
                if isinstance(entry, dict):
                    label = str(entry.get("label", "")).strip()
                else:
                    label = str(entry).strip()
                if label:
                    lines.append(f"<li>{self._escape_html(label)}</li>")
            lines.append("</ul>")

        active_items = payload.get("items", []) if isinstance(payload.get("items"), list) else []
        if active_items:
            lines.append("<h4>Active candidates</h4>")
            lines.append(self._render_topic_items(active_items))

        decisions = payload.get("decisions", []) if isinstance(payload.get("decisions"), list) else []
        if decisions:
            lines.append("<h4>Decisions</h4><ul>")
            for entry in decisions:
                value = str(entry).strip()
                if value:
                    lines.append(f"<li>{self._escape_html(value)}</li>")
            lines.append("</ul>")

        open_questions = payload.get("open_questions", []) if isinstance(payload.get("open_questions"), list) else []
        unresolved = []
        for entry in open_questions:
            if isinstance(entry, dict):
                if str(entry.get("status", "open")).strip().lower() == "resolved":
                    continue
                text = str(entry.get("text", "")).strip()
            else:
                text = str(entry).strip()
            if text:
                unresolved.append(text)
        if unresolved:
            lines.append("<h4>Open questions</h4><ul>")
            for entry in unresolved:
                lines.append(f"<li>{self._escape_html(entry)}</li>")
            lines.append("</ul>")

        notes = payload.get("notes", []) if isinstance(payload.get("notes"), list) else []
        if notes:
            lines.append("<h4>Notes</h4><ul>")
            for entry in notes:
                value = str(entry).strip()
                if value:
                    lines.append(f"<li>{self._escape_html(value)}</li>")
            lines.append("</ul>")

        return "".join(lines) if lines else "<i>No details available.</i>"

    def _render_topic_items(self, items: list[object]) -> str:
        blocks: list[str] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip() or "Item"
            blocks.append("<div style='margin-bottom: 0.75em; padding: 0.5em; border: 1px solid #ccc; border-radius: 6px;'>")
            blocks.append(f"<b>{self._escape_html(name)}</b><br>")
            attributes = entry.get("attributes", {}) if isinstance(entry.get("attributes"), dict) else {}
            for key in sorted(attributes.keys()):
                value = attributes.get(key)
                if value is None or str(value).strip() == "":
                    continue
                blocks.append(f"{self._escape_html(str(key).replace('_', ' ').title())}: {self._escape_html(str(value))}<br>")
            facts = entry.get("facts", {}) if isinstance(entry.get("facts"), dict) else {}
            details = facts.get("details", []) if isinstance(facts.get("details"), list) else []
            for detail in details[:3]:
                text = str(detail).strip()
                if text:
                    blocks.append(f"{self._escape_html(text)}<br>")
            blocks.append("</div>")
        if not blocks:
            return "<i>No active candidates.</i>"
        return "".join(blocks)

    def _accumulate_explore_actions(self, topic_id: str | None, details) -> None:
        if not topic_id:
            return
        workspace = self._topic_workspaces.setdefault(
            topic_id,
            {"title": "Details", "sections": [], "explore_actions": [], "explore_seen": set()},
        )
        actions_list = workspace.setdefault("explore_actions", [])
        seen = workspace.setdefault("explore_seen", set())
        if not isinstance(actions_list, list) or not isinstance(seen, set):
            return

        action_candidates: list[ActionSuggestion] = list(details.actions) if details is not None else []

        for action in action_candidates:
            if not isinstance(action, ActionSuggestion):
                continue
            payload = action.payload if isinstance(action.payload, dict) else {}
            command = str(payload.get("command") or payload.get("prompt") or "").strip()
            label = str(action.label or "").strip()
            if not command or not label:
                continue
            dedupe_key = command.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            actions_list.append((label, command))

    def _render_explore_actions_for_topic(self, topic_id: str | None) -> None:
        self._details_actions_clear()
        if not topic_id:
            self.details_actions_row.setVisible(False)
            return
        workspace = self._topic_workspaces.get(topic_id)
        if not isinstance(workspace, dict):
            self.details_actions_row.setVisible(False)
            return
        actions_list = workspace.get("explore_actions", [])
        if not isinstance(actions_list, list) or not actions_list:
            self.details_actions_row.setVisible(False)
            return

        header = QLabel("Explore further")
        self.details_actions_layout.addWidget(header)
        for item in actions_list:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            label, command = item
            button = QPushButton(str(label))
            button.clicked.connect(lambda _checked=False, command_text=str(command): self._submit_command(command_text))
            self.details_actions_layout.addWidget(button)
            self.details_action_buttons.append(button)

        self.details_actions_row.setVisible(bool(self.details_action_buttons))

    def _response_topic(self, response) -> TopicContext | None:
        return getattr(response, "topic", None)

    def _unpack_topic(self, topic: TopicContext | None) -> tuple[str, str, str]:
        if topic is None:
            return "", "Details", "continue"
        return topic.id, topic.title, topic.relationship

    def _render_result_cards(self, items) -> str:
        if not items:
            return "<i>No matching items.</i>"
        blocks: list[str] = []
        for item in items:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("path") or item.get("title") or "Item")
                path = str(item.get("path") or item.get("directory") or "")
                directory = str(item.get("directory") or (Path(path).parent if path else ""))
                size = self._format_size(item.get("size_bytes"))
                modified_at = str(item.get("modified_at") or "")
                match_reason = str(item.get("match_reason") or item.get("reason") or "")
                blocks.append("<div style='margin-bottom: 0.75em; padding: 0.5em; border: 1px solid #ccc; border-radius: 6px;'>")
                blocks.append(f"<b>{self._escape_html(name)}</b><br>")
                if path:
                    blocks.append(f"{self._escape_html(path)}<br>")
                if directory:
                    blocks.append(f"{self._escape_html(directory)}<br>")
                if size:
                    blocks.append(f"{self._escape_html(size)}<br>")
                if modified_at:
                    blocks.append(f"Modified: {self._escape_html(modified_at)}<br>")
                if match_reason:
                    blocks.append(f"{self._escape_html(match_reason)}<br>")
                blocks.append("</div>")
            else:
                blocks.append(f"<div style='margin-bottom: 0.75em; padding: 0.5em; border: 1px solid #ccc; border-radius: 6px;'>{self._escape_html(str(item))}</div>")
        return "".join(blocks)

    def _show_file_operations_panel(self, title: str) -> None:
        self.details_title.setText(title)
        self.details_stack.setCurrentWidget(self.file_ops_panel)
        self._details_actions_clear()
        self.details_actions_row.setVisible(False)

    def _sync_file_operations_panel(self, detail_type: str, items, metadata) -> None:
        payload = metadata.get("file_operations") if isinstance(metadata, dict) else None
        if isinstance(payload, dict) and str(payload.get("mode", "")).strip().lower() == "search":
            self.file_ops_panel.show_search_results(payload)
            self._show_file_operations_panel("Search")
            return

        if detail_type in {"search_results", "file_results", "file_list"}:
            fallback_payload = self._build_file_search_payload_from_items(items)
            if fallback_payload is not None:
                self.file_ops_panel.show_search_results(fallback_payload)
                self._show_file_operations_panel("Search")
            return

        command_text = str(metadata.get("command", "")).strip().lower() if isinstance(metadata, dict) else ""
        if command_text.startswith("/index"):
            summary = self._extract_index_summary_from_items(items)
            if summary:
                self.file_ops_panel.update_index_progress(summary)
                self._show_file_operations_panel("Indexing")

    def _build_file_search_payload_from_items(self, items) -> dict[str, object] | None:
        if not isinstance(items, list):
            return None
        matches: list[dict[str, object]] = []
        seen_paths: set[str] = set()
        for entry in items:
            if isinstance(entry, dict):
                direct_path = str(entry.get("path", "")).strip()
                if direct_path:
                    key = direct_path.lower()
                    if key not in seen_paths:
                        seen_paths.add(key)
                        matches.append({"path": direct_path, "classification": "partial"})
                text_value = str(entry.get("text", "")).strip()
            else:
                text_value = str(entry).strip()
            if not text_value:
                continue
            match = re.search(r"\bpath:\s*(.+)$", text_value, re.IGNORECASE)
            if match is None:
                continue
            candidate = match.group(1).strip()
            if not candidate:
                continue
            key = candidate.lower()
            if key in seen_paths:
                continue
            seen_paths.add(key)
            matches.append({"path": candidate, "classification": "partial"})

        if not matches:
            return None
        return {
            "mode": "search",
            "query": "search results",
            "total_matches": len(matches),
            "displayed_matches": len(matches),
            "searched_roots": [],
            "failed_roots": [],
            "matches": matches,
        }

    def _extract_index_summary_from_items(self, items) -> str:
        if not isinstance(items, list):
            return ""
        for entry in reversed(items):
            if not isinstance(entry, dict):
                text_value = str(entry).strip()
            else:
                text_value = str(entry.get("text", "")).strip()
            if text_value.lower().startswith("scan complete:"):
                return text_value
        return ""

    def _details_actions_clear(self) -> None:
        while self.details_actions_layout.count():
            item = self.details_actions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.details_action_buttons = []

    def _set_confirmation_controls_visible(self, visible: bool) -> None:
        self.approve_button.setVisible(visible)
        self.cancel_confirmation_button.setVisible(visible)

    def _approve_pending_action(self) -> None:
        self._submit_command("/confirm")

    def _cancel_pending_action(self) -> None:
        self._submit_command("/cancel")

    def _submit_command(self, command_text: str) -> None:
        self.input_box.setPlainText(command_text)
        self.send_message()

    def _response_conversation(self, response) -> ConversationContent | None:
        return getattr(response, "conversation", None)

    def _response_details(self, response) -> DetailContent | None:
        return getattr(response, "details", None)

    def _response_metadata(self, response) -> dict[str, object]:
        metadata = getattr(response, "metadata", None)
        if isinstance(metadata, dict) and metadata:
            return metadata
        details = self._response_details(response)
        return details.metadata if details is not None else {}

    def _unpack_detail(self, detail: DetailContent | None):
        if detail is None:
            return "text", None, None, None, None, [], [], {}
        return detail.type, detail.section_id, detail.title, detail.content, detail.summary, detail.items, detail.actions, detail.metadata

    def _format_detail_item(self, item) -> str:
        if isinstance(item, dict):
            parts = []
            for key in ("name", "path", "directory", "match_reason", "summary", "message"):
                value = item.get(key)
                if value:
                    parts.append(f"{key}: {value}")
            if not parts:
                parts = [f"{key}: {value}" for key, value in item.items()]
            return " | ".join(parts)
        return str(item)

    def _format_metadata(self, metadata: dict[str, object]) -> str:
        return "\n".join(f"{key}: {value}" for key, value in metadata.items())

    def _format_size(self, value) -> str:
        if not isinstance(value, (int, float)):
            return ""
        size = float(value)
        units = ["bytes", "KB", "MB", "GB", "TB"]
        index = 0
        while size >= 1024 and index < len(units) - 1:
            size /= 1024
            index += 1
        if index == 0:
            return f"{int(size)} {units[index]}"
        return f"{size:.1f} {units[index]}"

    def _escape_html(self, text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )

    def _set_details_visible(self, visible: bool) -> None:
        self.details_panel.setVisible(visible)
        self.details_toggle.setText("Hide Details" if visible else "Show Details")

    def set_status(self, value: str) -> None:
        self.status_label.setText(value)

    def _restore_window_state(self) -> None:
        splitter_state = self.settings.value("window/splitter")
        if splitter_state is not None:
            self.splitter.restoreState(splitter_state)
        else:
            total_width = max(self.width(), 1200)
            left_width = total_width // 3
            right_width = total_width - left_width
            self.splitter.setSizes([left_width, right_width])
        self.details_toggle.setChecked(True)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/splitter", self.splitter.saveState())
        self.settings.setValue("window/details_visible", self.details_panel.isVisible())
        if self.init_worker is not None and self.init_worker.isRunning():
            self._close_requested = True
            self.set_status("Stopping")
            event.ignore()
            return
        if self.worker is not None and self.worker.isRunning():
            self._close_requested = True
            self.worker.cancel_event.set()
            self.worker.cancel_prompt()
            self.stop_button.setEnabled(False)
            self.set_status("Stopping")
            event.ignore()
            return
        try:
            self.app_service.shutdown()
        except (RuntimeError, OSError, ValueError, AssertionError) as error:
            QMessageBox.warning(self, "Shutdown Warning", f"Shutdown encountered an error: {error}")
        super().closeEvent(event)


def _resolve_config_path(argv: list[str]) -> tuple[Path, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=str, default=None)
    parsed, remaining = parser.parse_known_args(argv)

    if parsed.config:
        return Path(parsed.config).expanduser(), remaining

    env_config = os.getenv("IRIS_CONFIG_PATH", "").strip()
    if env_config:
        return Path(env_config).expanduser(), remaining

    return workspace_root / "core" / "config.json", remaining


def main() -> None:
    config_path, qt_argv = _resolve_config_path(sys.argv[1:])
    app = QApplication([sys.argv[0], *qt_argv])
    window = IrisWindow(config_path)
    if window._start_maximized:
        window.showMaximized()
    else:
        window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
