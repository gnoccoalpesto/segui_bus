"""Configuration tests: the version comes from VERSION, headers from Settings."""

from segui_bus.config import ATTRIBUTION, load_settings, read_version


def test_version_is_read_from_the_version_file():
    version = read_version()

    # e.g. "0.1.0" — never duplicated elsewhere (docs/versioning.md).
    assert version.count(".") >= 1


def test_headers_carry_the_runtime_version():
    settings = load_settings()

    assert settings.headers() == {
        "X-BusOne-Client": f"segui_bus/{read_version()} gh/gnoccoalpesto",
        "X-BusOne-Use": "personal",
        "User-Agent": f"segui_bus/{read_version()}",
    }


def test_feed_and_websocket_urls_are_assembled_here():
    settings = load_settings()

    assert settings.feed_url("vehicle-positions") == (
        "https://proxy.busone.app/seta-pc/gtfs-rt/vehicle-positions.pb"
    )
    assert settings.ws_url == "wss://proxy.busone.app/seta-pc/ws"


def test_attribution_line_is_the_mandated_one():
    assert ATTRIBUTION == "BusOne (https://busone.app) e la fonte originale (SETA S.p.A.)"
