"""Inbound Telegram preset commands.

Issue #29: the bot receives operator commands with ``getUpdates`` long polling,
authorises the configured chat, validates mutations through the preset store and
applies them live. The module is mode-agnostic: the wiring in #12 decides
whether the service runs.

A bad command replies with the reason plus syntax and leaves the store unchanged.
Replies are plain text (no parse mode), so values are never escaped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..presets import (
    AUTO,
    PRESET_KEYS,
    Preset,
    PresetError,
    PresetStore,
    ResolvedPreset,
    canonical_id,
)
from ..service.watch import WatchConfig
from .telegram import API_ROOT, CHAT_ID_ENV, TOKEN_ENV

logger = logging.getLogger(__name__)

DEFAULT_POLL_TIMEOUT_S = 30.0
DEFAULT_TIMEOUT_S = 10.0
POLL_LIMIT = 100
RETRY_DELAYS = (1.0, 2.0, 4.0)
DEFAULT_RETRY_AFTER_S = 1.0
MAX_RETRY_AFTER_S = 60.0
MAX_POLL_BACKOFF_S = 30.0
MAX_SEND_RETRIES = 3

ADD_TEMPLATE = (
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

HELP_TEXT = (
    "Comandi:\n"
    "/help — mostra questa guida\n"
    "/list — elenca i preset (* = seguita)\n"
    "/watch <id> — segui un preset\n"
    "/watch_<id> — come /watch <id>\n"
    "/add — mostra il modello da compilare\n"
    "/add <id> chiave=valore ... — crea un preset\n"
    "/set <id> chiave=valore ... — modifica un preset\n"
    "/remove <id> — elimina un preset"
)

Sleep = Callable[[float], Awaitable[None]]
WatchCallback = Callable[["WatchChange"], None]

_TOKEN_IN_URL = re.compile(r"/bot[^/\s]+/")


class CommandError(ValueError):
    """A command's text could not be parsed."""


@dataclass(frozen=True, slots=True)
class WatchChange:
    """A followed-watch change for the service that owns the watch engine.

    ``reset`` is True when the new watch must start with fresh alarm state:
    following a preset, changing its line or resolved stop, or removing it.
    """

    watch_id: str | None
    watch: WatchConfig | None
    reset: bool


