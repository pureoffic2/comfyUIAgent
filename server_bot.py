from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import io
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "server_data"
UPLOAD_DIR = DATA_DIR / "uploads"
STATE_PATH = DATA_DIR / "state.json"

BOT_TOKEN = os.environ.get("PCBOT_TOKEN", "").strip()
REGISTRATION_KEY = os.environ.get("PCBOT_REGISTRATION_KEY", "change-this-key").strip()
FIXED_OWNER_CHAT_ID = int(os.environ.get("PCBOT_OWNER_CHAT_ID", "6494641721"))
API_HOST = os.environ.get("PCBOT_HOST", "0.0.0.0").strip()
API_PORT = int(os.environ.get("PCBOT_PORT", "8080"))
ONLINE_TIMEOUT_SECONDS = int(os.environ.get("PCBOT_ONLINE_TIMEOUT", "120"))
PRESENCE_SWEEP_INTERVAL_SECONDS = int(os.environ.get("PCBOT_PRESENCE_SWEEP_INTERVAL", "5"))
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("PCBOT_COMMAND_TIMEOUT", "35"))
COMMAND_RESULT_POLL_INTERVAL_SECONDS = float(os.environ.get("PCBOT_COMMAND_POLL_INTERVAL", "0.05"))
CMD_COMMAND_PREVIEW_CHARS = int(os.environ.get("PCBOT_CMD_COMMAND_PREVIEW", "400"))
CMD_OUTPUT_PREVIEW_CHARS = int(os.environ.get("PCBOT_CMD_OUTPUT_PREVIEW", "1000"))
PUBLIC_BASE_URL = os.environ.get("PCBOT_PUBLIC_BASE_URL", "").strip().rstrip("/")
REMOTE_SESSION_TTL_SECONDS = int(os.environ.get("PCBOT_REMOTE_SESSION_TTL", "43200"))
REMOTE_FRAME_TIMEOUT_SECONDS = int(os.environ.get("PCBOT_REMOTE_FRAME_TIMEOUT", "8"))
AUTO_PUBLIC_TUNNEL = os.environ.get("PCBOT_AUTO_PUBLIC_TUNNEL", "1").strip().lower() not in {"0", "false", "off", "no"}
CLOUDFLARED_DIR = DATA_DIR / "cloudflared"
CLOUDFLARED_BIN = CLOUDFLARED_DIR / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
CLOUDFLARED_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def format_time(value: str | None) -> str:
    if not value:
        return "никогда"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return value


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "н/д"
    seconds = int(max(seconds, 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours or parts:
        parts.append(f"{hours}ч")
    if minutes or parts:
        parts.append(f"{minutes}м")
    parts.append(f"{seconds}с")
    return " ".join(parts)


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "н/д"
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.1f} {units[index]}"


def trim_text(value: str, limit: int) -> tuple[str, bool]:
    text = value.replace("\r\n", "\n").strip()
    if len(text) <= limit:
        return text, False
    return f"{text[: limit - 3].rstrip()}...", True


def sanitize_text(value: str) -> str:
    return re.sub(r"[\ud800-\udfff]", "\uFFFD", value)


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {sanitize_json_value(key): sanitize_json_value(item) for key, item in value.items()}
    return value


def last_seen_at(device: dict[str, Any]) -> datetime | None:
    raw_last_seen = device.get("last_seen")
    if not raw_last_seen:
        return None
    try:
        return datetime.fromisoformat(raw_last_seen)
    except ValueError:
        return None


def last_seen_is_recent(device: dict[str, Any]) -> bool:
    last_seen = last_seen_at(device)
    return last_seen is not None and utc_now() - last_seen <= timedelta(seconds=ONLINE_TIMEOUT_SECONDS)


def presence_status(device: dict[str, Any]) -> str:
    stored = str(device.get("presence_status") or "").lower()
    recent = last_seen_is_recent(device)
    if stored == "online" and recent:
        return "online"
    if stored == "offline" and not recent:
        return "offline"
    return "online" if recent else "offline"


def is_online(device: dict[str, Any]) -> bool:
    return presence_status(device) == "online"


def status_label(device: dict[str, Any]) -> str:
    return "онлайн" if presence_status(device) == "online" else "оффлайн"


def status_badge(device: dict[str, Any]) -> str:
    return "ONLINE" if presence_status(device) == "online" else "OFFLINE"


def format_relative_age(value: str | None) -> str:
    last_seen = last_seen_at({"last_seen": value})
    if last_seen is None:
        return "нет heartbeat"
    seconds = int(max((utc_now() - last_seen).total_seconds(), 0))
    if seconds < 5:
        return "только что"
    if seconds < 60:
        return f"{seconds}с назад"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}м {rem}с назад"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}ч {minutes}м назад"
    days, hours = divmod(hours, 24)
    return f"{days}д {hours}ч назад"


def device_sort_key(device: dict[str, Any]) -> tuple[int, str, str]:
    return (
        0 if is_online(device) else 1,
        str(device.get("display_name", "")).lower(),
        str(device.get("device_id", "")).lower(),
    )


def command_counters(device: dict[str, Any]) -> dict[str, int]:
    commands = device.get("commands", [])
    return {
        "queued": sum(1 for command in commands if command.get("status") == "queued"),
        "in_progress": sum(1 for command in commands if command.get("status") == "in_progress"),
        "completed": sum(1 for command in commands if command.get("status") == "completed"),
        "failed": sum(1 for command in commands if command.get("status") == "failed"),
    }


class RegisterRequest(BaseModel):
    registration_key: str
    device_id: str
    display_name: str
    hostname: str
    agent_version: str = "1.1.0"


class HeartbeatRequest(BaseModel):
    device_id: str
    agent_token: str
    snapshot: dict[str, Any] = Field(default_factory=dict)


class NextCommandRequest(BaseModel):
    device_id: str
    agent_token: str


class CommandResultRequest(BaseModel):
    device_id: str
    agent_token: str
    command_id: str
    ok: bool
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    file_name: str | None = None
    file_b64: str | None = None


