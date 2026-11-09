"""Transport tests.

The WebSocket is a scripted fake and HTTP is an in-memory fake, so these tests
exercise the loop, the coalescing and the health thresholds without opening a
socket or waiting for the real 15/30/90 s intervals.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import replace

import httpx
import pytest
from google.transit import gtfs_realtime_pb2 as pb

from segui_bus.config import load_settings
from segui_bus.service.transport import (
    FeedUpdate,
    Transport,
    TransportHealth,
    TransportTiming,
)

# --- fixtures and fakes ----------------------------------------------------


@pytest.fixture
def settings(tmp_path):
    return replace(load_settings(), cache_dir=tmp_path)


VP_URL = "https://proxy.busone.app/seta-pc/gtfs-rt/vehicle-positions.pb"
TU_URL = "https://proxy.busone.app/seta-pc/gtfs-rt/trip-updates.pb"
STATUS_URL = "https://proxy.busone.app/seta-pc/status"


def vp_payload(timestamp: int, vehicle_ids: tuple[str, ...] = ("5284",)) -> bytes:
    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = timestamp
    for vehicle_id in vehicle_ids:
        entity = feed.entity.add()
        entity.id = vehicle_id
        vehicle = entity.vehicle
        vehicle.trip.trip_id = f"PC15-{vehicle_id}"
        vehicle.trip.route_id = "PC15"
        vehicle.vehicle.id = vehicle_id
        vehicle.position.latitude = 45.0521
        vehicle.position.longitude = 9.6930
        vehicle.timestamp = timestamp
    return feed.SerializeToString()


def tu_payload(timestamp: int, vehicle_id: str = "5284") -> bytes:
    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = timestamp
    entity = feed.entity.add()
    entity.id = f"PC15-{vehicle_id}"
    update = entity.trip_update
    update.trip.trip_id = f"PC15-{vehicle_id}"
    update.trip.route_id = "PC15"
    update.vehicle.id = vehicle_id
    stu = update.stop_time_update.add()
    stu.stop_sequence = 4
    stu.stop_id = "PCU1002A"
    stu.arrival.time = 1_792_505_040
    stu.arrival.delay = 120
    return feed.SerializeToString()


def tick(feed: str, url: str, timestamp: int = 1000, count: int = 1) -> str:
    return json.dumps({"type": feed, "url": url, "timestamp": timestamp, "count": count})


def response(url: str, *, status: int = 200, content: bytes = b"", headers=None):
    return httpx.Response(
        status,
        content=content,
        headers=headers or {},
        request=httpx.Request("GET", url),
    )


class FakeHttp:
    """Minimal async HTTP client: one scripted response per URL, in order."""

    def __init__(self) -> None:
        self._responses: dict[str, list[Callable[[], Awaitable[httpx.Response]]]] = defaultdict(list)
        self.calls: list[str] = []
        self.started = asyncio.Event()

    def route(self, url: str, *responses: Callable[[], Awaitable[httpx.Response]] | httpx.Response):
        for item in responses:
            if isinstance(item, httpx.Response):
                async def fixed(value=item):
                    return value

                self._responses[url].append(fixed)
            else:
                self._responses[url].append(item)

    async def get(self, url: str) -> httpx.Response:
        self.calls.append(url)
        self.started.set()
        if not self._responses[url]:
            raise AssertionError(f"unexpected GET {url}")
        return await self._responses[url].pop(0)()


class FakeWebSocket:
    def __init__(self) -> None:
        self._messages: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def push(self, message: str) -> None:
        await self._messages.put(message)

    def close_sync(self) -> None:
        if not self.closed:
            self.closed = True
            self._messages.put_nowait(None)

    async def close(self) -> None:
        self.close_sync()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        message = await self._messages.get()
        if message is None:
            raise StopAsyncIteration
        return message


class _Connection:
    def __init__(self, websocket: FakeWebSocket | None = None, error: Exception | None = None):
        self._websocket = websocket
        self._error = error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self._websocket

    async def __aexit__(self, *exc_info):
        if self._websocket is not None:
            await self._websocket.close()
        return False


class FakeMonotonic:
    """A controllable monotonic clock; tests decide when time passes."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> None:
        self.value += delta


