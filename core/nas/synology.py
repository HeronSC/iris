# File: core/nas/synology.py

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15.0
PASSWORD_SECRET = "synology:password"
PERMISSION_CODES = frozenset({105, 1006})

AUTH_ERRORS = {
    400: "DSM does not know that account, or the password is wrong. Check the user name and reset the secret with /secrets set synology:password <value>.",
    401: "That DSM account is disabled.",
    402: "That DSM account may not sign in here. In Control Panel give the account permission to use DSM.",
    403: "That DSM account needs a two-factor code, which Iris cannot supply. Turn 2FA off for this account.",
    404: "DSM rejected the two-factor code.",
    406: "DSM enforces two-factor sign-in for this account, which Iris cannot supply.",
    407: "DSM has blocked this machine's address. Clear it in Control Panel, Security, Account, Auto Block.",
    408: "The password on that DSM account has expired.",
    409: "That DSM account must change its password before it can be used.",
    411: "That DSM account is locked.",
}

REQUEST_ERRORS = {
    101: "DSM says the request was missing something.",
    102: "DSM does not offer that API on this model.",
    103: "DSM does not offer that method.",
    105: "That DSM account is not an administrator, and DSM reserves the system and storage readings for administrators.",
    106: "The DSM session expired.",
    119: "The DSM session is no longer valid.",
    1006: "That DSM account is not an administrator, and DSM reserves the system readings for administrators.",
}


class SynologyError(RuntimeError):
    pass


class SynologyPermissionError(SynologyError):
    pass


@dataclass(frozen=True)
class SystemInfo:
    model: str
    serial: str
    firmware: str
    uptime: str
    temperature_c: float | None
    temperature_warning: bool
    ram_mb: int
    cpu: str
    ntp_server: str = ""

    @property
    def summary(self) -> str:
        parts = [f"{self.model} on DSM {self.firmware}"]
        if self.temperature_c is not None:
            parts.append(f"{self.temperature_c:.0f} °C")
        if self.uptime:
            parts.append(f"up {self.uptime}")
        return ", ".join(parts)


@dataclass(frozen=True)
class Volume:
    id: str
    name: str
    status: str
    filesystem: str
    total_bytes: int
    used_bytes: int

    @property
    def free_bytes(self) -> int:
        return max(0, self.total_bytes - self.used_bytes)

    @property
    def percent_used(self) -> float:
        return (self.used_bytes / self.total_bytes * 100.0) if self.total_bytes else 0.0

    @property
    def healthy(self) -> bool:
        return self.status.lower() in {"normal", "healthy"}


@dataclass(frozen=True)
class Disk:
    id: str
    name: str
    model: str
    serial: str
    status: str
    smart_status: str
    temperature_c: float | None
    size_bytes: int
    slot: str
    disk_type: str

    @property
    def healthy(self) -> bool:
        return self.status.lower() in {"normal", "healthy"} and self.smart_status.lower() in {"normal", "healthy", ""}


@dataclass(frozen=True)
class Share:
    name: str
    path: str
    real_path: str
    total_bytes: int
    free_bytes: int
    read_only: bool

    @property
    def used_bytes(self) -> int:
        return max(0, self.total_bytes - self.free_bytes)

    @property
    def percent_used(self) -> float:
        return (self.used_bytes / self.total_bytes * 100.0) if self.total_bytes else 0.0


@dataclass(frozen=True)
class Utilization:
    cpu_percent: float
    memory_percent: float
    disk_read_bytes: int
    disk_write_bytes: int
    network_up_bytes: int
    network_down_bytes: int