class RemoteInputRequest(BaseModel):
    action: str
    x: float = 0
    y: float = 0
    button: str = "left"
    delta: int = 0
    text: str = ""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.state = self._load()

    def _ensure_device_defaults(self, device: dict[str, Any]) -> None:
        registered_at = device.get("registered_at") or utc_now().isoformat()
        last_seen = device.get("last_seen")
        inferred_status = "online" if last_seen and last_seen_is_recent(device) else "offline"
        device.setdefault("registered_at", registered_at)
        device.setdefault("updated_at", registered_at)
        device.setdefault("snapshot", {})
        device.setdefault("commands", [])
        device.setdefault("presence_status", inferred_status)
        device.setdefault("presence_changed_at", registered_at if inferred_status == "offline" else last_seen or registered_at)
        device.setdefault("last_online_at", last_seen if inferred_status == "online" else None)
        device.setdefault("last_offline_at", registered_at if inferred_status == "offline" else None)
        device.setdefault("bot_label", None)

    def _make_unique_display_name(
        self,
        devices: dict[str, dict[str, Any]],
        *,
        requested_name: str,
        device_id: str,
    ) -> str:
        base = requested_name.strip() or device_id
        occupied = {
            str(item.get("display_name") or "").strip().lower()
            for other_id, item in devices.items()
            if other_id != device_id
        }
        if base.lower() not in occupied:
            return base
        counter = 1
        while True:
            candidate = f"{base} {counter}"
            if candidate.lower() not in occupied:
                return candidate
            counter += 1

    def _load(self) -> dict[str, Any]:
        ensure_dirs()
        if self.path.exists():
            state = sanitize_json_value(json.loads(self.path.read_text(encoding="utf-8")))
            state.setdefault("owner_chat_id", None)
            state.setdefault("devices", {})
            state.setdefault("chat_preferences", {})
            for device in state.get("devices", {}).values():
                self._ensure_device_defaults(device)
            self.path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            return state
        state = {"owner_chat_id": None, "devices": {}, "chat_preferences": {}}
        self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return state

    def _save_unlocked(self) -> None:
        self.state = sanitize_json_value(self.state)
        self.path.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    async def get_owner(self) -> int | None:
        async with self.lock:
            owner = self.state.get("owner_chat_id")
            return int(owner) if owner is not None else None

    async def claim_owner(self, chat_id: int) -> bool:
        async with self.lock:
            owner = self.state.get("owner_chat_id")
            if owner is None:
                self.state["owner_chat_id"] = int(chat_id)
                self._save_unlocked()
                return True
            return int(owner) == int(chat_id)

    async def is_owner(self, chat_id: int) -> bool:
        owner = await self.get_owner()
        return owner is not None and owner == int(chat_id)

    async def set_selected(self, chat_id: int, device_id: str) -> None:
        async with self.lock:
            self.state.setdefault("chat_preferences", {})[str(chat_id)] = {
                "selected_device_id": device_id
            }
            self._save_unlocked()

    async def get_selected(self, chat_id: int) -> str | None:
        async with self.lock:
            prefs = self.state.get("chat_preferences", {}).get(str(chat_id), {})
            return prefs.get("selected_device_id")

    async def list_devices(self) -> list[dict[str, Any]]:
        async with self.lock:
            devices = list(self.state.get("devices", {}).values())
        return sorted(devices, key=device_sort_key)

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        async with self.lock:
            device = self.state.get("devices", {}).get(device_id)
            return json.loads(json.dumps(device)) if device else None

    async def register(self, payload: RegisterRequest) -> tuple[dict[str, Any], bool]:
        async with self.lock:
            devices = self.state.setdefault("devices", {})
            created = payload.device_id not in devices
            unique_name = self._make_unique_display_name(
                devices,
                requested_name=payload.display_name.strip() or payload.hostname,
                device_id=payload.device_id,
            )
            if created:
                devices[payload.device_id] = {
                    "device_id": payload.device_id,
                    "display_name": unique_name,
                    "hostname": payload.hostname,
                    "agent_version": payload.agent_version,
                    "agent_token": secrets.token_urlsafe(24),
                    "registered_at": utc_now().isoformat(),
                    "updated_at": utc_now().isoformat(),
                    "last_seen": None,
                    "snapshot": {},
                    "commands": [],
                    "presence_status": "offline",
                    "presence_changed_at": utc_now().isoformat(),
                    "last_online_at": None,
                    "last_offline_at": utc_now().isoformat(),
                }
            else:
                device = devices[payload.device_id]
                device["display_name"] = unique_name
                device["hostname"] = payload.hostname
                device["agent_version"] = payload.agent_version
                device["updated_at"] = utc_now().isoformat()
                self._ensure_device_defaults(device)
            self._save_unlocked()
            return json.loads(json.dumps(devices[payload.device_id])), created

    async def set_bot_label(self, device_id: str, value: str | None) -> dict[str, Any] | None:
        async with self.lock:
            device = self.state.get("devices", {}).get(device_id)
            if not device:
                return None
            trimmed = (value or "").strip()
            device["bot_label"] = trimmed or None
            device["updated_at"] = utc_now().isoformat()
            self._save_unlocked()
            return json.loads(json.dumps(device))

    async def heartbeat(self, payload: HeartbeatRequest) -> tuple[dict[str, Any], bool]:
        async with self.lock:
            device = self.state.get("devices", {}).get(payload.device_id)
            if not device or device.get("agent_token") != payload.agent_token:
                raise HTTPException(status_code=403, detail="invalid agent token")
            self._ensure_device_defaults(device)
            became_online = device.get("presence_status") != "online"
            device["snapshot"] = payload.snapshot
            device["last_seen"] = utc_now().isoformat()
            device["updated_at"] = utc_now().isoformat()
            if became_online:
                device["presence_status"] = "online"
                device["presence_changed_at"] = device["last_seen"]
                device["last_online_at"] = device["last_seen"]
            self._save_unlocked()
            return json.loads(json.dumps(device)), became_online

    async def sweep_presence(self) -> list[dict[str, Any]]:
        async with self.lock:
            now = utc_now()
            changed: list[dict[str, Any]] = []
            for device in self.state.get("devices", {}).values():
                self._ensure_device_defaults(device)
                if device.get("presence_status") != "online":
                    continue
                raw_last_seen = device.get("last_seen")
                try:
                    last_seen = datetime.fromisoformat(raw_last_seen) if raw_last_seen else None
                except ValueError:
                    last_seen = None
                if last_seen and now - last_seen <= timedelta(seconds=ONLINE_TIMEOUT_SECONDS):
                    continue
                device["presence_status"] = "offline"
                device["presence_changed_at"] = now.isoformat()
                device["last_offline_at"] = now.isoformat()
                changed.append(json.loads(json.dumps(device)))
            if changed:
                self._save_unlocked()
            return changed

    async def queue_command(
        self,
        device_id: str,
        command_type: str,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.lock:
            device = self.state.get("devices", {}).get(device_id)
            if not device:
                raise ValueError("device not found")
            command = {
                "id": secrets.token_hex(8),
                "type": command_type,
                "args": args or {},
                "status": "queued",
                "created_at": utc_now().isoformat(),
                "last_dispatch_at": None,
                "result": None,
            }
            device.setdefault("commands", []).append(command)
            self._save_unlocked()
            return json.loads(json.dumps(command))

    async def queue_remote_input(self, device_id: str, args: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            device = self.state.get("devices", {}).get(device_id)
            if not device:
                raise ValueError("device not found")
            action = str(args.get("action", "")).strip().lower()
            if action == "move":
                for command in reversed(device.setdefault("commands", [])):
                    if command.get("type") != "remote_input":
                        continue
                    if command.get("status") != "queued":
                        continue
                    if str(command.get("args", {}).get("action", "")).strip().lower() != "move":
                        continue
                    command["args"] = dict(args)
                    command["updated_at"] = utc_now().isoformat()
                    self._save_unlocked()
                    return json.loads(json.dumps(command))
            command = {
                "id": secrets.token_hex(8),
                "type": "remote_input",
                "args": dict(args),
                "status": "queued",
                "created_at": utc_now().isoformat(),
                "last_dispatch_at": None,
                "result": None,
            }
            device.setdefault("commands", []).append(command)
            self._save_unlocked()
            return json.loads(json.dumps(command))

    async def get_next_command(self, payload: NextCommandRequest) -> dict[str, Any] | None:
        async with self.lock:
            device = self.state.get("devices", {}).get(payload.device_id)
            if not device or device.get("agent_token") != payload.agent_token:
                raise HTTPException(status_code=403, detail="invalid agent token")
            now = utc_now()
            commands = device.setdefault("commands", [])
            prioritized = sorted(
                commands,
                key=lambda command: (
                    0 if command.get("status") == "queued" and command.get("type") == "remote_input" else 1,
                    str(command.get("created_at") or ""),
                ),
            )
            for command in prioritized:
                if command["status"] == "queued":
                    command["status"] = "in_progress"
                    command["last_dispatch_at"] = now.isoformat()
                    self._save_unlocked()
                    return json.loads(json.dumps(command))
                if command["status"] == "in_progress":
                    last_dispatch = command.get("last_dispatch_at")
                    stale = True
                    if last_dispatch:
                        with contextlib.suppress(ValueError):
                            stale = now - datetime.fromisoformat(last_dispatch) > timedelta(seconds=45)
                    if stale:
                        command["last_dispatch_at"] = now.isoformat()
                        self._save_unlocked()
                        return json.loads(json.dumps(command))
            return None

    async def complete_command(self, payload: CommandResultRequest) -> None:
        async with self.lock:
            device = self.state.get("devices", {}).get(payload.device_id)
            if not device or device.get("agent_token") != payload.agent_token:
                raise HTTPException(status_code=403, detail="invalid agent token")
            for command in device.get("commands", []):
                if command["id"] != payload.command_id:
                    continue
                command["status"] = "completed" if payload.ok else "failed"
                file_path = None
                if payload.file_b64 and payload.file_name:
                    raw = base64.b64decode(payload.file_b64)
                    file_path = UPLOAD_DIR / f"{payload.command_id}_{payload.file_name}"
                    file_path.write_bytes(raw)
                command["result"] = {
                    "ok": payload.ok,
                    "message": payload.message,
                    "data": payload.data,
                    "file_name": payload.file_name,
                    "file_path": str(file_path) if file_path else None,
                    "completed_at": utc_now().isoformat(),
                }
                self._save_unlocked()
                return
            raise HTTPException(status_code=404, detail="command not found")

    async def get_command(self, device_id: str, command_id: str) -> dict[str, Any] | None:
        async with self.lock:
            device = self.state.get("devices", {}).get(device_id)
            if not device:
                return None
            for command in device.get("commands", []):
                if command["id"] == command_id:
                    return json.loads(json.dumps(command))
            return None

    async def remove_device(self, device_id: str) -> dict[str, Any] | None:
        async with self.lock:
            device = self.state.get("devices", {}).pop(device_id, None)
            if device is None:
                return None
            for prefs in self.state.get("chat_preferences", {}).values():
                if prefs.get("selected_device_id") == device_id:
                    prefs.pop("selected_device_id", None)
            self._save_unlocked()
            return json.loads(json.dumps(device))


class RemoteSessionManager:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.sessions: dict[str, dict[str, Any]] = {}

    async def create(self, chat_id: int, device_id: str) -> str:
        async with self.lock:
            token = secrets.token_urlsafe(24)
            self.sessions[token] = {
                "chat_id": int(chat_id),
                "device_id": device_id,
                "created_at": utc_now().isoformat(),
                "expires_at": (utc_now() + timedelta(seconds=REMOTE_SESSION_TTL_SECONDS)).isoformat(),
            }
            return token

    async def get(self, token: str) -> dict[str, Any] | None:
        async with self.lock:
            session = self.sessions.get(token)
            if not session:
                return None
            try:
                expires_at = datetime.fromisoformat(str(session.get("expires_at")))
            except ValueError:
                expires_at = utc_now()
            if utc_now() >= expires_at:
                self.sessions.pop(token, None)
                return None
            session["expires_at"] = (utc_now() + timedelta(seconds=REMOTE_SESSION_TTL_SECONDS)).isoformat()
            return json.loads(json.dumps(session))

    async def sweep(self) -> None:
        async with self.lock:
            now = utc_now()
            expired = []
            for token, session in self.sessions.items():
                try:
                    expires_at = datetime.fromisoformat(str(session.get("expires_at")))
                except ValueError:
                    expires_at = now
                if now >= expires_at:
                    expired.append(token)
            for token in expired:
                self.sessions.pop(token, None)

    async def active_device_ids(self) -> set[str]:
        async with self.lock:
            now = utc_now()
            active: set[str] = set()
            for session in self.sessions.values():
                with contextlib.suppress(ValueError):
                    expires_at = datetime.fromisoformat(str(session.get("expires_at")))
                    if now < expires_at:
                        active.add(str(session.get("device_id", "")))
            return active


def cloudflared_download_url() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        if machine in {"x86_64", "amd64"}:
            return "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
        if machine in {"aarch64", "arm64"}:
            return "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
    if system == "windows":
        if machine in {"x86_64", "amd64"}:
            return "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        if machine in {"aarch64", "arm64"}:
            return "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-arm64.exe"
    return None


class TunnelManager:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.public_url: str | None = None
        self.last_error: str | None = None
        self.owner_notified_url: str | None = None
        self.watch_task: asyncio.Task[None] | None = None

    async def ensure_binary(self) -> Path:
        ensure_dirs()
        CLOUDFLARED_DIR.mkdir(parents=True, exist_ok=True)
        resolved = shutil.which("cloudflared")
        if resolved:
            return Path(resolved)
        if CLOUDFLARED_BIN.exists():
            if os.name != "nt":
                CLOUDFLARED_BIN.chmod(0o755)
            return CLOUDFLARED_BIN
        url = cloudflared_download_url()
        if not url:
            raise RuntimeError(f"cloudflared unsupported on {platform.system()} {platform.machine()}")
        with urllib.request.urlopen(url, timeout=60) as response:
            CLOUDFLARED_BIN.write_bytes(response.read())
        if os.name != "nt":
            CLOUDFLARED_BIN.chmod(0o755)
        return CLOUDFLARED_BIN

    async def _read_output(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            match = CLOUDFLARED_URL_RE.search(text)
            if match:
                async with self.lock:
                    self.public_url = match.group(0).rstrip("/")
                    self.last_error = None

    async def ensure_running(self) -> str | None:
        async with self.lock:
            if PUBLIC_BASE_URL:
                self.public_url = PUBLIC_BASE_URL
                return self.public_url
            if not AUTO_PUBLIC_TUNNEL:
                return None
            if self.process and self.process.returncode is None and self.public_url:
                return self.public_url
            self.public_url = None
            self.last_error = None
            binary = await self.ensure_binary()
            env = os.environ.copy()
            env["HOME"] = str(CLOUDFLARED_DIR / "home")
            Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
            self.process = await asyncio.create_subprocess_exec(
                str(binary),
                "tunnel",
                "--url",
                f"http://127.0.0.1:{API_PORT}",
                "--no-autoupdate",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            asyncio.create_task(self._read_output(self.process.stdout))
            asyncio.create_task(self._read_output(self.process.stderr))
            if self.watch_task is None or self.watch_task.done():
                self.watch_task = asyncio.create_task(self._watch())
        deadline = asyncio.get_running_loop().time() + 25
        while asyncio.get_running_loop().time() < deadline:
            async with self.lock:
                if self.public_url:
                    return self.public_url
                if self.process and self.process.returncode not in {None, 0}:
                    break
            await asyncio.sleep(0.25)
        async with self.lock:
            return self.public_url

    async def _watch(self) -> None:
        process: asyncio.subprocess.Process | None
        async with self.lock:
            process = self.process
        if process is None:
            return
        await process.wait()
        async with self.lock:
            if self.process is process:
                self.last_error = f"cloudflared exited with code {process.returncode}"
                self.process = None
                self.public_url = None

    async def sweep(self) -> None:
        url_to_notify: str | None = None
        if PUBLIC_BASE_URL:
            async with self.lock:
                self.public_url = PUBLIC_BASE_URL
            return
        if AUTO_PUBLIC_TUNNEL:
            url = await self.ensure_running()
            async with self.lock:
                if url and self.owner_notified_url != url:
                    self.owner_notified_url = url
                    url_to_notify = url
        if url_to_notify:
            await notify_owner(f"Mini App tunnel готов: {url_to_notify}")


class RemoteFrameBroker:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.frames: dict[str, dict[str, Any]] = {}
        self.waiters: dict[str, asyncio.Event] = {}
        self.refreshing: set[str] = set()

    async def _set_frame(
        self,
        device_id: str,
        *,
        frame_bytes: bytes | None = None,
        width: int | None = None,
        height: int | None = None,
        error: str | None = None,
    ) -> None:
        async with self.lock:
            entry = self.frames.setdefault(device_id, {})
            entry["updated_at"] = utc_now().isoformat()
            if frame_bytes is not None:
                entry["frame_bytes"] = frame_bytes
                entry["width"] = width
                entry["height"] = height
                entry["error"] = None
            elif error is not None:
                entry["error"] = error
            waiter = self.waiters.setdefault(device_id, asyncio.Event())
            waiter.set()
            self.waiters[device_id] = asyncio.Event()

    async def get_cached(self, device_id: str, max_age_seconds: float = 0.6) -> dict[str, Any] | None:
        async with self.lock:
            entry = self.frames.get(device_id)
            if not entry:
                return None
            updated_at_raw = entry.get("updated_at")
            try:
                updated_at = datetime.fromisoformat(str(updated_at_raw))
            except ValueError:
                return None
            if utc_now() - updated_at > timedelta(seconds=max_age_seconds):
                return None
            return {
                "updated_at": str(entry.get("updated_at") or ""),
                "width": int(entry.get("width") or 0),
                "height": int(entry.get("height") or 0),
                "error": entry.get("error"),
                "frame_bytes": entry.get("frame_bytes"),
            }

    async def wait_for_frame(self, device_id: str, timeout_seconds: float = 3.0) -> dict[str, Any] | None:
        waiter: asyncio.Event
        async with self.lock:
            waiter = self.waiters.setdefault(device_id, asyncio.Event())
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(waiter.wait(), timeout=timeout_seconds)
        return await self.get_cached(device_id, max_age_seconds=10.0)

    async def refresh_once(self, device_id: str) -> dict[str, Any] | None:
        async with self.lock:
            if device_id in self.refreshing:
                waiter = self.waiters.setdefault(device_id, asyncio.Event())
                already_refreshing = True
            else:
                self.refreshing.add(device_id)
                waiter = self.waiters.setdefault(device_id, asyncio.Event())
                already_refreshing = False
        if already_refreshing:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(waiter.wait(), timeout=REMOTE_FRAME_TIMEOUT_SECONDS)
            return await self.get_cached(device_id, max_age_seconds=10.0)
        result = await run_and_wait(device_id, "remote_frame", timeout_seconds=REMOTE_FRAME_TIMEOUT_SECONDS)
        try:
            if result is None:
                await self._set_frame(device_id, error="device did not return a frame in time")
                return None
            payload = result.get("result", {})
            if not payload.get("ok", False):
                await self._set_frame(device_id, error=str(payload.get("message") or "frame failed"))
                return None
            file_path = payload.get("file_path")
            if not file_path or not Path(file_path).exists():
                await self._set_frame(device_id, error="frame file missing")
                return None
            data = payload.get("data", {})
            frame_bytes = Path(file_path).read_bytes()
            await self._set_frame(
                device_id,
                frame_bytes=frame_bytes,
                width=int(data.get("screen_width") or 0),
                height=int(data.get("screen_height") or 0),
            )
            return await self.get_cached(device_id, max_age_seconds=10.0)
        finally:
            async with self.lock:
                self.refreshing.discard(device_id)

    async def loop(self) -> None:
        while True:
            active_ids = await remote_sessions.active_device_ids()
            if not active_ids:
                await asyncio.sleep(0.15)
                continue
            for device_id in active_ids:
                device = await store.get_device(device_id)
                if not device or not is_online(device):
                    continue
                cached = await self.get_cached(device_id, max_age_seconds=0.18)
                if cached is None:
                    await self.refresh_once(device_id)
            await asyncio.sleep(0.03)


store = Store(STATE_PATH)
remote_sessions = RemoteSessionManager()
tunnel_manager = TunnelManager()
remote_frame_broker = RemoteFrameBroker()
api = FastAPI(title="Safe Telegram PC Bot")
telegram_app: Application | None = None


def tg_emoji(emoji_id: int, fallback: str) -> str:
    return ""


EMOJI = {
    "app": tg_emoji(5260681660189408650, "💎"),
    "pc": tg_emoji(5447607759421863856, "🖥"),
    "remote": tg_emoji(5445158077579952110, "📹"),
    "monitor": tg_emoji(5445146408153806223, "📊"),
    "control": tg_emoji(5444869180899752137, "⚙️"),
    "apps": tg_emoji(5444924349754667822, "💼"),
    "maintenance": tg_emoji(5447611706496808621, "⚙️"),
    "wifi": tg_emoji(5447602197439218445, "🌐"),
    "update": tg_emoji(5445388803223091254, "⚡️"),
    "text": tg_emoji(5444889156792646660, "📝"),
    "close": tg_emoji(5447434637880098257, "🚪"),
    "delete": tg_emoji(5445005936953424165, "🗑"),
    "back": tg_emoji(5445362436418859744, "↩️"),
    "info": tg_emoji(5247236071795754971, "ℹ️"),
    "screen": tg_emoji(5447588260270341594, "🖼"),
    "network": tg_emoji(5447448489149625830, "📡"),
    "rocket": tg_emoji(5444883062234053429, "▶️"),
    "ok": tg_emoji(5444987348334965906, "✅"),
    "warning": tg_emoji(5447381715293074599, "⚠️"),
}


def premium_button(
    text: str,
    *,
    callback_data: str | None = None,
    web_app: WebAppInfo | None = None,
    emoji_id: int | None = None,
) -> InlineKeyboardButton:
    api_kwargs: dict[str, Any] = {}
    if emoji_id is not None:
        api_kwargs["icon_custom_emoji_id"] = str(emoji_id)
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        web_app=web_app,
        api_kwargs=api_kwargs or None,
    )


def status_icon(device: dict[str, Any]) -> str:
    return "ONLINE" if is_online(device) else "OFFLINE"


def effective_display_name(device: dict[str, Any]) -> str:
    return str(device.get("bot_label") or device.get("display_name") or device.get("device_id") or "ПК")


def device_button_text(device: dict[str, Any], current_id: str | None = None) -> str:
    selected = "ACTIVE | " if current_id and current_id == device["device_id"] else ""
    return f"{selected}{status_badge(device)} | {effective_display_name(device)}"


def device_card(device: dict[str, Any]) -> str:
    snapshot = device.get("snapshot", {})
    counters = command_counters(device)
    last_seen = device.get("last_seen")
    lines = [
        "<b>SystemPortal</b>",
        f"<b>{html.escape(effective_display_name(device))}</b>",
        f"<b>Статус</b>: <code>{status_badge(device)}</code> | {html.escape(status_label(device))}",
        f"<b>ID</b>: <code>{html.escape(device.get('device_id', 'n/a'))}</code>",
        f"<b>Heartbeat</b>: {html.escape(format_time(last_seen))} | {html.escape(format_relative_age(last_seen))}",
        f"<b>Версия агента</b>: <code>{html.escape(str(device.get('agent_version', 'n/a')))}</code>",
        f"<b>Команды</b>: <code>{counters['queued']}</code> в очереди | <code>{counters['in_progress']}</code> выполняется",
    ]
    if snapshot:
        wifi_profile = snapshot.get("wifi_profile") or "н/д"
        wifi_signal = snapshot.get("wifi_signal")
        wifi_line = wifi_profile if wifi_signal is None else f"{wifi_profile} ({wifi_signal}%)"
        lines.extend(
            [
                "",
                "<b>Система</b>",
                f"<b>Хост</b>: {html.escape(str(snapshot.get('hostname', 'н/д')))}",
                f"<b>ОС</b>: {html.escape(str(snapshot.get('os', 'н/д')))}",
                f"<b>Пользователь</b>: {html.escape(str(snapshot.get('username', 'н/д')))}",
                f"<b>Аптайм</b>: {html.escape(format_duration(snapshot.get('uptime_seconds')))}",
                f"<b>CPU</b>: <code>{snapshot.get('cpu_percent', 'n/a')}%</code>",
                f"<b>RAM</b>: <code>{snapshot.get('memory_percent', 'n/a')}%</code>",
                f"<b>Диск</b>: <code>{snapshot.get('disk_percent', 'n/a')}%</code>",
                f"<b>Wi-Fi</b>: <code>{html.escape(str(wifi_line))}</code>",
                f"<b>Интернет</b>: <code>{'OK' if snapshot.get('internet_ok') else 'NO'}</code>",
                f"<b>Сеть</b>: <code>{format_bytes(snapshot.get('net_down_per_sec'))}/с</code> вниз | "
                f"<code>{format_bytes(snapshot.get('net_up_per_sec'))}/с</code> вверх",
            ]
        )
        ips = snapshot.get("ip_addresses", [])
        if ips:
            lines.append(f"<b>IP</b>: <code>{html.escape(', '.join(ips))}</code>")
    lines.extend(
        [
            "",
            "<b>История</b>",
            f"<b>Зарегистрирован</b>: {html.escape(format_time(device.get('registered_at')))}",
            f"<b>Последний онлайн</b>: {html.escape(format_time(device.get('last_online_at')))}",
            f"<b>Последний оффлайн</b>: {html.escape(format_time(device.get('last_offline_at')))}",
            f"<b>Смена статуса</b>: {html.escape(format_time(device.get('presence_changed_at')))}",
            f"<b>Команды всего</b>: <code>{counters['completed']}</code> успешно | <code>{counters['failed']}</code> с ошибкой",
        ]
    )
    return "\n".join(lines)


def snapshot_text(device: dict[str, Any], command_type: str) -> str | None:
    snapshot = device.get("snapshot", {})
    if not snapshot:
        return None
    updated_at = format_time(device.get("last_seen"))
    if command_type == "info":
        return (
            f"ПК: {effective_display_name(device)}\n"
            f"Обновлено: {updated_at}\n"
            f"Хост: {snapshot.get('hostname', 'н/д')}\n"
            f"Пользователь: {snapshot.get('username', 'н/д')}\n"
            f"ОС: {snapshot.get('os', 'н/д')}\n"
            f"Аптайм: {format_duration(snapshot.get('uptime_seconds'))}\n"
            f"CPU: {snapshot.get('cpu_percent', 'н/д')}%\n"
            f"RAM: {snapshot.get('memory_percent', 'н/д')}%\n"
            f"Диск: {snapshot.get('disk_percent', 'н/д')}%\n"
            f"IP: {', '.join(snapshot.get('ip_addresses', [])) or 'н/д'}\n"
            f"Wi-Fi: {snapshot.get('wifi_profile') or 'н/д'}\n"
            f"Интернет: {'ok' if snapshot.get('internet_ok') else 'нет'}"
        )
    if command_type == "uptime":
        return f"Аптайм: {format_duration(snapshot.get('uptime_seconds'))}\nОбновлено: {updated_at}"
    if command_type == "net":
        wifi_profile = snapshot.get("wifi_profile") or "н/д"
        wifi_signal = snapshot.get("wifi_signal")
        wifi_suffix = f" ({wifi_signal}%)" if wifi_signal is not None else ""
        return (
            f"IP: {', '.join(snapshot.get('ip_addresses', [])) or 'н/д'}\n"
            f"Wi-Fi: {wifi_profile}{wifi_suffix}\n"
            f"Интернет: {'ok' if snapshot.get('internet_ok') else 'нет'}\n"
            f"Скачивание: {format_bytes(snapshot.get('net_down_per_sec'))}/с\n"
            f"Отдача: {format_bytes(snapshot.get('net_up_per_sec'))}/с\n"
            f"Обновлено: {updated_at}"
        )
    if command_type == "drives":
        drives = snapshot.get("drives", [])
        return "\n".join(drives) if drives else "Нет данных по дискам."
    if command_type == "apps":
        apps = snapshot.get("apps", [])
        return "\n".join(apps) if apps else "Нет открытых окон."
    if command_type == "services":
        services = snapshot.get("services", [])
        return "\n".join(services) if services else "Нет данных по службам."
    if command_type == "top":
        top = snapshot.get("top_processes", [])
        return "\n".join(top) if top else "Нет данных по процессам."
    if command_type == "jobs":
        jobs = snapshot.get("jobs", [])
        return "\n".join(jobs) if jobs else "Нет доступных джоб."
    return None


def help_text() -> str:
    return (
        "<b>SystemPortal</b>\n"
        "Структурированная панель для ПК, мини-апп удалённого экрана и быстрые команды.\n\n"
        "<b>Основное</b>\n\n"
        "/pcs - список устройств и их статус\n"
        "/select &lt;name_or_id&gt; - выбрать активный ПК\n"
        "/status - подробная карточка выбранного ПК\n"
        "/menu - главное меню выбранного ПК\n\n"
        "/rename &lt;имя&gt; - задать подпись ПК только для бота\n\n"
        "<b>Мониторинг</b>\n\n"
        "/info - сводка по системе\n"
        "/uptime - аптайм\n"
        "/net - сеть и IP\n"
        "/specs - характеристики ПК\n"
        "/drives - диски\n"
        "/apps - открытые окна\n"
        "/top или /tasks - верхние процессы\n"
        "/services - отслеживаемые службы\n"
        "/screenshot - скриншот\n\n"
        "<b>Действия</b>\n\n"
        "/cmd &lt;powershell&gt; - выполнить PowerShell-команду\n"
        "/run &lt;alias&gt; - запустить программу\n"
        "/job &lt;alias&gt; - запустить джобу\n"
        "/closeapp &lt;pid|name&gt; - закрыть окно процесса\n"
        "/kill &lt;pid|name&gt; - завершить процесс\n"
        "/restartapp &lt;alias&gt; - перезапустить программу\n"
        "/lock - заблокировать Windows и показать экран входа\n"
        "/restart - перезагрузить ПК\n"
        "/shutdown - выключить ПК\n\n"
        "<b>Обслуживание</b>\n\n"
        "/wifi - вручную попросить агент переподключить Wi-Fi\n"
        "/update - обновить агент с GitHub\n"
        "/text &lt;текст&gt; - показать отдельное окно с текстом на экране ПК\n\n"
        "/pic &lt;ссылка&gt; или reply на фото - показать картинку на экране ПК\n\n"
        "/file &lt;имя или путь&gt; - найти файл и отправить его в Telegram\n\n"
        "/wls - открыть панель WLS / Ubuntu SSH\n\n"
        "Уведомления об онлайне и оффлайне приходят автоматически. Mini App удалённого экрана доступен из меню выбранного ПК и поднимается через автотуннель."
    )


def devices_keyboard(devices: list[dict[str, Any]], current_id: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for device in devices:
        rows.append(
            [
                premium_button(
                    device_button_text(device, current_id),
                    callback_data=f"select:{device['device_id']}",
                    emoji_id=5447607759421863856,
                )
            ]
        )
    if not rows:
        rows = [[premium_button("Пока нет устройств", callback_data="noop", emoji_id=5447381715293074599)]]
    if current_id:
        rows.append([premium_button("Назад", callback_data="action:menu_root", emoji_id=5445362436418859744)])
    return InlineKeyboardMarkup(rows)


def effective_public_base_url() -> str:
    return (PUBLIC_BASE_URL or tunnel_manager.public_url or "").strip().rstrip("/")


def public_remote_supported() -> bool:
    return effective_public_base_url().startswith("https://")


def device_home_text(device: dict[str, Any]) -> str:
    return (
        "<b>Панель ПК</b>\n"
        f"ПК: <b>{html.escape(effective_display_name(device))}</b>\n"
        f"Статус: <code>{status_badge(device)}</code>\n"
        f"Heartbeat: {html.escape(format_relative_age(device.get('last_seen')))}\n\n"
        "Выбери раздел ниже. Всё меню работает в одном сообщении без спама."
    )


def root_menu_keyboard(remote_url: str | None) -> InlineKeyboardMarkup:
    remote_button: InlineKeyboardButton
    if remote_url:
        remote_button = premium_button("Remote", web_app=WebAppInfo(url=remote_url), emoji_id=5445158077579952110)
    else:
        remote_button = premium_button("Remote", callback_data="action:remote_unavailable", emoji_id=5445158077579952110)
    return InlineKeyboardMarkup(
        [
            [
                premium_button("Карточка", callback_data="action:status", emoji_id=5447607759421863856),
                remote_button,
            ],
            [
                premium_button("Мониторинг", callback_data="action:section:monitor", emoji_id=5445146408153806223),
                premium_button("Управление", callback_data="action:section:control", emoji_id=5444869180899752137),
            ],
            [
                premium_button("Приложения", callback_data="action:section:apps", emoji_id=5444924349754667822),
                premium_button("WLS", callback_data="action:section:wls", emoji_id=5445224894386172410),
            ],
            [
                premium_button("Обслуживание", callback_data="action:section:maintenance", emoji_id=5447611706496808621),
                premium_button("Список ПК", callback_data="action:pcs", emoji_id=5447607759421863856),
                premium_button("Помощь", callback_data="action:help", emoji_id=5247236071795754971),
            ],
        ]
    )


def monitor_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                premium_button("Скрин", callback_data="action:screenshot", emoji_id=5447588260270341594),
                premium_button("Система", callback_data="action:info", emoji_id=5247236071795754971),
            ],
            [
                premium_button("Аптайм", callback_data="action:uptime", emoji_id=5445350406215465190),
                premium_button("Сеть", callback_data="action:net", emoji_id=5447448489149625830),
            ],
            [
                premium_button("Характеристики", callback_data="action:specs", emoji_id=5444869180899752137),
                premium_button("Диски", callback_data="action:drives", emoji_id=5445260044398524944),
            ],
            [
                premium_button("Службы", callback_data="action:services", emoji_id=5445203741672243391),
                premium_button("Окна", callback_data="action:apps", emoji_id=5447190164046639079),
                premium_button("Процессы", callback_data="action:top", emoji_id=5445146408153806223),
            ],
            [premium_button("Назад", callback_data="action:menu_root", emoji_id=5445362436418859744)],
        ]
    )


def control_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                premium_button("Блок", callback_data="action:lock", emoji_id=5445373775132522312),
                premium_button("Рестарт", callback_data="action:restart", emoji_id=5445388803223091254),
            ],
            [
                premium_button("Закрыть окно", callback_data="action:close_active", emoji_id=5447434637880098257),
                premium_button("/text", callback_data="action:text_help", emoji_id=5444889156792646660),
            ],
            [
                premium_button("Выключить", callback_data="action:shutdown", emoji_id=5287372146039861774),
                premium_button("Назад", callback_data="action:menu_root", emoji_id=5445362436418859744),
            ],
        ]
    )


