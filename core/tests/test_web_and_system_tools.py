# File: core/tests/test_web_and_system_tools.py

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import httpx

from core.actions.implementations.fetch_web_page import FetchWebPageAction
from core.actions.implementations.system_info import (
    SYSTEM_ACTIONS,
    DiskUsageAction,
    DriveHealthAction,
    EventLogErrorsAction,
    SystemOverviewAction,
    TopProcessesAction,
    WindowsServicesAction,
)
from core.actions.models import ActionRequest
from core.actions.registry import ActionRegistry
from core.system import probes
from core.web.fetch import PageFetcher, UrlRejected, check_public_url

HTML = """
<html><head><title>Pump Maintenance</title><meta name="author" content="H. Zuraw"></head>
<body><nav>Home | About | Contact</nav>
<article>
<h1>Pump Maintenance</h1>
<p>Replace the pump filter every six months. The seal wears faster in winter, so inspect it in October.</p>
<p>Keep a spare gasket on the shelf; the part number is 4471.</p>
<p>When the pressure gauge reads below twenty, bleed the line before restarting, and log the reading in the maintenance book so the next visit can compare.</p>
</article>
<footer>Copyright 2026</footer></body></html>
"""


def _no_dns(host: str) -> list[str]:
    return {"example.com": ["93.184.216.34"], "intranet": ["10.0.0.5"]}.get(host, ["203.0.113.9"])


class UrlGuardTests(unittest.TestCase):
    def test_public_https_passes_and_scheme_is_added(self) -> None:
        self.assertEqual(check_public_url("example.com/page", resolve=_no_dns), "https://example.com/page")

    def test_private_local_and_odd_schemes_are_rejected(self) -> None:
        for bad in ("http://localhost:8765/assess", "http://127.0.0.1/", "http://192.168.1.10/", "http://intranet/", "ftp://example.com/x", "http://user:pw@example.com/", "http://[::1]/", "nas.local"):
            with self.subTest(bad=bad):
                with self.assertRaises(UrlRejected):
                    check_public_url(bad, resolve=_no_dns)


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path == "/moved":
                return httpx.Response(302, headers={"location": "https://example.com/pump"})
            if request.url.path == "/big":
                return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>" + b"x" * 5000)
            if request.url.path == "/pdf":
                return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4")
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=HTML.encode("utf-8"))

        self.fetcher = PageFetcher(transport=httpx.MockTransport(handler), resolve=_no_dns, per_host_interval_seconds=0.0)

    def test_fetch_extracts_readable_text_and_metadata(self) -> None:
        page = self.fetcher.fetch("https://example.com/pump")
        self.assertEqual(page.title, "Pump Maintenance")
        self.assertEqual(page.author, "H Zuraw")
        self.assertIn("Replace the pump filter every six months", page.text)
        self.assertNotIn("Home | About", page.text)
        self.assertNotIn("Copyright", page.text)
        self.assertEqual(self.requests[-1].headers["user-agent"].split(" ")[0], "Iris/1.0")
        self.assertFalse(page.from_cache)

    def test_second_fetch_is_served_from_cache(self) -> None:
        self.fetcher.fetch("https://example.com/pump")
        again = self.fetcher.fetch("https://example.com/pump")
        self.assertTrue(again.from_cache)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("cached", again.source_line)

    def test_redirects_are_followed_and_the_final_url_cited(self) -> None:
        page = self.fetcher.fetch("https://example.com/moved")
        self.assertEqual(page.final_url, "https://example.com/pump")
        self.assertIn("https://example.com/pump", page.source_line)

    def test_size_cap(self) -> None:
        small = PageFetcher(transport=self.fetcher.transport, resolve=_no_dns, max_bytes=1000, per_host_interval_seconds=0.0)
        with self.assertRaises(ValueError):
            small.fetch("https://example.com/big")

    def test_action_renders_source_and_truncation(self) -> None:
        action = FetchWebPageAction(self.fetcher)
        registry = ActionRegistry()
        registry.register(action)
        self.assertIn("fetch_web_page", {spec.name for spec in registry.tools.model_tools()})

        bad = action.validate(ActionRequest(action="fetch_web_page", arguments={"url": "http://127.0.0.1/"}), None)
        self.assertFalse(bad.ok)
        self.assertIn("private or local", bad.error or "")

        ok = action.validate(ActionRequest(action="fetch_web_page", arguments={"url": "example.com/pump", "max_chars": 200}), None)
        self.assertTrue(ok.ok)
        result = action.execute(ActionRequest(action="fetch_web_page", arguments=ok.resolved_arguments or {}), None)
        self.assertEqual(result.status, "success", result.message)
        self.assertTrue(result.message.startswith("Pump Maintenance\nSource: https://example.com/pump (fetched "))
        self.assertIn("By H Zuraw", result.message)
        self.assertNotIn("dated", result.message, "no date is stated on the page, so none is invented")
        self.assertIn("more characters not shown", result.message)

        no_text = action.execute(ActionRequest(action="fetch_web_page", arguments={"url": "https://example.com/pdf"}), None)
        self.assertEqual(no_text.error, "no_text")


