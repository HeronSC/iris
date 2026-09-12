# File: core/network/probes.py

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core.system.probes import run_powershell

IS_WINDOWS = sys.platform == "win32"
PUBLIC_NAME = "dns.google"
PUBLIC_ADDRESS = "1.1.1.1"
SKIP_MACS = ("ff-ff-ff-ff-ff-ff", "01-00-5e", "33-33-")

Runner = Callable[[list[str], float], str]

PING_REPLY = re.compile(r"time[=<](?P<ms>\d+)ms", re.IGNORECASE)
PING_LOSS = re.compile(r"Lost = (?P<lost>\d+)", re.IGNORECASE)
PING_SENT = re.compile(r"Sent = (?P<sent>\d+)", re.IGNORECASE)
PING_UNREACHABLE = re.compile(r"(Destination host unreachable|Request timed out|could not find host|General failure)", re.IGNORECASE)
TRACE_HOP = re.compile(r"^\s*(?P<hop>\d+)\s+(?P<a>\*|<?\d+ ms)\s+(?P<b>\*|<?\d+ ms)\s+(?P<c>\*|<?\d+ ms)\s+(?P<addr>\S.*?)\s*$", re.IGNORECASE)
ARP_INTERFACE = re.compile(r"^Interface:\s+(?P<ip>[\d.]+)", re.IGNORECASE)
ARP_ROW = re.compile(r"^\s*(?P<ip>[\d.]+)\s+(?P<mac>[0-9a-f]{2}(?:-[0-9a-f]{2}){5})\s+(?P<type>\w+)", re.IGNORECASE)


def default_runner(args: list[str], timeout: float) -> str:
    completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", check=False)
    return (completed.stdout or "") + (completed.stderr or "")


@dataclass(frozen=True)
class PingResult:
    host: str
    sent: int
    received: int
    min_ms: int | None
    avg_ms: int | None
    max_ms: int | None
    note: str = ""

    @property
    def loss_percent(self) -> int:
        return int(round(100 * (self.sent - self.received) / self.sent)) if self.sent else 100

    @property
    def reachable(self) -> bool:
        return self.received > 0

    def describe(self) -> str:
        if not self.reachable:
            return f"{self.host}: no reply ({self.note or 'timed out'})"
        return f"{self.host}: {self.received}/{self.sent} replies, {self.avg_ms} ms average (min {self.min_ms}, max {self.max_ms}), {self.loss_percent}% loss"


@dataclass(frozen=True)
class Hop:
    number: int
    address: str
    rtts_ms: tuple[int | None, ...]

    @property
    def best_ms(self) -> int | None:
        values = [item for item in self.rtts_ms if item is not None]
        return min(values) if values else None


@dataclass(frozen=True)
class Neighbour:
    ip: str
    mac: str
    kind: str
    interface: str


@dataclass(frozen=True)
class Interface:
    name: str
    ipv4: tuple[str, ...]
    mac: str | None
    up: bool
    speed_mbps: int | None


@dataclass(frozen=True)
class CheckReport:
    host: str
    addresses: tuple[str, ...] = ()
    dns_ms: int | None = None
    dns_error: str | None = None
    ping: PingResult | None = None
    port: int | None = None
    port_open: bool | None = None
    port_ms: int | None = None
    port_error: str | None = None

    @property
    def verdict(self) -> str:
        if self.dns_error:
            return f"{self.host} does not resolve: {self.dns_error}"
        parts = [f"{self.host} resolves to {', '.join(self.addresses)}" + (f" in {self.dns_ms} ms" if self.dns_ms is not None else "")]
        if self.ping is not None:
            parts.append(self.ping.describe())
        if self.port is not None:
            if self.port_open:
                parts.append(f"port {self.port} answers in {self.port_ms} ms")
            else:
                parts.append(f"port {self.port} does not answer ({self.port_error or 'timed out'})")
        return "; ".join(parts)


@dataclass(frozen=True)
class StatusReport:
    interfaces: tuple[Interface, ...]
    gateway: str | None
    gateway_ping: PingResult | None
    dns_servers: tuple[str, ...]
    public_dns_ok: bool
    public_dns_error: str | None
    internet_ok: bool
    internet_error: str | None
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return not self.problems


