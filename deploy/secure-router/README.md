# Router to Jev deployment

Activated 2026-10-21 at 15:43:57 UTC. This is the standalone Router/API
deployment; application integration is a separate workflow.

```text
Trusted local client on secure.techoverfl.com (192.168.20.41)
  http://127.0.0.1:9420/v1/decisions
    -> tof-model-router, native typed decision lane, four active calls
    -> http://127.0.0.1:19421, adapter bearer injected by Router
    -> tof-jev-tunnel.service, authenticated SSH through alias manatree
    -> Manatree (192.168.20.152), loopback kubectl port-forward
    -> tof-jev/service/tof-prediction-adapter:8080
    -> Laya english at 192.168.10.207:8084
```

The adapter remains the existing single-replica deployment at image digest
`sha256:92419f223f323901bfe44b569f68dfce4b527ddcfa4718f91f771b91a2fa97c1`
(source `c0ff8c354594006974c4d72b0488178284ef1b4e`). Its live ConfigMap
already sets shared inference capacity to four. This deployment uses the
existing service credential and does not activate separate consumer accounts,
new profiles, cross-namespace access, or an application integration.

Both tunnel listeners bind `127.0.0.1`. Kubernetes access uses Manatree's
existing authenticated control-plane configuration. The tunnel uses the
configured SSH alias and verifies the existing host key. Its forced remote
TTY ensures that an SSH disconnect also terminates kubectl. Systemd restarts
the tunnel when its process exits. The Router's `Wants` dependency starts the
tunnel at boot; transport failure does not stop unrelated Router lanes.

The Router trusts local clients; it substitutes its service bearer even if a
caller provides another Authorization header. The adapter still rejects
missing or incorrect credentials at the tunnel endpoint. SSH encrypts the
Secure-to-cluster leg. The adapter-to-Laya hop remains HTTP on the private LAN;
data requiring encrypted transport must wait for protection of that hop.

## Installed configuration

On Secure:

- `/etc/systemd/system/tof-jev-tunnel.service`: copy of this directory's unit.
- `/etc/systemd/system/tof-model-router.service.d/jev-tunnel.conf`: copy of
  `router-tunnel.conf`.
- `/home/zachlp2016/tof-model-router/tof-model-router.env`:
  `ROUTER_JEV_ENABLED=true`, `ROUTER_JEV_URL=http://127.0.0.1:19421`,
  `ROUTER_JEV_CAPACITY=4`, `ROUTER_JEV_TIMEOUT_SECONDS=95`.
- The credential is read from the existing mode-0600
  `/home/zachlp2016/tof-jev/.local/adapter-client-token`; never put it in source
  or request logs.

Install only after verifying both documented host identities and the
loopback port is free. From this checkout on Secure:

```sh
sudo install -m 0644 deploy/secure-router/tof-jev-tunnel.service /etc/systemd/system/
sudo install -d -m 0755 /etc/systemd/system/tof-model-router.service.d
sudo install -m 0644 deploy/secure-router/router-tunnel.conf \
  /etc/systemd/system/tof-model-router.service.d/jev-tunnel.conf
sudo systemctl daemon-reload
sudo systemctl enable --now tof-jev-tunnel.service
curl --fail http://127.0.0.1:19421/health/ready
```

Set the Router environment above, validate with its `--check` command using
the systemd EnvironmentFile values, drain active Router requests, then restart
`tof-model-router.service`. Do not shell-source the EnvironmentFile: some
hardware descriptions contain unquoted spaces. No Nginx reload is needed.

The canonical AI gateway inventory comment records the active typed lane;
Jev is intentionally absent from its OpenAI model rows. Regeneration produced
byte-identical Nginx routing files. The repository and installed inventory
hash is `014673cbc542a427c03739d4f2277803920c8c857a7758e52a352176c7fb9c66`;
the Operations snapshot reported that hash and matching Router source hash
after activation.

## Qualification

```sh
timeout 180 python3 deploy/secure-router/smoke.py
```

This makes 14 bounded real decisions, using observed deployment facts. It is
a protocol and transport qualification, not a model-quality benchmark.

Observed results on 2026-10-21:

- 14/14 real decisions succeeded, including 12 measured requests at concurrency
  four and a versioned `provider.progress.v1` call.
