#!/usr/bin/env python3
"""
DN-AUTO amplifier monitor -- dark "radio display" style desktop GUI.

Usage:
    python3 dnauto_gui.py /dev/ttyUSB0        # live, from real hardware
    python3 dnauto_gui.py --demo              # no hardware needed, feeds
                                               # itself fake data so you can
                                               # see what it looks like

    # Optionally, get the BAND readout from a running Hamlib rigctld
    # instance instead of DN-AUTO's own (unconfirmed) band channel --
    # more reliable, since it comes straight from the radio:
    python3 dnauto_gui.py /dev/ttyUSB0 --rigctld
    python3 dnauto_gui.py /dev/ttyUSB0 --rigctld 127.0.0.1:4532

Requires: pip install pyserial   (not needed for --demo)
Requires: tkinter -- usually already installed with Python; if not:
    Debian/Ubuntu:  sudo apt install python3-tk
    Fedora:         sudo dnf install python3-tkinter
    Arch:           sudo pacman -S tk

Only channels 4 (temperature), 8 (antenna), 9 (fan) and 14 (radio type) are
confirmed against real hardware so far -- see README.md. Everything else
shown here (power, current, SWR) is wired up to a *placeholder* channel
number and will show "--" until you finish the mapping with your real
amplifier and fill in the CHANNEL_MAP below. Band is the exception: pass
--rigctld and it's read directly from your radio via Hamlib instead, no
mapping needed.
"""

from __future__ import annotations

import argparse
import os
import queue
import random
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from typing import Optional

from dnauto_protocol import AmplifierState, ChannelUpdate, DNAutoDecoder, open_serial
from rigctl_client import freq_to_band, query_frequency

# ---------------------------------------------------------------------------
# Palette -- dark navy/black background with teal digits and amber/green
# accents, loosely evoking the FT-991A's own colour scheme rather than
# trying to be a pixel-perfect clone (we don't have spectrum/waterfall data
# to show -- the amplifier only reports status, not RF spectrum).
# ---------------------------------------------------------------------------
BG = "#05080d"
PANEL_BG = "#0b1220"
FG_TEAL = "#28e0e0"
FG_AMBER = "#ffb734"
FG_GREEN = "#39ff6a"
FG_RED = "#ff4d4d"
FG_DIM = "#4a5568"
FG_WHITE = "#e8edf4"
BORDER = "#1c2740"

BANDS = ["160", "80", "40", "30", "20", "17", "15", "12", "10", "6"]

# How long the amp's own reported band must disagree with rigctld's before
# it's flagged as a real mismatch/alarm, rather than normal relay-switching
# lag right after the radio changes bands.
MISMATCH_GRACE_S = 2.5

# Band, FOR/REF power, current, antenna, and device are all now decoded
# directly by AmplifierState (dnauto_protocol.py) from disassembly-
# confirmed channels -- see that module's docstring for the full
# per-channel writeup. SWR is the one field still genuinely unmapped;
# fill in its real channel number here once you've found it empirically
# (dnauto_monitor.py --raw is the tool for that -- see README.md).
CHANNEL_MAP = {
    "swr": None,  # unconfirmed
}


