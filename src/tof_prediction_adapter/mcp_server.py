import json
import logging

from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from . import __version__
from .calculations import CalculationRequest, calculate
from .config import parse_authority
from .contracts import BatchRequest, DecisionRequest
from .errors import AdapterError, error_body
from .profiles import PROFILES
from .security import request_id_context
from .upstream import UpstreamError


def create_mcp(service, settings):
    # Match the outer authority parser, including case-insensitive hostnames
    # and bracketed IPv6. An explicit configured port remains exact; an
    # unqualified hostname accepts both the host alone and any valid port.
    allowed_hosts = set()
    for authority in settings.allowed_hosts:
        hostname, port = parse_authority(authority[:-2] if authority.endswith(":*") else authority)
        host = f"[{hostname}]" if ":" in hostname else hostname
        if port is None:
            allowed_hosts.update({host, f"{host}:*"})
        else:
            allowed_hosts.add(f"{host}:{port}")

    server = Server(
        "tof-prediction-adapter",
        version=__version__,
        instructions="Independent advisory prediction and mathematical service. Results do not grant application authority. Mock results are synthetic.",
    )
    empty = {"type": "object", "properties": {}, "additionalProperties": False}

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name="prediction_decide",
                description="Evaluate a versioned profile or explicit typed questions. Hosted inference requires public data and explicit remote opt-in.",
                inputSchema=DecisionRequest.model_json_schema(),
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=False,
                    openWorldHint=True,
                ),
            ),
            types.Tool(
                name="prediction_batch",
                description="Evaluate up to 16 independent requests; preserve order and return item-level errors.",
                inputSchema=BatchRequest.model_json_schema(),
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=False,
                    openWorldHint=True,
                ),
            ),
            types.Tool(
                name="prediction_calculate",
                description="Deterministic ratios, Bayes updates, expected values, normal CDF and binomial probabilities. No learned forecasting.",
                inputSchema=CalculationRequest.model_json_schema(),
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            types.Tool(
                name="prediction_profiles",
                description="List versioned advisory decision profiles.",
                inputSchema=empty,
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
        ]

    # Pydantic performs validation here so invalid inputs are never echoed in SDK errors.
    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        request_id = request_id_context.get()
        try:
            if name == "prediction_decide":
                result = await service.decide(DecisionRequest.model_validate(arguments), request_id)
                return result.model_dump(mode="json")
            if name == "prediction_batch":
                batch = BatchRequest.model_validate(arguments)
                return {"request_id": request_id, "items": await service.batch(batch.items)}
            if name == "prediction_calculate":
                result = calculate(CalculationRequest.model_validate(arguments))
                return {"request_id": request_id, **result.model_dump(mode="json")}
            if name == "prediction_profiles" and not arguments:
                return {"profiles": PROFILES}
            raise AdapterError("unknown_tool", "Unknown tool or invalid arguments.")
        except ValidationError:
            error = AdapterError("validation_error", "Arguments do not match the tool schema.")
        except (AdapterError, UpstreamError) as exc:
            error = exc
        except ValueError:
            error = AdapterError(
                "invalid_calculation",
                "Calculation is undefined or outside supported finite bounds.",
            )
        except Exception:
            logging.getLogger(__name__).error("Unhandled MCP error request_id=%s", request_id)
            error = AdapterError("internal_error", "An internal error occurred.", 500)
        body = error_body(error, request_id)
        return types.CallToolResult(
            isError=True,
            content=[types.TextContent(type="text", text=json.dumps(body))],
            structuredContent=body,
        )

    return StreamableHTTPSessionManager(
        server,
        json_response=True,
        stateless=True,
        max_request_body_size=settings.max_body_bytes,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(allowed_hosts),
            allowed_origins=settings.allowed_origins,
        ),
    )
