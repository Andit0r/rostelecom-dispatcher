# API для мобильного приложения сотрудника

Расширение решения (не часть спецификации `/api/v1`): префикс `/ui/employee`. Интерактивная схема — `http://<host>:8080/docs` (раздел `ui-employee`).
Спецификация API (`/api/v1`) не менялась; отметки сотрудника превращаются в его события внутри сервера.

- Базовый URL: `http://<host>:8080` (локально `http://localhost:8080`).
- Формат: JSON, UTF-8. Ошибки: `{"error": {"code": "...", "message": "...", "details": [...]}}`.
- CORS: разрешены origin из env `CORS_ORIGINS` (через запятую); по умолчанию dev-адреса фронта (`:5173`). `*` — только если задан явно (тогда credentials/cookie не включаются). Разрешены заголовки `Authorization`, `Content-Type`, `Accept`, `X-API-Key` и credentials.
- Дата дня `date=YYYY-MM-DD` необязательна: по умолчанию активный день (как в интерфейсе: изменившийся последним), иначе последний день с построенным планом. Работает и для плана, загруженного через `PUT /api/v1/days/{date}` + `POST .../plan`.

## Авторизация

Все `/ui/*` (в том числе `/ui/employee/*`) требуют входа, если не задано `AUTH_ENABLED=off`. `/api/v1`, `/docs`, `/openapi.json` и `/api/v1/health` открыты всегда.

Роли: `EMPLOYEE` (видит и отмечает только свой маршрут `/ui/employee/{staff_id}/...`, где `staff_id` берётся из токена) и `DISPATCHER` (любой маршрут, исправление отметок, остальные `/ui/*`).

Вход: логин сотрудника — его табельный номер (`personnel_number` из справочника, иначе `staff_id`); пароль выдаёт диспетчер (`POST /ui/directory/accounts/{staff_id}/reset-password`).

```
curl -X POST http://localhost:8080/ui/auth/login -H "Content-Type: application/json" \
  -d '{"login":"660022","password":"<пароль>"}'
```
```json
{"token":"eyJ...","role":"EMPLOYEE","staff_id":"ST-001","display_name":"Иванов И. И.","must_change_password":false,"login":"660022","expires_in":43200}
```

Токен подписан HMAC и живёт `AUTH_TOKEN_TTL_HOURS` (12 ч по умолчанию). Мобильное приложение хранит `token` и шлёт `Authorization: Bearer <token>` в каждом запросе; веб использует httpOnly-cookie `rt_session` (SameSite=Lax), которую сервер выставляет при входе (для cookie из другого origin нужны `CORS_ORIGINS` и `fetch(..., {credentials: "include"})`). Заголовок Bearer приоритетнее cookie.

```
curl http://localhost:8080/ui/employee/ST-001/route -H "Authorization: Bearer $TOKEN"
curl http://localhost:8080/ui/auth/me -H "Authorization: Bearer $TOKEN"        # login, role, staff_id, display_name, must_change_password
curl -X POST http://localhost:8080/ui/auth/change-password -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"old_password":"...","new_password":"не короче 8 символов"}'   # вернёт новый токен
curl -X POST http://localhost:8080/ui/auth/logout -H "Authorization: Bearer $TOKEN"
```

Права проверяются на каждом запросе: отключённый аккаунт, смена/сброс пароля и выход сразу делают прежний токен недействительным. Если `must_change_password=true` (после сброса пароля диспетчером), клиент должен предложить сменить пароль; сервер доступ при этом не ограничивает.

| HTTP | `code` | Когда |
|---|---|---|
| 401 | `UNAUTHORIZED` | нет токена, он истёк/отозван, аккаунт отключён; при входе — неверный логин **или** пароль (ответ одинаков) |
| 403 | `FORBIDDEN` | сотрудник запросил чужой маршрут или раздел диспетчера |
| 428 | `CONFIRMATION_REQUIRED` | `TRANSFER`/`UNAVAILABLE` без `confirmed=true` |
| 429 | `TOO_MANY_ATTEMPTS` | 5 неудачных входов подряд по паре логин+IP: пауза 60 с, заголовок `Retry-After` |

Тестовые учётные записи (`DEMO_ACCOUNTS=on`, имена переменных и путей технические): пароль всех аккаунтов — `DEMO_PASSWORD` (по умолчанию `demo`), диспетчер `demo-dispatcher`; `GET /ui/auth/demo-hint` (без токена) возвращает `{enabled, dispatcher_login, employee_sample_logins}`. Мобильному клиенту, у которого нет прав на `/ui/demo/*` (загрузка дня из каталога данных, раздел диспетчера), состояние дня показывает `GET /ui/employee/status` (`loaded`, `date`, `plan_version`); день загружает диспетчер.

## Время плана

Система живёт во времени плана (`as_of`) — времени, на которое построен план; оно продвигается событиями и отметками сотрудников, а не по часам телефона. Время отметки передавайте в `timestamp` (ISO 8601 с поясом, например `2026-11-16T10:12:00+05:00`); без него берётся текущее время плана. Время событий не может быть раньше последнего принятого события (иначе 422 `EVENT_OUT_OF_ORDER`).

