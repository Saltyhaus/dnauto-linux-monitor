#!/usr/bin/env python3
"""
DN-AUTO amplifier monitor -- GTK 4 window (native Wayland).

Same data as dnauto_gui.py (the Tk window, kept for rollback) and the same
decoder: dnauto_protocol.py for the amp, rigctl_client.py for the radio.
New here:

  * desktop notifications, once per event and again when it clears:
      - amp band != radio band for longer than the grace time (MISMATCH)
      - the amp's own protection flags: SWR alarm, Imax alarm, ALARM IN,
        ALARM B, error
      - temperature over the limit
      - SWR over the limit while transmitting (SWR worked out from the FOR
        and REF readings -- both use the same range multiplier, so the
        ratio doesn't depend on the scaling guess)
      - no data from DN-AUTO for a while after it had been talking
        (cable off, amp switched off); turn that one off in the limits file
  * the protection flags are shown on screen (the Tk window never did)
  * alarm limits in ~/.config/dnauto-monitor/limits.ini -- menu > Edit limits
  * the serial port reconnects by itself if the adapter is unplugged
  * closing the window keeps watching in the background; open the monitor
    again to get the window back, menu > Quit to stop

Usage:
    python3 dnauto_gtk.py /dev/serial/by-id/... --rigctld 127.0.0.1:4534
    python3 dnauto_gtk.py --demo [--rigctld]
"""

from __future__ import annotations

import argparse
import collections
import configparser
import json
import math
import os
import queue
import random
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from dnauto_protocol import AmplifierState, ChannelUpdate, DNAutoDecoder, open_serial  # noqa: E402
from rigctl_client import freq_to_band, query_frequency  # noqa: E402

APP_ID = "net.ag7jc.DNAutoMonitor"
BANDS = ["160", "80", "40", "30", "20", "17", "15", "12", "10", "6"]
LIMITS_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dnauto-monitor" / "limits.ini"

DEFAULT_LIMITS = """\
# DN-AUTO Monitor alarm limits. Saved changes are picked up within a few seconds.
[alarms]
# temperature warning, degrees F
temp_f = 140
# SWR warning, only judged while forward power is at least swr_min_watts
swr = 2.5
swr_min_watts = 20
# seconds an out-of-limit reading must last before it alerts
hold_s = 2
# seconds the amp's band may disagree with the radio's before MISMATCH
mismatch_s = 2.5
# current warning in amps (0 = off)
current_a = 0
# alert when DN-AUTO goes quiet for this many seconds after talking (0 = off)
link_lost_s = 10
"""

BG = "#05080d"
PANEL_BG = "#0b1220"
TEAL = "#28e0e0"
AMBER = "#ffb734"
GREEN = "#39ff6a"
RED = "#ff4d4d"
DIM = "#4a5568"
WHITE = "#e8edf4"
BORDER = "#1c2740"
COLORS = ("dim", "green", "red", "amber", "teal")

CSS = f"""
@define-color accent_color {TEAL};
@define-color accent_bg_color #129a9a;
@define-color window_bg_color {BG};
@define-color window_fg_color {WHITE};
@define-color view_bg_color {PANEL_BG};
@define-color headerbar_bg_color {BG};
@define-color headerbar_fg_color {WHITE};
@define-color card_bg_color {PANEL_BG};
@define-color popover_bg_color {PANEL_BG};
@define-color dialog_bg_color {PANEL_BG};

.panel {{ background: {PANEL_BG}; border: 1px solid {BORDER}; border-radius: 10px; padding: 8px 12px; }}
.cap   {{ color: {DIM}; font-weight: bold; font-size: 0.8em; letter-spacing: 1px; }}
.mono  {{ font-family: monospace; font-weight: bold; }}
.huge  {{ font-family: monospace; font-weight: bold; font-size: 46px; }}
.big   {{ font-family: monospace; font-weight: bold; font-size: 20px; }}
.small {{ color: {DIM}; }}
.c-dim {{ color: {DIM}; }} .c-green {{ color: {GREEN}; }} .c-red {{ color: {RED}; }}
.c-amber {{ color: {AMBER}; }} .c-teal {{ color: {TEAL}; }} .c-white {{ color: {WHITE}; }}
.bandbox {{ background: {PANEL_BG}; color: {DIM}; border-radius: 6px; padding: 4px 0; font-family: monospace; }}
.bandbox.on  {{ background: {GREEN}; color: #00250c; font-weight: bold; }}
.bandbox.bad {{ background: {RED}; color: #2a0000; font-weight: bold; }}
.badge {{ background: {DIM}; color: {BG}; border-radius: 6px; padding: 2px 10px; font-weight: bold; font-size: 0.85em; }}
.flag  {{ background: {PANEL_BG}; color: {DIM}; border: 1px solid {BORDER}; border-radius: 6px; padding: 2px 8px; font-size: 0.8em; font-weight: bold; }}
.flag.on {{ background: {RED}; color: #fff; border-color: {RED}; }}
.light {{ min-width: 11px; min-height: 11px; border-radius: 999px; background: {DIM}; }}
.light.green {{ background: {GREEN}; box-shadow: 0 0 6px {GREEN}; }}
.light.red   {{ background: {RED}; box-shadow: 0 0 6px {RED}; }}
levelbar.for block.filled {{ background: {GREEN}; }}
levelbar.ref block.filled {{ background: {RED}; }}
levelbar block.empty {{ background: #0d1526; }}
levelbar trough {{ min-height: 16px; }}
"""


