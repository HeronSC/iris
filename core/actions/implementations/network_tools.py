# File: core/actions/implementations/network_tools.py

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.network.probes import NetworkProbes
from core.results.models import Result, Source, status, table
from core.results.models import text as text_result
from core.tools.models import PermissionLevel, ToolDefinition

HOST_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,253}$")


def _host_ok(value: str) -> bool:
    return bool(HOST_PATTERN.match(value.strip())) and not value.strip().startswith("-")


class _NetworkAction:

    name = ""
    definition: ToolDefinition
    arguments_model: type[BaseModel] | None = None

    def __init__(self, probes: NetworkProbes | None = None) -> None:
        self.probes = probes or NetworkProbes()

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        if self.arguments_model is None:
            return ValidationResult(ok=True, resolved_arguments={})
        try:
            parsed = self.arguments_model.model_validate(request.arguments)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        host = getattr(parsed, "host", None)
        if host is not None and not _host_ok(host):
            return ValidationResult(ok=False, error=f"{host!r} is not a host name or address")
        return ValidationResult(ok=True, resolved_target=host, resolved_arguments=parsed.model_dump())

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        try:
            message, results = self.produce(request.arguments)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return ActionResult(status="failed", message=f"{self.name} failed: {error}", action=self.name, error=str(error))
        return ActionResult(status="success", message=message, action=self.name, resolved_target=request.arguments.get("host"), results=results)

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        raise NotImplementedError


class NetworkStatusAction(_NetworkAction):
    name = "network_status"
    definition = ToolDefinition(
        name="network_status",
        description="How this PC is connected right now: interfaces with addresses, the default gateway and whether it answers, DNS servers, and whether the internet is reachable. Start here for 'is my internet down' or 'why is the network slow'.",
        permission=PermissionLevel.READ,
        outbound=True,
        keywords=("internet", "network", "wifi", "ethernet", "gateway", "dns", "offline", "connection", "connected", "ip address", "my ip"),
    )

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        report = self.probes.status()
        source = Source("network_status", "tool", "this PC")
        lines: list[str] = []
        up = [item for item in report.interfaces if item.up and item.ipv4]
        lines.append("Interfaces up: " + (", ".join(f"{item.name} {', '.join(item.ipv4)}" + (f" ({item.speed_mbps} Mbps)" if item.speed_mbps else "") for item in up) if up else "none"))
        if report.gateway:
            lines.append(f"Gateway {report.gateway}: " + (report.gateway_ping.describe().split(": ", 1)[1] if report.gateway_ping else "not tested"))
        else:
            lines.append("No default gateway.")
        lines.append("DNS servers: " + (", ".join(report.dns_servers) if report.dns_servers else "none configured"))
        lines.append(f"Public DNS: {'ok' if report.public_dns_ok else 'failing (' + str(report.public_dns_error) + ')'}; internet over TCP 443: {'ok' if report.internet_ok else 'failing (' + str(report.internet_error) + ')'}")
        verdict = "Network looks healthy." if report.healthy else "Problems: " + "; ".join(report.problems)
        lines.append(verdict)
        results: list[Result] = [
            status("ok" if report.healthy else "error", verdict, source=source),
            table(("interface", "ipv4", "mac", "up", "speed"), [(item.name, ", ".join(item.ipv4), item.mac or "", "yes" if item.up else "no", item.speed_mbps or "") for item in report.interfaces], source=source, title="Interfaces"),
            text_result("\n".join(lines), source=source, title="Network status", format="text"),
        ]
        return "\n".join(lines), tuple(results)


class CheckArguments(BaseModel):
    host: str = Field(description="Host name or IP address to test, such as nas.local, 192.168.1.5 or example.com")
    port: int | None = Field(default=None, ge=1, le=65535, description="A TCP port to try as well, such as 443 or 5000")
    count: int = Field(default=4, ge=1, le=10, description="Number of pings")


