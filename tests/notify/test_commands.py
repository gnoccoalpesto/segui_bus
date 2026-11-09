"""Inbound Telegram preset-command tests.

The Telegram API is an in-memory ``httpx.MockTransport`` and sleep is a
recording fake, so polling, retries and replies run instantly and offline.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from segui_bus.notify.commands import (
    ADD_TEMPLATE,
    HELP_TEXT,
    PresetCommands,
    WatchChange,
    format_effective,
)
from segui_bus.notify.telegram import CHAT_ID_ENV, TOKEN_ENV
from segui_bus.presets import PresetStore

CHAT = "123456"
OTHER_CHAT = "999999"
TOKEN = "secret-token"


def _values(**overrides):
    values = {
        "line": "15",
        "stop": "marconi",
        "lead_minutes": 5,
        "earliest": "07:30",
    }
    values.update(overrides)
    return values


def _store(tmp_path, static) -> PresetStore:
    return PresetStore(tmp_path / "config.json", static)


def _commands(store, changes=None, **kwargs) -> PresetCommands:
    callback = changes.append if changes is not None else None
    return PresetCommands(store, CHAT, token=TOKEN, on_watch_change=callback, **kwargs)


def _update(update_id: int, text: str, chat_id: str = CHAT) -> dict:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": chat_id}, "text": text},
    }


async def _no_sleep(_seconds: float) -> None:
    return None


class FakeTelegram:
    """One queued ``getUpdates`` batch per call; sendMessage always succeeds."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.requests: list[httpx.Request] = []
        self.sent: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            batch = self.batches.pop(0)
            return httpx.Response(200, json={"ok": True, "result": batch})
        if method == "sendMessage":
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


# --- templates and formatting ---------------------------------------------


def test_add_template_is_the_locked_config_shape():
    assert ADD_TEMPLATE == (
        "line=\n"
        "stop=\n"
        "radius_m=150\n"
        "stops_n=auto\n"
        "lead_minutes=\n"
        "earliest=\n"
        "on_time_tolerance_s=240\n"
        "assumed_speed_kmh=auto\n"
        "route_detour_factor=auto\n"
        "position_mismatch_m=auto\n"
        "strategy=auto"
    )


def test_effective_values_keep_auto_and_show_computed_markers(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())

    text = format_effective(store.get("15_marconi"), store.resolved("15_marconi"))

    assert "stops_n=auto  # 1" in text
    assert "assumed_speed_kmh=auto  # 10" in text
    assert "route_detour_factor=auto  # 1" in text
    assert "strategy=auto  # nearest" in text
    assert "radius_m=150" in text
    assert "earliest=07:30:00" in text


def test_effective_values_quote_stop_names_with_spaces(tmp_path, static):
    store = _store(tmp_path, static)
    name = "P.le Marconi, 20 (Stazione Fs corsia 1)"
    store.add("15_marconi", _values(stop=name))

    text = format_effective(store.get("15_marconi"), store.resolved("15_marconi"))

    assert f'stop="{name}"' in text


# --- help and list ---------------------------------------------------------


def test_help_lists_the_allowed_commands(tmp_path, static):
    commands = _commands(_store(tmp_path, static))

    assert commands.handle_text("/help") == HELP_TEXT
    assert "/watch_<id>" in HELP_TEXT
    assert "/remove <id>" in HELP_TEXT


def test_list_marks_the_followed_watch_and_the_invalid_entries(tmp_path, static):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "selected": "15_marconi",
                "watches": {
                    "15_marconi": _values(),
                    "broken": {"line": "15"},
                },
            }
        ),
        encoding="utf-8",
    )
    store = PresetStore(path, static)
    commands = _commands(store)

    reply = commands.handle_text("/list")

    assert "* 15_marconi" in reply
    assert "broken  (non valido:" in reply


def test_list_on_an_empty_store(tmp_path, static):
    assert _commands(_store(tmp_path, static)).handle_text("/LIST") == "nessun preset"


# --- /add ------------------------------------------------------------------


def test_add_without_arguments_returns_the_locked_template(tmp_path, static):
    commands = _commands(_store(tmp_path, static))

    assert commands.handle_text("/add") == ADD_TEMPLATE
    assert commands.handle_text("/add # anything after the marker is ignored") == ADD_TEMPLATE


