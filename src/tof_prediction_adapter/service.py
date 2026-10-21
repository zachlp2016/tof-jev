import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from .config import Settings
from .contracts import DecisionRequest, DecisionResponse
from .errors import AdapterError, error_body
from .profiles import PROFILES
from .security import consumer_context
from .upstream import UpstreamError, call_backend, normalize_answers


class PredictionService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.capacity = asyncio.Semaphore(settings.max_concurrent_requests)
        self.consumer_capacity = {
            consumer: asyncio.Semaphore(settings.max_concurrent_requests_per_consumer)
            for consumer in settings.consumer_names()
        }

    def prepare(self, request: DecisionRequest) -> tuple[str, dict]:
        backend = request.backend or self.settings.default_backend
        if backend == "mock" and not self.settings.allow_mock:
            raise AdapterError("backend_disabled", "The mock backend is disabled.", 403)
        if backend == "jev":
            if not self.settings.enable_hosted:
                raise AdapterError("backend_disabled", "The hosted backend is disabled.", 403)
            if request.data_classification != "public" or not request.allow_remote:
                raise AdapterError(
                    "remote_not_allowed",
                    "Hosted inference requires public data and allow_remote=true.",
                    403,
                )
        if request.decision_key:
            profile = PROFILES.get(request.decision_key)
            if profile is None:
                raise AdapterError("unknown_profile", "Unknown decision profile.")
            questions = profile["questions"]
        else:
            questions = {
                key: value.model_dump(exclude_none=True) for key, value in request.questions.items()
            }
        return backend, questions

    async def decide(
        self, request: DecisionRequest, request_id: str | None = None
    ) -> DecisionResponse:
        request_id = request_id or str(uuid4())
        backend, questions = self.prepare(request)
        started = time.monotonic()
        consumer = consumer_context.get()
        consumer_capacity = self.consumer_capacity.get(consumer)
        if consumer_capacity is None:
            raise AdapterError("unauthorized", "Authenticated consumer identity is unavailable.", 401)
        consumer_acquired = False
        global_acquired = False
        try:
            await asyncio.wait_for(consumer_capacity.acquire(), timeout=0.01)
            consumer_acquired = True
            await asyncio.wait_for(self.capacity.acquire(), timeout=0.01)
            global_acquired = True
        except TimeoutError as exc:
            if consumer_acquired:
                consumer_capacity.release()
                consumer_acquired = False
            raise AdapterError(
                "capacity_exceeded", "Inference capacity is full; retry later.", 503, True
            ) from exc
        try:
            warnings = ["Advisory model assessment; not a calibrated forecast or authorization."]
            if backend == "mock":
                raw = self.mock_response(questions)
                warnings.append("SYNTHETIC MOCK: uniform fixture output, not model inference.")
            else:
                local = backend == "laya"
                key = self.settings.laya_api_key if local else self.settings.jev_api_key
                model = self.settings.laya_model if local else self.settings.jev_model
                payload = {"state": request.state, "questions": questions}
                if model:
                    payload["model"] = model
                try:
                    async with asyncio.timeout(self.settings.request_timeout_seconds):
                        raw = await call_backend(
                            self.client,
                            base_url=self.settings.laya_base_url
                            if local
                            else self.settings.jev_base_url,
                            api_key=key.get_secret_value() if key else None,
                            payload=payload,
                            backend=backend,
                            max_response_bytes=self.settings.max_response_bytes,
                        )
                except TimeoutError as exc:
                    raise AdapterError(
                        "upstream_timeout", "Inference exceeded its time limit.", 504, True
                    ) from exc
            answers = normalize_answers(raw, questions, backend=backend)
            if backend == "mock":
                for answer in answers.values():
                    answer.pop("confidence", None)
                    answer.pop("confidence_kind", None)
            routing = raw.get("routing")
            model = (
                routing.get("model")
                if backend == "laya" and isinstance(routing, dict)
                else raw.get("model")
            )
            if not isinstance(model, str) or len(model) > 256:
                model = None
            revision = (
                self.settings.laya_model_revision
                if backend == "laya"
                else (model if backend == "jev" else None)
            )
            source = (
                ("operator_configured" if revision else None)
                if backend == "laya"
                else ("upstream_reported" if revision else None)
            )
            if backend == "laya" and not revision:
                warnings.append(
                    "Checkpoint revision is unverified; configure a pinned backend and TOF_LAYA_MODEL_REVISION."
                )
            usage = raw.get("usage", {})
            usage = (
                {
                    k: v
                    for k, v in usage.items()
                    if k in {"input_tokens", "output_tokens"} and type(v) is int and v >= 0
                }
                if isinstance(usage, dict)
                else {}
            )
            # Never return arbitrary upstream routing, logs, or usage metadata.
            raw_usage = raw.get("usage", {})
            if isinstance(raw_usage, dict) and any(
                raw_usage.get(k)
                for k in (
                    "truncated",
                    "state_truncated",
                    "input_truncated",
                    "state_tokens_dropped",
                    "truncated_questions",
                )
            ):
                warnings.append("Upstream reported input truncation.")
            return DecisionResponse(
                request_id=request_id,
                created_at=datetime.now(UTC),
                decision_key=request.decision_key,
                schema_hash=hashlib.sha256(
                    json.dumps(questions, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                backend=backend,
                model=model,
                model_revision=revision,
                model_revision_source=source,
                latency_ms=round((time.monotonic() - started) * 1000, 3),
                answers=answers,
                usage=usage,
                job_run_id=request.job_run_id,
                evidence_refs=request.evidence_refs,
                data_classification=request.data_classification,
                warnings=warnings,
            )
        finally:
            if global_acquired:
                self.capacity.release()
            if consumer_acquired:
                consumer_capacity.release()

    async def batch(self, items: list[DecisionRequest]) -> list[dict]:
        # Preflight the entire batch before transmitting any state.
        for item in items:
            self.prepare(item)

        batch_capacity = asyncio.Semaphore(self.settings.max_concurrent_requests)

        async def one(item):
            request_id = str(uuid4())
            try:
                async with batch_capacity:
                    result = await self.decide(item, request_id)
                return {"ok": True, "result": result.model_dump(mode="json")}
            except (AdapterError, UpstreamError) as exc:
                return {"ok": False, **error_body(exc, request_id)}

        return list(await asyncio.gather(*(one(item) for item in items)))

    @staticmethod
    def mock_response(questions):
        answers = {}
        for name, question in questions.items():
            kind = question["type"]
            if kind == "noul":
                answers[name] = {"type": kind, "noul": 0.5}
            else:
                criteria = question["criteria"]
                keys = (
                    list(criteria) if kind == "choice" else [str(i) for i in range(len(criteria))]
                )
                answer = {"type": kind, "probabilities": dict.fromkeys(keys, 1 / len(keys))}
                answer[kind] = keys[0] if kind == "choice" else (len(keys) - 1) / 2
                if kind == "score":
                    answer["legend"] = dict(zip(keys, criteria, strict=True))
                answers[name] = answer
        return {"model": "synthetic-uniform-fixture-v1", "answers": answers, "usage": {}}

    async def readiness(self) -> bool:
        backend = self.settings.default_backend
        if backend == "mock":
            return self.settings.allow_mock
        local = backend == "laya"
        key = self.settings.laya_api_key if local else self.settings.jev_api_key
        url = (
            self.settings.laya_base_url + "/health"
            if local
            else self.settings.jev_base_url + "/v1/models"
        )
        try:
            async with asyncio.timeout(3):
                async with self.client.stream(
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {key.get_secret_value()}"} if key else {},
                    timeout=3,
                    follow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        return False
                    if not local or key is None:
                        return True

                    # Locked-down Laya intentionally returns HTTP 200 with only
                    # {"status":"ok"} to callers that fail authentication. A
                    # readiness probe carrying a configured key must therefore
                    # require fields that are present only in authenticated health.
                    # ``loaded`` may legitimately be empty after idle unloading.
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16_384):
                        if len(body) + len(chunk) > 65_536:
                            return False
                        body.extend(chunk)
                    health = json.loads(body.decode("utf-8"))
                    return (
                        isinstance(health, dict)
                        and health.get("status") == "ok"
                        and all(
                            field in health
                            for field in ("loaded", "revisions", "device", "checkpoint_devices")
                        )
                    )
        except (httpx.HTTPError, TimeoutError, UnicodeDecodeError, ValueError):
            return False
