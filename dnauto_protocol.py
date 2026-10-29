"""
DN-AUTO CPU serial protocol decoder.

Reverse-engineered from the Windows "AmpMonitor.exe" program (POUCH Amplifier
Monitor) that ships with the DN-AUTO band-decoder/amplifier-protection
controller (dn-radio.com / ut2fw.com). See README.md in this folder for the
full writeup of how this was derived and which fields are confirmed vs. best
guesses that still need verification against real hardware.

--------------------------------------------------------------------------
Wire format (confirmed by disassembling AmpMonitor.exe and by feeding it
synthetic bytes over a virtual serial port):

  * RS-232, 9600 baud, 8 data bits, no parity, 1 stop bit (9600 8N1).
  * The DN-AUTO CPU continuously *streams* its status out the TX line.
    AmpMonitor never sends anything back -- it just listens. A Linux
    program only needs to open the port and read.
  * Every protocol byte has its top two bits set to '11' (0xC0-0xFF). This
    lets the receiver resynchronize on any byte boundary without needing
    a distinct escape/framing byte.
  * There are 16 "channels" (0-15), each carrying one status value. A
    channel's value is sent as a short burst of framing bytes:

      0xC0 | channel                  -- "select channel N" (header byte)
      0xE0 | (value & 0x0F)           -- low nibble of the value
      0xF0 | ((value >> 4) & 0x0F)    -- high nibble of the value; on
                                          receipt of *this* byte the
                                          receiver combines the two nibbles
                                          into a final 0-255 value for the
                                          channel and (if it changed) marks
                                          it dirty for redraw.

    So a channel update is always header+low+high = 3 bytes, e.g. to send
    channel 4 = 50 you transmit: C4 E2 F3

  * Channels 6 and 7 are special: instead of one 0-255 byte they carry a
    3-decimal-digit ASCII number (0-999), sent as header + 3 digit bytes:

      0xC0 | channel                  -- select channel (6 or 7)
      0xE0 | hundreds_digit           -- first character of the number
      0xF0 | tens_digit               -- second character
      0xD0 | units_digit              -- third character; this is the byte
                                          that finalizes/redraws the field

    This matches how AmpMonitor null-terminates a 3-character string buffer
    digit by digit rather than doing binary math, which is why it can show
    values that don't fit in a single byte (e.g. an 1802W power reading).

--------------------------------------------------------------------------
Channel meaning -- as of this revision, EVERY channel's role has been
identified by disassembling AmpMonitor.exe's byte-dispatch routine in full
(not just the few that were dynamically verified against the running
program earlier). See README.md for the full per-channel writeup and the
distinction between "confirmed live" (watched it happen on screen) and
"confirmed by disassembly" (read the compiled logic directly -- very high
confidence on *what* a channel drives, slightly lower on exact scaling
constants that were never seen actually rendered).

    2  -> NOT a live band indicator,    disassembly suggested this was the
          despite disassembly            primary band source (same logic as
                                          ch3), but empirically (calibrate_
                                          band.py, swept all 10 real ABCD
                                          codes against the real decoder)
                                          it held a constant value the
                                          entire time regardless of actual
                                          band -- still recorded in .raw,
                                          but no longer used for band.
    3  -> band, CONFIRMED live and       calibrate_band.py's sweep showed
          empirically calibrated         channel 3's raw value tracks the
                                          ABCD code exactly 1:1 (raw N ==
                                          ABCD code N, 1=160m..10=6m) --
                                          see AmplifierState.apply()
    4  -> temperature, deg C            CONFIRMED live + static
    8  -> FOR/REF power RANGE selector  static: a 0-7 value selecting
          (NOT antenna -- see ch11)      which multiplier (x0.4/x2/x4/x6/
                                          x8/x10) is applied to channels
                                          13 and 15's raw readings
   10  -> current, Amps                 static: drives a numeric field
                                          labelled "strom" (Russian for
                                          "current") in AmpMonitor's own
                                          data
   11  -> status bitfield (1 byte)      static: individual bits, not one
                                          value -- bit0=fan1 running,
                                          bit1=SWR alarm, bit2=Imax alarm,
                                          bit3=fan2 running, bit6=external
                                          ALARM_IN, bit7=antenna A/B select
                                          (this is the real antenna
                                          source; an earlier dynamic test
                                          had misattributed antenna to
                                          channel 8 -- corrected here)
   12  -> status bitfield (1 byte)      static: bit6=ALARMB flag,
                                          bit7=general error flag
   13  -> FOR power, raw ADC reading    static: watts = raw * multiplier
                                          selected by channel 8; drives
                                          the numeric FOR power field
   14  -> connected radio type          CONFIRMED live + static, full
                                          enum: 0=No Device, 1=Kenwood,
                                          2=Yaesu, 3=Icom, 4=Icom CI-V,
                                          5-7=Decoder. The front-panel
                                          "radio type" menu on a real
                                          DN-AUTO CPU was stepped through
                                          live and confirmed to walk this
                                          same list in this same order
                                          (2=Yaesu, 4=Icom CI-V, 5=Decoder
                                          directly observed on channel 14)
   15  -> REF power, raw ADC reading    static: drives a REF power
                                          BARGRAPH (drawn with BitBlt/
                                          FillRect, not shown as text) --
                                          scaled the same way as ch13
    9  -> fan mode (0=off,1=on,2=force) static structure + earlier
                                          dynamic test; exact enum values
                                          still a best guess
    6  -> 3-digit field, never displayed static: AmpMonitor receives and
    7  -> 3-digit field, never displayed  decodes these but never shows
                                          them anywhere in its own UI --
                                          genuinely unused/hidden data
    0  -> received, never displayed     static: parsed into the raw
    1  -> received, never displayed      channel array but AmpMonitor's
    5  -> received, never displayed      UI never reads them back out

In short: AmpMonitor.exe's own window shows a subset of what DN-AUTO
actually streams. Channels 0, 1, 5, 6, and 7 are received and decoded
internally but never surfaced anywhere in the program's UI -- this Linux
tool already captures them (via AmplifierState.raw / ChannelLogger), so it
can show strictly more than the original ever did, once real hardware
testing confirms what those hidden channels actually represent.

--------------------------------------------------------------------------
Is AmpMonitor (and this program) able to send commands back, e.g. change
band?

Effectively no. AmpMonitor's main loop only ever calls ReadFile on the
serial port -- there is no periodic/command-driven WriteFile in normal
operation. The one place it *does* write back is a one-shot 16-byte
handshake: the first time each of the 16 channels has been received, it
sends a single fixed acknowledgement byte (0xD0 for channel 0, 0xD1 for
channel 1, ... 0xDF for channel 15) back out the same port, gated so each
one only ever fires once per connection. That reads as "PC monitor is alive
and has seen channel N" bookkeeping, not a control/command channel -- the
bytes are fixed constants, not something the user or the data stream
influences.

The vendor manual mentions a *separate* Windows program (not this one) that
can switch bands in the DN-AUTO CPU over the same port. That program's
protocol has not been reverse engineered here (it wasn't provided) -- if
you get a copy of it, the same disassembly approach used for AmpMonitor.exe
would apply.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import dataclasses
import time
from typing import Callable, Optional


DEVICE_NAMES = {
    0: "No Device",
    1: "Kenwood",
    2: "Yaesu",
    3: "Icom",
    4: "Icom CI-V",
    5: "Decoder",
    6: "Decoder",
    7: "Decoder",
}

FAN_NAMES = {
    0: "off",
    1: "on",
    2: "force",
}


@dataclasses.dataclass
class ChannelUpdate:
    channel: int
    value: int
    raw_bytes: bytes


class DNAutoDecoder:
    """Streaming decoder for the DN-AUTO CPU serial protocol.

    Feed it raw bytes as they arrive from the serial port with `feed()`;
    it calls `on_update(ChannelUpdate)` every time a channel's value is
    fully received (i.e. on the finalizing high-nibble / third-digit byte),
    exactly mirroring when the real AmpMonitor program would redraw.

    This class does not know anything about *what* each channel means --
    see `AmplifierState` below for that layer.
    """

    def __init__(self, on_update: Callable[[ChannelUpdate], None]):
        self.on_update = on_update
        self._channel: Optional[int] = None
        self._low_nibble = 0
        self._digits = [0, 0, 0]
        self._digit_pos = 0
        self._values = [0] * 16
        self._pending_bytes = bytearray()

    def feed(self, data: bytes) -> None:
        for b in data:
            self._feed_byte(b)

    def _feed_byte(self, b: int) -> None:
        if (b & 0xC0) != 0xC0:
            # Not a framing byte -- the real device should never send
            # these, but ignore anything unexpected rather than crashing.
            return

        self._pending_bytes.append(b)
        top = b & 0x30

        if top == 0x00:
            # Header byte: 0xC0 | channel -- select channel, no value yet.
            self._channel = b & 0x0F
            self._digit_pos = 0
            self._pending_bytes = bytearray([b])
            return

        if self._channel is None:
            return  # got a data byte before any header; ignore

        if self._channel in (6, 7):
            self._feed_digit_byte(b, top)
        else:
            self._feed_nibble_byte(b, top)

    def _feed_nibble_byte(self, b: int, top: int) -> None:
        if top == 0x20:
            self._low_nibble = b & 0x0F
        elif top == 0x30:
            value = ((b & 0x0F) << 4) | self._low_nibble
            ch = self._channel
            self._values[ch] = value
            self.on_update(ChannelUpdate(ch, value, bytes(self._pending_bytes)))

    def _feed_digit_byte(self, b: int, top: int) -> None:
        digit = b & 0x0F
        if top == 0x20:
            self._digits[0] = digit
        elif top == 0x30:
            self._digits[1] = digit
        elif top == 0x10:
            self._digits[2] = digit
            value = self._digits[0] * 100 + self._digits[1] * 10 + self._digits[2]
            ch = self._channel
            self._values[ch] = value
            self.on_update(ChannelUpdate(ch, value, bytes(self._pending_bytes)))

    @property
    def values(self):
        return list(self._values)


# FOR/REF power range multipliers, indexed by channel 8's value (0-7).
# Reverse engineered from AmpMonitor.exe's disassembly: raw ADC reading
# (channel 13 for FOR, channel 15 for REF) times this multiplier gives the
# displayed wattage. Index 0 is hard-coded to 0 in the original binary.
POWER_SCALE_MULTIPLIERS = [0, 0.4, 2, 4, 6, 8, 10, 10]


@dataclasses.dataclass
class AmplifierState:
    """Best-effort semantic view built on top of DNAutoDecoder.

    See the module docstring's channel table for the confidence level of
    each field -- most are now backed by disassembly of AmpMonitor.exe's
    full byte-dispatch routine, not just guesses.
    """

    temperature_c: Optional[int] = None        # channel 4, CONFIRMED live
    device: Optional[str] = None                # channel 14, CONFIRMED live
    band: Optional[int] = None                  # channel 2 or 3 (raw 0-15 index)
    current_a: Optional[int] = None             # channel 10 ("strom")
    fan: Optional[str] = None                   # channel 9, structure confirmed
    power_scale_index: Optional[int] = None     # channel 8, raw 0-7 range selector
    for_power_raw: Optional[int] = None         # channel 13, raw ADC reading
    ref_power_raw: Optional[int] = None         # channel 15, raw ADC reading

    # channel 11 status bitfield (all static-confirmed, bit-level)
    fan1_running: Optional[bool] = None
    fan2_running: Optional[bool] = None
    swr_alarm: Optional[bool] = None
    imax_alarm: Optional[bool] = None
    alarm_in: Optional[bool] = None
    antenna: Optional[str] = None               # channel 11 bit 7 (NOT channel 8)

    # channel 12 status bitfield
    alarmb: Optional[bool] = None
    error_flag: Optional[bool] = None

    raw: dict = dataclasses.field(default_factory=dict)

    @property
    def temperature_f(self) -> Optional[float]:
        """Temperature in Fahrenheit, derived from the confirmed channel-4 Celsius value."""
        if self.temperature_c is None:
            return None
        return self.temperature_c * 9 / 5 + 32

    def _power_multiplier(self) -> Optional[float]:
        if self.power_scale_index is None or not (0 <= self.power_scale_index < len(POWER_SCALE_MULTIPLIERS)):
            return None
        return POWER_SCALE_MULTIPLIERS[self.power_scale_index]

    @property
    def for_power_w(self) -> Optional[float]:
        """FOR (forward) power in watts: channel 13's raw reading scaled by
        channel 8's range selector, matching AmpMonitor's own calculation."""
        mult = self._power_multiplier()
        if mult is None or self.for_power_raw is None:
            return None
        return self.for_power_raw * mult

    @property
    def ref_power_w(self) -> Optional[float]:
        """REF (reflected) power in watts, estimated with the same scaling
        as FOR power. AmpMonitor itself only ever draws REF as a bargraph
        (not text), so this specific number was never seen rendered --
        treat it as a reasonable inference, not a confirmed value."""
        mult = self._power_multiplier()
        if mult is None or self.ref_power_raw is None:
            return None
        return self.ref_power_raw * mult

    def apply(self, update: ChannelUpdate) -> None:
        self.raw[update.channel] = update.value
        ch, v = update.channel, update.value
        if ch == 4:
            self.temperature_c = v
        elif ch == 14:
            self.device = DEVICE_NAMES.get(v & 0x07, "?")
        elif ch == 3:
            # Channel 3's raw value == the ABCD band code directly (1=160m
            # .. 10=6m, 0=none), confirmed by calibrate_band.py sweeping
            # all 10 real codes against the real decoder/DN-AUTO. BANDS is
            # 0-indexed (BANDS[0]=="160" .. BANDS[9]=="6"), so subtract 1.
            # Channel 2 was assumed to be a second, cross-checking band
            # source (same disassembled logic as channel 3), but the same
            # sweep showed it holds a constant value regardless of actual
            # band -- not a live indicator, so it's intentionally ignored
            # here (still available via .raw[2] if that ever changes).
            raw = v & 0x0F
            self.band = (raw - 1) if 1 <= raw <= 10 else None
        elif ch == 10:
            self.current_a = v
        elif ch == 9:
            self.fan = FAN_NAMES.get(v, str(v))
        elif ch == 8:
            self.power_scale_index = v & 0x07
        elif ch == 13:
            self.for_power_raw = v
        elif ch == 15:
            self.ref_power_raw = v
        elif ch == 11:
            self.fan1_running = bool(v & 0x01)
            self.swr_alarm = bool((v >> 1) & 0x01)
            self.imax_alarm = bool((v >> 2) & 0x01)
            self.fan2_running = bool((v >> 3) & 0x01)
            self.alarm_in = bool((v >> 6) & 0x01)
            # antenna select is the sign bit of the raw signed byte, i.e.
            # bit 7 -- matches AmpMonitor's own `cmp byte,0; jns` test.
            signed = v - 256 if v >= 128 else v
            self.antenna = "B" if signed < 0 else "A"
        elif ch == 12:
            self.alarmb = bool((v >> 6) & 0x01)
            signed = v - 256 if v >= 128 else v
            self.error_flag = signed < 0


class ChannelLogger:
    """Utility for the empirical-mapping step described in README.md.

    Prints every channel change with a timestamp so you can correlate it
    by eye with something you just did on the real amplifier (changed
    band, kicked the fan on, kicked in RF, etc).
    """

    def __init__(self):
        self.state = AmplifierState()

    def __call__(self, update: ChannelUpdate) -> None:
        self.state.apply(update)
        ts = time.strftime("%H:%M:%S")
        extra = ""
        if update.channel == 4 and self.state.temperature_f is not None:
            extra = f"   [{self.state.temperature_c} C = {self.state.temperature_f:.0f} F]"
        elif update.channel == 14:
            extra = f"   [device={self.state.device}]"
        print(
            f"[{ts}] channel {update.channel:2d} = {update.value:>4d} "
            f"(0x{update.value:02x})   raw={update.raw_bytes.hex()}{extra}"
        )


def open_serial(port: str, baudrate: int = 9600):
    """Open the DN-AUTO's serial port. Requires `pyserial` (pip install pyserial)."""
    import serial  # local import so the rest of this module has no hard dependency

    return serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.5,
    )