def apps_section_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                premium_button("Окна", callback_data="action:apps", emoji_id=5447190164046639079),
                premium_button("Процессы", callback_data="action:top", emoji_id=5445146408153806223),
            ],
            [premium_button("Джобы", callback_data="action:jobs", emoji_id=5444883062234053429)],
            [premium_button("Назад", callback_data="action:menu_root", emoji_id=5445362436418859744)],
        ]
    )


def maintenance_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                premium_button("Wi-Fi", callback_data="action:wifi", emoji_id=5447602197439218445),
                premium_button("Update", callback_data="action:update", emoji_id=5445388803223091254),
            ],
            [premium_button("Переименовать", callback_data="action:rename_help", emoji_id=5445128296276718145)],
            [premium_button("Удалить", callback_data="action:delete_prompt", emoji_id=5445005936953424165)],
            [premium_button("Назад", callback_data="action:menu_root", emoji_id=5445362436418859744)],
        ]
    )


def wls_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                premium_button("Статус", callback_data="action:wls_info", emoji_id=5247236071795754971),
                premium_button("Запуск", callback_data="action:wls_start", emoji_id=5444883062234053429),
            ],
            [
                premium_button("Стоп", callback_data="action:wls_stop", emoji_id=5445092669522996408),
                premium_button("Назад", callback_data="action:menu_root", emoji_id=5445362436418859744),
            ],
        ]
    )


