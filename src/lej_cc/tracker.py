"""One polling cycle for one AWB: fetch -> parse -> diff -> post -> reschedule.

Everything Slack-facing goes through the `Notifier` protocol so the whole
cycle can be tested without a Slack workspace.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from . import formatting
from .awb import format_display
from .config import Settings
from .errors import LejCcError
from .model import Snapshot, diff_snapshots
from .parser import StatusMapper, parse_workbook
from .portground import PortGroundClient
from .report import (
    build_open_shipments_workbook,
    open_shipments_filename,
)
from .store import Job, JobStore, utcnow

log = logging.getLogger(__name__)


class Notifier(Protocol):
    """The slice of Slack the tracker needs."""

    def post(
        self,
        channel: str,
        *,
        text: str,
        blocks: list[dict] | None = None,
        thread_ts: str | None = None,
        broadcast: bool = False,
    ) -> str | None: ...

    def upload(
        self,
        channel: str,
        path: Path,
        *,
        filename: str,
        title: str,
        thread_ts: str | None = None,
        comment: str | None = None,
    ) -> None: ...


class Tracker:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        client: PortGroundClient,
        notifier: Notifier,
        mapper: StatusMapper | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.notifier = notifier
        self.mapper = mapper or StatusMapper.load(settings.status_map_path)

    # --- scheduling policy ---------------------------------------------

    def next_run(self, job: Job, *, failed: bool = False) -> datetime:
        """First gap is 15 minutes, then every 30; failures back off ×2 (capped)."""
        if failed:
            minutes = min(
                self.settings.repeat_interval_minutes * 2 ** min(job.consecutive_failures, 3),
                240,
            )
        elif job.is_first_poll:
            minutes = self.settings.first_interval_minutes
        else:
            minutes = self.settings.repeat_interval_minutes
        return utcnow() + timedelta(minutes=minutes)

    # --- the cycle ------------------------------------------------------

    def run_once(self, job: Job) -> None:
        """Poll one job. Never raises: every failure ends as a Slack message."""
        try:
            self._poll(job)
        except LejCcError as exc:
            self._handle_error(job, exc)
        except Exception as exc:  # noqa: BLE001 - a bug must not kill the scheduler
            log.exception("unhandled error polling %s", job.mawb)
            self._handle_error(
                job,
                LejCcError(
                    str(exc),
                    user_message="Something went wrong on our side while checking this AWB.",
                ),
            )

    def _poll(self, job: Job) -> None:
        path, server_name = self.client.download(job.mawb, self.settings.download_dir)
        snapshot = parse_workbook(
            path,
            job.mawb,
            mapper=self.mapper,
            source_filename=server_name,
            fetched_at=utcnow(),
        )

        diff = diff_snapshots(job.last_status_map, snapshot)
        self.store.record_snapshot(job.id, snapshot)

        timed_out = job.age() > timedelta(hours=self.settings.max_tracking_hours)
        is_final = snapshot.is_complete or timed_out
        next_run_at = None if is_final else self.next_run(job)

        self._post_update(job, snapshot, diff, next_run_at, is_final=is_final, path=path)

        if snapshot.is_complete:
            self.store.finish(job.id, "complete", "100% cleared")
        elif timed_out:
            self.store.finish(
                job.id, "timeout", f"still {snapshot.percent:.0f}% after "
                f"{self.settings.max_tracking_hours}h"
            )
        else:
            self.store.reschedule(
                job.id,
                next_run_at,  # type: ignore[arg-type]
                status_map=snapshot.status_by_hawb(),
                percent=snapshot.percent,
                cleared=snapshot.cleared,
                total=snapshot.total,
                reset_failures=True,
            )

    def _post_update(
        self,
        job: Job,
        snapshot: Snapshot,
        diff,  # noqa: ANN001
        next_run_at: datetime | None,
        *,
        is_final: bool,
        path: Path,
    ) -> None:
        changed = diff.has_changes or job.is_first_poll
        settings = self.settings

        # Nothing moved and the operator asked for quiet updates: one short line.
        if not changed and not is_final and not settings.post_unchanged_updates:
            log.info("%s unchanged at %.1f%%, staying quiet", job.mawb, snapshot.percent)
            return

        blocks = formatting.build_status_blocks(
            snapshot,
            diff=diff,
            next_run_at=next_run_at,
            requested_by=job.requested_by,
            inline_threshold=settings.inline_list_threshold,
            is_final=is_final,
            poll_count=job.poll_count + 1,
        )
        text = formatting.summary_line(snapshot)

        if job.is_first_poll and not job.thread_ts:
            # First message goes to the channel and becomes the thread root.
            ts = self.notifier.post(job.channel_id, text=text, blocks=blocks)
            if ts:
                self.store.set_thread(job.id, ts)
                job.thread_ts = ts
        elif not changed:
            self.notifier.post(
                job.channel_id,
                text=formatting.unchanged_text(snapshot, next_run_at),
                thread_ts=job.thread_ts,
            )
            return
        else:
            # Completion is broadcast back to the channel; routine updates stay
            # in the thread so the channel does not fill up.
            self.notifier.post(
                job.channel_id,
                text=text,
                blocks=blocks,
                thread_ts=job.thread_ts,
                broadcast=is_final,
            )

        if not self._should_attach(job, changed=changed, is_final=is_final):
            return

        # The chase sheet goes first: it is the one people actually open.
        if settings.attach_open_summary:
            self._upload_chase_sheet(job, snapshot)

        if settings.attach_full_workbook:
            self.notifier.upload(
                job.channel_id,
                path,
                filename=snapshot.source_filename or path.name,
                title=f"{format_display(job.mawb)} — full export from PortGround",
                thread_ts=job.thread_ts,
            )

    def _upload_chase_sheet(self, job: Job, snapshot: Snapshot) -> None:
        try:
            sheet = build_open_shipments_workbook(snapshot, self.settings.download_dir)
        except Exception:  # noqa: BLE001 - a report bug must not lose the update
            log.exception("could not build the chase sheet for %s", job.mawb)
            return
        if sheet is None:
            return  # nothing open: the full export says everything there is
        self.notifier.upload(
            job.channel_id,
            sheet,
            filename=open_shipments_filename(snapshot),
            title=f"{format_display(job.mawb)} — {len(snapshot.open_rows):,} still open",
            thread_ts=job.thread_ts,
        )

    def _should_attach(self, job: Job, *, changed: bool, is_final: bool) -> bool:
        if job.is_first_poll or is_final:
            return True
        return changed or not self.settings.attach_file_on_change_only

    # --- failure handling -----------------------------------------------

    def _handle_error(self, job: Job, exc: LejCcError) -> None:
        from .errors import ApiUnauthorized, AwbNotFound, MawbMismatch, SchemaDrift

        log.warning("job %s (%s) error: %s", job.id, job.mawb, exc)

        # An AWB PortGround has never heard of is reported at once rather than
        # polled for hours -- that is the case operators hit with a typo.
        if isinstance(exc, AwbNotFound) and job.is_first_poll:
            self._report(
                job,
                f"{exc.user_message}\n_If it was only just filed, try `/awb "
                f"{format_display(job.mawb)}` again in a few minutes._",
                fatal=True,
            )
            self.store.finish(job.id, "not_found", "AWB unknown to PortGround")
            return

        if isinstance(exc, AwbNotFound):
            if job.empty_polls + 1 >= self.settings.empty_result_grace_polls:
                self._report(job, "The AWB has stopped returning any shipments.", fatal=True)
                self.store.finish(job.id, "not_found", "AWB disappeared from the export")
                return
            self.store.reschedule(job.id, self.next_run(job), increment_empty=True)
            return

        if isinstance(exc, (SchemaDrift, MawbMismatch)):
            self._report(job, exc.user_message, fatal=True)
            self.store.finish(job.id, "failed", type(exc).__name__)
            self._alert_ops(f"*{type(exc).__name__}* on {format_display(job.mawb)}: {exc}")
            return

        failures = job.consecutive_failures + 1
        if isinstance(exc, ApiUnauthorized):
            self._alert_ops(f"PortGround rejected the API key: {exc}")

        if failures >= self.settings.max_consecutive_failures:
            self._report(
                job,
                f"{exc.user_message}\nGiving up after {failures} consecutive failures.",
                fatal=True,
            )
            self.store.finish(job.id, "failed", f"{type(exc).__name__} ×{failures}")
            return

        # Stay quiet on the first blip; report once it looks persistent.
        if failures >= 3:
            self._report(job, exc.user_message, fatal=False)
        self.store.reschedule(
            job.id,
            self.next_run(job, failed=True),
            increment_poll=False,
            increment_failure=True,
        )

    def _report(self, job: Job, message: str, *, fatal: bool) -> None:
        self.notifier.post(
            job.channel_id,
            text=f"{format_display(job.mawb)}: {message}",
            blocks=formatting.build_error_blocks(job.mawb, message, fatal=fatal),
            thread_ts=job.thread_ts,
            broadcast=fatal and bool(job.thread_ts),
        )

    def _alert_ops(self, message: str) -> None:
        channel = self.settings.slack_ops_channel
        if channel:
            self.notifier.post(channel, text=f":rotating_light: lej-cc-tool: {message}")
