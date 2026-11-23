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
PAUSE_KINDLE_UI="${PAUSE_KINDLE_UI:-0}"
TOUCH_DEV="${TOUCH_DEV:-}"

LOG_FILE="$SCRIPT_DIR/deskbuddy.log"
: > "$LOG_FILE" 2>/dev/null || LOG_FILE="/tmp/deskbuddy.log"
log() {
    echo "$(date '+%H:%M:%S') $*" >> "$LOG_FILE" 2>/dev/null || true
}
log "Starting Desk Buddy | PI_URL=$PI_URL ROTATION=$ROTATION PAUSE_KINDLE_UI=$PAUSE_KINDLE_UI"

TMP_DIR="/tmp/deskbuddy"
mkdir -p "$TMP_DIR"
IMG_FILE="$TMP_DIR/screen.png"
HDR_FILE="$TMP_DIR/headers.txt"
PID_FILE="$TMP_DIR/deskbuddy.pid"
DIRTY_FLAG="$TMP_DIR/dirty"
EXIT_FLAG="$TMP_DIR/exit"

# Locate fbink binary (PATH or common jailbreak / KOReader locations)
FBINK=""
for candidate in "$(command -v fbink 2>/dev/null || true)" \
    /usr/local/bin/fbink \
    /mnt/us/bin/fbink \
    /mnt/us/linkss/bin/fbink \
    /mnt/us/koreader/fbink; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        FBINK="$candidate"
        break
    fi
done

EIPS=""
for candidate in "$(command -v eips 2>/dev/null || true)" /usr/sbin/eips /usr/bin/eips; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        EIPS="$candidate"
        break
    fi
done

if [ -z "$FBINK" ] && [ -z "$EIPS" ]; then
    echo "ERROR: Neither fbink nor eips found." >&2
    exit 1
fi

# Image renderer backend: prefer built-in eips when available (KOReader's fbink lacks image support)
if [ -n "$EIPS" ]; then
    IMAGE_BACKEND="eips"
else
    IMAGE_BACKEND="fbink"
fi

draw_image() {
    FILE="$1"
    FULL="$2"
    if [ "$IMAGE_BACKEND" = "fbink" ]; then
        if [ "$FULL" = "1" ]; then
            "$FBINK" -q -f -g "file=${FILE}" 2>>"$LOG_FILE" && return 0
        else
            "$FBINK" -q -g "file=${FILE}" 2>>"$LOG_FILE" && return 0
        fi
        if [ -n "$EIPS" ]; then
            log "fbink -g unsupported; switching permanently to built-in eips ($EIPS)"
            IMAGE_BACKEND="eips"
        else
            log "fbink -g failed and eips not found"
            return 1
        fi
    fi
    if [ "$IMAGE_BACKEND" = "eips" ] && [ -n "$EIPS" ]; then
        if [ "$FULL" = "1" ]; then
            "$EIPS" -f -g "$FILE" >/dev/null 2>>"$LOG_FILE" && return 0
        else
            "$EIPS" -g "$FILE" >/dev/null 2>>"$LOG_FILE" && return 0
        fi
        log "eips -g failed ($?)"
        return 1
    fi
    return 1
}

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
    fi
    if [ -z "$BATT" ]; then
        for p in /sys/class/power_supply/*/capacity; do
            if [ -r "$p" ]; then
                BATT="$(cat "$p" 2>/dev/null | tr -dc '0-9')"
                break
            fi
        done
    fi
    # Use official Kindle powerd property for accurate charging detection
    if command -v lipc-get-prop >/dev/null 2>&1; then
        IS_CHG="$(lipc-get-prop com.lab126.powerd isCharging 2>/dev/null | tr -dc '0-9')"
        [ "$IS_CHG" = "1" ] && CHG="1"
    else
        for s in /sys/class/power_supply/*/status; do
            if [ -r "$s" ] && grep -qi "^charging$" "$s" 2>/dev/null; then
                CHG="1"
                break
            fi
        done
    fi
    BATT="${BATT:-50}"
}

read_brightness() {
    BRIGHT=""
    if command -v lipc-get-prop >/dev/null 2>&1; then
        BRIGHT="$(lipc-get-prop com.lab126.powerd flIntensity 2>/dev/null | tr -dc '0-9')"
    fi
}

set_brightness() {
    VAL="$1"
    [ -z "$VAL" ] && return
    if command -v lipc-set-prop >/dev/null 2>&1; then
        lipc-set-prop com.lab126.powerd flIntensity "$VAL" >/dev/null 2>&1 || true
    fi
}

