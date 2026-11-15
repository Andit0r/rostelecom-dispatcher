-- v1: журнал истории (таблицы, которые раньше создавал journal.py без версионирования).
-- Идемпотентно: на базе, где таблицы уже есть, ничего не теряется.
CREATE TABLE IF NOT EXISTS days (
    date         date PRIMARY KEY,
    "now"        timestamptz,
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    orders_total integer
);
CREATE TABLE IF NOT EXISTS plan_versions (
    date               date    NOT NULL,
    version            integer NOT NULL,
    as_of              timestamptz,
    based_on_event_id  text,
    computation_ms     integer,
    visits_planned     integer,
    unassigned         integer,
    pgp_orders         integer,
    cancelled          integer,
    late_risk_active   integer,
    travel_sec_total   integer,
    utilization_cv     double precision,
    plan               jsonb NOT NULL,
    PRIMARY KEY (date, version)
);
-- разбивка загрузки по профилям (вне контракта); колонка добавлена позже
ALTER TABLE plan_versions ADD COLUMN IF NOT EXISTS utilization_profiles jsonb;
CREATE TABLE IF NOT EXISTS events (
    date          date NOT NULL,
    event_id      text NOT NULL,
    ts            timestamptz,
    type          text,
    payload       jsonb,
    accepted      boolean,
    duplicate     boolean,
    processing_ms integer,
    plan_version  integer,
    PRIMARY KEY (date, event_id)
);
CREATE TABLE IF NOT EXISTS alerts (
    date       date NOT NULL,
    alert_id   text NOT NULL,
    created_at timestamptz,
    type       text,
    status     text,
    order_id   text,
    payload    jsonb,
    PRIMARY KEY (date, alert_id)
);
