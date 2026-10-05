"""Verify a running mock server without any external model or API key."""

import argparse
import json
import os
import time
import uuid

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    headers = {}
    if key := os.getenv("QUEUEPILOT_API_KEY"):
        headers["X-API-Key"] = key
    with httpx.Client(base_url=args.url, headers=headers, timeout=5, trust_env=False) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/api/stats").json()["provider"] == "mock", "Use mock mode for this demo"
        text = "后台任务先保存到数据库。Worker 负责执行，失败后自动重试。"
        request = {"text": text, "max_attempts": 3, "simulate_failures": 0}
        key_headers = {"Idempotency-Key": "smoke-" + uuid.uuid4().hex}
        response = client.post("/api/jobs", json=request, headers=key_headers)
        assert response.status_code == 202
        normal = response.json()
        replay = client.post("/api/jobs", json=request, headers=key_headers)
        assert replay.status_code == 200 and replay.json()["id"] == normal["id"]
        conflict = client.post(
            "/api/jobs", json={**request, "text": "不同内容"}, headers=key_headers
        )
        assert conflict.status_code == 409
        retrying = client.post("/api/jobs", json={**request, "simulate_failures": 1}).json()
        failing = client.post(
            "/api/jobs", json={**request, "max_attempts": 2, "simulate_failures": 2}
        ).json()
        jobs = [normal, retrying, failing]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            jobs = [client.get("/api/jobs/" + job["id"]).json() for job in jobs]
            if all(job["status"] in ("succeeded", "failed") for job in jobs):
                break
            time.sleep(0.2)
        assert [job["status"] for job in jobs] == ["succeeded", "succeeded", "failed"]
        assert [job["attempt"] for job in jobs] == [1, 2, 2]
        history = client.get(f"/api/jobs/{retrying['id']}/events").json()["items"]
        assert [event["type"] for event in history] == [
            "submitted",
            "started",
            "attempt_failed",
            "started",
            "succeeded",
        ]
        for path in (
            "/",
            "/static/app.js",
            "/static/style.css",
            "/docs",
            "/openapi.json",
            "/metrics",
        ):
            assert client.get(path).status_code == 200, path
        print(
            json.dumps(
                {
                    "health": "ok",
                    "idempotency": "ok",
                    "conflict": "ok",
                    "jobs": [{"status": job["status"], "attempt": job["attempt"]} for job in jobs],
                    "event_history": "ok",
                    "web_assets": "ok",
                },
                ensure_ascii=True,
            )
        )


if __name__ == "__main__":
    main()
