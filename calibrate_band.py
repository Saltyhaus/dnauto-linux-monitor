#!/usr/bin/env python3
"""
Automated calibration for DN-AUTO's own band channel (2/3).

The GUI/dashboard originally guessed that channel 2/3's raw 0-15 value is
a direct 0-based index into BANDS (0=160m ... 9=6m), but that mapping was
never actually confirmed against real hardware -- and in practice it's been
observed getting stuck showing the wrong band. This script settles it
empirically instead of guessing again: it drives the Arduino ABCD bridge
through all 10 real band codes (the same one kx3_abcd_bridge.ino/
sweep_test.py use) and, for each one, reads whatever DN-AUTO itself reports
on its own serial channels 2 and 3 once the amp has settled onto that band.
That only works because DN-AUTO's band input (connector X11) listens to the
same physical ABCD bus as the DXWorld decoder -- driving the bus with the
Arduino changes what DN-AUTO itself thinks the band is, not just what the
decoder does.

Usage:
    python3 calibrate_band.py --arduino /dev/ttyACM0 --dnauto /dev/ttyUSB0

Requires nothing be running against either port already (stop
kx3-band-bridge.service and close any other monitor first, or you'll get
a "port already open" error and/or fight the service for control of the
Arduino):

    sudo systemctl stop kx3-band-bridge.service

Prints a table of: ABCD code -> expected band -> DN-AUTO ch2 raw -> ch3 raw,
then a best-guess formula relating the raw value to the expected band index,
so `dnauto_protocol.py`/`dnauto_gui.py` can be corrected to match reality
instead of the original guess. Ends by sending code 0 (no band) to the
Arduino and leaving both ports closed.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from dnauto_protocol import AmplifierState, ChannelUpdate, DNAutoDecoder, open_serial

CODES = [
    (1, "160"), (2, "80"), (3, "40"), (4, "30"), (5, "20"),
    (6, "17"), (7, "15"), (8, "12"), (9, "10"), (10, "6"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arduino", required=True, help="Arduino ABCD bridge serial port, e.g. /dev/ttyACM0")
    parser.add_argument("--dnauto", required=True, help="DN-AUTO's own serial port, e.g. /dev/ttyUSB0")
    parser.add_argument("--arduino-baud", type=int, default=9600)
    parser.add_argument("--settle", type=float, default=4.0, help="seconds to wait after each code before sampling DN-AUTO (default 4)")
    args = parser.parse_args()

    import serial  # local import, same convention as the rest of this project

    print(f"Opening Arduino at {args.arduino} ...")
    ard = serial.Serial(args.arduino, baudrate=args.arduino_baud, timeout=1)
    time.sleep(2.0)  # let the board reset after opening the port

    print(f"Opening DN-AUTO at {args.dnauto} ...")
    state = AmplifierState()
    stop = threading.Event()

    def on_update(update: ChannelUpdate) -> None:
        state.apply(update)

    decoder = DNAutoDecoder(on_update)
    dn = open_serial(args.dnauto)

    def reader_loop():
        while not stop.is_set():
            data = dn.read(256)
            if data:
                decoder.feed(data)
            else:
                time.sleep(0.01)

    reader_thread = threading.Thread(target=reader_loop, daemon=True)
    reader_thread.start()

    results = []
    try:
        print(f"\nSweeping all 10 codes, {args.settle}s settle time each. Watch the amp.\n")
        for code, band in CODES:
            ard.write(bytes([code]))
            echo = ard.readline().decode(errors="replace").strip()
            print(f"code {code:2d} (expect {band:>3s}m) -- Arduino: {echo!r} -- settling {args.settle}s...")
            time.sleep(args.settle)
            ch2 = state.raw.get(2)
            ch3 = state.raw.get(3)
            print(f"           DN-AUTO reports: ch2={ch2}  ch3={ch3}")
            results.append((code, band, ch2, ch3))
    except KeyboardInterrupt:
        print("\nstopped early")
    finally:
        print("\nSending code 0 (no band) and closing ports...")
        ard.write(bytes([0]))
        time.sleep(0.2)
        stop.set()
        reader_thread.join(timeout=2)
        ard.close()
        dn.close()

    if not results:
        return

    print("\n" + "=" * 60)
    print(f"{'code':>4s}  {'band':>5s}  {'ch2 raw':>8s}  {'ch3 raw':>8s}")
    print("-" * 60)
    for code, band, ch2, ch3 in results:
        print(f"{code:4d}  {band:>4s}m  {str(ch2):>8s}  {str(ch3):>8s}")
    print("=" * 60)

    # Best-effort pattern guess: is ch2 (or ch3) a fixed offset from `code`?
    for label, idx in (("ch2", 2), ("ch3", 3)):
        vals = [r[idx] for r in results]
        if any(v is None for v in vals):
            print(f"\n{label} never changed across the sweep -- DN-AUTO's X11 band input "
                  f"is probably not wired to this ABCD bus, or isn't being read for some "
                  f"other reason. That channel can't be calibrated from this test.")
            continue
        offsets = {code - v for (code, _, _, _), v in zip(results, vals)}
        if len(offsets) == 1:
            offset = offsets.pop()
            print(f"\n{label}: consistent pattern found -- raw value = code - {offset} "
                  f"(i.e. raw = ABCD code minus {offset}). Update AmplifierState.apply() "
                  f"and BANDS indexing in dnauto_gui.py to use `v + {offset}` as the ABCD "
                  f"code, or equivalently reindex BANDS to start at {offset}.")
        else:
            print(f"\n{label}: no simple fixed-offset pattern (raw values: {vals}). "
                  f"Paste this table back for a closer look -- it might be a different "
                  f"encoding (e.g. bit-reversed, or masked differently) rather than a "
                  f"simple index shift.")


if __name__ == "__main__":
    main()
