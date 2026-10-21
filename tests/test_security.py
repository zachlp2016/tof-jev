"""Exercise the ASGI boundary directly, including streamed and stalled clients."""

import asyncio
import json
import logging

import pytest
from pydantic import ValidationError

from tof_prediction_adapter.config import Settings
from tof_prediction_adapter.security import BoundaryMiddleware, request_id_context

TOKEN = "test-boundary-service-key-12345678"


class Metric:
    def labels(self, **_labels):
        return self

    def inc(self):
        pass

    def observe(self, _value):
        pass


async def response_app(scope, receive, send):
    if scope["method"] == "POST":
        received = await receive()
        assert received["type"] == "http.request"
        assert received["more_body"] is False
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b'{"ok":true}'})


def boundary(app=response_app, **overrides):
    return BoundaryMiddleware(app, Settings(api_keys=[TOKEN], **overrides), Metric(), Metric())


def scope(*, path="/v1/decisions", method="POST", host="localhost:8080", headers=None):
    values = {
        "host": host,
        "authorization": "Bearer " + TOKEN,
        "content-type": "application/json",
    }
    if headers:
        values.update(headers)
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(key.encode(), value.encode("latin1")) for key, value in values.items()],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8080),
    }


async def invoke(app, *, request_scope=None, chunks=None, receive=None):
    if receive is None:
        pending = list(chunks if chunks is not None else [b"{}"])

        async def receive():
            if not pending:
                return {"type": "http.disconnect"}
            chunk = pending.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    sent = []

    async def send(message):
        sent.append(message)

    await app(request_scope or scope(), receive, send)
    return sent


def status(sent):
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


def body(sent):
    return json.loads(b"".join(message.get("body", b"") for message in sent))


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8080", "LOCALHOST:80", "[::1]", "[::1]:8080"]
)
async def test_valid_hosts_include_bracketed_ipv6_with_port(host):
    assert status(await invoke(boundary(), request_scope=scope(host=host))) == 200


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "localhost.attacker.com",
        "localhost:bad",
        "localhost:65536",
        "localhost@evil.com",
        "[::1]evil",
        "[::1]evil:8080",
        "::1",
        "localhost:0",
    ],
)
async def test_unrecognized_or_malformed_host_rejected_without_reading_body(host):
    async def forbidden_receive():
        pytest.fail("host rejection must precede body consumption")

    sent = await invoke(boundary(), request_scope=scope(host=host), receive=forbidden_receive)
    assert status(sent) == 421
    assert body(sent)["error"]["code"] == "invalid_host"


async def test_configured_explicit_host_port_is_enforced():
    app = boundary(allowed_hosts=["localhost:8080", "[::1]:9000"])
    assert status(await invoke(app, request_scope=scope(host="localhost:8080"))) == 200
    assert status(await invoke(app, request_scope=scope(host="localhost:9000"))) == 421
    assert status(await invoke(app, request_scope=scope(host="[::1]:9000"))) == 200
    assert status(await invoke(app, request_scope=scope(host="[::1]:8080"))) == 421


@pytest.mark.parametrize("authorization", ["", "Bearer wrong-secret", "Basic " + TOKEN])
async def test_bad_auth_is_rejected_without_reading_or_echoing_credentials(authorization):
    async def forbidden_receive():
        pytest.fail("authentication must precede body consumption")

    sent = await invoke(
        boundary(),
        request_scope=scope(headers={"authorization": authorization}),
        receive=forbidden_receive,
    )
    assert status(sent) == 401
    assert body(sent)["error"]["code"] == "unauthorized"
    response_headers = dict(sent[0]["headers"])
    assert response_headers[b"www-authenticate"] == b"Bearer"
    assert TOKEN not in str(sent)
    assert "wrong-secret" not in str(sent)


async def test_health_endpoint_requires_valid_host_but_no_service_key():
    sent = await invoke(
        boundary(),
        request_scope=scope(path="/health/live", method="GET", headers={"authorization": ""}),
    )
    assert status(sent) == 200


async def test_browser_origin_is_explicitly_allowed():
    app = boundary(allowed_origins=["https://tof.example"])
    assert (
        status(await invoke(app, request_scope=scope(headers={"origin": "https://tof.example"})))
        == 200
    )
    assert (
        status(await invoke(app, request_scope=scope(headers={"origin": "https://evil.example"})))
        == 403
    )


async def test_chunked_body_limit_does_not_depend_on_content_length():
    sent = await invoke(
        boundary(max_body_bytes=1024), chunks=[b'{"secret":"' + b"x" * 600, b"x" * 600 + b'"}']
    )
    assert status(sent) == 413
    assert body(sent)["error"]["code"] == "request_too_large"
    assert "secret" not in str(sent)


async def test_lying_content_length_cannot_bypass_streaming_limit():
    sent = await invoke(
        boundary(max_body_bytes=1024),
        request_scope=scope(headers={"content-length": "2"}),
        chunks=[b'"' + b"x" * 1100 + b'"'],
    )
    assert status(sent) == 413


