"""Минимальный API Gateway (To-Be схема: маршрутизация /control и /commands).

    POST /api/v1/control/   — управление от пользователя (свет, температура):
                              {"type": "light"|"thermostat", "action": ..., "params": {...}}
                              → команда ставится в очередь device service (202)
    POST /api/v1/commands/  — единственная точка обмена с house hub: хаб присылает
                              данные своих устройств (состояния), gateway ретранслирует
                              их в device service, публикует телеметрию в RabbitMQ
                              и обновляет датчик в монолите; в ответе — команды для хаба
    GET  /health

Gateway — только маршрутизация: очередь команд и валидация по capabilities
живут в device service.
"""

import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pika

PORT = int(os.environ.get("PORT", "8005"))
DEVICE_API_URL = os.environ.get("DEVICE_API_URL", "http://device-service:8002")
MONOLITH_API_URL = os.environ.get("MONOLITH_API_URL", "http://app:8080")
RABBIT_URL = os.environ.get("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672")
QUEUE = "telemetry.raw"
HOUSE_ID = "8f14e45f-ea4e-4b7a-9b2c-6d1f0e2a7c11"
MONOLITH_SENSOR_NAME = "Thermostat (from device-service)"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def error(code: str, message: str) -> dict:
    return {"code": code, "message": message}


