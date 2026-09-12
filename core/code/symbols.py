# File: core/code/symbols.py

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from core.code.workspace import ALWorkspace, PackageInfo, parse_package_name

logger = logging.getLogger(__name__)

EVENT_ATTRIBUTES = {"IntegrationEvent", "BusinessEvent", "InternalEvent"}

PACKAGE_COLLECTIONS: dict[str, str] = {
    "Tables": "table",
    "Pages": "page",
    "Codeunits": "codeunit",
    "Reports": "report",
    "Queries": "query",
    "XmlPorts": "xmlport",
    "EnumTypes": "enum",
    "Interfaces": "interface",
    "PermissionSets": "permissionset",
    "TableExtensions": "tableextension",
    "PageExtensions": "pageextension",
    "EnumExtensions": "enumextension",
    "ReportExtensions": "reportextension",
    "PermissionSetExtensions": "permissionsetextension",
    "ControlAddIns": "controladdin",
    "Profiles": "profile",
}

OBJECT_HEADER = re.compile(
    r"^\s*(?P<kind>table|page|codeunit|report|query|xmlport|enum|interface|permissionset|tableextension|pageextension|enumextension|reportextension|permissionsetextension|pagecustomization|profile|controladdin|entitlement)"
    r"\s+(?P<id>\d+)?\s*(?P<name>\"[^\"]+\"|[A-Za-z_][\w]*)"
    r"(?:\s+extends\s+(?P<target>\"[^\"]+\"|[A-Za-z_][\w]*))?"
    r"(?:\s+implements\s+(?P<implements>[^{\r\n]+))?",
    re.IGNORECASE | re.MULTILINE,
)
PROCEDURE = re.compile(r"^\s*(?P<local>local\s+|internal\s+|protected\s+)?procedure\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)", re.IGNORECASE | re.MULTILINE)
ATTRIBUTE = re.compile(r"^\s*\[(?P<name>[A-Za-z]+)\s*(?:\((?P<args>.*?)\))?\]\s*$", re.MULTILINE)
SUBSCRIBER_ARGS = re.compile(
    r"ObjectType::(?P<object_type>\w+)\s*,\s*(?:(?P<type_prefix>\w+)::)?(?P<object>\"[^\"]+\"|[\w.]+)\s*,\s*'(?P<event>[^']*)'\s*(?:,\s*'(?P<element>[^']*)')?",
    re.IGNORECASE,
)


def _unquote(value: str | None) -> str:
    if value is None:
        return ""
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


@dataclass(frozen=True)
class SymbolField:
    id: int | None
    name: str
    type: str

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "type": self.type}


@dataclass(frozen=True)
class SymbolMethod:
    name: str
    event: str | None = None
    parameters: tuple[str, ...] = ()
    local: bool = False

    @property
    def signature(self) -> str:
        return f"{self.name}({', '.join(self.parameters)})"

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "parameters": list(self.parameters)}
        if self.event:
            payload["event"] = self.event
        if self.local:
            payload["local"] = True
        return payload


@dataclass(frozen=True)
class Subscription:
    object_type: str
    object_name: str
    event: str
    element: str = ""
    procedure: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"object_type": self.object_type, "object_name": self.object_name, "event": self.event, "element": self.element, "procedure": self.procedure}