@pytest.mark.parametrize("length,expected", [("not-an-integer", 400), ("-1", 413), ("1025", 413)])
async def test_invalid_or_oversized_declared_length_rejected_before_read(length, expected):
    async def forbidden_receive():
        pytest.fail("declared length must be checked before body consumption")

    sent = await invoke(
        boundary(max_body_bytes=1024),
        request_scope=scope(headers={"content-length": length}),
        receive=forbidden_receive,
    )
    assert status(sent) == expected


async def test_media_type_is_required():
    sent = await invoke(boundary(), request_scope=scope(headers={"content-type": "text/plain"}))
    assert status(sent) == 415


@pytest.mark.parametrize(
    "payload",
    [
        b'{"secret":"private-request",}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1e9999}',
        b'{"x":1,"x":2}',
        b'{"nested":{"x":1,"x":2}}',
        b'{"x":"\\ud800"}',
        b'{"\\ud800":1}',
        b"\xff",
        b"[" * 50 + b"0" + b"]" * 50,
    ],
)
async def test_invalid_json_rejected_without_echoing_payload(payload, caplog):
    caplog.set_level(logging.INFO, logger="tof_prediction_adapter.requests")
    sent = await invoke(boundary(), chunks=[payload])
    assert status(sent) == 400
    assert body(sent)["error"]["code"] == "invalid_json"
    assert "private-request" not in str(sent) + caplog.text
    assert TOKEN not in str(sent) + caplog.text


async def test_body_timeout_releases_ingress_slot():
    app = boundary(max_inflight_requests=1, body_timeout_seconds=0.01)

    async def stalled_receive():
        await asyncio.Event().wait()

    sent = await invoke(app, receive=stalled_receive)
    assert status(sent) == 408
    assert body(sent)["error"]["code"] == "request_timeout"
    assert status(await invoke(app)) == 200


async def test_ingress_capacity_rejects_immediately_before_reading_another_body():
    started, finish = asyncio.Event(), asyncio.Event()

    async def held_app(request_scope, receive, send):
        started.set()
        await finish.wait()
        await response_app(request_scope, receive, send)

    app = boundary(held_app, max_inflight_requests=1)
    first = asyncio.create_task(invoke(app))
    await started.wait()

    async def forbidden_receive():
        pytest.fail("capacity rejection must precede body consumption")

    try:
        second = await invoke(app, receive=forbidden_receive)
        assert status(second) == 503
        assert body(second)["error"]["code"] == "capacity_exceeded"
    finally:
        finish.set()
        assert status(await first) == 200
    assert status(await invoke(app)) == 200


async def test_cancelled_or_disconnected_client_releases_ingress_slot():
    app = boundary(max_inflight_requests=1)
    entered = asyncio.Event()

    async def stalled_receive():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(invoke(app, receive=stalled_receive))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert status(await invoke(app)) == 200

    async def disconnected_receive():
        return {"type": "http.disconnect"}

    assert await invoke(app, receive=disconnected_receive) == []
    assert status(await invoke(app)) == 200


async def test_valid_chunked_body_replayed_exactly_once_and_context_reset():
    received_bodies = []

    async def recording_app(request_scope, receive, send):
        assert request_id_context.get()
        received_bodies.append(await receive())
        assert (await receive())["type"] == "http.disconnect"
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = await invoke(boundary(recording_app), chunks=[b'{"x":', b"1}"])
    assert status(sent) == 204
    assert received_bodies == [{"type": "http.request", "body": b'{"x":1}', "more_body": False}]
    assert request_id_context.get() == ""
    response_headers = dict(sent[0]["headers"])
    assert response_headers[b"cache-control"] == b"no-store"
    assert response_headers[b"x-content-type-options"] == b"nosniff"
    assert response_headers[b"x-request-id"]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:bad",
        "http://localhost:99999",
        "http://localhost:0",
        "http://localhost:",
        "http://[::1]evil",
        "http://localhost/a",
        "http://user:password@localhost",
        "http://localhost/?secret=1",
        "http://local host",
        "http://localhost\n",
    ],
)
def test_settings_reject_malformed_backend_authorities_at_startup(url):
    with pytest.raises(ValidationError):
        Settings(api_keys=[TOKEN], laya_base_url=url)


@pytest.mark.parametrize(
    "settings",
    [
        {"max_inflight_requests": 0},
        {"max_inflight_requests": 4097},
        {"body_timeout_seconds": 0},
        {"body_timeout_seconds": 61},
        {"body_timeout_seconds": float("nan")},
        {"body_timeout_seconds": float("inf")},
        {"allowed_hosts": ["*"]},
        {"allowed_hosts": ["localhost:bad"]},
    ],
)
def test_resource_and_host_settings_are_bounded(settings):
    with pytest.raises(ValidationError):
        Settings(api_keys=[TOKEN], **settings)
