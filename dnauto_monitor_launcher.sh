#!/usr/bin/env bash
# Launches the DN-AUTO Monitor GUI from a clickable desktop icon (no
# terminal). Because there's no terminal to show an error in if something
# goes wrong, this script checks for the serial adapter first and pops up
# a graphical error dialog instead of silently opening a blank/frozen GUI
# window, and it logs everything to a file you can check afterward.

set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Settings: ~/.config/dnauto-monitor/launcher.env (DNAUTO_PORT=..., DNAUTO_RIGCTLD=...)
[ -f "$HOME/.config/dnauto-monitor/launcher.env" ] && . "$HOME/.config/dnauto-monitor/launcher.env"
# Your adapter: a /dev/serial/by-id/... path, or /dev/dnauto with the udev rule in docs/NOTES.md
PORT="${DNAUTO_PORT:-/dev/dnauto}"
# The big band readout always comes from DN-AUTO's own channel 3 (now
# empirically calibrated via calibrate_band.py -- see README.md), never
# from rigctld. --rigctld here just adds the KX3's rigctld as a live
# cross-check: if what the amp reports and what the radio commanded ever
# disagree for more than a couple seconds, the GUI flags a MISMATCH alarm.
RIGCTLD="${DNAUTO_RIGCTLD:-127.0.0.1:4532}"   # empty = no radio cross-check
LOG_DIR="$HOME/.local/share/dnauto-monitor"
LOG_FILE="$LOG_DIR/monitor.log"

mkdir -p "$LOG_DIR"

show_error() {
    # Prefer zenity for a native-looking dialog; fall back to a Tk popup
    # (Tkinter is already a hard dependency of the app itself, so it's
    # guaranteed to be available) if zenity isn't installed.
    local msg="$1"
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title="DN-AUTO Monitor" --text="$msg" 2>/dev/null
    else
        python3 -c "
import tkinter as tk
from tkinter import messagebox
root = tk.Tk()
root.withdraw()
messagebox.showerror('DN-AUTO Monitor', '''$msg''')
" 2>/dev/null
    fi
}

cd "$REPO_DIR" || {
    show_error "Can't find the app directory:\n$REPO_DIR\n\nDid the folder move?"
    exit 1
}

if [ ! -e "$PORT" ]; then
    show_error "Can't find the DN-AUTO's serial adapter at:\n$PORT\n\nIs it plugged in? Check with:\nls -la /dev/serial/by-id/\n\n(Full log: $LOG_FILE)"
    echo "$(date): serial adapter not found at $PORT" >> "$LOG_FILE"
    exit 1
fi

# GTK 4 window (dnauto_gtk.py) by default; DNAUTO_UI=tk for the old Tk one.
# Falls back to Tk by itself if GTK 4 / libadwaita >= 1.4 isn't installed.
APP=dnauto_gui.py
if [ "${DNAUTO_UI:-gtk}" != tk ] && /usr/bin/python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Adw; assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 4)' 2>/dev/null; then
    APP=dnauto_gtk.py
fi
echo "$(date): launching $APP $PORT ${RIGCTLD:+--rigctld $RIGCTLD}" >> "$LOG_FILE"
ARGS=("$PORT"); [ -n "$RIGCTLD" ] && ARGS+=(--rigctld "$RIGCTLD")
exec /usr/bin/python3 "$APP" "${ARGS[@]}" >> "$LOG_FILE" 2>&1
