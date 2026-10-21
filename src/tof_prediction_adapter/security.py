import asyncio
import json
import logging
import math
import time
from contextvars import ContextVar
from uuid import uuid4

from starlette.responses import JSONResponse

from .config import parse_authority
from .errors import AdapterError, error_body

request_id_context: ContextVar[str] = ContextVar("request_id", default="")
consumer_context: ContextVar[str] = ContextVar("consumer", default="anonymous")
logger = logging.getLogger("tof_prediction_adapter.requests")


def _invalid_constant(_):
    raise ValueError("Nonfinite JSON")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON")
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _bounded_json(value, level=0):
    if level > 48:
        raise ValueError("JSON nesting too deep")
    if isinstance(value, dict):
        for key, child in value.items():
            key.encode("utf-8")
            _bounded_json(child, level + 1)
    elif isinstance(value, list):
        for child in value:
            _bounded_json(child, level + 1)
    elif isinstance(value, str):
        # Lone surrogate escapes otherwise survive JSON decoding but fail
        # later when an upstream request is serialized as UTF-8.
        value.encode("utf-8")


class BoundaryMiddleware:
    """One bounded auth/body/origin boundary shared by REST and mounted MCP."""

    def __init__(self, app, settings, counter, latency):
        self.app, self.settings = app, settings
        self.counter, self.latency = counter, latency
        self.inflight = asyncio.Semaphore(settings.max_inflight_requests)
        self.allowed_hosts = [
            parse_authority(host[:-2] if host.endswith(":*") else host)
            for host in settings.allowed_hosts
        ]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        token = request_id_context.set(request_id)
        consumer_token = consumer_context.set("anonymous")
        started = time.monotonic()
        status = 500
        acquired = False
        path = scope.get("path", "")
        operation = (
            path
            if path
            in {
                "/v1/decisions",
                "/v1/decisions/batch",
                "/v1/calculations",
                "/v1/profiles",
                "/v1/backends",
                "/health/live",
                "/health/ready",
                "/metrics",
            }
            else ("/mcp" if path.startswith("/mcp") else "other")
        )
        headers = {
            key.decode("latin1").lower(): value.decode("latin1") for key, value in scope["headers"]
        }

        async def wrapped_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode()),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        async def reject(code, message, status_code, retryable=False):
            response = JSONResponse(
                error_body(AdapterError(code, message, status_code, retryable), request_id),
                status_code=status_code,
                headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
            )
            await response(scope, receive, wrapped_send)

        try:
            try:
                hostname, port = parse_authority(headers.get("host", ""))
                allowed = any(
                    hostname == allowed_host and (allowed_port is None or allowed_port == port)
                    for allowed_host, allowed_port in self.allowed_hosts
                )
            except ValueError:
                allowed = False
            if not allowed:
                await reject("invalid_host", "Unrecognized service host.", 421)
                return
            if path not in {"/health/live", "/health/ready"}:
                authorization = headers.get("authorization", "")
                scheme, _, supplied = authorization.partition(" ")
                consumer = self.settings.consumer_for_api_key(supplied)
                if scheme.lower() != "bearer" or consumer is None:
                    await reject("unauthorized", "A valid service bearer token is required.", 401)
                    return
                consumer_context.reset(consumer_token)
                consumer_token = consumer_context.set(consumer)
                origin = headers.get("origin")
                if origin is not None and origin not in self.settings.allowed_origins:
                    await reject("invalid_origin", "Origin is not allowed.", 403)
                    return
            # No await between locked() and an available acquire(): the
            # semaphore rejects overflow immediately without queuing more
            # request bodies in this event loop.
            is_liveness_probe = path == "/health/live" and scope["method"] == "GET"
            if not is_liveness_probe and self.inflight.locked():
                await reject(
                    "capacity_exceeded", "Request capacity is full; retry later.", 503, True
                )
                return
            if not is_liveness_probe:
                await self.inflight.acquire()
                acquired = True
            if scope["method"] in {"POST", "PUT", "PATCH"}:
                if (
                    headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                ):
                    await reject("unsupported_media_type", "Use application/json.", 415)
                    return
                length = headers.get("content-length")
                if length is not None:
                    try:
                        declared = int(length)
                    except ValueError:
                        await reject("invalid_request", "Invalid Content-Length.", 400)
                        return
                    if declared < 0 or declared > self.settings.max_body_bytes:
                        await reject(
                            "request_too_large", "Request body exceeds the configured limit.", 413
                        )
                        return
                body = bytearray()
                try:
                    async with asyncio.timeout(self.settings.body_timeout_seconds):
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                status = 499
                                return
                            chunk = message.get("body", b"")
                            if len(body) + len(chunk) > self.settings.max_body_bytes:
                                await reject(
                                    "request_too_large",
                                    "Request body exceeds the configured limit.",
                                    413,
                                )
                                return
                            body.extend(chunk)
                            if not message.get("more_body", False):
                                break
                except TimeoutError:
                    await reject(
                        "request_timeout", "Request body was not received in time.", 408, True
                    )
                    return
                try:
                    decoded = json.loads(
                        body,
                        parse_constant=_invalid_constant,
                        parse_float=_finite_float,
                        object_pairs_hook=_unique_object,
                    )
                    _bounded_json(decoded)
                except (ValueError, RecursionError, UnicodeError):
                    await reject(
                        "invalid_json",
                        "Request must be finite JSON with unique keys and bounded nesting.",
                        400,
                    )
                    return
                delivered = False

                async def replay():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, replay, wrapped_send)
            else:
                await self.app(scope, receive, wrapped_send)
        finally:
            if acquired:
                self.inflight.release()
            elapsed = time.monotonic() - started
            self.counter.labels(operation=operation, status=str(status)).inc()
            self.latency.labels(operation=operation).observe(elapsed)
            logger.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "consumer": consumer_context.get(),
                        "operation": operation,
                        "status": status,
                        "latency_ms": round(elapsed * 1000, 3),
                    }
                )
            )
            request_id_context.reset(token)
            consumer_context.reset(consumer_token)
