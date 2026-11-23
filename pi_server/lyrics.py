"""Lightweight synced lyrics fetcher backed by LRCLIB (free, no API key required)."""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from .spotify_client import PlaybackState

logger = logging.getLogger("deskbuddy.lyrics")

_LRC_LINE_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")


@dataclass
class LyricsEntry:
    synced: list[tuple[int, str]] = field(default_factory=list)
    plain: list[str] = field(default_factory=list)
    instrumental: bool = False


def parse_lrc(lrc_text: str) -> list[tuple[int, str]]:
    """Parse LRC formatted text into a sorted list of (timestamp_ms, line)."""
    result: list[tuple[int, str]] = []
    for raw_line in (lrc_text or "").splitlines():
        matches = list(_LRC_LINE_RE.finditer(raw_line))
        if not matches:
            continue
        # Handle lines with multiple timestamps like [00:12.00][01:05.00] Chorus
        text = raw_line[matches[-1].end(2) + 1 :].strip()
        if not text:
            continue
        for m in matches:
            mins = int(m.group(1))
            secs = float(m.group(2))
            ms = int(round((mins * 60 + secs) * 1000))
            result.append((ms, text))
    result.sort(key=lambda pair: pair[0])
    return result


def get_synced_window(
    synced: list[tuple[int, str]],
    progress_ms: int,
) -> tuple[str, str, str]:
    """Return (prev_line, current_line, next_line) for the given playback position."""
    if not synced:
        return "", "", ""
    idx = -1
    for i, (ts, _) in enumerate(synced):
        if ts <= progress_ms + 800:  # slight 0.8s lookahead compensates for e-ink poll delay
            idx = i
        else:
            break
    if idx < 0:
        return "", "", synced[0][1]
    prev_line = synced[idx - 1][1] if idx > 0 else ""
    cur_line = synced[idx][1]
    next_line = synced[idx + 1][1] if idx + 1 < len(synced) else ""
    return prev_line, cur_line, next_line


class LyricsService:
    """Non-blocking LRCLIB lyrics cache."""

    def __init__(self, demo_mode: bool = False, max_cache: int = 64) -> None:
        self._demo = demo_mode
        self._max_cache = max_cache
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, LyricsEntry] = OrderedDict()
        self._inflight: set[str] = set()

    def _cache_key(self, pb: PlaybackState) -> str:
        return f"{pb.track_id or pb.track_name}|{pb.artist_name}"

    def get_lyrics(self, pb: PlaybackState) -> Optional[LyricsEntry]:
        if not pb.track_name:
            return None
        key = self._cache_key(pb)

        if self._demo:
            return LyricsEntry(
                synced=[
                    (0, "Waiting in a car"),
                    (15000, "Waiting for a ride in the dark"),
                    (35000, "The night city grows"),
                    (55000, "Look at the horizon glow"),
                    (72000, "Waiting in a car"),
                    (90000, "Waiting for the right time"),
                    (120000, "The city is my church"),
                    (160000, "It wraps me in the sparkling twilight"),
                ]
            )

        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                self._cache.move_to_end(key)
                return entry
            if key in self._inflight:
                return None
            self._inflight.add(key)

        thread = threading.Thread(
            target=self._fetch_worker,
            args=(key, pb.track_name, pb.artist_name, pb.album_name, pb.duration_ms),
            daemon=True,
        )
        thread.start()
        return None

    def _store(self, key: str, entry: LyricsEntry) -> None:
        with self._lock:
            self._cache[key] = entry
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_cache:
                self._cache.popitem(last=False)
            self._inflight.discard(key)

    def _fetch_worker(
        self,
        key: str,
        track_name: str,
        artist_name: str,
        album_name: str,
        duration_ms: int,
    ) -> None:
        entry = LyricsEntry()
        try:
            import requests

            headers = {"User-Agent": "KindleDeskBuddy/1.0 (https://github.com/kindle-deskbuddy)"}
            primary_artist = artist_name.split(",")[0].strip()
            params = {
                "track_name": track_name,
                "artist_name": primary_artist,
            }
            if album_name:
                params["album_name"] = album_name
            if duration_ms > 0:
                params["duration"] = str(round(duration_ms / 1000))

            data = None
            resp = requests.get("https://lrclib.net/api/get", params=params, headers=headers, timeout=4)
            if resp.status_code == 200:
                data = resp.json()
            else:
                # Fallback to fuzzy search if exact album/duration match wasn't found
                sresp = requests.get(
                    "https://lrclib.net/api/search",
                    params={"track_name": track_name, "artist_name": primary_artist},
                    headers=headers,
                    timeout=4,
                )
                if sresp.status_code == 200:
                    results = sresp.json() or []
                    if results:
                        # Prefer a result that has syncedLyrics
                        data = next((r for r in results if r.get("syncedLyrics")), results[0])

            if isinstance(data, dict):
                entry.instrumental = bool(data.get("instrumental"))
                if data.get("syncedLyrics"):
                    entry.synced = parse_lrc(data["syncedLyrics"])
                if data.get("plainLyrics"):
                    entry.plain = [ln.strip() for ln in data["plainLyrics"].splitlines() if ln.strip()]
        except Exception as exc:
            logger.debug("Lyrics fetch failed for %s: %s", track_name, exc)
        finally:
            self._store(key, entry)
