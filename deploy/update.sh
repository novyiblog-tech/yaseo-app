#!/bin/bash
# Выкладка новой версии. Запускать от root: bash /opt/yaseo-app/deploy/update.sh
# Схема базы накатывается сама при старте исполнителя.
set -euo pipefail
cd /opt/yaseo-app
sudo -u yaseo git pull --ff-only
sudo -u yaseo bash -lc "cd /opt/yaseo-app && ~/.local/bin/uv sync --frozen --no-dev"
systemctl restart yaseo-worker yaseo-scheduler yaseo-web
systemctl --no-pager --lines=0 status yaseo-web yaseo-worker yaseo-scheduler
