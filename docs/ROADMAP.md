# Audit and roadmap

State as of 2026-09-15: running in production on WSL, tracking live AWBs
and booking references, 245 tests, CI green.

**Shipped since the audit:** email notifications, the escalation rule,
online/offline reporting with a heartbeat, running-version visibility,
booking-reference support, the chase sheet, one row per AWB in a Google
Sheet, the completion forecast, lead-time analytics, and starting a check by
email. What follows is what is left.

---

# Part 1 — Audit

A read-through of all 2,662 lines looking for things that are wrong rather
than things that are missing.

## Fixed in this pass

| Severity | Finding |
|---|---|
| **High** | `get`, `find_active` and `list_active` ran on the shared SQLite connection **outside the lock** while poll threads wrote through it. sqlite3's threadsafety level varies by build, so this was correct only by luck. All statements are now serialised. Fixing it exposed a latent deadlock — `create_job` holds the lock and reads back the row it inserted — caught immediately by the new concurrency test. |
| **High** | A lease outlives the slowest poll by design, so a process **killed mid-poll left its jobs untouchable for up to 30 minutes** after restart, exactly when they most need picking up. Startup now releases stale leases. |
| **Medium** | **No Slack rate-limit handling.** `chat.postMessage` is limited to ~1/sec per channel; several AWBs finishing together would silently drop updates. Retry handlers attached. |
| **Medium** | **`mypy` was configured and run by nothing**, so it had quietly rotted. Now in CI with the missing stubs, and every check runs via `python -m` so it uses the interpreter that has the dependencies. |

Earlier passes fixed: the API key being written to logs by httpx, the
container crashing at import for want of a timezone database, a 60s timeout
against an API that takes 112s, the status map never resolving inside the
container, the retention setting that deleted nothing, the Slack handler
blocking for two minutes before replying, and test fixtures that a
`.gitignore` pattern silently kept out of every clone.

## Outstanding — with an honest severity

| Severity | Finding | Notes |
|---|---|---|
| ~~Medium~~ | ~~No liveness signal.~~ **Done** — startup/shutdown announcements, `/awb status`, and a dead-man's-switch heartbeat. |
| **Medium** | **Untested edges.** `slack_app`, `slack_io`, `portground`, `cli` and `main` have no tests — the entire Slack input surface and the HTTP client. The domain core is well covered; the boundaries are not. |
| **Medium** | **No auto-restart.** `nohup` survives closing a window, not a reboot. |
| **Low** | **At-least-once delivery.** A crash between posting and rescheduling re-posts on restart. Correct trade (never lose an update), worth knowing. |
| **Low** | **Docker image never built.** The Dockerfile is unverified — no daemon was available while writing it. |
| **Low** | **No pruning of finished jobs.** `jobs` grows forever. Thousands of rows is nothing, but it is unbounded. |
| **Low** | **No per-user throttle on `/awb`.** Someone could start a hundred jobs. Trust-based today, which matches a private channel. |
| **Info** | **Single process.** No horizontal scaling. The design allows it (leases, DB-held schedule); the volume does not need it. |

---

# Part 2 — Email

The requested feature, in two independent halves. Either can ship alone.

## 2a. Email out — notifications

**Transport.** SMTP through Google Workspace (`smtp.gmail.com:587`, app
password on a dedicated account), or the Workspace SMTP relay if IT prefers
it. No inbound anything.

**Content.** The same information as the Slack card, rendered as HTML: the
progress bar as styled table cells, the stat tiles as a two-column table,
the chase sheet and the full export attached. Plain-text alternative for
mobile clients and for people who block HTML.

**Threading.** Set `In-Reply-To` and `References` to the original request so
updates collapse into one conversation rather than filling an inbox.

**Cadence.** Stricter than Slack by default: first status, changes, and
completion. Never a "no change" email — that is what kills adoption.

**Recipients.** Whoever asked, plus an optional standing distribution list
per channel or globally (`EMAIL_ALWAYS_CC`).

Effort: about a day, including the HTML template.

## 2b. Email in — start a check by email — **DONE**

Built as designed below, in `src/lej_cc/inbox.py`, with every security rule
enforced and tested. Two things landed differently from this plan:

