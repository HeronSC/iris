# File: core/actions/implementations/code_tools.py

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.code.search import SearchToolMissing
from core.code.symbols import Symbol
from core.code.workspace import ALWorkspace
from core.results.models import Result, Source, code, file, status, table
from core.results.models import text as text_result
from core.tools.models import PermissionLevel, ToolDefinition

NO_WORKSPACE = "No AL workspace is in view. Say which folder, or open the project in VS Code so Iris can see it."
NO_SERVICE = "Code awareness is not running in this host."


def _service(context: object) -> Any | None:
    return getattr(context, "code_service", None)


def _resolve(service: Any, path: str | None) -> tuple[ALWorkspace | None, str | None]:
    if path and path.strip() and not service.within_roots(path) and service.workspace_named(path) is None:
        return None, f"{path} is outside the folders Iris may read ({', '.join(str(root) for root in service.roots)})."
    workspace = service.resolve_workspace(path)
    if workspace is None:
        return None, NO_WORKSPACE if not path else f"No app.json was found at or above {path}."
    return workspace, None


class RepoSearchArguments(BaseModel):
    pattern: str = Field(description="The text or regular expression to find in the code")
    path: str | None = Field(default=None, description="Folder or workspace name to search; the AL workspace in view when omitted")
    glob: str | None = Field(default=None, description="Only files matching this glob, such as *.al or src/Codeunits/**")
    regex: bool = Field(default=False, description="Treat the pattern as a regular expression")
    case_sensitive: bool = Field(default=False)
    max_results: int = Field(default=40, ge=1, le=200)


