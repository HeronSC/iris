# File: core/tests/test_file_tools.py

"""Controlled file editing (3.2): read a window, replace exact text with a diff preview, create a file; confined and undoable."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.actions.implementations.file_tools import EditFileAction, ReadFileAction, WriteFileAction
from core.actions.models import ActionRequest
from core.results.models import ResultKind

SOURCE = "codeunit 99514 Sync\r\n{\r\n    procedure Run()\r\n    begin\r\n        Message('hi');\r\n    end;\r\n}\r\n"


class FileToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.file = self.root / "src" / "Sync.Codeunit.al"
        self.file.parent.mkdir()
        self.file.write_bytes(SOURCE.encode("utf-8"))
        self.context = SimpleNamespace(allowed_roots=[self.root])

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_read_returns_a_numbered_window(self) -> None:
        action = ReadFileAction()
        validation = action.validate(ActionRequest(action="read_file", arguments={"path": str(self.file), "start_line": 3, "max_lines": 2}), self.context)
        self.assertTrue(validation.ok)
        result = action.execute(ActionRequest(action="read_file", arguments=validation.resolved_arguments), self.context)
        self.assertIn("lines 3-4 of 7 (more follows)", result.message)
        self.assertIn("    3      procedure Run()", result.message)
        self.assertEqual(result.results[0].kind, ResultKind.CODE)
        self.assertEqual(result.results[0].data["language"], "al")
        outside = action.validate(ActionRequest(action="read_file", arguments={"path": str(self.root.parent / "elsewhere.txt")}), self.context)
        self.assertFalse(outside.ok)
        self.assertIn("outside the folders", outside.error)
        relative = action.validate(ActionRequest(action="read_file", arguments={"path": "src/Sync.Codeunit.al"}), self.context)
        self.assertFalse(relative.ok)

    def test_edit_previews_a_diff_keeps_crlf_and_names_the_file_for_undo(self) -> None:
        action = EditFileAction()
        validation = action.validate(ActionRequest(action="edit_file", arguments={"path": str(self.file), "old_text": "Message('hi');", "new_text": "Message('hello');"}), self.context)
        self.assertTrue(validation.ok, validation.error)
        self.assertEqual(validation.changes, (str(self.file),))
        self.assertIn("-        Message('hi');", validation.confirmation_preview.metadata["diff"])
        self.assertIn("+        Message('hello');", validation.confirmation_preview.metadata["diff"])
        self.assertIn("Run al_compile afterwards", validation.confirmation_preview.impact)
        self.assertEqual(validation.confirmation_preview.summary, "Edit Sync.Codeunit.al: replace 1 occurrence, 2 lines change")
        result = action.execute(ActionRequest(action="edit_file", arguments=validation.resolved_arguments), self.context)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.results[0].kind, ResultKind.DIFF)
        written = self.file.read_bytes()
        self.assertIn(b"Message('hello');\r\n", written)
        self.assertNotIn(b"\n\n", written.replace(b"\r\n", b""))
        self.assertEqual(written.count(b"\r\n"), SOURCE.count("\r\n"))

    def test_edit_refuses_missing_ambiguous_and_unchanged_text(self) -> None:
        action = EditFileAction()
        missing = action.validate(ActionRequest(action="edit_file", arguments={"path": str(self.file), "old_text": "nope", "new_text": "x"}), self.context)
        self.assertIn("was not found", missing.error)
        ambiguous = action.validate(ActionRequest(action="edit_file", arguments={"path": str(self.file), "old_text": "    ", "new_text": "\t"}), self.context)
        self.assertIn("occurs", ambiguous.error)
        self.assertIn("set occurrences to", ambiguous.error)
        same = action.validate(ActionRequest(action="edit_file", arguments={"path": str(self.file), "old_text": "Run()", "new_text": "Run()"}), self.context)
        self.assertIn("the same", same.error)
        many = action.validate(ActionRequest(action="edit_file", arguments={"path": str(self.file), "old_text": "\r\n", "new_text": "\n", "occurrences": 7}), self.context)
        self.assertIn("the same", many.error)
        git_file = self.root / ".git" / "config"
        git_file.parent.mkdir()
        git_file.write_text("x", encoding="utf-8")
        refused = action.validate(ActionRequest(action="edit_file", arguments={"path": str(git_file), "old_text": "x", "new_text": "y"}), self.context)
        self.assertIn("does not edit", refused.error)

    def test_edit_fails_cleanly_when_the_file_changed_after_the_preview(self) -> None:
        action = EditFileAction()
        validation = action.validate(ActionRequest(action="edit_file", arguments={"path": str(self.file), "old_text": "Message('hi');", "new_text": "Message('hello');"}), self.context)
        self.file.write_text("something else", encoding="utf-8")
        result = action.execute(ActionRequest(action="edit_file", arguments=validation.resolved_arguments), self.context)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "stale")

    def test_write_creates_and_replaces_only_when_told(self) -> None:
        action = WriteFileAction()
        target = self.root / "docs" / "notes.md"
        validation = action.validate(ActionRequest(action="write_file", arguments={"path": str(target), "content": "# Notes\n\nfirst\n"}), self.context)
        self.assertTrue(validation.ok, validation.error)
        self.assertEqual(validation.confirmation_preview.summary, "Create notes.md (3 lines)")
        self.assertIn("/undo removes it again", validation.confirmation_preview.impact)
        result = action.execute(ActionRequest(action="write_file", arguments=validation.resolved_arguments), self.context)
        self.assertEqual(result.status, "success")
        self.assertEqual(target.read_text(encoding="utf-8"), "# Notes\n\nfirst\n")
        again = action.validate(ActionRequest(action="write_file", arguments={"path": str(target), "content": "x"}), self.context)
        self.assertFalse(again.ok)
        self.assertIn("already exists", again.error)
        replace = action.validate(ActionRequest(action="write_file", arguments={"path": str(target), "content": "# Notes\n\nsecond\n", "overwrite": True}), self.context)
        self.assertTrue(replace.ok)
        self.assertEqual(replace.confirmation_preview.summary, "Replace notes.md (3 lines)")
        self.assertIn("-first", replace.confirmation_preview.metadata["diff"])
        crlf = action.validate(ActionRequest(action="write_file", arguments={"path": str(self.file), "content": "a\nb\n", "overwrite": True}), self.context)
        action.execute(ActionRequest(action="write_file", arguments=crlf.resolved_arguments), self.context)
        self.assertEqual(self.file.read_bytes(), b"a\r\nb\r\n")
        binary = self.root / "blob.bin"
        binary.write_bytes(b"\x00\x01\x02")
        refused = ReadFileAction().validate(ActionRequest(action="read_file", arguments={"path": str(binary)}), self.context)
        self.assertTrue(refused.ok)
        read = ReadFileAction().execute(ActionRequest(action="read_file", arguments=refused.resolved_arguments), self.context)
        self.assertEqual(read.error, "refused")
        for tool in (ReadFileAction(), EditFileAction(), WriteFileAction()):
            self.assertEqual(tool.definition.permission.value, "read" if tool.name == "read_file" else "write")


if __name__ == "__main__":
    unittest.main()
