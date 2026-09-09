# Running it for free

The bot needs a computer that stays switched on. It does **not** need a
powerful one: roughly 250 MB of RAM and a handful of HTTPS calls per hour.

It also needs **no inbound network access at all**. Slack Socket Mode is an
outbound websocket and PortGround is an outbound HTTPS call, so the machine
opens no ports to the internet and needs no firewall rule beyond SSH for
you to administer it.

## Pick a machine

| Option | Cost | Region | Notes |
|---|---|---|---|
| **A machine you already own** — office server, always-on PC, Synology NAS with Docker | Free | Your building | Best if you have one. Data never leaves the network. |
| **Oracle Cloud Always Free** | Free indefinitely | **Frankfurt / Amsterdam** | Recommended cloud option. EU region matters here — see *Data protection*. |
| **Google Cloud Always Free** (e2-micro) | Free indefinitely | US only | Works, but puts consignee data on a US server. |
| **Ubuntu under WSL2 on a Windows PC** | Free | Your desk | Fine for testing today; stops whenever Windows sleeps or restarts — see below. |

If you have your own machine, skip to [Install](#install) — the steps are
the same once you have a Linux box with a shell.

### Data protection

The workbooks contain house tracking numbers, invoice numbers and MRNs for
consumer parcels. Keep them in the EU or on your own hardware, and keep
`DOWNLOAD_RETENTION_DAYS` set (the bot deletes downloads older than that
automatically). This is the reason Oracle Frankfurt is recommended over
Google's free tier, which is US-only.

---

## Already have Ubuntu on Windows (WSL2)?

Then you can run the bot today, without signing up for anything. But know
what you are getting:

**WSL2 on a desktop is a good test host and a poor 24/7 host.**

| | |
|---|---|
| Windows sleeps or hibernates | WSL stops; no polls, no Slack updates |
| Windows restarts (including Windows Update) | WSL does **not** start again on its own |
| You open a WSL terminal | WSL starts |

The bot itself recovers cleanly — the schedule is in SQLite, so every
tracked AWB resumes on its own clock. But an AWB tracked at 17:00 gets no
updates overnight if the PC sleeps, and picks up again only when someone
opens a terminal. For daytime customs work that may be perfectly
acceptable; just decide it deliberately rather than discovering it.

The recommendation: **use WSL to prove it works today, move to an
always-on host once you are happy with it.**

WSL's NAT networking, normally the awkward part of hosting anything under
WSL, is a non-issue here — the bot accepts no inbound connections.

### First: make sure you are actually in Ubuntu

This is the step that catches everyone. Windows PowerShell and Ubuntu are
two different shells, and Linux commands pasted into PowerShell fail in
confusing ways — a trailing `\` becomes a filename, and you get
`fatal: repository '\' does not exist`.

Open Ubuntu from the Start menu, or type `wsl` in PowerShell. Then check
the prompt:

| Prompt | Where you are |
|---|---|
| `PS C:\Windows\system32>` | **PowerShell — wrong.** Type `wsl` and press Enter. |
| `jozsefkqt@DESKTOP-...:~$` | Ubuntu — correct. |

The rule of thumb: the prompt must end in `$`, not `>`.

### Fastest path — no Docker, about five minutes

> **Run these one line at a time.** Pasting the whole block at once fails
> in a way that is hard to read: `sudo` stops to ask for your password, and
> every remaining pasted line is consumed as a password attempt. You get
> three "Authentication failed" messages and nothing is installed.

Start in your home directory. If you reached Ubuntu by typing `wsl` inside
PowerShell, you are in `/mnt/c/Windows/system32`, which is the wrong place:

```bash
cd ~
```

Check what is already installed — Ubuntu images vary, and if these are
present you can skip the `sudo` step entirely:

```bash
git --version; python3 --version; python3 -m venv --help >/dev/null 2>&1 && echo "venv: OK" || echo "venv: MISSING"
```

Only if something is missing:

```bash
sudo apt update
```
```bash
sudo apt install -y python3-venv python3-pip git
```

Then, one line at a time:

```bash
git clone -b claude/awb-tracking-slack-bot-noy6d8 https://github.com/jozsefkacser-qtqt/lej-cc-tool.git ~/lej-cc-tool
```
```bash
cd ~/lej-cc-tool
```
```bash
python3 -m venv .venv
```
```bash
source .venv/bin/activate
```
```bash
pip install -e .
```
```bash
cp .env.example .env && chmod 600 .env
```
```bash
nano .env
```

Fill in the three secrets, then Ctrl-O, Enter, Ctrl-X. Finally:

```bash
lej-cc-doctor
```
```bash
lej-cc
```

Clone into `~`, never `/mnt/c/...` — the Windows filesystem is far slower
under WSL and brings its own permission oddities.

Then in Slack: `/awb 488-20744846`.

While `lej-cc` is running in that terminal the bot is live. Close the
terminal and it stops — which is exactly what you want while testing.

### If you decide to keep it on this PC

Three things to change:

1. **Enable systemd in WSL** so the service can run in the background.
   Create `/etc/wsl.conf`:

   ```ini
   [boot]
   systemd=true
   ```

   Then in PowerShell: `wsl --shutdown`, and reopen Ubuntu.

2. **Install the service** — follow [Without Docker](#without-docker)
   below, using `~/lej-cc-tool` as the path.

3. **Start WSL when Windows starts.** WSL does not do this by itself.
   In Task Scheduler, create a task that runs at logon:

   ```
   Program:   wsl.exe
   Arguments: -d Ubuntu -u root systemctl start lej-cc-tool
   ```

   And set the Windows power plan so the machine never sleeps.

Even then it is only as reliable as the PC. If clearance status genuinely
needs watching overnight, use the Oracle VM.

### A note on Docker under WSL

If you want Docker here, install **Docker Engine inside WSL**
(`curl -fsSL https://get.docker.com | sudo sh`) rather than Docker Desktop.
Docker Desktop needs a paid business licence above 250 employees or $10M
revenue; Docker Engine is free regardless. Engine inside WSL needs systemd
enabled as above.

---

## Oracle Cloud Always Free — full walkthrough

Budget about 30 minutes, most of it waiting for the account to verify.

### 1. Create the account

1. <https://www.oracle.com/cloud/free/> → **Start for free**
2. **Home region: Germany Central (Frankfurt)** — this cannot be changed later
3. A credit card is required for identity verification. Always Free
   resources are not charged; the account stays on the free tier unless you
   explicitly upgrade.

### 2. Create the VM

**Compute → Instances → Create instance**

| Field | Value |
|---|---|
| Image | Canonical Ubuntu 24.04 |
| Shape | `VM.Standard.E2.1.Micro` (1/8 OCPU, 1 GB) — marked *Always Free* |
| SSH keys | Generate a key pair and **download the private key** |
| Networking | Defaults are fine — assign a public IPv4 |

`VM.Standard.A1.Flex` (ARM, up to 4 cores / 24 GB) is also Always Free and
much faster, but is frequently out of capacity. The E2.1.Micro is plenty
for this workload — take the ARM shape only if it is available.

**No ingress rules are needed.** Leave the default security list alone; the
bot never accepts inbound connections.

### 3. Connect

```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@<public-ip>
```

### 4. Install the bot

Ubuntu images ship without git, so install it and fetch the repository:

```bash
sudo apt update && sudo apt install -y git
sudo mkdir -p /opt/lej-cc-tool && sudo chown "$USER" /opt/lej-cc-tool
git clone -b claude/awb-tracking-slack-bot-noy6d8 https://github.com/jozsefkacser-qtqt/lej-cc-tool.git /opt/lej-cc-tool
cd /opt/lej-cc-tool
```

The repository is private, so `git clone` asks for credentials. Use your
GitHub username and a **personal access token** as the password (GitHub →
Settings → Developer settings → Personal access tokens → Fine-grained
tokens, with read access to this repository). Your account password will
not work.

### 5. Run the setup script

```bash
bash deploy/bootstrap.sh
```

It adds swap, installs Docker, asks for the three secrets, runs the
preflight checks and starts the bot. It is safe to re-run — every step
checks before it acts, so if something fails you fix it and run the same
command again.

You will be prompted for:

| Prompt | Where it comes from |
|---|---|
| Slack bot token (`xoxb-…`) | Slack app → OAuth & Permissions |
| Slack app token (`xapp-…`) | Slack app → Basic Information → App-Level Tokens |
| PortGround API key | The key from PortGround |

Input is hidden while you type. The script writes them to `.env` with
`chmod 600` and never echoes them back.

### 6. Check it

The preflight runs automatically, but you can run it any time:

```bash
sudo docker compose run --rm lej-cc-tool lej-cc-doctor
```

```
lej-cc-tool preflight

[  ok  ] configuration    .env loaded
[  ok  ] slack bot token  xoxb-901… (57 chars)
[  ok  ] slack app token  xapp-1-A… (91 chars)
[  ok  ] portground key   BJuvNOZ0… (64 chars)
[  ok  ] database dir     data is writable
[  ok  ] download dir     data/downloads is writable
[  ok  ] status map       'cleared' and 'not cleared' map correctly
[  ok  ] slack auth       connected as awb_tracker in QT Logistics
[  ok  ] portground api   downloaded and parsed 48820744846: 225 shipments, 100% cleared

All checks passed — start the bot and try /awb 488-20744846 in Slack.
```

Every failure names the fix. The most common one is the two Slack tokens
swapped — the check catches that explicitly.

### 7. Try it

In Slack, in a channel the bot was invited to:

```
/awb 488-20744846
```

A status card should appear within a few seconds.

---

## Day to day

```bash
cd /opt/lej-cc-tool

docker compose logs -f --tail=100     # what is it doing
docker compose restart                # restart (tracked AWBs survive)
docker compose down                   # stop
git pull && docker compose up -d --build   # update to a new version
```

**Backup.** Everything worth keeping is in `data/`:

```bash
tar czf ~/lej-cc-backup-$(date +%F).tar.gz data/
```

`data/lej_cc.sqlite3` holds the tracked AWBs and the full snapshot history.
`data/downloads/` holds the workbooks, auto-purged after
`DOWNLOAD_RETENTION_DAYS`.

**Restarts are safe.** The polling schedule lives in the database, not in
memory, so a reboot, a crash or a `docker compose restart` resumes every
tracked AWB on its own clock.

---

## Without Docker

If your IT would rather not run containers, `deploy/lej-cc-tool.service`
runs it as an ordinary system service:

```bash
sudo apt install -y python3-venv git
sudo useradd --system --home /opt/lej-cc-tool lejcc

cd /opt/lej-cc-tool
python3 -m venv .venv
.venv/bin/pip install -e .
sudo chown -R lejcc /opt/lej-cc-tool

sudo cp deploy/lej-cc-tool.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lej-cc-tool

systemctl status lej-cc-tool
journalctl -u lej-cc-tool -f
```

Same auto-restart behaviour, no Docker involved.

---

## Known caveat: Oracle's idle-instance policy

Oracle may **stop** (not delete) Always Free compute instances that look
idle over a 7-day window — under roughly 10% CPU, network and memory. This
bot is deliberately light, so it can trip that heuristic during a quiet
week.

In practice this is a minor annoyance rather than a risk:

- The instance is stopped, not deleted; restart it from the console
- The boot volume is untouched, so `data/` and all tracked AWBs survive
- `restart: unless-stopped` brings the bot back automatically on boot

If it becomes a nuisance, either run it on your own hardware, or switch the
account to Pay As You Go — Always Free resources stay free there and idle
reclamation no longer applies. Only do that if you are comfortable with a
payment method on file.

Google Cloud's free e2-micro has no idle policy, so if the Oracle stopping
becomes disruptive and the US region is acceptable to your DPO, that is the
alternative.