class ToggleSwitch(tk.Canvas):
    """A small sliding on/off switch (no stock Tkinter widget looks like
    this) -- click anywhere on it to flip state, knob animates across.
    Calls `command(new_state: bool)` after each flip, if given."""

    WIDTH = 42
    HEIGHT = 22

    def __init__(self, parent, command=None, initial=False, bg=BG, on_color=FG_GREEN, off_color=FG_DIM):
        super().__init__(parent, width=self.WIDTH, height=self.HEIGHT, bg=bg, highlightthickness=0, cursor="hand2")
        self.command = command
        self.on_color = on_color
        self.off_color = off_color
        self.state = initial
        self._knob_x = self._on_x() if initial else self._off_x()
        self._draw()
        self.bind("<Button-1>", self._on_click)

    def _off_x(self):
        return self.HEIGHT / 2

    def _on_x(self):
        return self.WIDTH - self.HEIGHT / 2

    def _draw(self):
        self.delete("all")
        track_color = self.on_color if self.state else self.off_color
        h = self.HEIGHT
        w = self.WIDTH
        r = h / 2
        self.create_oval(0, 0, h, h, fill=track_color, outline="")
        self.create_oval(w - h, 0, w, h, fill=track_color, outline="")
        self.create_rectangle(r, 0, w - r, h, fill=track_color, outline="")
        knob_r = r - 3
        x = self._knob_x
        self.create_oval(x - knob_r, r - knob_r, x + knob_r, r + knob_r, fill="#f5f7fa", outline="")

    def _on_click(self, _event):
        self.state = not self.state
        target_x = self._on_x() if self.state else self._off_x()
        self._animate(target_x)
        if self.command:
            self.command(self.state)

    def _animate(self, target_x, steps=6):
        start_x = self._knob_x
        delta = (target_x - start_x) / steps

        def step(i):
            self._knob_x = start_x + delta * i
            self._draw()
            if i < steps:
                self.after(12, lambda: step(i + 1))

        step(1)


