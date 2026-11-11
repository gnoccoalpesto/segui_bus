"""Command line: one-shot diagnostics and the long-running notifier.

    python -m segui_bus line 15
    python -m segui_bus stop 15 "marconi"
    python -m segui_bus vehicle 5284 [--stop "marconi"]
    python -m segui_bus watch [--preset 15_marconi] [--config PATH]
    python -m segui_bus monitor --line 15 --stop marconi --lead-minutes 5 --earliest 07:30

Exit code 2 means "the requested thing does not exist" or "the service is not
configured", with a message that lists what was found instead.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from .clock import TIMEZONE
from .config import (
    REVALIDATION_RETRY_S,
    Settings,
    calendar_warning,
    load_settings,
    next_local_midnight,
)
from .domain import StaticFeed, Stop
from .live import LiveData, fetch_live
from .lookup import resolve_stops
from .notify.commands import PresetCommands, WatchChange
from .notify.telegram import CHAT_ID_ENV, TOKEN_ENV, TelegramNotifier
from .presets import Preset, PresetError, PresetStore, parse_earliest, resolve_watch
from .report import (
    STRATEGIES,
    format_table,
    line_report,
    stop_report,
    vehicle_report,
)
from .service.transport import FeedUpdate, Transport, TransportHealth
from .service.watch import AlarmEvent, WatchConfig, WatchEngine
from .static_gtfs import (
    StaticFeedUnavailable,
    is_date_covered,
    line_of_route,
    load_static,
    route_of_line,
)

logger = logging.getLogger("segui_bus")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings()

    if args.command in ("watch", "monitor"):
        return _service(args, settings)

    live = fetch_live(settings)

    if args.command == "line":
        return _line(live, args)
    if args.command == "stop":
        return _stop(live, args)
    return _vehicle(live, args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="segui_bus",
        description="Where are the SETA Piacenza buses of a line, right now?",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    line = commands.add_parser("line", help="every bus of a line")
    line.add_argument("line", help='human line number, e.g. "15"')
    line.add_argument("--strategy", choices=STRATEGIES, default="nearest")

    stop = commands.add_parser("stop", help="every bus of a line, against one stop")
    stop.add_argument("line", help='human line number, e.g. "15"')
    stop.add_argument("query", help='stop id, name fragment or "lat,lon"')
    stop.add_argument("--radius", type=float, default=150.0, metavar="M")
    stop.add_argument("--strategy", choices=STRATEGIES, default="fence")

    vehicle = commands.add_parser("vehicle", help="one bus, its progress and itinerary")
    vehicle.add_argument("vehicle_id", help="matricola, e.g. 5284")
    vehicle.add_argument("--stop", dest="stop_query", help="also measure the distance to this stop")
    vehicle.add_argument("--radius", type=float, default=150.0, metavar="M")

    watch = commands.add_parser("watch", help="run the long-lived Telegram-managed notifier")
    watch.add_argument("--preset", metavar="ID", help="follow this preset for this run only")
    watch.add_argument(
        "--config",
        metavar="PATH",
        help="config.json path (default: $SEGUI_BUS_HOME/config.json)",
    )

    monitor = commands.add_parser("monitor", help="run one ephemeral watch from the command line")
    monitor.add_argument("--line", required=True, help='human line number, e.g. "15"')
    monitor.add_argument("--stop", required=True, help='stop id, name fragment or "lat,lon"')
    monitor.add_argument(
        "--lead-minutes", type=int, required=True, metavar="MIN", help="ETA lead time in minutes"
    )
    monitor.add_argument(
        "--earliest", required=True, help="earliest service-day arrival (HH, HHMM, HH:MM, ...)"
    )
    monitor.add_argument("--radius", type=float, default=150.0, metavar="M")
    monitor.add_argument("--stops", type=_stops_value, default="auto", metavar="N")
    monitor.add_argument("--strategy", choices=(*STRATEGIES, "auto"), default="auto")

    return parser


def _stops_value(value: str) -> int | str:
    text = value.strip().casefold()
    if text == "auto":
        return "auto"
    try:
        number = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use an integer or 'auto'") from exc
    if not 1 <= number <= 10:
        raise argparse.ArgumentTypeError("stops must be between 1 and 10")
    return number


# --- long-running service --------------------------------------------------


@dataclass(slots=True)
class _Runtime:
    """Everything the running service mutates between ticks."""

    settings: Settings
    feed: StaticFeed
    notifier: TelegramNotifier
    store: PresetStore | None = None
    engine: WatchEngine | None = None
    followed_id: str | None = None
    monitor_preset: Preset | None = None
    startup_preset: str | None = None
    startup_selected: str | None = None
    last_calendar_warning: date | None = None
    transport: Transport | None = None
    queue: asyncio.Queue[AlarmEvent] = field(default_factory=asyncio.Queue)

    def tick(self) -> None:
        """Evaluate the followed watch against the transport's latest snapshots."""
        if self.engine is None or self.transport is None:
            return
        vehicles = self.transport.vehicle_positions
        updates = self.transport.trip_updates
        if vehicles is None or updates is None:
            return

        live = LiveData(self.feed, vehicles, updates, int(time.time()))
        for event in self.engine.tick(live):
            logger.info("alarm %s for line %s bus %s", event.kind, event.line, event.vehicle_id)
            self.queue.put_nowait(event)

    def follow_change(self, change: WatchChange) -> None:
        """Apply a command's followed-watch change."""
        self.startup_preset = None
        if change.watch is None:
            self.engine = None
            self.followed_id = None
            return
        watch = self._with_settings(change.watch)
        if self.engine is not None and not change.reset:
            self.engine.watch = watch
        else:
            self.engine = WatchEngine(watch)
        self.followed_id = change.watch_id

    def reconcile(self) -> None:
        """Re-read the store after a hot reload without losing threshold-only state."""
        if self.store is None:
            return

        if self.startup_preset is not None and self.store.selected == self.startup_selected:
            self._set_engine(self.store.watch(self.startup_preset), self.startup_preset)
            return

        self.startup_preset = None
        if self.store.selected is None:
            self.engine = None
            self.followed_id = None
            return
        self._set_engine(self.store.watch(self.store.selected), self.store.selected)

    def _set_engine(self, watch: WatchConfig | None, watch_id: str | None) -> None:
        if watch is None:
            logger.warning("preset %r is invalid; no watch is followed", watch_id)
            self.engine = None
            self.followed_id = None
            return
        watch = self._with_settings(watch)
        current = self.engine.watch if self.engine is not None else None
        if current is not None and _same_identity(current, watch):
            self.engine.watch = watch
        else:
            self.engine = WatchEngine(watch)
        self.followed_id = watch_id

    def _with_settings(self, watch: WatchConfig) -> WatchConfig:
        if watch.stale_after_s == self.settings.stale_after_s:
            return watch
        return replace(watch, stale_after_s=self.settings.stale_after_s)


