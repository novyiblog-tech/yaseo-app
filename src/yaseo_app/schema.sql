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
    ('yandex-gen', 'Генеративный ответ Яндекса', true, 5.08, 50, 1500, NULL, interval '7 days',
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

-- Тарифы от 30.09.2026 — таблица «yaseo — тарифы и настройки», решение Сергея.
-- Накатываются один раз (отметка в applied): дальше цифры правятся в базе, и выкладка
-- их не затирает. «Бета» закрыта: строка остаётся для тех, у кого она уже есть.
CREATE TABLE IF NOT EXISTS applied (
    name text PRIMARY KEY,
    at   timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE plans ADD COLUMN IF NOT EXISTS weekly_digest boolean NOT NULL DEFAULT false;
INSERT INTO plans (code, title, price_rub, period, public, sort) VALUES
    ('promo', 'По промокоду', 0, 'free', false, 0)
ON CONFLICT (code) DO NOTHING;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM applied WHERE name = 'plans-2026-09-30') THEN
        UPDATE plans p SET title = v.title, price_rub = v.price, period = v.period,
               public = v.public, sort = v.sort, sites = v.sites,
               audits_per_period = v.audits, queries_per_audit = v.queries,
               ai_checks = v.ai, tracked_queries = v.tracked, free_every_days = v.every,
               steps_shown = v.steps, pdf = v.pdf, white_label = v.wl,
               max_pages = v.pages, weekly_digest = v.digest
          FROM (VALUES
            ('free',   'Проверка',      0,     'free',  true,  0, 1,  1,  0,  0,   0,    30,   3,    false, false, 10,  false),
            ('promo',  'По промокоду',  0,     'free',  false, 0, 1,  1,  5,  3,   0,    30,   NULL, false, false, 30,  false),
            ('once',   'Разовый аудит', 1990,  'once',  true,  1, 1,  1,  10, 5,   0,    NULL, NULL, true,  false, 50,  false),
            ('start',  'Старт',         2990,  'month', true,  2, 2,  2,  20, 10,  500,  NULL, NULL, true,  false, 50,  true),
            ('pro',    'Про',           4990,  'month', true,  3, 5,  5,  30, 50,  1500, NULL, NULL, true,  true,  100, true),
            ('agency', 'Ультра',        12900, 'month', true,  4, 15, 20, 50, 150, 3000, NULL, NULL, true,  true,  150, true)
          ) AS v(code, title, price, period, public, sort, sites, audits, queries, ai, tracked,
                 every, steps, pdf, wl, pages, digest)
         WHERE p.code = v.code;
        UPDATE plans SET public = false WHERE code = 'beta';
        INSERT INTO applied (name) VALUES ('plans-2026-09-30');
    END IF;
END $$;

-- Потолки нейросети под новые тарифы: проверка на «Ультра» — до 50 ответов за раз,
-- на весь сервис — 1 500 ₽ в сутки (Сергей, 30.09.2026). Ручные правки не трогаем.
UPDATE sources SET per_user_daily = 50 WHERE name = 'yandex-gen' AND per_user_daily = 20;
UPDATE sources SET service_daily_rub = 1500
    WHERE name = 'yandex-gen' AND service_daily_rub = 300;

-- Сроки и продление (Сергей, 30.09.2026). Тариф оплачивается на 1 или 3 месяца
-- (месяц — 30 дней), лимиты действуют на каждые 30 дней срока. Само срок не продлевается:
-- письмо за 3 дня и в день окончания. Автопродление — только по отдельному согласию.
CREATE TABLE IF NOT EXISTS plan_terms (
    plan      text NOT NULL REFERENCES plans(code),
    months    int NOT NULL CHECK (months > 1),
    price_rub numeric(12, 2) NOT NULL,
    PRIMARY KEY (plan, months)
);
INSERT INTO plan_terms (plan, months, price_rub) VALUES
    ('start', 3, 7990), ('pro', 3, 13490), ('agency', 3, 34490)
ON CONFLICT (plan, months) DO NOTHING;
-- Разовый аудит на 3 месяца: по полному аудиту на каждые 30 дней (Сергей, 01.10.2026).
INSERT INTO plan_terms (plan, months, price_rub) VALUES ('once', 3, 4990)
ON CONFLICT (plan, months) DO NOTHING;

ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS months int NOT NULL DEFAULT 1;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS auto_renew_consent_at timestamptz;
ALTER TABLE subscriptions ALTER COLUMN auto_renew SET DEFAULT false;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS months int NOT NULL DEFAULT 1;
-- Зачёт неиспользованных дней прошлого тарифа при переходе на тариф выше.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS credit_rub numeric(12, 2) NOT NULL DEFAULT 0;
-- Человек отметил при оплате «продлевать автоматически».
ALTER TABLE payments ADD COLUMN IF NOT EXISTS auto_renew boolean NOT NULL DEFAULT false;
ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_purpose_check;
ALTER TABLE payments ADD CONSTRAINT payments_purpose_check
    CHECK (purpose IN ('purchase', 'renewal', 'upgrade'));
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM applied WHERE name = 'autorenew-off-2026-09-30') THEN
        -- Продление было включено без отдельного согласия — выключаем.
        UPDATE subscriptions SET auto_renew = false WHERE auto_renew_consent_at IS NULL;
        INSERT INTO applied (name) VALUES ('autorenew-off-2026-09-30');
    END IF;