http_get_screen() {
    URL="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 8 -D "$HDR_FILE" -o "$IMG_FILE.tmp" "$URL" 2>>"$LOG_FILE"
        return $?
    elif command -v wget >/dev/null 2>&1; then
        : > "$HDR_FILE"
        wget -q -T 8 -O "$IMG_FILE.tmp" "$URL" 2>>"$LOG_FILE"
        return $?
    fi
    log "ERROR: neither curl nor wget found"
    return 1
}

http_send_tap() {
    URL="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 5 "$URL" 2>/dev/null || true
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 5 -O - "$URL" 2>/dev/null || true
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
    rm -f "$PID_FILE" "$DIRTY_FLAG" "$EXIT_FLAG" "$IMG_FILE.tmp"
    if [ -n "$EIPS" ]; then
        "$EIPS" -c >/dev/null 2>&1 || true
    elif [ -n "$FBINK" ]; then
        "$FBINK" -q -c -f >/dev/null 2>&1 || true
    fi
    log "Desk Buddy exited cleanly"
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

log "Detected FB=${FB_W}x${FB_H} FBINK=${FBINK:-none} EIPS=${EIPS:-none}"
rm -f "$DIRTY_FLAG" "$EXIT_FLAG"

# Background touch listener: forwards taps to Pi and sets DIRTY_FLAG for immediate redraw
export TOUCH_MAX_X="${TOUCH_MAX_X:-0}"
export TOUCH_MAX_Y="${TOUCH_MAX_Y:-0}"
export TOUCH_SWAP_XY="${TOUCH_SWAP_XY:-0}"
export TOUCH_INVERT_X="${TOUCH_INVERT_X:-0}"
export TOUCH_INVERT_Y="${TOUCH_INVERT_Y:-0}"

(
    CORNER_TAPS=0
    CORNER_START=0
    sh "$SCRIPT_DIR/touch_reader.sh" "$TOUCH_DEV" "$FB_W" "$FB_H" 2>>"$LOG_FILE" | while read -r TX TY; do
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
                : > "$EXIT_FLAG"
                break
            fi
        fi

        read_battery
        RESP="$(http_send_tap "${PI_URL}/api/tap?x=${TX}&y=${TY}&rotate=${ROTATION}&battery=${BATT}&charging=${CHG}")"
        if echo "$RESP" | grep -q '"exit": *true'; then
            log "Exit button tapped"
            : > "$EXIT_FLAG"
            break
        fi
        NEW_BRIGHT="$(echo "$RESP" | sed -n 's/.*"brightness": *\([0-9][0-9]*\).*/\1/p')"
        if [ -n "$NEW_BRIGHT" ]; then
            log "Setting brightness to $NEW_BRIGHT"
            set_brightness "$NEW_BRIGHT"
        fi
        : > "$DIRTY_FLAG"
    done
) &
TOUCH_PID="$!"

LAST_HASH=""
PARTIAL_COUNT=0
POLL_SEC=10
HAS_USLEEP="0"
command -v usleep >/dev/null 2>&1 && HAS_USLEEP="1"

while [ ! -f "$EXIT_FLAG" ]; do
    read_battery
    read_brightness
    BRIGHT_PARAM=""
    [ -n "$BRIGHT" ] && BRIGHT_PARAM="&brightness=${BRIGHT}"
    SCREEN_URL="${PI_URL}/api/screen.png?w=${FB_W}&h=${FB_H}&rotate=${ROTATION}&battery=${BATT}&charging=${CHG}${BRIGHT_PARAM}"

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
                draw_image "$IMG_FILE" 1 || true
            else
                draw_image "$IMG_FILE" 0 || true
            fi
        fi
    else
        log "HTTP fetch failed for $SCREEN_URL"
        POLL_SEC=10
    fi

    # Wait up to POLL_SEC seconds, waking immediately (<250ms) on any touch event
    if [ "$HAS_USLEEP" = "1" ]; then
        TICKS=$((POLL_SEC * 4))
        I=0
        while [ "$I" -lt "$TICKS" ]; do
            [ -f "$EXIT_FLAG" ] && break
            if [ -f "$DIRTY_FLAG" ]; then
                rm -f "$DIRTY_FLAG"
                break
            fi
            usleep 250000
            I=$((I + 1))
        done
    else
        I=0
        while [ "$I" -lt "$POLL_SEC" ]; do
            [ -f "$EXIT_FLAG" ] && break
            if [ -f "$DIRTY_FLAG" ]; then
                rm -f "$DIRTY_FLAG"
                break
            fi
            sleep 1
            I=$((I + 1))
        done
    fi
done
