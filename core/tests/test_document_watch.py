# File: core/tests/test_document_watch.py

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from core.documents.catalog import DocumentCatalog
from core.documents.extractors.text_extractor import TextExtractor
from core.documents.models import DocumentSearchConfig, DocumentSearchRoot
from core.documents.scanner import DocumentScanner
from core.documents.watch import DocumentWatchService
from core.storage.sqlite_database import SQLiteDatabase


def _wait(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class ScannerIncrementalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "docs"
        (self.root / "node_modules").mkdir(parents=True)
        (self.root / "notes").mkdir()
        self.catalog = DocumentCatalog(SQLiteDatabase(Path(self._tmp.name) / "documents.db"))
        config = DocumentSearchConfig(
            roots=[DocumentSearchRoot(path=self.root)],
            excluded_directories={"node_modules"},
            supported_extensions={".txt", ".md"},
            max_file_size_mb=1,
        )
        self.scanner = DocumentScanner(config, self.catalog, [TextExtractor()])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_index_file_respects_roots_extensions_and_exclusions(self) -> None:
        good = self.root / "notes" / "a.txt"
        good.write_text("pump filter notes", encoding="utf-8")
        self.assertTrue(self.scanner.is_indexable(good))
        self.assertFalse(self.scanner.is_indexable(self.root / "notes" / "a.exe"))
        self.assertFalse(self.scanner.is_indexable(self.root / "node_modules" / "x.txt"))
        self.assertFalse(self.scanner.is_indexable(Path(self._tmp.name) / "outside.txt"))

        self.assertEqual(self.scanner.index_file(good), "indexed")
        record = self.catalog.get_by_path(str(good.resolve()))
        self.assertIn("pump filter", record.extracted_text)
        self.assertEqual(self.scanner.index_file(good), "unchanged")
        time.sleep(0.02)
        good.write_text("pump filter notes, revised", encoding="utf-8")
        import os

        os.utime(good, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.scanner.index_file(good), "updated")
        self.assertIn("revised", self.catalog.get_by_path(str(good.resolve())).extracted_text)
        self.assertEqual(self.scanner.index_file(self.root / "node_modules" / "x.txt"), "ignored")
        self.assertEqual(self.scanner.remove_path(good), 1)
        self.assertIsNone(self.catalog.get_by_path(str(good.resolve())))

    def test_remove_path_forgets_a_whole_folder(self) -> None:
        for name in ("one.txt", "two.md"):
            (self.root / "notes" / name).write_text("x", encoding="utf-8")
            self.scanner.index_file(self.root / "notes" / name)
        self.assertEqual(self.catalog.count_documents(), 2)
        self.assertEqual(self.scanner.remove_path(self.root / "notes"), 2)
        self.assertEqual(self.catalog.count_documents(), 0)

    def test_full_scan_still_works_after_the_refactor(self) -> None:
        (self.root / "notes" / "a.txt").write_text("alpha", encoding="utf-8")
        (self.root / "node_modules" / "b.txt").write_text("beta", encoding="utf-8")
        result = self.scanner.scan()
        self.assertEqual(result.indexed_files, 1)
        self.assertEqual(self.catalog.count_documents(), 1)
        (self.root / "notes" / "a.txt").unlink()
        result = self.scanner.scan()
        self.assertEqual(result.deleted_files, 1)


class WatchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "docs"
        (self.root / "sub").mkdir(parents=True)
        self.catalog = DocumentCatalog(SQLiteDatabase(Path(self._tmp.name) / "documents.db"))
        config = DocumentSearchConfig(roots=[DocumentSearchRoot(path=self.root)], excluded_directories=set(), supported_extensions={".txt"}, max_file_size_mb=1)
        self.scanner = DocumentScanner(config, self.catalog, [TextExtractor()])
        self.service = DocumentWatchService(self.scanner, debounce_seconds=0.2, rescan_interval_hours=0)
        self.service.start()
        self.assertTrue(_wait(lambda: self.service.running, 5.0))
        time.sleep(0.3)  # let the observer settle before generating events

    def tearDown(self) -> None:
        self.service.stop()
        self._tmp.cleanup()

    def _indexed(self, path: Path) -> bool:
        return self.catalog.get_by_path(str(path.resolve())) is not None

    def test_create_modify_move_and_delete_are_reflected(self) -> None:
        target = self.root / "sub" / "memo.txt"
        target.write_text("first draft", encoding="utf-8")
        self.assertTrue(_wait(lambda: self._indexed(target)), self.service.status())
        self.assertIn("first draft", self.catalog.get_by_path(str(target.resolve())).extracted_text)

        time.sleep(0.5)
        target.write_text("second draft, longer", encoding="utf-8")
        self.assertTrue(_wait(lambda: "second draft" in (self.catalog.get_by_path(str(target.resolve())) or type("R", (), {"extracted_text": ""})()).extracted_text), self.service.status())

        moved = self.root / "memo-moved.txt"
        target.rename(moved)
        self.assertTrue(_wait(lambda: self._indexed(moved) and not self._indexed(target)), self.service.status())

        moved.unlink()
        self.assertTrue(_wait(lambda: not self._indexed(moved)), self.service.status())
        status = self.service.status()
        self.assertGreaterEqual(status["events"], 4)
        self.assertGreaterEqual(status["removed"], 1)

    def test_unsupported_files_are_ignored_and_status_reports(self) -> None:
        (self.root / "image.png").write_bytes(b"\x89PNG")
        self.service.enqueue(str(self.root / "image.png"), "changed")
        # The write itself raised real events too (folder, create, modify); all are ignored.
        applied = self.service.flush()
        self.assertGreaterEqual(applied["ignored"] + self.service.stats["ignored"], 1)
        self.assertEqual(applied.get("indexed", 0) + applied.get("errors", 0), 0)
        self.assertEqual(self.catalog.count_documents(), 0)
        status = self.service.status()
        self.assertTrue(status["running"])
        self.assertEqual(status["roots"], [str(self.root)])


if __name__ == "__main__":
    unittest.main()
