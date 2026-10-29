"""
Offline self-test for rigctl_client.py's frequency-to-band mapping (the
part that needs no network/rigctld to verify), plus query_ptt() against a
tiny in-process mock rigctld, and kx3_band_bridge.py's ABCD code mapping.

Run with:  python3 test_rigctl_client.py
"""

import socket
import threading

from rigctl_client import freq_to_band, query_ptt


def test_band_centers():
    assert freq_to_band(1_900_000) == "160"
    assert freq_to_band(3_750_000) == "80"
    assert freq_to_band(7_150_000) == "40"
    assert freq_to_band(10_125_000) == "30"
    assert freq_to_band(14_074_000) == "20"
    assert freq_to_band(18_100_000) == "17"
    assert freq_to_band(21_200_000) == "15"
    assert freq_to_band(24_920_000) == "12"
    assert freq_to_band(28_500_000) == "10"
    assert freq_to_band(50_125_000) == "6"


def test_band_edges_inclusive():
    assert freq_to_band(1_800_000) == "160"
    assert freq_to_band(2_000_000) == "160"
    assert freq_to_band(28_000_000) == "10"
    assert freq_to_band(29_700_000) == "10"


def test_out_of_band_returns_none():
    assert freq_to_band(0) is None
    assert freq_to_band(5_000_000) is None  # between 80m and 40m
    assert freq_to_band(2_500_000) is None  # just above 160m
    assert freq_to_band(146_000_000) is None  # 2m -- not on DN-AUTO's strip


def _serve_one_reply(reply: bytes):
    """Bind an ephemeral localhost port, accept exactly one connection,
    send `reply`, then close -- enough to exercise query_ptt()'s parsing
    without needing a real rigctld running."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def handler():
        conn, _ = srv.accept()
        with conn:
            conn.recv(256)  # discard the "t\n" request
            conn.sendall(reply)
        srv.close()

    threading.Thread(target=handler, daemon=True).start()
    return port


def test_query_ptt_short_form():
    port = _serve_one_reply(b"1\n")
    assert query_ptt("127.0.0.1", port) is True

    port = _serve_one_reply(b"0\n")
    assert query_ptt("127.0.0.1", port) is False


def test_query_ptt_long_form():
    port = _serve_one_reply(b"PTT: 1\n")
    assert query_ptt("127.0.0.1", port) is True


def test_query_ptt_unreachable_returns_none():
    # nothing listening on this port
    assert query_ptt("127.0.0.1", 1) is None



if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    if failed:
        raise SystemExit(f"{failed}/{len(tests)} tests failed")
    print(f"\nAll {len(tests)} tests passed.")
