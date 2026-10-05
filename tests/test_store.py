from concurrent.futures import ThreadPoolExecutor

import pytest

from queuepilot.store import Conflict, NotFound, Store


@pytest.fixture
def clock():
    class Clock:
        value = 1000.0

        def __call__(self):
            return self.value

    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "jobs.db", clock=clock)


def payload(**overrides):
    return {
        "text": "Learn durable jobs. Recover safely.",
        "max_attempts": 3,
        "simulate_failures": 0,
        **overrides,
    }


def test_idempotency_replays_original_job_and_rejects_changed_request(store):
    first, created = store.submit(payload(), "same-request")
    second, repeated = store.submit(payload(), "same-request")
    assert created is True and repeated is False
    assert first["id"] == second["id"]
    with pytest.raises(Conflict):
        store.submit(payload(text="Different input"), "same-request")
    assert store.stats()["total"] == 1


def test_concurrent_idempotency_creates_one_job(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.submit(payload(), "concurrent"), range(8)))
    assert len({job["id"] for job, _ in results}) == 1
    assert sum(created for _, created in results) == 1


def test_claim_is_atomic_between_workers(store):
    job, _ = store.submit(payload())
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: store.claim(), range(8)))
    winners = [item for item in claimed if item]
    assert len(winners) == 1
    assert winners[0]["id"] == job["id"]
    assert winners[0]["attempt"] == 1
    assert "lease_token" not in store.get(job["id"])


def test_retry_backoff_and_attempt_limit(store, clock):
    job, _ = store.submit(payload(max_attempts=2))
    claim = store.claim()
    assert store.fail(job["id"], claim["lease_token"], "provider_unavailable")
    pending = store.get(job["id"])
    assert pending["status"] == "retry_wait" and pending["available_at"] == 1002
    assert store.claim() is None
    clock.value = 1002
    second = store.claim()
    assert second["attempt"] == 2
    assert store.fail(job["id"], second["lease_token"], "provider_unavailable")
    assert store.get(job["id"])["status"] == "failed"
    assert store.claim() is None


def test_success_persists_result_and_event_history(store):
    job, _ = store.submit(payload())
    claim = store.claim()
    assert store.complete(job["id"], claim["lease_token"], {"summary": "Done"})
    assert store.get(job["id"])["result"] == {"summary": "Done"}
    assert [event["type"] for event in store.events(job["id"])] == [
        "submitted",
        "started",
        "succeeded",
    ]
    events = store.events(job["id"])
    assert [event["type"] for event in store.events(job["id"], after=events[1]["id"])] == [
        "succeeded"
    ]


def test_expired_lease_rejects_result_even_before_recovery(store, clock):
    job, _ = store.submit(payload())
    claim = store.claim(lease_seconds=5)
    clock.value = 1005
    assert not store.complete(job["id"], claim["lease_token"], {"summary": "Late"})
    assert not store.fail(job["id"], claim["lease_token"], "late_error")
    assert store.get(job["id"])["status"] == "running"


def test_recovery_fences_previous_worker(store, clock):
    job, _ = store.submit(payload())
    old = store.claim(lease_seconds=5)
    clock.value = 1006
    assert store.recover_expired() == 1
    assert store.get(job["id"])["status"] == "retry_wait"
    clock.value = 1008
    current = store.claim()
    assert current["lease_token"] != old["lease_token"]
    assert not store.complete(job["id"], old["lease_token"], {"summary": "Stale"})
    assert store.complete(job["id"], current["lease_token"], {"summary": "Current"})
    assert store.get(job["id"])["result"]["summary"] == "Current"


def test_expired_final_attempt_becomes_failed(store, clock):
    job, _ = store.submit(payload(max_attempts=1))
    store.claim(lease_seconds=1)
    clock.value = 1001
    store.recover_expired()
    assert store.get(job["id"])["status"] == "failed"


def test_manual_retry_preserves_history_and_fences_old_token(store):
    job, _ = store.submit(payload(max_attempts=1))
    old = store.claim()
    store.fail(job["id"], old["lease_token"], "simulated_failure")
    assert store.retry(job["id"])["attempt"] == 0
    current = store.claim()
    assert not store.complete(job["id"], old["lease_token"], {})
    assert store.complete(job["id"], current["lease_token"], {})
    assert store.stats()["total_attempts"] == 2
    assert "retried" in [event["type"] for event in store.events(job["id"])]


def test_cancellation_and_invalid_transitions(store):
    queued, _ = store.submit(payload())
    assert store.cancel(queued["id"])["status"] == "cancelled"
    assert store.claim() is None
    with pytest.raises(Conflict):
        store.retry(queued["id"])
    running, _ = store.submit(payload())
    store.claim()
    with pytest.raises(Conflict):
        store.cancel(running["id"])
    with pytest.raises(Conflict):
        store.retry(running["id"])
    with pytest.raises(NotFound):
        store.get("missing")


def test_restart_keeps_jobs_idempotency_and_events(tmp_path, clock):
    path = tmp_path / "persistent.db"
    first = Store(path, clock=clock)
    job, _ = first.submit(payload(), "persist")
    first.claim()
    reopened = Store(path, clock=clock)
    assert reopened.get(job["id"])["status"] == "running"
    replay, created = reopened.submit(payload(), "persist")
    assert replay["id"] == job["id"] and not created
    assert len(reopened.events(job["id"])) == 2


def test_list_filters_paginates_and_counts(store, clock):
    first, _ = store.submit(payload())
    clock.value += 1
    second, _ = store.submit(payload())
    store.cancel(first["id"])
    page = store.list_jobs(limit=1)
    assert page["total"] == 2 and page["items"][0]["id"] == second["id"]
    assert store.list_jobs(limit=1, offset=1)["items"][0]["id"] == first["id"]
    assert store.list_jobs(status="queued")["total"] == 1
    assert store.stats()["counts"]["cancelled"] == 1
