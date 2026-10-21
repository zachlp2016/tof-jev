"""Bounded saturation benchmark; ten questions sharing one short state.

This synthetic support-routing fixture measures software throughput, not model
quality. Each attempt is distinct. 503 backpressure delays the next attempt;
indeterminate/transport errors are recorded without retrying that request.
"""

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import math
import random
import statistics
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
STATE = "The customer reports a duplicate charge on their invoice and asks for a refund."
QUESTIONS = {
    f"queue_{index}": {
        "type": "choice",
        "instructions": "Which customer support queue should handle this request?",
        "criteria": {
            "billing": "Invoices, charges, payments and refunds",
            "technical": "Product errors and technical troubleshooting",
            "sales": "Product information and new purchases",
        },
    }
    for index in range(10)
}
SCHEMA_HASH = hashlib.sha256(
    json.dumps(QUESTIONS, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def percentile(values, p):
    return round(sorted(values)[max(0, math.ceil(len(values) * p) - 1)], 3) if values else None


class Caller:
    def __init__(self, base_url, token=None):
        target = urlsplit(base_url)
        if target.scheme != "http" or target.hostname != "127.0.0.1" or target.path:
            raise ValueError("Benchmark endpoints must be explicit loopback HTTP origins")
        self.connection = http.client.HTTPConnection(target.hostname, target.port, timeout=100)
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            self.headers["Authorization"] = "Bearer " + token

    def close(self):
        self.connection.close()

    def decide(self, identity):
        body = json.dumps(
            {
                "state": STATE,
                "questions": QUESTIONS,
                "backend": "laya",
                "data_classification": "internal",
                "job_run_id": identity,
            },
            separators=(",", ":"),
        )
        started = time.monotonic()
        try:
            self.connection.request(
                "POST", "/v1/decisions", body=body.encode(), headers=self.headers
            )
            response = self.connection.getresponse()
            raw = response.read(1_048_577)
            elapsed_ms = (time.monotonic() - started) * 1000
            if len(raw) > 1_048_576:
                raise ValueError("Response size limit exceeded")
            payload = json.loads(raw)
            result = {
                "status": response.status,
                "elapsed_ms": round(elapsed_ms, 3),
                "request_id": payload.get("request_id"),
                "router_request_id": response.getheader("X-Request-ID"),
                "error_code": payload.get("error", {}).get("code"),
            }
            if response.status == 200:
                answers = payload.get("answers", {})
                result["correct"] = set(answers) == set(QUESTIONS) and all(
                    answer.get("type") == "choice" and answer.get("value") == "billing"
                    for answer in answers.values()
                )
                result["provenance_ok"] = (
                    payload.get("backend") == "laya"
                    and payload.get("model") == "english"
                    and payload.get("model_revision") == REVISION
                    and payload.get("schema_hash") == SCHEMA_HASH
                    and payload.get("job_run_id") == identity
                    and payload.get("advisory_only") is True
                    and payload.get("calibrated") is False
                )
                result["adapter_ms"] = payload["latency_ms"]
            try:
                result["retry_after_seconds"] = max(
                    0, float(response.getheader("Retry-After", "0"))
                )
            except ValueError:
                result["retry_after_seconds"] = 0
            return result
        except (OSError, http.client.HTTPException, ValueError) as error:
            self.close()
            return {
                "status": "transport_or_protocol_error",
                "error_type": type(error).__name__,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            }


def tier(base_url, token, callers, seconds, run_id, path):
    barrier = threading.Barrier(callers + 1)
    timing = {}

    def worker(index):
        client = Caller(base_url, token)
        results = []
        barrier.wait()
        try:
            while time.monotonic() < timing["deadline"]:
                result = client.decide(f"bench-{run_id}-{path}-{callers}-{index}-{len(results)}")
                results.append(result)
                if result["status"] != 200:
                    # Bound load even if a server omits Retry-After; do not hammer admission.
                    delay = max(0.1, result.get("retry_after_seconds", 0)) + random.uniform(0, 0.1)
                    remaining = timing["deadline"] - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(delay, remaining))
        finally:
            client.close()
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=callers) as pool:
        futures = [pool.submit(worker, index) for index in range(callers)]
        timing["started"] = time.monotonic()
        timing["deadline"] = timing["started"] + seconds
        barrier.wait()
        rows = [row for future in futures for row in future.result()]
    elapsed = time.monotonic() - timing["started"]
    successes = [row for row in rows if row["status"] == 200]
    latencies = [row["elapsed_ms"] for row in successes]
    errors = [row for row in rows if row["status"] != 200]
    return {
        "path": path,
        "callers": callers,
        "submission_seconds": seconds,
        "elapsed_including_drain_seconds": round(elapsed, 6),
        "attempts": len(rows),
        "successes": len(successes),
        "requests_per_second": round(len(successes) / elapsed, 3),
        "questions_per_second": round(len(successes) * len(QUESTIONS) / elapsed, 3),
        "p50_ms": round(statistics.median(latencies), 3) if latencies else None,
        "p95_ms": percentile(latencies, 0.95),
        "p99_ms": percentile(latencies, 0.99),
        "adapter_p50_ms": round(statistics.median(row["adapter_ms"] for row in successes), 3)
        if successes
        else None,
        "outside_adapter_p50_ms": round(
            statistics.median(row["elapsed_ms"] - row["adapter_ms"] for row in successes), 3
        )
        if successes
        else None,
        "statuses": dict(Counter(str(row["status"]) for row in rows)),
        "error_codes": dict(
            Counter(row.get("error_code", row.get("error_type")) for row in errors)
        ),
        "error_p50_ms": round(statistics.median(row["elapsed_ms"] for row in errors), 3)
        if errors
        else None,
        "incorrect_responses": sum(not row["correct"] for row in successes),
        "invalid_provenance": sum(not row["provenance_ok"] for row in successes),
        "samples": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--adapter-key-file", type=Path)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 15:
        parser.error("Each tier must last between 1 and 15 seconds")
    run_id = uuid4().hex[:12]
    result = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "questions_per_request": len(QUESTIONS),
        "state": STATE,
        "questions": QUESTIONS,
        "schema_hash": SCHEMA_HASH,
        "model_revision": REVISION,
        "method": "closed-loop workers, success latency only; throughput includes final drain; honor Retry-After with 0-100ms jitter; no retry of dispatched requests",
        "tiers": [],
    }

    def save():
        args.output.write_text(json.dumps(result, indent=2) + "\n")

    client = Caller("http://127.0.0.1:9420")
    try:
        result["initial_request"] = client.decide(f"bench-{run_id}-initial")
    finally:
        client.close()
    save()
    first = result["initial_request"]
    print(json.dumps({"initial_request": first}), flush=True)
    if first["status"] != 200 or not first["correct"] or not first["provenance_ok"]:
        raise SystemExit("Initial qualification failed; not starting saturation load")

    paths = [("router", "http://127.0.0.1:9420", None, (1, 4, 8, 16))]
    if args.adapter_key_file:
        paths.append(
            (
                "adapter_control",
                "http://127.0.0.1:19421",
                args.adapter_key_file.read_text().strip(),
                (1, 4),
            )
        )
    for path, base_url, token, counts in paths:
        for count in counts:
            record = tier(base_url, token, count, args.seconds, run_id, path)
            result["tiers"].append(record)
            save()
            print(json.dumps({k: v for k, v in record.items() if k != "samples"}), flush=True)
            if (
                record["incorrect_responses"]
                or record["invalid_provenance"]
                or any(code not in {"200", "503"} for code in record["statuses"])
            ):
                raise SystemExit("Unexpected response; stopping benchmark")
    result["finished_at"] = datetime.now(UTC).isoformat()
    save()


if __name__ == "__main__":
    main()