def test_add_creates_the_preset_canonicalises_the_id_and_writes_through(tmp_path, static):
    store = _store(tmp_path, static)
    commands = _commands(store)

    reply = commands.handle_text(
        "/add 15_MARCONI line=15 stop=marconi lead_minutes=5 earliest=07:30"
    )

    assert reply == "preset aggiunto: 15_marconi"
    assert store.get("15_marconi") is not None
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["watches"]["15_marconi"]["earliest"] == "07:30:00"


def test_add_accepts_quoted_values_with_spaces_and_ignores_comments(tmp_path, static):
    store = _store(tmp_path, static)
    commands = _commands(store)
    name = "P.le Marconi, 20 (Stazione Fs corsia 1)"

    reply = commands.handle_text(
        f'/add marconi line=15 stop="{name}" lead_minutes=5 earliest=07:30 # user note'
    )

    assert reply.startswith("preset aggiunto:")
    assert store.get("marconi").stop == name
    assert store.resolved("marconi").stop.stop_id == "S1"


def test_add_reports_a_reason_and_the_template_without_mutating(tmp_path, static):
    store = _store(tmp_path, static)
    commands = _commands(store)

    reply = commands.handle_text("/add a line=15 stop=marconi")

    assert reply.startswith("errore:")
    assert "missing required" in reply
    assert reply.endswith(ADD_TEMPLATE)
    assert store.ids() == ()

    unknown = commands.handle_text(
        "/add b line=99 stop=marconi lead_minutes=5 earliest=07:30"
    )
    assert "unknown line" in unknown
    assert store.ids() == ()


def test_add_rejects_unclosed_quotes_with_an_explanation(tmp_path, static):
    store = _store(tmp_path, static)
    commands = _commands(store)

    reply = commands.handle_text('/add a line=15 stop="marconi lead_minutes=5 earliest=07:30')

    assert reply.startswith("errore:")
    assert "virgolette" in reply
    assert store.ids() == ()


# --- /watch ----------------------------------------------------------------


def test_watch_selects_persists_and_resets_the_followed_watch(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())
    changes = []
    commands = _commands(store, changes)

    reply = commands.handle_text("/WATCH 15_MARCONI")

    assert store.selected == "15_marconi"
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["selected"] == (
        "15_marconi"
    )
    assert reply.startswith("line=15\n")
    assert "stops_n=auto  # 1" in reply
    assert changes == [WatchChange("15_marconi", store.watch("15_marconi"), True)]


def test_watch_inline_form_works_too(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())
    changes = []
    commands = _commands(store, changes)

    reply = commands.handle_text("/watch_15_Marconi")

    assert store.selected == "15_marconi"
    assert reply.startswith("line=15\n")
    assert changes[0].reset is True


def test_watch_without_an_id_replies_help(tmp_path, static):
    commands = _commands(_store(tmp_path, static))

    assert commands.handle_text("/watch") == HELP_TEXT
    assert commands.handle_text("/watch_").startswith("errore:")


def test_watch_rejects_unknown_or_invalid_ids(tmp_path, static):
    store = _store(tmp_path, static)
    commands = _commands(store)

    reply = commands.handle_text("/watch nope")

    assert reply.startswith("errore:")
    assert "unknown watch" in reply
    assert store.selected is None


# --- /set ------------------------------------------------------------------


def test_set_threshold_only_keeps_the_alarm_state(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())
    store.select("15_marconi")
    changes = []
    commands = _commands(store, changes)

    reply = commands.handle_text("/set 15_marconi stops_n=3 radius_m=200")

    assert store.get("15_marconi").stops_n == 3
    assert reply.startswith("line=15\n")
    assert "stops_n=3" in reply
    assert len(changes) == 1
    assert changes[0].watch_id == "15_marconi"
    assert changes[0].reset is False
    assert changes[0].watch.stops_n == 3


def test_set_line_change_resets_the_followed_watch(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())
    store.select("15_marconi")
    changes = []
    commands = _commands(store, changes)

    reply = commands.handle_text("/set 15_marconi line=1 stop=crocetta")

    assert reply.startswith("line=1\n")
    assert store.resolved("15_marconi").stop.stop_id == "S4"
    assert len(changes) == 1
    assert changes[0].reset is True


