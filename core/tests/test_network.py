# File: core/tests/test_network.py

"""Network diagnostics (7.2): status, one-host checks, traceroute, and the devices the LAN has seen."""

from __future__ import annotations

import socket
import unittest
from types import SimpleNamespace

from core.actions.implementations.network_tools import NetworkCheckAction, NetworkDevicesAction, NetworkStatusAction, NetworkTraceAction
from core.actions.models import ActionRequest
from core.network.probes import NetworkProbes, parse_arp, parse_ping, parse_tracert
from core.results.models import ResultKind
from core.watchers.checks import run_check

PING_OK = """
Pinging 192.168.1.1 with 32 bytes of data:
Reply from 192.168.1.1: bytes=32 time=2ms TTL=64
Reply from 192.168.1.1: bytes=32 time<1ms TTL=64
Reply from 192.168.1.1: bytes=32 time=5ms TTL=64
Request timed out.

Ping statistics for 192.168.1.1:
    Packets: Sent = 4, Received = 3, Lost = 1 (25% loss),
"""

PING_DOWN = """
Pinging 10.0.0.9 with 32 bytes of data:
Request timed out.
Request timed out.

Ping statistics for 10.0.0.9:
    Packets: Sent = 2, Received = 0, Lost = 2 (100% loss),
"""

TRACERT = """
Tracing route to example.com [93.184.216.34]
over a maximum of 15 hops:

  1     1 ms    <1 ms     1 ms  192.168.1.1
  2     *        *        *     Request timed out.
  3    12 ms    11 ms    14 ms  100.64.0.1

Trace complete.
"""

ARP = """
Interface: 192.168.1.6 --- 0x17
  Internet Address      Physical Address      Type
  192.168.1.1           ac-8b-a9-11-09-9b     dynamic
  192.168.1.5           4c-ed-fb-67-0b-0d     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static

Interface: 172.18.112.1 --- 0x2a
  Internet Address      Physical Address      Type
  172.18.127.255        ff-ff-ff-ff-ff-ff     static
"""


def _runner(outputs: dict[str, str]):
    calls: list[list[str]] = []

    def run(args: list[str], timeout: float) -> str:
        calls.append(list(args))
        return outputs.get(args[0], "")

    run.calls = calls
    return run


def _probes(outputs: dict[str, str], *, resolver=None, connector=None, gateway="192.168.1.1", dns=("192.168.1.1",)) -> NetworkProbes:
    def powershell(script: str):
        if "Get-NetRoute" in script:
            return [{"NextHop": gateway, "InterfaceAlias": "Ethernet", "RouteMetric": 0}] if gateway else []
        return [{"InterfaceAlias": "Ethernet", "ServerAddresses": list(dns)}]

    def net_if():
        addrs = {
            "Ethernet": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.6"), SimpleNamespace(family=-1, address="4c-ed-fb-00-00-01")],
            "Loopback": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1")],
        }
        stats = {"Ethernet": SimpleNamespace(isup=True, speed=1000), "Loopback": SimpleNamespace(isup=True, speed=0)}
        return addrs, stats

    return NetworkProbes(
        runner=_runner(outputs),
        resolver=resolver or (lambda host: ["93.184.216.34"]),
        connector=connector or (lambda host, port, timeout: None),
        powershell=powershell,
        net_if=net_if,
    )


class ParserTests(unittest.TestCase):
    def test_ping_output_gives_counts_and_timings(self) -> None:
        result = parse_ping("192.168.1.1", PING_OK, 4)
        self.assertEqual((result.sent, result.received, result.loss_percent), (4, 3, 25))
        self.assertEqual((result.min_ms, result.avg_ms, result.max_ms), (1, 3, 5))
        self.assertIn("3/4 replies, 3 ms average", result.describe())
        down = parse_ping("10.0.0.9", PING_DOWN, 2)
        self.assertFalse(down.reachable)
        self.assertEqual(down.note, "Request timed out")
        self.assertIn("no reply", down.describe())

    def test_tracert_output_gives_hops_with_stars(self) -> None:
        hops = parse_tracert(TRACERT)
        self.assertEqual([hop.number for hop in hops], [1, 2, 3])
        self.assertEqual(hops[0].rtts_ms, (1, 1, 1))
        self.assertEqual((hops[1].address, hops[1].rtts_ms), ("*", (None, None, None)))
        self.assertEqual(hops[2].best_ms, 11)

    def test_arp_output_skips_broadcast_and_multicast(self) -> None:
        neighbours = parse_arp(ARP)
        self.assertEqual([(item.ip, item.interface) for item in neighbours], [("192.168.1.1", "192.168.1.6"), ("192.168.1.5", "192.168.1.6")])
        self.assertEqual(neighbours[0].mac, "ac-8b-a9-11-09-9b")


