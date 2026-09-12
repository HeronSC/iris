# File: core/actions/implementations/screen_tools.py

from __future__ import annotations

import base64
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.llm.models import ChatMessage, LLMRequest
from core.results.models import (
    Result,
    Source,
    image,
    status,
)
from core.results.models import (
    text as text_result,
)
from core.tools.models import PermissionLevel, ToolDefinition

CAPTURE_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
    "$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
    "$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height; "
    "$graphics = [System.Drawing.Graphics]::FromImage($bitmap); "
    "$graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bitmap.Size); "
    "$bitmap.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png); $graphics.Dispose(); $bitmap.Dispose()"
)
VISION_PROMPT = "You are looking at a screenshot of the user's Windows desktop. Describe what is on screen plainly and answer the question. Read visible text exactly; do not guess at what is cut off."


def capture_screen(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    script = CAPTURE_SCRIPT.replace("{path}", str(destination).replace("'", "''"))
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or not destination.is_file():
        raise RuntimeError(
            f"Screen capture failed: {completed.stderr.strip()[:200] or 'no image written'}"
        )
    return destination


class LookArguments(BaseModel):
    question: str = Field(
        default="What is on the screen?",
        description="What to look for or answer from the screen",
    )
    keep: bool = Field(
        default=True,
        description="Keep the screenshot under Data\\Captures (retention removes it after the kept days)",
    )


class ScreenLookAction:
    name = "screen_look"
    definition = ToolDefinition(
        name="screen_look",
        description="Take a screenshot of the desktop right now and have the local vision model answer a question about it. Only when asked; nothing is captured otherwise.",
        arguments=LookArguments,
        permission=PermissionLevel.READ,
        timeout_seconds=180.0,
        cost="ten to thirty seconds; loads the vision model",
        keywords=(
            "look at my screen",
            "what is on my screen",
            "see my screen",
            "read the screen",
            "screenshot",
            "what does this say",
            "on screen",
        ),
    )

    def __init__(self, capture: Callable[[Path], Path] = capture_screen) -> None:
        self.capture = capture

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = LookArguments.model_validate(request.arguments)
        except ValidationError as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        if getattr(context, "model_router", None) is None:
            return ValidationResult(
                ok=False, error="No model router is available in this host"
            )
        return ValidationResult(
            ok=True,
            resolved_target="screen",
            resolved_arguments={
                "question": " ".join(arguments.question.split())
                or "What is on the screen?",
                "keep": arguments.keep,
            },
        )

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        router = getattr(context, "model_router", None)
        captures = Path(
            getattr(context, "captures_dir", None) or Path.cwd() / "Captures"
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        destination = captures / f"screen-{stamp}.png"
        try:
            self.capture(destination)
        except (RuntimeError, OSError) as error:
            return ActionResult(
                status="failed",
                message=str(error),
                action=self.name,
                error="capture_failed",
            )
        payload = destination.read_bytes()
        question = str(request.arguments.get("question") or "What is on the screen?")
        started = time.perf_counter()
        try:
            response = router.chat(
                LLMRequest(
                    messages=(
                        ChatMessage(role="system", content=VISION_PROMPT),
                        ChatMessage(role="user", content=question, images=(payload,)),
                    ),
                    task="vision",
                )
            )
            answer = str(getattr(response, "content", "") or "").strip()
        except (RuntimeError, ValueError, OSError, TimeoutError) as error:
            answer = ""
            failure = str(error)
        else:
            failure = ""
        if not bool(request.arguments.get("keep", True)):
            try:
                destination.unlink()
            except OSError:
                pass
        source = Source(
            "screen_look",
            "tool",
            str(destination) if destination.exists() else "screen",
        )
        results: list[Result] = []
        if destination.exists():
            results.append(
                image(
                    mime_type="image/png",
                    uri=str(destination),
                    source=source,
                    alt="screenshot",
                    title=f"Screen at {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]} UTC",
                )
            )
        else:
            results.append(
                image(
                    mime_type="image/png",
                    base64=base64.b64encode(payload).decode("ascii"),
                    source=source,
                    alt="screenshot",
                    title="Screen",
                )
            )
        if failure:
            message = f"The screenshot was taken but the vision model did not answer: {failure}"
            results.append(status("warning", message, source=source))
            return ActionResult(
                status="failed",
                message=message,
                action=self.name,
                error="vision_failed",
                results=tuple(results),
            )
        elapsed = time.perf_counter() - started
        results.append(
            text_result(
                answer, source=source, title="What the vision model saw", format="text"
            )
        )
        return ActionResult(
            status="success",
            message=answer
            + f"\n(vision model answered in {elapsed:.0f} s"
            + (f"; screenshot kept at {destination}" if destination.exists() else "")
            + ")",
            action=self.name,
            resolved_target=str(destination),
            results=tuple(results),
        )


SCREEN_ACTIONS = (ScreenLookAction,)

__all__ = ["SCREEN_ACTIONS", "ScreenLookAction", "capture_screen"]
