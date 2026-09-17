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

# A copy before anything is touched. A secret pasted into the wrong prompt
# is otherwise unrecoverable -- it happened, and it cost a PortGround key.
BACKUP="$ENV_FILE.backup-$(date +%Y%m%d-%H%M%S)"
cp "$ENV_FILE" "$BACKUP"
chmod 600 "$BACKUP"
# Keep the five most recent and no more: these are files full of credentials.
ls -1t "$ENV_FILE".backup-* 2>/dev/null | tail -n +6 | xargs -r rm -f

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
    # A Slack token pasted into the PortGround or mailbox prompt. The clipboard
    # still holds the last thing you copied, so this is the easy mistake to
    # make and the expensive one: it silently replaces a credential you may
    # have nowhere else.
    if [[ -z "$prefix" && ( "$value" == xoxb-* || "$value" == xapp-* ) ]]; then
        echo "  ✗ that is a Slack token, and this prompt is not asking for one."
        echo "    Nothing written. Press Enter here to keep the current value."
        return
    fi
    # A replacement of a very different length is usually the wrong clipboard.
    if [[ -n "$existing" ]] && (( ${#value} != ${#existing} )); then
        printf '  ! this replaces a %d-character value with a %d-character one.\n' \
            "${#existing}" "${#value}"
        read -rp "    Type yes to confirm, anything else to keep the old one: " confirm
        if [[ "$confirm" != "yes" ]]; then
            echo "  kept the existing value"
            return
        fi
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
# Both optional: press Enter to skip if email is not switched on.
ask SMTP_PASSWORD      "SMTP password     (optional — Enter to skip)"   ""      8
ask IMAP_PASSWORD      "Mailbox password  (optional — Enter to skip)"   ""      8

echo
echo "A copy of .env as it was before this run: $BACKUP"
echo
echo "Current .env (values shown only as lengths):"
awk -F= 'NF>1 && $1 !~ /^#/ {printf "  %-22s %d chars\n", $1, length($2)}' "$ENV_FILE"
cat <<'DONE'

Next:
  awb doctor                                          # verify before restarting
  awb restart                                         # apply the new secrets

(If `awb` is not a command yet: bash deploy/awbctl install, then
 source ~/.bashrc.)
DONE
