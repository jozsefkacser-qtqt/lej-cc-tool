"""Preflight checks.

Run this after deploying, before wondering why nothing happens. Each check
either passes or explains precisely what to fix, so a failure is a fix
instruction rather than a stack trace.

    lej-cc-doctor              # or: docker compose run --rm lej-cc-tool lej-cc-doctor

Nothing here prints a secret: tokens are shown as a prefix and a length.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

#: PortGround's own documented example, used as a live end-to-end probe.
SAMPLE_MAWB = "48820744846"

OK, FAIL, WARN = "  ok  ", " FAIL ", " warn "

#: Titles the bot should not be pointed at. Writing its own tab into one
#: of these is safe but wrong: the reports pull from the central sheet.
HAND_MAINTAINED = re.compile(r"daily\s*report|weekly\s*report", re.I)



@dataclass
class Result:
    check: str
    status: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.status is FAIL


def _mask(value: str) -> str:
    """Show enough of a token to identify it, never enough to use it."""
    if not value:
        return "(empty)"
    return f"{value[:8]}… ({len(value)} chars)"


def check_settings() -> tuple[Result, object | None]:
    from pydantic import ValidationError

    from .config import Settings

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing = sorted({str(e["loc"][0]).upper() for e in exc.errors()})
        return (
            Result(
                "configuration",
                FAIL,
                f"missing from .env: {', '.join(missing)}. "
                "Copy .env.example to .env and fill it in.",
            ),
            None,
        )
    return Result("configuration", OK, ".env loaded"), settings


def check_tokens(settings) -> list[Result]:  # noqa: ANN001
    results = []
    for name, value, prefix in (
        ("slack bot token", settings.slack_bot_token, "xoxb-"),
        ("slack app token", settings.slack_app_token, "xapp-"),
    ):
        if not value.startswith(prefix):
            results.append(
                Result(name, FAIL, f"should start with {prefix} — got {_mask(value)}")
            )
        else:
            results.append(Result(name, OK, _mask(value)))

    key = settings.portground_api_key
    results.append(
        Result("portground key", OK if key else FAIL, _mask(key) if key else "not set")
    )
    return results


def check_storage(settings) -> list[Result]:  # noqa: ANN001
    results = []
    for name, directory in (
        ("database dir", Path(settings.database_path).parent),
        ("download dir", Path(settings.download_dir)),
    ):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory):
                pass
            results.append(Result(name, OK, f"{directory} is writable"))
        except OSError as exc:
            results.append(Result(name, FAIL, f"{directory} is not writable: {exc}"))
    return results


def check_status_map(settings) -> Result:  # noqa: ANN001
    from .parser import StatusMapper

    try:
        mapper = StatusMapper.load(settings.status_map_path)
    except Exception as exc:  # noqa: BLE001
        return Result("status map", FAIL, f"could not load: {exc}")

    from .model import ClearanceStatus

    if mapper.map("cleared") is not ClearanceStatus.CLEARED:
        return Result("status map", FAIL, "'cleared' does not map to CLEARED")
    return Result("status map", OK, "'cleared' and 'not cleared' map correctly")


def check_email(settings) -> Result:  # noqa: ANN001
    """Connect and authenticate without sending anything."""
    import smtplib

    if not settings.email_enabled:
        return Result("email", OK, "not configured (SMTP_HOST empty) — Slack only")

    recipients = settings.email_always_recipients
    detail = f"{settings.smtp_host}:{settings.smtp_port} as {settings.email_from}"
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
    except smtplib.SMTPAuthenticationError:
        return Result(
            "email",
            FAIL,
            f"{detail} — authentication rejected. With Google Workspace this "
            "needs an app password, not the account password.",
        )
    except Exception as exc:  # noqa: BLE001 - network, DNS, TLS
        return Result("email", FAIL, f"{detail} — {exc}")

    always = f", always-to {', '.join(recipients)}" if recipients else ", no standing recipients"

    if not settings.allowed_email_domains:
        return Result(
            "email",
            WARN,
            f"{detail}{always} — EMAIL_ALLOWED_DOMAINS is empty, so anyone in the "
            "channel can mail customs data to any address. Set it to your own "
            "domains.",
        )
    return Result(
        "email",
        OK,
        f"{detail}{always}, only to {', '.join(settings.allowed_email_domains)}",
    )


#: What Slack's error means for the person reading it.
CHANNEL_ERRORS = {
    "channel_not_found": (
        "no channel with that id. Check AUTODETECT_CHANNELS: the id is on the "
        "channel's link (Copy link -> .../archives/C09ABCDEF), not its name. "
        "A private channel the bot has never been invited to reads the same way."
    ),
    "not_in_channel": "the bot is not a member. In Slack: /invite @AWB Tracker",
    "is_archived": "that channel is archived",
    "missing_scope": (
        "the bot has no channels:history scope. Paste the current manifest into "
        "the app's App Manifest page, save, then Install App -> Reinstall."
    ),
}


def check_track(settings) -> Result:  # noqa: ANN001
    """Can `awb track` run, before the morning somebody needs it to?

    Three separate things have to hold, and each of them failed on its own
    in practice: a channel to post the cards into, the Google client
    installed, and the report actually readable. Discovering any of them at
    the moment you want to start a month of AWBs is the wrong time, and none
    of the existing checks covered them -- the sheet check looks at the tab
    the bot writes, which is a different file with different sharing.
    """
    from .bulk import latest_month_tab
    from .sheets import describe_spreadsheet

    notes: list[str] = []
    warnings: list[str] = []

    channel = settings.status_channel
    if channel:
        notes.append(f"posts to {channel}")
    else:
        watched = settings.autodetect_channel_ids
        hint = f"; AUTODETECT_CHANNELS names {watched[0]}" if len(watched) == 1 else ""
        warnings.append(f"no channel — set SLACK_STATUS_CHANNEL{hint}")

    if not settings.report_sheet_id:
        notes.append("no REPORT_SHEET_ID (use --from-file, or set it)")
    elif settings.google_credentials_file is None:
        return Result("track", FAIL, "REPORT_SHEET_ID is set but GOOGLE_CREDENTIALS_FILE is not")
    else:
        try:
            title, tabs = describe_spreadsheet(settings, settings.report_sheet_id)
        except ImportError:
            return Result(
                "track",
                FAIL,
                "the Google client libraries are not installed. In the repo, run: "
                "\".venv/bin/pip install -e '.[google]'\"",
            )
        except Exception as exc:  # noqa: BLE001 - auth, network, permissions
            detail = str(exc)
            if "404" in detail or "not found" in detail.lower():
                detail = (
                    "report not found. Either REPORT_SHEET_ID is wrong, or it has "
                    "not been shared with the service account (Reader is enough)."
                )
            return Result("track", FAIL, detail)

        latest = latest_month_tab(tabs)
        where = f"reads {title!r} ({len(tabs)} tabs)"
        if latest:
            where += f", latest --tab {latest}"
        notes.append(where)

    if warnings:
        return Result("track", WARN, "; ".join(warnings + notes))
    return Result("track", OK, "; ".join(notes))


def check_autodetect(settings) -> Result:  # noqa: ANN001
    """Can the bot actually see the channels it is meant to watch?

    A one-line read of each channel's history proves the id exists and the
    bot can read it -- which is the whole of what auto-detect needs. Added
    after a placeholder channel id went into .env and only surfaced as an
    error in the log after a restart.
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    channels = settings.autodetect_channel_ids
    if not channels:
        return Result("auto-detect", OK, "off (AUTODETECT_CHANNELS empty)")

    client = WebClient(token=settings.slack_bot_token)
    problems: list[str] = []
    for channel in channels:
        try:
            client.conversations_history(channel=channel, limit=1)
        except SlackApiError as exc:
            code = exc.response.get("error", "unknown")
            problems.append(f"{channel}: {CHANNEL_ERRORS.get(code, code)}")
        except Exception as exc:  # noqa: BLE001 - network, DNS, TLS
            return Result("auto-detect", FAIL, f"{channel}: {exc}")

    if problems:
        return Result("auto-detect", FAIL, "; ".join(problems))
    return Result("auto-detect", OK, f"watching {', '.join(channels)}")


