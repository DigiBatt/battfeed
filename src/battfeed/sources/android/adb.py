"""ADB (Android Debug Bridge) backends for the Android battery source.

:class:`AdbBackend` is the seam between the source and the ``adb``
executable: production code uses :class:`SubprocessAdbBackend`, tests
inject an in-memory fake. The protocol is structural, so a fake only
needs ``list_devices()`` and ``shell()``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Protocol, Sequence

__all__ = [
    "ADBCommandError",
    "ADBDeviceDisconnected",
    "ADBError",
    "ADBNotFound",
    "AdbBackend",
    "AdbDevice",
    "SubprocessAdbBackend",
]


class ADBError(RuntimeError):
    """Base class for ADB command failures."""


class ADBNotFound(ADBError):
    """Raised when the ``adb`` executable is not available."""


class ADBCommandError(ADBError):
    """Raised when an ADB command exits unsuccessfully."""


class ADBDeviceDisconnected(ADBCommandError):
    """Raised when a device disappears or becomes unavailable mid-poll."""


@dataclass(frozen=True)
class AdbDevice:
    """One row of ``adb devices -l`` output."""

    serial: str
    state: str = "device"
    transport_id: str | None = None
    qualifiers: dict[str, str] = field(default_factory=dict)


class AdbBackend(Protocol):
    """Minimal ADB surface the Android battery source needs."""

    def list_devices(self) -> list[AdbDevice]: ...

    def shell(
        self, device: AdbDevice, args: Sequence[str], timeout: float | None = None
    ) -> str: ...


class SubprocessAdbBackend:
    """ADB backend that shells out to the platform ``adb`` executable."""

    def __init__(self, adb_path: str = "adb", timeout: float = 5.0) -> None:
        self.adb_path = adb_path
        self.timeout = timeout

    def list_devices(self) -> list[AdbDevice]:
        from .parser import parse_adb_devices

        out = self._run([self.adb_path, "devices", "-l"], timeout=self.timeout)
        return parse_adb_devices(out)

    def shell(
        self,
        device: AdbDevice,
        args: Sequence[str],
        timeout: float | None = None,
    ) -> str:
        selector = ["-t", device.transport_id] if device.transport_id else ["-s", device.serial]
        return self._run(
            [self.adb_path, *selector, "shell", *args],
            timeout=timeout or self.timeout,
            device_serial=device.serial,
        )

    def getprop(self, device: AdbDevice, name: str) -> str:
        return self.shell(device, ["getprop", name]).strip()

    def tcpip(self, device: AdbDevice, port: int = 5555) -> str:
        """Switch a USB-attached device to TCP/IP mode (Wi-Fi ADB)."""
        selector = ["-t", device.transport_id] if device.transport_id else ["-s", device.serial]
        return self._run([self.adb_path, *selector, "tcpip", str(port)], timeout=self.timeout)

    def connect(self, address: str) -> str:
        """Connect to a Wi-Fi ADB device at ``host[:port]``."""
        return self._run([self.adb_path, "connect", address], timeout=self.timeout)

    def _run(
        self,
        cmd: list[str],
        *,
        timeout: float,
        device_serial: str | None = None,
    ) -> str:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise ADBNotFound(f"adb executable not found: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            if device_serial:
                raise ADBDeviceDisconnected(
                    f"ADB command timed out for {device_serial}: {' '.join(cmd)}"
                ) from exc
            raise ADBCommandError(f"ADB command timed out: {' '.join(cmd)}") from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or proc.stdout or "").strip()
            message = stderr or f"exit code {proc.returncode}"
            low = message.lower()
            if device_serial and any(
                hint in low
                for hint in ("device offline", "device not found", "no devices", "unauthorized")
            ):
                raise ADBDeviceDisconnected(f"{device_serial}: {message}")
            raise ADBCommandError(f"{' '.join(cmd)} failed: {message}")
        return proc.stdout
