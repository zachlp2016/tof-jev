# Operations

## Isolation and deployment boundary

This repository builds only the CPU adapter. The model server is an independent service with its own model revision, hardware, cache, GPU runtime and health checks. The adapter does not access Docker's socket, caller databases, Job systems, Rails code, or local files containing application state.

The process owns no persistent data. Each replica has bounded in-flight requests, inference capacity and body size. MCP is stateless and can be load balanced without session affinity. Limits and metrics are per process/replica; run one worker per container and scale replicas deliberately. There is no global distributed quota, durable request queue, result cache or exactly-once processing.

## Configuration

All settings use the `TOF_` prefix; `.env.example` describes normal settings. Docker Compose passes an untracked `.env`; direct Python execution reads process environment only.

- `API_KEYS`: JSON list, minimum 24 characters per key; empty configuration fails startup. Rotate by deploying `[old,new]`, updating clients, then removing old. Generate using `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `DEFAULT_BACKEND`: `laya`, `jev` or explicit development `mock`.
- `LAYA_BASE_URL`, `LAYA_API_KEY`, `LAYA_MODEL`, `LAYA_MODEL_REVISION`: local inference service and operator-declared checkpoint identity. Model revision is not independently attested by the adapter.
- `ENABLE_HOSTED`, `JEV_BASE_URL`, `JEV_API_KEY`, `JEV_MODEL`: hosted configuration. HTTPS is required. Default Jev model is pinned to `jev-1.13.0`; availability belongs to the provider.
- `ALLOW_MOCK`: false by default. Base Compose forces false; development override enables it and marks results synthetic.
- `REQUEST_TIMEOUT_SECONDS`: total inference deadline, default 30; upstream client also has phase timeouts. No retries.
- `MAX_CONCURRENT_REQUESTS`: shared inference slots, default 4. Enabled batches schedule within this bound. Saturation fails quickly with retryable 503. The four-slot default is the measured V100 saturation point for ten-question requests; increasing it requires a new workload-shaped capacity qualification.
- `MAX_CONCURRENT_REQUESTS_PER_CONSUMER`: nonblocking per-credential admission ceiling, default 3. Use distinct entries in `CONSUMER_API_KEYS` for Parallax and TOF so neither can occupy every shared slot.
- `ENABLE_BATCH`: false by default. Prefer one state with multiple questions; enabling batches requires a separate weighted-admission review.
- `MAX_INFLIGHT_REQUESTS`: total per-process HTTP ingress bound, default 64.
- `BODY_TIMEOUT_SECONDS`: deadline to read a request body, default 10.
- `MAX_BODY_BYTES`: default 262,144. Both declared length and streamed bytes are checked.
- `MAX_RESPONSE_BYTES`: default 1,048,576. Upstream compressed bodies and redirects are refused.
- `ALLOWED_HOSTS`: JSON array of exact service hostnames (optional `:*` port suffix). Configure reverse-proxy/Kubernetes names explicitly. Arbitrary wildcard hosts are not accepted.
- `ALLOWED_ORIGINS`: JSON array, empty by default. Requests with an Origin header require an exact match. There is no general browser CORS API; trusted server-to-server clients normally send no Origin.

Use TLS at an ingress/reverse proxy before exposing beyond localhost or a trusted isolated network. Configure that proxy to forward the configured Host and Authorization headers and enforce appropriate caller policy. Credentials are not delegated to upstream: upstream tokens come only from operator configuration. Environment proxy variables are ignored for backend traffic.

## Health and logs

`/health/live` checks the process. `/health/ready` checks the configured default backend: Laya `/health`, Jev `/v1/models`, or explicit mock mode. When a Laya key is configured, readiness requires the authenticated detail fields rather than accepting Laya's intentionally public status-only liveness response; an empty `loaded` list remains ready because idle-unloaded checkpoints can reload on demand. It does not make a prediction or validate checkpoint quality. An unavailable optional backend does not change readiness for the selected default. The mathematical endpoints remain functional when an inference backend is unavailable.

The TechOverFL V100 service currently uses `http://192.168.10.207:8084`. If the adapter runs on another machine, that HTTP hop carries the Laya bearer and request state without transport encryption; use an authenticated tunnel/TLS proxy or an explicitly trusted isolated network. The stock Kubernetes policy targets a same-namespace Laya pod on port 8080 and must not be applied unchanged for the bare-metal V100 endpoint.

Structured request logs include generated request ID, authenticated service consumer, bounded operation label, HTTP status and duration. They omit state, arbitrary query strings, bearer tokens and raw upstream payloads. Metrics use only configured service names, bounded backend/outcome/scope labels, operations and HTTP statuses; never request IDs, JobRun IDs, credentials or user text. Protect `/metrics` using the service token. MCP errors are represented inside successful HTTP protocol responses; inspect MCP `isError`, not just HTTP status.