class FakeConnector:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []
        self.allow = True
        self.websocket: FakeWebSocket | None = None

    def __call__(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if not self.allow:
            return _Connection(error=ConnectionError("websocket down"))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                return _Connection(error=outcome)
            self.websocket = outcome
            return _Connection(outcome)
        if self.websocket is None:
            self.websocket = FakeWebSocket()
        return _Connection(self.websocket)


async def wait_for(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.001)


async def start(transport: Transport) -> asyncio.Task:
    task = asyncio.create_task(transport.run())
    await wait_for(lambda: transport.health is not None)
    return task


async def stop(transport: Transport, task: asyncio.Task) -> None:
    await transport.close()
    await asyncio.wait_for(task, timeout=1.0)


def fast_timing(**overrides) -> TransportTiming:
    values = {
        "no_tick_vp_s": 10.0,
        "no_tick_tu_s": 10.0,
        "stale_after_s": 120.0,
        "ws_down_before_fallback_s": 10.0,
        "status_interval_s": 10.0,
        "fallback_vp_s": 0.05,
        "fallback_tu_s": 0.04,
        "fallback_vp_jitter_s": 0.0,
        "fallback_tu_jitter_s": 0.0,
        "reconnect_initial_s": 0.01,
        "reconnect_max_s": 0.02,
        "rate_limit_initial_s": 0.02,
        "rate_limit_max_s": 0.04,
        "health_check_s": 0.01,
    }
    values.update(overrides)
    return TransportTiming(**values)


def make_transport(
    settings, http, connector, *, now=lambda: 1000.0, monotonic=None, **timing
) -> Transport:
    return Transport(
        settings,
        http=http,
        ws_connect=connector,
        timing=fast_timing(**timing),
        now=now,
        monotonic=monotonic,
        rng=random.Random(0),
    )


# --- tests -----------------------------------------------------------------


def test_bootstrap_connects_with_headers_and_fetches_both_feeds():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        http = FakeHttp()
        http.route(VP_URL, response(VP_URL, content=vp_payload(1000)))
        http.route(TU_URL, response(TU_URL, content=tu_payload(1001)))
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        updates: list[FeedUpdate] = []
        transport = Transport(
            settings,
            http=http,
            ws_connect=connector,
            timing=fast_timing(),
            now=lambda: 1000.0,
            on_update=lambda update: _collect(updates, update),
        )

        task = await start(transport)
        await wait_for(lambda: len(updates) == 2)
        url, kwargs = connector.calls[0]
        assert url == settings.ws_url
        assert kwargs["additional_headers"]["X-BusOne-Client"].startswith("segui_bus/")
        assert kwargs["additional_headers"]["X-BusOne-Use"] == "personal"
        assert kwargs["user_agent_header"] == settings.user_agent
        assert http.calls == [VP_URL, TU_URL]
        assert transport.vehicle_positions is not None
        assert transport.trip_updates is not None
        await stop(transport, task)

    asyncio.run(scenario())


async def _collect(updates: list[FeedUpdate], update: FeedUpdate) -> None:
    updates.append(update)


def test_tick_fetches_pinned_url_and_replaces_the_snapshot():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        http = FakeHttp()
        http.route(VP_URL, response(VP_URL, content=vp_payload(1000, ("5284", "5285"))))
        http.route(TU_URL, response(TU_URL, content=tu_payload(1001)))
        http.route(VP_URL, response(VP_URL, content=vp_payload(1002, ("5286",))))
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        updates: list[FeedUpdate] = []
        transport = make_transport(settings, http, connector)
        transport._on_update = lambda update: _collect(updates, update)

        task = await start(transport)
        await wait_for(lambda: len(updates) == 2)
        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 1002, 1)
        )
        await wait_for(lambda: len(updates) == 3)

        latest = [u for u in updates if u.feed == "vehicle_positions"][-1]
        assert [v.vehicle.vehicle_id for v in latest.snapshot.vehicles] == ["5286"]
        assert latest.stale is False
        await stop(transport, task)

    asyncio.run(scenario())