def _service(args: argparse.Namespace, settings: Settings) -> int:
    _configure_logging(settings.log_level)
    logger.info("segui_bus %s · %s · %s", settings.version, settings.namespace, settings.use)

    missing = _missing_credentials(settings)
    if missing:
        print(f"Telegram is not configured: missing {', '.join(missing)}", file=sys.stderr)
        return 2

    try:
        feed = load_static(settings)
    except StaticFeedUnavailable as exc:
        print(f"static GTFS unavailable: {exc}", file=sys.stderr)
        return 1

    notifier = TelegramNotifier(settings.telegram_token or "", settings.telegram_chat_id or "")
    runtime = _Runtime(settings=settings, feed=feed, notifier=notifier)
    _log_feed_calendar(runtime)
    _log_calendar_status(runtime)

    commands: PresetCommands | None = None
    if args.command == "watch":
        path = Path(args.config).expanduser() if args.config else settings.config_path
        try:
            store = PresetStore(path, feed)
        except PresetError as exc:
            print(f"cannot load preset store {path}: {exc}", file=sys.stderr)
            return 1

        runtime.store = store
        runtime.startup_selected = store.selected
        runtime.startup_preset = args.preset

        if args.preset is not None:
            preset = store.get(args.preset)
            watch = store.watch(preset.watch_id) if preset is not None else None
            if preset is None or watch is None:
                reason = store.error(args.preset) or "unknown preset"
                print(f"cannot follow preset {args.preset!r}: {reason}", file=sys.stderr)
                return 2
            runtime._set_engine(watch, preset.watch_id)
        elif store.selected is not None:
            watch = store.watch(store.selected)
            if watch is None:
                reason = store.error(store.selected) or "invalid preset"
                print(
                    f"cannot follow selected preset {store.selected!r}: {reason}",
                    file=sys.stderr,
                )
                return 2
            runtime._set_engine(watch, store.selected)

        commands = PresetCommands(
            store,
            settings.telegram_chat_id or "",
            token=settings.telegram_token or "",
            on_watch_change=runtime.follow_change,
        )
    else:
        try:
            if not 1 <= args.lead_minutes <= 1440:
                raise PresetError("lead_minutes must be between 1 and 1440")
            if not 1 <= args.radius <= 10000:
                raise PresetError("radius_m must be between 1 and 10000")
            preset = Preset(
                watch_id="monitor",
                line=args.line,
                stop=args.stop,
                lead_minutes=args.lead_minutes,
                earliest=parse_earliest(args.earliest),
                radius_m=args.radius,
                stops_n=args.stops,
                strategy=args.strategy,
            )
            watch = resolve_watch(preset, feed, stale_after_s=settings.stale_after_s)
        except PresetError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        runtime.monitor_preset = preset
        runtime.engine = WatchEngine(watch)
        runtime.followed_id = preset.watch_id

    try:
        return asyncio.run(_serve(runtime, commands))
    except KeyboardInterrupt:
        return 0


