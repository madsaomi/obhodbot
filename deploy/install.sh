#!/usr/bin/env bash
# Установка и обновление бота на чистом VPS (Ubuntu 22.04/24.04 или Debian 12).
#
# Первая установка:   sudo bash install.sh
# Обновление:         sudo bash /opt/obhodbot/deploy/install.sh
#
# Скрипт ставит Docker (если его нет), скачивает код из GitHub в /opt/obhodbot,
# один раз спрашивает токен бота и запускает бота с автоперезапуском.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/madsaomi/obhodbot.git}"
APP_DIR="${APP_DIR:-/opt/obhodbot}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root: sudo bash install.sh"
  exit 1
fi

echo "==> Установка git и curl"
apt-get update -y
apt-get install -y ca-certificates curl git

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Установка Docker (официальный скрипт get.docker.com)"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true

if [ -d "$APP_DIR/.git" ]; then
  echo "==> Обновление кода в $APP_DIR"
  git -C "$APP_DIR" pull --ff-only
else
  echo "==> Скачивание кода в $APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

if [ ! -f .env ]; then
  echo
  read -rsp "Вставьте токен бота (BOT_TOKEN от @BotFather) и нажмите Enter: " TOKEN
  echo
  if [ -z "$TOKEN" ]; then
    echo "Токен пустой — установка прервана."
    exit 1
  fi
  umask 077
  printf 'BOT_TOKEN=%s\n' "$TOKEN" > .env
  echo "Токен сохранён в $APP_DIR/.env (доступен только root)."
fi

mkdir -p data

echo "==> Сборка и запуск"
docker compose up -d --build

echo
echo "==> Последние строки лога:"
sleep 5
docker compose logs --tail=15 bot
echo
echo "Готово. Бот работает 24/7 и сам перезапускается после сбоя или перезагрузки сервера."
echo "Лог в реальном времени:  cd $APP_DIR && docker compose logs -f bot"
