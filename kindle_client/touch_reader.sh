#!/bin/sh
# Lightweight Linux evdev touchscreen reader for Kindle 10th Gen (32-bit ARM).
# Outputs one "x y" line per finger tap to stdout. Zero external binaries required.

set -u

TOUCH_DEV="${1:-}"
FB_W="${2:-600}"
FB_H="${3:-800}"
TOUCH_MAX_X="${TOUCH_MAX_X:-$FB_W}"
TOUCH_MAX_Y="${TOUCH_MAX_Y:-$FB_H}"
TOUCH_SWAP_XY="${TOUCH_SWAP_XY:-0}"
TOUCH_INVERT_X="${TOUCH_INVERT_X:-0}"
TOUCH_INVERT_Y="${TOUCH_INVERT_Y:-0}"

detect_touch_dev() {
    # Scan /proc/bus/input/devices for a touchscreen handler
    awk '
        BEGIN { RS=""; FS="\n" }
        tolower($0) ~ /(touch|zforce|cyttsp|elan|goodix|pt_mt|synaptics|fts)/ {
            for (i = 1; i <= NF; i++) {
                if ($i ~ /H: Handlers=/) {
                    if (match($i, /event[0-9]+/)) {
                        print "/dev/input/" substr($i, RSTART, RLENGTH)
                        exit
                    }
                }
            }
        }
    ' /proc/bus/input/devices 2>/dev/null
}

if [ -z "$TOUCH_DEV" ]; then
    TOUCH_DEV="$(detect_touch_dev)"
fi

if [ -z "$TOUCH_DEV" ] || [ ! -r "$TOUCH_DEV" ]; then
    # Fallback common Kindle 10th Gen touchscreen paths
    for candidate in /dev/input/event1 /dev/input/event2 /dev/input/event0; do
        if [ -r "$candidate" ]; then
            TOUCH_DEV="$candidate"
            break
        fi
    done
fi

if [ -z "$TOUCH_DEV" ] || [ ! -r "$TOUCH_DEV" ]; then
    echo "ERROR: Could not find readable touchscreen device" >&2
    exit 1
fi

# 32-bit ARM Linux struct input_event is 16 bytes:
#   4B tv_sec, 4B tv_usec, 2B type, 2B code, 4B value
# EV_ABS=3, EV_KEY=1, EV_SYN=0
# ABS_X=0, ABS_Y=1, ABS_MT_POSITION_X=53, ABS_MT_POSITION_Y=54, ABS_MT_TRACKING_ID=57, BTN_TOUCH=330
hexdump -v -e '1/4 "%u " 1/4 "%u " 1/2 "%u " 1/2 "%u " 1/4 "%d\n"' "$TOUCH_DEV" | awk \
    -v fb_w="$FB_W" \
    -v fb_h="$FB_H" \
    -v max_x="$TOUCH_MAX_X" \
    -v max_y="$TOUCH_MAX_Y" \
    -v swap_xy="$TOUCH_SWAP_XY" \
    -v inv_x="$TOUCH_INVERT_X" \
    -v inv_y="$TOUCH_INVERT_Y" '
    BEGIN {
        tx = -1; ty = -1; touching = 0; last_ms = 0;
        if (max_x <= 0) max_x = fb_w;
        if (max_y <= 0) max_y = fb_h;
    }
    {
        sec = $1; usec = $2; type = $3; code = $4; val = $5;

        # Track coordinates (MT or single-touch)
        if (type == 3 && (code == 53 || code == 0)) {
            tx = val;
            touching = 1;
        } else if (type == 3 && (code == 54 || code == 1)) {
            ty = val;
            touching = 1;
        } else if (type == 3 && code == 57) {
            if (val >= 0) {
                touching = 1;
            } else if (val < 0 && touching && tx >= 0 && ty >= 0) {
                emit(sec, usec, tx, ty);
                touching = 0; tx = -1; ty = -1;
            }
        } else if (type == 1 && code == 330) {
            if (val == 1) {
                touching = 1;
            } else if (val == 0 && touching && tx >= 0 && ty >= 0) {
                emit(sec, usec, tx, ty);
                touching = 0; tx = -1; ty = -1;
            }
        }
    }
    function emit(sec, usec, rx, ry,    now_ms, px, py, tmp) {
        now_ms = sec * 1000 + int(usec / 1000);
        # Debounce rapid duplicate release events (< 180ms)
        if (now_ms - last_ms < 180) return;
        last_ms = now_ms;

        if (swap_xy == 1) {
            tmp = rx; rx = ry; ry = tmp;
        }
        px = int((rx * fb_w) / max_x);
        py = int((ry * fb_h) / max_y);
        if (inv_x == 1) px = fb_w - 1 - px;
        if (inv_y == 1) py = fb_h - 1 - py;
        if (px < 0) px = 0;
        if (px >= fb_w) px = fb_w - 1;
        if (py < 0) py = 0;
        if (py >= fb_h) py = fb_h - 1;

        printf "%d %d\n", px, py;
        fflush();
    }
'