## 1. Список сотрудников

```
curl "http://localhost:8080/ui/employee/staff"
```
```json
{"date":"2026-11-16","plan_version":1,"as_of":"2026-11-16T08:30:00+05:00",
 "staff":[{"staff_id":"ST-002","display_name":"Иванов И. И.","profiles":["INSTALL"],
   "service_center_id":"SC-1","service_center_name":"СЦ Центр","shift":{"start":"09:00","end":"18:00"},
   "route_status":"ACTIVE","visits":8,"visits_done":0}]}
```

## 2. Маршрут сотрудника

```
curl "http://localhost:8080/ui/employee/ST-002/route"
```
Основные поля ответа:

- `plan_version`, `as_of`, `on_shift`, `route_status` (`ACTIVE|UNAVAILABLE`), `progress {visits_total, visits_done}`.
- `allowed_actions` (уровень маршрута): `["UNAVAILABLE"]`, пока сотрудник на смене и ещё не заявлял недоступность.
- `next` — ближайший невыполненный визит: `order_id`, `address`, `lat`, `lon`, `customer_phone` (во внешних данных телефона нет, всегда `null`), `slot`, плановые `arrival/start/end`, `call_window {start,end}` (звонить за 60–30 минут до слота), `next_mark`, `allowed_actions`.
- `stops[]` — все остановки: `type` (`VISIT|BREAK|RESTOCK|DEPOT_START|DEPOT_END`), `location {address,lat,lon}`, `order`, `arrival/start/end`, `state` (`PLANNED|IN_PROGRESS|DONE`; отметки сотрудника важнее плана), `marks {CALLED,ARRIVED,DONE,TRANSFER}` (время или `null`), `allowed_actions` визита.
- `marks[]` — журнал отметок с `event` (какое событие было отправлено).

Перечитывайте маршрут после каждой отметки, где `route_changed=true`, и периодически (раз в 5–10 с): диспетчер и события других сотрудников тоже меняют план. План целиком — напрямую `GET /api/v1/days/{date}/plan` (по спецификации); отдельные вызовы `/api/v1` для действий сотрудника не нужны.

## 3. Отметка

`POST /ui/employee/{staff_id}/marks[?date=YYYY-MM-DD]`

| `kind` | Нужно в теле | Событие спецификации |
|---|---|---|
| `CALLED` | `order_id` | нет, только запись |
| `ARRIVED` | `order_id`, `timestamp` | `VISIT_DELAYED`, если `timestamp` позже планового начала визита на 5 минут и больше (`expected_end` = плановый конец + опоздание); иначе только отметка |
| `DONE` (синоним `COMPLETED`) | `order_id`, `timestamp` | `VISIT_DELAYED` с `expected_end = timestamp`, если позже планового конца; раньше или вовремя: только отметка |
| `TRANSFER` | `order_id`, `reason` = `STATION_EQUIPMENT` \| `TRUNK_CABLE`, `timestamp` | `ORDER_TRANSFERRED` (`actual_end = timestamp`, `transferred_to = reason`) |
| `UNAVAILABLE` | `reason` = `VEHICLE_BREAKDOWN` \| `ILLNESS` \| `OTHER`, `timestamp`, `until` (необязательно) | `STAFF_UNAVAILABLE` |

Ограничения спецификации:

- `VISIT_DELAYED` и `ORDER_TRANSFERRED` допустимы только для уже начатого визита (`timestamp` позже планового начала) этого сотрудника. Досрочное завершение событием не выражается: освобождение времени есть только у `ORDER_TRANSFERRED`, поэтому ранний `DONE` лишь записывается.
- Порядок отметок: `CALLED` → `ARRIVED` → `DONE`; `TRANSFER` возможен после `ARRIVED` вместо `DONE`.
- Подтверждение опасных действий: для `TRANSFER` и `UNAVAILABLE` при включённой авторизации нужно поле `"confirmed": true`, иначе 428 `CONFIRMATION_REQUIRED` (клиент показывает диалог «Вы уверены?» и повторяет запрос с `confirmed=true`). Для остальных видов поле не требуется.
- `timestamp` (и `at`) не может отличаться от времени плана больше чем на 6 часов: иначе 422 `TIMESTAMP_OUT_OF_RANGE` (защита от неверных часов телефона).
- Журнал: в каждой отметке (`marks[]` маршрута) сохраняются `author {login, role, staff_id}` и время сервера `recorded_at`.
- Исправление отметки диспетчером: `POST /ui/employee/{staff_id}/marks/revert` с телом `{"order_id": "...", "kind": "CALLED"}` (только `DISPATCHER`). Откатываются только отметки, по которым не отправлялось событие в план и после которых нет более поздних; иначе 409 `EVENT_ALREADY_APPLIED` («событие уже применено») или `MARK_HAS_DEPENDENT`; нет отметки: 404 `MARK_NOT_FOUND`.
- Событие отправляется тем же путём, что `POST /api/v1/days/{date}/events`, `event_id` детерминирован: `EV-MOB-<yyyymmdd>-<order_id>-<kind>` (для недоступности `EV-MOB-<yyyymmdd>-UNAV-<staff_id>`).