def test_ticks_are_coalesced_while_a_fetch_is_in_flight():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        gate = asyncio.Event()

        async def blocked():
            await gate.wait()
            return response(VP_URL, content=vp_payload(1002, ("5286",)))

        http = FakeHttp()
        http.route(VP_URL, response(VP_URL, content=vp_payload(1000)))
        http.route(TU_URL, response(TU_URL, content=tu_payload(1001)))
        http.route(VP_URL, blocked, response(VP_URL, content=vp_payload(1004, ("5288",))))
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        updates: list[FeedUpdate] = []
        transport = make_transport(settings, http, connector)
        transport._on_update = lambda update: _collect(updates, update)

        task = await start(transport)
        await wait_for(lambda: len(updates) == 2)
        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 1002, 1)
        )
        await wait_for(lambda: http.calls.count(VP_URL) == 2)
        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 1003, 1)
        )
        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 1004, 1)
        )
        await asyncio.sleep(0.02)
        gate.set()
        await wait_for(lambda: http.calls.count(VP_URL) == 3)
        await wait_for(lambda: sum(u.feed == "vehicle_positions" for u in updates) == 3)

        assert http.calls.count(VP_URL) == 3
        await stop(transport, task)

    asyncio.run(scenario())


def test_invalid_tick_urls_are_ignored():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        http = FakeHttp()
        http.route(VP_URL, response(VP_URL, content=vp_payload(1000)))
        http.route(TU_URL, response(TU_URL, content=tu_payload(1001)))
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        updates: list[FeedUpdate] = []
        transport = make_transport(settings, http, connector)
        transport._on_update = lambda update: _collect(updates, update)

        task = await start(transport)
        await wait_for(lambda: len(updates) == 2)
        bad = [
            tick("vehicle_positions", "https://evil.example/seta-pc/gtfs-rt/vehicle-positions.pb"),
            tick("vehicle_positions", "/other-ns/gtfs-rt/vehicle-positions.pb"),
            tick("vehicle_positions", "/seta-pc/gtfs-rt/trip-updates.pb"),
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.json"),
        ]
        for message in bad:
            await ws.push(message)
        await asyncio.sleep(0.05)

        assert http.calls == [VP_URL, TU_URL]
        assert len(updates) == 2
        await stop(transport, task)

    asyncio.run(scenario())


def test_out_of_order_feed_timestamps_are_dropped():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        http = FakeHttp()
        http.route(VP_URL, response(VP_URL, content=vp_payload(1000)))
        http.route(TU_URL, response(TU_URL, content=tu_payload(1001)))
        http.route(VP_URL, response(VP_URL, content=vp_payload(999)))
        http.route(VP_URL, response(VP_URL, content=vp_payload(1001)))
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        updates: list[FeedUpdate] = []
        transport = make_transport(settings, http, connector)
        transport._on_update = lambda update: _collect(updates, update)

        task = await start(transport)
        await wait_for(lambda: len(updates) == 2)
        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 999)
        )
        await wait_for(lambda: http.calls.count(VP_URL) == 2)
        await asyncio.sleep(0.02)
        assert len(updates) == 2
        assert transport.vehicle_positions.feed_timestamp == 1000

        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 1001)
        )
        await wait_for(lambda: len(updates) == 3)
        assert transport.vehicle_positions.feed_timestamp == 1001
        await stop(transport, task)

    asyncio.run(scenario())


