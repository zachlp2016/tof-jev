import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPBearer
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.routing import Mount

from . import __version__
from .calculations import CalculationRequest, calculate
from .config import Settings, parse_authority
from .contracts import BatchRequest, DecisionRequest, DecisionResponse
from .errors import AdapterError, error_body
from .mcp_server import create_mcp
from .profiles import PROFILES
from .security import BoundaryMiddleware, request_id_context
from .service import PredictionService
from .upstream import UpstreamError


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or Settings()
    request_logger = logging.getLogger("tof_prediction_adapter.requests")
    if not request_logger.handlers:
        request_logger.addHandler(logging.StreamHandler())
    request_logger.setLevel(logging.INFO)
    request_logger.propagate = False
    # No environment proxy inheritance: only operator-configured upstream destinations.
    client = httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(
            max_connections=settings.max_concurrent_requests + 2,
            max_keepalive_connections=settings.max_concurrent_requests,
        ),
        transport=transport,
    )
    service = PredictionService(settings, client)
    manager = create_mcp(service, settings)
    registry = CollectorRegistry()
    requests = Counter(
        "tof_adapter_requests_total",
        "Requests by bounded operation and HTTP status",
        ["operation", "status"],
        registry=registry,
    )
    latency = Histogram(
        "tof_adapter_request_duration_seconds", "Request duration", ["operation"], registry=registry
    )

    @asynccontextmanager
    async def lifespan(app):
        # Suppress external HTTP/SDK diagnostics that can contain supplied content.
        for name in ("httpx", "httpcore", "mcp"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        async with client, manager.run():
            yield

    app = FastAPI(
        title="TOF Prediction Adapter",
        version=__version__,
        lifespan=lifespan,
        description="Independent, advisory prediction and deterministic mathematics over REST and MCP.",
    )
    app.state.service = service
    app.state.settings = settings
    auth = [Depends(HTTPBearer(auto_error=False))]

    @app.exception_handler(AdapterError)
    @app.exception_handler(UpstreamError)
    async def adapter_error(_, exc):
        return JSONResponse(
            error_body(exc, request_id_context.get()),
            status_code=exc.status_code,
            headers={"Retry-After": "1"} if exc.retryable else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, exc):
        # Avoid returning FastAPI's default input echo (may contain sensitive state).
        return JSONResponse(
            error_body(
                AdapterError("validation_error", "Request does not match the endpoint schema."),
                request_id_context.get(),
            ),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def internal_error(_, exc):
        logging.getLogger(__name__).error("Unhandled request error")
        return JSONResponse(
            error_body(
                AdapterError("internal_error", "An internal error occurred.", 500),
                request_id_context.get(),
            ),
            status_code=500,
        )

    @app.get("/health/live", tags=["health"])
    async def live():
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready", tags=["health"])
    async def ready():
        is_ready = await service.readiness()
        return JSONResponse(
            {
                "status": "ready" if is_ready else "unavailable",
                "backend": settings.default_backend,
                "synthetic": settings.default_backend == "mock",
            },
            status_code=200 if is_ready else 503,
        )

    @app.post(
        "/v1/decisions", response_model=DecisionResponse, dependencies=auth, tags=["predictions"]
    )
    async def decide(request: DecisionRequest):
        return await service.decide(request, request_id_context.get())

    @app.post("/v1/decisions/batch", dependencies=auth, tags=["predictions"])
    async def batch(request: BatchRequest):
        if not settings.enable_batch:
            raise AdapterError(
                "batch_disabled",
                "Batch decisions are disabled; submit one state with multiple questions.",
                403,
            )
        return {"request_id": request_id_context.get(), "items": await service.batch(request.items)}

    @app.post("/v1/calculations", dependencies=auth, tags=["mathematics"])
    async def calculation(request: CalculationRequest):
        try:
            result = calculate(request)
        except ValueError as exc:
            raise AdapterError(
                "invalid_calculation",
                "Calculation is undefined or outside supported finite bounds.",
            ) from exc
        return {"request_id": request_id_context.get(), **result.model_dump(mode="json")}

    @app.get("/v1/profiles", dependencies=auth, tags=["discovery"])
    async def profiles():
        return {"profiles": PROFILES}

    @app.get("/v1/backends", dependencies=auth, tags=["discovery"])
    async def backends():
        return {
            "default": settings.default_backend,
            "limits": {
                "active_requests": settings.max_concurrent_requests,
                "active_requests_per_consumer": settings.max_concurrent_requests_per_consumer,
                "batch_enabled": settings.enable_batch,
            },
            "backends": [
                {
                    "name": "laya",
                    "enabled": True,
                    "local": True,
                    "configured_model": settings.laya_model,
                },
                {
                    "name": "jev",
                    "enabled": settings.enable_hosted,
                    "local": False,
                    "configured_model": settings.jev_model,
                },
                {"name": "mock", "enabled": settings.allow_mock, "local": True, "synthetic": True},
            ],
            "forecasting_available": False,
        }

    @app.get("/metrics", dependencies=auth, include_in_schema=False)
    async def metrics():
        return Response(generate_latest(registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    # Mount last so FastAPI routes win. MCP is served at /mcp (no redirect).
    class MCPApp:
        async def __call__(self, scope, receive, send):
            # The shared boundary has already validated the authority. Present
            # the same canonical spelling to the SDK's case-sensitive host
            # matcher while retaining its independent DNS rebinding checks.
            host_values = [value for name, value in scope["headers"] if name.lower() == b"host"]
            hostname, port = parse_authority(host_values[-1].decode("latin1"))
            host = f"[{hostname}]" if ":" in hostname else hostname
            if port is not None:
                host = f"{host}:{port}"
            canonical_scope = dict(scope)
            canonical_scope["headers"] = [
                (name, value) for name, value in scope["headers"] if name.lower() != b"host"
            ] + [(b"host", host.encode("latin1"))]
            await manager.handle_request(canonical_scope, receive, send)

    app.router.routes.append(Mount("/mcp", app=MCPApp()))
    # Starlette Mount redirects its exact prefix; register exact ASGI route too.
    from starlette.routing import Route

    app.router.routes.insert(
        -1, Route("/mcp", endpoint=MCPApp(), methods=["GET", "POST", "DELETE"])
    )
    app.add_middleware(BoundaryMiddleware, settings=settings, counter=requests, latency=latency)
    return app
