#!/usr/bin/env python3
"""
Linux replacement for the Windows "AmpMonitor" program that watches a
DN-AUTO CPU amplifier controller over its RS-232 monitor port (X7).

Usage:
    python3 dnauto_monitor.py /dev/ttyUSB0            # live dashboard
    python3 dnauto_monitor.py /dev/ttyUSB0 --raw       # raw channel logger
                                                        # (use this first to
                                                        #  finish mapping the
                                                        #  unconfirmed channels
                                                        #  -- see README.md)

Requires: pip install pyserial
"""

import argparse
import sys
import time

from dnauto_protocol import AmplifierState, ChannelLogger, ChannelUpdate, DNAutoDecoder, open_serial


def run_dashboard(port: str) -> None:
    state = AmplifierState()

    def on_update(update: ChannelUpdate) -> None:
        state.apply(update)
        render(state)

    decoder = DNAutoDecoder(on_update)
    ser = open_serial(port)
    print(f"Listening on {port} (9600 8N1)... Ctrl-C to quit.\n")
    try:
        while True:
            data = ser.read(256)
            if data:
                decoder.feed(data)
            else:
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


def _fmt(v, suffix=""):
    return f"{v}{suffix}" if v is not None else "?"


def render(state: AmplifierState) -> None:
    sys.stdout.write("\x1b[2J\x1b[H")  # clear screen, home cursor
    print("DN-AUTO amplifier monitor (Linux)")
    print("=" * 44)
    print(f"Device        : {state.device or '?'}")
    band = f"{state.band}" if state.band is not None else "?"
    print(f"Band (raw idx): {band}  (index into BANDS list, see dnauto_gui.py)")
    print(f"Temperature   : {_fmt(state.temperature_c, ' C')}")
    print(f"Current       : {_fmt(state.current_a, ' A')}")
    print(f"Antenna       : {state.antenna or '?'}  (from channel 11, bit 7)")
    print(f"Fan mode      : {state.fan or '?'}")
    for_w = state.for_power_w
    ref_w = state.ref_power_w
    print(f"FOR power     : {_fmt(round(for_w, 1) if for_w is not None else None, ' W')}")
    print(f"REF power     : {_fmt(round(ref_w, 1) if ref_w is not None else None, ' W')} (estimated, AmpMonitor only bargraphs this)")
    print()
    print("Status flags (channel 11/12 bitfields):")
    print(
        f"  fan1={_fmt(state.fan1_running)}  fan2={_fmt(state.fan2_running)}  "
        f"SWR alarm={_fmt(state.swr_alarm)}  Imax alarm={_fmt(state.imax_alarm)}  "
        f"ALARM_IN={_fmt(state.alarm_in)}"
    )
    print(f"  ALARMB={_fmt(state.alarmb)}  error={_fmt(state.error_flag)}")
    print()
    print("All raw channel values (channels 0,1,5,6,7 are received but never")
    print("shown by the original AmpMonitor.exe -- still unmapped here):")
    for ch in range(16):
        if ch in state.raw:
            print(f"  ch{ch:2d}: {state.raw[ch]}")
    print("\n(Ctrl-C to quit)")


def run_raw(port: str) -> None:
    logger = ChannelLogger()
    decoder = DNAutoDecoder(logger)
    ser = open_serial(port)
    print(f"Raw channel logger on {port} (9600 8N1). Ctrl-C to quit.")
    print("Change one thing at a time on the amplifier (band, temp, etc.)")
    print("and note which channel number reacts -- that's how you finish")
    print("filling in dnauto_protocol.py's AmplifierState.apply().\n")
    try:
        while True:
            data = ser.read(256)
            if data:
                decoder.feed(data)
            else:
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", help="serial device, e.g. /dev/ttyUSB0")
    parser.add_argument(
        "--raw", action="store_true", help="print raw channel updates instead of the dashboard"
    )
    args = parser.parse_args()

    if args.raw:
        run_raw(args.port)
    else:
        run_dashboard(args.port)


if __name__ == "__main__":
    main()
