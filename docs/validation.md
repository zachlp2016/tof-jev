# Validation record

Completed October 20, 2026 (America/Los_Angeles), for adapter version 0.1.0.

| Check | Result |
| --- | --- |
| Python tests on Windows, Python 3.12.14 | 284 passed |
| Tests in Linux Docker build, Python 3.12.15 | 284 passed |
| Ruff lint and formatting | Passed |
| Locked runtime dependencies, pip-audit | No known vulnerabilities reported |
| Docker runtime image build | Passed |
| Production and development Compose parsing | Passed |
| Runtime user/read-only filesystem/capabilities | UID/GID 10001, read-only root, ALL capabilities dropped |
| Running-container REST and real MCP SDK smoke test | Passed |
| Docker health | Healthy |
| Remote GitHub Actions | Not run: repository write access was unavailable during development |

Coverage includes strict typed questions, deterministic numerical edge cases, upstream contract normalization, timeouts, no redirects/retries/fallback, body/response limits, chunked bodies, request deadlines, concurrency, batch ordering and partial failures, shared authentication, host/origin checks, IPv6/uppercase host interoperability, liveness during saturation, sanitized errors, token rotation, and synthetic mock labeling.

The runtime smoke test made real HTTP connections to the local container. It exercised readiness, rejected missing credentials, evaluated a mock profile, calculated a ratio, initialized an MCP session, discovered all tools, called decision/calculation tools and observed a calculation error. Model answers in these tests are synthetic fixtures.

The final development container listens at `127.0.0.1:8080`, using the explicit `compose.dev.yaml` override. It can remain running for client integration. Use `docker compose -f compose.yaml -f compose.dev.yaml down` to stop it. The image is `tof-prediction-adapter:local`; rebuild from this repository when moving to another host. The Python base image is pinned by digest in the Dockerfile.

Source is committed in the local checkout with `origin` set to `https://github.com/zachlp2016/tof-jev.git`. Publishing was attempted but blocked: command-line Git had no configured login and the connected GitHub integration returned HTTP 403 on repository writes. No remote branch was published. Once write access is configured, `git push -u origin main` publishes the complete repository and starts CI.

## Not validated here

- Real Laya inference, model weights, model accuracy, calibration or V100/CUDA compatibility.
- Live hosted Jev inference or account/model availability; no hosted API credential was supplied.
- Kubernetes deployment, cluster network-policy enforcement, production ingress or TLS.
- OAuth-only MCP clients; this service uses explicitly configured bearer tokens.

These are external deployment/model acceptance tasks. The adapter contains complete local/hosted HTTP integrations and deterministic calculations, but does not invent trained forecasts or model-quality results. See the operations guide for the transition to a real backend.
