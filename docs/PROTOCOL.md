# RayNeo / FFalcon USB HID Protocol (reverse-engineered)

Derived from the official Android app `雷鸟 XR 眼镜_2.1.2.apk`
(native library `libFFalconXRServer.so` + Java layer `com.tcl.xr.api.AirApi`,
`com.rayneo.adapter.device.GlassDeviceManager`, `com.ffalcon.xr.sdk.FxrApi`).

## Transport

The glasses are a **DisplayPort / USB-C external display**, *not* a Bluetooth
device. The app uses **libusb bulk transfers** to a vendor-specific HID-like
interface.

- USB IDs (from `com/rayneo/adapter/usb/UsbReceiver.java`):
  - `VID 0x1BCB (7099)` / `PID 0xAF50 (44880)`  — legacy
  - `VID 0x3951 (14657)` / `PID 0xAF50 (44880)` — current
- Device interface selection (`XRUsbConnection::QueryInterface`):
  - `bInterfaceClass == 0xFF` (vendor-specific) **or** `0x03` (HID)
  - `bNumEndpoints != 0`
  - Endpoints must be **bulk** (`bmAttributes & 0x02`), one IN + one OUT.
- App calls `libusb_set_auto_detach_kernel_driver(handle, 1)` then
  `libusb_claim_interface` on the matched interface.

## Frame format

Commands are built into a **64-byte (0x40) buffer** and sent as one bulk OUT
transfer (`XRUsbController::SendCommand`).

`XRUsbConnection::ResetCommandBuffer`:
```
buf[0]    = 0x66          # magic / marker for a host->device command
buf[1]    = command       # FXRUsbCommand (uint8)
buf[2]    = value/sub-op  # (uint8)
buf[3..]  = payload       # optional std::initializer_list<uint8>
buf[35..] = 0x00 (zero padding to 64 bytes)
```

The bulk OUT length is the OUT endpoint's `wMaxPacketSize` (64), timeout 100 ms.

Responses arrive asynchronously on the bulk IN endpoint
(`XRUsbConnection::StartListening` / `QueueTransfer`). Validation in
`XRUsbController::HandlePacket`:
- `len >= 0x40 (64)`
- `resp[0] == 0x99`  (magic for a device->host response)
- Dispatch key is `resp[8]` (the echoed command).

## Command set (byte 1 of the command frame)

Values below are the `w1` argument to
`XRUsbController::SendHidCommand(FXRUsbCommand, uint8 value, initializer_list<uint8>)`.

### Two methods, checked against each other

The table was built twice, from different directions:

1. **Capstone**, reading the ELF's own bytes: scan for `bl`/`b` to the PLT stub
   at file vaddr `0xbbc70`, then walk back for the `w1` immediate. Commands whose
   opcode is computed rather than loaded come out as "indirect".
2. **Ghidra**, reading its own reference table and decompiler: every reference to
   the stub and to the real `SendHidCommand` (`0xa21b4`, from `nm -D`), then the
   arguments straight out of the C.

Both give **the same 33 opcodes**, with nothing found by only one of them.
Ghidra also names the containing function for each call, which is where the names
in the table come from, and it identifies exactly one computed opcode --
`PanelFrameRateSet`, which the capstone scan had also flagged as indirect. The two
agree on that too.

The value arguments were checked the same way. Worth singling out:

| call | decompiled as | confirms |
|---|---|---|
| `PanelLunaSave` | `SendHidCommand(h, 0xd, 0, 0, 0)` | value is zero; nothing is passed |
| `SetScreenSize` | `SendHidCommand(h, 0x76, param_2, 0, 0)` | argument passed straight through, no remap |
| `SetAudioVolume` | `SendHidCommand(h, 0x50, param_1, 0, 0)` | raw index, as the app sends it |
| `PanelColorAdjust` | `SendHidCommand(h, 0x73, param_1, &local_80, 3)` | the 3-byte payload the scene-mode frames rely on |
| `SetGyroBias` | `SendHidCommand(h, 0x3f, param_5, &local_38, 0xd)` | 13-byte payload |

This is still one artefact read two ways, so it shows the reading is not an
artefact of one tool -- not that the reading matches the glasses. The genuinely
independent checks are the hardware ones: the audio-tube ceiling of 12 against
the official app, the brightness drag feel, and 15 tube toggles producing 15
state transitions.

| Cmd  | Value byte | Payload | Native function | Meaning |
|------|-----------|---------|-----------------|---------|
| 0x00 | -         | -       | AcquireDeviceInfo (part 1) | read device info |
| 0x01 | -         | -       | OpenIMU | start IMU |
| 0x02 | -         | -       | CloseIMU | stop IMU |
| 0x06 | -         | -       | SwitchTo3D | switch to **3D** |
| 0x07 | -         | -       | SwitchTo2D | switch to **2D** |
| 0x09 | luminance index | - | PanelLunaSet | set **brightness** (stage) |
| 0x0D | -         | -       | PanelLunaSave | **save brightness** (commit) |
| 0x0E | -         | -       | PanelPowerOn | panel on |
| 0x0F | -         | -       | PanelPowerOff | panel off |
| 0x12 | -         | -       | PanelPowerSwap | swap panel power |
| 0x17 | distance  | -       | PanelSetDistance | set IPD / screen distance |
| 0x18 | 0/1       | -       | PanelSetHighDynamic | HDR on/off |
| 0x1A | hdrMode   | -       | PanelSetHDRMode | **画质动态** (SDR / AI-HDR) |
| 0x1B | 0/1       | -       | PanelSetColorEnhance | **色彩增强** toggle |
| 0x1D | -         | -       | ResetSettings | factory reset |
| 0x1F | -         | -       | SaveSettings | save settings |
| 0x20 | -         | -       | PanelFrameRateSet(60) | 60 Hz |
| 0x21 | -         | -       | PanelFrameRateSet(120) | 120 Hz |

`PanelFrameRateSet` takes 60 or 120 and picks the opcode from it, which is worth
knowing if you go looking for these two by scanning for immediates — they are not
there. `XRService::PanelFrameRateSet` @ `0x5a894`:

```
cmp   w8, #0x78        ; 0x78 == 120
mov   w8, #0x20
cinc  w1, w8, eq       ; w1 = 0x20, or 0x21 when the argument is 120
```