def test_duplicate_vehicles_are_deduped_and_empty_is_valid():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        http = FakeHttp()
        http.route(VP_URL, response(VP_URL, content=vp_payload(1000, ("5284", "5284"))))
        http.route(TU_URL, response(TU_URL, content=tu_payload(1001)))
        http.route(VP_URL, response(VP_URL, content=vp_payload(1002, ())))
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        updates: list[FeedUpdate] = []
        transport = make_transport(settings, http, connector)
        transport._on_update = lambda update: _collect(updates, update)

        task = await start(transport)
        await wait_for(lambda: len(updates) == 2)
        assert len(transport.vehicle_positions.vehicles) == 1

        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 1002, 0)
        )
        await wait_for(lambda: len(updates) == 3)
        assert transport.vehicle_positions.vehicles == ()
        await stop(transport, task)

    asyncio.run(scenario())


def test_unchanged_last_modified_skips_the_parse():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        http = FakeHttp()
        http.route(
            VP_URL,
            response(VP_URL, content=vp_payload(1000), headers={"Last-Modified": "LM1"}),
        )
        http.route(TU_URL, response(TU_URL, content=tu_payload(1001)))
        http.route(
            VP_URL,
            response(VP_URL, content=vp_payload(1002), headers={"Last-Modified": "LM1"}),
        )
        http.route(
            VP_URL,
            response(VP_URL, content=vp_payload(1003), headers={"Last-Modified": "LM2"}),
        )
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        updates: list[FeedUpdate] = []
        transport = make_transport(settings, http, connector)
        transport._on_update = lambda update: _collect(updates, update)

        task = await start(transport)
        await wait_for(lambda: len(updates) == 2)
        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 1002)
        )
        await wait_for(lambda: http.calls.count(VP_URL) == 2)
        await asyncio.sleep(0.02)
        assert len(updates) == 2
        assert transport.vehicle_positions.feed_timestamp == 1000

        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 1003)
        )
        await wait_for(lambda: len(updates) == 3)
        assert transport.vehicle_positions.feed_timestamp == 1003
        await stop(transport, task)

    asyncio.run(scenario())


def test_stale_is_computed_from_the_feed_header_timestamp():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        http = FakeHttp()
        http.route(VP_URL, response(VP_URL, content=vp_payload(800)))
        http.route(TU_URL, response(TU_URL, content=tu_payload(990)))
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        updates: list[FeedUpdate] = []
        transport = make_transport(settings, http, connector, now=lambda: 1000.0)
        transport._on_update = lambda update: _collect(updates, update)

        task = await start(transport)
        await wait_for(lambda: len(updates) == 2)
        by_feed = {update.feed: update for update in updates}
        assert by_feed["vehicle_positions"].stale is True
        assert by_feed["trip_updates"].stale is False
        await stop(transport, task)

    asyncio.run(scenario())


def test_silence_marks_degraded_and_probes_status():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        http = FakeHttp()
        http.route(VP_URL, response(VP_URL, content=vp_payload(1000)))
        http.route(TU_URL, response(TU_URL, content=tu_payload(1000)))
        http.route(STATUS_URL, response(STATUS_URL, content=b"{}"))
        http.route(VP_URL, response(VP_URL, content=vp_payload(1000)))
        http.route(TU_URL, response(TU_URL, content=tu_payload(1000)))
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        transport = make_transport(
            settings,
            http,
            connector,
            no_tick_vp_s=0.03,
            no_tick_tu_s=0.02,
            health_check_s=0.01,
        )

        task = await start(transport)
        await wait_for(lambda: transport.health.degraded)
        assert STATUS_URL in http.calls

        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 1000)
        )
        await ws.push(tick("trip_updates", "/seta-pc/gtfs-rt/trip-updates.pb", 1000))
        await wait_for(lambda: not transport.health.degraded)
        await stop(transport, task)

    asyncio.run(scenario())


