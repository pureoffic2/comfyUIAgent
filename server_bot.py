from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "server_data"
UPLOAD_DIR = DATA_DIR / "uploads"
STATE_PATH = DATA_DIR / "state.json"

BOT_TOKEN = os.environ.get("PCBOT_TOKEN", "").strip()
REGISTRATION_KEY = os.environ.get("PCBOT_REGISTRATION_KEY", "change-this-key").strip()
API_HOST = os.environ.get("PCBOT_HOST", "0.0.0.0").strip()
API_PORT = int(os.environ.get("PCBOT_PORT", "8080"))
ONLINE_TIMEOUT_SECONDS = int(os.environ.get("PCBOT_ONLINE_TIMEOUT", "45"))
PRESENCE_SWEEP_INTERVAL_SECONDS = int(os.environ.get("PCBOT_PRESENCE_SWEEP_INTERVAL", "5"))
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("PCBOT_COMMAND_TIMEOUT", "35"))
COMMAND_RESULT_POLL_INTERVAL_SECONDS = float(os.environ.get("PCBOT_COMMAND_POLL_INTERVAL", "0.35"))
CMD_COMMAND_PREVIEW_CHARS = int(os.environ.get("PCBOT_CMD_COMMAND_PREVIEW", "400"))
CMD_OUTPUT_PREVIEW_CHARS = int(os.environ.get("PCBOT_CMD_OUTPUT_PREVIEW", "1000"))


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

    def _load(self) -> dict[str, Any]:
        ensure_dirs()
        if self.path.exists():
            state = json.loads(self.path.read_text(encoding="utf-8"))
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
            if created:
                devices[payload.device_id] = {
                    "device_id": payload.device_id,
                    "display_name": payload.display_name.strip() or payload.hostname,
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
                device["display_name"] = payload.display_name.strip() or payload.hostname
                device["hostname"] = payload.hostname
                device["agent_version"] = payload.agent_version
                device["updated_at"] = utc_now().isoformat()
                self._ensure_device_defaults(device)
            self._save_unlocked()
            return json.loads(json.dumps(devices[payload.device_id])), created

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

    async def get_next_command(self, payload: NextCommandRequest) -> dict[str, Any] | None:
        async with self.lock:
            device = self.state.get("devices", {}).get(payload.device_id)
            if not device or device.get("agent_token") != payload.agent_token:
                raise HTTPException(status_code=403, detail="invalid agent token")
            now = utc_now()
            for command in device.setdefault("commands", []):
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


store = Store(STATE_PATH)
api = FastAPI(title="Safe Telegram PC Bot")
telegram_app: Application | None = None


def device_card(device: dict[str, Any]) -> str:
    snapshot = device.get("snapshot", {})
    counters = command_counters(device)
    last_seen = device.get("last_seen")
    lines = [
        "<b>SchoolPro</b>",
        f"<b>{html.escape(device.get('display_name', device['device_id']))}</b>",
        f"<b>Статус</b>: <code>{status_badge(device)}</code> | {html.escape(status_label(device))}",
        f"<b>ID</b>: <code>{html.escape(device.get('device_id', 'n/a'))}</code>",
        f"<b>Heartbeat</b>: {html.escape(format_time(last_seen))} | {html.escape(format_relative_age(last_seen))}",
        f"<b>Версия агента</b>: <code>{html.escape(str(device.get('agent_version', 'n/a')))}</code>",
        f"<b>Команды</b>: <code>{counters['queued']}</code> в очереди | <code>{counters['in_progress']}</code> выполняется",
    ]
    if snapshot:
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
            f"ПК: {device.get('display_name', device['device_id'])}\n"
            f"Обновлено: {updated_at}\n"
            f"Хост: {snapshot.get('hostname', 'н/д')}\n"
            f"Пользователь: {snapshot.get('username', 'н/д')}\n"
            f"ОС: {snapshot.get('os', 'н/д')}\n"
            f"Аптайм: {format_duration(snapshot.get('uptime_seconds'))}\n"
            f"CPU: {snapshot.get('cpu_percent', 'н/д')}%\n"
            f"RAM: {snapshot.get('memory_percent', 'н/д')}%\n"
            f"Диск: {snapshot.get('disk_percent', 'н/д')}%\n"
            f"IP: {', '.join(snapshot.get('ip_addresses', [])) or 'н/д'}"
        )
    if command_type == "uptime":
        return f"Аптайм: {format_duration(snapshot.get('uptime_seconds'))}\nОбновлено: {updated_at}"
    if command_type == "net":
        return (
            f"IP: {', '.join(snapshot.get('ip_addresses', [])) or 'н/д'}\n"
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
        "Основное\n\n"
        "/pcs - список устройств и их статус\n"
        "/select <name_or_id> - выбрать активный ПК\n"
        "/status - подробная карточка выбранного ПК\n"
        "/menu - кнопки управления\n\n"
        "Мониторинг\n\n"
        "/info - сводка по системе\n"
        "/uptime - аптайм\n"
        "/net - сеть и IP\n"
        "/drives - диски\n"
        "/apps - открытые окна\n"
        "/top или /tasks - верхние процессы\n"
        "/services - отслеживаемые службы\n"
        "/screenshot - скриншот\n\n"
        "Действия\n\n"
        "/cmd <powershell> - выполнить PowerShell-команду\n"
        "/run <alias> - запустить программу\n"
        "/job <alias> - запустить джобу\n"
        "/closeapp <pid|name> - закрыть окно процесса\n"
        "/kill <pid|name> - завершить процесс\n"
        "/restartapp <alias> - перезапустить программу\n"
        "/lock - заблокировать ПК\n"
        "/restart - перезагрузить ПК\n"
        "/shutdown - выключить ПК\n\n"
        "Уведомления об онлайне и оффлайне приходят автоматически. Удаление SchoolPro доступно из карточки устройства и требует подтверждения."
    )


