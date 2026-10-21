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
