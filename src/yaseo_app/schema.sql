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
    ('yandex-serp', 'Выдача Яндекса', true, 0.0305, 3500, 5000, NULL, interval '20 hours',
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

-- Кабинет (этап 3). Пароль — scrypt, соль внутри строки. Согласие на обработку
-- персональных данных (152-ФЗ) фиксируется временем.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS consent_at timestamptz;

-- В cookie уходит случайный токен, в базе лежит только его хэш: утечка таблицы
-- не даёт войти ни в одну сессию.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);

-- Попытки входа: перебор пароля режется по адресу почты.
CREATE TABLE IF NOT EXISTS login_attempts (
    id    bigserial PRIMARY KEY,
    email text NOT NULL,
    at    timestamptz NOT NULL DEFAULT now(),
    ok    boolean NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_email_at ON login_attempts (email, at);

-- Деньги (этап 4). Тарифы — данные: цифры правятся в админке без выкладки кода.
-- Пустой лимит — без ограничения; 0 — недоступно в тарифе.
CREATE TABLE IF NOT EXISTS plans (
    code              text PRIMARY KEY,
    title             text NOT NULL,
    price_rub         numeric(12, 2) NOT NULL,
    period            text NOT NULL CHECK (period IN ('free', 'once', 'month')),
    public            boolean NOT NULL DEFAULT true,
    sort              int NOT NULL DEFAULT 0,
    sites             int,
    audits_per_period int,
    queries_per_audit int,
    ai_checks         int,
    tracked_queries   int,
    free_every_days   int,
    steps_shown       int,
    pdf               boolean NOT NULL DEFAULT false,
    white_label       boolean NOT NULL DEFAULT false
);

-- Версия 2 из PLAN.md §4 — гипотеза, не решение владельцев.
INSERT INTO plans (code, title, price_rub, period, sort, sites, audits_per_period,
                   queries_per_audit, ai_checks, tracked_queries, free_every_days,
                   steps_shown, pdf, white_label) VALUES
    ('free',   'Проверка',      0,     'free',  0, 1,  1,  5,  0,   0,    30, 3,    false, false),
    ('once',   'Разовый аудит', 1990,  'once',  1, 1,  1,  30, 5,   0,    NULL, NULL, true,  false),
    ('start',  'Старт',         1990,  'month', 2, 2,  2,  30, 10,  300,  NULL, NULL, true,  false),
    ('pro',    'Про',           4990,  'month', 3, 5,  5,  30, 50,  1000, NULL, NULL, true,  true),
    ('agency', 'Агентство',     12900, 'month', 4, 20, 20, 30, 200, 3000, NULL, NULL, true,  true)
ON CONFLICT (code) DO NOTHING;

-- Тариф участников закрытой беты: 3 проверки в скользящие 30 дней, все шаги и PDF.
-- Сергей, 30.09.2026. В уже созданной базе строка правится UPDATE — вставка её не трогает. Не публичный — выдаётся только кодом приглашения.
INSERT INTO plans (code, title, price_rub, period, public, sort, sites, audits_per_period,
                   queries_per_audit, ai_checks, tracked_queries, free_every_days,
                   steps_shown, pdf, white_label) VALUES
    ('beta', 'Бета', 0, 'free', false, 0, 1, 3, 5, 0, 0, 30, NULL, true, false)
ON CONFLICT (code) DO NOTHING;

-- Сколько страниц обходит одна проверка. Сергей, 30.09.2026: бесплатно и бета — 30,
-- платные — 100 / 150 / 200. Заполняется только пустое: правки в базе не затираются.
ALTER TABLE plans ADD COLUMN IF NOT EXISTS max_pages int;
UPDATE plans SET max_pages = CASE code WHEN 'free' THEN 30 WHEN 'beta' THEN 30
    WHEN 'once' THEN 100 WHEN 'start' THEN 100 WHEN 'pro' THEN 150 WHEN 'agency' THEN 200 END
 WHERE max_pages IS NULL;

-- Одна строка на пользователя: что у него сейчас. Нет строки — тариф free.
-- Разовый аудит — период на 30 дней без продления.
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id              bigint PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    plan                 text NOT NULL REFERENCES plans(code),
    status               text NOT NULL CHECK (status IN ('active', 'past_due', 'canceled')),
    period_start         timestamptz NOT NULL,
    period_end           timestamptz NOT NULL,
    auto_renew           boolean NOT NULL DEFAULT true,
    payment_method       text,
    renew_attempts       int NOT NULL DEFAULT 0,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- Платёж создаётся у нас до перехода к платёжному сервису; статус меняется только
-- после проверки у самого сервиса, телу уведомления не верим.
CREATE TABLE IF NOT EXISTS payments (
    id               bigserial PRIMARY KEY,
    user_id          bigint NOT NULL REFERENCES users(id),
    plan             text NOT NULL REFERENCES plans(code),
    amount_rub       numeric(12, 2) NOT NULL,
    purpose          text NOT NULL CHECK (purpose IN ('purchase', 'renewal')),
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'succeeded', 'canceled')),
    provider         text NOT NULL,
    provider_id      text UNIQUE,
    idempotence_key  text NOT NULL UNIQUE,
    error            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    paid_at          timestamptz
);
CREATE INDEX IF NOT EXISTS payments_user ON payments (user_id, id DESC);

