# Prediction backend contracts

Verified against upstream documentation and source on October 20, 2026. These
are HTTP contract checks, not live accuracy or GPU qualification results.

The Laya snapshot inspected was commit
[`ffc142012af63d015fa19f05f29f9c5ff329a2cc`](https://github.com/NandhaKishorM/laya/commit/ffc142012af63d015fa19f05f29f9c5ff329a2cc),
whose `pyproject.toml` reports package version `0.4.12`. An independently deployed
Laya server can run a different version; pin and record its image and model
artifacts before comparing evaluations.

## Shared request and answer shape

Both services expose `POST /v1/systemone`. The adapter's configured base URL must
be the service root (scheme, host, and optional port only); application settings
reject reverse-proxy path prefixes and the `/v1/systemone` suffix. Hosted Jev uses
`https://api.typesafe.ai` and requires
Bearer authentication. Laya accepts optional Bearer authentication, configured
on its server with `LAYA_API_KEY`.

A representative text-only request is:

```json
{
  "model": "jev-1.13.0",
  "state": {"message": "A duplicate invoice must be resolved today."},
  "questions": {
    "queue": {
      "type": "choice",
      "instructions": "Which team should review the message?",
      "criteria": {"billing": "Invoices and payments", "technical": "Application issues"}
    },
    "urgency": {
      "type": "score",
      "instructions": "How soon does this require attention?",
      "criteria": ["Routine", "Soon", "Immediate"]
    },
    "intervention": {
      "type": "noul",
      "instructions": "Does this require human review?"
    }
  }
}
```

`model` is required by Jev and optional in Laya. State may be a string, object,
or array. Choice criteria map option names to descriptions. Score criteria are
ordered levels starting at zero. Noul criteria may optionally describe `true`
and `false`. Structured descriptions exist upstream; the adapter may expose a
smaller, explicitly validated text-only contract.

An illustrative response (not a measured prediction) is:

```json
{
  "model": "jev-1.13.0",
  "answers": {
    "queue": {
      "type": "choice",
      "choice": "billing",
      "probabilities": {"billing": 0.9, "technical": 0.1},
      "confidence": 0.8
    },
    "urgency": {
      "type": "score",
      "score": 1.7,
      "legend": {"0": "Routine", "1": "Soon", "2": "Immediate"},
      "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8},
      "confidence": 0.55
    },
    "intervention": {"type": "noul", "noul": 0.92}
  },
  "usage": {"input_tokens": 120, "output_tokens": 12}
}
```

Scores are expected level indices, including fractional values, bounded by zero
and `len(criteria) - 1`. Score probability and legend keys are stringified
indices. Noul is the probability of yes/true and has no separate Jev confidence.
Answers retain the exact request IDs. Sources: [TypeSafe API reference](https://docs.typesafe.ai/api/system-one),
[official OpenAPI schema](https://api.typesafe.ai/openapi.json).

## Laya extensions and confidence semantics

The normal Laya response adds a root `routing` object. Its `model` identifies
the selected checkpoint, while the response's top-level `model` normally says
`laya-rl-agent`. The standard checkpoints are `english`, `multilingual`, and
`typed-decisions`. Public checkpoint aliases and registered extra models can
select them; an ordinary Jev model ID is accepted as a request to route
automatically. An unregistered path or Hub repository ID is rejected.

Laya may add these answer fields:

| Field | Meaning |
| --- | --- |
| `confidence` on choice/score | Normalized entropy: `1 - H(p)/log(k)` |
| `confidence` on noul | Maximum of the yes/no probabilities |
| `answer_confidence` | Maximum answer probability |
| `x_jev_confidence` | Jev's confidence formula applied to the distribution, for choice/score |
| `action.act_probability` | Laya action-head output; does not grant TOF workflow authority |

The adapter preserves native `confidence` with `confidence_kind` set to
`laya_entropy`, `laya_max_probability`, or `jev_reported` as appropriate. It
does not synthesize confidence from a probability and does not interchange
thresholds between these quantities. Establish thresholds with labeled TOF
evaluation cases for the deployed model.

`LAYA_JEV_STRICT=1` removes extensions but **does not recompute confidence**.
Consequently a strict Laya response still has Laya confidence semantics. Full
responses are preferable when truncation and actual checkpoint provenance are
needed. Additional Laya usage fields include `state_tokens`,
`state_tokens_dropped`, `truncated`, `truncated_questions`, and sometimes
option-span diagnostics. Successful predictions also carry
`X-Inference-Time-Ms` and `Server-Timing` headers.

`GET /health` is open for liveness. With `LAYA_API_KEY` configured, an
unauthenticated caller receives only `{"status":"ok"}`. Authenticated health
can report loaded checkpoint names, revision SHAs, measured devices, and CPU
fallbacks. A healthy process alone does not prove that GPU inference is ready.

Sources: [Laya HTTP documentation at the inspected commit](https://github.com/NandhaKishorM/laya/blob/ffc142012af63d015fa19f05f29f9c5ff329a2cc/docs/http-api.md),
[HTTP implementation](https://github.com/NandhaKishorM/laya/blob/ffc142012af63d015fa19f05f29f9c5ff329a2cc/laya/serve.py).

## Versions and limits

At verification time the TypeSafe model page listed `jev-latest` as
`jev-1.13.0` and `jev-preview` as `jev-1.14.0`. Aliases may move; the response's
model field identifies the version used. Pin a version when evaluating stable
thresholds. `GET /v1/models` requires authentication and lists models available
to the account. [TypeSafe models](https://docs.typesafe.ai/models).

| Limit | Laya default | Hosted Jev |
| --- | --- | --- |
| Body | 2 MiB | 32 MiB |
| State | 50,000 serialized characters | Model context limit |
| Questions | 64 | Combined/per-question context limits |
| Choice options | 100 | 255 |
| Score levels | 32 | 10 |
| Combined options | 512 | Context limits |

Laya can raise its choice limit to 255 with tournament mode, which changes how
the distribution is produced. The adapter does not enable or assume that mode.
The Laya state count uses the supplied string or Python's
`json.dumps(state, ensure_ascii=False)` for structured state. Request counts do
not eliminate token truncation; inspect the response metadata too.

TypeSafe requires finite JSON numbers and limits nesting to 64 levels. Its
documented errors include 400, 401, 413, 415, 422, 429, and 529. The last two
indicate rate limiting or temporary overload. [Request rules and errors](https://docs.typesafe.ai/api/errors).

Laya uses 400 for malformed input, 401 for failed authentication, 413 for size
limits, 422 for invalid questions/controls, 500 for inference failure, and 503
when admission capacity is exhausted. Its 503 response includes
`Retry-After: 1`. [Laya limits and errors](https://github.com/NandhaKishorM/laya/blob/ffc142012af63d015fa19f05f29f9c5ff329a2cc/docs/http-api.md).

## Adapter behavior

The upstream transport makes one request and refuses redirects. It requests
identity encoding, refuses unexpected compressed responses, and limits bytes
read independently of `Content-Length`. Invalid JSON, duplicate object keys,
non-finite numbers, and non-object response roots are rejected. Errors are
sanitized: upstream response bodies, private state, and API keys are never
included in the adapter error message.

Answer normalization checks exact question IDs, matching types, allowed choice
values, finite numeric bounds, complete probability labels, and score legends.
Distributions must sum to one within 0.01, accommodating Laya's rounded
probabilities; values are preserved rather than silently rescaled. Unsupported
backend extensions are not copied into normalized answer objects. Transport
results retain the raw root for the service's model/usage metadata handling.

There are no automatic inference retries. Timeouts, connection failures,
rate limits, and upstream server errors are marked retryable for the caller;
a timeout may still represent a processed or billable request. Live Jev access
requires credentials. Model weights, GPU deployment, and model calibration are
separate from adapter contract testing.