class SystemProbeTests(unittest.TestCase):
    def test_overview_reports_cpu_memory_and_disks(self) -> None:
        data = probes.overview()
        self.assertGreater(data["cpu_count"], 0)
        self.assertGreater(data["memory_total"], 0)
        self.assertTrue(data["disks"])
        text = SystemOverviewAction().execute(ActionRequest(action="system_overview", arguments={}), None)
        self.assertEqual(text.status, "success")
        self.assertIn("CPU", text.message)
        self.assertIn("free of", text.message)

    def test_top_processes(self) -> None:
        rows = probes.processes(sort_by="memory", limit=3)
        self.assertEqual(len(rows), 3)
        self.assertGreaterEqual(rows[0]["memory"], rows[1]["memory"])
        result = TopProcessesAction().execute(ActionRequest(action="top_processes", arguments={"sort_by": "memory", "limit": 3}), None)
        self.assertIn("Top processes by memory", result.message)

    def test_disk_usage_ranks_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "big").mkdir()
            (root / "big" / "a.bin").write_bytes(b"x" * 5000)
            (root / "small").mkdir()
            (root / "small" / "b.bin").write_bytes(b"x" * 10)
            (root / "loose.txt").write_bytes(b"x" * 700)
            data = probes.disk_usage(str(root), top=10)
            self.assertEqual([Path(item["path"]).name for item in data["entries"]], ["big", "loose.txt", "small"])
            self.assertEqual(data["measured"], 5710)
            result = DiskUsageAction().execute(ActionRequest(action="disk_usage", arguments={"path": str(root), "top": 2}), None)
            self.assertIn("big", result.message)
            self.assertNotIn("small", result.message)
        with self.assertRaises(FileNotFoundError):
            probes.disk_usage(str(Path(tmp) / "gone"))

    def test_human_bytes(self) -> None:
        self.assertEqual(probes.human_bytes(512), "512 B")
        self.assertEqual(probes.human_bytes(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(probes.human_bytes(None), "?")

    def test_every_system_action_is_registrable_and_read_only(self) -> None:
        registry = ActionRegistry()
        for action_type in SYSTEM_ACTIONS:
            registry.register(action_type())
        for definition in registry.tools.definitions():
            self.assertEqual(definition.permission.value, "read")
            self.assertFalse(definition.requires_confirmation)
        self.assertEqual(len(registry.names()), len(SYSTEM_ACTIONS))


@unittest.skipUnless(sys.platform == "win32", "PowerShell probes are Windows-only")
class WindowsProbeTests(unittest.TestCase):
    def test_drive_health_lists_disks(self) -> None:
        rows = probes.drive_health()
        disks = [row for row in rows if row.get("device") != "smart"]
        self.assertTrue(disks)
        self.assertIn(disks[0]["health"], {"Healthy", "Warning", "Unhealthy", "Unknown"})
        result = DriveHealthAction().execute(ActionRequest(action="drive_health", arguments={}), None)
        self.assertIn("Drive health", result.message)

    def test_services_filter(self) -> None:
        rows = probes.services("Spooler", limit=5)
        self.assertTrue(any(row["name"] == "Spooler" for row in rows))
        self.assertIn(rows[0]["status"], {"Running", "Stopped", "Paused", "StartPending", "StopPending"})
        result = WindowsServicesAction().execute(ActionRequest(action="windows_services", arguments={"name_filter": "Spooler"}), None)
        self.assertIn("Spooler", result.message)

    def test_event_log_errors_render(self) -> None:
        result = EventLogErrorsAction().execute(ActionRequest(action="event_log_errors", arguments={"hours": 24, "limit": 3}), None)
        self.assertEqual(result.status, "success", result.message)
        self.assertTrue(result.message.startswith(("No errors", "3 errors", "2 errors", "1 errors")), result.message[:80])

    def test_powershell_date(self) -> None:
        self.assertRegex(probes.powershell_date("/Date(1789070097581)/"), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


if __name__ == "__main__":
    unittest.main()