A scan for `mov w1, #imm` before each `SendHidCommand` classifies this as
indirect, correctly. It is also why 0x20 and 0x21 are the only two rows in the
table that a call-site scan does not turn up.
| 0x23 | -         | -       | AcquirePanelFov | read FOV |
| 0x25 | !arg      | -       | EnableAudioPersistence | mute persistence |
| 0x30 | -         | -       | SwitchSideBySide | toggle 2D/3D |
| 0x33 | -         | `1000` (u16 LE) | AcquireTraceReport | trace |
| 0x34 | a         | `[b]`    | EnableAudio | audio enable |
| 0x38 | !arg      | -       | EnablePSensorDetect | proximity |
| 0x3C | -         | -       | AcquireImuCalibration | IMU calib |
| 0x3E | start     | `[end-start+1]` | AcquireGyroBias | gyro bias |
| 0x3F | -         | 13 bytes | SetGyroBias | set gyro bias |
| 0x48 | 0/1       | -       | AudioSetTubeMode | **导音鳍** toggle |
| 0x49 | mode      | -       | SetAudioMode | **音效** (标准/轻语/空间环绕) |
| 0x50 | volume    | -       | SetAudioVolume | **音量** |
| 0x58 | b         | -       | DisableWheelKeySideBySide | wheel key |
| 0x66 | b         | -       | RebootAndBootloader | reboot |
| 0x73 | sub-op    | `[0, a, b]` | PanelColorAdjust | panel colour (see below) |
| 0x76 | screenSize| -       | SetScreenSize | **屏幕尺寸** |
| 0xE0 | -         | -       | AcquireDeviceFuncSupport | capability bitmap |
| 0xE3 | -         | -       | (second half of AcquireDeviceInfo) | device info |

### PanelColorAdjust (cmd 0x73)

Command frame is `66 73 <sub-op> 00 <a> <b>` (3-byte payload starting at
offset 3: byte3 = 0x00, byte4 = a, byte5 = b). The Java layer calls
`PanelColorParmsAdjust(op, a, b)`.

Sub-ops (`FxrConstant.PanelColorAdjustOp`):
```
opColorModePreview = 12   -> preview a colour mode
opSave            = 255   -> save colour params
opSave2           = 15    -> save colour params (2nd write)
opContrast        = 9
opColorTemperature= 10
opHue             = 11
opColorGainR/G/B  = 3/4/5
opColorEnhancementR/G/B = 6/7/8
opWhiteBalanceWx/Wy = 1/2
opLoad = 240, opLoad2 = 13, opReset = 254
```

Colour/scene modes (`FxrConstant.PanelColorModeOp`):
```
colorModeStandard    = 0
colorModeSplendid    = 1
colorModeSoft        = 2
colorModeEyeProtection = 3
```

To change the scene mode the app does:
`previewColorMode(m)` -> `PanelColorParmsAdjust(12, m, m)`
`saveColorMode(m)`    -> `PanelColorParmsAdjust(255, 1, m)` then `(15, 1, m)`

## Response dispatch (`XRService::HandleResponseMessage`)

The dispatcher switches on `resp[8]` (a 228-entry jump table). Notable:

| resp[8] | Action |
|---------|--------|
| 0x00 | `UpdateDeviceState(XrHidDeviceInfo)` -> onDeviceInfoResponseArrived |
| 0x06 | 3D mode active |
| 0x07 | 2D mode active |
| 0x09 | luminance changed (payload `resp+9`) |
| 0x17 | panel distance |
| 0x18 | panel high dynamic |
| 0x1A | HDR mode |
| 0x1B | panel colour enhance |
| 0x20 | frame rate = 60 (read-only in this plugin) |
| 0x21 | frame rate = 120 (read-only in this plugin) |
| 0x23 | FOV |
| 0x48 | audio tube mode |
| 0x49 | audio mode |
| 0x50 | volume (payload `resp+9`) |
| 0x73 | panel colour params |
| 0x76 | screen size |
| 0xE0 | `UpdateDeviceState(XrHidDeviceFuncSupport)` |
| 0xE3 | `UpdateDeviceState(XrHidDeviceStatusReport)` |

## Device info payload offsets

The `0x00` reply is decoded by `FUN_0019ba50` (`0x19ba50`), which copies each
byte into an `XrHidDeviceInfo` struct. Pairing the reply offsets with the struct
offsets from `XrHidDeviceInfo` gives the whole map. Offsets marked
**[measured]** were additionally confirmed by diffing the response across a
single setting change on real hardware.

```
reply  struct  field
0x15   0x000   deviceType (u8)          [measured]
0x18   ---     build date, NUL-terminated "Sep 22 2026"  [measured]
0x24   0x0A0   firmwareVersion (u16 LE) [measured]
0x26   0x0A4   glassesId char 1         [measured]
0x27   0x0A8   glassesId char 2         [measured]
0x28   0x0D0   frameRate (u8)           [measured]
0x29   0x0CE   luminance                [measured]  see below
0x2A   0x0CC   volume (u8)              [measured]
0x2B   0x0AE   display mode (non-zero => side-by-side / 3D)
0x2C   0x0B4   wakeup (0 => awake)
0x2D   0x0B0   audio mode (u8)          [measured]
0x2E   0x0AD   mute (non-zero => muted)
0x2F   0x0D1   panelDistance (u8)
0x30   0x0B5   sensor-valid flag
0x31   0x0B6   sensor-valid flag
0x32   0x0B7   sensor-valid flag
0x33   0x0B8   sensor-valid flag
0x3C   0x0B9   psensor flag (zero => present)
0x3D   0x0CD   maxVolume (u8)           [measured]  see below
0x3E   0x0E4   dp status (non-zero => dp)
---   0x0CF   maxLuminance = local table length, NOT a reply byte
```

`0x3C` was previously documented as "psensor state" derived from the struct
name; it is only ever compared against zero, so it is a flag and nothing more.

### Firmware identity is two fields, not one

`reply[0x24]` (uint16) is 26 on a GT Max. `reply[0x18..0x22]` is the NUL-
terminated string `Sep 22 2026`. The official app displays **20260922** — it
parses the string into `FXRDeviceInfo.TIME { year, month, day }` and formats it,
while `reply[0x24]` goes to `FXRDeviceInfo::firmwareVersion` and out through
`GlassInfo.firmwareVersion` without being shown.