def test_set_stop_change_resets_the_followed_watch(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())
    store.select("15_marconi")
    changes = []
    commands = _commands(store, changes)

    commands.handle_text("/set 15_marconi stop=ferrovia")

    assert store.resolved("15_marconi").stop.stop_id == "S3"
    assert changes[0].reset is True


def test_set_same_stop_under_a_different_spelling_does_not_reset(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())
    store.select("15_marconi")
    changes = []
    commands = _commands(store, changes)

    commands.handle_text("/set 15_marconi stop=MARCONI")

    assert changes[0].reset is False


def test_set_on_a_non_followed_watch_does_not_notify(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())
    store.add("other", _values(stop="ferrovia"))
    store.select("other")
    changes = []
    commands = _commands(store, changes)

    commands.handle_text("/set 15_marconi stops_n=2")

    assert store.get("15_marconi").stops_n == 2
    assert changes == []


def test_set_without_pairs_returns_effective_values_and_pastes_back(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())
    commands = _commands(store)

    effective = commands.handle_text("/set 15_marconi")

    assert "stops_n=auto  # 1" in effective
    pasted = commands.handle_text("/set 15_marconi\n" + effective)
    assert pasted.startswith("line=15\n")
    assert "stops_n=auto  # 1" in pasted
    assert store.get("15_marconi").stops_n == "auto"


def test_set_rejects_an_unknown_id_or_bad_key(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())
    commands = _commands(store)

    unknown = commands.handle_text("/set nope stops_n=2")
    bad_key = commands.handle_text("/set 15_marconi bogus=1")

    assert "unknown watch" in unknown
    assert "unknown key" in bad_key
    assert store.get("15_marconi").stops_n == "auto"


# --- /remove ---------------------------------------------------------------


def test_remove_followed_watch_clears_selection_and_resets(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())
    store.select("15_marconi")
    changes = []
    commands = _commands(store, changes)

    reply = commands.handle_text("/remove 15_MARCONI")

    assert reply == "preset rimosso: 15_marconi"
    assert store.selected is None
    assert store.ids() == ()
    assert changes == [WatchChange(None, None, True)]


def test_remove_non_followed_watch_keeps_selection(tmp_path, static):
    store = _store(tmp_path, static)
    store.add("15_marconi", _values())
    store.add("other", _values(stop="ferrovia"))
    store.select("other")
    changes = []
    commands = _commands(store, changes)

    commands.handle_text("/remove 15_marconi")

    assert store.selected == "other"
    assert changes == []


def test_remove_rejects_an_unknown_id(tmp_path, static):
    commands = _commands(_store(tmp_path, static))

    reply = commands.handle_text("/remove nope")

    assert reply.startswith("errore:")
    assert "unknown watch" in reply


# --- bad input -------------------------------------------------------------


def test_unknown_commands_and_plain_text_are_harmless(tmp_path, static):
    commands = _commands(_store(tmp_path, static))

    assert commands.handle_text("ciao") is None
    assert "comando sconosciuto" in commands.handle_text("/bogus")
    assert HELP_TEXT in commands.handle_text("/bogus")


# --- polling and authorisation ---------------------------------------------


def test_polling_advances_the_offset_and_ignores_other_chats(tmp_path, static):
    store = _store(tmp_path, static)
    fake = FakeTelegram(
        [
            [_update(1, "/help"), _update(2, "/help", chat_id=OTHER_CHAT)],
            [_update(3, "/list")],
        ]
    )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
            commands = PresetCommands(store, CHAT, token=TOKEN, http=http, sleep=_no_sleep)
            first = await commands.poll_once()
            second = await commands.poll_once()
        return first, second

    first, second = asyncio.run(scenario())

    assert (first, second) == (2, 1)
    assert len(fake.sent) == 2
    assert fake.sent[0]["chat_id"] == CHAT
    assert fake.sent[0]["text"] == HELP_TEXT
    assert "nessun preset" in fake.sent[1]["text"]

    polls = [
        json.loads(request.content)
        for request in fake.requests
        if request.url.path.endswith("getUpdates")
    ]
    assert [payload["offset"] for payload in polls] == [0, 3]
    assert polls[0]["allowed_updates"] == ["message"]