class DNAutoGUI:
    def __init__(self, root: tk.Tk, demo: bool = False, rig_enabled: bool = False):
        self.root = root
        self.demo = demo
        self.rig_enabled = rig_enabled
        self.state = AmplifierState()
        self.temp_unit = "F"
        self.update_queue: "queue.Queue[ChannelUpdate]" = queue.Queue()
        self.rig_queue: "queue.Queue[float]" = queue.Queue()
        self.connected = False
        self.last_rx = 0.0
        self.rig_freq_hz: Optional[float] = None
        self.rig_band: Optional[str] = None
        self.rig_last_ok = 0.0
        self.mismatch_since: Optional[float] = None  # for the sync-alarm debounce

        root.title("DN-AUTO Monitor")
        root.configure(bg=BG)
        root.geometry("760x420")
        root.minsize(680, 380)

        # Window icon -- this is what shows in the taskbar/window list, the
        # alt-tab switcher, and (on desktops that support one) the window's
        # own entry in a system tray/notification-area applet. Tk's PNG
        # support (built into Tk 8.6+) reads assets/icon.png directly, no
        # extra library needed. Never let a missing/bad icon file crash the
        # app -- it's cosmetic.
        try:
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png")
            self._icon_img = tk.PhotoImage(file=icon_path)  # keep a reference, Tk drops GC'd images
            root.iconphoto(True, self._icon_img)
        except Exception:
            pass

        self._build_fonts()
        self._build_layout()
        self._poll_queue()
        self._tick_clock_and_link()

    # -- fonts ---------------------------------------------------------
    def _build_fonts(self):
        self.f_huge = tkfont.Font(family="DejaVu Sans Mono", size=40, weight="bold")
        self.f_big = tkfont.Font(family="DejaVu Sans Mono", size=22, weight="bold")
        self.f_med = tkfont.Font(family="DejaVu Sans Mono", size=14, weight="bold")
        self.f_small = tkfont.Font(family="DejaVu Sans", size=10)
        self.f_label = tkfont.Font(family="DejaVu Sans", size=9, weight="bold")

    # -- layout ----------------------------------------------------------
    def _build_layout(self):
        outer = tk.Frame(self.root, bg=BORDER, bd=0)
        outer.pack(fill="both", expand=True, padx=8, pady=8)
        screen = tk.Frame(outer, bg=BG, bd=0)
        screen.pack(fill="both", expand=True, padx=2, pady=2)

        # ---- top bar: title, device badge, link status ----
        top = tk.Frame(screen, bg=BG)
        top.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(top, text="DN-AUTO", font=self.f_med, fg=FG_DIM, bg=BG).pack(side="left")
        self.device_lbl = tk.Label(
            top, text="No Device", font=self.f_label, fg=BG, bg=FG_DIM, padx=10, pady=3
        )
        self.device_lbl.pack(side="left", padx=12)
        self.link_lbl = tk.Label(top, text="● NO SIGNAL", font=self.f_label, fg=FG_RED, bg=BG)
        self.link_lbl.pack(side="right")
        self.rig_lbl = tk.Label(top, text="● RIG", font=self.f_label, fg=FG_DIM, bg=BG)
        if self.rig_enabled:
            self.rig_lbl.pack(side="right", padx=12)
        if self.demo:
            tk.Label(top, text="DEMO DATA", font=self.f_label, fg=FG_AMBER, bg=BG).pack(
                side="right", padx=12
            )

        # ---- always-on-top toggle ----
        ontop_frame = tk.Frame(top, bg=BG)
        ontop_frame.pack(side="right", padx=12)
        tk.Label(ontop_frame, text="ON TOP", font=self.f_label, fg=FG_DIM, bg=BG).pack(
            side="left", padx=(0, 6)
        )
        self.ontop_toggle = ToggleSwitch(ontop_frame, command=self._on_ontop_toggle, initial=False, bg=BG)
        self.ontop_toggle.pack(side="left")

        # ---- band strip ----
        band_row = tk.Frame(screen, bg=BG)
        band_row.pack(fill="x", padx=14, pady=(10, 4))
        self.band_boxes = {}
        for b in BANDS:
            lbl = tk.Label(
                band_row, text=b, font=self.f_small, fg=FG_DIM, bg=PANEL_BG, width=4, pady=4
            )
            lbl.pack(side="left", padx=2)
            self.band_boxes[b] = lbl

        # ---- main readout row: big band + meters ----
        main = tk.Frame(screen, bg=BG)
        main.pack(fill="both", expand=True, padx=14, pady=6)

        left = tk.Frame(main, bg=PANEL_BG, bd=0)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6), pady=4)
        tk.Label(left, text="BAND", font=self.f_label, fg=FG_DIM, bg=PANEL_BG).pack(
            anchor="w", padx=10, pady=(8, 0)
        )
        self.band_big = tk.Label(left, text="--", font=self.f_huge, fg=FG_TEAL, bg=PANEL_BG)
        self.band_big.pack(anchor="w", padx=10, pady=(0, 0))
        self.freq_lbl = tk.Label(left, text="", font=self.f_small, fg=FG_DIM, bg=PANEL_BG)
        self.freq_lbl.pack(anchor="w", padx=10, pady=(0, 2))
        self.sync_lbl = tk.Label(left, text="", font=self.f_label, fg=FG_DIM, bg=PANEL_BG)
        if self.rig_enabled:
            self.sync_lbl.pack(anchor="w", padx=10, pady=(0, 8))

        meters = tk.Frame(main, bg=BG)
        meters.pack(side="right", fill="both", expand=True)

        self.for_bar = self._make_meter(meters, "FOR (W)", FG_GREEN)
        self.ref_bar = self._make_meter(meters, "REF (W)", FG_RED)

        # ---- bottom row: temp / current / swr / fan / ant ----
        bottom = tk.Frame(screen, bg=BG)
        bottom.pack(fill="x", padx=14, pady=(4, 12))

        self.temp_val = self._make_stat(bottom, "TEMP", f"-- {self.temp_unit}", FG_TEAL, click=self._toggle_temp_unit)
        self.current_val = self._make_stat(bottom, "CURRENT", "-- A", FG_AMBER)
        self.swr_val = self._make_stat(bottom, "SWR", "--", FG_AMBER)
        self.fan_val = self._make_stat(bottom, "FAN", "--", FG_GREEN)
        self.ant_val = self._make_stat(bottom, "ANT", "--", FG_TEAL)

    def _make_meter(self, parent, label, color):
        frame = tk.Frame(parent, bg=PANEL_BG)
        frame.pack(fill="x", pady=4)
        tk.Label(frame, text=label, font=self.f_label, fg=FG_DIM, bg=PANEL_BG).pack(
            anchor="w", padx=10, pady=(6, 0)
        )
        row = tk.Frame(frame, bg=PANEL_BG)
        row.pack(fill="x", padx=10, pady=(0, 8))
        canvas = tk.Canvas(row, height=18, bg="#0d1526", highlightthickness=1, highlightbackground=BORDER)
        canvas.pack(side="left", fill="x", expand=True)
        value_lbl = tk.Label(row, text="---", font=self.f_med, fg=color, bg=PANEL_BG, width=5)
        value_lbl.pack(side="left", padx=(8, 0))
        return {"canvas": canvas, "label": value_lbl, "color": color, "value": 0, "max": 2000}

    def _make_stat(self, parent, label, initial, color, click=None):
        box = tk.Frame(parent, bg=PANEL_BG)
        box.pack(side="left", fill="both", expand=True, padx=4)
        tk.Label(box, text=label, font=self.f_label, fg=FG_DIM, bg=PANEL_BG).pack(pady=(8, 0))
        val = tk.Label(box, text=initial, font=self.f_med, fg=color, bg=PANEL_BG)
        val.pack(pady=(0, 8))
        if click:
            for w in (box, val):
                w.bind("<Button-1>", lambda e: click())
                w.configure(cursor="hand2")
        return val

    def _toggle_temp_unit(self):
        self.temp_unit = "F" if self.temp_unit == "C" else "C"
        self._render()

    def _on_ontop_toggle(self, state: bool):
        # Keeps the window pinned above every other window/desktop process,
        # not just other app windows -- window managers implement this as
        # "always on top" / "keep above". Tk's -topmost attribute is the
        # portable way to ask for that on Linux/X11 and Wayland alike
        # (support varies a little by window manager, but every mainstream
        # Linux desktop honors it).
        self.root.attributes("-topmost", state)

    # -- data plumbing ---------------------------------------------------
    def on_update(self, update: ChannelUpdate):
        self.update_queue.put(update)

    def on_rig_update(self, freq_hz: float):
        self.rig_queue.put(freq_hz)

    def _poll_queue(self):
        got_any = False
        try:
            while True:
                update = self.update_queue.get_nowait()
                self.state.apply(update)
                got_any = True
        except queue.Empty:
            pass
        if got_any:
            self.last_rx = time.time()

        rig_got = False
        try:
            while True:
                freq_hz = self.rig_queue.get_nowait()
                self.rig_freq_hz = freq_hz
                self.rig_band = freq_to_band(freq_hz)
                self.rig_last_ok = time.time()
                rig_got = True
        except queue.Empty:
            pass

        if got_any or rig_got:
            self._render()
        self.root.after(100, self._poll_queue)

    def _tick_clock_and_link(self):
        alive = (time.time() - self.last_rx) < 3.0 if self.last_rx else False
        if alive:
            self.link_lbl.configure(text="● LINKED", fg=FG_GREEN)
        else:
            self.link_lbl.configure(text="● NO SIGNAL", fg=FG_RED)

        if self.rig_enabled:
            rig_alive = (time.time() - self.rig_last_ok) < 5.0 if self.rig_last_ok else False
            self.rig_lbl.configure(text="● RIG", fg=FG_GREEN if rig_alive else FG_RED)

        self.root.after(500, self._tick_clock_and_link)

    def _set_meter(self, meter, value):
        meter["value"] = value
        meter["label"].configure(text=str(value))
        c = meter["canvas"]
        c.update_idletasks()
        w = max(c.winfo_width(), 1)
        h = int(c.winfo_height())
        frac = max(0.0, min(1.0, value / meter["max"]))
        c.delete("bar")
        c.create_rectangle(0, 0, int(w * frac), h, fill=meter["color"], width=0, tags="bar")

    def _render(self):
        s = self.state

        self.device_lbl.configure(text=s.device or "No Device")

        for b, lbl in self.band_boxes.items():
            lbl.configure(bg=PANEL_BG, fg=FG_DIM)

        # Primary band reading always comes from DN-AUTO's own channel 3 --
        # empirically calibrated (calibrate_band.py) to track the real ABCD
        # code live. This is deliberately NOT replaced by rigctld: the whole
        # point of this monitor is to show what the amplifier itself is
        # actually doing, not what the radio merely requested -- otherwise
        # a stuck relay or a dead decoder line could never be noticed here.
        band_val = s.band
        amp_name = BANDS[band_val] if band_val is not None and 0 <= band_val < len(BANDS) else None
        if amp_name is not None:
            self.band_boxes[amp_name].configure(bg=FG_GREEN, fg="#00250c")
            self.band_big.configure(text=f"{amp_name}M")
        else:
            self.band_big.configure(text="--")

        rig_fresh = self.rig_enabled and bool(self.rig_last_ok) and (time.time() - self.rig_last_ok) < 5.0
        if rig_fresh:
            self.freq_lbl.configure(text=f"{self.rig_freq_hz / 1e6:.6f} MHz" if self.rig_freq_hz else "")
            # Cross-check: does what the amp is actually on agree with what
            # the radio commanded? Debounced by MISMATCH_GRACE_S so a relay
            # mid-switch for a moment doesn't false-alarm.
            mismatched = amp_name is not None and self.rig_band is not None and amp_name != self.rig_band
            if mismatched:
                if self.mismatch_since is None:
                    self.mismatch_since = time.time()
            else:
                self.mismatch_since = None

            if self.mismatch_since is not None and (time.time() - self.mismatch_since) >= MISMATCH_GRACE_S:
                self.sync_lbl.configure(
                    text=f"⚠ MISMATCH -- amp={amp_name}m  radio={self.rig_band}m", fg=FG_RED
                )
                self.band_big.configure(fg=FG_RED)
            else:
                self.sync_lbl.configure(text="✓ matches radio", fg=FG_GREEN)
                self.band_big.configure(fg=FG_TEAL)
        elif self.rig_enabled:
            self.freq_lbl.configure(text="")
            self.mismatch_since = None
            self.sync_lbl.configure(text="(no rig data -- can't cross-check)", fg=FG_DIM)
            self.band_big.configure(fg=FG_TEAL)
        else:
            self.freq_lbl.configure(text="")
            self.band_big.configure(fg=FG_TEAL)

        # FOR/REF power: channels 13/15 scaled by channel 8's range
        # selector (disassembly-confirmed for FOR; REF is AmpMonitor's own
        # bargraph value, only ever drawn as a bar there, never as text).
        for_val = s.for_power_w if s.for_power_w is not None else 0
        ref_val = s.ref_power_w if s.ref_power_w is not None else 0
        self._set_meter(self.for_bar, round(for_val))
        self._set_meter(self.ref_bar, round(ref_val))

        if s.temperature_c is not None:
            if self.temp_unit == "F":
                self.temp_val.configure(text=f"{s.temperature_f:.0f} F")
            else:
                self.temp_val.configure(text=f"{s.temperature_c} C")
        else:
            self.temp_val.configure(text=f"-- {self.temp_unit}")

        cur_val = s.current_a  # channel 10, disassembly-confirmed ("strom")
        self.current_val.configure(text=f"{cur_val} A" if cur_val is not None else "-- A")

        swr_ch = CHANNEL_MAP["swr"]
        swr_val = s.raw.get(swr_ch) if swr_ch is not None else None
        self.swr_val.configure(text=str(swr_val) if swr_val is not None else "--")

        self.fan_val.configure(text=(s.fan or "--").upper())
        self.ant_val.configure(text=s.antenna or "--")