# ----------------------------------------------------------------- limits

def load_limits(path: Path = LIMITS_FILE) -> dict:
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_LIMITS)
        except OSError:
            pass
    cp = configparser.ConfigParser(inline_comment_prefixes=("#",))
    cp.read_string(DEFAULT_LIMITS)
    try:
        cp.read(path)
    except configparser.Error as e:
        print(f"[limits] {path}: {e} -- using defaults", flush=True)
    defaults = configparser.ConfigParser(inline_comment_prefixes=("#",))
    defaults.read_string(DEFAULT_LIMITS)
    out = {}
    for k in ("temp_f", "swr", "swr_min_watts", "hold_s", "mismatch_s", "current_a", "link_lost_s"):
        try:
            out[k] = float(cp["alarms"].get(k))
        except (TypeError, ValueError):
            print(f"[limits] bad value for {k} -- using the default", flush=True)
            out[k] = float(defaults["alarms"][k])
    return out


def swr_from(for_w: Optional[float], ref_w: Optional[float]) -> Optional[float]:
    if not for_w or ref_w is None or for_w <= 0:
        return None
    rho = math.sqrt(max(ref_w, 0) / for_w)
    if rho >= 0.999:
        return 99.0
    return (1 + rho) / (1 - rho)


# ----------------------------------------------------------------- alerts

