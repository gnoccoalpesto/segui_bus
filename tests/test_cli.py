"""CLI tests: argument parsing, exit codes and dispatch, with live data faked.

No network: ``fetch_live`` is replaced by a snapshot built from the sample feed;
the long-running service is replaced at its ``_serve`` seam.
"""

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime

import pytest

import segui_bus.__main__ as cli
from segui_bus.clock import TIMEZONE, absolute_epoch, parse_service_date
from segui_bus.config import (
    calendar_warning,
    next_local_midnight,
)
from segui_bus.config import (
    load_settings as real_load_settings,
)
from segui_bus.domain import (
    GeoPoint,
    TripRef,
    TripUpdateSnapshot,
    Vehicle,
    VehiclePosition,
    VehicleSnapshot,
)
from segui_bus.live import LiveData
from segui_bus.presets import PresetStore
from segui_bus.service.watch import WatchConfig
from segui_bus.static_gtfs import StaticFeedUnavailable

DATE = "20261028"
NOW = absolute_epoch(parse_service_date(DATE), "19:40:00")
S1 = GeoPoint(45.052069, 9.705346)


def _vehicle(vehicle_id: str, trip_id: str, sequence: int) -> VehiclePosition:
    return VehiclePosition(
        vehicle=Vehicle(vehicle_id=vehicle_id, label=vehicle_id),
        timestamp=NOW,
        trip=TripRef(trip_id=trip_id, route_id="PC15", start_date=DATE, start_time="19:32:00"),
        position=S1,
        current_stop_sequence=sequence,
        current_status="IN_TRANSIT_TO",
    )


@pytest.fixture
def live(static) -> LiveData:
    vehicles = VehicleSnapshot(
        feed_timestamp=NOW,
        received_at=NOW,
        vehicles=(_vehicle("5284", "PC15-1", 1), _vehicle("6000", "PC15-2", 1)),
    )
    updates = TripUpdateSnapshot(feed_timestamp=NOW, received_at=NOW, trip_updates=())
    return LiveData(feed=static, vehicles=vehicles, updates=updates, now=NOW)


@pytest.fixture(autouse=True)
def fake_live(monkeypatch, live):
    monkeypatch.setattr(cli, "load_settings", lambda: None)
    monkeypatch.setattr(cli, "fetch_live", lambda settings: live)


def test_line_mode(capsys):
    assert cli.main(["line", "15"]) == 0

    out = capsys.readouterr().out
    assert "Line 15 (route PC15)" in out
    assert "5284" in out and "6000" in out


def test_unknown_line_exits_with_two(capsys):
    assert cli.main(["line", "99"]) == 2

    assert "unknown line: 99" in capsys.readouterr().out


def test_stop_mode_resolves_a_name(capsys):
    assert cli.main(["stop", "15", "marconi"]) == 0

    out = capsys.readouterr().out
    assert "stop S1" in out
    assert "strategy: fence" in out


def test_stop_mode_rejects_a_stop_the_line_does_not_serve(capsys):
    assert cli.main(["stop", "15", "crocetta"]) == 2

    assert "serves no stop" in capsys.readouterr().out


def test_stop_mode_lists_ambiguous_matches(capsys):
    # Both sample stops of line 15 contain an "s" ("Stazione", "corsia").
    assert cli.main(["stop", "15", "s"]) == 0

    out = capsys.readouterr().out
    assert "2 stops match" in out
    assert "using S3" in out


def test_vehicle_mode(capsys):
    assert cli.main(["vehicle", "5284"]) == 0

    out = capsys.readouterr().out
    assert "Vehicle 5284" in out
    assert "scheduled" in out


def test_vehicle_mode_with_a_stop(capsys):
    assert cli.main(["vehicle", "5284", "--stop", "marconi"]) == 0

    out = capsys.readouterr().out
    assert "Vehicle 5284" in out
    assert "S1" in out


def test_vehicle_mode_rejects_an_unknown_stop(capsys):
    assert cli.main(["vehicle", "5284", "--stop", "crocetta"]) == 2

    assert "serves no stop" in capsys.readouterr().out


def test_unknown_vehicle_exits_with_two(capsys):
    assert cli.main(["vehicle", "nope"]) == 2

    assert "not reporting" in capsys.readouterr().out


def test_radius_is_passed_through(capsys):
    assert cli.main(["stop", "15", "marconi", "--radius", "5"]) == 0

    # The header does not repeat the radius, but the run must still succeed.
    assert "Line 15" in capsys.readouterr().out


# --- long-running service --------------------------------------------------

WATCH_VALUES = {
    "line": "15",
    "stop": "marconi",
    "lead_minutes": 5,
    "earliest": "07:30",
}


def _service_settings(tmp_path, **overrides):
    base = real_load_settings()
    values = {
        "home": tmp_path,
        "config_path": tmp_path / "config.json",
        "cache_dir": tmp_path / "cache",
        "telegram_token": "token",
        "telegram_chat_id": "123456",
    }
    values.update(overrides)
    return replace(base, **values)