def run_serial_reader(gui: DNAutoGUI, port: str):
    decoder = DNAutoDecoder(gui.on_update)
    ser = open_serial(port)
    try:
        while True:
            data = ser.read(256)
            if data:
                decoder.feed(data)
            else:
                time.sleep(0.01)
    finally:
        ser.close()


def run_demo_feed(gui: DNAutoGUI):
    """Generates plausible-looking fake traffic so the GUI can be previewed
    and developed without real hardware, using the real (disassembly-
    confirmed) channel numbers -- see dnauto_protocol.py's docstring."""
    decoder = DNAutoDecoder(gui.on_update)

    def encode_channel(channel, value):
        lo = value & 0x0F
        hi = (value >> 4) & 0x0F
        return bytes([0xC0 | channel, 0xE0 | lo, 0xF0 | hi])

    band_idx = 0
    t = 0
    devices = [0, 1, 2, 3, 4]
    while True:
        band_idx = (band_idx + (1 if t % 40 == 0 else 0)) % len(BANDS)
        temp = 25 + int(15 * (0.5 + 0.5 * random.random()))
        scale_idx = 5  # x8 range, comfortably shows a few hundred watts
        for_raw = max(0, int(30 + 10 * random.random()))
        ref_raw = max(0, int(for_raw * 0.05 * random.random()))
        current = max(0, int(3 + 2 * random.random()))
        fan_mode = 1 if temp > 35 else 0
        ant_b = t % 200 > 100
        device = devices[(t // 60) % len(devices)]

        # channel 11 status bitfield: bit0=fan1, bit1=SWR alarm,
        # bit2=Imax alarm, bit3=fan2, bit6=ALARM_IN, bit7=antenna(sign bit)
        status11 = 0
        if fan_mode:
            status11 |= 0x01 | 0x08  # both fans running when fan is on
        if ant_b:
            status11 |= 0x80

        data = b"".join(
            [
                encode_channel(2, band_idx),           # band (primary)
                encode_channel(4, temp),                # temperature
                encode_channel(8, scale_idx),           # power range selector
                encode_channel(13, for_raw),            # FOR power (raw)
                encode_channel(15, ref_raw),             # REF power (raw)
                encode_channel(10, current),            # current, A
                encode_channel(9, fan_mode),             # fan mode
                encode_channel(11, status11),           # status bitfield (incl. antenna)
                encode_channel(14, device),              # device type
            ]
        )
        decoder.feed(data)
        t += 1
        time.sleep(0.3)


def _parse_host_port(s: str, default_port: int = 4532):
    if ":" in s:
        host, _, port_s = s.rpartition(":")
        return host, int(port_s)
    return s, default_port


def run_rigctld_poller(gui: "DNAutoGUI", host: str, port: int):
    """Polls a running rigctld (Hamlib) instance once a second for the
    radio's current VFO frequency and feeds it to the GUI as the band
    source. More reliable than DN-AUTO's own unconfirmed band channel,
    since it comes straight from the radio rather than a guessed mapping.
    Silently keeps retrying if rigctld/the radio is briefly unreachable --
    the RIG status indicator in the GUI reflects whether it's currently
    getting data."""
    while True:
        freq = query_frequency(host, port)
        if freq is not None:
            gui.on_rig_update(freq)
        time.sleep(1.0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port", nargs="?", help="serial device, e.g. /dev/ttyUSB0")
    parser.add_argument("--demo", action="store_true", help="run with synthetic demo data, no hardware needed")
    parser.add_argument(
        "--rigctld",
        nargs="?",
        const="127.0.0.1:4532",
        default=None,
        metavar="HOST:PORT",
        help=(
            "also read live band/frequency from a running Hamlib rigctld "
            "instance (defaults to 127.0.0.1:4532 if given with no value). "
            "When reachable this overrides DN-AUTO's own unconfirmed band "
            "channel, since it comes straight from the radio."
        ),
    )
    args = parser.parse_args()

    if not args.demo and not args.port:
        parser.error("either give a serial port or pass --demo")

    # className sets the window's WM_CLASS, which GNOME Shell (and other
    # desktops' docks/taskbars) use to match a running window back to its
    # .desktop launcher -- without a match, GNOME shows a generic fallback
    # icon in the dock and "Add to Favorites" won't pin the right thing.
    # Must match dnauto-monitor.desktop's StartupWMClass exactly.
    root = tk.Tk(className="dnauto-monitor")
    gui = DNAutoGUI(root, demo=args.demo, rig_enabled=bool(args.rigctld))

    if args.demo:
        thread = threading.Thread(target=run_demo_feed, args=(gui,), daemon=True)
    else:
        thread = threading.Thread(target=run_serial_reader, args=(gui, args.port), daemon=True)
    thread.start()

    if args.rigctld:
        host, port = _parse_host_port(args.rigctld)
        rig_thread = threading.Thread(target=run_rigctld_poller, args=(gui, host, port), daemon=True)
        rig_thread.start()

    root.mainloop()


if __name__ == "__main__":
    main()