class Alerts:
    """Turns readings into notifications. Pure logic so it's testable:
    call update(...) once per tick, get [("send"|"withdraw", id, title, body, urgent)]."""

    FLAGS = (("swr_alarm", "SWR ALARM", "The amp's own SWR protection tripped."),
             ("imax_alarm", "Imax ALARM", "The amp's over-current protection tripped."),
             ("alarm_in", "ALARM IN", "The external alarm input is active."),
             ("alarmb", "ALARM B", "The amp reports ALARM B."),
             ("error_flag", "ERROR", "The amp reports a general error."))

    def __init__(self):
        self.active = set()
        self.since = {}          # condition -> first time seen
        self.ever_linked = False

    def _held(self, key, cond, now, hold):
        if not cond:
            self.since.pop(key, None)
            return False
        self.since.setdefault(key, now)
        return now - self.since[key] >= hold

    def update(self, s: AmplifierState, now, lim, linked, last_rx, amp_band, rig_band, rig_fresh):
        out = []

        def raise_(key, title, body, urgent=True):
            if key not in self.active:
                self.active.add(key)
                out.append(("send", key, title, body, urgent))

        def clear(key, title=None):
            if key in self.active:
                self.active.discard(key)
                out.append(("withdraw", key, "", "", False))
                if title:
                    out.append(("send", key + ":ok", title, "", False))

        if linked:
            self.ever_linked = True
        # DN-AUTO went quiet
        if lim["link_lost_s"] > 0 and self.ever_linked:
            quiet = (now - last_rx) if last_rx else 0
            if quiet >= lim["link_lost_s"]:
                raise_("link", "DN-AUTO stopped talking",
                       f"No data from the amp for {quiet:.0f} s -- amp off, or the serial cable?", False)
            elif linked:
                clear("link", "DN-AUTO is talking again")
        if not linked:
            return out          # nothing below is meaningful on stale data

        # band mismatch
        mism = rig_fresh and amp_band is not None and rig_band is not None and amp_band != rig_band
        if self._held("mismatch", mism, now, lim["mismatch_s"]):
            raise_("mismatch", "AMP / RADIO BAND MISMATCH",
                   f"Amp is on {amp_band} m but the radio is on {rig_band} m.")
        elif not mism:
            clear("mismatch", f"Amp and radio agree again ({amp_band} m)" if amp_band else None)

        # the amp's own protection flags
        for attr, title, body in self.FLAGS:
            if getattr(s, attr):
                raise_(attr, f"Amp: {title}", body)
            else:
                clear(attr, f"Amp: {title} cleared")

        # temperature
        tf = s.temperature_f
        hot = tf is not None and tf >= lim["temp_f"]
        if self._held("temp", hot, now, lim["hold_s"]):
            raise_("temp", f"Amp hot: {tf:.0f} °F", f"Limit is {lim['temp_f']:.0f} °F.")
        elif tf is not None and tf < lim["temp_f"] - 5:
            clear("temp", f"Amp temperature back to {tf:.0f} °F")

        # SWR while transmitting
        fw, rw = s.for_power_w, s.ref_power_w
        swr = swr_from(fw, rw) if fw and fw >= lim["swr_min_watts"] else None
        bad = swr is not None and swr >= lim["swr"]
        if self._held("swr", bad, now, lim["hold_s"]):
            raise_("swr", f"High SWR: {swr:.1f}", f"{fw:.0f} W forward, {rw:.0f} W reflected (limit {lim['swr']}).")
        elif swr is not None and swr < lim["swr"]:
            clear("swr")

        # current
        if lim["current_a"] > 0:
            hi = s.current_a is not None and s.current_a >= lim["current_a"]
            if self._held("current", hi, now, lim["hold_s"]):
                raise_("current", f"Amp current high: {s.current_a} A", f"Limit is {lim['current_a']:.0f} A.")
            elif not hi:
                clear("current")
        return out


# --------------------------------------------------------------- history

HISTORY_S = 30 * 60      # what the graph shows
SAMPLE_S = 2             # one point every 2 s -> 900 points


class History:
    """Last 30 minutes of temperature / SWR / forward power. SWR is only
    recorded while transmitting (None otherwise), so the graph shows gaps,
    not a flat 1.0, between overs."""

    def __init__(self, span=HISTORY_S, step=SAMPLE_S):
        self.span, self.step = span, step
        self.points = collections.deque(maxlen=int(span / step) + 2)
        self._last = None

    def add(self, now, temp_f, swr, for_w):
        if self._last is not None and now - self._last < self.step:
            return False
        self._last = now
        self.points.append((now, temp_f, swr, for_w))
        while self.points and now - self.points[0][0] > self.span:
            self.points.popleft()
        return True

    def series(self, idx):
        return [(t, p[idx]) for p in self.points for t in [p[0]] if p[idx] is not None]


# ---------------------------------------------------------- status file
# A small JSON file other programs can read (Shack Services' AMP row).
# Lives in the session's runtime dir, rewritten atomically every couple of
# seconds; a file older than ~10 s means the monitor isn't running.

STATUS_FILE = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "dnauto-monitor" / "status.json"


def write_status(data, path=STATUS_FILE):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError as e:
        print(f"[status] {e}", flush=True)


# ------------------------------------------------------------------ feeds

def serial_feed(port, on_update, on_status, stop):
    """Read DN-AUTO forever; reopen the port if it goes away."""
    while not stop.is_set():
        try:
            ser = open_serial(port)
        except Exception as e:
            on_status(f"can't open {os.path.basename(port)}: {e}")
            stop.wait(3)
            continue
        on_status(None)
        decoder = DNAutoDecoder(on_update)
        try:
            while not stop.is_set():
                data = ser.read(256)
                if data:
                    decoder.feed(data)
        except Exception as e:
            on_status(f"serial error: {e}")
        finally:
            try:
                ser.close()
            except Exception:
                pass
        stop.wait(2)