def _write_config(path, selected, watches):
    path.write_text(
        json.dumps({"version": 1, "selected": selected, "watches": watches}),
        encoding="utf-8",
    )


class _FakeCommands:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def run(self):
        return None

    async def close(self):
        return None


class _FakeTransport:
    def __init__(self, _settings, *, on_update=None, on_health=None):
        self.vehicle_positions = None
        self.trip_updates = None

    async def run(self):
        return None

    async def close(self):
        return None


class _FakeNotifier:
    async def send(self, _event):
        return None


def _capture_serve(monkeypatch):
    calls = []

    async def serve(runtime, commands):
        calls.append((runtime, commands))
        return 0

    monkeypatch.setattr(cli, "_serve", serve)
    return calls


def _service_env(monkeypatch, settings, static, store=None):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_static", lambda _settings: static)
    monkeypatch.setattr(cli, "PresetCommands", _FakeCommands)
    if store is not None:
        monkeypatch.setattr(cli, "PresetStore", lambda _path, _feed: store)
    return _capture_serve(monkeypatch)


def test_watch_parsing():
    args = cli._parser().parse_args(["watch", "--preset", "15_marconi", "--config", "x.json"])

    assert args.command == "watch"
    assert args.preset == "15_marconi"
    assert args.config == "x.json"


def test_monitor_parsing_defaults_and_overrides():
    args = cli._parser().parse_args(
        [
            "monitor",
            "--line",
            "15",
            "--stop",
            "marconi",
            "--lead-minutes",
            "5",
            "--earliest",
            "07:30",
            "--stops",
            "3",
            "--strategy",
            "schedule",
        ]
    )

    assert args.command == "monitor"
    assert args.line == "15"
    assert args.stop == "marconi"
    assert args.lead_minutes == 5
    assert args.earliest == "07:30"
    assert args.radius == 150.0
    assert args.stops == 3
    assert args.strategy == "schedule"


def test_monitor_stops_auto_and_range():
    args = cli._parser().parse_args(
        ["monitor", "--line", "15", "--stop", "marconi", "--lead-minutes", "5", "--earliest", "07:30"]
    )
    assert args.stops == "auto"

    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            [
                "monitor",
                "--line",
                "15",
                "--stop",
                "marconi",
                "--lead-minutes",
                "5",
                "--earliest",
                "07:30",
                "--stops",
                "11",
            ]
        )


def test_monitor_requires_the_watch_flags():
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["monitor", "--line", "15"])


def test_watch_dispatches_to_the_service(tmp_path, monkeypatch, static):
    settings = _service_settings(tmp_path)
    store = PresetStore(tmp_path / "config.json", static)
    calls = _service_env(monkeypatch, settings, static, store)

    assert cli.main(["watch"]) == 0

    runtime, commands = calls[0]
    assert runtime.store is store
    assert runtime.engine is None
    assert runtime.followed_id is None
    assert isinstance(commands, _FakeCommands)


def test_watch_follows_the_selected_preset(tmp_path, monkeypatch, static):
    settings = _service_settings(tmp_path)
    _write_config(settings.config_path, "15_marconi", {"15_marconi": WATCH_VALUES})
    store = PresetStore(settings.config_path, static)
    calls = _service_env(monkeypatch, settings, static, store)

    assert cli.main(["watch"]) == 0

    runtime, _commands = calls[0]
    assert runtime.followed_id == "15_marconi"
    assert isinstance(runtime.engine.watch, WatchConfig)


def test_watch_preset_override_does_not_require_selection(tmp_path, monkeypatch, static):
    settings = _service_settings(tmp_path)
    _write_config(settings.config_path, None, {"15_marconi": WATCH_VALUES})
    store = PresetStore(settings.config_path, static)
    calls = _service_env(monkeypatch, settings, static, store)

    assert cli.main(["watch", "--preset", "15_marconi"]) == 0

    runtime, _commands = calls[0]
    assert runtime.followed_id == "15_marconi"
    assert runtime.startup_preset == "15_marconi"


def test_watch_uses_the_config_override(tmp_path, monkeypatch, static):
    settings = _service_settings(tmp_path)
    custom = tmp_path / "custom.json"
    _write_config(custom, None, {})
    store = PresetStore(custom, static)
    seen = []

    def build(path, feed):
        seen.append(path)
        return store

    calls = _service_env(monkeypatch, settings, static)
    monkeypatch.setattr(cli, "PresetStore", build)

    assert cli.main(["watch", "--config", str(custom)]) == 0
    assert seen == [custom]
    assert calls[0][0].store is store


def test_watch_unknown_preset_exits_two(tmp_path, monkeypatch, static, capsys):
    settings = _service_settings(tmp_path)
    store = PresetStore(tmp_path / "config.json", static)
    calls = _service_env(monkeypatch, settings, static, store)

    assert cli.main(["watch", "--preset", "nope"]) == 2
    assert calls == []
    assert "cannot follow preset" in capsys.readouterr().err


