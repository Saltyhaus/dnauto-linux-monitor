"""
Optional helper: get the current band directly from a running Hamlib
`rigctld` instance instead of relying on DN-AUTO's own band channel (which
is still unconfirmed -- see README.md). Talks rigctld's plain-text TCP
protocol directly with a socket, so no python-hamlib / Cython bindings are
required, just a `rigctld` process already running somewhere reachable.

Typical setup:

    rigctld -m <model> -r /dev/ttyUSB0 -s 38400 &   # you likely already
                                                      # have this running
    python3 dnauto_gui.py --demo --rigctld            # host:port optional,
                                                        # defaults to
                                                        # 127.0.0.1:4532

rigctld happily serves multiple simultaneous TCP clients against the same
serial port -- this module opens a short-lived connection per query, so it
plays fine alongside e.g. an SDRconnect panadapter sync script also talking
to the same rigctld instance. If you're running a separate rigctld per rig
(e.g. one rig on 4532, another on 4534), just point host/port at the right
one; nothing here assumes a single global rigctld.

Besides query_frequency(), query_ptt() is here for callers (like
kx3_band_bridge.py) that need to know whether the rig is transmitting --
e.g. to hold off on a band-data change until PTT clears. It's a read-only
software gate, not a substitute for a real hardware PTT/key line.
"""

from __future__ import annotations

import socket
from typing import Optional

# (band name, low Hz, high Hz) -- names must match dnauto_gui.py's BANDS
# list. Standard amateur band edges; adjust if your region's allocation
# differs (e.g. some countries have a narrower/wider 60m or no 60m at all --
# DN-AUTO's band strip doesn't have a 60m box, so it's left out here too).
BAND_EDGES = [
    ("160", 1_800_000, 2_000_000),
    ("80", 3_500_000, 4_000_000),
    ("40", 7_000_000, 7_300_000),
    ("30", 10_100_000, 10_150_000),
    ("20", 14_000_000, 14_350_000),
    ("17", 18_068_000, 18_168_000),
    ("15", 21_000_000, 21_450_000),
    ("12", 24_890_000, 24_990_000),
    ("10", 28_000_000, 29_700_000),
    ("6", 50_000_000, 54_000_000),
]


def freq_to_band(freq_hz: float) -> Optional[str]:
    """Map a frequency in Hz to one of DN-AUTO's band-strip names, or None
    if it's outside all of them (out of band, or on a band DN-AUTO's strip
    doesn't have a box for)."""
    for name, lo, hi in BAND_EDGES:
        if lo <= freq_hz <= hi:
            return name
    return None


def query_ptt(host: str, port: int, timeout: float = 1.0) -> Optional[bool]:
    """Ask a running rigctld whether the rig is currently transmitting.

    Uses rigctld's short "t" (get_ptt) command, which replies "0" (RX) or
    a non-zero PTT-source code (TX). Returns None on any failure, same
    convention as query_frequency -- callers should treat None as "don't
    know", not as "not transmitting", and act conservatively (e.g. hold
    off on a band change) when they can't get a clean answer.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"t\n")
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(256)
                if not chunk:
                    break
                data += chunk
        line = data.decode("ascii", errors="ignore").strip().splitlines()[0]
        if ":" in line:
            line = line.split(":", 1)[1]
        return int(line.strip()) != 0
    except Exception:
        return None


def query_frequency(host: str, port: int, timeout: float = 1.0) -> Optional[float]:
    """Ask a running rigctld for the current VFO frequency, in Hz.

    Opens a fresh short-lived connection per call rather than keeping one
    open -- simpler, and tolerant of rigctld or the radio bouncing between
    polls. Returns None on any failure (rigctld not running, radio off,
    a stale/broken connection, etc.) rather than raising, so a caller
    polling this in a loop can just treat None as "no rig data right now"
    and keep trying.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"f\n")
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(256)
                if not chunk:
                    break
                data += chunk
        line = data.decode("ascii", errors="ignore").strip().splitlines()[0]
        # rigctld's short-command ("f") reply is just the frequency in Hz
        # on its own line. Be a little defensive in case a server instead
        # replies in the long ("\get_freq") form, "Frequency: 14074000".
        if ":" in line:
            line = line.split(":", 1)[1]
        return float(line.strip())
    except Exception:
        return None
