from __future__ import annotations

import argparse
import base64
import codecs
import contextlib
import ctypes
import ctypes.wintypes
import getpass
import hashlib
import io
import json
import os
import platform
import py_compile
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil
import requests
from PIL import Image, ImageGrab, ImageTk

with contextlib.suppress(ImportError):
    import msvcrt

with contextlib.suppress(ImportError):
    import tkinter as tk

with contextlib.suppress(ImportError):
    import winreg


EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
GetForegroundWindow = ctypes.windll.user32.GetForegroundWindow
GetSystemMetrics = ctypes.windll.user32.GetSystemMetrics
GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
IsWindowVisible = ctypes.windll.user32.IsWindowVisible
LockWorkStation = ctypes.windll.user32.LockWorkStation
PostMessageW = ctypes.windll.user32.PostMessageW
SendInput = ctypes.windll.user32.SendInput
SetCursorPos = ctypes.windll.user32.SetCursorPos
GetWindowTextLengthW = ctypes.windll.user32.GetWindowTextLengthW
GetWindowTextW = ctypes.windll.user32.GetWindowTextW
CloseClipboard = ctypes.windll.user32.CloseClipboard
EmptyClipboard = ctypes.windll.user32.EmptyClipboard
OpenClipboard = ctypes.windll.user32.OpenClipboard
SetClipboardData = ctypes.windll.user32.SetClipboardData
GlobalAlloc = ctypes.windll.kernel32.GlobalAlloc
GlobalLock = ctypes.windll.kernel32.GlobalLock
GlobalUnlock = ctypes.windll.kernel32.GlobalUnlock
RtlMoveMemory = ctypes.windll.kernel32.RtlMoveMemory
WM_CLOSE = 0x0010
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

OpenClipboard.argtypes = [ctypes.wintypes.HWND]
OpenClipboard.restype = ctypes.wintypes.BOOL
CloseClipboard.argtypes = []
CloseClipboard.restype = ctypes.wintypes.BOOL
EmptyClipboard.argtypes = []
EmptyClipboard.restype = ctypes.wintypes.BOOL
SetClipboardData.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.HANDLE]
SetClipboardData.restype = ctypes.wintypes.HANDLE
GlobalAlloc.argtypes = [ctypes.wintypes.UINT, ctypes.c_size_t]
GlobalAlloc.restype = ctypes.wintypes.HGLOBAL
GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
GlobalLock.restype = ctypes.c_void_p
GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
GlobalUnlock.restype = ctypes.wintypes.BOOL
RtlMoveMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
RtlMoveMemory.restype = None


# Edit these values first.
SERVER_URL = os.environ.get("PCBOT_SERVER_URL", "http://217.60.245.42:8090").strip()
REGISTRATION_KEY = os.environ.get("PCBOT_REGISTRATION_KEY", "change-this-key").strip()
DEFAULT_AGENT_UPDATE_URL = os.environ.get(
    "PCBOT_AGENT_UPDATE_URL",
    "https://raw.githubusercontent.com/pureoffic2/comfyUIAgent/main/pc_agent.py",
).strip()
HEARTBEAT_INTERVAL_SECONDS = max(4.0, float(os.environ.get("PCBOT_HEARTBEAT_INTERVAL", "8")))
COMMAND_POLL_SECONDS = max(0.02, float(os.environ.get("PCBOT_COMMAND_POLL", "0.02")))
HTTP_TIMEOUT_SECONDS = max(5, int(os.environ.get("PCBOT_HTTP_TIMEOUT", "12")))
WIFI_RECOVERY_COOLDOWN_SECONDS = max(10.0, float(os.environ.get("PCBOT_WIFI_RECOVERY_COOLDOWN", "35")))
WIFI_CONNECT_SETTLE_SECONDS = max(3.0, float(os.environ.get("PCBOT_WIFI_CONNECT_SETTLE", "6")))
TEXT_WINDOW_SECONDS = max(5, int(os.environ.get("PCBOT_TEXT_WINDOW_SECONDS", "25")))
AGENT_VERSION = "1.6.0"
SHELL_COMMAND_TIMEOUT_SECONDS = 25
SHELL_COMMAND_CWD = str(Path.home())
SHELL_COMMAND_PREVIEW_CHARS = 1600
SHELL_COMMAND_FILE_LIMIT_BYTES = 22000000
DEFAULT_STARTUP_ENTRY_NAME = "SystemPortalAgent"
LEGACY_STARTUP_ENTRY_NAMES = ("SafePcTelegramAgent", "SchoolProAgent")
REMOTE_FRAME_MAX_WIDTH = 960
REMOTE_FRAME_MAX_HEIGHT = 540
REMOTE_FRAME_QUALITY = 36
STANDARD_SCREENSHOT_MAX_WIDTH = 1600
STANDARD_SCREENSHOT_MAX_HEIGHT = 900
STANDARD_SCREENSHOT_QUALITY = 72
FILE_SEARCH_MAX_RESULTS = 80
FILE_TRANSFER_LIMIT_BYTES = 49 * 1024 * 1024
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SM_CXSCREEN = 0
SM_CYSCREEN = 1
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
WLAN_CLIENT_VERSION_LONGHORN = 2
WLAN_AVAILABLE_NETWORK_CONNECTED = 0x00000001
WLAN_AVAILABLE_NETWORK_HAS_PROFILE = 0x00000002
WLAN_CONNECTION_MODE_PROFILE = 0
DOT11_BSS_TYPE_ANY = 3


# Optional launch aliases for /run and /restartapp.
APP_ALIASES: dict[str, dict[str, Any]] = {
    "brave": {
        "start": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        "processes": ["brave.exe"],
    },
    "steam": {
        "start": r"C:\Program Files (x86)\Steam\steam.exe",
        "processes": ["steam.exe"],
    },
    "discord": {
        "start": r"%LocalAppData%\Discord\Update.exe --processStart Discord.exe",
        "processes": ["Discord.exe"],
    },
}



JOB_ALIASES: dict[str, dict[str, Any]] = {
    "comfy_start": {
        "command": r"D:\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable\python_embeded\python.exe ComfyUI\main.py --listen 127.0.0.1 --port 8188",
        "cwd": r"D:\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable",
    },
    "comfy_portal": {
        "command": r"D:\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable\ComfyPortal.exe",
        "cwd": r"D:\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable",
    },
}


WATCHED_SERVICES = [
    "Spooler",
    "wuauserv",
]


BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "agent_state.json"
LOCK_PATH = BASE_DIR / "agent.lock"
AGENT_LOCK_HANDLE: Any | None = None
FILE_SEARCH_ROOTS = [
    Path.home() / "Desktop",
    Path.home() / "Downloads",
    Path.home() / "Documents",
    Path.home() / "Pictures",
    BASE_DIR,
]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.wintypes.DWORD),
        ("Data2", ctypes.wintypes.WORD),
        ("Data3", ctypes.wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", GUID),
        ("strInterfaceDescription", ctypes.wintypes.WCHAR * 256),
        ("isState", ctypes.wintypes.DWORD),
    ]


class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", ctypes.wintypes.DWORD),
        ("dwIndex", ctypes.wintypes.DWORD),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
    ]


class WLAN_PROFILE_INFO(ctypes.Structure):
    _fields_ = [
        ("strProfileName", ctypes.wintypes.WCHAR * 256),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


class WLAN_PROFILE_INFO_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", ctypes.wintypes.DWORD),
        ("dwIndex", ctypes.wintypes.DWORD),
        ("ProfileInfo", WLAN_PROFILE_INFO * 1),
    ]


class DOT11_SSID(ctypes.Structure):
    _fields_ = [
        ("uSSIDLength", ctypes.wintypes.ULONG),
        ("ucSSID", ctypes.c_ubyte * 32),
    ]


class WLAN_AVAILABLE_NETWORK(ctypes.Structure):
    _fields_ = [
        ("strProfileName", ctypes.wintypes.WCHAR * 256),
        ("dot11Ssid", DOT11_SSID),
        ("dot11BssType", ctypes.wintypes.DWORD),
        ("uNumberOfBssids", ctypes.wintypes.DWORD),
        ("bNetworkConnectable", ctypes.wintypes.BOOL),
        ("wlanNotConnectableReason", ctypes.wintypes.DWORD),
        ("uNumberOfPhyTypes", ctypes.wintypes.DWORD),
        ("dot11PhyTypes", ctypes.wintypes.DWORD * 8),
        ("bMorePhyTypes", ctypes.wintypes.BOOL),
        ("wlanSignalQuality", ctypes.wintypes.DWORD),
        ("bSecurityEnabled", ctypes.wintypes.BOOL),
        ("dot11DefaultAuthAlgorithm", ctypes.wintypes.DWORD),
        ("dot11DefaultCipherAlgorithm", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("dwReserved", ctypes.wintypes.DWORD),
    ]


class WLAN_AVAILABLE_NETWORK_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", ctypes.wintypes.DWORD),
        ("dwIndex", ctypes.wintypes.DWORD),
        ("Network", WLAN_AVAILABLE_NETWORK * 1),
    ]


