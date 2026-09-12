# File: core/tools/mcp_client.py

"""MCP servers as tools.

Each configured server runs as a child process on its own thread with its own
event loop (the ``mcp`` SDK is async, Iris is not). Its tools are registered
into the shared ``ToolRegistry`` as ordinary actions, so they pass through the
same validate, preview, confirm, audit spine as everything else.

Permission and confirmation follow the server's tool annotations: a tool that
declares itself read-only runs without confirmation, everything else asks
first. ``confirm`` and ``no_confirm`` in the server's config override that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.actions.models import ActionRequest, ActionResult, ConfirmationPreview, ValidationResult
from core.actions.registry import ActionRegistry
from core.results.mcp import from_mcp
from core.results.models import Source
from core.tools.models import PermissionLevel, ToolDefinition

logger = logging.getLogger(__name__)

try:  # optional: a transitive dependency today, used when present
    import jsonschema as _jsonschema
except ImportError:  # pragma: no cover - environment without jsonschema
    _jsonschema = None


# -- configuration ----------------------------------------------------------

_LOCAL_BIN_FOLDERS = (
    Path.home() / ".local" / "bin",
    Path.home() / ".cargo" / "bin",
)


def resolve_command(command: str) -> str:
    """Find ``command`` on PATH, then in the usual per-user bin folders.

    ``uv`` installs ``uvx`` into ``~/.local/bin`` which is not on PATH for
    every shell on this machine, so a bare name must be resolved explicitly.
    """
    if os.path.isabs(command) and Path(command).exists():
        return command
    found = shutil.which(command)
    if found:
        return found
    for folder in _LOCAL_BIN_FOLDERS:
        for suffix in ("", ".exe", ".cmd", ".bat"):
            candidate = folder / f"{command}{suffix}"
            if candidate.exists():
                return str(candidate)
    return command


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    cwd: str | None = None
    enabled: bool = True
    bind: dict[str, Any] = field(default_factory=dict)
    confirm: tuple[str, ...] = ()
    no_confirm: tuple[str, ...] = ()
    expose_to_model: bool = True
    timeout_seconds: float = 60.0
    ready_timeout_seconds: float = 30.0

    @classmethod
    def from_config(cls, name: str, payload: dict[str, Any]) -> "McpServerConfig":
        command = str(payload.get("command", "")).strip()
        if not command:
            raise ValueError(f"MCP server {name} has no command")
        env_raw = payload.get("env")
        env = {str(k): str(v) for k, v in env_raw.items()} if isinstance(env_raw, dict) else None
        bind_raw = payload.get("bind", {})
        return cls(
            name=name,
            command=command,
            args=tuple(str(item) for item in payload.get("args", []) or []),
            env=env,
            cwd=str(payload["cwd"]) if payload.get("cwd") else None,
            enabled=bool(payload.get("enabled", True)),
            bind=dict(bind_raw) if isinstance(bind_raw, dict) else {},
            confirm=tuple(str(item) for item in payload.get("confirm", []) or []),
            no_confirm=tuple(str(item) for item in payload.get("no_confirm", []) or []),
            expose_to_model=bool(payload.get("expose_to_model", True)),
            timeout_seconds=float(payload.get("timeout_seconds", 60.0)),
            ready_timeout_seconds=float(payload.get("ready_timeout_seconds", 30.0)),
        )


def load_server_configs(section: Any) -> list[McpServerConfig]:
    """Read the ``mcp_servers`` block of config.json."""
    configs: list[McpServerConfig] = []
    if not isinstance(section, dict):
        return configs
    for name, payload in section.items():
        if not isinstance(payload, dict):
            continue
        try:
            configs.append(McpServerConfig.from_config(str(name), payload))
        except ValueError as error:
            logger.warning("Skipping MCP server %s: %s", name, error)
    return configs


# -- the server process -------------------------------------------------------

@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool | None:
        value = self.annotations.get("read_only_hint", self.annotations.get("readOnlyHint"))
        return bool(value) if value is not None else None

    @property
    def destructive(self) -> bool | None:
        value = self.annotations.get("destructive_hint", self.annotations.get("destructiveHint"))
        return bool(value) if value is not None else None


@dataclass(frozen=True)
class McpCallResult:
    text: str
    is_error: bool = False
    structured: Any = None
    #: The content blocks as Iris results (2.7): text, code, image, link, file.
    #: ``text`` above is what the model reads; this is what a client renders.
    results: tuple[Any, ...] = ()


class McpServerError(Exception):
    pass


class McpServerConnection:
    """One running MCP server, driven from a private thread and event loop."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._session: Any = None
        self._tools: list[McpTool] = []
        self._error: BaseException | None = None
        self.server_name: str = ""
        self.server_version: str = ""

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, name=f"mcp-{self.config.name}", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.config.ready_timeout_seconds):
            self._error = McpServerError(f"MCP server {self.config.name} did not become ready in {self.config.ready_timeout_seconds:.0f}s")
            self.stop()
            raise self._error
        if self._error is not None:
            raise McpServerError(f"MCP server {self.config.name} failed to start: {self._error}") from self._error

    def stop(self, join_timeout: float = 10.0) -> None:
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and stop_event is not None and loop.is_running():
            loop.call_soon_threadsafe(stop_event.set)
        if self._thread is not None:
            self._thread.join(join_timeout)
        self._thread = None
        self._session = None

    @property
    def running(self) -> bool:
        return self._session is not None and self._error is None

    @property
    def tools(self) -> list[McpTool]:
        return list(self._tools)

    # -- calls ------------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any] | None = None, timeout: float | None = None) -> McpCallResult:
        if self._loop is None or self._session is None:
            raise McpServerError(f"MCP server {self.config.name} is not running")
        future = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, dict(arguments or {})), self._loop)
        try:
            result = future.result(timeout if timeout is not None else self.config.timeout_seconds)
        except TimeoutError as error:
            future.cancel()
            raise McpServerError(f"{name} timed out on MCP server {self.config.name}") from error
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            raise McpServerError(f"{name} failed on MCP server {self.config.name}: {error}") from error
        content = getattr(result, "content", None) or []
        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
        structured = getattr(result, "structured_content", None)
        if not parts and structured is not None:
            parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
        results = from_mcp(content, source=Source(f"{self.config.name}:{name}", "mcp"), structured=structured)
        return McpCallResult(
            text="\n".join(parts),
            is_error=bool(getattr(result, "is_error", False)),
            structured=structured,
            results=results,
        )

    # -- internals --------------------------------------------------------

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except (OSError, ValueError, RuntimeError, TypeError):
                pass
            loop.close()
            self._loop = None

    async def _run(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self._stop_event = asyncio.Event()
        try:
            params = StdioServerParameters(
                command=resolve_command(self.config.command),
                args=list(self.config.args),
                env=self.config.env,
                cwd=self.config.cwd,
            )
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    info = await session.initialize()
                    server_info = getattr(info, "server_info", None)
                    self.server_name = str(getattr(server_info, "name", "") or "")
                    self.server_version = str(getattr(server_info, "version", "") or "")
                    listed = await session.list_tools()
                    self._tools = [self._to_tool(item) for item in getattr(listed, "tools", [])]
                    self._session = session
                    self._ready.set()
                    await self._stop_event.wait()
        except (OSError, ValueError, RuntimeError, TypeError) as error:  # noqa: BLE001 - reported to the starting thread
            self._error = error
            self._ready.set()
        finally:
            self._session = None

    @staticmethod
    def _to_tool(item: Any) -> McpTool:
        annotations_obj = getattr(item, "annotations", None)
        annotations: dict[str, Any] = {}
        if annotations_obj is not None:
            dump = getattr(annotations_obj, "model_dump", None)
            annotations = dump(exclude_none=True) if callable(dump) else dict(annotations_obj)
        schema = getattr(item, "input_schema", None) or getattr(item, "inputSchema", None) or {}
        return McpTool(
            name=str(item.name),
            description=str(getattr(item, "description", "") or ""),
            input_schema=dict(schema),
            annotations=annotations,
        )


# -- tools as actions ---------------------------------------------------------

def permission_for(tool: McpTool, config: McpServerConfig) -> tuple[PermissionLevel, bool]:
    """Map a tool's annotations (and the server's overrides) to Iris's terms."""
    read_only = tool.read_only
    if read_only:
        permission = PermissionLevel.READ
    elif tool.destructive:
        permission = PermissionLevel.EXECUTE
    else:
        permission = PermissionLevel.WRITE
    if tool.name in config.confirm:
        requires_confirmation = True
    elif tool.name in config.no_confirm:
        requires_confirmation = False
    else:
        requires_confirmation = read_only is not True
    return permission, requires_confirmation


def exposed_schema(schema: dict[str, Any], bound: dict[str, Any]) -> dict[str, Any]:
    """The tool's schema minus the arguments Iris fills in itself."""
    result = json.loads(json.dumps(schema))
    result.pop("title", None)
    properties = result.get("properties")
    if isinstance(properties, dict):
        for key in bound:
            properties.pop(key, None)
        for prop in properties.values():
            if isinstance(prop, dict):
                prop.pop("title", None)
    required = result.get("required")
    if isinstance(required, list):
        result["required"] = [item for item in required if item not in bound]
    result.setdefault("type", "object")
    return result


def schema_errors(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    if _jsonschema is not None:
        try:
            validator = _jsonschema.Draft202012Validator(schema)
            return [
                (".".join(str(piece) for piece in error.absolute_path) or "arguments") + ": " + error.message
                for error in sorted(validator.iter_errors(arguments), key=lambda item: list(item.absolute_path))
            ]
        except (OSError, ValueError, RuntimeError, TypeError) as error:  # a schema we cannot compile is not the user's fault
            logger.debug("jsonschema could not validate %s: %s", schema, error)
    missing = [key for key in schema.get("required", []) if key not in arguments]
    return [f"{key}: required" for key in missing]


class McpToolAction:
    """An MCP tool wearing the ``Action`` interface so the executor can run it."""

    def __init__(self, connection: McpServerConnection, tool: McpTool, registered_name: str) -> None:
        self.connection = connection
        self.tool = tool
        self.name = registered_name
        config = connection.config
        permission, requires_confirmation = permission_for(tool, config)
        description = tool.description.strip() or f"{tool.name} on the {config.name} MCP server"
        self.definition = ToolDefinition(
            name=registered_name,
            description=description,
            parameters=exposed_schema(tool.input_schema, config.bind),
            permission=permission,
            requires_confirmation=requires_confirmation,
            expose_to_model=config.expose_to_model,
            source=f"mcp:{config.name}",
        )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        arguments = dict(request.arguments)
        arguments.update(self.connection.config.bind)
        errors = schema_errors(self.tool.input_schema, arguments)
        if errors:
            return ValidationResult(ok=False, error="; ".join(errors))
        if not self.connection.running:
            return ValidationResult(ok=False, error=f"MCP server {self.connection.config.name} is not running")
        visible = {key: value for key, value in arguments.items() if key not in self.connection.config.bind}
        target = f"{self.connection.config.name}:{self.tool.name}"
        preview = ConfirmationPreview(
            title=f"Run {self.tool.name}",
            summary=f"Run {self.tool.name} on the {self.connection.config.name} server with {json.dumps(visible, ensure_ascii=False)}",
            target=target,
            impact=self._impact(),
            metadata={"server": self.connection.config.name, "tool": self.tool.name, "arguments": arguments},
        )
        return ValidationResult(
            ok=True,
            resolved_target=target,
            resolved_arguments=arguments,
            confirmation_preview=preview if self.definition.requires_confirmation else None,
        )

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        target = f"{self.connection.config.name}:{self.tool.name}"
        try:
            result = self.connection.call(self.tool.name, request.arguments)
        except McpServerError as error:
            return ActionResult(status="failed", message=str(error), action=self.name, resolved_target=target, error="mcp_call_failed")
        if result.is_error:
            return ActionResult(
                status="failed",
                message=result.text or f"{self.tool.name} reported an error",
                action=self.name,
                resolved_target=target,
                error="mcp_tool_error",
            )
        return ActionResult(
            status="success",
            message=result.text or "(no output)",
            action=self.name,
            resolved_target=target,
            results=result.results,
        )

    def _impact(self) -> str:
        if self.tool.destructive:
            return "Destructive: this may discard or overwrite data."
        if self.tool.read_only:
            return "Read-only."
        return "Makes a change."


# -- manager ------------------------------------------------------------------

class McpManager:
    """Starts the configured servers and registers their tools."""

    def __init__(
        self,
        configs: list[McpServerConfig],
        action_registry: ActionRegistry,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.configs = configs
        self.action_registry = action_registry
        self.on_event = on_event
        self.connections: dict[str, McpServerConnection] = {}
        self.errors: dict[str, str] = {}
        self.registered: dict[str, list[str]] = {}
        self._thread: threading.Thread | None = None

    def start(self, background: bool = True) -> None:
        if not self.configs:
            return
        if background:
            self._thread = threading.Thread(target=self._start_all, name="mcp-startup", daemon=True)
            self._thread.start()
        else:
            self._start_all()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def stop(self) -> None:
        for connection in self.connections.values():
            try:
                connection.stop()
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                logger.warning("Stopping MCP server %s failed: %s", connection.config.name, error)
        self.connections.clear()

    def status(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for config in self.configs:
            connection = self.connections.get(config.name)
            rows.append(
                {
                    "server": config.name,
                    "enabled": config.enabled,
                    "running": bool(connection and connection.running),
                    "tools": list(self.registered.get(config.name, [])),
                    "error": self.errors.get(config.name),
                    "version": connection.server_version if connection else "",
                }
            )
        return rows

    def _start_all(self) -> None:
        for config in self.configs:
            if not config.enabled:
                continue
            connection = McpServerConnection(config)
            try:
                connection.start()
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                self.errors[config.name] = str(error)
                logger.warning("MCP server %s unavailable: %s", config.name, error)
                self._notify(f"MCP server {config.name} is unavailable: {error}")
                continue
            self.connections[config.name] = connection
            names: list[str] = []
            for tool in connection.tools:
                registered_name = self._unique_name(config.name, tool.name)
                try:
                    self.action_registry.register(McpToolAction(connection, tool, registered_name))
                except ValueError as error:
                    logger.warning("Could not register MCP tool %s: %s", tool.name, error)
                    continue
                names.append(registered_name)
            self.registered[config.name] = names
            logger.info("MCP server %s ready with %d tools", config.name, len(names))

    def _unique_name(self, server: str, tool: str) -> str:
        if tool not in self.action_registry.tools:
            return tool
        return f"{server}_{tool}"

    def _notify(self, message: str) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(message)
        except (OSError, ValueError, RuntimeError, TypeError):
            pass
