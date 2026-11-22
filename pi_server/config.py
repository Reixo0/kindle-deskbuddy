"""Configuration loaded from environment variables / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    spotify_client_id: str
    spotify_client_secret: str
    spotify_redirect_uri: str
    spotify_cache_path: Path
    host: str
    port: int
    demo_mode: bool
    screen_width: int
    screen_height: int
    rotation: int
    idle_timeout_seconds: int
    volume_step: int
    time_format: str
    font_path: str
    font_bold_path: str

    @property
    def has_spotify_credentials(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)


def load_settings() -> Settings:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    demo_flag = _env_bool("DEMO_MODE", False)
    # Automatically fall back to demo mode if no credentials are provided
    demo_mode = demo_flag or not (client_id and client_secret)

    rotation = _env_int("ROTATION", 90) % 360
    if rotation not in (0, 90, 180, 270):
        rotation = 90

    time_fmt = os.getenv("TIME_FORMAT", "24h").strip().lower()
    if time_fmt not in ("24h", "12h"):
        time_fmt = "24h"

    return Settings(
        spotify_client_id=client_id,
        spotify_client_secret=client_secret,
        spotify_redirect_uri=os.getenv(
            "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8765/auth/callback"
        ).strip(),
        spotify_cache_path=PROJECT_ROOT / os.getenv("SPOTIFY_CACHE_PATH", ".spotify_cache"),
        host=os.getenv("HOST", "0.0.0.0").strip(),
        port=_env_int("PORT", 8765),
        demo_mode=demo_mode,
        screen_width=_env_int("SCREEN_WIDTH", 800),
        screen_height=_env_int("SCREEN_HEIGHT", 600),
        rotation=rotation,
        idle_timeout_seconds=max(10, _env_int("IDLE_TIMEOUT_SECONDS", 300)),
        volume_step=max(1, min(25, _env_int("VOLUME_STEP", 10))),
        time_format=time_fmt,
        font_path=os.getenv("FONT_PATH", "").strip(),
        font_bold_path=os.getenv("FONT_BOLD_PATH", "").strip(),
    )
