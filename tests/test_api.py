from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from queuepilot.api import create_app
from queuepilot.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "api.db", worker_enabled=False)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as session:
        yield session


def test_submit_normalizes_text_and_replays_idempotency(client):
    body = {"text": "  Hello durable backend.  "}
    first = client.post("/api/jobs", json=body, headers={"Idempotency-Key": "demo-1"})
    assert first.status_code == 202
    assert first.json()["text"] == "Hello durable backend."
    replay = client.post(
        "/api/jobs", json={"text": "Hello durable backend."}, headers={"Idempotency-Key": "demo-1"}
    )
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    conflict = client.post(
        "/api/jobs", json={"text": "Changed"}, headers={"Idempotency-Key": "demo-1"}
    )
    assert conflict.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"text": "  "},
        {"text": "a" * 20001},
        {"text": "a", "max_attempts": 0},
        {"text": "a", "max_attempts": 6},
        {"text": "a", "simulate_failures": 5},
        {"text": "a", "max_attempts": True},
        {"text": "a", "unknown": "field"},
    ],
)
def test_invalid_inputs_do_not_create_jobs(client, body):
    assert client.post("/api/jobs", json=body).status_code == 422
    assert client.get("/api/stats").json()["total"] == 0


def test_idempotency_key_must_be_bounded_nonempty_ascii(client):
    for key in ("", "a" * 129, "with spaces"):
        assert (
            client.post(
                "/api/jobs", json={"text": "Hello"}, headers={"Idempotency-Key": key}
            ).status_code
            == 422
        )


def test_invalid_unicode_json_returns_validation_error(client):
    response = client.post(
        "/api/jobs",
        content=b'{"text":"\\ud800"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert client.get("/api/stats").json()["total"] == 0


def test_job_details_events_cancel_and_bad_transitions(client):
    job = client.post("/api/jobs", json={"text": "Hello"}).json()
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "queued"
    events = client.get(f"/api/jobs/{job['id']}/events").json()["items"]
    assert events[0]["type"] == "submitted"
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 409
    assert client.get("/api/jobs/missing").status_code == 404
    assert client.get("/api/jobs/missing/events").status_code == 404


def test_valid_unicode_pair_and_chinese_text_are_accepted(client):
    response = client.post(
        "/api/jobs",
        content=b'{"text":"\\ud83d\\ude00"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 202 and response.json()["text"] == "😀"
    assert client.post("/api/jobs", json={"text": "后端任务。可靠恢复。"}).status_code == 202


def test_list_validates_pagination_and_status(client):
    for i in range(3):
        client.post("/api/jobs", json={"text": f"Job {i}"})
    page = client.get("/api/jobs?limit=2&offset=1").json()
    assert len(page["items"]) == 2 and page["total"] == 3
    for query in ("limit=0", "limit=101", "offset=-1", "status=unknown"):
        assert client.get("/api/jobs?" + query).status_code == 422


def test_api_key_protects_business_data_and_metrics(settings):
    with TestClient(create_app(replace(settings, api_key="local-test-key"))) as client:
        assert client.get("/healthz").status_code == 200
        for path in ("/api/jobs", "/api/stats", "/metrics"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
            assert client.get(path, headers={"X-API-Key": "local-test-key"}).status_code == 200
        assert client.post("/api/jobs", json={"text": "Hello"}).status_code == 401


def test_ollama_does_not_accept_simulated_failures(settings):
    with TestClient(create_app(replace(settings, provider="ollama"))) as client:
        assert (
            client.post("/api/jobs", json={"text": "Hello", "simulate_failures": 1}).status_code
            == 422
        )


def test_health_stats_and_metrics(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    client.post("/api/jobs", json={"text": "Hello"})
    stats = client.get("/api/stats").json()
    assert stats["counts"]["queued"] == 1 and stats["provider"] == "mock"
    metrics = client.get("/metrics")
    assert 'queuepilot_jobs{status="queued"} 1' in metrics.text
    assert metrics.headers["content-type"].startswith("text/plain")
