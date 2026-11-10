"""Minimal temperature API that returns a random temperature.

Endpoints:
    GET /temperature?location=<room>[&sensor_id=<id>]
    GET /temperature/<sensor_id>
"""

import json
import os
import random
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8081"))

LOCATION_BY_SENSOR = {
    "1": "Living Room",
    "2": "Bedroom",
    "3": "Kitchen",
}
SENSOR_BY_LOCATION = {location: sensor_id for sensor_id, location in LOCATION_BY_SENSOR.items()}


def build_response(location: str, sensor_id: str) -> dict:
    if not location:
        location = LOCATION_BY_SENSOR.get(sensor_id, "Unknown")

    if not sensor_id:
        sensor_id = SENSOR_BY_LOCATION.get(location, "0")
    return {
        "value": round(random.uniform(-67.8, 45.4), 1),
        "unit": "°C",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "location": location,
        "status": "active",
        "sensor_id": sensor_id,
        "sensor_type": "temperature",
        "description": f"Random temperature for {location}",
    }


class TemperatureHandler(BaseHTTPRequestHandler):
    server_version = "temperature-api/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/temperature":
            query = parse_qs(parsed.query)
            location = query.get("location", [""])[0].strip()
            sensor_id = query.get("sensor_id", [""])[0].strip()
            self._send_json(build_response(location, sensor_id))
        elif path.startswith("/temperature/"):
            self._send_json(build_response("", path[len("/temperature/"):]))
        else:
            self._send_json({"error": "not found"}, status=404)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), TemperatureHandler)
    print(f"temperature-api listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()