So both are real and they are different fields. The panel shows `20260922 (26)`:
the build date leads, because that is what the app shows and what people
recognise, and the numeric revision is kept beside it because it is measured and
not invented. An unparseable date yields nothing rather than a guess — a
plausible-looking wrong date next to a real version number is worse than a blank.

### The two maxima are not symmetric

`maxVolume` is a real device-reported value: `struct[0xCD] = reply[0x3D]`. It is
a **count**, not an inclusive bound. A GT Max reports 16, giving indices 0..15,
where 0..12 is normal volume and 13..15 is the overdrive range. This matches the
official app's slider exactly (confirmed against the hardware).

`maxLuminance` is **not** reported by the device. The parser computes it from the
length of its own brightness table:

```c
struct[0xCE] = index of reply[0x29] within the brightness table;   // luminance
struct[0xCF] = (table_end - table_begin) >> 2;                    // maxLuminance
```

So `maxLuminance` is 29 for a GT Max simply because the table has 29 entries.
No device field states a ceiling.

What the glasses *do* limit is the value they report back at `reply[0x29]`.
Measured: that byte stops rising once brightness passes the low band (wire
values 1..9) or the low band plus one step (1..12), depending on the display
strategy mode (防抖 / 固定 / 随行), which the response does not report. Sending
a higher step is accepted but not reflected, which is what makes the official
app's slider appear to fall back on its own.

**The plugin fixes the slider at 12 steps** (`BRIGHTNESS_STEPS`), i.e. indices
0..11, which are exactly the table entries that map onto wire values 0..12 — the
band the glasses' own UI labels 1-12. Entries past index 11 (`4, 13..28`) belong
to a second, much brighter band this panel does not drive.

Deriving the ceiling at runtime was tried and dropped: watching for a requested
step that comes back lower works, but the display strategy mode can change at any
time, so a ceiling learned in one mode is wrong in the next and there is no way
to know it changed. A fixed bound is predictable and always reachable.

`maxLuminance` from the reply is deliberately **not** used as the bound: it is
the table length (29), which is larger than anything the panel renders.

`GetProp("manufacturer")` is also read from `XRConfiguration`, never from the
wire.

## Status report payload offsets (0xE3)

The panel settings live in a **second reply**, not in the 0x00 block. It is
decoded by a different function, `FUN_0019ca50` (`0x19ca50`), reached from
`XRService::UpdateDeviceState(XrHidDeviceStatusReport)` @ `0x192e30`. That is why
scene mode and picture quality looked unreadable at first: neither appears
anywhere in the 0x00 response, because `FUN_0019ba50` stops at `0xD1`
(panelDistance) and never touches the region they live in.

```
reply  field
0x0A   luminance index (u8)              [calibrated]
0x0B   maxVolume                         (a second source for 0x00's 0x3D)
0x0C   volume (u8)                       [calibrated]
0x0D   psensor state
0x0E   mute (non-zero => muted)
0x0F   display mode (non-zero => side-by-side / 3D)
0x10   audio mode (u8)                   [calibrated]
0x12   scene mode        通用/电影/护眼/阅读  [calibrated, both directions]
0x13   --                                [never identified; see below]
0x14   --                                [always 0 on a GT Max]
0x15   picture quality  0 = SDR, 1 = AI-HDR [calibrated, one direction]
0x16   --                                [always 0 on a GT Max]
0x17   colour enhance                     [calibrated, both directions]
0x18   audio tube                         [calibrated]
0x1A   screen size    0 = 115%, 1 = 100%, 2 = 85%   [calibrated]
```

`0x13` is read but **never published**. Nine snapshots spanning all four scene
modes, plus a run with AI-HDR on and off, all report the same value (2). Nothing
this plugin does writes `0x18` (`PanelSetHighDynamicMode`) outside
`calibrate_status`, where it is only a probe, so there is nothing left to
correlate it against. It stays in the dump, labelled as unidentified, rather
than being shown as a setting: a value that has never been observed to move is
not one the panel has any business presenting.

`0x04..0x06` are **not settings**. `0x04` is a checksum over the report and
`0x05..0x06` a counter that climbs between reads — observed directly, `0x05` went
197 → 207 → 216 → 226 → 238 → 249 → 5 → 28 across one calibration with no
setting change in between. Any diff includes them unless they are masked out.
Frame rate is not here: `struct[0xD0]` is written by the 0x00 parser from
`reply[0x28]`, and a `120` seen at 0x11 is a coincidence.

`0x13` (high dynamic) is the one setting still unmapped, and the earlier note
here -- "every probe for it hit an endpoint halt, so no conclusion either way" --
was too pessimistic about what could be learned. It can be read; it has simply
never moved. Nine snapshots across all four scene modes, plus a run with AI-HDR
on and off, all report 2. Nothing this plugin does writes `0x18`
(`PanelSetHighDynamicMode`) outside `calibrate_status`, where it is only a probe,
so there is nothing left to correlate against.

It was published in the state until it was removed: a value that has never been
observed to move is not one the panel has any business presenting. `0x18` itself
is a real command the app sends, so the capability stays available for
calibration -- it is only the *display* that went.

### The firmware's field names are off by one across this group, twice

`FUN_0019ca50` copies a `uint16` from `0x16` into `struct[0xD5]` and the next
byte into `struct[0xD6]`, and pairs `0x13/0x14/0x15` with `panelHighDynamic` /
`panelHDRMode` / `panelHDREnabled`. The measurements put **picture quality at
`0x15`, not `0x14`**, and **colour enhance at `0x17`, not `0x16`**. Four of the
firmware's six attributions were wrong, and it happened to be right about the two
that had been measured directly first.

Reading the pairing as fact is what broke colour enhance and, through it, the
volume ceiling, which is derived from the audio tube.

Static analysis cannot settle this class of question. It says which struct field
a byte was *inferred* to feed; only changing one setting on the device and
watching one byte move says what it actually is.

### How the calibration works, and why it is written that way

`calibrate_status()` writes **every option** of every setting in turn and diffs
the status report against a baseline re-read immediately before each write.