def test_watch_missing_telegram_credentials_exits_two(tmp_path, monkeypatch, static, capsys):
    settings = _service_settings(tmp_path, telegram_token=None)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_static", pytest.fail)

    assert cli.main(["watch"]) == 2
    assert "SEGUI_BUS_TELEGRAM_TOKEN" in capsys.readouterr().err


def test_monitor_missing_telegram_credentials_exits_two(tmp_path, monkeypatch, static, capsys):
    settings = _service_settings(tmp_path, telegram_chat_id=None)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_static", pytest.fail)

    assert cli.main(["monitor", "--line", "15", "--stop", "marconi", "--lead-minutes", "5",
                     "--earliest", "07:30"]) == 2
    assert "SEGUI_BUS_TELEGRAM_CHAT_ID" in capsys.readouterr().err


def test_monitor_dispatches_without_a_store_or_commands(tmp_path, monkeypatch, static):
    settings = _service_settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_static", lambda _settings: static)
    monkeypatch.setattr(cli, "PresetStore", pytest.fail)
    monkeypatch.setattr(cli, "PresetCommands", pytest.fail)
    calls = _capture_serve(monkeypatch)

    assert cli.main(
        [
            "monitor",
            "--line",
            "15",
            "--stop",
            "marconi",
            "--lead-minutes",
            "5",
            "--earliest",
            "07:30",
        ]
    ) == 0

    runtime, commands = calls[0]
    assert runtime.store is None
    assert runtime.followed_id == "monitor"
    assert isinstance(runtime.engine.watch, WatchConfig)
    assert commands is None


def test_monitor_unknown_line_exits_two(tmp_path, monkeypatch, static, capsys):
    settings = _service_settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_static", lambda _settings: static)
    calls = _capture_serve(monkeypatch)

    assert cli.main(
        ["monitor", "--line", "99", "--stop", "marconi", "--lead-minutes", "5",
         "--earliest", "07:30"]
    ) == 2
    assert calls == []
    assert "unknown line" in capsys.readouterr().err


def test_monitor_unknown_stop_exits_two(tmp_path, monkeypatch, static, capsys):
    settings = _service_settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_static", lambda _settings: static)
    calls = _capture_serve(monkeypatch)

    assert cli.main(
        ["monitor", "--line", "15", "--stop", "crocetta", "--lead-minutes", "5",
         "--earliest", "07:30"]
    ) == 2
    assert calls == []
    assert "not served" in capsys.readouterr().err


def test_load_settings_reads_the_home_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("SEGUI_BUS_HOME", str(tmp_path))
    monkeypatch.setenv("SEGUI_BUS_LOG_LEVEL", "debug")
    monkeypatch.setenv("SEGUI_BUS_TELEGRAM_TOKEN", "token")
    monkeypatch.setenv("SEGUI_BUS_TELEGRAM_CHAT_ID", "42")

    settings = real_load_settings(env_file=tmp_path / ".env")

    assert settings.home == tmp_path
    assert settings.config_path == tmp_path / "config.json"
    assert settings.cache_dir == tmp_path / ".cache"
    assert settings.log_level == "DEBUG"
    assert settings.telegram_token == "token"
    assert settings.telegram_chat_id == "42"


def test_next_local_midnight_is_dst_aware():
    now = datetime(2026, 11, 9, 23, 0, tzinfo=TIMEZONE).timestamp()
    expected = datetime(2026, 11, 10, 0, 0, tzinfo=TIMEZONE).timestamp()

    assert next_local_midnight(now) == expected


def test_calendar_warning_covers_the_expiry_window(static):
    feed = replace(static, calendar_range=("20261020", "20261223"))

    assert calendar_warning(feed, date(2026, 12, 1)) is None
    assert "ends in 3 days (2026-12-23)" in calendar_warning(feed, date(2026, 12, 20))
    assert "expired on 2026-12-23" in calendar_warning(feed, date(2026, 12, 24))
    assert "fence only" in calendar_warning(feed, date(2026, 12, 24))


def test_serve_shuts_down_when_the_transport_ends(tmp_path, monkeypatch, static):
    settings = _service_settings(tmp_path)
    monkeypatch.setattr(cli, "Transport", _FakeTransport)
    monkeypatch.setattr(cli, "_flush_logs", lambda: None)
    runtime = cli._Runtime(settings=settings, feed=static, notifier=_FakeNotifier())

    assert asyncio.run(cli._serve(runtime, None)) == 0


def test_watch_malformed_config_exits_one(tmp_path, monkeypatch, static, capsys):
    settings = _service_settings(tmp_path)
    settings.config_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_static", lambda _settings: static)
    calls = _capture_serve(monkeypatch)

    assert cli.main(["watch"]) == 1
    assert calls == []
    assert "cannot load preset store" in capsys.readouterr().err


def test_service_static_feed_unavailable_exits_one(tmp_path, monkeypatch, capsys):
    settings = _service_settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "load_static",
        lambda _settings: (_ for _ in ()).throw(StaticFeedUnavailable("offline")),
    )

    assert cli.main(["watch"]) == 1
    assert "static GTFS unavailable" in capsys.readouterr().err
