"""Bounded HTTP calls and strict normalization for System One backends.

The adapter never automatically retries inference: a timed out request may already
have been evaluated (and billed). Retryability is information for the caller.
"""

from __future__ import annotations

import json
import math
from typing import Any

import httpx


class UpstreamError(Exception):
    """An intentionally sanitized error safe to present to an adapter client."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


def _invalid_response() -> UpstreamError:
    return UpstreamError(
        "upstream_invalid_response",
        "The prediction backend returned an invalid response.",
        502,
    )


def _reject_constant(value: str) -> Any:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _http_error(status: int) -> UpstreamError:
    if status in {401, 403}:
        return UpstreamError(
            "upstream_authentication_failed",
            "The prediction backend rejected its configured credentials.",
            502,
        )
    if status == 429:
        return UpstreamError(
            "upstream_rate_limited",
            "The prediction backend is rate limited. Retry later.",
            503,
            retryable=True,
        )
    if status in {408, 504}:
        return UpstreamError(
            "upstream_timeout",
            "The prediction backend timed out. The request may have been processed.",
            504,
            retryable=True,
        )
    if status >= 500:
        return UpstreamError(
            "upstream_unavailable",
            "The prediction backend is temporarily unavailable.",
            503,
            retryable=True,
        )
    if 400 <= status < 500:
        return UpstreamError(
            "upstream_rejected_request",
            "The prediction backend rejected the request.",
            502,
        )
    return _invalid_response()


async def call_backend(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str | None,
    payload: dict,
    backend: str,
    max_response_bytes: int = 1_048_576,
) -> dict:
    """Call a service-root URL's ``/v1/systemone`` exactly once.

    Redirects are refused so a backend cannot redirect the bearer credential or
    private state elsewhere. The response limit applies to bytes actually read,
    regardless of an absent or misleading Content-Length header. Compressed
    responses are refused; requesting identity encoding makes the bound apply
    before decompression and avoids compressed-response amplification.

    ``backend`` is a routing label retained in the interface; it is never used
    to construct a URL or expose upstream diagnostics to the client. The caller
    owns backend configuration validation and request timeout configuration.
    """
    if max_response_bytes < 1:
        raise ValueError("max_response_bytes must be positive")

    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{base_url.rstrip('/')}/v1/systemone"
    try:
        async with client.stream(
            "POST", url, json=payload, headers=headers, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise _http_error(response.status_code)
            if response.headers.get("content-encoding", "identity").lower().strip() not in {
                "",
                "identity",
            }:
                raise _invalid_response()
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = 0  # The streaming limit is authoritative.
                if declared_size > max_response_bytes:
                    raise UpstreamError(
                        "upstream_response_too_large",
                        "The prediction backend response exceeded the configured size limit.",
                        502,
                    )
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65_536):
                if len(body) + len(chunk) > max_response_bytes:
                    raise UpstreamError(
                        "upstream_response_too_large",
                        "The prediction backend response exceeded the configured size limit.",
                        502,
                    )
                body.extend(chunk)
    except httpx.TimeoutException:
        raise UpstreamError(
            "upstream_timeout",
            "The prediction backend timed out. The request may have been processed.",
            504,
            retryable=True,
        ) from None
    except httpx.RequestError:
        raise UpstreamError(
            "upstream_unavailable",
            "The prediction backend could not be reached.",
            502,
            retryable=True,
        ) from None

    try:
        raw = json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _invalid_response() from None
    if not isinstance(raw, dict):
        raise _invalid_response()
    return raw


def _number(value: Any, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid_response()
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise _invalid_response() from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise _invalid_response()
    return number


def _distribution(value: Any, keys: set[str]) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != keys or not keys:
        raise _invalid_response()
    result = {
        key: _number(probability, minimum=0.0, maximum=1.0) for key, probability in value.items()
    }
    # Laya rounds probabilities to four decimal places. At 100 options this
    # can accumulate ~0.005 error. Keep reported values; never silently rescale.
    if not math.isclose(math.fsum(result.values()), 1.0, rel_tol=0.0, abs_tol=0.01):
        raise _invalid_response()
    return result


def normalize_answers(raw: dict, questions: dict, backend: str) -> dict[str, dict]:
    """Validate answers against their questions and preserve confidence meaning.

    Extra upstream metadata is accepted but is not copied into normalized
    answers. Answer IDs and probability labels must match the request exactly.
    Confidence is included only when the backend actually supplies it; Jev
    Noul answers therefore have no normalized confidence. Laya's native
    confidence remains explicitly labeled rather than conflated with Jev's.
    """
    answers = raw.get("answers") if isinstance(raw, dict) else None
    if (
        not isinstance(answers, dict)
        or not isinstance(questions, dict)
        or not questions
        or set(answers) != set(questions)
    ):
        raise _invalid_response()

    normalized: dict[str, dict] = {}
    for question_id, question in questions.items():
        answer = answers[question_id]
        if not isinstance(question, dict) or not isinstance(answer, dict):
            raise _invalid_response()
        question_type = question.get("type")
        if question_type not in {"choice", "score", "noul"}:
            raise _invalid_response()
        if answer.get("type") != question_type:
            raise _invalid_response()
        result: dict[str, Any] = {"type": question_type}
        if question_type == "choice":
            criteria = question.get("criteria")
            if not isinstance(criteria, dict) or not criteria:
                raise _invalid_response()
            choice = answer.get("choice")
            if not isinstance(choice, str) or choice not in criteria:
                raise _invalid_response()
            result["value"] = choice
            result["probabilities"] = _distribution(answer.get("probabilities"), set(criteria))
        elif question_type == "score":
            criteria = question.get("criteria")
            if not isinstance(criteria, list) or not criteria:
                raise _invalid_response()
            keys = {str(index) for index in range(len(criteria))}
            legend = answer.get("legend")
            if not isinstance(legend, dict) or set(legend) != keys:
                raise _invalid_response()
            # String rubric levels must be faithfully echoed. Laya serializes
            # structured criteria into strings, so their textual encoding is
            # deliberately not compared across different JSON implementations.
            for index, criterion in enumerate(criteria):
                if isinstance(criterion, str) and legend[str(index)] != criterion:
                    raise _invalid_response()
            result["value"] = _number(answer.get("score"), minimum=0.0, maximum=len(criteria) - 1)
            result["probabilities"] = _distribution(answer.get("probabilities"), keys)
        else:
            result["value"] = _number(answer.get("noul"), minimum=0.0, maximum=1.0)

        if "confidence" in answer:
            result["confidence"] = _number(answer["confidence"], minimum=0.0, maximum=1.0)
            if backend == "laya":
                result["confidence_kind"] = (
                    "laya_max_probability" if question_type == "noul" else "laya_entropy"
                )
            elif backend == "jev":
                result["confidence_kind"] = "jev_reported"
            else:
                # Unknown backends must never acquire Jev's semantics by default.
                result["confidence_kind"] = "backend_reported"
        normalized[question_id] = result
    return normalized
