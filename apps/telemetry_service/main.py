"""Минимальный telemetry service: забирает батчи телеметрии из RabbitMQ
(очередь telemetry.raw, публикует их house hub) и раздаёт последние
показания по HTTP. Всё в памяти, без БД.

Эндпоинты:
    GET /api/v1/telemetry/latest[?device_id=<uuid>]
    GET /health
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pika

PORT = int(os.environ.get("PORT", "8003"))
RABBIT_URL = os.environ.get("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672")
QUEUE = "telemetry.raw"
KEEP_PER_DEVICE = 20


class TelemetryStore:
    """Последние показания по каждому устройству (ограниченный буфер)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_device = {}

    def add(self, reading: dict) -> None:
        device_id = reading.get("device_id") or "unknown"
        with self._lock:
            readings = self._by_device.setdefault(device_id, [])
            readings.append(reading)
            if len(readings) > KEEP_PER_DEVICE:
                readings.pop(0)

    def latest(self, device_id: str | None) -> list:
        with self._lock:
            if device_id:
                readings = self._by_device.get(device_id, [])
                return [readings[-1]] if readings else []
            return [readings[-1] for readings in self._by_device.values() if readings]


class TelemetryConsumer:
    """Consumer очереди telemetry.raw с переподключением при сбое."""

    def __init__(self, rabbit_url: str, store: TelemetryStore):
        self.rabbit_url = rabbit_url
        self.store = store

    def start(self) -> None:
        threading.Thread(target=self._run_forever, daemon=True).start()

    def _run_forever(self) -> None:
        while True:
            try:
                connection = pika.BlockingConnection(pika.URLParameters(self.rabbit_url))
                channel = connection.channel()
                channel.queue_declare(queue=QUEUE, durable=True)
                channel.basic_qos(prefetch_count=10)
                print(f"[telemetry] consuming {QUEUE} from {self.rabbit_url}", flush=True)
                for method, _properties, body in channel.consume(QUEUE, inactivity_timeout=1):
                    if method is None:
                        continue
                    try:
                        batch = json.loads(body)
                        for reading in batch.get("readings", []):
                            self.store.add(reading)
                    except json.JSONDecodeError:
                        print("[telemetry] dropping malformed message", flush=True)
                    channel.basic_ack(method.delivery_tag)
            except Exception as exc:
                print(f"[telemetry] rabbit unavailable ({exc}), retrying in 3s", flush=True)
                time.sleep(3)


class TelemetryHandler(BaseHTTPRequestHandler):
    server_version = "telemetry-service/1.0"
    store: TelemetryStore | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")

        if path == "health":
            self._send_json({"status": "ok"})
        elif path == "api/v1/telemetry/latest":
            device_id = (parse_qs(parsed.query).get("device_id") or [None])[0]
            self._send_json(self.store.latest(device_id))
        else:
            self._send_json({"code": "NOT_FOUND", "message": "Путь не найден."}, 404)

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    store = TelemetryStore()
    TelemetryConsumer(RABBIT_URL, store).start()

    TelemetryHandler.store = store
    server = ThreadingHTTPServer(("0.0.0.0", PORT), TelemetryHandler)
    print(f"telemetry-service listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()
