#!/usr/bin/env python3
"""
Bare-bones serial diagnostic tool -- prints every raw byte that arrives on
a port as hex, with nothing else in the way. Use this BEFORE trying
dnauto_monitor.py or dnauto_gui.py when you're not sure the wiring/adapter
itself is working, because it doesn't assume anything about the DN-AUTO
protocol -- it'll show you garbage bytes just as readily as valid ones,
which is exactly what you want when troubleshooting a dead link.

Usage:
    python3 dnauto_rawdump.py /dev/ttyUSB0
    python3 dnauto_rawdump.py /dev/ttyUSB0 --baud 4800   # try other bauds

Requires: pip install pyserial
"""

import argparse
import sys
import time

import serial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", help="serial device, e.g. /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=9600, help="baud rate (default 9600)")
    args = parser.parse_args()

    print(f"Opening {args.port} at {args.baud} 8N1 ...")
    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.5,
        )
    except Exception as e:
        print(f"FAILED to open port: {e}")
        print("Common causes: wrong device name, permission denied (see")
        print("README/troubleshooting notes about the 'dialout' group), or")
        print("another program (ModemManager, a previous run of this tool,")
        print("AmpMonitor under Wine, etc.) already has the port open.")
        sys.exit(1)

    print("Port opened OK. Waiting for bytes -- Ctrl-C to quit.")
    print("If you NEVER see a line printed below, no data is reaching the")
    print("PC at all -- that points at wiring/levels/power, not software.")
    print("If you see a 'CTS/DSR/CD' summary but no byte lines, the cable")
    print("is at least electrically connected but nothing is transmitting.")
    print()

    total = 0
    last_report = time.time()
    try:
        while True:
            data = ser.read(256)
            if data:
                total += len(data)
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] {len(data):3d} bytes: {data.hex(' ')}")
            elif time.time() - last_report > 5:
                last_report = time.time()
                try:
                    cts, dsr, cd = ser.cts, ser.dsr, ser.cd
                except Exception:
                    cts = dsr = cd = "?"
                print(
                    f"...still waiting ({total} bytes total so far). "
                    f"Line status -- CTS:{cts} DSR:{dsr} CD:{cd}"
                )
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        print(f"\nTotal bytes received: {total}")


if __name__ == "__main__":
    main()