def test_malformed_updates_and_messages_do_not_crash(tmp_path, static):
    store = _store(tmp_path, static)
    fake = FakeTelegram(
        [
            [
                "not an object",
                {},
                {"update_id": 7},
                {"update_id": 8, "message": {"chat": {"id": CHAT}, "text": None}},
            ]
        ]
    )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
            commands = PresetCommands(store, CHAT, token=TOKEN, http=http, sleep=_no_sleep)
            return await commands.poll_once()

    assert asyncio.run(scenario()) == 3
    assert fake.sent == []


def test_polling_waits_after_429_and_continues(tmp_path, static):
    store = _store(tmp_path, static)
    responses = [
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}}),
        httpx.Response(200, json={"ok": True, "result": []}),
    ]
    sleeps: list[float] = []

    async def handler(_request):
        return responses.pop(0)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            commands = PresetCommands(store, CHAT, token=TOKEN, http=http, sleep=sleep)
            await commands.poll_once()
            await commands.poll_once()

    asyncio.run(scenario())

    assert sleeps == [2.0]


def test_polling_backs_off_after_a_network_error(tmp_path, static):
    store = _store(tmp_path, static)
    calls = 0
    sleeps: list[float] = []

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True, "result": []})

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            commands = PresetCommands(store, CHAT, token=TOKEN, http=http, sleep=sleep)
            await commands.poll_once()
            await commands.poll_once()

    asyncio.run(scenario())

    assert calls == 2
    assert sleeps == [1.0]


def test_polling_backs_off_after_a_5xx(tmp_path, static):
    store = _store(tmp_path, static)
    responses = [
        httpx.Response(503),
        httpx.Response(200, json={"ok": True, "result": []}),
    ]
    sleeps: list[float] = []

    async def handler(_request):
        return responses.pop(0)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            commands = PresetCommands(store, CHAT, token=TOKEN, http=http, sleep=sleep)
            await commands.poll_once()
            await commands.poll_once()

    asyncio.run(scenario())

    assert sleeps == [1.0]


def test_reply_delivery_retries_without_raising(tmp_path, static):
    store = _store(tmp_path, static)
    fake = FakeTelegram([[_update(1, "/help")]])
    send_responses = [httpx.Response(500), httpx.Response(200, json={"ok": True})]
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            return await fake(request)
        return send_responses.pop(0)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            commands = PresetCommands(store, CHAT, token=TOKEN, http=http, sleep=sleep)
            await commands.poll_once()

    asyncio.run(scenario())

    assert sleeps == [1.0]


def test_run_survives_an_unexpected_failure_then_can_be_cancelled(tmp_path, static, caplog):
    store = _store(tmp_path, static)
    commands = _commands(store, sleep=_no_sleep)
    calls = 0

    async def fake_poll():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        raise asyncio.CancelledError

    commands.poll_once = fake_poll

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await commands.run()

    caplog.set_level(logging.ERROR, logger="segui_bus.notify.commands")
    asyncio.run(scenario())

    assert calls == 2
    assert "unexpected Telegram polling failure" in caplog.text


def test_run_is_cancel_safe(tmp_path, static):
    store = _store(tmp_path, static)

    async def handler(_request):
        await asyncio.sleep(0)
        return httpx.Response(200, json={"ok": True, "result": []})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            commands = PresetCommands(store, CHAT, token=TOKEN, http=http, sleep=_no_sleep)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(commands.run(), timeout=0.05)

    asyncio.run(scenario())


# --- configuration ---------------------------------------------------------


def test_from_env_requires_both_values(tmp_path, static, monkeypatch):
    store = _store(tmp_path, static)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(CHAT_ID_ENV, raising=False)

    with pytest.raises(RuntimeError, match=TOKEN_ENV):
        PresetCommands.from_env(store)


def test_from_env_reports_the_missing_chat_id(tmp_path, static, monkeypatch):
    store = _store(tmp_path, static)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.delenv(CHAT_ID_ENV, raising=False)

    with pytest.raises(RuntimeError, match=CHAT_ID_ENV):
        PresetCommands.from_env(store)


def test_from_env_builds_the_service(tmp_path, static, monkeypatch):
    store = _store(tmp_path, static)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setenv(CHAT_ID_ENV, CHAT)

    assert isinstance(PresetCommands.from_env(store), PresetCommands)