def delete_confirm_keyboard(device_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [premium_button("Удалить с ПК", callback_data=f"delete:confirm:{device_id}", emoji_id=5445005936953424165)],
            [premium_button("Назад", callback_data="action:section:maintenance", emoji_id=5445362436418859744)],
        ]
    )


def delete_prompt_text(device: dict[str, Any]) -> str:
    return (
        "<b>Удаление SystemPortal</b>\n"
        f"Устройство: <b>{html.escape(effective_display_name(device))}</b>\n"
        f"ID: <code>{html.escape(device.get('device_id', 'n/a'))}</code>\n\n"
        "После подтверждения устройство исчезнет из списка. Если агент онлайн, он удалит папку установки, автозапуск, временные инсталляторы и старые следы SchoolPro/SystemPortal."
    )


def app_keyboard(app_lines: list[str]) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for line in app_lines[:8]:
        parts = line.split(" | ", 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid = parts[0]
        name = parts[1]
        rows.append([premium_button(f"Закрыть {pid} {name}", callback_data=f"proc:close:{pid}", emoji_id=5447434637880098257)])
    rows.append([premium_button("Назад", callback_data="action:section:apps", emoji_id=5445362436418859744)])
    return InlineKeyboardMarkup(rows)


def process_keyboard(process_lines: list[str]) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for line in process_lines[:8]:
        parts = line.split(" | ", 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid = parts[0]
        name = parts[1]
        rows.append([premium_button(f"Завершить {pid} {name}", callback_data=f"proc:kill:{pid}", emoji_id=5445092669522996408)])
    rows.append([premium_button("Назад", callback_data="action:section:apps", emoji_id=5445362436418859744)])
    return InlineKeyboardMarkup(rows)


def jobs_keyboard(job_names: list[str]) -> InlineKeyboardMarkup | None:
    rows = [[premium_button(f"Запустить {job}", callback_data=f"job:run:{job}", emoji_id=5444883062234053429)] for job in job_names[:10]]
    rows.append([premium_button("Назад", callback_data="action:section:apps", emoji_id=5445362436418859744)])
    return InlineKeyboardMarkup(rows)


async def notify_owner(text: str) -> None:
    owner = await store.get_owner()
    if telegram_app is None or owner is None:
        return
    await telegram_app.bot.send_message(chat_id=owner, text=text)


async def authorize(update: Update) -> bool:
    if update.effective_chat is None:
        return False
    if update.effective_chat.id == FIXED_OWNER_CHAT_ID:
        with contextlib.suppress(Exception):
            await store.claim_owner(FIXED_OWNER_CHAT_ID)
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Доступ запрещён.")
    return False


async def selected_device(chat_id: int, explicit: str | None = None) -> dict[str, Any] | None:
    devices = await store.list_devices()
    if not devices:
        return None
    if explicit:
        needle = explicit.strip().lower()
        if not needle:
            return None
        for device in devices:
            values = {
                str(device.get("device_id", "")).lower(),
                str(device.get("display_name", "")).lower(),
                str(device.get("bot_label", "")).lower(),
                str(device.get("hostname", "")).lower(),
            }
            if needle in values:
                await store.set_selected(chat_id, device["device_id"])
                return device
        partial_matches = [
            device
            for device in devices
            if any(
                needle in value
                for value in {
                    str(device.get("device_id", "")).lower(),
                    str(device.get("display_name", "")).lower(),
                    str(device.get("bot_label", "")).lower(),
                    str(device.get("hostname", "")).lower(),
                }
            )
        ]
        if len(partial_matches) == 1:
            await store.set_selected(chat_id, partial_matches[0]["device_id"])
            return partial_matches[0]
        return None
    selected_id = await store.get_selected(chat_id)
    if selected_id:
        for device in devices:
            if device["device_id"] == selected_id:
                return device
    if len(devices) == 1:
        await store.set_selected(chat_id, devices[0]["device_id"])
        return devices[0]
    return None


async def build_remote_url(chat_id: int, device_id: str) -> str | None:
    if not public_remote_supported():
        await tunnel_manager.ensure_running()
    if not public_remote_supported():
        return None
    token = await remote_sessions.create(chat_id, device_id)
    return f"{effective_public_base_url()}/remote/{token}"


async def root_menu_markup(chat_id: int, device_id: str) -> InlineKeyboardMarkup:
    return root_menu_keyboard(await build_remote_url(chat_id, device_id))


async def show_device_root(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    device: dict[str, Any],
) -> None:
    if update.effective_chat is None:
        return
    await send_text(
        update,
        context,
        device_home_text(device),
        reply_markup=await root_menu_markup(update.effective_chat.id, device["device_id"]),
        parse_mode="HTML",
    )


def section_text(title: str, description: str) -> str:
    return f"<b>{EMOJI['info']} {html.escape(title)}</b>\n{html.escape(description)}"


def extract_command_text(update: Update) -> str:
    message = update.effective_message
    if message is None:
        return ""
    raw_text = message.text or message.caption or ""
    parts = raw_text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def format_shell_result_html(payload: dict[str, Any]) -> str:
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    command = str(data.get("command", "")).strip()
    stdout = str(data.get("stdout", "")).strip()
    stderr = str(data.get("stderr", "")).strip()
    fallback_message = str(payload.get("message", "")).strip()
    shell_name = str(data.get("shell", "powershell")).strip() or "powershell"
    cwd = str(data.get("cwd", "")).strip()
    exit_code = data.get("returncode")
    duration = data.get("duration_sec")
    timed_out = bool(data.get("timed_out"))
    output_truncated = bool(data.get("truncated"))
    fallback_cut = False

    command_preview, command_cut = trim_text(command, CMD_COMMAND_PREVIEW_CHARS)
    stdout_preview, stdout_cut = trim_text(stdout, CMD_OUTPUT_PREVIEW_CHARS)
    stderr_preview, stderr_cut = trim_text(stderr, CMD_OUTPUT_PREVIEW_CHARS)

    lines = [
        f"<b>CMD</b>: {'OK' if payload.get('ok') else 'ошибка'}",
        f"<b>Shell</b>: <code>{html.escape(shell_name)}</code>",
    ]
    if cwd:
        lines.append(f"<b>Папка</b>: <code>{html.escape(cwd)}</code>")
    if exit_code is not None:
        lines.append(f"<b>Код выхода</b>: <code>{html.escape(str(exit_code))}</code>")
    if duration is not None:
        lines.append(f"<b>Время</b>: <code>{html.escape(f'{float(duration):.2f}')} c</code>")
    if timed_out:
        lines.append("<b>Статус</b>: превышен таймаут выполнения")
    if command_preview:
        lines.append(f"<b>Команда</b>:\n<blockquote expandable>{html.escape(command_preview)}</blockquote>")
    if stdout_preview:
        lines.append(f"<b>STDOUT</b>:\n<blockquote expandable>{html.escape(stdout_preview)}</blockquote>")
    if stderr_preview:
        lines.append(f"<b>STDERR</b>:\n<blockquote expandable>{html.escape(stderr_preview)}</blockquote>")
    if not stdout_preview and not stderr_preview and fallback_message:
        fallback_preview, fallback_cut = trim_text(fallback_message, CMD_OUTPUT_PREVIEW_CHARS)
        lines.append(f"<b>Сообщение</b>:\n<blockquote expandable>{html.escape(fallback_preview)}</blockquote>")
    if not stdout_preview and not stderr_preview and not fallback_message:
        lines.append("<i>Команда не вывела текст.</i>")
    if output_truncated or command_cut or stdout_cut or stderr_cut or fallback_cut:
        lines.append("<i>Полный вывод отправлен отдельным файлом, если он не поместился в сообщение.</i>")
    return "\n".join(lines)


async def run_and_wait(
    device_id: str,
    command_type: str,
    args: dict[str, Any] | None = None,
    timeout_seconds: int | float | None = None,
) -> dict[str, Any] | None:
    queued = await store.queue_command(device_id, command_type, args)
    deadline = asyncio.get_running_loop().time() + float(timeout_seconds or COMMAND_TIMEOUT_SECONDS)
    while asyncio.get_running_loop().time() < deadline:
        current = await store.get_command(device_id, queued["id"])
        if current and current.get("status") in {"completed", "failed"}:
            return current
        await asyncio.sleep(COMMAND_RESULT_POLL_INTERVAL_SECONDS)
    return None


async def send_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    if update.effective_chat is None:
        return
    query = update.callback_query
    if query is not None and query.message is not None:
        try:
            await query.message.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
        except Exception:
            pass
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )


def command_reply_markup(
    command_type: str,
    payload: dict[str, Any] | None = None,
) -> InlineKeyboardMarkup | None:
    payload = payload or {}
    reply_markup = None
    if command_type == "apps":
        items = payload.get("data", {}).get("apps", [])
        if isinstance(items, list):
            reply_markup = app_keyboard([str(item) for item in items])
    if command_type == "top":
        items = payload.get("data", {}).get("processes", [])
        if isinstance(items, list):
            reply_markup = process_keyboard([str(item) for item in items])
    if command_type == "jobs":
        items = payload.get("data", {}).get("jobs", [])
        if isinstance(items, list):
            reply_markup = jobs_keyboard([str(item) for item in items])
    if command_type in {"info", "uptime", "net", "drives", "services", "screenshot", "specs"}:
        reply_markup = monitor_keyboard()
    if command_type == "wsl_info":
        reply_markup = wls_keyboard()
    alias_value = str(payload.get("data", {}).get("alias", "")).strip().lower()
    if command_type == "run_job" and alias_value.startswith("wsl_ubuntu_ssh_"):
        reply_markup = wls_keyboard()
    if command_type in {"close_app", "kill_process", "run_job", "restart_app", "run_alias", "top", "apps", "jobs"} and reply_markup is None:
        reply_markup = apps_section_keyboard()
    if command_type in {"lock_pc", "restart_pc", "shutdown_pc", "show_text", "close_foreground_window"}:
        reply_markup = control_keyboard()
    if command_type in {"wifi_recover", "self_update", "find_file"}:
        reply_markup = maintenance_keyboard()
    return reply_markup


async def send_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_type: str,
    result: dict[str, Any] | None,
) -> None:
    if update.effective_chat is None:
        return
    if result is None:
        await send_text(update, context, f"Команда {command_type}: время ожидания вышло. ПК может быть оффлайн.")
        return
    payload = result.get("result", {})
    if command_type == "shell_cmd":
        await send_text(update, context, format_shell_result_html(payload), reply_markup=control_keyboard(), parse_mode="HTML")
        file_path = payload.get("file_path")
        if file_path and Path(file_path).exists():
            with Path(file_path).open("rb") as handle:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=handle,
                    filename=payload.get("file_name") or Path(file_path).name,
                )
        return
    if not payload.get("ok", False):
        await send_text(
            update,
            context,
            f"Команда {command_type}: ошибка.\n{payload.get('message', 'Без деталей')}",
            reply_markup=command_reply_markup(command_type, payload),
        )
        return
    reply_markup = command_reply_markup(command_type, payload)
    file_path = payload.get("file_path")
    if file_path and Path(file_path).exists():
        suffix = Path(file_path).suffix.lower()
        raw = Path(file_path).read_bytes()
        buffer = io.BytesIO(raw)
        buffer.name = payload.get("file_name") or Path(file_path).name
        if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=buffer,
                caption=payload.get("message") or None,
            )
        else:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=buffer,
                filename=buffer.name,
            )
        if command_type == "find_file":
            await send_text(update, context, payload.get("message", "").strip() or "Файл отправлен.", reply_markup=maintenance_keyboard())
        return
    text = payload.get("message", "").strip() or json.dumps(payload.get("data", {}), indent=2, ensure_ascii=False)
    await send_text(update, context, text, reply_markup=reply_markup)


