from __future__ import annotations

import argparse
import base64
import codecs
import ctypes
import ctypes.wintypes
import getpass
import io
import json
import os
import platform
import shlex
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import psutil
import requests
from PIL import ImageGrab


EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
IsWindowVisible = ctypes.windll.user32.IsWindowVisible
PostMessageW = ctypes.windll.user32.PostMessageW
GetWindowTextLengthW = ctypes.windll.user32.GetWindowTextLengthW
GetWindowTextW = ctypes.windll.user32.GetWindowTextW
WM_CLOSE = 0x0010


# Edit these values first.
SERVER_URL = "http://217.60.245.42:8090"
REGISTRATION_KEY = "change-this-key"
HEARTBEAT_INTERVAL_SECONDS = 10
COMMAND_POLL_SECONDS = 1
HTTP_TIMEOUT_SECONDS = 15
AGENT_VERSION = "1.0.0"
SHELL_COMMAND_TIMEOUT_SECONDS = 25
SHELL_COMMAND_CWD = str(Path.home())
SHELL_COMMAND_PREVIEW_CHARS = 1600


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


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


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


def collect_snapshot(previous_net: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\")
    net = psutil.net_io_counters()
    now = time.time()

    down_per_sec = 0.0
    up_per_sec = 0.0
    if previous_net:
        elapsed = max(now - previous_net["time"], 1e-6)
        down_per_sec = max(net.bytes_recv - previous_net["bytes_recv"], 0) / elapsed
        up_per_sec = max(net.bytes_sent - previous_net["bytes_sent"], 0) / elapsed

    snapshot = {
        "hostname": socket.gethostname(),
        "username": getpass.getuser(),
        "os": f"{platform.system()} {platform.release()}",
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
        f"IP: {', '.join(snapshot['ip_addresses']) or 'н/д'}"
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
    }


def start_alias(alias: str) -> str:
    app = APP_ALIASES.get(alias)
    if not app:
        raise ValueError(f"Неизвестный алиас программы: {alias}")
    command_line = os.path.expandvars(str(app["start"]))
    if command_line.lower().endswith(".exe") and " " not in command_line.strip():
        os.startfile(command_line)
    else:
        subprocess.Popen(shlex.split(command_line, posix=False), shell=False)
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


def screenshot_data() -> tuple[str, str]:
    image = ImageGrab.grab(all_screens=False)
    image = image.convert("RGB")
    image.thumbnail((1600, 900))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=72, optimize=True)
    return "desktop.jpg", base64.b64encode(buffer.getvalue()).decode("ascii")


def lock_pc() -> str:
    ctypes.windll.user32.LockWorkStation()
    return "Команда блокировки отправлена."


def restart_pc() -> str:
    subprocess.Popen(["shutdown", "/r", "/t", "5"], shell=False)
    return "Перезагрузка запланирована через 5 секунд."


def shutdown_pc() -> str:
    subprocess.Popen(["shutdown", "/s", "/t", "5"], shell=False)
    return "Выключение запланировано через 5 секунд."


def install_startup_task() -> None:
    python_exe = Path(sys.executable).resolve()
    pythonw_exe = python_exe.with_name("pythonw.exe")
    launcher = pythonw_exe if pythonw_exe.exists() else python_exe
    script_path = str(Path(__file__).resolve())
    task_name = os.environ.get("PCBOT_STARTUP_TASK_NAME", "SchoolProAgent").strip() or "SchoolProAgent"
    command = (
        f'schtasks /Create /SC ONLOGON /TN "{task_name}" '
        f'/TR "\\"{launcher}\\" \\"{script_path}\\"" /RL LIMITED /F'
    )
    result = subprocess.run(command, shell=True, check=False)
    if result.returncode != 0:
        raise RuntimeError("Не удалось создать задачу планировщика.")


def handle_command(command: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
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
        return {
            "ok": True,
            "message": (
                f"IP: {', '.join(snapshot['ip_addresses']) or 'н/д'}\n"
                f"Скачивание: {human_bytes(snapshot['net_down_per_sec'])}/с\n"
                f"Отдача: {human_bytes(snapshot['net_up_per_sec'])}/с"
            ),
            "data": {
                "ip_addresses": snapshot["ip_addresses"],
                "net_down_per_sec": snapshot["net_down_per_sec"],
                "net_up_per_sec": snapshot["net_up_per_sec"],
            },
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
    if command_type == "shell_cmd":
        command_text = str(args.get("command", "")).strip()
        return run_shell_command(command_text)
    if command_type == "run_alias":
        alias = str(args.get("alias", "")).strip().lower()
        return {"ok": True, "message": start_alias(alias), "data": {"alias": alias}}
    if command_type == "run_job":
        alias = str(args.get("alias", "")).strip().lower()
        return {"ok": True, "message": run_job(alias), "data": {"alias": alias}}
    if command_type == "close_app":
        target = str(args.get("target") or args.get("alias") or "").strip()
        return {"ok": True, "message": close_process(target), "data": {"target": target}}
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
    raise ValueError(f"Неподдерживаемая команда: {command_type}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe Telegram PC agent")
    parser.add_argument("--set-name", help="Override the display name used on the server")
    parser.add_argument(
        "--install-startup",
        action="store_true",
        help="Create a visible Task Scheduler entry for logon startup",
    )
    return parser.parse_args()


def run_loop() -> None:
    session = requests.Session()
    previous_net: dict[str, Any] | None = None
    cached_snapshot: dict[str, Any] | None = None
    last_heartbeat = 0.0

    while True:
        try:
            state = ensure_registered(session)
            now = time.time()
            if cached_snapshot is None or now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                cached_snapshot, previous_net = collect_snapshot(previous_net)
                heartbeat(session, state, cached_snapshot)
                last_heartbeat = now

            command = fetch_command(session, state)
            if command:
                if cached_snapshot is None:
                    cached_snapshot, previous_net = collect_snapshot(previous_net)
                try:
                    result = handle_command(command, cached_snapshot)
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
            time.sleep(COMMAND_POLL_SECONDS)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
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
        save_state(state)
        changed_state = True
        print(f"Имя устройства изменено на: {args.set_name}")

    if args.install_startup:
        install_startup_task()
        print("Автозапуск через планировщик установлен.")
        return

    if changed_state:
        return

    run_loop()


if __name__ == "__main__":
    main()
