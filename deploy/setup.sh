#!/usr/bin/env bash
# Installs Bedtime on a Debian/Ubuntu machine (a cloud VM or a Raspberry Pi)
# and makes it reachable only from your own devices through Tailscale.
#
#   git clone <this repo> && cd Hatch-Baby && sudo ./deploy/setup.sh
#
# It asks for your Hatch and Spotify details and checks them before saving.
# Safe to re-run: it updates the code and keeps your settings.
#   sudo ./deploy/setup.sh --reconfigure   # re-enter Hatch/Spotify details
#   sudo ./deploy/setup.sh --new-password  # make a new Bedtime password
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo ./deploy/setup.sh"; exit 1; }

RECONFIGURE=0
NEW_PASSWORD_REQUESTED=0
for arg in "$@"; do
  case "$arg" in
    --reconfigure) RECONFIGURE=1 ;;
    --new-password) NEW_PASSWORD_REQUESTED=1 ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
APP_DIR=/opt/bedtime
STATE_DIR=/var/lib/bedtime
ENV_FILE=$STATE_DIR/.env

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "Installing system packages and automatic security updates"
apt-get update -q
apt-get install -y -q python3 python3-venv curl unattended-upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

step "Creating the bedtime service user"
id bedtime >/dev/null 2>&1 || useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin bedtime

step "Installing the app to $APP_DIR (owned by root, read-only to the app)"
install -d -m 755 "$APP_DIR"
cp -r "$REPO_DIR/hatch_spotify.py" "$REPO_DIR/hatch_ui.py" "$REPO_DIR/ui" "$REPO_DIR/requirements.txt" "$APP_DIR/"
# hatch_rest_api needs Python 3.12+. Older systems (Debian 12, Raspberry Pi OS
# bookworm) get a private Python from uv under $APP_DIR, leaving the system alone.
is_new_enough() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; }
if [ -x "$APP_DIR/venv/bin/python" ] && ! is_new_enough "$APP_DIR/venv/bin/python"; then
  rm -rf "$APP_DIR/venv"  # left over from a run with an older Python
fi
if [ ! -d "$APP_DIR/venv" ]; then
  if is_new_enough python3; then
    python3 -m venv "$APP_DIR/venv"
  else
    echo "This system has Python $(python3 -c 'import platform; print(platform.python_version())'); installing Python 3.12 for Bedtime"
    # uv comes from PyPI like the app's other packages.
    [ -x "$APP_DIR/uv/bin/uv" ] || { python3 -m venv "$APP_DIR/uv" && "$APP_DIR/uv/bin/pip" install -q uv; }
    UV_PYTHON_INSTALL_DIR="$APP_DIR/python" "$APP_DIR/uv/bin/uv" venv --seed --python 3.12 "$APP_DIR/venv"
  fi
fi
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"

step "Installing Tailscale"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
if ! tailscale status >/dev/null 2>&1; then
  echo "Open the link below and log in to Tailscale."
  echo "Use the same account you'll use on your phone."
  tailscale up --ssh
fi
TS_NAME=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
PUBLIC_URL="https://$TS_NAME"

step "Preparing settings in $ENV_FILE (readable only by the app)"
install -d -m 700 -o bedtime -g bedtime "$STATE_DIR"
if [ ! -f "$ENV_FILE" ]; then
  install -m 600 -o bedtime -g bedtime /dev/null "$ENV_FILE"
  cat > "$ENV_FILE" <<'CONF'
# Bedtime settings. Re-enter Hatch/Spotify details with:
#   sudo ./deploy/setup.sh --reconfigure
HATCH_COLOR=ff6a00
HATCH_BRIGHTNESS=20
CONF
fi

get_env() {  # get_env KEY: print the value (empty if unset)
  KEY="$1" python3 - "$ENV_FILE" <<'PY'
import os, sys
key = os.environ["KEY"]
for line in open(sys.argv[1]):
    if line.startswith(key + "="):
        print(line.rstrip("\n").split("=", 1)[1])
        break
PY
}

