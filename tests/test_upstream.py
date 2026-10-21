"""Contract tests without downloading models or contacting hosted inference."""

import copy
import json

import httpx
import pytest

from tof_prediction_adapter.upstream import UpstreamError, call_backend, normalize_answers


@pytest.fixture
def questions():
    return {
        "queue": {
            "type": "choice",
            "instructions": "Which queue?",
            "criteria": {"billing": "Billing", "technical": "Technical"},
        },
        "urgency": {
            "type": "score",
            "instructions": "How urgent?",
            "criteria": ["Routine", "Soon", "Immediate"],
        },
        "intervention": {"type": "noul", "instructions": "Does this need intervention?"},
    }


@pytest.fixture
def response():
    return {
        "model": "jev-1.13.0",
        "answers": {
            "queue": {
                "type": "choice",
                "choice": "billing",
                "confidence": 0.8,
                "probabilities": {"billing": 0.9, "technical": 0.1},
            },
            "urgency": {
                "type": "score",
                "score": 1.7,
                "confidence": 0.55,
                "legend": {"0": "Routine", "1": "Soon", "2": "Immediate"},
                "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8},
            },
            "intervention": {"type": "noul", "noul": 0.92},
        },
        "usage": {"input_tokens": 120, "output_tokens": 12},
    }


def test_normalizes_jev_and_does_not_invent_noul_confidence(questions, response):
    original = copy.deepcopy(response)
    normalized = normalize_answers(response, questions, "jev")
    assert normalized["queue"] == {
        "type": "choice",
        "value": "billing",
        "probabilities": {"billing": 0.9, "technical": 0.1},
        "confidence": 0.8,
        "confidence_kind": "jev_reported",
    }
    assert normalized["urgency"]["value"] == 1.7
    assert normalized["urgency"]["probabilities"] == {"0": 0.1, "1": 0.1, "2": 0.8}
    assert normalized["intervention"] == {"type": "noul", "value": 0.92}
    assert response == original


def test_laya_confidence_is_identified_and_extras_do_not_leak(questions, response):
    response["answers"]["queue"].update(
        {
            "confidence": 0.531,
            "answer_confidence": 0.9,
            "x_jev_confidence": 0.8,
            "action": {"act_probability": 1.0},
        }
    )
    response["answers"]["intervention"]["confidence"] = 0.92
    normalized = normalize_answers(response, questions, "laya")
    assert normalized["queue"]["confidence"] == 0.531
    assert normalized["queue"]["confidence_kind"] == "laya_entropy"
    assert normalized["urgency"]["confidence_kind"] == "laya_entropy"
    assert normalized["intervention"]["confidence_kind"] == "laya_max_probability"
    assert "action" not in normalized["queue"]
    assert "x_jev_confidence" not in normalized["queue"]


def test_missing_confidence_is_not_synthesized(questions, response):
    del response["answers"]["queue"]["confidence"]
    result = normalize_answers(response, questions, "laya")
    assert "confidence" not in result["queue"]
    assert "confidence_kind" not in result["queue"]


@pytest.mark.parametrize(
    "change",
    [
        lambda raw: raw["answers"].pop("queue"),
        lambda raw: raw["answers"].update({"extra": {"type": "noul", "noul": 0.2}}),
        lambda raw: raw["answers"].update({"queue": []}),
        lambda raw: raw["answers"]["queue"].update({"type": "noul"}),
        lambda raw: raw["answers"]["queue"].update({"choice": "invented"}),
        lambda raw: raw["answers"]["queue"].update({"choice": True}),
        lambda raw: raw["answers"]["queue"].update({"probabilities": {"billing": 1.0}}),
        lambda raw: raw["answers"]["queue"].update(
            {"probabilities": {"billing": 0.2, "technical": 0.2}}
        ),
        lambda raw: raw["answers"]["queue"]["probabilities"].update(
            {"billing": -0.1, "technical": 1.1}
        ),
        lambda raw: raw["answers"]["urgency"].update({"score": -0.01}),
        lambda raw: raw["answers"]["urgency"].update({"score": 2.01}),
        lambda raw: raw["answers"]["urgency"].update({"legend": {"0": "Routine", "1": "Soon"}}),
        lambda raw: raw["answers"]["urgency"]["legend"].update({"1": "A different rubric"}),
        lambda raw: raw["answers"]["urgency"].update(
            {"probabilities": {"1": 0.1, "2": 0.1, "3": 0.8}}
        ),
        lambda raw: raw["answers"]["intervention"].update({"noul": 1.01}),
    ],
)
def test_rejects_malformed_answers(questions, response, change):
    change(response)
    with pytest.raises(UpstreamError) as raised:
        normalize_answers(response, questions, "jev")
    assert raised.value.code == "upstream_invalid_response"
    assert raised.value.status_code == 502
    assert raised.value.retryable is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "0.9", None])
@pytest.mark.parametrize("field", ["confidence", "score", "noul", "probability"])
def test_rejects_nonfinite_and_wrong_numeric_types(questions, response, bad, field):
    if field == "confidence":
        response["answers"]["queue"]["confidence"] = bad
    elif field == "score":
        response["answers"]["urgency"]["score"] = bad
    elif field == "noul":
        response["answers"]["intervention"]["noul"] = bad
    else:
        response["answers"]["queue"]["probabilities"]["billing"] = bad
    with pytest.raises(UpstreamError):
        normalize_answers(response, questions, "jev")