Пример: прибытие с опозданием.
```
curl -X POST "http://localhost:8080/ui/employee/ST-002/marks" -H "Content-Type: application/json" \
  -d '{"order_id":"ORD-20261116-0148","kind":"ARRIVED","timestamp":"2026-11-16T09:30:49+05:00"}'
```
```json
{"accepted":true,"duplicate":false,"kind":"ARRIVED","order_id":"ORD-20261116-0148","staff_id":"ST-002",
 "at":"2026-11-16T09:30:49+05:00","recorded_at":"2026-11-14T21:39:42+00:00",
 "event_sent":true,
 "event":{"event_id":"EV-MOB-20261116-ORD-20261116-0148-ARRIVED","type":"VISIT_DELAYED","accepted":true,"duplicate":false,"message":"ORD-20261116-0148 у ST-002 задерживается до 10:30"},
 "event_note":null,"plan_version":2,"route_changed":true,"added_order_ids":[],"removed_order_ids":[],
 "changed_staff_ids":["ST-002"],
 "marks":{"CALLED":"2026-11-16T08:30:00+05:00","ARRIVED":"2026-11-16T09:30:49+05:00","DONE":null,"TRANSFER":null},
 "next_mark":"DONE","allowed_actions":["DONE","TRANSFER"]}
```

Пример: передача наряда.
```
curl -X POST "http://localhost:8080/ui/employee/ST-002/marks" -H "Content-Type: application/json" \
  -d '{"order_id":"ORD-20261116-0148","kind":"TRANSFER","reason":"TRUNK_CABLE","timestamp":"2026-11-16T09:38:49+05:00"}'
```
Ответ той же формы: `event.type = ORDER_TRANSFERRED`, `plan_version` +1, `allowed_actions = []`.

Пример: недоступен до 14:00.
```
curl -X POST "http://localhost:8080/ui/employee/ST-002/marks" -H "Content-Type: application/json" \
  -d '{"kind":"UNAVAILABLE","reason":"ILLNESS","timestamp":"2026-11-16T12:00:00+05:00","until":"2026-11-16T14:00:00+05:00"}'
```
`removed_order_ids` покажет наряды, снятые с маршрута сотрудника; после `until` он продолжает смену.

Поля ответа: `accepted` (отметка принята), `duplicate`, `event_sent` и `event` (что отправлено; `event_note` объясняет, почему события нет), `plan_version` (новая версия), `route_changed` (маршрут сотрудника изменился: перечитайте `/route`), `added_order_ids` / `removed_order_ids`, `changed_staff_ids` (чьи маршруты пересчитаны, включая других сотрудников), `marks`, `allowed_actions`.

## Идемпотентность

Повтор отметки того же вида на тот же наряд (для `UNAVAILABLE`: на того же сотрудника) возвращает 200 с `duplicate: true`, `event_sent: false`, прежним `event` и не создаёт события и версии плана. Безопасно повторять запрос при обрыве связи.

## Коды ошибок

Если событие отклонено планом, возвращается 422 с кодом события и отметка не записывается.

| HTTP | `code` | Когда |
|---|---|---|
| 400 | `BAD_REQUEST` | неверный JSON/схема, дата не `YYYY-MM-DD` |
| 401, 403, 428, 429 | `UNAUTHORIZED`, `FORBIDDEN`, `CONFIRMATION_REQUIRED`, `TOO_MANY_ATTEMPTS` | см. раздел «Авторизация» |
| 404 | `STAFF_NOT_FOUND`, `DAY_NOT_FOUND` | неизвестный сотрудник или день |
| 409 | `REFERENCE_NOT_LOADED`, `PLAN_NOT_BUILT` | справочники или план ещё не загружены |
| 422 | `ORDER_ID_REQUIRED`, `REASON_REQUIRED` | не хватает поля |
| 422 | `TIMESTAMP_OUT_OF_RANGE` | `timestamp` дальше 6 часов от времени плана |
| 422 | `ORDER_NOT_IN_ROUTE` | наряда нет в маршруте сотрудника |
| 422 | `MARK_OUT_OF_ORDER` | нарушен порядок отметок, время раньше предыдущей отметки |
| 422 | `VISIT_NOT_STARTED` | `TRANSFER` до планового начала визита |
| 422 | `EVENT_OUT_OF_ORDER`, `EVENT_CONFLICTS_PLAN`, `INVALID_VISIT_END`, `INVALID_PERIOD`, `STAFF_NOT_ON_SHIFT` | событие по спецификации отклонено (время раньше последнего события, следующая остановка уже начата, `until` не позже `timestamp`, сотрудник не на смене и т. д.) |