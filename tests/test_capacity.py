import asyncio

import httpx
import pytest

from tof_prediction_adapter.app import create_app
from tof_prediction_adapter.config import Settings

KEY = "capacity-tests-service-key-at-least-24"
HEADERS = {"Authorization": f"Bearer {KEY}"}
ROUTER_KEY = "router-capacity-key-at-least-24"
OPERATIONS_KEY = "operations-capacity-key-at-least-24"
DECISION = {"state": "Example", "questions": {"q": {"type": "noul", "instructions": "Relevant?"}}}


def test_v100_qualified_default_is_four_shared_inference_slots():
    assert Settings(api_keys=[KEY]).max_concurrent_requests == 4


async def test_liveness_survives_healthy_inference_saturation():
    entered, release = asyncio.Event(), asyncio.Event()

    async def upstream(request):
        entered.set()
        await release.wait()
        return httpx.Response(
            200, json={"model": "fixture", "answers": {"q": {"type": "noul", "noul": 0.8}}}
        )

    app = create_app(
        Settings(api_keys=[KEY], max_inflight_requests=1), httpx.MockTransport(upstream)
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client,
    ):
        pending = asyncio.create_task(client.post("/v1/decisions", json=DECISION, headers=HEADERS))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            live = await client.get("/health/live")
            assert live.status_code == 200
            busy = await client.get("/v1/profiles", headers=HEADERS)
            assert busy.status_code == 503
            assert busy.headers["retry-after"] == "1"
        finally:
            release.set()
            result = await pending
        assert result.status_code == 200


async def test_batch_schedules_more_items_than_available_inference_slots():
    active = 0
    peak = 0

    async def upstream(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.025)
        active -= 1
        return httpx.Response(
            200, json={"model": "fixture", "answers": {"q": {"type": "noul", "noul": 0.8}}}
        )

    app = create_app(
        Settings(api_keys=[KEY], max_concurrent_requests=2, enable_batch=True),
        httpx.MockTransport(upstream),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client,
    ):
        result = await client.post(
            "/v1/decisions/batch", json={"items": [DECISION] * 16}, headers=HEADERS
        )
    assert result.status_code == 200
    assert all(item["ok"] for item in result.json()["items"])
    assert peak == 2


@pytest.mark.parametrize("router_calls,operations_calls", [(4, 0), (3, 1)])
async def test_router_can_use_all_four_slots_and_operations_has_no_reservation(
    router_calls, operations_calls
):
    active = 0
    peak = 0
    four_active = asyncio.Event()
    release = asyncio.Event()

    async def upstream(request):
        nonlocal active, peak
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        active += 1
        peak = max(peak, active)
        if active == 4:
            four_active.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return httpx.Response(
            200, json={"model": "fixture", "answers": {"q": {"type": "noul", "noul": 0.8}}}
        )

    settings = Settings(
        consumer_api_keys={"router": ROUTER_KEY, "operations": OPERATIONS_KEY},
        max_concurrent_requests=4,
        consumer_concurrency_limits={"router": 4, "operations": 1},
    )
    app = create_app(settings, httpx.MockTransport(upstream))
    router_headers = {"Authorization": f"Bearer {ROUTER_KEY}"}
    operations_headers = {"Authorization": f"Bearer {OPERATIONS_KEY}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client,
    ):
        admitted = [
            asyncio.create_task(client.post("/v1/decisions", json=DECISION, headers=headers))
            for headers, count in (
                (router_headers, router_calls),
                (operations_headers, operations_calls),
            )
            for _ in range(count)
        ]
        try:
            await asyncio.wait_for(four_active.wait(), 2)
            for headers in (router_headers, operations_headers):
                busy = await client.post("/v1/decisions", json=DECISION, headers=headers)
                assert busy.status_code == 503
                assert busy.headers["retry-after"] == "1"
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 200
            metrics = (await client.get("/metrics", headers=operations_headers)).text
            assert "tof_adapter_global_active_decisions 4.0" in metrics
            assert f'tof_adapter_active_decisions{{consumer="router"}} {router_calls}.0' in metrics
            expected_scopes = {
                "router": "consumer" if router_calls == 4 else "global",
                "operations": "consumer" if operations_calls else "global",
            }
            for name, scope in expected_scopes.items():
                assert f'consumer="{name}",scope="{scope}"}} 1.0' in metrics
            assert ROUTER_KEY not in metrics and OPERATIONS_KEY not in metrics
        finally:
            release.set()
            completed = await asyncio.gather(*admitted)
        metrics = (await client.get("/metrics", headers=operations_headers)).text
        assert "tof_adapter_global_active_decisions 0.0" in metrics
        assert f'backend="laya",consumer="router",outcome="success"}} {router_calls}.0' in metrics
    assert all(response.status_code == 200 for response in completed)
    assert peak == 4