set_env() {  # set_env KEY VALUE: replace or append, safely for any characters
  KEY="$1" VALUE="$2" python3 - "$ENV_FILE" <<'PY'
import os, sys
path, key, value = sys.argv[1], os.environ["KEY"], os.environ["VALUE"]
if "\n" in value or "\r" in value:
    sys.exit("Values can't contain line breaks")
lines = open(path).read().splitlines()
lines = [l for l in lines if not l.startswith(key + "=")] + [f"{key}={value}"]
with open(path, "w") as f:
    f.write("\n".join(lines) + "\n")
PY
}

ask() {  # ask KEY "Question" [secret]: prompt until a non-empty answer, then save it
  local value=""
  while [ -z "$value" ]; do
    if [ "${3:-}" = secret ]; then
      read -rsp "  $2 (hidden as you type): " value; echo
    else
      read -rp "  $2: " value
    fi
    value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"  # trim
  done
  set_env "$1" "$value"
}

check() {  # check COMMAND: run a hatch_spotify.py check as the service user
  (cd "$STATE_DIR" && runuser -u bedtime -- "$APP_DIR/venv/bin/python" "$APP_DIR/hatch_spotify.py" "$1")
}

set_env UI_HOST 127.0.0.1
set_env UI_PUBLIC_URL "$PUBLIC_URL"
set_env UI_SECURE_COOKIES 1
set_env SPOTIFY_REDIRECT_URI "$PUBLIC_URL/callback"
NEW_PASSWORD=""
if [ "$NEW_PASSWORD_REQUESTED" = 1 ] || [ -z "$(get_env UI_PASSWORD)" ]; then
  NEW_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
  set_env UI_PASSWORD "$NEW_PASSWORD"
fi
chown bedtime:bedtime "$ENV_FILE"
chmod 600 "$ENV_FILE"

CONFIGURED=1
if [ -t 0 ]; then
  if [ "$RECONFIGURE" = 1 ] || [ -z "$(get_env HATCH_EMAIL)" ] || [ -z "$(get_env HATCH_PASSWORD)" ]; then
    step "Your Hatch account (the login you use in the Hatch app)"
    until
      ask HATCH_EMAIL "Hatch email"
      ask HATCH_PASSWORD "Hatch password" secret
      echo "  Checking..."
      check check-hatch
    do
      echo "  That didn't work. Let's try again."
    done
  fi

  if [ "$RECONFIGURE" = 1 ] || [ -z "$(get_env SPOTIFY_CLIENT_ID)" ] || [ -z "$(get_env SPOTIFY_CLIENT_SECRET)" ]; then
    step "Your Spotify app"
    cat <<INFO
  1. Open https://developer.spotify.com/dashboard and log in.
  2. Tap "Create app". Use any name (e.g. Bedtime), and any description.
  3. Under "Redirect URIs", add exactly:
       $PUBLIC_URL/callback
  4. Tick "Web API", agree to the terms, and save.
  5. Open the app's Settings to find the Client ID and Client secret.

INFO
    until
      ask SPOTIFY_CLIENT_ID "Spotify Client ID"
      ask SPOTIFY_CLIENT_SECRET "Spotify Client secret" secret
      echo "  Checking..."
      check check-spotify
    do
      echo "  That didn't work. Let's try again."
    done
  fi
else
  [ -n "$(get_env HATCH_PASSWORD)" ] && [ -n "$(get_env SPOTIFY_CLIENT_SECRET)" ] || CONFIGURED=0
fi
chown bedtime:bedtime "$ENV_FILE"
chmod 600 "$ENV_FILE"

step "Installing the service"
install -m 644 "$REPO_DIR/deploy/bedtime.service" /etc/systemd/system/bedtime.service
systemctl daemon-reload
systemctl enable bedtime >/dev/null

step "Serving it over HTTPS to your Tailscale devices only"
tailscale serve --bg 8888

if [ "$CONFIGURED" = 1 ]; then
  systemctl restart bedtime
  STATUS="Bedtime is running."
else
  STATUS="Finish setup by running this again in a terminal:
    sudo ./deploy/setup.sh"
fi

cat <<DONE

==========================================================
 $STATUS

 On your phone (with Tailscale installed and logged in), open:
    $PUBLIC_URL
 then log in and tap "Connect Spotify".
DONE
if [ -n "$NEW_PASSWORD" ]; then
  cat <<DONE

 Your Bedtime password. Save it in your password manager now;
 it won't be shown again:
    $NEW_PASSWORD
DONE
fi
echo "=========================================================="