@dataclass(frozen=True)
class Symbol:
    kind: str
    id: int | None
    name: str
    app: str
    source: str = ""
    namespace: str = ""
    target: str = ""
    implements: tuple[str, ...] = ()
    fields: tuple[SymbolField, ...] = ()
    methods: tuple[SymbolMethod, ...] = ()
    subscriptions: tuple[Subscription, ...] = ()
    properties: dict[str, str] = field(default_factory=dict)
    origin: str = "package"

    @property
    def label(self) -> str:
        head = f"{self.kind} {self.id} {self.name}" if self.id is not None else f"{self.kind} {self.name}"
        return head + (f" extends {self.target}" if self.target else "")

    @property
    def events(self) -> tuple[SymbolMethod, ...]:
        return tuple(method for method in self.methods if method.event)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "app": self.app,
            "source": self.source,
            "namespace": self.namespace,
            "target": self.target,
            "implements": list(self.implements),
            "fields": [item.to_json() for item in self.fields],
            "methods": [item.to_json() for item in self.methods],
            "subscriptions": [item.to_json() for item in self.subscriptions],
            "properties": dict(self.properties),
            "origin": self.origin,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Symbol":
        return cls(
            kind=str(payload.get("kind") or ""),
            id=payload.get("id"),
            name=str(payload.get("name") or ""),
            app=str(payload.get("app") or ""),
            source=str(payload.get("source") or ""),
            namespace=str(payload.get("namespace") or ""),
            target=str(payload.get("target") or ""),
            implements=tuple(payload.get("implements") or ()),
            fields=tuple(SymbolField(item.get("id"), str(item.get("name") or ""), str(item.get("type") or "")) for item in payload.get("fields") or ()),
            methods=tuple(
                SymbolMethod(str(item.get("name") or ""), item.get("event"), tuple(item.get("parameters") or ()), bool(item.get("local")))
                for item in payload.get("methods") or ()
            ),
            subscriptions=tuple(
                Subscription(str(item.get("object_type") or ""), str(item.get("object_name") or ""), str(item.get("event") or ""), str(item.get("element") or ""), str(item.get("procedure") or ""))
                for item in payload.get("subscriptions") or ()
            ),
            properties=dict(payload.get("properties") or {}),
            origin=str(payload.get("origin") or "package"),
        )


@dataclass(frozen=True)
class PackageSymbols:
    app_id: str
    name: str
    publisher: str
    version: str
    symbols: tuple[Symbol, ...]

    def to_json(self) -> dict[str, Any]:
        return {"app_id": self.app_id, "name": self.name, "publisher": self.publisher, "version": self.version, "symbols": [item.to_json() for item in self.symbols]}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "PackageSymbols":
        return cls(
            app_id=str(payload.get("app_id") or ""),
            name=str(payload.get("name") or ""),
            publisher=str(payload.get("publisher") or ""),
            version=str(payload.get("version") or ""),
            symbols=tuple(Symbol.from_json(item) for item in payload.get("symbols") or ()),
        )


def read_symbol_reference(app_path: str | Path) -> dict[str, Any]:
    raw = Path(app_path).read_bytes()
    start = raw.find(b"PK\x03\x04")
    if start < 0:
        raise ValueError(f"{app_path} is not an AL package (no zip content)")
    with zipfile.ZipFile(io.BytesIO(raw[start:])) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith("symbolreference.json")]
        if not names:
            raise ValueError(f"{app_path} has no SymbolReference.json")
        return json.loads(archive.read(names[0]).decode("utf-8-sig"))


def _type_name(definition: Any) -> str:
    if not isinstance(definition, dict):
        return ""
    name = str(definition.get("Name") or "")
    subtype = definition.get("Subtype")
    if isinstance(subtype, dict) and subtype.get("Name"):
        return f"{name} {subtype['Name']}".strip()
    return name


def _method_from_json(item: dict[str, Any]) -> SymbolMethod:
    event = None
    for attribute in item.get("Attributes") or []:
        name = str(attribute.get("Name") or "")
        if name in EVENT_ATTRIBUTES:
            event = name
    parameters = tuple(
        f"{parameter.get('Name')}: {_type_name(parameter.get('TypeDefinition'))}".strip(": ")
        for parameter in item.get("Parameters") or []
        if isinstance(parameter, dict)
    )
    return SymbolMethod(name=str(item.get("Name") or ""), event=event, parameters=parameters)


def _properties(item: dict[str, Any], names: Iterable[str] = ("Caption", "SourceTable", "PageType", "Subtype", "TableType", "Extensible")) -> dict[str, str]:
    wanted = set(names)
    found: dict[str, str] = {}
    for entry in item.get("Properties") or []:
        if isinstance(entry, dict) and entry.get("Name") in wanted and entry.get("Value") is not None:
            found[str(entry["Name"])] = str(entry["Value"])
    return found


