"""Preflight checks.

Run this after deploying, before wondering why nothing happens. Each check
either passes or explains precisely what to fix, so a failure is a fix
instruction rather than a stack trace.

    lej-cc-doctor              # or: docker compose run --rm lej-cc-tool lej-cc-doctor

Nothing here prints a secret: tokens are shown as a prefix and a length.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: PortGround's own documented example, used as a live end-to-end probe.
SAMPLE_MAWB = "48820744846"

OK, FAIL, WARN = "  ok  ", " FAIL ", " warn "


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
    from .errors import LejCcError
    from .parser import parse_workbook
    from .portground import PortGroundClient

    with tempfile.TemporaryDirectory() as tmp:
        try:
            with PortGroundClient(settings) as client:
                path, _ = client.download(mawb, Path(tmp))
            snapshot = parse_workbook(path, mawb)
        except LejCcError as exc:
            return Result("portground api", FAIL, f"{type(exc).__name__}: {exc.user_message}")
        except Exception as exc:  # noqa: BLE001
            return Result("portground api", FAIL, f"unexpected: {exc}")

    detail = (
        f"downloaded and parsed {mawb}: {snapshot.total:,} shipments, "
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
    results.append(check_portground(settings, mawb))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that the bot is correctly configured")
    parser.add_argument(
        "--mawb", default=SAMPLE_MAWB, help=f"AWB to probe the API with (default {SAMPLE_MAWB})"
    )
    parser.add_argument("--offline", action="store_true", help="skip the Slack and API calls")
    args = parser.parse_args(argv)

    print("lej-cc-tool preflight\n")
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
