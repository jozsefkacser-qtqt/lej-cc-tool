"""Durable job store (SQLite).

Each tracked AWB is one row with a `next_run_at`. The scheduler claims due
rows, polls them, and writes the next due time back. Keeping the schedule in
the database rather than in memory is what makes the bot restart-safe: if
the process dies at 02:00, every AWB resumes on its own clock, and two AWBs
started 15 minutes apart stay 15 minutes apart.

WAL mode plus a short lease on each claim means a second process would not
double-poll a job, so this scales to more than one worker without changing
the design.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    mawb                  TEXT    NOT NULL,
    channel_id            TEXT    NOT NULL,
    thread_ts             TEXT,
    requested_by          TEXT,
    state                 TEXT    NOT NULL DEFAULT 'active',
    created_at            TEXT    NOT NULL,
    next_run_at           TEXT    NOT NULL,
    leased_until          TEXT,
    poll_count            INTEGER NOT NULL DEFAULT 0,
    empty_polls           INTEGER NOT NULL DEFAULT 0,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_status_map       TEXT,
    last_percent          REAL,
    last_cleared          INTEGER,
    last_total            INTEGER,
    last_polled_at        TEXT,
    finished_at           TEXT,
    finish_reason         TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_due   ON jobs (state, next_run_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active
    ON jobs (mawb, channel_id) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL REFERENCES jobs(id),
    mawb         TEXT    NOT NULL,
    taken_at     TEXT    NOT NULL,
    generated_at TEXT,
    total        INTEGER NOT NULL,
    cleared      INTEGER NOT NULL,
    not_cleared  INTEGER NOT NULL,
    other        INTEGER NOT NULL,
    items_total  INTEGER NOT NULL DEFAULT 0,
    items_cleared INTEGER NOT NULL DEFAULT 0,
    percent      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_job ON snapshots (job_id, taken_at);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Job:
    id: int
    mawb: str
    channel_id: str
    thread_ts: str | None
    requested_by: str | None
    state: str
    created_at: datetime
    next_run_at: datetime
    poll_count: int = 0
    empty_polls: int = 0
    consecutive_failures: int = 0
    last_status_map: dict[str, str] | None = None
    last_percent: float | None = None
    last_cleared: int | None = None
    last_total: int | None = None
    last_polled_at: datetime | None = None
    finish_reason: str | None = None

    @property
    def is_first_poll(self) -> bool:
        return self.poll_count == 0

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or utcnow()) - self.created_at

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            mawb=row["mawb"],
            channel_id=row["channel_id"],
            thread_ts=row["thread_ts"],
            requested_by=row["requested_by"],
            state=row["state"],
            created_at=_dt(row["created_at"]),  # type: ignore[arg-type]
            next_run_at=_dt(row["next_run_at"]),  # type: ignore[arg-type]
            poll_count=row["poll_count"],
            empty_polls=row["empty_polls"],
            consecutive_failures=row["consecutive_failures"],
            last_status_map=json.loads(row["last_status_map"]) if row["last_status_map"] else None,
            last_percent=row["last_percent"],
            last_cleared=row["last_cleared"],
            last_total=row["last_total"],
            last_polled_at=_dt(row["last_polled_at"]),
            finish_reason=row["finish_reason"],
        )


class JobStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- lifecycle ------------------------------------------------------

    def create_job(
        self,
        mawb: str,
        channel_id: str,
        requested_by: str | None,
        *,
        thread_ts: str | None = None,
        run_at: datetime | None = None,
    ) -> Job | None:
        """Insert a tracking job. Returns None if this AWB is already tracked here."""
        now = utcnow()
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO jobs (mawb, channel_id, thread_ts, requested_by, state,"
                    " created_at, next_run_at) VALUES (?,?,?,?, 'active', ?, ?)",
                    (mawb, channel_id, thread_ts, requested_by, _iso(now), _iso(run_at or now)),
                )
            except sqlite3.IntegrityError:
                return None  # unique index on (mawb, channel_id) where active
            return self.get(cur.lastrowid)  # type: ignore[arg-type]

    def get(self, job_id: int) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def find_active(self, mawb: str, channel_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE mawb = ? AND channel_id = ? AND state = 'active'",
            (mawb, channel_id),
        ).fetchone()
        return Job.from_row(row) if row else None

    def list_active(self, channel_id: str | None = None) -> list[Job]:
        sql = "SELECT * FROM jobs WHERE state = 'active'"
        params: tuple = ()
        if channel_id:
            sql += " AND channel_id = ?"
            params = (channel_id,)
        sql += " ORDER BY created_at"
        return [Job.from_row(r) for r in self._conn.execute(sql, params)]

    # --- scheduling -----------------------------------------------------

    def claim_due(self, limit: int = 20, lease_seconds: int = 1800) -> list[Job]:
        """Atomically take ownership of jobs whose next_run_at has passed.

        The lease must outlast the slowest possible poll. PortGround can take
        minutes to generate a workbook, and the client retries once, so a
        single poll can legitimately run for ten minutes; a shorter lease
        would let the next tick start a second poll of the same AWB.
        """
        now = utcnow()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE state = 'active' AND next_run_at <= ?"
                " AND (leased_until IS NULL OR leased_until <= ?)"
                " ORDER BY next_run_at LIMIT ?",
                (_iso(now), _iso(now), limit),
            ).fetchall()
            jobs = [Job.from_row(r) for r in rows]
            if jobs:
                lease = _iso(now + timedelta(seconds=lease_seconds))
                self._conn.executemany(
                    "UPDATE jobs SET leased_until = ? WHERE id = ?",
                    [(lease, j.id) for j in jobs],
                )
            return jobs

    def reschedule(
        self,
        job_id: int,
        next_run_at: datetime,
        *,
        status_map: dict[str, str] | None = None,
        percent: float | None = None,
        cleared: int | None = None,
        total: int | None = None,
        reset_failures: bool = False,
        increment_poll: bool = True,
        increment_empty: bool = False,
        increment_failure: bool = False,
    ) -> None:
        sets = ["next_run_at = ?", "leased_until = NULL", "last_polled_at = ?"]
        params: list = [_iso(next_run_at), _iso(utcnow())]

        if increment_poll:
            sets.append("poll_count = poll_count + 1")
        if increment_empty:
            sets.append("empty_polls = empty_polls + 1")
        else:
            sets.append("empty_polls = 0")
        if increment_failure:
            sets.append("consecutive_failures = consecutive_failures + 1")
        elif reset_failures:
            sets.append("consecutive_failures = 0")
        if status_map is not None:
            sets.append("last_status_map = ?")
            params.append(json.dumps(status_map))
        for column, value in (
            ("last_percent", percent),
            ("last_cleared", cleared),
            ("last_total", total),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)

        params.append(job_id)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)

    def finish(self, job_id: int, state: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET state = ?, finish_reason = ?, finished_at = ?,"
                " leased_until = NULL WHERE id = ?",
                (state, reason, _iso(utcnow()), job_id),
            )
        log.info("job %s finished: %s (%s)", job_id, state, reason)

    def set_thread(self, job_id: int, thread_ts: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET thread_ts = ? WHERE id = ?", (thread_ts, job_id))

    # --- history (feeds later lead-time analytics) ----------------------

    def record_snapshot(self, job_id: int, snapshot) -> None:  # noqa: ANN001
        with self._lock:
            self._conn.execute(
                "INSERT INTO snapshots (job_id, mawb, taken_at, generated_at, total, cleared,"
                " not_cleared, other, items_total, items_cleared, percent)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    snapshot.mawb,
                    _iso(utcnow()),
                    _iso(snapshot.generated_at),
                    snapshot.total,
                    snapshot.cleared,
                    snapshot.not_cleared,
                    snapshot.other,
                    snapshot.items_total,
                    snapshot.items_cleared,
                    snapshot.percent,
                ),
            )