def devices_keyboard(devices: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{index}. {device['display_name']} [{status_badge(device)}]",
                callback_data=f"select:{device['device_id']}",
            )
        ]
        for index, device in enumerate(devices, start=1)
    ]
    if not rows:
        rows = [[InlineKeyboardButton("Пока нет устройств", callback_data="noop")]]
    return InlineKeyboardMarkup(rows)


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Карточка", callback_data="action:status"),
                InlineKeyboardButton("Скриншот", callback_data="action:screenshot"),
            ],
            [
                InlineKeyboardButton("Система", callback_data="action:info"),
                InlineKeyboardButton("Аптайм", callback_data="action:uptime"),
                InlineKeyboardButton("Сеть", callback_data="action:net"),
            ],
            [
                InlineKeyboardButton("Диски", callback_data="action:drives"),
                InlineKeyboardButton("Окна", callback_data="action:apps"),
                InlineKeyboardButton("Процессы", callback_data="action:top"),
            ],
            [
                InlineKeyboardButton("Службы", callback_data="action:services"),
                InlineKeyboardButton("Джобы", callback_data="action:jobs"),
            ],
            [
                InlineKeyboardButton("Список ПК", callback_data="action:pcs"),
                InlineKeyboardButton("Помощь", callback_data="action:help"),
            ],
            [
                InlineKeyboardButton("Блок", callback_data="action:lock"),
                InlineKeyboardButton("Рестарт", callback_data="action:restart"),
            ],
            [
                InlineKeyboardButton("Выкл", callback_data="action:shutdown"),
                InlineKeyboardButton("Удалить", callback_data="action:delete_prompt"),
            ],
        ]
    )


def delete_confirm_keyboard(device_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Удалить с ПК", callback_data=f"delete:confirm:{device_id}")],
            [InlineKeyboardButton("Назад", callback_data="action:status")],
        ]
    )


def delete_prompt_text(device: dict[str, Any]) -> str:
    return (
        "<b>Удаление SchoolPro</b>\n"
        f"Устройство: <b>{html.escape(device.get('display_name', device['device_id']))}</b>\n"
        f"ID: <code>{html.escape(device.get('device_id', 'n/a'))}</code>\n\n"
        "После подтверждения агент остановится, удалит папку SchoolPro, уберет автозапуск и исчезнет из списка устройств."
    )


