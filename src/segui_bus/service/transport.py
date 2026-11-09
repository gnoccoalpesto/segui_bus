"""WebSocket-first realtime transport, with polling as degraded mode.

The proxy pushes a small tick whenever a feed is republished; the tick names
the ``.pb`` URL to fetch. This module owns that loop and nothing else: no watch
logic, no notifications, no static GTFS. It hands each accepted snapshot to an
``on_update`` callback and exposes the latest snapshots.

Behaviour follows ``docs/research/2026-10-20-busone-proxy-seta-pc.md`` §4:

* bootstrap both feeds after every successful WebSocket connect;
* coalesce ticks with one dirty flag per feed (never queue a backlog);
* pin the tick URL, replace snapshots wholesale, reject older headers;
* compare ``Last-Modified`` locally and skip the parse when it is unchanged;
* flag stale snapshots from the feed header, never suppress them;
* fall back to polling only while the WebSocket is unavailable, and never run
  both paths at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime

import httpx
from websockets.asyncio.client import connect

from ..config import Settings
from ..domain import TripUpdateSnapshot, VehicleSnapshot
from ..gtfsrt import parse_trip_updates, parse_vehicle_positions

logger = logging.getLogger(__name__)

VEHICLE_POSITIONS = "vehicle_positions"
TRIP_UPDATES = "trip_updates"
FEEDS = (VEHICLE_POSITIONS, TRIP_UPDATES)

# Tick feed name -> the file stem used in the pinned URL.
_FEED_STEM = {
    VEHICLE_POSITIONS: "vehicle-positions",
    TRIP_UPDATES: "trip-updates",
}
_STEM_FEED = {stem: feed for feed, stem in _FEED_STEM.items()}

_TICK_PATH = re.compile(
    r"^/(?P<namespace>[^/]+)/gtfs-rt/(?P<stem>vehicle-positions|trip-updates)\.pb$"
)


@dataclass(frozen=True, slots=True)
class TransportTiming:
    """Intervals from research §4, injectable so tests need not wait for them."""

    no_tick_vp_s: float = 90.0
    no_tick_tu_s: float = 45.0
    stale_after_s: float = 120.0
    ws_down_before_fallback_s: float = 60.0
    status_interval_s: float = 60.0
    fallback_vp_s: float = 30.0
    fallback_tu_s: float = 15.0
    fallback_vp_jitter_s: float = 3.0
    fallback_tu_jitter_s: float = 2.0
    reconnect_initial_s: float = 1.0
    reconnect_max_s: float = 30.0
    rate_limit_initial_s: float = 15.0
    rate_limit_max_s: float = 60.0
    health_check_s: float = 1.0


@dataclass(frozen=True, slots=True)
class FeedUpdate:
    """One accepted snapshot, ready for the watch engine."""

    feed: str
    snapshot: VehicleSnapshot | TripUpdateSnapshot
    stale: bool


@dataclass(frozen=True, slots=True)
class TransportHealth:
    """The transport's own view of whether the realtime picture is healthy."""

    ws_connected: bool
    fallback: bool
    degraded: bool
    stale: bool


OnUpdate = Callable[[FeedUpdate], Awaitable[None]]
OnHealth = Callable[[TransportHealth], Awaitable[None]]


