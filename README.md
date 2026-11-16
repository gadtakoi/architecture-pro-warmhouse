# Project_template

# Задание 1. Анализ и планирование

### 1. Описание функциональности монолитного приложения

**Управление отоплением:**

- Пользователи могут контролировать отопление в доме.
- Система поддерживает изменение температуры в системе отопления дома.

**Мониторинг температуры:**

- Пользователи могут получать информацию о температуре с датчиков.
- Система поддерживает датчики, устанавливаемые только специалистом.

### 2. Анализ архитектуры монолитного приложения

Архитектура приложения представляет собой монолит на Go с СУБД Postgres. Всё синхронно. Никаких асинхронных вызовов,
микросервисов и реактивного взаимодействия в системе нет. Всё управление идёт от сервера к датчику. Данные о температуре
также получаются через запрос от сервера к датчику.

### 3. Определение доменов и границы контекстов

Домены:

- Пользователи
- Дома
- Управление устройствами
    - Датчики
    - Система отопления

### **4. Проблемы монолитного решения**

- Синхронное взаимодействие с датчиками, запросы обрабатываются последовательно.
- Ограничена, так как монолит сложно масштабировать по частям.
- Требует остановки всего приложения.

### 5. Визуализация контекста системы — диаграмма С4

[Диаграмма контекста в модели C4](diagrams/Task_1_Context.puml)

# Задание 2. Проектирование микросервисной архитектуры

**Диаграмма контейнеров (Containers)**

[Диаграмма контейнеров To-Be архитектуры в модели C4](diagrams/Task_2_Containers.puml)

**Диаграмма компонентов (Components)**

[Диаграмма компонентов ключевых микросервисов](diagrams/Task_2_Components.puml)

**Диаграмма кода (Code)**

[Диаграмма классов и последовательности для Device Control Service](diagrams/Task_2_Code.puml)

# Задание 3. Разработка ER-диаграммы

[ER-диаграмма ключевых сущностей системы](diagrams/Task_3_ER.puml)

# Задание 4. Создание и документирование API

### 1. Тип API

- Используются REST - для синхронных взаимодействий: UI запрашивает данные и ставит команды, Device Control Service
  сохраняет команды устройства, хаб опрашивает очередь команд.

### 2. Документация API

- 3 эндпоинта в одном сервисе:

- [Device Management API](docs/api/device-api.yaml) - получение информации
  об устройстве, обновление состояния, отправка команды

# Задание 5. Работа с docker и docker-compose

1) При запросе /temperature?location= будет отдавать рандомное значение температуры.

```bash
cd apps
docker compose up -d --build
```

```bash
curl http://localhost:8081/temperature?location=Living%20Room
```
```bash
curl http://localhost:8081/temperature?sensor_id=1
```

2) Приложение следует упаковать в Docker и добавить в docker-compose. Порт по умолчанию должен быть 8081

3) Кроме того для smart_home приложения требуется база данных - добавьте в docker-compose файл настройки для запуска
   postgres с указанием скрипта инициализации ./smart_home/init.sql

Для проверки можно использовать Postman коллекцию smarthome-api.postman_collection.json и вызвать:

- Create Sensor
- Get All Sensors

Должно при каждом вызове отображаться разное значение температуры

# **Задание 6. Разработка MVP**

### Решение

К монолиту добавлены два новых микросервиса и симулятор хаба умного дома. Интеграция с монолитом идёт через его
существующий API.

```
Пользователь ──POST /control──▶ API Gateway :8005 ──POST /commands──▶ Device Service :8002 (очередь команд)
House Hub (Python) ──POST /commands (данные устройств ⇄ команды)──▶ API Gateway :8005 — единственный эндпойнт хаба
API Gateway ──PATCH state──▶ Device Service :8002 · ──telemetry.raw──▶ RabbitMQ :5672 ──▶ Telemetry Service :8003 · ──PATCH sensors──▶ Монолит :8080
index.html (открывается как файл) ── iframe на все сервисы
```

API Gateway маршрутизирует `/control` (пользователь) и `/commands` (хаб общается с системой только через Gateway — по
схеме это `Rel(houses, ApiGateway, ... GET /commands)`, в MVP это один POST-обмен: хаб присылает данные устройств,
забирает команды). Очередь команд остаётся в device-домене (device service) — в MVP роли Device Service и Device Control
объединены в одном сервисе `:8002`; при росте очередь выделяется в отдельный Device Control Service со своей БД. Gateway
своего состояния не держит — только маршрутизирует.