def parse_ping(host: str, output: str, sent_hint: int) -> PingResult:
    times = [int(match.group("ms")) for match in PING_REPLY.finditer(output)]
    sent_match = PING_SENT.search(output)
    sent = int(sent_match.group("sent")) if sent_match else sent_hint
    lost_match = PING_LOSS.search(output)
    received = sent - int(lost_match.group("lost")) if lost_match else len(times)
    received = max(0, min(received, sent))
    note = ""
    trouble = PING_UNREACHABLE.search(output)
    if trouble and received == 0:
        note = trouble.group(1)
    return PingResult(
        host=host,
        sent=sent,
        received=received,
        min_ms=min(times) if times else None,
        avg_ms=int(round(sum(times) / len(times))) if times else None,
        max_ms=max(times) if times else None,
        note=note,
    )


def parse_tracert(output: str) -> list[Hop]:
    hops: list[Hop] = []
    for line in output.splitlines():
        match = TRACE_HOP.match(line)
        if not match:
            continue
        rtts = tuple(None if value.strip() == "*" else int(re.sub(r"\D", "", value) or 0) for value in (match.group("a"), match.group("b"), match.group("c")))
        address = match.group("addr").strip()
        if address.lower().startswith("request timed out"):
            address = "*"
        hops.append(Hop(number=int(match.group("hop")), address=address, rtts_ms=rtts))
    return hops


def parse_arp(output: str) -> list[Neighbour]:
    found: list[Neighbour] = []
    interface = ""
    for line in output.splitlines():
        head = ARP_INTERFACE.match(line)
        if head:
            interface = head.group("ip")
            continue
        row = ARP_ROW.match(line)
        if not row:
            continue
        mac = row.group("mac").lower()
        if any(mac.startswith(prefix) for prefix in SKIP_MACS):
            continue
        ip = row.group("ip")
        try:
            if ipaddress.ip_address(ip).is_multicast or ip.endswith(".255"):
                continue
        except ValueError:
            continue
        found.append(Neighbour(ip=ip, mac=mac, kind=row.group("type").lower(), interface=interface))
    return found


