import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from core.assistant.index_commands import IndexCommandHandler
from core.assistant.search_commands import SearchCommandHandler
from core.config.loader import ConfigLoader
from core.documents.catalog import DocumentCatalog
from core.documents.extractors.csv_extractor import CsvExtractor
from core.documents.extractors.text_extractor import TEXT_FILE_EXTENSIONS, TextExtractor
from core.documents.models import DocumentSearchConfig, DocumentSearchRoot
from core.documents.query_parser import FileSearchQueryParser, QueryParserConfig
from core.documents.scanner import DocumentScanner, ScanResult
from core.documents.search_service import DocumentSearchService
from core.storage.sqlite_database import SQLiteDatabase


class PhaseThreeTests(unittest.TestCase):
    def test_config_loader_parses_document_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory_dir = root / "memory"
            memory_dir.mkdir(parents=True, exist_ok=True)
            docs_dir = root / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "assistant_name": "Iris",
                        "memory_path": str(memory_dir),
                        "model": "qwen3:8b",
                        "llm_server": "http://localhost:11434",
                        "document_search": {
                            "directory_groups": {
                                "coding": ["build", "bin"]
                            },
                            "roots": [
                                {
                                    "path": str(docs_dir),
                                    "excluded_directory_groups": ["coding"],
                                    "excluded_directories": ["env"],
                                    "excluded_directory_prefixes": ["__"],
                                    "excluded_relative_paths": ["generated/assets"],
                                }
                            ],
                            "excluded_directories": ["__pycache__"],
                            "supported_extensions": [".txt", ".csv"],
                            "max_file_size_mb": 5,
                            "catalog_path": str(root / "index" / "docs.db"),
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = ConfigLoader(config_path).load()
            document_search = config["document_search"]

            self.assertEqual(document_search["roots"][0].path, docs_dir)
            self.assertIn("coding", document_search["roots"][0].excluded_directory_groups)
            self.assertIn("generated/assets", document_search["roots"][0].excluded_relative_paths)
            self.assertIn("build", document_search["directory_groups"]["coding"])
            self.assertIn(".", document_search["excluded_directory_prefixes"])
            self.assertIn(".txt", document_search["supported_extensions"])
            self.assertEqual(document_search["max_file_size_mb"], 5)
            self.assertTrue(str(document_search["catalog_path"]).endswith("docs.db"))

    def test_scanner_indexes_incremental_and_deletes_removed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            hidden = docs / "__pycache__"
            hidden.mkdir(parents=True, exist_ok=True)
            (hidden / "skip.txt").write_text("skip me", encoding="utf-8")
            tracked = docs / "etrade_notes.txt"
            tracked.write_text("ETrade holdings", encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            config = DocumentSearchConfig(
                roots=[docs],
                excluded_directories={"__pycache__"},
                supported_extensions={".txt", ".csv"},
                max_file_size_mb=10,
            )
            scanner = DocumentScanner(config=config, catalog=catalog, extractors=[TextExtractor(), CsvExtractor()])

            first = scanner.scan()
            self.assertEqual(first.indexed_files, 1)
            self.assertEqual(catalog.count_documents(), 1)

            second = scanner.scan()
            self.assertEqual(second.indexed_files, 0)
            self.assertEqual(second.updated_files, 0)

            tracked.write_text("ETrade holdings updated", encoding="utf-8")
            third = scanner.scan()
            self.assertEqual(third.updated_files, 1)

            tracked.unlink()
            fourth = scanner.scan()
            self.assertEqual(fourth.deleted_files, 1)
            self.assertEqual(catalog.count_documents(), 0)

    def test_same_size_rapid_change_is_reindexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            tracked = docs / "rapid.txt"
            tracked.write_text("alpha", encoding="utf-8")

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

            tracked.write_text("bravo", encoding="utf-8")
            before = tracked.stat()
            os.utime(tracked, ns=(before.st_atime_ns, before.st_mtime_ns + 1))

            scanner.scan()
            record = catalog.get_by_path(str(tracked.resolve()))

            self.assertIsNotNone(record)
            self.assertEqual(record.extracted_text.strip(), "bravo")

    def test_scanner_reports_root_before_walk_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            tracked = docs / "note.txt"
            tracked.write_text("hello", encoding="utf-8")

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

            updates: list[str] = []
            scanner.scan(progress_callback=lambda value: updates.append(value))

            self.assertGreaterEqual(len(updates), 3)
            self.assertEqual(updates[0], f"ROOT:{docs}")
            self.assertIn(f"ROOT_DONE:{docs}", updates)

    def test_scanner_skips_dot_prefixed_directories_with_prefix_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)

            gradle_cache = docs / ".gradle-user-home-fresh"
            gradle_cache.mkdir(parents=True, exist_ok=True)
            (gradle_cache / "skip.txt").write_text("cache file", encoding="utf-8")
            tracked = docs / "keep.txt"
            tracked.write_text("index me", encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            scanner = DocumentScanner(
                config=DocumentSearchConfig(
                    roots=[docs],
                    excluded_directories=set(),
                    supported_extensions={".txt"},
                    max_file_size_mb=10,
                    excluded_directory_prefixes={"."},
                ),
                catalog=catalog,
                extractors=[TextExtractor()],
            )

            result = scanner.scan()
            self.assertEqual(result.indexed_files, 1)
            self.assertIsNotNone(catalog.get_by_path(str(tracked.resolve())))
            self.assertIsNone(catalog.get_by_path(str((gradle_cache / "skip.txt").resolve())))

    def test_scanner_skips_build_and_language_directories_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)

            build_generated = docs / "build" / "intermediates" / "debug" / "merged.dir" / "values-en"
            build_generated.mkdir(parents=True, exist_ok=True)
            (build_generated / "skip.xml").write_text("<resources />", encoding="utf-8")

            source_file = docs / "app.txt"
            source_file.write_text("keep me", encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            scanner = DocumentScanner(
                config=DocumentSearchConfig(
                    roots=[docs],
                    excluded_directories={"build", "intermediates"},
                    supported_extensions={".txt", ".xml"},
                    max_file_size_mb=10,
                    excluded_directory_prefixes={"values-"},
                ),
                catalog=catalog,
                extractors=[TextExtractor()],
            )

            result = scanner.scan()
            self.assertEqual(result.indexed_files, 1)
            self.assertIsNotNone(catalog.get_by_path(str(source_file.resolve())))
            self.assertIsNone(catalog.get_by_path(str((build_generated / "skip.xml").resolve())))

    def test_scanner_skips_excluded_relative_paths_for_one_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)

            skipped_subtree = docs / "generated" / "assets"
            skipped_subtree.mkdir(parents=True, exist_ok=True)
            (skipped_subtree / "skip.txt").write_text("ignore me", encoding="utf-8")

            included_subtree = docs / "generated" / "other"
            included_subtree.mkdir(parents=True, exist_ok=True)
            (included_subtree / "keep.txt").write_text("keep me", encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            scanner = DocumentScanner(
                config=DocumentSearchConfig(
                    roots=[
                        DocumentSearchRoot(
                            path=docs,
                            excluded_relative_paths={"generated/assets"},
                        )
                    ],
                    excluded_directories=set(),
                    supported_extensions={".txt"},
                    max_file_size_mb=10,
                ),
                catalog=catalog,
                extractors=[TextExtractor()],
            )

            result = scanner.scan()
            self.assertEqual(result.indexed_files, 1)
            self.assertIsNone(catalog.get_by_path(str((skipped_subtree / "skip.txt").resolve())))
            self.assertIsNotNone(catalog.get_by_path(str((included_subtree / "keep.txt").resolve())))

    def test_scanner_indexes_python_source_as_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            source = docs / "main.py"
            source.write_text("print('iris')\n", encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            scanner = DocumentScanner(
                config=DocumentSearchConfig(
                    roots=[docs],
                    excluded_directories=set(),
                    supported_extensions={".py"},
                    max_file_size_mb=10,
                ),
                catalog=catalog,
                extractors=[TextExtractor()],
            )

            result = scanner.scan()

            self.assertEqual(result.indexed_files, 1)
            record = catalog.get_by_path(str(source.resolve()))
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.extractor, "text")
            self.assertEqual(record.content_status, "indexed")
            self.assertIn("print('iris')", record.extracted_text)

    def test_text_extractor_supports_program_text_extensions(self) -> None:
        self.assertIn(".py", TEXT_FILE_EXTENSIONS)
        self.assertIn(".cs", TEXT_FILE_EXTENSIONS)
        self.assertIn(".al", TEXT_FILE_EXTENSIONS)

    def test_removed_supported_extension_is_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            tracked = docs / "legacy.txt"
            tracked.write_text("legacy", encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            scanner_supported = DocumentScanner(
                config=DocumentSearchConfig(
                    roots=[docs],
                    excluded_directories=set(),
                    supported_extensions={".txt"},
                    max_file_size_mb=10,
                ),
                catalog=catalog,
                extractors=[TextExtractor()],
            )
            scanner_supported.scan()
            self.assertIsNotNone(catalog.get_by_path(str(tracked.resolve())))

            scanner_removed = DocumentScanner(
                config=DocumentSearchConfig(
                    roots=[docs],
                    excluded_directories=set(),
                    supported_extensions={".csv"},
                    max_file_size_mb=10,
                ),
                catalog=catalog,
                extractors=[TextExtractor()],
            )
            result = scanner_removed.scan()

            self.assertEqual(result.deleted_files, 1)
            self.assertIsNone(catalog.get_by_path(str(tracked.resolve())))

    def test_query_parser_handles_excel_and_last_month(self) -> None:
        parser = FileSearchQueryParser(QueryParserConfig(default_roots=[Path("C:/Docs")]))

        query = parser.parse("I am looking for an Excel file related to E*TRADE. I think I created it last month.")

        self.assertIn(".xlsx", query.extensions)
        self.assertIn("etrade", query.text_terms)
        self.assertIsNotNone(query.created_after)
        self.assertIsNotNone(query.created_before)

    def test_query_parser_does_not_require_file_type_words_as_terms(self) -> None:
        parser = FileSearchQueryParser(QueryParserConfig(default_roots=[Path("C:/Docs")]))

        excel_query = parser.parse("Find an Excel file")
        pdf_query = parser.parse("Find PDFs")
        word_query = parser.parse("Find Word documents")

        self.assertIn(".xlsx", excel_query.extensions)
        self.assertNotIn("excel", excel_query.text_terms)
        self.assertIn(".pdf", pdf_query.extensions)
        self.assertNotIn("pdf", pdf_query.text_terms)
        self.assertIn(".docx", word_query.extensions)
        self.assertNotIn("word", word_query.text_terms)

    def test_search_results_are_ranked_and_explained(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            (docs / "etrade_portfolio.xlsx").write_text("", encoding="utf-8")
            (docs / "notes.txt").write_text("etrade discussion", encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            config = DocumentSearchConfig(
                roots=[docs],
                excluded_directories=set(),
                supported_extensions={".txt", ".xlsx"},
                max_file_size_mb=10,
            )
            scanner = DocumentScanner(config=config, catalog=catalog, extractors=[TextExtractor()])
            scanner.scan()

            parser = FileSearchQueryParser(QueryParserConfig(default_roots=[docs]))
            service = DocumentSearchService(catalog, parser)

            results = service.search("excel etrade", limit=5)

            self.assertTrue(results)
            self.assertIn("etrade_portfolio.xlsx", results[0].record.name)
            self.assertTrue(any("filename contains" in reason or "extension matches" in reason for reason in results[0].reasons))

    def test_search_with_multiple_roots_returns_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs_a = root / "Docs"
            docs_b = root / "Projects"
            docs_a.mkdir(parents=True, exist_ok=True)
            docs_b.mkdir(parents=True, exist_ok=True)
            (docs_a / "etrade_a.xlsx").write_text("", encoding="utf-8")
            (docs_b / "etrade_b.xlsx").write_text("", encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            scanner = DocumentScanner(
                config=DocumentSearchConfig(
                    roots=[docs_a, docs_b],
                    excluded_directories=set(),
                    supported_extensions={".xlsx"},
                    max_file_size_mb=10,
                ),
                catalog=catalog,
                extractors=[TextExtractor()],
            )
            scanner.scan()

            parser = FileSearchQueryParser(QueryParserConfig(default_roots=[docs_a, docs_b]))
            service = DocumentSearchService(catalog, parser)
            results = service.search("Excel etrade", limit=10)

            names = {item.record.name for item in results}
            self.assertIn("etrade_a.xlsx", names)
            self.assertIn("etrade_b.xlsx", names)

    def test_root_like_filter_uses_platform_separator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs_archive = root / "docsArchive"
            docs.mkdir(parents=True, exist_ok=True)
            docs_archive.mkdir(parents=True, exist_ok=True)
            in_root = docs / "in_root.xlsx"
            out_root = docs_archive / "out_root.xlsx"
            in_root.write_text("", encoding="utf-8")
            out_root.write_text("", encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            scanner = DocumentScanner(
                config=DocumentSearchConfig(
                    roots=[docs, docs_archive],
                    excluded_directories=set(),
                    supported_extensions={".xlsx"},
                    max_file_size_mb=10,
                ),
                catalog=catalog,
                extractors=[TextExtractor()],
            )
            scanner.scan()

            parser = FileSearchQueryParser(QueryParserConfig(default_roots=[docs]))
            service = DocumentSearchService(catalog, parser)
            results = service.search("excel", limit=10)
            names = {item.record.name for item in results}

            self.assertIn("in_root.xlsx", names)
            self.assertNotIn("out_root.xlsx", names)

    def test_fts_query_errors_fall_back_without_crashing(self) -> None:
        class ExecuteProxy:
            def __init__(self, inner, fail_on_match: bool = False):
                self.inner = inner
                self.fail_on_match = fail_on_match

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return self.inner.__exit__(exc_type, exc_val, exc_tb)

            def execute(self, sql, params=()):
                if self.fail_on_match and "documents_fts MATCH" in sql:
                    import sqlite3

                    raise sqlite3.OperationalError("forced fts error")
                return self.inner.execute(sql, params)

            def __getattr__(self, item):
                return getattr(self.inner, item)

        class FailingFtsDatabase(SQLiteDatabase):
            def connect(self):
                return ExecuteProxy(super().connect(), fail_on_match=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            (docs / "alpha.txt").write_text("alpha content", encoding="utf-8")

            catalog = DocumentCatalog(FailingFtsDatabase(root / "index" / "documents.db"))
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
            service = DocumentSearchService(catalog, parser)

            results = service.search("alpha", limit=5)

            self.assertTrue(results)
            self.assertEqual(results[0].record.name, "alpha.txt")

    def test_oversized_new_file_is_cataloged_as_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            big_file = docs / "big.txt"
            big_file.write_text("x" * (2 * 1024 * 1024), encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            scanner = DocumentScanner(
                config=DocumentSearchConfig(
                    roots=[docs],
                    excluded_directories=set(),
                    supported_extensions={".txt"},
                    max_file_size_mb=1,
                ),
                catalog=catalog,
                extractors=[TextExtractor()],
            )

            result = scanner.scan()
            record = catalog.get_by_path(str(big_file.resolve()))

            self.assertEqual(result.indexed_files, 1)
            self.assertIsNotNone(record)
            self.assertEqual(record.content_status, "skipped_size_limit")
            self.assertEqual(record.extracted_text, "")

    def test_natural_language_search_does_not_hijack_date_only_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            (docs / "todo.txt").write_text("phase three", encoding="utf-8")

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
            service = DocumentSearchService(catalog, parser)
            handler = SearchCommandHandler(service)

            self.assertFalse(handler.handle_natural_language("What did we decide yesterday?"))
            self.assertTrue(handler.handle_natural_language("Find the txt file from yesterday"))

    def test_root_boundary_matching_does_not_include_similar_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "Docs"
            docs_archive = root / "DocsArchive"
            docs.mkdir(parents=True, exist_ok=True)
            docs_archive.mkdir(parents=True, exist_ok=True)
            in_root = docs / "note.txt"
            out_root = docs_archive / "archive_note.txt"
            in_root.write_text("in root", encoding="utf-8")
            out_root.write_text("out root", encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            scanner = DocumentScanner(
                config=DocumentSearchConfig(
                    roots=[docs, docs_archive],
                    excluded_directories=set(),
                    supported_extensions={".txt"},
                    max_file_size_mb=10,
                ),
                catalog=catalog,
                extractors=[TextExtractor()],
            )
            scanner.scan()

            kept = set(catalog.list_paths_under_roots([docs]))
            self.assertIn(str(in_root.resolve()), kept)
            self.assertNotIn(str(out_root.resolve()), kept)

    def test_unavailable_root_does_not_delete_existing_catalog_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            tracked = docs / "keep.txt"
            tracked.write_text("keep", encoding="utf-8")

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
            self.assertEqual(catalog.count_documents(), 1)

            docs.rename(root / "docs_offline")
            result = scanner.scan()

            self.assertGreaterEqual(result.error_files, 1)
            self.assertEqual(catalog.count_documents(), 1)

    def test_per_file_extraction_failure_does_not_abort_scan(self) -> None:
        class ExplodingExtractor:
            name = "explode"

            def supports(self, path: Path) -> bool:
                return path.name == "boom.txt"

            def extract(self, path: Path):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            (docs / "boom.txt").write_text("bad", encoding="utf-8")
            (docs / "ok.txt").write_text("good", encoding="utf-8")

            catalog = DocumentCatalog(SQLiteDatabase(root / "index" / "documents.db"))
            scanner = DocumentScanner(
                config=DocumentSearchConfig(
                    roots=[docs],
                    excluded_directories=set(),
                    supported_extensions={".txt"},
                    max_file_size_mb=10,
                ),
                catalog=catalog,
                extractors=[ExplodingExtractor(), TextExtractor()],
            )

            result = scanner.scan()

            self.assertGreaterEqual(result.error_files, 1)
            self.assertIsNotNone(catalog.get_by_path(str((docs / "ok.txt").resolve())))

    def test_index_and_search_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            (docs / "todo.txt").write_text("phase three items", encoding="utf-8")
            ad_hoc = root / "external"
            ad_hoc.mkdir(parents=True, exist_ok=True)
            (ad_hoc / "outside.txt").write_text("outside configured roots", encoding="utf-8")

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
            parser = FileSearchQueryParser(QueryParserConfig(default_roots=[docs]))
            search_service = DocumentSearchService(catalog, parser)

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
                            "catalog_path": str(root / "index" / "documents.db"),
                        },
                    }
                ),
                encoding="utf-8",
            )

            index_handler = IndexCommandHandler(scanner, catalog, config_path=config_path)
            search_handler = SearchCommandHandler(search_service)

            output = StringIO()
            with redirect_stdout(output), patch("builtins.input", return_value="confirm"):
                self.assertTrue(index_handler.handle("/index scan", {}))
                self.assertTrue(index_handler.handle("/index status", {}))
                self.assertTrue(index_handler.handle(f"/index scan {str(docs)}", {}))
                self.assertTrue(index_handler.handle(f"/index scan {str(ad_hoc)}", {}))
                self.assertTrue(index_handler.handle("/index scan missing root", {}))
                self.assertTrue(search_handler.handle("/search todo", {}))

            text = output.getvalue().lower()
            self.assertIn("/index scan is running", text)
            self.assertIn("scan complete", text)
            self.assertIn("indexed documents", text)
            self.assertIn("todo.txt", text)
            self.assertIn("added root to config", text)
            self.assertIn("unknown configured root or directory not found", text)

            persisted = json.loads(config_path.read_text(encoding="utf-8"))
            persisted_roots = persisted.get("document_search", {}).get("roots", [])
            normalized_expected = str(ad_hoc.resolve()).lower()
            normalized_persisted = {
                str(Path(item.get("path", "")).resolve()).lower()
                for item in persisted_roots
                if isinstance(item, dict) and str(item.get("path", "")).strip()
            }
            self.assertIn(normalized_expected, normalized_persisted)

    def test_index_scan_accepts_quoted_configured_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            (docs / "todo.txt").write_text("quoted root", encoding="utf-8")

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

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "assistant_name": "Iris",
                        "memory_path": str(root / "memory"),
                        "model": "qwen3:8b",
                        "llm_server": "http://localhost:11434",
                        "document_search": {
                            "roots": [{"path": str(docs)}],
                            "excluded_directories": [],
                            "supported_extensions": [".txt"],
                            "max_file_size_mb": 10,
                            "catalog_path": str(root / "index" / "documents.db"),
                        },
                    }
                ),
                encoding="utf-8",
            )

            index_handler = IndexCommandHandler(scanner, catalog, config_path=config_path)

            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(index_handler.handle(f'/index scan "{docs}"', {}))

            text = output.getvalue().lower()
            self.assertIn("scan complete", text)
            self.assertNotIn("unknown configured root or directory not found", text)

    def test_index_scan_emits_started_and_finished_folder_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            (docs / "a").mkdir(parents=True, exist_ok=True)
            (docs / "b").mkdir(parents=True, exist_ok=True)
            (docs / "a" / "one.txt").write_text("one", encoding="utf-8")
            (docs / "b" / "two.txt").write_text("two", encoding="utf-8")

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

            progress_messages: list[str] = []

            def sink(text: str, role: str | None) -> None:
                if role == "progress":
                    progress_messages.append(text)

            index_handler = IndexCommandHandler(scanner, catalog, output=sink)
            self.assertTrue(index_handler.handle("/index scan", {}))

            started = [item for item in progress_messages if "started folder:" in item]
            finished = [item for item in progress_messages if "finished folder:" in item]

            self.assertGreater(len(started), 0)
            self.assertEqual(len(started), len(finished))

    def test_index_scan_emits_cleanup_progress_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs = root / "docs"
            docs.mkdir(parents=True, exist_ok=True)

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

            def fake_scan(root_filter: str | None = None, progress_callback=None) -> ScanResult:
                del root_filter
                assert progress_callback is not None
                progress_callback(f"ROOT:{docs}")
                progress_callback(f"ROOT_DONE:{docs}")
                progress_callback("CLEANUP_START")
                progress_callback("CLEANUP_PATHS:5000:12000")
                progress_callback("CLEANUP_CATALOG_DONE:12000")
                progress_callback("CLEANUP_DIFF_START")
                progress_callback("CLEANUP_DELETE_TOTAL:200")
                progress_callback("CLEANUP_DELETE:100:200")
                progress_callback("CLEANUP_DELETE:200:200")
                progress_callback("CLEANUP_DONE")
                return ScanResult(scanned_files=0, indexed_files=0, updated_files=0, deleted_files=200, error_files=0)

            scanner.scan = fake_scan  # type: ignore[method-assign]

            progress_messages: list[str] = []

            def sink(text: str, role: str | None) -> None:
                if role == "progress":
                    progress_messages.append(text)

            index_handler = IndexCommandHandler(scanner, catalog, output=sink)
            self.assertTrue(index_handler.handle("/index scan", {}))

            self.assertTrue(any("reconciling index (cleanup)" in item for item in progress_messages))
            self.assertTrue(any("cleanup catalog scan: 5000/12000" in item for item in progress_messages))
            self.assertTrue(any("cleanup deleting stale entries: 200/200" in item for item in progress_messages))
            self.assertTrue(any("cleanup complete" in item for item in progress_messages))


if __name__ == "__main__":
    unittest.main()

