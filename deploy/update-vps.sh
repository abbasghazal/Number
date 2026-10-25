#!/usr/bin/env bash
set -euo pipefail

# Run as root after pulling the latest code into /opt/shahem-store-bot.
APP_DIR=/opt/shahem-store-bot
ENV_FILE=/etc/shahem-store-bot.env
if [ ! -f "$APP_DIR/main.py" ] || [ ! -f "$APP_DIR/.venv/bin/python" ]; then
  echo "Invalid installation directory: $APP_DIR" >&2
  exit 1
fi
# Secrets live outside the tree and are never copied or overwritten by updates.
if [ ! -f "$ENV_FILE" ]; then
  echo "Missing required environment file: $ENV_FILE" >&2
  exit 1
fi
systemctl stop shahem-store-bot
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R shahembot:shahembot "$APP_DIR"
systemctl daemon-reload
systemctl start shahem-store-bot
systemctl --no-pager --full status shahem-store-bot
