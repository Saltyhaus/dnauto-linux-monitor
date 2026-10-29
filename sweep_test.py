#!/usr/bin/env python3
"""
Manual bring-up test: cycle the Arduino through every ABCD code (1-10, plus
0 for "no band") one at a time, pausing between each so you can watch the
decoder's display and listen for relay clicks and confirm it matches the
truth table.

Usage:
    python3 sweep_test.py /dev/ttyACM0
    python3 sweep_test.py /dev/ttyACM0 --delay 5

Prints the code and expected band before sending it, waits for the
"set code N" echo to confirm the Arduino actually got it, then pauses
`--delay` seconds (default 4) before moving to the next one. Ctrl-C to stop
early. Ends by sending 0 (no band / all relays de-energized) regardless of
where it was interrupted.
"""

import argparse
import sys
import time

import serial

# code -> band name, same table as kx3_band_bridge.py
CODES = [
    (1, "160m"), (2, "80m"), (3, "40m"), (4, "30m"), (5, "20m"),
    (6, "17m"), (7, "15m"), (8, "12m"), (9, "10m"), (10, "6m"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("serial_port", help="Arduino's serial port (e.g. /dev/ttyACM0)")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--delay", type=float, default=4.0, help="seconds to hold each code (default 4)")
    args = parser.parse_args()

    ser = serial.Serial(args.serial_port, baudrate=args.baud, timeout=2)
    time.sleep(2.0)  # let the board reset after opening the port

    try:
        for code, band in CODES:
            print(f"\n--- sending code {code} (expect decoder to show {band}) ---")
            ser.write(bytes([code]))
            echo = ser.readline().decode(errors="replace").strip()
            print(f"  Arduino says: {echo!r}")
            print(f"  check the decoder now -- does it show {band}?")
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nstopped early")
    finally:
        print("\n--- sending code 0 (no band / all relays should de-energize) ---")
        ser.write(bytes([0]))
        echo = ser.readline().decode(errors="replace").strip()
        print(f"  Arduino says: {echo!r}")
        ser.close()


if __name__ == "__main__":
    main()