async def run_for_current(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_type: str,
    args: dict[str, Any] | None = None,
    use_cache: bool = True,
) -> None:
    if not await authorize(update):
        return
    if update.effective_chat is None:
        return
    device = await selected_device(update.effective_chat.id)
    if not device:
        await send_text(update, context, "ПК не выбран. Сначала открой /pcs.")
        return
    cached = snapshot_text(device, command_type) if use_cache else None
    if use_cache and cached is not None:
        await send_text(update, context, cached, reply_markup=command_reply_markup(command_type))
        return
    result = await run_and_wait(device["device_id"], command_type, args)
    await send_result(update, context, command_type, result)


async def delete_device_installation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    device_id: str,
) -> None:
    device = await store.get_device(device_id)
    if not device:
        await send_text(update, context, "Устройство не найдено.")
        return
    queued_remote_delete = False
    if is_online(device):
        with contextlib.suppress(Exception):
            await store.queue_command(device_id, "uninstall_self")
            queued_remote_delete = True
    await store.remove_device(device_id)
    devices = await store.list_devices()
    current = await selected_device(update.effective_chat.id) if update.effective_chat is not None else None
    note = "Команда самоудаления отправлена." if queued_remote_delete else "Устройство просто убрано из списка бота."
    await send_text(
        update,
        context,
        f"<b>{EMOJI['delete']} Устройство удалено</b>\n<b>{html.escape(effective_display_name(device))}</b> скрыто из списка.\n{html.escape(note)}",
        reply_markup=devices_keyboard(devices, current["device_id"] if current else None),
        parse_mode="HTML",
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_message is None:
        return
    if update.effective_chat.id != FIXED_OWNER_CHAT_ID:
        await update.effective_message.reply_text("Доступ запрещён.")
        return
    await store.claim_owner(FIXED_OWNER_CHAT_ID)
    await update.effective_message.reply_text(
        f"{EMOJI['app']} <b>SystemPortal активен</b>\nОткрой /pcs, выбери ПК и дальше работай через разделы меню.",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await authorize(update):
        await send_text(update, context, help_text(), parse_mode="HTML")


async def pcs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None:
        return
    devices = await store.list_devices()
    online_count = sum(1 for device in devices if is_online(device))
    current = await selected_device(update.effective_chat.id)
    body = (
        f"<b>{EMOJI['pc']} Устройства</b>\n"
        f"Всего: <code>{len(devices)}</code> | Онлайн: <code>{online_count}</code> | Оффлайн: <code>{len(devices) - online_count}</code>\n\n"
    )
    if current:
        body += f"<b>Активный ПК</b>: {html.escape(effective_display_name(current))} | <code>{status_badge(current)}</code>\n\n"
    if devices:
        lines: list[str] = []
        for index, device in enumerate(devices, start=1):
            marker = "<code>ACTIVE</code> | " if current and current["device_id"] == device["device_id"] else ""
            lines.append(
                f"{index}. {marker}<b>{html.escape(effective_display_name(device))}</b> | <code>{status_badge(device)}</code> | "
                f"{html.escape(format_relative_age(device.get('last_seen')))} | <code>{html.escape(device['device_id'])}</code>"
            )
        body += "\n".join(lines)
    else:
        body += f"{EMOJI['warning']} Пока нет подключённых агентов."
    await send_text(
        update,
        context,
        body,
        reply_markup=devices_keyboard(devices, current["device_id"] if current else None),
        parse_mode="HTML",
    )


async def select_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None:
        return
    if not context.args:
        current = await selected_device(update.effective_chat.id)
        if not current:
            await send_text(update, context, "ПК не выбран. Сначала открой /pcs.")
            return
        await show_device_root(update, context, current)
        return
    current = await selected_device(update.effective_chat.id, " ".join(context.args))
    if not current:
        await send_text(update, context, "ПК не найден.")
        return
    await show_device_root(update, context, current)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None:
        return
    current = await selected_device(update.effective_chat.id)
    if not current:
        await send_text(update, context, "ПК не выбран. Сначала открой /pcs.")
        return
    await send_text(
        update,
        context,
        device_card(current),
        reply_markup=await root_menu_markup(update.effective_chat.id, current["device_id"]),
        parse_mode="HTML",
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None:
        return
    current = await selected_device(update.effective_chat.id)
    if not current:
        await send_text(update, context, "Сначала выбери ПК через /pcs.")
        return
    await show_device_root(update, context, current)


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "info")


async def uptime_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "uptime")


async def net_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "net")


async def drives_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "drives")


