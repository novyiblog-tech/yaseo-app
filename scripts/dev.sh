#!/bin/bash
# Кабинет целиком на машине разработчика: тестовый сайт, исполнитель с виртуальными
# источниками и веб. Ключи не нужны, денег не тратит.
#   scripts/dev.sh        → http://127.0.0.1:8000, тестовый сайт http://127.0.0.1:8765/
set -u
cd "$(dirname "$0")/.."
export YASEO_APP_DSN="${YASEO_APP_DSN:-dbname=yaseo_app}"
export YASEO_ALLOW_PRIVATE=1 YASEO_INSECURE_COOKIES=1 YASEO_SOURCES=fake
dbname="${YASEO_APP_DSN##*dbname=}"; dbname="${dbname%% *}"
psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$dbname'" | grep -q 1 \
  || createdb "$dbname"

pids=()
trap 'kill "${pids[@]}" 2>/dev/null' EXIT INT TERM
uv run python -m http.server 8765 --bind 127.0.0.1 --directory tests/site >/dev/null 2>&1 & pids+=($!)
uv run python -m yaseo_app.worker --allow-private & pids+=($!)
uv run uvicorn yaseo_app.web:app --host 127.0.0.1 --port 8000 & pids+=($!)
wait
