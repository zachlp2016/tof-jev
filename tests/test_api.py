"""Exercise the ASGI boundary and backend policy without external inference."""

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from tof_prediction_adapter.app import create_app
from tof_prediction_adapter.config import Settings

SERVICE_KEY = "test-service-key-with-at-least-24-characters"
AUTH = {"Authorization": f"Bearer {SERVICE_KEY}"}
PRIVATE = "PRIVATE_STATE_DO_NOT_ECHO_931"
QUESTION = {"relevant": {"type": "noul", "instructions": "Is this relevant?"}}


def decision(**overrides):
    return {"state": "A useful source", "questions": QUESTION, **overrides}


def answer(probability=0.8):
    return {
        "model": "laya-rl-agent",
        "answers": {"relevant": {"type": "noul", "noul": probability}},
        "usage": {"input_tokens": 20, "output_tokens": 0},
        "routing": {"model": "english"},
    }


@asynccontextmanager
async def api(*, handler=None, **overrides):
    def no_network(request):
        raise AssertionError(f"Unexpected upstream request to {request.url.host}")

    values = {"api_keys": [SERVICE_KEY], "default_backend": "mock", "allow_mock": True}
    app = create_app(
        Settings(**(values | overrides)), transport=httpx.MockTransport(handler or no_network)
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield client


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/decisions"),
        ("POST", "/v1/decisions/batch"),
        ("POST", "/v1/calculations"),
        ("GET", "/v1/profiles"),
        ("GET", "/v1/backends"),
        ("GET", "/docs"),
        ("GET", "/redoc"),
        ("GET", "/openapi.json"),
        ("GET", "/metrics"),
        ("POST", "/mcp"),
        ("POST", "/mcp/"),
    ],
)
@pytest.mark.parametrize("authorization", [None, "Bearer incorrect-service-key"])
async def test_auth_boundary_covers_rest_mcp_docs_and_metrics(method, path, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    async with api() as client:
        response = await client.request(method, path, json={"state": PRIVATE}, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert PRIVATE not in response.text


async def test_health_is_public_and_mock_readiness_is_explicit():
    async with api() as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
    assert live.status_code == ready.status_code == 200
    assert live.json()["status"] == "ok"
    assert ready.json() == {"status": "ready", "backend": "mock", "synthetic": True}


async def test_rotating_service_keys_and_authenticated_discovery():
    replacement = "replacement-test-key-more-than-24-characters"
    async with api(api_keys=[SERVICE_KEY, replacement]) as client:
        for key in (SERVICE_KEY, replacement):
            response = await client.get("/v1/profiles", headers={"Authorization": f"Bearer {key}"})
            assert response.status_code == 200
        for path in ("/docs", "/redoc", "/openapi.json", "/metrics", "/v1/backends"):
            response = await client.get(path, headers=AUTH)
            assert response.status_code == 200
            assert SERVICE_KEY not in response.text
            assert replacement not in response.text


@pytest.mark.parametrize("path", ["/health/live", "/v1/profiles", "/mcp"])
async def test_unrecognized_host_is_rejected_before_execution(path):
    async with api() as client:
        response = await client.get(path, headers={**AUTH, "Host": "attacker.example"})
    assert response.status_code == 421
    assert response.json()["error"]["code"] == "invalid_host"


@pytest.mark.parametrize("path", ["/v1/profiles", "/mcp"])
async def test_origin_boundary_is_shared(path):
    async with api() as client:
        response = await client.get(path, headers={**AUTH, "Origin": "https://attacker.example"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_origin"


async def test_allowed_origin_can_access_rest():
    async with api(allowed_origins=["https://rails.example"]) as client:
        response = await client.get(
            "/v1/profiles", headers={**AUTH, "Origin": "https://rails.example"}
        )
    assert response.status_code == 200


async def test_explicit_mock_roundtrip_is_synthetic_advisory_and_has_no_fabricated_confidence():
    payload = decision(
        backend="mock",
        job_run_id="job-run-42",
        evidence_refs=["receipt:123"],
        questions={
            "noul": {"type": "noul", "instructions": "Is it relevant?"},
            "choice": {
                "type": "choice",
                "instructions": "Choose.",
                "criteria": {"a": "A", "b": "B"},
            },
            "score": {
                "type": "score",
                "instructions": "Rate.",
                "criteria": ["Low", "Medium", "High"],
            },
        },
    )
    async with api(default_backend="laya") as client:
        response = await client.post("/v1/decisions", json=payload, headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["backend"] == "mock"
    assert data["model"] == "synthetic-uniform-fixture-v1"
    assert data["answers"]["noul"]["value"] == 0.5
    assert data["answers"]["choice"]["probabilities"] == {"a": 0.5, "b": 0.5}
    assert data["answers"]["score"]["value"] == 1.0
    assert all("confidence" not in result for result in data["answers"].values())
    assert data["advisory_only"] is True and data["calibrated"] is False
    assert data["calibration_version"] is None
    assert data["job_run_id"] == "job-run-42"
    assert data["evidence_refs"] == ["receipt:123"]
    assert len(data["schema_hash"]) == 64
    assert any("SYNTHETIC" in warning for warning in data["warnings"])
    assert data["request_id"] == response.headers["x-request-id"]


async def test_mock_cannot_be_selected_when_disabled():
    async with api(default_backend="laya", allow_mock=False) as client:
        response = await client.post("/v1/decisions", json=decision(backend="mock"), headers=AUTH)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "backend_disabled"


async def test_versioned_profiles_are_executable_and_have_stable_schema_hashes():
    async with api() as client:
        profiles = (await client.get("/v1/profiles", headers=AUTH)).json()["profiles"]
        assert "research.evidence_screen.v1" in profiles
        first = await client.post(
            "/v1/decisions",
            json={"state": "First evidence", "decision_key": "research.evidence_screen.v1"},
            headers=AUTH,
        )
        second = await client.post(
            "/v1/decisions",
            json={"state": "Different evidence", "decision_key": "research.evidence_screen.v1"},
            headers=AUTH,
        )
    assert first.status_code == second.status_code == 200
    assert set(first.json()["answers"]) == {
        "relevance",
        "independent_evidence",
        "needs_verification",
    }
    assert first.json()["schema_hash"] == second.json()["schema_hash"]
    assert first.json()["request_id"] != second.json()["request_id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"state": PRIVATE},
        {"state": PRIVATE, "questions": {}},
        {"state": PRIVATE, "questions": QUESTION, "decision_key": "research.evidence_screen.v1"},
        {"state": PRIVATE, "questions": QUESTION, "backend_url": "https://attacker.example"},
        {
            "state": PRIVATE,
            "questions": {
                "q": {"type": "score", "instructions": "Rate", "criteria": ["same", "same"]}
            },
        },
        {
            "state": PRIVATE,
            "questions": {
                "q": {"type": "noul", "instructions": "Test", "criteria": {"true": "Yes"}}
            },
        },
        {"state": PRIVATE, "questions": QUESTION, "allow_remote": "true"},
    ],
)
async def test_request_validation_does_not_echo_sensitive_inputs(payload):
    async with api() as client:
        response = await client.post("/v1/decisions", json=payload, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert PRIVATE not in response.text
    assert "attacker.example" not in response.text


async def test_unknown_profile_is_an_explicit_error():
    async with api() as client:
        response = await client.post(
            "/v1/decisions", json={"state": PRIVATE, "decision_key": "missing.v1"}, headers=AUTH
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_profile"
    assert PRIVATE not in response.text


@pytest.mark.parametrize("path", ["/v1/decisions", "/mcp"])
async def test_streamed_request_limit_cannot_be_bypassed_by_false_content_length(path):
    async def chunks():
        yield b'{"state":"'
        yield b"x" * 1500
        yield b'"}'

    async with api(max_body_bytes=1024) as client:
        response = await client.post(
            path,
            content=chunks(),
            headers={**AUTH, "Content-Type": "application/json", "Content-Length": "1"},
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


@pytest.mark.parametrize("path", ["/v1/decisions", "/mcp"])
@pytest.mark.parametrize("body", [b"not-json", b'{"state":NaN}', b'{"state":Infinity}'])
async def test_malformed_or_nonfinite_json_is_rejected_at_shared_boundary(path, body):
    async with api() as client:
        response = await client.post(
            path, content=body, headers={**AUTH, "Content-Type": "application/json"}
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


@pytest.mark.parametrize(
    "enabled,classification,allow_remote,code",
    [
        (False, "public", True, "backend_disabled"),
        (True, "public", False, "remote_not_allowed"),
        (True, "internal", True, "remote_not_allowed"),
        (True, "confidential", True, "remote_not_allowed"),
        (True, "restricted", True, "remote_not_allowed"),
    ],
)
async def test_hosted_policy_rejects_before_any_egress(enabled, classification, allow_remote, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=answer())

    async with api(
        handler=handler, enable_hosted=enabled, jev_api_key="private-hosted-api-key"
    ) as client:
        response = await client.post(
            "/v1/decisions",
            json=decision(
                backend="jev",
                state=PRIVATE,
                data_classification=classification,
                allow_remote=allow_remote,
            ),
            headers=AUTH,
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == code
    assert PRIVATE not in response.text
    assert calls == []


async def test_hosted_opt_in_calls_only_configured_endpoint_with_upstream_credentials():
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["authorization"] == "Bearer private-hosted-api-key"
        payload = json.loads(request.content)
        assert payload["model"] == "jev-1.13.0"
        assert payload["state"] == "Public material"
        result = answer()
        result["model"] = "jev-1.13.0"
        return httpx.Response(200, json=result)

    async with api(
        handler=handler, enable_hosted=True, jev_api_key="private-hosted-api-key"
    ) as client:
        response = await client.post(
            "/v1/decisions",
            json=decision(
                backend="jev",
                state="Public material",
                data_classification="public",
                allow_remote=True,
            ),
            headers=AUTH,
        )
    assert response.status_code == 200, response.text
    assert response.json()["model_revision"] == "jev-1.13.0"
    assert response.json()["model_revision_source"] == "upstream_reported"
    assert len(calls) == 1
    assert "private-hosted-api-key" not in response.text


async def test_batch_is_disabled_by_default_without_calling_upstream():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=answer())

    async with api(handler=handler) as client:
        response = await client.post(
            "/v1/decisions/batch", json={"items": [decision()]}, headers=AUTH
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "batch_disabled"
    assert calls == []


async def test_upstream_failure_never_falls_back_to_mock_or_hosted():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(503, text=PRIVATE)

    async with api(
        handler=handler,
        default_backend="laya",
        enable_hosted=True,
        jev_api_key="private-hosted-api-key",
    ) as client:
        response = await client.post(
            "/v1/decisions",
            json=decision(data_classification="public", allow_remote=True),
            headers=AUTH,
        )
    assert response.status_code == 503
    assert response.json()["error"]["retryable"] is True
    assert calls == ["http://laya-inference:8080/v1/systemone"]
    assert PRIVATE not in response.text


async def test_laya_model_provenance_and_truncation_are_preserved_without_arbitrary_metadata():
    def handler(request):
        result = answer()
        result["routing"]["private_path"] = PRIVATE
        result["usage"].update({"truncated": True, "internal_debug": PRIVATE})
        result["answers"]["relevant"].update({"confidence": 0.8, "action": {"act_probability": 1}})
        return httpx.Response(200, json=result)

    async with api(
        handler=handler, default_backend="laya", laya_model_revision="pinned-sha"
    ) as client:
        response = await client.post("/v1/decisions", json=decision(), headers=AUTH)
    assert response.status_code == 200
    data = response.json()
    assert data["model"] == "english"
    assert data["model_revision"] == "pinned-sha"
    assert data["model_revision_source"] == "operator_configured"
    assert data["answers"]["relevant"]["confidence_kind"] == "laya_max_probability"
    assert "action" not in data["answers"]["relevant"]
    assert data["usage"] == {"input_tokens": 20, "output_tokens": 0}
    assert any("truncation" in warning for warning in data["warnings"])
    assert PRIVATE not in response.text


async def test_batch_preserves_order_partial_errors_and_independent_request_ids():
    def handler(request):
        state = json.loads(request.content)["state"]
        return (
            httpx.Response(429, text=PRIVATE)
            if state == "limited"
            else httpx.Response(200, json=answer(0.9))
        )

    async with api(handler=handler, enable_batch=True) as client:
        response = await client.post(
            "/v1/decisions/batch",
            json={
                "items": [
                    decision(backend="mock"),
                    decision(state="limited", backend="laya"),
                    decision(state="works", backend="laya"),
                ]
            },
            headers=AUTH,
        )
    assert response.status_code == 200, response.text
    data = response.json()
    items = data["items"]
    assert [item["ok"] for item in items] == [True, False, True]
    assert items[0]["result"]["answers"]["relevant"]["value"] == 0.5
    assert items[1]["error"]["code"] == "upstream_rate_limited"
    assert items[1]["error"]["retryable"] is True
    assert items[2]["result"]["answers"]["relevant"]["value"] == 0.9
    ids = [
        items[0]["result"]["request_id"],
        items[1]["request_id"],
        items[2]["result"]["request_id"],
        data["request_id"],
    ]
    assert len(set(ids)) == 4
    assert PRIVATE not in response.text


async def test_batch_policy_is_preflighted_before_transmitting_any_item():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=answer())

    async with api(
        handler=handler,
        enable_batch=True,
        enable_hosted=True,
        jev_api_key="private-hosted-api-key",
    ) as client:
        response = await client.post(
            "/v1/decisions/batch",
            json={
                "items": [
                    decision(backend="laya"),
                    decision(backend="jev", data_classification="restricted", allow_remote=True),
                ]
            },
            headers=AUTH,
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "remote_not_allowed"
    assert calls == []


@pytest.mark.parametrize("status,expected", [(200, 200), (401, 503), (503, 503), (307, 503)])
async def test_laya_readiness_queries_upstream_without_redirects(status, expected):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == "http://laya-inference:8080/health"
        assert request.headers["authorization"] == "Bearer laya-private-key"
        health = {
            "status": "ok",
            "loaded": [],
            "revisions": {},
            "device": "cuda:0",
            "checkpoint_devices": {},
        }
        return httpx.Response(status, json=health, headers={"Location": "https://attacker.example"})

    async with api(
        handler=handler, default_backend="laya", laya_api_key="laya-private-key"
    ) as client:
        response = await client.get("/health/ready")
    assert response.status_code == expected
    assert len(calls) == 1
    assert "laya-private-key" not in response.text


async def test_laya_readiness_rejects_public_liveness_body_when_key_is_configured():
    def handler(request):
        assert request.headers["authorization"] == "Bearer wrong-laya-key"
        # This is Laya's deliberate response to missing or invalid health auth.
        return httpx.Response(200, json={"status": "ok"})

    async with api(
        handler=handler, default_backend="laya", laya_api_key="wrong-laya-key"
    ) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503


async def test_unkeyed_laya_accepts_public_health():
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"status": "ok"})

    async with api(handler=handler, default_backend="laya", laya_api_key=None) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200


async def test_readiness_failure_does_not_affect_liveness():
    def handler(request):
        raise httpx.ConnectError("private backend host", request=request)

    async with api(handler=handler, default_backend="laya") as client:
        ready = await client.get("/health/ready")
        live = await client.get("/health/live")
    assert ready.status_code == 503
    assert live.status_code == 200
    assert "private" not in ready.text


async def test_calculations_work_without_inference_and_report_undefined_results_safely():
    async with api(default_backend="laya", allow_mock=False) as client:
        result = await client.post(
            "/v1/calculations",
            json={"operation": "ratio", "numerator": 3, "denominator": 2},
            headers=AUTH,
        )
        undefined = await client.post(
            "/v1/calculations",
            json={"operation": "bayes", "prior": 0.5, "sensitivity": 0, "false_positive_rate": 0},
            headers=AUTH,
        )
    assert result.status_code == 200
    assert result.json()["result"]["ratio"] == 1.5
    assert result.json()["advisory_only"] is True
    assert undefined.status_code == 422
    assert undefined.json()["error"]["code"] == "invalid_calculation"


async def test_metrics_and_request_logs_exclude_state_and_tokens(caplog):
    caplog.set_level("INFO", logger="tof_prediction_adapter.requests")
    async with api() as client:
        await client.post("/v1/decisions", json=decision(state=PRIVATE), headers=AUTH)
        metrics = await client.get("/metrics", headers=AUTH)
    assert metrics.status_code == 200
    assert "tof_adapter_requests_total" in metrics.text
    assert 'operation="/v1/decisions"' in metrics.text
    assert PRIVATE not in metrics.text + caplog.text
    assert SERVICE_KEY not in metrics.text + caplog.text
    assert "request_id" in caplog.text