class DeviceClient:
    """HTTP-клиент к device service; возвращает (статус, JSON-тело)."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            # ошибки 4xx/5xx проксируем клиенту как есть (422, 404 и т.д.)
            return exc.code, json.loads(exc.read() or b"null")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            print(f"[gateway] {method} {path} failed: {exc}", flush=True)
            return 502, None

    def devices(self) -> list:
        status, body = self.request("GET", "/api/v1/devices")
        return body if status == 200 and isinstance(body, list) else []

    def patch_state(self, device_id: str, state: dict) -> None:
        self.request("PATCH", f"/api/v1/devices/{device_id}/state", {"state": state})

    def queue_command(self, device_id: str, action: str, params: dict):
        return self.request(
            "POST", f"/api/v1/devices/{device_id}/commands", {"action": action, "params": params}
        )

    def next_command(self, device_id: str) -> dict | None:
        status, body = self.request("GET", f"/api/v1/devices/{device_id}/commands/next")
        return (body or {}).get("command") if status == 200 else None


class MonolithClient(DeviceClient):
    """Питает датчик легаси-монолита телеметрией через его существующий API
    (код монолита не меняется): создаёт датчик один раз и обновляет значение."""

    def __init__(self, base_url: str):
        super().__init__(base_url)
        self.sensor_id = None
        self.updates = 0

    def update_value(self, value: float) -> None:
        if self.sensor_id is None:
            self._ensure_sensor()
        if self.sensor_id is None:
            return
        status, _ = self.request(
            "PATCH", f"/api/v1/sensors/{self.sensor_id}/value", {"value": value, "status": "active"}
        )
        if status == 200:
            self.updates += 1

    def _ensure_sensor(self) -> None:
        status, sensors = self.request("GET", "/api/v1/sensors")
        if status == 200 and isinstance(sensors, list):
            for sensor in sensors:
                if sensor.get("name") == MONOLITH_SENSOR_NAME:
                    self.sensor_id = sensor["id"]
                    return
        status, created = self.request(
            "POST",
            "/api/v1/sensors",
            {"name": MONOLITH_SENSOR_NAME, "type": "telemetry", "location": "Living Room", "unit": "°C"},
        )
        if status == 201 and isinstance(created, dict):
            self.sensor_id = created["id"]


class TelemetryPublisher:
    """Публикация батчей телеметрии в RabbitMQ (durable-очередь, persistent-сообщения)."""

    def __init__(self, rabbit_url: str):
        self.rabbit_url = rabbit_url
        self._lock = threading.Lock()
        self._connection = None
        self._channel = None

    def _connect(self) -> bool:
        try:
            self._connection = pika.BlockingConnection(pika.URLParameters(self.rabbit_url))
            self._channel = self._connection.channel()
            self._channel.queue_declare(queue=QUEUE, durable=True)
            print("[gateway] connected to rabbitmq", flush=True)
            return True
        except Exception as exc:
            print(f"[gateway] rabbit not ready: {exc}", flush=True)
            self._connection = None
            self._channel = None
            return False

    def publish(self, batch: dict) -> bool:
        with self._lock:
            if self._channel is None or not self._connection.is_open:
                if not self._connect():
                    return False
            try:
                self._channel.basic_publish(
                    exchange="",
                    routing_key=QUEUE,
                    body=json.dumps(batch),
                    properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
                )
                return True
            except Exception as exc:
                print(f"[gateway] publish failed: {exc}", flush=True)
                self._connection = None
                self._channel = None
                return False


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "api-gateway/1.0"
    client = DeviceClient(DEVICE_API_URL)
    monolith = MonolithClient(MONOLITH_API_URL)
    publisher = TelemetryPublisher(RABBIT_URL)

    def do_OPTIONS(self):
        # preflight-запрос браузера для кнопки на демо-странице
        self._send_json({})

    def do_GET(self):
        parts = urlparse(self.path).path.strip("/").split("/")

        if parts == ["health"]:
            self._send_json({"status": "ok"})
        else:
            self._send_json(error("NOT_FOUND", "Путь не найден."), status=404)

    def do_POST(self):
        parts = urlparse(self.path).path.strip("/").split("/")

        if parts == ["api", "v1", "control"]:
            self._handle_control()
        elif parts == ["api", "v1", "commands"]:
            self._handle_commands()
        else:
            self._send_json(error("NOT_FOUND", "Путь не найден."), status=404)

    def _handle_control(self) -> None:
        """Управление от пользователя: команда в очередь device service."""
        body = self._read_json()
        if body is None:
            return
        action = body.get("action")
        params = body.get("params") or {}
        if not isinstance(action, str) or not isinstance(params, dict):
            self._send_json(error("BAD_REQUEST", "Нужны поля action (string) и params (object)."), status=400)
            return

        device = self._find_device(body)
        if device is None:
            return

        status, result = self.client.queue_command(device["id"], action, params)
        if result is None:
            self._send_json(error("INTERNAL_ERROR", "Device service недоступен."), status=502)
            return
        self._send_json(result, status=status)

    def _handle_commands(self) -> None:
        """Обмен с хабом: состояния хаба → device service / RabbitMQ / монолит,
        в ответ — команды для хаба (device service помечает их DELIVERED)."""
        body = self._read_json()
        if body is None:
            return
        devices = body.get("devices")
        if not isinstance(devices, dict):
            self._send_json(error("BAD_REQUEST", "Поле devices должно быть объектом {device_id: state}."), status=400)
            return

        # ретрансляция состояний хаба в device service
        for device_id, state in devices.items():
            if isinstance(state, dict):
                self.client.patch_state(device_id, state)

        # телеметрия хаба → RabbitMQ (маршрутизация в телеметрическую платформу)
        self.publisher.publish(self._build_batch(devices))

        # телеметрия питает датчик легаси-монолита (интеграция без изменения его кода)
        for state in devices.values():
            if isinstance(state, dict) and "current_temperature" in state:
                self.monolith.update_value(state["current_temperature"])
                break

        commands = []
        for device in self.client.devices():
            command = self.client.next_command(device["id"])
            if command:
                commands.append(command)
        self._send_json({"commands": commands})

    def _build_batch(self, devices: dict) -> dict:
        """Собирает TelemetryReadingBatch из данных устройств хаба."""
        readings = []
        for device_id, state in devices.items():
            if not isinstance(state, dict):
                continue
            if "current_temperature" in state:
                readings.append({"device_id": device_id, "metric": "temperature",
                                 "value": state["current_temperature"], "unit": "°C", "recorded_at": now()})
            elif "power" in state:
                readings.append({"device_id": device_id, "metric": "power",
                                 "value": 1 if state["power"] == "on" else 0, "unit": "boolean",
                                 "recorded_at": now()})
        return {"batch_id": str(uuid.uuid4()), "house_id": HOUSE_ID, "sent_at": now(), "readings": readings}

    def _find_device(self, body: dict) -> dict | None:
        """Ищет устройство по device_id или по типу (light / thermostat)."""
        device_id = body.get("device_id")
        device_type = body.get("type")
        for device in self.client.devices():
            if device["id"] == device_id or device["type"] == device_type:
                return device
        if device_id or device_type:
            self._send_json(
                error("DEVICE_NOT_FOUND", f"Устройство {device_id or device_type} не найдено."), status=404
            )
        else:
            self._send_json(error("BAD_REQUEST", "Нужен device_id или type устройства."), status=400)
        return None

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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), GatewayHandler)
    print(f"api-gateway listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()