- Warm median 124.022 ms, maximum 196.773 ms for two-question requests;
  initial warm-up 5,045.565 ms. Reload time was not measured separately.
- Every measured response retained model `english`, configured checkpoint
  `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`, distinct Router and adapter
  request IDs, caller correlation, schema hash, `advisory_only=true`, and
  `calibrated=false`.
- Missing/wrong adapter credentials returned 401. Caller-provided credentials
  were replaced successfully by the Router's service credential.
- Invalid schemas/unknown profiles returned 422, disabled mock/hosted backends
  returned 403, and the unrouted batch API returned 404.
- Stopping the tunnel removed the remote listener and kubectl process. A
  Router decision then returned `503 jev_backend_unavailable`; Router health
  stayed 200. Starting the tunnel restored readiness.
- Deliberately killing SSH triggered one automatic systemd restart. Exactly
  one remote port-forward remained. A subsequent real decision succeeded
  (adapter latency 68.759 ms, request ID
  `da42a860-5236-4b15-a8ee-4e2b701df5ba`).
- Adapter pod stayed Ready with zero restarts.
- Router suite: 89 tests passed. The two added protocol-fault tests cover
  unchanged adapter 503/Retry-After propagation, no redispatch after timeout,
  and admission release. The timeout response now correctly has
  `retryable=false` for `jev_backend_indeterminate`.
- Adapter release-line suite: 287 passed; Ruff passed. Gateway suite: four
  passed; `nginx -t` passed with existing unrelated warnings.

The authoritative installed Router files are outside this Git repository.
An exact source snapshot is retained on Secure under
`/home/zachlp2016/deployment-staging/router-jev-transport-20261021/source/`.
Their deployment hashes are:

| File | SHA-256 |
| --- | --- |
| `router.py` | `41ba4054f3aeef8fb2fe79d9cee2b5c10557e84a739f0484251a04f84b9077b4` |
| `tof-model-router.env` | `cb4dc7e2da976e9b77b2e78f257ce5f658c2e8ec998951d8f50f3b750eb3f407` |
| `tests/test_router.py` | `b9f0fbe56e461f38a0f6a2db287a0d4489e9fd5c6fa5599bee6c0209f90371e0` |
| `tests/test_production_config.py` | `0c3b3a2707e166a3a94aa4d3c2a250336245ead319597effce16f51817ea7385` |

## Saturation benchmark

The 2026-10-21 16:07:50 UTC run qualified the full Router -> adapter -> Laya
path with ten questions per request. Four concurrent callers achieved
30.925 requests/s (309.253 questions/s), p50 128.874 ms, p95 132.364 ms,
and p99 134.669 ms with zero errors. Eight and sixteen callers maintained
approximately the same throughput with immediate Router 503 backpressure.

The matched direct-adapter control through the same tunnel reached 30.934
requests/s at four callers. The Router added approximately 5.533 ms at one
caller and had negligible observed effect on saturated throughput. The
confirmed cold reload took 1.597 seconds end to end. GPU utilization peaked
at 96%, VRAM at 2,902 MiB, and temperature at 55 C. No service restarted.

This measures the current four-global-call release, not the unshipped
`216cf04` three-per-consumer policy. The earlier benchmark's precise payload
and backoff differ, so the results support similar capacity rather than a
controlled speed-improvement claim. The complete record and raw samples are
under `deployment-staging/tof-jev-kubernetes-20261021/README.md` and
`deployment-staging/router-jev-benchmark-20261021` on Secure.

Repeat this bounded run on Secure:

```sh
timeout --signal=TERM --kill-after=3s 180s python3 deploy/secure-router/benchmark.py \
  --seconds 10 --output /secure/evidence/path/results.json \
  --adapter-key-file /home/zachlp2016/tof-jev/.local/adapter-client-token
```

The optional key-file argument enables direct-adapter control tiers. Router
tiers need no adapter credential. Every tier honors `Retry-After`; reported
percentiles include successes only, and throughput includes final drain.

## Rollback

Set `ROUTER_JEV_ENABLED=false` and empty `ROUTER_JEV_URL`, drain requests,
and restart the Router. Remove only its `jev-tunnel.conf` drop-in, reload
systemd, then disable/stop `tof-jev-tunnel.service`. Keep the correct
nonretryable timeout response. The adapter and Laya can remain running.
No database, application deployment, or model weights need to be restored.
