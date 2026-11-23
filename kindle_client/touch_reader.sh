#!/bin/sh
# Zero-buffering Linux evdev touchscreen reader for Kindle 10th Gen (32-bit ARM).
# Uses KOReader's bundled luajit when available (instant, zero-buffering, auto-calibrates
# hardware max_x/max_y via EVIOCGABS), with a pure-shell dd fallback.
# Outputs one "x y" line per finger tap to stdout.

set -u

[ -n "${TOUCH_PID_FILE:-}" ] && echo "$$" > "$TOUCH_PID_FILE"

TOUCH_DEV="${1:-}"
FB_W="${2:-600}"
FB_H="${3:-800}"
TOUCH_MAX_X="${TOUCH_MAX_X:-0}"
TOUCH_MAX_Y="${TOUCH_MAX_Y:-0}"
TOUCH_SWAP_XY="${TOUCH_SWAP_XY:-0}"
TOUCH_INVERT_X="${TOUCH_INVERT_X:-0}"
TOUCH_INVERT_Y="${TOUCH_INVERT_Y:-0}"

detect_touch_dev() {
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

# Prefer KOReader's bundled luajit (zero buffering, exact binary struct parsing, auto EVIOCGABS)
LUAJIT=""
for candidate in "$(command -v luajit 2>/dev/null || true)" /mnt/us/koreader/luajit; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        LUAJIT="$candidate"
        break
    fi
done

if [ -n "$LUAJIT" ]; then
    exec "$LUAJIT" - "$TOUCH_DEV" "$FB_W" "$FB_H" "$TOUCH_MAX_X" "$TOUCH_MAX_Y" "$TOUCH_SWAP_XY" "$TOUCH_INVERT_X" "$TOUCH_INVERT_Y" <<'EOF'
local ffi = require("ffi")
local bit = require("bit")

ffi.cdef[[
    int open(const char *pathname, int flags);
    int close(int fd);
    long read(int fd, void *buf, size_t count);
    int ioctl(int fd, unsigned long request, void *argp);
    struct input_event {
        uint32_t tv_sec;
        uint32_t tv_usec;
        uint16_t type;
        uint16_t code;
        int32_t value;
    };
    struct input_absinfo {
        int32_t value;
        int32_t minimum;
        int32_t maximum;
        int32_t fuzz;
        int32_t flat;
        int32_t resolution;
    };
]]

local dev     = arg[1]
local fb_w    = tonumber(arg[2]) or 600
local fb_h    = tonumber(arg[3]) or 800
local max_x   = tonumber(arg[4]) or 0
local max_y   = tonumber(arg[5]) or 0
local swap_xy = tonumber(arg[6]) or 0
local inv_x   = tonumber(arg[7]) or 0
local inv_y   = tonumber(arg[8]) or 0

local fd = ffi.C.open(dev, 0) -- O_RDONLY
if fd < 0 then
    io.stderr:write("ERROR: failed to open " .. dev .. "\n")
    os.exit(1)
end

-- Auto-detect hardware ABS_MT_POSITION_X / Y (or ABS_X / Y) range via EVIOCGABS
local absinfo = ffi.new("struct input_absinfo")
if max_x <= 0 then
    if ffi.C.ioctl(fd, 0x80184575, absinfo) == 0 and absinfo.maximum > 0 then
        max_x = absinfo.maximum
    elseif ffi.C.ioctl(fd, 0x80184540, absinfo) == 0 and absinfo.maximum > 0 then
        max_x = absinfo.maximum
    else
        max_x = fb_w - 1
    end
end
if max_y <= 0 then
    if ffi.C.ioctl(fd, 0x80184576, absinfo) == 0 and absinfo.maximum > 0 then
        max_y = absinfo.maximum
    elseif ffi.C.ioctl(fd, 0x80184541, absinfo) == 0 and absinfo.maximum > 0 then
        max_y = absinfo.maximum
    else
        max_y = fb_h - 1
    end
end

io.stderr:write(string.format("touch_reader(luajit): dev=%s fb=%dx%d hw_max=%dx%d\n", dev, fb_w, fb_h, max_x, max_y))
io.stderr:flush()

local ev = ffi.new("struct input_event")
local ev_size = ffi.sizeof(ev)
local tx, ty = -1, -1
local touching = false
local seen_mt = false
local last_ms = 0

local function emit(sec, usec, rx, ry)
    local now_ms = tonumber(sec) * 1000 + math.floor(tonumber(usec) / 1000)
    if (now_ms - last_ms) < 280 then return end
    last_ms = now_ms

    local x, y = rx, ry
    if swap_xy == 1 then x, y = y, x end
    local px = math.floor((x * (fb_w - 1)) / max_x + 0.5)
    local py = math.floor((y * (fb_h - 1)) / max_y + 0.5)
    if inv_x == 1 then px = (fb_w - 1) - px end
    if inv_y == 1 then py = (fb_h - 1) - py end
    if px < 0 then px = 0 elseif px >= fb_w then px = fb_w - 1 end
    if py < 0 then py = 0 elseif py >= fb_h then py = fb_h - 1 end

    io.stderr:write(string.format("touch raw=(%d,%d) -> fb=(%d,%d)\n", rx, ry, px, py))
    io.stderr:flush()
    io.stdout:write(string.format("%d %d\n", px, py))
    io.stdout:flush()
end

while true do
    local n = ffi.C.read(fd, ev, ev_size)
    if n <= 0 then break end
    if n == ev_size then
        local t = ev.type
        local c = ev.code
        local v = ev.value
        if t == 3 and (c == 53 or c == 0) then
            tx = v; touching = true
        elseif t == 3 and (c == 54 or c == 1) then
            ty = v; touching = true
        elseif t == 3 and c == 57 then
            if v >= 0 then
                touching = true
            elseif touching and tx >= 0 and ty >= 0 then
                emit(ev.tv_sec, ev.tv_usec, tx, ty)
                touching = false; tx = -1; ty = -1
            end
        elseif t == 1 and c == 330 then
            if v == 1 then
                touching = true
            elseif v == 0 and touching and tx >= 0 and ty >= 0 then
                emit(ev.tv_sec, ev.tv_usec, tx, ty)
                touching = false; tx = -1; ty = -1
            end
        end
    end
end
ffi.C.close(fd)
EOF
fi

# Pure-shell fallback (reads 16 bytes at a time via dd to avoid pipe buffering)
echo "touch_reader(sh): dev=$TOUCH_DEV fb=${FB_W}x${FB_H}" >&2
TX=-1
TY=-1
TOUCHING=0
LAST_SEC=0
[ "$TOUCH_MAX_X" -le 0 ] && TOUCH_MAX_X="$FB_W"
[ "$TOUCH_MAX_Y" -le 0 ] && TOUCH_MAX_Y="$FB_H"

while LINE="$(dd if="$TOUCH_DEV" bs=16 count=1 2>/dev/null | hexdump -v -e '1/4 "%u " 1/4 "%u " 1/2 "%u " 1/2 "%u " 1/4 "%d\n"')"; do
    [ -z "$LINE" ] && break
    set -- $LINE
    SEC="$1"; TYPE="$3"; CODE="$4"; VAL="$5"
    if [ "$TYPE" = "3" ] && { [ "$CODE" = "53" ] || [ "$CODE" = "0" ]; }; then
        TX="$VAL"; TOUCHING=1
    elif [ "$TYPE" = "3" ] && { [ "$CODE" = "54" ] || [ "$CODE" = "1" ]; }; then
        TY="$VAL"; TOUCHING=1
    elif { [ "$TYPE" = "3" ] && [ "$CODE" = "57" ] && [ "$VAL" -lt 0 ]; } || { [ "$TYPE" = "1" ] && [ "$CODE" = "330" ] && [ "$VAL" = "0" ]; }; then
        if [ "$TOUCHING" = "1" ] && [ "$TX" -ge 0 ] && [ "$TY" -ge 0 ]; then
            if [ $((SEC - LAST_SEC)) -ge 1 ]; then
                LAST_SEC="$SEC"
                PX=$(( (TX * FB_W) / TOUCH_MAX_X ))
                PY=$(( (TY * FB_H) / TOUCH_MAX_Y ))
                echo "$PX $PY"
            fi
            TOUCHING=0; TX=-1; TY=-1
        fi
    fi
done
