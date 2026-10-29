#!/usr/bin/env bash
# Switch DN-AUTO Monitor to the GTK 4 window. Run inside ~/dnauto-linux-monitor
# after copying the new files in. Safe to re-run.
# Rollback any time: DNAUTO_UI=tk ./dnauto_monitor_launcher.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="$HOME/.local/share/applications"
echo "== 1/3 packages"
sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-serial
echo "== 2/3 tests"
cd "$DIR"
/usr/bin/python3 -m unittest -q test_alerts 2>&1 | tail -1
/usr/bin/python3 test_dnauto_protocol.py | tail -1
echo "== 3/3 launcher"
mkdir -p "$APPS"
sed "s|@APP_DIR@|$DIR|g" net.ag7jc.DNAutoMonitor.desktop > "$APPS/net.ag7jc.DNAutoMonitor.desktop"
if [ -f "$APPS/dnauto-monitor.desktop" ]; then
    mv "$APPS/dnauto-monitor.desktop" "$DIR/dnauto-monitor.desktop.installed-old"
    echo "   old launcher moved aside"
fi
update-desktop-database "$APPS" 2>/dev/null || true
echo
echo "Done. Close the old monitor window, start DN-AUTO Monitor from the app grid"
echo "(re-pin it to the dock if it was pinned). Alarm limits: ~/.config/dnauto-monitor/limits.ini"
