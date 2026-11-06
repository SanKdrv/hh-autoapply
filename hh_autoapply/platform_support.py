"""Различия операционных систем: питание и удержание сна.

Поддерживаются macOS, Windows и Linux. На каждой платформе свои средства:
- macOS : pmset + caffeinate
- Windows: GetSystemPowerStatus + SetThreadExecutionState
- Linux : /sys/class/power_supply + systemd-inhibit (если есть)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading

PLATFORM = sys.platform


def platform_name() -> str:
    if PLATFORM.startswith("darwin"):
        return "macos"
    if PLATFORM.startswith("win"):
        return "windows"
    return "linux"


# ── питание: от сети или от батареи ──────────────────────────────────────

def on_ac_power() -> bool:
    """True — питание от сети. Если определить нельзя, считаем сеть."""
    try:
        if platform_name() == "macos":
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
            return "AC Power" in out
        if platform_name() == "windows":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            status = _Status()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return status.ACLineStatus == 1
            return True
        # linux
        import glob

        for path in glob.glob("/sys/class/power_supply/A*/online"):
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip() == "1"
        return True
    except Exception:  # noqa: BLE001
        return True


# ── удержание сна на время цикла ─────────────────────────────────────────

class SleepGuard:
    """Не даёт системе уснуть. Экран при этом гаснет как обычно."""

    def __init__(self, log=print):
        self.log = log
        self._proc = None
        self._thread = None
        self._stop = None

    def start(self) -> "SleepGuard":
        name = platform_name()
        try:
            if name == "macos":
                self._start_macos()
            elif name == "windows":
                self._start_windows()
            else:
                self._start_linux()
        except Exception as exc:  # noqa: BLE001
            self.log(f"удержание сна не включилось: {exc}")
        return self

    def stop(self) -> None:
        try:
            if self._proc:
                self._proc.terminate()
                self._proc.wait(timeout=5)
                self.log("удержание сна снято")
            if self._thread:
                self._stop.set()
                self._thread.join(timeout=3)
                self.log("удержание сна снято")
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._proc = None
            self._thread = None

    # платформенные реализации

    def _start_macos(self) -> None:
        exe = shutil.which("caffeinate")
        if not exe:
            return
        self._proc = subprocess.Popen(
            [exe, "-i", "-m", "-s"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def _start_windows(self) -> None:
        import ctypes

        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        kernel32 = ctypes.windll.kernel32
        self._stop = threading.Event()

        def holder() -> None:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            self._stop.wait()
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)

        self._thread = threading.Thread(target=holder, daemon=True)
        self._thread.start()

    def _start_linux(self) -> None:
        exe = shutil.which("systemd-inhibit")
        if not exe:
            return
        self._proc = subprocess.Popen(
            [exe, "--what=idle:sleep", "--why=hh-autoapply", "sleep", "86400"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def default_system_profile_dir() -> str:
    """Путь к профилю системного браузера по умолчанию (если понадобится)."""
    import os

    name = platform_name()
    if name == "macos":
        return "~/Library/Application Support/Microsoft Edge"
    if name == "windows":
        return os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
    return "~/.config/microsoft-edge"
