from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.conversation.session import ConversationSession


class SessionRepositoryError(Exception):
    pass


class SessionRepository:
    def __init__(
        self,
        sessions_folder: str | Path,
        assistant_name: str | None = None,
        model: str | None = None,
    ) -> None:
        self.sessions_folder = Path(sessions_folder).expanduser()
        self.sessions_folder.mkdir(parents=True, exist_ok=True)
        self.index_path = self.sessions_folder / "index.json"
        self.default_assistant_name = assistant_name
        self.default_model = model
        self._ensure_index()

    def create_session(self, title: str | None = None, project_id: str | None = None) -> ConversationSession:
        session = ConversationSession(max_messages=None)
        session.id = self._generate_session_id()
        session.title = title or "New Session"
        session.project_id = project_id
        session.created_at = self._utc_now_iso()
        session.updated_at = session.created_at
        session.status = "active"
        session.summary = ""
        session.metadata = {
            "assistant_name": self.default_assistant_name,
            "model": self.default_model,
        }
        self.save_session(session)
        return session

    def get_session(self, session_id: str) -> ConversationSession | None:
        session_path = self._session_path(session_id)
        if not session_path.exists():
            return None
        try:
            payload = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SessionRepositoryError(f"Could not load session {session_id}: {error}") from error

        if not isinstance(payload, dict):
            raise SessionRepositoryError(f"Session {session_id} is not a JSON object")

        session = ConversationSession(max_messages=None)
        session.id = str(payload.get("id", session_id))
        session.title = str(payload.get("title", "New Session"))
        session.created_at = str(payload.get("created_at", self._utc_now_iso()))
        session.updated_at = str(payload.get("updated_at", session.created_at))
        session.status = str(payload.get("status", "active"))
        session.project_id = payload.get("project_id")
        session.summary = str(payload.get("summary", ""))
        session.metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        session.set_messages(payload.get("messages") if isinstance(payload.get("messages"), list) else [])
        return session

    def save_session(self, session: ConversationSession) -> None:
        session.updated_at = self._utc_now_iso()
        session_path = self._session_path(session.id)
        payload = {
            "id": session.id,
            "title": session.title,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "status": session.status,
            "project_id": session.project_id,
            "summary": session.summary,
            "messages": session.get_messages(),
            "metadata": session.metadata,
        }
        try:
            self._write_json_atomic(session_path, payload)
        except OSError as error:
            raise SessionRepositoryError(f"Could not write session {session.id}: {error}") from error
        self._update_index(session)

    def list_sessions(self, status: str | None = None, project_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        payload = self._load_index()
        sessions = payload.get("sessions", []) if isinstance(payload.get("sessions"), list) else []
        filtered = []
        for item in sessions:
            if status is not None and item.get("status") != status:
                continue
            if project_id is not None and item.get("project_id") != project_id:
                continue
            filtered.append(item)
        filtered.sort(key=self._updated_at_sort_key, reverse=True)
        if limit is not None:
            return filtered[:limit]
        return filtered

    def close_session(self, session_id: str) -> None:
        session = self.get_session(session_id)
        if session is None:
            raise SessionRepositoryError(f"Session not found: {session_id}")
        session.status = "closed"
        self.save_session(session)

    def delete_session(self, session_id: str) -> None:
        session_path = self._session_path(session_id)
        if session_path.exists():
            session_path.unlink()
        self._remove_index_entry(session_id)

    def _ensure_index(self) -> None:
        if not self.index_path.exists():
            self._write_json_atomic(self.index_path, {"sessions": []})

    def _load_index(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            payload = self._rebuild_index_from_sessions()
            if payload is None:
                raise SessionRepositoryError(f"Could not read index: {error}") from error
        if not isinstance(payload, dict):
            raise SessionRepositoryError("Session index must be a JSON object")
        return payload

    def _update_index(self, session: ConversationSession) -> None:
        payload = self._load_index()
        sessions = payload.get("sessions", []) if isinstance(payload.get("sessions"), list) else []
        updated = False
        for item in sessions:
            if item.get("id") == session.id:
                item.update(
                    {
                        "id": session.id,
                        "title": session.title,
                        "project_id": session.project_id,
                        "created_at": session.created_at,
                        "updated_at": session.updated_at,
                        "status": session.status,
                    }
                )
                updated = True
                break
        if not updated:
            sessions.append(
                {
                    "id": session.id,
                    "title": session.title,
                    "project_id": session.project_id,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                    "status": session.status,
                }
            )
        payload["sessions"] = sessions
        self._write_json_atomic(self.index_path, payload)

    def _remove_index_entry(self, session_id: str) -> None:
        payload = self._load_index()
        sessions = payload.get("sessions", []) if isinstance(payload.get("sessions"), list) else []
        payload["sessions"] = [item for item in sessions if item.get("id") != session_id]
        self._write_json_atomic(self.index_path, payload)

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_folder / f"{session_id}.json"

    def _generate_session_id(self) -> str:
        now = datetime.now(timezone.utc)
        return now.strftime("%Y%m%d-%H%M%S") + "-" + f"{now.microsecond % 10000:04d}"

    def _utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        temp_path = path.with_suffix(path.suffix + ".tmp")
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)

    def _updated_at_sort_key(self, item: dict[str, Any]) -> tuple[int, str]:
        value = str(item.get("updated_at", "")).strip()
        if not value:
            return (0, "")
        return (1, value)

    def _rebuild_index_from_sessions(self) -> dict[str, Any] | None:
        sessions: list[dict[str, Any]] = []
        try:
            for session_file in self.sessions_folder.glob("*.json"):
                if session_file.name == self.index_path.name:
                    continue
                payload = json.loads(session_file.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                sessions.append(
                    {
                        "id": payload.get("id"),
                        "title": payload.get("title", "New Session"),
                        "project_id": payload.get("project_id"),
                        "created_at": payload.get("created_at"),
                        "updated_at": payload.get("updated_at"),
                        "status": payload.get("status", "active"),
                    }
                )
        except (OSError, json.JSONDecodeError):
            return None

        rebuilt = {"sessions": sessions}
        self._write_json_atomic(self.index_path, rebuilt)
        return rebuilt