| Сервис                   | Язык                                        | Порт                   | Что делает                                                                                                                                                                                                                                                                                                                         |
|--------------------------|---------------------------------------------|------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `apps/device_service`    | Python 3.14 (stdlib `http.server`)          | 8002                   | Реализует `docs/api/device-api.yaml`: `GET /api/v1/devices/{id}`, `PATCH /devices/{id}/state`, `POST /devices/{id}/commands` (202, очередь), плюс demo-дополнения `GET /devices` и `GET /devices/{id}/commands/next` (выдача команд через gateway)                                                                                 |
| `apps/api_gateway`       | Python 3.14 (stdlib `http.server` + `pika`) | 8005                   | API Gateway из схемы To-Be: `POST /api/v1/control/` — управление от пользователя (свет, температура, по `type` или `device_id`); `POST /api/v1/commands/` — обмен с хабом: ретранслирует состояния хаба в device service, публикует телеметрию в RabbitMQ, обновляет датчик монолита, в ответ отдаёт команды. Своего состояния нет |
| `apps/telemetry_service` | Python 3.14 (stdlib `http.server` + `pika`) | 8003                   | Consumer очереди `telemetry.raw` и `GET /api/v1/telemetry/latest` — последние показания                                                                                                                                                                                                                                            |
| `apps/house_hub`         | Python 3.14 (stdlib)                        | — (входящего HTTP нет) | Симулятор хаба: раз в 4 секунды обменивается с системой через **один** эндпойнт — `POST /commands/` API Gateway (отдаёт данные устройств, забирает команды и применяет локально); живость видна в `docker logs smarthome-house-hub`                                                                                                |
| `apps/web`               | — (открывается как файл)                    | —                      | Статическая страница `index.html` с iframe и кнопкой отправки команды; открывается двойным кликом, контейнер не нужен                                                                                                                                                                                                              |
| `apps/smart_home`        | Go (монолит)                                | 8080                   | Код не менялся; датчик «Thermostat (from device-service)» в его БД обновляется gateway — телеметрия микросервисов течёт в монолит                                                                                                                                                                                                  |
| rabbitmq                 | —                                           | 5672 / 15672           | Брокер для телеметрии; management UI на :15672 (guest/guest)                                                                                                                                                                                                                                                                       |

### **Запуск и проверка**

```bash
cd apps
docker compose up -d --build
```

Открыть `apps/web/index.html` двойным кликом в браузере — страница с iframe (обновляются каждые 3с)
и кнопками «Переключить свет» или «Задать температуру»: команда уходит в API Gateway (`POST /control`, 202 PENDING), хаб
забирает её в обмене `POST /commands/` при следующем тике, применяет — и состояние света или температуры меняется во
всех iframe.

Проверка через curl:

```bash
# список устройств (device service)
curl http://localhost:8002/api/v1/devices

# управление светом через gateway (202 Accepted)
curl -X POST http://localhost:8005/api/v1/control/ -H "Content-Type: application/json" -d '{"type": "light", "action": "turn_on", "params": {}}'

# управление отоплением через gateway: целевая температура (202 Accepted)
curl -X POST http://localhost:8005/api/v1/control/ -H "Content-Type: application/json" -d '{"type": "thermostat", "action": "set_temperature", "params": {"target": 25.5}}'

# то же напрямую по контракту docs/api/device-api.yaml, мимо gateway (202 Accepted)
curl -X POST http://localhost:8002/api/v1/devices/7c9e6679-7425-40de-944b-e07fc1f90ae7/commands -H "Content-Type: application/json" -d '{"action": "turn_off", "params": {}}'

curl -X POST http://localhost:8002/api/v1/devices/3f1c2a9e-3b4d-4c5e-8f7a-1b2c3d4e5f60/commands -H "Content-Type: application/json" -d '{"action": "set_temperature", "params": {"target": 39}}'

curl -X POST http://localhost:8002/api/v1/devices/3f1c2a9e-3b4d-4c5e-8f7a-1b2c3d4e5f60/commands -H "Content-Type: application/json" -d '{"action": "turn_off", "params": {}}'

# датчики монолита: датчик «Thermostat (from device-service)»
# получает свежее значение через gateway каждые 4 секунды
curl http://localhost:8080/api/v1/sensors

# телеметрия, накопленная через RabbitMQ (через пару секунд после старта)
curl http://localhost:8003/api/v1/telemetry/latest
```

RabbitMQ UI: http://localhost:15672 (guest/guest) — видно очередь `telemetry.raw` и её трафик.
 