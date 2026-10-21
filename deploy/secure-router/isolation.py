"""Bounded live infrastructure qualification; no application integration.

Run on Secure with an outer `timeout 180s`. Credentials are read from protected
files, never arguments or output. Independent attempts pause on backpressure;
this harness does not install retry behavior in Router or adapter.
"""

import argparse
import concurrent.futures
import json
import random
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
from benchmark import QUESTIONS, STATE, Caller, percentile
from prometheus_client.parser import text_string_to_metric_families

ROUTER = "http://127.0.0.1:9420"
ADAPTER = "http://127.0.0.1:19421"


def samples(text):
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
        if s.name.startswith("tof_adapter_") and not s.name.endswith("_created")
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations-key-file", required=True, type=Path)
    parser.add_argument("--revoked-key-file", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    key = args.operations_key_file.read_text().strip()
    auth = {"Authorization": "Bearer " + key}
    output = {"started_at": datetime.now(UTC).isoformat(), "stages": []}
    with httpx.Client(timeout=5, trust_env=False, headers=auth) as client:
        assert (
            client.get(
                ADAPTER + "/metrics", headers={"Authorization": "Bearer invalid"}
            ).status_code
            == 401
        )
        if args.revoked_key_file:
            revoked = args.revoked_key_file.read_text().strip()
            assert (
                client.get(
                    ADAPTER + "/v1/backends", headers={"Authorization": "Bearer " + revoked}
                ).status_code
                == 401
            )
        backend = client.get(ADAPTER + "/v1/backends")
        backend.raise_for_status()
        assert backend.json()["limits"] == {
            "active_requests": 4,
            "consumer_active_requests": {"router": 4, "operations": 1},
            "batch_enabled": False,
        }
        payload = {"items": [{"state": STATE, "questions": QUESTIONS}]}
        assert client.post(ADAPTER + "/v1/decisions/batch", json=payload).status_code == 403
        assert client.post(ROUTER + "/v1/decisions/batch", json=payload).status_code == 404
        initial = samples(client.get(ADAPTER + "/metrics").text)
        warm = Caller(ROUTER)
        try:
            result = warm.decide("infra-isolation-warmup")
            assert result["status"] == 200 and result["correct"] and result["provenance_ok"]
        finally:
            warm.close()
        for router_callers, operations_callers in ((4, 0), (3, 1), (4, 2)):
            stop = threading.Event()
            barrier = threading.Barrier(router_callers + operations_callers + 1)
            timing = {}
            peaks = {"global": 0, "router": 0, "operations": 0}
            sample_errors = []

            def sample(stop=stop, peaks=peaks, sample_errors=sample_errors):
                with httpx.Client(timeout=3, trust_env=False, headers=auth) as observer:
                    while not stop.is_set():
                        try:
                            response = observer.get(ADAPTER + "/metrics")
                            response.raise_for_status()
                            for (name, labels), value in samples(response.text).items():
                                if name == "tof_adapter_global_active_decisions":
                                    peaks["global"] = max(peaks["global"], value)
                                if name == "tof_adapter_active_decisions":
                                    consumer = dict(labels)["consumer"]
                                    if consumer in peaks:
                                        peaks[consumer] = max(peaks[consumer], value)
                        except (httpx.HTTPError, ValueError) as exc:
                            sample_errors.append(type(exc).__name__)
                        stop.wait(0.02)

            def worker(
                consumer,
                index,
                barrier=barrier,
                timing=timing,
                stop=stop,
                router_callers=router_callers,
            ):
                caller = Caller(
                    ROUTER if consumer == "router" else ADAPTER,
                    None if consumer == "router" else key,
                )
                # The authenticated credential, not this forged header, owns quota.
                caller.headers["X-Consumer-ID"] = "forged-identity"
                results = []
                barrier.wait(timeout=10)
                try:
                    while time.monotonic() < timing["deadline"]:
                        result = caller.decide(
                            f"infra-{router_callers}-{consumer}-{index}-{len(results)}"
                        )
                        result["consumer"] = consumer
                        results.append(result)
                        if result["status"] != 200:
                            delay = max(1, result.get("retry_after_seconds", 0)) + random.uniform(
                                0, 0.1
                            )
                            stop.wait(min(delay, max(0, timing["deadline"] - time.monotonic())))
                finally:
                    caller.close()
                return results

            observer = threading.Thread(target=sample, daemon=True)
            observer.start()
            try:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=router_callers + operations_callers
                ) as pool:
                    futures = [
                        pool.submit(worker, consumer, index)
                        for consumer, count in (
                            ("router", router_callers),
                            ("operations", operations_callers),
                        )
                        for index in range(count)
                    ]
                    timing["start"] = time.monotonic()
                    timing["deadline"] = timing["start"] + 8
                    barrier.wait(timeout=10)
                    records = [
                        result for future in futures for result in future.result(timeout=110)
                    ]
            finally:
                stop.set()
                observer.join(timeout=5)
            elapsed = time.monotonic() - timing["start"]
            summary = {}
            for consumer in ("router", "operations"):
                own = [r for r in records if r["consumer"] == consumer]
                success = [r for r in own if r["status"] == 200]
                summary[consumer] = {
                    "statuses": dict(Counter(str(r["status"]) for r in own)),
                    "successful_requests_per_second": round(len(success) / elapsed, 3),
                    "success_p50_ms": percentile([r["elapsed_ms"] for r in success], 0.5),
                    "success_p95_ms": percentile([r["elapsed_ms"] for r in success], 0.95),
                    "error_codes": dict(
                        Counter(r["error_code"] for r in own if r["status"] != 200)
                    ),
                }
            output["stages"].append(
                {
                    "callers": {"router": router_callers, "operations": operations_callers},
                    "seconds": elapsed,
                    "observed_peak_active": peaks,
                    "sampler_errors": sample_errors,
                    "summary": summary,
                    "records": records,
                }
            )
        final_response = client.get(ADAPTER + "/metrics")
        final_response.raise_for_status()
        final = samples(final_response.text)
        output["metric_deltas"] = [
            {"name": name, "labels": dict(labels), "delta": value - initial.get((name, labels), 0)}
            for (name, labels), value in final.items()
            if name
            in {
                "tof_adapter_decisions_total",
                "tof_adapter_questions_total",
                "tof_adapter_admission_rejections_total",
                "tof_adapter_decision_duration_seconds_count",
            }
        ]
        output["final_global_active"] = final[("tof_adapter_global_active_decisions", ())]
        output["finished_at"] = datetime.now(UTC).isoformat()
        args.output.write_text(json.dumps(output, indent=2) + "\n")
        assert "forged-identity" not in final_response.text and key not in final_response.text
        assert output["final_global_active"] == 0
        for stage in output["stages"]:
            assert not stage["sampler_errors"]
            assert stage["observed_peak_active"]["global"] <= 4
            assert stage["observed_peak_active"]["router"] <= 4
            assert stage["observed_peak_active"]["operations"] <= 1
            for result in stage["records"]:
                assert result["status"] in (200, 503)
                if result["status"] == 200:
                    assert result["correct"] and result["provenance_ok"]
                    assert result["request_id"] and result["router_request_id"]
                else:
                    assert result["retry_after_seconds"] >= 1
        first = output["stages"][0]
        assert first["observed_peak_active"]["global"] == 4
        assert first["observed_peak_active"]["router"] == 4
        assert list(first["summary"]["router"]["statuses"]) == ["200"]
        assert first["summary"]["operations"]["statuses"] == {}
        mixed = output["stages"][1]
        assert mixed["observed_peak_active"]["global"] == 4
        assert all(list(value["statuses"]) == ["200"] for value in mixed["summary"].values())
        for scope in ("consumer", "global"):
            assert any(
                m["name"] == "tof_adapter_admission_rejections_total"
                and m["labels"]["scope"] == scope
                and m["delta"] > 0
                for m in output["metric_deltas"]
            )
        print(
            json.dumps(
                {
                    "passed": True,
                    "stages": [
                        {k: v for k, v in stage.items() if k != "records"}
                        for stage in output["stages"]
                    ],
                    "output": str(args.output),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
