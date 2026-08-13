from __future__ import annotations

from typing import Any

from core.memory.store import MemoryStore


class ContextBuilder:
    def __init__(self, assistant_name: str, memory_store: MemoryStore) -> None:
        self.assistant_name = assistant_name
        self.memory_store = memory_store

    def build_context(
        self,
        user_message: str,
        project_id: str | None = None,
        session_summary: str | None = None,
        recent_messages: list[dict[str, Any]] | None = None,
    ) -> str:
        profile = self.memory_store.get_profile().get("profile", {})
        professional = profile.get("professional", {})
        technical_environment = profile.get("technical_environment", {})
        location = profile.get("location", {})

        lines: list[str] = []
        lines.append("Assistant identity:")
        lines.append(f"- Name: {self.assistant_name}")
        lines.append("")
        lines.append("User profile:")
        if profile.get("display_name"):
            lines.append(f"- Name: {profile['display_name']}")
        if professional.get("primary_role"):
            lines.append(f"- Role: {professional['primary_role']}")
        if professional.get("primary_language"):
            lines.append(f"- Primary language: {professional['primary_language']}")
        if technical_environment.get("primary_os"):
            lines.append(f"- OS: {technical_environment['primary_os']}")
        if location.get("city"):
            lines.append(f"- Location: {location['city']}")

        lines.append("")
        lines.append("Working preferences:")
        for preference in self.memory_store.get_active_preferences():
            if preference.get("name"):
                lines.append(f"- {preference['name']}")

        if project_id:
            project = self.memory_store.get_project(project_id)
            if project is not None:
                lines.append("")
                lines.append("Active project:")
                lines.append(f"- Name: {project.get('name', project_id)}")
                if project.get("status"):
                    lines.append(f"- Status: {project['status']}")
                if project.get("current_focus"):
                    lines.append(f"- Current focus: {project['current_focus']}")
                if project.get("technologies"):
                    lines.append(f"- Technologies: {', '.join(project['technologies'])}")

        if session_summary:
            lines.append("")
            lines.append("Session summary:")
            lines.append(f"- {session_summary}")

        if recent_messages:
            lines.append("")
            lines.append("Recent conversation:")
            for entry in recent_messages:
                role = entry.get("role", "user")
                content = entry.get("content", "")
                if content:
                    lines.append(f"- {role}: {content}")

        relevant_areas = self._find_relevant_knowledge(user_message)
        if relevant_areas:
            lines.append("")
            lines.append("Relevant knowledge:")
            for area in relevant_areas:
                lines.append(f"- {area.get('name', area.get('id', 'Unknown'))}")

        return "\n".join(lines)

    def _find_relevant_knowledge(self, user_message: str) -> list[dict[str, Any]]:
        lowered = user_message.lower()
        relevant: list[dict[str, Any]] = []
        for area in self.memory_store.get_knowledge_areas():
            name = str(area.get("name", "")).lower()
            topics = [str(topic).lower() for topic in area.get("topics", [])]
            if name in lowered or any(token in lowered for token in topics):
                relevant.append(area)
        return relevant

