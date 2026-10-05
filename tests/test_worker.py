import json
from dataclasses import replace

import httpx
import pytest

from queuepilot.config import Settings
from queuepilot.providers import ProviderError, execute
from queuepilot.store import Store
from queuepilot.worker import Worker


def test_worker_completes_mock_job(tmp_path):
    store = Store(tmp_path / "jobs.db")
    job, _ = store.submit(
        {
            "text": "First sentence. Second sentence. Third sentence.",
            "max_attempts": 3,
            "simulate_failures": 0,
        }
    )
    worker = Worker(store, Settings(db_path=store.path))
    assert worker.run_once()
    done = store.get(job["id"])
    assert done["status"] == "succeeded"
    assert done["result"] == {
        "summary": "First sentence. Second sentence.",
        "provider": "mock",
        "characters": 48,
    }
    assert not worker.run_once()


def test_worker_retries_and_preserves_failure_history(tmp_path):
    now = [1000.0]
    store = Store(tmp_path / "jobs.db", clock=lambda: now[0])
    job, _ = store.submit({"text": "Hello world.", "max_attempts": 3, "simulate_failures": 1})
    worker = Worker(store, Settings(db_path=store.path))
    worker.run_once()
    assert store.get(job["id"])["error"] == "simulated_failure"
    assert not worker.run_once()
    now[0] = 1002
    worker.run_once()
    done = store.get(job["id"])
    assert done["status"] == "succeeded" and done["attempt"] == 2
    assert done["error"] is None
    assert [item["type"] for item in store.events(job["id"])] == [
        "submitted",
        "started",
        "attempt_failed",
        "started",
        "succeeded",
    ]


def test_worker_does_not_record_raw_exception_secrets(tmp_path):
    store = Store(tmp_path / "jobs.db")
    job, _ = store.submit({"text": "Hello", "max_attempts": 1, "simulate_failures": 0})

    def broken_executor(job, settings):
        raise RuntimeError("secret-value-in-upstream-error")

    worker = Worker(store, Settings(db_path=store.path), executor=broken_executor)
    assert worker.run_once()
    assert store.get(job["id"])["error"] == "unexpected_error"
    assert "secret-value" not in json.dumps(store.events(job["id"]))


def test_worker_recovers_expired_claim_before_processing(tmp_path):
    now = [1000.0]
    store = Store(tmp_path / "jobs.db", clock=lambda: now[0])
    job, _ = store.submit({"text": "Hello", "max_attempts": 1, "simulate_failures": 0})
    store.claim(lease_seconds=1)
    now[0] = 1002
    worker = Worker(store, Settings(db_path=store.path))
    assert not worker.run_once()
    assert store.get(job["id"])["status"] == "failed"


def ollama_job():
    return {"text": "A useful article.", "attempt": 1, "simulate_failures": 0}


def test_ollama_uses_configured_endpoint_and_non_streaming_protocol():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"response": "A concise summary.", "done": True})

    settings = replace(Settings(), provider="ollama", ollama_model="test-model")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = execute(ollama_job(), settings, client=client)
    assert result["summary"] == "A concise summary." and result["provider"] == "ollama"
    assert str(seen[0].url) == "http://127.0.0.1:11434/api/generate"
    body = json.loads(seen[0].content)
    assert body["model"] == "test-model" and body["stream"] is False
    assert "A useful article." in body["prompt"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="secret-upstream-body"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"response": ""}),
        httpx.Response(200, json={"response": 123}),
        httpx.Response(200, text='{"response":"\\ud800"}'),
    ],
)
def test_ollama_rejects_bad_responses_without_exposing_body(response):
    transport = httpx.MockTransport(lambda request: response)
    with httpx.Client(transport=transport) as client, pytest.raises(ProviderError) as exc:
        execute(ollama_job(), replace(Settings(), provider="ollama"), client=client)
    assert "secret" not in str(exc.value)


def test_ollama_timeout_maps_to_safe_error():
    def handler(request):
        raise httpx.ReadTimeout("secret-upstream-error", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="provider_timeout"):
            execute(ollama_job(), replace(Settings(), provider="ollama"), client=client)


def test_settings_reject_unsafe_lease_and_invalid_environment(monkeypatch):
    with pytest.raises(ValueError):
        Settings(lease_seconds=30)
    monkeypatch.setenv("QUEUEPILOT_WORKER_ENABLED", "maybe")
    with pytest.raises(ValueError):
        Settings.from_env()
