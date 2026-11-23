"""Spotify Web API wrapper (Spotipy) + interactive Demo mode fallback."""

from __future__ import annotations

import io
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .config import Settings

logger = logging.getLogger("deskbuddy.spotify")

SPOTIFY_SCOPES = " ".join(
    [
        "user-read-playback-state",
        "user-modify-playback-state",
        "user-read-currently-playing",
        "user-library-read",
        "user-library-modify",
        "playlist-read-private",
        "playlist-read-collaborative",
        "user-read-recently-played",
    ]
)


@dataclass
class PlaybackState:
    is_playing: bool = False
    track_id: str = ""
    track_uri: str = ""
    track_name: str = ""
    artist_name: str = ""
    album_name: str = ""
    album_art_url: str = ""
    progress_ms: int = 0
    duration_ms: int = 0
    shuffle_state: bool = False
    volume_percent: Optional[int] = 50
    device_id: str = ""
    device_name: str = ""
    device_type: str = ""
    is_liked: bool = False
    authenticated: bool = True
    error_message: str = ""

    @property
    def has_track(self) -> bool:
        return bool(self.track_name)


@dataclass
class MediaItem:
    id: str
    uri: str
    title: str
    subtitle: str
    kind: str  # "album" | "track" | "playlist"
    context_uri: str = ""
    image_url: str = ""


@dataclass
class DeviceItem:
    id: str
    name: str
    device_type: str
    is_active: bool
    volume_percent: Optional[int] = None


@dataclass
class ActionResult:
    ok: bool
    message: str = ""


class SpotifyBackend(Protocol):
    is_demo: bool

    def is_authenticated(self) -> bool: ...
    def get_authorize_url(self) -> str: ...
    def exchange_code(self, code: str) -> ActionResult: ...
    def get_playback(self) -> PlaybackState: ...
    def toggle_play_pause(self) -> ActionResult: ...
    def next_track(self) -> ActionResult: ...
    def prev_track(self) -> ActionResult: ...
    def adjust_volume(self, delta: int) -> ActionResult: ...
    def toggle_shuffle(self) -> ActionResult: ...
    def toggle_like(self) -> ActionResult: ...
    def seek_track(self, position_ms: int) -> ActionResult: ...
    def get_saved_albums(self, limit: int = 30) -> list[MediaItem]: ...
    def get_album_tracks(self, album_id: str, album_uri: str = "") -> list[MediaItem]: ...
    def get_liked_songs(self, limit: int = 30) -> list[MediaItem]: ...
    def get_playlists(self, limit: int = 30) -> list[MediaItem]: ...
    def get_playlist_tracks(self, playlist_id: str, playlist_uri: str = "") -> list[MediaItem]: ...
    def get_recently_played(self, limit: int = 20) -> list[MediaItem]: ...
    def get_queue(self) -> tuple[Optional[MediaItem], list[MediaItem]]: ...
    def get_devices(self) -> list[DeviceItem]: ...
    def play_item(self, item: MediaItem) -> ActionResult: ...
    def transfer_playback(self, device_id: str, force_play: bool = True) -> ActionResult: ...
    def get_album_art_bytes(self, url: str) -> Optional[bytes]: ...


class _ArtCache:
    """Small LRU cache for downloaded album art bytes."""

    def __init__(self, max_items: int = 16) -> None:
        self._max = max_items
        self._data: OrderedDict[str, bytes] = OrderedDict()

    def get(self, url: str) -> Optional[bytes]:
        if url in self._data:
            self._data.move_to_end(url)
            return self._data[url]
        return None

    def put(self, url: str, payload: bytes) -> None:
        self._data[url] = payload
        self._data.move_to_end(url)
        while len(self._data) > self._max:
            self._data.popitem(last=False)