class WLAN_CONNECTION_PARAMETERS(ctypes.Structure):
    _fields_ = [
        ("wlanConnectionMode", ctypes.wintypes.DWORD),
        ("strProfile", ctypes.wintypes.LPCWSTR),
        ("pDot11Ssid", ctypes.c_void_p),
        ("pDesiredBssidList", ctypes.c_void_p),
        ("dot11BssType", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("union", INPUT_UNION),
    ]


def appdata_roaming_dir() -> Path:
    raw = os.environ.get("APPDATA")
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Roaming"


def localappdata_dir() -> Path:
    raw = os.environ.get("LOCALAPPDATA")
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Local"


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def ensure_single_instance() -> bool:
    global AGENT_LOCK_HANDLE
    if "msvcrt" not in globals():
        return True
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+b")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()).encode("ascii", errors="ignore"))
    handle.flush()
    AGENT_LOCK_HANDLE = handle
    return True


def hidden_startupinfo() -> Any | None:
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return startupinfo


def hidden_subprocess_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
        startupinfo = hidden_startupinfo()
        if startupinfo is not None:
            kwargs["startupinfo"] = startupinfo
    return kwargs


def boost_process_priority() -> None:
    with contextlib.suppress(Exception):
        proc = psutil.Process()
        if hasattr(psutil, "HIGH_PRIORITY_CLASS"):
            proc.nice(psutil.HIGH_PRIORITY_CLASS)


def stable_device_id() -> str:
    seed = f"{socket.gethostname().lower()}-{uuid.getnode()}"
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex[:16]


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return "н/д"
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.1f} {units[index]}"


def windows_version_label() -> str:
    if platform.system().lower() != "windows":
        return f"{platform.system()} {platform.release()}".strip()
    release, version, _, _ = platform.win32_ver()
    parts = ["Windows"]
    if release:
        parts.append(release)
    if version and version not in parts:
        parts.append(version)
    return " ".join(parts)


def startup_entry_name() -> str:
    return os.environ.get("PCBOT_STARTUP_TASK_NAME", DEFAULT_STARTUP_ENTRY_NAME).strip() or DEFAULT_STARTUP_ENTRY_NAME


def startup_vbs_path(entry_name: str | None = None) -> Path:
    entry = entry_name or startup_entry_name()
    return appdata_roaming_dir() / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{entry}.vbs"


def legacy_startup_cmd_path(entry_name: str | None = None) -> Path:
    entry = entry_name or startup_entry_name()
    return appdata_roaming_dir() / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{entry}.cmd"


def autostart_scan_patterns() -> tuple[str, ...]:
    return (
        "SystemPortal*.cmd",
        "SystemPortal*.vbs",
        "SchoolPro*.cmd",
        "SchoolPro*.vbs",
        "SafePc*.cmd",
        "SafePc*.vbs",
    )


def startup_dir() -> Path:
    return appdata_roaming_dir() / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def cleanup_legacy_startup_files() -> None:
    base = startup_dir()
    if not base.exists():
        return
    for pattern in autostart_scan_patterns():
        for candidate in base.glob(pattern):
            if not candidate.is_file():
                continue
            text = ""
            with contextlib.suppress(OSError):
                text = candidate.read_text(encoding="utf-8", errors="ignore").lower()
            if "pc_agent.py" not in text and "schoolpro" not in candidate.name.lower() and "safepc" not in candidate.name.lower() and "systemportal" not in candidate.name.lower():
                continue
            with contextlib.suppress(FileNotFoundError):
                candidate.unlink()


def cleanup_legacy_scheduled_tasks() -> None:
    task_names = {
        DEFAULT_STARTUP_ENTRY_NAME,
        *LEGACY_STARTUP_ENTRY_NAMES,
        "SystemPortalAgentTest",
        "SchoolProAgentTest",
        "SchoolProAgentTest2",
    }
    for task_name in task_names:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", task_name, "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(),
        )


def get_ip_addresses() -> list[str]:
    addresses: list[str] = []
    for _, interface_addrs in psutil.net_if_addrs().items():
        for addr in interface_addrs:
            if addr.family == socket.AF_INET and addr.address not in {"127.0.0.1", "0.0.0.0"}:
                addresses.append(addr.address)
    return sorted(set(addresses))


def get_drive_lines() -> list[str]:
    lines: list[str] = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts.lower():
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except PermissionError:
            continue
        lines.append(
            f"{part.device} | занято {usage.percent}% | "
            f"свободно {human_bytes(usage.free)} из {human_bytes(usage.total)}"
        )
    return lines


def get_service_lines() -> list[str]:
    lines: list[str] = []
    for name in WATCHED_SERVICES:
        try:
            service = psutil.win_service_get(name)
            info = service.as_dict()
            lines.append(f"{name} | {info['status']} | {info['display_name']}")
        except Exception as exc:
            lines.append(f"{name} | ошибка | {exc}")
    return lines


def get_window_title(hwnd: int) -> str:
    length = GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def get_visible_window_rows(limit: int = 12) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()

    def callback(hwnd: int, lparam: int) -> bool:
        if not IsWindowVisible(hwnd):
            return True
        title = get_window_title(hwnd)
        if not title:
            return True
        process_id = ctypes.wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        try:
            proc = psutil.Process(process_id.value)
            name = proc.name()
        except psutil.Error:
            return True
        key = (process_id.value, name.lower(), title)
        if key in seen:
            return True
        seen.add(key)
        rows.append(
            {
                "pid": process_id.value,
                "name": name,
                "title": title,
            }
        )
        return len(rows) < limit

    ctypes.windll.user32.EnumWindows(EnumWindowsProc(callback), 0)
    rows.sort(key=lambda item: (item["name"].lower(), item["title"].lower()))
    return rows


def get_app_lines() -> list[str]:
    rows = get_visible_window_rows()
    return [
        f"{row['pid']} | {row['name']} | {row['title']}"
        for row in rows
    ]


def get_top_process_rows(limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            if proc.pid == 0:
                continue
            cpu = proc.cpu_percent(interval=None)
            memory = proc.info["memory_info"].rss if proc.info["memory_info"] else 0
            name = proc.info["name"] or f"pid-{proc.pid}"
            if name.lower() == "system idle process":
                continue
            rows.append(
                {
                    "pid": proc.pid,
                    "name": name,
                    "cpu": cpu,
                    "memory": memory,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda item: (item["memory"], item["cpu"]), reverse=True)
    return rows[:limit]


def get_job_lines() -> list[str]:
    return sorted(JOB_ALIASES.keys())


def get_top_process_lines(limit: int = 8) -> list[str]:
    return [
        f"{row['pid']} | {row['name']} | CPU {row['cpu']:.1f}% | RAM {human_bytes(row['memory'])}"
        for row in get_top_process_rows(limit=limit)
    ]


def wlan_available() -> bool:
    return hasattr(ctypes.windll, "wlanapi")


def wlan_call(function: Any, *args: Any) -> None:
    code = function(*args)
    if code != 0:
        raise OSError(f"WLAN error {code}")


def decode_dot11_ssid(value: DOT11_SSID) -> str:
    raw = bytes(value.ucSSID[: value.uSSIDLength])
    return raw.decode("utf-8", errors="ignore")


class WlanClient:
    def __init__(self) -> None:
        if not wlan_available():
            raise OSError("wlanapi unavailable")
        self.handle = ctypes.wintypes.HANDLE()
        negotiated_version = ctypes.wintypes.DWORD()
        wlan_call(
            ctypes.windll.wlanapi.WlanOpenHandle,
            WLAN_CLIENT_VERSION_LONGHORN,
            None,
            ctypes.byref(negotiated_version),
            ctypes.byref(self.handle),
        )

    def close(self) -> None:
        if self.handle:
            ctypes.windll.wlanapi.WlanCloseHandle(self.handle, None)
            self.handle = ctypes.wintypes.HANDLE()

    def __enter__(self) -> "WlanClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _list_from_pointer(self, pointer: ctypes.c_void_p, header_type: type[Any], item_type: type[Any], field_name: str) -> list[Any]:
        header = ctypes.cast(pointer, ctypes.POINTER(header_type)).contents
        count = int(header.dwNumberOfItems)
        first_offset = getattr(header_type, field_name).offset
        array_type = item_type * max(count, 1)
        array_ptr = ctypes.cast(ctypes.addressof(header) + first_offset, ctypes.POINTER(array_type))
        return list(array_ptr.contents[:count])

    def interfaces(self) -> list[WLAN_INTERFACE_INFO]:
        pointer = ctypes.c_void_p()
        wlan_call(ctypes.windll.wlanapi.WlanEnumInterfaces, self.handle, None, ctypes.byref(pointer))
        try:
            return self._list_from_pointer(pointer, WLAN_INTERFACE_INFO_LIST, WLAN_INTERFACE_INFO, "InterfaceInfo")
        finally:
            ctypes.windll.wlanapi.WlanFreeMemory(pointer)

    def profiles(self, interface_guid: GUID) -> list[WLAN_PROFILE_INFO]:
        pointer = ctypes.c_void_p()
        wlan_call(
            ctypes.windll.wlanapi.WlanGetProfileList,
            self.handle,
            ctypes.byref(interface_guid),
            None,
            ctypes.byref(pointer),
        )
        try:
            return self._list_from_pointer(pointer, WLAN_PROFILE_INFO_LIST, WLAN_PROFILE_INFO, "ProfileInfo")
        finally:
            ctypes.windll.wlanapi.WlanFreeMemory(pointer)

    def available_networks(self, interface_guid: GUID) -> list[WLAN_AVAILABLE_NETWORK]:
        pointer = ctypes.c_void_p()
        wlan_call(
            ctypes.windll.wlanapi.WlanGetAvailableNetworkList,
            self.handle,
            ctypes.byref(interface_guid),
            0,
            None,
            ctypes.byref(pointer),
        )
        try:
            return self._list_from_pointer(pointer, WLAN_AVAILABLE_NETWORK_LIST, WLAN_AVAILABLE_NETWORK, "Network")
        finally:
            ctypes.windll.wlanapi.WlanFreeMemory(pointer)

    def connect_profile(self, interface_guid: GUID, profile_name: str) -> None:
        params = WLAN_CONNECTION_PARAMETERS(
            wlanConnectionMode=WLAN_CONNECTION_MODE_PROFILE,
            strProfile=profile_name,
            pDot11Ssid=None,
            pDesiredBssidList=None,
            dot11BssType=DOT11_BSS_TYPE_ANY,
            dwFlags=0,
        )
        wlan_call(
            ctypes.windll.wlanapi.WlanConnect,
            self.handle,
            ctypes.byref(interface_guid),
            ctypes.byref(params),
            None,
        )


def list_wifi_profiles() -> list[str]:
    names: list[str] = []
    if wlan_available():
        try:
            with WlanClient() as client:
                for interface_info in client.interfaces():
                    for profile in client.profiles(interface_info.InterfaceGuid):
                        name = profile.strProfileName.strip()
                        if name and name not in names:
                            names.append(name)
        except Exception:
            pass
    if names:
        return names
    try:
        completed = subprocess.run(
            ["netsh", "wlan", "show", "profiles"],
            check=False,
            capture_output=True,
            text=False,
            timeout=12,
            **hidden_subprocess_kwargs(),
        )
    except Exception:
        return []
    text = decode_process_output(completed.stdout)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if ":" not in line or ("profile" not in lower and "профиль" not in lower):
            continue
        name = line.split(":", 1)[1].strip()
        if name and name not in names:
            names.append(name)
    return names


def network_probe_targets() -> list[tuple[str, int]]:
    parsed = urlparse(SERVER_URL)
    targets: list[tuple[str, int]] = []
    if parsed.hostname:
        targets.append((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)))
    targets.extend([("1.1.1.1", 53), ("8.8.8.8", 53)])
    unique: list[tuple[str, int]] = []
    for target in targets:
        if target not in unique:
            unique.append(target)
    return unique


def network_reachable(timeout: float = 2.0) -> bool:
    for host, port in network_probe_targets():
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def current_wifi_connection() -> tuple[str | None, int | None]:
    try:
        completed = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            check=False,
            capture_output=True,
            text=False,
            timeout=10,
            **hidden_subprocess_kwargs(),
        )
    except Exception:
        return None, None
    text = decode_process_output(completed.stdout) + "\n" + decode_process_output(completed.stderr)
    if "location permission" in text.lower() or "requires elevation" in text.lower():
        return None, None
    profile_name = None
    signal = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = [part.strip() for part in line.split(":", 1)]
        lower_key = key.lower()
        lower_value = value.lower()
        if "ssid" in lower_key and "bssid" not in lower_key and value:
            profile_name = value
        if ("profile" in lower_key or "профиль" in lower_key) and value and lower_value != "n/a":
            profile_name = value
        if ("signal" in lower_key or "сигнал" in lower_key) and value.endswith("%"):
            with contextlib.suppress(ValueError):
                signal = int(value.rstrip("% ").strip())
    return profile_name, signal


