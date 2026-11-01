#!/usr/bin/env bash
# Загружает signal_bot на сервер, перезапускает службу и проверяет её состояние.
# Запускать из Git Bash: bash deploy/deploy.sh
# Предыдущая версия остаётся в /opt/crypto-signals/signal_bot.prev для отката.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/deploy/server.env"

# В Git Bash ssh ищет known_hosts по битому пути с кириллицей, поэтому путь указан явно.
SSH=(ssh -i "$SSH_KEY" -o UserKnownHostsFile="$HOME/.ssh/known_hosts" -o BatchMode=yes "$SERVER")

cd "$ROOT"
tar --exclude='__pycache__' -czf - signal_bot | "${SSH[@]}" 'set -e
cd /opt/crypto-signals
rm -rf signal_bot.new
mkdir signal_bot.new
tar -xzf - --no-same-owner --strip-components=1 -C signal_bot.new
chown -R root:root signal_bot.new
rm -rf signal_bot.prev
mv signal_bot signal_bot.prev
mv signal_bot.new signal_bot
systemctl restart crypto-signals
sleep 10
systemctl is-active crypto-signals
systemctl show crypto-signals -p NRestarts -p MemoryCurrent
journalctl -u crypto-signals -n 5 --no-pager -o cat'
