#!/bin/bash
# Первичная установка на чистую Ubuntu 24.04. Запускать от root один раз:
#   bash /opt/yaseo-app/deploy/setup.sh
# Код уже должен лежать в /opt/yaseo-app (см. docs/DEPLOY.md, шаг 3).
set -euo pipefail
APP=/opt/yaseo-app

apt-get update
apt-get install -y postgresql git curl ufw debian-keyring debian-archive-keyring \
  apt-transport-https fonts-noto-core fonts-noto-color-emoji

# Chrome для PDF отчётов.
curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
# Caddy — прокси с автоматическим HTTPS.
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y google-chrome-stable caddy

id yaseo >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash yaseo
chown -R yaseo:yaseo "$APP"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='yaseo'" | grep -q 1 \
  || sudo -u postgres createuser yaseo
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='yaseo_app'" | grep -q 1 \
  || sudo -u postgres createdb -O yaseo yaseo_app

sudo -u yaseo bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u yaseo bash -lc "cd $APP && ~/.local/bin/uv sync --frozen --no-dev"

mkdir -p /etc/yaseo /var/backups/yaseo
chown postgres:postgres /var/backups/yaseo
if [ ! -f /etc/yaseo/app.env ]; then
  sed "s/^YASEO_SECRET=$/YASEO_SECRET=$(openssl rand -hex 32)/" "$APP/deploy/app.env.example" > /etc/yaseo/app.env
fi
chown root:yaseo /etc/yaseo/app.env && chmod 0640 /etc/yaseo/app.env

cp "$APP"/deploy/yaseo-*.service "$APP"/deploy/yaseo-backup.timer /etc/systemd/system/
cp "$APP/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now yaseo-web yaseo-worker yaseo-scheduler yaseo-backup.timer
systemctl reload caddy

ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
echo "Готово. Проверка: systemctl status yaseo-web; curl -I https://yaseo.site"
