from datetime import timedelta

from lej_cc.store import JobStore, utcnow


def make_store(tmp_path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3")


def test_create_and_find(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job("48820744846", "C1", "U1")
    assert job is not None
    assert store.find_active("48820744846", "C1").id == job.id


def test_duplicate_in_same_channel_is_refused(tmp_path):
    store = make_store(tmp_path)
    store.create_job("48820744846", "C1", "U1")
    assert store.create_job("48820744846", "C1", "U2") is None


def test_same_awb_in_another_channel_is_allowed(tmp_path):
    store = make_store(tmp_path)
    store.create_job("48820744846", "C1", "U1")
    assert store.create_job("48820744846", "C2", "U1") is not None


def test_finished_job_frees_the_slot(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job("48820744846", "C1", "U1")
    store.finish(job.id, "complete", "done")
    assert store.create_job("48820744846", "C1", "U1") is not None


def test_claim_due_only_returns_due_jobs(tmp_path):
    store = make_store(tmp_path)
    now = utcnow()
    due = store.create_job("48820744846", "C1", "U1", run_at=now - timedelta(minutes=1))
    store.create_job("93600333955", "C1", "U1", run_at=now + timedelta(minutes=15))

    claimed = store.claim_due()
    assert [j.id for j in claimed] == [due.id]


def test_claim_leases_so_a_job_is_not_polled_twice(tmp_path):
    """Two scheduler ticks overlapping must not double-poll the same AWB."""
    store = make_store(tmp_path)
    store.create_job("48820744846", "C1", "U1")
    assert len(store.claim_due()) == 1
    assert store.claim_due() == []


def test_parallel_jobs_keep_independent_clocks(tmp_path):
    """The 10:00 AWB and the 10:15 AWB stay 15 minutes apart across restarts."""
    store = make_store(tmp_path)
    now = utcnow()
    first = store.create_job("48820744846", "C1", "U1", run_at=now)
    store.create_job("93600333955", "C1", "U1", run_at=now + timedelta(minutes=15))

    store.reschedule(first.id, now + timedelta(minutes=15), percent=50.0, cleared=5, total=10)

    reloaded = JobStore(tmp_path / "jobs.sqlite3")  # simulate a process restart
    jobs = {j.mawb: j for j in reloaded.list_active()}
    assert jobs["48820744846"].last_percent == 50.0
    assert jobs["48820744846"].poll_count == 1
    assert jobs["93600333955"].poll_count == 0
    assert jobs["48820744846"].next_run_at <= jobs["93600333955"].next_run_at


def test_status_map_round_trips(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job("48820744846", "C1", "U1")
    store.reschedule(job.id, utcnow(), status_map={"a": "cleared"})
    assert store.get(job.id).last_status_map == {"a": "cleared"}
