# DN-AUTO monitor: build notes

The full story of how the protocol was worked out, the channel map, and every
problem hit along the way. For install and everyday use, see the
[README](../README.md).

This is a Linux replacement for the Windows-only **AmpMonitor** program that
ships with the DN-AUTO CPU band-decoder / amplifier-protection controller
(dn-radio.com / ut2fw.com). It talks to the same RS-232 monitor port
(connector **X7** on the DN-AUTO CPU board, standard pinout: pin 1 -> PC RxD,
pin 2 -> PC TxD, pin 3 -> GND, 9600 baud 8N1) and decodes the amplifier's
live status: band, power, temperature, current, fan, antenna, and so on.

## How this was built

The vendor manual documents the hardware (pinouts, menus, schematics) but
never publishes the actual serial protocol AmpMonitor speaks to the
controller. To get it, `AmpMonitor.exe` was disassembled (it's a small,
unobfuscated MinGW-compiled C/C++ program using the raw Win32 serial API --
no protocol logic is hidden in a runtime or DLL), and the parsing routine
inside its WM_TIMER handler was reverse engineered instruction by
instruction. The result was then verified live: `AmpMonitor.exe` was run
under Wine against a virtual serial port, synthetic bytes matching the
derived protocol were injected, and screenshots of the running program
confirmed the decoded meaning for several fields (see "Confidence" below).

## The wire protocol (confirmed)

* RS-232, **9600 baud, 8 data bits, no parity, 1 stop bit**.
* The DN-AUTO CPU continuously *streams* its status out the TX line, all on
  its own. AmpMonitor never transmits anything back during normal
  monitoring -- it just opens the port and reads. A Linux program only
  needs to do the same (no query/poll command required).
* Every protocol byte has its top two bits set (`0xC0`-`0xFF`), which is how
  the parser resynchronizes on any byte boundary without a separate framing
  byte.
* There are **16 channels** (0-15), each carrying one status value, sent as
  a 3-byte burst:

  ```
  0xC0 | channel                -- select channel N (0-15)
  0xE0 | (value & 0x0F)         -- low nibble of the value
  0xF0 | ((value >> 4) & 0x0F)  -- high nibble; receiving this byte finalizes
                                    the value (0-255) and marks it changed
  ```

  Example: channel 4 = 50 is transmitted as `C4 E2 F3`.

* Channels **6 and 7** are special: they carry a 3-decimal-digit ASCII
  number (0-999) instead of a single byte, sent as:

  ```
  0xC0 | channel      -- select channel (6 or 7)
  0xE0 | hundreds
  0xF0 | tens
  0xD0 | units         -- finalizes the value
  ```

  This is presumably used for numbers too big to fit in one byte, most
  likely the forward/reflected power readout in watts.

`dnauto_protocol.py`'s `DNAutoDecoder` class implements exactly this and is
covered by `test_dnauto_protocol.py` (8 tests, all offline -- no hardware
needed to check the decoder logic is correct).

## Channel meaning (confidence levels)

Every one of AmpMonitor.exe's 16 channel handlers has now been read
directly out of its disassembly (not just the handful that were watched
live earlier) -- see `dnauto_protocol.py`'s module docstring for the full
writeup of how each one was traced. Two confidence levels appear below:
**confirmed live** means it was watched happening on the real program's
screen; **confirmed by disassembly** means the compiled logic driving it
was read directly, which is very reliable for *what* a channel does, and
fairly reliable (but not eyeball-verified) for exact scaling constants.