def check_inbox(settings) -> Result:  # noqa: ANN001
    """Log in to the mailbox and select the folder. Reads nothing."""
    import imaplib

    from .inbox import why_disabled

    if not settings.imap_host and not settings.imap_user:
        return Result("email trigger", OK, "not configured (IMAP_HOST empty)")

    # Misconfiguration first: a mailbox that logs in fine but whose mail is
    # refused or unanswerable is the failure people find hardest to see.
    reason = why_disabled(settings)
    if reason:
        return Result("email trigger", FAIL, reason)

    detail = f"{settings.imap_user}@{settings.imap_host}:{settings.imap_port}"
    try:
        conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
        try:
            conn.login(settings.imap_user, settings.imap_password)
            typ, _ = conn.select(settings.imap_folder, readonly=True)
            if typ != "OK":
                return Result(
                    "email trigger", FAIL, f"{detail} — no folder named {settings.imap_folder!r}"
                )
        finally:
            conn.logout()
    except imaplib.IMAP4.error as exc:
        return Result(
            "email trigger",
            FAIL,
            f"{detail} — login rejected ({exc}). With Google Workspace this "
            "needs an app password and IMAP switched on in Gmail settings.",
        )
    except Exception as exc:  # noqa: BLE001 - network, DNS, TLS
        return Result("email trigger", FAIL, f"{detail} — {exc}")

    who = settings.imap_allowed_senders
    if not settings.email_enabled:
        return Result(
            "email trigger",
            WARN,
            f"{detail} — mail would start checks, but SMTP is off so nobody "
            "gets an answer. Set SMTP_HOST to complete the round trip.",
        )
    return Result(
        "email trigger",
        OK,
        f"{detail}, from {who} -> {settings.email_target_channel}",
    )


