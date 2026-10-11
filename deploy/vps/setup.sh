#!/usr/bin/env bash
# One-time (and safe to re-run) setup of the Voice Tutor API on Ubuntu 24.04 (Oracle Cloud VM.Standard.E2.1.Micro).
# Run from the cloned repository:   sudo bash /opt/voice-tutor/app/deploy/vps/setup.sh [api-domain]
#
# Does: swap, Python 3.13 (uv), the server-only dependencies, a locked-down service user and systemd service,
# Caddy (HTTPS), the firewall for 80/443, nightly backups, and the `voice-tutor` command. Changes nothing else.
set -euo pipefail

API_DOMAIN="${1:-api.tutor.edgedrop.app}"
APP=/opt/voice-tutor/app
VENV=/opt/voice-tutor/venv
PYTHONS=/opt/voice-tutor/python
UV_VERSION=0.9.16
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
[ -f "$APP/app.py" ] || { echo "Clone the repository to $APP first (see deploy/README.md)."; exit 1; }
. /etc/os-release
[ "${VERSION_ID:-}" = "24.04" ] || echo "Note: written for Ubuntu 24.04; this is ${PRETTY_NAME:-unknown}."

say "Swap (2 GB): a safety net for a 1 GB machine"
if ! swapon --show --noheadings | grep -q .; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/90-voice-tutor.conf
sysctl -q --system

say "Switching off services a cloud VM doesn't use (frees memory)"
for svc in fwupd ModemManager; do
  systemctl disable --now "$svc" >/dev/null 2>&1 || true
done

say "Packages"
apt-get update -q
apt-get install -y -q curl git debian-keyring debian-archive-keyring apt-transport-https gnupg sqlite3 >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy >/dev/null
fi
if ! command -v uv >/dev/null || [ "$(uv --version | awk '{print $2}')" != "$UV_VERSION" ]; then
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

say "Service user and folders"
id voicetutor >/dev/null 2>&1 || useradd --system --home-dir /var/lib/voice-tutor --shell /usr/sbin/nologin voicetutor
install -d -m 700 -o voicetutor -g voicetutor /var/lib/voice-tutor /var/lib/voice-tutor/data /var/lib/voice-tutor/cache
install -d -m 750 -o voicetutor -g voicetutor /var/log/voice-tutor
install -d -m 700 -o voicetutor -g voicetutor /var/backups/voice-tutor
install -d -m 750 -o root -g voicetutor /etc/voice-tutor

say "Python 3.13 and the server-only dependencies (no torch)"
export UV_PYTHON_INSTALL_DIR="$PYTHONS"
uv python install 3.13
[ -x "$VENV/bin/python" ] || uv venv --python 3.13 "$VENV"
uv pip install --python "$VENV/bin/python" -r "$APP/deploy/vps/requirements-server.txt" -c "$APP/requirements.lock"
"$VENV/bin/python" -m compileall -q "$APP/tutor" "$APP/app.py" "$APP/manage.py" || true  # the code is read-only to the service
chmod -R a+rX "$PYTHONS" "$VENV" "$APP"

say "Settings"
if [ ! -f /etc/voice-tutor/api.env ]; then
  install -m 640 -o root -g voicetutor "$HERE/api.env.example" /etc/voice-tutor/api.env
  echo "Created /etc/voice-tutor/api.env: add GEMINI_API_KEY (and check the domains) before using the app."
fi

say "Service, nightly backup, and the voice-tutor command"
install -m 644 "$HERE/voice-tutor-api.service" /etc/systemd/system/voice-tutor-api.service
install -m 644 "$HERE/voice-tutor-backup.service" /etc/systemd/system/voice-tutor-backup.service
install -m 644 "$HERE/voice-tutor-backup.timer" /etc/systemd/system/voice-tutor-backup.timer
install -m 755 "$HERE/voice-tutor" /usr/local/bin/voice-tutor
systemctl daemon-reload
systemctl enable --now voice-tutor-backup.timer
systemctl enable voice-tutor-api
systemctl restart voice-tutor-api

say "HTTPS (Caddy) for $API_DOMAIN"
install -d -m 755 -o caddy -g caddy /var/log/caddy
sed "s/^api\.tutor\.edgedrop\.app {/$API_DOMAIN {/" "$HERE/Caddyfile" > /etc/caddy/Caddyfile
# Validate as the caddy user: run as root, the check would create its log file owned by root, which the
# service (user caddy) then can't open.
runuser -u caddy -- caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
chown -R caddy:caddy /var/log/caddy
systemctl enable caddy >/dev/null
systemctl reload caddy 2>/dev/null || systemctl restart caddy

say "Firewall: allow 80 and 443 (Oracle's Ubuntu image rejects everything but SSH)"
for port in 80 443; do
  if ! iptables -C INPUT -p tcp -m state --state NEW -m tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    line=$(iptables -L INPUT --line-numbers -n | awk '/REJECT/ {print $1; exit}')
    iptables -I INPUT "${line:-1}" -p tcp -m state --state NEW -m tcp --dport "$port" -j ACCEPT
  fi
done
if command -v netfilter-persistent >/dev/null; then
  netfilter-persistent save >/dev/null
else
  echo "WARNING: netfilter-persistent not found; the 80/443 rules won't survive a reboot."
fi

say "Done"
sleep 2
voice-tutor status || true
cat <<EOF

Next:
  1. sudo nano /etc/voice-tutor/api.env      add GEMINI_API_KEY (check the domains)
  2. sudo voice-tutor user create you@example.com
  3. sudo voice-tutor restart
  4. In Oracle Cloud, allow TCP 80 and 443 in this VM's subnet security list (deploy/README.md, step 2).
  5. Check: curl https://$API_DOMAIN/api/health
EOF
