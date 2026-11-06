"""PROTOTYPE — throwaway WebSocket tick logger for issue #7.

Answers docs/research/2026-10-20-busone-proxy-seta-pc.md §8: does the
``seta-pc`` WebSocket keep emitting during upstream fleet-poll outages?

This is not production code and not a test. Run it directly:

    python tests/experiments/ws_tick_logger_prototype.py --duration 600

It writes one JSON object per line to ``--out`` and prints a summary to stdout.
Delete this file once the finding is written.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect
except ImportError:  # pragma: no cover - prototype convenience
    sys.exit("PROTOTYPE needs the websockets package: python -m pip install websockets")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402

from segui_bus.config import load_settings  # noqa: E402

FEEDS = ("vehicle_positions", "trip_updates")
BACKOFF_CAP_S = 30.0


def wall_clock(epoch: float | None = None) -> str:
    """UTC wall clock, milliseconds, explicit offset."""
    moment = datetime.fromtimestamp(epoch if epoch is not None else time.time(), timezone.utc)
    return moment.isoformat(timespec="milliseconds")


class Log:
    """JSONL sink plus a human-readable line on stdout. Thread-safe."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, event: str, **fields: object) -> dict[str, object]:
        record: dict[str, object] = {
            "wall": wall_clock(),
            "epoch": round(time.time(), 3),
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._file.write(line + "\n")
            self._file.flush()
        return record

    def close(self) -> None:
        with self._lock:
            self._file.close()


def status_url(settings: object) -> str:
    return f"{settings.base_url}/{settings.namespace}/status"


def probe_status(settings: object, log: Log) -> dict[str, object] | None:
    """One /status GET, so feed silence can be correlated with the upstream poller."""
    try:
        with httpx.Client(headers=settings.headers(), timeout=settings.fetch_timeout_s) as http:
            response = http.get(status_url(settings))
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 - prototype: log, never crash
        log.write("status_error", error=type(exc).__name__, message=str(exc))
        return None

    now = time.time()
    feeds = data.get("feeds", {})
    logged: dict[str, object] = {
        "ws_clients": data.get("ws_clients"),
        "status_keys": sorted(data),
        "gtfs_built_at": (data.get("gtfs") or {}).get("built_at"),
    }
    for feed in FEEDS:
        item = feeds.get(feed, {})
        timestamp = item.get("timestamp")
        logged[f"{feed}_count"] = item.get("count")
        logged[f"{feed}_timestamp"] = timestamp
        logged[f"{feed}_age_s"] = None if timestamp is None else round(now - timestamp, 3)
    log.write("status", **logged)
    return logged


def status_loop(settings: object, log: Log, every_s: float, stop: threading.Event) -> None:
    """Background /status probe: keeps the receive loop free of HTTP latency."""
    while not stop.is_set():
        probe_status(settings, log)
        stop.wait(every_s)


def backoff_seconds(attempt: int) -> float:
    """1, 2, 4, ... capped at 30 s, with ±20 % jitter."""
    base = min(BACKOFF_CAP_S, 2.0 ** min(attempt, 5))
    return base * random.uniform(0.8, 1.2)


def main() -> int:
    parser = argparse.ArgumentParser(description="PROTOTYPE: log seta-pc WS ticks")
    parser.add_argument("--duration", type=float, default=600.0, help="seconds to run")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "docs/research/2026-11-04-ws-ticks.jsonl",
        help="raw JSONL log",
    )
    parser.add_argument(
        "--status-every",
        type=float,
        default=60.0,
        help="seconds between /status probes (0 disables)",
    )
    parser.add_argument(
        "--gap-warn",
        type=float,
        default=90.0,
        help="log a gap when server timestamps for one feed jump by this many seconds",
    )
    parser.add_argument(
        "--force-reconnect-after",
        type=float,
        default=0.0,
        help="close the socket after this many seconds to exercise reconnect (0 disables)",
    )
    args = parser.parse_args()

    settings = load_settings()
    log = Log(args.out)
    started = time.monotonic()
    deadline = started + args.duration

    log.write(
        "start",
        ws_url=settings.ws_url,
        duration_s=args.duration,
        headers=settings.headers(),
        gap_warn_s=args.gap_warn,
    )

    stop_status = threading.Event()
    status_thread: threading.Thread | None = None
    if args.status_every:
        status_thread = threading.Thread(
            target=status_loop,
            args=(settings, log, args.status_every, stop_status),
            daemon=True,
        )
        status_thread.start()

    last_receive_at: dict[str, float] = {}
    last_server_ts: dict[str, int] = {}
    last_any_tick_at: float | None = None
    counts: dict[str, int] = {feed: 0 for feed in FEEDS}
    zero_counts: dict[str, int] = {feed: 0 for feed in FEEDS}
    gaps: list[dict[str, object]] = []
    reconnects = 0
    errors = 0
    attempt = 0

    try:
        while time.monotonic() < deadline:
            try:
                headers = settings.headers()
                user_agent = headers.pop("User-Agent")
                connection = connect(
                    settings.ws_url,
                    additional_headers=headers,
                    user_agent_header=user_agent,
                    open_timeout=settings.fetch_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 - prototype: log and retry
                errors += 1
                delay = backoff_seconds(attempt)
                log.write(
                    "connect_error",
                    attempt=attempt + 1,
                    error=type(exc).__name__,
                    message=str(exc),
                    retry_in_s=round(delay, 2),
                )
                attempt += 1
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                continue

            attempt = 0
            try:
                with connection as ws:
                    log.write("connected", ws_url=settings.ws_url)
                    connected_at = time.monotonic()
                    while time.monotonic() < deadline:
                        if (
                            args.force_reconnect_after
                            and time.monotonic() - connected_at >= args.force_reconnect_after
                        ):
                            log.write("forced_reconnect", after_s=args.force_reconnect_after)
                            ws.close()
                            break

                        try:
                            raw = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue

                        received = time.time()
                        try:
                            tick = json.loads(raw)
                        except (TypeError, ValueError):
                            log.write("non_json", payload=str(raw)[:500])
                            continue

                        feed = tick.get("type")
                        timestamp = tick.get("timestamp")
                        count = tick.get("count")

                        previous_receive = last_receive_at.get(feed)
                        previous_ts = last_server_ts.get(feed)
                        server_gap = (
                            None
                            if previous_ts is None or timestamp is None
                            else timestamp - previous_ts
                        )
                        if server_gap is not None and server_gap >= args.gap_warn:
                            gap = {
                                "feed": feed,
                                "server_gap_s": server_gap,
                                "previous_timestamp": previous_ts,
                                "timestamp": timestamp,
                            }
                            gaps.append(gap)
                            log.write("gap", **gap)

                        last_receive_at[feed] = received
                        if timestamp is not None:
                            last_server_ts[feed] = timestamp
                        last_any_tick_at = received
                        if feed in counts:
                            counts[feed] += 1
                            if count == 0:
                                zero_counts[feed] += 1

                        log.write(
                            "tick",
                            type=feed,
                            timestamp=timestamp,
                            count=count,
                            url=tick.get("url"),
                            age_s=None if timestamp is None else round(received - timestamp, 3),
                            receive_gap_s=None
                            if previous_receive is None
                            else round(received - previous_receive, 3),
                            server_gap_s=server_gap,
                        )
            except ConnectionClosed as exc:
                log.write(
                    "closed",
                    code=exc.code,
                    reason=exc.reason,
                    sent=exc.sent,
                    received=exc.received,
                )
            except Exception as exc:  # noqa: BLE001 - prototype: log and retry
                errors += 1
                log.write("error", error=type(exc).__name__, message=str(exc))

            if time.monotonic() < deadline:
                reconnects += 1
                delay = backoff_seconds(attempt)
                log.write("reconnect", reconnects=reconnects, retry_in_s=round(delay, 2))
                attempt += 1
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        log.write("interrupted")
    finally:
        stop_status.set()
        if status_thread is not None:
            status_thread.join(timeout=settings.fetch_timeout_s + 2)
        summary = log.write(
            "summary",
            elapsed_s=round(time.monotonic() - started, 3),
            ticks=counts,
            zero_count_ticks=zero_counts,
            gaps=gaps,
            reconnects=reconnects,
            errors=errors,
            last_tick_at={feed: wall_clock(at) for feed, at in last_receive_at.items()},
            last_any_tick_at=None if last_any_tick_at is None else wall_clock(last_any_tick_at),
            out=str(args.out),
        )
        log.close()
        print(json.dumps(summary, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
