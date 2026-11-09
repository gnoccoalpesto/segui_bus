"""Outbound Telegram delivery for watch alarms.

Implements issue #11: render the alert-first compact message agreed in #8,
escape it for MarkdownV2, and deliver it with retries that never raise into the
watch loop. Inbound preset commands are a separate ticket (#29).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable

import httpx

from ..clock import local_clock
from ..config import ATTRIBUTION, read_version
from ..service.watch import PREAVVISO, AlarmEvent

logger = logging.getLogger(__name__)

TOKEN_ENV = "SEGUI_BUS_TELEGRAM_TOKEN"
CHAT_ID_ENV = "SEGUI_BUS_TELEGRAM_CHAT_ID"

API_ROOT = "https://api.telegram.org"
MAX_MESSAGE_UNITS = 4096
MAX_RETRIES = 3
RETRY_DELAYS = (1.0, 2.0, 4.0)
DEFAULT_RETRY_AFTER_S = 1.0
MAX_RETRY_AFTER_S = 60.0
DEFAULT_TIMEOUT_S = 10.0

VERDICT_REAL = "AUTOBUS VICINO"
VERDICT_PREAVVISO = "PREAVVISO"
MALFORMED_RIDE = "corsa non inizializzata"
STALE_PREFIX = "dati non aggiornati da"

Sleep = Callable[[float], Awaitable[None]]

_MARKDOWN_SPECIALS = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_TOKEN_IN_URL = re.compile(r"/bot[^/\s]+/")


def escape_markdown_v2(text: str) -> str:
    """Escape normal-text MarkdownV2 specials, including backslash itself."""
    return _MARKDOWN_SPECIALS.sub(r"\\\1", text)


def utf16_length(text: str) -> int:
    """Telegram measures text in UTF-16 code units, not code points."""
    return len(text.encode("utf-16-le")) // 2


def render_alarm(event: AlarmEvent, *, version: str | None = None) -> tuple[str, str]:
    """Render one event as ``(markdown_v2, plain_text)``.

    Both strings carry the same visible text, so the plain rendering is safe to
    resend when Telegram rejects the MarkdownV2 entities.
    """
    verdict = VERDICT_PREAVVISO if event.kind == PREAVVISO else VERDICT_REAL
    first_line = f"{verdict} · linea {event.line}"
    if event.headsign:
        first_line += f" → {event.headsign}"

    fact_tokens = [f"bus {event.vehicle_id}"]
    if not event.malformed:
        stops = _stops_text(event.stops_to_target)
        if stops is not None:
            fact_tokens.append(stops)
        eta = _eta_text(event)
        if eta is not None:
            fact_tokens.append(eta)

    distance = _distance_text(event.distance_m)
    if distance is not None:
        fact_tokens.append(distance)

    if event.malformed:
        fact_tokens.append(MALFORMED_RIDE)
    else:
        punctuality = _punctuality_text(event)
        if punctuality is not None:
            fact_tokens.append(punctuality)

    fact_line = " · ".join((*fact_tokens, f"dato {local_clock(event.snapshot_unix)}"))
    stale_line = f"{STALE_PREFIX} {event.stale_age_s} s" if event.stale else None

    current_version = read_version() if version is None else version
    footer = f"{ATTRIBUTION}\nsegui_bus {current_version}"

    body = _fit_body(first_line, fact_line, stale_line, footer)
    plain = f"{body}\n\n{footer}"

    first, _, rest = body.partition("\n")
    rest_of_first = first[len(verdict) :] if first.startswith(verdict) else ""
    markdown = f"*{escape_markdown_v2(verdict)}*{escape_markdown_v2(rest_of_first)}"
    if rest:
        markdown += "\n" + escape_markdown_v2(rest)
    markdown += "\n\n" + escape_markdown_v2(footer)
    return markdown, plain


def _stops_text(stops: int | None) -> str | None:
    if stops is None:
        return None
    if stops <= 0:
        return "alla fermata"
    if stops == 1:
        return "1 fermata"
    return f"{stops} fermate"


def _eta_text(event: AlarmEvent) -> str | None:
    seconds = event.seconds_from_now
    if seconds is None:
        return None
    if seconds <= 0:
        return "in arrivo"

    minutes = max(1, round(seconds / 60))
    if event.arrival_unix is None:
        return f"~{minutes} min"
    return f"~{minutes} min ({local_clock(event.arrival_unix)})"


def _distance_text(distance_m: float | None) -> str | None:
    if distance_m is None:
        return None
    return f"{round(distance_m)} m"


def _punctuality_text(event: AlarmEvent) -> str | None:
    if event.eta_source == "schedule":
        return "orario programmato"

    delay = event.delay_s
    if event.eta_source != "realtime" or delay is None:
        return None
    if abs(delay) <= event.on_time_tolerance_s:
        return None

    minutes = max(1, round(abs(delay) / 60))
    if delay > 0:
        return f"in ritardo di {minutes} min"
    return f"in anticipo di {minutes} min"


def _fit_body(
    first_line: str,
    fact_line: str,
    stale_line: str | None,
    footer: str,
) -> str:
    """Keep the visible body under budget by shrinking the fact line only."""
    budget = MAX_MESSAGE_UNITS - utf16_length("\n\n" + footer)
    stale = f"\n{stale_line}" if stale_line else ""
    body = f"{first_line}\n{fact_line}{stale}"
    if utf16_length(body) <= budget:
        return body

    fixed = utf16_length(first_line) + 1 + utf16_length(stale)
    allowed = budget - fixed - 1
    if allowed <= 0:
        return f"{first_line}{stale}"
    return f"{first_line}\n{_truncate_utf16(fact_line, allowed)}…{stale}"


def _truncate_utf16(text: str, limit: int) -> str:
    """Truncate without splitting a surrogate pair."""
    if limit <= 0:
        return ""
    if utf16_length(text) <= limit:
        return text

    kept: list[str] = []
    used = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > limit:
            break
        kept.append(char)
        used += width
    return "".join(kept)


class TelegramNotifier:
    """Async sender for one bot and one chat.

    The HTTP client and the sleep function are injectable so tests can run the
    retry paths without a network or real waiting.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        http: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        version_reader: Callable[[], str] = read_version,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not token or not chat_id:
            raise RuntimeError("Telegram token and chat id are required")
        self._token = token
        self._chat_id = chat_id
        self._http = http
        self._owns_http = False
        self._sleep = sleep
        self._version_reader = version_reader
        self._timeout_s = timeout_s

    @classmethod
    def from_env(cls, **kwargs: object) -> TelegramNotifier:
        """Build a notifier from ``SEGUI_BUS_TELEGRAM_*``; fail without leaking secrets."""
        token = os.getenv(TOKEN_ENV)
        chat_id = os.getenv(CHAT_ID_ENV)
        missing = [name for name, value in ((TOKEN_ENV, token), (CHAT_ID_ENV, chat_id)) if not value]
        if missing:
            raise RuntimeError(f"Telegram is not configured: missing {', '.join(missing)}")
        return cls(token or "", chat_id or "", **kwargs)

    async def close(self) -> None:
        """Close the HTTP client only if this notifier created it."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
            self._owns_http = False

    async def send(self, event: AlarmEvent) -> None:
        """Render and deliver one alarm; never raise into the watch loop."""
        try:
            markdown, plain = render_alarm(event, version=self._version_reader())
        except Exception:
            logger.exception("could not render a Telegram alarm; dropping it")
            return
        await self.send_message(markdown, plain_text=plain)

    async def send_message(self, text: str, *, plain_text: str | None = None) -> None:
        """Send MarkdownV2 text, falling back to ``plain_text`` on a parse error."""
        if utf16_length(text) > MAX_MESSAGE_UNITS:
            text = _truncate_utf16(text, MAX_MESSAGE_UNITS - 1) + "…"
        try:
            await self._deliver(text, parse_mode="MarkdownV2", plain_text=plain_text)
        except Exception:
            logger.exception("unexpected Telegram notifier failure; dropping the message")

    async def _deliver(
        self,
        text: str,
        *,
        parse_mode: str | None,
        plain_text: str | None,
    ) -> None:
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self._client().post(
                    self._url(),
                    json=_payload(self._chat_id, text, parse_mode),
                )
            except (httpx.HTTPError, OSError) as exc:
                if attempt >= MAX_RETRIES:
                    logger.error(
                        "Telegram sendMessage failed after %d attempts: %s",
                        attempt + 1,
                        _redact(str(exc)),
                    )
                    return
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "Telegram sendMessage network error (%s); retrying in %.0f s",
                    _redact(str(exc)),
                    delay,
                )
                await self._sleep(delay)
                continue

            status = response.status_code
            if status == 200:
                if _ok(response):
                    return
                if attempt >= MAX_RETRIES:
                    logger.error(
                        "Telegram sendMessage returned ok=false after %d attempts",
                        attempt + 1,
                    )
                    return
                delay = RETRY_DELAYS[attempt]
                logger.warning("Telegram sendMessage returned ok=false; retrying in %.0f s", delay)
                await self._sleep(delay)
                continue

            if status == 429:
                if attempt >= MAX_RETRIES:
                    logger.error(
                        "Telegram sendMessage was rate-limited after %d attempts",
                        attempt + 1,
                    )
                    return
                delay = _retry_after(response)
                logger.warning("Telegram rate limit; retrying in %.0f s", delay)
                await self._sleep(delay)
                continue

            if status >= 500:
                if attempt >= MAX_RETRIES:
                    logger.error(
                        "Telegram sendMessage failed after %d attempts (HTTP %d)",
                        attempt + 1,
                        status,
                    )
                    return
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "Telegram sendMessage HTTP %d; retrying in %.0f s",
                    status,
                    delay,
                )
                await self._sleep(delay)
                continue

            if status == 400 and parse_mode is not None and plain_text is not None:
                logger.warning(
                    "Telegram rejected MarkdownV2 (%s); resending plain text",
                    _description(response),
                )
                await self._deliver(plain_text, parse_mode=None, plain_text=None)
                return

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

    def _url(self) -> str:
        return f"{API_ROOT}/bot{self._token}/sendMessage"


def _payload(chat_id: str, text: str, parse_mode: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    return payload


def _ok(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("ok") is True


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
    """Keep the bot token out of every log line."""
    return _TOKEN_IN_URL.sub("/bot<redacted>/", value)
