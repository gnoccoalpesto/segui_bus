"""Watch presets and the auto values derived from the static feed.

A preset is an inert, stored watch. Loading ``config.json`` validates it,
resolves ``line``/``stop`` against the static feed and computes every ``auto``
value; only then does it become a :class:`~segui_bus.service.watch.WatchConfig`
the engine can follow. Nothing here knows about transport, Telegram or the CLI.

The file is the operator-facing contract from issue #3: version 1, ``selected``
plus a ``watches`` map, atomic writes, canonical lowercase ids, and the literal
``auto`` sentinel. Computed values are never persisted.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import statistics
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clock import parse_hms
from .domain import Route, StaticFeed, Stop
from .geo import distance_m
from .lookup import resolve_stops
from .report import STRATEGIES
from .service.watch import WatchConfig
from .static_gtfs import route_of_line

logger = logging.getLogger(__name__)

AUTO = "auto"

WATCH_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")
RESERVED_IDS = frozenset({"inline", "monitor"})

DEFAULT_RADIUS_M = 150.0
DEFAULT_ON_TIME_TOLERANCE_S = 240
DEFAULT_DEBOUNCE_S = 1.0

VERSION = 1
TOP_LEVEL_KEYS = frozenset({"version", "selected", "watches"})

# Locked order from #3: the same order the Telegram template uses.
PRESET_KEYS = (
    "line",
    "stop",
    "radius_m",
    "stops_n",
    "lead_minutes",
    "earliest",
    "on_time_tolerance_s",
    "assumed_speed_kmh",
    "route_detour_factor",
    "position_mismatch_m",
    "strategy",
)
REQUIRED_KEYS = ("line", "stop", "lead_minutes", "earliest")


class PresetError(ValueError):
    """A preset or the config file is invalid."""


# --- pure parsing ----------------------------------------------------------


def canonical_id(text: str) -> str:
    """Canonical watch id: lowercase ``[a-z0-9_-]+``, no reserved names."""
    if not isinstance(text, str):
        raise PresetError("watch id must be a string")
    value = text.strip().lower()
    if not value:
        raise PresetError("watch id is empty")
    if value in RESERVED_IDS:
        raise PresetError(f"watch id {value!r} is reserved")
    if not WATCH_ID_PATTERN.fullmatch(value):
        raise PresetError(f"watch id {text!r} must match [a-z0-9_-]+")
    return value


def parse_earliest(value: str) -> str:
    """Canonicalise a GTFS service-day time to ``HH:MM:SS``.

    Accepted: ``HH``, ``HHMM``, ``HHMMSS``, ``HH:MM``, ``HH:MM:SS``. Hours may
    exceed 23 for trips after midnight; values up to 47:59:59 are accepted.
    """
    if not isinstance(value, str):
        raise PresetError("earliest must be a string")
    text = value.strip()
    if not text:
        raise PresetError("earliest is empty")

    if ":" in text:
        parts = text.split(":")
        if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
            raise PresetError(f"earliest {value!r} is not HH, HHMM, HHMMSS, HH:MM or HH:MM:SS")
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2]) if len(parts) == 3 else 0
    else:
        if not text.isdigit():
            raise PresetError(f"earliest {value!r} is not HH, HHMM, HHMMSS, HH:MM or HH:MM:SS")
        if len(text) <= 2:
            hours, minutes, seconds = int(text), 0, 0
        elif len(text) <= 4:
            hours, minutes, seconds = int(text[:-2]), int(text[-2:]), 0
        else:
            hours = int(text[:-4])
            minutes = int(text[-4:-2])
            seconds = int(text[-2:])

    if hours > 47 or minutes > 59 or seconds > 59:
        raise PresetError(f"earliest {value!r} is out of range")
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


# --- parsed preset ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Preset:
    """A stored watch: effective values may still be the ``auto`` sentinel."""

    watch_id: str
    line: str
    stop: str
    lead_minutes: int
    earliest: str
    radius_m: float = DEFAULT_RADIUS_M
    stops_n: int | str = AUTO
    on_time_tolerance_s: int = DEFAULT_ON_TIME_TOLERANCE_S
    assumed_speed_kmh: float | str = AUTO
    route_detour_factor: float | str = AUTO
    position_mismatch_m: float | str = AUTO
    strategy: str = AUTO

    def to_mapping(self) -> dict[str, object]:
        """The persisted shape, in the locked key order, with ``auto`` intact."""
        return {
            "line": self.line,
            "stop": self.stop,
            "radius_m": self.radius_m,
            "stops_n": self.stops_n,
            "lead_minutes": self.lead_minutes,
            "earliest": self.earliest,
            "on_time_tolerance_s": self.on_time_tolerance_s,
            "assumed_speed_kmh": self.assumed_speed_kmh,
            "route_detour_factor": self.route_detour_factor,
            "position_mismatch_m": self.position_mismatch_m,
            "strategy": self.strategy,
        }


@dataclass(frozen=True, slots=True)
class AutoValues:
    """What the static feed says the ``auto`` fields should be right now."""

    assumed_speed_kmh: float
    stops_n: int
    route_detour_factor: float
    position_mismatch_m: float
    strategy: str


@dataclass(frozen=True, slots=True)
class ResolvedPreset:
    """A preset with its stop resolved and its auto values computed."""

    preset: Preset
    stop: Stop
    auto: AutoValues
    watch: WatchConfig


# --- auto values -----------------------------------------------------------
#
# NOTE auto — parameters computed from the static feed; never persisted.
#
# assumed_speed_kmh  median scheduled segment speed of the line, clipped
#                    [10, 60] km/h; fallback 55 for E lines, 30 otherwise.
# stops_n            how many scheduled stops fit in the lead time at that
#                    speed, clipped [1, 10].
# route_detour_factor always 1.0: assume no detour.
# position_mismatch_m max(2000, 3 x median stop spacing).
# strategy           nearest.


def compute_auto_values(feed: StaticFeed, line: str, lead_s: int) -> AutoValues:
    """Compute every ``auto`` value for a line from the static feed."""
    route = route_of_line(feed, line)
    segments = list(_segments(feed, route)) if route is not None else ()

    spacings = [distance for distance, _ in segments if distance > 0]
    median_spacing = statistics.median(spacings) if spacings else None

    speeds = [distance / dt * 3.6 for distance, dt in segments if dt > 0 and distance > 50]
    if speeds:
        assumed_speed = min(60.0, max(10.0, statistics.median(speeds)))
    else:
        # Low-traffic fallback (no usable scheduled segments).
        assumed_speed = 55.0 if "E" in line.upper() else 30.0

    if median_spacing:
        stops_n = round(lead_s * (assumed_speed / 3.6) / median_spacing)
        stops_n = max(1, min(10, stops_n))
        mismatch = max(2000.0, 3.0 * median_spacing)
    else:
        stops_n = 1
        mismatch = 2000.0

    return AutoValues(
        assumed_speed_kmh=assumed_speed,
        stops_n=stops_n,
        route_detour_factor=1.0,
        position_mismatch_m=mismatch,
        strategy="nearest",
    )


def _segments(feed: StaticFeed, route: Route) -> list[tuple[float, int]]:
    """Consecutive scheduled (distance_m, dt_s) pairs across every line trip."""
    found: list[tuple[float, int]] = []
    for trip_id, trip in feed.trips.items():
        if trip.route_id != route.route_id:
            continue
        scheduled = feed.trip_stops.get(trip_id, ())
        for earlier, later in itertools.pairwise(scheduled):
            start = feed.stops.get(earlier.stop_id)
            end = feed.stops.get(later.stop_id)
            if start is None or end is None:
                continue
            dt = parse_hms(later.arrival_time) - parse_hms(earlier.arrival_time)
            found.append((distance_m(start.point, end.point), dt))
    return found


# --- resolution ------------------------------------------------------------


def resolve_preset(
    preset: Preset,
    feed: StaticFeed,
    *,
    stale_after_s: int = 120,
) -> ResolvedPreset:
    """Resolve line/stop and substitute auto values, or raise PresetError."""
    route = route_of_line(feed, preset.line)
    if route is None:
        raise PresetError(f"unknown line {preset.line!r}")

    matches = resolve_stops(feed, preset.line, preset.stop)
    if not matches:
        raise PresetError(f"stop {preset.stop!r} is not served by line {preset.line!r}")
    if len(matches) > 1:
        names = ", ".join(f"{stop.stop_id} {stop.name}" for stop in matches[:5])
        raise PresetError(f"stop {preset.stop!r} is ambiguous on line {preset.line!r}: {names}")

    stop = matches[0]
    lead_s = preset.lead_minutes * 60
    auto = compute_auto_values(feed, route.line, lead_s)
    watch = WatchConfig(
        line=route.line,
        stop=stop,
        lead_s=lead_s,
        earliest=preset.earliest,
        stops_n=_effective_int(preset.stops_n, auto.stops_n),
        assumed_speed_kmh=_effective_float(preset.assumed_speed_kmh, auto.assumed_speed_kmh),
        route_detour_factor=_effective_float(preset.route_detour_factor, auto.route_detour_factor),
        position_mismatch_m=_effective_float(preset.position_mismatch_m, auto.position_mismatch_m),
        strategy=auto.strategy if _is_auto(preset.strategy) else preset.strategy,
        radius_m=preset.radius_m,
        on_time_tolerance_s=preset.on_time_tolerance_s,
        stale_after_s=stale_after_s,
    )
    return ResolvedPreset(preset=preset, stop=stop, auto=auto, watch=watch)


def resolve_watch(
    preset: Preset,
    feed: StaticFeed,
    *,
    stale_after_s: int = 120,
) -> WatchConfig:
    """The engine input for one preset."""
    return resolve_preset(preset, feed, stale_after_s=stale_after_s).watch


def _effective_int(value: int | str, auto: int) -> int:
    return auto if _is_auto(value) else int(value)


def _effective_float(value: float | str, auto: float) -> float:
    return auto if _is_auto(value) else float(value)


def _is_auto(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() == AUTO


# --- validation ------------------------------------------------------------


def _validate_preset(watch_id: str, raw: Mapping[str, Any], *, coerce: bool) -> Preset:
    """Build a Preset from JSON (strict) or command (textual) values."""
    if not isinstance(raw, Mapping):
        raise PresetError("watch must be a JSON object")

    unknown = sorted(set(raw) - set(PRESET_KEYS))
    if unknown:
        raise PresetError(f"unknown key(s): {', '.join(unknown)}")
    missing = [key for key in REQUIRED_KEYS if key not in raw]
    if missing:
        raise PresetError(f"missing required key(s): {', '.join(missing)}")

    return Preset(
        watch_id=watch_id,
        line=_text(raw["line"], "line"),
        stop=_text(raw["stop"], "stop"),
        radius_m=_number(
            raw.get("radius_m", DEFAULT_RADIUS_M),
            "radius_m",
            coerce=coerce,
            minimum=1,
            maximum=10000,
        ),
        stops_n=_stops_n(raw.get("stops_n", AUTO), coerce=coerce),
        lead_minutes=_integer(
            raw["lead_minutes"], "lead_minutes", coerce=coerce, minimum=1, maximum=1440
        ),
        earliest=parse_earliest(raw["earliest"]),
        on_time_tolerance_s=_integer(
            raw.get("on_time_tolerance_s", DEFAULT_ON_TIME_TOLERANCE_S),
            "on_time_tolerance_s",
            coerce=coerce,
            minimum=0,
            maximum=3600,
        ),
        assumed_speed_kmh=_speed(raw.get("assumed_speed_kmh", AUTO), coerce=coerce),
        route_detour_factor=_detour(raw.get("route_detour_factor", AUTO), coerce=coerce),
        position_mismatch_m=_mismatch(raw.get("position_mismatch_m", AUTO), coerce=coerce),
        strategy=_strategy(raw.get("strategy", AUTO), coerce=coerce),
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise PresetError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise PresetError(f"{name} is empty")
    return text


def _integer(
    value: object,
    name: str,
    *,
    coerce: bool,
    minimum: int,
    maximum: int,
) -> int:
    if coerce and isinstance(value, str):
        text = value.strip()
        if not text.lstrip("+-").isdigit():
            raise PresetError(f"{name} must be an integer")
        value = int(text)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PresetError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise PresetError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(
    value: object,
    name: str,
    *,
    coerce: bool,
    minimum: float,
    maximum: float,
) -> float:
    if coerce and isinstance(value, str):
        try:
            value = float(value)
        except ValueError as exc:
            raise PresetError(f"{name} must be a number") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PresetError(f"{name} must be a number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise PresetError(f"{name} must be between {minimum:g} and {maximum:g}")
    return number


def _stops_n(value: object, *, coerce: bool) -> int | str:
    if _is_auto(value):
        return AUTO
    return _integer(value, "stops_n", coerce=coerce, minimum=1, maximum=10)


def _speed(value: object, *, coerce: bool) -> float | str:
    if _is_auto(value):
        return AUTO
    return _number(value, "assumed_speed_kmh", coerce=coerce, minimum=1, maximum=120)


def _detour(value: object, *, coerce: bool) -> float | str:
    if _is_auto(value):
        return AUTO
    return _number(value, "route_detour_factor", coerce=coerce, minimum=1, maximum=5)


def _mismatch(value: object, *, coerce: bool) -> float | str:
    if _is_auto(value):
        return AUTO
    return _number(value, "position_mismatch_m", coerce=coerce, minimum=0, maximum=50000)


def _strategy(value: object, *, coerce: bool) -> str:
    if not isinstance(value, str):
        raise PresetError("strategy must be a string")
    text = value.strip()
    if text.lower() == AUTO:
        return AUTO
    if text not in STRATEGIES:
        raise PresetError(f"strategy must be auto or one of {', '.join(STRATEGIES)}")
    return text


# --- config document -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Document:
    selected: str | None
    presets: dict[str, Preset]
    invalid_raw: dict[str, dict[str, Any]]
    errors: dict[str, str]
    order: tuple[str, ...]


def _parse_document(text: str) -> _Document:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PresetError(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise PresetError("config must be a JSON object")

    unknown = sorted(set(data) - TOP_LEVEL_KEYS)
    if unknown:
        raise PresetError(f"unknown top-level key(s): {', '.join(unknown)}")
    if "version" not in data:
        raise PresetError("missing version")
    if isinstance(data["version"], bool) or data["version"] != VERSION:
        raise PresetError(f"version must be {VERSION}")
    if "watches" not in data:
        raise PresetError("missing watches")
    if not isinstance(data["watches"], dict):
        raise PresetError("watches must be an object")

    raw_watches = data["watches"]
    presets: dict[str, Preset] = {}
    invalid_raw: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

    for watch_id, raw in raw_watches.items():
        try:
            canonical = canonical_id(watch_id)
            if canonical != watch_id:
                raise PresetError(f"watch id {watch_id!r} is not canonical (use {canonical!r})")
            presets[watch_id] = _validate_preset(watch_id, raw, coerce=False)
        except PresetError as exc:
            invalid_raw[watch_id] = raw
            errors[watch_id] = str(exc)

    selected_raw = data.get("selected")
    selected: str | None = None
    if selected_raw is not None:
        if not isinstance(selected_raw, str):
            raise PresetError("selected must be null or a watch id")
        selected = canonical_id(selected_raw)
        if selected != selected_raw:
            raise PresetError(f"selected id {selected_raw!r} is not canonical (use {selected!r})")
        if selected not in raw_watches:
            raise PresetError(f"selected watch {selected!r} does not exist")

    return _Document(
        selected=selected,
        presets=presets,
        invalid_raw=invalid_raw,
        errors=errors,
        order=tuple(raw_watches),
    )


# --- store -----------------------------------------------------------------


class PresetStore:
    """The in-memory preset store and its ``config.json``.

    Constructing the store loads the file. A missing file is a valid empty
    store; a malformed file raises PresetError (fail fast at startup). Once
    running, :meth:`refresh` hot-reloads on mtime changes and keeps the last
    good store when the new file is invalid.
    """

    def __init__(
        self,
        path: str | Path,
        feed: StaticFeed,
        *,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self.feed = feed
        self.debounce_s = debounce_s
        self._monotonic = monotonic

        self.selected: str | None = None
        self.presets: dict[str, Preset] = {}
        self.raw_invalid: dict[str, dict[str, Any]] = {}
        self._errors: dict[str, str] = {}
        self._resolved: dict[str, ResolvedPreset] = {}
        self._order: list[str] = []

        self._observed_mtime_ns: int | None = None
        self._pending = False
        self._pending_mtime_ns: int | None = None
        self._pending_since: float | None = None

        if self.path.exists():
            if not self._load_from_disk(initial=True):
                raise PresetError(f"cannot load preset store {self.path}")
        else:
            self._apply_empty()

    # --- reading -----------------------------------------------------------

    def ids(self) -> tuple[str, ...]:
        """Every stored id, in file order, including disabled presets."""
        return tuple(self._order)

    def get(self, watch_id: str) -> Preset | None:
        found = self._find(watch_id)
        return self.presets.get(found) if found is not None else None

    def is_valid(self, watch_id: str) -> bool:
        found = self._find(watch_id)
        return found is not None and found in self._resolved

    def error(self, watch_id: str) -> str | None:
        found = self._find(watch_id)
        return self._errors.get(found) if found is not None else None

    def resolved(self, watch_id: str) -> ResolvedPreset | None:
        found = self._find(watch_id)
        return self._resolved.get(found) if found is not None else None

    def watch(self, watch_id: str) -> WatchConfig | None:
        resolved = self.resolved(watch_id)
        return resolved.watch if resolved is not None else None

    def _find(self, watch_id: str) -> str | None:
        try:
            canonical = canonical_id(watch_id)
        except PresetError:
            return None
        if canonical in self._order:
            return canonical
        for existing in self._order:
            try:
                if canonical_id(existing) == canonical:
                    return existing
            except PresetError:
                continue
        return None

    # --- mutation ----------------------------------------------------------

    def add(self, watch_id: str, values: Mapping[str, Any]) -> Preset:
        """Create a preset, validate it and write the file through."""
        canonical = canonical_id(watch_id)
        if self._find(canonical) is not None:
            raise PresetError(f"watch {canonical!r} already exists")

        preset = _validate_preset(canonical, values, coerce=True)
        resolve_preset(preset, self.feed)

        self.presets[canonical] = preset
        self._order.append(canonical)
        self._revalidate()
        self._write()
        return preset

    def update(self, watch_id: str, values: Mapping[str, Any]) -> Preset:
        """Modify a preset in place. Unknown ids are rejected."""
        found = self._find(watch_id)
        if found is None:
            raise PresetError(f"unknown watch {watch_id!r}")
        if canonical_id(found) != found:
            raise PresetError(f"watch id {found!r} is not canonical; remove and add it again")

        if found in self.raw_invalid:
            merged = dict(self.raw_invalid[found])
        else:
            merged = self.presets[found].to_mapping()
        merged.update(dict(values))

        preset = _validate_preset(found, merged, coerce=True)
        resolve_preset(preset, self.feed)

        self.raw_invalid.pop(found, None)
        self.presets[found] = preset
        self._revalidate()
        self._write()
        return preset

    def remove(self, watch_id: str) -> str:
        """Delete a preset and discard its state; returns the canonical id."""
        found = self._find(watch_id)
        if found is None:
            raise PresetError(f"unknown watch {watch_id!r}")

        self.presets.pop(found, None)
        self.raw_invalid.pop(found, None)
        self._order.remove(found)
        if self.selected == found:
            self.selected = None
        self._revalidate()
        self._write()
        return found

    def select(self, watch_id: str) -> Preset:
        """Persist ``selected``; the id must exist and be usable."""
        found = self._find(watch_id)
        if found is None:
            raise PresetError(f"unknown watch {watch_id!r}")
        if found not in self._resolved:
            reason = self._errors.get(found, "unresolved")
            raise PresetError(f"watch {found!r} is invalid: {reason}")

        self.selected = found
        self._write()
        return self.presets[found]

    # --- feed changes ------------------------------------------------------

    def revalidate(self, feed: StaticFeed) -> None:
        """Recompute stop resolution and auto values after a GTFS reload."""
        self.feed = feed
        self._revalidate()

    # --- hot reload --------------------------------------------------------

    def refresh(self, now: float | None = None) -> bool:
        """Apply changed config.json after a debounce; True when state changed."""
        current = self._monotonic() if now is None else now
        try:
            mtime_ns: int | None = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime_ns = None

        if mtime_ns == self._observed_mtime_ns:
            self._pending = False
            self._pending_mtime_ns = None
            self._pending_since = None
            return False

        if not self._pending or mtime_ns != self._pending_mtime_ns:
            self._pending = True
            self._pending_mtime_ns = mtime_ns
            self._pending_since = current
            return False

        if self._pending_since is None or current - self._pending_since < self.debounce_s:
            return False

        self._observed_mtime_ns = mtime_ns
        self._pending = False
        self._pending_mtime_ns = None
        self._pending_since = None

        if mtime_ns is None:
            self._apply_empty()
            return True
        return self._load_from_disk(initial=False)

    # --- internals ---------------------------------------------------------

    def _load_from_disk(self, *, initial: bool) -> bool:
        try:
            text = self.path.read_text(encoding="utf-8")
            document = _parse_document(text)
        except (OSError, PresetError) as exc:
            message = f"config {self.path} is invalid; keeping the last good store: {exc}"
            if initial:
                raise PresetError(message) from exc
            logger.error(message)
            self._remember_mtime()
            return False

        self._apply_document(document)
        self._remember_mtime()
        return True

    def _apply_document(self, document: _Document) -> None:
        self.selected = document.selected
        self.presets = dict(document.presets)
        self.raw_invalid = dict(document.invalid_raw)
        self._order = list(document.order)
        self._errors = dict(document.errors)
        self._revalidate()

    def _apply_empty(self) -> None:
        self.selected = None
        self.presets = {}
        self.raw_invalid = {}
        self._order = []
        self._errors = {}
        self._resolved = {}
        self._remember_mtime()

    def _revalidate(self) -> None:
        previous = self._errors
        resolved: dict[str, ResolvedPreset] = {}
        errors: dict[str, str] = {
            watch_id: reason
            for watch_id, reason in previous.items()
            if watch_id in self.raw_invalid
        }

        for watch_id, preset in self.presets.items():
            try:
                resolved[watch_id] = resolve_preset(preset, self.feed)
            except PresetError as exc:
                errors[watch_id] = str(exc)
                if previous.get(watch_id) != str(exc):
                    logger.warning("preset %r disabled: %s", watch_id, exc)

        self._resolved = resolved
        self._errors = errors

    def _write(self) -> None:
        watches: dict[str, Any] = {}
        for watch_id in self._order:
            if watch_id in self.presets:
                watches[watch_id] = self.presets[watch_id].to_mapping()
            elif watch_id in self.raw_invalid:
                watches[watch_id] = self.raw_invalid[watch_id]

        document = {"version": VERSION, "selected": self.selected, "watches": watches}
        text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        _atomic_write(self.path, text.encode("utf-8"))
        self._remember_mtime()
        self._pending = False
        self._pending_mtime_ns = None
        self._pending_since = None

    def _remember_mtime(self) -> None:
        try:
            self._observed_mtime_ns = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._observed_mtime_ns = None


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a temporary file, so a reader never sees a half-written store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