class RepoSearchAction:

    name = "repo_search"
    definition = ToolDefinition(
        name="repo_search",
        description="Search source code for text or a regular expression with ripgrep, inside the AL workspace in view or a named folder. Returns file, line and the matching line.",
        arguments=RepoSearchArguments,
        permission=PermissionLevel.READ,
        cost="seconds",
        keywords=("grep", "search the code", "search code", "in the code", "in the repo", "where is", "find usages", "references to", "who calls", "uses of"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = RepoSearchArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        if not arguments.pattern.strip():
            return ValidationResult(ok=False, error="The search pattern is empty")
        service = _service(context)
        if service is None:
            return ValidationResult(ok=False, error=NO_SERVICE)
        workspace, problem = _resolve(service, arguments.path)
        if problem:
            return ValidationResult(ok=False, error=problem)
        resolved = arguments.model_dump()
        resolved["path"] = str(workspace.root)
        return ValidationResult(ok=True, resolved_target=str(workspace.root), resolved_arguments=resolved)

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service = _service(context)
        arguments = request.arguments
        root = Path(str(arguments.get("path") or ""))
        pattern = str(arguments.get("pattern") or "")
        try:
            outcome = service.search_text(
                pattern,
                root,
                glob=arguments.get("glob") or None,
                regex=bool(arguments.get("regex")),
                case_sensitive=bool(arguments.get("case_sensitive")),
                max_results=int(arguments.get("max_results") or 40),
            )
        except SearchToolMissing as error:
            return ActionResult(status="failed", message=str(error), action=self.name, resolved_target=str(root), error="rg_missing")
        except (TimeoutError, RuntimeError) as error:
            return ActionResult(status="failed", message=str(error), action=self.name, resolved_target=str(root), error="search_failed")
        source = Source("repo_search", "tool", str(root))
        if not outcome.matches:
            message = f"No matches for {pattern!r} under {root}."
            return ActionResult(status="success", message=message, action=self.name, resolved_target=str(root), results=(status("ok", message, source=source),))
        lines = [f"{len(outcome.matches)} match{'es' if len(outcome.matches) != 1 else ''} for {pattern!r} in {len(outcome.files)} file{'s' if len(outcome.files) != 1 else ''} under {root}" + (" (more not shown)" if outcome.truncated else "")]
        for match in outcome.matches:
            lines.append(f"{match.file}:{match.line}: {match.text.strip()[:200]}")
        rows = [(match.file, match.line, match.text.strip()[:200]) for match in outcome.matches]
        results: list[Result] = [table(("file", "line", "text"), rows, source=source, title=f"Matches for {pattern}")]
        if outcome.truncated:
            results.append(status("warning", f"Only the first {len(outcome.matches)} matches are shown; narrow the pattern or add a glob.", source=source))
        return ActionResult(status="success", message="\n".join(lines), action=self.name, resolved_target=str(root), results=tuple(results))


class ALWorkspaceArguments(BaseModel):
    path: str | None = Field(default=None, description="Folder inside the workspace or the workspace name; the one in view when omitted")


class ALWorkspaceAction:

    name = "al_workspace"
    definition = ToolDefinition(
        name="al_workspace",
        description="Describe the Business Central AL workspace: app name, publisher, version, target platform, dependencies, id ranges, launch targets, symbol packages, and how many objects of each kind it defines.",
        arguments=ALWorkspaceArguments,
        permission=PermissionLevel.READ,
        keywords=("app.json", "launch.json", "alpackages", "workspace", "this extension", "this app", "which app", "dependencies", "id range", "business central project"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = ALWorkspaceArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        service = _service(context)
        if service is None:
            return ValidationResult(ok=False, error=NO_SERVICE)
        workspace, problem = _resolve(service, arguments.path)
        if problem:
            return ValidationResult(ok=False, error=problem)
        return ValidationResult(ok=True, resolved_target=str(workspace.root), resolved_arguments={"path": str(workspace.root)})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service = _service(context)
        workspace = service.workspace_at(str(request.arguments.get("path") or ""))
        if workspace is None:
            return ActionResult(status="failed", message=NO_WORKSPACE, action=self.name, error="no_workspace")
        counts = service.index_for(workspace).counts()
        description = workspace.describe()
        if counts:
            description += "\nDefines " + ", ".join(f"{count} {kind}{'s' if count != 1 else ''}" for kind, count in counts.items())
        source = Source("al_workspace", "tool", str(workspace.root / "app.json"))
        results: list[Result] = [text_result(description, source=source, title=workspace.name, format="text")]
        if workspace.dependencies:
            results.append(table(("dependency", "publisher", "version"), [(item.get("name"), item.get("publisher"), item.get("version")) for item in workspace.dependencies], source=source, title="Dependencies"))
        if workspace.packages:
            results.append(table(("package", "publisher", "version"), [(item.name, item.publisher, item.version) for item in workspace.packages], source=source, title="Symbol packages"))
        if counts:
            results.append(table(("object kind", "count"), list(counts.items()), source=source, title="Objects in this workspace"))
        return ActionResult(status="success", message=description, action=self.name, resolved_target=str(workspace.root), results=tuple(results))


class ALSymbolArguments(BaseModel):
    name: str = Field(description="Object name or id, such as Customer, 18, 'Sales-Post', or a prefix like ELEPCompanyCam")
    kind: Literal["table", "page", "codeunit", "report", "query", "xmlport", "enum", "interface", "permissionset", "tableextension", "pageextension", "enumextension", "reportextension"] | None = Field(default=None)
    detail: Literal["summary", "fields", "events", "procedures", "all"] = Field(default="summary", description="What to include: fields of a table, events an object publishes with their subscribers, procedures, or everything")
    path: str | None = Field(default=None, description="Workspace folder or name; the one in view when omitted")


class ALSymbolAction:

    name = "al_symbol"
    definition = ToolDefinition(
        name="al_symbol",
        description=(
            "Look up a Business Central AL object by name or id in the workspace's own sources and its .alpackages symbols: kind, id, fields, "
            "procedures, the events it publishes, who subscribes to them in this workspace, and which extensions target it."
        ),
        arguments=ALSymbolArguments,
        permission=PermissionLevel.READ,
        keywords=("table", "codeunit", "page", "field", "fields of", "events of", "subscribers", "subscribes", "event subscriber", "integration event", "which object", "what is codeunit", "what is table", "al object", "extends"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = ALSymbolArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        if not arguments.name.strip():
            return ValidationResult(ok=False, error="The object name is empty")
        service = _service(context)
        if service is None:
            return ValidationResult(ok=False, error=NO_SERVICE)
        workspace, problem = _resolve(service, arguments.path)
        if problem:
            return ValidationResult(ok=False, error=problem)
        resolved = arguments.model_dump()
        resolved["path"] = str(workspace.root)
        return ValidationResult(ok=True, resolved_target=f"{arguments.kind or 'object'} {arguments.name.strip()}", resolved_arguments=resolved)

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service = _service(context)
        arguments = request.arguments
        workspace = service.workspace_at(str(arguments.get("path") or ""))
        if workspace is None:
            return ActionResult(status="failed", message=NO_WORKSPACE, action=self.name, error="no_workspace")
        index = service.index_for(workspace)
        query = str(arguments.get("name") or "")
        matches = index.find(query, kind=arguments.get("kind") or None, limit=8)
        source = Source("al_symbol", "tool", str(workspace.root))
        if not matches:
            message = f"No object named {query!r} in {workspace.name} or its symbol packages."
            return ActionResult(status="success", message=message, action=self.name, results=(status("warning", message, source=source),))
        detail = str(arguments.get("detail") or "summary")
        if len(matches) > 1 and matches[0].name.lower() != query.strip().strip('"').lower() and not query.strip().isdigit():
            lines = [f"{len(matches)} objects match {query!r}:"] + [f"- {item.label} ({item.app})" for item in matches]
            rows = [(item.kind, item.id, item.name, item.app, item.source) for item in matches]
            return ActionResult(status="success", message="\n".join(lines), action=self.name, results=(table(("kind", "id", "name", "app", "source"), rows, source=source, title=f"Objects matching {query}"),))
        symbol = matches[0]
        message, results = self._describe(symbol, index, detail, source)
        return ActionResult(status="success", message=message, action=self.name, resolved_target=symbol.label, results=tuple(results))

    def _describe(self, symbol: Symbol, index: Any, detail: str, source: Source) -> tuple[str, list[Result]]:
        lines = [f"{symbol.label} in {symbol.app}" + (f" ({symbol.source})" if symbol.source else "")]
        if symbol.namespace:
            lines.append(f"Namespace {symbol.namespace}")
        if symbol.implements:
            lines.append("Implements " + ", ".join(symbol.implements))
        for key in ("Caption", "SourceTable", "PageType", "Subtype", "TableType"):
            if symbol.properties.get(key):
                lines.append(f"{key}: {symbol.properties[key]}")
        events = symbol.events
        procedures = [method for method in symbol.methods if not method.event]
        summary_bits = []
        if symbol.fields:
            summary_bits.append(f"{len(symbol.fields)} {'values' if symbol.kind == 'enum' else 'fields'}")
        if procedures:
            summary_bits.append(f"{len(procedures)} procedures")
        if events:
            summary_bits.append(f"{len(events)} events")
        if summary_bits:
            lines.append(", ".join(summary_bits))
        results: list[Result] = []
        extensions = index.extensions_of(symbol.name)
        if extensions:
            lines.append("Extended by " + ", ".join(f"{item.label} ({item.app})" for item in extensions[:10]))
        subscribers = index.subscribers_to(symbol.name)
        if detail in {"fields", "all"} and symbol.fields:
            rows = [(item.id, item.name, item.type) for item in symbol.fields[:150]]
            results.append(table(("id", "name", "type"), rows, source=source, title=f"Fields of {symbol.name}"))
            lines.append("Fields: " + ", ".join(f"{item.name} ({item.type})" for item in symbol.fields[:40]) + (" …" if len(symbol.fields) > 40 else ""))
        if detail in {"events", "all"} and events:
            rows = [(item.event, item.signature) for item in events[:150]]
            results.append(table(("kind", "event"), rows, source=source, title=f"Events published by {symbol.name}"))
            lines.append("Events: " + ", ".join(item.name for item in events[:40]) + (" …" if len(events) > 40 else ""))
        if detail in {"events", "all", "summary"} and subscribers:
            rows = [(owner.label, subscription.event, subscription.procedure, owner.source) for owner, subscription in subscribers[:100]]
            results.append(table(("subscriber", "event", "procedure", "file"), rows, source=source, title=f"Subscribers to {symbol.name} in this workspace"))
            lines.append("Subscribed in this workspace: " + "; ".join(f"{owner.name}.{subscription.procedure} on {subscription.event}" for owner, subscription in subscribers[:20]))
        if detail in {"procedures", "all"} and procedures:
            lines.append("Procedures: " + ", ".join(item.signature for item in procedures[:40]) + (" …" if len(procedures) > 40 else ""))
            results.append(code("\n".join(("local " if item.local else "") + "procedure " + item.signature for item in procedures[:150]), source=source, language="al", title=f"Procedures of {symbol.name}"))
        if symbol.subscriptions:
            lines.append("Subscribes to: " + "; ".join(f"{item.object_name}.{item.event} ({item.procedure})" for item in symbol.subscriptions[:20]))
        results.insert(0, text_result("\n".join(lines), source=source, title=symbol.label, format="text"))
        return "\n".join(lines), results


class ALCompileArguments(BaseModel):
    path: str | None = Field(default=None, description="Workspace folder or name; the one in view when omitted")
    analyzers: bool = Field(default=True, description="Also run the workspace's code analyzers (CodeCop, UICop, PerTenantExtensionCop) with its ruleset")
    max_diagnostics: int = Field(default=40, ge=1, le=200)


class ALCompileAction:

    name = "al_compile"
    definition = ToolDefinition(
        name="al_compile",
        description="Compile the Business Central AL workspace with the AL compiler from the VS Code extension and return every error and warning with file, line and code. The .app goes to Iris's own build folder; the workspace is not touched.",
        arguments=ALCompileArguments,
        permission=PermissionLevel.EXECUTE,
        timeout_seconds=660.0,
        cost="seconds to a minute; runs the compiler",
        keywords=("compile", "build the extension", "build the app", "does it compile", "compiler errors", "alc", "code analysis", "codecop"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = ALCompileArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        service = _service(context)
        if service is None:
            return ValidationResult(ok=False, error=NO_SERVICE)
        if not service.compiler.available:
            return ValidationResult(ok=False, error="alc.exe was not found; install the AL Language extension for VS Code or set code.alc_path in config.json")
        workspace, problem = _resolve(service, arguments.path)
        if problem:
            return ValidationResult(ok=False, error=problem)
        return ValidationResult(ok=True, resolved_target=str(workspace.root), resolved_arguments={"path": str(workspace.root), "analyzers": arguments.analyzers, "max_diagnostics": arguments.max_diagnostics})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service = _service(context)
        workspace = service.workspace_at(str(request.arguments.get("path") or ""))
        if workspace is None:
            return ActionResult(status="failed", message=NO_WORKSPACE, action=self.name, error="no_workspace")
        try:
            report = service.compile(workspace, analyzers=bool(request.arguments.get("analyzers", True)))
        except FileNotFoundError as error:
            return ActionResult(status="failed", message=str(error), action=self.name, error="alc_missing")
        except Exception as error:
            return ActionResult(status="failed", message=f"Compiling {workspace.name} failed to run: {error}", action=self.name, error="compile_failed")
        source = Source("al_compile", "tool", str(workspace.root))
        limit = int(request.arguments.get("max_diagnostics") or 40)
        shown = list(report.errors) + list(report.warnings)
        lines = [report.summary()]
        for item in shown[:limit]:
            lines.append(item.describe())
        if len(shown) > limit:
            lines.append(f"... {len(shown) - limit} more not shown")
        if report.timed_out:
            lines.append("The compiler did not finish; try again without analyzers or check the workspace in VS Code.")
        results: list[Result] = [status("ok" if report.ok else ("warning" if report.timed_out else "error"), report.summary(), source=source)]
        if shown:
            rows = [(item.severity, item.code, item.file, item.line or "", item.message) for item in shown[:limit]]
            results.append(table(("severity", "code", "file", "line", "message"), rows, source=source, title=f"Diagnostics for {workspace.name}"))
        if report.output_path:
            results.append(file(report.output_path, source=source, title="Built package"))
        return ActionResult(status="success" if not report.timed_out else "failed", message="\n".join(lines), action=self.name, resolved_target=str(workspace.root), results=tuple(results), error=None if not report.timed_out else "timeout")


CODE_ACTIONS = (RepoSearchAction, ALWorkspaceAction, ALSymbolAction, ALCompileAction)

__all__ = ["ALCompileAction", "ALSymbolAction", "ALWorkspaceAction", "CODE_ACTIONS", "RepoSearchAction"]
