# TOF Prediction Adapter

Independent Python service for typed Laya/Jev decisions and deterministic mathematics, available over REST and MCP. It runs without Rails, a database, a GPU, or a model download. Any authorized application can be a client.

```text
Application / operator / MCP client
                 |
        REST + Streamable HTTP MCP
                 |
       tof-prediction-adapter       (this repository, CPU container)
          |              |
     local Laya       hosted Jev    (separate, operator-configured services)
     GPU worker       opt-in only
```

The adapter validates inputs, selects an explicitly configured backend, normalizes responses, and returns provenance for the caller to store. It has no workflow authority, Job execution, receipt database, application dependencies, or automatic remote failover. `job_run_id` and `evidence_refs` are optional opaque caller metadata and are not sent upstream. They do not authorize anything.

## Start locally in Docker

```sh
docker compose -f compose.yaml -f compose.dev.yaml up --build -d
```

This explicit development override enables **synthetic uniform mock answers**, not learned predictions. Mathematical calculations are real deterministic computations. The adapter binds only to `127.0.0.1:8080`.

```sh
curl http://127.0.0.1:8080/health/ready
curl -H 'Authorization: Bearer local-development-key-change-before-deploying' \
  -H 'Content-Type: application/json' \
  -d '{"state":{"claim":"Example claim","source_summary":"Example excerpt"},"decision_key":"research.evidence_screen.v1"}' \
  http://127.0.0.1:8080/v1/decisions
```

For PowerShell use `curl.exe` or the examples in [the client guide](docs/clients.md). API documentation and OpenAPI are authenticated at `/docs` and `/openapi.json`; fetch the schema with the same bearer token. Browser navigation to `/docs` also needs an Authorization header, e.g. through your authenticated gateway.

```sh
docker compose -f compose.yaml -f compose.dev.yaml logs --tail 50
docker compose -f compose.yaml -f compose.dev.yaml down
```

## Connect a real backend

Copy `.env.example` to untracked `.env`, generate a service token of at least 24 characters, and set `TOF_API_KEYS` to a JSON list containing it. Configure `TOF_LAYA_BASE_URL` and, if applicable, `TOF_LAYA_API_KEY`. The base URL is the service root, without `/v1/systemone` or a path prefix. Start the production configuration:

```sh
docker compose up --build -d
```

The normal Compose configuration disables mock inference. With no running backend, liveness passes and readiness returns 503; prediction requests return an explicit backend error. No synthetic predictions are substituted.

Laya runs separately. Attach its container to Docker network `tof-prediction` with DNS name `laya-inference`, or use `http://host.docker.internal:PORT` for a host service. Pin its checkpoint/revision in that service, configure the corresponding `TOF_LAYA_MODEL` and `TOF_LAYA_MODEL_REVISION`, and verify its authenticated `/health` response. The adapter does not install CUDA, load weights, or validate V100 compatibility. That belongs to the separate inference deployment.

For TechOverFL's bare-metal Laya service on `node-host-2`, use the fail-closed template and placement notes in [`deploy/node-host-2-v100`](deploy/node-host-2-v100/README.md). Its longer initial timeout accommodates a checkpoint reload after idle VRAM release.

Hosted Jev requires all of:

- Operator configuration `TOF_ENABLE_HOSTED=true` and `TOF_JEV_API_KEY`.
- Request routing to `backend: "jev"` (or an operator-selected default).
- Request `data_classification: "public"` and `allow_remote: true`.

Internal, confidential and restricted requests are never sent to hosted Jev. Classification is an assertion by the trusted calling service, not content inspection; use application policy before making that assertion. There are no automatic retries or backend fallbacks. A timed-out request may already have been processed or billed.

## Interfaces

| REST endpoint | MCP tool | Purpose |
| --- | --- | --- |
| `POST /v1/decisions` | `prediction_decide` | Versioned profile or custom questions |
| `POST /v1/decisions/batch` | `prediction_batch` | Up to 16 ordered item results |
| `POST /v1/calculations` | `prediction_calculate` | Ratio, Bayes, expected value, normal CDF, binomial |
| `GET /v1/profiles` | `prediction_profiles` | Profile definitions |
| `GET /v1/backends` | — | Configured capabilities, without credentials |
| `GET /health/live` | — | Process liveness; unauthenticated |
| `GET /health/ready` | — | Default backend reachability; unauthenticated |
| `GET /metrics` | — | Authenticated Prometheus metrics |

MCP endpoint: `http://127.0.0.1:8080/mcp`, using the official MCP SDK with stateless Streamable HTTP and JSON responses. The same bearer-token, host, origin and payload boundaries cover both interfaces. This is service-to-service token authentication; OAuth discovery/authorization is not implemented. Use a client that accepts an explicit Authorization header or an appropriate gateway.

Decision requests provide exactly one of `decision_key` or `questions`. Questions support `choice`, `score`, and `noul`, using a deliberately bounded common Laya/Jev subset. See [verified upstream contracts](docs/upstream-contracts.md) and [examples](examples/decision.json).

Built-in advisory profiles:

- `research.evidence_screen.v1`: relevance, independence, verification priority.
- `provider.progress.v1`: progress, repetition, blockage, insufficient context.
- `job.complexity.v1`: complexity and vision requirements.

Normalized answers contain `type`, `value`, and, where applicable, `probabilities`, `confidence`, and `confidence_kind`. Scores are fractional zero-based rubric indices, not a universal 0–1 scale. Noul is a model assessment in [0,1]. Laya entropy confidence and Jev confidence retain different labels. Nothing in this release declares those estimates calibrated.

Results include request ID, UTC timestamp, profile/schema hash, backend, reported model, configured/reported revision provenance, timing, safe usage counters and optional caller references. No request state or raw upstream diagnostics are persisted. `advisory_only=true`, `calibrated=false` and `calibration_version=null` make the current boundary explicit. There is no trained time-series forecasting endpoint.

## Develop and verify

Python 3.12 is the tested baseline. The universal lockfiles include platform markers for Windows/Linux.

```sh
python -m venv .venv
# Activate .venv for your shell, then:
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest
python -m ruff check src tests scripts
docker build --target test -t tof-prediction-adapter:test .
```

The tests use synthetic upstream fixtures and HTTP transports; they exercise contracts and security, not model quality. `scripts/smoke.py` calls a running container using both REST and a real MCP client. It requires the explicit mock deployment:

```sh
export TOF_SMOKE_API_KEY=local-development-key-change-before-deploying
python scripts/smoke.py --url http://127.0.0.1:8080
```

To regenerate lockfiles with `uv`: `uv pip compile pyproject.toml requirements-build.in --python-version 3.12 --universal -o requirements.lock`, then add `--extra dev -o requirements-dev.lock` for development. Audit dependencies before deployment. CI tests the container and starts its hardened runtime.

See [operations and Kubernetes](docs/operations.md), [client examples](docs/clients.md), and [validation record](docs/validation.md).