- Every option, not one that differs from the current value: the current value
  is itself read from the offset under test, so using it to pick a different
  target assumes the answer.
- A fresh baseline per write, and `0x04..0x06` masked: the first run reported
  those three bytes as moving for every option of every setting, purely because
  time had passed.
- Bare offsets, never the assumed field name — printing it would just restate
  the assumption being tested.
- Retried once through a recovered endpoint: the second run lost five of
  eighteen probes to halts that recovery then cleared, so some settings got an
  answer and others did not depending only on where the halt happened to land.
- Every setting restored afterwards, and an unknown baseline reported rather
  than hidden, because this routine changes what the user sees.
- Hardest first: the panel commands are also the ones that wedge the endpoint.

## Commands the device never acknowledges

Tallying every send in the TX trace against whether a frame came back:

```
cmd    replied   silent   meaning
0x09       0        23   brightness
0x1A       0        18   picture quality
0x73       0        16   panel colour params (scene mode)
0x18       0         3   high dynamic
0x48      13         0   audio tube
0x50      11         0   volume
0x76       9         0   screen size
0xE0      14         0   capabilities
0x1B      35         2   colour enhance
```

Waiting for a reply to one of the silent four costs the full timeout and — far
worse — **three unanswered writes in a row leave the interrupt endpoint halted**,
after which every later transfer fails at once. That is the same limit the
earlier notes recorded as "four transactions in quick succession"; the trace
sharpens it to three consecutive writes of a silent command.

Two consequences, both now fixed:

1. **Scene mode's save frames were never sent.** The code read the preview's
   missing reply as failure and skipped the two save frames, so the change was
   previewed on the panel and then never persisted. All three frames now go out
   unconditionally, none expecting a reply.
2. **Scene mode's frames used to be padded 150 ms apart.** Three unspaced writes
   were found to leave the endpoint halted, and the obvious fix was to space
   them — which cost 300 ms on every scene change. But the official app does not
   space them:

   ```java
   public void saveColorMode(int i) {
       this.mFXRServer.PanelColorParmsAdjust(255, (byte) 1, b);
       this.mFXRServer.PanelColorParmsAdjust(15, (byte) 1, b);
   }
   ```

   Two JNI calls with nothing between them, and no `sleep` anywhere in the
   colour path — the only ones in the SDK are in `Dfu.java`, for firmware
   updates. More to the point the app never sends a preview and a save together
   at all: `GlassDeviceManager` dispatches `previewColorMode` on one Flutter
   callback (case 32) and `saveColorMode` on another (case 33), so they are two
   separate user actions. The burst this plugin invented is a flow the app never
   exercises.

   The padding is gone. What actually covers the endpoint is `_write_panel`: a
   failed write clears the halt and retries once, costing 50 ms *only when
   something went wrong* instead of 300 ms on every change. If the panel does
   turn out to need the frames separated, `_confirm_scene_mode` will say so — it
   logs a WARN whenever the device reports a different scene than was asked for.

Because there is no reply to synchronise on, the status report is the only
evidence a change landed, and how long the panel takes is not published anywhere.
`_settle_then_read` therefore probes on a backoff — 50 ms, then 100, 200, 350,
500 — and stops as soon as the device reports what was asked for. A flat 250 ms
first probe was not enough for a scene change *and* was pure dead time for the
writes that land in under a millisecond.

Measured on the GT Max, from the panel's own trace (`[ui] apply:<key>` lines):

| control | backend round trip |
|---------|--------------------|
| volume | 5–7 ms |
| brightness | 6–12 ms |
| colour enhance | 50 ms |
| picture quality | 73–257 ms |
| scene mode | 101–132 ms |
| audio mode | **~600 ms** |

Audio mode is the outlier, and consistently so. Nothing in the read path is
wrong there — the device really does take several hundred milliseconds to report
a new sound mode, so the probe backoff runs several rounds before the value
matches. The dropdown still moves instantly; only the confirmation is slow.

### A missed poll is not a disconnect

The poll's 0x00 query is given 250 ms, and it lands right after whatever the
user last changed. A silent panel command leaves the device busy, so that query
can time out even though nothing is wrong — and one timeout used to be reported
as `glasses stopped answering`, greying the panel out for a moment before the
next poll succeeded. The trace showed it happening after brightness changes that
had themselves taken 261 ms instead of the usual 6.

It now takes three consecutive misses (4.5 s) to declare the glasses gone, and
the poll retries an unanswered query once before giving up on it at all. A real
disconnect answers nothing, so the only thing the wait costs is the time to
notice a genuine unplug.

The two attempts are deliberately unequal: **60 ms first, 200 ms on the retry**.
The glasses answer this query in 1–2 ms when healthy, so a long first attempt
buys nothing — but the poll holds `_lock` for its entire window and every write
queues behind it. At 250 ms that turned **43 of 66 brightness changes in one
session into ~261 ms**, against 1–18 ms for the rest: the setting worked, it just
felt broken. A panel that is busy after a silent command gets the longer second
attempt instead, where waiting costs nothing.

Declaring them gone also **releases the USB handle**. `open()` is idempotent —
it hands back whatever handle it already holds and never re-enumerates — so
leaving a stale one in place made the next Connect spend its only attempt on a
device that was no longer there. The first press failed and only the second
worked, which reads as a dead button rather than a stale handle. The state is
read *before* the close, since `get_device_info()` returns an empty
`DeviceInfo` once the handle is gone.

`connect()` retries once through a close for the same reason, so one press
succeeds whatever state the handle happens to be in.

## Capability bitmap (command 0xE0)

The 0xE0 response carries 24 capability bools. Two independent Ghidra passes pin
down both halves of the mapping, and they agree:

**a) flag index -> response byte**, from the SIMD unpack in
`XRService::UpdateDeviceState(XrHidDeviceFuncSupport)` @ `0x9cc54`:

