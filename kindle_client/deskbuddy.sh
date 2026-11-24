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
TOUCH_PID_FILE="$TMP_DIR/touch.pid"
DIRTY_FLAG="$TMP_DIR/dirty"
EXIT_FLAG="$TMP_DIR/exit"
SLEEP_FLAG="$TMP_DIR/sleep_mode"
LIGHT_UNTIL_FILE="$TMP_DIR/light_until"
SAVED_BRIGHT_FILE="$TMP_DIR/saved_brightness"

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

# Prevent duplicate instances (including any orphaned touch reader)
if [ -f "$TOUCH_PID_FILE" ]; then
    OLD_TPID="$(cat "$TOUCH_PID_FILE" 2>/dev/null || true)"
    [ -n "$OLD_TPID" ] && kill "$OLD_TPID" 2>/dev/null || true
    rm -f "$TOUCH_PID_FILE"
fi
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

reconnect_wifi() {
    FORCE_BOUNCE="${1:-0}"
    if ! command -v lipc-set-prop >/dev/null 2>&1; then
        return
    fi
    WSTATE="$(lipc-get-prop com.lab126.wifid cmState 2>/dev/null || echo unknown)"
    log "Wi-Fi check: cmState=${WSTATE} force_bounce=${FORCE_BOUNCE}"
    if [ "$FORCE_BOUNCE" = "1" ] || [ "$WSTATE" != "CONNECTED" ]; then
        if [ "$FORCE_BOUNCE" = "1" ]; then
            lipc-set-prop com.lab126.wifid enable 0 >/dev/null 2>&1 || true
            sleep 2
        fi
        lipc-set-prop com.lab126.wifid enable 1 >/dev/null 2>&1 || true
        lipc-set-prop com.lab126.cmd ensureConnection flag >/dev/null 2>&1 || true
    fi
}

