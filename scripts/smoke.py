"""Exercise a running explicit mock deployment over REST and real MCP HTTP."""

import argparse
import asyncio
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def smoke(url: str, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=url, headers=headers, timeout=30, trust_env=False
    ) as client:
        ready = await client.get("/health/ready")
        ready.raise_for_status()
        assert ready.json()["synthetic"], "Smoke test expects the explicit mock deployment"
        unauthorized = await client.get("/v1/profiles", headers={"Authorization": ""})
        assert unauthorized.status_code == 401
        prediction = await client.post(
            "/v1/decisions",
            json={
                "state": "An example research source.",
                "decision_key": "research.evidence_screen.v1",
            },
        )
        prediction.raise_for_status()
        result = prediction.json()
        assert result["backend"] == "mock" and result["advisory_only"] and not result["calibrated"]
        calculation = await client.post(
            "/v1/calculations", json={"operation": "ratio", "numerator": 3, "denominator": 2}
        )
        calculation.raise_for_status()
        assert calculation.json()["result"]["ratio"] == 1.5
        async with streamable_http_client(url.rstrip("/") + "/mcp", http_client=client) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "prediction_decide",
                    "prediction_batch",
                    "prediction_calculate",
                    "prediction_profiles",
                }
                answer = await session.call_tool(
                    "prediction_calculate",
                    arguments={"operation": "ratio", "numerator": 7, "denominator": 2},
                )
                assert not answer.isError and answer.structuredContent["result"]["ratio"] == 3.5
                decision = await session.call_tool(
                    "prediction_decide",
                    arguments={"state": "Example", "decision_key": "provider.progress.v1"},
                )
                assert not decision.isError and decision.structuredContent["backend"] == "mock"
                error = await session.call_tool(
                    "prediction_calculate",
                    arguments={"operation": "ratio", "numerator": 1, "denominator": 0},
                )
                assert error.isError
    print(
        "PASS: runtime readiness, authentication, REST decision/calculation, MCP initialize/discovery/decision/calculation/error"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    key = os.environ.get("TOF_SMOKE_API_KEY")
    if not key:
        parser.error("set TOF_SMOKE_API_KEY (not a command-line secret)")
    asyncio.run(smoke(args.url, key))
