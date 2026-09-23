#!/usr/bin/env bash
# Google Compute Engine startup script -- runs automatically on first boot
# (and every reboot) when set as the VM's startup-script metadata. Installs
# system deps, clones/updates the repo, sets up the venv, and installs +
# (re)starts the systemd service. Safe to re-run.
#
# Deliberately does NOT touch .env -- that's created once by hand over SSH
# (see the README's deploy section), same convention as everywhere else in
# this project: secrets are never baked into scripts, metadata, or git.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/woskam/job-radar-hub.git}"
APP_DIR="/opt/job-radar-hub"

apt-get update -y
apt-get install -y python3-venv python3-pip git

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

# .env is NOT created here -- if this is a first boot, the services will
# fail to start until you SSH in and create it by hand (see README.md).
install -m 644 systemd/job-radar-hub.service /etc/systemd/system/job-radar-hub.service
install -m 644 systemd/job-radar-hub-mcp.service /etc/systemd/system/job-radar-hub-mcp.service
install -m 644 systemd/job-radar-hub-alerts.service /etc/systemd/system/job-radar-hub-alerts.service
install -m 644 systemd/job-radar-hub-alerts.timer /etc/systemd/system/job-radar-hub-alerts.timer
systemctl daemon-reload
systemctl enable job-radar-hub.service job-radar-hub-mcp.service job-radar-hub-alerts.timer
systemctl restart job-radar-hub.service job-radar-hub-mcp.service || true
systemctl restart job-radar-hub-alerts.timer || true
