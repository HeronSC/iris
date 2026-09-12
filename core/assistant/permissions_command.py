# File: core/assistant/permissions_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.audit.stream import AuditCategory, AuditStream
from core.permissions.policy import PermissionPolicy
from core.permissions.secrets import SecretError, SecretStore, environment_name

USAGE = "Usage: /permissions [show|denied]  ·  /secrets [list|set <name> <value>|clear <name>]"


class PermissionsCommandHandler:

    def __init__(
        self,
        policy: PermissionPolicy,
        secrets: SecretStore,
        audit: AuditStream | None = None,
        output: OutputSink | None = None,
    ) -> None:
        self.policy = policy
        self.secrets = secrets
        self.audit = audit
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        lowered = text.lower()
        if lowered.startswith("/permissions"):
            return self._permissions(text)
        if lowered.startswith("/secrets"):
            return self._secrets(text)
        return False

    def _permissions(self, text: str) -> bool:
        parts = text.split(maxsplit=1)
        subcommand = parts[1].strip().lower() if len(parts) > 1 else "show"
        if subcommand in {"", "show"}:
            self._show()
            return True
        if subcommand in {"denied", "denials"}:
            self._denied()
            return True
        emit_output(self.output, USAGE)
        return True

    def _show(self) -> None:
        rules = self.policy.describe()
        lines = ["Permission levels:"]
        for level, mode in rules["modes"].items():
            lines.append(f"- {level}: {mode}")
        lines.append(
            "Folders: " + (", ".join(rules["allowed_paths"]) if rules["allowed_paths"] else "anywhere the action itself allows")
        )
        if rules["denied_paths"]:
            lines.append("Never: " + ", ".join(rules["denied_paths"]))
        lines.append("Hosts: " + (", ".join(rules["allowed_hosts"]) if rules["allowed_hosts"] else "any"))
        if rules["denied_hosts"]:
            lines.append("Blocked hosts: " + ", ".join(rules["denied_hosts"]))
        for name, limit in rules["rate_limits"].items():
            lines.append(f"Cap on {name}: {limit}")
        remaining = rules["outbound_remaining"]
        if remaining is not None:
            lines.append(f"Outbound left this window: {remaining}")
        emit_output(self.output, "\n".join(lines))

    def _denied(self) -> None:
        if self.audit is None:
            emit_output(self.output, "No audit trail is attached.")
            return
        events = self.audit.read(category=AuditCategory.PERMISSION, limit=10)
        if not events:
            emit_output(self.output, "Nothing has been refused.")
            return
        lines = ["Recently refused:"]
        for event in events:
            target = f" on {event.target}" if event.target else ""
            lines.append(f"- {event.created_at} {event.event}{target}: {event.message}")
        emit_output(self.output, "\n".join(lines))

    def _secrets(self, text: str) -> bool:
        parts = text.split(maxsplit=3)
        subcommand = parts[1].lower() if len(parts) > 1 else "list"
        name = parts[2].strip() if len(parts) > 2 else ""
        value = parts[3].strip() if len(parts) > 3 else ""

        if subcommand == "list":
            self._list_secrets()
            return True
        if subcommand == "set":
            if not name or not value:
                emit_output(self.output, "Usage: /secrets set <name> <value>")
                return True
            try:
                self.secrets.set(name, value)
            except SecretError as error:
                emit_output(self.output, str(error))
                return True
            emit_output(self.output, f"Saved {name} to the credential store. It is never written to config.json or a log.")
            return True
        if subcommand in {"clear", "remove", "delete"}:
            if not name:
                emit_output(self.output, "Usage: /secrets clear <name>")
                return True
            removed = self.secrets.delete(name)
            emit_output(self.output, f"Cleared {name}." if removed else f"Nothing stored under {name}.")
            return True

        emit_output(self.output, USAGE)
        return True

    def _list_secrets(self) -> None:
        rows = self.secrets.describe()
        if not rows:
            where = "the credential store" if self.secrets.available else f"the environment ({environment_name('name')} style)"
            emit_output(self.output, f"No secrets are stored. /secrets set <name> <value> puts one in {where}.")
            return
        lines = ["Stored secrets (names only):"]
        for row in rows:
            state = row["source"] if row["set"] else "missing"
            lines.append(f"- {row['name']}: {state}")
        emit_output(self.output, "\n".join(lines))
