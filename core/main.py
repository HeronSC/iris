from __future__ import annotations

from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from core.application import IrisApplication, MessageRole
from core.application.contracts import DetailContent
from core.config.loader import ConfigError
from core.memory.loader import AssistantMemoryError


def _render_console_response(response, assistant_name: str) -> None:
    conversation = getattr(response, "conversation", None)
    if conversation is not None and getattr(conversation, "message", ""):
        print(f"{assistant_name}: {conversation.message}")

    details = getattr(response, "details", None)
    if details is not None:
        title = details.title or details.type.replace("_", " ").title()
        if title:
            print(f"Details: {title}")
        if details.summary:
            print(details.summary)
        for item in details.items:
            print(f"- {item}")

    for role, text in [(message.role, message.text) for message in getattr(response, "messages", [])]:
        if role == MessageRole.USER:
            continue
        if role == MessageRole.ASSISTANT:
            print(f"{assistant_name}: {text}")
            continue
        label = {
            MessageRole.SYSTEM: "System",
            MessageRole.PROGRESS: "Progress",
            MessageRole.ERROR: "Error",
            MessageRole.CONFIRMATION: "Confirmation",
        }.get(role, "System")
        print(f"{label}: {text}")


def main() -> None:
    app = IrisApplication(Path(__file__).resolve().parent / "config.json")
    try:
        app.initialize()
    except (ConfigError, AssistantMemoryError) as error:
        print(f"Assistant could not start: {error}")
        return

    for startup in app.startup_messages:
        print(startup.text)
    print()

    while True:
        user_input = input("You: ").strip()
        response = app.process_message(user_input)
        _render_console_response(response, app.config["assistant_name"])
        if user_input.lower() in {"exit", "quit"}:
            break


if __name__ == "__main__":
    main()