class ProbeTests(unittest.TestCase):
    def test_check_combines_dns_ping_and_port(self) -> None:
        probes = _probes({"ping": PING_OK})
        report = probes.check("example.com", port=443)
        self.assertEqual(report.addresses, ("93.184.216.34",))
        self.assertTrue(report.ping.reachable)
        self.assertTrue(report.port_open)
        self.assertIn("example.com resolves to 93.184.216.34", report.verdict)
        self.assertIn("port 443 answers", report.verdict)
        self.assertEqual(probes.runner.calls[0][:3], ["ping", "-n", "4"])

    def test_check_reports_dns_failure_and_closed_ports(self) -> None:
        def failing(host: str) -> list[str]:
            raise socket.gaierror("no such host")

        probes = _probes({"ping": PING_DOWN}, resolver=failing)
        report = probes.check("nowhere.invalid")
        self.assertEqual(report.dns_error, "no such host")
        self.assertIn("does not resolve", report.verdict)

        def refused(host: str, port: int, timeout: float) -> None:
            raise OSError("refused")

        closed = _probes({"ping": PING_OK}, connector=refused).check("192.168.1.5", port=5000)
        self.assertFalse(closed.port_open)
        self.assertIn("port 5000 does not answer (refused)", closed.verdict)

    def test_status_finds_problems(self) -> None:
        healthy = _probes({"ping": PING_OK}).status()
        self.assertTrue(healthy.healthy)
        self.assertEqual(healthy.gateway, "192.168.1.1")
        self.assertEqual(healthy.dns_servers, ("192.168.1.1",))
        self.assertEqual([item.name for item in healthy.interfaces], ["Ethernet", "Loopback"])

        def refused(host: str, port: int, timeout: float) -> None:
            raise OSError("unreachable")

        broken = _probes({"ping": PING_DOWN}, connector=refused, gateway=None).status()
        self.assertFalse(broken.healthy)
        self.assertIn("no default gateway", broken.problems)
        self.assertTrue(any("no TCP path" in item for item in broken.problems))


class ToolTests(unittest.TestCase):
    def test_status_check_trace_and_devices_tools(self) -> None:
        probes = _probes({"ping": PING_OK, "tracert": TRACERT, "arp": ARP})
        context = object()
        status_result = NetworkStatusAction(probes).execute(ActionRequest(action="network_status", arguments={}), context)
        self.assertEqual(status_result.status, "success")
        self.assertIn("Network looks healthy.", status_result.message)
        self.assertEqual([item.kind for item in status_result.results], [ResultKind.STATUS, ResultKind.TABLE, ResultKind.TEXT])

        check = NetworkCheckAction(probes)
        validation = check.validate(ActionRequest(action="network_check", arguments={"host": "example.com", "port": 443}), context)
        self.assertTrue(validation.ok)
        result = check.execute(ActionRequest(action="network_check", arguments=validation.resolved_arguments), context)
        self.assertIn("port 443 answers", result.message)
        self.assertEqual(result.results[1].data["rows"][2][0], "tcp 443")
        bad = check.validate(ActionRequest(action="network_check", arguments={"host": "-n 100 evil"}), context)
        self.assertFalse(bad.ok)

        trace = NetworkTraceAction(probes).execute(ActionRequest(action="network_trace", arguments={"host": "example.com", "max_hops": 15}), context)
        self.assertIn("3 hops toward example.com; last answer from 100.64.0.1 at hop 3 (11 ms)", trace.message)
        self.assertEqual(trace.results[0].data["rows"][1][1], "*")

        devices = NetworkDevicesAction(probes).execute(ActionRequest(action="network_devices", arguments={}), context)
        self.assertIn("2 devices seen recently: 2 on 192.168.1.6", devices.message)
        self.assertEqual(devices.results[0].data["rows"][0][0], "192.168.1.1")
        for action in (NetworkStatusAction(probes), NetworkCheckAction(probes), NetworkTraceAction(probes), NetworkDevicesAction(probes)):
            self.assertEqual(action.definition.permission.value, "read")

    def test_the_gateway_watcher_uses_the_same_probes(self) -> None:
        healthy = run_check("gateway_unreachable", {"probes": _probes({"ping": PING_OK})}, None)
        self.assertFalse(healthy.triggered)
        self.assertIn("192.168.1.1", healthy.summary)
        down = run_check("gateway_unreachable", {"probes": _probes({"ping": PING_DOWN})}, None)
        self.assertTrue(down.triggered)
        missing = run_check("gateway_unreachable", {"probes": _probes({"ping": PING_OK}, gateway=None)}, None)
        self.assertTrue(missing.triggered)


if __name__ == "__main__":
    unittest.main()
