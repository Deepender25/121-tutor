#!/usr/bin/env bash
# Pull the latest code and restart the API (run as: sudo voice-tutor update). Stops at the first error;
# the running API is only restarted after the new code and its dependencies installed cleanly.
set -euo pipefail
APP=/opt/voice-tutor/app
VENV=/opt/voice-tutor/venv
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo voice-tutor update"; exit 1; }

cd "$APP"
before=$(git rev-parse --short HEAD)
git pull --ff-only
after=$(git rev-parse --short HEAD)
if [ "$before" = "$after" ]; then
  echo "Already up to date ($after)."
  exit 0
fi
echo "Updating $before -> $after"
UV_PYTHON_INSTALL_DIR=/opt/voice-tutor/python uv pip install --python "$VENV/bin/python" \
  -r deploy/vps/requirements-server.txt -c requirements.lock
"$VENV/bin/python" -m compileall -q tutor app.py manage.py || true
chmod -R a+rX "$APP" "$VENV"
# Service files may have changed too.
install -m 644 deploy/vps/voice-tutor-api.service /etc/systemd/system/voice-tutor-api.service
install -m 644 deploy/vps/voice-tutor-backup.service /etc/systemd/system/voice-tutor-backup.service
install -m 644 deploy/vps/voice-tutor-backup.timer /etc/systemd/system/voice-tutor-backup.timer
install -m 755 deploy/vps/voice-tutor /usr/local/bin/voice-tutor
systemctl daemon-reload
systemctl restart voice-tutor-api
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:8766/api/health >/dev/null; then
    echo "Updated to $after and healthy."
    exit 0
  fi
  sleep 1
done
echo "The API didn't come back. See: sudo voice-tutor logs"
exit 1