```
ldur s0,   [x9,#0x9]    -> flags 0..3    resp[0x09..0x0c]
ld1  v0.b[4], [x9,#0xd]  -> flag  4        resp[0x0d]
ld1  v0.b[5], [x9,#0x16] -> flag  5        resp[0x16]
ld1  v0.b[6], [x9,#0x19] -> flag  6        resp[0x19]
ld1  v1.b[4], [x9,#0x14] -> flags 12,13    resp[0x13], resp[0x14]
ld1  v0.b[7], [x9,#0xe]  -> flag  7        resp[0x0e]
ld1  v0.s[2], [x9,#0xf]  -> flags 8..11    resp[0x0f..0x12]
ld1  v0.b[0xe],[x9,#0x15]-> flag  14       resp[0x15]
ld1  v0.b[0xf],[x9,#0x18]-> flag  15       resp[0x18]
ldur d0,   [x9,#0x1a]    -> flags 16..23   resp[0x1a..0x21]
```

**b) flag index -> field name**, from
`JNIFormatHelper::C2Java::ConvertXRDeviceInfo` @ `0x155ac4`. Each field builds
its name string then reads its byte immediately before `SetBooleanField`:

```
adrp x9, <page>        ; "isSupportXxx" lives in .rodata
add  x9, x9, #<disp>
...                    ; build std::string on the stack
bl  <field lookup>
ldr x8, [x19]
mov x2, x0
ldrb w3, [x21, #<struct_off]>   ; <-- this field's value
blr x8                          ; SetBooleanField(name, value)
```

The 24 struct offsets run `0x04..0x1b` with **no gaps**, so ascending struct
offset is ascending flag index. `FxrApi.java`'s `Support*()` methods name 22 of
the 24 fields and all 22 agree.

| flag | resp byte | SDK field | plugin key | UI control |
|-----:|----------:|-----------|------------|------------|
| 0  | 0x09 | `isSupportFovGet`               | `fovGet`          | – |
| 1  | 0x0A | `isSupportSideBySide`           | `sideBySide`      | 显示模式 2D/3D |
| 2  | 0x0B | `isSupportFps120`               | `fps120`          | **not exposed** |
| 3  | 0x0C | `isSupportLumChange`            | `lumChange`       | 屏幕亮度 |
| 4  | 0x0D | `isSupportAccumaster`           | `colorAdjust`     | – |
| 5  | 0x16 | `isSupportAccumasterModeChange` | `colorModeChange` | 场景模式 |
| 6  | 0x19 | `isSupportAccumasterModeEye`    | `colorModeEye`    | 护眼场景 |
| 7  | 0x0E | `isSupportAudioVolumeChange`    | `audioVolume`     | 音量 |
| 8  | 0x0F | `isSupportAudioQuietMode`       | `audioQuietMode`  | 音频模式 |
| 9  | 0x10 | `isSupportAudioDisable`         | `audioDisable`    | 静音 |
| 10 | 0x11 | `isSupportGsensorRAW`           | `gsensorRaw`      | – |
| 11 | 0x12 | `isSupportGsensorAngelCorrect`  | `gsensorCorrect`  | – |
| 12 | 0x13 | `isSupportGryoTempBias`         | `gyroTempBias`    | – |
| 13 | 0x14 | `isSupportTimeSyncMsg`          | `timeSync`        | – |
| 14 | 0x15 | `isSupportGetOrbit`             | `getOrbit`        | – |
| 15 | 0x18 | `isSupportUserOrbit`            | `userOrbit`       | – |
| 16 | 0x1A | `isSupportPanelDistanceAdjust`  | `panelDistance`   | – |
| 17 | 0x1B | `isSupportPanelHighDynamic`     | `panelHighDynamic`| – |
| 18 | 0x1C | `isSupportAudioSpatialMode`     | `audioSpatial`    | 环绕 |
| 19 | 0x1D | `isSupportAudioTubeMode`        | `audioTubeMode`   | 导音鳍 |
| 20 | 0x1E | `isSupportPanelHDR`             | `panelHDR`        | 画质动态 |
| 21 | 0x1F | `isSupportPanelColorEnhance`    | `colorEnhance`    | 色彩增强 |
| 22 | 0x20 | `isSupportPanelDolbyLLDV`       | `panelDolby`      | – |
| 23 | 0x21 | `isSupportPanelScreenSize`      | `panelScreenSize` | 屏幕尺寸 |

This unpack only runs for `deviceType >= 0x30`, which covers every Gemini/Taurus
unit (GT Max reports 64/65).

### Why 120 Hz is not exposed

Commands `0x20` / `0x21` exist (`PanelFrameRateSet`: `cmp w8,#0x78` +
`cinc w1,w8,eq` → 60 fps sends `0x20`, 120 fps sends `0x21`), and the plugin
reads `resp[0x28]` to report the current rate but has no code path that writes
it.

**The reason is the app's behaviour on this model, not the protocol.** Checked on
a GT Max with the official app connected: there is no refresh-rate setting. The
glasses themselves say they could — the capability bitmap reports
`isSupportFps120 = yes` and `FxrApi.Support120fps()` is `deviceType == 36 || that
flag`, which is true for deviceType 65. So RayNeo declines to offer it here, and
the plugin follows the app rather than inventing a control it does not have.

**A note on the i18n.** `strings_zh.json` does contain `refreshRate = 刷新率` and
two related tips:

> `refreshRate3dTips` = "3D 模式下不支持 120Hz 刷新率"
> `refresh_rate_locked_at_60Hz_in_3D_mode` = "3D 模式下刷新率锁定为 60Hz"

That file ships to **every** RayNeo model, so it is evidence about the models that
have the control, not about this one. Reading those strings as proof that the GT
Max has a refresh-rate setting was a mistake made here once, and it is worth
stating plainly because the file looks like a feature list and is not one: it is
the union across the family.

If the setting is ever wanted, it would need the 3D lockout those strings
describe — 3D mode pins the rate to 60 Hz.

## Device type codes

```
32  DEVICE_NEXTVIEWPRO
33  DEVICE_ARIES
34  DEVICE_ARIES1p5_SEEYA     (also SONY)
35  DEVICE_ARIES1p5_SONY
36  DEVICE_ARIES1p8
48  DEVICE_TAURUS
49  DEVICE_TAURUS1p5
53  DEVICE_TAURUS2p0
54  DEVICE_TAURUS3p0
55  DEVICE_TAURUS3p0PRO
56  DEVICE_TAURUS2p0PRO
57  DEVICE_TAURUS4p0
58  DEVICE_TAURUS4p0PRO
64  DEVICE_GEMINI1p0
65  DEVICE_GEMINI2p0
```

