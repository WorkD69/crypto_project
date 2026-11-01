#!/usr/bin/env bash
# Разовая настройка брокера Hermes на сервере. Повторный запуск безопасен.
# Запускать из Git Bash после deploy.sh: bash deploy/setup-broker.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/deploy/server.env"
SSH=(ssh -i "$SSH_KEY" -o UserKnownHostsFile="$HOME/.ssh/known_hosts" -o BatchMode=yes "$SERVER")

"${SSH[@]}" 'cat > /etc/systemd/system/crypto-hermes-broker.service' < "$ROOT/deploy/crypto-hermes-broker.service"
"${SSH[@]}" 'set -e
id crypto-hermes >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin \
    --gid cryptosignals --groups docker crypto-hermes
# Группа может войти в каталог, но видит только спул: база и сессия остаются с правами 600.
chmod 710 /var/lib/crypto-signals
install -d -o cryptosignals -g cryptosignals -m 2750 /var/lib/crypto-signals/spool
systemctl daemon-reload
systemctl enable --now crypto-hermes-broker
systemctl restart crypto-hermes-broker
sleep 3
systemctl is-active crypto-hermes-broker
ls -l /run/crypto-hermes/
journalctl -u crypto-hermes-broker -n 3 --no-pager -o cat'
