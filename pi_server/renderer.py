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
from .lyrics import get_synced_lines
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

_CJK_REGULAR_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansJP-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/takao-gothic/TakaoPGothic.ttf",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
]

_CJK_BOLD_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansJP-Bold.otf",
    "/usr/share/fonts/truetype/noto/NotoSansJP-Bold.ttf",
]


def _has_cjk(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        if code >= 0x2E80:
            return True
    return False


def _find_font_file(preferred: str, candidates: list[str]) -> Optional[str]:
    if preferred and Path(preferred).is_file():
        return preferred
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


class _FontKit:
    def __init__(
        self,
        regular_path: Optional[str],
        bold_path: Optional[str],
        cjk_regular_path: Optional[str] = None,
        cjk_bold_path: Optional[str] = None,
    ) -> None:
        self._regular = regular_path
        self._bold = bold_path or regular_path
        self._cjk_regular = cjk_regular_path
        self._cjk_bold = cjk_bold_path or cjk_regular_path
        self._cache: dict[tuple[bool, bool, int], ImageFont.ImageFont] = {}

    def get(self, size: int, bold: bool = False, cjk: bool = False) -> ImageFont.ImageFont:
        size = max(10, int(size))
        use_cjk = cjk and bool(self._cjk_regular)
        key = (bold, use_cjk, size)
        if key not in self._cache:
            if use_cjk:
                path = self._cjk_bold if bold else self._cjk_regular
            else:
                path = self._bold if bold else self._regular
            if path:
                self._cache[key] = ImageFont.truetype(path, size)
            else:
                self._cache[key] = ImageFont.load_default()
        return self._cache[key]

    def for_text(self, text: str, size: int, bold: bool = False) -> ImageFont.ImageFont:
        return self.get(size, bold=bold, cjk=_has_cjk(text))


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
        cjk_reg = _find_font_file("", _CJK_REGULAR_CANDIDATES)
        cjk_bold = _find_font_file("", _CJK_BOLD_CANDIDATES)
        self.fonts = _FontKit(reg, bold, cjk_reg, cjk_bold)

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
            elif state.view == ViewMode.BRIGHTNESS:
                self._render_brightness(draw, state, logical_w, logical_h, top_h, s, add_box)

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

    def _draw_sun_icon(self, draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: int) -> None:
        inner_r = max(3, int(r * 0.48))
        draw.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r], fill=color)
        w = max(2, int(r * 0.16))
        for deg in range(0, 360, 45):
            rad = math.radians(deg)
            x1 = cx + int(math.cos(rad) * (inner_r + max(2, int(r * 0.2))))
            y1 = cy + int(math.sin(rad) * (inner_r + max(2, int(r * 0.2))))
            x2 = cx + int(math.cos(rad) * r)
            y2 = cy + int(math.sin(rad) * r)
            draw.line([(x1, y1), (x2, y2)], fill=color, width=w)

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

        # Right side: Exit button + Battery + Brightness button (+ compact clock on non-NowPlaying views)
        cy = top_h // 2
        exit_w = s(38)
        exit_box = (w - s(12) - exit_w, btn_y1, w - s(12), btn_y2)
        self._draw_pill_button(draw, exit_box, "X", font, active=False, radius=s(7))
        add_box(*exit_box, "exit")

        left_of_batt = self._draw_battery(
            draw, exit_box[0] - s(12), cy, state.kindle_battery, state.kindle_charging, s
        )

        # Brightness button with crisp sun icon
        bright_active = state.view == ViewMode.BRIGHTNESS
        bright_w = s(44)
        bright_box = (left_of_batt - s(8) - bright_w, btn_y1, left_of_batt - s(8), btn_y2)
        draw.rounded_rectangle(
            bright_box,
            radius=s(7),
            fill=BLACK if bright_active else WHITE,
            outline=BLACK,
            width=max(1, s(2)),
        )
        self._draw_sun_icon(
            draw,
            (bright_box[0] + bright_box[2]) // 2,
            cy,
            s(11),
            WHITE if bright_active else BLACK,
        )
        add_box(*bright_box, "toggle_brightness")

        # Show compact clock in top bar on Library/Queue/Devices/Brightness (Now Playing has the big clock)
        if state.view != ViewMode.NOW_PLAYING:
            clock_str = _format_clock(self.settings.time_format)
            clock_font = self.fonts.get(s(21), bold=True)
            cw = int(draw.textlength(clock_str, font=clock_font))
            draw.text((bright_box[0] - s(14) - cw, cy), clock_str, font=clock_font, fill=BLACK, anchor="lm")

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
        margin_x = s(20)
        content_top = top_h + s(12)
        content_bottom = h - s(16)

        # Left column: Big Clock + Date on top, Large square Album Art below
        clock_band_h = s(68)
        avail_art_h = content_bottom - (content_top + clock_band_h + s(10))
        art_size = min(s(440), avail_art_h, int(w * 0.54))
        art_x = margin_x
        art_y = content_top + clock_band_h + s(10)

        # Prominent Clock + Date aligned cleanly with album art width
        clock_str = _format_clock(self.settings.time_format)
        clock_font_size = s(58) if self.settings.time_format == "24h" else s(44)
        clock_font = self.fonts.get(clock_font_size, bold=True)
        clock_cy = content_top + clock_band_h // 2
        draw.text((art_x, clock_cy), clock_str, font=clock_font, fill=BLACK, anchor="lm")

        date_str = _format_date()
        date_font = self.fonts.get(s(20), bold=True)
        dw = int(draw.textlength(date_str, font=date_font))
        draw.text((art_x + art_size - dw, clock_cy + s(6)), date_str, font=date_font, fill=DARK_GRAY, anchor="lm")
        draw.line(
            [(art_x, art_y - s(6)), (art_x + art_size, art_y - s(6))],
            fill=MID_GRAY,
            width=max(1, s(1)),
        )

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

        # Right column: Track metadata + controls grouped tightly at the top, Lyrics at the bottom
        rx = art_x + art_size + s(24)
        rw = w - rx - margin_x
        y = content_top + s(2)

        title_text = pb.track_name or ("Nothing Playing" if pb.authenticated else "Spotify Not Linked")
        title_font = self.fonts.for_text(title_text, s(25), bold=True)
        title_lines = _wrap_text(draw, title_text, title_font, rw, max_lines=2)
        for line in title_lines:
            draw.text((rx, y), line, font=title_font, fill=BLACK)
            y += s(31)

        artist_raw = pb.artist_name or ("Tap Library to pick music" if pb.authenticated else "Open /auth/login on Pi")
        artist_font = self.fonts.for_text(artist_raw, s(19), bold=False)
        artist_str = _truncate_text(draw, artist_raw, artist_font, rw)
        draw.text((rx, y + s(2)), artist_str, font=artist_font, fill=BLACK)
        y += s(26)

        if pb.album_name:
            album_font = self.fonts.for_text(pb.album_name, s(15), bold=False)
            draw.text((rx, y + s(1)), _truncate_text(draw, pb.album_name, album_font, rw), font=album_font, fill=DARK_GRAY)
            y += s(22)

        # Active device pill (tappable shortcut to Devices view)
        y += s(6)
        dev_h = s(30)
        dev_label = f"On: {pb.device_name}" if pb.device_name else "Select Device..."
        dev_font = self.fonts.for_text(dev_label, s(15), bold=True)
        dev_text = _truncate_text(draw, dev_label, dev_font, rw - s(16))
        dev_box = (rx, y, rx + rw, y + dev_h)
        draw.rounded_rectangle(dev_box, radius=s(7), fill=LIGHT_GRAY, outline=BLACK, width=max(1, s(1)))
        draw.text(((dev_box[0] + dev_box[2]) // 2, (dev_box[1] + dev_box[3]) // 2), dev_text, font=dev_font, fill=BLACK, anchor="mm")
        add_box(*dev_box, "nav", view=ViewMode.DEVICES.value)
        y += dev_h + s(14)

        # Progress bar (tap or slide anywhere on the bar to seek)
        prog_y = y
        bar_h = s(10)
        draw.rounded_rectangle([rx, prog_y, rx + rw, prog_y + bar_h], radius=s(4), fill=WHITE, outline=BLACK, width=max(1, s(2)))
        thumb_x = rx
        if pb.duration_ms > 0:
            ratio = max(0.0, min(1.0, pb.progress_ms / pb.duration_ms))
            fill_w = int(rw * ratio)
            thumb_x = rx + fill_w
            if fill_w > s(4):
                draw.rounded_rectangle([rx, prog_y, rx + fill_w, prog_y + bar_h], radius=s(4), fill=BLACK)
            # Crisp thumb knob so the bar looks interactive
            knob_r = s(7)
            knob_cy = prog_y + bar_h // 2
            draw.ellipse([thumb_x - knob_r, knob_cy - knob_r, thumb_x + knob_r, knob_cy + knob_r], fill=BLACK, outline=WHITE, width=max(1, s(2)))
        # Generous vertical touch zone for easy tapping/sliding on e-ink
        add_box(rx - s(8), prog_y - s(12), rx + rw + s(8), prog_y + bar_h + s(20), "seek", x1=rx, x2=rx + rw)

        time_font = self.fonts.get(s(13), bold=False)
        draw.text((rx, prog_y + bar_h + s(6)), _format_ms(pb.progress_ms), font=time_font, fill=DARK_GRAY)
        dur_str = _format_ms(pb.duration_ms)
        draw.text((rx + rw - int(draw.textlength(dur_str, font=time_font)), prog_y + bar_h + s(6)), dur_str, font=time_font, fill=DARK_GRAY)
        y = prog_y + s(34)

        # Transport Controls: [ Prev ] [ Play / Pause ] [ Next ]
        gap = s(12)
        btn_h = s(64)
        trans_y = y
        side_w = (rw - 2 * gap) // 3
        center_w = rw - 2 * side_w - 2 * gap

        prev_box = (rx, trans_y, rx + side_w, trans_y + btn_h)
        play_box = (rx + side_w + gap, trans_y, rx + side_w + gap + center_w, trans_y + btn_h)
        next_box = (rx + side_w + gap + center_w + gap, trans_y, rx + rw, trans_y + btn_h)

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
        y = trans_y + btn_h + s(12)

        # Toggle Controls: [ Like ] [ Shuffle ]
        tog_h = s(46)
        tog_y = y
        half_w = (rw - gap) // 2
        like_box = (rx, tog_y, rx + half_w, tog_y + tog_h)
        shuf_box = (rx + half_w + gap, tog_y, rx + rw, tog_y + tog_h)

        like_fill = BLACK if pb.is_liked else WHITE
        like_fg = WHITE if pb.is_liked else BLACK
        draw.rounded_rectangle(like_box, radius=s(9), fill=like_fill, outline=BLACK, width=max(1, s(2)))
        self._draw_heart_icon(draw, like_box[0] + s(28), (like_box[1] + like_box[3]) // 2, s(11), filled=pb.is_liked, color=like_fg)
        tog_font = self.fonts.get(s(16), bold=True)
        draw.text((like_box[0] + s(48), (like_box[1] + like_box[3]) // 2), "Liked" if pb.is_liked else "Like", font=tog_font, fill=like_fg, anchor="lm")
        add_box(*like_box, "like")

        shuf_fill = BLACK if pb.shuffle_state else WHITE
        shuf_fg = WHITE if pb.shuffle_state else BLACK
        draw.rounded_rectangle(shuf_box, radius=s(9), fill=shuf_fill, outline=BLACK, width=max(1, s(2)))
        self._draw_shuffle_icon(draw, shuf_box[0] + s(28), (shuf_box[1] + shuf_box[3]) // 2, s(11), color=shuf_fg)
        draw.text((shuf_box[0] + s(48), (shuf_box[1] + shuf_box[3]) // 2), "Shuffle", font=tog_font, fill=shuf_fg, anchor="lm")
        add_box(*shuf_box, "shuffle")
        y = tog_y + tog_h + s(12)

        # Volume row: [ - ] [ Volume Bar + % ] [ + ]
        vol_h = s(46)
        vol_y = y
        vbtn_w = s(54)
        vmin_box = (rx, vol_y, rx + vbtn_w, vol_y + vol_h)
        vplus_box = (rx + rw - vbtn_w, vol_y, rx + rw, vol_y + vol_h)
        vbar_x1 = rx + vbtn_w + s(10)
        vbar_x2 = rx + rw - vbtn_w - s(10)

        vol_font = self.fonts.get(s(24), bold=True)
        self._draw_pill_button(draw, vmin_box, "-", vol_font, active=False, radius=s(9))
        add_box(*vmin_box, "vol_down")

        self._draw_pill_button(draw, vplus_box, "+", vol_font, active=False, radius=s(9))
        add_box(*vplus_box, "vol_up")

        vol_pct = pb.volume_percent if pb.volume_percent is not None else 0
        vol_label = f"Vol {pb.volume_percent}%" if pb.volume_percent is not None else "Vol --"
        vl_font = self.fonts.get(s(14), bold=True)
        draw.text(((vbar_x1 + vbar_x2) // 2, vol_y + s(10)), vol_label, font=vl_font, fill=BLACK, anchor="mm")
        m_y1 = vol_y + s(24)
        m_y2 = vol_y + s(38)
        draw.rounded_rectangle([vbar_x1, m_y1, vbar_x2, m_y2], radius=s(4), fill=WHITE, outline=BLACK, width=max(1, s(2)))
        if pb.volume_percent is not None and vol_pct > 0:
            fw = int((vbar_x2 - vbar_x1) * vol_pct / 100)
            if fw > s(4):
                draw.rounded_rectangle([vbar_x1, m_y1, vbar_x1 + fw, m_y2], radius=s(4), fill=BLACK)

        # Synced Lyrics filling all remaining space below Volume down to the bottom margin
        lyrics_y = vol_y + vol_h + s(14)
        lyrics_h = (h - s(14)) - lyrics_y
        if pb.has_track and lyrics_h >= s(40):
            self._render_lyrics_snippet(draw, state, rx, lyrics_y, rw, lyrics_h, s)

    def _render_lyrics_snippet(
        self,
        draw: ImageDraw.ImageDraw,
        state: DeskBuddyState,
        x: int,
        y: int,
        w: int,
        h: int,
        s,
    ) -> None:
        pb = state.playback
        entry = state.lyrics.get_lyrics(pb)
        if entry is None:
            return

        prev_line = ""
        cur_line = ""
        upcoming: list[str] = []
        if entry.instrumental:
            cur_line = "Instrumental"
        elif entry.synced:
            prev_line, cur_line, upcoming = get_synced_lines(entry.synced, pb.progress_ms, upcoming_count=5)
        elif entry.plain and pb.duration_ms > 0:
            ratio = max(0.0, min(0.99, pb.progress_ms / pb.duration_ms))
            idx = int(len(entry.plain) * ratio)
            prev_line = entry.plain[idx - 1] if idx > 0 else ""
            cur_line = entry.plain[idx]
            upcoming = entry.plain[idx + 1 : idx + 5]

        if not cur_line and not upcoming:
            return

        # Crisp left accent bar + previous line (muted) + current line (bold) + multiple upcoming lines
        bar_w = max(2, s(3))
        tx = x + bar_w + s(10)
        tw = w - bar_w - s(10)
        max_y = y + h
        cy = y + s(2)

        # Show previous line subtly when we have generous vertical space
        if prev_line and h >= s(96):
            prev_font = self.fonts.for_text(prev_line, s(14), bold=False)
            draw.text((tx, cy), _truncate_text(draw, prev_line, prev_font, tw), font=prev_font, fill=MID_GRAY)
            cy += s(20)

        # Current line (bold black, wrapped up to 2 lines)
        cur_text = cur_line or upcoming[0]
        cur_font = self.fonts.for_text(cur_text, s(18), bold=True)
        cur_lines = _wrap_text(draw, cur_text, cur_font, tw, max_lines=2)
        for ln in cur_lines:
            if cy + s(20) > max_y:
                break
            draw.text((tx, cy), ln, font=cur_font, fill=BLACK)
            cy += s(23)

        # Fill remaining space with upcoming lines
        rest = upcoming if cur_line else upcoming[1:]
        for idx, uline in enumerate(rest):
            if cy + s(18) > max_y:
                break
            u_font = self.fonts.for_text(uline, s(15), bold=False)
            shade = DARK_GRAY if idx < 2 else MID_GRAY
            draw.text((tx, cy + s(2)), _truncate_text(draw, uline, u_font, tw), font=u_font, fill=shade)
            cy += s(21)

        draw.rectangle([x, y + s(2), x + bar_w, min(max_y, cy)], fill=BLACK)

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
        # Clean header: Date on left, Devices + Battery + Exit on right
        header_y = s(28)
        date_font = self.fonts.get(s(22), bold=True)
        draw.text((s(24), header_y), _format_date(), font=date_font, fill=BLACK, anchor="lm")

        dev_btn_font = self.fonts.get(s(15), bold=True)
        exit_box = (w - s(20) - s(38), s(10), w - s(20), s(46))
        self._draw_pill_button(draw, exit_box, "X", dev_btn_font, active=False, radius=s(7))
        add_box(*exit_box, "exit")

        left_of_batt = self._draw_battery(
            draw, exit_box[0] - s(12), header_y, state.kindle_battery, state.kindle_charging, s
        )
        bright_w = s(44)
        bright_box = (left_of_batt - s(8) - bright_w, s(10), left_of_batt - s(8), s(46))
        draw.rounded_rectangle(bright_box, radius=s(7), fill=WHITE, outline=BLACK, width=max(1, s(2)))
        self._draw_sun_icon(draw, (bright_box[0] + bright_box[2]) // 2, header_y, s(11), BLACK)
        add_box(*bright_box, "toggle_brightness")

        dev_box = (bright_box[0] - s(108), s(10), bright_box[0] - s(10), s(46))
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
            ("Playlists", BrowseTab.PLAYLISTS),
            ("Albums", BrowseTab.ALBUMS),
            ("Liked Songs", BrowseTab.SONGS),
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
            ct_font = self.fonts.for_text(item.title, s(19), bold=True)
            cs_font = self.fonts.for_text(item.subtitle, s(15), bold=False)
            draw.text((tx, bcy - s(13)), _truncate_text(draw, item.title, ct_font, tw), font=ct_font, fill=BLACK, anchor="lm")
            draw.text((tx, bcy + s(15)), _truncate_text(draw, item.subtitle, cs_font, tw), font=cs_font, fill=DARK_GRAY, anchor="lm")

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
            ("Playlists", BrowseTab.PLAYLISTS),
            ("Albums", BrowseTab.ALBUMS),
            ("Liked Songs", BrowseTab.SONGS),
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
            btn_font = self.fonts.get(s(15), bold=True)

            for i, item in enumerate(page_items):
                ry1 = list_top + i * (row_h + row_gap)
                ry2 = ry1 + row_h
                rx1, rx2 = s(18), w - s(18)
                draw.rounded_rectangle([rx1, ry1, rx2, ry2], radius=s(8), fill=WHITE, outline=BLACK, width=max(1, s(2)))

                cy = (ry1 + ry2) // 2
                title_font = self.fonts.for_text(item.title, s(19), bold=True)
                sub_font = self.fonts.for_text(item.subtitle, s(15), bold=False)
                if item.kind in ("album", "playlist"):
                    # Right-side Play button + main area opens tracklist
                    play_w = s(96)
                    play_box = (rx2 - play_w - s(10), ry1 + s(10), rx2 - s(10), ry2 - s(10))
                    self._draw_pill_button(draw, play_box, "Play", btn_font, active=True, radius=s(7))
                    text_max = (play_box[0] - rx1) - s(28)
                    draw.text((rx1 + s(16), cy - s(11)), _truncate_text(draw, item.title, title_font, text_max), font=title_font, fill=BLACK, anchor="lm")
                    sub_txt = f"{item.subtitle}  ·  Tap for tracks" if item.subtitle else "Tap for tracks"
                    draw.text((rx1 + s(16), cy + s(13)), _truncate_text(draw, sub_txt, sub_font, text_max), font=sub_font, fill=DARK_GRAY, anchor="lm")
                    add_box(rx1, ry1, play_box[0] - s(4), ry2, "open_collection", item=item)
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

        play_label = "Play Playlist" if (album and album.kind == "playlist") else "Play Album"
        play_box = (w - s(164), header_y1, w - s(18), header_y2)
        self._draw_pill_button(draw, play_box, play_label, btn_font, active=True, radius=s(7))
        if album:
            add_box(*play_box, "play_item", item=album)

        title_x = back_box[2] + s(16)
        title_w = play_box[0] - title_x - s(14)
        alb_title = album.title if album else "Album"
        alb_sub = album.subtitle if album else ""
        t_font = self.fonts.for_text(alb_title, s(20), bold=True)
        a_font = self.fonts.for_text(alb_sub, s(14), bold=False)
        draw.text((title_x, header_y1 + s(12)), _truncate_text(draw, alb_title, t_font, title_w), font=t_font, fill=BLACK, anchor="lm")
        draw.text((title_x, header_y1 + s(34)), _truncate_text(draw, alb_sub, a_font, title_w), font=a_font, fill=DARK_GRAY, anchor="lm")

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

        for i, tr in enumerate(page_tracks):
            ry1 = list_top + i * (row_h + row_gap)
            ry2 = ry1 + row_h
            rx1, rx2 = s(18), w - s(18)
            draw.rounded_rectangle([rx1, ry1, rx2, ry2], radius=s(8), fill=WHITE, outline=BLACK, width=max(1, s(2)))
            cy = (ry1 + ry2) // 2
            max_tw = (rx2 - rx1) - s(80)
            row_title_font = self.fonts.for_text(tr.title, s(18), bold=True)
            row_sub_font = self.fonts.for_text(tr.subtitle, s(14), bold=False)
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
        np_font = self.fonts.for_text(np_label, s(18), bold=True)
        draw.text((s(32), y + np_h // 2), _truncate_text(draw, np_label, np_font, w - s(170)), font=np_font, fill=BLACK, anchor="lm")

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
            for idx, item in enumerate(page_items):
                num = page * per_page + idx + 1
                ry1 = y + idx * (row_h + row_gap)
                ry2 = ry1 + row_h
                rx1, rx2 = s(18), w - s(18)
                draw.rounded_rectangle([rx1, ry1, rx2, ry2], radius=s(7), fill=WHITE, outline=BLACK, width=max(1, s(1)))
                cy = (ry1 + ry2) // 2
                max_tw = (rx2 - rx1) - s(70)
                t_label = f"{num}. {item.title}"
                t_font = self.fonts.for_text(t_label, s(18), bold=True)
                s_font = self.fonts.for_text(item.subtitle, s(14), bold=False)
                draw.text((rx1 + s(16), cy - s(9)), _truncate_text(draw, t_label, t_font, max_tw), font=t_font, fill=BLACK, anchor="lm")
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

    # ------------------------------------------------------------------
    # 7. BRIGHTNESS VIEW (Kindle frontlight control 0..24)
    # ------------------------------------------------------------------
    def _render_brightness(
        self,
        draw: ImageDraw.ImageDraw,
        state: DeskBuddyState,
        w: int,
        h: int,
        top_h: int,
        s,
        add_box,
    ) -> None:
        level = max(0, min(24, state.kindle_brightness))
        pct = int(round(level * 100 / 24))

        # Header with Back button
        hy1 = top_h + s(20)
        hy2 = hy1 + s(44)
        btn_font = self.fonts.get(s(17), bold=True)
        back_box = (s(24), hy1, s(132), hy2)
        self._draw_pill_button(draw, back_box, "< Back", btn_font, active=False, radius=s(8))
        add_box(*back_box, "toggle_brightness")

        title_font = self.fonts.get(s(26), bold=True)
        draw.text((w // 2, (hy1 + hy2) // 2), "Screen Brightness", font=title_font, fill=BLACK, anchor="mm")

        # Big sun icon + level readout
        cy = top_h + s(140)
        self._draw_sun_icon(draw, w // 2 - s(90), cy, s(28), BLACK)
        val_font = self.fonts.get(s(54), bold=True)
        draw.text((w // 2 + s(30), cy), f"{pct}%", font=val_font, fill=BLACK, anchor="mm")
        sub_font = self.fonts.get(s(16), bold=False)
        draw.text((w // 2, cy + s(48)), f"Kindle Frontlight Level: {level} / 24", font=sub_font, fill=DARK_GRAY, anchor="mm")

        # Wide bar + big [-] and [+] controls
        row_y1 = top_h + s(225)
        row_y2 = row_y1 + s(76)
        pm_w = s(96)
        minus_box = (s(40), row_y1, s(40) + pm_w, row_y2)
        plus_box = (w - s(40) - pm_w, row_y1, w - s(40), row_y2)

        pm_font = self.fonts.get(s(36), bold=True)
        self._draw_pill_button(draw, minus_box, "-", pm_font, active=False, radius=s(12))
        add_box(*minus_box, "brightness_step", delta=-2)

        self._draw_pill_button(draw, plus_box, "+", pm_font, active=False, radius=s(12))
        add_box(*plus_box, "brightness_step", delta=2)

        bar_x1 = minus_box[2] + s(22)
        bar_x2 = plus_box[0] - s(22)
        bar_y1 = row_y1 + s(18)
        bar_y2 = row_y2 - s(18)
        draw.rounded_rectangle([bar_x1, bar_y1, bar_x2, bar_y2], radius=s(8), fill=WHITE, outline=BLACK, width=max(2, s(3)))
        if level > 0:
            fw = int((bar_x2 - bar_x1) * level / 24)
            if fw > s(8):
                draw.rounded_rectangle([bar_x1, bar_y1, bar_x1 + fw, bar_y2], radius=s(8), fill=BLACK)

        # 5 quick presets: Off, Low, Med, High, Max
        presets = [
            ("Off", 0),
            ("Low", 5),
            ("Med", 12),
            ("High", 18),
            ("Max", 24),
        ]
        py1 = row_y2 + s(42)
        py2 = py1 + s(66)
        pgap = s(14)
        total_w = w - s(80)
        pw = (total_w - pgap * (len(presets) - 1)) // len(presets)
        p_font = self.fonts.get(s(19), bold=True)
        for i, (label, pval) in enumerate(presets):
            px1 = s(40) + i * (pw + pgap)
            px2 = px1 + pw
            active = abs(level - pval) <= 1
            self._draw_pill_button(draw, (px1, py1, px2, py2), label, p_font, active=active, radius=s(10))
            add_box(px1, py1, px2, py2, "brightness_set", level=pval)

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