`RayNeo GT Max` is a Gemini-family device (`Config.PACKAGE_NAME_GLASSES_ROM_GEMINI*`
and i18n keys `gemini1p0_*` / `gemini2p0_*`), so deviceType is 64 or 65.

## Brightness lookup table

`XRService::PanelLunaSet(index)` does **not** transmit the raw index. It sends
`table[index]`, and `0xFF` when the index is out of range:

```
5a5dc  ldrb w9, [sp, #0xc]        ; index
5a5e4  cmp  w9, x10, asr #2       ; index < table length?
5a5e8  b.hs -> send 0xFF
5a5ec  ldr  w2, [x8, x9, lsl #2]  ; value = table[index]   <-- lookup
5a5f4  mov  w1, #9                ; command 0x09
```

The table is one of four arrays compiled into `libFFalconXRServer.so`,
selected by `LuminanceUtils::Init(name, deviceType, maxBrightness)` and then
truncated by `cutBrightnessArray(table, maxBrightness)`.

Selection logic (recovered from `LuminanceUtils::Init` @ `0x9c410`):

```
manufacturer == "samsung":
    deviceType in [33, 36] -> ARIES_SAMSUNG  (7 entries)
    deviceType == 32       -> NXTVIEW         (5 entries)
    otherwise              -> SAMSUNG_OTHER   (4 entries)
otherwise:
    deviceType == 32       -> NXTVIEW         (5 entries)
    otherwise              -> GENERAL         (29 entries)
```

Extracted arrays:

```
GENERAL (29)  : 1 2 0 5 6 7 3 8 9 10 11 12 4 13 14 15 16 17 18 19 20 21
                22 23 24 25 26 27 28
NXTVIEW (5)   : 1 2 0 3 4
ARIES_SAMSUNG : 1 2 0 5 6 7 3
SAMSUNG_OTHER : 1 2 0 3
```

`RayNeo GT Max` reports `deviceType` 64/65 (Gemini) with a non-Samsung panel, so
it uses **GENERAL** -- the 29-step table. The first few entries are special-cased
low-brightness levels (0, 5, 6, 7, 3), index 12 maps to 4, and 13..28 are linear.

Read back out of `.rodata` at `0x00135690` as uint32 (29 entries), confirmed
against the live device:

```
1 2 0 5 6 7 3 8 9 10 11 12 4 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28
```

Indices 0..11 are a bijection onto wire values 0..12:

| index | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| wire  | 1 | 2 | 0 | 5 | 6 | 7 | 3 | 8 | 9 | 10 | 11 | 12 |

That range is the origin of the two brightness bands reported on the glasses'
own UI (1-9 and 1-12); the plugin exposes the whole 0..12 band as 12 steps.

The table is not monotonic (index 2 gives 0, index 6 gives 3, index 12 gives 4),
so `reply[0x29]` must be *searched*, not used as an index -- which is exactly what
the firmware does to derive its `luminance` field (see above).

Note the panel `manufacturer` string is read by the SDK from its own config
(`XRConfiguration::GetProp("manufacturer", ...)`), **not** from the USB response,
so it cannot be recovered over the wire. Devices that are neither Samsung nor
`deviceType == 32` all resolve to GENERAL, which covers every Gemini/Taurus unit.

## Volume ceiling

`maxVolume` comes from `reply[0x3D]`. A GT Max reports 16.

**Whether 16 is a count or an inclusive index is an inference, not a
measurement.** The official app settles nothing here: `getVolumeMax()` returns
`GetAudioMaxVolume()` raw, `setVolumeIndex()` sends the index unchecked, and
`getVolumeMax()` has **no caller anywhere in the decompiled app** — the slider's
ceiling lives in the Flutter layer, which is not readable from here.

What *is* measured: the device reports its own current volume as 15, so index 15
exists. **Index 16 has never been sent**, so it is untested rather than
known-bad. The plugin treats 16 as a count, giving a ceiling of 15.

An earlier version of this note claimed the app "splits this into 0..12 (normal
volume) and 13..15 (overdrive gain), which matches the hardware". That 12 is
this plugin's own `AUDIO_TUBE_VOLUME_LIMIT`, so it was being used to support the
claim it was derived from. Withdrawn.

### What locks a control out, and which source settles it

Three restrictions, from two different sources -- and the one that comes from
hardware is the one the status report gets wrong:

| control | locked by | source |
|---|---|---|
| colour enhance | AI-HDR | app i18n: `colorEnhanceDisableTips = AI-HDR模式下不支持启用色彩增强` |
| colour enhance | **阅读** | **hardware only** -- see below |
| screen size | 阅读 | app i18n: `screen_size_not_supported_in_scene_office_mode` |
| screen size | 3D | app i18n: `screen_size_not_supported_in_3D_mode` |

**In 阅读, `0x17` reads back as 1 and the panel does not apply it.** The toggle
cannot be turned on there, confirmed by hand, and the status byte reporting
success is the read-back lying rather than the setting taking. This one was
nearly removed from the panel: the byte said 1, nothing in the app's i18n
mentions colour enhancement and a scene mode, and a wire trace showed us sending
`0x1B` exactly once all session -- all of which pointed at "阅读 does not block
this". The hardware disagreed with every one of those, which is the standing
lesson of this document: a status report is evidence about a setting, never
authority over it.

**护眼 is not a lock.** The device clears `0x17` by itself when you switch into
it -- observed with no `0x1B` write from this plugin -- but it accepts the value
straight back, so gating the control would remove one that works.

With the audio tube on, the overdrive range is unusable. Where that limit lives,
measured rather than assumed:

| layer | does it limit? | how it was established |
|---|---|---|
| firmware | **no** | `reply[0x3D]` stays 16 with the tube on, across four sessions; indices past the limit are accepted without complaint |
| JNI / `AirApi` | **no** | `setVolumeIndex(i)` calls `SetAudioVolume(i)` unchecked; `getVolumeMax()` returns the device value raw |
| Flutter UI | **yes** | `strings_zh.json` carries `audio_tube_opened_volume_cannot_increased_tip` = "导音鳍开启后，无法继续增益音量" |

So the official app enforces it in its Flutter layer, as a *refusal with a
message* rather than a shortened slider — the key is a tip, and the wording is
about not being able to increase further, which is the overdrive range.

