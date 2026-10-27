#!/usr/bin/env bash
# Runs ON THE VM as root (sudo bash provision.sh). Idempotent.
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/discord-bot}"; APP_USER="${APP_USER:-botsvc}"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip sqlite3 rsync
# 2 GB swap (e2-micro has 1 GB RAM)
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -q vm.swappiness=10; echo vm.swappiness=10 > /etc/sysctl.d/90-swap.conf
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"; chown -R "$APP_USER:$APP_USER" "$APP_DIR"; chmod 750 "$APP_DIR"
[ -f "$APP_DIR/.env" ] && chmod 600 "$APP_DIR/.env"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"
# No logrotate for bot.log: only the weekly trim touches it. Keep journald small.
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=100M\n' > /etc/systemd/journald.conf.d/size.conf
cat > /etc/systemd/system/discord-bot.service <<U
[Unit]
Description=Discord Q&A bot
After=network-online.target
Wants=network-online.target
[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=/bin/bash run.sh
StandardOutput=append:$APP_DIR/bot.log
StandardError=append:$APP_DIR/bot.log
ExecStopPost=/bin/sh -c 'echo "[systemd] bot exited (\$EXIT_STATUS) at \$(date), restarting in 15s" >> $APP_DIR/bot.log'
Restart=always
RestartSec=15
[Install]
WantedBy=multi-user.target
U
cat > /etc/systemd/system/discord-bot-synthetic.service <<U
[Unit]
Description=Hourly synthetic test harness (one run)
After=discord-bot.service
[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=/bin/bash -c 'set -a; . ./.env; set +a; exec .venv/bin/python tools_local/synthetic_test.py'
StandardOutput=null
StandardError=append:$APP_DIR/bot.log
TimeoutStartSec=900
U
cat > /etc/systemd/system/discord-bot-synthetic.timer <<U
[Unit]
Description=Run the synthetic harness hourly
[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Persistent=true
[Install]
WantedBy=timers.target
U
cat > /etc/systemd/system/discord-bot-weekly.service <<U
[Unit]
Description=Weekly bot.log trim + DB maintenance
[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=/bin/bash tools_local/trim_logs.sh
U
cat > /etc/systemd/system/discord-bot-weekly.timer <<U
[Unit]
Description=Weekly bot.log trim (Sat 10:00 Pacific)
[Timer]
OnCalendar=Sat *-*-* 10:00:00 America/Los_Angeles
Persistent=true
[Install]
WantedBy=timers.target
U
timedatectl set-timezone America/Los_Angeles || true
systemctl daemon-reload
systemctl enable discord-bot.service discord-bot-synthetic.timer discord-bot-weekly.timer
touch "$APP_DIR/STANDBY"; chown "$APP_USER:$APP_USER" "$APP_DIR/STANDBY"
systemctl restart discord-bot.service
echo "Provisioned. Bot running in STANDBY (no Discord session); cutover.sh removes STANDBY and starts the timers."
