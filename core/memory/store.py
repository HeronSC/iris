from __future__ import annotations

from copy import deepcopy
from typing import Any


class MemoryStore:
    def __init__(self, memory_data: dict[str, Any]) -> None:
        self._memory_data = memory_data

    def get_profile(self) -> dict[str, Any]:
        return self._memory_data.get("profile", {})

    def get_preferences(self) -> list[dict[str, Any]]:
        return self._memory_data.get("preferences", {}).get("preferences", [])

    def get_active_preferences(self) -> list[dict[str, Any]]:
        return [item for item in self.get_preferences() if item.get("status") == "active"]

    def get_projects(self) -> list[dict[str, Any]]:
        return self._memory_data.get("projects", {}).get("projects", [])

    def get_active_projects(self) -> list[dict[str, Any]]:
        return [item for item in self.get_projects() if item.get("status") == "active"]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        needle = str(project_id).casefold()
        for project in self.get_projects():
            if str(project.get("id", "")).casefold() == needle:
                return project
        return None

    def get_knowledge_areas(self) -> list[dict[str, Any]]:
        return self._memory_data.get("knowledge", {}).get("knowledge_areas", [])

    def get_knowledge_area(self, knowledge_id: str) -> dict[str, Any] | None:
        for area in self.get_knowledge_areas():
            if area.get("id") == knowledge_id:
                return area
        return None

    def get_preference(self, preference_id: str) -> dict[str, Any] | None:
        for preference in self.get_preferences():
            if preference.get("id") == preference_id:
                return preference
        return None

    def get_active_project_context(self, project_id: str) -> dict[str, Any] | None:
        project = self.get_project(project_id)
        if project is None:
            return None
        return {
            "id": project.get("id"),
            "name": project.get("name"),
            "status": project.get("status"),
            "current_focus": project.get("current_focus"),
            "technologies": project.get("technologies", []),
        }

    def find_projects(self, search_text: str) -> list[dict[str, Any]]:
        needle = search_text.lower()
        return [
            project
            for project in self.get_projects()
            if needle in str(project.get("name", "")).lower()
            or needle in str(project.get("summary", "")).lower()
        ]

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._memory_data)

    def replace_data(self, memory_data: dict[str, Any]) -> None:
        self._memory_data = deepcopy(memory_data)
