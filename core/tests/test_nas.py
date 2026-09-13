# File: core/tests/test_nas.py

"""Synology NAS (7.2), read-only: DSM sign-in, system health, volumes and drive SMART."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import httpx

from core.actions.implementations.nas_tools import NasStatusAction, NasStorageAction
from core.actions.models import ActionRequest
from core.nas import NasService
from core.nas.synology import READ_CALLS, SynologyClient, SynologyError, _uptime
from core.results.models import ResultKind

SID = "sid-1234"


class FakeDsm:
    def __init__(self, *, auth_error: int | None = None, failing_disk: bool = False, hot: bool = False, non_admin: bool = False, shares: bool = True) -> None:
        self.auth_error = auth_error
        self.failing_disk = failing_disk
        self.hot = hot
        self.non_admin = non_admin
        self.shares = shares
        self.logins = 0
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        api = params.get("api", "")
        method = params.get("method", "")
        self.calls.append(f"{api}.{method}")
        if api == "SYNO.API.Auth" and method == "login":
            if "session" in params:
                return httpx.Response(200, json={"success": False, "error": {"code": 402}})
            if self.auth_error is not None:
                return httpx.Response(200, json={"success": False, "error": {"code": self.auth_error}})
            if params.get("account") != "Iris" or params.get("passwd") != "hunter2":
                return httpx.Response(200, json={"success": False, "error": {"code": 400}})
            self.logins += 1
            return httpx.Response(200, json={"success": True, "data": {"sid": SID}})
        if api == "SYNO.API.Auth" and method == "logout":
            return httpx.Response(200, json={"success": True})
        if params.get("_sid") != SID:
            return httpx.Response(200, json={"success": False, "error": {"code": 119}})
        if self.non_admin and api in {"SYNO.Core.System", "SYNO.Core.System.Utilization", "SYNO.Storage.CGI.Storage"}:
            return httpx.Response(200, json={"success": False, "error": {"code": 1006 if api == "SYNO.Core.System" else 105}})
        if api == "SYNO.FileStation.Info":
            return httpx.Response(200, json={"success": True, "data": {"hostname": "Bespin", "is_manager": False}})
        if api == "SYNO.FileStation.List":
            if not self.shares:
                return httpx.Response(200, json={"success": True, "data": {"shares": [], "total": 0}})
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "shares": [
                            {
                                "name": "Henry",
                                "path": "/Henry",
                                "isdir": True,
                                "additional": {"real_path": "/volume1/Henry", "volume_status": {"freespace": 25609713729536, "totalspace": 40307065483264, "readonly": False}},
                            },
                            {
                                "name": "EagleLeasing",
                                "path": "/EagleLeasing",
                                "isdir": True,
                                "additional": {"real_path": "/volume1/EagleLeasing", "volume_status": {"freespace": 25609713729536, "totalspace": 40307065483264, "readonly": True}},
                            },
                        ],
                        "total": 2,
                    },
                },
            )
        if api == "SYNO.Core.System":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "model": "DS1821+",
                        "serial": "2010ABC",
                        "firmware_ver": "DSM 7.2.2-72806",
                        "up_time": "12:04:31:07",
                        "temperature": 41,
                        "temperature_warn": self.hot,
                        "ram_size": 32768,
                        "cpu_vendor": "AMD",
                        "cpu_series": "Ryzen V1500B",
                    },
                },
            )
        if api == "SYNO.Core.System.Utilization":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "cpu": {"user_load": 7, "system_load": 3, "other_load": 1},
                        "memory": {"real_usage": 46},
                        "disk": {"total": {"read_byte": 1024, "write_byte": 2048}},
                        "network": [{"device": "eth0", "tx": 1, "rx": 2}, {"device": "total", "tx": 500, "rx": 900}],
                    },
                },
            )
        if api == "SYNO.Storage.CGI.Storage":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "volumes": [
                            {"id": "volume_1", "display_name": "Volume 1", "status": "normal", "fs_type": "btrfs", "size": {"total": "8000000000000", "used": "7600000000000"}},
                        ],
                        "disks": [
                            {"id": "sata1", "name": "Drive 1", "model": "WD80EFZZ", "serial": "WD-A1", "status": "normal", "smart_status": "normal", "temp": 55 if self.hot else 38, "size_total": "8001563222016", "container": {"str": "RS819"}, "slot_id": 1, "diskType": "SATA"},
                            {"id": "sata2", "name": "Drive 2", "model": "WD80EFZZ", "serial": "WD-A2", "status": "crashed" if self.failing_disk else "normal", "smart_status": "critical" if self.failing_disk else "normal", "temp": 39, "size_total": "8001563222016", "container": {"str": "RS819"}, "slot_id": 2, "diskType": "SATA"},
                        ],
                    },
                },
            )
        return httpx.Response(200, json={"success": False, "error": {"code": 102}})


def service_for(fake: FakeDsm, *, password: str = "hunter2", section: dict | None = None) -> NasService:
    config = {"synology": section if section is not None else {"url": "https://nas.test:5001", "user": "Iris"}}
    secrets = SimpleNamespace(get=lambda name: password if name == "synology:password" else None)
    return NasService.from_config(config, secrets, transport=httpx.MockTransport(fake.handle))


class SynologyClientTests(unittest.TestCase):
    def test_signs_in_once_and_reuses_the_session(self) -> None:
        fake = FakeDsm()
        client = SynologyClient("https://nas.test:5001", "Iris", "hunter2", transport=httpx.MockTransport(fake.handle))
        info = client.system_info()
        client.storage()
        self.assertEqual(fake.logins, 1)
        self.assertEqual(info.model, "DS1821+")
        self.assertEqual(info.firmware, "DSM 7.2.2-72806")
        self.assertEqual(info.uptime, "12 days 4h 31m")
        self.assertEqual(info.temperature_c, 41.0)

    def test_signs_in_again_when_the_session_expired(self) -> None:
        fake = FakeDsm()
        client = SynologyClient("https://nas.test:5001", "Iris", "hunter2", transport=httpx.MockTransport(fake.handle))
        client.system_info()
        client._sid = "stale"
        client.storage()
        self.assertEqual(fake.logins, 2)

    def test_a_wrong_password_says_what_to_do(self) -> None:
        fake = FakeDsm(auth_error=400)
        client = SynologyClient("https://nas.test:5001", "Iris", "wrong", transport=httpx.MockTransport(fake.handle))
        with self.assertRaises(SynologyError) as raised:
            client.system_info()
        self.assertIn("/secrets set synology:password", str(raised.exception))

    def test_two_factor_is_named_as_the_reason(self) -> None:
        fake = FakeDsm(auth_error=403)
        client = SynologyClient("https://nas.test:5001", "Iris", "hunter2", transport=httpx.MockTransport(fake.handle))
        with self.assertRaises(SynologyError) as raised:
            client.system_info()
        self.assertIn("two-factor", str(raised.exception))

    def test_a_blocked_address_is_named_as_the_reason(self) -> None:
        fake = FakeDsm(auth_error=407)
        client = SynologyClient("https://nas.test:5001", "Iris", "hunter2", transport=httpx.MockTransport(fake.handle))
        with self.assertRaises(SynologyError) as raised:
            client.system_info()
        self.assertIn("Auto Block", str(raised.exception))

    def test_storage_and_utilization_are_parsed(self) -> None:
        fake = FakeDsm()
        client = SynologyClient("https://nas.test:5001", "Iris", "hunter2", transport=httpx.MockTransport(fake.handle))
        volumes, disks = client.storage()
        load = client.utilization()
        self.assertEqual(len(volumes), 1)
        self.assertEqual(volumes[0].name, "Volume 1")
        self.assertEqual(volumes[0].free_bytes, 400000000000)
        self.assertAlmostEqual(volumes[0].percent_used, 95.0)
        self.assertEqual([disk.slot for disk in disks], ["Bay 1", "Bay 2"])
        self.assertEqual(load.cpu_percent, 11.0)
        self.assertEqual(load.memory_percent, 46.0)
        self.assertEqual(load.network_up_bytes, 500)


class ReadOnlyTests(unittest.TestCase):
    def test_a_call_outside_the_readings_never_reaches_the_nas(self) -> None:
        fake = FakeDsm()
        client = SynologyClient("https://nas.test:5001", "Iris", "hunter2", transport=httpx.MockTransport(fake.handle))
        with self.assertRaises(SynologyError) as raised:
            client._request("SYNO.Core.System", "shutdown", "1")
        self.assertIn("readings", str(raised.exception))
        self.assertEqual(fake.calls, [])

    def test_every_reading_the_client_makes_is_on_the_list(self) -> None:
        fake = FakeDsm()
        client = SynologyClient("https://nas.test:5001", "Iris", "hunter2", transport=httpx.MockTransport(fake.handle))
        client.system_info()
        client.utilization()
        client.storage()
        client.shares()
        client.hostname()
        allowed = {f"{api}.{method}" for api, method in READ_CALLS} | {"SYNO.API.Auth.login"}
        self.assertLessEqual(set(fake.calls), allowed)

    def test_uptime_is_read_in_hours_or_days(self) -> None:
        self.assertEqual(_uptime("12:04:31:07"), "12 days 4h 31m")
        self.assertEqual(_uptime("712:21:38"), "29 days 16h 21m")
        self.assertEqual(_uptime(""), "")

    def test_the_release_is_not_said_twice(self) -> None:
        fake = FakeDsm()
        client = SynologyClient("https://nas.test:5001", "Iris", "hunter2", transport=httpx.MockTransport(fake.handle))
        self.assertNotIn("DSM DSM", client.system_info().summary)


class NasServiceTests(unittest.TestCase):
    def test_an_unconfigured_nas_says_what_is_missing(self) -> None:
        service = NasService.from_config({}, None)
        self.assertFalse(service.configured)
        self.assertIn("synology.url", service.problem)

    def test_a_missing_password_says_which_secret(self) -> None:
        fake = FakeDsm()
        service = service_for(fake, password="")
        self.assertFalse(service.configured)
        self.assertIn("/secrets set synology:password", service.problem)

    def test_unhealthy_reports_only_what_is_wrong(self) -> None:
        service = service_for(FakeDsm(failing_disk=True))
        volumes, disks = service.unhealthy()
        self.assertEqual(volumes, [])
        self.assertEqual([disk.name for disk in disks], ["Drive 2"])


class NasToolTests(unittest.TestCase):
    def test_status_reports_the_model_and_the_volumes(self) -> None:
        context = SimpleNamespace(nas=service_for(FakeDsm()))
        result = NasStatusAction().execute(ActionRequest(action="nas_status", arguments={}), context)
        self.assertEqual(result.status, "success")
        self.assertIn("DS1821+", result.message)
        self.assertIn("Volume 1", result.message)
        kinds = [item.kind for item in result.results]
        self.assertIn(ResultKind.STATUS, kinds)
        self.assertIn(ResultKind.TABLE, kinds)
        self.assertEqual(result.results[0].data["state"], "warning")

    def test_status_turns_red_when_a_drive_is_crashed(self) -> None:
        context = SimpleNamespace(nas=service_for(FakeDsm(failing_disk=True)))
        result = NasStatusAction().execute(ActionRequest(action="nas_status", arguments={}), context)
        self.assertEqual(result.results[0].data["state"], "error")
        self.assertIn("Drive 2", result.results[0].data["message"])

    def test_storage_lists_every_drive_with_smart(self) -> None:
        context = SimpleNamespace(nas=service_for(FakeDsm()))
        result = NasStorageAction().execute(ActionRequest(action="nas_storage", arguments={}), context)
        self.assertEqual(result.status, "success")
        self.assertIn("Drive 1", result.message)
        self.assertIn("SMART normal", result.message)
        table = [item for item in result.results if item.kind == ResultKind.TABLE][0]
        self.assertEqual(len(table.data["rows"]), 2)

    def test_storage_warns_about_a_warm_drive(self) -> None:
        context = SimpleNamespace(nas=service_for(FakeDsm(hot=True)))
        result = NasStorageAction().execute(ActionRequest(action="nas_storage", arguments={}), context)
        self.assertEqual(result.results[0].data["state"], "warning")
        self.assertIn("Drive 1", result.results[0].data["message"])

    def test_tools_refuse_politely_when_the_nas_is_not_configured(self) -> None:
        context = SimpleNamespace(nas=NasService.from_config({}, None))
        for action in (NasStatusAction(), NasStorageAction()):
            result = action.execute(ActionRequest(action=action.name, arguments={}), context)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error, "nas_unavailable")

    def test_status_falls_back_to_capacity_for_a_non_admin_account(self) -> None:
        context = SimpleNamespace(nas=service_for(FakeDsm(non_admin=True)))
        result = NasStatusAction().execute(ActionRequest(action="nas_status", arguments={}), context)
        self.assertEqual(result.status, "success")
        self.assertIn("Bespin", result.message)
        self.assertIn("Henry", result.message)
        self.assertIn("administrators group", result.message)
        table = [item for item in result.results if item.kind == ResultKind.TABLE][0]
        self.assertEqual(table.title, "Shares")
        self.assertEqual(len(table.data["rows"]), 2)

    def test_storage_says_it_needs_an_administrator(self) -> None:
        context = SimpleNamespace(nas=service_for(FakeDsm(non_admin=True)))
        result = NasStorageAction().execute(ActionRequest(action="nas_storage", arguments={}), context)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "nas_needs_admin")

    def test_status_gives_up_when_a_non_admin_sees_no_shares(self) -> None:
        context = SimpleNamespace(nas=service_for(FakeDsm(non_admin=True, shares=False)))
        result = NasStatusAction().execute(ActionRequest(action="nas_status", arguments={}), context)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "nas_needs_admin")

    def test_tools_report_a_refused_sign_in(self) -> None:
        context = SimpleNamespace(nas=service_for(FakeDsm(auth_error=400)))
        result = NasStatusAction().execute(ActionRequest(action="nas_status", arguments={}), context)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "synology")


if __name__ == "__main__":
    unittest.main()