def demo_feed(on_update, on_status, stop):
    decoder = DNAutoDecoder(on_update)

    def ch(c, v):
        return bytes([0xC0 | c, 0xE0 | (v & 0x0F), 0xF0 | ((v >> 4) & 0x0F)])
    t = 0
    while not stop.is_set():
        code = 1 + (t // 40) % 10
        temp = 25 + int(15 * random.random())
        for_raw = int(30 + 10 * random.random())
        ref_raw = int(for_raw * 0.05 * random.random())
        status11 = (0x09 if temp > 35 else 0) | (0x80 if t % 200 > 100 else 0)
        decoder.feed(b"".join([ch(3, code), ch(4, temp), ch(8, 5), ch(13, for_raw), ch(15, ref_raw),
                               ch(10, 3 + int(2 * random.random())), ch(9, 1 if temp > 35 else 0),
                               ch(11, status11), ch(12, 0), ch(14, 5)]))
        t += 1
        stop.wait(0.3)


def rig_feed(host, port, on_freq, stop):
    while not stop.is_set():
        f = query_frequency(host, port)
        if f is not None:
            on_freq(f)
        stop.wait(1.0)


# ------------------------------------------------------------------ window

class Light(Gtk.Box):
    def __init__(self):
        super().__init__(valign=Gtk.Align.CENTER)
        self.add_css_class("light")
        self._c = "dim"

    def set_color(self, c):
        if c != self._c:
            for x in COLORS:
                self.remove_css_class(x)
            if c != "dim":
                self.add_css_class(c)
            self._c = c


def tint(w, c):
    for x in COLORS + ("white",):
        w.remove_css_class("c-" + x)
    if c:
        w.add_css_class("c-" + c)


class MonitorWindow:
    def __init__(self, app, args):
        self.app = app
        self.args = args
        self.state = AmplifierState()
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.last_rx = 0.0
        self.port_status = None
        self.rig_freq = None
        self.rig_band = None
        self.rig_last = 0.0
        self.temp_unit = "F"
        self.alerts = Alerts()
        self.limits = load_limits()
        self._limits_mtime = self._mtime()
        self.mismatch_since = None

        prov = Gtk.CssProvider()
        try:
            prov.load_from_string(CSS)
        except AttributeError:
            prov.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        self._build()

        put = self.q.put
        if args.demo:
            threading.Thread(target=demo_feed, args=(lambda u: put(("u", u)), lambda s: put(("s", s)), self.stop),
                             daemon=True).start()
        else:
            threading.Thread(target=serial_feed, args=(args.port, lambda u: put(("u", u)),
                                                       lambda s: put(("s", s)), self.stop), daemon=True).start()
        if args.rigctld:
            host, _, port = args.rigctld.rpartition(":")
            threading.Thread(target=rig_feed, args=(host or "127.0.0.1", int(port), lambda f: put(("f", f)),
                                                    self.stop), daemon=True).start()
        GLib.timeout_add(150, self._drain)
        GLib.timeout_add(500, self._tick)

    def _mtime(self):
        try:
            return LIMITS_FILE.stat().st_mtime
        except OSError:
            return None

    # ---------------------------------------------------------------- layout

    def _build(self):
        win = Adw.ApplicationWindow(application=self.app, title="DN-AUTO Monitor")
        win.set_default_size(820, 720)
        win.set_size_request(380, 360)
        win.connect("close-request", self._on_close)
        self.win = win

        header = Adw.HeaderBar()
        t = Gtk.Label(label="DN-AUTO")
        t.add_css_class("mono")
        tint(t, "dim")
        header.pack_start(t)
        self.device_lbl = Gtk.Label(label="No Device")
        self.device_lbl.add_css_class("badge")
        header.pack_start(self.device_lbl)
        if self.args.demo:
            d = Gtk.Label(label="DEMO DATA")
            tint(d, "amber")
            header.pack_start(d)
        menu = Gio.Menu()
        menu.append("Edit alarm limits…", "app.limits")
        menu.append("Quit (stop watching)", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        st = Gtk.Box(spacing=12)
        self.link_light, self.link_lbl = Light(), Gtk.Label(label="NO SIGNAL")
        self.rig_light = Light()
        for light, lbl in ((self.link_light, self.link_lbl), (self.rig_light, Gtk.Label(label="RIG"))):
            b = Gtk.Box(spacing=5)
            b.append(light)
            lbl.add_css_class("cap")
            b.append(lbl)
            st.append(b)
        if not self.args.rigctld:
            st.get_last_child().set_visible(False)
        header.pack_end(st)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                       margin_start=12, margin_end=12, margin_top=8, margin_bottom=12)

        strip = Gtk.Box(spacing=4, homogeneous=True)
        self.band_boxes = {}
        for b in BANDS:
            lbl = Gtk.Label(label=b)
            lbl.add_css_class("bandbox")
            strip.append(lbl)
            self.band_boxes[b] = lbl
        page.append(strip)

        main = Gtk.Box(spacing=8, homogeneous=True)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left.add_css_class("panel")
        cap = Gtk.Label(label="BAND (amp)", xalign=0)
        cap.add_css_class("cap")
        left.append(cap)
        self.band_big = Gtk.Label(label="--", xalign=0)
        self.band_big.add_css_class("huge")
        tint(self.band_big, "teal")
        left.append(self.band_big)
        self.freq_lbl = Gtk.Label(label="", xalign=0)
        self.freq_lbl.add_css_class("small")
        left.append(self.freq_lbl)
        self.sync_lbl = Gtk.Label(label="", xalign=0, wrap=True)
        self.sync_lbl.add_css_class("mono")
        left.append(self.sync_lbl)
        main.append(left)

        meters = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.for_bar, self.for_val = self._meter(meters, "FOR (W)", "for", "green")
        self.ref_bar, self.ref_val = self._meter(meters, "REF (W)", "ref", "red")
        main.append(meters)
        page.append(main)

        stats = Gtk.Box(spacing=6, homogeneous=True)
        self.temp_val = self._stat(stats, "TEMP", "teal", click=self._toggle_unit)
        self.cur_val = self._stat(stats, "CURRENT", "amber")
        self.swr_val = self._stat(stats, "SWR", "amber")
        self.fan_val = self._stat(stats, "FAN", "green")
        self.ant_val = self._stat(stats, "ANT", "teal")
        page.append(stats)

        flags = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.flag_lbls = {}
        for attr, title, _ in Alerts.FLAGS:
            lbl = Gtk.Label(label=title)
            lbl.add_css_class("flag")
            flags.append(lbl)
            self.flag_lbls[attr] = lbl
        page.append(flags)

        self.history = History()
        self.graph = Gtk.DrawingArea(content_height=170, hexpand=True, vexpand=True)
        self.graph.set_draw_func(self._draw_graph)
        gbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        gbox.add_css_class("panel")
        gcap = Gtk.Label(xalign=0, use_markup=True,
                         label=f"<span foreground='{DIM}'><b>LAST 30 MIN</b></span>   "
                               f"<span foreground='{TEAL}'>━ temp °F</span>   "
                               f"<span foreground='{AMBER}'>━ SWR</span>   "
                               f"<span foreground='#2f6b44'>▮ FOR W</span>   "
                               f"<span foreground='{DIM}'>┄ limits</span>")
        gbox.append(gcap)
        gbox.append(self.graph)
        gbox.set_vexpand(True)
        page.append(gbox)

        self.port_lbl = Gtk.Label(label="", wrap=True)
        tint(self.port_lbl, "red")
        page.append(self.port_lbl)

        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=Gtk.ScrolledWindow(child=page, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                                                vexpand=True))
        tv.set_content(self.toasts)
        win.set_content(tv)
        win.present()

    def _meter(self, parent, caption, css, color):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("panel")
        cap = Gtk.Label(label=caption, xalign=0)
        cap.add_css_class("cap")
        box.append(cap)
        row = Gtk.Box(spacing=8)
        bar = Gtk.LevelBar(min_value=0, max_value=2000, hexpand=True, valign=Gtk.Align.CENTER)
        for name in ("low", "high", "full"):
            bar.remove_offset_value(name)
        bar.add_css_class(css)
        row.append(bar)
        val = Gtk.Label(label="---", width_chars=5, xalign=1)
        val.add_css_class("big")
        tint(val, color)
        row.append(val)
        box.append(row)
        parent.append(box)
        return bar, val

    def _stat(self, parent, caption, color, click=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("panel")
        cap = Gtk.Label(label=caption)
        cap.add_css_class("cap")
        box.append(cap)
        val = Gtk.Label(label="--")
        val.add_css_class("big")
        tint(val, color)
        box.append(val)
        if click:
            g = Gtk.GestureClick()
            g.connect("released", lambda *_a: click())
            box.add_controller(g)
            box.set_tooltip_text("Click for °F / °C")
        parent.append(box)
        return val

    def _draw_graph(self, _area, cr, w, h):
        def rgb(hexc):
            hexc = hexc.lstrip("#")
            return tuple(int(hexc[i:i + 2], 16) / 255 for i in (0, 2, 4))

        now = time.time()
        left, right, top, bot = 34, 34, 6, 16
        pw, ph = max(w - left - right, 10), max(h - top - bot, 10)
        x = lambda t: left + pw * (1 - (now - t) / HISTORY_S)
        lim = self.limits
        temps = self.history.series(1)
        tvals = [v for _t, v in temps] + [lim["temp_f"]]
        t_lo = min(tvals + [60]) - 5
        t_hi = max(tvals) + 5
        ty = lambda v: top + ph * (1 - (v - t_lo) / max(t_hi - t_lo, 1))
        s_hi = max(3.0, lim["swr"] + 0.5)
        sy = lambda v: top + ph * (1 - (min(v, s_hi) - 1) / (s_hi - 1))
        pmax = max([v for _t, v in self.history.series(3)] + [100])

        cr.set_font_size(9)
        # grid: a line every 5 minutes
        cr.set_source_rgb(*rgb(BORDER))
        cr.set_line_width(1)
        for m in range(0, 31, 5):
            gx = left + pw * (1 - m / 30)
            cr.move_to(gx, top)
            cr.line_to(gx, top + ph)
            cr.stroke()
            cr.set_source_rgb(*rgb(DIM))
            cr.move_to(gx - 8, h - 3)
            cr.show_text("now" if m == 0 else f"-{m}m")
            cr.set_source_rgb(*rgb(BORDER))
        # forward power as faint bars
        cr.set_source_rgb(*rgb("#173a26"))
        bw = max(pw / (HISTORY_S / SAMPLE_S), 1)
        for t, v in self.history.series(3):
            if v > 0:
                bh = ph * 0.35 * min(v / pmax, 1)       # bottom third: context, not the main event
                cr.rectangle(x(t), top + ph - bh, bw, bh)
        cr.fill()
        # limit lines
        cr.set_dash([4, 4])
        for yv, col in ((ty(lim["temp_f"]), TEAL), (sy(lim["swr"]), AMBER)):
            cr.set_source_rgba(*rgb(col), 0.6)
            cr.move_to(left, yv)
            cr.line_to(left + pw, yv)
            cr.stroke()
        cr.set_dash([])
        # series: temp (left axis), SWR (right axis); gaps where there's no data
        cr.set_line_width(2)
        for pts, fy, col in ((temps, ty, TEAL), (self.history.series(2), sy, AMBER)):
            cr.set_source_rgb(*rgb(col))
            prev = None
            for t, v in pts:
                px, py = x(t), fy(v)
                if prev is not None and t - prev <= SAMPLE_S * 3:
                    cr.line_to(px, py)
                else:
                    cr.move_to(px, py)
                prev = t
            cr.stroke()
        # axis labels
        cr.set_source_rgb(*rgb(TEAL))
        for v in (t_lo + 5, lim["temp_f"]):
            cr.move_to(2, ty(v) + 3)
            cr.show_text(f"{v:.0f}°")
        cr.set_source_rgb(*rgb(AMBER))
        for v in (1.0, lim["swr"]):
            cr.move_to(left + pw + 4, sy(v) + 3)
            cr.show_text(f"{v:.1f}")
        if not self.history.points:
            cr.set_source_rgb(*rgb(DIM))
            cr.set_font_size(11)
            cr.move_to(left + pw / 2 - 70, top + ph / 2)
            cr.show_text("waiting for data from the amp…")

    def _toggle_unit(self):
        self.temp_unit = "C" if self.temp_unit == "F" else "F"
        self._render()

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    # ------------------------------------------------------------- data

    def _drain(self):
        changed = False
        try:
            while True:
                kind, v = self.q.get_nowait()
                if kind == "u":
                    self.state.apply(v)
                    self.last_rx = time.time()
                elif kind == "f":
                    self.rig_freq, self.rig_band, self.rig_last = v, freq_to_band(v), time.time()
                elif kind == "s":
                    self.port_status = v
                changed = True
        except queue.Empty:
            pass
        if changed:
            self._render()
        return True

    def _linked(self, now):
        return bool(self.last_rx) and now - self.last_rx < 3.0

    def _rig_fresh(self, now):
        return bool(self.args.rigctld) and bool(self.rig_last) and now - self.rig_last < 5.0

    def _amp_band(self):
        b = self.state.band
        return BANDS[b] if b is not None and 0 <= b < len(BANDS) else None

    def _tick(self):
        now = time.time()
        mt = self._mtime()
        if mt != self._limits_mtime:
            self._limits_mtime = mt
            self.limits = load_limits()
            self.toast("Alarm limits reloaded")
        linked = self._linked(now)
        self.link_light.set_color("green" if linked else "red")
        self.link_lbl.set_label("LINKED" if linked else "NO SIGNAL")
        self.rig_light.set_color("green" if self._rig_fresh(now) else "red")
        self.port_lbl.set_label(self.port_status or "")
        self.port_lbl.set_visible(bool(self.port_status))
        self._render()
        s, lim = self.state, self.limits
        fw = s.for_power_w
        swr = swr_from(fw, s.ref_power_w) if fw and fw >= lim["swr_min_watts"] else None
        if linked and self.history.add(now, s.temperature_f, swr, fw):
            self.graph.queue_draw()
        for action, nid, title, body, urgent in self.alerts.update(
                self.state, now, self.limits, linked, self.last_rx, self._amp_band(), self.rig_band,
                self._rig_fresh(now)):
            if action == "send":
                n = Gio.Notification.new(title)
                if body:
                    n.set_body(body)
                n.set_priority(Gio.NotificationPriority.URGENT if urgent else Gio.NotificationPriority.NORMAL)
                n.set_default_action("app.show")
                self.app.send_notification(nid, n)
            else:
                self.app.withdraw_notification(nid)
        if now - getattr(self, "_status_at", 0) >= 2:
            self._status_at = now
            titles = {a: t for a, t, _ in Alerts.FLAGS}
            titles.update(mismatch="MISMATCH", temp="HOT", swr="HIGH SWR", current="CURRENT", link="NO DATA")
            write_status({
                "time": now, "pid": os.getpid(), "demo": bool(self.args.demo),
                "linked": linked, "rig": self._rig_fresh(now),
                "amp_band": self._amp_band(), "rig_band": self.rig_band if self._rig_fresh(now) else None,
                "temp_f": round(s.temperature_f, 1) if s.temperature_f is not None else None,
                "swr": round(swr, 2) if swr is not None else None,
                "for_w": round(fw) if fw is not None else None,
                "alerts": [titles.get(a, a.upper()) for a in sorted(self.alerts.active)],
            })
        return True

    def _render(self):
        s, now = self.state, time.time()
        self.device_lbl.set_label(s.device or "No Device")
        amp = self._amp_band()
        mism = "mismatch" in self.alerts.active
        for b, lbl in self.band_boxes.items():
            lbl.remove_css_class("on")
            lbl.remove_css_class("bad")
            if b == amp:
                lbl.add_css_class("bad" if mism else "on")
        self.band_big.set_label(f"{amp}M" if amp else "--")
        tint(self.band_big, "red" if mism else "teal")

        if self._rig_fresh(now):
            self.freq_lbl.set_label(f"radio {self.rig_freq / 1e6:.6f} MHz" if self.rig_freq else "")
            if mism:
                self.sync_lbl.set_label(f"⚠ MISMATCH -- amp {amp} m, radio {self.rig_band} m")
                tint(self.sync_lbl, "red")
            elif amp and self.rig_band:
                self.sync_lbl.set_label("✓ matches radio")
                tint(self.sync_lbl, "green")
            else:
                self.sync_lbl.set_label("radio out of the amp's bands" if amp else "")
                tint(self.sync_lbl, "dim")
        elif self.args.rigctld:
            self.freq_lbl.set_label("")
            self.sync_lbl.set_label("(no rig data -- can't cross-check)")
            tint(self.sync_lbl, "dim")

        fw, rw = s.for_power_w, s.ref_power_w
        self.for_bar.set_value(min(fw or 0, 2000))
        self.ref_bar.set_value(min(rw or 0, 2000))
        self.for_val.set_label(f"{fw:.0f}" if fw is not None else "---")
        self.ref_val.set_label(f"{rw:.0f}" if rw is not None else "---")

        lim = self.limits
        if s.temperature_c is not None:
            self.temp_val.set_label(f"{s.temperature_f:.0f} °F" if self.temp_unit == "F" else f"{s.temperature_c} °C")
            tint(self.temp_val, "red" if s.temperature_f >= lim["temp_f"] else "teal")
        else:
            self.temp_val.set_label(f"-- °{self.temp_unit}")
        self.cur_val.set_label(f"{s.current_a} A" if s.current_a is not None else "-- A")
        swr = swr_from(fw, rw) if fw and fw >= lim["swr_min_watts"] else None
        self.swr_val.set_label(f"{swr:.1f}" if swr is not None else "--")
        tint(self.swr_val, "red" if swr is not None and swr >= lim["swr"] else "amber")
        self.swr_val.set_tooltip_text("Worked out from FOR/REF while transmitting "
                                      f"(at least {lim['swr_min_watts']:.0f} W)")
        self.fan_val.set_label((s.fan or "--").upper())
        self.ant_val.set_label(s.antenna or "--")
        for attr, lbl in self.flag_lbls.items():
            if getattr(s, attr):
                lbl.add_css_class("on")
            else:
                lbl.remove_css_class("on")

    # --------------------------------------------------------- lifecycle

    def show(self):
        self.app.release_bg()
        self.win.set_visible(True)
        self.win.present()

    def _on_close(self, _w):
        if self.app.quitting:
            return False
        self.app.hold_bg()
        self.win.set_visible(False)
        if not getattr(self, "_told", False):
            self._told = True
            n = Gio.Notification.new("DN-AUTO Monitor is still watching")
            n.set_body("You'll get a notification for a band mismatch or an amp alarm. "
                       "Open DN-AUTO Monitor to see the window, or Quit from its menu.")
            n.set_default_action("app.show")
            self.app.send_notification("bg", n)
        return True


class MonitorApp(Adw.Application):
    def __init__(self, args):
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        super().__init__(application_id=APP_ID, flags=flags)
        self.args = args
        self.window = None
        self.quitting = False
        self._held = False
        for name, cb in (("show", lambda: self.window and self.window.show()),
                         ("limits", self._edit_limits), ("quit", self._quit)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(a)

    def do_activate(self):
        if self.window:
            self.window.show()
        else:
            self.window = MonitorWindow(self, self.args)

    def hold_bg(self):
        if not self._held:
            self.hold()
            self._held = True

    def release_bg(self):
        if self._held:
            self._held = False
            self.release()

    def _edit_limits(self):
        load_limits()
        f = Gio.File.new_for_path(str(LIMITS_FILE))
        if hasattr(Gtk, "FileLauncher"):
            Gtk.FileLauncher.new(f).launch(self.window.win if self.window else None, None, None, None)
        if self.window:
            self.window.toast("Edit and save -- limits reload by themselves")

    def _quit(self):
        self.quitting = True
        try:
            STATUS_FILE.unlink()
        except OSError:
            pass
        if self.window:
            self.window.stop.set()
            self.window.win.destroy()
        self.release_bg()
        self.quit()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", nargs="?", help="serial device, e.g. /dev/serial/by-id/...")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no hardware")
    ap.add_argument("--rigctld", nargs="?", const="127.0.0.1:4532", default=None, metavar="HOST:PORT",
                    help="cross-check the amp's band against this rigctld")
    args = ap.parse_args()
    if not args.demo and not args.port:
        ap.error("give a serial port or --demo")
    return MonitorApp(args).run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
