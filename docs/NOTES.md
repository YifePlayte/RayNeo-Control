# Engineering notes

Why this plugin is built the way it is. Most of it exists because a simpler
version was tried first and the measurements said no.

The protocol itself is documented separately, in [`PROTOCOL.md`](PROTOCOL.md).
This file is about behaviour: latency, failure modes, and the choices that came
out of them.

## Contents

- [Polling, not push](#polling-not-push)
- [The poll never stops for good](#the-poll-never-stops-for-good)
- [Both sliders write on every tick](#both-sliders-write-on-every-tick)
- [Per-tick writes have to be coalesced](#per-tick-writes-have-to-be-coalesced)
- [What wedges the USB endpoint](#what-wedges-the-usb-endpoint)
- [Most of a change's latency is not ours](#most-of-a-changes-latency-is-not-ours)
- [Progress is reported, never assumed](#progress-is-reported-never-assumed)
- [Scene mode and picture quality](#scene-mode-and-picture-quality)
- [Log format](#log-format)

## Polling, not push

The USB IN endpoint is read **only while a reply is being awaited**
(`RayNeoDevice._await_response`), so nothing is read while the plugin is idle. A
setting changed on the glasses' own side — the phone app, a hardware button — is
therefore picked up at the next poll, up to `POLL_INTERVAL` (1.5 s) later.

This was a deliberate choice, not a gap. The interrupt endpoint is fragile on
this hardware: four USB transactions in quick succession are enough to time it
out, after which every later transfer fails instantly until the endpoint's halt
is cleared. A permanent background reader would add exactly that kind of load.
1.5 s is well under the threshold at which a slider feels unresponsive.

The poll's two attempts are deliberately patient — 250 ms each. They were briefly
shorter, on the theory that the poll was holding the device lock and delaying the
user's next write. That theory was wrong (see
[Most of a change's latency is not ours](#most-of-a-changes-latency-is-not-ours)),
and the short version cost a false disconnect: a wedged endpoint made one poll
miss, the shortened query made the next two miss as well, and three strikes
declared the glasses gone while they were sitting there answering writes.

## The poll never stops for good

Three unanswered polls in a row declare the glasses gone and release the handle,
because a dead handle makes the next Connect spend its only attempt on a device
that is no longer there.

After that, the loop used to `continue` past every cycle, so nothing polled
again: the panel sat on a frozen state and *every control stopped reflecting
reality* until the user noticed and pressed Connect. One wrong "gone" turned into
a plugin that looked dead with the glasses plugged in.

It now keeps looking for them — one USB enumeration per interval, only while
disconnected — and logs

```
glasses answered again, resuming live updates
```

when they turn up. Replugging needs no button press.

**A disconnect the user asked for is left alone.** The same self-healing is
exactly wrong after an explicit Disconnect: the panel closed the device and the
poll put it straight back within one interval, so the button appeared not to
work. Absence the user asked for and absence that happened to us are different
things, and only the second is worth healing.

## Both sliders write on every tick

This is the official app's shape. `GlassDeviceManager` dispatches
`setBrightnessIndex` (`0x09`, stages the step) on every UI event and
`saveBrightness` (`0x0D`, commits it) once at the end, so it stages while you
drag and commits when you let go. Volume is a single `SetAudioVolume` with no
commit stage, so for it sending per tick is the whole change.

Both used to be debounced at 220 ms, which collapsed each drag into one write
landing after the thumb had already stopped — precisely when the panel felt like
it was lagging.

### 0x0D is a persistence nudge, not the thing that applies brightness

The pair reads like stage-then-commit, and it was documented that way for a
while. The disassembly does not support it: `PanelLunaSet` calls
`UpdateDeviceState` itself on success, so the app treats the Set as the point at
which brightness has landed, while `PanelLunaSave` sends one fire-and-forget
frame and looks at nothing that comes back.

Measured, a brightness change round-trips in 6–11 ms with the commit on or off
the critical path. And the commit has not been observed to persist anything: every
session in the logs starts with the device reporting luminance index 8, including
sessions that followed one where the user had left brightness at 10 or 11 and the
commit had been sent.

It is kept because the app sends it and the intent is clear. It is not kept
because it has been shown to do anything.

## Per-tick writes have to be coalesced

The official app can afford one write per tick because JNI is in-process. Every
call here crosses the Decky bridge, which serialises. Measured, uncoalesced:

```
six writes issued together
backend ran each in 1 ms
every confirmation came back 1.3–1.5 s later
```

A six-deep queue that reads as stutter, and it took the median brightness round
trip from 515 ms to 1556 ms.

`useCoalesced` keeps exactly one write in flight and remembers only the newest
value. The first movement of a gesture still goes out immediately, values the
user has already dragged past are dropped rather than queued, and the value under
their finger is always the last one written.

The commit's read-back is bounded for the same reason: two probes totalling
0.33 s, not the full 2.2 s backoff, which measured 1874 ms and was most of the
regression above. It runs while the user may start dragging again, so it must not
be the slow thing in the room.

## What wedges the USB endpoint

A silent command is never answered, and the endpoint halts after three
unanswered writes. Recovering on failure is too late: the write that trips the
halt still succeeds, and it is the **next** transfer that fails, after sitting
through `_bulk_write`'s 200 ms timeout. That cost lands on whatever the user does
next, which is how a brightness drag ends up stuttering.

Measured over one session, every `-7` was accounted for by a silent command and
none by an answered one:

| command | replies? | writes | `-7` after it |
|---|---|---|---|
| `0x50` volume | yes | 85, at 20/s | **0** |
| `0x09` brightness | no | 34 | 18 |
| `0x0D` commit | no | 18 | 14 |

So `_write_panel` clears the halt *after* every silent write, not only after a
failure: two control transfers, about a millisecond, and the count of unanswered
writes never reaches three.

## Most of a change's latency is not ours

The TX trace is written by the backend at the moment it touches USB, while
`apply:<key>` is written by the frontend when the RPC comes back — so the gap
between them and the reported total split cleanly in one session:

```
13:26:18,306  [ui] screenSize pick: chose "large"      ← the RPC is issued here
13:26:35,869  [ui] screenSize pick: chose "medium"
13:26:35,870  USB write failed (libusb error -4)       ← backend runs all 5
13:26:35,884  [ui] apply:screenSize: ... in 17579 ms   ← answers come back
```

The backend executed five queued writes inside a single millisecond, and the five
responses reached the frontend 32 ms later. The 17.5 s was spent before the
plugin ever saw the request. The same shape at smaller scale is the 262 ms
brightness figure: the TX and the confirmation are 6 ms apart, so ~256 ms went
into getting the call to the backend at all.

So a stalled bridge looks exactly like a slow device, and the honest reading of a
long `apply:<key>` number is: *our part is the gap between the pick line and the
TX line*. Only that part is worth optimising here.

**Every frontend call is serialised, so the panel must not make one per tick.**
Decky handles RPCs one at a time. A brightness drag fires `onChange` far more
often than the write debounce, and sending `pausePolling` on each of those queued
hundreds of calls behind each other — measured at 261 ms on 4 of 29 changes,
while the USB write itself took 1 ms. Drag state that only affects what is drawn
(`adjustingUntil`) is kept in the frontend and costs nothing; the one call that
genuinely has to cross the bridge is throttled to one per 800 ms.

Writes still feel immediate because the reply to our own command is folded into
the state as soon as it arrives — that is what `RayNeoDevice._on_frame` does. It
is an *echo* of our own write, not a notification from the device, and it cannot
reflect a change made elsewhere.

## Progress is reported, never assumed

`0x09` is silent, so the panel could publish what it asked for and hope. It does
not. After the commit the status report is read back, and if it disagrees the
published state carries the panel's value, so the slider moves to where the
hardware actually is. That answer travels in the RPC reply rather than on the
state event, because the settle window is still open when it lands and incoming
state is ignored for the duration of a drag.

Two log lines exist because the log once said the opposite:

```
poll: device info unanswered after both attempts (60 + 200 ms), device lock held for that long
```

```
[ui] apply:screenSize: backend REFUSED the change after 17579 ms: Could not open the glasses (libusb error -4). Reconnect the glasses and press Connect.
```

For five queued writes the panel reported "backend confirmed in 17579 ms" for
commands that had in fact failed — the backend answers a failure with
`{"ok": false}` instead of raising, so awaiting it resolved normally. And
`libusb error -4` is `NO_DEVICE`, not a permissions fault, yet every open failure
ended with "Check udev permissions", which points at the udev rules when the real
cause is that the cable was pulled mid-command.

**One disagreement is deliberately *not* a warning.** A brightness read-back that
comes back lower than requested is the panel's documented behaviour, not a fault
(see the usage note in the README), and logging it as a fault made normal
operation look broken — six and twelve such warnings in single sessions. Scene
mode, audio mode and picture quality keep theirs; for those a disagreement really
is the device refusing a change.

## Scene mode and picture quality

These two took longest to pin down. They were long documented as having no
read-back at all, which was wrong: they live in the `0xE3` status report, which is
decoded by a *different* firmware function than the `0x00` device-info block.
Nothing in the `0x00` response touches that struct region, so searching only that
response is what made them look unreadable.

Their offsets were settled by measurement rather than by reading the firmware:
`calibrate_status()` in the debug panel writes every option of every setting and
diffs the status report. That is what caught the firmware's field names being off
by one across the panel group — picture quality is at `0x15`, not the `0x14` its
parser claims.

Both are read from the device now, so if the phone app changes them this panel
picks it up on the next poll.

## Log format

Every read-only response is rendered by one function, so the log reads the same
way whichever command produced it: one block per command, in command-id order,
each with a `raw` hex section and a `parsed` table.

```
===== 0x00 device info =====
  raw     0x00 device info (64 bytes)
      [00] 99 c8 40 00 ...
  parsed
    field            value     cmd:off
    ----------------------------------
    deviceType       65        00:15
    firmwareVersion  26        00:24
    firmwareBuild    20260922  00:18
    hdrMode          0         E3:15
    maxLuminance     29        -
```

All three are tables rather than `key = value`, because each row relates the
value to something else and a flat pair throws that away: the parsed state names
the byte each value came from, and the capability bitmap relates flag index,
response offset, SDK field name and plugin key. A dash means the value is not on
the wire at all — computed locally, read from the SDK's own configuration, or
derived from another field.

This did not used to be so. The device-info decode sat in its own block *before*
`0x00`, `0xE0` carried a bespoke hex loop, and `0xE3` was logged as a bare
`response` with no command named at all — which is how one command's bytes could
be mistaken for another's. The capability dump also sent `0xE0` twice, once to
render the frame and again through `capabilities()`, throwing the reply away.