def check_sheet(settings) -> Result:  # noqa: ANN001
    """Can the bot reach the sheet, and is it the one it should be writing to?

    Two failures this catches before they happen. A service account that was
    never shared on the file fails with a 404 that reads like a wrong id, so
    the detail here says which of the two it is. And a GOOGLE_SHEET_ID left
    pointing at a hand-maintained report would have the bot add its tab to a
    live operational document -- harmless to the existing tabs, but not what
    anyone intended, and much easier to notice here than afterwards.
    """
    from .sheets import SheetExporter

    exporter = SheetExporter(settings)
    if not exporter.enabled:
        return Result("sheet", OK, "off (GOOGLE_SHEET_ID / GOOGLE_CREDENTIALS_FILE unset)")

    path = settings.google_credentials_file
    if path is None or not Path(path).is_file():
        return Result("sheet", FAIL, f"no credentials file at {path}")

    try:
        title, tabs = exporter.describe()
    except ImportError:
        # The Google client is an optional extra, so a sheet configured on a
        # plain install fails at the first import with a bare module name.
        return Result(
            "sheet",
            FAIL,
            "the Google client libraries are not installed. In the repo, run: "
            "\".venv/bin/pip install -e '.[google]'\" — then restart. "
            "(`make google` does the same inside an activated venv.)",
        )
    except Exception as exc:  # noqa: BLE001 - auth, network, permissions
        detail = str(exc)
        if "404" in detail or "not found" in detail.lower():
            detail = (
                "spreadsheet not found. Either GOOGLE_SHEET_ID is wrong, or the "
                "sheet has not been shared with the service account as Editor."
            )
        return Result("sheet", FAIL, detail)

    tab = settings.google_sheet_tab
    where = f"{title!r}, tab {tab!r}"

    if HAND_MAINTAINED.search(title):
        return Result(
            "sheet",
            WARN,
            f"{where} — that looks like a hand-maintained report. The bot only "
            "ever writes its own tab, but point it at the central sheet instead "
            "and pull from there with IMPORTRANGE (docs/CENTRAL-SHEET.md).",
        )
    if tab not in tabs:
        return Result("sheet", OK, f"{where} (created on first write)")
    return Result("sheet", OK, where)


