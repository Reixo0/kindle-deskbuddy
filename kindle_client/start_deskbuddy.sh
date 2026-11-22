#!/bin/sh
# Name: Desk Buddy
# Author: Kindle Desk Buddy
# Icon:
#
# KUAL-less Kindle Scriptlet!
# Copy this file to /mnt/us/documents/DeskBuddy.sh (for WinterBreak / ScriptletsForKindle)
# so you can tap "Desk Buddy" directly from your Kindle library to launch it.

DESKBUDDY_DIR="/mnt/us/deskbuddy"

if [ ! -f "$DESKBUDDY_DIR/deskbuddy.sh" ]; then
    fbink -q -c -f -y 2 "Desk Buddy not found in $DESKBUDDY_DIR" 2>/dev/null || true
    sleep 3
    exit 1
fi

sh "$DESKBUDDY_DIR/deskbuddy.sh"