The REST and MCP decision paths share `tof_adapter_decisions_total` (consumer,
backend, outcome), `tof_adapter_questions_total` (successful questions),
`tof_adapter_decision_duration_seconds` (admitted calls, including failures),
`tof_adapter_admission_rejections_total` (consumer/global scope), and active
global/per-consumer gauges. Admission is immediate: there is no inference queue,
no server retry, and overflow returns 503 with `Retry-After: 1`. HTTP ingress
overload is separately bounded and also returns this header. These metrics are
available at authenticated `/metrics`; deployment does not imply a Prometheus
scraper or alerting service has been installed.

## Kubernetes

Manifests are templates to review for the destination cluster; they have not been applied by development. Select an immutable pushed image digest in `deploy/kubernetes/deployment.yaml`. The adapter requests no GPU. Deploy the inference workload separately.

```sh
kubectl apply -f deploy/kubernetes/namespace.yaml
# Create secrets out of band; do not commit literal credentials.
kubectl -n tof create secret generic tof-prediction-adapter-secrets \
  --from-literal=TOF_API_KEYS='["REPLACE_WITH_A_GENERATED_TOKEN_AT_LEAST_24_CHARACTERS"]'
kubectl apply -f deploy/kubernetes/configmap.yaml
kubectl apply -f deploy/kubernetes/deployment.yaml
kubectl apply -f deploy/kubernetes/service.yaml
kubectl apply -f deploy/kubernetes/networkpolicy.yaml
```

Check exact filenames before use; the secret template and hosted-egress policy are deliberately separate opt-ins. Do not apply the directory recursively. Supply the local backend token in the Secret if enabled. Network policy admits same-namespace client pods and pods in the `tof-ai-app` or `tof-ai-app-test` namespaces labeled `tof-prediction-client=true`. It allows DNS and the separately deployed local Laya pod label. Readiness probes send Host `localhost` to satisfy host protection.

The `deploy/kubernetes/node-host-2-v100` Kustomize overlay instead permits only
the bare-metal Laya address `192.168.10.207:8084`, selects an immutable GHCR
image, and runs one replica. Its Secret remains deliberately out of source
control. Callers require the `tof-prediction-client=true` label. The checked-in
overlay policy admits labeled callers only in `tof-jev`; the generic base's
staged application namespace access is explicitly removed by this overlay.
It uses `Recreate` and a 120-second termination grace period. Admission is
process-local: keep one replica and one Uvicorn worker, with no HPA. Replacements
have a brief outage rather than overlapping two independent four-slot pools.
Scaling requires distributed admission or a separately allocated inference pool.

Hosted mode additionally requires pod label `tof-prediction-hosted=enabled`, explicit hosted settings/secrets, and the optional hosted egress policy. The stock policy permits public IPv4 TCP/443 rather than enforcing a domain allowlist. Use a cluster-specific FQDN policy or controlled egress proxy if the destination must be restricted to TypeSafe. A NetworkPolicy-enforcing CNI is required for these controls to take effect.

The runtime uses UID/GID 10001, a read-only root filesystem, temporary `/tmp`, dropped capabilities and no privilege escalation. There are no host mounts or Docker socket mounts.

## Model acceptance and future expansion

The Secure Router's native `/v1/decisions` route is active through the
[supervised SSH/Kubernetes transport](../deploy/secure-router/README.md).
The infrastructure isolation rollout retains the four-call shared limit and
uses distinct `router` and `operations` credentials, each limited to three.
Router traffic is one consumer regardless of caller-supplied identity headers.
The operations credential is for controlled infrastructure validation, not an
application integration. Multi-state batch endpoints remain disabled; a single
state may still contain ten questions. The 25 requests/sec target is capacity
guidance for combined traffic, not a configured rate limiter or guaranteed SLA.
Its loopback API, transport failure/recovery, real inference, and provenance
were qualified independently of any application integration.

Before using model scores to influence application behavior, collect a representative held-out labeled dataset, pin question schema and checkpoint, and assess accuracy, Brier/log loss and calibration. Keep training, calibration and test sets separate. No calibration dataset or trained forecasting model is supplied here, and `calibrated` remains false. Model evaluation and V100 inference are separate from adapter contract tests.

To add a backend, implement a bounded transport/normalizer, register explicit routing configuration and privacy policy, add contract fixtures, and extend discovery. To change a profile, add a new versioned key rather than silently changing an established schema. Consumers should store the returned schema hash and model identity with their own observations.
