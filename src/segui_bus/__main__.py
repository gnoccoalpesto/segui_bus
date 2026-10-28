"""Command line: one fetch, one report.

    python -m segui_bus line 15
    python -m segui_bus stop 15 "marconi"
    python -m segui_bus vehicle 5284 [--stop "marconi"]

Exit code 2 means "the requested thing does not exist", with a message that
lists what was found instead.
"""

from __future__ import annotations

import argparse

from .config import load_settings
from .domain import Stop
from .live import LiveData, fetch_live
from .lookup import resolve_stops
from .report import (
    STRATEGIES,
    format_table,
    line_report,
    stop_report,
    vehicle_report,
)
from .static_gtfs import line_of_route, route_of_line


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings()
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

    return parser


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
