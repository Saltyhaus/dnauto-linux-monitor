#!/usr/bin/env python3
"""
Anomaly-hunting tool for the "screen goes crazy with power/SWR even though
I'm not transmitting" symptom.

What this does differently from dnauto_monitor.py --raw:

  * It keeps a rolling window of the raw bytes actually seen on the wire
    (including any non-conforming bytes the decoder throws away), so when
    a suspicious update happens we can show exactly what came in around it.
  * It flags two kinds of suspicious channel updates:
      - FOR/REF power (channels 13/15) implying non-zero watts while you
        are not transmitting (any reading above --power-threshold).
      - channel 12's error_flag or alarmb bits flipping on.
  * On a flagged update it prints the raw hex window and a note on whether
    the DN-AUTO CPU's now-known "mystery block" (a repeating ~28-byte run
    of bytes whose top two bits are NOT both set, which both AmpMonitor.exe
    and this decoder verifiably ignore -- confirmed by disassembling the
    real AmpMonitor.exe byte-dispatch loop, see README.md) was present in
    that window, so we can tell noise-during-real-bytes apart from this
    already-explained block.

Usage:
    python3 dnauto_diagnose.py /dev/ttyUSB0
    python3 dnauto_diagnose.py /dev/ttyUSB0 --power-threshold 5

Let it run and reproduce the "crazy" glitch on screen (with the real
AmpMonitor.exe/dnauto_gui.py running at the same time is fine -- this just
listens, it doesn't touch the port). Send back whatever it prints when the
glitch happens.
"""

import argparse
import sys
import time

from dnauto_protocol import AmplifierState, ChannelUpdate, DNAutoDecoder, open_serial

WINDOW_BYTES = 64


def looks_like_mystery_block(window: bytes) -> bool:
    """True if the raw window contains a run of >=6 consecutive bytes that
    do NOT have both top bits set (0xC0 mask != 0xC0). That's the same
    "non-conforming" pattern seen in the ~28-byte repeating block that
    both AmpMonitor.exe and DNAutoDecoder silently discard."""
    run = 0
    for b in window:
        if (b & 0xC0) != 0xC0:
            run += 1
            if run >= 6:
                return True
        else:
            run = 0
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port")
    parser.add_argument(
        "--power-threshold",
        type=float,
        default=1.0,
        help="flag FOR/REF power readings above this many watts (default: 1.0)",
    )
    args = parser.parse_args()

    state = AmplifierState()
    raw_window = bytearray()
    prev_for = None
    prev_ref = None
    prev_error = False
    prev_alarmb = False

    def on_raw(b: int) -> None:
        raw_window.append(b)
        del raw_window[:-WINDOW_BYTES]

    def flag(reason: str) -> None:
        ts = time.strftime("%H:%M:%S")
        mystery = looks_like_mystery_block(bytes(raw_window))
        print(f"\n[{ts}] *** SUSPICIOUS UPDATE: {reason} ***")
        print(f"  last {len(raw_window)} raw bytes: {bytes(raw_window).hex(' ')}")
        print(
            f"  mystery block (non-conforming run) present in window: {mystery}"
            + ("  <- known-benign, both parsers ignore it, not the cause" if mystery else "")
        )

    def on_update(update: ChannelUpdate) -> None:
        nonlocal prev_for, prev_ref, prev_error, prev_alarmb
        state.apply(update)
        ts = time.strftime("%H:%M:%S")
        extra = ""
        if update.channel == 4 and state.temperature_f is not None:
            extra = f"  [{state.temperature_c} C = {state.temperature_f:.0f} F]"
        elif update.channel == 14:
            extra = f"  [{state.device}]"
        print(
            f"[{ts}] ch{update.channel:2d} = {update.value:>4d} (0x{update.value:02x})  "
            f"raw={update.raw_bytes.hex()}{extra}"
        )

        if update.channel in (8, 13):
            for_w = state.for_power_w
            if for_w is not None and for_w != prev_for:
                if for_w > args.power_threshold:
                    note = " (first reading -- decoder may have started mid-byte-stream)" if prev_for is None else ""
                    flag(f"FOR power jumped to {for_w:.1f} W (was {prev_for}){note}")
                prev_for = for_w
        if update.channel in (8, 15):
            ref_w = state.ref_power_w
            if ref_w is not None and ref_w != prev_ref:
                if ref_w > args.power_threshold:
                    note = " (first reading -- decoder may have started mid-byte-stream)" if prev_ref is None else ""
                    flag(f"REF power jumped to {ref_w:.1f} W (was {prev_ref}){note}")
                prev_ref = ref_w
        if update.channel == 12:
            if state.error_flag and not prev_error:
                flag("error_flag (channel 12 bit7) turned on")
            if state.alarmb and not prev_alarmb:
                flag("alarmb (channel 12 bit6) turned on")
            prev_error = bool(state.error_flag)
            prev_alarmb = bool(state.alarmb)

    decoder = DNAutoDecoder(on_update)
    ser = open_serial(args.port)
    print(f"Listening on {args.port} (9600 8N1). Reproduce the glitch, then Ctrl-C.\n")
    try:
        while True:
            data = ser.read(256)
            if data:
                for b in data:
                    on_raw(b)
                decoder.feed(data)
            else:
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


if __name__ == "__main__":
    main()