def app_keyboard(app_lines: list[str]) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for line in app_lines[:8]:
        parts = line.split(" | ", 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid = parts[0]
        name = parts[1]
        rows.append(
            [InlineKeyboardButton(f"Закрыть {pid} {name}", callback_data=f"proc:close:{pid}")]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


def process_keyboard(process_lines: list[str]) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for line in process_lines[:8]:
        parts = line.split(" | ", 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid = parts[0]
        name = parts[1]
        rows.append(
            [InlineKeyboardButton(f"Завершить {pid} {name}", callback_data=f"proc:kill:{pid}")]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


def jobs_keyboard(job_names: list[str]) -> InlineKeyboardMarkup | None:
    rows = [
        [InlineKeyboardButton(f"Запустить {job}", callback_data=f"job:run:{job}")]
        for job in job_names[:10]
    ]
    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


async def notify_owner(text: str) -> None:
    owner = await store.get_owner()
    if telegram_app is None or owner is None:
        return
    await telegram_app.bot.send_message(chat_id=owner, text=text)


async def authorize(update: Update) -> bool:
    if update.effective_chat is None:
        return False
    if await store.get_owner() is None:
        await store.claim_owner(update.effective_chat.id)
    if await store.is_owner(update.effective_chat.id):
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
        lines.append(f"<b>Команда</b>:\n<pre>{html.escape(command_preview)}</pre>")
    if stdout_preview:
        lines.append(f"<b>STDOUT</b>:\n<pre>{html.escape(stdout_preview)}</pre>")
    if stderr_preview:
        lines.append(f"<b>STDERR</b>:\n<pre>{html.escape(stderr_preview)}</pre>")
    if not stdout_preview and not stderr_preview and fallback_message:
        fallback_preview, fallback_cut = trim_text(fallback_message, CMD_OUTPUT_PREVIEW_CHARS)
        lines.append(f"<b>Сообщение</b>:\n<pre>{html.escape(fallback_preview)}</pre>")
    if not stdout_preview and not stderr_preview and not fallback_message:
        lines.append("<i>Команда не вывела текст.</i>")
    if output_truncated or command_cut or stdout_cut or stderr_cut or fallback_cut:
        lines.append("<i>Вывод обрезан для Telegram.</i>")
    return "\n".join(lines)


async def run_and_wait(device_id: str, command_type: str, args: dict[str, Any] | None = None) -> dict[str, Any] | None:
    queued = await store.queue_command(device_id, command_type, args)
    deadline = asyncio.get_running_loop().time() + COMMAND_TIMEOUT_SECONDS
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
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )


async def send_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_type: str,
    result: dict[str, Any] | None,
) -> None:
    if update.effective_chat is None:
        return
    if result is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Команда {command_type}: время ожидания вышло. ПК может быть оффлайн.",
        )
        return
    payload = result.get("result", {})
    if command_type == "shell_cmd":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=format_shell_result_html(payload),
            parse_mode="HTML",
        )
        return
    if not payload.get("ok", False):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Команда {command_type}: ошибка.\n{payload.get('message', 'Без деталей')}",
        )
        return
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
    file_path = payload.get("file_path")
    if file_path and Path(file_path).exists():
        with Path(file_path).open("rb") as handle:
            suffix = Path(file_path).suffix.lower()
            if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=handle,
                    caption=payload.get("message") or None,
                )
            else:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=handle,
                    filename=payload.get("file_name"),
                )
        return
    text = payload.get("message", "").strip() or json.dumps(payload.get("data", {}), indent=2, ensure_ascii=False)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=reply_markup,
    )


