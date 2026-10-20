#!/usr/bin/env bash
# Installs Bedtime on a Debian/Ubuntu machine (a cloud VM or a Raspberry Pi)
# and makes it reachable only from your own devices through Tailscale.
#
#   git clone <this repo> && cd Hatch-Baby && sudo ./deploy/setup.sh
#
# Safe to re-run: it updates the code and keeps your settings.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo ./deploy/setup.sh"; exit 1; }

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
APP_DIR=/opt/bedtime
STATE_DIR=/var/lib/bedtime
ENV_FILE=$STATE_DIR/.env

echo "==> Installing system packages and automatic security updates"
apt-get update -q
apt-get install -y -q python3 python3-venv curl unattended-upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

echo "==> Creating the bedtime service user"
id bedtime >/dev/null 2>&1 || useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin bedtime

echo "==> Installing the app to $APP_DIR (owned by root, read-only to the app)"
install -d -m 755 "$APP_DIR"
cp -r "$REPO_DIR/hatch_spotify.py" "$REPO_DIR/hatch_ui.py" "$REPO_DIR/ui" "$REPO_DIR/requirements.txt" "$APP_DIR/"
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"

echo "==> Installing Tailscale"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
if ! tailscale status >/dev/null 2>&1; then
  echo "    Log in to Tailscale with the link below (use the same account as your phone)."
  tailscale up --ssh
fi
TS_NAME=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
PUBLIC_URL="https://$TS_NAME"

echo "==> Preparing settings in $ENV_FILE (readable only by the app)"
install -d -m 700 -o bedtime -g bedtime "$STATE_DIR"
[ -f "$ENV_FILE" ] || install -m 600 -o bedtime -g bedtime "$REPO_DIR/.env.example" "$ENV_FILE"

set_env() {  # set_env KEY VALUE: replace or append a line in .env
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
set_env UI_HOST 127.0.0.1
set_env UI_PUBLIC_URL "$PUBLIC_URL"
set_env UI_SECURE_COOKIES 1
set_env SPOTIFY_REDIRECT_URI "$PUBLIC_URL/callback"
NEW_PASSWORD=""
if ! grep -q "^UI_PASSWORD=..*" "$ENV_FILE"; then
  NEW_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
  set_env UI_PASSWORD "$NEW_PASSWORD"
fi
chown bedtime:bedtime "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo "==> Installing the service"
install -m 644 "$REPO_DIR/deploy/bedtime.service" /etc/systemd/system/bedtime.service
systemctl daemon-reload
systemctl enable bedtime >/dev/null

echo "==> Serving it over HTTPS to your Tailscale devices only"
tailscale serve --bg 8888

if grep -q "^HATCH_PASSWORD=your-hatch-password" "$ENV_FILE" || grep -q "^SPOTIFY_CLIENT_ID=$" "$ENV_FILE"; then
  STATUS="Fill in your Hatch and Spotify details, then start it:
    sudo nano $ENV_FILE
    sudo systemctl restart bedtime"
else
  systemctl restart bedtime
  STATUS="The service is running."
fi

cat <<DONE

==========================================================
 Bedtime is installed.

 $STATUS

 In your Spotify developer app, add this redirect URI:
    $PUBLIC_URL/callback

 Then open on your phone (with Tailscale on):
    $PUBLIC_URL
DONE
if [ -n "$NEW_PASSWORD" ]; then
  echo
  echo " Your Bedtime password (save it in your password manager):"
  echo "    $NEW_PASSWORD"
fi
echo "=========================================================="
