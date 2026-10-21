"""Bounded live Router -> adapter -> Laya protocol qualification on Secure.

Uses observed deployment facts, not a model-quality or calibration dataset.
No token is needed: the loopback Router owns the adapter credential.
"""

import concurrent.futures
import hashlib
import json
import statistics
import time
import urllib.error
import urllib.request
from uuid import uuid4

ROUTER = "http://127.0.0.1:9420"
TUNNEL = "http://127.0.0.1:19421"
REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
QUESTIONS = {
    "deployment": {
        "type": "choice",
        "instructions": "Where is the adapter running, according to the state?",
        "criteria": {"kubernetes": "Kubernetes", "other": "A different platform"},
    },
    "inference": {
        "type": "choice",
        "instructions": "Which inference backend is in use, according to the state?",
        "criteria": {"laya": "Laya", "other": "Another backend"},
    },
}
STATE = {
    "observed_at": "2026-10-21",
    "adapter": "tof-prediction-adapter runs in the tof-jev Kubernetes namespace.",
    "backend": "Authenticated readiness reports Laya; the configured model is english.",
}


def request(base, path, payload=None, authorization=None):
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    data = None if payload is None else json.dumps(payload).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers)
    started = time.monotonic()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=100)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = json.loads(response.read(1_048_577))
        return response.status, response.headers, body, (time.monotonic() - started) * 1000


def main():
    checks = {}
    status, _, ready, _ = request(ROUTER, "/health/ready")
    assert status == 200 and ready == {"status": "ready", "backend": "laya", "synthetic": False}
    checks["laya_ready"] = status
    status, _, routes, _ = request(ROUTER, "/v1/routes?model=jev")
    assert status == 200 and routes["routes"][0]["dispatchable"]
    checks["configured_route"] = status
    status, _, profiles, _ = request(ROUTER, "/v1/profiles")
    assert status == 200 and "provider.progress.v1" in profiles["profiles"]
    checks["profile_discovery"] = status
    status, _, models, _ = request(ROUTER, "/v1/models")
    assert status == 200 and "jev" not in {model["id"] for model in models["data"]}
    checks["separate_native_contract"] = True

    for name, authorization in [("missing_auth", None), ("incorrect_auth", "Bearer incorrect")]:
        status, _, body, _ = request(TUNNEL, "/v1/profiles", authorization=authorization)
        assert status == 401 and body["error"]["code"] == "unauthorized"
        checks[name] = status

    errors = [
        ("invalid_schema", {"state": STATE}, 422, "validation_error"),
        (
            "unknown_profile",
            {"state": STATE, "decision_key": "not_a_profile.v1"},
            422,
            "unknown_profile",
        ),
        (
            "mock_disabled",
            {"state": STATE, "questions": QUESTIONS, "backend": "mock"},
            403,
            "backend_disabled",
        ),
        (
            "hosted_disabled",
            {"state": STATE, "questions": QUESTIONS, "backend": "jev"},
            403,
            "backend_disabled",
        ),
    ]
    for name, payload, expected_status, code in errors:
        status, headers, body, _ = request(ROUTER, "/v1/decisions", payload)
        assert status == expected_status and body["error"]["code"] == code, (name, status, body)
        assert body["request_id"] == headers["X-TOF-Upstream-Request-ID"]
        checks[name] = status
    status, _, _, _ = request(ROUTER, "/v1/decisions/batch", {"items": []})
    assert status == 404
    checks["batch_not_routed"] = status

    schema_hash = hashlib.sha256(
        json.dumps(QUESTIONS, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    def decide(index):
        caller_ref = f"router-qualification-{uuid4()}-{index}"
        payload = {
            "state": STATE,
            "questions": QUESTIONS,
            "job_run_id": caller_ref,
            "data_classification": "internal",
        }
        status, headers, body, elapsed = request(
            ROUTER, "/v1/decisions", payload, authorization="Bearer caller-token-must-be-replaced"
        )
        assert status == 200, (status, body)
        assert headers["X-TOF-Router-Route"] == "jev-decision"
        assert headers["X-TOF-Router-Backend"] == "tof-prediction-adapter"
        assert body["request_id"] == headers["X-TOF-Upstream-Request-ID"]
        assert body["request_id"] != headers["X-Request-ID"]
        assert body["job_run_id"] == caller_ref and body["schema_hash"] == schema_hash
        assert body["backend"] == "laya" and body["model"] == "english"
        assert body["model_revision"] == REVISION
        assert body["advisory_only"] is True and body["calibrated"] is False
        assert body["answers"]["deployment"]["value"] == "kubernetes"
        assert body["answers"]["inference"]["value"] == "laya"
        return {
            "router_request_id": headers["X-Request-ID"],
            "adapter_request_id": body["request_id"],
            "elapsed_ms": round(elapsed, 3),
            "adapter_ms": body["latency_ms"],
        }

    warmup = decide("warmup")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(decide, range(12)))
    assert len({result["adapter_request_id"] for result in results}) == 12
    status, _, progress, _ = request(
        ROUTER,
        "/v1/decisions",
        {
            "state": {
                "activity": [
                    "Established SSH transport",
                    "Verified Laya readiness",
                    "Completed 12 routed decisions",
                ]
            },
            "decision_key": "provider.progress.v1",
        },
    )
    assert status == 200 and progress["decision_key"] == "provider.progress.v1"
    assert progress["backend"] == "laya" and progress["advisory_only"] is True
    print(
        json.dumps(
            {
                "checks": checks,
                "concurrency": 4,
                "successful_decisions": 14,
                "model": "english",
                "revision": REVISION,
                "schema_hash": schema_hash,
                "warmup": warmup,
                "measured": results,
                "measured_p50_ms": round(statistics.median(r["elapsed_ms"] for r in results), 3),
                "measured_max_ms": max(r["elapsed_ms"] for r in results),
                "router_configuration_sha256": routes["configuration_sha256"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
