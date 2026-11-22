"""Crisp grayscale e-ink Pillow renderer for Kindle 10th Gen (landscape)."""

from __future__ import annotations

import hashlib
import io
import math
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import Settings
from .spotify_client import DeviceItem, MediaItem, PlaybackState
from .state import BrowseTab, DeskBuddyState, Hitbox, ViewMode

# E-ink palette shades (8-bit grayscale)
WHITE = 255
LIGHT_GRAY = 232
MID_GRAY = 165
DARK_GRAY = 90
BLACK = 0

_REGULAR_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
]

_BOLD_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
]


def _find_font_file(preferred: str, candidates: list[str]) -> Optional[str]:
    if preferred and Path(preferred).is_file():
        return preferred
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


class _FontKit:
    def __init__(self, regular_path: Optional[str], bold_path: Optional[str]) -> None:
        self._regular = regular_path
        self._bold = bold_path or regular_path
        self._cache: dict[tuple[bool, int], ImageFont.ImageFont] = {}

    def get(self, size: int, bold: bool = False) -> ImageFont.ImageFont:
        size = max(10, int(size))
        key = (bold, size)
        if key not in self._cache:
            path = self._bold if bold else self._regular
            if path:
                self._cache[key] = ImageFont.truetype(path, size)
            else:
                self._cache[key] = ImageFont.load_default()
        return self._cache[key]


def _format_ms(ms: int) -> str:
    total_sec = max(0, ms // 1000)
    minutes, seconds = divmod(total_sec, 60)
    return f"{minutes}:{seconds:02d}"


def _format_clock(time_format: str) -> str:
    now = datetime.now()
    if time_format == "12h":
        return now.strftime("%I:%M %p").lstrip("0")
    return now.strftime("%H:%M")


def _format_date() -> str:
    now = datetime.now()
    return now.strftime("%a, %b %d").replace(" 0", " ")


def _truncate_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "..."
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid].rstrip() + ellipsis, font=font) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ellipsis if lo > 0 else ellipsis


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 2,
) -> list[str]:
    if not text:
        return [""]
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
                current = word
            else:
                lines.append(_truncate_text(draw, word, font, max_width))
                current = ""
            if len(lines) == max_lines - 1:
                break
    if current:
        remaining_idx = sum(len(line.split()) for line in lines)
        rest = " ".join(words[remaining_idx:]) if len(lines) == max_lines - 1 else current
        lines.append(_truncate_text(draw, rest, font, max_width))
    return lines[:max_lines] or [""]


def _dither_eink_art(raw_bytes: bytes, size: int) -> Optional[Image.Image]:
    """Convert album cover bytes into a crisp 16-level grayscale dithered image."""
    try:
        img = Image.open(io.BytesIO(raw_bytes)).convert("L")
        img = ImageOps.fit(img, (size, size), method=Image.Resampling.LANCZOS)
        img = ImageOps.autocontrast(img, cutoff=1)
        # Quantize with Floyd-Steinberg dithering into 16 clean e-ink shades
        pal_img = Image.new("P", (1, 1))
        palette: list[int] = []
        for i in range(16):
            shade = int(round(i * 255 / 15))
            palette.extend([shade, shade, shade])
        palette.extend([0] * (768 - len(palette)))
        pal_img.putpalette(palette)
        dithered = img.convert("RGB").quantize(palette=pal_img, dither=Image.Dither.FLOYDSTEINBERG)
        return dithered.convert("L")
    except Exception:
        return None