END $$;

-- Настройки кабинета (таблица «yaseo — тарифы и настройки», столбец «с тарифа»,
-- Сергей, 30.09.2026). Доступна, если тариф кабинета не ниже from_plan по plans.sort.
CREATE TABLE IF NOT EXISTS features (
    code      text PRIMARY KEY,
    title     text NOT NULL,
    from_plan text NOT NULL REFERENCES plans(code)
);
INSERT INTO features (code, title, from_plan) VALUES
    ('region',    'Город для места в Яндексе',     'start'),
    ('schedule',  'Проверять сайт автоматически',  'start'),
    ('alerts',    'Срочные письма',                'start'),
    ('exclude',   'Не проверять разделы сайта',    'start'),
    ('gentle',    'Бережный обход',                'start'),
    ('share',     'Ссылка на отчёт без входа',     'start'),
    ('webmaster', 'Яндекс Вебмастер и Метрика',    'start'),
    ('sections',  'Какие разделы проверять',       'pro'),
    ('rivals',    'Конкуренты',                    'pro'),
    ('brand',     'Логотип и контакты в отчёте',   'pro'),
    ('team',      'Доступ коллегам',               'agency')
ON CONFLICT (code) DO NOTHING;

-- Настройки сайта.
ALTER TABLE sites ADD COLUMN IF NOT EXISTS region int NOT NULL DEFAULT 225;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS schedule text NOT NULL DEFAULT 'off'
    CHECK (schedule IN ('off', 'week', 'month'));
ALTER TABLE sites ADD COLUMN IF NOT EXISTS schedule_queries jsonb NOT NULL DEFAULT '[]';
-- День последней попытки автопроверки: кончились проверки — следующая попытка завтра.
ALTER TABLE sites ADD COLUMN IF NOT EXISTS schedule_tried date;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS exclude jsonb NOT NULL DEFAULT '[]';
ALTER TABLE sites ADD COLUMN IF NOT EXISTS gentle boolean NOT NULL DEFAULT false;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS sections jsonb NOT NULL
    DEFAULT '{"demand": true, "positions": true, "answers": true}';
ALTER TABLE sites ADD COLUMN IF NOT EXISTS rivals jsonb NOT NULL DEFAULT '[]';
ALTER TABLE positions ADD COLUMN IF NOT EXISTS rivals jsonb;

-- Настройки кабинета.
ALTER TABLE users ADD COLUMN IF NOT EXISTS alerts boolean NOT NULL DEFAULT true;
ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_name text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_contacts text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_logo bytea;
ALTER TABLE users ADD COLUMN IF NOT EXISTS brand_logo_type text;
-- Коллега работает в кабинете владельца: свой вход, общие сайты, проверки и тариф.
ALTER TABLE users ADD COLUMN IF NOT EXISTS owner_id bigint REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE email_tokens DROP CONSTRAINT IF EXISTS email_tokens_purpose_check;
ALTER TABLE email_tokens ADD CONSTRAINT email_tokens_purpose_check
    CHECK (purpose IN ('confirm', 'reset', 'team'));

-- Ссылка на отчёт без входа: случайный токен, выключается владельцем.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS share_token text UNIQUE;

-- Срочные письма: здоровье сайта по последней проверке планировщика.
CREATE TABLE IF NOT EXISTS site_health (
    site_id     bigint PRIMARY KEY REFERENCES sites(id) ON DELETE CASCADE,
    checked_at  timestamptz NOT NULL DEFAULT now(),
    ok          boolean NOT NULL,
    fails       int NOT NULL DEFAULT 0,
    error       text,
    tls_until   date,
    down_since  timestamptz
);

-- Вебмастер и Метрика: токен Яндекс ID владельца. Данные Вебмастера и Метрики не храним —
-- только показываем владельцу (условия Яндекса), хранится лишь доступ.
CREATE TABLE IF NOT EXISTS yandex_links (
    user_id       bigint PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    access_token  text NOT NULL,
    refresh_token text,
    expires_at    timestamptz,
    login         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