def test_long_disconnect_enters_fallback_and_reconnect_stops_it():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        http = FakeHttp()
        for _ in range(20):
            http.route(VP_URL, response(VP_URL, content=vp_payload(1000)))
            http.route(TU_URL, response(TU_URL, content=tu_payload(1001)))
        http.route(STATUS_URL, response(STATUS_URL, content=b"{}"))
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        transport = make_transport(
            settings,
            http,
            connector,
            ws_down_before_fallback_s=0.04,
            fallback_vp_s=0.02,
            fallback_tu_s=0.02,
            status_interval_s=0.04,
            reconnect_initial_s=0.01,
            reconnect_max_s=0.01,
            health_check_s=0.01,
        )

        task = await start(transport)
        await wait_for(lambda: transport.vehicle_positions is not None)
        baseline = len(http.calls)
        connector.allow = False
        ws.close_sync()

        await wait_for(lambda: transport.health.fallback)
        await wait_for(lambda: len(http.calls) > baseline)
        assert not transport.health.ws_connected

        connector.allow = True
        connector.websocket = FakeWebSocket()
        await wait_for(lambda: transport.health.ws_connected)
        await wait_for(lambda: not transport.health.fallback)
        await stop(transport, task)

    asyncio.run(scenario())


def test_reconnect_backoff_is_exponential_and_jittered():
    timing = TransportTiming(reconnect_initial_s=1.0, reconnect_max_s=30.0)
    delays = [Transport._reconnect_delay(timing, attempt, random.Random(0)) for attempt in range(7)]

    assert 0.8 <= delays[0] <= 1.2
    assert 1.6 <= delays[1] <= 2.4
    assert 3.2 <= delays[2] <= 4.8
    assert 6.4 <= delays[3] <= 9.6
    assert 12.8 <= delays[4] <= 19.2
    assert 24.0 <= delays[5] <= 36.0
    assert 24.0 <= delays[6] <= 36.0


def test_429_honours_retry_after_without_hammering():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        http = FakeHttp()
        http.route(
            VP_URL,
            response(VP_URL, status=429, headers={"Retry-After": "0.05"}),
        )
        http.route(TU_URL, response(TU_URL, content=tu_payload(1001)))
        http.route(VP_URL, response(VP_URL, content=vp_payload(1000)))
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        clock = FakeMonotonic(1000.0)
        transport = make_transport(settings, http, connector, monotonic=clock)

        task = await start(transport)
        await wait_for(lambda: VP_URL in http.calls)
        await wait_for(lambda: transport._not_before > clock())
        calls_after_429 = len(http.calls)

        # Still inside the Retry-After window: the tick must be dropped.
        clock.advance(0.001)
        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 1000)
        )
        await wait_for(lambda: "vehicle_positions" in transport._pending_url)
        if "vehicle_positions" in transport._fetch_tasks:
            await wait_for(lambda: "vehicle_positions" not in transport._fetch_tasks)
        assert len(http.calls) == calls_after_429
        assert transport.vehicle_positions is None

        # Cross the recorded deadline; the next tick may fetch.
        clock.advance(transport._not_before - clock() + 0.001)
        await ws.push(
            tick("vehicle_positions", "/seta-pc/gtfs-rt/vehicle-positions.pb", 1000)
        )
        await wait_for(lambda: transport.vehicle_positions is not None)
        await stop(transport, task)

    asyncio.run(scenario())


def test_health_dataclass_is_stable_when_nothing_changes():
    async def scenario():
        settings = replace(load_settings(), cache_dir=None)
        http = FakeHttp()
        http.route(VP_URL, response(VP_URL, content=vp_payload(1000)))
        http.route(TU_URL, response(TU_URL, content=tu_payload(1001)))
        ws = FakeWebSocket()
        connector = FakeConnector(ws)
        seen: list[TransportHealth] = []
        transport = Transport(
            settings,
            http=http,
            ws_connect=connector,
            timing=fast_timing(),
            now=lambda: 1000.0,
            on_health=lambda health: _collect_health(seen, health),
        )

        task = await start(transport)
        await wait_for(lambda: len(seen) >= 1)
        await asyncio.sleep(0.05)
        assert len(seen) == len({(item.ws_connected, item.fallback, item.degraded, item.stale) for item in seen})
        await stop(transport, task)

    asyncio.run(scenario())


async def _collect_health(seen: list[TransportHealth], health: TransportHealth) -> None:
    seen.append(health)