async def apps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None:
        return
    device = await selected_device(update.effective_chat.id)
    if not device:
        await send_text(update, context, "ПК не выбран. Сначала открой /pcs.")
        return
    cached = snapshot_text(device, "apps")
    app_lines = [str(item) for item in device.get("snapshot", {}).get("apps", [])]
    if cached is not None:
        await send_text(update, context, cached, reply_markup=app_keyboard(app_lines))
        return
    result = await run_and_wait(device["device_id"], "apps")
    await send_result(update, context, "apps", result)


async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None:
        return
    device = await selected_device(update.effective_chat.id)
    if not device:
        await send_text(update, context, "ПК не выбран. Сначала открой /pcs.")
        return
    cached = snapshot_text(device, "jobs")
    job_lines = [str(item) for item in device.get("snapshot", {}).get("jobs", [])]
    if cached is not None:
        await send_text(update, context, cached, reply_markup=jobs_keyboard(job_lines))
        return
    result = await run_and_wait(device["device_id"], "jobs")
    await send_result(update, context, "jobs", result)


async def services_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "services")


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None:
        return
    device = await selected_device(update.effective_chat.id)
    if not device:
        await send_text(update, context, "ПК не выбран. Сначала открой /pcs.")
        return
    cached = snapshot_text(device, "top")
    process_lines = [str(item) for item in device.get("snapshot", {}).get("top_processes", [])]
    if cached is not None:
        await send_text(update, context, cached, reply_markup=process_keyboard(process_lines))
        return
    result = await run_and_wait(device["device_id"], "top")
    await send_result(update, context, "top", result)


async def screenshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "screenshot")


async def lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "lock_pc")


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "restart_pc")


async def shutdown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "shutdown_pc")


async def close_active_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "close_foreground_window", use_cache=False)


async def wifi_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "wifi_recover", use_cache=False)


async def update_agent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None:
        return
    device = await selected_device(update.effective_chat.id)
    if not device:
        await send_text(update, context, "ПК не выбран. Сначала открой /pcs.")
        return
    result = await run_and_wait(device["device_id"], "self_update")
    if result is None:
        await send_text(update, context, "Обновление не подтвердилось: устройство не ответило вовремя.")
        return
    payload = result.get("result", {})
    if not payload.get("ok", False) and "self_update" in str(payload.get("message", "")):
        await send_text(
            update,
            context,
            "На этом ПК ещё старый агент без self-update. Один раз переустанови его или обнови вручную, и дальше кнопка Update будет работать уже сама с GitHub.",
        )
        return
    await send_result(update, context, "self_update", result)


async def text_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text_value = extract_command_text(update)
    if not text_value:
        if await authorize(update):
            await send_text(update, context, "Использование: /text <текст>")
        return
    await run_for_current(update, context, "show_text", {"text": text_value}, use_cache=False)


async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None:
        return
    device = await selected_device(update.effective_chat.id)
    if not device:
        await send_text(update, context, "ПК не выбран. Сначала открой /pcs.")
        return
    new_name = extract_command_text(update)
    if not new_name:
        await send_text(
            update,
            context,
            "Использование: /rename <новая подпись>. Пустое имя не принимается.",
            reply_markup=maintenance_keyboard(),
        )
        return
    updated = await store.set_bot_label(device["device_id"], new_name)
    if not updated:
        await send_text(update, context, "ПК не найден.")
        return
    await show_device_root(update, context, updated)


async def pic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update):
        return
    message = update.effective_message
    if message is None:
        return
    args_text = extract_command_text(update)
    payload: dict[str, Any] | None = None

    reply = message.reply_to_message
    if reply and reply.photo:
        photo = reply.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())
        payload = {
            "image_b64": base64.b64encode(image_bytes).decode("ascii"),
            "source": "telegram_photo",
        }
    elif args_text and re.match(r"^https?://", args_text, flags=re.IGNORECASE):
        payload = {
            "url": args_text.strip(),
            "source": "url",
        }

    if payload is None:
        await send_text(update, context, "Использование: /pic <ссылка> или reply на фото.", reply_markup=control_keyboard())
        return
    await run_for_current(update, context, "show_picture", payload, use_cache=False)


async def file_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update):
        return
    query = extract_command_text(update)
    if not query:
        await send_text(update, context, "Использование: /file <имя, часть пути или полный путь>.", reply_markup=maintenance_keyboard())
        return
    await run_for_current(update, context, "find_file", {"query": query}, use_cache=False, )


async def specs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "specs", use_cache=False)


async def wls_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_for_current(update, context, "wsl_info", use_cache=False)


async def wls_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None:
        return
    device = await selected_device(update.effective_chat.id)
    if not device:
        await send_text(update, context, "ПК не выбран. Сначала открой /pcs.")
        return
    result = await run_and_wait(device["device_id"], "run_job", {"alias": "wsl_ubuntu_ssh_start"})
    await send_result(update, context, "run_job", result)


async def wls_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None:
        return
    device = await selected_device(update.effective_chat.id)
    if not device:
        await send_text(update, context, "ПК не выбран. Сначала открой /pcs.")
        return
    result = await run_and_wait(device["device_id"], "run_job", {"alias": "wsl_ubuntu_ssh_stop"})
    await send_result(update, context, "run_job", result)


async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        if update.effective_message:
            await update.effective_message.reply_text("Использование: /run <alias>")
        return
    await run_for_current(update, context, "run_alias", {"alias": context.args[0]})


async def job_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        if update.effective_message:
            await update.effective_message.reply_text("Использование: /job <alias>")
        return
    await run_for_current(update, context, "run_job", {"alias": context.args[0]})


async def cmd_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    command_text = extract_command_text(update)
    if not command_text:
        if update.effective_message:
            await update.effective_message.reply_text("Использование: /cmd <powershell-команда>")
        return
    await run_for_current(update, context, "shell_cmd", {"command": command_text})


async def closeapp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        if update.effective_message:
            await update.effective_message.reply_text("Использование: /closeapp <pid|name>")
        return
    await run_for_current(update, context, "close_app", {"target": context.args[0]})


async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        if update.effective_message:
            await update.effective_message.reply_text("Использование: /kill <pid|name>")
        return
    await run_for_current(update, context, "kill_process", {"target": context.args[0]})


async def restartapp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        if update.effective_message:
            await update.effective_message.reply_text("Использование: /restartapp <alias>")
        return
    await run_for_current(update, context, "restart_app", {"alias": context.args[0]})


