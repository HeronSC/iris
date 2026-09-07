# File: core/profile/update_service.py

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.audit.logger import AuditLogger
from core.profile.proposal import MemoryProposal
from core.profile.proposal_store import MemoryProposalStore
from core.profile.writer import MemoryWriter


class MemoryUpdateError(Exception):
    pass


class MemoryUpdateService:
    def __init__(
        self,
        memory_folder: str | Path,
        proposal_store: MemoryProposalStore,
        audit_logger: AuditLogger,
        memory_store: Any | None = None,
    ) -> None:
        self.memory_folder = Path(memory_folder).expanduser()
        self.proposal_store = proposal_store
        self.audit_logger = audit_logger
        self.writer = MemoryWriter(self.memory_folder)
        self.memory_store = memory_store

    def apply_proposal(self, proposal: MemoryProposal) -> dict[str, Any]:
        try:
            current_memory = self._load_current_memory()
            self._validate_area(proposal.memory_area)
            self._validate_proposal(proposal, current_memory)
            before = deepcopy(current_memory)
            after = deepcopy(current_memory)
            self._apply_change(after, proposal)
            self._write_memory(after)
            self.audit_logger.log(
                {
                    "timestamp": self._utc_now_iso(),
                    "proposal_id": proposal.id,
                    "action": "approved",
                    "memory_area": proposal.memory_area,
                    "operation": proposal.operation,
                    "target_id": proposal.target_id,
                    "session_id": proposal.source_session_id,
                    "before": before,
                    "after": after,
                }
            )
            if self.memory_store is not None and hasattr(self.memory_store, "replace_data"):
                self.memory_store.replace_data(after)
            return {"status": "applied", "before": before, "after": after}
        except Exception as error:
            self.audit_logger.log(
                {
                    "timestamp": self._utc_now_iso(),
                    "proposal_id": proposal.id,
                    "action": "failed",
                    "memory_area": proposal.memory_area,
                    "operation": proposal.operation,
                    "target_id": proposal.target_id,
                    "session_id": proposal.source_session_id,
                    "error": str(error),
                }
            )
            if isinstance(error, MemoryUpdateError):
                raise
            raise MemoryUpdateError(str(error)) from error

    def _load_current_memory(self) -> dict[str, Any]:
        memory: dict[str, Any] = {}
        for name in ["profile", "preferences", "projects", "knowledge"]:
            path = self.memory_folder / f"{name}.json"
            if not path.exists():
                raise MemoryUpdateError(f"Missing memory file: {path.name}")
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise MemoryUpdateError(f"{path.name} must contain a JSON object")
            memory[name] = payload
        return memory

    def _validate_area(self, area: str) -> None:
        if area not in {"profile", "preferences", "projects", "knowledge"}:
            raise MemoryUpdateError(f"Unsupported memory area: {area}")

    def _validate_proposal(self, proposal: MemoryProposal, current_memory: dict[str, Any]) -> None:
        if proposal.operation not in {"add", "update", "archive"}:
            raise MemoryUpdateError("Unsupported operation")
        if not isinstance(proposal.proposed_value, dict):
            raise MemoryUpdateError("Proposed value must be an object")

        area_payload = current_memory.get(proposal.memory_area, {})
        if proposal.memory_area == "preferences":
            container = area_payload.get("preferences", [])
            if not isinstance(container, list):
                raise MemoryUpdateError("preferences must be a list")
            if proposal.operation == "add" and any(item.get("id") == proposal.target_id for item in container if isinstance(item, dict)):
                raise MemoryUpdateError("Duplicate ID conflict")
            if proposal.operation in {"update", "archive"} and not any(item.get("id") == proposal.target_id for item in container if isinstance(item, dict)):
                raise MemoryUpdateError("Missing target conflict")
        if proposal.memory_area == "projects":
            container = area_payload.get("projects", [])
            if not isinstance(container, list):
                raise MemoryUpdateError("projects must be a list")
            if proposal.operation == "add" and any(item.get("id") == proposal.target_id for item in container if isinstance(item, dict)):
                raise MemoryUpdateError("Duplicate ID conflict")
            if proposal.operation == "update" and not any(item.get("id") == proposal.target_id for item in container if isinstance(item, dict)):
                raise MemoryUpdateError("Missing target conflict")
            if proposal.operation == "archive" and not any(item.get("id") == proposal.target_id for item in container if isinstance(item, dict)):
                raise MemoryUpdateError("Missing target conflict")

    def _apply_change(self, memory: dict[str, Any], proposal: MemoryProposal) -> None:
        area_payload = memory.setdefault(proposal.memory_area, {})
        if proposal.memory_area == "preferences":
            items = area_payload.setdefault("preferences", [])
            if proposal.operation == "add":
                items.append(proposal.proposed_value)
            elif proposal.operation == "archive":
                for item in items:
                    if isinstance(item, dict) and item.get("id") == proposal.target_id:
                        item["metadata"] = {**item.get("metadata", {}), "status": "archived"}
            elif proposal.operation == "update":
                for item in items:
                    if isinstance(item, dict) and item.get("id") == proposal.target_id:
                        item.update(proposal.proposed_value)
        elif proposal.memory_area == "projects":
            items = area_payload.setdefault("projects", [])
            if proposal.operation == "add":
                items.append(proposal.proposed_value)
            elif proposal.operation == "update":
                for item in items:
                    if isinstance(item, dict) and item.get("id") == proposal.target_id:
                        item.update(proposal.proposed_value)
            elif proposal.operation == "archive":
                for item in items:
                    if isinstance(item, dict) and item.get("id") == proposal.target_id:
                        item["status"] = "archived"

    def _write_memory(self, memory: dict[str, Any]) -> None:
        self.writer.save_profile(memory.get("profile", {}))
        self.writer.save_preferences(memory.get("preferences", {}))
        self.writer.save_projects(memory.get("projects", {}))
        self.writer.save_knowledge(memory.get("knowledge", {}))

    def _utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

