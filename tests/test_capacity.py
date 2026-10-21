import asyncio

import httpx
import pytest

from tof_prediction_adapter.app import create_app
from tof_prediction_adapter.config import Settings

KEY = "capacity-tests-service-key-at-least-24"
HEADERS = {"Authorization": f"Bearer {KEY}"}
PARALLAX_KEY = "parallax-capacity-key-at-least-24"
RAILS_KEY = "tof-ai-app-capacity-key-at-least-24"
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


async def test_two_named_consumers_share_four_slots_and_each_is_limited_to_three():
    active = 0
    peak = 0
    four_active = asyncio.Event()
    release = asyncio.Event()

    async def upstream(request):
        nonlocal active, peak
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
        consumer_api_keys={"parallax": PARALLAX_KEY, "tof-ai-app": RAILS_KEY},
        max_concurrent_requests=4,
        max_concurrent_requests_per_consumer=3,
    )
    app = create_app(settings, httpx.MockTransport(upstream))
    parallax_headers = {"Authorization": f"Bearer {PARALLAX_KEY}"}
    rails_headers = {"Authorization": f"Bearer {RAILS_KEY}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client,
    ):
        admitted = [
            asyncio.create_task(
                client.post("/v1/decisions", json=DECISION, headers=parallax_headers)
            )
            for _ in range(3)
        ]
        admitted.append(
            asyncio.create_task(client.post("/v1/decisions", json=DECISION, headers=rails_headers))
        )
        try:
            await asyncio.wait_for(four_active.wait(), 2)
            fourth_parallax = await client.post(
                "/v1/decisions", json=DECISION, headers=parallax_headers
            )
            second_rails = await client.post("/v1/decisions", json=DECISION, headers=rails_headers)
            assert fourth_parallax.status_code == 503
            assert second_rails.status_code == 503
            assert fourth_parallax.headers["retry-after"] == "1"
            assert second_rails.headers["retry-after"] == "1"
            metrics = (await client.get("/metrics", headers=rails_headers)).text
            assert "tof_adapter_global_active_decisions 4.0" in metrics
            assert 'tof_adapter_active_decisions{consumer="parallax"} 3.0' in metrics
            assert 'tof_adapter_active_decisions{consumer="tof-ai-app"} 1.0' in metrics
            assert 'consumer="parallax",scope="consumer"} 1.0' in metrics
            assert 'consumer="tof-ai-app",scope="global"} 1.0' in metrics
            assert PARALLAX_KEY not in metrics and RAILS_KEY not in metrics
        finally:
            release.set()
            completed = await asyncio.gather(*admitted)
        metrics = (await client.get("/metrics", headers=rails_headers)).text
        assert "tof_adapter_global_active_decisions 0.0" in metrics
        assert 'backend="laya",consumer="parallax",outcome="success"} 3.0' in metrics
        assert 'backend="laya",consumer="tof-ai-app",outcome="success"} 1.0' in metrics
    assert all(response.status_code == 200 for response in completed)
    assert peak == 4


async def test_cancellation_releases_both_pools_and_is_recorded():
    entered, release = asyncio.Event(), asyncio.Event()

    async def upstream(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.8}}})

    app = create_app(
        Settings(api_keys=[KEY], max_concurrent_requests=1, max_concurrent_requests_per_consumer=1),
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