def _number(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    return int(_number(value))


def _uptime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    pieces = text.split(":")
    if len(pieces) != 4:
        return text
    days, hours, minutes, _seconds = (_int(piece) for piece in pieces)
    if days:
        return f"{days} day{'s' if days != 1 else ''} {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def parse_system_info(data: dict[str, Any]) -> SystemInfo:
    temperature = data.get("temperature")
    return SystemInfo(
        model=str(data.get("model") or "Synology"),
        serial=str(data.get("serial") or ""),
        firmware=str(data.get("firmware_ver") or data.get("firmware") or ""),
        uptime=_uptime(data.get("up_time")),
        temperature_c=_number(temperature) if temperature not in (None, "") else None,
        temperature_warning=bool(data.get("temperature_warn")),
        ram_mb=_int(data.get("ram_size")),
        cpu=" ".join(part for part in (str(data.get("cpu_vendor") or "").strip(), str(data.get("cpu_series") or "").strip()) if part),
        ntp_server=str(data.get("ntp_server") or ""),
    )


def parse_volume(data: dict[str, Any]) -> Volume:
    size = data.get("size") if isinstance(data.get("size"), dict) else {}
    return Volume(
        id=str(data.get("id") or ""),
        name=str(data.get("display_name") or data.get("id") or ""),
        status=str(data.get("status") or ""),
        filesystem=str(data.get("fs_type") or ""),
        total_bytes=_int(size.get("total")),
        used_bytes=_int(size.get("used")),
    )


def parse_disk(data: dict[str, Any]) -> Disk:
    temperature = data.get("temp")
    container = data.get("container")
    slot = str((container or {}).get("str") or "") if isinstance(container, dict) else str(container or "")
    return Disk(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or data.get("id") or ""),
        model=str(data.get("model") or "").strip(),
        serial=str(data.get("serial") or "").strip(),
        status=str(data.get("status") or ""),
        smart_status=str(data.get("smart_status") or ""),
        temperature_c=_number(temperature) if temperature not in (None, "") else None,
        size_bytes=_int(data.get("size_total")),
        slot=slot,
        disk_type=str(data.get("diskType") or data.get("disk_type") or ""),
    )