class NetworkProbes:
    def __init__(self, *, runner: Runner = default_runner, resolver: Callable[[str], list[str]] | None = None, connector: Callable[[str, int, float], None] | None = None, powershell: Callable[[str], Any] | None = None, net_if: Callable[[], tuple[dict, dict]] | None = None) -> None:
        self.runner = runner
        self.resolver = resolver or self._resolve
        self.connector = connector or self._connect
        self.powershell = powershell or run_powershell
        self.net_if = net_if or self._net_if

    @staticmethod
    def _resolve(host: str) -> list[str]:
        infos = socket.getaddrinfo(host, None)
        seen: list[str] = []
        for info in infos:
            address = str(info[4][0])
            if address not in seen:
                seen.append(address)
        return seen

    @staticmethod
    def _connect(host: str, port: int, timeout: float) -> None:
        with socket.create_connection((host, port), timeout=timeout):
            return None

    @staticmethod
    def _net_if() -> tuple[dict, dict]:
        #! @allow-local-import
        import psutil

        return psutil.net_if_addrs(), psutil.net_if_stats()

    def resolve(self, host: str) -> tuple[list[str], int | None, str | None]:
        started = time.perf_counter()
        try:
            addresses = self.resolver(host)
        except (OSError, socket.gaierror) as error:
            return [], None, str(error)
        return addresses, int((time.perf_counter() - started) * 1000), None

    def ping(self, host: str, *, count: int = 4, timeout_ms: int = 1000) -> PingResult:
        args = ["ping", "-n", str(count), "-w", str(timeout_ms), host] if IS_WINDOWS else ["ping", "-c", str(count), "-W", str(max(1, timeout_ms // 1000)), host]
        try:
            output = self.runner(args, count * (timeout_ms / 1000 + 1) + 5)
        except subprocess.TimeoutExpired:
            return PingResult(host=host, sent=count, received=0, min_ms=None, avg_ms=None, max_ms=None, note="ping did not finish")
        except OSError as error:
            return PingResult(host=host, sent=count, received=0, min_ms=None, avg_ms=None, max_ms=None, note=str(error))
        return parse_ping(host, output, count)

    def port(self, host: str, port: int, *, timeout: float = 3.0) -> tuple[bool, int | None, str | None]:
        started = time.perf_counter()
        try:
            self.connector(host, int(port), timeout)
        except OSError as error:
            return False, None, str(error)
        return True, int((time.perf_counter() - started) * 1000), None

    def traceroute(self, host: str, *, max_hops: int = 15, timeout_ms: int = 800) -> list[Hop]:
        args = ["tracert", "-d", "-h", str(max_hops), "-w", str(timeout_ms), host] if IS_WINDOWS else ["traceroute", "-n", "-m", str(max_hops), "-w", str(max(1, timeout_ms // 1000)), host]
        output = self.runner(args, max_hops * 3 * (timeout_ms / 1000) + 10)
        return parse_tracert(output)

    def neighbours(self) -> list[Neighbour]:
        output = self.runner(["arp", "-a"], 10.0)
        return parse_arp(output)

    def interfaces(self) -> list[Interface]:
        addrs, stats = self.net_if()
        found: list[Interface] = []
        for name, entries in addrs.items():
            ipv4 = tuple(str(entry.address) for entry in entries if getattr(entry, "family", None) == socket.AF_INET and not str(entry.address).startswith("169.254"))
            mac = next((str(entry.address) for entry in entries if getattr(entry, "family", None) == getattr(socket, "AF_LINK", -1) or (isinstance(entry.address, str) and entry.address.count("-") == 5)), None)
            stat = stats.get(name)
            found.append(Interface(name=name, ipv4=ipv4, mac=mac, up=bool(getattr(stat, "isup", False)) if stat else False, speed_mbps=int(getattr(stat, "speed", 0)) or None if stat else None))
        return [item for item in found if item.ipv4 or item.up]

    def gateway(self) -> str | None:
        try:
            rows = self.powershell("Get-NetRoute -DestinationPrefix '0.0.0.0/0' -AddressFamily IPv4 | Sort-Object RouteMetric | Select-Object -First 3 NextHop, InterfaceAlias, RouteMetric | ConvertTo-Json -Compress")
        except Exception:
            rows = None
        for row in (rows if isinstance(rows, list) else [rows] if rows else []):
            if isinstance(row, dict) and row.get("NextHop") and str(row["NextHop"]) != "0.0.0.0":
                return str(row["NextHop"])
        return None

    def dns_servers(self) -> list[str]:
        try:
            rows = self.powershell("Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object { $_.ServerAddresses } | Select-Object InterfaceAlias, ServerAddresses | ConvertTo-Json -Compress")
        except Exception:
            rows = None
        servers: list[str] = []
        for row in (rows if isinstance(rows, list) else [rows] if rows else []):
            if not isinstance(row, dict):
                continue
            addresses = row.get("ServerAddresses")
            for address in (addresses if isinstance(addresses, list) else [addresses]):
                if address and str(address) not in servers:
                    servers.append(str(address))
        return servers

    def check(self, host: str, *, port: int | None = None, count: int = 4) -> CheckReport:
        addresses, dns_ms, dns_error = self.resolve(host)
        if dns_error:
            return CheckReport(host=host, dns_error=dns_error)
        ping = self.ping(host, count=count)
        report = CheckReport(host=host, addresses=tuple(addresses), dns_ms=dns_ms, ping=ping)
        if port is not None:
            open_, port_ms, port_error = self.port(host, port)
            report = CheckReport(host=host, addresses=tuple(addresses), dns_ms=dns_ms, ping=ping, port=port, port_open=open_, port_ms=port_ms, port_error=port_error)
        return report

    def status(self) -> StatusReport:
        interfaces = tuple(self.interfaces())
        gateway = self.gateway()
        gateway_ping = self.ping(gateway, count=2, timeout_ms=800) if gateway else None
        dns_servers = tuple(self.dns_servers())
        _addresses, _ms, dns_error = self.resolve(PUBLIC_NAME)
        internet_ok, _port_ms, internet_error = self.port(PUBLIC_ADDRESS, 443, timeout=3.0)
        problems: list[str] = []
        if not any(item.up and item.ipv4 for item in interfaces):
            problems.append("no network interface is up with an IPv4 address")
        if gateway is None:
            problems.append("no default gateway")
        elif gateway_ping is not None and not gateway_ping.reachable:
            problems.append(f"the gateway {gateway} does not answer pings")
        if dns_error:
            problems.append(f"public DNS lookup of {PUBLIC_NAME} failed: {dns_error}")
        if not internet_ok:
            problems.append(f"no TCP path to {PUBLIC_ADDRESS}:443 ({internet_error})")
        return StatusReport(
            interfaces=interfaces,
            gateway=gateway,
            gateway_ping=gateway_ping,
            dns_servers=dns_servers,
            public_dns_ok=dns_error is None,
            public_dns_error=dns_error,
            internet_ok=internet_ok,
            internet_error=internet_error,
            problems=tuple(problems),
        )


__all__ = ["CheckReport", "Hop", "Interface", "Neighbour", "NetworkProbes", "PingResult", "StatusReport", "default_runner", "parse_arp", "parse_ping", "parse_tracert"]
