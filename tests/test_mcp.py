"""Real MCP SDK interoperability over an in-process Streamable HTTP transport."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tof_prediction_adapter.app import create_app
from tof_prediction_adapter.config import Settings

SERVICE_KEY = "mcp-service-key-with-at-least-24-characters"
PRIVATE = "MCP_PRIVATE_CONTENT_DO_NOT_ECHO_831"
QUESTION = {"relevant": {"type": "noul", "instructions": "Is this relevant?"}}


@asynccontextmanager
async def connected_mcp(*, handler=None, token=SERVICE_KEY, host=None, **overrides):
    def no_network(request):
        raise AssertionError(f"Unexpected upstream request to {request.url.host}")

    values = {"api_keys": [SERVICE_KEY], "default_backend": "mock", "allow_mock": True}
    app = create_app(
        Settings(**(values | overrides)), transport=httpx.MockTransport(handler or no_network)
    )
    async with (
        asyncio.timeout(10),
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {token}", **({"Host": host} if host else {})},
        ) as http_client,
    ):
        async with streamable_http_client("http://testserver/mcp", http_client=http_client) as (
            read,
            write,
            _,
        ):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=5)
            ) as session:
                initialized = await session.initialize()
                yield session, http_client, initialized


async def test_real_sdk_initializes_discovers_and_calls_tools_on_exact_mcp_path():
    async with connected_mcp() as (session, http_client, initialized):
        assert initialized.serverInfo.name == "tof-prediction-adapter"
        tools = await session.list_tools()
        by_name = {tool.name: tool for tool in tools.tools}
        assert set(by_name) == {
            "prediction_decide",
            "prediction_batch",
            "prediction_calculate",
            "prediction_profiles",
        }
        assert all(tool.annotations.readOnlyHint for tool in tools.tools)
        assert by_name["prediction_calculate"].annotations.openWorldHint is False
        assert by_name["prediction_decide"].annotations.idempotentHint is False

        profiles = await session.call_tool("prediction_profiles", {})
        assert profiles.isError is False
        assert "research.evidence_screen.v1" in profiles.structuredContent["profiles"]

        arguments = {"state": "Useful evidence", "decision_key": "research.evidence_screen.v1"}
        mcp_result = await session.call_tool("prediction_decide", arguments)
        rest_result = await http_client.post("/v1/decisions", json=arguments)
        assert mcp_result.isError is False
        data = mcp_result.structuredContent
        assert data["answers"] == rest_result.json()["answers"]
        assert data["schema_hash"] == rest_result.json()["schema_hash"]
        assert data["backend"] == "mock"
        assert data["advisory_only"] is True and data["calibrated"] is False
        assert data["request_id"] and data["request_id"] != rest_result.json()["request_id"]
        assert any("SYNTHETIC" in warning for warning in data["warnings"])

        ratio = await session.call_tool(
            "prediction_calculate", {"operation": "ratio", "numerator": 9, "denominator": 3}
        )
        assert ratio.isError is False
        assert ratio.structuredContent["result"]["ratio"] == 3.0
        assert ratio.structuredContent["request_id"]

        batch = await session.call_tool(
            "prediction_batch",
            {
                "items": [
                    {"state": "First", "questions": QUESTION},
                    {"state": "Second", "questions": QUESTION},
                ]
            },
        )
        assert batch.isError is False
        assert len(batch.structuredContent["items"]) == 2
        assert all(item["ok"] for item in batch.structuredContent["items"])


@pytest.mark.parametrize(
    "name,arguments,code",
    [
        ("prediction_decide", {"state": PRIVATE}, "validation_error"),
        (
            "prediction_decide",
            {"state": PRIVATE, "questions": QUESTION, "unknown": PRIVATE},
            "validation_error",
        ),
        ("prediction_decide", {"state": PRIVATE, "decision_key": "missing.v1"}, "unknown_profile"),
        (
            "prediction_calculate",
            {"operation": "ratio", "numerator": PRIVATE, "denominator": 2},
            "validation_error",
        ),
        (
            "prediction_calculate",
            {"operation": "bayes", "prior": 0.5, "sensitivity": 0, "false_positive_rate": 0},
            "invalid_calculation",
        ),
        ("prediction_profiles", {"unexpected": PRIVATE}, "unknown_tool"),
    ],
)
async def test_mcp_tool_errors_are_structured_and_never_echo_inputs(name, arguments, code):
    async with connected_mcp() as (session, _, _):
        result = await session.call_tool(name, arguments)
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == code
    assert result.structuredContent["request_id"]
    serialized = result.model_dump_json()
    assert PRIVATE not in serialized
    assert SERVICE_KEY not in serialized
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == result.structuredContent


async def test_mcp_hosted_policy_enforced_without_egress():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    async with connected_mcp(
        handler=handler, enable_hosted=True, jev_api_key="private-jev-key"
    ) as (session, _, _):
        result = await session.call_tool(
            "prediction_decide",
            {
                "state": PRIVATE,
                "questions": QUESTION,
                "backend": "jev",
                "data_classification": "restricted",
                "allow_remote": True,
            },
        )
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "remote_not_allowed"
    assert PRIVATE not in result.model_dump_json()
    assert calls == []


async def test_mcp_does_not_bypass_disabled_mock_backend():
    async with connected_mcp(default_backend="laya", allow_mock=False) as (session, _, _):
        result = await session.call_tool(
            "prediction_decide", {"state": "A source", "questions": QUESTION, "backend": "mock"}
        )
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "backend_disabled"


async def test_mcp_batch_keeps_partial_backend_errors_and_sanitizes_details():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(529, text=PRIVATE)

    async with connected_mcp(handler=handler) as (session, _, _):
        result = await session.call_tool(
            "prediction_batch",
            {
                "items": [
                    {"state": "First", "questions": QUESTION, "backend": "mock"},
                    {"state": PRIVATE, "questions": QUESTION, "backend": "laya"},
                ]
            },
        )
    assert result.isError is False
    items = result.structuredContent["items"]
    assert [item["ok"] for item in items] == [True, False]
    assert items[1]["error"]["retryable"] is True
    assert items[1]["error"]["code"] == "upstream_unavailable"
    assert len(calls) == 1
    assert PRIVATE not in result.model_dump_json()


async def test_real_sdk_cannot_initialize_without_service_authorization():
    # Task groups may wrap transport errors; inspect leaf exceptions to verify
    # the actual server response rather than accepting any initialization error.
    with pytest.raises(Exception) as raised:
        async with connected_mcp(token="wrong-token"):
            pytest.fail("Unauthorized MCP session initialized")

    def leaves(error):
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]

    errors = leaves(raised.value)
    assert any(
        isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 401
        for error in errors
    )


@pytest.mark.parametrize(
    "host,allowed_hosts",
    [
        ("LOCALHOST:8080", ["localhost"]),
        ("[::1]:8080", ["[::1]"]),
        ("[2001:DB8::1]:8443", ["[2001:db8::1]:8443"]),
        ("service.example:8443", ["SERVICE.EXAMPLE:8443"]),
        ("service.example", ["service.example:*"]),
        ("service.example:8080", ["service.example:*"]),
    ],
)
async def test_mcp_and_rest_accept_the_same_canonical_host_authorities(host, allowed_hosts):
    async with connected_mcp(host=host, allowed_hosts=allowed_hosts) as (session, client, _):
        rest = await client.get("/v1/profiles")
        mcp = await session.call_tool("prediction_profiles", {})
    assert rest.status_code == 200
    assert mcp.isError is False
    assert rest.json()["profiles"] == mcp.structuredContent["profiles"]


@pytest.mark.parametrize(
    "host,allowed_hosts",
    [
        ("service.example:8444", ["service.example:8443"]),
        ("service.example", ["service.example:8443"]),
        ("[::1]:8444", ["[::1]:8443"]),
        ("[::1]", ["[::1]:8443"]),
    ],
)
async def test_mcp_explicit_port_configuration_rejects_other_or_absent_ports(host, allowed_hosts):
    app = create_app(
        Settings(
            api_keys=[SERVICE_KEY],
            default_backend="mock",
            allow_mock=True,
            allowed_hosts=allowed_hosts,
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {SERVICE_KEY}", "Host": host},
        ) as client,
    ):
        rest = await client.get("/v1/profiles")
        mcp = await client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "host-regression", "version": "1"},
                },
            },
        )
    assert rest.status_code == mcp.status_code == 421
    assert rest.json()["error"]["code"] == mcp.json()["error"]["code"] == "invalid_host"