def _network_total(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        total = value.get("total")
        return total if isinstance(total, dict) else value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and str(item.get("device") or "").lower() == "total":
                return item
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def parse_share(data: dict[str, Any]) -> Share:
    additional = data.get("additional") if isinstance(data.get("additional"), dict) else {}
    volume = additional.get("volume_status") if isinstance(additional.get("volume_status"), dict) else {}
    return Share(
        name=str(data.get("name") or ""),
        path=str(data.get("path") or ""),
        real_path=str(additional.get("real_path") or ""),
        total_bytes=_int(volume.get("totalspace")),
        free_bytes=_int(volume.get("freespace")),
        read_only=bool(volume.get("readonly")),
    )


def parse_utilization(data: dict[str, Any]) -> Utilization:
    cpu = data.get("cpu") if isinstance(data.get("cpu"), dict) else {}
    memory = data.get("memory") if isinstance(data.get("memory"), dict) else {}
    disk = data.get("disk") if isinstance(data.get("disk"), dict) else {}
    disk_total = disk.get("total") if isinstance(disk.get("total"), dict) else {}
    network_rows = _network_total(data.get("network"))
    return Utilization(
        cpu_percent=_number(cpu.get("user_load")) + _number(cpu.get("system_load")) + _number(cpu.get("other_load")),
        memory_percent=_number(memory.get("real_usage")),
        disk_read_bytes=_int(disk_total.get("read_byte")),
        disk_write_bytes=_int(disk_total.get("write_byte")),
        network_up_bytes=_int(network_rows.get("tx")),
        network_down_bytes=_int(network_rows.get("rx")),
    )


class SynologyClient:
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        verify: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.verify = verify
        self.transport = transport
        self._sid: str | None = None
        self._lock = threading.Lock()

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout_seconds,
            transport=self.transport,
            verify=self.verify,
            headers={"User-Agent": "Iris/1.0"},
        )

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.get(f"{self.base_url}{path}", params=params)
        except httpx.ConnectError as error:
            raise SynologyError(f"The NAS at {self.base_url} is not reachable: {error}") from error
        except httpx.HTTPError as error:
            raise SynologyError(f"The NAS did not answer: {error}") from error
        if response.status_code >= 400:
            raise SynologyError(f"The NAS answered {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise SynologyError("The NAS returned something other than JSON; check the address and the port") from error
        return payload if isinstance(payload, dict) else {}

    def login(self) -> str:
        with self._lock:
            payload = self._get(
                "/webapi/entry.cgi",
                {
                    "api": "SYNO.API.Auth",
                    "version": "7",
                    "method": "login",
                    "account": self.user,
                    "passwd": self.password,
                    "format": "sid",
                },
            )
            if not payload.get("success"):
                code = _int((payload.get("error") or {}).get("code"))
                raise SynologyError(AUTH_ERRORS.get(code, f"DSM refused the sign-in (error {code})."))
            sid = str((payload.get("data") or {}).get("sid") or "")
            if not sid:
                raise SynologyError("DSM accepted the sign-in but offered no session")
            self._sid = sid
            return sid

    def _request(self, api: str, method: str, version: str = "1", **fields: Any) -> dict[str, Any]:
        sid = self._sid or self.login()
        params = {"api": api, "method": method, "version": version, "_sid": sid, **fields}
        payload = self._get("/webapi/entry.cgi", params)
        if not payload.get("success"):
            code = _int((payload.get("error") or {}).get("code"))
            if code in {105, 106, 119}:
                self._sid = None
                params["_sid"] = self.login()
                payload = self._get("/webapi/entry.cgi", params)
        if not payload.get("success"):
            code = _int((payload.get("error") or {}).get("code"))
            message = REQUEST_ERRORS.get(code, f"DSM refused {api}.{method} (error {code}).")
            if code in PERMISSION_CODES:
                raise SynologyPermissionError(message)
            raise SynologyError(message)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def system_info(self) -> SystemInfo:
        return parse_system_info(self._request("SYNO.Core.System", "info", "1"))

    def utilization(self) -> Utilization:
        return parse_utilization(self._request("SYNO.Core.System.Utilization", "get", "1"))

    def storage(self) -> tuple[list[Volume], list[Disk]]:
        data = self._request("SYNO.Storage.CGI.Storage", "load_info", "1")
        raw_volumes = data.get("volumes") if isinstance(data.get("volumes"), list) else []
        raw_disks = data.get("disks") if isinstance(data.get("disks"), list) else []
        volumes = [parse_volume(item) for item in raw_volumes if isinstance(item, dict)]
        disks = [parse_disk(item) for item in raw_disks if isinstance(item, dict)]
        return volumes, disks

    def shares(self) -> list[Share]:
        data = self._request(
            "SYNO.FileStation.List",
            "list_share",
            "2",
            additional='["real_path","volume_status"]',
        )
        raw = data.get("shares") if isinstance(data.get("shares"), list) else []
        return [parse_share(item) for item in raw if isinstance(item, dict)]

    def hostname(self) -> str:
        return str(self._request("SYNO.FileStation.Info", "get", "2").get("hostname") or "")

    def logout(self) -> None:
        if self._sid is None:
            return
        try:
            self._get(
                "/webapi/entry.cgi",
                {"api": "SYNO.API.Auth", "version": "7", "method": "logout", "_sid": self._sid},
            )
        except SynologyError:
            logger.info("DSM logout did not answer; dropping the session anyway")
        self._sid = None


__all__ = [
    "AUTH_ERRORS",
    "DEFAULT_TIMEOUT_SECONDS",
    "Disk",
    "PASSWORD_SECRET",
    "PERMISSION_CODES",
    "Share",
    "SynologyClient",
    "SynologyError",
    "SynologyPermissionError",
    "SystemInfo",
    "Utilization",
    "Volume",
    "parse_disk",
    "parse_share",
    "parse_system_info",
    "parse_utilization",
    "parse_volume",
]