* **The `Destination` refactor was not needed.** An email-started job posts
  to `IMAP_TARGET_CHANNEL` and mails the requester, which the existing
  `channel_id` + `email_to` columns already express. Introducing a
  destination abstraction to model two destinations would have been
  scaffolding, not structure.
* **References are read from the subject line**, not only from a slash
  command. The subject is as deliberate an act as typing a command, and
  without it a reference could not be mailed in at all. Bodies are still
  scanned for checksum-valid AWBs only, and the quoted history in a reply is
  cut off before anything is read, so answering "thanks" to an update does
  not restart the whole thread.

**Transport options considered:**

| Approach | Verdict |
|---|---|
| **IMAP polling of a shared mailbox** | **Recommended.** Works with any provider, no cloud project, no inbound firewall, no MX changes. 60s latency is irrelevant against a 15/30 minute cadence. |
| Gmail API + Pub/Sub push | Instant, but needs a Google Cloud project, a service account and domain-wide delegation. Worth it only if latency ever matters. |
| Inbound SMTP | Needs port 25, MX records and spam handling. No. |

**Flow.** Poll `awb@qtlogistics.eu` every 60s → for each unread message,
authenticate the sender → extract AWBs from subject and body with the
existing `extract_all()` → create jobs → reply confirming what was picked
up, or explaining why nothing was → move to a `Processed` folder.

The checksum validation already written for Slack does the heavy lifting
here: signatures, phone numbers and reference numbers in an email body are
full of digit strings, and only genuine AWBs survive the mod-7 test.

**Security — this is the part that matters.** Email is trivially spoofable,
and a reply carries customs data for a shipment.

- A **sender allowlist is mandatory** — addresses or domains, no default-open mode.
- Additionally require SPF/DKIM to have passed, read from `Authentication-Results`.
- **Only ever reply to an allowlisted address.** Never to `Reply-To` or `From` on an unverified message.
- Deduplicate on `Message-ID`; a resent mail must not start a second job.
- Cap jobs per sender per hour.

**Data model.** `jobs` gains `source`, `reply_to`, `email_message_id`; a new
`processed_emails` table holds the dedup keys.

**Architecture.** `Tracker` currently takes one `Notifier`. It becomes a
list, with a `Destination` describing where a job reports (`slack:C0123`,
`email:a@b.com`) and how to thread there. An AWB started in Slack can then
also notify a mailing list, and one started by email can post to a channel.

Effort: about a day for the poller, plus half a day for the security work.
Do **2a before 2b** — notifications are useful on their own, and the
`Destination` refactor they need is the same refactor 2b depends on.

---

# Part 3 — The map

Everything worth considering, grouped, with a rough size and what it is
actually worth.

## Operational confidence
*Making it something you can rely on rather than something you check.*

| Idea | Size | Value |
|---|---|---|
| Oracle VM, 24/7 | half a day | **Critical.** Everything else assumes it. |
| systemd unit + auto-restart | 1 h | **High.** Survives reboots, restarts on crash. |
| Daily heartbeat post ("tracking N AWBs, all healthy") | 2 h | **High.** Silence stops being ambiguous. |
| External dead-man's-switch (healthchecks.io ping) | 1 h | **High.** Tells you when the bot dies, not the bot. |
| `/awb health` — uptime, jobs, last poll, API latency | 2 h | Medium |
| Nightly SQLite backup to Drive | 2 h | Medium |
| Tests for the Slack and HTTP edges | 1 day | Medium |
| Build and smoke-test the Docker image | 2 h | Medium |

## Input — how an AWB gets tracked

| Idea | Size | Value |
|---|---|---|
| ~~Email trigger~~ (Part 2b) | — | **Done** — allowlist mandatory, SPF/DKIM required, dedup + rate limit. Needs a mailbox named to switch on. |
| ~~Auto-detect AWBs posted in a dedicated channel~~ | — | **Done** — per-channel, checksum-gated, silent on an AWB already tracked |
| Bulk: paste or upload a list of AWBs | half a day | Medium — useful for a flight's worth at once |
| Standing AWBs: track every AWB on a route automatically | 1 day | Medium, needs a source of "which AWBs" |
| Slack shortcut / workflow step | half a day | Low |

