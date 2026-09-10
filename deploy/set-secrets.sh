#!/usr/bin/env bash
#
# Safely set or update the secrets in .env.
#
#   bash deploy/set-secrets.sh
#
# Prompts for each secret, keeps the current value if you just press Enter,
# refuses to write anything that is obviously wrong, and leaves every other
# setting in the file untouched. Writing .env by hand with a shell one-liner
# is how an aborted prompt silently becomes an empty token.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE=".env"
[[ -f "$ENV_FILE" ]] || : > "$ENV_FILE"
chmod 600 "$ENV_FILE"

current() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }

ask() {
    local key="$1" label="$2" prefix="$3" minimum="$4"
    local existing value
    existing="$(current "$key")"

    if [[ -n "$existing" ]]; then
        printf '\n%s\n  currently set (%d chars). Press Enter to keep it.\n' "$label" "${#existing}"
    else
        printf '\n%s\n  not set.\n' "$label"
    fi

    # -s so the value never reaches the screen, a screenshot or the scrollback.
    # The hint matters: hidden input reads as a frozen terminal otherwise.
    read -rsp "  paste it (nothing will appear), then press Enter: " value || true
    printf '\n'

    if [[ -z "$value" ]]; then
        if [[ -n "$existing" ]]; then
            echo "  kept"
            return
        fi
        echo "  still empty — skipping (the preflight will flag it)"
        return
    fi

    if [[ -n "$prefix" && "$value" != "$prefix"* ]]; then
        echo "  ✗ should start with '$prefix' — not written. Run this again."
        return
    fi
    if (( ${#value} < minimum )); then
        echo "  ✗ only ${#value} chars, expected at least $minimum — not written. Run this again."
        return
    fi

    KEY="$key" VALUE="$value" FILE="$ENV_FILE" python3 - <<'PY'
import os, pathlib, re, tempfile
key, value, path = os.environ["KEY"], os.environ["VALUE"], pathlib.Path(os.environ["FILE"])
lines, seen = path.read_text().splitlines(), False
out = []
for line in lines:
    if re.match(rf"^{re.escape(key)}=", line):
        out.append(f"{key}={value}"); seen = True
    else:
        out.append(line)
if not seen:
    out.append(f"{key}={value}")
# Write via a temp file in the same directory so a crash cannot leave a
# half-written .env behind.
with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
    tmp.write("\n".join(out) + "\n")
    temporary = pathlib.Path(tmp.name)
temporary.chmod(0o600)
temporary.replace(path)
PY
    echo "  ✓ written (${#value} chars)"
}

echo "Setting secrets in $(pwd)/$ENV_FILE"
echo "Input is hidden: nothing appears as you paste. Only lengths are shown."

ask SLACK_BOT_TOKEN    "Slack bot token   (OAuth & Permissions)"       "xoxb-" 40
ask SLACK_APP_TOKEN    "Slack app token   (Basic Information)"         "xapp-" 40
ask PORTGROUND_API_KEY "PortGround API key (from the PortGround mail)" ""      32

echo
echo "Current .env (values shown only as lengths):"
awk -F= 'NF>1 && $1 !~ /^#/ {printf "  %-22s %d chars\n", $1, length($2)}' "$ENV_FILE"
cat <<'DONE'

Next:
  lej-cc-doctor                                       # verify before restarting
  pkill -f lej-cc; nohup .venv/bin/lej-cc > ~/lej-cc.log 2>&1 &
DONE
