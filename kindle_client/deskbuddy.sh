#!/bin/sh
# Kindle Desk Buddy — Kindle 10th Gen thin client (fbink + evdev touch + battery).
# No KUAL required. Run via SSH or via the start_deskbuddy.sh scriptlet.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF_FILE="$SCRIPT_DIR/deskbuddy.conf"
[ -f "$CONF_FILE" ] && . "$CONF_FILE"

PI_URL="${PI_URL:-http://192.168.1.50:8765}"
ROTATION="${ROTATION:-90}"
FULL_REFRESH_EVERY="${FULL_REFRESH_EVERY:-15}"
PAUSE_KINDLE_UI="${PAUSE_KINDLE_UI:-1}"
TOUCH_DEV="${TOUCH_DEV:-}"

TMP_DIR="/tmp/deskbuddy"
mkdir -p "$TMP_DIR"
IMG_FILE="$TMP_DIR/screen.png"
HDR_FILE="$TMP_DIR/headers.txt"
PID_FILE="$TMP_DIR/deskbuddy.pid"
WAKE_FIFO="$TMP_DIR/wake.fifo"

# Locate fbink binary (PATH or common jailbreak / KOReader locations)
FBINK=""
for candidate in "$(command -v fbink 2>/dev/null || true)" \
    /usr/local/bin/fbink \
    /mnt/us/bin/fbink \
    /mnt/us/koreader/fbink \
    /mnt/us/linkss/bin/fbink; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        FBINK="$candidate"
        break
    fi
done

if [ -z "$FBINK" ]; then
    echo "ERROR: fbink not found. Please install fbink or check PATH." >&2
    exit 1
fi

# Auto-detect framebuffer resolution via fbink -e
FB_W="600"
FB_H="800"
FBINK_ENV="$("$FBINK" -e 2>/dev/null || true)"
if [ -n "$FBINK_ENV" ]; then
    eval "$FBINK_ENV" 2>/dev/null || true
    FB_W="${viewWidth:-${screenWidth:-600}}"
    FB_H="${viewHeight:-${screenHeight:-800}}"
fi

# Diagnostic mode: test touch coordinates without starting the UI loop
if [ "${1:-}" = "--test-touch" ]; then
    echo "Detected framebuffer: ${FB_W}x${FB_H} | fbink: $FBINK"
    echo "Tap the screen to see (x, y) coordinates (Ctrl+C to quit)..."
    export TOUCH_MAX_X="${TOUCH_MAX_X:-$FB_W}"
    export TOUCH_MAX_Y="${TOUCH_MAX_Y:-$FB_H}"
    export TOUCH_SWAP_XY="${TOUCH_SWAP_XY:-0}"
    export TOUCH_INVERT_X="${TOUCH_INVERT_X:-0}"
    export TOUCH_INVERT_Y="${TOUCH_INVERT_Y:-0}"
    exec sh "$SCRIPT_DIR/touch_reader.sh" "$TOUCH_DEV" "$FB_W" "$FB_H"
fi

# Prevent duplicate instances
if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Desk Buddy already running (PID $OLD_PID). Stopping previous instance..."
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
fi
echo "$$" > "$PID_FILE"

TOUCH_PID=""
PAUSED_UI="0"