async def run_for_current(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_type: str,
    args: dict[str, Any] | None = None,
) -> None:
    if not await authorize(update):
        return
    if update.effective_chat is None:
        return
    device = await selected_device(update.effective_chat.id)
    if not device:
        await send_text(update, context, "ПК не выбран. Сначала открой /pcs.")
        return
    cached = snapshot_text(device, command_type)
    if cached is not None:
        await send_text(update, context, cached)
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
    if not is_online(device):
        await send_text(update, context, "Устройство оффлайн. Сначала дождитесь, когда агент будет онлайн.")
        return

    await send_text(update, context, f"Запускаю удаление SchoolPro на {device['display_name']}...")
    result = await run_and_wait(device_id, "uninstall_self")
    if result is None:
        await send_text(update, context, "Удаление не подтвердилось: устройство не ответило вовремя.")
        return

    payload = result.get("result", {})
    if not payload.get("ok", False):
        await send_text(update, context, f"Удаление не выполнено.\n{payload.get('message', 'Без деталей')}")
        return

    await store.remove_device(device_id)
    await send_text(update, context, f"SchoolPro удален с {device['display_name']}. Устройство убрано из списка.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_message is None:
        return
    await store.claim_owner(update.effective_chat.id)
    await update.effective_message.reply_text("Бот активен. Открой /pcs и выбери устройство.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await authorize(update) and update.effective_message:
        await update.effective_message.reply_text(help_text())


async def pcs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_message is None or update.effective_chat is None:
        return
    devices = await store.list_devices()
    online_count = sum(1 for device in devices if is_online(device))
    current = await selected_device(update.effective_chat.id)
    body = (
        "<b>Устройства</b>\n"
        f"Всего: <code>{len(devices)}</code> | Онлайн: <code>{online_count}</code> | Оффлайн: <code>{len(devices) - online_count}</code>\n\n"
    )
    if current:
        body += f"<b>Активный ПК</b>: {html.escape(current['display_name'])} | <code>{status_badge(current)}</code>\n\n"
    if devices:
        body += "\n".join(
            f"{'•' if current and current['device_id'] == device['device_id'] else '·'} "
            f"<b>{html.escape(device['display_name'])}</b> | <code>{status_badge(device)}</code> | "
            f"{html.escape(format_relative_age(device.get('last_seen')))} | <code>{html.escape(device['device_id'])}</code>"
            for device in devices
        )
    else:
        body += "Пока нет подключённых агентов."
    await update.effective_message.reply_html(body, reply_markup=devices_keyboard(devices))


async def select_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None or update.effective_message is None:
        return
    if not context.args:
        current = await selected_device(update.effective_chat.id)
        if not current:
            await update.effective_message.reply_text("ПК не выбран. Сначала открой /pcs.")
            return
        await update.effective_message.reply_html(device_card(current), reply_markup=menu_keyboard())
        return
    current = await selected_device(update.effective_chat.id, " ".join(context.args))
    if not current:
        await update.effective_message.reply_text("ПК не найден.")
        return
    await update.effective_message.reply_html(device_card(current), reply_markup=menu_keyboard())


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None or update.effective_message is None:
        return
    current = await selected_device(update.effective_chat.id)
    if not current:
        await update.effective_message.reply_text("ПК не выбран. Сначала открой /pcs.")
        return
    await update.effective_message.reply_html(device_card(current), reply_markup=menu_keyboard())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update) or update.effective_chat is None or update.effective_message is None:
        return
    current = await selected_device(update.effective_chat.id)
    if not current:
        await update.effective_message.reply_text("Сначала выбери ПК через /pcs.")
        return
    await update.effective_message.reply_html(
        "<b>Панель управления</b>\n"
        f"ПК: <b>{html.escape(current['display_name'])}</b>\n"
        f"Статус: <code>{status_badge(current)}</code> | {html.escape(format_relative_age(current.get('last_seen')))}",
        reply_markup=menu_keyboard(),
    )


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
            await query.message.reply_text("ПК не найден.")
            return
        await store.set_selected(update.effective_chat.id, device_id)
        await query.message.reply_html(device_card(device), reply_markup=menu_keyboard())
        return
    if data == "action:pcs":
        await pcs_command(update, context)
        return
    if data == "action:help":
        await query.message.reply_text(help_text())
        return
    if data == "action:delete_prompt":
        current = await selected_device(update.effective_chat.id)
        if not current:
            await query.message.reply_text("Сначала выбери устройство через /pcs.")
            return
        await query.message.reply_html(delete_prompt_text(current), reply_markup=delete_confirm_keyboard(current["device_id"]))
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
    }
    handler = mapping.get(data)
    if handler:
        await handler(update, context)


async def notifier_loop() -> None:
    while True:
        for device in await store.sweep_presence():
            await notify_owner(f"{device['display_name']} теперь оффлайн. Heartbeat не приходил дольше таймаута.")
        await asyncio.sleep(PRESENCE_SWEEP_INTERVAL_SECONDS)


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
    app.add_handler(CommandHandler("drives", drives_command))
    app.add_handler(CommandHandler("apps", apps_command))
    app.add_handler(CommandHandler("jobs", jobs_command))
    app.add_handler(CommandHandler("services", services_command))
    app.add_handler(CommandHandler("screenshot", screenshot_command))
    app.add_handler(CommandHandler("lock", lock_command))
    app.add_handler(CommandHandler("restart", restart_command))
    app.add_handler(CommandHandler("shutdown", shutdown_command))
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
        if telegram_app.updater is None:
            raise RuntimeError("Telegram updater is unavailable.")
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        notifier_task = asyncio.create_task(notifier_loop())
        http_task = asyncio.create_task(run_http_server())
        try:
            await http_task
        finally:
            notifier_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notifier_task
            await telegram_app.updater.stop()
            await telegram_app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
