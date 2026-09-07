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

If you have your own machine, skip to [Install](#install) — the steps are
the same once you have a Linux box with a shell.

### Data protection

The workbooks contain house tracking numbers, invoice numbers and MRNs for
consumer parcels. Keep them in the EU or on your own hardware, and keep
`DOWNLOAD_RETENTION_DAYS` set (the bot deletes downloads older than that
automatically). This is the reason Oracle Frankfurt is recommended over
Google's free tier, which is US-only.

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

### 4. Prepare the machine

```bash
sudo apt update && sudo apt upgrade -y

# 1 GB of RAM is enough, but a little swap avoids surprises
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
newgrp docker
```

### 5. Install the bot

```bash
sudo mkdir -p /opt/lej-cc-tool && sudo chown ubuntu /opt/lej-cc-tool
git clone https://github.com/jozsefkacser-qtqt/lej-cc-tool.git /opt/lej-cc-tool
cd /opt/lej-cc-tool
git checkout claude/awb-tracking-slack-bot-noy6d8

cp .env.example .env
nano .env        # fill in the three secrets, then Ctrl-O, Enter, Ctrl-X
```

The repository is private, so `git clone` will ask for credentials. Use
your GitHub username and a **personal access token** (Settings → Developer
settings → Personal access tokens) as the password — not your account
password.

Fill in:

```ini
SLACK_BOT_TOKEN=xoxb-...        # from the Slack app you created
SLACK_APP_TOKEN=xapp-...        # from the Slack app you created
PORTGROUND_API_KEY=...          # the key from PortGround
SLACK_OPS_CHANNEL=C0123456789   # optional: where global alerts go
```

Lock the file down — it holds three secrets:

```bash
chmod 600 .env
```

### 6. Start it

```bash
docker compose up --build -d
docker compose logs -f          # Ctrl-C stops watching, not the bot
```

You are looking for `lej-cc-tool ready`.

### 7. Try it

In Slack, in a channel the bot was invited to:

```
/awb 488-20744846
```

You should get a status card within a few seconds.

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