| Channel | Meaning | Confidence |
|---|---|---|
| 4 | Temperature, deg C | **Confirmed live** + disassembly |
| 14 | Connected radio type, full enum (0=No Device, 1=Kenwood, 2=Yaesu, 3=Icom, 4=Icom CI-V, 5-7=Decoder) | **Confirmed live** + disassembly |
| 2 | Disassembly suggested this was a band source (identical logic to channel 3), but `calibrate_band.py`'s real-hardware sweep (all 10 ABCD codes) showed it holds a constant value regardless of actual band -- not a live indicator, no longer used for the band display | Confirmed by disassembly (what it *could* drive); empirically NOT live (calibrate_band.py) |
| 3 | Band, drives the main band-name lookup/display -- raw value is the ABCD code directly (1=160m..10=6m), confirmed empirically | Confirmed by disassembly + empirical calibration (calibrate_band.py) |
| 8 | FOR/REF power **range selector** (0-7), NOT antenna -- picks the x0.4/x2/x4/x6/x8/x10 multiplier applied to channels 13 and 15 | Confirmed by disassembly (corrects an earlier live-test misattribution -- see below) |
| 10 | Current, Amps (labelled "strom" -- Russian for current -- in AmpMonitor's own data) | Confirmed by disassembly |
| 11 | Status bitfield, one byte, not a single value: bit0=fan1 running, bit1=SWR alarm, bit2=Imax alarm, bit3=fan2 running, bit6=external ALARM_IN, **bit7=antenna A/B select** | Confirmed by disassembly |
| 12 | Status bitfield: bit6=ALARMB, bit7=general error flag | Confirmed by disassembly |
| 13 | FOR power, raw ADC reading -- watts = raw x (multiplier from channel 8) | Confirmed by disassembly |
| 15 | REF power, raw ADC reading, same scaling as channel 13 -- AmpMonitor only ever draws this as a bargraph (BitBlt/FillRect), never as a number, so the wattage conversion is a reasonable inference rather than something actually seen rendered | Confirmed by disassembly (display mechanism); wattage math inferred |
| 9 | Fan mode (0=off/1=on/2=force, guessed enum) | Confirmed live that it drives *a* fan indicator, structure confirmed by disassembly; exact enum values still a guess |
| 6 | 3-digit field -- received and decoded, but never displayed anywhere in AmpMonitor's own UI | Confirmed by disassembly (that it's unused) |
| 7 | Same as channel 6 -- structurally a 3-digit field, never displayed | Confirmed by disassembly (that it's unused) |
| 0, 1, 5 | Received and stored, but never read back out or displayed anywhere in AmpMonitor.exe | Confirmed by disassembly (that they're unused) |

**Antenna correction:** an earlier session had tentatively attributed
antenna A/B to channel 8 from a live Wine test where sending a value there
happened to coincide with the antenna indicator changing. The full
disassembly pass makes clear that's actually channel 11's bit 7 -- channel
8 is a power-range selector with no antenna logic at all. If you've been
using `dnauto_gui.py` from an earlier version of this package, channel 8
in your own scripts should be repointed to channel 11.

**What AmpMonitor never shows you:** channels 0, 1, 5, 6, and 7 are all
received and decoded by the original program internally, but never once
read back out to draw anything on screen. This tool already captures all
of them (`AmplifierState.raw` / `ChannelLogger`) -- once you empirically
determine what they represent (see below), this Linux replacement can
display genuinely more than AmpMonitor.exe ever did.

The one field that's still a real unknown is **SWR** -- nothing in
AmpMonitor's disassembly points at a dedicated SWR display (it's
presumably computed live from the FOR/REF readings, or shown only on the
DN-AUTO CPU's own touchscreen rather than in the PC monitor). Channels 0,
1, 5, 6, 7 and SWR are exactly what's left to nail down empirically.

## Finishing the channel mapping with your real hardware

1. Wire a USB-to-RS232 adapter (or a real COM port) to the DN-AUTO CPU's
   X7 connector as described in the vendor manual, and find the device
   node, e.g. `/dev/ttyUSB0`.
2. Install pyserial: `pip install pyserial` (or `pip install --user pyserial`).
3. Run the raw logger:

   ```
   python3 dnauto_monitor.py /dev/ttyUSB0 --raw
   ```

   This prints every channel value change with a timestamp, e.g.:
   ```
   [14:02:11] channel  4 =   47 (0x2f)   raw=c4e2f3
   ```
4. Change one thing at a time on the amplifier/transceiver -- switch bands,
   key up briefly to move the power meter, watch the temperature climb,
   toggle the antenna, force the fan on -- and note which channel number
   reacts and how its value tracks what you did.
5. Update `AmplifierState.apply()` in `dnauto_protocol.py` with what you
   find (it already has 4 of the mappings filled in as a template).

### Calibrating the band channel automatically (with the ABCD bridge)

Channel 2/3 (band) was originally *guessed* to be a direct 0-based index
into `BANDS`, and that guess turned out to be unreliable in practice (seen
getting stuck reporting the wrong band). Since DN-AUTO's own band input
(connector X11, active because the front-panel radio-type menu is set to
"Yaesu") listens to the exact same physical ABCD bus as the DXWorld
decoder, and that bus can now be driven on demand via the Arduino bridge,
this can be calibrated for real instead of guessed again:

```
sudo systemctl stop kx3-band-bridge.service   # so it doesn't fight you for the Arduino
python3 calibrate_band.py --arduino /dev/ttyACM0 --dnauto /dev/ttyUSB0
```

This drives the Arduino through all 10 ABCD codes, waits for the amp to
settle on each one, and reads back whatever DN-AUTO itself reports on
channels 2 and 3 for that band -- producing a code/band/ch2/ch3 table plus
a best-guess formula (e.g. "raw = code - 1") if the pattern is a simple
fixed offset. Use that to fix the indexing in `AmplifierState.apply()` and
`BANDS` lookups in `dnauto_gui.py`, then restart
`kx3-band-bridge.service` (`sudo systemctl start kx3-band-bridge.service`)
once you're done.

If ch2 and ch3 never change across the whole sweep, that means X11 isn't
actually wired into the ABCD bus (or isn't being read for some other
reason) -- in which case DN-AUTO's own band channel can't be calibrated at
all this way, and rigctld remains the only accurate band source for the
monitor, same as it is right now.

If you'd rather cross-check against the original program directly, the
Windows `AmpMonitor.exe` has a hidden debug view: press **F4** (RX1B) or
**F5** (RX LOG) to see the raw bytes it's receiving, or **F6** for port
settings. That view was very useful for this reverse-engineering effort and
would let you eyeball raw traffic side-by-side with the normal dashboard on
real hardware (e.g. via Wine, or a Windows machine/VM) if you want a second
source of truth beyond this repo's decoder.

## Figuring out the DN-AUTO's "radio type" menu options (for non-Yaesu rigs)

The DN-AUTO CPU has its own front-panel menu for selecting which radio/band
-decode scheme it should read (you currently have yours set to "Yaesu",
which is why it tracks band changes on your FT-991A). That menu lives in
the amplifier's own firmware, separate from AmpMonitor/X7 -- there's no
copy of that firmware here to disassemble, so the exact label-to-number
mapping for every menu entry isn't known for certain yet.

What *is* known, from disassembling AmpMonitor.exe: channel 14 on the
monitor stream reports back a "device" value with this enum (see
`DEVICE_NAMES` in `dnauto_protocol.py`):

```
0 = No Device
1 = Kenwood
2 = Yaesu
3 = Icom
4 = Icom CI-V
5, 6, 7 = Decoder  (three separate "Decoder" entries, meaning unclear)
```

Since channel 14 is DN-AUTO reporting back whatever the front panel is
currently set to, the front-panel menu almost certainly steps through this
exact same list in this exact order -- which means you can find out for
certain, today, without needing a non-Yaesu radio connected at all:

1. Run `python3 dnauto_monitor.py /dev/ttyUSB0 --raw` (or `dnauto_diagnose.py`,
   which now also prints the decoded device name next to channel 14).
2. Go into the DN-AUTO's front-panel menu and step through the radio-type
   options one at a time, pausing on each.
3. Note the channel 14 value/name printed for each menu position.

That will tell you, in order: which numeric value is "Yaesu" (should be 2,
confirming the mapping), and exactly what each of the other menu entries
is -- including whichever one turns out to be for a Kenwood/Icom radio, or
one of the "Decoder" modes, which is most likely what you'd use for a rig
that isn't Yaesu/Kenwood/Icom, or that you want to feed band data into
DN-AUTO some other way. If one of those "Decoder" positions turns out to
correspond to a label you didn't expect, send me what you see next to each
menu entry and I'll fold the real names into `DEVICE_NAMES` so this is
documented properly instead of guessed.

**Update from real hardware testing:** stepping through the DN-AUTO's
front-panel radio-type menu while watching channel 14 confirmed this
directly -- 2 really is Yaesu, and the menu also stepped through 4 (Icom
CI-V) and 5 (Decoder), landing exactly on the enum above in the same
order. So the mapping isn't a guess anymore for at least those three
entries; if you get a chance to also note what 0, 1, 3, 6, and 7 show,
that'll fill in the rest (0/No Device and 1/Kenwood and 3/Icom are
probably just before/around where you already were).

One side effect worth knowing about: channel 3 (the "secondary band
source") dropped to 0 as soon as the menu left Yaesu mode, while channel 2
kept reporting the same band the whole time. That fits with channel 3
being specific to Yaesu's particular band-data scheme (the manual's
"cross-checking two RIGs at once" from two independent VFO/band lines)
rather than something every radio type populates -- so don't be surprised
if channel 3 stays at 0 once you're not in Yaesu mode; channel 2 is the
one to trust as "the" band in general.

One thing to keep in mind: X7 (the port this whole tool reads) is
DN-AUTO's *monitor output* only -- confirmed by disassembly, AmpMonitor
never writes band/control data back over it (see below). So whatever the
menu calls "USB" almost certainly is not about *this* USB-serial link;
it's more likely referring to how DN-AUTO gets band data *in* from the
radio (e.g. a rig connected via a real CAT/serial command set rather than
parallel band-data pins) -- the menu step-through above will confirm
exactly what it means rather than leaving it a guess.

## Is AmpMonitor (or this program) able to send commands back -- e.g. change band?

Effectively no. Disassembling `AmpMonitor.exe` shows its main loop only ever
calls `ReadFile` on the serial port -- there's no periodic or
command-driven `WriteFile` in normal operation. The one place it *does*
write anything back is a one-shot 16-byte handshake: the first time each of
the 16 channels is received, it sends a single fixed acknowledgement byte
(`0xD0` for channel 0, `0xD1` for channel 1, ... `0xDF` for channel 15) back
out the port, and each one only ever fires once per connection. That's
"PC monitor is alive and has seen channel N" bookkeeping, not a control
channel -- the bytes are fixed constants, not something you or the data
stream influence. `dnauto_monitor.py` and `dnauto_gui.py` are pure
listeners for the same reason; they never write to the port.

The vendor manual references a *separate* Windows program (not AmpMonitor)
that can switch bands on the DN-AUTO CPU over the same port. That program
wasn't provided here, so its command protocol hasn't been reverse
engineered -- if you get a copy of it, the same disassembly approach used
for AmpMonitor.exe would apply.

## Files

* `dnauto_protocol.py` -- the protocol decoder (`DNAutoDecoder`) and a
  semantic wrapper (`AmplifierState`) with the confirmed fields filled in,
  including a `temperature_f` property for Fahrenheit.
* `dnauto_monitor.py` -- CLI tool: live text dashboard, or `--raw` mode for
  the mapping work described above.
* `dnauto_rawdump.py` -- zero-assumptions raw hex dump of whatever arrives
  on the port, for troubleshooting a dead/misbehaving connection (see
  "Troubleshooting" below) before suspecting the protocol decoder itself.
* `dnauto_diagnose.py` -- anomaly hunter for "the screen goes crazy with
  power/SWR" -- flags suspicious channel updates and prints the raw bytes
  around them (see "Erratic power/SWR readings" below).
* `dnauto_gui.py` -- styled desktop GUI (see below).
* `rigctl_client.py` -- optional helper for reading band/frequency from a
  running Hamlib `rigctld` instance instead of DN-AUTO's own band channel.
* `test_dnauto_protocol.py` -- offline tests that replay byte sequences
  verified against the real AmpMonitor.exe.
* `test_rigctl_client.py` -- offline tests for the frequency-to-band
  mapping in `rigctl_client.py`.
* The KX3-to-amp band-switching bridge (`kx3_band_bridge.py` and its
  Arduino sketch) now lives in its own repo:
  [kx3-band-bridge](https://github.com/Saltyhaus/kx3-band-bridge).

## Troubleshooting "nothing happens" on X7

If neither this tool nor the original `AmpMonitor.exe` receives anything
over your USB-serial adapter wired into X7, that points at something
upstream of the software (both are independent codebases, so it's very
unlikely both have the same bug). In rough order of likelihood:

1. **Voltage-level mismatch.** The manual describes X7 as a genuine
   RS-232 port. A lot of cheap USB-serial adapters (the small FTDI/CP2102/
   CH340 boards commonly used for Arduino work) are actually **TTL-level**
   (0-5V), not true RS-232 (which swings roughly ±5 to ±12V). Feeding a
   true RS-232 signal into a TTL adapter's RX pin (or vice versa) generally
   does *not* work -- you need an adapter that specifically says "RS232"
   and has its own onboard line driver chip (often a DB9 connector on the
   adapter, not bare wires), not a bare TTL breakout board.
2. **TX/RX swapped, or no shared ground.** Double-check X7 pin 1 (DN-AUTO
   OUT) goes to your adapter's RxD/RX input, X7 pin 2 (DN-AUTO IN) goes to
   the adapter's TxD/TX output, and X7 pin 3 is tied to the adapter's GND.
   Swapped TX/RX or a missing ground reference is a very common wiring
   mistake and produces exactly "nothing happens."
3. **Linux grabbing the port before you do.** `ModemManager` and (less
   commonly) `brltty` auto-probe newly plugged-in serial/USB devices,
   which can produce a single flash of activity right at plug-in time
   (that's them sending probe commands) and then leave the port either
   busy or in a weird state. Check with:
   ```
   systemctl is-active ModemManager
   systemctl is-active brltty
   ```
   If either says `active`, try `sudo systemctl stop ModemManager` (and/or
   `brltty`) and test again -- this is a very well-known Linux gotcha for
   USB-serial devices that aren't actually modems.
4. **Permissions or wrong device.** Run `ls -l /dev/ttyUSB*` after
   plugging in -- confirm the device exists and that your user is in the
   group that owns it (usually `dialout`; check with `groups`, and if
   you're not in it: `sudo usermod -aG dialout $USER`, then log out/in).
   Also confirm you're pointing at the right `/dev/ttyUSBx` if you have
   more than one adapter plugged in.

**`dnauto_rawdump.py`** is included specifically for this kind of
troubleshooting -- it prints every raw byte it receives as hex, with zero
assumptions about the DN-AUTO protocol, so it'll show you garbage bytes
just as readily as valid ones:

```
python3 dnauto_rawdump.py /dev/ttyUSB0
```

If it runs for a while and reports **zero bytes ever received**, the
problem is entirely electrical (levels, wiring, or the port being grabbed
by something else) -- no software fix will help until that's sorted out.
If you do see hex bytes but they don't look like the framing described
below (every byte should have its top two bits set, i.e. every byte should
be `0xC0` or higher), that points more toward a level/wiring issue
producing garbled data rather than a total disconnection.

## Erratic power/SWR readings ("the screen goes a bit crazy")

If FOR power / SWR / similar fields flicker or spike on screen while the
amplifier isn't transmitting, the first thing worth ruling out is a
repeating block of bytes the DN-AUTO CPU streams alongside its normal 16
channels that neither AmpMonitor.exe nor this decoder was ever designed to
understand. A real capture (`dnauto_rawdump.py`) on a live X7 line looks
like this, once per roughly one-second cycle:

```
c7 e0 f0 d0 c6 e0 f0 d0 c5 e0 f0 c4 ea f1 c3 e0 f0 c2 e9 f0 c1 e0 f8 c0 e0 f8
41 4b 8c 8c 8c 80 8a 83 8c 8f 8e 8d 8c 82 8c 84 8c 86 8c 8b 8c 85 8c 87 60 44 4c 42
cf e0 f0 ce e2 f0 cd e0 f0 cc e6 f3 cb e1 fb ca e0 f0 c9 e0 f2 c8 e5 f0
```

The first and third lines are ordinary channel updates (channels 7 down to
0, then 15 down to 8) -- completely normal. The middle ~28-byte run is the
odd part: none of those bytes have both top bits set (`0xC0` or higher),
so they don't match the wire format described above at all.

**This block is not the cause of spurious readings, and it's not a bug in
this tool.** Disassembling AmpMonitor.exe's own byte-dispatch loop shows it
does the exact same `(byte & 0xC0) == 0xC0` check as `DNAutoDecoder`
before treating a byte as a channel header/value byte (see
`404e01`-`404e0b` in the disassembly: `and eax,0xc0` / `cmp eax,0xc0` /
`jne <skip-byte>`). Any byte that fails that check -- including this whole
block -- is silently skipped by *both* programs. So this specific
repeating pattern is provably inert; it cannot be what's making the power/
SWR fields jump. It's also too consistent (byte-for-byte identical every
cycle, for as long as it's been captured) to be random electrical noise --
it looks like a second, legitimate stream of data the DN-AUTO CPU emits
that nothing has ever needed to decode before. What it actually represents
is still unknown; if you want to chase it, the same "change one thing,
watch what byte reacts" method used for the main channels would apply
(toggle the front panel band/antenna/fan controls and see which byte in
that ~28-byte run changes).

Given that block is a dead end for explaining the symptom, the more likely
culprit is the physical link itself corrupting a *real* channel-13/15
byte in a way that still happens to pass the `0xC0` check (a single
flipped bit in a value nibble is enough), producing a brief bogus power
reading before the next update overwrites it. That points at the
USB-serial adapter or cabling rather than the protocol:

* Note which adapter is actually wired to X7 when you see this. If it's
  the FTDI FT232R adapter that failed its own loopback self-test earlier
  in this troubleshooting, that adapter is a prime suspect even though it
  is clearly receiving *something* -- a marginal/flaky adapter can pass
  bytes most of the time and still drop or corrupt bits occasionally. Try
  the Belkin F5U409 adapter or the dedicated RS-232 adapter instead and
  see if the glitch still happens.
* Run `dnauto_diagnose.py` (below) while reproducing the glitch -- it
  watches specifically for FOR/REF power or error-flag updates that look
  wrong and prints the exact raw bytes around them, which will show
  whether a real channel byte got corrupted or something else is going on.

```
python3 dnauto_diagnose.py /dev/ttyUSB0
python3 dnauto_diagnose.py /dev/ttyUSB0 --power-threshold 5   # only flag jumps above 5W
```

**Update from real hardware testing:** switching from the FTDI FT232R adapter
(the one that failed its own loopback self-test) to the Belkin F5U409 on a
different USB port made the repeating ~28-byte mystery block disappear
completely, and produced ~14 seconds of perfectly stable channel data with
no flagged anomalies. That's a strong signal the mystery block, and likely
the erratic readings too, were coming from the FTDI adapter/port rather
than from the DN-AUTO CPU or the protocol itself -- worth treating that
adapter/port as unreliable going forward and sticking with the Belkin (or
your new dedicated RS-232 adapter once it arrives).

The one thing worth knowing about: the very first channel update
`dnauto_diagnose.py` sees right after you start it can occasionally look
like a brief bogus spike (e.g. an implausibly high REF power) even on an
otherwise clean link. That's expected -- the decoder starts listening
mid-stream, so its first "channel selected" byte can actually be a nibble
byte left over from a channel update that was already in progress before
you opened the port, producing one bogus combined value before everything
resyncs on the next header byte. If it's only the very first reading and
everything is rock-solid after that, it's not a real fault -- it's just a
startup artifact of opening the port mid-byte.

## Making the port name survive reboots

`/dev/ttyUSB0`, `/dev/ttyUSB1`, etc. are assigned in whatever order Linux
happens to enumerate USB devices at boot, so they can and do shuffle around
-- plug in one more gadget, or plug the same adapter into a different port,
and the DN-AUTO adapter might come up as `ttyUSB2` instead of `ttyUSB0`
next time.

The good news: `/dev/serial/by-id/usb-Belkin_USB_PDA_Adapter_0109_XXXXXX-if00-port0`
(or whatever your `ls -la /dev/serial/by-id/` shows) is **already** immune
to this. It's built from the adapter's own USB vendor/model/serial number,
not from enumeration order, so it's guaranteed by udev to always point at
that exact physical adapter, reboot after reboot, port after port. The
simplest fix is just to always run the tools against that path instead of
`/dev/ttyUSBx`:

```
python3 dnauto_gui.py /dev/serial/by-id/usb-Belkin_USB_PDA_Adapter_0109_XXXXXX-if00-port0
```

If you'd rather have something short to type, add a custom udev rule that
gives it a friendly name like `/dev/dnauto`. Create
`/etc/udev/rules.d/99-dnauto.rules` (needs `sudo`) with:

```
SUBSYSTEM=="tty", ENV{ID_SERIAL}=="Belkin_USB_PDA_Adapter_0109_XXXXXX", SYMLINK+="dnauto"
```

That `ID_SERIAL` value is exactly the middle part of the by-id name you
already have (everything between `usb-` and `-if00-port0`) -- copy it from
your own `ls -la /dev/serial/by-id/` output if you switch adapters later.
Then apply it without rebooting:

```
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Unplug and replug the adapter (or just check right away -- `trigger`
usually re-runs the rules immediately), then confirm:

```
ls -l /dev/dnauto
```

It should show a symlink pointing at whatever `ttyUSBx` the Belkin landed
on this time. From then on, `/dev/dnauto` always means this specific
adapter, and you can use it directly:

```
python3 dnauto_gui.py /dev/dnauto
```

Want a friendly name for another adapter too (e.g. once your dedicated
RS-232 adapter arrives)? Same pattern -- add another line to the same
rules file with that adapter's own `ID_SERIAL` and a different `SYMLINK+=`
name.

## Running the text dashboard

```
pip install pyserial
python3 dnauto_monitor.py /dev/ttyUSB0
```

It'll show temperature, antenna, fan and radio type immediately (those are
confirmed), plus a raw dump of every other channel's current value so you
can watch them change live while you finish the mapping.

## Running the styled GUI

`dnauto_gui.py` is a dark-themed Tkinter dashboard -- a big teal band
readout, FOR/REF power bar meters, and a bottom row of TEMP / CURRENT / SWR
/ FAN / ANT stats, loosely evoking the dark navy-and-teal look of a modern
transceiver display (like the FT-991A) rather than being a literal clone --
the amplifier's protocol only reports status numbers, not a spectrum scope
or waterfall, so there's nothing to draw there.

```
pip install pyserial
python3 dnauto_gui.py /dev/ttyUSB0        # live, from real hardware

python3 dnauto_gui.py --demo              # no hardware needed -- feeds
                                           # itself fake data so you can see
                                           # what it looks like right away
```

Defaults to Fahrenheit on launch; click the **TEMP** box to toggle to
Celsius and back.

The **ON TOP** slider switch in the top-right corner pins the window above
every other window on the desktop (not just other app windows) when
switched on -- handy for keeping the amp visible while operating in other
programs. Off by default; flip it per-session, it doesn't persist between
launches.

Only channels 4 (temperature), 8 (antenna), 9 (fan) and 14 (radio type) are
confirmed against real hardware. The FOR/REF power meters are wired to
placeholder channel numbers in the `CHANNEL_MAP` dict near the top of
`dnauto_gui.py` -- they'll show `0` until you finish the empirical mapping
above and fill in the real channel numbers there (the file has comments
showing exactly which lines to edit). Band is the one exception -- see
below.

### Getting the band from rigctld instead of guessing DN-AUTO's channel

If you already run (or can run) Hamlib's `rigctld` against your radio --
e.g. you have a CAT cable from the transceiver straight to your Linux box,
independent of the DN-AUTO wiring -- you can skip the band-channel mapping
entirely and get the BAND readout straight from the radio instead:

```
python3 dnauto_gui.py /dev/ttyUSB0 --rigctld              # default 127.0.0.1:4532
python3 dnauto_gui.py /dev/ttyUSB0 --rigctld 127.0.0.1:4532  # or --demo instead of a port
```

**The big band readout always shows what DN-AUTO itself reports (channel
3, empirically calibrated -- see "Calibrating the band channel
automatically" above), never rigctld.** That's deliberate: the whole point
of this monitor is watching what the *amplifier* is actually doing, not
just mirroring what the radio requested -- if the display just echoed
rigctld, a stuck relay or a dead decoder line could never show up here.

What `--rigctld` adds instead is a live cross-check/alarm. `dnauto_gui.py`
polls rigctld once a second for VFO frequency and maps it to a band the
same way the band strip does. When rigctld is reachable (green **● RIG**
indicator), a small status line under the frequency reads **"✓ matches
radio"** in green as long as DN-AUTO's reported band agrees with what the
radio commanded. If they disagree for more than ~2.5 seconds (long enough
to not false-alarm on normal relay-switching lag right after a band
change), it flips to a red **"⚠ MISMATCH -- amp=Xm radio=Ym"** and the big
band readout itself turns red too -- exactly the "amp and radio are out of
sync" alarm this was built for. If rigctld becomes unreachable, the ●RIG
indicator turns red and the status line says there's no rig data to
cross-check against; the band readout itself is unaffected either way,
since it never depended on rigctld to begin with.

Note: DN-AUTO's own Yaesu interface (connector X11, if you've wired it) is
*not* a serial/CAT link -- it's Yaesu's older 4-wire parallel "Band Data"
output (Data A/B/C/D + ground, same pinout as the FT-1000MP's Band Data
connector), so it can't be repurposed into a rigctld connection. Use your
radio's own CAT-capable port (e.g. the FT-991A's rear USB port) for
rigctld -- that's a separate, independent connection from whatever's wired
to the DN-AUTO CPU.

Requires Tkinter, which usually ships with Python already. If
`import tkinter` fails:

```
Debian/Ubuntu:  sudo apt install python3-tk
Fedora:         sudo dnf install python3-tkinter
Arch:           sudo pacman -S tk
```

### Clickable desktop icon

Instead of typing the command every time, `dnauto-monitor.desktop` +
`dnauto_monitor_launcher.sh` give you a normal clickable app icon (the one
in `assets/icon.png`) that launches the GUI with no terminal window.

The launcher script is hardcoded to your actual setup: it runs
`dnauto_gui.py` against `/dev/serial/by-id/usb-Belkin_USB_PDA_Adapter_0109_XXXXXX-if00-port0`
(the stable by-id path for the Belkin adapter -- see the by-id section
above for why this is used instead of `/dev/ttyUSB0`), plus `--rigctld
127.0.0.1:4534` so the KX3's rigctld is available as a live cross-check
against DN-AUTO's own (now empirically calibrated -- see "Calibrating the
band channel automatically") band reading, per "Getting the band from
rigctld instead of guessing DN-AUTO's channel" above. The big band
readout itself always comes from DN-AUTO, never rigctld -- rigctld only
drives the small "matches radio" / "MISMATCH" status line. If your adapter
ID, rigctld port, or setup changes, edit the `PORT=` / `RIGCTLD=` lines
near the top of `dnauto_monitor_launcher.sh`.

Because there's no terminal to show an error in if something's wrong (radio
off, adapter unplugged, folder moved), the launcher checks for the serial
device first and pops up a graphical error dialog instead of silently
opening a blank, frozen-looking GUI window. Everything it does is also
logged to `~/.local/share/dnauto-monitor/monitor.log` for troubleshooting.

To install:

```
chmod +x dnauto_monitor_launcher.sh dnauto-monitor.desktop
mkdir -p ~/.local/share/applications
cp dnauto-monitor.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications   # optional, usually automatic
```

It should now show up in your application launcher/menu under "DN-AUTO
Monitor" -- search for it like any other installed app. To also put an
icon on the Desktop itself:

```
cp dnauto-monitor.desktop ~/Desktop/
```

On GNOME (and some other modern desktops), a `.desktop` file placed on the
Desktop is untrusted by default the first time -- right-click it and choose
"Allow Launching" (or double-click and confirm "Trust and Launch") once
before it'll run.

### Pinning it to the Ubuntu/GNOME dock

To pin the app to the left-side dock with the real icon showing (instead
of a generic fallback), two things have to line up: the running window's
`WM_CLASS` has to match the `.desktop` file's `StartupWMClass`, so GNOME
Shell can tell which running window belongs to which launcher. Both sides
of that are already set up here -- `dnauto_gui.py` sets its window class to
`dnauto-monitor` (`tk.Tk(className="dnauto-monitor")`), and
`dnauto-monitor.desktop` declares `StartupWMClass=dnauto-monitor` to
match.

To pin it:

1. Reinstall the `.desktop` file if you installed it before this update:
   `cp dnauto-monitor.desktop ~/.local/share/applications/` (overwrites
   the old one).
2. Launch the app from the GNOME Activities search or applications menu
   (not by running the script directly in a terminal) -- search "DN-AUTO"
   and open it that way at least once.
3. While it's running, right-click its icon in the dock and choose **Add
   to Favorites** (older GNOME versions may say "Pin to Dash"). It'll stay
   pinned there from then on, icon and all, whether the app is running or
   not.

If the dock still shows a generic icon instead of the meter icon after
this, log out and back in once (GNOME's icon cache and window-matching can
be sticky about picking up a `.desktop` file change immediately).
