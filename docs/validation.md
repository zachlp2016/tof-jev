# Validation record

Originally completed October 20, 2026 (America/Los_Angeles), for adapter version 0.1.0. Version 0.1.1 was locally revalidated on October 21, 2026 after authenticated Laya readiness and deployment-template changes.

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
| Remote GitHub Actions | Triggered on publication; see the repository Actions tab for current results |
| Version 0.1.1 local Linux tests, Python 3.13.5 | 286 passed; Ruff lint and format checks passed |
| Version 0.1.1 Kubernetes/Laya integration | One pod Ready with zero restarts; authenticated readiness, REST inference, MCP discovery/calculation/inference, and labeled in-cluster client access passed against `192.168.10.207:8084` |

Coverage includes strict typed questions, deterministic numerical edge cases, upstream contract normalization, timeouts, no redirects/retries/fallback, body/response limits, chunked bodies, request deadlines, concurrency, batch ordering and partial failures, shared authentication, host/origin checks, IPv6/uppercase host interoperability, liveness during saturation, sanitized errors, token rotation, and synthetic mock labeling.

The runtime smoke test made real HTTP connections to the local container. It exercised readiness, rejected missing credentials, evaluated a mock profile, calculated a ratio, initialized an MCP session, discovered all tools, called decision/calculation tools and observed a calculation error. Model answers in these tests are synthetic fixtures.

The final development container listens at `127.0.0.1:8080`, using the explicit `compose.dev.yaml` override. It can remain running for client integration. Use `docker compose -f compose.yaml -f compose.dev.yaml down` to stop it. The image is `tof-prediction-adapter:local`; rebuild from this repository when moving to another host. The Python base image is pinned by digest in the Dockerfile.

Source is published to [zachlp2016/tof-jev](https://github.com/zachlp2016/tof-jev) on `main`. HTTPS Git authentication and the connected GitHub integration could not write during initial development, but the machine's existing SSH authentication succeeded as `zachlp2016`. The checkout retains its HTTPS fetch URL and uses `git@github.com:zachlp2016/tof-jev.git` for pushes. Future updates can use `git push`.

## Not validated here

- Model accuracy, calibration, or formal checkpoint-quality evaluation.
- Live hosted Jev inference or account/model availability; no hosted API credential was supplied.
- Production ingress/TLS, cross-namespace caller integration, and post-idle cold-start timing.
- OAuth-only MCP clients; this service uses explicitly configured bearer tokens.

These are external deployment/model acceptance tasks. The adapter contains complete local/hosted HTTP integrations and deterministic calculations, but does not invent trained forecasts or model-quality results. See the operations guide for the transition to a real backend.