## Output — where results go

| Idea | Size | Value |
|---|---|---|
| ~~Email notifications~~ (Part 2a) | — | **Done** — HTML + text, both workbooks attached, domain allowlist |
| ~~Google Sheet, one row per AWB~~ | — | **Done** — own tab, pulled into Daily Report_LEJ by VLOOKUP |
| Google Drive archive of every export | 2 h | **High.** Audit trail, and it is nearly free. |
| Weekly digest: what cleared, what took longest | half a day | Medium |
| Push notification files to `portground.notification@singular-it.de` | 1 day | Medium — closes the loop back to PortGround |

## Insight — the part with compounding value

You already store a snapshot every poll. Nobody else in the chain has this
data, and it accumulates whether or not anyone builds on it.

| Idea | Size | Value |
|---|---|---|
| ~~Lead-time report: distribution of check-in → cleared~~ | — | **Done** — `/awb stats`, refuses to average below 3 AWBs |
| ~~**Completion forecast** on the card~~ | — | **Done** — least squares over 6 h, returns nothing rather than a guess |
| Which HAWBs / consignees habitually block | 1 day | High |
| Per-airline, per-broker clearance performance | 1 day | High — supplier conversations with evidence |
| Anomaly alert: this AWB is slower than its peers | 1 day | Medium |
| Declaration complexity vs clearance time (lines per shipment) | half a day | Medium — testable hunch, not yet a fact |

## Message and workflow

| Idea | Size | Value |
|---|---|---|
| ~~Escalation if an AWB stops moving~~ | — | **Done** — once per stall, re-arms on progress |
| Quiet hours: no updates 20:00–06:00, digest at 06:00 | 2 h | Medium |
| Per-AWB cadence (`/awb 488-… every 10m`) | 2 h | Medium |
| Adaptive cadence: faster while moving, slower overnight | half a day | Medium |
| Hungarian / German message text | half a day | Low unless colleagues ask |

## Data quality

| Idea | Size | Value |
|---|---|---|
| Alert the first time an unknown status appears | done | — |
| Ask PortGround for a JSON endpoint | one email | **High.** Deletes the whole parsing layer. |
| Reconcile against a second source | ? | Only if PortGround is ever wrong |

---

## Scope boundaries, decided

Recording these so they are not re-litigated or quietly widened.

| Boundary | Why |
|---|---|
| The sheet export writes `CC Completed` and no further milestone | The bot owns what it can observe in PortGround's export. Pickup, line haul and depot arrival come from systems it cannot see, and an inferred value in a column an SLA calculation reads is worse than a blank. |
| It writes only to its own tab, never into the hand-maintained report | Column alignment there cannot be read reliably, and 234 rows of real work are one offset away from corruption. |
| Booking references are accepted when typed, never scraped from message text | They carry no checksum, so a pattern loose enough to catch one also catches order numbers and file names. |
| An unrecognised customs status counts as *not cleared* | Silently rounding an unknown up to cleared is the one failure mode that costs money. |
| A shipment under customs inspection counts as *settled*, not *open* | Nobody here can move it, so listing it as open work is false. It is not counted as cleared either: the two percentages are shown apart and sum to 100%, and `CC Completed` stays blank until everything genuinely clears. |

# Recommended order

1. **A real server + systemd** — the last thing standing between this and
   something colleagues can rely on without anyone nursing it. WSL has now
   cost sleep interruptions, DNS failures and manual restarts; none of them
   are bugs in the tool and none of them happen on a server. Now planned on
   the local server rather than an Oracle VM; `deploy/lej-cc-tool.service`
   applies either way.
2. ~~Email trigger~~ — **done.** Needs a shared mailbox named and its
   credentials set to switch on.
3. ~~Completion forecast~~ — **done.**
4. ~~Lead-time analytics~~ — **done** (`/awb stats`).
5. **Drive archive** of every export — cheap, and the audit trail this
   process should have had from day one.

Item 1 is half a day and is now the only thing on this list that changes
how reliable the tool is. The rest change how much it is worth.
