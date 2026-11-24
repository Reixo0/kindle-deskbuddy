"""UI state machine, idle clock timer, library caching, and touch hitbox handling."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .config import Settings
from .lyrics import LyricsService
from .spotify_client import DeviceItem, MediaItem, PlaybackState, SpotifyBackend
from .weather import WeatherService


class ViewMode(str, Enum):
    NOW_PLAYING = "now_playing"
    IDLE_CLOCK = "idle_clock"
    BROWSE = "browse"
    ALBUM_DETAIL = "album_detail"
    QUEUE = "queue"
    DEVICES = "devices"
    BRIGHTNESS = "brightness"
    SLEEP = "sleep"


class BrowseTab(str, Enum):
    ALBUMS = "albums"
    SONGS = "songs"
    PLAYLISTS = "playlists"
    RECENT = "recent"


@dataclass
class Hitbox:
    x1: int
    y1: int
    x2: int
    y2: int
    action: str
    payload: dict[str, Any] = field(default_factory=dict)

    def contains(self, x: int, y: int) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2


def map_touch_to_logical(
    fb_x: int,
    fb_y: int,
    logical_w: int,
    logical_h: int,
    rotation_cw: int,
) -> tuple[int, int]:
    """Inverse-map a tap on the (possibly rotated) Kindle framebuffer to landscape (x, y)."""
    rot = rotation_cw % 360
    if rot == 90:
        return fb_y, logical_h - 1 - fb_x
    if rot == 270:
        return logical_w - 1 - fb_y, fb_x
    if rot == 180:
        return logical_w - 1 - fb_x, logical_h - 1 - fb_y
    return fb_x, fb_y


class DeskBuddyState:
    """Thread-safe application state shared between rendering and touch handling."""

    def __init__(self, settings: Settings, spotify: SpotifyBackend) -> None:
        self.settings = settings
        self.spotify = spotify
        self.lyrics = LyricsService(demo_mode=spotify.is_demo)
        self.weather = WeatherService(settings, demo_mode=spotify.is_demo)
        self._lock = threading.RLock()

        now = time.monotonic()
        self.view: ViewMode = ViewMode.NOW_PLAYING
        self.previous_view: ViewMode = ViewMode.NOW_PLAYING
        self.browse_tab: BrowseTab = BrowseTab.PLAYLISTS
        self.idle_tab: BrowseTab = BrowseTab.PLAYLISTS
        self.page: int = 0

        self.selected_album: Optional[MediaItem] = None
        self.selected_album_tracks: list[MediaItem] = []

        self.playback: PlaybackState = PlaybackState()
        self.last_playing_at: float = now
        self.last_interaction_at: float = now
        self.last_view_change_at: float = now
        self.needs_full_refresh: bool = True

        self.kindle_battery: Optional[int] = None
        self.kindle_charging: bool = False
        self.last_battery_at: float = 0.0
        self.kindle_brightness: int = 12  # Kindle frontlight scale 0..24
        self.saved_brightness: int = 12
        self._prefs_path: Path = settings.spotify_cache_path.parent / ".deskbuddy_prefs.json"
        self.sleep_brightness: int = self._load_sleep_brightness()
        self.brightness_dirty: bool = False

        self.status_banner: str = ""
        self.status_banner_until: float = 0.0

        # Cached library / queue / devices with TTL
        self._cache: dict[str, tuple[Any, float]] = {}
        self.hitboxes: list[Hitbox] = []
        self.last_logical_size: tuple[int, int] = (
            settings.screen_width,
            settings.screen_height,
        )

    def _load_sleep_brightness(self) -> int:
        try:
            if self._prefs_path.is_file():
                data = json.loads(self._prefs_path.read_text(encoding="utf-8"))
                return max(0, min(24, int(data.get("sleep_brightness", 6))))
        except Exception:
            pass
        return 6

    def _save_sleep_brightness(self) -> None:
        try:
            self._prefs_path.write_text(
                json.dumps({"sleep_brightness": self.sleep_brightness}),
                encoding="utf-8",
            )
        except Exception:
            pass

    @property
    def editing_sleep_brightness(self) -> bool:
        return self.view == ViewMode.BRIGHTNESS and self.previous_view == ViewMode.SLEEP

    def set_banner(self, message: str, ttl: float = 4.0) -> None:
        if message:
            self.status_banner = message
            self.status_banner_until = time.monotonic() + ttl

    def active_banner(self) -> str:
        if self.status_banner and time.monotonic() < self.status_banner_until:
            return self.status_banner
        return ""

    def update_battery(
        self,
        battery: Optional[int],
        charging: Optional[bool],
        brightness: Optional[int] = None,
    ) -> None:
        with self._lock:
            if battery is not None:
                self.kindle_battery = max(0, min(100, battery))
                self.last_battery_at = time.monotonic()
            if charging is not None:
                self.kindle_charging = bool(charging)
            if (
                brightness is not None
                and not self.brightness_dirty
                and self.view != ViewMode.SLEEP
                and not self.editing_sleep_brightness
            ):
                self.kindle_brightness = max(0, min(24, brightness))

    def _set_view(self, new_view: ViewMode, reset_page: bool = True) -> None:
        if self.view != new_view:
            self.view = new_view
            self.last_view_change_at = time.monotonic()
            self.needs_full_refresh = True
            if new_view == ViewMode.IDLE_CLOCK:
                self.invalidate_cache("recent_playlists", "recent_albums")
        if reset_page:
            self.page = 0

    def _cached_fetch(self, key: str, ttl: float, loader) -> Any:
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry and (now - entry[1]) < ttl:
            return entry[0]
        data = loader()
        self._cache[key] = (data, now)
        return data

    def invalidate_cache(self, *keys: str) -> None:
        if not keys:
            self._cache.clear()
        else:
            for k in keys:
                self._cache.pop(k, None)

    def refresh_playback(self) -> PlaybackState:
        """Poll Spotify playback and apply the 5-minute idle clock transition."""
        with self._lock:
            now = time.monotonic()
            pb = self.spotify.get_playback()
            self.playback = pb

            if pb.is_playing:
                self.last_playing_at = now
                if self.view == ViewMode.IDLE_CLOCK:
                    self._set_view(ViewMode.NOW_PLAYING)
            elif self.view != ViewMode.SLEEP:
                idle_elapsed = now - self.last_playing_at
                interaction_elapsed = now - self.last_interaction_at
                # If Spotify lost all active devices (after a brief grace period for freshly started playback),
                # jump straight to the Standby Clock without waiting 5 minutes.
                lost_all_devices = (
                    pb.authenticated
                    and not pb.device_id
                    and not pb.error_message
                    and interaction_elapsed >= 12.0
                )
                if lost_all_devices and self.view == ViewMode.NOW_PLAYING:
                    self._set_view(ViewMode.IDLE_CLOCK)
                elif idle_elapsed >= self.settings.idle_timeout_seconds:
                    if self.view == ViewMode.NOW_PLAYING:
                        self._set_view(ViewMode.IDLE_CLOCK)
                    elif (
                        self.view in (ViewMode.BROWSE, ViewMode.ALBUM_DETAIL, ViewMode.QUEUE, ViewMode.DEVICES)
                        and interaction_elapsed >= 90.0
                    ):
                        self._set_view(ViewMode.IDLE_CLOCK)

            return pb

    def force_idle_clock(self) -> None:
        """Helper for preview/testing to jump straight into the Idle Clock view."""
        with self._lock:
            if self.playback.is_playing and self.spotify.is_demo:
                self.spotify.toggle_play_pause()
            self.last_playing_at = time.monotonic() - self.settings.idle_timeout_seconds - 5
            self._set_view(ViewMode.IDLE_CLOCK)

    def get_browse_items(self, tab: Optional[BrowseTab] = None) -> list[MediaItem]:
        active_tab = tab or self.browse_tab
        if active_tab == BrowseTab.ALBUMS:
            return self._cached_fetch("albums", 60.0, self.spotify.get_saved_albums)
        if active_tab == BrowseTab.SONGS:
            return self._cached_fetch("songs", 45.0, self.spotify.get_liked_songs)
        if active_tab == BrowseTab.PLAYLISTS:
            return self._cached_fetch("playlists", 60.0, self.spotify.get_playlists)
        return self._cached_fetch("recent", 30.0, self.spotify.get_recently_played)

    def get_idle_items(self, tab: Optional[BrowseTab] = None) -> list[MediaItem]:
        """Return the 4 most recently played playlists/albums (or liked songs) for the Standby Clock."""
        active_tab = tab or self.idle_tab
        if active_tab == BrowseTab.PLAYLISTS:
            return self._cached_fetch("recent_playlists", 45.0, self.spotify.get_recent_playlists)
        if active_tab == BrowseTab.ALBUMS:
            return self._cached_fetch("recent_albums", 45.0, self.spotify.get_recent_albums)
        return self.get_browse_items(active_tab)[:4]

    def get_queue_data(self) -> tuple[Optional[MediaItem], list[MediaItem]]:
        return self._cached_fetch("queue", 10.0, self.spotify.get_queue)

    def get_devices_data(self) -> list[DeviceItem]:
        return self._cached_fetch("devices", 10.0, self.spotify.get_devices)

    def consume_full_refresh_flag(self) -> bool:
        with self._lock:
            flag = self.needs_full_refresh
            self.needs_full_refresh = False
            return flag

    def set_hitboxes(self, hitboxes: list[Hitbox], logical_size: tuple[int, int]) -> None:
        with self._lock:
            self.hitboxes = hitboxes
            self.last_logical_size = logical_size

    def handle_tap(
        self,
        fb_x: int,
        fb_y: int,
        rotation_cw: int = 0,
    ) -> dict[str, Any]:
        """Process a touch coordinate and dispatch the matching UI action."""
        with self._lock:
            logical_w, logical_h = self.last_logical_size
            x, y = map_touch_to_logical(fb_x, fb_y, logical_w, logical_h, rotation_cw)
            self.last_interaction_at = time.monotonic()

            matched: Optional[Hitbox] = None
            for hb in reversed(self.hitboxes):
                if hb.contains(x, y):
                    matched = hb
                    break

            if not matched:
                return {
                    "ok": False,
                    "action": "none",
                    "message": "",
                    "x": x,
                    "y": y,
                    "view": self.view.value,
                }

            result = self._execute_action(matched.action, matched.payload, tap_x=x, tap_y=y)
            res_dict: dict[str, Any] = {
                "ok": result.get("ok", True),
                "action": matched.action,
                "message": result.get("message", ""),
                "exit": bool(result.get("exit", False)),
                "x": x,
                "y": y,
                "view": self.view.value,
            }
            if "brightness" in result:
                res_dict["brightness"] = result["brightness"]
            if "sleep_mode" in result:
                res_dict["sleep_mode"] = result["sleep_mode"]
            if "sleep_wake_brightness" in result:
                res_dict["sleep_wake_brightness"] = result["sleep_wake_brightness"]
            return res_dict

    def _execute_action(
        self,
        action: str,
        payload: dict[str, Any],
        tap_x: int = 0,
        tap_y: int = 0,
    ) -> dict[str, Any]:
        if action == "exit":
            if self.view == ViewMode.SLEEP or self.editing_sleep_brightness:
                restore = self.saved_brightness
                self.kindle_brightness = restore
                self._set_view(
                    ViewMode.NOW_PLAYING if self.playback.is_playing else ViewMode.IDLE_CLOCK
                )
                return {
                    "ok": True,
                    "exit": True,
                    "brightness": restore,
                    "message": "Exiting Desk Buddy",
                }
            return {"ok": True, "exit": True, "message": "Exiting Desk Buddy"}

        if action == "toggle_sleep":
            if self.view == ViewMode.SLEEP:
                restore = self.saved_brightness
                self.kindle_brightness = restore
                target = ViewMode.NOW_PLAYING if self.playback.is_playing else ViewMode.IDLE_CLOCK
                self._set_view(target)
                return {
                    "ok": True,
                    "sleep_mode": False,
                    "brightness": restore,
                    "message": "Sleep mode off",
                }
            self.saved_brightness = self.kindle_brightness
            wake_level = self.sleep_brightness
            self.kindle_brightness = 0
            self.previous_view = self.view
            self._set_view(ViewMode.SLEEP)
            return {
                "ok": True,
                "sleep_mode": True,
                "sleep_wake_brightness": wake_level,
                "brightness": 0,
                "message": "Sleep mode on",
            }

        if action == "toggle_brightness":
            if self.view == ViewMode.BRIGHTNESS:
                self._set_view(self.previous_view)
            else:
                self.previous_view = self.view
                self._set_view(ViewMode.BRIGHTNESS)
            return {"ok": True, "message": ""}

        if action == "brightness_set":
            val = max(0, min(24, int(payload.get("level", 12))))
            pct = int(round(val * 100 / 24))
            if self.editing_sleep_brightness:
                self.sleep_brightness = val
                self._save_sleep_brightness()
                self.set_banner(f"Sleep Brightness {pct}%", ttl=2.0)
                return {"ok": True, "brightness": val, "sleep_wake_brightness": val, "message": f"Sleep Brightness {pct}%"}
            self.kindle_brightness = val
            self.brightness_dirty = False
            self.set_banner(f"Brightness {pct}%", ttl=2.0)
            return {"ok": True, "brightness": val, "message": f"Brightness {pct}%"}

        if action == "brightness_step":
            delta = int(payload.get("delta", 0))
            if self.editing_sleep_brightness:
                val = max(0, min(24, self.sleep_brightness + delta))
                self.sleep_brightness = val
                self._save_sleep_brightness()
                pct = int(round(val * 100 / 24))
                self.set_banner(f"Sleep Brightness {pct}%", ttl=2.0)
                return {"ok": True, "brightness": val, "sleep_wake_brightness": val, "message": f"Sleep Brightness {pct}%"}
            val = max(0, min(24, self.kindle_brightness + delta))
            self.kindle_brightness = val
            self.brightness_dirty = False
            pct = int(round(val * 100 / 24))
            self.set_banner(f"Brightness {pct}%", ttl=2.0)
            return {"ok": True, "brightness": val, "message": f"Brightness {pct}%"}

        if action == "nav":
            target = ViewMode(payload["view"])
            self._set_view(target)
            if target == ViewMode.QUEUE:
                self.invalidate_cache("queue")
            elif target == ViewMode.DEVICES:
                self.invalidate_cache("devices")
            return {"ok": True, "message": ""}

        if action == "browse_tab":
            self.browse_tab = BrowseTab(payload["tab"])
            self.page = 0
            self._set_view(ViewMode.BROWSE, reset_page=True)
            return {"ok": True, "message": ""}

        if action == "idle_tab":
            self.idle_tab = BrowseTab(payload["tab"])
            return {"ok": True, "message": ""}

        if action == "page":
            delta = int(payload.get("delta", 0))
            self.page = max(0, self.page + delta)
            return {"ok": True, "message": ""}

        if action == "refresh_list":
            self.invalidate_cache("queue", "devices", "albums", "songs", "playlists", "recent")
            self.needs_full_refresh = True
            self.set_banner("Refreshed", ttl=2.0)
            return {"ok": True, "message": "Refreshed"}

        if action == "play_pause":
            res = self.spotify.toggle_play_pause()
            self.set_banner(res.message, ttl=2.5)
            self.refresh_playback()
            return {"ok": res.ok, "message": res.message}

        if action == "next":
            res = self.spotify.next_track()
            self.invalidate_cache("queue")
            self.set_banner(res.message, ttl=2.5)
            self.refresh_playback()
            return {"ok": res.ok, "message": res.message}

        if action == "prev":
            res = self.spotify.prev_track()
            self.invalidate_cache("queue")
            self.set_banner(res.message, ttl=2.5)
            self.refresh_playback()
            return {"ok": res.ok, "message": res.message}

        if action == "vol_up":
            res = self.spotify.adjust_volume(self.settings.volume_step)
            self.set_banner(res.message, ttl=2.5)
            self.refresh_playback()
            return {"ok": res.ok, "message": res.message}

        if action == "vol_down":
            res = self.spotify.adjust_volume(-self.settings.volume_step)
            self.set_banner(res.message, ttl=2.5)
            self.refresh_playback()
            return {"ok": res.ok, "message": res.message}

        if action == "shuffle":
            res = self.spotify.toggle_shuffle()
            self.set_banner(res.message, ttl=2.5)
            self.refresh_playback()
            return {"ok": res.ok, "message": res.message}

        if action == "like":
            res = self.spotify.toggle_like()
            self.invalidate_cache("songs")
            self.set_banner(res.message, ttl=2.5)
            self.refresh_playback()
            return {"ok": res.ok, "message": res.message}

        if action == "seek":
            bar_x1 = int(payload.get("bar_x1", 0))
            bar_x2 = int(payload.get("bar_x2", 1))
            if bar_x2 > bar_x1 and self.playback.duration_ms > 0:
                ratio = max(0.0, min(1.0, (tap_x - bar_x1) / float(bar_x2 - bar_x1)))
                target_ms = int(self.playback.duration_ms * ratio)
                res = self.spotify.seek_track(target_ms)
                self.set_banner(res.message, ttl=2.0)
                self.refresh_playback()
                return {"ok": res.ok, "message": res.message}
            return {"ok": False, "message": "Nothing playing"}

        if action in ("open_album", "open_collection"):
            item: MediaItem = payload["item"]
            self.selected_album = item
            if item.kind == "playlist":
                self.selected_album_tracks = self.spotify.get_playlist_tracks(item.id, item.uri)
            else:
                self.selected_album_tracks = self.spotify.get_album_tracks(item.id, item.uri)
            self._set_view(ViewMode.ALBUM_DETAIL, reset_page=True)
            return {"ok": True, "message": ""}

        if action == "play_item":
            item = payload["item"]
            shuffle_flag = payload.get("shuffle")
            res = self.spotify.play_item(item, shuffle=shuffle_flag)
            self.set_banner(res.message, ttl=3.0)
            if res.ok:
                self.invalidate_cache("queue", "recent_albums", "recent_playlists")
                self.last_playing_at = time.monotonic()
                self._set_view(ViewMode.NOW_PLAYING)
                self.refresh_playback()
            return {"ok": res.ok, "message": res.message}

        if action == "play_queue_index":
            q_idx = int(payload.get("index", 0))
            _, upcoming = self.get_queue_data()
            res = self.spotify.play_from_queue(q_idx, upcoming)
            self.set_banner(res.message, ttl=3.0)
            if res.ok:
                self.invalidate_cache("queue")
                self.last_playing_at = time.monotonic()
                self._set_view(ViewMode.NOW_PLAYING)
                self.refresh_playback()
            return {"ok": res.ok, "message": res.message}

        if action == "cycle_device":
            self.invalidate_cache("devices")
            devices = self.get_devices_data()
            if not devices:
                self.set_banner("No Spotify devices found", ttl=3.0)
                return {"ok": False, "message": "No Spotify devices found"}
            active_idx = next((i for i, d in enumerate(devices) if d.is_active), -1)
            if len(devices) == 1 and active_idx == 0:
                msg = f"Active: {devices[0].name}"
                self.set_banner(msg, ttl=2.0)
                return {"ok": True, "message": msg}
            next_dev = devices[(active_idx + 1) % len(devices)]
            res = self.spotify.transfer_playback(next_dev.id, force_play=self.playback.is_playing)
            self.invalidate_cache("devices")
            msg = f"Device: {next_dev.name}" if res.ok else res.message
            self.set_banner(msg, ttl=3.0)
            self.refresh_playback()
            return {"ok": res.ok, "message": msg}

        if action == "select_device":
            device_id = payload["device_id"]
            res = self.spotify.transfer_playback(device_id, force_play=self.playback.is_playing)
            self.invalidate_cache("devices")
            self.set_banner(res.message, ttl=3.0)
            self.refresh_playback()
            return {"ok": res.ok, "message": res.message}

        return {"ok": False, "message": f"Unknown action: {action}"}

    def snapshot_dict(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            return {
                "view": self.view.value,
                "browse_tab": self.browse_tab.value,
                "idle_tab": self.idle_tab.value,
                "page": self.page,
                "demo_mode": self.spotify.is_demo,
                "authenticated": self.playback.authenticated,
                "is_playing": self.playback.is_playing,
                "track": self.playback.track_name,
                "artist": self.playback.artist_name,
                "album": self.playback.album_name,
                "device": self.playback.device_name,
                "volume_percent": self.playback.volume_percent,
                "shuffle": self.playback.shuffle_state,
                "liked": self.playback.is_liked,
                "kindle_battery": self.kindle_battery,
                "kindle_charging": self.kindle_charging,
                "idle_seconds": int(max(0.0, now - self.last_playing_at)),
                "idle_timeout_seconds": self.settings.idle_timeout_seconds,
            }