class NetworkCheckAction(_NetworkAction):
    name = "network_check"
    definition = ToolDefinition(
        name="network_check",
        description="Test one host: DNS resolution with timing, ping with loss and latency, and optionally whether a TCP port answers. Use for 'can I reach X', 'is the NAS up', 'is port 443 open on Y'.",
        arguments=CheckArguments,
        permission=PermissionLevel.READ,
        outbound=True,
        cost="seconds; sends pings",
        keywords=("ping", "reach", "reachable", "resolve", "dns lookup", "port open", "is up", "is down", "latency", "responding"),
    )
    arguments_model = CheckArguments

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        host = str(arguments["host"]).strip()
        report = self.probes.check(host, port=arguments.get("port"), count=int(arguments.get("count") or 4))
        source = Source("network_check", "tool", host)
        ok = report.dns_error is None and (report.ping is None or report.ping.reachable or report.port_open is True) and (report.port is None or report.port_open)
        rows = [("resolves", ", ".join(report.addresses) if report.addresses else report.dns_error or "?", f"{report.dns_ms} ms" if report.dns_ms is not None else "")]
        if report.ping is not None:
            rows.append(("ping", f"{report.ping.received}/{report.ping.sent} replies, {report.ping.loss_percent}% loss", f"{report.ping.avg_ms} ms avg" if report.ping.avg_ms is not None else report.ping.note or "no reply"))
        if report.port is not None:
            rows.append((f"tcp {report.port}", "open" if report.port_open else f"closed ({report.port_error})", f"{report.port_ms} ms" if report.port_ms is not None else ""))
        results: tuple[Result, ...] = (
            status("ok" if ok else "warning", report.verdict, source=source),
            table(("test", "result", "timing"), rows, source=source, title=f"Checks for {host}"),
        )
        return report.verdict, results


class TraceArguments(BaseModel):
    host: str = Field(description="Destination host name or IP address")
    max_hops: int = Field(default=15, ge=1, le=30)


class NetworkTraceAction(_NetworkAction):
    name = "network_trace"
    definition = ToolDefinition(
        name="network_trace",
        description="Trace the route to a host hop by hop with round-trip times, to see where a slow or failing connection stops.",
        arguments=TraceArguments,
        permission=PermissionLevel.READ,
        outbound=True,
        timeout_seconds=180.0,
        cost="up to a minute; sends packets to every hop",
        keywords=("traceroute", "tracert", "trace the route", "which hop", "where does it stop", "route to"),
    )
    arguments_model = TraceArguments

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        host = str(arguments["host"]).strip()
        hops = self.probes.traceroute(host, max_hops=int(arguments.get("max_hops") or 15))
        source = Source("network_trace", "tool", host)
        if not hops:
            message = f"No route information came back for {host}."
            return message, (status("warning", message, source=source),)
        answered = [hop for hop in hops if hop.address != "*"]
        last = answered[-1] if answered else None
        lines = [f"{len(hops)} hop{'s' if len(hops) != 1 else ''} toward {host}" + (f"; last answer from {last.address} at hop {last.number} ({last.best_ms} ms)" if last else "; nothing answered")]
        for hop in hops:
            rtts = " / ".join("*" if value is None else f"{value} ms" for value in hop.rtts_ms)
            lines.append(f"{hop.number:>2}. {hop.address:<18} {rtts}")
        rows = [(hop.number, hop.address, *("*" if value is None else value for value in hop.rtts_ms)) for hop in hops]
        results: tuple[Result, ...] = (table(("hop", "address", "rtt 1", "rtt 2", "rtt 3"), rows, source=source, title=f"Route to {host}"),)
        return "\n".join(lines), results


class NetworkDevicesAction(_NetworkAction):
    name = "network_devices"
    definition = ToolDefinition(
        name="network_devices",
        description="Devices this PC has recently talked to on the local network, from the ARP table: IP and MAC per interface. Passive, so a quiet device may be missing until it is pinged.",
        permission=PermissionLevel.READ,
        keywords=("devices on the network", "what is on my network", "lan devices", "arp", "mac address", "who is connected"),
    )

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        neighbours = self.probes.neighbours()
        source = Source("network_devices", "tool", "arp table")
        if not neighbours:
            message = "The ARP table holds no devices right now."
            return message, (status("ok", message, source=source),)
        by_interface: dict[str, int] = {}
        for item in neighbours:
            by_interface[item.interface] = by_interface.get(item.interface, 0) + 1
        lines = [f"{len(neighbours)} device{'s' if len(neighbours) != 1 else ''} seen recently: " + ", ".join(f"{count} on {interface}" for interface, count in by_interface.items())]
        lines.extend(f"{item.ip:<16} {item.mac} ({item.kind}, via {item.interface})" for item in neighbours[:60])
        rows = [(item.ip, item.mac, item.kind, item.interface) for item in neighbours]
        results: tuple[Result, ...] = (table(("ip", "mac", "type", "interface"), rows, source=source, title="Devices seen on the LAN"),)
        return "\n".join(lines), results


NETWORK_ACTIONS = (NetworkStatusAction, NetworkCheckAction, NetworkTraceAction, NetworkDevicesAction)

__all__ = ["NETWORK_ACTIONS", "NetworkCheckAction", "NetworkDevicesAction", "NetworkStatusAction", "NetworkTraceAction"]
