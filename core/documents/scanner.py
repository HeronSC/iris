from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from core.documents.catalog import DocumentCatalog
from core.documents.extractors.base import DocumentExtractor
from core.documents.models import DocumentSearchConfig, DocumentSearchRoot, ExtractedDocument, coerce_document_search_root


@dataclass(frozen=True)
class ScanResult:
    scanned_files: int
    indexed_files: int
    updated_files: int
    deleted_files: int
    error_files: int


_MAX_DIRECTORY_ERRORS = 1


class DocumentScanner:
    def __init__(
        self,
        config: DocumentSearchConfig,
        catalog: DocumentCatalog,
        extractors: list[DocumentExtractor],
    ) -> None:
        self.config = config
        self.catalog = catalog
        self.extractors = extractors

    def scan(self, root_filter: str | None = None, progress_callback: Callable[[str], None] | None = None) -> ScanResult:
        roots = self._selected_roots(root_filter)
        seen_paths: set[str] = set()
        successfully_scanned_roots: list[Path] = []
        scanned_files = 0
        indexed_files = 0
        updated_files = 0
        error_files = 0

        for root in roots:
            root_config = coerce_document_search_root(root)
            root_path = root_config.path
            if not root_path.exists() or not root_path.is_dir():
                self.catalog.log_scan_error(str(root_path), "Root does not exist or is not a directory")
                error_files = error_files + 1
                continue

            if progress_callback is not None:
                progress_callback(f"ROOT:{root_path}")

            root_had_walk_errors = False
            directory_error_counts: dict[str, int] = {}
            skipped_directories: set[str] = set()

            def mark_directory_error(directory_path: Path) -> None:
                nonlocal error_files, root_had_walk_errors
                directory_key = self._directory_key(directory_path)
                directory_error_counts[directory_key] = directory_error_counts.get(directory_key, 0) + 1
                error_files = error_files + 1
                if directory_error_counts[directory_key] >= _MAX_DIRECTORY_ERRORS and directory_key not in skipped_directories:
                    skipped_directories.add(directory_key)
                    root_had_walk_errors = True
                    self.catalog.log_scan_error(str(directory_path), "Skipping directory after scan errors")

            def on_walk_error(error: OSError) -> None:
                target = Path(str(getattr(error, "filename", "") or root_path))
                self.catalog.log_scan_error(str(target), str(error))
                mark_directory_error(target)

            try:
                for current_root, directories, files in os.walk(root_path, onerror=on_walk_error):
                    current_root_path = Path(current_root)
                    if self._directory_key(current_root_path) in skipped_directories:
                        directories[:] = []
                        continue

                    if progress_callback is not None:
                        progress_callback(current_root)

                    directories[:] = [
                        item
                        for item in directories
                        if not self.config.is_excluded_directory(
                            root_config,
                            current_root_path.relative_to(root_path) / item,
                            item,
                        )
                    ]

                    if not files:
                        continue

                    has_supported_file = any(
                        os.path.splitext(filename)[1].lower() in self.config.supported_extensions
                        for filename in files
                    )
                    if not has_supported_file:
                        continue

                    for filename in files:
                        file_path = current_root_path / filename
                        extension = file_path.suffix.lower()
                        if extension not in self.config.supported_extensions:
                            continue

                        absolute_path = str(file_path.resolve())
                        seen_paths.add(absolute_path)

                        try:
                            stat = file_path.stat()
                        except OSError as error:
                            self.catalog.log_scan_error(str(file_path), str(error))
                            mark_directory_error(file_path.parent)
                            if self._directory_key(file_path.parent) in skipped_directories:
                                directories[:] = []
                                break
                            continue

                        existing = self.catalog.get_by_path(absolute_path)

                        if stat.st_size > self.config.max_file_size_bytes:
                            payload = {
                                "id": existing.id if existing is not None else self._file_id(absolute_path),
                                "path": absolute_path,
                                "name": file_path.name,
                                "extension": extension,
                                "size": int(stat.st_size),
                                "created_at": self._timestamp_iso(stat.st_ctime),
                                "modified_at": self._timestamp_iso(stat.st_mtime),
                                "indexed_at": self._utc_now_iso(),
                                "content_hash": existing.content_hash if existing is not None else "",
                                "content_status": "skipped_size_limit",
                                "extracted_text": "",
                                "extractor": "none",
                                "error": f"Skipped due to size > {self.config.max_file_size_mb}MB",
                            }
                            self.catalog.upsert_document(payload)
                            if existing is None:
                                indexed_files = indexed_files + 1
                            else:
                                updated_files = updated_files + 1
                            continue

                        scanned_files = scanned_files + 1
                        created_at = self._timestamp_iso(stat.st_ctime)
                        modified_at = self._timestamp_iso(stat.st_mtime)

                        if existing is not None and existing.size == stat.st_size and existing.modified_at == modified_at:
                            continue

                        try:
                            extracted = self._extract(file_path)
                            content_hash = self._sha256(file_path)
                            payload = {
                                "id": self._file_id(absolute_path),
                                "path": absolute_path,
                                "name": file_path.name,
                                "extension": extension,
                                "size": int(stat.st_size),
                                "created_at": created_at,
                                "modified_at": modified_at,
                                "indexed_at": self._utc_now_iso(),
                                "content_hash": content_hash,
                                "content_status": extracted.content_status,
                                "extracted_text": extracted.text,
                                "extractor": extracted.extractor,
                                "error": extracted.error,
                            }
                            self.catalog.upsert_document(payload)
                        except Exception as error:
                            self.catalog.log_scan_error(absolute_path, str(error))
                            mark_directory_error(file_path.parent)
                            if self._directory_key(file_path.parent) in skipped_directories:
                                directories[:] = []
                                break
                            continue

                        if extracted.content_status == "error":
                            error_files = error_files + 1
                            self.catalog.log_scan_error(absolute_path, extracted.error or "Extraction failed")

                        if existing is None:
                            indexed_files = indexed_files + 1
                        else:
                            updated_files = updated_files + 1
            except Exception as error:
                root_had_walk_errors = True
                self.catalog.log_scan_error(str(root_path), f"Unexpected scan traversal failure: {error}")
                error_files = error_files + 1

            if not root_had_walk_errors:
                successfully_scanned_roots.append(root_path)

            if progress_callback is not None:
                progress_callback(f"ROOT_DONE:{root_path}")

        if progress_callback is not None:
            progress_callback("CLEANUP_START")

        def report_cleanup_seen_progress(processed: int, total: int) -> None:
            if progress_callback is not None:
                progress_callback(f"CLEANUP_PATHS:{processed}:{total}")

        def report_cleanup_delete_progress(processed: int, total: int) -> None:
            if progress_callback is not None:
                if processed == 0:
                    progress_callback(f"CLEANUP_DELETE_TOTAL:{total}")
                progress_callback(f"CLEANUP_DELETE:{processed}:{total}")

        if progress_callback is not None:
            progress_callback(f"CLEANUP_CATALOG_DONE:{len(seen_paths)}")

        if progress_callback is not None:
            progress_callback("CLEANUP_DIFF_START")

        deleted_files = self.catalog.prune_missing_paths_under_roots(
            successfully_scanned_roots,
            seen_paths,
            seen_progress_callback=report_cleanup_seen_progress,
            delete_progress_callback=report_cleanup_delete_progress,
            progress_interval=1000,
        )

        if progress_callback is not None:
            progress_callback("CLEANUP_DONE")

        self.catalog.set_scan_state("last_scan_at", self._utc_now_iso())
        return ScanResult(
            scanned_files=scanned_files,
            indexed_files=indexed_files,
            updated_files=updated_files,
            deleted_files=deleted_files,
            error_files=error_files,
        )

    def _selected_roots(self, root_filter: str | None) -> list[Path | DocumentSearchRoot]:
        if not root_filter:
            return self.config.roots
        normalized_filter = root_filter.strip()
        if len(normalized_filter) >= 2 and normalized_filter[0] == normalized_filter[-1] and normalized_filter[0] in {'"', "'"}:
            normalized_filter = normalized_filter[1:-1].strip()
        lowered = normalized_filter.lower()
        roots: list[Path | DocumentSearchRoot] = []
        for root in self.config.roots:
            root_path = coerce_document_search_root(root).path
            root_name = root_path.name.lower() or str(root_path).lower()
            if lowered in {root_name, str(root_path).lower(), str(root_path.resolve()).lower()}:
                roots.append(root)
        if roots:
            return roots

        candidate = Path(normalized_filter).expanduser()
        try:
            resolved_candidate = candidate.resolve()
        except OSError:
            resolved_candidate = candidate

        if resolved_candidate.exists() and resolved_candidate.is_dir():
            return [DocumentSearchRoot(path=resolved_candidate)]

        raise ValueError(f"Unknown configured root or directory not found: {root_filter}")

    def _extract(self, file_path: Path) -> ExtractedDocument:
        for extractor in self.extractors:
            if extractor.supports(file_path):
                return extractor.extract(file_path)
        return ExtractedDocument(text="", content_status="text_unavailable", extractor="none", error="No extractor available")

    def _sha256(self, file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()

    def _timestamp_iso(self, epoch_seconds: float) -> str:
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()

    def _utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _file_id(self, path: str) -> str:
        return "file-" + hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]

    def _directory_key(self, path: Path) -> str:
        return str(path).lower()

