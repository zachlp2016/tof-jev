# Clients

The service is independent of every client application. No Rails component, MCP installation in another application, database migration, or application-specific authorization is required to run it.

## PowerShell REST

```powershell
$headers = @{ Authorization = 'Bearer local-development-key-change-before-deploying' }
$body = Get-Content -Raw examples/decision.json
Invoke-RestMethod -Uri http://127.0.0.1:8080/v1/decisions -Method Post -Headers $headers -ContentType 'application/json' -Body $body
Invoke-RestMethod -Uri http://127.0.0.1:8080/v1/calculations -Method Post -Headers $headers -ContentType 'application/json' -Body '{"operation":"ratio","numerator":150,"denominator":100}'
```

## Python REST

```python
import os
import httpx

with httpx.Client(base_url="http://127.0.0.1:8080", timeout=35,
                  headers={"Authorization": f"Bearer {os.environ['TOF_CLIENT_API_KEY']}"}) as client:
    response = client.post("/v1/decisions", json={
        "decision_key": "provider.progress.v1",
        "state": {"activity": ["Fetched source", "Verified claim against original data"]},
        "data_classification": "internal",
    })
    response.raise_for_status()
    observation = response.json()
```

## Python MCP

```python
import asyncio
import os
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with httpx.AsyncClient(headers={
        "Authorization": f"Bearer {os.environ['TOF_CLIENT_API_KEY']}"
    }) as http:
        async with streamable_http_client("http://127.0.0.1:8080/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("prediction_calculate", arguments={
                    "operation": "bayes", "prior": 0.1,
                    "sensitivity": 0.8, "false_positive_rate": 0.2,
                })
                if result.isError:
                    raise RuntimeError(result.structuredContent)
                print(result.structuredContent)

asyncio.run(main())
```

MCP tool arguments use the same flat JSON objects as REST, without a nested `request` wrapper. MCP errors set `isError=true` and return the same sanitized `{error, request_id}` envelope. Configure your client for Streamable HTTP, endpoint `/mcp`, and an Authorization bearer header. This release does not expose stdio or the old HTTP+SSE transport.

## Custom decision schema

```json
{
  "state": "A customer says their invoice is duplicated.",
  "questions": {
    "queue": {
      "type": "choice",
      "instructions": "Which support queue fits this message?",
      "criteria": {"billing": "Invoice and payment questions", "technical": "Product troubleshooting"}
    },
    "urgency": {
      "type": "score",
      "instructions": "Assess urgency from the supplied message only.",
      "criteria": ["Routine", "Soon", "Immediate"]
    },
    "needs_review": {
      "type": "noul",
      "instructions": "Does the customer describe a potential billing error?"
    }
  }
}
```

Supported bounds: 50,000 serialized state characters, nesting 32, 64 questions, 2–100 choices, 2–10 score levels, 512 combined options, 16 batch items, and a default 256 KiB entire request body. Question instructions and criterion descriptions are bounded strings. Unknown fields, boolean-as-number coercion, nonfinite numbers, duplicate JSON keys, and oversized bodies are rejected.

## Mathematics

| Operation | Required fields | Returned `result` |
| --- | --- | --- |
| `ratio` | `numerator`, `denominator` | `ratio` |
| `bayes` | `prior`, `sensitivity`, `false_positive_rate` | `posterior`, `evidence_probability` |
| `expected_value` | `outcomes: [{value, probability}, ...]` | `expected_value`, `variance`, `standard_deviation` |
| `normal_cdf` | `x`; optional `mean=0`, `standard_deviation=1` | `probability` for X ≤ x |
| `binomial` | `n`, `k`, `probability`; optional `mode=exact` | `probability` |

Binomial modes: `exact`, `at_most`, `at_least`; maximum n=10,000. Ratios reject denominator zero; expected-value probabilities must sum to one; standard deviation must be positive. These are deterministic floating-point computations under supplied assumptions, not data-derived forecasts. Results carry method and assumptions. No expression evaluation or arbitrary Python is accepted.

## Errors and batches

REST errors return `{ "error": { "code": "...", "message": "...", "retryable": false }, "request_id": "..." }`. HTTP statuses include 401 authentication, 403 policy, 413 size, 415 media type, 422 schema/calculation, 502 invalid backend response, 503 saturation/unavailability, and 504 timeout. Body-read timeouts return 408. Validation and upstream errors never echo the supplied state or upstream error body.

Batches use `{ "items": [decisionRequest, ...] }`. Policy/profile checks run for all items before any upstream submission. After preflight, results preserve order as `{ok:true,result:...}` or `{ok:false,error:...,request_id:...}`. An external request can still consume shared capacity and yield a retryable item error. Batch items have individual correlation IDs and the outer HTTP request has another ID. Batch failures are not transactions and do not undo prior inference.
