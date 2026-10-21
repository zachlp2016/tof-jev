import asyncio

import httpx

from tof_prediction_adapter.app import create_app
from tof_prediction_adapter.config import Settings

KEY = "capacity-tests-service-key-at-least-24"
HEADERS = {"Authorization": f"Bearer {KEY}"}
DECISION = {"state": "Example", "questions": {"q": {"type": "noul", "instructions": "Relevant?"}}}


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
        Settings(api_keys=[KEY], max_concurrent_requests=2), httpx.MockTransport(upstream)
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