def _walk(node: dict[str, Any], app_name: str, namespace: str, out: list[Symbol]) -> None:
    for collection, kind in PACKAGE_COLLECTIONS.items():
        for item in node.get(collection) or []:
            if not isinstance(item, dict) or not item.get("Name"):
                continue
            fields = tuple(
                SymbolField(entry.get("Id"), str(entry.get("Name") or ""), _type_name(entry.get("TypeDefinition")))
                for entry in item.get("Fields") or []
                if isinstance(entry, dict) and entry.get("Name")
            )
            if kind == "enum":
                fields = tuple(SymbolField(entry.get("Ordinal", 0), str(entry.get("Name") or ""), "value") for entry in item.get("Values") or [] if isinstance(entry, dict))
            methods = tuple(_method_from_json(entry) for entry in item.get("Methods") or [] if isinstance(entry, dict) and entry.get("Name"))
            out.append(
                Symbol(
                    kind=kind,
                    id=item.get("Id"),
                    name=str(item["Name"]),
                    app=app_name,
                    source=str(item.get("ReferenceSourceFileName") or ""),
                    namespace=namespace,
                    target=str(item.get("TargetObject") or ""),
                    fields=fields,
                    methods=methods,
                    properties=_properties(item),
                )
            )
    for child in node.get("Namespaces") or []:
        if isinstance(child, dict):
            child_name = str(child.get("Name") or "")
            _walk(child, app_name, f"{namespace}.{child_name}".strip(".") if child_name else namespace, out)


def package_symbols(app_path: str | Path) -> PackageSymbols:
    payload = read_symbol_reference(app_path)
    app_name = str(payload.get("Name") or Path(app_path).stem)
    symbols: list[Symbol] = []
    _walk(payload, app_name, "", symbols)
    return PackageSymbols(
        app_id=str(payload.get("AppId") or ""),
        name=app_name,
        publisher=str(payload.get("Publisher") or ""),
        version=str(payload.get("Version") or ""),
        symbols=tuple(symbols),
    )


def parse_source(text: str, *, file_name: str, app_name: str) -> list[Symbol]:
    headers = list(OBJECT_HEADER.finditer(text))
    found: list[Symbol] = []
    for index, header in enumerate(headers):
        start = header.end()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[start:end]
        methods: list[SymbolMethod] = []
        subscriptions: list[Subscription] = []
        for procedure in PROCEDURE.finditer(body):
            attributes = _attributes_before(body[: procedure.start()])
            event = None
            for match in attributes:
                if match.group("name") in EVENT_ATTRIBUTES:
                    event = match.group("name")
                if match.group("name") == "EventSubscriber":
                    args = SUBSCRIBER_ARGS.search(match.group("args") or "")
                    if args:
                        subscriptions.append(
                            Subscription(
                                object_type=args.group("object_type"),
                                object_name=_unquote(args.group("object")),
                                event=args.group("event"),
                                element=args.group("element") or "",
                                procedure=procedure.group("name"),
                            )
                        )
            parameters = tuple(part.strip() for part in procedure.group("params").split(";") if part.strip())
            methods.append(SymbolMethod(name=procedure.group("name"), event=event, parameters=parameters, local=bool(procedure.group("local"))))
        implements = tuple(_unquote(part) for part in (header.group("implements") or "").split(",") if part.strip())
        raw_id = header.group("id")
        found.append(
            Symbol(
                kind=header.group("kind").lower(),
                id=int(raw_id) if raw_id else None,
                name=_unquote(header.group("name")),
                app=app_name,
                source=file_name,
                target=_unquote(header.group("target")),
                implements=implements,
                methods=tuple(methods),
                subscriptions=tuple(subscriptions),
                origin="workspace",
            )
        )
    return found


def _attributes_before(text: str) -> list[re.Match[str]]:
    found: list[re.Match[str]] = []
    for line in reversed(text.rstrip().splitlines()):
        if not line.strip():
            continue
        match = ATTRIBUTE.match(line)
        if match is None:
            break
        found.append(match)
    found.reverse()
    return found


def workspace_symbols(workspace: ALWorkspace) -> list[Symbol]:
    symbols: list[Symbol] = []
    for path in workspace.source_files():
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as error:
            logger.warning("Could not read %s: %s", path, error)
            continue
        symbols.extend(parse_source(text, file_name=str(path.relative_to(workspace.root)), app_name=workspace.name))
    return symbols


