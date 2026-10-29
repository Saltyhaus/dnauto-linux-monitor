"""
Offline self-test for dnauto_protocol.py.

This does NOT need a serial port or the real amplifier -- it just replays
the exact byte sequences that were verified against the real Windows
AmpMonitor.exe program (running under Wine, fed synthetic bytes over a
virtual COM port) and checks the decoder reproduces the same values.

Run with:  python3 test_dnauto_protocol.py
"""

from dnauto_protocol import AmplifierState, ChannelUpdate, DNAutoDecoder


def encode_channel(channel: int, value: int) -> bytes:
    lo = value & 0x0F
    hi = (value >> 4) & 0x0F
    return bytes([0xC0 | channel, 0xE0 | lo, 0xF0 | hi])


def encode_digits(channel: int, value: int) -> bytes:
    value = max(0, min(999, value))
    d1, d2, d3 = value // 100 % 10, value // 10 % 10, value % 10
    return bytes([0xC0 | channel, 0xE0 | d1, 0xF0 | d2, 0xD0 | d3])


def test_basic_channel_roundtrip():
    updates = []
    decoder = DNAutoDecoder(updates.append)
    decoder.feed(encode_channel(4, 50))
    assert updates == [ChannelUpdate(4, 50, bytes.fromhex("c4e2f3"))], updates


def test_confirmed_device_field_matches_real_ampmonitor():
    # Verified live: sending C0|14, E0|3, F0|0 made the real AmpMonitor.exe
    # (running under Wine against a virtual serial port) display "Icom"
    # instead of "No Signal".
    updates = []
    decoder = DNAutoDecoder(updates.append)
    decoder.feed(encode_channel(14, 3))
    assert updates[-1].value == 3
    state = AmplifierState()
    state.apply(updates[-1])
    assert state.device == "Icom"


def test_confirmed_temperature_field_matches_real_ampmonitor():
    # Verified live: channel 4 = 50 made the real AmpMonitor.exe display
    # "50" in the temperature (deg C) slot.
    updates = []
    decoder = DNAutoDecoder(updates.append)
    decoder.feed(encode_channel(4, 50))
    state = AmplifierState()
    state.apply(updates[-1])
    assert state.temperature_c == 50


def test_all_16_channels_independent():
    updates = []
    decoder = DNAutoDecoder(updates.append)
    for ch in range(16):
        if ch in (6, 7):
            continue
        decoder.feed(encode_channel(ch, ch * 7 + 1))
    for ch in range(16):
        if ch in (6, 7):
            continue
        assert decoder.values[ch] == ch * 7 + 1, (ch, decoder.values[ch])


def test_digit_channels_6_and_7():
    updates = []
    decoder = DNAutoDecoder(updates.append)
    decoder.feed(encode_digits(6, 912))
    decoder.feed(encode_digits(7, 45))
    assert decoder.values[6] == 912
    assert decoder.values[7] == 45


def test_byte_stream_can_be_split_arbitrarily():
    # The real link is a continuous byte stream -- reads can chop a
    # channel update in half. The decoder must not care.
    data = encode_channel(4, 50) + encode_channel(9, 1)
    updates = []
    decoder = DNAutoDecoder(updates.append)
    for i in range(0, len(data), 2):
        decoder.feed(data[i : i + 2])
    assert decoder.values[4] == 50
    assert decoder.values[9] == 1


def test_band_from_channel_3():
    # Empirically calibrated via calibrate_band.py: channel 3's raw value
    # is the ABCD code directly (1=160m..10=6m), converted to a 0-based
    # BANDS index by subtracting 1. Channel 2 was assumed (from disassembly)
    # to be an equivalent secondary source, but the same real-hardware
    # sweep showed it holds constant regardless of actual band, so it must
    # NOT drive state.band -- only channel 3 does.
    state = AmplifierState()
    decoder = DNAutoDecoder(lambda u: state.apply(u))
    decoder.feed(encode_channel(3, 5))  # ABCD code 5 == 20m == BANDS[4]
    assert state.band == 4
    decoder.feed(encode_channel(3, 7))  # ABCD code 7 == 15m == BANDS[6]
    assert state.band == 6


def test_band_channel_2_does_not_drive_band():
    state = AmplifierState()
    decoder = DNAutoDecoder(lambda u: state.apply(u))
    decoder.feed(encode_channel(3, 5))  # 20m, established via channel 3
    assert state.band == 4
    decoder.feed(encode_channel(2, 9))  # channel 2's real-world constant -- must be ignored
    assert state.band == 4
    assert state.raw[2] == 9  # still recorded, just not used for .band


def test_current_from_channel_10():
    state = AmplifierState()
    decoder = DNAutoDecoder(lambda u: state.apply(u))
    decoder.feed(encode_channel(10, 7))
    assert state.current_a == 7


def test_for_power_scaled_by_channel_8():
    state = AmplifierState()
    decoder = DNAutoDecoder(lambda u: state.apply(u))
    decoder.feed(encode_channel(8, 5))  # x8 range
    decoder.feed(encode_channel(13, 30))
    assert state.for_power_w == 240  # 30 * 8


def test_for_power_range_zero_forces_zero():
    state = AmplifierState()
    decoder = DNAutoDecoder(lambda u: state.apply(u))
    decoder.feed(encode_channel(8, 0))
    decoder.feed(encode_channel(13, 99))
    assert state.for_power_w == 0


def test_antenna_from_channel_11_sign_bit():
    state = AmplifierState()
    decoder = DNAutoDecoder(lambda u: state.apply(u))
    decoder.feed(encode_channel(11, 0x00))  # bit7 clear -> antenna A
    assert state.antenna == "A"
    decoder.feed(encode_channel(11, 0x80))  # bit7 set -> antenna B
    assert state.antenna == "B"


def test_channel_11_alarm_bits():
    state = AmplifierState()
    decoder = DNAutoDecoder(lambda u: state.apply(u))
    # bit0=fan1, bit1=SWR alarm, bit2=Imax alarm, bit3=fan2, bit6=ALARM_IN
    decoder.feed(encode_channel(11, 0b01001111))
    assert state.fan1_running is True
    assert state.swr_alarm is True
    assert state.imax_alarm is True
    assert state.fan2_running is True
    assert state.alarm_in is True
    decoder.feed(encode_channel(11, 0x00))
    assert state.fan1_running is False
    assert state.swr_alarm is False


def test_channel_12_alarmb_and_error():
    state = AmplifierState()
    decoder = DNAutoDecoder(lambda u: state.apply(u))
    decoder.feed(encode_channel(12, 0x40))  # bit6 set -> ALARMB, bit7 clear
    assert state.alarmb is True
    assert state.error_flag is False
    decoder.feed(encode_channel(12, 0x80))  # bit7 set -> error flag
    assert state.error_flag is True


def test_fahrenheit_conversion():
    state = AmplifierState()
    updates = []
    decoder = DNAutoDecoder(updates.append)
    decoder.feed(encode_channel(4, 25))
    state.apply(updates[-1])
    assert state.temperature_c == 25
    assert state.temperature_f == 77.0


def test_non_framing_bytes_are_ignored():
    decoder = DNAutoDecoder(lambda u: None)
    decoder.feed(b"\x00\x01\x7f")  # garbage / noise
    decoder.feed(encode_channel(4, 33))
    assert decoder.values[4] == 33


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