async def test_cancellation_releases_both_pools_and_is_recorded():
    entered, release = asyncio.Event(), asyncio.Event()

    async def upstream(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.8}}})

    app = create_app(
        Settings(api_keys=[KEY], max_concurrent_requests=1),
        httpx.MockTransport(upstream),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client,
    ):
        pending = asyncio.create_task(client.post("/v1/decisions", json=DECISION, headers=HEADERS))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            release.set()
        response = await client.post("/v1/decisions", json=DECISION, headers=HEADERS)
        assert response.status_code == 200
        metrics = (await client.get("/metrics", headers=HEADERS)).text
        assert "tof_adapter_global_active_decisions 0.0" in metrics
        assert 'backend="laya",consumer="legacy",outcome="cancelled"} 1.0' in metrics
        assert 'backend="laya",consumer="legacy",outcome="success"} 1.0' in metrics


async def test_consumer_header_cannot_spoof_authenticated_identity():
    from test_api import api, decision

    async with api(api_keys=[], consumer_api_keys={"router": KEY}) as client:
        response = await client.post(
            "/v1/decisions",
            json=decision(),
            headers={**HEADERS, "X-Consumer-ID": "forged", "X-Consumer": "forged"},
        )
        assert response.status_code == 200
        metrics = (await client.get("/metrics", headers=HEADERS)).text
        assert 'backend="mock",consumer="router",outcome="success"} 1.0' in metrics
        assert "forged" not in metrics


@pytest.mark.parametrize(
    "limits", [{"unknown": 1}, {"router": 0}, {"router": 5}, {"router": True}, {"router": "4"}]
)
def test_invalid_consumer_limits_fail_closed(limits):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(consumer_api_keys={"router": ROUTER_KEY}, consumer_concurrency_limits=limits)


def test_unrestricted_consumer_defaults_to_shared_global_limit():
    settings = Settings(consumer_api_keys={"router": ROUTER_KEY})
    assert settings.consumer_limit("router") == settings.max_concurrent_requests == 4


async def test_operations_is_capped_at_one_even_when_global_slots_are_free():
    entered, release = asyncio.Event(), asyncio.Event()

    async def upstream(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.8}}})

    app = create_app(
        Settings(
            consumer_api_keys={"operations": OPERATIONS_KEY},
            consumer_concurrency_limits={"operations": 1},
        ),
        httpx.MockTransport(upstream),
    )
    headers = {"Authorization": "Bearer " + OPERATIONS_KEY}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client,
    ):
        pending = asyncio.create_task(client.post("/v1/decisions", json=DECISION, headers=headers))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            busy = await client.post("/v1/decisions", json=DECISION, headers=headers)
            assert busy.status_code == 503 and busy.headers["retry-after"] == "1"
            metrics = (await client.get("/metrics", headers=headers)).text
            assert "tof_adapter_global_active_decisions 1.0" in metrics
            assert 'consumer="operations",scope="consumer"} 1.0' in metrics
        finally:
            release.set()
            assert (await pending).status_code == 200