-- Итог проверки для истории и «Моих сайтов»: не разбирать JSON результата на каждый показ.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS score int;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lights jsonb;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS plan text;

-- Наблюдение (этап 5).
-- Повторная постановка той же задачи (ежедневные позиции сайта за день) — пустая операция.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dedupe_key text;
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe ON jobs (dedupe_key) WHERE dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS tracked_queries (
    id         bigserial PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    site_id    bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    query      text NOT NULL,
    region     int NOT NULL DEFAULT 225,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site_id, query, region)
);

-- Место сайта по запросу за день. NULL — вне глубины съёма. День — московский.
CREATE TABLE IF NOT EXISTS positions (
    site_id    bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    query      text NOT NULL,
    region     int NOT NULL,
    day        date NOT NULL,
    position   int,
    url        text,
    top3       jsonb,
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, query, region, day)
);

-- Позиции снимаются раз в сутки: кэш выдачи короче суток, иначе завтрашний съём
-- получит вчерашнюю выдачу. Меняем только заводские значения, ручные правки не трогаем.
UPDATE sources SET cache_ttl = interval '20 hours'
    WHERE name = 'yandex-serp' AND cache_ttl = interval '1 day';
UPDATE sources SET per_user_daily = 3500
    WHERE name = 'yandex-serp' AND per_user_daily = 300;
UPDATE sources SET service_daily_rub = 5000
    WHERE name = 'yandex-serp' AND service_daily_rub = 500;

-- Письма: всё, что уходит пользователю, сначала ложится сюда. Отправитель (SMTP или
-- сервис рассылок) забирает отсюда. Ключ не даёт отправить одно письмо дважды.
CREATE TABLE IF NOT EXISTS outbox (
    id         bigserial PRIMARY KEY,
    user_id    bigint REFERENCES users(id) ON DELETE CASCADE,
    to_email   text NOT NULL,
    subject    text NOT NULL,
    html       text NOT NULL,
    text       text NOT NULL,
    kind       text NOT NULL,
    dedupe_key text UNIQUE,
    status     text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'failed')),
    attempts   int NOT NULL DEFAULT 0,
    error      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at    timestamptz
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS weekly_digest boolean NOT NULL DEFAULT true;

-- Подтверждение почты и восстановление пароля. В письме — случайный токен, в базе — его хэш.
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_confirmed_at timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS signup_ip inet;
CREATE TABLE IF NOT EXISTS email_tokens (
    token_hash text PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose    text NOT NULL CHECK (purpose IN ('confirm', 'reset')),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);
CREATE INDEX IF NOT EXISTS email_tokens_user ON email_tokens (user_id, purpose, created_at);
CREATE INDEX IF NOT EXISTS users_signup_ip ON users (signup_ip, created_at);

-- Закрытая бета (этап 6). При YASEO_BETA=1 регистрация только по коду приглашения.
-- Код может дать тариф на срок — решение владельцев, по умолчанию не даёт.
CREATE TABLE IF NOT EXISTS invites (
    code       text PRIMARY KEY,
    note       text,
    max_uses   int NOT NULL DEFAULT 1,
    used       int NOT NULL DEFAULT 0,
    grant_plan text REFERENCES plans(code),
    grant_days int,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS invite_code text REFERENCES invites(code);

-- Лист ожидания: почта с согласием, откуда пришёл и какой сайт хотел проверить.
CREATE TABLE IF NOT EXISTS waitlist (
    id          bigserial PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    site        text,
    source      text,
    consent_at  timestamptz NOT NULL,
    invited     text REFERENCES invites(code),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Оферта: какую редакцию принял человек при регистрации и при оплате.
ALTER TABLE users ADD COLUMN IF NOT EXISTS offer_version text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS offer_accepted_at timestamptz;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS offer_version text;
