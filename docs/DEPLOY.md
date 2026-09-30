# Запуск на сервере

Боевой адрес — https://yaseo.site (домен на рег.ру, куплен 30.09.2026).

## Сервер

Рег.облако (reg.ru), «Облачные серверы»: 2 vCPU, 4 ГБ, 50–60 ГБ диска, Москва,
Ubuntu 24.04, оплата почасовая с облачного счёта Сергея. Там же живёт сервер
realty-platform — yaseo ставится на свой, отдельный: другой продукт, совладелец и ИП.
Сервер в России: почта и данные пользователей — персональные данные граждан РФ,
хранить их положено здесь (152-ФЗ, ст. 18 ч. 5).

Хостинг Reg.Host-0 сервис не держит: он для PHP-сайтов, а нужны Python, PostgreSQL,
постоянно работающие исполнитель и планировщик и Chrome для PDF. Хостинг остаётся для
DNS-зоны домена (правится в его панели, если у домена NS `ns*.hosting.reg.ru`) и,
возможно, для почты на домене.

На одной машине живут: Postgres, веб (uvicorn, 2 процесса), исполнитель очереди,
планировщик, Chrome (запускается на время рендера PDF), Caddy (HTTPS). 4 ГБ хватает с
запасом; упрётся — тариф поднимается в панели без переустановки.

## Порядок

1. **Сервер.** В Рег.облаке создать облачный сервер: 2 vCPU, 4 ГБ, Москва, Ubuntu 24.04,
   вход по SSH-ключу `yaseo-server` (`~/.ssh/id_ed25519_yaseo` на маке Сергея). Записать IP.
2. **DNS.** A-записи `@` и `www` для yaseo.site → IP сервера. Если у домена NS хостинга
   (`ns*.hosting.reg.ru`), зона правится в панели Reg.Host-0, иначе — в карточке домена. Сертификат
   выпустится сам, когда DNS разойдётся (обычно до часа).
3. **Доступ к коду.** Оба репозитория приватные у `novyiblog-tech`: этот (`yaseo-app`)
   и движок (`yaseo`, ставится по `uv.lock`). Александр выпускает fine-grained токен
   GitHub: доступ только к этим двум репозиториям, права Contents — Read-only. На сервере:
   ```
   adduser --system --group --home /home/yaseo --shell /bin/bash yaseo
   sudo -u yaseo git config --global credential.helper store
   sudo -u yaseo bash -c 'echo "https://x-access-token:ТОКЕН@github.com" > ~/.git-credentials; chmod 600 ~/.git-credentials'
   sudo -u yaseo git clone https://github.com/novyiblog-tech/yaseo-app.git /tmp/yaseo-app
   mv /tmp/yaseo-app /opt/yaseo-app
   ```
4. **Установка.** `bash /opt/yaseo-app/deploy/setup.sh` — ставит Postgres, Chrome, Caddy,
   uv и зависимости, создаёт базу, `/etc/yaseo/app.env` со случайным `YASEO_SECRET`,
   службы, ночной дамп базы и файрвол (открыты 22, 80, 443).
5. **Настройки.** `/etc/yaseo/app.env` (образец — `deploy/app.env.example`). На старте
   достаточно того, что есть: источники `fake`, оплата `fake`, письма в журнал.
   После правки — `systemctl restart yaseo-web yaseo-worker yaseo-scheduler`.
6. **Проверка.** `curl -I https://yaseo.site` → 200; лендинг и регистрация в браузере;
   `journalctl -u yaseo-worker -f` — исполнитель берёт проверку; PDF отчёта скачивается.

## Что включать дальше

- **Почта.** Ящик и SMTP на домене (например, Яндекс 360 для бизнеса или почта рег.ру),
  в DNS — SPF, DKIM, DMARC по инструкции почтового сервиса. Затем `YASEO_SMTP_*`.
- **Яндекс.** `YASEO_SOURCES=live`, `YC_FOLDER_ID`, `YANDEX_AI_STUDIO_API_KEY` — ключ
  Облака на то же ИП, что принимает деньги. Квота Wordstat — 100 запросов в час.
- **Реквизиты и оплата.** `YASEO_OPERATOR*`, `YASEO_VAT_NOTE` — плашка «Черновик» с
  оферты уйдёт; затем платёжный сервис вместо `YASEO_PAYMENTS=fake`.
- **Роскомнадзор.** Сервис собирает почту пользователей — оператору ПДн нужно
  уведомление в реестр РКН до начала обработки.

## Обновление

`bash /opt/yaseo-app/deploy/update.sh` — `git pull`, зависимости, перезапуск служб.
Схема базы накатывается при старте исполнителя.

## Резервные копии

`yaseo-backup.timer` в 04:30 кладёт `pg_dump` в `/var/backups/yaseo`, хранит 14 дней.
Это копия на том же диске — вдобавок включить резервное копирование сервера в панели
Рег.облака. Восстановление: `pg_restore -d yaseo_app --clean файл.dump` от пользователя postgres.

## Журналы

`journalctl -u yaseo-web -u yaseo-worker -u yaseo-scheduler -f`