read_battery() {
    BATT=""
    CHG="0"
    if command -v gasgauge-info >/dev/null 2>&1; then
        BATT="$(gasgauge-info -s 2>/dev/null | tr -dc '0-9')"
        CURR="$(gasgauge-info -c 2>/dev/null | tr -dc '0-9-')"
        if [ -n "$CURR" ] && [ "$CURR" -gt 0 ] 2>/dev/null; then
            CHG="1"
        fi
    fi
    if [ -z "$BATT" ]; then
        for p in /sys/class/power_supply/*/capacity; do
            if [ -r "$p" ]; then
                BATT="$(cat "$p" 2>/dev/null | tr -dc '0-9')"
                STATUS_FILE="$(dirname "$p")/status"
                if [ -r "$STATUS_FILE" ] && grep -qi "charging" "$STATUS_FILE" 2>/dev/null; then
                    CHG="1"
                fi
                break
            fi
        done
    fi
    BATT="${BATT:-50}"
}

http_get_screen() {
    URL="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 8 -D "$HDR_FILE" -o "$IMG_FILE.tmp" "$URL" 2>/dev/null
        return $?
    elif command -v wget >/dev/null 2>&1; then
        : > "$HDR_FILE"
        wget -q -T 8 -O "$IMG_FILE.tmp" "$URL" 2>/dev/null
        return $?
    fi
    return 1
}

http_send_tap() {
    URL="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 5 "$URL" >/dev/null 2>&1
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 5 -O /dev/null "$URL" >/dev/null 2>&1
    fi
}

cleanup() {
    [ -n "$TOUCH_PID" ] && kill "$TOUCH_PID" 2>/dev/null || true
    if [ "$PAUSED_UI" = "1" ]; then
        for proc in awesome lab126_gui; do
            for pid in $(pidof "$proc" 2>/dev/null || true); do
                kill -CONT "$pid" 2>/dev/null || true
            done
        done
    fi
    if command -v lipc-set-prop >/dev/null 2>&1; then
        lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null || true
    fi
    rm -f "$PID_FILE" "$WAKE_FIFO" "$IMG_FILE.tmp"
    "$FBINK" -q -c -f 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Keep the Kindle awake while Desk Buddy is active
if command -v lipc-set-prop >/dev/null 2>&1; then
    lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null || true
fi

# Pause Kindle's home UI so taps only go to Desk Buddy
if [ "$PAUSE_KINDLE_UI" = "1" ]; then
    for proc in awesome lab126_gui; do
        for pid in $(pidof "$proc" 2>/dev/null || true); do
            kill -STOP "$pid" 2>/dev/null && PAUSED_UI="1"
        done
    done
fi

"$FBINK" -q -c -f 2>/dev/null || true

rm -f "$WAKE_FIFO"
mkfifo "$WAKE_FIFO"

# Background touch listener: forwards taps to Pi and nudges the render loop
export TOUCH_MAX_X="${TOUCH_MAX_X:-$FB_W}"
export TOUCH_MAX_Y="${TOUCH_MAX_Y:-$FB_H}"
export TOUCH_SWAP_XY="${TOUCH_SWAP_XY:-0}"
export TOUCH_INVERT_X="${TOUCH_INVERT_X:-0}"
export TOUCH_INVERT_Y="${TOUCH_INVERT_Y:-0}"

(
    CORNER_TAPS=0
    CORNER_START=0
    sh "$SCRIPT_DIR/touch_reader.sh" "$TOUCH_DEV" "$FB_W" "$FB_H" | while read -r TX TY; do
        [ -z "$TX" ] && continue

        # Secret exit gesture: 4 quick taps in the native top-left 60x60 corner within 4 seconds
        if [ "$TX" -lt 60 ] && [ "$TY" -lt 60 ]; then
            NOW="$(date +%s)"
            if [ $((NOW - CORNER_START)) -gt 4 ]; then
                CORNER_TAPS=1
                CORNER_START="$NOW"
            else
                CORNER_TAPS=$((CORNER_TAPS + 1))
            fi
            if [ "$CORNER_TAPS" -ge 4 ]; then
                echo "exit" > "$WAKE_FIFO"
                break
            fi
        fi

        read_battery
        http_send_tap "${PI_URL}/api/tap?x=${TX}&y=${TY}&rotate=${ROTATION}&battery=${BATT}&charging=${CHG}"
        echo "tap" > "$WAKE_FIFO"
    done
) &
TOUCH_PID="$!"

LAST_HASH=""
PARTIAL_COUNT=0
POLL_SEC=15

# Open FIFO read-write on FD 3 so timed reads don't block on EOF
exec 3<>"$WAKE_FIFO"

while true; do
    read_battery
    SCREEN_URL="${PI_URL}/api/screen.png?w=${FB_W}&h=${FB_H}&rotate=${ROTATION}&battery=${BATT}&charging=${CHG}"

    if http_get_screen "$SCREEN_URL"; then
        mv -f "$IMG_FILE.tmp" "$IMG_FILE"
        NEW_HASH="$(grep -i '^X-DeskBuddy-Hash:' "$HDR_FILE" 2>/dev/null | awk '{print $2}' | tr -d '\r\n')"
        REFRESH_HINT="$(grep -i '^X-DeskBuddy-Refresh:' "$HDR_FILE" 2>/dev/null | awk '{print $2}' | tr -d '\r\n')"
        POLL_HINT="$(grep -i '^X-DeskBuddy-Poll:' "$HDR_FILE" 2>/dev/null | awk '{print $2}' | tr -d '\r\n')"
        [ -n "$POLL_HINT" ] && POLL_SEC="$POLL_HINT"

        if [ -z "$NEW_HASH" ] || [ "$NEW_HASH" != "$LAST_HASH" ]; then
            LAST_HASH="$NEW_HASH"
            PARTIAL_COUNT=$((PARTIAL_COUNT + 1))
            if [ "$REFRESH_HINT" = "full" ] || [ "$PARTIAL_COUNT" -ge "$FULL_REFRESH_EVERY" ]; then
                PARTIAL_COUNT=0
                "$FBINK" -q -f -g "file=${IMG_FILE},w=-1,h=-1" 2>/dev/null || true
            else
                "$FBINK" -q -g "file=${IMG_FILE},w=-1,h=-1" 2>/dev/null || true
            fi
        fi
    else
        "$FBINK" -q -y 2 "Desk Buddy: Waiting for Pi at ${PI_URL}..." 2>/dev/null || true
        POLL_SEC=10
    fi

    # Wait up to POLL_SEC seconds, waking immediately on any touch event
    EVENT=""
    if read -r -t "$POLL_SEC" EVENT <&3 2>/dev/null; then
        if [ "$EVENT" = "exit" ]; then
            break
        fi
        # Drain any queued burst events
        while read -r -t 0 _ <&3 2>/dev/null; do :; done
    fi
done