class RealSpotifyService:
    """Live Spotify Web API client backed by Spotipy."""

    is_demo = False

    def __init__(self, settings: Settings) -> None:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth

        self._settings = settings
        self._auth = SpotifyOAuth(
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret,
            redirect_uri=settings.spotify_redirect_uri,
            scope=SPOTIFY_SCOPES,
            cache_path=str(settings.spotify_cache_path),
            open_browser=False,
        )
        self._sp = spotipy.Spotify(auth_manager=self._auth, requests_timeout=8)
        self._art_cache = _ArtCache()
        self._liked_cache: dict[str, tuple[bool, float]] = {}
        self._last_playback = PlaybackState(authenticated=self.is_authenticated())

    def is_authenticated(self) -> bool:
        try:
            token = self._auth.get_cached_token()
            return token is not None
        except Exception:
            return False

    def get_authorize_url(self) -> str:
        return self._auth.get_authorize_url()

    def exchange_code(self, code: str) -> ActionResult:
        try:
            self._auth.get_access_token(code, as_dict=True, check_cache=False)
            return ActionResult(True, "Spotify connected!")
        except Exception as exc:
            logger.exception("OAuth code exchange failed")
            return ActionResult(False, f"Auth failed: {exc}")

    def _check_liked(self, track_id: str) -> bool:
        if not track_id:
            return False
        now = time.monotonic()
        cached = self._liked_cache.get(track_id)
        if cached and (now - cached[1]) < 30.0:
            return cached[0]
        try:
            res = self._sp.current_user_saved_tracks_contains([track_id])
            liked = bool(res and res[0])
            self._liked_cache[track_id] = (liked, now)
            return liked
        except Exception:
            return cached[0] if cached else False

    def _pick_target_device_id(self) -> Optional[str]:
        """Return active device id, or the first available Spotify Connect device."""
        try:
            devices = self.get_devices()
            for dev in devices:
                if dev.is_active:
                    return dev.id
            if devices:
                return devices[0].id
        except Exception:
            pass
        return None

    def get_playback(self) -> PlaybackState:
        if not self.is_authenticated():
            return PlaybackState(
                authenticated=False,
                error_message="Connect Spotify at /auth/login",
            )
        try:
            data = self._sp.current_playback(additional_types="track")
        except Exception as exc:
            logger.warning("Failed to fetch playback: %s", exc)
            state = PlaybackState(
                authenticated=True,
                error_message="Spotify API unreachable",
            )
            return state

        if not data:
            self._last_playback = PlaybackState(authenticated=True, is_playing=False)
            return self._last_playback

        item = data.get("item") or {}
        device = data.get("device") or {}
        artists = ", ".join(a.get("name", "") for a in item.get("artists", []) if a.get("name"))
        album = item.get("album") or {}
        images = album.get("images") or []
        art_url = ""
        if images:
            # Prefer a medium-large image (~300-640px) for fast download and sharp dithering
            art_url = images[0].get("url", "")
            for img in images:
                if 250 <= (img.get("width") or 0) <= 640:
                    art_url = img.get("url", art_url)
                    break

        track_id = item.get("id") or ""
        state = PlaybackState(
            is_playing=bool(data.get("is_playing")),
            track_id=track_id,
            track_uri=item.get("uri") or "",
            track_name=item.get("name") or "",
            artist_name=artists,
            album_name=album.get("name") or "",
            album_art_url=art_url,
            progress_ms=int(data.get("progress_ms") or 0),
            duration_ms=int(item.get("duration_ms") or 0),
            shuffle_state=bool(data.get("shuffle_state")),
            volume_percent=device.get("volume_percent"),
            device_id=device.get("id") or "",
            device_name=device.get("name") or "",
            device_type=device.get("type") or "",
            is_liked=self._check_liked(track_id),
            authenticated=True,
        )
        self._last_playback = state
        return state

    def toggle_play_pause(self) -> ActionResult:
        if not self.is_authenticated():
            return ActionResult(False, "Connect Spotify first")
        try:
            pb = self.get_playback()
            if pb.is_playing:
                self._sp.pause_playback(device_id=pb.device_id or None)
                return ActionResult(True, "Paused")
            device_id = pb.device_id or self._pick_target_device_id()
            if not device_id:
                return ActionResult(False, "No Spotify device found")
            self._sp.start_playback(device_id=device_id)
            return ActionResult(True, "Playing")
        except Exception as exc:
            logger.warning("toggle_play_pause error: %s", exc)
            return ActionResult(False, "No active device")

    def next_track(self) -> ActionResult:
        if not self.is_authenticated():
            return ActionResult(False, "Connect Spotify first")
        try:
            self._sp.next_track()
            return ActionResult(True, "Next track")
        except Exception as exc:
            logger.warning("next_track error: %s", exc)
            return ActionResult(False, "Could not skip track")

    def prev_track(self) -> ActionResult:
        if not self.is_authenticated():
            return ActionResult(False, "Connect Spotify first")
        try:
            self._sp.previous_track()
            return ActionResult(True, "Previous track")
        except Exception as exc:
            logger.warning("prev_track error: %s", exc)
            return ActionResult(False, "Could not go back")

    def adjust_volume(self, delta: int) -> ActionResult:
        if not self.is_authenticated():
            return ActionResult(False, "Connect Spotify first")
        try:
            pb = self.get_playback()
            current = pb.volume_percent if pb.volume_percent is not None else 50
            target = max(0, min(100, current + delta))
            self._sp.volume(target, device_id=pb.device_id or None)
            return ActionResult(True, f"Volume {target}%")
        except Exception as exc:
            logger.warning("adjust_volume error: %s", exc)
            return ActionResult(False, "Device volume unavailable")

    def toggle_shuffle(self) -> ActionResult:
        if not self.is_authenticated():
            return ActionResult(False, "Connect Spotify first")
        try:
            pb = self.get_playback()
            new_state = not pb.shuffle_state
            self._sp.shuffle(new_state, device_id=pb.device_id or None)
            return ActionResult(True, f"Shuffle {'ON' if new_state else 'OFF'}")
        except Exception as exc:
            logger.warning("toggle_shuffle error: %s", exc)
            return ActionResult(False, "Could not toggle shuffle")

    def toggle_like(self) -> ActionResult:
        if not self.is_authenticated():
            return ActionResult(False, "Connect Spotify first")
        try:
            pb = self.get_playback()
            if not pb.track_id:
                return ActionResult(False, "No track playing")
            if pb.is_liked:
                self._sp.current_user_saved_tracks_delete([pb.track_id])
                self._liked_cache[pb.track_id] = (False, time.monotonic())
                return ActionResult(True, "Removed from Liked")
            self._sp.current_user_saved_tracks_add([pb.track_id])
            self._liked_cache[pb.track_id] = (True, time.monotonic())
            return ActionResult(True, "Added to Liked")
        except Exception as exc:
            logger.warning("toggle_like error: %s", exc)
            return ActionResult(False, "Could not update Like")

    def seek_track(self, position_ms: int) -> ActionResult:
        if not self.is_authenticated():
            return ActionResult(False, "Connect Spotify first")
        try:
            pb = self.get_playback()
            target = max(0, int(position_ms))
            if pb.duration_ms > 0:
                target = min(target, max(0, pb.duration_ms - 1000))
            self._sp.seek_track(target, device_id=pb.device_id or None)
            mins, secs = divmod(target // 1000, 60)
            return ActionResult(True, f"Seek {mins}:{secs:02d}")
        except Exception as exc:
            logger.warning("seek_track error: %s", exc)
            return ActionResult(False, "Could not seek track")

    def get_saved_albums(self, limit: int = 30) -> list[MediaItem]:
        if not self.is_authenticated():
            return []
        try:
            res = self._sp.current_user_saved_albums(limit=limit) or {}
            items: list[MediaItem] = []
            for entry in res.get("items", []):
                alb = entry.get("album") or {}
                artists = ", ".join(a.get("name", "") for a in alb.get("artists", []))
                images = alb.get("images") or []
                items.append(
                    MediaItem(
                        id=alb.get("id", ""),
                        uri=alb.get("uri", ""),
                        title=alb.get("name", "Unknown Album"),
                        subtitle=artists,
                        kind="album",
                        image_url=images[-1].get("url", "") if images else "",
                    )
                )
            return items
        except Exception as exc:
            logger.warning("get_saved_albums error: %s", exc)
            return []

    def get_album_tracks(self, album_id: str, album_uri: str = "") -> list[MediaItem]:
        if not self.is_authenticated() or not album_id:
            return []
        try:
            res = self._sp.album_tracks(album_id, limit=50) or {}
            items: list[MediaItem] = []
            for tr in res.get("items", []):
                artists = ", ".join(a.get("name", "") for a in tr.get("artists", []))
                num = tr.get("track_number", "")
                title = f"{num}. {tr.get('name', 'Unknown')}" if num else tr.get("name", "Unknown")
                items.append(
                    MediaItem(
                        id=tr.get("id", ""),
                        uri=tr.get("uri", ""),
                        title=title,
                        subtitle=artists,
                        kind="track",
                        context_uri=album_uri,
                    )
                )
            return items
        except Exception as exc:
            logger.warning("get_album_tracks error: %s", exc)
            return []

    def get_liked_songs(self, limit: int = 30) -> list[MediaItem]:
        if not self.is_authenticated():
            return []
        try:
            res = self._sp.current_user_saved_tracks(limit=limit) or {}
            items: list[MediaItem] = []
            for entry in res.get("items", []):
                tr = entry.get("track") or {}
                artists = ", ".join(a.get("name", "") for a in tr.get("artists", []))
                items.append(
                    MediaItem(
                        id=tr.get("id", ""),
                        uri=tr.get("uri", ""),
                        title=tr.get("name", "Unknown Track"),
                        subtitle=artists,
                        kind="track",
                    )
                )
            return items
        except Exception as exc:
            logger.warning("get_liked_songs error: %s", exc)
            return []

    def get_playlists(self, limit: int = 30) -> list[MediaItem]:
        if not self.is_authenticated():
            return []
        try:
            res = self._sp.current_user_playlists(limit=limit) or {}
            items: list[MediaItem] = []
            for pl in res.get("items", []):
                if not pl:
                    continue
                tracks_obj = pl.get("tracks") or pl.get("items") or {}
                total = tracks_obj.get("total") if isinstance(tracks_obj, dict) else None
                owner = (pl.get("owner") or {}).get("display_name", "")
                if total and total > 0:
                    sub = f"{owner} · {total} tracks" if owner else f"{total} tracks"
                else:
                    sub = owner or "Playlist"
                items.append(
                    MediaItem(
                        id=pl.get("id", ""),
                        uri=pl.get("uri", ""),
                        title=pl.get("name", "Untitled Playlist"),
                        subtitle=sub,
                        kind="playlist",
                    )
                )
            return items
        except Exception as exc:
            logger.warning("get_playlists error: %s", exc)
            return []

    def get_playlist_tracks(self, playlist_id: str, playlist_uri: str = "") -> list[MediaItem]:
        if not self.is_authenticated() or not playlist_id:
            return []
        try:
            res = self._sp.playlist_items(playlist_id, limit=50, additional_types=("track",)) or {}
            items: list[MediaItem] = []
            for idx, entry in enumerate(res.get("items", [])):
                tr = (entry or {}).get("track") or {}
                if not tr or not tr.get("uri"):
                    continue
                artists = ", ".join(a.get("name", "") for a in tr.get("artists", []) if a.get("name"))
                items.append(
                    MediaItem(
                        id=tr.get("id", ""),
                        uri=tr.get("uri", ""),
                        title=f"{idx + 1}. {tr.get('name', 'Unknown Track')}",
                        subtitle=artists,
                        kind="track",
                        context_uri=playlist_uri,
                    )
                )
            return items
        except Exception as exc:
            logger.warning("get_playlist_tracks error: %s", exc)
            return []

    def get_recently_played(self, limit: int = 20) -> list[MediaItem]:
        if not self.is_authenticated():
            return []
        try:
            res = self._sp.current_user_recently_played(limit=limit) or {}
            seen: set[str] = set()
            items: list[MediaItem] = []
            for entry in res.get("items", []):
                tr = entry.get("track") or {}
                tid = tr.get("id", "")
                if not tid or tid in seen:
                    continue
                seen.add(tid)
                artists = ", ".join(a.get("name", "") for a in tr.get("artists", []))
                items.append(
                    MediaItem(
                        id=tid,
                        uri=tr.get("uri", ""),
                        title=tr.get("name", "Unknown Track"),
                        subtitle=artists,
                        kind="track",
                    )
                )
            return items
        except Exception as exc:
            logger.warning("get_recently_played error: %s", exc)
            return []

    def get_queue(self) -> tuple[Optional[MediaItem], list[MediaItem]]:
        if not self.is_authenticated():
            return None, []
        try:
            res = self._sp.queue() or {}
            current_raw = res.get("currently_playing")
            current_item: Optional[MediaItem] = None
            if current_raw:
                artists = ", ".join(a.get("name", "") for a in current_raw.get("artists", []))
                current_item = MediaItem(
                    id=current_raw.get("id", ""),
                    uri=current_raw.get("uri", ""),
                    title=current_raw.get("name", ""),
                    subtitle=artists,
                    kind="track",
                )
            upcoming: list[MediaItem] = []
            for tr in res.get("queue", [])[:30]:
                artists = ", ".join(a.get("name", "") for a in tr.get("artists", []))
                upcoming.append(
                    MediaItem(
                        id=tr.get("id", ""),
                        uri=tr.get("uri", ""),
                        title=tr.get("name", "Unknown Track"),
                        subtitle=artists,
                        kind="track",
                    )
                )
            return current_item, upcoming
        except Exception as exc:
            logger.warning("get_queue error: %s", exc)
            return None, []

    def get_devices(self) -> list[DeviceItem]:
        if not self.is_authenticated():
            return []
        try:
            res = self._sp.devices() or {}
            return [
                DeviceItem(
                    id=d.get("id", ""),
                    name=d.get("name", "Unknown Device"),
                    device_type=d.get("type", "Speaker"),
                    is_active=bool(d.get("is_active")),
                    volume_percent=d.get("volume_percent"),
                )
                for d in res.get("devices", [])
                if d.get("id")
            ]
        except Exception as exc:
            logger.warning("get_devices error: %s", exc)
            return []

    def play_item(self, item: MediaItem) -> ActionResult:
        if not self.is_authenticated():
            return ActionResult(False, "Connect Spotify first")
        try:
            device_id = self._last_playback.device_id or self._pick_target_device_id()
            if not device_id:
                return ActionResult(False, "No Spotify device — open Spotify on a device")
            if item.kind in ("album", "playlist"):
                self._sp.start_playback(device_id=device_id, context_uri=item.uri)
            elif item.context_uri:
                self._sp.start_playback(
                    device_id=device_id,
                    context_uri=item.context_uri,
                    offset={"uri": item.uri},
                )
            else:
                self._sp.start_playback(device_id=device_id, uris=[item.uri])
            return ActionResult(True, f"Playing {item.title}")
        except Exception as exc:
            logger.warning("play_item error: %s", exc)
            return ActionResult(False, "Could not start playback")

    def transfer_playback(self, device_id: str, force_play: bool = True) -> ActionResult:
        if not self.is_authenticated():
            return ActionResult(False, "Connect Spotify first")
        try:
            self._sp.transfer_playback(device_id=device_id, force_play=force_play)
            return ActionResult(True, "Switched device")
        except Exception as exc:
            logger.warning("transfer_playback error: %s", exc)
            return ActionResult(False, "Could not switch device")

    def get_album_art_bytes(self, url: str) -> Optional[bytes]:
        if not url:
            return None
        cached = self._art_cache.get(url)
        if cached is not None:
            return cached
        try:
            import requests

            resp = requests.get(url, timeout=6)
            resp.raise_for_status()
            self._art_cache.put(url, resp.content)
            return resp.content
        except Exception as exc:
            logger.warning("Failed to download album art: %s", exc)
            return None


@dataclass
class _DemoTrack:
    id: str
    uri: str
    title: str
    artist: str
    album: str
    album_id: str
    duration_ms: int
    art_seed: int
    liked: bool = False


class DemoSpotifyService:
    """In-memory mock Spotify service for testing UI & Kindle without credentials."""

    is_demo = True

    def __init__(self) -> None:
        self._tracks: list[_DemoTrack] = [
            _DemoTrack("t1", "spotify:track:t1", "Midnight City", "M83", "Hurry Up, We're Dreaming", "alb1", 243000, 1, True),
            _DemoTrack("t2", "spotify:track:t2", "Instant Crush", "Daft Punk, Julian Casablancas", "Random Access Memories", "alb2", 337000, 2, True),
            _DemoTrack("t3", "spotify:track:t3", "Everything In Its Right Place", "Radiohead", "Kid A", "alb3", 251000, 3, False),
            _DemoTrack("t4", "spotify:track:t4", "Nude", "Radiohead", "In Rainbows", "alb4", 255000, 4, True),
            _DemoTrack("t5", "spotify:track:t5", "Resonance", "HOME", "Odyssey", "alb5", 212000, 5, True),
            _DemoTrack("t6", "spotify:track:t6", "Give Life Back to Music", "Daft Punk", "Random Access Memories", "alb2", 274000, 2, False),
            _DemoTrack("t7", "spotify:track:t7", "Wait", "M83", "Hurry Up, We're Dreaming", "alb1", 343000, 1, False),
            _DemoTrack("t8", "spotify:track:t8", "Weird Fishes/Arpeggi", "Radiohead", "In Rainbows", "alb4", 318000, 4, True),
        ]
        self._albums: list[MediaItem] = [
            MediaItem("alb1", "spotify:album:alb1", "Hurry Up, We're Dreaming", "M83", "album"),
            MediaItem("alb2", "spotify:album:alb2", "Random Access Memories", "Daft Punk", "album"),
            MediaItem("alb3", "spotify:album:alb3", "Kid A", "Radiohead", "album"),
            MediaItem("alb4", "spotify:album:alb4", "In Rainbows", "Radiohead", "album"),
            MediaItem("alb5", "spotify:album:alb5", "Odyssey", "HOME", "album"),
        ]
        self._playlists: list[MediaItem] = [
            MediaItem("pl1", "spotify:playlist:pl1", "Late Night Desk Focus", "You · 24 tracks", "playlist"),
            MediaItem("pl2", "spotify:playlist:pl2", "Synthwave Classics", "You · 42 tracks", "playlist"),
            MediaItem("pl3", "spotify:playlist:pl3", "Rainy Sunday Vinyl", "You · 18 tracks", "playlist"),
            MediaItem("pl4", "spotify:playlist:pl4", "Deep Work Ambient", "You · 31 tracks", "playlist"),
        ]
        self._devices: list[DeviceItem] = [
            DeviceItem("dev1", "Desk Speakers (Pi)", "Speaker", True, 65),
            DeviceItem("dev2", "MacBook Pro", "Computer", False, 50),
            DeviceItem("dev3", "Pixel Phone", "Smartphone", False, 80),
        ]
        self._index = 0
        self._is_playing = True
        self._shuffle = False
        self._progress_ms = 74000
        self._last_tick = time.monotonic()

    def is_authenticated(self) -> bool:
        return True

    def get_authorize_url(self) -> str:
        return "/preview"

    def exchange_code(self, code: str) -> ActionResult:
        return ActionResult(True, "Demo mode active")

    def _advance_clock(self) -> None:
        now = time.monotonic()
        elapsed = int((now - self._last_tick) * 1000)
        self._last_tick = now
        if self._is_playing and elapsed > 0:
            self._progress_ms += elapsed
            cur = self._tracks[self._index]
            if self._progress_ms >= cur.duration_ms:
                self._index = (self._index + 1) % len(self._tracks)
                self._progress_ms = 0

    def _active_device(self) -> DeviceItem:
        for d in self._devices:
            if d.is_active:
                return d
        return self._devices[0]

    def get_playback(self) -> PlaybackState:
        self._advance_clock()
        tr = self._tracks[self._index]
        dev = self._active_device()
        return PlaybackState(
            is_playing=self._is_playing,
            track_id=tr.id,
            track_uri=tr.uri,
            track_name=tr.title,
            artist_name=tr.artist,
            album_name=tr.album,
            album_art_url=f"demo://art/{tr.art_seed}",
            progress_ms=self._progress_ms,
            duration_ms=tr.duration_ms,
            shuffle_state=self._shuffle,
            volume_percent=dev.volume_percent,
            device_id=dev.id,
            device_name=dev.name,
            device_type=dev.device_type,
            is_liked=tr.liked,
            authenticated=True,
        )

    def toggle_play_pause(self) -> ActionResult:
        self._advance_clock()
        self._is_playing = not self._is_playing
        return ActionResult(True, "Playing" if self._is_playing else "Paused")

    def next_track(self) -> ActionResult:
        self._index = (self._index + 1) % len(self._tracks)
        self._progress_ms = 0
        self._is_playing = True
        self._last_tick = time.monotonic()
        return ActionResult(True, f"Next: {self._tracks[self._index].title}")

    def prev_track(self) -> ActionResult:
        self._index = (self._index - 1) % len(self._tracks)
        self._progress_ms = 0
        self._is_playing = True
        self._last_tick = time.monotonic()
        return ActionResult(True, f"Prev: {self._tracks[self._index].title}")

    def adjust_volume(self, delta: int) -> ActionResult:
        dev = self._active_device()
        cur = dev.volume_percent if dev.volume_percent is not None else 50
        dev.volume_percent = max(0, min(100, cur + delta))
        return ActionResult(True, f"Volume {dev.volume_percent}%")

    def toggle_shuffle(self) -> ActionResult:
        self._shuffle = not self._shuffle
        return ActionResult(True, f"Shuffle {'ON' if self._shuffle else 'OFF'}")

    def toggle_like(self) -> ActionResult:
        tr = self._tracks[self._index]
        tr.liked = not tr.liked
        return ActionResult(True, "Added to Liked" if tr.liked else "Removed from Liked")

    def seek_track(self, position_ms: int) -> ActionResult:
        cur = self._tracks[self._index]
        self._progress_ms = max(0, min(cur.duration_ms - 1000, int(position_ms)))
        self._last_tick = time.monotonic()
        mins, secs = divmod(self._progress_ms // 1000, 60)
        return ActionResult(True, f"Seek {mins}:{secs:02d}")

    def get_saved_albums(self, limit: int = 30) -> list[MediaItem]:
        return self._albums[:limit]

    def get_album_tracks(self, album_id: str, album_uri: str = "") -> list[MediaItem]:
        tracks = [t for t in self._tracks if t.album_id == album_id]
        return [
            MediaItem(
                id=t.id,
                uri=t.uri,
                title=f"{i + 1}. {t.title}",
                subtitle=t.artist,
                kind="track",
                context_uri=album_uri,
            )
            for i, t in enumerate(tracks)
        ]

    def get_liked_songs(self, limit: int = 30) -> list[MediaItem]:
        return [
            MediaItem(t.id, t.uri, t.title, t.artist, "track")
            for t in self._tracks
            if t.liked
        ][:limit]

    def get_playlists(self, limit: int = 30) -> list[MediaItem]:
        return self._playlists[:limit]

    def get_playlist_tracks(self, playlist_id: str, playlist_uri: str = "") -> list[MediaItem]:
        return [
            MediaItem(
                id=t.id,
                uri=t.uri,
                title=f"{i + 1}. {t.title}",
                subtitle=t.artist,
                kind="track",
                context_uri=playlist_uri,
            )
            for i, t in enumerate(self._tracks)
        ]

    def get_recently_played(self, limit: int = 20) -> list[MediaItem]:
        return [
            MediaItem(t.id, t.uri, t.title, t.artist, "track")
            for t in reversed(self._tracks)
        ][:limit]

    def get_queue(self) -> tuple[Optional[MediaItem], list[MediaItem]]:
        cur = self._tracks[self._index]
        current_item = MediaItem(cur.id, cur.uri, cur.title, cur.artist, "track")
        upcoming = []
        for offset in range(1, len(self._tracks)):
            t = self._tracks[(self._index + offset) % len(self._tracks)]
            upcoming.append(MediaItem(t.id, t.uri, t.title, t.artist, "track"))
        return current_item, upcoming

    def get_devices(self) -> list[DeviceItem]:
        return list(self._devices)

    def play_item(self, item: MediaItem) -> ActionResult:
        if item.kind == "track":
            for i, t in enumerate(self._tracks):
                if t.id == item.id or t.uri == item.uri:
                    self._index = i
                    break
        elif item.kind == "album":
            for i, t in enumerate(self._tracks):
                if t.album_id == item.id:
                    self._index = i
                    break
        else:
            self._index = (self._index + 1) % len(self._tracks)
        self._progress_ms = 0
        self._is_playing = True
        self._last_tick = time.monotonic()
        return ActionResult(True, f"Playing {item.title}")

    def transfer_playback(self, device_id: str, force_play: bool = True) -> ActionResult:
        for d in self._devices:
            d.is_active = d.id == device_id
        if force_play:
            self._is_playing = True
        dev = self._active_device()
        return ActionResult(True, f"Switched to {dev.name}")

    def get_album_art_bytes(self, url: str) -> Optional[bytes]:
        """Generate crisp procedural geometric album cover PNG for demo mode."""
        from PIL import Image, ImageDraw

        seed = 1
        if url.startswith("demo://art/"):
            try:
                seed = int(url.rsplit("/", 1)[-1])
            except ValueError:
                seed = 1

        size = 420
        img = Image.new("L", (size, size), 240)
        draw = ImageDraw.Draw(img)

        # Smooth gradient background
        for y in range(size):
            shade = int(30 + (y / size) * 180) if seed % 2 == 1 else int(210 - (y / size) * 165)
            draw.line([(0, y), (size, y)], fill=shade)

        # Bold geometric centerpiece depending on seed
        cx, cy = size // 2, size // 2
        if seed % 3 == 0:
            for r in range(160, 20, -24):
                fill = 20 if (r // 24) % 2 == 0 else 235
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=0, width=3)
        elif seed % 3 == 1:
            # Retro sun + horizon stripes
            draw.ellipse([cx - 110, cy - 130, cx + 110, cy + 90], fill=245, outline=15, width=4)
            for y in range(cy + 10, size, 18):
                h = max(3, (y - cy) // 14)
                draw.rectangle([0, y, size, y + h], fill=15)
        else:
            # Diagonal modernist bars
            for i in range(-4, 8):
                x0 = i * 65
                draw.polygon([(x0, 0), (x0 + 35, 0), (x0 - 110, size), (x0 - 145, size)], fill=25 if i % 2 == 0 else 225)
            draw.rectangle([cx - 85, cy - 85, cx + 85, cy + 85], fill=245, outline=0, width=6)
            draw.ellipse([cx - 45, cy - 45, cx + 45, cy + 45], fill=20)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def create_spotify_service(settings: Settings) -> SpotifyBackend:
    if settings.demo_mode:
        logger.info("Running in DEMO_MODE (simulated Spotify playback)")
        return DemoSpotifyService()
    logger.info("Running with live Spotify Web API")
    return RealSpotifyService(settings)
