"""Watch presets: canonical ids, earliest parsing, auto values and the store.

The static feed comes from tests/conftest.py (tests/gtfs_sample.py): line 15
serves S1 and S3, line 1 serves S4 only. Auto-value tests build tiny synthetic
feeds where the geometry has a known answer.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace

import pytest

from segui_bus.domain import Route, ScheduledStop, Trip
from segui_bus.presets import (
    AUTO,
    Preset,
    PresetError,
    PresetStore,
    canonical_id,
    compute_auto_values,
    parse_earliest,
    resolve_preset,
    resolve_watch,
)

# --- canonical ids ---------------------------------------------------------


def test_canonical_id_lowercases_and_keeps_allowed_characters():
    assert canonical_id("15_Marconi") == "15_marconi"
    assert canonical_id("  E17-north  ") == "e17-north"
    assert canonical_id("a_b-9") == "a_b-9"


@pytest.mark.parametrize("text", ["", "   ", "a b", "a.b", "à", "inline", "MONITOR"])
def test_canonical_id_rejects_bad_or_reserved_ids(text):
    with pytest.raises(ValueError):
        canonical_id(text)


# --- earliest --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7", "07:00:00"),
        ("07", "07:00:00"),
        ("730", "07:30:00"),
        ("0730", "07:30:00"),
        ("73000", "07:30:00"),
        ("073000", "07:30:00"),
        ("7:30", "07:30:00"),
        ("07:30", "07:30:00"),
        ("07:30:00", "07:30:00"),
        ("25:00:00", "25:00:00"),
        ("2500", "25:00:00"),
    ],
)
def test_parse_earliest_accepts_every_documented_form(text, expected):
    assert parse_earliest(text) == expected


@pytest.mark.parametrize("text", ["", "24:60", "24:00:60", "abc", "-1", "1:2:3:4", "48:00:00"])
def test_parse_earliest_rejects_invalid_values(text):
    with pytest.raises(ValueError):
        parse_earliest(text)


# --- auto values -----------------------------------------------------------


def test_auto_values_for_line_15_come_from_the_scheduled_segments(static):
    auto = compute_auto_values(static, "15", lead_s=300)

    # Median scheduled speed is below the 10 km/h floor.
    assert auto.assumed_speed_kmh == pytest.approx(10.0)
    # 300 s at 10 km/h over ~977 m stops to one stop.
    assert auto.stops_n == 1
    assert auto.route_detour_factor == 1.0
    assert auto.position_mismatch_m == pytest.approx(3 * 976.7629802144792)
    assert auto.strategy == "nearest"


def test_auto_values_fall_back_to_30_without_usable_segments(static):
    # Line 1 has a single stop, so it has no scheduled segments at all.
    auto = compute_auto_values(static, "1", lead_s=300)

    assert auto.assumed_speed_kmh == pytest.approx(30.0)
    assert auto.stops_n == 1
    assert auto.position_mismatch_m == pytest.approx(2000.0)


def test_auto_values_fall_back_to_55_for_a_lettered_e_line(static):
    feed = replace(
        static,
        routes={**static.routes, "PCE17": Route(route_id="PCE17", line="E17")},
        routes_by_line={**static.routes_by_line, "E17": Route(route_id="PCE17", line="E17")},
    )

    auto = compute_auto_values(feed, "E17", lead_s=300)

    assert auto.assumed_speed_kmh == pytest.approx(55.0)


# --- resolution ------------------------------------------------------------


def _preset(**overrides) -> Preset:
    values = {
        "watch_id": "15_marconi",
        "line": "15",
        "stop": "marconi",
        "lead_minutes": 5,
        "earliest": "07:30:00",
    }
    values.update(overrides)
    return Preset(**values)


def test_resolve_substitutes_auto_and_resolves_the_stop(static):
    resolved = resolve_preset(_preset(), static)

    assert resolved.stop.stop_id == "S1"
    assert resolved.watch.line == "15"
    assert resolved.watch.lead_s == 300
    assert resolved.watch.stops_n == 1
    assert resolved.watch.assumed_speed_kmh == pytest.approx(10.0)
    assert resolved.watch.strategy == "nearest"
    assert resolved.auto.position_mismatch_m == resolved.watch.position_mismatch_m


def test_resolve_keeps_explicit_values(static):
    preset = _preset(
        stops_n=3,
        assumed_speed_kmh=42.0,
        route_detour_factor=1.5,
        position_mismatch_m=2500.0,
        strategy="fence",
        radius_m=200.0,
        on_time_tolerance_s=90,
    )

    watch = resolve_watch(preset, static)

    assert watch.stops_n == 3
    assert watch.assumed_speed_kmh == pytest.approx(42.0)
    assert watch.route_detour_factor == pytest.approx(1.5)
    assert watch.position_mismatch_m == pytest.approx(2500.0)
    assert watch.strategy == "fence"
    assert watch.radius_m == pytest.approx(200.0)
    assert watch.on_time_tolerance_s == 90


def test_resolve_rejects_unknown_line_or_stop(static):
    with pytest.raises(PresetError):
        resolve_preset(_preset(line="99"), static)
    with pytest.raises(PresetError):
        resolve_preset(_preset(stop="crocetta"), static)


def test_resolve_rejects_an_ambiguous_stop(static):
    extra = ScheduledStop(stop_id="S2", stop_sequence=2, arrival_time="20:00:00")
    feed = replace(
        static,
        trips={
            **static.trips,
            "PC15-9": Trip(trip_id="PC15-9", route_id="PC15", direction_id=0),
        },
        trip_stops={
            **static.trip_stops,
            "PC15-9": (ScheduledStop("S1", 1, "19:50:00"), extra),
        },
    )

    with pytest.raises(PresetError, match="ambiguous"):
        resolve_preset(_preset(), feed)


# --- loading config.json ---------------------------------------------------


def _configured_watch(**overrides) -> dict[str, object]:
    watch = {
        "line": "15",
        "stop": "marconi",
        "radius_m": 150,
        "stops_n": "auto",
        "lead_minutes": 5,
        "earliest": "07:30:00",
        "on_time_tolerance_s": 240,
        "assumed_speed_kmh": "auto",
        "route_detour_factor": "auto",
        "position_mismatch_m": "auto",
        "strategy": "auto",
    }
    watch.update(overrides)
    return watch


def _document(**overrides) -> dict[str, object]:
    document: dict[str, object] = {
        "version": 1,
        "selected": "15_marconi",
        "watches": {"15_marconi": _configured_watch()},
    }
    document.update(overrides)
    return document


def _write(path, document: object) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def test_missing_file_is_an_empty_store(tmp_path, static):
    store = PresetStore(tmp_path / "config.json", static)

    assert store.selected is None
    assert store.ids() == ()
    assert store.get("anything") is None


def test_loads_a_full_document_and_exposes_the_resolved_watch(tmp_path, static):
    path = tmp_path / "config.json"
    _write(path, _document())

    store = PresetStore(path, static)

    preset = store.get("15_marconi")
    assert preset is not None
    assert preset.watch_id == "15_marconi"
    assert store.selected == "15_marconi"
    assert store.is_valid("15_marconi")
    assert store.error("15_marconi") is None
    assert store.watch("15_marconi").stop.stop_id == "S1"
    assert store.resolved("15_marconi").auto.stops_n == 1


def test_optional_keys_get_the_documented_defaults(tmp_path, static):
    path = tmp_path / "config.json"
    _write(
        path,
        {
            "version": 1,
            "watches": {
                "15_marconi": {
                    "line": "15",
                    "stop": "marconi",
                    "lead_minutes": 5,
                    "earliest": "07:30",
                }
            },
        },
    )

    store = PresetStore(path, static)
    preset = store.get("15_marconi")

    assert store.selected is None  # selected is optional
    assert preset.radius_m == pytest.approx(150.0)
    assert preset.stops_n == AUTO
    assert preset.on_time_tolerance_s == 240
    assert preset.earliest == "07:30:00"  # canonicalised on load


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"version": 2, "watches": {}},
        {"version": 1},
        {"version": 1, "watches": []},
        {"version": 1, "watches": {}, "extra": True},
        {"version": 1, "selected": "missing", "watches": {}},
        {"version": 1, "selected": 7, "watches": {}},
    ],
)
def test_file_level_schema_errors_fail_fast(tmp_path, static, document):
    path = tmp_path / "config.json"
    _write(path, document)

    with pytest.raises(PresetError):
        PresetStore(path, static)


def test_invalid_json_fails_fast(tmp_path, static):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(PresetError, match="invalid JSON"):
        PresetStore(path, static)


def test_one_bad_watch_is_disabled_but_the_others_still_load(tmp_path, static):
    path = tmp_path / "config.json"
    _write(
        path,
        {
            "version": 1,
            "selected": "15_marconi",
            "watches": {
                "15_marconi": _configured_watch(),
                "bad": _configured_watch(radius_m="150"),
            },
        },
    )

    store = PresetStore(path, static)

    assert store.ids() == ("15_marconi", "bad")
    assert store.get("bad") is None
    assert not store.is_valid("bad")
    assert "radius_m" in store.error("bad")
    assert store.is_valid("15_marconi")


def test_an_unresolvable_watch_is_kept_but_disabled_with_a_warning(tmp_path, static, caplog):
    path = tmp_path / "config.json"
    _write(
        path,
        {
            "version": 1,
            "watches": {"gone": _configured_watch(line="99")},
        },
    )

    with caplog.at_level(logging.WARNING):
        store = PresetStore(path, static)

    assert store.get("GONE").line == "99"  # case-insensitive lookup, still stored
    assert not store.is_valid("gone")
    assert "unknown line" in store.error("gone")
    assert any("disabled" in record.message for record in caplog.records)


def test_selected_may_point_at_a_disabled_watch(tmp_path, static):
    path = tmp_path / "config.json"
    _write(
        path,
        {
            "version": 1,
            "selected": "broken",
            "watches": {"broken": {"line": "15"}},
        },
    )

    store = PresetStore(path, static)

    assert store.selected == "broken"
    assert store.resolved("broken") is None


def test_a_save_preserves_invalid_raw_entries_and_auto_sentinels(tmp_path, static):
    path = tmp_path / "config.json"
    broken = {"line": "15", "stop": "marconi", "lead_minutes": 5, "earliest": "07:30", "bogus": 1}
    _write(
        path,
        {
            "version": 1,
            "selected": None,
            "watches": {"15_marconi": _configured_watch(), "broken": broken},
        },
    )
    store = PresetStore(path, static)

    store.select("15_marconi")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["selected"] == "15_marconi"
    assert saved["watches"]["broken"] == broken
    assert saved["watches"]["15_marconi"]["stops_n"] == "auto"
    assert saved["watches"]["15_marconi"]["assumed_speed_kmh"] == "auto"
    assert "position_mismatch_m" in saved["watches"]["15_marconi"]
    assert not (tmp_path / "config.json.tmp").exists()


# --- mutations -------------------------------------------------------------


def _new_store(tmp_path, static) -> PresetStore:
    return PresetStore(tmp_path / "config.json", static)


def _add_values(**overrides) -> dict[str, object]:
    values: dict[str, object] = {
        "line": "15",
        "stop": "marconi",
        "lead_minutes": 5,
        "earliest": "07:30",
    }
    values.update(overrides)
    return values


def test_add_creates_a_preset_canonicalises_the_id_and_writes_through(tmp_path, static):
    store = _new_store(tmp_path, static)

    preset = store.add("15_MARCONI", _add_values())

    assert preset.watch_id == "15_marconi"
    assert store.ids() == ("15_marconi",)
    assert store.is_valid("15_marconi")
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["watches"]["15_marconi"]["stops_n"] == "auto"
    assert saved["watches"]["15_marconi"]["earliest"] == "07:30:00"


def test_add_accepts_textual_numbers_from_telegram(tmp_path, static):
    store = _new_store(tmp_path, static)

    preset = store.add(
        "a",
        _add_values(radius_m="200", stops_n="3", lead_minutes="10", on_time_tolerance_s="60"),
    )

    assert preset.radius_m == pytest.approx(200.0)
    assert preset.stops_n == 3
    assert preset.lead_minutes == 10
    assert preset.on_time_tolerance_s == 60


def test_add_rejects_a_duplicate_case_insensitively(tmp_path, static):
    store = _new_store(tmp_path, static)
    store.add("15_marconi", _add_values())

    with pytest.raises(PresetError, match="already exists"):
        store.add("15_MARCONI", _add_values())


@pytest.mark.parametrize("watch_id", ["", "a b", "inline", "MONITOR"])
def test_add_rejects_bad_or_reserved_ids(tmp_path, static, watch_id):
    store = _new_store(tmp_path, static)

    with pytest.raises(PresetError):
        store.add(watch_id, _add_values())


def test_add_rejects_missing_required_keys(tmp_path, static):
    store = _new_store(tmp_path, static)

    with pytest.raises(PresetError, match="missing required"):
        store.add("a", {"line": "15", "stop": "marconi"})


def test_add_rejects_an_unresolvable_watch_without_touching_the_file(tmp_path, static):
    store = _new_store(tmp_path, static)

    with pytest.raises(PresetError, match="unknown line"):
        store.add("a", _add_values(line="99"))

    assert store.ids() == ()
    assert not (tmp_path / "config.json").exists()


def test_add_rejects_an_ambiguous_stop(tmp_path, static):
    extra = ScheduledStop(stop_id="S2", stop_sequence=2, arrival_time="20:00:00")
    feed = replace(
        static,
        trips={**static.trips, "PC15-9": Trip(trip_id="PC15-9", route_id="PC15")},
        trip_stops={
            **static.trip_stops,
            "PC15-9": (ScheduledStop("S1", 1, "19:50:00"), extra),
        },
    )
    store = PresetStore(tmp_path / "config.json", feed)

    with pytest.raises(PresetError, match="ambiguous"):
        store.add("a", _add_values())


def test_update_merges_values_and_keeps_auto_fields(tmp_path, static):
    store = _new_store(tmp_path, static)
    store.add("a", _add_values())

    preset = store.update("A", {"stop": "ferrovia", "stops_n": 2})

    assert preset.stop == "ferrovia"
    assert preset.stops_n == 2
    assert preset.earliest == "07:30:00"
    assert store.resolved("a").stop.stop_id == "S3"


def test_update_rejects_unknown_keys_and_bad_values(tmp_path, static):
    store = _new_store(tmp_path, static)
    store.add("a", _add_values())

    with pytest.raises(PresetError, match="unknown key"):
        store.update("a", {"bogus": 1})
    with pytest.raises(PresetError, match="stops_n"):
        store.update("a", {"stops_n": 99})

    assert store.get("a").stops_n == AUTO


def test_update_rejects_an_unknown_id(tmp_path, static):
    store = _new_store(tmp_path, static)

    with pytest.raises(PresetError, match="unknown watch"):
        store.update("nope", {"stops_n": 2})


def test_update_repairs_a_syntactically_invalid_entry(tmp_path, static):
    path = tmp_path / "config.json"
    _write(
        path,
        {"version": 1, "watches": {"broken": {"line": "15", "stop": "marconi", "lead_minutes": 5}}},
    )
    store = PresetStore(path, static)

    preset = store.update("broken", {"earliest": "07:30"})

    assert preset.earliest == "07:30:00"
    assert store.is_valid("broken")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "earliest" in saved["watches"]["broken"]


def test_remove_deletes_the_preset_and_clears_selected(tmp_path, static):
    store = _new_store(tmp_path, static)
    store.add("a", _add_values())
    store.select("a")

    store.remove("A")

    assert store.ids() == ()
    assert store.selected is None
    assert store.get("a") is None
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["watches"] == {}


def test_remove_rejects_an_unknown_id(tmp_path, static):
    store = _new_store(tmp_path, static)

    with pytest.raises(PresetError, match="unknown watch"):
        store.remove("nope")


def test_select_persists_the_followed_watch(tmp_path, static):
    store = _new_store(tmp_path, static)
    store.add("a", _add_values())

    store.select("A")

    assert store.selected == "a"
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["selected"] == "a"


def test_select_rejects_an_unknown_or_disabled_watch(tmp_path, static):
    store = _new_store(tmp_path, static)
    store.add("a", _add_values())

    with pytest.raises(PresetError, match="unknown watch"):
        store.select("nope")

    store.feed = replace(
        static,
        routes={},
        routes_by_line={},
    )
    store.revalidate(store.feed)
    with pytest.raises(PresetError, match="invalid"):
        store.select("a")


# --- hot reload and feed revalidation --------------------------------------


def _clocked_store(tmp_path, static, path=None):
    clock = [0.0]
    store = PresetStore(
        path or tmp_path / "config.json",
        static,
        debounce_s=1.0,
        monotonic=lambda: clock[0],
    )
    return store, clock


def _touch(path, stamp_ns: int) -> None:
    os.utime(path, ns=(stamp_ns, stamp_ns))


def test_refresh_debounces_then_applies_a_changed_file(tmp_path, static):
    path = tmp_path / "config.json"
    _write(path, {"version": 1, "watches": {"a": _add_values()}})
    store, clock = _clocked_store(tmp_path, static, path)

    _write(path, {"version": 1, "watches": {"a": _add_values(), "b": _add_values(stop="ferrovia")}})
    _touch(path, 10_000_000_000)

    assert store.refresh() is False
    assert store.ids() == ("a",)  # not applied yet

    clock[0] = 0.5
    assert store.refresh() is False
    clock[0] = 1.1
    assert store.refresh() is True
    assert store.ids() == ("a", "b")


def test_a_second_change_restarts_the_debounce(tmp_path, static):
    path = tmp_path / "config.json"
    _write(path, {"version": 1, "watches": {}})
    store, clock = _clocked_store(tmp_path, static, path)

    _write(path, {"version": 1, "watches": {"a": _add_values()}})
    _touch(path, 10_000_000_000)
    assert store.refresh() is False

    clock[0] = 0.5
    _write(path, {"version": 1, "watches": {"b": _add_values()}})
    _touch(path, 20_000_000_000)
    assert store.refresh() is False

    clock[0] = 1.2
    assert store.refresh() is False  # pending restarted at 0.5
    clock[0] = 1.6
    assert store.refresh() is True
    assert store.ids() == ("b",)


def test_an_invalid_hot_reload_keeps_the_last_good_store(tmp_path, static, caplog):
    path = tmp_path / "config.json"
    _write(path, {"version": 1, "watches": {"a": _add_values()}})
    store, clock = _clocked_store(tmp_path, static, path)

    path.write_text("{not json", encoding="utf-8")
    _touch(path, 10_000_000_000)
    with caplog.at_level(logging.ERROR):
        assert store.refresh() is False
        clock[0] = 1.5
        assert store.refresh() is False

    assert store.ids() == ("a",)
    assert store.is_valid("a")
    assert any("keeping the last good store" in record.message for record in caplog.records)


def test_a_deleted_file_reloads_as_an_empty_store(tmp_path, static):
    path = tmp_path / "config.json"
    _write(path, {"version": 1, "selected": None, "watches": {"a": _add_values()}})
    store, clock = _clocked_store(tmp_path, static, path)
    assert store.ids() == ("a",)

    path.unlink()
    assert store.refresh() is False
    clock[0] = 1.5
    assert store.refresh() is True
    assert store.ids() == ()
    assert store.selected is None


def test_a_write_through_does_not_reload_itself(tmp_path, static):
    store, clock = _clocked_store(tmp_path, static)
    store.add("a", _add_values())
    clock[0] = 100.0

    assert store.refresh() is False
    assert store.ids() == ("a",)


def test_revalidate_disables_and_reenables_a_watch_after_a_feed_change(tmp_path, static, caplog):
    store, _ = _clocked_store(tmp_path, static)
    store.add("a", _add_values())
    without_routes = replace(static, routes={}, routes_by_line={})

    with caplog.at_level(logging.WARNING):
        store.revalidate(without_routes)

    assert not store.is_valid("a")
    assert "unknown line" in store.error("a")
    assert any("disabled" in record.message for record in caplog.records)

    store.revalidate(static)

    assert store.is_valid("a")
    assert store.error("a") is None
