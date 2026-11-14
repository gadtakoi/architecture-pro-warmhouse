"""Симулятор хаба умного дома.

Общается с системой через единственный эндпойнт — POST /commands/ у API
Gateway: раз в тик отправляет данные своих устройств (состояния) и забирает
список команд, которые применяет локально.

Входящего HTTP нет — живость хаба видна в `docker logs smarthome-house-hub`.
"""

import json
import os
import time
import urllib.error
import urllib.request

GATEWAY_API_URL = os.environ.get("GATEWAY_API_URL", "http://api-gateway:8005")
TICK_SECONDS = float(os.environ.get("TICK_SECONDS", "4"))
THERMOSTAT_ID = "3f1c2a9e-3b4d-4c5e-8f7a-1b2c3d4e5f60"
LIGHT_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


class GatewayClient:
    """HTTP-клиент к API Gateway (stdlib); единственный внешний вызов хаба."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def exchange(self, devices: dict) -> dict | None:
        """POST /commands/: отправить данные устройств, получить команды."""
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/commands/",
            data=json.dumps({"devices": devices}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read() or b"null")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            print(f"[hub] exchange failed: {exc}", flush=True)
            return None


class HouseHub:
    """Локальные состояния устройств и применение команд от gateway."""

    def __init__(self):
        self.gateway = GatewayClient(GATEWAY_API_URL)
        self.state = {
            THERMOSTAT_ID: {"power": "on", "target_temperature": 22.0, "current_temperature": 21.4},
            LIGHT_ID: {"power": "off"},
        }

    def tick(self) -> None:
        result = self.gateway.exchange(self.state)
        for command in (result or {}).get("commands", []):
            device_id = command.get("device_id")
            if device_id not in self.state:
                continue
            state = self._apply(command)
            if state is not None:
                self.state[device_id].update(state)
                print(f"[hub] applied {command['action']} -> {state}", flush=True)

    def _apply(self, command: dict) -> dict | None:
        """Команда → патч локального состояния (None — неизвестное действие)."""
        params = command.get("params") or {}
        if command["action"] == "turn_on":
            return {"power": "on"}
        if command["action"] == "turn_off":
            return {"power": "off"}
        if command["action"] == "set_temperature":
            return {"target_temperature": float(params.get("target", 22.0))}
        return None


if __name__ == "__main__":
    hub = HouseHub()
    print(
        f"house-hub started: exchange with {GATEWAY_API_URL}/api/v1/commands/ every {TICK_SECONDS}s",
        flush=True,
    )
    while True:
        try:
            hub.tick()
        except Exception as exc:
            print(f"[hub] tick failed: {exc}", flush=True)
        time.sleep(TICK_SECONDS)