http_get_screen() {
    URL="$1"
    ERR_DEST="$LOG_FILE"
    if [ "${FAIL_COUNT:-0}" -ge 5 ] && [ $((FAIL_COUNT % 10)) -ne 0 ]; then
        ERR_DEST="/dev/null"
    fi
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 8 -D "$HDR_FILE" -o "$IMG_FILE.tmp" "$URL" 2>>"$ERR_DEST"
        return $?
    elif command -v wget >/dev/null 2>&1; then
        : > "$HDR_FILE"
        wget -q -T 8 -O "$IMG_FILE.tmp" "$URL" 2>>"$ERR_DEST"
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
    if [ -f "$TOUCH_PID_FILE" ]; then
        TPID="$(cat "$TOUCH_PID_FILE" 2>/dev/null || true)"
        [ -n "$TPID" ] && kill "$TPID" 2>/dev/null || true
        rm -f "$TOUCH_PID_FILE"
    fi
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
    if [ -f "$SAVED_BRIGHT_FILE" ]; then
        RESTORE_B="$(cat "$SAVED_BRIGHT_FILE" 2>/dev/null || echo 0)"
        set_brightness "${RESTORE_B:-0}"
        log "Restored pre-sleep brightness (${RESTORE_B:-0}) on exit"
    elif [ -f "$SLEEP_FLAG" ]; then
        set_brightness 0
    fi
    rm -f "$PID_FILE" "$DIRTY_FLAG" "$EXIT_FLAG" "$SLEEP_FLAG" "$LIGHT_UNTIL_FILE" "$SAVED_BRIGHT_FILE" "$IMG_FILE.tmp"
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
rm -f "$DIRTY_FLAG" "$EXIT_FLAG" "$SLEEP_FLAG" "$LIGHT_UNTIL_FILE" "$SAVED_BRIGHT_FILE"

# Background touch listener: forwards taps to Pi and sets DIRTY_FLAG for immediate redraw
export TOUCH_MAX_X="${TOUCH_MAX_X:-0}"
export TOUCH_MAX_Y="${TOUCH_MAX_Y:-0}"
export TOUCH_SWAP_XY="${TOUCH_SWAP_XY:-0}"
export TOUCH_INVERT_X="${TOUCH_INVERT_X:-0}"
export TOUCH_INVERT_Y="${TOUCH_INVERT_Y:-0}"

(
    EXIT_TAPS=0
    EXIT_START=0
    TOUCH_PID_FILE="$TOUCH_PID_FILE" sh "$SCRIPT_DIR/touch_reader.sh" "$TOUCH_DEV" "$FB_W" "$FB_H" 2>>"$LOG_FILE" | while read -r TX TY; do
        [ -z "$TX" ] && continue

        # Failsafe local exit (works even if Pi is offline or returning errors):
        # Double-tap the top-right corner (either landscape 'X' corner OR portrait top-right
        # corner matching the fbink error screen) within 4 seconds.
        IN_EXIT_CORNER="0"
        if [ "$TX" -gt $((FB_W - 110)) ] && [ "$TY" -lt 110 ]; then
            # Portrait top-right corner (matches fbink offline notice)
            IN_EXIT_CORNER="1"
        elif [ "$ROTATION" = "90" ] && [ "$TX" -gt $((FB_W - 110)) ] && [ "$TY" -gt $((FB_H - 110)) ]; then
            # Landscape top-right 'X' corner (USB on right)
            IN_EXIT_CORNER="1"
        elif [ "$ROTATION" = "270" ] && [ "$TX" -lt 110 ] && [ "$TY" -lt 110 ]; then
            # Landscape top-right 'X' corner (USB on left)
            IN_EXIT_CORNER="1"
        fi

        if [ "$IN_EXIT_CORNER" = "1" ]; then
            NOW="$(date +%s)"
            if [ $((NOW - EXIT_START)) -gt 4 ]; then
                EXIT_TAPS=1
                EXIT_START="$NOW"
            else
                EXIT_TAPS=$((EXIT_TAPS + 1))
            fi
            if [ "$EXIT_TAPS" -ge 2 ]; then
                log "Local failsafe double-tap exit triggered at ($TX,$TY)"
                : > "$EXIT_FLAG"
                break
            fi
        else
            EXIT_TAPS=0
        fi

        # Sleep mode nightlight logic:
        # - If light is OFF, first touch ONLY turns light ON for 15s (no accidental clicks in the dark)
        # - If light is ON, reset the 15s timer and process the tap normally
        if [ -f "$SLEEP_FLAG" ]; then
            NOW="$(date +%s)"
            UNTIL="$(cat "$LIGHT_UNTIL_FILE" 2>/dev/null || echo 0)"
            UNTIL="${UNTIL:-0}"
            if [ "$NOW" -ge "$UNTIL" ]; then
                WAKE_LVL="$(cat "$SLEEP_FLAG" 2>/dev/null || echo 6)"
                WAKE_LVL="${WAKE_LVL:-6}"
                set_brightness "$WAKE_LVL"
                echo $((NOW + 15)) > "$LIGHT_UNTIL_FILE"
                log "Sleep mode: woke frontlight ($WAKE_LVL) for 15s"
                continue
            else
                echo $((NOW + 15)) > "$LIGHT_UNTIL_FILE"
            fi
        fi

        read_battery
        RESP="$(http_send_tap "${PI_URL}/api/tap?x=${TX}&y=${TY}&rotate=${ROTATION}&battery=${BATT}&charging=${CHG}")"
        log "tap ($TX,$TY) -> ${RESP:-EMPTY}"
        if echo "$RESP" | grep -q '"exit": *true'; then
            log "Exit button tapped"
            : > "$EXIT_FLAG"
            break
        fi
        if echo "$RESP" | grep -q '"sleep_mode": *true'; then
            read_brightness
            echo "${BRIGHT:-0}" > "$SAVED_BRIGHT_FILE"
            WAKE_LVL="$(echo "$RESP" | sed -n 's/.*"sleep_wake_brightness": *\([0-9][0-9]*\).*/\1/p')"
            echo "${WAKE_LVL:-6}" > "$SLEEP_FLAG"
            echo "0" > "$LIGHT_UNTIL_FILE"
            set_brightness 0
            log "Entered sleep mode (frontlight off, saved=${BRIGHT:-0}, wake=${WAKE_LVL:-6})"
        elif echo "$RESP" | grep -q '"sleep_mode": *false'; then
            NEW_BRIGHT="$(echo "$RESP" | sed -n 's/.*"brightness": *\([0-9][0-9]*\).*/\1/p')"
            if [ -z "$NEW_BRIGHT" ] && [ -f "$SAVED_BRIGHT_FILE" ]; then
                NEW_BRIGHT="$(cat "$SAVED_BRIGHT_FILE" 2>/dev/null || true)"
            fi
            rm -f "$SLEEP_FLAG" "$LIGHT_UNTIL_FILE" "$SAVED_BRIGHT_FILE"
            [ -n "$NEW_BRIGHT" ] && set_brightness "$NEW_BRIGHT"
            log "Exited sleep mode (restored brightness=${NEW_BRIGHT:-unchanged})"
        else
            NEW_WAKE="$(echo "$RESP" | sed -n 's/.*"sleep_wake_brightness": *\([0-9][0-9]*\).*/\1/p')"
            if [ -n "$NEW_WAKE" ] && [ -f "$SLEEP_FLAG" ]; then
                echo "$NEW_WAKE" > "$SLEEP_FLAG"
                log "Updated sleep wake brightness to $NEW_WAKE"
            fi
            NEW_BRIGHT="$(echo "$RESP" | sed -n 's/.*"brightness": *\([0-9][0-9]*\).*/\1/p')"
            if [ -n "$NEW_BRIGHT" ]; then
                log "Setting brightness to $NEW_BRIGHT"
                set_brightness "$NEW_BRIGHT"
            fi
        fi
        : > "$DIRTY_FLAG"
    done
) &
TOUCH_PID="$!"

LAST_HASH=""
PARTIAL_COUNT=0
FAIL_COUNT=0
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
        if [ "$FAIL_COUNT" -ge 2 ]; then
            log "Connection to Pi restored after $FAIL_COUNT failures"
            LAST_HASH=""
            PARTIAL_COUNT="$FULL_REFRESH_EVERY"
        fi
        FAIL_COUNT=0
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
        FAIL_COUNT=$((FAIL_COUNT + 1))
        if [ "$FAIL_COUNT" -le 5 ] || [ $((FAIL_COUNT % 10)) -eq 0 ]; then
            log "HTTP fetch failed ($FAIL_COUNT) for $SCREEN_URL"
        fi
        if [ "$FAIL_COUNT" -eq 2 ]; then
            LAST_HASH=""
            if [ -n "$FBINK" ]; then
                "$FBINK" -q -c -f >/dev/null 2>&1 || true
                "$FBINK" -q -y 3 "Desk Buddy: Cannot reach Pi" >/dev/null 2>&1 || true
                "$FBINK" -q -y 5 "URL: ${PI_URL}" >/dev/null 2>&1 || true
                "$FBINK" -q -y 7 "Retrying & reconnecting Wi-Fi..." >/dev/null 2>&1 || true
                "$FBINK" -q -y 9 "Double-tap top-right corner to exit" >/dev/null 2>&1 || true
            fi
        fi
        if [ "$FAIL_COUNT" -eq 3 ] || [ $((FAIL_COUNT % 10)) -eq 3 ]; then
            reconnect_wifi 0
        elif [ $((FAIL_COUNT % 20)) -eq 15 ]; then
            reconnect_wifi 1
        fi
        POLL_SEC=6
    fi

    # Helper to check 15s sleep-mode auto-light-off
    check_sleep_light() {
        if [ -f "$SLEEP_FLAG" ] && [ -f "$LIGHT_UNTIL_FILE" ]; then
            UNTIL="$(cat "$LIGHT_UNTIL_FILE" 2>/dev/null || echo 0)"
            if [ -n "$UNTIL" ] && [ "$UNTIL" -gt 0 ] 2>/dev/null; then
                NOW="$(date +%s)"
                if [ "$NOW" -ge "$UNTIL" ]; then
                    echo "0" > "$LIGHT_UNTIL_FILE"
                    set_brightness 0
                    log "Sleep mode: 15s idle, frontlight turned off"
                fi
            fi
        fi
    }

    # Wait up to POLL_SEC seconds, waking immediately (<250ms) on any touch event
    if [ "$HAS_USLEEP" = "1" ]; then
        TICKS=$((POLL_SEC * 4))
        I=0
        while [ "$I" -lt "$TICKS" ]; do
            [ -f "$EXIT_FLAG" ] && break
            check_sleep_light
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
            check_sleep_light
            if [ -f "$DIRTY_FLAG" ]; then
                rm -f "$DIRTY_FLAG"
                break
            fi
            sleep 1
            I=$((I + 1))
        done
    fi
done
