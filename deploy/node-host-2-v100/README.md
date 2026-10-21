# node-host-2 V100 adapter deployment

This overlay targets the existing authenticated Laya service at
`192.168.10.207:8084`. The adapter remains CPU-only and does not own either
V100, Laya's systemd unit, or its checkpoint cache.

1. Copy `adapter.env.example` to the repository root as untracked `.env`.
2. Generate a distinct adapter client token for `TOF_API_KEYS`.
3. Obtain the Laya token and English checkpoint revision on the inference host
   without printing either into logs or committing them.
4. Start the normal `compose.yaml`; it binds the adapter to loopback only.
5. Verify authenticated readiness, one REST decision, one MCP decision, and a
   cold request after Laya's five-minute idle unload.

Do not use the stock Kubernetes NetworkPolicy for this bare-metal endpoint. If
the adapter is placed off-host, protect the upstream hop with TLS or an
authenticated tunnel before sending non-public state.

## Kubernetes overlay

`deploy/kubernetes/node-host-2-v100` deploys one stateless adapter replica using
the immutable GHCR image produced from commit `c0ff8c3`. It configures the
reviewed English checkpoint revision and adds narrowly scoped egress to
`192.168.10.207:8084`; the base policy continues to provide DNS egress and
label-gated ingress. Create `tof-prediction-adapter-secrets` out of band before
applying the overlay. The deployment does not use a PVC or service-account
token.

The overlay is deployed to the dedicated `tof-jev` namespace. The replacement
pod became Ready on `mananode-2` with no restarts. Authenticated REST and MCP decisions,
model revision provenance, and a labeled in-cluster client call through the
Service and NetworkPolicy all passed. The deployed image resolves to
`sha256:92419f223f323901bfe44b569f68dfce4b527ddcfa4718f91f771b91a2fa97c1`.
The ConfigMap explicitly sets `TOF_MAX_CONCURRENT_REQUESTS=4`, matching the
measured V100 saturation point. This remains a system-wide adapter limit even
when multiple callers submit batched questions.

The current overlay references base manifests above its directory. From a
checked-out repository, render it with:

```sh
kubectl kustomize --load-restrictor LoadRestrictionsNone \
  deploy/kubernetes/node-host-2-v100 | kubectl apply --server-side -f -
```