def check_slack(settings) -> Result:  # noqa: ANN001
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    try:
        response = WebClient(token=settings.slack_bot_token).auth_test()
    except SlackApiError as exc:
        error = exc.response.get("error", "unknown")
        hint = {
            "invalid_auth": "the bot token is wrong or the app was uninstalled",
            "account_inactive": "the app was removed from the workspace",
        }.get(error, "check the token in .env")
        return Result("slack auth", FAIL, f"{error} — {hint}")
    except Exception as exc:  # noqa: BLE001 - network, DNS, proxy
        return Result("slack auth", FAIL, f"could not reach Slack: {exc}")

    return Result(
        "slack auth",
        OK,
        f"connected as {response.get('user')} in {response.get('team')}",
    )


def check_portground(settings, mawb: str) -> Result:  # noqa: ANN001
    from .errors import ApiUnavailable, LejCcError
    from .parser import parse_workbook
    from .portground import PortGroundClient

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with PortGroundClient(settings) as client:
                path, _ = client.download(mawb, Path(tmp))
            snapshot = parse_workbook(path, mawb)
        except LejCcError as exc:
            elapsed = time.monotonic() - started
            hint = ""
            if isinstance(exc, ApiUnavailable) and elapsed >= settings.http_timeout_seconds:
                hint = (
                    f" — it ran the full {settings.http_timeout_seconds:.0f}s timeout. "
                    "The export is slow to generate; raise HTTP_TIMEOUT_SECONDS in .env."
                )
            return Result(
                "portground api",
                FAIL,
                f"{type(exc).__name__} after {elapsed:.0f}s: {exc.user_message}{hint}",
            )
        except Exception as exc:  # noqa: BLE001
            return Result("portground api", FAIL, f"unexpected: {exc}")

    elapsed = time.monotonic() - started
    detail = (
        f"downloaded and parsed {mawb} in {elapsed:.0f}s: {snapshot.total:,} shipments, "
        f"{snapshot.percent:.0f}% cleared"
    )
    if snapshot.unknown_statuses:
        return Result(
            "portground api",
            WARN,
            f"{detail} — unrecognised statuses {snapshot.unknown_statuses}; "
            "add them to config/status_map.yaml",
        )
    return Result("portground api", OK, detail)


def run(mawb: str = SAMPLE_MAWB, *, offline: bool = False) -> list[Result]:
    settings_result, settings = check_settings()
    results = [settings_result]
    if settings is None:
        return results

    results += check_tokens(settings)
    results += check_storage(settings)
    results.append(check_status_map(settings))

    if offline:
        return results

    results.append(check_slack(settings))
    results.append(check_email(settings))
    results.append(check_sheet(settings))
    results.append(check_track(settings))
    results.append(check_autodetect(settings))
    results.append(check_inbox(settings))
    results.append(check_portground(settings, mawb))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that the bot is correctly configured")
    parser.add_argument(
        "--mawb", default=SAMPLE_MAWB, help=f"AWB to probe the API with (default {SAMPLE_MAWB})"
    )
    parser.add_argument("--offline", action="store_true", help="skip the Slack and API calls")
    args = parser.parse_args(argv)

    from . import version

    print(f"lej-cc-tool preflight — checked-out version {version.current()}\n")
    results = run(args.mawb, offline=args.offline)

    width = max(len(r.check) for r in results)
    for result in results:
        print(f"[{result.status}] {result.check.ljust(width)}  {result.detail}")

    failures = [r for r in results if r.failed]
    print()
    if failures:
        print(f"{len(failures)} check(s) failed. Fix the above, then run this again.")
        return 1
    print("All checks passed — start the bot and try /awb 488-20744846 in Slack.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
