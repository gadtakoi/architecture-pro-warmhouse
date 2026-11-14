"""Минимальный device service по контракту docs/api/device-api.yaml.

Эндпоинты:
    GET    /api/v1/devices                             список устройств (demo)
    GET    /api/v1/devices/{device_id}
    PATCH  /api/v1/devices/{device_id}/state
    POST   /api/v1/devices/{device_id}/commands
    GET    /api/v1/devices/{device_id}/commands/next   выдача команды хабу (demo)
    GET    /health
"""

import json
import os
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "8002"))
HOUSE_ID = "8f14e45f-ea4e-4b7a-9b2c-6d1f0e2a7c11"

SEED_DEVICES = [
    {
        "id": "3f1c2a9e-3b4d-4c5e-8f7a-1b2c3d4e5f60",
        "serial_number": "WH-TH-000123",
        "type": "thermostat",
        "actions": ["set_temperature", "turn_on", "turn_off"],
        "state": {"power": "on", "target_temperature": 22.0, "current_temperature": 21.4},
    },
    {
        "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "serial_number": "WH-LT-000456",
        "type": "light",
        "actions": ["turn_on", "turn_off"],
        "state": {"power": "off"},
    },
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def error(code: str, message: str) -> dict:
    return {"code": code, "message": message}


class Device:
    """Устройство и его текущее состояние."""

    def __init__(self, seed: dict):
        self.id = seed["id"]
        self.serial_number = seed["serial_number"]
        self.type = seed["type"]
        self.capabilities = {"actions": seed["actions"]}
        self.status = "online"
        self.state = dict(seed["state"])

    def merge_state(self, patch: dict) -> None:
        """Переданные ключи заменяют текущие, остальные сохраняются."""
        self.state.update(patch)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "house_id": HOUSE_ID,
            "serial_number": self.serial_number,
            "type": self.type,
            "capabilities": self.capabilities,
            "status": self.status,
            "state": self.state,
        }


class Command:
    """Команда в очереди устройства."""

    def __init__(self, device_id: str, action: str, params: dict):
        self.id = str(uuid.uuid4())
        self.device_id = device_id
        self.action = action
        self.params = params
        self.status = "PENDING"
        self.created_at = now()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "action": self.action,
            "params": self.params,
            "status": self.status,
            "created_at": self.created_at,
        }


class DeviceStore:
    """In-memory хранилище устройств и очереди команд (без БД)."""

    def __init__(self):
        self._devices = {d.id: d for d in (Device(seed) for seed in SEED_DEVICES)}
        self._pending: dict[str, deque] = {}

    def all(self) -> list:
        return [d.to_dict() for d in self._devices.values()]

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def enqueue(self, device: Device, action: str, params: dict) -> Command:
        command = Command(device.id, action, params)
        self._pending.setdefault(device.id, deque()).append(command)
        return command

    def next(self, device_id: str) -> Command | None:
        """Отдаёт хабу старейшую команду в очереди и помечает её DELIVERED."""
        queue = self._pending.get(device_id)
        if not queue:
            return None
        command = queue.popleft()
        command.status = "DELIVERED"
        return command


class DeviceHandler(BaseHTTPRequestHandler):
    server_version = "device-service/1.0"
    store = DeviceStore()

    def do_OPTIONS(self):
        # preflight-запрос браузера для кнопки на демо-странице
        self._send_json({})

    def do_GET(self):
        parts = urlparse(self.path).path.strip("/").split("/")

        if parts == ["health"]:
            self._send_json({"status": "ok"})
            return

        if parts[:3] != ["api", "v1", "devices"]:
            self._send_json(error("NOT_FOUND", "Путь не найден."), status=404)
            return

        if len(parts) == 3:
            self._send_json(self.store.all())
            return

        device = self._find_device(parts[3])
        if device is None:
            return

        if len(parts) == 4:
            self._send_json(device.to_dict())
        elif parts[4:] == ["commands", "next"]:
            command = self.store.next(device.id)
            self._send_json({"command": command.to_dict() if command else None})
        else:
            self._send_json(error("NOT_FOUND", "Путь не найден."), status=404)

    def do_PATCH(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if parts[:3] != ["api", "v1", "devices"] or parts[4:] != ["state"]:
            self._send_json(error("NOT_FOUND", "Путь не найден."), status=404)
            return

        device = self._find_device(parts[3])
        if device is None:
            return

        body = self._read_json()
        if body is None:
            return
        if "state" not in body:
            self._send_json(error("BAD_REQUEST", "Отсутствует обязательное поле state."), status=400)
            return
        if not isinstance(body["state"], dict):
            self._send_json(error("VALIDATION_ERROR", "Поле state должно быть JSON-объектом."), status=422)
            return

        device.merge_state(body["state"])
        self._send_json({"device_id": device.id, "state": device.state, "updated_at": now()})

    def do_POST(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if parts[:3] != ["api", "v1", "devices"] or parts[4:] != ["commands"]:
            self._send_json(error("NOT_FOUND", "Путь не найден."), status=404)
            return

        device = self._find_device(parts[3])
        if device is None:
            return

        body = self._read_json()
        if body is None:
            return
        action = body.get("action")
        params = body.get("params")
        if not isinstance(action, str) or not isinstance(params, dict):
            self._send_json(
                error("BAD_REQUEST", "Нужны поля action (string) и params (object)."),
                status=400,
            )
            return
        if action not in device.capabilities["actions"]:
            self._send_json(
                error("UNSUPPORTED_ACTION", f"Устройство типа {device.type} не поддерживает действие {action}."),
                status=422,
            )
            return

        command = self.store.enqueue(device, action, params)
        self._send_json(command.to_dict(), status=202)

    def _find_device(self, device_id: str) -> Device | None:
        try:
            uuid.UUID(device_id)
        except ValueError:
            self._send_json(error("BAD_REQUEST", "Параметр device_id не является UUID."), status=400)
            return None
        device = self.store.get(device_id)
        if device is None:
            self._send_json(error("DEVICE_NOT_FOUND", f"Устройство {device_id} не найдено."), status=404)
            return None
        return device

    def _read_json(self) -> dict | None:
        """Читает JSON-тело запроса; None — ошибка (ответ уже отправлен)."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json(error("BAD_REQUEST", "Битый JSON."), status=400)
            return None
        if not isinstance(body, dict):
            self._send_json(error("BAD_REQUEST", "Тело запроса должно быть JSON-объектом."), status=400)
            return None
        return body

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PATCH, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), DeviceHandler)
    print(f"device-service listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()