**12 is confirmed against the official app** — turn the tube on there and its
volume slider tops out at the same step. It was originally this plugin's own
guess, carried for a while with no measurement behind it, and the note that
supported it was circular: it cited "0..12 is normal volume" as though the app
had said so, when the 12 came from this plugin's own `AUDIO_TUBE_VOLUME_LIMIT`.
The number survived that; the reasoning for it did not.

### Why the ceiling moved on its own when toggling the tube

The 0x48 reply is matched by command id, and this protocol carries **no sequence
number**. When a write's own reply times out, that reply arrives late and is
consumed by the *next* write, which then reports the value before last:

```
19:54:56,363  TX 0x48  [66 48 01]  -> no reply  (300 ms)   timed out
19:54:56,525  TX 0x48  [66 48 00]  -> ...4801 0000         wrote 00, reply says 01
```

Over one session of rapid toggling: **26 writes, 5 of them timing out, and 34
state transitions instead of 26.** Every extra transition moved the volume
ceiling between 12 and 15, which is the "the maximum changes irregularly when I
toggle a few times" report.

So the 0x48 echo no longer drives `audio_tube` at all. `set_audio_tube` sets the
value optimistically, so the panel still moves at once, and the 0xE3 status
report is the authority — it reports the device's state as of the moment it
answers rather than as of an earlier request, so a late reply cannot poison it.

One consequence worth stating: `volumeLimit` is an index and `maxVolume` is a
count, so the panel must never fall back to the latter. It used to, silently,
wherever `volumeLimit` was null -- first paint and after a disconnect -- which
produced a ceiling that was right most of the time and wrong occasionally.

## Value byte semantics from the Java wrapper (`AirApi`)

- `setBrightnessIndex(i)`: guards `0 <= i < GetMaxLuminance()`, sends `PanelLuminanceSet(i)`.
- `saveBrightness()`: `PanelLuminanceSave()` → **`SendHidCommand(handle, 0x0D, 0, 0, 0)`**, no payload.

### Brightness is the only setting the app splits in two

`GlassDeviceManager.flutterLLEventCallback` dispatches the two halves on
different Flutter callbacks. The enum names them (`NativeDataEventType`), and
**brightness is the only setting with a separate save event**:

| case | event | call | frame |
|---|---|---|---|
| 4 | `FSetBrightnessIndex` (1002005) | `setBrightnessIndex(i)` | `66 09 <table[i]>` |
| 5 | `FSaveBrightness` (1002006) | `saveBrightness()` | `66 0D 00` |
| 6 | `FSetVolumeIndex` (1002007) | `setVolumeIndex(i)` | `66 50 <i>` — *no save event* |

`XRService::PanelLunaSave` @ `0x15a7a4` in `libFFalconXRServer.so`
(Ghidra image base `0x100000`):

```
ldrb w8, [x0, #0x1c9]      ; present?
cbz  w8, ...
ldr  x0, [x0, #0x130]      ; USB handle
mov  w1, #0xd               ; command 0x0D
mov  w2, wzr               ; value 0
mov  x3, xzr
mov  x4, xzr
b    SendHidCommand
```

The two stubs immediately after it are the same shape sending `0x0E`
(`PanelPowerOn`) and `0x0F` (`PanelPowerOff`).

### 0x09 applies; 0x0D persists. It is not a preview/commit pair for the panel

An earlier note here read the pair as "stage, then commit", and credited `0x0D`
with making the panel apply promptly. **The disassembly does not support that.**
The two functions are not symmetrical:

| | instructions | calls `UpdateDeviceState` |
|---|---|---|
| `PanelLunaSet` | 100+, with the table lookup and out-of-range fallback | **yes**, on success |
| `PanelLunaSave` | 12: presence check, handle, `SendHidCommand(0x0D,0,0,0)` | **no** |

`PanelLunaSet` updates the app's own displayed state itself, so the app treats
the Set as the point at which brightness has applied. `PanelLunaSave` sends one
fire-and-forget frame and looks at nothing that comes back — it is a persistence
nudge, in the same family as `saveColorMode` and `SaveSettings` (`0x1F`).

That matters for what to expect from it: it is **not** the reason a drag feels
immediate. Measured, brightness runs at a ~6-11 ms median round trip with the
same behaviour whether the commit is on the critical path or not, and the
staging/coalescing changes are what fixed the lag.

### The commit's read-back must not warn about the brightness band

The two are easy to conflate, and were: the commit reads the panel back, sees a
lower step than it asked for, and reports a fault. But a disagreement there is
**the panel's documented behaviour**, not a defect -- it stops *reflecting*
brightness past the current display strategy mode's band, so a request above the
band is accepted and reported back lower. That is the mechanism behind "the
official app's slider falls back on its own" described above.

Measured cost of getting that wrong: six such warnings in one session, twelve in
another, every one of them `asked index 11, panel reports 8` or `asked 9, reports
8`. Normal operation reading as a fault.

The read-back stays. The published state carries the panel's step, so the thumb
moves to where the hardware is; only the warning is gone. Scene mode, audio mode
and picture quality keep theirs -- for those, a disagreement really is the device
refusing a change.

**Persistence is unproven, and one measurement argues against it.** Every
session in the logs starts with the device reporting luminance index 8, including
sessions that followed one where the user had left brightness at 10 or 11 and
`0x0D` had been sent and reported as committed. So on this hardware the value has
not been observed to survive a session boundary. The command is kept because the
app sends it and the intent is clear; it is not kept because it has been shown to
do anything.

`0x0D` is deliberately *not* in `UNACKNOWLEDGED_COMMANDS`: it had never been
sent, so no reply rate had been observed, and that table is guarded against the
TX trace.

### What actually wedges the interrupt endpoint

A silent command is never answered, and the endpoint halts after three
unanswered writes. Recovering on failure is too late: the write that trips the
halt still succeeds, and it is the **next** transfer that fails, after sitting
through `_bulk_write`'s 200 ms timeout. That cost lands on whatever the user
does next, which is how a brightness drag ends up stuttering.

Measured over one session, every `-7` was accounted for by a silent command and
none by an answered one:

