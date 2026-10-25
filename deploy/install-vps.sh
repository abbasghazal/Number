#!/usr/bin/env bash
set -euo pipefail

# Run as root from a Git clone of the repository on Ubuntu/Debian.
APP_DIR=/opt/shahem-store-bot
APP_USER=shahembot
ENV_FILE=/etc/shahem-store-bot.env

if [ ! -f "main.py" ] || [ ! -f "requirements.txt" ]; then
  echo "Run this script from the project root." >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip postgresql-client

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

if [ -e "$APP_DIR/main.py" ]; then
  echo "$APP_DIR already contains an installation. Use deploy/update-vps.sh for updates." >&2
  exit 1
fi

mkdir -p "$APP_DIR"
# Do not copy workstation secrets, local sessions, databases, or build caches.
# The Git metadata is retained so the in-bot /update command can fetch updates.
tar \
  --exclude='./.env' \
  --exclude='./bot_session.session' \
  --exclude='./bot_session.session-journal' \
  --exclude='./*.session' \
  --exclude='./*.session-journal' \
  --exclude='./database' \
  --exclude='./backups' \
  --exclude='./uploads' \
  --exclude='./tmp' \
  --exclude='./temp' \
  --exclude='./mst-main' \
  --exclude='./__pycache__' \
  --exclude='./.pytest_cache' \
  --exclude='./.venv' \
  --exclude='./venv' \
  --exclude='./*.pyc' \
  -cf - . | tar -C "$APP_DIR" -xf -

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

mkdir -p "$APP_DIR/database/backups"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

if [ ! -f "$ENV_FILE" ]; then
  install -m 600 -o root -g root /dev/null "$ENV_FILE"
  cat >&2 <<'EOF'
Add the required variables to /etc/shahem-store-bot.env, then run:
  systemctl enable --now shahem-store-bot
EOF
fi

install -m 644 "$APP_DIR/deploy/shahem-store-bot.service" /etc/systemd/system/shahem-store-bot.service
systemctl daemon-reload
echo "Installation complete. Add secrets to $ENV_FILE, then enable the service."