class PresetCommands:
    """Long-polling Telegram command surface over one :class:`PresetStore`."""

    def __init__(
        self,
        store: PresetStore,
        chat_id: str,
        *,
        token: str,
        http: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
        on_watch_change: WatchCallback | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not chat_id:
            raise RuntimeError("Telegram chat id is required")
        self._store = store
        self._chat_id = str(chat_id)
        self._token = token
        self._http = http
        self._owns_http = False
        self._sleep = sleep
        self._poll_timeout_s = poll_timeout_s
        self._on_watch_change = on_watch_change
        self._timeout_s = timeout_s
        self._offset = 0
        self._poll_delay = RETRY_DELAYS[0]

    @classmethod
    def from_env(cls, store: PresetStore, **kwargs: object) -> PresetCommands:
        """Build the service from ``SEGUI_BUS_TELEGRAM_*``; fail without leaking secrets."""
        token = os.getenv(TOKEN_ENV)
        chat_id = os.getenv(CHAT_ID_ENV)
        missing = [name for name, value in ((TOKEN_ENV, token), (CHAT_ID_ENV, chat_id)) if not value]
        if missing:
            raise RuntimeError(f"Telegram is not configured: missing {', '.join(missing)}")
        return cls(store, chat_id or "", token=token or "", **kwargs)

    async def close(self) -> None:
        """Close the HTTP client only if this service created it."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
            self._owns_http = False

    # --- polling -----------------------------------------------------------

    async def run(self) -> None:
        """Poll forever; cancel-safe and never dies on a transient failure."""
        logger.info("Telegram preset commands listening for chat %s", self._chat_id)
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("unexpected Telegram polling failure")
                await self._poll_wait()

    async def poll_once(self) -> int:
        """Fetch and process one ``getUpdates`` batch; returns updates handled."""
        payload: dict[str, Any] = {
            "offset": self._offset,
            "timeout": self._poll_timeout_s,
            "allowed_updates": ["message"],
            "limit": POLL_LIMIT,
        }
        try:
            response = await self._client().post(self._url("getUpdates"), json=payload)
        except (httpx.HTTPError, OSError) as exc:
            await self._poll_wait(f"getUpdates network error: {_redact(str(exc))}")
            return 0

        status = response.status_code
        if status == 429:
            logger.warning("Telegram getUpdates was rate-limited")
            await self._sleep(_retry_after(response))
            return 0
        if status >= 500:
            await self._poll_wait(f"getUpdates HTTP {status}")
            return 0
        if status != 200:
            await self._poll_wait(f"getUpdates HTTP {status}: {_description(response)}")
            return 0

        try:
            body = response.json()
        except ValueError:
            logger.error("Telegram getUpdates returned invalid JSON")
            return 0
        if not isinstance(body, dict) or body.get("ok") is not True:
            await self._poll_wait("getUpdates returned ok=false")
            return 0
        result = body.get("result")
        if not isinstance(result, list):
            logger.error("Telegram getUpdates result is not a list")
            return 0

        self._poll_delay = RETRY_DELAYS[0]
        processed = 0
        for update in result:
            if not isinstance(update, Mapping):
                logger.warning("ignoring a non-object Telegram update")
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int) and not isinstance(update_id, bool):
                self._offset = update_id + 1
            await self.handle_update(update)
            processed += 1
        return processed

    async def _poll_wait(self, message: str | None = None) -> None:
        if message is not None:
            logger.warning("%s; retrying in %.0f s", message, self._poll_delay)
        await self._sleep(self._poll_delay)
        self._poll_delay = min(self._poll_delay * 2, MAX_POLL_BACKOFF_S)

    async def handle_update(self, update: Mapping[str, Any]) -> None:
        """Authorise, parse and answer one Telegram update; never raises."""
        try:
            message = update.get("message")
            if not isinstance(message, Mapping):
                return
            chat = message.get("chat")
            chat_id = chat.get("id") if isinstance(chat, Mapping) else None
            if str(chat_id) != self._chat_id:
                logger.info("ignoring a Telegram update from chat %s", chat_id)
                return
            text = message.get("text")
            if not isinstance(text, str):
                return
            reply = self.handle_text(text)
            if reply is not None:
                await self._send(reply)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("unhandled error while processing a Telegram update")

    # --- command dispatch --------------------------------------------------

    def handle_text(self, text: str) -> str | None:
        """Turn one message into a reply; side effects go through the store."""
        if not isinstance(text, str):
            return None
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None

        parts = stripped.split(maxsplit=1)
        token = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        raw_command = token.split("@", 1)[0]
        command = raw_command.casefold()

        try:
            if command == "/help":
                return HELP_TEXT
            if command == "/list":
                return self._list()
            if command == "/watch":
                return self._watch(rest)
            if command.startswith("/watch_"):
                inline_id = raw_command[len("/watch_") :]
                if rest.strip():
                    return _error("/watch_<id> non accetta altri argomenti", HELP_TEXT)
                if not inline_id:
                    return _error("manca l'id del preset", HELP_TEXT)
                return self._watch(inline_id)
            if command == "/add":
                return self._add(rest)
            if command == "/set":
                return self._set(rest)
            if command == "/remove":
                return self._remove(rest)
            return f"comando sconosciuto: {raw_command}\n\n{HELP_TEXT}"
        except Exception:
            logger.exception("unhandled error while handling a Telegram command")
            return None

    def _list(self) -> str:
        ids = self._store.ids()
        if not ids:
            return "nessun preset"

        lines = ["preset (* = seguita):"]
        for watch_id in ids:
            if self._store.resolved(watch_id) is None:
                reason = self._store.error(watch_id) or "non risolvibile"
                lines.append(f"  {watch_id}  (non valido: {reason})")
            else:
                mark = "*" if watch_id == self._store.selected else " "
                lines.append(f"{mark} {watch_id}")
        return "\n".join(lines)

    def _watch(self, rest: str) -> str:
        try:
            tokens = _tokens(rest)
            if not tokens:
                return HELP_TEXT
            if len(tokens) > 1:
                raise CommandError("specifica un solo id")
            preset = self._store.select(tokens[0])
            resolved = self._store.resolved(preset.watch_id)
            if resolved is None:
                raise PresetError(f"watch {preset.watch_id!r} is invalid")
            self._notify(WatchChange(preset.watch_id, resolved.watch, True))
            return format_effective(preset, resolved)
        except (CommandError, PresetError) as exc:
            return _error(str(exc), HELP_TEXT)

    def _add(self, rest: str) -> str:
        try:
            tokens = _tokens(rest)
            if not tokens:
                return ADD_TEMPLATE
            watch_id = tokens[0]
            values = _pairs(tokens[1:])
            preset = self._store.add(watch_id, values)
            return f"preset aggiunto: {preset.watch_id}"
        except (CommandError, PresetError) as exc:
            return _error(str(exc), ADD_TEMPLATE)

    def _set(self, rest: str) -> str:
        try:
            tokens = _tokens(rest)
            if not tokens:
                raise CommandError("manca l'id del preset")
            found = self._find_id(tokens[0])
            if found is None:
                raise PresetError(f"unknown watch {tokens[0]!r}")
            values = _pairs(tokens[1:])

            if not values:
                preset = self._store.get(found)
                resolved = self._store.resolved(found)
                if preset is None or resolved is None:
                    reason = self._store.error(found) or "non risolvibile"
                    raise PresetError(f"watch {found!r} is invalid: {reason}")
                return format_effective(preset, resolved)

            selected = found == self._store.selected
            before = self._store.resolved(found) if selected else None
            preset = self._store.update(found, values)
            resolved = self._store.resolved(found)
            if resolved is None:
                raise PresetError(f"watch {found!r} is invalid")
            if selected:
                reset = before is None or _identity_changed(before, resolved)
                self._notify(WatchChange(found, resolved.watch, reset))
            return format_effective(preset, resolved)
        except (CommandError, PresetError) as exc:
            return _error(str(exc), HELP_TEXT)

    def _remove(self, rest: str) -> str:
        try:
            tokens = _tokens(rest)
            if not tokens:
                raise CommandError("manca l'id del preset")
            if len(tokens) > 1:
                raise CommandError("specifica un solo id")
            found = self._find_id(tokens[0])
            if found is None:
                raise PresetError(f"unknown watch {tokens[0]!r}")
            was_selected = self._store.selected == found
            removed = self._store.remove(found)
            if was_selected:
                self._notify(WatchChange(None, None, True))
            return f"preset rimosso: {removed}"
        except (CommandError, PresetError) as exc:
            return _error(str(exc), HELP_TEXT)

    def _find_id(self, watch_id: str) -> str | None:
        try:
            canonical = canonical_id(watch_id)
        except PresetError:
            return None
        for existing in self._store.ids():
            try:
                if canonical_id(existing) == canonical:
                    return existing
            except PresetError:
                continue
        return None

    def _notify(self, change: WatchChange) -> None:
        if self._on_watch_change is None:
            return
        try:
            self._on_watch_change(change)
        except Exception:
            logger.exception("watch-change callback failed")

    # --- HTTP --------------------------------------------------------------

    async def _send(self, text: str) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        for attempt in range(MAX_SEND_RETRIES + 1):
            try:
                response = await self._client().post(self._url("sendMessage"), json=payload)
            except (httpx.HTTPError, OSError) as exc:
                if attempt >= MAX_SEND_RETRIES:
                    logger.error(
                        "Telegram sendMessage failed after %d attempts: %s",
                        attempt + 1,
                        _redact(str(exc)),
                    )
                    return
                logger.warning("Telegram sendMessage network error; retrying")
                await self._sleep(RETRY_DELAYS[attempt])
                continue

            status = response.status_code
            if status == 200:
                return
            if status == 429:
                if attempt >= MAX_SEND_RETRIES:
                    logger.error("Telegram sendMessage was rate-limited after retries")
                    return
                await self._sleep(_retry_after(response))
                continue
            if status >= 500:
                if attempt >= MAX_SEND_RETRIES:
                    logger.error("Telegram sendMessage failed with HTTP %d", status)
                    return
                await self._sleep(RETRY_DELAYS[attempt])
                continue
            logger.error(
                "Telegram sendMessage failed with HTTP %d: %s",
                status,
                _description(response),
            )
            return

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout_s)
            self._owns_http = True
        return self._http

    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self._token}/{method}"


# --- formatting ------------------------------------------------------------


def format_effective(preset: Preset, resolved: ResolvedPreset) -> str:
    """Effective values in the locked key order, pasteable back into /set."""
    mapping = preset.to_mapping()
    lines: list[str] = []
    for key in PRESET_KEYS:
        value = mapping[key]
        if _is_auto(value):
            computed = getattr(resolved.auto, key)
            lines.append(f"{key}=auto  # {_render_value(computed)}")
        else:
            lines.append(f"{key}={_render_value(value)}")
    return "\n".join(lines)


def _identity_changed(before: ResolvedPreset, after: ResolvedPreset) -> bool:
    return (
        before.watch.line.casefold() != after.watch.line.casefold()
        or before.stop.stop_id != after.stop.stop_id
    )


def _render_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, str):
        return _quote(value)
    return str(value)


def _quote(value: str) -> str:
    if value and all(char.isalnum() or char in "-_./:" for char in value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _is_auto(value: object) -> bool:
    return isinstance(value, str) and value.strip().casefold() == AUTO


def _error(reason: str, syntax: str) -> str:
    return f"errore: {reason}\n\n{syntax}"


# --- parsing ---------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    try:
        return shlex.split(_strip_comments(text), comments=False, posix=True)
    except ValueError as exc:
        raise CommandError(f"virgolette non chiuse: {exc}") from exc


def _pairs(tokens: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in tokens:
        key, separator, value = token.partition("=")
        if not separator:
            raise CommandError(f"atteso chiave=valore, trovato {token!r}")
        key = key.strip()
        if not key:
            raise CommandError("chiave vuota")
        values[key] = value
    return values


def _strip_comments(text: str) -> str:
    """Drop unquoted ``#`` to the end of each line; quoted ``#`` survives."""
    return "\n".join(_strip_comment_line(line) for line in text.splitlines())


def _strip_comment_line(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "#":
            return line[:index]
    return line


# --- HTTP helpers ----------------------------------------------------------


def _retry_after(response: httpx.Response) -> float:
    value: object = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        parameters = body.get("parameters")
        if isinstance(parameters, dict):
            value = parameters.get("retry_after")
    if value is None:
        value = response.headers.get("Retry-After")

    if isinstance(value, (int, float, str)):
        try:
            seconds = float(value)
        except ValueError:
            seconds = DEFAULT_RETRY_AFTER_S
    else:
        seconds = DEFAULT_RETRY_AFTER_S
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)


def _description(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return _redact(response.text[:200])
    if isinstance(body, dict):
        return _redact(str(body.get("description", "")))
    return _redact(response.text[:200])


def _redact(value: str) -> str:
    return _TOKEN_IN_URL.sub("/bot<redacted>/", value)