def get_wifi_status(include_connection: bool = False) -> dict[str, Any]:
    connected_profile = None
    connected_signal = None
    if include_connection:
        connected_profile, connected_signal = current_wifi_connection()
    info: dict[str, Any] = {
        "profiles": [],
        "visible_profiles": [],
        "connected_profile": connected_profile,
        "connected_signal": connected_signal,
        "available": False,
        "internet_ok": network_reachable(timeout=1.0),
    }
    profiles = list_wifi_profiles()
    info["profiles"] = profiles
    info["available"] = bool(profiles or connected_profile)
    return info


def connect_wifi_profile(profile_name: str) -> None:
    stripped = profile_name.strip()
    if not stripped:
        raise ValueError("Пустое имя Wi-Fi профиля.")
    completed = subprocess.run(
        ["netsh", "wlan", "connect", f"name={stripped}"],
        check=False,
        capture_output=True,
        text=False,
        timeout=15,
        **hidden_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            decode_process_output(completed.stderr)
            or decode_process_output(completed.stdout)
            or f"netsh exit {completed.returncode}"
        )


def attempt_wifi_recovery() -> dict[str, Any]:
    status = get_wifi_status()
    if not status.get("available"):
        raise ValueError("На этом ПК не найден Wi-Fi интерфейс.")

    ordered = [str(name) for name in status.get("profiles", []) if str(name).strip()]
    if not ordered:
        raise ValueError("Не найдено сохранённых Wi-Fi профилей для переподключения.")

    attempts: list[str] = []
    with WlanClient() as client:
        interfaces = client.interfaces()
        if not interfaces:
            raise ValueError("Wi-Fi интерфейс не найден.")
        interface_info = interfaces[0]
        for profile_name in ordered[:8]:
            attempts.append(profile_name)
            try:
                client.connect_profile(interface_info.InterfaceGuid, profile_name)
            except Exception:
                continue
            deadline = time.time() + WIFI_CONNECT_SETTLE_SECONDS
            while time.time() < deadline:
                if network_reachable(timeout=1.0):
                    refreshed = get_wifi_status()
                    connected = refreshed.get("connected_profile") or profile_name
                    signal = refreshed.get("connected_signal")
                    signal_text = f" ({signal}%)" if isinstance(signal, int) else ""
                    return {
                        "ok": True,
                        "message": f"Wi-Fi восстановлен через профиль {connected}{signal_text}.",
                        "data": {
                            "profile": connected,
                            "attempts": attempts,
                            "wifi": refreshed,
                        },
                    }
                time.sleep(1.0)
    raise ValueError(
        "Не удалось восстановить Wi-Fi автоматически. Попробованы профили: "
        + ", ".join(attempts)
    )


def attempt_wifi_recovery_safe() -> dict[str, Any]:
    status = get_wifi_status(include_connection=True)
    if not status.get("available"):
        raise ValueError("На этом ПК не найден Wi-Fi адаптер или не видны сохранённые Wi-Fi профили.")

    ordered = [str(name) for name in status.get("profiles", []) if str(name).strip()]
    current = str(status.get("connected_profile") or "").strip()
    if current:
        ordered = [current, *[name for name in ordered if name.lower() != current.lower()]]
    if not ordered:
        raise ValueError("Не найдены сохранённые Wi-Fi профили для переподключения.")

    attempts: list[str] = []
    for profile_name in ordered[:8]:
        attempts.append(profile_name)
        try:
            connect_wifi_profile(profile_name)
        except Exception:
            continue
        deadline = time.time() + WIFI_CONNECT_SETTLE_SECONDS
        while time.time() < deadline:
            refreshed = get_wifi_status(include_connection=True)
            connected = refreshed.get("connected_profile")
            if network_reachable(timeout=1.0) or (connected and connected.lower() == profile_name.lower()):
                signal = refreshed.get("connected_signal")
                signal_text = f" ({signal}%)" if isinstance(signal, int) else ""
                return {
                    "ok": True,
                    "message": f"Wi-Fi восстановлен через профиль {connected or profile_name}{signal_text}.",
                    "data": {
                        "profile": connected or profile_name,
                        "attempts": attempts,
                        "wifi": refreshed,
                    },
                }
            time.sleep(1.0)
    raise ValueError("Не удалось восстановить Wi-Fi автоматически. Попробованы профили: " + ", ".join(attempts))