def _missing_credentials(settings: Settings) -> list[str]:
    missing = []
    if not settings.telegram_token:
        missing.append(TOKEN_ENV)
    if not settings.telegram_chat_id:
        missing.append(CHAT_ID_ENV)
    return missing


async def _serve(runtime: _Runtime, commands: PresetCommands | None) -> int:
    """Run until SIGTERM/SIGINT, then shut the whole service down."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    _install_signal_handlers(loop, stop)

    async def on_update(_update: FeedUpdate) -> None:
        runtime.tick()

    async def on_health(health: TransportHealth) -> None:
        logger.info(
            "transport ws=%s fallback=%s degraded=%s stale=%s",
            health.ws_connected,
            health.fallback,
            health.degraded,
            health.stale,
        )

    transport = Transport(runtime.settings, on_update=on_update, on_health=on_health)
    runtime.transport = transport

    tasks = [
        asyncio.create_task(transport.run(), name="segui_bus-transport"),
        asyncio.create_task(_notify_loop(runtime), name="segui_bus-notifier"),
        asyncio.create_task(_revalidate_loop(runtime, stop), name="segui_bus-revalidate"),
    ]
    if runtime.store is not None:
        tasks.append(asyncio.create_task(_hot_reload_loop(runtime, stop), name="segui_bus-config"))

    command_task = None
    if commands is not None:
        command_task = asyncio.create_task(commands.run(), name="segui_bus-commands")
        tasks.append(command_task)

    transport_task = tasks[0]
    stop_task = asyncio.create_task(stop.wait(), name="segui_bus-stop")
    await asyncio.wait({stop_task, transport_task}, return_when=asyncio.FIRST_COMPLETED)

    if transport_task.done() and not stop.is_set():
        with contextlib.suppress(asyncio.CancelledError):
            error = transport_task.exception()
            if error is not None:
                logger.error("transport stopped unexpectedly: %s: %s", type(error).__name__, error)

    stop.set()
    if command_task is not None:
        command_task.cancel()
        await asyncio.gather(command_task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await commands.close()
    await transport.close()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if not stop_task.done():
        stop_task.cancel()
    await asyncio.gather(stop_task, return_exceptions=True)

    _log_shutdown_summary(runtime, transport)
    _flush_logs()
    return 0


async def _notify_loop(runtime: _Runtime) -> None:
    while True:
        event = await runtime.queue.get()
        await runtime.notifier.send(event)


async def _hot_reload_loop(runtime: _Runtime, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await _sleep_or_stop(stop, 1.0)
        if stop.is_set() or runtime.store is None:
            continue
        if runtime.store.refresh():
            logger.info("config.json reloaded")
            runtime.reconcile()


async def _revalidate_loop(runtime: _Runtime, stop: asyncio.Event) -> None:
    while not stop.is_set():
        delay = next_local_midnight() - time.time()
        if delay > 0:
            await _sleep_or_stop(stop, delay)
        if stop.is_set():
            break

        while not stop.is_set():
            try:
                feed = await asyncio.to_thread(load_static, runtime.settings)
            except Exception as exc:  # noqa: BLE001 - keep the cached feed and retry
                logger.warning(
                    "static GTFS revalidation failed: %s: %s; retrying in %d minutes",
                    type(exc).__name__,
                    exc,
                    REVALIDATION_RETRY_S // 60,
                )
                _log_calendar_status(runtime)
                await _sleep_or_stop(stop, REVALIDATION_RETRY_S)
                continue

            _apply_feed(runtime, feed)
            break


async def _sleep_or_stop(stop: asyncio.Event, delay: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, delay))


def _apply_feed(runtime: _Runtime, feed: StaticFeed) -> None:
    if feed.etag is not None and feed.etag == runtime.feed.etag:
        logger.info("static GTFS unchanged (ETag %s)", feed.etag)
        _log_calendar_status(runtime)
        return

    runtime.feed = feed
    if runtime.store is not None:
        runtime.store.revalidate(feed)
        runtime.reconcile()
    elif runtime.monitor_preset is not None:
        try:
            watch = resolve_watch(
                runtime.monitor_preset,
                feed,
                stale_after_s=runtime.settings.stale_after_s,
            )
        except PresetError as exc:
            logger.warning("monitor watch disabled after GTFS reload: %s", exc)
            runtime.engine = None
        else:
            runtime._set_engine(watch, runtime.followed_id)

    _log_feed_calendar(runtime)
    _log_calendar_status(runtime)


def _log_feed_calendar(runtime: _Runtime) -> None:
    feed = runtime.feed
    today = datetime.now(TIMEZONE).strftime("%Y%m%d")
    covered = is_date_covered(feed, today)
    if feed.calendar_range is None:
        logger.warning("static GTFS has no calendar dates; today %s covered=False", today)
    else:
        first, last = feed.calendar_range
        logger.info(
            "static GTFS calendar %s..%s; today %s covered=%s",
            first,
            last,
            today,
            covered,
        )


def _log_calendar_status(runtime: _Runtime) -> None:
    today = datetime.now(TIMEZONE).date()
    if runtime.last_calendar_warning == today:
        return
    warning = calendar_warning(runtime.feed, today)
    if warning is None:
        return
    logger.warning(warning)
    runtime.last_calendar_warning = today


def _log_shutdown_summary(runtime: _Runtime, transport: Transport) -> None:
    vehicles = transport.vehicle_positions
    age = "n/a" if vehicles is None else f"{int(time.time() - vehicles.feed_timestamp)}s"
    logger.info(
        "stopped; followed watch=%s; last feed age=%s",
        runtime.followed_id or "none",
        age,
    )


def _flush_logs() -> None:
    for handler in logging.getLogger().handlers:
        with contextlib.suppress(Exception):
            handler.flush()
    logging.shutdown()


def _configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        numeric = logging.INFO

    root = logging.getLogger()
    if root.handlers:
        root.setLevel(numeric)
        return
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def handler(signum: int) -> None:
        if stop.is_set():
            logger.warning("second signal %s; exiting immediately", signum)
            os._exit(128 + signum)
        logger.info("signal %s received; shutting down", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, handler, sig)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, lambda signum, _frame: handler(signum))
            except (ValueError, OSError):
                pass


def _same_identity(left: WatchConfig, right: WatchConfig) -> bool:
    return (
        left.line.casefold() == right.line.casefold()
        and left.stop.stop_id == right.stop.stop_id
    )


# --- one-shot diagnostics --------------------------------------------------


def _line(live: LiveData, args: argparse.Namespace) -> int:
    if route_of_line(live.feed, args.line) is None:
        return _unknown_line(args.line)

    report = line_report(
        live.feed,
        live.vehicles,
        live.updates,
        args.line,
        now=live.now,
        strategy=args.strategy,
    )
    print(format_table(report))
    return 0


def _stop(live: LiveData, args: argparse.Namespace) -> int:
    if route_of_line(live.feed, args.line) is None:
        return _unknown_line(args.line)

    stop = _resolve(live, args.line, args.query)
    if stop is None:
        return 2

    report = stop_report(
        live.feed,
        live.vehicles,
        live.updates,
        args.line,
        stop,
        radius_m=args.radius,
        now=live.now,
        strategy=args.strategy,
    )
    print(format_table(report))
    return 0


def _vehicle(live: LiveData, args: argparse.Namespace) -> int:
    vehicle = live.vehicles.by_id(args.vehicle_id)
    if vehicle is None:
        print(f"vehicle {args.vehicle_id} is not reporting right now")
        return 2

    stop = None
    if args.stop_query is not None:
        line = line_of_route(live.feed, vehicle.trip.route_id) if vehicle.trip else None
        if line is None:
            print(f"vehicle {args.vehicle_id} has no trip, so its line is unknown")
            return 2
        stop = _resolve(live, line, args.stop_query)
        if stop is None:
            return 2

    report = vehicle_report(
        live.feed,
        live.vehicles,
        live.updates,
        args.vehicle_id,
        stop=stop,
        radius_m=args.radius,
        now=live.now,
    )
    if report is None:
        print(f"vehicle {args.vehicle_id} is not reporting right now")
        return 2

    print(format_table(report))
    return 0


def _resolve(live: LiveData, line: str, query: str) -> Stop | None:
    matches = resolve_stops(live.feed, line, query)
    if not matches:
        print(f'line {line} serves no stop matching "{query}"')
        return None
    if len(matches) > 1:
        listed = ", ".join(f"{stop.stop_id} ({stop.name})" for stop in matches[:5])
        print(f"{len(matches)} stops match; using {matches[0].stop_id}: {listed}")
    return matches[0]


def _unknown_line(line: str) -> int:
    print(f"unknown line: {line}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
