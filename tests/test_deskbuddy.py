"""Unit tests for Kindle Desk Buddy state machine, touch mapping, and renderer."""

from __future__ import annotations

import time
import unittest

from pi_server.config import Settings
from pi_server.spotify_client import DemoSpotifyService
from pi_server.state import BrowseTab, DeskBuddyState, ViewMode, map_touch_to_logical


def _make_settings(idle_timeout: int = 300) -> Settings:
    from pathlib import Path

    return Settings(
        spotify_client_id="",
        spotify_client_secret="",
        spotify_redirect_uri="http://127.0.0.1:8765/auth/callback",
        spotify_cache_path=Path(".test_cache"),
        host="127.0.0.1",
        port=8765,
        demo_mode=True,
        screen_width=800,
        screen_height=600,
        rotation=90,
        idle_timeout_seconds=idle_timeout,
        volume_step=10,
        time_format="24h",
        font_path="",
        font_bold_path="",
    )


class TestTouchMapping(unittest.TestCase):
    def test_rotation_90_cw_inverse(self) -> None:
        # Landscape 800x600 rotated 90 CW -> Portrait 600x800
        # Top-left (0, 0) lands at portrait (599, 0)
        self.assertEqual(map_touch_to_logical(599, 0, 800, 600, 90), (0, 0))
        # Top-right (799, 0) lands at portrait (599, 799)
        self.assertEqual(map_touch_to_logical(599, 799, 800, 600, 90), (799, 0))
        # Bottom-left (0, 599) lands at portrait (0, 0)
        self.assertEqual(map_touch_to_logical(0, 0, 800, 600, 90), (0, 599))

    def test_rotation_270_cw_inverse(self) -> None:
        # Top-left (0, 0) lands at portrait (0, 799)
        self.assertEqual(map_touch_to_logical(0, 799, 800, 600, 270), (0, 0))
        # Top-right (799, 0) lands at portrait (0, 0)
        self.assertEqual(map_touch_to_logical(0, 0, 800, 600, 270), (799, 0))

    def test_rotation_0_identity(self) -> None:
        self.assertEqual(map_touch_to_logical(123, 456, 800, 600, 0), (123, 456))


class TestStateMachine(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = _make_settings(idle_timeout=300)
        self.spotify = DemoSpotifyService()
        self.state = DeskBuddyState(self.settings, self.spotify)

    def test_idle_clock_after_5_minutes_and_resume_on_play(self) -> None:
        pb = self.state.refresh_playback()
        self.assertTrue(pb.is_playing)
        self.assertEqual(self.state.view, ViewMode.NOW_PLAYING)

        # Pause music and simulate 5 minutes passing
        self.spotify.toggle_play_pause()
        self.state.last_playing_at = time.monotonic() - 305
        self.state.refresh_playback()
        self.assertEqual(self.state.view, ViewMode.IDLE_CLOCK)

        # Selecting an item from the idle clock screen resumes playback -> Now Playing
        albums = self.state.get_browse_items(BrowseTab.ALBUMS)
        self.assertTrue(len(albums) > 0)
        res = self.state._execute_action("play_item", {"item": albums[0]})
        self.assertTrue(res["ok"])
        self.assertEqual(self.state.view, ViewMode.NOW_PLAYING)
        self.assertTrue(self.state.playback.is_playing)

    def test_playback_controls(self) -> None:
        self.state.refresh_playback()
        orig_vol = self.state.playback.volume_percent or 50
        self.state._execute_action("vol_up", {})
        self.assertEqual(self.state.playback.volume_percent, min(100, orig_vol + 10))
        self.state._execute_action("vol_down", {})
        self.assertEqual(self.state.playback.volume_percent, orig_vol)

        orig_shuffle = self.state.playback.shuffle_state
        self.state._execute_action("shuffle", {})
        self.assertEqual(self.state.playback.shuffle_state, not orig_shuffle)

        orig_liked = self.state.playback.is_liked
        self.state._execute_action("like", {})
        self.assertEqual(self.state.playback.is_liked, not orig_liked)

        orig_track = self.state.playback.track_id
        self.state._execute_action("next", {})
        self.assertNotEqual(self.state.playback.track_id, orig_track)

    def test_devices_and_queue(self) -> None:
        current, upcoming = self.state.get_queue_data()
        self.assertIsNotNone(current)
        self.assertTrue(len(upcoming) > 0)

        devices = self.state.get_devices_data()
        self.assertTrue(len(devices) >= 2)
        target = next(d for d in devices if not d.is_active)
        res = self.state._execute_action("select_device", {"device_id": target.id})
        self.assertTrue(res["ok"])
        self.assertEqual(self.state.playback.device_id, target.id)

    def test_album_drilldown(self) -> None:
        albums = self.state.get_browse_items(BrowseTab.ALBUMS)
        self.state._execute_action("open_album", {"item": albums[0]})
        self.assertEqual(self.state.view, ViewMode.ALBUM_DETAIL)
        self.assertTrue(len(self.state.selected_album_tracks) > 0)


class TestRendererOptional(unittest.TestCase):
    def test_render_all_views_if_pillow_installed(self) -> None:
        try:
            import io
            from PIL import Image
            from pi_server.renderer import DeskBuddyRenderer
        except ImportError:
            self.skipTest("Pillow not installed in this environment")

        settings = _make_settings()
        spotify = DemoSpotifyService()
        state = DeskBuddyState(settings, spotify)
        state.update_battery(82, True)
        state.refresh_playback()
        renderer = DeskBuddyRenderer(settings)

        for mode in (ViewMode.NOW_PLAYING, ViewMode.IDLE_CLOCK, ViewMode.BROWSE, ViewMode.QUEUE, ViewMode.DEVICES):
            state.view = mode
            png_bytes, digest, _ = renderer.render(state, 600, 800, rotation_cw=90)
            self.assertTrue(len(digest) == 12)
            im = Image.open(io.BytesIO(png_bytes))
            # Rotated 90 CW back to Kindle portrait framebuffer 600x800
            self.assertEqual(im.size, (600, 800))
            self.assertTrue(len(state.hitboxes) > 0)


if __name__ == "__main__":
    unittest.main()
