-- Схема ядра: очередь задач, журнал расхода, выключатели источников, кэш.
-- Идемпотентна: накатывается при каждом старте исполнителя.

CREATE TABLE IF NOT EXISTS users (
    id         bigserial PRIMARY KEY,
    email      text NOT NULL UNIQUE,
    plan       text NOT NULL DEFAULT 'free',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sites (
    id         bigserial PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users(id),
    url        text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, url)
);

-- Очередь. Исполнитель берёт задачу через FOR UPDATE SKIP LOCKED и держит аренду
-- (locked_until). Упавший исполнитель аренду не продлит — задачу подберёт другой.
CREATE TABLE IF NOT EXISTS jobs (
    id           bigserial PRIMARY KEY,
    user_id      bigint NOT NULL REFERENCES users(id),
    site_id      bigint REFERENCES sites(id),
    kind         text NOT NULL,
    params       jsonb NOT NULL DEFAULT '{}',
    status       text NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts     int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    run_after    timestamptz NOT NULL DEFAULT now(),
    locked_by    text,
    locked_until timestamptz,
    result       jsonb,
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    finished_at  timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (run_after, id) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS jobs_lease ON jobs (locked_until) WHERE status = 'running';

-- Источники данных — будущая админка. Цена за одно обращение правится руками при смене
-- тарифа поставщика. Пустой лимит — лимита нет. plans пустой — доступно во всех тарифах.
CREATE TABLE IF NOT EXISTS sources (
    name              text PRIMARY KEY,
    title             text NOT NULL,
    enabled           boolean NOT NULL DEFAULT false,
    plans             text[],
    price_rub         numeric(12, 4) NOT NULL,
    per_user_daily    int,
    service_daily_rub numeric(12, 2),
    rate_per_hour     int,
    cache_ttl         interval NOT NULL DEFAULT interval '1 day',
    note              text
);

INSERT INTO sources (name, title, enabled, price_rub, per_user_daily, service_daily_rub,
                     rate_per_hour, cache_ttl, note) VALUES
    ('yandex-serp', 'Выдача Яндекса', true, 0.0305, 300, 500, NULL, interval '1 day',
     'отложенный запрос, одна страница выдачи = одно обращение; прайс 16.09.2026'),
    ('wordstat', 'Wordstat', true, 0.020, 200, 100, 100, interval '30 days',
     'квота 100 запросов в час на весь сервис; прайс 16.09.2026'),
    ('yandex-gen', 'Генеративный ответ Яндекса', true, 5.08, 20, 300, NULL, interval '7 days',
     'YandexGPT поверх Поиска; прайс 29.09.2026')
ON CONFLICT (name) DO NOTHING;

-- Журнал расхода: строка на каждое обращение к платному источнику, в том числе
-- ответ из кэша (cost 0) — так видно, сколько сэкономил кэш. Строка пишется ДО вызова
-- (резерв), поэтому два исполнителя не проскочат потолок одновременно. Неудачный вызов
-- остаётся в журнале с ценой: поставщик мог его посчитать, лучше переоценить расход.
CREATE TABLE IF NOT EXISTS spend (
    id        bigserial PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    user_id   bigint REFERENCES users(id),
    job_id    bigint REFERENCES jobs(id),
    source    text NOT NULL REFERENCES sources(name),
    units     int NOT NULL,
    price_rub numeric(12, 4) NOT NULL,
    cost_rub  numeric(12, 4) NOT NULL,
    cached    boolean NOT NULL DEFAULT false,
    fake      boolean NOT NULL DEFAULT false,
    ok        boolean,
    detail    text
);
CREATE INDEX IF NOT EXISTS spend_source_at ON spend (source, at);
CREATE INDEX IF NOT EXISTS spend_user_at ON spend (user_id, source, at);

CREATE TABLE IF NOT EXISTS cache (
    source     text NOT NULL,
    key        text NOT NULL,
    fake       boolean NOT NULL DEFAULT false,
    payload    jsonb NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (source, key, fake)
);
