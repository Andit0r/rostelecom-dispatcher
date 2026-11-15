-- v2: состояние сервиса для восстановления после рестарта + заготовки под перенос файлов (часть 2).

-- Входные данные целиком (справочники, матрица): sha256 исходного тела, zlib-сжатые байты.
CREATE TABLE IF NOT EXISTS inputs (
    sha256     text PRIMARY KEY,
    kind       text NOT NULL,             -- reference | matrix
    size_bytes bigint NOT NULL,
    payload    bytea NOT NULL,            -- zlib(тело PUT-запроса)
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Указатели на текущее состояние: key = reference | matrix.
-- reference: {"sha256", "gen"} (gen меняется при каждом PUT reference: дни прежнего поколения не восстанавливаются).
CREATE TABLE IF NOT EXISTS service_state (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Последний снимок дня: DayState целиком (наряды, отмены, overrides, события, план) — достаточно для восстановления.
CREATE TABLE IF NOT EXISTS day_state (
    date         date PRIMARY KEY,
    load_id      text NOT NULL,           -- поколение дня (новое при каждом PUT /days/{date})
    ref_gen      text,                    -- поколение справочников, на которых загружен день
    scenario     text,                    -- источник дня: api | demo:<сценарий>
    "now"        timestamptz,
    as_of        timestamptz,
    plan_version integer NOT NULL DEFAULT 0,
    payload      jsonb NOT NULL,
    demo         jsonb,                   -- состояние проигрывателя /ui/demo/* (без order_info)
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Отметки сотрудников (мобильное API). mark_key = order_id или '@staff:<staff_id>' (UNAVAILABLE).
CREATE TABLE IF NOT EXISTS marks (
    load_id     text NOT NULL,
    date        date NOT NULL,
    mark_key    text NOT NULL,
    kind        text NOT NULL,
    staff_id    text NOT NULL,
    order_id    text,
    "timestamp" timestamptz,
    recorded_at timestamptz,
    author      jsonb,
    event_sent  boolean NOT NULL DEFAULT false,
    event_id    text,
    payload     jsonb NOT NULL,
    PRIMARY KEY (load_id, mark_key, kind)
);
CREATE INDEX IF NOT EXISTS marks_date_idx ON marks (date, staff_id);

-- Журнал действий диспетчера (изменяющие запросы /ui/*).
CREATE TABLE IF NOT EXISTS actions (
    id      bigserial PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    actor   text,
    action  text NOT NULL,
    target  text,
    details jsonb
);
CREATE INDEX IF NOT EXISTS actions_ts_idx ON actions (ts);

-- Заготовки под часть 2 (данные переносятся из JSON-файлов позже).
CREATE TABLE IF NOT EXISTS accounts (
    login      text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS staff_profiles (
    staff_id   text PRIMARY KEY,
    value      jsonb NOT NULL,            -- {"profile": {...}, "equipment": {...} | null}
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS settings (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