| command | replies? | writes | `-7` after it |
|---|---|---|---|
| `0x50` volume | yes | 85, at 20/s | **0** |
| `0x09` brightness | no | 34 | 18 |
| `0x0D` commit | no | 18 | 14 |

So `_write_panel` clears the halt *after* every silent write, not only after a
failure: two control transfers, about a millisecond, and the count of
unanswered writes never reaches three.

### How the panel drives the two halves

Stage on every tick, commit once the gesture settles, then read the panel back:

| step | when | frames | source |
|---|---|---|---|
| stage | every `onChange` | `66 09 <table[i]>` | app case 4 |
| commit | 500 ms after the last tick | `66 0D 00` | app case 5 |
| read-back | after the commit | `0xE3` | the panel's own answer |

The stage used to be debounced at 220 ms, which collapsed each drag into a
single write landing after the thumb had already stopped — exactly when the
panel felt like it was lagging. Volume has no commit stage, so removing its
debounce was the whole of that change.

The commit's read-back is reported in the RPC reply rather than only on the
state event, because the slider's settle window is still open when it lands and
incoming state is ignored for the duration of a drag. If the panel refused the
step, the reply carries *its* value and the slider goes back there.

**This replaces an earlier conclusion.** `save_brightness` existed as an unused
command behind `send_raw`, written off as doing nothing on the evidence that
"brightness reads back correctly straight after a plain brightness write". A
status report reports what was *staged*, so that observation could not
distinguish staged from committed and said nothing about the panel. The user
reporting that the panel caught up late is what settled it.

`PanelLunaSet` calls `UpdateDeviceState` itself once the write returns, so the
app's own slider reflects the change without waiting for a read-back — this
plugin publishing the requested step immediately matches it rather than
diverging from it.

### Every other setting re-checked against the app: single call, nothing missing

Once the image base was pinned down (`0x100000`), all 36 `SendHidCommand` call
sites in `XRService` were recovered by scanning for `b`/`bl` to the stub at
`0xbbc70` and reading back the `w1` immediate:

```
0x01 0x02 0x06 0x07 0x09 0x0D 0x0E 0x0F 0x12 0x17 0x18 0x1A 0x1B 0x1D 0x1E
0x1F 0x23 0x25 0x30 0x33 0x34 0x38 0x3C 0x3E 0x3F 0x49 0x50 0x58 0x66 0x73
0x76 0xE0
```

Cross-checked against every `AirApi` method, **brightness is the only setting
the app splits into a stage and a commit.** Everything else is one JNI call:

| setting | app call | frames we send |
|---|---|---|
| 2D / 3D | `SwitchTo2DMode()` / `SwitchTo3DMode()` | `0x07` / `0x06` |
| volume | `SetAudioVolume(i)` | `0x50` |
| audio mode | `SetAudioMode(i)` | `0x49` |
| whisper | `SetAudioMode(0|1)` | same opcode as audio mode |
| panel distance | `PanelSetDistance(i)` | not exposed |
| high dynamic (HDR) | `PanelSetHighDynamicMode(0|1)` | not exposed |
| picture quality | `PanelSetHDRMode(i)` | `0x1A` |
| colour enhance | `PanelSetColorEnhance(0|1)` | `0x1B` |
| audio tube | `AudioSetTubeMode(0|1)` | `0x48` |
| screen size | `SetScreenSize(i)` | `0x76` |
| frame rate | `PanelFrameRateSet(60\|120)` | not exposed |
| **brightness** | `PanelLuminanceSet` + **`PanelLuminanceSave`** | `0x09` + `0x0D` |
| colour params | `PanelColorParmsAdjust(6..11)` + `colorSave` | not exposed |
| colour mode | `previewColorMode(12)` + `saveColorMode(255, 15)` | `0x73` ×3 |

Two things that look like a second call but are not: `setFps120` is an
if/else on a single `PanelFrameRateSet`, and `SaveSettings` (`0x1F`) has a
native declaration but **no caller anywhere in the app**, so not sending it
matches the app.

So the remaining differences from the app are missing *features*, not missing
halves: colour contrast/temperature/hue/RGB (`0x73` subOps 6–11), HDR
high-dynamic (`0x18`), panel distance (`0x17`), frame rate (`0x20`/`0x21`),
and SBS. None of them is a defect in what we do expose.

> `0x66` is both the frame magic (`CMD_MAGIC`) and `RebootAndBootloader`
> (`CMD_REBOOT_BOOTLOADER`). Not a duplicate typo — the magic is `frame[0]` and
> the command is `frame[1]`. Same for the two names on `0xE3`.
- `setVolumeIndex(i)`: `SetAudioVolume(i)`.
- `setWhisperMode(on)`: `SetAudioMode(1)` if on else `SetAudioMode(0)`.
- `setAudioMode(i)`: `SetAudioMode(i)` (0 = standard, 1 = whisper, ...).
- `setHDR(on)`: `PanelSetHighDynamic(1|0)`.
- `setPanelHDRMode(i)`: `PanelSetHDRMode(i)`.
- `setPanelColorEnhance(on)`: `PanelSetColorEnhance(1|0)`.
- `setAudioTubeMode(on)`: `AudioSetTubeMode(1|0)`.
- `setScreenSize(i)`: Java divides by 10 (`(i/10)%10`) before `SetScreenSize`.

  That is the tens digit of whatever the Flutter layer passes, which is not
  recoverable from the Java layer. `XRService::SetScreenSize` @ `0x5b3dc` moves
  its argument straight into the value byte of `0x76` with no lookup or clamp,
  so the byte is decided entirely by the caller.

  The table above records `0 = 115%, 1 = 100%, 2 = 85%` as **hardware
  calibrated**, and that is the stronger evidence: the arithmetic only yields
  0/1/2 for those labels if Flutter sends `100/110/120`, and 0/1/8 if it sends
  `100/115/85`. Rather than guess which, an unrecognised value in a status
  report is now named in the log instead of being dropped — dropping it was the
  one way this field could show a size the glasses were not set to, with nothing
  else reporting a problem.
- 3D/2D: `SwitchTo3D()` / `SwitchTo2D()` (cmd 0x06 / 0x07).

## Verification helpers

`/tmp/opencode/apk/tools/` contains the RE toolkit used (ELF reader, aarch64
disassembler with PLT annotation, jump-table decoder, call-argument extractor).