class Transport:
    """The long-running realtime loop.

    ``run`` blocks until ``close`` is called (or the task is cancelled). The
    WebSocket connector and HTTP client are injectable, so tests can drive the
    whole loop without a network.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        on_update: OnUpdate | None = None,
        on_health: OnHealth | None = None,
        http: httpx.AsyncClient | None = None,
        ws_connect: Callable[..., object] | None = None,
        timing: TransportTiming | None = None,
        rng: random.Random | None = None,
        now: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        self._on_update = on_update
        self._on_health = on_health
        self._http = http
        self._owns_http = http is None
        self._ws_connect = ws_connect or connect
        self.timing = timing or TransportTiming(
            stale_after_s=float(settings.stale_after_s),
            fallback_vp_s=float(settings.fallback_vp_interval_s),
            fallback_tu_s=float(settings.fallback_tu_interval_s),
        )
        self._rng = rng or random.Random()
        self._now = now or time.time
        self._monotonic = monotonic or time.monotonic

        self._closed = asyncio.Event()
        self._http_lock = asyncio.Lock()
        self._snapshots: dict[str, VehicleSnapshot | TripUpdateSnapshot] = {}
        self._last_seen: dict[str, int] = {}
        self._last_modified: dict[str, str | None] = {}
        self._last_tick: dict[str, float] = {}
        self._pending_url: dict[str, str] = {}
        self._dirty: set[str] = set()
        self._fetch_tasks: dict[str, asyncio.Task] = {}
        self._health_task: asyncio.Task | None = None
        self._fallback_task: asyncio.Task | None = None
        self._ws = None
        self._connected = False
        self._connected_at: float | None = None
        self._disconnected_at = self._monotonic()
        self._health = TransportHealth(False, False, False, False)
        self._not_before = 0.0
        self._rate_limit_attempt = 0

    # --- public state ------------------------------------------------------

    @property
    def health(self) -> TransportHealth:
        return self._health

    @property
    def vehicle_positions(self) -> VehicleSnapshot | None:
        snapshot = self._snapshots.get(VEHICLE_POSITIONS)
        return snapshot if isinstance(snapshot, VehicleSnapshot) else None

    @property
    def trip_updates(self) -> TripUpdateSnapshot | None:
        snapshot = self._snapshots.get(TRIP_UPDATES)
        return snapshot if isinstance(snapshot, TripUpdateSnapshot) else None

    # --- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        """Connect, serve ticks, reconnect and poll until closed."""
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers=self.settings.headers(),
                timeout=self.settings.fetch_timeout_s,
            )
        self._disconnected_at = self._monotonic()
        self._health_task = asyncio.create_task(
            self._health_loop(), name="segui_bus-transport-health"
        )
        try:
            await self._ws_loop()
        finally:
            await self._shutdown()

    async def close(self) -> None:
        """Stop the loop and close the WebSocket, if one is open."""
        self._closed.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _shutdown(self) -> None:
        tasks = [
            task
            for task in (
                self._health_task,
                self._fallback_task,
                *self._fetch_tasks.values(),
            )
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._health_task = None
        self._fallback_task = None
        self._fetch_tasks.clear()

        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    async def _sleep(self, delay: float) -> None:
        """Sleep, but wake immediately when the transport is closed."""
        if delay <= 0:
            await asyncio.sleep(0)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._closed.wait(), timeout=delay)

    # --- WebSocket loop ----------------------------------------------------

    async def _ws_loop(self) -> None:
        attempt = 0
        while not self._closed.is_set():
            try:
                headers = self.settings.headers()
                user_agent = headers.pop("User-Agent")
                connection = self._ws_connect(
                    self.settings.ws_url,
                    additional_headers=headers,
                    user_agent_header=user_agent,
                    open_timeout=self.settings.fetch_timeout_s,
                )
                async with connection as ws:
                    self._ws = ws
                    self._set_connected(True)
                    attempt = 0
                    logger.info("websocket connected to %s", self.settings.ws_url)
                    await self._stop_fallback()
                    await self._bootstrap()
                    await self._read_ticks(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any WS failure means reconnect
                logger.warning("websocket error: %s: %s", type(exc).__name__, exc)
            finally:
                self._ws = None
                self._set_connected(False)

            if self._closed.is_set():
                break
            delay = self._reconnect_delay(self.timing, attempt, self._rng)
            attempt += 1
            logger.info("websocket reconnect in %.2fs", delay)
            await self._sleep(delay)

    async def _bootstrap(self) -> None:
        """Fetch both feeds once; never wait for the first tick."""
        for feed in FEEDS:
            await self._fetch_and_apply(feed, self.settings.feed_url(_FEED_STEM[feed]))

    async def _read_ticks(self, ws) -> None:
        async for raw in ws:
            self._handle_tick(raw)

    def _handle_tick(self, raw: object) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("ignoring non-JSON websocket message")
            return

        if not isinstance(message, dict):
            return
        feed = message.get("type")
        if feed not in FEEDS:
            return
        target = self._pinned_url(feed, message.get("url"))
        if target is None:
            logger.warning("ignoring unpinned tick url: %r", message.get("url"))
            return

        self._last_tick[feed] = self._monotonic()
        self._pending_url[feed] = target
        self._dirty.add(feed)
        task = self._fetch_tasks.get(feed)
        if task is None or task.done():
            self._fetch_tasks[feed] = asyncio.create_task(
                self._fetch_loop(feed), name=f"segui_bus-fetch-{feed}"
            )

    def _pinned_url(self, feed: str, url: object) -> str | None:
        """Accept only the feed URL the tick is allowed to name."""
        if not isinstance(url, str):
            return None
        match = _TICK_PATH.fullmatch(url)
        if match is None:
            return None
        if match.group("namespace") != self.settings.namespace:
            return None
        if _STEM_FEED[match.group("stem")] != feed:
            return None
        return f"{self.settings.base_url}{url}"

    # --- fetching ----------------------------------------------------------

    async def _fetch_loop(self, feed: str) -> None:
        """Serve one feed; coalesce any ticks that arrive while fetching."""
        try:
            while feed in self._dirty and not self._closed.is_set():
                self._dirty.discard(feed)
                await self._fetch_and_apply(feed, self._pending_url[feed])
        finally:
            self._fetch_tasks.pop(feed, None)

    async def _fetch_and_apply(self, feed: str, url: str) -> None:
        fetched = await self._fetch(feed, url)
        if fetched is None:
            return
        snapshot, last_modified = fetched

        previous = self._last_seen.get(feed)
        if previous is not None and snapshot.feed_timestamp < previous:
            logger.warning(
                "dropping out-of-order %s snapshot: %s < %s",
                feed,
                snapshot.feed_timestamp,
                previous,
            )
            return

        self._last_modified[feed] = last_modified
        self._last_seen[feed] = snapshot.feed_timestamp
        self._snapshots[feed] = snapshot
        stale = self._stale(snapshot, self._now())
        await self._emit(FeedUpdate(feed=feed, snapshot=snapshot, stale=stale))

    async def _fetch(
        self, feed: str, url: str
    ) -> tuple[VehicleSnapshot | TripUpdateSnapshot, str | None] | None:
        if self._http is None:
            return None

        async with self._http_lock:
            if self._monotonic() < self._not_before:
                logger.debug("rate limited; skipping %s until backoff expires", feed)
                return None

            try:
                response = await self._http.get(url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed fetch is not fatal
                logger.warning("GET %s failed: %s: %s", url, type(exc).__name__, exc)
                return None

            if response.status_code == 429:
                delay = self._record_rate_limit(response.headers.get("Retry-After"))
                logger.warning("429 from %s; backing off %.2fs", url, delay)
                return None

            self._rate_limit_attempt = 0
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("GET %s failed: %s", url, exc)
                return None

            last_modified = response.headers.get("last-modified")
            if last_modified is not None and self._last_modified.get(feed) == last_modified:
                logger.debug("%s unchanged (Last-Modified %s); skipping parse", feed, last_modified)
                return None

            try:
                snapshot = self._parse(feed, response.content)
            except ValueError as exc:
                logger.warning("GET %s returned an unparsable feed: %s", url, exc)
                return None

            return snapshot, last_modified

    def _parse(self, feed: str, payload: bytes) -> VehicleSnapshot | TripUpdateSnapshot:
        received_at = self._now()
        if feed == VEHICLE_POSITIONS:
            return parse_vehicle_positions(payload, received_at=received_at)
        return parse_trip_updates(payload, received_at=received_at)

    def _record_rate_limit(self, retry_after: str | None) -> float:
        attempt = self._rate_limit_attempt
        self._rate_limit_attempt += 1
        exponential = self.timing.rate_limit_initial_s * (2 ** min(attempt, 10))
        exponential = min(self.timing.rate_limit_max_s, exponential)
        delay = max(_retry_after_seconds(retry_after, self._now()), exponential)
        self._not_before = self._monotonic() + delay
        return delay

    # --- health and fallback ----------------------------------------------

    async def _health_loop(self) -> None:
        while not self._closed.is_set():
            await self._sleep(self.timing.health_check_s)
            if self._closed.is_set():
                break
            await self._health_check()

    async def _health_check(self) -> None:
        mono = self._monotonic()
        connected = self._connected

        degraded = False
        if connected:
            for feed, threshold in (
                (VEHICLE_POSITIONS, self.timing.no_tick_vp_s),
                (TRIP_UPDATES, self.timing.no_tick_tu_s),
            ):
                last = self._last_tick.get(feed)
                if last is None or mono - last > threshold:
                    degraded = True
                    break

        newly_degraded = degraded and not self._health.degraded
        stale = any(self._stale(snapshot, self._now()) for snapshot in self._snapshots.values())

        if connected:
            await self._stop_fallback()
        elif (
            mono - self._disconnected_at >= self.timing.ws_down_before_fallback_s
            and not self._fallback_running()
        ):
            self._fallback_task = asyncio.create_task(
                self._fallback_loop(), name="segui_bus-transport-fallback"
            )

        await self._update_health(
            connected=connected,
            fallback=self._fallback_running(),
            degraded=degraded,
            stale=stale,
        )

        if newly_degraded:
            logger.warning("realtime feeds silent; probing status and marking degraded")
            await self._probe_status()

    async def _fallback_loop(self) -> None:
        """Poll the feeds only while the WebSocket is down."""
        next_vp = 0.0
        next_tu = 0.0
        next_status = 0.0
        try:
            while not self._closed.is_set() and not self._connected:
                mono = self._monotonic()
                due = min(next_vp, next_tu, next_status)
                if mono < due:
                    await self._sleep(due - mono)
                    continue

                if mono >= next_status:
                    await self._probe_status()
                    next_status = self._monotonic() + self.timing.status_interval_s
                if mono >= next_vp:
                    await self._fetch_and_apply(
                        VEHICLE_POSITIONS,
                        self.settings.feed_url(_FEED_STEM[VEHICLE_POSITIONS]),
                    )
                    next_vp = self._monotonic() + self._jitter(
                        self.timing.fallback_vp_s, self.timing.fallback_vp_jitter_s
                    )
                if mono >= next_tu:
                    await self._fetch_and_apply(
                        TRIP_UPDATES,
                        self.settings.feed_url(_FEED_STEM[TRIP_UPDATES]),
                    )
                    next_tu = self._monotonic() + self._jitter(
                        self.timing.fallback_tu_s, self.timing.fallback_tu_jitter_s
                    )
        finally:
            if self._fallback_task is asyncio.current_task():
                self._fallback_task = None

    def _fallback_running(self) -> bool:
        task = self._fallback_task
        return task is not None and not task.done()

    async def _stop_fallback(self) -> None:
        task = self._fallback_task
        self._fallback_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _probe_status(self) -> None:
        if self._http is None:
            return
        url = f"{self.settings.base_url}/{self.settings.namespace}/status"
        async with self._http_lock:
            if self._monotonic() < self._not_before:
                return
            try:
                response = await self._http.get(url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a probe failure is only logged
                logger.warning("status probe failed: %s: %s", type(exc).__name__, exc)
                return
            if response.status_code == 429:
                self._record_rate_limit(response.headers.get("Retry-After"))
                return
            self._rate_limit_attempt = 0
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("status probe failed: %s", exc)
                return
        logger.info("status probe: HTTP %s", response.status_code)

    async def _update_health(
        self, *, connected: bool, fallback: bool, degraded: bool, stale: bool
    ) -> None:
        health = TransportHealth(
            ws_connected=connected,
            fallback=fallback,
            degraded=degraded,
            stale=stale,
        )
        if health == self._health:
            return
        self._health = health
        if self._on_health is not None:
            try:
                await self._on_health(health)
            except Exception:
                logger.exception("transport health callback failed")

    async def _emit(self, update: FeedUpdate) -> None:
        if self._on_update is None:
            return
        try:
            await self._on_update(update)
        except Exception:
            logger.exception("transport update callback failed")

    def _set_connected(self, connected: bool) -> None:
        was_connected = self._connected
        self._connected = connected
        mono = self._monotonic()
        if connected:
            self._connected_at = mono
            self._disconnected_at = 0.0
            for feed in FEEDS:
                self._last_tick[feed] = mono
        elif was_connected:
            # Only a real transition starts the fallback clock; a failed
            # reconnect attempt must not keep postponing it.
            self._connected_at = None
            self._disconnected_at = mono

    def _stale(self, snapshot: VehicleSnapshot | TripUpdateSnapshot, now: float) -> bool:
        return now - snapshot.feed_timestamp > self.timing.stale_after_s

    def _jitter(self, base: float, spread: float) -> float:
        return max(0.0, base + self._rng.uniform(-spread, spread))

    @staticmethod
    def _reconnect_delay(timing: TransportTiming, attempt: int, rng: random.Random) -> float:
        base = min(timing.reconnect_max_s, timing.reconnect_initial_s * (2 ** min(attempt, 10)))
        return base * rng.uniform(0.8, 1.2)


def _retry_after_seconds(value: str | None, now: float) -> float:
    """``Retry-After`` is either delta-seconds or an HTTP date."""
    if value is None:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, moment.timestamp() - now)