class SymbolIndex:
    def __init__(self, workspace: ALWorkspace, *, cache_dir: str | Path | None = None) -> None:
        self.workspace = workspace
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._packages: dict[str, PackageSymbols] = {}
        self._sources: list[Symbol] | None = None
        self._source_stamp: tuple[int, int] | None = None

    def sources(self) -> list[Symbol]:
        files = self.workspace.source_files()
        stamp = (len(files), max((int(path.stat().st_mtime) for path in files), default=0))
        if self._sources is None or stamp != self._source_stamp:
            self._sources = workspace_symbols(self.workspace)
            self._source_stamp = stamp
        return list(self._sources)

    def packages(self) -> list[PackageSymbols]:
        latest: dict[str, PackageInfo] = {}
        for package in self.workspace.packages:
            key = f"{package.publisher}|{package.name}".lower()
            current = latest.get(key)
            if current is None or _version_key(package.version) > _version_key(current.version):
                latest[key] = package
        loaded: list[PackageSymbols] = []
        for package in latest.values():
            symbols = self._load_package(package)
            if symbols is not None:
                loaded.append(symbols)
        return loaded

    def _load_package(self, package: PackageInfo) -> PackageSymbols | None:
        key = str(package.path)
        if key in self._packages:
            return self._packages[key]
        cached = self._read_cache(package)
        if cached is not None:
            self._packages[key] = cached
            return cached
        try:
            parsed = package_symbols(package.path)
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            logger.warning("Could not read symbols from %s: %s", package.path, error)
            return None
        self._packages[key] = parsed
        self._write_cache(package, parsed)
        return parsed

    def _cache_path(self, package: PackageInfo) -> Path | None:
        if self.cache_dir is None:
            return None
        try:
            stat = package.path.stat()
        except OSError:
            return None
        digest = hashlib.sha1(f"{package.path}|{stat.st_size}|{int(stat.st_mtime)}".encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{package.path.stem[:60]}-{digest}.json"

    def _read_cache(self, package: PackageInfo) -> PackageSymbols | None:
        path = self._cache_path(package)
        if path is None or not path.is_file():
            return None
        try:
            return PackageSymbols.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            logger.debug("Symbol cache unreadable %s: %s", path, error)
            return None

    def _write_cache(self, package: PackageInfo, parsed: PackageSymbols) -> None:
        path = self._cache_path(package)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(parsed.to_json(), ensure_ascii=False), encoding="utf-8")
        except OSError as error:
            logger.debug("Symbol cache not written %s: %s", path, error)

    def all_symbols(self) -> list[Symbol]:
        found = self.sources()
        for package in self.packages():
            found.extend(package.symbols)
        return found

    def find(self, query: str, *, kind: str | None = None, limit: int = 10) -> list[Symbol]:
        wanted = query.strip().strip('"').lower()
        if not wanted:
            return []
        wanted_kind = kind.strip().lower() if kind else None
        wanted_id = int(wanted) if wanted.isdigit() else None
        exact: list[Symbol] = []
        prefix: list[Symbol] = []
        partial: list[Symbol] = []
        for symbol in self.all_symbols():
            if wanted_kind and symbol.kind != wanted_kind:
                continue
            name = symbol.name.lower()
            if name == wanted or (wanted_id is not None and symbol.id == wanted_id):
                exact.append(symbol)
            elif name.startswith(wanted):
                prefix.append(symbol)
            elif wanted in name:
                partial.append(symbol)
        ordered = exact + prefix + partial
        ordered.sort(key=lambda item: (0 if item.origin == "workspace" else 1, len(item.name)), reverse=False)
        exact_sorted = sorted(exact, key=lambda item: (0 if item.origin == "workspace" else 1, len(item.name)))
        rest = [item for item in ordered if item not in exact_sorted]
        return (exact_sorted + rest)[:limit]

    def extensions_of(self, name: str) -> list[Symbol]:
        wanted = name.strip().strip('"').lower()
        return [symbol for symbol in self.all_symbols() if symbol.target and symbol.target.lower() == wanted]

    def subscribers_to(self, object_name: str, event: str | None = None) -> list[tuple[Symbol, Subscription]]:
        wanted = object_name.strip().strip('"').lower()
        wanted_event = event.strip().lower() if event else None
        found: list[tuple[Symbol, Subscription]] = []
        for symbol in self.sources():
            for subscription in symbol.subscriptions:
                if subscription.object_name.lower() != wanted:
                    continue
                if wanted_event and subscription.event.lower() != wanted_event:
                    continue
                found.append((symbol, subscription))
        return found

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for symbol in self.sources():
            counts[symbol.kind] = counts.get(symbol.kind, 0) + 1
        return dict(sorted(counts.items()))


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts)


__all__ = [
    "EVENT_ATTRIBUTES",
    "PackageSymbols",
    "Subscription",
    "Symbol",
    "SymbolField",
    "SymbolIndex",
    "SymbolMethod",
    "package_symbols",
    "parse_package_name",
    "parse_source",
    "read_symbol_reference",
    "workspace_symbols",
]