async def wls_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update):
        return
    await send_text(
        update,
        context,
        section_text("WLS", "Ubuntu в WSL, запуск SSH, получение логина и остановка сессии."),
        reply_markup=wls_keyboard(),
        parse_mode="HTML",
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    with contextlib.suppress(Exception):
        await query.answer()
    if not await authorize(update) or update.effective_chat is None or query.message is None:
        return
    data = query.data or ""
    if data == "noop":
        return
    if data.startswith("select:"):
        device_id = data.split(":", 1)[1]
        device = await store.get_device(device_id)
        if not device:
            await send_text(update, context, "ПК не найден.")
            return
        await store.set_selected(update.effective_chat.id, device_id)
        await send_text(
            update,
            context,
            device_home_text(device),
            reply_markup=await root_menu_markup(update.effective_chat.id, device_id),
            parse_mode="HTML",
        )
        return
    if data == "action:pcs":
        await pcs_command(update, context)
        return
    if data == "action:help":
        await send_text(
            update,
            context,
            help_text(),
            reply_markup=InlineKeyboardMarkup([[premium_button("Назад", callback_data="action:menu_root", emoji_id=5445362436418859744)]]),
            parse_mode="HTML",
        )
        return
    if data == "action:specs":
        await specs_command(update, context)
        return
    if data == "action:wls_info":
        await wls_info_command(update, context)
        return
    if data == "action:wls_start":
        await wls_start_command(update, context)
        return
    if data == "action:wls_stop":
        await wls_stop_command(update, context)
        return
    if data == "action:menu_root":
        current = await selected_device(update.effective_chat.id)
        if not current:
            await send_text(update, context, "Сначала выбери устройство через /pcs.")
            return
        await send_text(
            update,
            context,
            device_home_text(current),
            reply_markup=await root_menu_markup(update.effective_chat.id, current["device_id"]),
            parse_mode="HTML",
        )
        return
    if data == "action:remote_unavailable":
        current = await selected_device(update.effective_chat.id)
        await send_text(
            update,
            context,
            "Mini App ещё прогревается. Бот сам поднимает HTTPS-туннель и подтянет remote, как только публичный адрес станет готов.",
            reply_markup=await root_menu_markup(update.effective_chat.id, current["device_id"]) if current else None,
        )
        return
    if data == "action:text_help":
        await send_text(
            update,
            context,
            f"{EMOJI['text']} Использование: <code>/text тут твой текст</code>",
            reply_markup=control_keyboard(),
            parse_mode="HTML",
        )
        return
    if data == "action:rename_help":
        await send_text(
            update,
            context,
            "Использование: /rename <новая подпись>. Это меняет только имя в боте.",
            reply_markup=maintenance_keyboard(),
        )
        return
    if data == "action:section:monitor":
        await send_text(
            update,
            context,
            section_text("Мониторинг", "Система, сеть, аптайм, скриншоты и процессы выбранного ПК."),
            reply_markup=monitor_keyboard(),
            parse_mode="HTML",
        )
        return
    if data == "action:section:control":
        await send_text(
            update,
            context,
            section_text("Управление", "Блокировка, рестарт, выключение и показ текста на экране."),
            reply_markup=control_keyboard(),
            parse_mode="HTML",
        )
        return
    if data == "action:section:apps":
        await send_text(
            update,
            context,
            section_text("Приложения", "Окна, процессы и джобы выбранного ПК."),
            reply_markup=apps_section_keyboard(),
            parse_mode="HTML",
        )
        return
    if data == "action:section:wls":
        await send_text(
            update,
            context,
            section_text("WLS", "Ubuntu в WSL, запуск SSH, получение логина и остановка сессии."),
            reply_markup=wls_keyboard(),
            parse_mode="HTML",
        )
        return
    if data == "action:section:maintenance":
        await send_text(
            update,
            context,
            section_text("Обслуживание", "Wi-Fi recovery, update агента и удаление установки."),
            reply_markup=maintenance_keyboard(),
            parse_mode="HTML",
        )
        return
    if data == "action:delete_prompt":
        current = await selected_device(update.effective_chat.id)
        if not current:
            await send_text(update, context, "Сначала выбери устройство через /pcs.")
            return
        await send_text(
            update,
            context,
            delete_prompt_text(current),
            reply_markup=delete_confirm_keyboard(current["device_id"]),
            parse_mode="HTML",
        )
        return
    if data.startswith("delete:confirm:"):
        device_id = data.split(":", 2)[2]
        await delete_device_installation(update, context, device_id)
        return
    if data.startswith("job:"):
        _, action, alias = data.split(":", 2)
        if action == "run":
            await run_for_current(update, context, "run_job", {"alias": alias})
        return
    if data.startswith("proc:"):
        _, action, target = data.split(":", 2)
        mapping = {
            "close": "close_app",
            "kill": "kill_process",
        }
        command_type = mapping.get(action)
        if command_type:
            await run_for_current(update, context, command_type, {"target": target})
        return
    if data.startswith("alias:"):
        _, action, alias = data.split(":", 2)
        mapping = {
            "run": "run_alias",
            "close": "close_app",
            "restart": "restart_app",
        }
        command_type = mapping.get(action)
        if command_type:
            await run_for_current(update, context, command_type, {"alias": alias})
        return
    mapping = {
        "action:status": status_command,
        "action:info": info_command,
        "action:uptime": uptime_command,
        "action:net": net_command,
        "action:drives": drives_command,
        "action:apps": apps_command,
        "action:jobs": jobs_command,
        "action:services": services_command,
        "action:top": top_command,
        "action:screenshot": screenshot_command,
        "action:lock": lock_command,
        "action:restart": restart_command,
        "action:shutdown": shutdown_command,
        "action:close_active": close_active_command,
        "action:wifi": wifi_command,
        "action:update": update_agent_command,
    }
    handler = mapping.get(data)
    if handler:
        await handler(update, context)


async def notifier_loop() -> None:
    while True:
        await remote_sessions.sweep()
        await tunnel_manager.sweep()
        for device in await store.sweep_presence():
            await notify_owner(f"{device['display_name']} теперь оффлайн. Heartbeat не приходил дольше таймаута.")
        await asyncio.sleep(PRESENCE_SWEEP_INTERVAL_SECONDS)


def remote_webapp_html(device_name: str) -> str:
    safe_name = html.escape(device_name)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <title>Remote {safe_name}</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {{
      --bg: #07111f;
      --panel: rgba(7, 17, 31, 0.9);
      --panel-soft: rgba(15, 23, 42, 0.82);
      --text: #ecfeff;
      --muted: #94a3b8;
      --accent: #22d3ee;
      --accent2: #38bdf8;
      --danger: #fb7185;
      --ok: #34d399;
      --border: rgba(148, 163, 184, 0.18);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(34, 211, 238, 0.18), transparent 32%),
        radial-gradient(circle at top right, rgba(59, 130, 246, 0.22), transparent 30%),
        linear-gradient(180deg, #020617, #07111f 58%, #020617);
      color: var(--text);
      font-family: "Segoe UI", sans-serif;
      padding: 12px;
    }}
    .shell {{
      max-width: 1120px;
      margin: 0 auto;
      display: grid;
      gap: 12px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 20px;
      backdrop-filter: blur(14px);
      box-shadow: 0 20px 50px rgba(2, 6, 23, 0.35);
      overflow: hidden;
    }}
    .hero {{
      padding: 16px 16px 8px;
    }}
    .hero h1 {{
      margin: 0 0 6px;
      font-size: 22px;
    }}
    .hero p {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .meta {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
    }}
    .pill {{
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid var(--border);
      color: var(--muted);
    }}
    .stage {{
      position: relative;
      min-height: 48vh;
      background: rgba(2, 6, 23, 0.84);
      touch-action: none;
      user-select: none;
    }}
    .stage img {{
      display: block;
      width: 100%;
      height: auto;
      max-height: 74vh;
      object-fit: contain;
      background: #020617;
    }}
    .overlay {{
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: var(--muted);
      font-size: 15px;
      background: linear-gradient(180deg, rgba(2, 6, 23, 0.08), rgba(2, 6, 23, 0.5));
      pointer-events: none;
    }}
    .toolbar {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      padding: 12px;
      background: var(--panel-soft);
      border-top: 1px solid var(--border);
    }}
    .controls {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      padding: 12px;
    }}
    button {{
      border: 0;
      border-radius: 15px;
      padding: 13px 15px;
      color: #06121f;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      font-size: 14px;
      font-weight: 700;
    }}
    button.alt {{
      color: var(--text);
      background: rgba(15, 23, 42, 0.88);
      border: 1px solid var(--border);
    }}
    button.warn {{
      color: #fff1f2;
      background: linear-gradient(135deg, #e11d48, var(--danger));
    }}
    button.active {{
      outline: 2px solid var(--ok);
      box-shadow: 0 0 0 4px rgba(52, 211, 153, 0.16);
    }}
    .keyboard {{
      display: grid;
      gap: 10px;
      padding: 12px;
      border-top: 1px solid var(--border);
      background: rgba(8, 15, 29, 0.92);
    }}
    .keyboard textarea {{
      width: 100%;
      min-height: 88px;
      resize: vertical;
      border-radius: 16px;
      border: 1px solid var(--border);
      background: rgba(15, 23, 42, 0.9);
      color: var(--text);
      padding: 12px 14px;
      font: inherit;
    }}
    .row {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .status {{
      padding: 0 16px 16px;
      color: var(--muted);
      font-size: 14px;
    }}
    .fullscreen .shell {{
      max-width: none;
    }}
    .fullscreen .stage img {{
      max-height: 84vh;
    }}
    body.immersive {{
      padding: 0;
      background: #020617;
    }}
    body.immersive .shell {{
      max-width: none;
      padding: 0;
    }}
    body.immersive .hero {{
      display: none;
    }}
    body.immersive .panel {{
      border-radius: 0;
      border-left: 0;
      border-right: 0;
    }}
    body.immersive .stage {{
      min-height: 100vh;
      border-radius: 0;
    }}
    body.immersive .stage img {{
      max-height: 100vh;
    }}
    body.rotated .stage {{
      min-height: 100vh;
      display: grid;
      place-items: center;
      overflow: hidden;
    }}
    body.rotated .stage img {{
      width: 100vh;
      max-width: none;
      max-height: none;
      height: auto;
      transform: rotate(90deg);
      transform-origin: center center;
    }}
    @media (max-width: 760px) {{
      .toolbar, .controls, .row {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="panel hero">
      <h1>Remote {safe_name}</h1>
      <p>Быстрый экран, удобный тап, drag-режим, полноэкранный просмотр и клавиатура для телефона.</p>
      <div class="meta">
        <div class="pill" id="pillLatency">Кадр: ...</div>
        <div class="pill" id="pillMode">Режим: tap</div>
        <div class="pill" id="pillSize">Экран: ...</div>
      </div>
    </section>
    <section class="panel">
      <div id="stage" class="stage">
        <img id="screen" alt="screen">
        <div id="overlay" class="overlay">Загружаю экран...</div>
      </div>
      <div class="toolbar">
    <button id="refresh">Обновить</button>
    <button id="fullscreen" class="alt">Полный экран</button>
    <button id="landscape" class="alt">Горизонтально</button>
      </div>
      <div class="controls">
        <button id="leftTap">Тап</button>
        <button id="rightClick" class="alt">Правый клик</button>
        <button id="dragMode" class="alt">Drag off</button>
        <button id="wheelUp" class="alt">Скролл вверх</button>
        <button id="wheelDown" class="warn">Скролл вниз</button>
        <button id="keyboardToggle" class="alt">Клавиатура</button>
      </div>
      <div id="keyboardPanel" class="keyboard" style="display:none">
        <textarea id="typeBox" placeholder="Введи текст, затем отправь его в активное окно на ПК"></textarea>
        <div class="row">
          <button id="typeSend">Отправить текст</button>
          <button id="typeEnter" class="alt">Enter</button>
        </div>
      </div>
      <div id="status" class="status">Подключение...</div>
    </section>
  </div>
  <script>
    const tg = window.Telegram?.WebApp;
    tg?.ready();
    tg?.expand();
    const screen = document.getElementById("screen");
    const stage = document.getElementById("stage");
    const overlay = document.getElementById("overlay");
    const statusEl = document.getElementById("status");
    const basePath = window.location.pathname.replace(/\\/$/, "");
    const refreshButton = document.getElementById("refresh");
    const fullscreenButton = document.getElementById("fullscreen");
    const landscapeButton = document.getElementById("landscape");
    const keyboardToggleButton = document.getElementById("keyboardToggle");
    const keyboardPanel = document.getElementById("keyboardPanel");
    const typeBox = document.getElementById("typeBox");
    const typeSendButton = document.getElementById("typeSend");
    const typeEnterButton = document.getElementById("typeEnter");
    const leftTapButton = document.getElementById("leftTap");
    const rightClickButton = document.getElementById("rightClick");
    const dragButton = document.getElementById("dragMode");
    const wheelUpButton = document.getElementById("wheelUp");
    const wheelDownButton = document.getElementById("wheelDown");
    const pillLatency = document.getElementById("pillLatency");
    const pillMode = document.getElementById("pillMode");
    const pillSize = document.getElementById("pillSize");
    let screenWidth = 1;
    let screenHeight = 1;
    let dragMode = false;
    let dragHeld = false;
    let pointerStart = null;
    let lastMoveAt = 0;
    let rightClickNext = false;
    let frameInFlight = false;
    let refreshTimer = null;
    let refreshDelay = 45;
    let keyboardVisible = false;
    let lastFrameStartedAt = 0;
    let moveInFlight = false;
    let queuedMove = null;
    let immersiveMode = false;
    let rotatedMode = false;
    function setStatus(text) {{
      statusEl.textContent = text;
    }}
    function setModeLabel() {{
      pillMode.textContent = `Режим: ${{dragMode ? "drag" : "tap"}}`;
      leftTapButton.classList.toggle("active", !dragMode);
      dragButton.classList.toggle("active", dragMode);
    }}
    function api(path, options) {{
      return fetch(`${{basePath}}${{path}}`, Object.assign({{ cache: "no-store" }}, options || {{}}));
    }}
    function scheduleFrame(delay) {{
      if (refreshTimer) {{
        clearTimeout(refreshTimer);
      }}
      refreshTimer = setTimeout(() => loadFrame(), delay);
    }}
    function applyViewportModes() {{
      document.body.classList.toggle("immersive", immersiveMode);
      document.body.classList.toggle("fullscreen", immersiveMode);
      document.body.classList.toggle("rotated", rotatedMode);
      fullscreenButton.classList.toggle("active", immersiveMode);
      landscapeButton.classList.toggle("active", rotatedMode);
      fullscreenButton.textContent = immersiveMode ? "Свернуть" : "Во весь экран";
      landscapeButton.textContent = rotatedMode ? "Вертикально" : "Горизонтально";
    }}
    function localToRemote(event) {{
      const rect = screen.getBoundingClientRect();
      const x = Math.max(0, Math.min(rect.width, event.clientX - rect.left));
      const y = Math.max(0, Math.min(rect.height, event.clientY - rect.top));
      return {{
        x: Math.round((x / Math.max(rect.width, 1)) * screenWidth),
        y: Math.round((y / Math.max(rect.height, 1)) * screenHeight),
      }};
    }}
    async function sendInput(payload) {{
      const response = await api("/api/input", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload),
      }});
      if (!response.ok) {{
        const data = await response.json().catch(() => ({{ detail: "Input error" }}));
        throw new Error(data.detail || "Input error");
      }}
    }}
    async function flushMoveQueue() {{
      if (moveInFlight || !queuedMove) return;
      moveInFlight = true;
      const payload = queuedMove;
      queuedMove = null;
      try {{
        await sendInput(payload);
      }} catch (error) {{
      }} finally {{
        moveInFlight = false;
        if (queuedMove) {{
          flushMoveQueue();
        }}
      }}
    }}
    function queueMove(point) {{
      queuedMove = {{ action: "move", ...point }};
      flushMoveQueue();
    }}
    async function loadFrame(forceOverlay = false) {{
      if (frameInFlight) return;
      frameInFlight = true;
      lastFrameStartedAt = performance.now();
      if (forceOverlay || !screen.src) {{
        overlay.style.display = "grid";
      }}
      try {{
        const response = await api("/api/frame");
        if (!response.ok) {{
          const data = await response.json().catch(() => ({{ detail: "Ошибка кадра" }}));
          throw new Error(data.detail || "Ошибка кадра");
        }}
        screenWidth = Number(response.headers.get("X-Screen-Width") || 1);
        screenHeight = Number(response.headers.get("X-Screen-Height") || 1);
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        screen.src = url;
        refreshDelay = 35;
        scheduleFrame(refreshDelay);
        pillSize.textContent = `Экран: ${{screenWidth}}x${{screenHeight}}`;
        pillLatency.textContent = `Кадр: ${{Math.round(performance.now() - lastFrameStartedAt)}} мс`;
        setStatus(`Экран ${{screenWidth}}x${{screenHeight}} обновлён`);
        setTimeout(() => URL.revokeObjectURL(url), 4000);
      }} catch (error) {{
        refreshDelay = Math.min(refreshDelay + 120, 900);
        scheduleFrame(refreshDelay);
        setStatus(error.message || "Не удалось получить экран");
      }} finally {{
        frameInFlight = false;
        overlay.style.display = "none";
      }}
    }}
    refreshButton.addEventListener("click", () => {{
      loadFrame(true);
    }});
    fullscreenButton.addEventListener("click", async () => {{
      try {{
        immersiveMode = !immersiveMode;
        if (typeof tg?.requestFullscreen === "function") {{
          if (immersiveMode) {{
            await tg.requestFullscreen();
            if (typeof tg?.disableVerticalSwipes === "function") {{
              tg.disableVerticalSwipes();
            }}
          }} else if (typeof tg?.exitFullscreen === "function") {{
            await tg.exitFullscreen();
          }}
        }} else if (immersiveMode && !document.fullscreenElement) {{
          await document.documentElement.requestFullscreen();
        }} else if (!immersiveMode && document.fullscreenElement) {{
          await document.exitFullscreen();
        }}
        applyViewportModes();
        setStatus(immersiveMode ? "Режим во весь экран включён" : "Обычный режим");
      }} catch (error) {{
        immersiveMode = !immersiveMode;
        applyViewportModes();
        setStatus(error.message || "Полноэкранный режим недоступен");
      }}
    }});
    landscapeButton.addEventListener("click", async () => {{
      try {{
        rotatedMode = !rotatedMode;
        if (typeof tg?.requestFullscreen === "function") {{
          await tg.requestFullscreen();
        }}
        if (typeof tg?.lockOrientation === "function") {{
          await tg.lockOrientation();
        }}
        if (rotatedMode && window.screen.orientation?.lock) {{
          await window.screen.orientation.lock("landscape");
        }} else if (!rotatedMode && window.screen.orientation?.unlock) {{
          window.screen.orientation.unlock();
        }}
        if (!immersiveMode) {{
          immersiveMode = true;
        }}
        applyViewportModes();
        setStatus(rotatedMode ? "Горизонтальный режим включён" : "Вертикальный режим включён");
      }} catch (error) {{
        rotatedMode = !rotatedMode;
        applyViewportModes();
        setStatus("Переключи телефон вручную, режим макета уже применён");
      }}
    }});
    keyboardToggleButton.addEventListener("click", () => {{
      keyboardVisible = !keyboardVisible;
      keyboardPanel.style.display = keyboardVisible ? "grid" : "none";
      keyboardToggleButton.classList.toggle("active", keyboardVisible);
      if (keyboardVisible) {{
        typeBox.focus();
      }}
    }});
    leftTapButton.addEventListener("click", () => {{
      dragMode = false;
      setModeLabel();
    }});
    rightClickButton.addEventListener("click", () => {{
      rightClickNext = true;
      setStatus("Следующий тап будет правым кликом");
    }});
    dragButton.addEventListener("click", () => {{
      dragMode = !dragMode;
      if (!dragMode && dragHeld) {{
        dragHeld = false;
      }}
      dragButton.textContent = dragMode ? "Drag on" : "Drag off";
      setModeLabel();
    }});
    wheelUpButton.addEventListener("click", async () => {{
      await sendInput({{ action: "scroll", x: screenWidth / 2, y: screenHeight / 2, delta: 120 }});
      setStatus("Скролл вверх отправлен");
      scheduleFrame(80);
    }});
    wheelDownButton.addEventListener("click", async () => {{
      await sendInput({{ action: "scroll", x: screenWidth / 2, y: screenHeight / 2, delta: -120 }});
      setStatus("Скролл вниз отправлен");
      scheduleFrame(80);
    }});
    typeSendButton.addEventListener("click", async () => {{
      const text = typeBox.value;
      if (!text.trim()) return;
      await sendInput({{ action: "type_text", text }});
      setStatus("Текст отправлен");
      typeBox.value = "";
      scheduleFrame(90);
    }});
    typeEnterButton.addEventListener("click", async () => {{
      await sendInput({{ action: "key_enter" }});
      setStatus("Enter отправлен");
      scheduleFrame(90);
    }});
    stage.addEventListener("pointerdown", async (event) => {{
      if (!screen.src) return;
      stage.setPointerCapture(event.pointerId);
      pointerStart = {{ ...localToRemote(event), moved: false }};
      if (dragMode) {{
        dragHeld = true;
        await sendInput({{ action: "button_down", ...pointerStart }});
      }}
    }});
    stage.addEventListener("pointermove", async (event) => {{
      if (!pointerStart) return;
      const now = Date.now();
      if (now - lastMoveAt < 6) return;
      lastMoveAt = now;
      const point = localToRemote(event);
      pointerStart.moved = true;
      queueMove(point);
    }});
    async function finishPointer(event) {{
      if (!pointerStart) return;
      const point = localToRemote(event);
      if (dragMode && dragHeld) {{
        dragHeld = false;
        await sendInput({{ action: "button_up", ...point }});
        setStatus("Drag отправлен");
      }} else if (rightClickNext) {{
        rightClickNext = false;
        await sendInput({{ action: "click", ...point, button: "right" }});
        setStatus("Правый клик отправлен");
      }} else {{
        await sendInput({{ action: "click", ...point, button: "left" }});
        setStatus("Клик отправлен");
      }}
      pointerStart = null;
      scheduleFrame(10);
    }}
    stage.addEventListener("pointerup", finishPointer);
    stage.addEventListener("pointercancel", finishPointer);
    document.addEventListener("fullscreenchange", () => {{
      immersiveMode = immersiveMode || Boolean(document.fullscreenElement);
      document.body.classList.toggle("fullscreen", immersiveMode || Boolean(document.fullscreenElement));
    }});
    setModeLabel();
    applyViewportModes();
    loadFrame(true);
  </script>
</body>
</html>"""


@api.get("/remote/{session_id}", response_class=HTMLResponse)
async def remote_webapp(session_id: str) -> HTMLResponse:
    session = await remote_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="remote session expired")
    device = await store.get_device(str(session.get("device_id")))
    if not device:
        raise HTTPException(status_code=404, detail="device not found")
    return HTMLResponse(remote_webapp_html(effective_display_name(device)))


@api.get("/remote/{session_id}/api/frame")
async def remote_frame(session_id: str) -> Response:
    session = await remote_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="remote session expired")
    device = await store.get_device(str(session.get("device_id")))
    if not device:
        raise HTTPException(status_code=404, detail="device not found")
    if not is_online(device):
        raise HTTPException(status_code=409, detail="device offline")
    cached = await remote_frame_broker.get_cached(device["device_id"], max_age_seconds=0.55)
    if cached is None:
        await remote_frame_broker.refresh_once(device["device_id"])
        cached = await remote_frame_broker.wait_for_frame(device["device_id"], timeout_seconds=1.6)
    if cached is None:
        raise HTTPException(status_code=504, detail="device did not return a frame in time")
    if cached.get("error"):
        raise HTTPException(status_code=500, detail=str(cached.get("error")))
    frame_bytes = cached.get("frame_bytes")
    if not frame_bytes:
        raise HTTPException(status_code=500, detail="frame missing")
    return Response(
        content=frame_bytes,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Screen-Width": str(cached.get("width", "")),
            "X-Screen-Height": str(cached.get("height", "")),
        },
    )


@api.post("/remote/{session_id}/api/input")
async def remote_input(session_id: str, payload: RemoteInputRequest) -> dict[str, Any]:
    session = await remote_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="remote session expired")
    device = await store.get_device(str(session.get("device_id")))
    if not device:
        raise HTTPException(status_code=404, detail="device not found")
    if not is_online(device):
        raise HTTPException(status_code=409, detail="device offline")
    command = await store.queue_remote_input(
        device["device_id"],
        {
            "action": payload.action,
            "x": payload.x,
            "y": payload.y,
            "button": payload.button,
            "delta": payload.delta,
            "text": payload.text,
        },
    )
    return {"ok": True, "command_id": command["id"]}


@api.post("/api/register")
async def api_register(payload: RegisterRequest) -> dict[str, Any]:
    if payload.registration_key != REGISTRATION_KEY:
        raise HTTPException(status_code=403, detail="bad registration key")
    device, created = await store.register(payload)
    if created:
        asyncio.create_task(notify_owner(f"Новое устройство подключено: {device['display_name']} ({device['device_id']})"))
    return {
        "device_id": device["device_id"],
        "display_name": device["display_name"],
        "agent_token": device["agent_token"],
    }


@api.post("/api/heartbeat")
async def api_heartbeat(payload: HeartbeatRequest) -> dict[str, Any]:
    device, became_online = await store.heartbeat(payload)
    if became_online:
        asyncio.create_task(notify_owner(f"{device['display_name']} теперь онлайн. Heartbeat получен."))
    return {"ok": True, "last_seen": device["last_seen"]}


@api.post("/api/command/next")
async def api_command_next(payload: NextCommandRequest) -> dict[str, Any]:
    return {"command": await store.get_next_command(payload)}


@api.post("/api/command/result")
async def api_command_result(payload: CommandResultRequest) -> dict[str, Any]:
    await store.complete_command(payload)
    return {"ok": True}


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("pcs", pcs_command))
    app.add_handler(CommandHandler("select", select_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("tasks", top_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("uptime", uptime_command))
    app.add_handler(CommandHandler("net", net_command))
    app.add_handler(CommandHandler("specs", specs_command))
    app.add_handler(CommandHandler("drives", drives_command))
    app.add_handler(CommandHandler("apps", apps_command))
    app.add_handler(CommandHandler("jobs", jobs_command))
    app.add_handler(CommandHandler("services", services_command))
    app.add_handler(CommandHandler("screenshot", screenshot_command))
    app.add_handler(CommandHandler("lock", lock_command))
    app.add_handler(CommandHandler("restart", restart_command))
    app.add_handler(CommandHandler("shutdown", shutdown_command))
    app.add_handler(CommandHandler("wifi", wifi_command))
    app.add_handler(CommandHandler("update", update_agent_command))
    app.add_handler(CommandHandler("text", text_command))
    app.add_handler(CommandHandler("rename", rename_command))
    app.add_handler(CommandHandler("pic", pic_command))
    app.add_handler(CommandHandler("file", file_command))
    app.add_handler(CommandHandler("wls", wls_command))
    app.add_handler(CommandHandler("cmd", cmd_command))
    app.add_handler(CommandHandler("run", run_command))
    app.add_handler(CommandHandler("job", job_command))
    app.add_handler(CommandHandler("closeapp", closeapp_command))
    app.add_handler(CommandHandler("kill", kill_command))
    app.add_handler(CommandHandler("restartapp", restartapp_command))
    app.add_handler(CallbackQueryHandler(callback_handler))


async def run_http_server() -> None:
    config = uvicorn.Config(api, host=API_HOST, port=API_PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    global telegram_app

    if not BOT_TOKEN:
        raise RuntimeError("Сначала задай PCBOT_TOKEN.")

    ensure_dirs()
    telegram_app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    register_handlers(telegram_app)

    async with telegram_app:
        await telegram_app.initialize()
        if not PUBLIC_BASE_URL and AUTO_PUBLIC_TUNNEL:
            asyncio.create_task(tunnel_manager.ensure_running())
        if telegram_app.updater is None:
            raise RuntimeError("Telegram updater is unavailable.")
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        notifier_task = asyncio.create_task(notifier_loop())
        remote_task = asyncio.create_task(remote_frame_broker.loop())
        http_task = asyncio.create_task(run_http_server())
        try:
            await http_task
        finally:
            notifier_task.cancel()
            remote_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notifier_task
            with contextlib.suppress(asyncio.CancelledError):
                await remote_task
            await telegram_app.updater.stop()
            await telegram_app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