class DeskBuddyRenderer:
    """Renders the full Kindle Desk Buddy UI into a grayscale PNG."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        reg = _find_font_file(settings.font_path, _REGULAR_FONT_CANDIDATES)
        bold = _find_font_file(settings.font_bold_path, _BOLD_FONT_CANDIDATES)
        self.fonts = _FontKit(reg, bold)

    def render(
        self,
        state: DeskBuddyState,
        width: int,
        height: int,
        rotation_cw: int = 0,
    ) -> tuple[bytes, str, bool]:
        """Render current state -> (png_bytes, short_hash, full_refresh)."""
        # Ensure landscape logical dimensions
        logical_w = max(width, height)
        logical_h = min(width, height)
        scale = min(logical_w / 800.0, logical_h / 600.0)

        def s(v: float) -> int:
            return int(round(v * scale))

        img = Image.new("L", (logical_w, logical_h), WHITE)
        draw = ImageDraw.Draw(img)
        hitboxes: list[Hitbox] = []

        def add_box(x1: int, y1: int, x2: int, y2: int, action: str, **payload) -> None:
            hitboxes.append(Hitbox(x1, y1, x2, y2, action, payload))

        if state.view == ViewMode.IDLE_CLOCK:
            self._render_idle_clock(img, draw, state, logical_w, logical_h, s, add_box)
        else:
            top_h = s(54)
            self._render_top_bar(draw, state, logical_w, top_h, s, add_box)
            if state.view == ViewMode.NOW_PLAYING:
                self._render_now_playing(img, draw, state, logical_w, logical_h, top_h, s, add_box)
            elif state.view == ViewMode.BROWSE:
                self._render_browse(draw, state, logical_w, logical_h, top_h, s, add_box)
            elif state.view == ViewMode.ALBUM_DETAIL:
                self._render_album_detail(draw, state, logical_w, logical_h, top_h, s, add_box)
            elif state.view == ViewMode.QUEUE:
                self._render_queue(draw, state, logical_w, logical_h, top_h, s, add_box)
            elif state.view == ViewMode.DEVICES:
                self._render_devices(draw, state, logical_w, logical_h, top_h, s, add_box)

        # Transient toast / error banner at bottom-center if present
        banner = state.active_banner() or state.playback.error_message
        if banner:
            self._render_banner(draw, banner, logical_w, logical_h, s)

        state.set_hitboxes(hitboxes, (logical_w, logical_h))
        full_refresh = state.consume_full_refresh_flag()

        # Rotate logical landscape canvas to match Kindle portrait framebuffer
        rot = rotation_cw % 360
        out_img = img
        if rot == 90:
            out_img = img.rotate(-90, expand=True)
        elif rot == 270:
            out_img = img.rotate(90, expand=True)
        elif rot == 180:
            out_img = img.rotate(180)

        buf = io.BytesIO()
        out_img.save(buf, format="PNG", optimize=True)
        png_bytes = buf.getvalue()
        digest = hashlib.sha1(png_bytes).hexdigest()[:12]
        return png_bytes, digest, full_refresh

    # ------------------------------------------------------------------
    # Shared widgets & crisp vector icons
    # ------------------------------------------------------------------
    def _draw_pill_button(
        self,
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        label: str,
        font: ImageFont.ImageFont,
        active: bool = False,
        radius: int = 8,
        outline_width: int = 2,
    ) -> None:
        fill = BLACK if active else WHITE
        text_fill = WHITE if active else BLACK
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=BLACK, width=outline_width)
        cx = (box[0] + box[2]) // 2
        cy = (box[1] + box[3]) // 2
        draw.text((cx, cy), label, font=font, fill=text_fill, anchor="mm")

    def _draw_battery(
        self,
        draw: ImageDraw.ImageDraw,
        right_x: int,
        cy: int,
        battery: Optional[int],
        charging: bool,
        s,
    ) -> int:
        """Draw battery icon + percentage right-aligned to right_x; return left_x."""
        pct_text = f"{battery}%" if battery is not None else "--%"
        font = self.fonts.get(s(17), bold=True)
        tw = int(draw.textlength(pct_text, font=font))
        text_x = right_x - tw
        draw.text((text_x, cy), pct_text, font=font, fill=BLACK, anchor="lm")

        # Battery body
        bw, bh = s(34), s(18)
        bx2 = text_x - s(8)
        bx1 = bx2 - bw
        by1 = cy - bh // 2
        by2 = by1 + bh
        draw.rounded_rectangle([bx1, by1, bx2, by2], radius=s(3), fill=WHITE, outline=BLACK, width=max(1, s(2)))
        # Positive terminal nub
        nub_w = s(3)
        nub_h = s(8)
        draw.rectangle([bx2, cy - nub_h // 2, bx2 + nub_w, cy + nub_h // 2], fill=BLACK)

        if battery is not None:
            pad = max(2, s(3))
            inner_w = max(0, (bx2 - bx1 - 2 * pad) * battery // 100)
            if inner_w > 0:
                draw.rectangle([bx1 + pad, by1 + pad, bx1 + pad + inner_w, by2 - pad], fill=BLACK)

        if charging:
            # Small lightning bolt to the left of the battery
            lx = bx1 - s(10)
            pts = [
                (lx + s(3), cy - s(8)),
                (lx - s(4), cy + s(1)),
                (lx + s(1), cy + s(1)),
                (lx - s(3), cy + s(8)),
                (lx + s(5), cy - s(1)),
                (lx, cy - s(1)),
            ]
            draw.polygon(pts, fill=BLACK)
            return lx - s(8)

        return bx1 - s(8)

    def _draw_play_icon(self, draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, fill: int) -> None:
        pts = [
            (cx - int(r * 0.65), cy - r),
            (cx - int(r * 0.65), cy + r),
            (cx + int(r * 0.95), cy),
        ]
        draw.polygon(pts, fill=fill)

    def _draw_pause_icon(self, draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, fill: int) -> None:
        bar_w = max(4, int(r * 0.45))
        gap = max(3, int(r * 0.35))
        draw.rectangle([cx - gap - bar_w, cy - r, cx - gap, cy + r], fill=fill)
        draw.rectangle([cx + gap, cy - r, cx + gap + bar_w, cy + r], fill=fill)

    def _draw_prev_icon(self, draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, fill: int) -> None:
        bar_w = max(3, int(r * 0.25))
        draw.rectangle([cx - r, cy - r, cx - r + bar_w, cy + r], fill=fill)
        pts = [(cx - r + bar_w + 2, cy), (cx + r, cy - r), (cx + r, cy + r)]
        draw.polygon(pts, fill=fill)

    def _draw_next_icon(self, draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, fill: int) -> None:
        bar_w = max(3, int(r * 0.25))
        draw.rectangle([cx + r - bar_w, cy - r, cx + r, cy + r], fill=fill)
        pts = [(cx + r - bar_w - 2, cy), (cx - r, cy - r), (cx - r, cy + r)]
        draw.polygon(pts, fill=fill)

    def _draw_heart_icon(
        self,
        draw: ImageDraw.ImageDraw,
        cx: int,
        cy: int,
        r: int,
        filled: bool,
        color: int,
    ) -> None:
        # Clean heart shape built from two circles + triangle
        rad = max(4, int(r * 0.52))
        top_y = cy - int(r * 0.22)
        left_cx = cx - int(r * 0.42)
        right_cx = cx + int(r * 0.42)
        if filled:
            draw.ellipse([left_cx - rad, top_y - rad, left_cx + rad, top_y + rad], fill=color)
            draw.ellipse([right_cx - rad, top_y - rad, right_cx + rad, top_y + rad], fill=color)
            draw.polygon(
                [
                    (left_cx - rad + 1, top_y + int(rad * 0.25)),
                    (right_cx + rad - 1, top_y + int(rad * 0.25)),
                    (cx, cy + r),
                ],
                fill=color,
            )
        else:
            w = max(2, int(r * 0.16))
            draw.arc([left_cx - rad, top_y - rad, left_cx + rad, top_y + rad], 150, 355, fill=color, width=w)
            draw.arc([right_cx - rad, top_y - rad, right_cx + rad, top_y + rad], 185, 30, fill=color, width=w)
            draw.line([(left_cx - int(rad * 0.88), top_y + int(rad * 0.4)), (cx, cy + r)], fill=color, width=w)
            draw.line([(right_cx + int(rad * 0.88), top_y + int(rad * 0.4)), (cx, cy + r)], fill=color, width=w)

    def _draw_shuffle_icon(self, draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: int) -> None:
        w = max(2, int(r * 0.2))
        x1, x2 = cx - r, cx + r
        y1, y2 = cy - int(r * 0.55), cy + int(r * 0.55)
        draw.line([(x1, y1), (cx - r // 4, y1), (cx + r // 4, y2), (x2, y2)], fill=color, width=w)
        draw.line([(x1, y2), (cx - r // 4, y2), (cx + r // 4, y1), (x2, y1)], fill=color, width=w)
        ah = max(3, int(r * 0.35))
        draw.polygon([(x2 + 2, y1), (x2 - ah, y1 - ah), (x2 - ah, y1 + ah)], fill=color)
        draw.polygon([(x2 + 2, y2), (x2 - ah, y2 - ah), (x2 - ah, y2 + ah)], fill=color)

    def _render_top_bar(
        self,
        draw: ImageDraw.ImageDraw,
        state: DeskBuddyState,
        w: int,
        top_h: int,
        s,
        add_box,
    ) -> None:
        draw.rectangle([0, 0, w, top_h], fill=WHITE)
        draw.line([(0, top_h), (w, top_h)], fill=BLACK, width=max(1, s(2)))

        tabs = [
            ("Playing", ViewMode.NOW_PLAYING),
            ("Library", ViewMode.BROWSE),
            ("Queue", ViewMode.QUEUE),
            ("Devices", ViewMode.DEVICES),
        ]
        font = self.fonts.get(s(17), bold=True)
        pad_x = s(14)
        cur_x = s(14)
        btn_y1 = s(9)
        btn_y2 = top_h - s(9)
        for label, mode in tabs:
            tw = int(draw.textlength(label, font=font))
            bw = tw + pad_x * 2
            active = state.view == mode or (
                mode == ViewMode.BROWSE and state.view == ViewMode.ALBUM_DETAIL
            )
            box = (cur_x, btn_y1, cur_x + bw, btn_y2)
            self._draw_pill_button(draw, box, label, font, active=active, radius=s(7))
            add_box(*box, "nav", view=mode.value)
            cur_x += bw + s(10)

        # Right side: Battery + Clock
        cy = top_h // 2
        left_of_batt = self._draw_battery(
            draw, w - s(16), cy, state.kindle_battery, state.kindle_charging, s
        )
        clock_str = _format_clock(self.settings.time_format)
        clock_font = self.fonts.get(s(22), bold=True)
        cw = int(draw.textlength(clock_str, font=clock_font))
        draw.text((left_of_batt - s(12) - cw, cy), clock_str, font=clock_font, fill=BLACK, anchor="lm")

    def _render_banner(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        w: int,
        h: int,
        s,
    ) -> None:
        font = self.fonts.get(s(15), bold=True)
        msg = _truncate_text(draw, text, font, w - s(80))
        tw = int(draw.textlength(msg, font=font))
        bw = tw + s(28)
        bh = s(30)
        x1 = (w - bw) // 2
        y2 = h - s(8)
        y1 = y2 - bh
        draw.rounded_rectangle([x1, y1, x1 + bw, y2], radius=s(8), fill=BLACK)
        draw.text((w // 2, (y1 + y2) // 2), msg, font=font, fill=WHITE, anchor="mm")

    # ------------------------------------------------------------------
    # 1. NOW PLAYING VIEW (Album art as the main focus!)
    # ------------------------------------------------------------------
    def _render_now_playing(
        self,
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        state: DeskBuddyState,
        w: int,
        h: int,
        top_h: int,
        s,
        add_box,
    ) -> None:
        pb: PlaybackState = state.playback
        content_top = top_h + s(18)
        content_h = h - content_top - s(18)

        # Left column: Large square Album Art (centerpiece)
        art_size = min(s(440), content_h, int(w * 0.55))
        art_x = s(22)
        art_y = content_top + (content_h - art_size) // 2

        art_img = None
        if pb.album_art_url:
            raw = state.spotify.get_album_art_bytes(pb.album_art_url)
            if raw:
                art_img = _dither_eink_art(raw, art_size)

        if art_img is not None:
            img.paste(art_img, (art_x, art_y))
        else:
            draw.rectangle([art_x, art_y, art_x + art_size, art_y + art_size], fill=LIGHT_GRAY)
            placeholder_font = self.fonts.get(s(24), bold=True)
            msg = "No Track Playing" if pb.authenticated else "Connect Spotify"
            draw.text(
                (art_x + art_size // 2, art_y + art_size // 2),
                msg,
                font=placeholder_font,
                fill=DARK_GRAY,
                anchor="mm",
            )
        draw.rectangle(
            [art_x, art_y, art_x + art_size, art_y + art_size],
            outline=BLACK,
            width=max(2, s(3)),
        )
        # Tapping the album art toggles play/pause for effortless desk use
        add_box(art_x, art_y, art_x + art_size, art_y + art_size, "play_pause")

        # Right column: Track metadata + complete controls
        rx = art_x + art_size + s(24)
        rw = w - rx - s(20)
        y = art_y

        # Track title (up to 2 lines)
        title_font = self.fonts.get(s(26), bold=True)
        title_text = pb.track_name or ("Nothing Playing" if pb.authenticated else "Spotify Not Linked")
        title_lines = _wrap_text(draw, title_text, title_font, rw, max_lines=2)
        for line in title_lines:
            draw.text((rx, y), line, font=title_font, fill=BLACK)
            y += s(32)

        # Artist & Album
        artist_font = self.fonts.get(s(19), bold=False)
        artist_str = _truncate_text(
            draw,
            pb.artist_name or ("Tap Library to pick music" if pb.authenticated else "Open /auth/login on Pi"),
            artist_font,
            rw,
        )
        y += s(4)
        draw.text((rx, y), artist_str, font=artist_font, fill=BLACK)
        y += s(26)

        if pb.album_name:
            album_font = self.fonts.get(s(15), bold=False)
            draw.text((rx, y), _truncate_text(draw, pb.album_name, album_font, rw), font=album_font, fill=DARK_GRAY)
        y += s(24)

        # Active device pill (tappable shortcut to Devices view)
        dev_label = f"On: {pb.device_name}" if pb.device_name else "Select Device..."
        dev_font = self.fonts.get(s(14), bold=True)
        dev_text = _truncate_text(draw, dev_label, dev_font, rw - s(16))
        dev_box = (rx, y, rx + rw, y + s(28))
        draw.rounded_rectangle(dev_box, radius=s(6), fill=LIGHT_GRAY, outline=BLACK, width=max(1, s(1)))
        draw.text(((dev_box[0] + dev_box[2]) // 2, (dev_box[1] + dev_box[3]) // 2), dev_text, font=dev_font, fill=BLACK, anchor="mm")
        add_box(*dev_box, "nav", view=ViewMode.DEVICES.value)
        y += s(42)

        # Progress bar
        bar_h = s(10)
        draw.rounded_rectangle([rx, y, rx + rw, y + bar_h], radius=s(4), fill=WHITE, outline=BLACK, width=max(1, s(2)))
        if pb.duration_ms > 0:
            ratio = max(0.0, min(1.0, pb.progress_ms / pb.duration_ms))
            fill_w = int(rw * ratio)
            if fill_w > s(4):
                draw.rounded_rectangle([rx, y, rx + fill_w, y + bar_h], radius=s(4), fill=BLACK)
        time_font = self.fonts.get(s(13), bold=False)
        draw.text((rx, y + bar_h + s(5)), _format_ms(pb.progress_ms), font=time_font, fill=DARK_GRAY)
        dur_str = _format_ms(pb.duration_ms)
        draw.text((rx + rw - int(draw.textlength(dur_str, font=time_font)), y + bar_h + s(5)), dur_str, font=time_font, fill=DARK_GRAY)
        y += s(36)

        # Transport Controls: [ Prev ] [ Play / Pause ] [ Next ]
        btn_h = s(68)
        gap = s(12)
        side_w = (rw - 2 * gap) // 3
        center_w = rw - 2 * side_w - 2 * gap

        prev_box = (rx, y, rx + side_w, y + btn_h)
        play_box = (rx + side_w + gap, y, rx + side_w + gap + center_w, y + btn_h)
        next_box = (rx + side_w + gap + center_w + gap, y, rx + rw, y + btn_h)

        draw.rounded_rectangle(prev_box, radius=s(10), fill=WHITE, outline=BLACK, width=max(2, s(2)))
        self._draw_prev_icon(draw, (prev_box[0] + prev_box[2]) // 2, (prev_box[1] + prev_box[3]) // 2, s(14), BLACK)
        add_box(*prev_box, "prev")

        draw.rounded_rectangle(play_box, radius=s(10), fill=BLACK, outline=BLACK, width=max(2, s(2)))
        pcx, pcy = (play_box[0] + play_box[2]) // 2, (play_box[1] + play_box[3]) // 2
        if pb.is_playing:
            self._draw_pause_icon(draw, pcx, pcy, s(16), WHITE)
        else:
            self._draw_play_icon(draw, pcx, pcy, s(16), WHITE)
        add_box(*play_box, "play_pause")

        draw.rounded_rectangle(next_box, radius=s(10), fill=WHITE, outline=BLACK, width=max(2, s(2)))
        self._draw_next_icon(draw, (next_box[0] + next_box[2]) // 2, (next_box[1] + next_box[3]) // 2, s(14), BLACK)
        add_box(*next_box, "next")
        y += btn_h + s(14)

        # Toggle Controls: [ Like ] [ Shuffle ]
        tog_h = s(50)
        half_w = (rw - gap) // 2
        like_box = (rx, y, rx + half_w, y + tog_h)
        shuf_box = (rx + half_w + gap, y, rx + rw, y + tog_h)

        # Like button
        like_fill = BLACK if pb.is_liked else WHITE
        like_fg = WHITE if pb.is_liked else BLACK
        draw.rounded_rectangle(like_box, radius=s(9), fill=like_fill, outline=BLACK, width=max(1, s(2)))
        self._draw_heart_icon(draw, like_box[0] + s(28), (like_box[1] + like_box[3]) // 2, s(11), filled=pb.is_liked, color=like_fg)
        tog_font = self.fonts.get(s(16), bold=True)
        draw.text((like_box[0] + s(48), (like_box[1] + like_box[3]) // 2), "Liked" if pb.is_liked else "Like", font=tog_font, fill=like_fg, anchor="lm")
        add_box(*like_box, "like")

        # Shuffle button
        shuf_fill = BLACK if pb.shuffle_state else WHITE
        shuf_fg = WHITE if pb.shuffle_state else BLACK
        draw.rounded_rectangle(shuf_box, radius=s(9), fill=shuf_fill, outline=BLACK, width=max(1, s(2)))
        self._draw_shuffle_icon(draw, shuf_box[0] + s(28), (shuf_box[1] + shuf_box[3]) // 2, s(11), color=shuf_fg)
        draw.text((shuf_box[0] + s(48), (shuf_box[1] + shuf_box[3]) // 2), "Shuffle", font=tog_font, fill=shuf_fg, anchor="lm")
        add_box(*shuf_box, "shuffle")
        y += tog_h + s(14)

        # Volume row: [ - ] [ Volume Bar + % ] [ + ]
        vol_h = s(48)
        vbtn_w = s(54)
        vmin_box = (rx, y, rx + vbtn_w, y + vol_h)
        vplus_box = (rx + rw - vbtn_w, y, rx + rw, y + vol_h)
        vbar_x1 = rx + vbtn_w + s(10)
        vbar_x2 = rx + rw - vbtn_w - s(10)

        vol_font = self.fonts.get(s(24), bold=True)
        self._draw_pill_button(draw, vmin_box, "-", vol_font, active=False, radius=s(9))
        add_box(*vmin_box, "vol_down")

        self._draw_pill_button(draw, vplus_box, "+", vol_font, active=False, radius=s(9))
        add_box(*vplus_box, "vol_up")

        # Volume meter
        vol_pct = pb.volume_percent if pb.volume_percent is not None else 0
        vol_label = f"Vol {pb.volume_percent}%" if pb.volume_percent is not None else "Vol --"
        vl_font = self.fonts.get(s(14), bold=True)
        draw.text(((vbar_x1 + vbar_x2) // 2, y + s(10)), vol_label, font=vl_font, fill=BLACK, anchor="mm")
        m_y1 = y + s(24)
        m_y2 = y + s(38)
        draw.rounded_rectangle([vbar_x1, m_y1, vbar_x2, m_y2], radius=s(4), fill=WHITE, outline=BLACK, width=max(1, s(2)))
        if pb.volume_percent is not None and vol_pct > 0:
            fw = int((vbar_x2 - vbar_x1) * vol_pct / 100)
            if fw > s(4):
                draw.rounded_rectangle([vbar_x1, m_y1, vbar_x1 + fw, m_y2], radius=s(4), fill=BLACK)

    # ------------------------------------------------------------------
    # 2. IDLE CLOCK VIEW (Clock is main focus + quick song/album selector!)
    # ------------------------------------------------------------------
    def _render_idle_clock(
        self,
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        state: DeskBuddyState,
        w: int,
        h: int,
        s,
        add_box,
    ) -> None:
        # Clean header: Date on left, Devices + Battery on right
        header_y = s(28)
        date_font = self.fonts.get(s(22), bold=True)
        draw.text((s(24), header_y), _format_date(), font=date_font, fill=BLACK, anchor="lm")

        left_of_batt = self._draw_battery(
            draw, w - s(24), header_y, state.kindle_battery, state.kindle_charging, s
        )
        dev_btn_font = self.fonts.get(s(15), bold=True)
        dev_box = (left_of_batt - s(108), s(10), left_of_batt - s(14), s(46))
        self._draw_pill_button(draw, dev_box, "Devices", dev_btn_font, active=False, radius=s(7))
        add_box(*dev_box, "nav", view=ViewMode.DEVICES.value)

        # Hero Clock (the unmistakable centerpiece)
        clock_str = _format_clock(self.settings.time_format)
        clock_size = s(136) if self.settings.time_format == "24h" else s(108)
        clock_font = self.fonts.get(clock_size, bold=True)
        draw.text((w // 2, s(145)), clock_str, font=clock_font, fill=BLACK, anchor="mm")

        sub_font = self.fonts.get(s(16), bold=False)
        subtitle = "Nothing playing  ·  Pick an album or song below to start"
        if not state.playback.authenticated:
            subtitle = "Connect Spotify at /auth/login to control playback"
        draw.text((w // 2, s(232)), subtitle, font=sub_font, fill=DARK_GRAY, anchor="mm")

        # Divider
        div_y = s(262)
        draw.line([(s(24), div_y), (w - s(24), div_y)], fill=BLACK, width=max(1, s(2)))

        # Quick Song & Album Selector right on the Idle Clock screen
        tab_y1 = div_y + s(14)
        tab_y2 = tab_y1 + s(38)
        tab_font = self.fonts.get(s(16), bold=True)
        idle_tabs = [
            ("Albums", BrowseTab.ALBUMS),
            ("Liked Songs", BrowseTab.SONGS),
            ("Playlists", BrowseTab.PLAYLISTS),
        ]
        cur_x = s(24)
        for label, tab in idle_tabs:
            bw = int(draw.textlength(label, font=tab_font)) + s(28)
            box = (cur_x, tab_y1, cur_x + bw, tab_y2)
            self._draw_pill_button(draw, box, label, tab_font, active=(state.idle_tab == tab), radius=s(7))
            add_box(*box, "idle_tab", tab=tab.value)
            cur_x += bw + s(10)

        # "Full Library ->" button on the right
        lib_label = "Full Library >"
        lib_w = int(draw.textlength(lib_label, font=tab_font)) + s(28)
        lib_box = (w - s(24) - lib_w, tab_y1, w - s(24), tab_y2)
        self._draw_pill_button(draw, lib_box, lib_label, tab_font, active=False, radius=s(7))
        add_box(*lib_box, "browse_tab", tab=state.idle_tab.value)

        # 2x2 grid of quick-launch cards
        items = state.get_browse_items(state.idle_tab)[:4]
        grid_top = tab_y2 + s(16)
        grid_bottom = h - s(20)
        gap = s(14)
        card_w = (w - s(48) - gap) // 2
        card_h = (grid_bottom - grid_top - gap) // 2

        if not items:
            empty_font = self.fonts.get(s(18), bold=False)
            draw.text((w // 2, (grid_top + grid_bottom) // 2), "No items found in your Spotify library.", font=empty_font, fill=DARK_GRAY, anchor="mm")
            return

        card_title_font = self.fonts.get(s(19), bold=True)
        card_sub_font = self.fonts.get(s(15), bold=False)
        for idx, item in enumerate(items):
            col = idx % 2
            row = idx // 2
            cx1 = s(24) + col * (card_w + gap)
            cy1 = grid_top + row * (card_h + gap)
            cx2 = cx1 + card_w
            cy2 = cy1 + card_h

            draw.rounded_rectangle([cx1, cy1, cx2, cy2], radius=s(10), fill=WHITE, outline=BLACK, width=max(2, s(2)))

            # Play badge on left of card
            badge_r = s(20)
            bcx = cx1 + s(32)
            bcy = (cy1 + cy2) // 2
            draw.ellipse([bcx - badge_r, bcy - badge_r, bcx + badge_r, bcy + badge_r], fill=BLACK)
            self._draw_play_icon(draw, bcx + s(2), bcy, s(9), WHITE)

            tx = cx1 + s(64)
            tw = card_w - s(78)
            draw.text((tx, bcy - s(13)), _truncate_text(draw, item.title, card_title_font, tw), font=card_title_font, fill=BLACK, anchor="lm")
            draw.text((tx, bcy + s(15)), _truncate_text(draw, item.subtitle, card_sub_font, tw), font=card_sub_font, fill=DARK_GRAY, anchor="lm")

            add_box(cx1, cy1, cx2, cy2, "play_item", item=item)

    # ------------------------------------------------------------------
    # 3. BROWSE VIEW (Albums, Liked Songs, Playlists, Recent)
    # ------------------------------------------------------------------
    def _render_browse(
        self,
        draw: ImageDraw.ImageDraw,
        state: DeskBuddyState,
        w: int,
        h: int,
        top_h: int,
        s,
        add_box,
    ) -> None:
        sub_y1 = top_h + s(12)
        sub_y2 = sub_y1 + s(38)
        tab_font = self.fonts.get(s(16), bold=True)
        subtabs = [
            ("Albums", BrowseTab.ALBUMS),
            ("Liked Songs", BrowseTab.SONGS),
            ("Playlists", BrowseTab.PLAYLISTS),
            ("Recent", BrowseTab.RECENT),
        ]
        cur_x = s(18)
        for label, tab in subtabs:
            bw = int(draw.textlength(label, font=tab_font)) + s(26)
            box = (cur_x, sub_y1, cur_x + bw, sub_y2)
            self._draw_pill_button(draw, box, label, tab_font, active=(state.browse_tab == tab), radius=s(7))
            add_box(*box, "browse_tab", tab=tab.value)
            cur_x += bw + s(10)

        # Refresh button on right
        ref_box = (w - s(112), sub_y1, w - s(18), sub_y2)
        self._draw_pill_button(draw, ref_box, "Refresh", tab_font, active=False, radius=s(7))
        add_box(*ref_box, "refresh_list")

        items = state.get_browse_items()
        per_page = 5
        total_pages = max(1, math.ceil(len(items) / per_page))
        page = max(0, min(state.page, total_pages - 1))
        state.page = page
        page_items = items[page * per_page : (page + 1) * per_page]

        list_top = sub_y2 + s(14)
        footer_h = s(54)
        list_h = h - list_top - footer_h - s(10)
        row_gap = s(10)
        row_h = (list_h - row_gap * (per_page - 1)) // per_page

        if not page_items:
            empty_font = self.fonts.get(s(20), bold=False)
            draw.text((w // 2, list_top + list_h // 2), "No items available.", font=empty_font, fill=DARK_GRAY, anchor="mm")
        else:
            title_font = self.fonts.get(s(19), bold=True)
            sub_font = self.fonts.get(s(15), bold=False)
            btn_font = self.fonts.get(s(15), bold=True)

            for i, item in enumerate(page_items):
                ry1 = list_top + i * (row_h + row_gap)
                ry2 = ry1 + row_h
                rx1, rx2 = s(18), w - s(18)
                draw.rounded_rectangle([rx1, ry1, rx2, ry2], radius=s(8), fill=WHITE, outline=BLACK, width=max(1, s(2)))

                cy = (ry1 + ry2) // 2
                if item.kind == "album":
                    # Right-side Play button + main area opens Album Tracks
                    play_w = s(96)
                    play_box = (rx2 - play_w - s(10), ry1 + s(10), rx2 - s(10), ry2 - s(10))
                    self._draw_pill_button(draw, play_box, "Play", btn_font, active=True, radius=s(7))
                    text_max = (play_box[0] - rx1) - s(28)
                    draw.text((rx1 + s(16), cy - s(11)), _truncate_text(draw, item.title, title_font, text_max), font=title_font, fill=BLACK, anchor="lm")
                    draw.text((rx1 + s(16), cy + s(13)), _truncate_text(draw, f"{item.subtitle}  ·  Tap for tracks", sub_font, text_max), font=sub_font, fill=DARK_GRAY, anchor="lm")
                    add_box(rx1, ry1, play_box[0] - s(4), ry2, "open_album", item=item)
                    add_box(*play_box, "play_item", item=item)
                else:
                    text_max = (rx2 - rx1) - s(90)
                    draw.text((rx1 + s(16), cy - s(11)), _truncate_text(draw, item.title, title_font, text_max), font=title_font, fill=BLACK, anchor="lm")
                    draw.text((rx1 + s(16), cy + s(13)), _truncate_text(draw, item.subtitle, sub_font, text_max), font=sub_font, fill=DARK_GRAY, anchor="lm")
                    self._draw_play_icon(draw, rx2 - s(32), cy, s(11), BLACK)
                    add_box(rx1, ry1, rx2, ry2, "play_item", item=item)

        self._render_pagination(draw, page, total_pages, w, h, footer_h, s, add_box)

    # ------------------------------------------------------------------
    # 4. ALBUM DETAIL VIEW (Pick a specific song or play whole album)
    # ------------------------------------------------------------------
    def _render_album_detail(
        self,
        draw: ImageDraw.ImageDraw,
        state: DeskBuddyState,
        w: int,
        h: int,
        top_h: int,
        s,
        add_box,
    ) -> None:
        album = state.selected_album
        header_y1 = top_h + s(12)
        header_y2 = header_y1 + s(44)
        btn_font = self.fonts.get(s(16), bold=True)

        back_box = (s(18), header_y1, s(114), header_y2)
        self._draw_pill_button(draw, back_box, "< Back", btn_font, active=False, radius=s(7))
        add_box(*back_box, "nav", view=ViewMode.BROWSE.value)

        play_box = (w - s(156), header_y1, w - s(18), header_y2)
        self._draw_pill_button(draw, play_box, "Play Album", btn_font, active=True, radius=s(7))
        if album:
            add_box(*play_box, "play_item", item=album)

        title_x = back_box[2] + s(16)
        title_w = play_box[0] - title_x - s(14)
        t_font = self.fonts.get(s(20), bold=True)
        a_font = self.fonts.get(s(14), bold=False)
        draw.text((title_x, header_y1 + s(12)), _truncate_text(draw, album.title if album else "Album", t_font, title_w), font=t_font, fill=BLACK, anchor="lm")
        draw.text((title_x, header_y1 + s(34)), _truncate_text(draw, album.subtitle if album else "", a_font, title_w), font=a_font, fill=DARK_GRAY, anchor="lm")

        tracks = state.selected_album_tracks
        per_page = 5
        total_pages = max(1, math.ceil(len(tracks) / per_page))
        page = max(0, min(state.page, total_pages - 1))
        state.page = page
        page_tracks = tracks[page * per_page : (page + 1) * per_page]

        list_top = header_y2 + s(14)
        footer_h = s(54)
        list_h = h - list_top - footer_h - s(10)
        row_gap = s(10)
        row_h = (list_h - row_gap * (per_page - 1)) // per_page

        row_title_font = self.fonts.get(s(18), bold=True)
        row_sub_font = self.fonts.get(s(14), bold=False)
        for i, tr in enumerate(page_tracks):
            ry1 = list_top + i * (row_h + row_gap)
            ry2 = ry1 + row_h
            rx1, rx2 = s(18), w - s(18)
            draw.rounded_rectangle([rx1, ry1, rx2, ry2], radius=s(8), fill=WHITE, outline=BLACK, width=max(1, s(2)))
            cy = (ry1 + ry2) // 2
            max_tw = (rx2 - rx1) - s(80)
            draw.text((rx1 + s(16), cy - s(10)), _truncate_text(draw, tr.title, row_title_font, max_tw), font=row_title_font, fill=BLACK, anchor="lm")
            draw.text((rx1 + s(16), cy + s(12)), _truncate_text(draw, tr.subtitle, row_sub_font, max_tw), font=row_sub_font, fill=DARK_GRAY, anchor="lm")
            self._draw_play_icon(draw, rx2 - s(30), cy, s(10), BLACK)
            add_box(rx1, ry1, rx2, ry2, "play_item", item=tr)

        self._render_pagination(draw, page, total_pages, w, h, footer_h, s, add_box)

    # ------------------------------------------------------------------
    # 5. QUEUE VIEW
    # ------------------------------------------------------------------
    def _render_queue(
        self,
        draw: ImageDraw.ImageDraw,
        state: DeskBuddyState,
        w: int,
        h: int,
        top_h: int,
        s,
        add_box,
    ) -> None:
        current, upcoming = state.get_queue_data()
        y = top_h + s(14)
        head_font = self.fonts.get(s(18), bold=True)
        btn_font = self.fonts.get(s(15), bold=True)

        # Now Playing summary strip
        np_h = s(56)
        draw.rounded_rectangle([s(18), y, w - s(126), y + np_h], radius=s(8), fill=LIGHT_GRAY, outline=BLACK, width=max(1, s(2)))
        np_label = f"Now: {current.title} — {current.subtitle}" if current else "Nothing currently playing"
        draw.text((s(32), y + np_h // 2), _truncate_text(draw, np_label, head_font, w - s(170)), font=head_font, fill=BLACK, anchor="lm")

        ref_box = (w - s(114), y, w - s(18), y + np_h)
        self._draw_pill_button(draw, ref_box, "Refresh", btn_font, active=False, radius=s(8))
        add_box(*ref_box, "refresh_list")

        y += np_h + s(14)
        sec_font = self.fonts.get(s(16), bold=True)
        draw.text((s(20), y), f"Up Next ({len(upcoming)})", font=sec_font, fill=DARK_GRAY)
        y += s(26)

        per_page = 5
        total_pages = max(1, math.ceil(len(upcoming) / per_page))
        page = max(0, min(state.page, total_pages - 1))
        state.page = page
        page_items = upcoming[page * per_page : (page + 1) * per_page]

        footer_h = s(54)
        list_h = h - y - footer_h - s(10)
        row_gap = s(8)
        row_h = (list_h - row_gap * (per_page - 1)) // per_page

        if not page_items:
            draw.text((w // 2, y + list_h // 2), "Queue is empty.", font=head_font, fill=DARK_GRAY, anchor="mm")
        else:
            t_font = self.fonts.get(s(18), bold=True)
            s_font = self.fonts.get(s(14), bold=False)
            for idx, item in enumerate(page_items):
                num = page * per_page + idx + 1
                ry1 = y + idx * (row_h + row_gap)
                ry2 = ry1 + row_h
                rx1, rx2 = s(18), w - s(18)
                draw.rounded_rectangle([rx1, ry1, rx2, ry2], radius=s(7), fill=WHITE, outline=BLACK, width=max(1, s(1)))
                cy = (ry1 + ry2) // 2
                max_tw = (rx2 - rx1) - s(70)
                draw.text((rx1 + s(16), cy - s(9)), _truncate_text(draw, f"{num}. {item.title}", t_font, max_tw), font=t_font, fill=BLACK, anchor="lm")
                draw.text((rx1 + s(38), cy + s(12)), _truncate_text(draw, item.subtitle, s_font, max_tw - s(22)), font=s_font, fill=DARK_GRAY, anchor="lm")
                self._draw_play_icon(draw, rx2 - s(28), cy, s(9), BLACK)
                add_box(rx1, ry1, rx2, ry2, "play_item", item=item)

        self._render_pagination(draw, page, total_pages, w, h, footer_h, s, add_box)

    # ------------------------------------------------------------------
    # 6. DEVICES VIEW (Spotify Connect device switcher)
    # ------------------------------------------------------------------
    def _render_devices(
        self,
        draw: ImageDraw.ImageDraw,
        state: DeskBuddyState,
        w: int,
        h: int,
        top_h: int,
        s,
        add_box,
    ) -> None:
        y = top_h + s(16)
        head_font = self.fonts.get(s(22), bold=True)
        btn_font = self.fonts.get(s(16), bold=True)
        draw.text((s(22), y + s(18)), "Select Playback Device", font=head_font, fill=BLACK, anchor="lm")

        ref_box = (w - s(122), y, w - s(20), y + s(38))
        self._draw_pill_button(draw, ref_box, "Refresh", btn_font, active=False, radius=s(8))
        add_box(*ref_box, "refresh_list")

        devices: list[DeviceItem] = state.get_devices_data()
        list_top = y + s(54)
        footer_h = s(44)
        list_h = h - list_top - footer_h

        if not devices:
            empty_font = self.fonts.get(s(19), bold=False)
            draw.text(
                (w // 2, list_top + list_h // 2 - s(14)),
                "No Spotify Connect devices found.",
                font=empty_font,
                fill=DARK_GRAY,
                anchor="mm",
            )
            hint_font = self.fonts.get(s(15), bold=False)
            draw.text(
                (w // 2, list_top + list_h // 2 + s(18)),
                "Open Spotify on your phone, PC, or speaker and tap Refresh.",
                font=hint_font,
                fill=DARK_GRAY,
                anchor="mm",
            )
            return

        row_h = s(76)
        row_gap = s(12)
        name_font = self.fonts.get(s(21), bold=True)
        meta_font = self.fonts.get(s(15), bold=False)
        badge_font = self.fonts.get(s(14), bold=True)

        for idx, dev in enumerate(devices[:5]):
            ry1 = list_top + idx * (row_h + row_gap)
            ry2 = ry1 + row_h
            rx1, rx2 = s(22), w - s(22)
            fill = BLACK if dev.is_active else WHITE
            fg = WHITE if dev.is_active else BLACK
            sub_fg = LIGHT_GRAY if dev.is_active else DARK_GRAY

            draw.rounded_rectangle([rx1, ry1, rx2, ry2], radius=s(10), fill=fill, outline=BLACK, width=max(2, s(2)))
            cy = (ry1 + ry2) // 2

            draw.text((rx1 + s(20), cy - s(11)), _truncate_text(draw, dev.name, name_font, rx2 - rx1 - s(170)), font=name_font, fill=fg, anchor="lm")
            vol_txt = f"  ·  Vol {dev.volume_percent}%" if dev.volume_percent is not None else ""
            draw.text((rx1 + s(20), cy + s(15)), f"{dev.device_type}{vol_txt}", font=meta_font, fill=sub_fg, anchor="lm")

            badge_text = "ACTIVE" if dev.is_active else "TAP TO PLAY"
            bw = int(draw.textlength(badge_text, font=badge_font)) + s(22)
            bx1 = rx2 - bw - s(16)
            by1 = cy - s(15)
            by2 = cy + s(15)
            draw.rounded_rectangle(
                [bx1, by1, rx2 - s(16), by2],
                radius=s(6),
                fill=WHITE if dev.is_active else LIGHT_GRAY,
                outline=WHITE if dev.is_active else BLACK,
                width=max(1, s(1)),
            )
            draw.text(((bx1 + rx2 - s(16)) // 2, cy), badge_text, font=badge_font, fill=BLACK, anchor="mm")
            add_box(rx1, ry1, rx2, ry2, "select_device", device_id=dev.id)

    def _render_pagination(
        self,
        draw: ImageDraw.ImageDraw,
        page: int,
        total_pages: int,
        w: int,
        h: int,
        footer_h: int,
        s,
        add_box,
    ) -> None:
        y1 = h - footer_h + s(4)
        y2 = h - s(8)
        font = self.fonts.get(s(16), bold=True)

        prev_box = (s(18), y1, s(148), y2)
        next_box = (w - s(148), y1, w - s(18), y2)

        if page > 0:
            self._draw_pill_button(draw, prev_box, "< Prev", font, active=False, radius=s(7))
            add_box(*prev_box, "page", delta=-1)
        if page + 1 < total_pages:
            self._draw_pill_button(draw, next_box, "Next >", font, active=False, radius=s(7))
            add_box(*next_box, "page", delta=1)

        page_str = f"Page {page + 1} / {total_pages}"
        draw.text((w // 2, (y1 + y2) // 2), page_str, font=font, fill=DARK_GRAY, anchor="mm")
