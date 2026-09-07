#!/usr/bin/env bash
#
# Sets up a fresh Ubuntu VM to run the bot: swap, Docker, .env, first start.
# Safe to re-run -- every step checks before it acts.
#
#   cd /opt/lej-cc-tool && bash deploy/bootstrap.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die()  { printf '\033[31m    %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Linux" ]] || die "This script is for Linux. See docs/DEPLOY-FREE.md."
command -v apt-get >/dev/null || die "Expected a Debian/Ubuntu system (apt-get not found)."
[[ $EUID -ne 0 ]] || warn "Running as root; the bot itself will not run as root."

# --- 1. swap ---------------------------------------------------------------
# The Always Free shape has 1 GB of RAM. That is enough, but a little swap
# stops a large workbook parse from ending in an OOM kill.
say "Swap"
if [[ -n "$(swapon --show --noheadings 2>/dev/null)" ]]; then
    echo "    already configured"
else
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    echo "    2 GB swap added"
fi

# --- 2. docker -------------------------------------------------------------
say "Docker"
if command -v docker >/dev/null; then
    echo "    already installed ($(docker --version))"
else
    sudo apt-get update -qq
    curl -fsSL https://get.docker.com | sudo sh
    echo "    installed"
fi
sudo usermod -aG docker "$USER" || true
sudo systemctl enable --now docker >/dev/null 2>&1 || true

# --- 3. configuration ------------------------------------------------------
say "Configuration"
if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    echo "    created .env from .env.example"
fi
chmod 600 .env

# Prompt for anything still unset. Values are read without echo and written
# with python so that special characters survive intact.
prompt_secret() {
    local key="$1" label="$2" current value
    current="$(grep -E "^${key}=" .env | cut -d= -f2- || true)"
    if [[ -n "$current" && "$current" != *"..."* ]]; then
        echo "    ${key} already set"
        return
    fi
    if [[ ! -t 0 ]]; then
        warn "${key} is not set and this is not an interactive shell — edit .env by hand"
        return
    fi
    read -rsp "    ${label}: " value; echo
    [[ -n "$value" ]] || { warn "${key} left empty"; return; }
    KEY="$key" VALUE="$value" python3 - <<'PY'
import os, pathlib, re
key, value = os.environ["KEY"], os.environ["VALUE"]
path = pathlib.Path(".env")
lines = path.read_text().splitlines()
out, seen = [], False
for line in lines:
    if re.match(rf"^{re.escape(key)}=", line):
        out.append(f"{key}={value}"); seen = True
    else:
        out.append(line)
if not seen:
    out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n")
PY
}

prompt_secret SLACK_BOT_TOKEN  "Slack bot token (xoxb-...)"
prompt_secret SLACK_APP_TOKEN  "Slack app token (xapp-...)"
prompt_secret PORTGROUND_API_KEY "PortGround API key"

# --- 4. build and start ----------------------------------------------------
say "Building"
sudo docker compose build

say "Preflight"
if ! sudo docker compose run --rm lej-cc-tool lej-cc-doctor; then
    die "Preflight failed. Fix what it reported, then re-run: bash deploy/bootstrap.sh"
fi

say "Starting"
sudo docker compose up -d

cat <<'DONE'

    Running. Next:

      In Slack, in a channel the bot was invited to:
          /awb 488-20744846

      Watch what it does:
          sudo docker compose logs -f

      Log out and back in to use docker without sudo.

DONE
