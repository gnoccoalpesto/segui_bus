"""Runtime configuration.

Everything the outside world dictates (URLs, identity headers, timeouts) lives
here, so no other module reads ``os.environ`` or the VERSION file directly.
The variable names mirror ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# config.py lives in <repo>/src/segui_bus/, so three hops up is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = REPO_ROOT / "VERSION"

# NOTE attribution line — messaggio attribuzione dati
ATTRIBUTION = "BusOne (https://busone.app) e la fonte originale (SETA S.p.A.)"


def read_version() -> str:
    """Read the version from its single source of truth (docs/versioning.md)."""
    return VERSION_FILE.read_text(encoding="utf-8").strip()


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable view of the runtime configuration."""

    version: str
    base_url: str
    namespace: str
    use: str
    contact: str
    fetch_timeout_s: float
    stale_after_s: int
    ws_path: str
    fallback_vp_interval_s: int
    fallback_tu_interval_s: int

    @property
    def client_header(self) -> str:
        """Value BusOne sees in ``X-BusOne-Client`` (research note §2.4)."""
        return f"segui_bus/{self.version} {self.contact}"

    @property
    def user_agent(self) -> str:
        """Our own UA.

        The proxy answers 403 to the default ``Python-urllib/3.12`` agent of
        the *stdlib* ``urllib.request`` (Cloudflare error 1010), so the UA is
        never left to the HTTP library's default. Research note
        ``docs/research/2026-10-21-busone-proxy-403-python-urllib.md``.
        """
        return f"segui_bus/{self.version}"

    def headers(self) -> dict[str, str]:
        """Identification headers: a soft auth, always sent (research note §1.2).

        Also carries the explicit ``User-Agent``; every client — HTTP and the
        WebSocket handshake alike — must be given this mapping so no library
        default reaches the wire.
        """
        return {
            "X-BusOne-Client": self.client_header,
            "X-BusOne-Use": self.use,
            "User-Agent": self.user_agent,
        }

    def feed_url(self, feed: str, fmt: str = "pb") -> str:
        """URL of a realtime feed, e.g. feed="vehicle-positions", fmt="pb"."""
        return f"{self.base_url}/{self.namespace}/gtfs-rt/{feed}.{fmt}"

    @property
    def ws_url(self) -> str:
        """WebSocket endpoint used later by the long-running service.

        ws_path is only the suffix ("/ws"); namespace and scheme are added here
        so there is one place that knows how the URL is assembled.
        """
        url = f"{self.base_url}/{self.namespace}{self.ws_path}"
        return url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)


def load_settings(env_file: Path | None = None) -> Settings:
    """Build Settings from the environment, with ``.env`` as an optional overlay.

    ``override=False`` keeps real environment variables authoritative over .env.
    Defaults match .env.example so the library works with no configuration.
    """
    load_dotenv(env_file or REPO_ROOT / ".env", override=False)

    return Settings(
        version=read_version(),
        base_url=os.getenv("BUSONE_BASE_URL", "https://proxy.busone.app").rstrip("/"),
        namespace=os.getenv("BUSONE_NAMESPACE", "seta-pc"),
        use=os.getenv("BUSONE_USE", "personal"),
        contact=os.getenv("BUSONE_CLIENT_CONTACT", "gh/gnoccoalpesto"),
        fetch_timeout_s=float(os.getenv("BUSONE_FETCH_TIMEOUT_S", "10")),
        stale_after_s=int(os.getenv("BUSONE_STALE_AFTER_S", "120")),
        ws_path=os.getenv("BUSONE_WS_PATH", "/ws"),
        fallback_vp_interval_s=int(os.getenv("BUSONE_FALLBACK_VP_INTERVAL_S", "30")),
        fallback_tu_interval_s=int(os.getenv("BUSONE_FALLBACK_TU_INTERVAL_S", "15")),
    )
