"""Telegram notifier tests.

All HTTP is an in-memory ``httpx.MockTransport`` and sleep is a recording fake,
so retry paths run instantly and never touch the network.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from segui_bus.clock import absolute_epoch, parse_service_date
from segui_bus.config import ATTRIBUTION, read_version
from segui_bus.notify.telegram import (
    CHAT_ID_ENV,
    MAX_MESSAGE_UNITS,
    TOKEN_ENV,
    TelegramNotifier,
    escape_markdown_v2,
    render_alarm,
    utf16_length,
)
from segui_bus.service.watch import PREAVVISO, REAL, AlarmEvent

DATE = "20261028"
NOW = absolute_epoch(parse_service_date(DATE), "19:36:00")
ARRIVAL = absolute_epoch(parse_service_date(DATE), "19:39:00")
FOOTER = f"{ATTRIBUTION}\nsegui_bus {read_version()}"


def event(**overrides) -> AlarmEvent:
    values = {
        "kind": REAL,
        "line": "15",
        "headsign": "Ferrovia",
        "vehicle_id": "5284",
        "trip_id": "PC15-1",
        "service_date": DATE,
        "route_id": "PC15",
        "direction_id": 0,
        "stop_name": "P.le Marconi, 20 (Stazione Fs corsia 1)",
        "stops_to_target": 2,
        "distance_m": 420.0,
        "seconds_from_now": 180,
        "arrival_unix": ARRIVAL,
        "delay_s": 0,
        "eta_source": "realtime",
        "on_time_tolerance_s": 240,
        "stale": False,
        "stale_age_s": 0,
        "snapshot_unix": NOW,
        "malformed": False,
        "arms": ("fence",),
    }
    values.update(overrides)
    return AlarmEvent(**values)


def ok() -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


async def no_sleep(_seconds: float) -> None:
    return None


# --- rendering -------------------------------------------------------------


def test_real_alarm_matches_the_contract_example():
    _, plain = render_alarm(event())

    assert plain == "\n".join(
        [
            "AUTOBUS VICINO · linea 15 → Ferrovia",
            "bus 5284 · 2 fermate · ~3 min (19:39) · 420 m · dato 19:36",
            "",
            ATTRIBUTION,
            f"segui_bus {read_version()}",
        ]
    )


def test_preavviso_schedule_only_uses_the_preavviso_verdict():
    _, plain = render_alarm(event(kind=PREAVVISO, delay_s=None, eta_source="schedule"))

    assert plain.startswith("PREAVVISO · linea 15 → Ferrovia")
    assert "orario programmato" in plain
    assert "in ritardo" not in plain
    assert "in anticipo" not in plain


def test_on_time_realtime_omits_punctuality():
    _, plain = render_alarm(event(delay_s=240))

    assert "orario programmato" not in plain
    assert "in ritardo" not in plain
    assert "in anticipo" not in plain


def test_late_and_early_delays_are_rounded_to_minutes():
    _, late = render_alarm(event(delay_s=600))
    _, early = render_alarm(event(delay_s=-30, on_time_tolerance_s=0))

    assert "in ritardo di 10 min" in late
    assert "in anticipo di 1 min" in early


def test_malformed_ride_replaces_stops_eta_and_punctuality():
    _, plain = render_alarm(
        event(
            headsign=None,
            stops_to_target=None,
            seconds_from_now=None,
            arrival_unix=None,
            delay_s=None,
            eta_source=None,
            malformed=True,
        )
    )

    assert plain == (
        "AUTOBUS VICINO · linea 15\n"
        "bus 5284 · 420 m · corsa non inizializzata · dato 19:36\n"
        f"\n{FOOTER}"
    )


def test_stale_feed_adds_the_stale_line():
    _, plain = render_alarm(event(stale=True, stale_age_s=412))

    assert "dati non aggiornati da 412 s" in plain
    assert "AUTOBUS VICINO" in plain


def test_stop_counts_and_arrival_edge_cases():
    _, at_stop = render_alarm(event(stops_to_target=0))
    _, one_stop = render_alarm(event(stops_to_target=1))
    _, arriving = render_alarm(event(seconds_from_now=-5))

    assert "alla fermata" in at_stop
    assert "1 fermata" in one_stop
    assert "in arrivo" in arriving
    assert "~" not in arriving


def test_unknown_values_are_omitted():
    _, plain = render_alarm(
        event(headsign=None, stops_to_target=None, seconds_from_now=None, distance_m=None)
    )

    assert plain.startswith("AUTOBUS VICINO · linea 15\n")
    assert "→" not in plain
    assert "fermate" not in plain
    assert "fermata" not in plain
    assert " min" not in plain
    assert " m ·" not in plain


def test_markdown_escapes_dynamic_values_and_footer():
    markdown, plain = render_alarm(
        event(line="E17", headsign="Ferrovia (centro)", vehicle_id="5_84")
    )

    assert markdown.startswith("*AUTOBUS VICINO* · linea E17 → Ferrovia \\(centro\\)")
    assert "bus 5\\_84" in markdown
    assert "\\~3 min \\(19:39\\)" in markdown
    assert markdown.endswith(escape_markdown_v2(FOOTER))
    assert "Ferrovia (centro)" in plain
    assert "bus 5_84" in plain
    assert ATTRIBUTION in plain


def test_escaper_covers_all_markdown_v2_specials():
    specials = "_*[]()~`>#+-=|{}.!\\"

    assert escape_markdown_v2(specials) == "".join(f"\\{char}" for char in specials)
    assert escape_markdown_v2("plain text") == "plain text"


def test_long_message_truncates_the_fact_line_before_the_footer():
    _, plain = render_alarm(event(vehicle_id="9" * 5000))

    assert utf16_length(plain) <= MAX_MESSAGE_UNITS
    assert "…" in plain
    assert plain.endswith(FOOTER)
    assert "\n\n" + FOOTER in plain


# --- delivery --------------------------------------------------------------


def test_send_message_posts_a_markdownv2_payload():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = TelegramNotifier("secret-token", "123456", http=http)
            await notifier.send_message("*AUTOBUS VICINO*")

        request = requests[0]
        body = json.loads(request.content)
        assert str(request.url) == "https://api.telegram.org/botsecret-token/sendMessage"
        assert body == {
            "chat_id": "123456",
            "text": "*AUTOBUS VICINO*",
            "parse_mode": "MarkdownV2",
            "link_preview_options": {"is_disabled": True},
        }

    asyncio.run(scenario())


def test_send_renders_the_event():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = TelegramNotifier("secret-token", "123456", http=http)
            await notifier.send(event())

        body = json.loads(requests[0].content)
        assert body["text"].startswith("*AUTOBUS VICINO* · linea 15 → Ferrovia")
        assert escape_markdown_v2(ATTRIBUTION) in body["text"]

    asyncio.run(scenario())


def test_429_uses_retry_after_from_the_body():
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    responses = [
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}}),
        ok(),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = TelegramNotifier("secret-token", "123456", http=http, sleep=sleep)
            await notifier.send_message("hello")

    asyncio.run(scenario())

    assert len(requests) == 2
    assert sleeps == [2.0]


def test_429_falls_back_to_the_retry_after_header():
    sleeps: list[float] = []
    responses = [httpx.Response(429, headers={"Retry-After": "3"}), ok()]

    async def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = TelegramNotifier("secret-token", "123456", http=http, sleep=sleep)
            await notifier.send_message("hello")

    asyncio.run(scenario())

    assert sleeps == [3.0]


def test_5xx_retries_with_exponential_backoff():
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    responses = [
        httpx.Response(500),
        httpx.Response(502),
        httpx.Response(503),
        ok(),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = TelegramNotifier("secret-token", "123456", http=http, sleep=sleep)
            await notifier.send_message("hello")

    asyncio.run(scenario())

    assert len(requests) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_ok_false_is_retried_as_a_transient_failure():
    sleeps: list[float] = []
    responses = [httpx.Response(200, json={"ok": False}) for _ in range(4)]

    async def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = TelegramNotifier("secret-token", "123456", http=http, sleep=sleep)
            await notifier.send_message("hello")

    asyncio.run(scenario())

    assert sleeps == [1.0, 2.0, 4.0]


def test_network_errors_are_retried_and_never_raise():
    calls = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise httpx.ConnectError("boom", request=request)
        return ok()

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = TelegramNotifier("secret-token", "123456", http=http, sleep=sleep)
            await notifier.send_message("hello")

    asyncio.run(scenario())

    assert calls == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_gives_up_without_raising_after_the_last_retry():
    calls = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = TelegramNotifier("secret-token", "123456", http=http, sleep=sleep)
            await notifier.send_message("hello")

    asyncio.run(scenario())

    assert calls == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_parse_error_resends_plain_text_without_parse_mode():
    requests: list[httpx.Request] = []
    responses = [
        httpx.Response(400, json={"ok": False, "description": "can't parse entities"}),
        ok(),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = TelegramNotifier("secret-token", "123456", http=http)
            await notifier.send_message("escaped \\(text\\)", plain_text="escaped (text)")

    asyncio.run(scenario())

    first = json.loads(requests[0].content)
    second = json.loads(requests[1].content)
    assert first["parse_mode"] == "MarkdownV2"
    assert "parse_mode" not in second
    assert second["text"] == "escaped (text)"


def test_401_is_not_retried_and_the_token_is_not_logged(caplog):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = TelegramNotifier("secret-token", "123456", http=http)
            await notifier.send_message("hello")

    caplog.set_level(logging.WARNING, logger="segui_bus.notify.telegram")
    asyncio.run(scenario())

    assert calls == 1
    assert "secret-token" not in caplog.text


# --- configuration ---------------------------------------------------------


def test_from_env_requires_both_values(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(CHAT_ID_ENV, raising=False)

    with pytest.raises(RuntimeError, match=TOKEN_ENV):
        TelegramNotifier.from_env()


def test_from_env_reports_the_missing_chat_id(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "secret-token")
    monkeypatch.delenv(CHAT_ID_ENV, raising=False)

    with pytest.raises(RuntimeError, match=CHAT_ID_ENV):
        TelegramNotifier.from_env()


def test_from_env_builds_a_notifier(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "secret-token")
    monkeypatch.setenv(CHAT_ID_ENV, "123456")

    assert isinstance(TelegramNotifier.from_env(), TelegramNotifier)