def test_allows_probability_rounding_without_rescaling():
    questions = {"q": {"type": "choice", "criteria": {"a": "A", "b": "B", "c": "C"}}}
    response = {
        "answers": {
            "q": {
                "type": "choice",
                "choice": "a",
                "probabilities": {"a": 0.3333, "b": 0.3333, "c": 0.3333},
            }
        }
    }
    result = normalize_answers(response, questions, "laya")
    assert result["q"]["probabilities"]["a"] == 0.3333


async def test_http_payload_auth_url_and_single_request(response, questions):
    calls = []
    payload = {"model": "jev-1.13.0", "state": {"message": "private"}, "questions": questions}

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://backend.example/prefix/v1/systemone"
        assert request.headers["authorization"] == "Bearer private-key"
        assert request.headers["accept-encoding"] == "identity"
        assert json.loads(request.content) == payload
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        actual = await call_backend(
            client,
            base_url="https://backend.example/prefix/",
            api_key="private-key",
            payload=payload,
            backend="jev",
        )
    assert actual == response
    assert len(calls) == 1


async def test_local_backend_without_api_key(response):
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (
            await call_backend(
                client,
                base_url="http://laya:8000",
                api_key=None,
                payload={},
                backend="laya",
            )
            == response
        )


@pytest.mark.parametrize(
    "status,code,adapter_status,retryable",
    [
        (301, "upstream_invalid_response", 502, False),
        (307, "upstream_invalid_response", 502, False),
        (400, "upstream_rejected_request", 502, False),
        (401, "upstream_authentication_failed", 502, False),
        (403, "upstream_authentication_failed", 502, False),
        (413, "upstream_rejected_request", 502, False),
        (422, "upstream_rejected_request", 502, False),
        (429, "upstream_rate_limited", 503, True),
        (500, "upstream_unavailable", 503, True),
        (503, "upstream_unavailable", 503, True),
        (504, "upstream_timeout", 504, True),
        (529, "upstream_unavailable", 503, True),
    ],
)
async def test_http_errors_are_sanitized_not_retried_or_redirected(
    status, code, adapter_status, retryable
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="private-key private state traceback C:\\private\\weights",
            headers={"Location": "https://elsewhere.example/steal", "Retry-After": "1"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(UpstreamError) as raised:
            await call_backend(
                client,
                base_url="https://backend.example",
                api_key="private-key",
                payload={"state": "private state"},
                backend="jev",
            )
    assert raised.value.code == code
    assert raised.value.status_code == adapter_status
    assert raised.value.retryable is retryable
    assert len(calls) == 1
    assert "private" not in str(raised.value)
    assert "weights" not in str(raised.value)


@pytest.mark.parametrize(
    "error,code,status",
    [
        (httpx.ReadTimeout, "upstream_timeout", 504),
        (httpx.ConnectTimeout, "upstream_timeout", 504),
        (httpx.ConnectError, "upstream_unavailable", 502),
        (httpx.RemoteProtocolError, "upstream_unavailable", 502),
    ],
)
async def test_transport_errors_are_sanitized(error, code, status):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise error("private-key https://private.internal", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError) as raised:
            await call_backend(
                client, base_url="https://backend.example", api_key=None, payload={}, backend="laya"
            )
    assert raised.value.code == code
    assert raised.value.status_code == status
    assert raised.value.retryable is True
    assert "private" not in str(raised.value)
    assert calls == 1


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("content_length", [None, "1", "invalid", "1000000"])
async def test_streaming_limit_enforced_with_missing_false_or_large_length(content_length):
    stream = Chunks([b'{"answers":', b" " * 100, b"{} }"])
    headers = {} if content_length is None else {"Content-Length": content_length}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream, headers=headers)
        )
    ) as client:
        with pytest.raises(UpstreamError) as raised:
            await call_backend(
                client,
                base_url="https://backend.example",
                api_key=None,
                payload={},
                backend="jev",
                max_response_bytes=64,
            )
    assert raised.value.code == "upstream_response_too_large"
    assert stream.closed


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"[]",
        b"null",
        b"\xff",
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"answers":{},"answers":{}}',
        b'{"x":[[[',
    ],
)
async def test_json_parse_rejects_invalid_or_ambiguous_response(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        with pytest.raises(UpstreamError) as raised:
            await call_backend(
                client, base_url="https://backend.example", api_key=None, payload={}, backend="jev"
            )
    assert raised.value.code == "upstream_invalid_response"


async def test_unexpected_compression_is_refused_before_reading():
    stream = Chunks([b"compressed content"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream, headers={"Content-Encoding": "gzip"})
        )
    ) as client:
        with pytest.raises(UpstreamError) as raised:
            await call_backend(
                client, base_url="https://backend.example", api_key=None, payload={}, backend="jev"
            )
    assert raised.value.code == "upstream_invalid_response"
    assert stream.closed