def collect_snapshot(previous_net: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    vm = psutil.virtual_memory()
    system_drive = os.environ.get("SystemDrive", "C:")
    disk = psutil.disk_usage(f"{system_drive}\\")
    net = psutil.net_io_counters()
    now = time.time()

    down_per_sec = 0.0
    up_per_sec = 0.0
    if previous_net:
        elapsed = max(now - previous_net["time"], 1e-6)
        down_per_sec = max(net.bytes_recv - previous_net["bytes_recv"], 0) / elapsed
        up_per_sec = max(net.bytes_sent - previous_net["bytes_sent"], 0) / elapsed

    wifi = get_wifi_status()
    snapshot = {
        "hostname": socket.gethostname(),
        "username": getpass.getuser(),
        "os": windows_version_label(),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "cpu_percent": round(psutil.cpu_percent(interval=0.2), 1),
        "memory_percent": round(vm.percent, 1),
        "disk_percent": round(disk.percent, 1),
        "memory_used": vm.used,
        "memory_total": vm.total,
        "disk_free": disk.free,
        "disk_total": disk.total,
        "ip_addresses": get_ip_addresses(),
        "net_down_per_sec": int(down_per_sec),
        "net_up_per_sec": int(up_per_sec),
        "drives": get_drive_lines(),
        "services": get_service_lines(),
        "apps": get_app_lines(),
        "top_processes": get_top_process_lines(),
        "jobs": get_job_lines(),
        "wifi_profile": wifi.get("connected_profile"),
        "wifi_signal": wifi.get("connected_signal"),
        "wifi_known_profiles": [row.get("profile") for row in wifi.get("visible_profiles", []) if row.get("profile")],
        "wifi_profiles_saved": wifi.get("profiles", []),
        "internet_ok": bool(wifi.get("internet_ok")),
    }
    next_net = {
        "bytes_recv": net.bytes_recv,
        "bytes_sent": net.bytes_sent,
        "time": now,
    }
    return snapshot, next_net


def register_agent(session: requests.Session, state: dict[str, Any]) -> dict[str, Any]:
    response = session.post(
        f"{SERVER_URL}/api/register",
        json={
            "registration_key": REGISTRATION_KEY,
            "device_id": state["device_id"],
            "display_name": state["display_name"],
            "hostname": socket.gethostname(),
            "agent_version": AGENT_VERSION,
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    state["agent_token"] = response.json()["agent_token"]
    save_state(state)
    return state


def ensure_registered(session: requests.Session) -> dict[str, Any]:
    state = load_state()
    if "device_id" not in state:
        state["device_id"] = stable_device_id()
    if "display_name" not in state:
        state["display_name"] = socket.gethostname()
    if not str(state.get("update_url") or "").strip():
        state["update_url"] = DEFAULT_AGENT_UPDATE_URL
    if "agent_token" not in state:
        state = register_agent(session, state)
    return state


def heartbeat(session: requests.Session, state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    response = session.post(
        f"{SERVER_URL}/api/heartbeat",
        json={
            "device_id": state["device_id"],
            "agent_token": state["agent_token"],
            "snapshot": snapshot,
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code == 403:
        state.pop("agent_token", None)
        save_state(state)
        raise RuntimeError("Токен агента отклонён. Перерегистрируй агент.")
    response.raise_for_status()


def fetch_command(session: requests.Session, state: dict[str, Any]) -> dict[str, Any] | None:
    response = session.post(
        f"{SERVER_URL}/api/command/next",
        json={
            "device_id": state["device_id"],
            "agent_token": state["agent_token"],
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code == 403:
        state.pop("agent_token", None)
        save_state(state)
        raise RuntimeError("Токен агента отклонён. Перерегистрируй агент.")
    response.raise_for_status()
    return response.json().get("command")


def post_result(
    session: requests.Session,
    state: dict[str, Any],
    command_id: str,
    ok: bool,
    message: str,
    data: dict[str, Any] | None = None,
    file_name: str | None = None,
    file_b64: str | None = None,
) -> None:
    response = session.post(
        f"{SERVER_URL}/api/command/result",
        json={
            "device_id": state["device_id"],
            "agent_token": state["agent_token"],
            "command_id": command_id,
            "ok": ok,
            "message": message,
            "data": data or {},
            "file_name": file_name,
            "file_b64": file_b64,
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def info_text(snapshot: dict[str, Any]) -> str:
    wifi_profile = snapshot.get("wifi_profile") or "н/д"
    wifi_signal = snapshot.get("wifi_signal")
    wifi_line = f"{wifi_profile}"
    if wifi_signal is not None:
        wifi_line += f" ({wifi_signal}%)"
    internet_state = "ok" if snapshot.get("internet_ok") else "нет"
    return (
        f"Хост: {snapshot['hostname']}\n"
        f"Пользователь: {snapshot['username']}\n"
        f"ОС: {snapshot['os']}\n"
        f"Аптайм: {snapshot['uptime_seconds']} сек\n"
        f"CPU: {snapshot['cpu_percent']}%\n"
        f"RAM: {snapshot['memory_percent']}% "
        f"({human_bytes(snapshot['memory_used'])}/{human_bytes(snapshot['memory_total'])})\n"
        f"Диск C: {snapshot['disk_percent']}% "
        f"(свободно {human_bytes(snapshot['disk_free'])} из {human_bytes(snapshot['disk_total'])})\n"
        f"IP: {', '.join(snapshot['ip_addresses']) or 'н/д'}\n"
        f"Wi-Fi: {wifi_line}\n"
        f"Интернет: {internet_state}"
    )


def collect_hardware_specs(snapshot: dict[str, Any]) -> dict[str, Any]:
    cpu_name = platform.processor().strip() or "н/д"
    motherboard = "н/д"
    bios = "н/д"
    gpu_lines: list[str] = []
    gpu_temp = "н/д"

    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode == 0 and completed.stdout.strip():
            base = json.loads(completed.stdout)
            motherboard = " ".join(filter(None, [str(base.get("Manufacturer", "")).strip(), str(base.get("Product", "")).strip()])).strip() or "н/д"
    except Exception:
        pass

    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_BIOS | Select-Object SMBIOSBIOSVersion | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode == 0 and completed.stdout.strip():
            bios_info = json.loads(completed.stdout)
            bios = str(bios_info.get("SMBIOSBIOSVersion", "")).strip() or "н/д"
    except Exception:
        pass

    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=12,
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode == 0 and completed.stdout.strip():
            raw = json.loads(completed.stdout)
            controllers = raw if isinstance(raw, list) else [raw]
            for item in controllers:
                name = str(item.get("Name", "")).strip()
                ram = item.get("AdapterRAM")
                driver = str(item.get("DriverVersion", "")).strip()
                parts = [name or "GPU"]
                if ram:
                    parts.append(human_bytes(ram))
                if driver:
                    parts.append(f"drv {driver}")
                gpu_lines.append(" | ".join(parts))
    except Exception:
        pass

    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=6,
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode == 0:
            temp_line = completed.stdout.splitlines()[0].strip()
            if temp_line:
                gpu_temp = f"{temp_line}°C"
    except Exception:
        pass

    return {
        "cpu_name": cpu_name,
        "cpu_cores": psutil.cpu_count(logical=False) or psutil.cpu_count() or 0,
        "cpu_threads": psutil.cpu_count() or 0,
        "ram_total": snapshot.get("memory_total"),
        "motherboard": motherboard,
        "bios": bios,
        "gpus": gpu_lines,
        "gpu_temp": gpu_temp,
        "drives": snapshot.get("drives", []),
    }


def specs_text(snapshot: dict[str, Any]) -> str:
    specs = collect_hardware_specs(snapshot)
    gpu_block = "\n".join(specs["gpus"]) if specs["gpus"] else "н/д"
    drives_block = "\n".join(specs["drives"]) if specs["drives"] else "н/д"
    return (
        f"CPU: {specs['cpu_name']}\n"
        f"Ядра/потоки: {specs['cpu_cores']}/{specs['cpu_threads']}\n"
        f"RAM: {human_bytes(specs['ram_total'])}\n"
        f"Матплата: {specs['motherboard']}\n"
        f"BIOS: {specs['bios']}\n"
        f"GPU температура: {specs['gpu_temp']}\n"
        f"Видеокарты:\n{gpu_block}\n\n"
        f"Диски:\n{drives_block}"
    )


def list_drives() -> str:
    lines = get_drive_lines()
    return "\n".join(lines) or "Диски не найдены."


def list_services() -> str:
    return "\n".join(get_service_lines()) or "Службы не настроены."


def list_apps() -> str:
    lines = get_app_lines()
    return "\n".join(lines) or "Открытые окна не найдены."


def list_top_processes() -> str:
    lines = get_top_process_lines()
    return "\n".join(lines) or "Не удалось собрать список процессов."


def list_jobs() -> str:
    jobs = get_job_lines()
    return "\n".join(jobs) or "Джобы не настроены."


def decode_process_output(raw: bytes | None) -> str:
    if not raw:
        return ""
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig", errors="replace")
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16", errors="replace")
    encodings = [
        "utf-8",
        "utf-16",
        "utf-16-le",
        "cp866",
        "cp1251",
    ]
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def trim_output(value: str, limit: int) -> tuple[str, bool]:
    text = value.replace("\r\n", "\n").strip()
    if len(text) <= limit:
        return text, False
    return f"{text[: limit - 3].rstrip()}...", True


def make_text_file_payload(prefix: str, stem: str, text: str) -> tuple[str | None, str | None]:
    value = text.strip()
    if not value:
        return None, None
    raw = value.encode("utf-8", errors="replace")
    if len(raw) > SHELL_COMMAND_FILE_LIMIT_BYTES:
        return None, None
    safe_stem = re.sub(r"[^a-zA-Z0-9_.-]+", "_", stem).strip("._") or "output"
    return f"{prefix}_{safe_stem}.txt", base64.b64encode(raw).decode("ascii")


def build_powershell_command(command: str) -> str:
    utf8_init = (
        "$utf8 = [System.Text.UTF8Encoding]::new($false); "
        "[Console]::InputEncoding = $utf8; "
        "[Console]::OutputEncoding = $utf8; "
        "$OutputEncoding = $utf8; "
        "chcp.com 65001 > $null; "
    )
    return f"{utf8_init}& {{ {command} }}"


def run_shell_command(command: str) -> dict[str, Any]:
    command_text = command.strip()
    if not command_text:
        raise ValueError("После /cmd нужно передать команду.")
    prepared_command = build_powershell_command(command_text)

    started_at = time.perf_counter()
    timed_out = False
    return_code: int | None = None
    stdout_text = ""
    stderr_text = ""

    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                prepared_command,
            ],
            capture_output=True,
            text=False,
            timeout=SHELL_COMMAND_TIMEOUT_SECONDS,
            shell=False,
            cwd=SHELL_COMMAND_CWD,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return_code = completed.returncode
        stdout_text = decode_process_output(completed.stdout)
        stderr_text = decode_process_output(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_text = decode_process_output(exc.stdout)
        stderr_text = decode_process_output(exc.stderr)

    stdout_preview, stdout_cut = trim_output(stdout_text, SHELL_COMMAND_PREVIEW_CHARS)
    stderr_preview, stderr_cut = trim_output(stderr_text, SHELL_COMMAND_PREVIEW_CHARS)
    duration = round(time.perf_counter() - started_at, 2)
    file_name = None
    file_b64 = None
    combined_output = "\n\n".join(
        chunk
        for chunk in [
            f"STDOUT\n{stdout_text.strip()}" if stdout_text.strip() else "",
            f"STDERR\n{stderr_text.strip()}" if stderr_text.strip() else "",
        ]
        if chunk
    )
    if stdout_cut or stderr_cut or len(combined_output) > SHELL_COMMAND_PREVIEW_CHARS:
        file_name, file_b64 = make_text_file_payload("cmd", "powershell_output", combined_output or command_text)

    summary_parts = [
        f"Shell: powershell.exe",
        f"Папка: {SHELL_COMMAND_CWD}",
        f"Время: {duration:.2f} с",
    ]
    if return_code is not None:
        summary_parts.append(f"Код выхода: {return_code}")
    if timed_out:
        summary_parts.append(f"Таймаут: {SHELL_COMMAND_TIMEOUT_SECONDS} с")

    if stdout_preview:
        summary_parts.append(f"STDOUT:\n{stdout_preview}")
    if stderr_preview:
        summary_parts.append(f"STDERR:\n{stderr_preview}")
    if not stdout_preview and not stderr_preview:
        summary_parts.append("Команда не вывела текст.")
    if stdout_cut or stderr_cut:
        summary_parts.append("Вывод обрезан.")

    ok = not timed_out and return_code == 0
    if timed_out:
        headline = f"Команда не завершилась за {SHELL_COMMAND_TIMEOUT_SECONDS} секунд."
    elif return_code == 0:
        headline = "Команда выполнена."
    else:
        headline = f"Команда завершилась с кодом {return_code}."

    return {
        "ok": ok,
        "message": f"{headline}\n\n" + "\n\n".join(summary_parts),
        "data": {
            "command": command_text,
            "shell": "powershell.exe",
            "cwd": SHELL_COMMAND_CWD,
            "returncode": return_code,
            "duration_sec": duration,
            "stdout": stdout_preview,
            "stderr": stderr_preview,
            "timed_out": timed_out,
            "timeout_sec": SHELL_COMMAND_TIMEOUT_SECONDS,
            "truncated": stdout_cut or stderr_cut,
        },
        "file_name": file_name,
        "file_b64": file_b64,
    }


def start_alias(alias: str) -> str:
    app = APP_ALIASES.get(alias)
    if not app:
        raise ValueError(f"Неизвестный алиас программы: {alias}")
    command_line = os.path.expandvars(str(app["start"]))
    if command_line.lower().endswith(".exe") and " " not in command_line.strip():
        os.startfile(command_line)
    else:
        subprocess.Popen(shlex.split(command_line, posix=False), shell=False, **hidden_subprocess_kwargs())
    return f"Запущено: {alias}."


def run_job(alias: str) -> str:
    job = JOB_ALIASES.get(alias)
    if not job:
        raise ValueError(f"Неизвестная джоба: {alias}")
    command_line = os.path.expandvars(str(job["command"]))
    cwd = os.path.expandvars(str(job.get("cwd", BASE_DIR)))
    if command_line.lower().endswith(".exe") and " " not in command_line.strip():
        os.startfile(command_line)
    else:
        subprocess.Popen(
            shlex.split(command_line, posix=False),
            shell=False,
            cwd=cwd,
            **hidden_subprocess_kwargs(),
        )
    return f"Джоба запущена: {alias}."

def close_windows_for_pid(pid: int) -> int:
    closed_hwnds: set[int] = set()

    def callback(hwnd: int, lparam: int) -> bool:
        if not IsWindowVisible(hwnd):
            return True
        process_id = ctypes.wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        try:
            proc = psutil.Process(process_id.value)
        except psutil.Error:
            return True
        if process_id.value == pid:
            closed_hwnds.add(hwnd)
            PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    ctypes.windll.user32.EnumWindows(EnumWindowsProc(callback), 0)
    return len(closed_hwnds)


def resolve_pids(target: str) -> list[int]:
    value = target.strip()
    if not value:
        raise ValueError("Не указан PID или имя процесса.")
    if value.isdigit():
        return [int(value)]
    lower_value = value.lower()
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info["name"] or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name == lower_value:
            pids.append(proc.pid)
    if not pids:
        raise ValueError(f"Процесс не найден: {target}")
    return sorted(set(pids))


def close_process(target: str) -> str:
    total = 0
    pids = resolve_pids(target)
    for pid in pids:
        total += close_windows_for_pid(pid)
    if total == 0:
        return f"Видимых окон для {target} не найдено."
    return f"Запрошено закрытие. PID/процесс: {target}. Окон: {total}."


def close_foreground_window() -> str:
    hwnd = int(GetForegroundWindow())
    if not hwnd:
        raise ValueError("Активное окно не найдено.")
    title = get_window_title(hwnd) or "без названия"
    PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return f"Команда закрытия отправлена для верхнего окна: {title}."


def terminate_process(target: str) -> str:
    pids = resolve_pids(target)
    killed: list[int] = []
    errors: list[str] = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                proc.kill()
            killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            errors.append(f"{pid}: {exc}")
    if not killed and errors:
        raise ValueError("; ".join(errors))
    suffix = f" Ошибки: {'; '.join(errors)}" if errors else ""
    return f"Завершены PID: {', '.join(map(str, killed))}.{suffix}"


def restart_alias(alias: str) -> str:
    app = APP_ALIASES.get(alias)
    if not app:
        raise ValueError(f"Неизвестный алиас программы: {alias}")
    close_message = ""
    for process_name in app.get("processes", []):
        close_message = close_process(process_name)
    time.sleep(2)
    start_message = start_alias(alias)
    return f"{close_message}\n{start_message}"


def file_search_roots() -> list[Path]:
    home = Path.home()
    roots = [
        home / "Desktop",
        home / "Downloads",
        home / "Documents",
        home / "Pictures",
        home / "Videos",
        Path(os.environ.get("LOCALAPPDATA", "")) / "SystemPortal",
        BASE_DIR,
    ]
    return [root for root in roots if root.exists()]


def candidate_file_score(path: Path, query_lower: str) -> tuple[int, int, int, str]:
    path_lower = str(path).lower()
    name_lower = path.name.lower()
    exact_name = 0 if name_lower == query_lower else 1
    name_contains = 0 if query_lower in name_lower else 1
    path_contains = 0 if query_lower in path_lower else 1
    return (exact_name, name_contains + path_contains, len(path_lower), path_lower)


def find_file_match(query: str) -> Path:
    raw_query = query.strip().strip('"')
    candidate = Path(os.path.expandvars(raw_query))
    if candidate.exists() and candidate.is_file():
        return candidate

    query_lower = raw_query.lower()
    if not query_lower:
        raise ValueError("После /file нужно передать имя или путь.")

    excluded_parts = {".venv", "__pycache__", ".git", "node_modules"}
    matches: list[Path] = []
    for root in file_search_roots():
        for current_root, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name.lower() not in excluded_parts]
            current_path = Path(current_root)
            for file_name in files:
                full_path = current_path / file_name
                full_lower = str(full_path).lower()
                if query_lower not in file_name.lower() and query_lower not in full_lower:
                    continue
                matches.append(full_path)
                if len(matches) >= FILE_SEARCH_MAX_RESULTS:
                    break
            if len(matches) >= FILE_SEARCH_MAX_RESULTS:
                break
        if len(matches) >= FILE_SEARCH_MAX_RESULTS:
            break
    if not matches:
        raise ValueError(f"Файл не найден: {query}")
    matches.sort(key=lambda path: candidate_file_score(path, query_lower))
    return matches[0]


def send_found_file(query: str) -> dict[str, Any]:
    path = find_file_match(query)
    size_bytes = path.stat().st_size
    if size_bytes > FILE_TRANSFER_LIMIT_BYTES:
        raise ValueError(
            f"Файл найден, но он слишком большой для отправки через Telegram: {path} ({human_bytes(size_bytes)})."
        )
    return {
        "ok": True,
        "message": f"Файл найден: {path}",
        "data": {
            "query": query,
            "path": str(path),
            "size_bytes": size_bytes,
        },
        "file_name": path.name,
        "file_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def get_screen_size() -> tuple[int, int]:
    return max(GetSystemMetrics(SM_CXSCREEN), 1), max(GetSystemMetrics(SM_CYSCREEN), 1)


def capture_screen(max_width: int, max_height: int, quality: int) -> tuple[str, str, dict[str, Any]]:
    width, height = get_screen_size()
    image = ImageGrab.grab()
    image = image.convert("RGB")
    image.thumbnail((max_width, max_height))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return (
        "desktop.jpg",
        base64.b64encode(buffer.getvalue()).decode("ascii"),
        {
            "screen_width": width,
            "screen_height": height,
            "image_width": image.width,
            "image_height": image.height,
            "captured_at": int(time.time()),
        },
    )


def screenshot_data() -> tuple[str, str]:
    file_name, file_b64, _ = capture_screen(
        max_width=STANDARD_SCREENSHOT_MAX_WIDTH,
        max_height=STANDARD_SCREENSHOT_MAX_HEIGHT,
        quality=STANDARD_SCREENSHOT_QUALITY,
    )
    return file_name, file_b64


def remote_frame_data() -> tuple[str, str, dict[str, Any]]:
    return capture_screen(
        max_width=REMOTE_FRAME_MAX_WIDTH,
        max_height=REMOTE_FRAME_MAX_HEIGHT,
        quality=REMOTE_FRAME_QUALITY,
    )


def lock_pc() -> str:
    LockWorkStation()
    return "Команда блокировки отправлена."


def restart_pc() -> str:
    subprocess.Popen(["shutdown", "/r", "/t", "5"], shell=False, **hidden_subprocess_kwargs())
    return "Перезагрузка запланирована через 5 секунд."


def shutdown_pc() -> str:
    subprocess.Popen(["shutdown", "/s", "/t", "5"], shell=False, **hidden_subprocess_kwargs())
    return "Выключение запланировано через 5 секунд."


def get_mouse_sender(flags: int, data: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        union=INPUT_UNION(
            mi=MOUSEINPUT(
                dx=0,
                dy=0,
                mouseData=data,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )


def send_mouse_flags(flags: int, data: int = 0) -> None:
    input_item = get_mouse_sender(flags, data)
    result = SendInput(1, ctypes.byref(input_item), ctypes.sizeof(INPUT))
    if result != 1:
        raise OSError("Не удалось отправить мышиное событие.")


def get_keyboard_sender(vk_code: int, flags: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        union=INPUT_UNION(
            ki=KEYBDINPUT(
                wVk=vk_code,
                wScan=0,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )


def tap_key(vk_code: int) -> None:
    press = get_keyboard_sender(vk_code)
    release = get_keyboard_sender(vk_code, KEYEVENTF_KEYUP)
    count = SendInput(2, (INPUT * 2)(press, release), ctypes.sizeof(INPUT))
    if count != 2:
        raise OSError("Не удалось отправить нажатие клавиши.")


def chord_ctrl_v() -> None:
    events = (INPUT * 4)(
        get_keyboard_sender(VK_CONTROL),
        get_keyboard_sender(VK_V),
        get_keyboard_sender(VK_V, KEYEVENTF_KEYUP),
        get_keyboard_sender(VK_CONTROL, KEYEVENTF_KEYUP),
    )
    count = SendInput(4, events, ctypes.sizeof(INPUT))
    if count != 4:
        raise OSError("Не удалось вставить текст через Ctrl+V.")


def set_clipboard_text(text: str) -> None:
    payload = ctypes.create_unicode_buffer(text + "\x00")
    payload_size = ctypes.sizeof(payload)
    handle = GlobalAlloc(GMEM_MOVEABLE, payload_size)
    if not handle:
        raise RuntimeError("Не удалось выделить память под буфер обмена.")
    locked = GlobalLock(handle)
    if not locked:
        raise RuntimeError("Не удалось открыть память буфера обмена.")
    try:
        RtlMoveMemory(locked, ctypes.byref(payload), payload_size)
    finally:
        GlobalUnlock(handle)
    if not OpenClipboard(None):
        raise RuntimeError("Не удалось открыть буфер обмена.")
    try:
        if not EmptyClipboard():
            raise RuntimeError("Не удалось очистить буфер обмена.")
        if not SetClipboardData(CF_UNICODETEXT, handle):
            raise RuntimeError("Не удалось записать Unicode-текст в буфер обмена.")
        handle = None
    finally:
        CloseClipboard()


def type_text_into_active_window(text: str) -> None:
    value = text.replace("\r\n", "\n").strip()
    if not value:
        raise ValueError("Пустой текст для ввода.")
    set_clipboard_text(value)
    time.sleep(0.06)
    chord_ctrl_v()


def clamp_coordinate(value: float, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(round(value)), maximum))


def move_cursor(x: float, y: float) -> tuple[int, int]:
    screen_width, screen_height = get_screen_size()
    target_x = clamp_coordinate(x, 0, max(screen_width - 1, 0))
    target_y = clamp_coordinate(y, 0, max(screen_height - 1, 0))
    if not SetCursorPos(target_x, target_y):
        raise OSError("Не удалось переместить курсор.")
    return target_x, target_y


def perform_remote_input(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action", "")).strip().lower()
    if action == "type_text":
        value = str(args.get("text", ""))
        type_text_into_active_window(value)
        return {
            "ok": True,
            "message": "Текст отправлен в активное окно.",
            "data": {"action": action, "length": len(value)},
        }
    if action == "key_enter":
        tap_key(VK_RETURN)
        return {
            "ok": True,
            "message": "Enter отправлен в активное окно.",
            "data": {"action": action},
        }

    x = float(args.get("x", 0))
    y = float(args.get("y", 0))
    button = str(args.get("button", "left")).strip().lower() or "left"
    target_x, target_y = move_cursor(x, y)

    if action == "move":
        return {
            "ok": True,
            "message": "",
            "data": {"x": target_x, "y": target_y, "action": action},
        }
    if action == "click":
        if button == "right":
            send_mouse_flags(MOUSEEVENTF_RIGHTDOWN)
            send_mouse_flags(MOUSEEVENTF_RIGHTUP)
        else:
            send_mouse_flags(MOUSEEVENTF_LEFTDOWN)
            send_mouse_flags(MOUSEEVENTF_LEFTUP)
        return {
            "ok": True,
            "message": f"Клик {button} в {target_x}, {target_y}.",
            "data": {"x": target_x, "y": target_y, "action": action, "button": button},
        }
    if action == "button_down":
        send_mouse_flags(MOUSEEVENTF_RIGHTDOWN if button == "right" else MOUSEEVENTF_LEFTDOWN)
        return {
            "ok": True,
            "message": f"Кнопка {button} зажата в {target_x}, {target_y}.",
            "data": {"x": target_x, "y": target_y, "action": action, "button": button},
        }
    if action == "button_up":
        send_mouse_flags(MOUSEEVENTF_RIGHTUP if button == "right" else MOUSEEVENTF_LEFTUP)
        return {
            "ok": True,
            "message": f"Кнопка {button} отпущена в {target_x}, {target_y}.",
            "data": {"x": target_x, "y": target_y, "action": action, "button": button},
        }
    if action == "scroll":
        delta = int(args.get("delta", 0))
        send_mouse_flags(MOUSEEVENTF_WHEEL, delta)
        return {
            "ok": True,
            "message": f"Колесо прокручено на {delta}.",
            "data": {"x": target_x, "y": target_y, "action": action, "delta": delta},
        }
    raise ValueError(f"Неподдерживаемое remote-действие: {action}")


def install_registry_run_entry(entry_name: str, command_line: str) -> bool:
    if "winreg" not in globals():
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, entry_name, 0, winreg.REG_SZ, command_line)
        return True
    except OSError:
        return False


def install_startup_vbs(entry_name: str, launcher: Path, script_path: str) -> None:
    startup_path = startup_vbs_path(entry_name)
    startup_path.parent.mkdir(parents=True, exist_ok=True)
    startup_path.write_text(
        "\r\n".join(
            [
                'Set shell = CreateObject("WScript.Shell")',
                f'shell.Run Chr(34) & "{launcher}" & Chr(34) & " " & Chr(34) & "{script_path}" & Chr(34), 0',
                "",
            ]
        ),
        encoding="ascii",
    )


def install_startup_task() -> None:
    python_exe = Path(sys.executable).resolve()
    pythonw_exe = python_exe.with_name("pythonw.exe")
    script_path = str(Path(__file__).resolve())
    entry_name = startup_entry_name()
    mode = "registry_run"

    cleanup_legacy_scheduled_tasks()
    cleanup_legacy_startup_files()

    launcher = pythonw_exe if pythonw_exe.exists() else python_exe
    command_line = f'"{launcher}" "{script_path}"'
    if not install_registry_run_entry(entry_name, command_line):
        raise RuntimeError("Не удалось создать запись автозапуска в HKCU\\...\\Run.")

    with contextlib.suppress(FileNotFoundError):
        startup_vbs_path(entry_name).unlink()

    with contextlib.suppress(FileNotFoundError):
        legacy_startup_cmd_path(entry_name).unlink()

    state = load_state()
    state["startup_entry_name"] = entry_name
    state["startup_mode"] = mode
    save_state(state)


def remove_startup_entry() -> None:
    state = load_state()
    entry_names = {
        str(state.get("startup_entry_name") or startup_entry_name()).strip() or DEFAULT_STARTUP_ENTRY_NAME,
        DEFAULT_STARTUP_ENTRY_NAME,
        *LEGACY_STARTUP_ENTRY_NAMES,
    }

    cleanup_legacy_scheduled_tasks()

    if "winreg" in globals():
        with contextlib.suppress(OSError):
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                for entry_name in entry_names:
                    with contextlib.suppress(OSError):
                        winreg.DeleteValue(key, entry_name)

    for entry_name in entry_names:
        with contextlib.suppress(FileNotFoundError):
            startup_vbs_path(entry_name).unlink()
        with contextlib.suppress(FileNotFoundError):
            legacy_startup_cmd_path(entry_name).unlink()
    cleanup_legacy_startup_files()


def managed_install_dir() -> bool:
    return BASE_DIR.name.lower() in {"schoolpro", "systemportal"} or (BASE_DIR / "INSTALL.txt").exists()


def build_self_uninstall_script(entry_name: str, target_dir: Path) -> Path:
    cleanup_names = [entry_name, DEFAULT_STARTUP_ENTRY_NAME, *LEGACY_STARTUP_ENTRY_NAMES]
    cleanup_commands = [f'reg delete "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" /v "{name}" /f >nul 2>&1' for name in cleanup_names]
    cleanup_commands.extend(f'schtasks /Delete /TN "{name}" /F >nul 2>&1' for name in cleanup_names)
    cleanup_commands.extend(f'del "{startup_vbs_path(name)}" >nul 2>&1' for name in cleanup_names)
    cleanup_commands.extend(f'del "{legacy_startup_cmd_path(name)}" >nul 2>&1' for name in cleanup_names)
    cleanup_commands.extend(
        [
            f'del "{startup_dir()}\\SystemPortal*.cmd" >nul 2>&1',
            f'del "{startup_dir()}\\SystemPortal*.vbs" >nul 2>&1',
            f'del "{startup_dir()}\\SchoolPro*.cmd" >nul 2>&1',
            f'del "{startup_dir()}\\SchoolPro*.vbs" >nul 2>&1',
            f'del "{startup_dir()}\\SafePc*.cmd" >nul 2>&1',
            f'del "{startup_dir()}\\SafePc*.vbs" >nul 2>&1',
        ]
    )
    cleanup_script = Path(tempfile.gettempdir()) / f"systemportal_cleanup_{os.getpid()}.cmd"
    cleanup_script.write_text(
        "\r\n".join(
            [
                "@echo off",
                "setlocal",
                "timeout /t 4 /nobreak >nul",
                *cleanup_commands,
                "for /l %%i in (1,1,12) do (",
                f'  rmdir /s /q "{target_dir}" >nul 2>&1',
                f'  if not exist "{target_dir}\\" goto cleanup_done',
                "  timeout /t 1 /nobreak >nul",
                ")",
                ":cleanup_done",
                '(goto) 2>nul & del "%~f0"',
                "",
            ]
        ),
        encoding="ascii",
    )
    return cleanup_script


def uninstall_self() -> dict[str, Any]:
    if not managed_install_dir():
        raise ValueError("Самоудаление разрешено только для установленной папки SystemPortal.")

    state = load_state()
    entry_name = str(state.get("startup_entry_name") or startup_entry_name()).strip() or DEFAULT_STARTUP_ENTRY_NAME
    cleanup_script = build_self_uninstall_script(entry_name, BASE_DIR)

    subprocess.Popen(
        ["cmd.exe", "/c", str(cleanup_script)],
        shell=False,
        **hidden_subprocess_kwargs(),
    )
    return {
        "ok": True,
        "message": "Удаление SystemPortal запущено. Агент завершит работу и очистит установку.",
        "data": {
            "entry_name": entry_name,
            "target_dir": str(BASE_DIR),
        },
        "shutdown_after_result": True,
    }


def parse_remote_version(source_text: str) -> str | None:
    match = re.search(r'^AGENT_VERSION\s*=\s*"([^"]+)"', source_text, flags=re.MULTILINE)
    if match:
        return match.group(1)
    return None


def write_temp_python_source(source_text: str) -> Path:
    temp_dir = Path(tempfile.gettempdir())
    temp_path = temp_dir / f"systemportal_agent_update_{os.getpid()}.py"
    temp_path.write_text(source_text, encoding="utf-8")
    return temp_path


def resolve_update_url(state: dict[str, Any]) -> str:
    raw = str(state.get("update_url") or DEFAULT_AGENT_UPDATE_URL).strip()
    if not raw:
        raise ValueError("URL обновления не настроен.")
    return raw


def ensure_hidden_python() -> Path:
    current = Path(sys.executable).resolve()
    pythonw = current.with_name("pythonw.exe")
    return pythonw if pythonw.exists() else current


def restart_self() -> None:
    launcher = ensure_hidden_python()
    subprocess.Popen(
        [str(launcher), str(Path(__file__).resolve())],
        shell=False,
        cwd=str(BASE_DIR),
        **hidden_subprocess_kwargs(),
    )


def update_install_summary() -> None:
    state = load_state()
    summary = "\n".join(
        [
            "SystemPortal installed.",
            "",
            "Folder:",
            str(BASE_DIR),
            "",
            "Agent file:",
            str(Path(__file__).resolve()),
            "",
            "Startup entry:",
            str(state.get("startup_entry_name") or startup_entry_name()),
            "",
            "Autostart mode:",
            str(state.get("startup_mode") or "per-user hidden logon start"),
            "",
            "Update URL:",
            str(state.get("update_url") or DEFAULT_AGENT_UPDATE_URL),
            "",
        ]
    )
    (BASE_DIR / "INSTALL.txt").write_text(summary, encoding="utf-8")


def perform_self_update(session: requests.Session) -> dict[str, Any]:
    state = load_state()
    update_url = resolve_update_url(state)
    response = session.get(update_url, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    source_text = response.text
    if "def handle_command" not in source_text or "AGENT_VERSION" not in source_text:
        raise ValueError("Скачанный файл не похож на pc_agent.py")

    temp_source = write_temp_python_source(source_text)
    try:
        py_compile.compile(str(temp_source), doraise=True)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_source.unlink()

    current_path = Path(__file__).resolve()
    current_bytes = current_path.read_bytes()
    incoming_bytes = source_text.encode("utf-8")
    current_hash = hashlib.sha256(current_bytes).hexdigest()
    incoming_hash = hashlib.sha256(incoming_bytes).hexdigest()
    remote_version = parse_remote_version(source_text) or "unknown"
    if current_hash == incoming_hash:
        install_startup_task()
        update_install_summary()
        return {
            "ok": True,
            "message": f"Агент уже актуален. Версия {AGENT_VERSION}.",
            "data": {
                "version": AGENT_VERSION,
                "remote_version": remote_version,
                "updated": False,
                "update_url": update_url,
            },
        }

    backup_path = current_path.with_suffix(".py.bak")
    backup_path.write_bytes(current_bytes)
    current_path.write_text(source_text, encoding="utf-8")

    state["update_url"] = update_url
    save_state(state)
    install_startup_task()
    update_install_summary()

    return {
        "ok": True,
        "message": f"Агент обновлён до версии {remote_version}. Перезапускаю процесс.",
        "data": {
            "version": AGENT_VERSION,
            "remote_version": remote_version,
            "updated": True,
            "update_url": update_url,
        },
        "restart_after_result": True,
    }


def show_text_popup(text: str) -> dict[str, Any]:
    value = text.strip()
    if not value:
        raise ValueError("После /text нужно передать текст.")
    render_popup_window(value)
    return {
        "ok": True,
        "message": "Текстовое окно показано на экране.",
        "data": {"text": value, "duration_seconds": TEXT_WINDOW_SECONDS},
    }


def render_popup_window(text: str) -> None:
    if "tk" not in globals():
        raise RuntimeError("Tkinter недоступен для показа текста.")
    value = text.replace("\r\n", "\n").strip()
    if not value:
        value = " "
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#09090b")
    root.wm_attributes("-alpha", 0.96)
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    width = min(880, max(520, screen_w - 220))
    height = min(340, max(180, screen_h // 3))
    pos_x = max((screen_w - width) // 2, 24)
    pos_y = max((screen_h - height) // 3, 24)
    root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")

    shell = tk.Frame(root, bg="#111827", highlightthickness=2, highlightbackground="#38bdf8")
    shell.pack(fill="both", expand=True)
    top = tk.Frame(shell, bg="#111827", padx=18, pady=14)
    top.pack(fill="x")
    body = tk.Frame(shell, bg="#111827", padx=18, pady=10)
    body.pack(fill="both", expand=True)

    title = tk.Label(top, text="Сообщение", fg="#f8fafc", bg="#111827", font=("Segoe UI Semibold", 16))
    title.pack(side="left")
    close_btn = tk.Button(
        top,
        text="×",
        command=root.destroy,
        bg="#111827",
        fg="#e5e7eb",
        relief="flat",
        borderwidth=0,
        activebackground="#1f2937",
        activeforeground="#ffffff",
        font=("Segoe UI", 18),
        padx=10,
        pady=0,
    )
    close_btn.pack(side="right")

    label = tk.Label(
        body,
        text=value,
        fg="#f3f4f6",
        bg="#111827",
        justify="left",
        anchor="nw",
        wraplength=width - 70,
        font=("Segoe UI", 22),
    )
    label.pack(fill="both", expand=True)

    root.after(TEXT_WINDOW_SECONDS * 1000, root.destroy)
    root.mainloop()


def show_picture_popup(args: dict[str, Any]) -> dict[str, Any]:
    if "tk" not in globals():
        raise RuntimeError("Tkinter недоступен для показа изображения.")
    image_bytes: bytes | None = None
    if args.get("image_b64"):
        image_bytes = base64.b64decode(str(args.get("image_b64")))
    else:
        image_url = str(args.get("url", "")).strip()
        if not image_url:
            raise ValueError("Не передана ссылка на изображение.")
        response = requests.get(image_url, timeout=20)
        response.raise_for_status()
        image_bytes = response.content
    if not image_bytes:
        raise ValueError("Не удалось получить изображение.")

    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#000000")

    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    max_w = max(screen_w - 140, 320)
    max_h = max(screen_h - 140, 240)
    image.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    photo = ImageTk.PhotoImage(image)

    width = image.width
    height = image.height
    pos_x = max((screen_w - width) // 2, 20)
    pos_y = max((screen_h - height) // 2, 20)
    root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")

    canvas = tk.Canvas(root, width=width, height=height, highlightthickness=0, bd=0, bg="#000000")
    canvas.pack(fill="both", expand=True)
    canvas.create_image(width // 2, height // 2, image=photo)
    close_size = 36
    close_x1 = width - close_size - 12
    close_y1 = 12
    close_x2 = width - 12
    close_y2 = 12 + close_size
    canvas.create_oval(close_x1, close_y1, close_x2, close_y2, fill="#111827", outline="#e5e7eb", width=1)
    canvas.create_text((close_x1 + close_x2) // 2, (close_y1 + close_y2) // 2, text="×", fill="#f9fafb", font=("Segoe UI", 18, "bold"))

    def maybe_close(event: Any) -> None:
        if close_x1 <= event.x <= close_x2 and close_y1 <= event.y <= close_y2:
            root.destroy()

    canvas.bind("<Button-1>", maybe_close)
    root.after(TEXT_WINDOW_SECONDS * 1000, root.destroy)
    root.mainloop()
    return {
        "ok": True,
        "message": "Изображение показано на экране.",
        "data": {"width": width, "height": height},
    }


def maybe_recover_wifi(last_attempt_at: float, reason: str, force: bool = False) -> tuple[float, str | None]:
    if not force and (time.time() - last_attempt_at) < WIFI_RECOVERY_COOLDOWN_SECONDS:
        return last_attempt_at, None
    try:
        result = attempt_wifi_recovery_safe()
        message = result.get("message", f"Wi-Fi recovery ok ({reason}).")
    except Exception as exc:
        message = f"Wi-Fi recovery failed ({reason}): {exc}"
    return time.time(), message


def handle_command(command: dict[str, Any], snapshot: dict[str, Any], session: requests.Session) -> dict[str, Any]:
    command_type = command["type"]
    args = command.get("args", {})

    if command_type == "info":
        return {"ok": True, "message": info_text(snapshot), "data": snapshot}
    if command_type == "uptime":
        return {
            "ok": True,
            "message": f"Аптайм: {snapshot['uptime_seconds']} секунд",
            "data": {"uptime_seconds": snapshot["uptime_seconds"]},
        }
    if command_type == "net":
        wifi_profile = snapshot.get("wifi_profile") or "н/д"
        wifi_signal = snapshot.get("wifi_signal")
        wifi_suffix = f" ({wifi_signal}%)" if wifi_signal is not None else ""
        return {
            "ok": True,
            "message": (
                f"IP: {', '.join(snapshot['ip_addresses']) or 'н/д'}\n"
                f"Wi-Fi: {wifi_profile}{wifi_suffix}\n"
                f"Интернет: {'ok' if snapshot.get('internet_ok') else 'нет'}\n"
                f"Скачивание: {human_bytes(snapshot['net_down_per_sec'])}/с\n"
                f"Отдача: {human_bytes(snapshot['net_up_per_sec'])}/с"
            ),
            "data": {
                "ip_addresses": snapshot["ip_addresses"],
                "net_down_per_sec": snapshot["net_down_per_sec"],
                "net_up_per_sec": snapshot["net_up_per_sec"],
                "wifi_profile": snapshot.get("wifi_profile"),
                "wifi_signal": snapshot.get("wifi_signal"),
                "internet_ok": snapshot.get("internet_ok"),
            },
        }
    if command_type == "specs":
        return {
            "ok": True,
            "message": specs_text(snapshot),
            "data": collect_hardware_specs(snapshot),
        }
    if command_type == "drives":
        return {"ok": True, "message": list_drives(), "data": {}}
    if command_type == "services":
        return {"ok": True, "message": list_services(), "data": {}}
    if command_type == "apps":
        return {"ok": True, "message": list_apps(), "data": {"apps": snapshot.get("apps", [])}}
    if command_type == "top":
        return {"ok": True, "message": list_top_processes(), "data": {"processes": snapshot.get("top_processes", [])}}
    if command_type == "jobs":
        return {"ok": True, "message": list_jobs(), "data": {"jobs": snapshot.get("jobs", [])}}
    if command_type == "screenshot":
        file_name, file_b64 = screenshot_data()
        return {
            "ok": True,
            "message": "Скриншот сохранён.",
            "data": {},
            "file_name": file_name,
            "file_b64": file_b64,
        }
    if command_type == "remote_frame":
        file_name, file_b64, frame_data = remote_frame_data()
        return {
            "ok": True,
            "message": "Удалённый кадр получен.",
            "data": frame_data,
            "file_name": file_name,
            "file_b64": file_b64,
        }
    if command_type == "remote_input":
        return perform_remote_input(args)
    if command_type == "shell_cmd":
        command_text = str(args.get("command", "")).strip()
        return run_shell_command(command_text)
    if command_type == "find_file":
        query = str(args.get("query", "")).strip()
        return send_found_file(query)
    if command_type == "run_alias":
        alias = str(args.get("alias", "")).strip().lower()
        return {"ok": True, "message": start_alias(alias), "data": {"alias": alias}}
    if command_type == "run_job":
        alias = str(args.get("alias", "")).strip().lower()
        return {"ok": True, "message": run_job(alias), "data": {"alias": alias}}
    if command_type == "close_app":
        target = str(args.get("target") or args.get("alias") or "").strip()
        return {"ok": True, "message": close_process(target), "data": {"target": target}}
    if command_type == "close_foreground_window":
        return {"ok": True, "message": close_foreground_window(), "data": {}}
    if command_type == "kill_process":
        target = str(args.get("target") or "").strip()
        return {"ok": True, "message": terminate_process(target), "data": {"target": target}}
    if command_type == "restart_app":
        alias = str(args.get("alias", "")).strip().lower()
        return {"ok": True, "message": restart_alias(alias), "data": {"alias": alias}}
    if command_type == "lock_pc":
        return {"ok": True, "message": lock_pc(), "data": {}}
    if command_type == "restart_pc":
        return {"ok": True, "message": restart_pc(), "data": {}}
    if command_type == "shutdown_pc":
        return {"ok": True, "message": shutdown_pc(), "data": {}}
    if command_type == "show_text":
        return show_text_popup(str(args.get("text", "")))
    if command_type == "show_picture":
        return show_picture_popup(args)
    if command_type == "wifi_recover":
        return attempt_wifi_recovery_safe()
    if command_type == "self_update":
        return perform_self_update(session)
    if command_type == "uninstall_self":
        return uninstall_self()
    raise ValueError(f"Неподдерживаемая команда: {command_type}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SystemPortal Telegram PC agent")
    parser.add_argument("--set-name", help="Override the display name used on the server")
    parser.add_argument("--set-update-url", help="Persist the URL used for self-updates")
    parser.add_argument(
        "--install-startup",
        action="store_true",
        help="Create a per-user startup entry for logon startup",
    )
    return parser.parse_args()


def run_loop() -> None:
    boost_process_priority()
    session = requests.Session()
    previous_net: dict[str, Any] | None = None
    cached_snapshot: dict[str, Any] | None = None
    last_heartbeat = 0.0
    last_wifi_recovery_at = 0.0
    consecutive_network_failures = 0

    while True:
        try:
            state = ensure_registered(session)
            now = time.time()
            if cached_snapshot is None or now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                cached_snapshot, previous_net = collect_snapshot(previous_net)
                heartbeat(session, state, cached_snapshot)
                last_heartbeat = now
                consecutive_network_failures = 0
                if not cached_snapshot.get("internet_ok"):
                    last_wifi_recovery_at, note = maybe_recover_wifi(last_wifi_recovery_at, "heartbeat-no-internet")
                    if note:
                        print(f"[agent] {note}")

            command = fetch_command(session, state)
            if command:
                if cached_snapshot is None:
                    cached_snapshot, previous_net = collect_snapshot(previous_net)
                try:
                    result = handle_command(command, cached_snapshot, session)
                except Exception as exc:
                    post_result(
                        session,
                        state,
                        command["id"],
                        ok=False,
                        message=str(exc),
                        data={},
                    )
                else:
                    post_result(
                        session,
                        state,
                        command["id"],
                        ok=result["ok"],
                        message=result["message"],
                        data=result.get("data"),
                        file_name=result.get("file_name"),
                        file_b64=result.get("file_b64"),
                    )
                    if result.get("restart_after_result"):
                        restart_self()
                        return
                    if result.get("shutdown_after_result"):
                        return
            time.sleep(COMMAND_POLL_SECONDS)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            consecutive_network_failures += 1
            if not network_reachable(timeout=1.0):
                last_wifi_recovery_at, note = maybe_recover_wifi(
                    last_wifi_recovery_at,
                    f"loop-exception-{type(exc).__name__}",
                    force=consecutive_network_failures >= 2,
                )
                if note:
                    print(f"[agent] {note}")
            print(f"[agent] {exc}")
            time.sleep(5)


def main() -> None:
    args = parse_args()

    state = load_state()
    changed_state = False

    if args.set_name:
        state["display_name"] = args.set_name
        if "device_id" not in state:
            state["device_id"] = stable_device_id()
        changed_state = True
        print(f"Имя устройства изменено на: {args.set_name}")

    if args.set_update_url:
        state["update_url"] = args.set_update_url.strip()
        changed_state = True
        print("URL обновления сохранён.")

    if changed_state:
        save_state(state)
        update_install_summary()

    if args.install_startup:
        install_startup_task()
        update_install_summary()
        print("Автозапуск установлен.")
        return

    if changed_state:
        return

    if not ensure_single_instance():
        return

    boost_process_priority()
    run_loop()


if __name__ == "__main__":
    main()
