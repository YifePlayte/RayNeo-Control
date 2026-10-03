#!/usr/bin/env python3
"""Import main.py the way Decky Loader does.

`python3 -m py_compile` only compiles; it never executes the imports, so a
broken sys.path (or a bad module layout) slips through and the plugin dies at
load time with nothing but a traceback in ~/homebrew/logs/. This harness
reproduces Decky's actual startup conditions:

  * a fresh interpreter with no repo on sys.path
  * cwd set somewhere unrelated
  * a minimal stub for the `decky` module (the real one only exists inside the
    PluginLoader sandbox)
  * then it calls every RPC method with no hardware attached and asserts each
    one returns a graceful error rather than raising

Usage:  python3 tools/smoke_test.py [path_to_plugin_dir]
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import os
import re
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
DEFAULT_PLUGIN_DIR = HERE.parent


def _stub_decky() -> types.ModuleType:
    """Minimal stand-in for the module Decky injects into plugin sandboxes."""
    mod = types.ModuleType("decky")
    mod.__path__ = []  # type: ignore[attr-defined]

    class _Logger:
        def __init__(self) -> None:
            self.records: list[str] = []

        def _log(self, level: str, msg: str) -> None:
            self.records.append(f"{level}: {msg}")
            print(f"    [decky.{level}] {msg}")

        def info(self, msg: str) -> None:
            self._log("info", msg)

        def warning(self, msg: str) -> None:
            self._log("warning", msg)

        def error(self, msg: str) -> None:
            self._log("error", msg)

        def debug(self, msg: str) -> None:
            self._log("debug", msg)

    emitted: list[tuple[str, str]] = []
    mod.logger = _Logger()  # type: ignore[attr-defined]
    mod.emit = lambda event, payload: emitted.append((event, payload))  # type: ignore[attr-defined]
    mod.__emitted__ = emitted  # type: ignore[attr-defined]
    return mod


#: RPC methods that take arguments: (method, args)
CALLS: dict[str, tuple] = {
    "connect": (),
    "disconnect": (),
    "get_capabilities": (),
    "get_metadata": (),
    
    "send_raw": (0x00, 0x00),
    "set_probe": (False,),
    "reopen_device": (),
    "set_audio_mode": ("standard",),
    "set_audio_tube": (True,),
    "set_brightness": (8,),
    "set_color_enhance": (True,),
    "set_display_mode": ("2d",),
    "set_hdr_mode": ("sdr",),
    "set_scene_mode": ("movie",),
    "set_screen_size": ("medium",),
    "set_volume": (5,),
}


def _calibration_block(report: list, label: str) -> list:
    """The per-option lines of one setting's section in a calibration report."""
    out, inside = [], False
    for line in report:
        if line.startswith(label):
            inside = True
        elif inside and line and not line.startswith(" "):
            break
        elif inside:
            out.append(line)
    return out


def _diff_offsets(line: str) -> list:
    """Offsets named by one calibration row such as ``moved [0x12] 0->3``."""
    return [int(m, 16) for m in re.findall(r"0x([0-9A-Fa-f]{2})\]", line)]


def main() -> int:
    plugin_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_PLUGIN_DIR
    print(f"plugin dir: {plugin_dir}")

    failures: list[str] = []

    # --- required layout ----------------------------------------------------
    print("\n=== layout ===")
    for rel in ("main.py", "plugin.json", "dist/index.js",
                "py_modules/rayneo.py", "py_modules/libusb_ctypes.py"):
        p = plugin_dir / rel
        ok = p.is_file()
        print(f"  {'OK ' if ok else 'MISSING'} {rel}")
        if not ok:
            failures.append(f"missing {rel}")

    # --- manifest ----------------------------------------------------------
    print("\n=== plugin.json ===")
    try:
        import json

        manifest = json.loads((plugin_dir / "plugin.json").read_text())
        for key in ("name", "author", "flags", "api_version"):
            print(f"  {key}: {manifest.get(key)!r}")
        if "root" not in manifest.get("flags", []):
            failures.append('plugin.json flags must contain "root" '
                            "(the glasses are 0664 root:root)")
        else:
            print("  OK  flags contains 'root'")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"plugin.json unreadable: {exc}")

    if failures:
        print("\nlayout problems, aborting before import")
        for f in failures:
            print(f"  - {f}")
        return 1

    # --- import, exactly like Decky ---------------------------------------
    print("\n=== import (Decky-style: fresh interpreter, unrelated cwd) ===")
    child = r'''
import asyncio, importlib.util, inspect, json, logging, os, sys, types

# Stub the module Decky injects; there is no real `decky` on this machine.
decky = types.ModuleType("decky"); decky.__path__ = []
class _L:
    def info(self, m): print(f"    [decky.info] {m}")
    def warning(self, m): print(f"    [decky.warning] {m}")
    def error(self, m): print(f"    [decky.error] {m}")
    def debug(self, m): pass
decky.logger = _L()
decky.emit = lambda e, p: print(f"    [decky.emit] {e}")
sys.modules["decky"] = decky

# cwd is NOT the plugin dir, and the repo root is not on the path.
os.chdir("/tmp")

plugin_dir = sys.argv[1]
sys.path.insert(0, os.path.join(plugin_dir, "py_modules"))
import libusb_ctypes as mod_libusb
_libusb_handle = mod_libusb.libusb()
main_py = os.path.join(plugin_dir, "main.py")
spec = importlib.util.spec_from_file_location("rayneo_plugin_main", main_py)
mod = importlib.util.module_from_spec(spec)
sys.modules["rayneo_plugin_main"] = mod
try:
    spec.loader.exec_module(mod)
except Exception:
    import traceback; traceback.print_exc()
    sys.exit(2)
print("  OK  main.py imported")

plugin_cls = getattr(mod, "Plugin", None)
if plugin_cls is None:
    print("  FAIL no Plugin class"); sys.exit(3)

rpc = sorted(n for n, f in inspect.getmembers(plugin_cls, inspect.isfunction)
             if not n.startswith("_"))
print(f"  RPC methods ({len(rpc)}): {', '.join(rpc)}")

CALLS = json.loads(sys.argv[2])

async def go():
    p = plugin_cls()
    bad = []
    for name in rpc:
        args = CALLS.get(name)
        if args is None:
            continue
        try:
            r = await getattr(p, name)(*args)
        except Exception as e:
            print(f"    FAIL {name}: {type(e).__name__}: {e}")
            bad.append(name); continue
        # These legitimately succeed without a device:
        #   disconnect  -- nothing to release
        #   get_metadata-- static lists, returns metadata not an error
        #   set_probe   -- flips a backend flag, no USB involved
        if name in ("disconnect", "get_metadata", "set_probe"):
            if not isinstance(r, dict):
                print(f"    FAIL {name}: expected dict, got {type(r).__name__}")
                bad.append(name)
            else:
                print(f"    OK  {name} -> {str(r)[:60]}")
        else:
            if isinstance(r, dict) and "error" in r:
                print(f"    OK  {name} -> graceful error")
            else:
                print(f"    FAIL {name}: expected error, got {str(r)[:60]}")
                bad.append(name)
    return bad

bad = asyncio.run(go())
if bad:
    print("  FAIL methods:", bad); sys.exit(4)
print("  OK  every RPC method degrades gracefully with no hardware")

# ---------------------------------------------------------------- descriptor path
# Without hardware, libusb_open() fails with ACCESS, so open() never reaches
# _find_control_interface(). That hid real bugs twice. Drive the descriptor
# parser directly with the exact wire bytes the kernel exposes for the glasses
# (/sys/bus/usb/devices/7-1/descriptors).
print()
print("=== descriptor parse (real GT Max wire bytes, no hardware needed) ===")
import struct as _s
sys.path.insert(0, os.path.join(plugin_dir, "py_modules"))
from libusb_ctypes import transfer_type_name

# CONFIG(9) + INTERFACE(9) + HID(9) + ENDPOINT(7) + ENDPOINT(7) = 41
GT_MAX_CONFIG = bytes([
    0x09, 0x02, 0x29, 0x00, 0x01, 0x01, 0x00, 0xc0, 0x00,   # config, wTotal=41
    0x09, 0x04, 0x00, 0x00, 0x02, 0x03, 0x00, 0x00, 0x00,   # iface0 HID, 2 EP
    0x09, 0x21, 0x10, 0x01, 0x00, 0x01, 0x22, 0x28, 0x00,   # HID descriptor
    0x07, 0x05, 0x81, 0x03, 0x40, 0x00, 0x04,               # EP 0x81 IN int 64
    0x07, 0x05, 0x02, 0x03, 0x40, 0x00, 0x04,               # EP 0x02 OUT int 64
])
assert len(GT_MAX_CONFIG) == 41, len(GT_MAX_CONFIG)
assert _s.unpack_from("<H", GT_MAX_CONFIG, 2)[0] == 41

from rayneo import RayNeoDevice, _parse_config_blob, _describe_device
r = _parse_config_blob(GT_MAX_CONFIG)
if r is None:
    print("  FAIL _parse_config_blob returned None"); sys.exit(5)
ino, ep_out, ep_in, ol, il, ot, it = r
print(f"  iface={ino} OUT=0x{ep_out:02X}({transfer_type_name(ot)}, mps={ol}) "
      f"IN=0x{ep_in:02X}({transfer_type_name(it)}, mps={il})")
problems = []
if (ep_out, ep_in) != (0x02, 0x81): problems.append(f"endpoints {ep_out:02X}/{ep_in:02X} != 02/81")
if ot != 3 or it != 3:             problems.append(f"transfer types {ot}/{it} != 3/3 (INTERRUPT)")
if ol != 64 or il != 64:           problems.append(f"mps {ol}/{il} != 64/64")
if ino != 0:                       problems.append(f"interface {ino} != 0")
for p in problems:
    print(f"  FAIL {p}")
if problems:
    sys.exit(5)
print("  OK  matches lsusb -v, including the HID descriptor sitting between the")
print("      interface and its endpoints")

# The same device without a class-specific descriptor must still parse.
NO_HID = bytes([
    0x09, 0x02, 0x20, 0x00, 0x01, 0x01, 0x00, 0xc0, 0x00,
    0x09, 0x04, 0x00, 0x00, 0x02, 0x03, 0x00, 0x00, 0x00,
    0x07, 0x05, 0x81, 0x03, 0x40, 0x00, 0x04,
    0x07, 0x05, 0x02, 0x03, 0x40, 0x00, 0x04,
])
r2 = _parse_config_blob(NO_HID)
if r2 is None or r2[1] != 0x02 or r2[2] != 0x81:
    print(f"  FAIL descriptor without a class-specific descriptor: {r2}"); sys.exit(5)
print("  OK  also parses a chain with no HID descriptor present")

# An unknown device must be reported, not raise.
txt = _describe_device(0xDEAD, 0xBEEF)
if "no sysfs descriptors matched" not in txt:
    print(f"  FAIL _describe_device for a missing device: {txt[:120]}"); sys.exit(5)
print("  OK  _describe_device reports a missing device without raising")

# ------------------------------------------------ unsolicited frame handling
print()
print("=== unsolicited frame folding ===")
from rayneo import DeviceInfo

d = RayNeoDevice()
seen = []
d.add_frame_listener(lambda cmd, data: seen.append(cmd))


def frame(cmd, payload=None):
    b = bytearray(64)
    b[0] = 0x99          # RESP_MAGIC
    b[8] = cmd           # echoed command
    if payload is not None:
        b[9] = payload
    return bytes(b)


d._info = DeviceInfo()
d._on_frame(0x09, frame(0x09, 11))   # brightness changed
d._on_frame(0x50, frame(0x50, 7))    # volume changed
d._on_frame(0x1A, frame(0x1A, 1))    # hdr mode -> AI-HDR
d._on_frame(0x06, frame(0x06, 0))    # 3D active
d._on_frame(0x1B, frame(0x1B, 1))    # colour enhance on
d._on_frame(0x01, frame(0x01, 0))    # IMU frame: must not be reported

i = d._info
problems = []
if i.luminance != 11: problems.append(f"luminance {i.luminance} != 11")
if i.volume != 7:     problems.append(f"volume {i.volume} != 7")
if i.hdr_mode != 1:   problems.append(f"hdr_mode {i.hdr_mode} != 1")
if i.display_mode_3d is not True: problems.append(f"display_mode_3d {i.display_mode_3d}")
if i.color_enhance is not True: problems.append(f"color_enhance {i.color_enhance}")
if seen != [0x09, 0x50, 0x1A, 0x06, 0x1B]:
    problems.append(f"listeners fired for {seen}, expected only state-changing frames")
for p in problems:
    print(f"  FAIL {p}")
if problems:
    sys.exit(6)
print(f"  OK  state changes folded in; listeners saw {[hex(c) for c in seen]}")
print("      and the 0x01 (IMU) frame was correctly ignored")

# ----------------------------------------------- offset-discovery diffing
print()
print("=== response diff (offset discovery) ===")
before = bytes([0x99, 0x00] * 32)
after = bytearray(before)
after[0x2A] = 7     # pretend the volume byte moved
after[0x1B] = 1     # pretend the colour-enhance byte moved
d = RayNeoDevice._diff_bytes(before, bytes(after))
# Known offsets carry their field name; 0x1B is a capability-flag byte in this
# frame, so it stays bare. That asymmetry is the point of the test: a bare
# offset is how three status-report fields ended up mislabelled for a while.
expect = ["[0x1B] 0->1", "[0x2A] 153->7  volume"]
if d != expect:
    print(f"  FAIL diff returned {d}, expected {expect}"); sys.exit(7)
print(f"  OK  reports only the bytes that moved, named where known: {d}")
if RayNeoDevice._diff_bytes(before, before) != []:
    print("  FAIL identical frames produced a diff"); sys.exit(7)
print("  OK  identical frames produce no diff")
if RayNeoDevice._diff_bytes(None, before) != []:
    print("  FAIL a missing frame produced a diff"); sys.exit(7)
print("  OK  a missing frame produces no diff")

# ------------------------------------------------- transfer-function selection
print()
print("=== transfer helper selection ===")
d = RayNeoDevice.__new__(RayNeoDevice)
L = _libusb_handle.lib
d._in_interrupt = True
d._out_interrupt = True
assert d._write_fn() is L.libusb_interrupt_transfer, "interrupt OUT -> interrupt_transfer"
assert d._read_fn()  is L.libusb_interrupt_transfer, "interrupt IN -> interrupt_transfer"
d._in_interrupt = d._out_interrupt = False
assert d._write_fn() is L.libusb_bulk_transfer, "bulk OUT -> bulk_transfer"
assert d._read_fn()  is L.libusb_bulk_transfer, "bulk IN -> bulk_transfer"
print("  OK  picks libusb_interrupt_transfer for INTERRUPT, bulk_transfer for BULK")
'''
    env = dict(os.environ, PYTHONPATH="")
    import json as _json

    calls_json = _json.dumps({k: list(v) for k, v in CALLS.items()})
    proc = subprocess.run(
        [sys.executable, "-c", child, str(plugin_dir), calls_json],
        capture_output=True, text=True, cwd="/tmp", env=env,
    )
    print(proc.stdout.rstrip() or "    (no output)")
    if proc.stderr.strip():
        print("  --- stderr ---")
        for line in proc.stderr.rstrip().splitlines():
            print(f"    {line}")
    if proc.returncode != 0:
        failures.append(f"import/RPC smoke test exited {proc.returncode}")

    # --- 0xE3 offset calibration -------------------------------------------
    # Runs the real calibrate_status() against a simulated device that encodes
    # each setting at a known offset. Two things have to hold: every setting
    # must be attributed to its own byte, and every setting must be left exactly
    # as it was found. The second one is the safety property -- this routine
    # changes what the user sees, so leaving the glasses altered is the one
    # failure mode that is not merely cosmetic.
    print("\n=== 0xE3 offset calibration (simulated device) ===")
    try:
        if str(plugin_dir / "py_modules") not in sys.path:
            sys.path.insert(0, str(plugin_dir / "py_modules"))
        R = importlib.import_module("rayneo")
        state = {"size": 1, "enhance": False, "tube": False,
                 "hdr": 0, "scene": 2, "high": False}
        origin = dict(state)

        dev = R.RayNeoDevice()
        dev._handle = object()          # `connected` only tests for a handle
        dev._info = R.DeviceInfo(device_type=65, connected=True, max_volume=16)
        dev.open = lambda: True
        dev.refresh_device_info = lambda: dev._info

        def encode():
            raw = bytearray(64)
            raw[0x08] = 0xE3
            raw[0x0A] = 5                              # luminance index
            raw[R.STATUS_PANEL_COLOR_PARAMS] = state["scene"]
            raw[R.STATUS_HIGH_DYNAMIC] = 1 if state["high"] else 0
            raw[R.STATUS_HDR_MODE] = state["hdr"]
            raw[R.STATUS_COLOR_ENHANCE] = 1 if state["enhance"] else 0
            raw[R.STATUS_AUDIO_TUBE] = 1 if state["tube"] else 0
            raw[R.STATUS_SCREEN_SIZE] = state["size"]
            return bytes(raw)

        def read_status():
            raw = encode()
            dev._apply_status_report(raw)
            return raw

        def fake_send(cmd, value, payload=b"", **_kw):
            p = bytes(payload)
            if cmd == R.CMD_SET_SCREEN_SIZE:
                state["size"] = value
            elif cmd == R.CMD_SET_COLOR_ENHANCE:
                state["enhance"] = bool(value)
            elif cmd == R.CMD_SET_AUDIO_TUBE:
                state["tube"] = bool(value)
            elif cmd == R.CMD_SET_HDR_MODE:
                state["hdr"] = value
            elif cmd == R.CMD_SET_HIGH_DYNAMIC:
                state["high"] = bool(value)
            elif cmd == R.CMD_PANEL_COLOR_ADJUST and len(p) >= 3:
                state["scene"] = p[2]
            return encode()

        dev.read_status_report = read_status
        dev.send = fake_send
        report = dev.calibrate_status()

        # Each setting must be credited with the byte the simulation puts it at.
        expected = {
            "screen size": R.STATUS_SCREEN_SIZE,
            "colour enhance": R.STATUS_COLOR_ENHANCE,
            "audio tube": R.STATUS_AUDIO_TUBE,
            "picture quality": R.STATUS_HDR_MODE,
            "scene mode": R.STATUS_PANEL_COLOR_PARAMS,
            "high dynamic": R.STATUS_HIGH_DYNAMIC,
        }
        for label, off in expected.items():
            block = _calibration_block(report, label)
            moved = {f"0x{i:02X}" for ln in block for i in _diff_offsets(ln)}
            if moved != {f"0x{off:02X}"}:
                print(f"  FAIL {label} attributed {sorted(moved)}, "
                      f"expected ['0x{off:02X}']")
                failures.append(f"calibration misattributes {label}")
            else:
                print(f"  OK  {label} -> 0x{off:02X}")

        if state != origin:
            print(f"  FAIL settings not restored: {state} != {origin}")
            failures.append("calibration left the device changed")
        else:
            print("  OK  every setting restored to its starting value")

        if "NOT fully restored" in "\n".join(report):
            print("  FAIL calibration reported unrestored settings")
            failures.append("calibration could not restore its baseline")
        else:
            print("  OK  reports a restorable baseline")

        # Bare offsets only: printing the *assumed* field name would just
        # restate the assumption the calibration exists to test.
        named = [ln for ln in report
                 if "moved" in ln and any(w in ln for w in
                                          R.STATUS_FIELD_NAMES.values())]
        if named:
            print(f"  FAIL calibration printed assumed field names: {named[:1]}")
            failures.append("calibration labels offsets with assumed names")
        else:
            print("  OK  reports bare offsets, not assumed field names")

        # The other safety property: when the baseline is *not* knowable, the
        # routine has to say so rather than quietly leaving the glasses altered.
        # A truncated report is the way in -- every panel offset reads as absent,
        # so nothing can be restored. This is the one case that genuinely leaves
        # the device changed, which is exactly why it has to be reported.
        blind = R.RayNeoDevice()
        blind._handle = object()
        blind._info = R.DeviceInfo(device_type=65, connected=True, max_volume=16)
        blind.open = lambda: True
        blind.refresh_device_info = lambda: blind._info
        blind.read_status_report = lambda: bytes(0x10)
        blind.send = lambda *a, **k: bytes(0x10)
        blind_report = "\n".join(blind.calibrate_status())
        if "NOT fully restored" not in blind_report:
            print("  FAIL an unknowable baseline was reported as restorable")
            failures.append("calibration hides an unrestorable baseline")
        else:
            print("  OK  an unknowable baseline is reported, not hidden")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"calibration test failed: {exc}")

    # --- fire-and-forget commands -----------------------------------------
    # Measured from the TX trace: 0x09, 0x1A, 0x73 and 0x18 never draw a reply.
    # Waiting on one costs the full timeout and, three unanswered writes deep,
    # leaves the interrupt endpoint halted so that every later transfer fails.
    # Scene mode was the expensive one: the code read the preview's missing reply
    # as failure and skipped the two save frames, so a scene change was
    # previewed and never persisted.
    print("\n=== silent commands are not waited on ===")
    try:
        if str(plugin_dir / "py_modules") not in sys.path:
            sys.path.insert(0, str(plugin_dir / "py_modules"))
        R = importlib.import_module("rayneo")
        src = inspect.getsource(R.RayNeoDevice)

        def sends_without_opt_out(cmd_value: int, label: str) -> Optional[str]:
            """Drive the real setters and report any reply we waited for.

            Behavioural, not textual. Parsing the source for
            ``expect_response=False`` is unreliable -- the argument lists nest
            parentheses (``bytes((a, b, c))``), and a count is fooled the moment
            one call opts out and another does not. Calling the code settles it.
            """
            probe = R.RayNeoDevice()
            probe._handle = object()
            probe._info = R.DeviceInfo(device_type=65, connected=True,
                                       max_volume=16)
            probe.open = lambda: True
            probe._calibrating = True      # skip the post-write read-back
            seen: list = []

            def fake_send(command, value=0, payload=b"", expect_response=True,
                          timeout_ms=400, trace=True):
                seen.append((command, expect_response))
                return None

            probe.send = fake_send
            probe.read_status_report = lambda: None
            actions = {
                "brightness": lambda: probe.set_brightness(4),
                "scene mode": lambda: probe.set_scene_mode("movie"),
                "picture quality": lambda: probe.set_hdr_mode("aihdr"),
                "high dynamic": lambda: probe.set_high_dynamic(True),
            }
            try:
                actions[label]()
            except Exception as exc:
                return f"{label} could not be driven ({exc})"
            mine = [e for c, e in seen if c == cmd_value]
            if not mine:
                return f"{label} never sent 0x{cmd_value:02X}"
            if any(mine):
                return (f"{label} waited for a reply on {sum(mine)}/{len(mine)} "
                        f"send(s) of 0x{cmd_value:02X}")
            return None

        for label, cmd in (("brightness", R.CMD_SET_BRIGHTNESS),
                           ("scene mode", R.CMD_PANEL_COLOR_ADJUST),
                           ("picture quality", R.CMD_SET_HDR_MODE),
                           ("high dynamic", R.CMD_SET_HIGH_DYNAMIC)):
            problem = sends_without_opt_out(cmd, label)
            if problem:
                print(f"  FAIL {problem}")
                failures.append(problem)
            else:
                print(f"  OK  {label} never waits for a reply")

        # Calibrated offsets are measurements. Pin them as literals so a later
        # "tidying" cannot quietly move one back to an inferred value -- which is
        # exactly how colour enhance and the volume ceiling broke before.
        measured = {
            "STATUS_LUMINANCE_INDEX": 0x0A,
            "STATUS_AUDIO_MODE": 0x10,
            "STATUS_PANEL_COLOR_PARAMS": 0x12,
            "STATUS_HDR_MODE": 0x15,
            "STATUS_COLOR_ENHANCE": 0x17,
            "STATUS_AUDIO_TUBE": 0x18,
            "STATUS_SCREEN_SIZE": 0x1A,
        }
        for name, want in measured.items():
            got = getattr(R, name)
            if got != want:
                print(f"  FAIL {name} is 0x{got:02X}, calibrated value is "
                      f"0x{want:02X}")
                failures.append(f"{name} moved off its calibrated offset")
            else:
                print(f"  OK  {name} = 0x{got:02X} (calibrated)")

        if len(R.UNACKNOWLEDGED_COMMANDS) != 5:
            print(f"  FAIL UNACKNOWLEDGED_COMMANDS has "
                  f"{len(R.UNACKNOWLEDGED_COMMANDS)} entries, expected 5")
            failures.append("UNACKNOWLEDGED_COMMANDS disagrees with the trace")
        else:
            print("  OK  UNACKNOWLEDGED_COMMANDS matches the measured trace "
                  "(0x09, 0x18, 0x1A, 0x49, 0x73)")

        # Scene mode must send all three frames. The save frames are the whole
        # point of the sequence; dropping them means the change reverts.
        scene = re.search(r"    def set_scene_mode\(.*?\n(?=\n    def |\n    #|\Z)",
                          src, re.S).group(0)
        for op in ("PANEL_COLOR_OP_MODE_PREVIEW", "PANEL_COLOR_OP_SAVE",
                   "PANEL_COLOR_OP_SAVE2"):
            if op not in scene:
                print(f"  FAIL scene mode no longer sends {op}")
                failures.append(f"scene mode dropped {op}")
        if "was not acknowledged" in scene or "if preview is None" in scene:
            print("  FAIL scene mode still treats a missing reply as failure")
            failures.append("scene mode still gates on a silent command's reply")
        else:
            print("  OK  scene mode sends preview + both saves unconditionally")

        # A fixed sleep is what produced the stale read-back in the first place.
        if "time.sleep(0.25)" in src:
            print("  FAIL a fixed 250 ms settle is back")
            failures.append("calibration uses a fixed settle delay")
        else:
            print("  OK  reads back after the panel settles, not after a guess")

        # The volatile bytes must never be reported as a setting's own byte.
        if not R.VOLATILE_STATUS_BYTES:
            print("  FAIL the per-report checksum/counter bytes are not excluded")
            failures.append("volatile status bytes not excluded")
        else:
            print(f"  OK  excludes per-report noise at "
                  f"{[hex(b) for b in R.VOLATILE_STATUS_BYTES]}")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"silent-command check failed: {exc}")

    print("\n=== colour-enhance gating ===")
    try:
        labels = (plugin_dir / "src" / "labels.ts").read_text()
        content = (plugin_dir / "src" / "Content.tsx").read_text()
        api = (plugin_dir / "src" / "api.ts").read_text()

        # Which control each restriction belongs to. Two different sources:
        #
        #   AI-HDR  -> colour enhance   app i18n: "AI-HDR模式下不支持启用色彩增强"
        #   阅读     -> colour enhance   **hardware**, and not the i18n: 0x17 reads
        #                               back as 1 in 阅读 while the panel does not
        #                               apply it, so the read-back lies. Confirmed
        #                               by hand -- the toggle cannot be turned on.
        #   阅读     -> screen size      app i18n: "...scene_office_mode..."
        #   3D      -> screen size      app i18n: "...not_supported_in_3D_mode"
        #   护眼     -> nothing          the device clears 0x17 on entry but takes
        #                               it straight back, so gating it would remove
        #                               a control that works
        #
        # The 阅读/colour-enhance row is the one that keeps going wrong: it was
        # written off on the strength of a status byte reading 1, which is exactly
        # the read-back that lies.
        for const, want, what in (
                ("SCENE_BLOCKS_COLOR_ENHANCE", ["reading"],
                 "阅读 blocking colour enhance (hardware)"),
                ("SCENE_BLOCKS_SCREEN_SIZE", ["reading"],
                 "阅读 blocking screen size (i18n)"),
                ("MODE_BLOCKS_SCREEN_SIZE", ["3d"],
                 "3D blocking screen size (i18n)")):
            block = re.search(rf"{const} = \[([^\]]*)\]", labels)
            modes = re.findall(r'"(\w+)"', block.group(1)) if block else []
            if modes != want:
                print(f"  FAIL {what}: {const} is {modes}, expected {want}")
                failures.append(f"{const} does not match its source")
            elif const not in content:
                print(f"  FAIL {const} is declared but the panel does not use it")
                failures.append(f"{const} unused")
            else:
                print(f"  OK  {what} is declared and used")

        # 护眼 must not appear in any of them: it is not a lock.
        if re.search(r"(SCENE|MODE)_BLOCKS_\w+ = \[[^\]]*eyeProtection", labels):
            print("  FAIL 护眼 is gated as a lock, but the device accepts the "
                  "value straight back")
            failures.append("护眼 is treated as a lock")
        else:
            print("  OK  护眼 is not treated as a lock")

        for gate, flag in (("screen size", "screenSizeBlocked"),
                           ("colour boost", "colorEnhanceBlocked")):
            if not re.search(rf"disabled=\{{[^}}]*{flag}", content):
                print(f"  FAIL the {gate} control is not disabled by its gate")
                failures.append(f"{gate} is not actually gated")
            else:
                print(f"  OK  the {gate} control is disabled by its gate")

        if 'hdrOpt === "aihdr"' not in content:
            print("  FAIL AI-HDR no longer gates colour boost")
            failures.append("AI-HDR does not gate colour boost")
        else:
            print("  OK  AI-HDR gates colour boost")

        # There must be no *button* for saving brightness. The RPC is now wired
        # up, because the official app sends it as the second half of every
        # brightness change -- but it is an internal commit step, not something
        # to put in front of the user. This guard used to forbid the RPC outright
        # and had to be narrowed when the commit became part of the real flow.
        if re.search(r'(onClick|Button|DropdownItem)[^\\n]*[Ss]ave', content):
            print("  FAIL a save-brightness button is back in the panel")
            failures.append("save-brightness button reintroduced")
        elif "saveBrightness" in content:
            print("  FAIL the panel calls the save RPC directly instead of "
                  "leaving the commit to the brightness flow")
            failures.append("the commit is driven by a control, not by a change")
        else:
            print("  OK  the commit is driven by a brightness change, and "
                  "there is no button for it")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"colour-enhance gating check failed: {exc}")

    print("\n=== the artefact matches the source ===")
    try:
        _pkg = (plugin_dir / "tools" / "package.py").read_text()
        _src = (plugin_dir / "src").rglob("*")
        # package.py used to stage whatever was already in dist/. A change to
        # src/types.ts therefore never reached the bundle -- every check passed,
        # because every check reads src/, and nothing compared dist/ to it. The
        # zip then carried a field the source no longer had, which is the
        # "tested a stale build" failure with a cause: nothing rebuilt.
        # The exact call, not the words: "pnpm", "run" and "build" all appear
        # elsewhere in the file, so a presence check passed with the build line
        # deleted. Same trap as every other hollow guard in here.
        if not re.search(r'run\(\["pnpm",\s*"run",\s*"build"\]\)', _pkg):
            print("  FAIL package.py does not build the frontend")
            failures.append("packaging does not rebuild the bundle")
        elif not re.search(r"=\s*newest_stale\(\)", _pkg):
            print("  FAIL package.py does not verify the build actually ran")
            failures.append("a build that does nothing would go unnoticed")
        else:
            print("  OK  packaging rebuilds the bundle and checks it is newer "
                  "than src/")

        bundle = plugin_dir / "dist" / "index.js"
        if not bundle.exists():
            print("  FAIL dist/index.js is missing")
            failures.append("the bundle is missing")
        else:
            newer = [str(q.relative_to(plugin_dir))
                     for q in sorted((plugin_dir / "src").rglob("*"))
                     if q.is_file() and q.stat().st_mtime > bundle.stat().st_mtime]
            if newer:
                print(f"  FAIL dist/ is older than {newer}; the bundle on disk "
                      f"does not match src/")
                failures.append("the built bundle is stale")
            else:
                print("  OK  dist/index.js is newer than every file in src/")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"artefact freshness check failed: {exc}")

    print("\n=== install instructions ===")
    try:
        _rm = (plugin_dir / "README.md").read_text()
        # Decky hides "Install Plugin from ZIP File" behind its Developer tab,
        # which only appears once Developer mode is on. The README used to send
        # the reader to Settings -> General -> Install from ZIP, where no such
        # control exists -- instructions that cannot be followed.
        start = _rm.index("### From a release zip")
        # From after this heading to the next one. Searching for "### " from the
        # start of the slice finds the heading itself and yields an empty string,
        # which made the check fail on a README that was correct.
        nxt = _rm.find("\n### ", start + 1)
        zip_bit = _rm[start:nxt if nxt > 0 else len(_rm)]
        missing = [w for w in ("Developer mode", "Developer", "ZIP")
                   if w not in zip_bit]
        if missing:
            print(f"  FAIL the ZIP install instructions do not mention {missing}")
            failures.append("the ZIP install path is not the real one")
        elif "from a release zip" not in _rm.lower():
            print("  FAIL there is no ZIP install section")
            failures.append("no ZIP install instructions")
        else:
            print("  OK  the ZIP install goes through Decky's Developer tab, "
                  "where the control actually is")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"install instruction check failed: {exc}")

    print("\n=== claims about withheld features ===")
    try:
        _r = (plugin_dir / "py_modules" / "rayneo.py").read_text()
        _doc = (plugin_dir / "docs" / "PROTOCOL.md").read_text()
        _rm = (plugin_dir / "README.md").read_text()

        # 120 Hz is a gap, not a hardware limitation, and the tree said otherwise
        # for a while: an inferred "the app hides it on Gemini-class hardware"
        # that the device's own capability bitmap contradicts. A withheld feature
        # must say why it is withheld, and "the device says no" is only allowed
        # when the device actually says no.
        bits = re.search(r'"fps120":\s*(\d+)', _r)
        cap = re.search(r'"fps120":\s*(\w+)', _r)
        if not bits or not cap:
            print("  FAIL the fps120 capability flag is gone")
            failures.append("the fps120 capability flag is missing")
        else:
            # Two facts have to survive, because losing either one is how this
            # went wrong before:
            #
            #   1. the evidence is the app's behaviour on THIS model, checked on
            #      real hardware -- not a protocol capability and not a string;
            #   2. strings_zh.json ships to every RayNeo model, so a label in it
            #      says nothing about this one. It looks like a feature list and
            #      is not; it is the union across the family.
            # Whitespace-normalised: both claims were written across a line
            # break, and a needle that has to match a line break is a needle
            # that reports a perfectly good note as missing.
            def _flat(t):
                # Comment markers too: the note lives in a `#` block, so
                # collapsing only whitespace leaves "for # this model" and the
                # phrase never matches.
                return re.sub(r"\s+", " ", t.replace("#", " "))
            r_flat, doc_flat, rm_flat = _flat(_r), _flat(_doc), _flat(_rm)
            missing = []
            if "no refresh-rate setting on the GT Max" not in rm_flat and \
                    "no refresh-rate setting for this model" not in r_flat:
                missing.append("the app's behaviour on this model")
            if not re.search(r"ships to (every|all) RayNeo model", r_flat + doc_flat):
                missing.append("that the i18n is shared across models")
            if missing:
                print(f"  FAIL the 120 Hz note no longer says: {missing}")
                failures.append(f"the 120 Hz note lost {missing}")
            else:
                print("  OK  120 Hz rests on the app's behaviour on this model, "
                      "and says the i18n is shared")

        # Any control the panel greys out for a device reason must name the
        # reason. The ones that legitimately exist are all accounted for.
        for name in ("SCENE_BLOCKS_COLOR_ENHANCE", "SCENE_BLOCKS_SCREEN_SIZE",
                     "MODE_BLOCKS_SCREEN_SIZE"):
            if name not in (plugin_dir / "src" / "labels.ts").read_text():
                print(f"  FAIL {name} is missing")
                failures.append(f"{name} disappeared")
                break
        else:
            print("  OK  every lockout still names its source in labels.ts")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"withheld-feature check failed: {exc}")

    print("\n=== debug surface ===")
    try:
        main_src_dbg = (plugin_dir / "main.py").read_text()
        api_dbg = (plugin_dir / "src" / "api.ts").read_text()
        dbg_tsx = (plugin_dir / "src" / "DebugSection.tsx").read_text()

        # One flag must gate both halves, or "hidden" and "quiet" drift apart:
        # buttons gone while the log still fills, or the reverse.
        gated = []
        tx = re.search(r"def _on_tx\(line: str\) -> None:(.*?)\n\n", main_src_dbg, re.S)
        if not tx or "if not _debug:" not in tx.group(1):
            gated.append("the TX trace")
        snap = re.search(r"async def _log_protocol_snapshot\(.*?\n(?=\n    async def |\Z)",
                         main_src_dbg, re.S).group(0)
        if "if not _debug:" not in snap:
            gated.append("the protocol snapshot")
        note = re.search(r"async def log_frontend_message\(.*?\n(?=\n    async def |\Z)",
                         main_src_dbg, re.S).group(0)
        if "if _debug:" not in note:
            gated.append("the frontend timing notes")
        if gated:
            print(f"  FAIL still logged with debug off: {gated}")
            failures.append(f"debug off does not silence: {gated}")
        else:
            print("  OK  debug off silences the trace, the tables and the notes")

        # Errors must NOT be gated: a failure the user has to act on is not
        # debug output, and losing it would make a bug report start blind.
        fail_src = re.search(r"def _fail\(.*?\n(?=\n    |\ndef )", main_src_dbg, re.S)
        err_src = re.search(r"async def log_frontend_error\(.*?\n(?=\n    async def |\Z)",
                            main_src_dbg, re.S).group(0)
        if (fail_src and "logger.error" in fail_src.group(0)) or "logger.error" in err_src:
            print("  OK  errors are logged whatever the debug flag says")
        else:
            print("  FAIL an error path is gated behind debug")
            failures.append("debug off can hide an error")

        # The frontend must drop the note before it costs an RPC, not after.
        if "if (!debugNotes) return Promise.resolve" not in api_dbg:
            print("  FAIL logUiTiming still sends the RPC when debug is off")
            failures.append("debug notes cost an RPC even when suppressed")
        else:
            print("  OK  a suppressed timing note costs no RPC")

        # The switch must stay reachable: hiding it with the tools it controls
        # would strand the panel in whichever state it was left in.
        toggle = re.search(r"<ToggleField[^>]*label=\"调试模式\"", dbg_tsx)
        if not toggle:
            print("  FAIL the debug toggle is missing from the panel")
            failures.append("there is no debug switch")
        else:
            after = dbg_tsx[toggle.start():]
            if "{!debug ?" not in after:
                print("  FAIL the debug tools are not behind the switch")
                failures.append("debug tools stay visible when debug is off")
            else:
                print("  OK  the switch is always visible and the tools hide behind it")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"debug surface check failed: {exc}")

    print("\n=== firmware identity ===")
    try:
        if str(plugin_dir / "py_modules") not in sys.path:
            sys.path.insert(0, str(plugin_dir / "py_modules"))
        R = importlib.import_module("rayneo")

        def fw_frame(date: bytes, rev: int) -> bytes:
            row = bytearray(64)
            row[0], row[8] = 0x99, 0x00
            row[R.DEVICE_INFO_BUILD_DATE:
                R.DEVICE_INFO_BUILD_DATE + len(date)] = date
            row[R.DEVICE_INFO_FIRMWARE_VERSION] = rev & 0xFF
            row[R.DEVICE_INFO_FIRMWARE_VERSION + 1] = (rev >> 8) & 0xFF
            return bytes(row)

        # The GT Max's real values: "Sep 22 2026" and 26.
        if R.firmware_build(fw_frame(b"Sep 22 2026", 26)) != "20260922":
            print("  FAIL the GT Max build date does not parse to 20260922")
            failures.append("firmware build date does not parse")
        else:
            print("  OK  'Sep 22 2026' -> 20260922 (the app's own display)")

        probe = R.RayNeoDevice()
        probe._info = R.DeviceInfo()
        probe._apply_device_info(fw_frame(b"Sep 22 2026", 26))
        got = (probe._info.firmware_build, probe._info.firmware_version)
        if got != ("20260922", 26):
            print(f"  FAIL device info gave {got}, expected ('20260922', 26)")
            failures.append("firmware fields disagree with the frame")
        else:
            print("  OK  build date and numeric revision are read separately")

        # Every unparseable shape must yield None rather than a guess: a
        # plausible-looking wrong date next to a real version number is worse
        # than a blank.
        unparseable = [b"Sep 32 2026", b"Xyz 22 2026", b"Sep", b"Sep 22 26",
                       b"2026-09-22", b"Zzz"]
        bad = [d for d in unparseable
               if R.firmware_build(fw_frame(d, 26)) is not None]
        if bad:
            print(f"  FAIL unreadable dates produced a value: {bad}")
            failures.append("firmware build date is guessed when unreadable")
        else:
            print(f"  OK  {len(unparseable)} unreadable shapes all return None")

        # The two must not come from the same bytes, or one silently shadows the
        # other -- which is how hdr_mode once came back as 16 (maxVolume).
        if R.DEVICE_INFO_BUILD_DATE == R.DEVICE_INFO_FIRMWARE_VERSION:
            print("  FAIL build date and revision share one offset")
            failures.append("firmware fields share an offset")
        else:
            print(f"  OK  distinct offsets: date 0x{R.DEVICE_INFO_BUILD_DATE:02X}, "
                  f"revision 0x{R.DEVICE_INFO_FIRMWARE_VERSION:02X}")

        content = (plugin_dir / "src" / "Content.tsx").read_text()
        if not re.search(r"\$\{build\} \(\$\{rev\}\)", content):
            print("  FAIL the panel no longer shows the revision beside the date")
            failures.append("firmware label dropped the numeric revision")
        else:
            print("  OK  the panel shows '<build> (<revision>)'")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"firmware identity check failed: {exc}")

    print("\n=== polling pause while dragging ===")
    # A poll that lands mid-drag shares the device lock with the debounced write
    # and re-applies the pre-drag value under the user's thumb. The pause is a
    # deadline on the backend precisely so a lost call cannot strand it: a
    # boolean that is never cleared silently stops live updates forever, which is
    # what the _mutating leak did.
    try:
        if str(plugin_dir / "py_modules") not in sys.path:
            sys.path.insert(0, str(plugin_dir / "py_modules"))
        R = importlib.import_module("rayneo")

        dev = R.RayNeoDevice()
        dev._handle = object()
        dev._info = R.DeviceInfo(connected=True, device_type=65, luminance=4)
        dev.open = lambda: True
        seen = []
        dev.send = lambda cmd, *a, **k: (seen.append(cmd), bytes(64))[1]

        def poll_cost() -> int:
            before = len(seen)
            dev.poll_state()
            return len(seen) - before

        baseline = poll_cost()
        if baseline == 0:
            print("  FAIL an unpaused poll sent nothing, so this proves nothing")
            failures.append("poll sends no transactions to begin with")
        else:
            print(f"  OK  an unpaused poll costs {baseline} transactions")

        dev.pause_polling(0.4)
        if poll_cost() != 0:
            print("  FAIL the poll still ran while paused")
            failures.append("polling pause does not stop the poll")
        else:
            print("  OK  a paused poll sends nothing")

        # A pause is not a disconnect. Returning None makes the poller publish
        # "glasses stopped answering" and the panel greys itself out.
        if dev.poll_state() is None:
            print("  FAIL a paused poll reports the device as gone")
            failures.append("polling pause is mistaken for a disconnect")
        else:
            print("  OK  a paused poll still reports the device as connected")

        time.sleep(0.5)
        if poll_cost() == 0:
            print("  FAIL the poll never came back after the deadline")
            failures.append("polling pause never expires")
        else:
            print("  OK  the poll resumes by itself when the deadline passes")

        content = (plugin_dir / "src" / "useDevice.ts").read_text()
        api = (plugin_dir / "src" / "api.ts").read_text()
        if "pausePolling(" not in content or "pause_polling" not in api:
            print("  FAIL the frontend no longer pauses the poll")
            failures.append("frontend does not pause the poll")
        else:
            print("  OK  the frontend pauses the poll on every slider change")

        # ...but throttled, not once per tick. A drag fires onChange far more
        # often than the write debounce, and an RPC each time queued up behind
        # itself: brightness took 261 ms on 4 of 29 changes while the write
        # itself took 1 ms. The local deadline above needs no RPC at all, so only
        # the pause has to be careful here.
        #
        # Checked as an ordering, not as a presence. A guard that only looked for
        # the constant passed with the early return deleted and with the stamp
        # never advancing -- both of which leave the constant sitting there,
        # looking fine.
        begin = re.search(
            r"const beginAdjust = useCallback\(\(\) => \{(.*?)\n  \}, \[\]\);",
            content, re.S)
        throttle = re.search(r"PAUSE_THROTTLE_MS = (\d+)", content)
        if not begin:
            print("  FAIL beginAdjust has no throttle")
            failures.append("the pause throttle could not be located")
        elif not throttle or int(throttle.group(1)) < 400:
            print(f"  FAIL the pause RPC can still fire every "
                  f"{throttle.group(1) if throttle else '?'} ms")
            failures.append("the pause RPC throttle is too tight")
        else:
            body = begin.group(1)
            gate = re.search(r"if\s*\([^)]*PAUSE_THROTTLE_MS[^)]*\)\s*return", body)
            rpc = body.find("pausePolling(")
            if gate is None or rpc < 0 or gate.start() > rpc:
                print("  FAIL the pause RPC is not gated by the throttle")
                failures.append("the pause throttle never gates the RPC")
            elif not re.search(r"pausedAt\.current = now\b", body):
                print("  FAIL the throttle stamp never advances")
                failures.append("the pause throttle always fires")
            else:
                print(f"  OK  the pause RPC is throttled to one per "
                      f"{throttle.group(1)} ms, and it gates the RPC")

        # And the slider must not be overwritten while the drag is live.
        if "isAdjusting()" not in content:
            print("  FAIL incoming state can still overwrite a slider mid-drag")
            failures.append("slider position is not guarded during a drag")
        else:
            print("  OK  incoming state is ignored while a slider is being dragged")

        # The window must outlast the poll interval, or one drag still straddles
        # a poll.
        settle = re.search(r"SLIDER_SETTLE_MS = (\d+)", content)
        if not settle:
            print("  FAIL the settle window is not a named constant")
            failures.append("slider settle window is not named")
        elif int(settle.group(1)) < 1600:
            print(f"  FAIL the settle window ({settle.group(1)} ms) is shorter "
                  f"than one poll interval")
            failures.append("slider settle window is shorter than a poll")
        else:
            print(f"  OK  the settle window ({settle.group(1)} ms) outlasts "
                  f"one poll")

        # One unanswered query must not read as an unplug. A silent panel command
        # leaves the device busy, and the poll that follows it can time out --
        # which greyed out the panel mid-drag, six times in one session.
        main_src = (plugin_dir / "main.py").read_text()
        threshold = re.search(r"MISSES_BEFORE_DISCONNECT = (\d+)", main_src)
        if not threshold or int(threshold.group(1)) < 2:
            print("  FAIL a single missed poll still counts as a disconnect")
            failures.append("one missed poll declares the glasses gone")
        else:
            print(f"  OK  a disconnect needs {threshold.group(1)} consecutive "
                  f"missed polls")
        loop = re.search(r"async def _poll_loop\(.*?\n(?=\n    async def |\Z)",
                         main_src, re.S).group(0)
        # The reset has to sit *after* the increment, in the success path. A
        # plain substring check also matches the initialisation before the loop,
        # which is why the first version of this passed on code that never reset.
        bump = loop.find("_missed += 1")
        reset = loop.find("_missed = 0", bump + 1) if bump >= 0 else -1
        if bump < 0 or reset < 0:
            print("  FAIL the missed-poll counter is never reset by a success")
            failures.append("missed-poll counter is not reset")
        else:
            print("  OK  any successful poll resets the counter")

        # Declaring the glasses gone must also release the handle. `open()` is
        # idempotent and hands back whatever handle it holds, so a stale one made
        # the next Connect spend its only attempt on a device that was no longer
        # there: the first press failed and only the second worked.
        gone_block = loop[loop.find("_last_state.get(\"connected\")"):] \
            if "_last_state.get(\"connected\")" in loop else loop
        if "_device.close" not in gone_block:
            print("  FAIL a disconnect leaves the dead USB handle in place")
            failures.append("disconnect does not release the handle")
        else:
            # ...and the state must be read before the close, or it is empty.
            if gone_block.find("get_device_info()") > \
                    gone_block.find("_device.close"):
                print("  FAIL the state is read after closing, so it is empty")
                failures.append("state is captured after the handle is dropped")
            else:
                print("  OK  a disconnect releases the handle, after reading "
                      "the state")

        # ...and the panel must then find them again by itself. The poll used to
        # skip every cycle while disconnected, so once it declared the glasses
        # gone nothing ever polled again: every control froze on a stale state
        # until the user noticed and pressed Connect. One wrong "gone" -- which a
        # busy panel can cause, and did -- turned into a plugin that looked dead
        # with the glasses plugged in.
        loop = re.search(r"    async def _poll_loop\(.*?\n(?=\n    async def "
                         r"|\n    def |\Z)", main_src, re.S).group(0)
        skip = re.search(r"if not _device\.connected[^\n]*:\s*\n\s*continue", loop)
        if skip:
            print("  FAIL the poll skips every cycle while disconnected, so a "
                  "replug is never noticed on its own")
            failures.append("the panel cannot recover from a disconnect by itself")
        elif not re.search(r"if not _device\.connected:", loop):
            # The condition matters as much as the helper. Checking only that
            # `_try_reopen` appears passed with the whole block unreachable,
            # because the dead code still contained every word being looked for.
            print("  FAIL nothing in the poll reacts to the device being gone, "
                  "so the reconnect probe is unreachable")
            failures.append("the reconnect probe is not gated on being disconnected")
        elif "_try_reopen" not in loop:
            print("  FAIL the poll never looks for the glasses coming back")
            failures.append("a disconnected device is never re-opened")
        elif not re.search(r"if not await asyncio\.to_thread\(_try_reopen\):\s*\n\s*continue",
                            loop):
            print("  FAIL the reopen attempt does not gate the cycle")
            failures.append("the reconnect probe does not actually gate the poll")
        else:
            print("  OK  a replug is picked up on its own, no Connect needed")

        # ...but not when the user asked for the disconnect. Absence they asked
        # for and absence that happened to us are different things, and the poll
        # healing the first one undoes the button: it reopened within one poll
        # interval and Disconnect appeared not to work.
        # Imported here rather than reusing the copy loaded at the top of this
        # file: that one is not reliably reachable from this block, and a guard
        # that breaks when the harness is reorganised is a guard that gets
        # deleted. Self-contained is worth the second import.
        # Self-contained: this whole file runs as one long module-level block,
        # and `decky` is stubbed only inside the `child` subprocess string. The
        # parent never stubbed it, so importing main.py here needs the stub.
        if "decky" not in sys.modules:
            sys.modules["decky"] = _stub_decky()
        _spec = importlib.util.spec_from_file_location(
            "_rayneo_probe_main", str(plugin_dir / "main.py"))
        main_mod = importlib.util.module_from_spec(_spec)
        sys.modules["_rayneo_probe_main"] = main_mod
        _spec.loader.exec_module(main_mod)
        opened = []
        real_open = main_mod._device.open
        main_mod._device.open = lambda: (opened.append(1), True)[1]
        try:
            main_mod._user_closed = True
            got = main_mod._try_reopen()
            if got or opened:
                print("  FAIL the poll reopens after a deliberate disconnect, "
                      "so the Disconnect button is undone")
                failures.append("a user disconnect is undone by the poll")
            else:
                print("  OK  a deliberate disconnect is not undone by the poll")
        finally:
            main_mod._device.open = real_open
            main_mod._user_closed = False

        # And Connect must clear it, or the poll would stay asleep afterwards.
        conn_src = re.search(r"    async def connect\(.*?\n(?=\n    async def |\Z)",
                             main_src, re.S).group(0)
        disc_src = re.search(r"    async def disconnect\(.*?\n(?=\n    async def |\Z)",
                             main_src, re.S).group(0)
        reopen_src = re.search(r"    async def reopen_device\(.*?\n(?=\n    async def |\Z)",
                               main_src, re.S).group(0)
        rules = [
            ("disconnect", disc_src, True),
            ("connect", conn_src, False),
            ("reopen_device", reopen_src, False),
        ]
        wrong = [n for n, src, want in rules
                 if f"_user_closed = {want}" not in src]
        probe_src = inspect.getsource(main_mod._try_reopen)
        if "if _user_closed:" not in probe_src:
            print("  FAIL the reconnect probe does not consult the flag")
            failures.append("the reconnect probe ignores a deliberate disconnect")
        elif probe_src.index("if _user_closed") > probe_src.index("_device.open()"):
            # Before, not after: the point is to not go near USB at all.
            print("  FAIL the reconnect probe only checks the flag after "
                  "touching USB")
            failures.append("the reconnect probe queries USB unnecessarily")
        elif wrong:
            print(f"  FAIL {wrong} do not set the user-closed flag correctly")
            failures.append("the user-closed flag is not maintained")
        else:
            print("  OK  only Disconnect sets the flag, Connect and Reopen clear it")

        # And the probe must be silent when the glasses really are gone, or a
        # genuinely unplugged pair fills the log once every poll interval.
        reopen = re.search(r"def _try_reopen\(\).*?(?=\n\ndef |\n\nclass )",
                           main_src, re.S).group(0)
        if "except RayNeoError" not in reopen or "return False" not in reopen:
            print("  FAIL the reconnect probe raises or reports when the "
                  "glasses are absent")
            failures.append("an absent device makes the reconnect probe complain")
        elif "logger" in reopen:
            print("  FAIL the reconnect probe logs on every attempt")
            failures.append("the reconnect probe is noisy")
        else:
            print("  OK  an absent device is probed for quietly")

        # And Connect must not depend on the handle being clean to begin with.
        conn = re.search(r"    async def connect\(.*?\n(?=\n    async def |\Z)",
                         main_src, re.S).group(0)
        if not re.search(r"for attempt in \(1, 2\)", conn) or \
                conn.count("refresh_device_info") < 1:
            print("  FAIL Connect still gives up on its first attempt")
            failures.append("connect does not retry through a stale handle")
        else:
            print("  OK  Connect retries once, so one press always suffices")

        # An unanswered poll is the thing that blocks writes, so it has to say
        # so. Without it the only symptom was brightness taking 262 ms with no
        # trace of why, which is what this log analysis kept having to infer.
        #
        # Matched on the message that is actually emitted, word for word, not
        # on the word "unanswered" anywhere in the function: that word is in the
        # explanatory comment too, so a looser guard passed with the log line
        # deleted. The line is worthless unless it says the device did not
        # answer, so that is part of what is required.
        poll_src = inspect.getsource(R.RayNeoDevice.poll_state)
        if not re.search(r'_note\(\s*\n?\s*f?"poll: device info unanswered', poll_src):
            print("  FAIL an unanswered poll reports nothing")
            failures.append("an unanswered poll is silent")
        else:
            print("  OK  an unanswered poll logs how long it held the lock")

        # And the poll itself should not give the device a single chance.
        poll_src = inspect.getsource(R.RayNeoDevice.poll_state)
        if poll_src.count("CMD_ACQUIRE_DEVICE_INFO") < 2:
            print("  FAIL the poll still gives the device one chance only")
            failures.append("poll does not retry an unanswered query")
        else:
            print("  OK  the poll retries an unanswered query once")

        # The poll must be patient. Its first attempt was briefly shortened to
        # 60 ms to chase a bottleneck that turned out to be the plugin bridge,
        # and it cost a false disconnect: a -7 wedge made one poll miss, the
        # short query made the next two miss too, and three strikes declared the
        # glasses gone five seconds before a write answered in 2 ms. The panel
        # told the user "not connected" while the glasses were sitting there
        # working.
        short = re.search(r"POLL_QUERY_TIMEOUT_MS = (\d+)", inspect.getsource(R))
        retry = re.search(r"POLL_QUERY_RETRY_MS = (\d+)", inspect.getsource(R))
        if not short or int(short.group(1)) < 150:
            print(f"  FAIL the poll gives up after "
                  f"{short.group(1) if short else '?'} ms, so a busy panel "
                  f"reads as a disconnect")
            failures.append("the poll gives up too early to avoid a false disconnect")
        elif not retry or int(retry.group(1)) < 150:
            print("  FAIL the poll's retry is not patient enough either")
            failures.append("the poll retry is too short")
        else:
            print(f"  OK  the poll waits {short.group(1)} ms, then retries "
                  f"for {retry.group(1)} ms -- patient enough that a busy "
                  f"panel is not read as a disconnect")
    except Exception as exc:
        import traceback as _tb
        if os.environ.get("SMOKE_DEBUG"):
            _tb.print_exc()
        print(f"  FAIL {exc}")
        failures.append(f"polling pause check failed: {exc}")

    print("\n=== log dump format ===")
    # Three dumps for three commands, previously rendered three different ways:
    # the status report logged a bare "response" with no command named, and the
    # capability dump carried its own hex loop. The 0xE0 dump also cost two
    # transactions, because it called capabilities() after already having the
    # reply.
    try:
        if str(plugin_dir / "py_modules") not in sys.path:
            sys.path.insert(0, str(plugin_dir / "py_modules"))
        R = importlib.import_module("rayneo")

        # format_frame must name the frame; an optional default is how the
        # status report ended up logged as an anonymous "response".
        spec = inspect.getfullargspec(R.format_frame)
        if spec.defaults:
            print("  FAIL format_frame's label has a default, so a dump can be "
                  "logged without naming its frame")
            failures.append("format_frame label is optional")
        elif "label" not in spec.args:
            print("  FAIL format_frame has no label parameter at all")
            failures.append("format_frame cannot name a frame")
        else:
            print("  OK  format_frame requires a label naming the frame")

        dev = R.RayNeoDevice()
        dev._handle = object()
        dev._info = R.DeviceInfo(connected=True, device_type=65)
        dev.open = lambda: True
        dev.refresh_device_info = lambda: dev._info
        sent = []
        e0 = bytearray(64)
        e0[0], e0[8], e0[9] = 0x99, 0xE0, 1
        e3 = bytearray(64)
        e3[0], e3[8] = 0x99, 0xE3
        e3[R.STATUS_LUMINANCE_INDEX] = 8
        e3[R.STATUS_AUDIO_TUBE] = 1
        dev.send = lambda cmd, *a, **k: (sent.append(cmd),
                                         bytes(e0) if cmd == R.CMD_GET_FUNC_SUPPORT
                                         else bytes(e3))[1]

        before = len(sent)
        cap = dev.capability_dump()
        cost = len(sent) - before
        if cost != 1:
            print(f"  FAIL the capability dump cost {cost} transactions, "
                  f"expected 1")
            failures.append("capability dump queries 0xE0 more than once")
        else:
            print("  OK  the capability dump costs one transaction")

        # Same block shape as the shared dump, so it cannot drift back into a
        # bespoke renderer.
        for name, block in (("capability dump", cap),
                            ("status report", dev.status_dump())):
            if not block.startswith("=====") or "raw" not in block \
                    or "parsed" not in block:
                print(f"  FAIL the standalone {name} is not a standard block")
                failures.append(f"{name} shape differs from the shared dump")
                break
        else:
            print("  OK  the standalone dumps are standard blocks too")

        # One shape for all three: a ===== block, a raw section, a parsed
        # section, both in key = value, blocks in command-id order.
        dump = dev.protocol_dump()
        blocks = [b for b in dump.split("\n\n") if b.strip()]
        titles = [b.splitlines()[0] for b in blocks]
        want = ["===== 0x00 device info =====", "===== 0xE0 capabilities =====",
                "===== 0xE3 status report ====="]
        if titles != want:
            print(f"  FAIL blocks are {titles}, expected {want}")
            failures.append("dump blocks are not uniform or not in order")
        else:
            print("  OK  three blocks, one per command, in command-id order")

        for block, title in zip(blocks, want):
            lines = block.splitlines()
            if not any(ln.strip().startswith("raw") for ln in lines):
                print(f"  FAIL {title} has no raw section")
                failures.append(f"{title} missing a raw section")
                break
            if not any(ln.strip() == "parsed" or ln.strip().startswith("parsed ")
                       for ln in lines):
                print(f"  FAIL {title} has no parsed section")
                failures.append(f"{title} missing a parsed section")
                break
            # A table, not key = value: every response gets a rule under the
            # header and rows beneath it.
            rule = [ln for ln in lines
                    if re.match(r"\s*-{10,}\s*$", ln)]
            if not rule:
                print(f"  FAIL {title}'s parsed section is not a table")
                failures.append(f"{title} parsed section is not a table")
                break
        else:
            print("  OK  every block has a raw section and a parsed table")

        # Each table names the columns it has, so a column cannot silently
        # disappear -- the source column is what makes a field checkable
        # against the raw frame above it.
        for index, (block, header, why) in enumerate((
                (blocks[0], "field", "the parsed state"),
                (blocks[1], "flag", "the capability bitmap"),
                (blocks[2], "off", "the status report"))):
            joined = "\n".join(block.splitlines())
            if not re.search(r"^\s+" + header + r"\s+\w", joined, re.M):
                print(f"  FAIL the table for {why} has no header row")
                failures.append(f"{why} table lost its header")
                break
        else:
            print("  OK  all three tables name their columns")

        if "cmd:off" not in blocks[0] or not re.search(
                r"^\s+\w+\s+\S+\s+00:[0-9A-Fa-f]{2}\s*$", blocks[0], re.M):
            print("  FAIL the parsed state no longer shows where each value "
                  "came from")
            failures.append("parsed state lost its source column")
        else:
            print("  OK  the parsed state names the byte each value came from")

        # The source column is only worth reading if it agrees with the offset
        # constants -- which is the invariant that stops it drifting back into
        # the inferred values this file exists to correct.
        origin = R.DEVICE_INFO_FIELD_ORIGIN
        must = {
            "deviceType": (0x00, 0x15),
            "firmwareVersion": (0x00, R.DEVICE_INFO_FIRMWARE_VERSION),
            "firmwareBuild": (0x00, R.DEVICE_INFO_BUILD_DATE),
            "maxVolume": (0x00, R.DEVICE_INFO_MAX_VOLUME),
            "frameRate": (0x00, R.DEVICE_INFO_FRAME_RATE),
            "luminanceValue": (0x00, R.DEVICE_INFO_LUMINANCE_VALUE),
            "luminance": (0xE3, R.STATUS_LUMINANCE_INDEX),
            "audioMode": (0x00, 0x2D),
            "hdrMode": (0xE3, R.STATUS_HDR_MODE),
            "hdrEnabled": (0xE3, R.STATUS_HDR_ENABLED),
            "sceneMode": (0xE3, R.STATUS_PANEL_COLOR_PARAMS),
            "colorEnhance": (0xE3, R.STATUS_COLOR_ENHANCE),
            "audioTube": (0xE3, R.STATUS_AUDIO_TUBE),
            "screenSize": (0xE3, R.STATUS_SCREEN_SIZE),
        }
        wrong = {k: (origin.get(k), v) for k, v in must.items()
                 if origin.get(k) != v}
        if wrong:
            for k, (got, want) in sorted(wrong.items()):
                print(f"  FAIL {k} claims {got}, the offset constants say {want}")
            failures.append("the source column disagrees with the constants")
        else:
            print(f"  OK  the source column agrees with all "
                  f"{len(must)} offset constants")

        # And every field the panel shows must have a source, or its row would
        # print a blank where the byte should be.
        keys = set(R.DeviceInfo(device_type=65).to_dict())
        missing = sorted(keys - set(origin) - {"raw"})
        if missing:
            print(f"  FAIL no source recorded for {missing}")
            failures.append("a parsed field has no recorded source")
        else:
            print(f"  OK  all {len(keys) - 1} parsed fields have a recorded source")

        # The parsed decode must not live in a block of its own any more; it
        # used to sit in "state (parsed)" ahead of 0x00, which broke the
        # one-block-per-command order.
        if "===== state (parsed) =====" in dump:
            print("  FAIL the parsed decode is still a separate block")
            failures.append("parsed state is not inside the 0x00 block")
        else:
            print("  OK  the parsed decode sits inside its own command's block")

        # Compared once the state has settled, not on the first pass: reading the
        # status report folds it into the state, so two dumps in a row legitimately
        # differ the first time and must not afterwards.
        dev.protocol_dump()
        if dev.snapshot() != dev.protocol_dump():
            print("  FAIL snapshot and the connect dump render differently")
            failures.append("snapshot and connect dump differ")
        else:
            print("  OK  snapshot renders identically to the connect dump")

        # All dumps must share one renderer, or they drift apart again.
        own_hex = [
            name for name in ("capability_dump", "status_dump")
            if "chr(b) if 32 <= b < 127"
            in inspect.getsource(getattr(R.RayNeoDevice, name))
        ]
        if own_hex:
            print(f"  FAIL {own_hex} carry their own hex loop instead of "
                  f"format_frame")
            failures.append("a dump reimplements the hex rendering")
        else:
            print("  OK  every dump renders frames through format_frame")

        main_src = (plugin_dir / "main.py").read_text()
        stale = [t for t in ('"capability dump 0xE0"', '"status report 0xE3"',
                             '"0x00 device info (raw)"', '"device info (parsed)"')
                 if t in main_src]
        if stale:
            print(f"  FAIL main.py still uses old dump titles: {stale}")
            failures.append("dump titles are inconsistent")
        else:
            print("  OK  no old dump titles left in main.py")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"log dump format check failed: {exc}")

    print("\n=== enum controls update without the poll ===")
    try:
        if str(plugin_dir / "py_modules") not in sys.path:
            sys.path.insert(0, str(plugin_dir / "py_modules"))
        R = importlib.import_module("rayneo")

        # Every write must reach the frontend without waiting out POLL_INTERVAL.
        # It used not to: only the commands whose reply happened to be folded
        # into the state updated at once, which left screen size, scene mode and
        # picture quality looking broken while the sliders felt fine.
        main_src = (plugin_dir / "main.py").read_text()
        run = re.search(r"    async def _run\(.*?\n(?=\n    async def |\n    # --|\Z)",
                        main_src, re.S).group(0)
        if "_safe_emit(" not in run:
            print("  FAIL a completed write publishes nothing, so every enum "
                  "waits for the poll")
            failures.append("writes do not publish their result")
        elif "before = _state_dict" not in run:
            print("  FAIL the publish cannot tell whether anything changed")
            failures.append("write publish has no before/after comparison")
        else:
            print("  OK  a completed write publishes the device's own state")

        # It must publish what the glasses report, not what was asked for. A
        # refused change has to leave the panel showing reality.
        if "after != before" not in run:
            print("  FAIL the publish is not conditional on the state changing")
            failures.append("write publish is unconditional")
        else:
            print("  OK  nothing is published when the device refused the change")

        # And the silent commands must confirm before publishing, rather than
        # echoing the requested value straight back. Driven, not read: the first
        # version of this check grepped the source and passed on code that had
        # the bug.
        settle_dev = R.RayNeoDevice()
        settle_dev._handle = object()
        settle_dev._info = R.DeviceInfo(device_type=65, connected=True,
                                        max_volume=16)
        settle_dev.open = lambda: True
        reads = []
        agree = bytearray(64)
        agree[0], agree[8] = 0x99, 0xE3
        agree[R.STATUS_HDR_MODE] = 1
        settle_dev.read_status_report = lambda: (reads.append(1), bytes(agree))[1]
        settle_dev._settle_then_read(lambda f: f[R.STATUS_HDR_MODE] == 1)
        if len(reads) != 1:
            print(f"  FAIL the read-back took {len(reads)} reads when the device "
                  f"already agreed, expected 1")
            failures.append("settle read has no early exit")
        else:
            print("  OK  the read-back returns after one read once the device "
                  "agrees")

        # Both silent writes must read the device back *and* stop as soon as it
        # agrees. Counted rather than grepped: both paths call read_status_report
        # either way, so only the read count tells them apart.
        for label, setter, offset, value in (
                ("scene mode", "set_scene_mode", R.STATUS_PANEL_COLOR_PARAMS, 3),
                ("picture quality", "set_hdr_mode", R.STATUS_HDR_MODE, 1),
                ("audio mode", "set_audio_mode", R.STATUS_AUDIO_MODE, 2)):
            dev2 = R.RayNeoDevice()
            dev2._handle = object()
            dev2._info = R.DeviceInfo(device_type=65, connected=True,
                                       max_volume=16)
            dev2.open = lambda: True
            frame = bytearray(64)
            frame[0], frame[8] = 0x99, 0xE3
            frame[offset] = value
            polls = []
            dev2.read_status_report = lambda: (polls.append(1), bytes(frame))[1]
            dev2.send = lambda *a, **k: None
            arg = {"scene mode": "reading", "picture quality": "aihdr",
                   "audio mode": "surround"}[label]
            getattr(dev2, setter)(arg)
            if not polls:
                print(f"  FAIL {label} publishes without reading the device back")
                failures.append(f"{label} is not confirmed before publishing")
            elif len(polls) != 1:
                print(f"  FAIL {label} took {len(polls)} reads although the "
                      f"device already agreed, expected 1")
                failures.append(f"{label} does not short-circuit on agreement")
            else:
                print(f"  OK  {label} reads back once and publishes on agreement")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"enum update check failed: {exc}")

    # Every dropdown must show the choice at once. Five of the six panel commands
    # draw no reply, so the device cannot confirm faster than a status-report
    # round trip -- scene mode is three spaced frames plus a settle read. That
    # delay is why the scene, picture-quality and sound-mode dropdowns lagged
    # while screen size, which replies, did not.
    print("\n=== dropdowns move under the thumb ===")
    try:
        content = (plugin_dir / "src" / "Content.tsx").read_text()
        hooks = (plugin_dir / "src" / "hooks.ts").read_text()

        if "useChoice" not in hooks:
            print("  FAIL there is no optimistic choice hook")
            failures.append("no optimistic choice hook")
        else:
            print("  OK  an optimistic choice hook exists")

        dropdowns = {
            "displayMode": "setDisplayMode", "screenSize": "setScreenSize",
            "sceneMode": "setSceneMode", "hdrMode": "setHdrMode",
            "audioMode": "setAudioMode",
        }
        empty = []
        for key, api in dropdowns.items():
            # The optimistic callback is the second argument of apply().
            m = re.search(r'apply\("' + key + r'",\s*\(\)\s*=>\s*(\w+)',
                          content)
            if not m or m.group(1) == "{}":
                empty.append(key)
        if empty:
            print(f"  FAIL {empty} show nothing until the device answers")
            failures.append(f"dropdowns not optimistic: {empty}")
        else:
            print(f"  OK  all {len(dropdowns)} dropdowns move immediately")

        # An override that never expires would sit there looking accepted after
        # the device refused the change.
        ttl = re.search(r"OPTIMISTIC_TTL_MS = (\d+)", hooks)
        if not ttl:
            print("  FAIL the optimistic override has no expiry")
            failures.append("optimistic choice never expires")
        else:
            print(f"  OK  the override expires ({ttl.group(1)} ms) if nothing "
                  f"arrives")

        # Whether a change landed is the backend's answer, not the frontend's.
        # The frontend once tried to decide it with an effect on `reported`, and
        # that effect never ran: 19 picks, 0 confirmations, 19 expiries in one
        # session. So the panel must not claim to know, and the authoritative
        # WARN has to exist on the backend for it to come from.
        for banned, why in ((r"`\$\{name\} confirmed`", "a confirmation log"),
                            (r"`\$\{name\} expire`", "an expiry log")):
            if re.search(banned, hooks):
                print(f"  FAIL the frontend still emits {why}")
                failures.append(why)
                break
        else:
            print("  OK  the frontend reports latency only, never success")

        backend = inspect.getsource(R.RayNeoDevice)
        if not re.search(r"WARN (scene mode|audio mode|picture quality)", backend):
            print("  FAIL nothing authoritative says whether a change landed")
            failures.append("no backend record of a refused change")
        else:
            print("  OK  the backend WARNs when a change did not land")

        # And it must fall back to what the glasses report, never to a guess.
        if "choice ?? reported ?? fallback" not in hooks:
            print("  FAIL the displayed value is not the reported one")
            failures.append("optimistic choice does not defer to the device")
        else:
            print("  OK  the reported value always wins once it arrives")

        # Without a timing note from the frontend there is no way to tell a slow
        # write from a slow render, and "does not feel responsive" is exactly the
        # symptom that hides from the backend log.
        api_src = (plugin_dir / "src" / "api.ts").read_text()
        dev_src = (plugin_dir / "src" / "useDevice.ts").read_text()
        if "log_frontend_message" not in api_src:
            print("  FAIL there is no frontend timing channel")
            failures.append("frontend cannot report its own timing")
        elif "backend confirmed in" not in dev_src:
            print("  FAIL nothing reports how long a write took")
            failures.append("write latency is not reported")
        elif "logUiTiming" not in hooks:
            print("  FAIL the panel does not record what was chosen")
            failures.append("the chosen value is not logged")

        # libusb error -4 is NO_DEVICE, not a permissions problem. Saying
        # "Check udev permissions" for every failure sent the reader after udev
        # rules when the glasses had simply been unplugged mid-command -- the
        # exact thing that happened in the log this guard came from.
        if not re.search(r"rc == LIBUSB_ERROR_ACCESS", inspect.getsource(R)):
            print("  FAIL every libusb open failure is blamed on udev "
                  "permissions, including -4 (NO_DEVICE)")
            failures.append("a missing device is reported as a permissions fault")
        elif not re.search(r"Reconnect the glasses", inspect.getsource(R)):
            print("  FAIL the device-gone case has no advice of its own")
            failures.append("no guidance for an unplugged device")
        else:
            print("  OK  only -3 is blamed on udev; -4 says reconnect")

        # A refusal must not be logged as a confirmation. The backend answers a
        # failed write with {"ok": false} instead of raising, so awaiting it
        # resolved normally and the panel reported success for writes that never
        # happened -- five of them, up to 17.6 s each, every one of which had
        # failed because the cable was pulled mid-command.
        #
        # Checked as a branch on the result, not on the word "REFUSED": a guard
        # that only looked for the new log text would still pass if the ok check
        # were deleted and only the message kept.
        apply_body = re.search(
            r"const apply = useCallback\(.*?\n    \[track\],\n  \);",
            dev_src, re.S)
        if not apply_body:
            print("  FAIL apply() could not be located")
            failures.append("apply() is not where the guard expected it")
        elif not re.search(r"\.ok === false", apply_body.group(0)):
            print("  FAIL the result's ok flag is never checked, so a failed "
                  "write is logged as confirmed")
            failures.append("a refused write is reported as a confirmation")
        elif not re.search(r"REFUSED", apply_body.group(0)):
            print("  FAIL a refused write is not recorded distinctly")
            failures.append("a refusal is not distinguishable in the log")
        elif "setError(why)" not in apply_body.group(0):
            print("  FAIL a refusal never reaches the panel")
            failures.append("a refused write is not shown to the user")
        else:
            print("  OK  a refused write is reported as refused, not confirmed")

        # The first read-back probe is what decides whether the user waits at
        # all. A flat 200 ms before it was dead time on a write that lands in
        # under a millisecond. The tail must keep backing off, because a scene
        # change genuinely can take a moment.
        delays = re.search(r"SETTLE_PROBE_DELAYS = \(([^)]*)\)", inspect.getsource(R))
        loop = re.search(r"for delay in (\w+):", inspect.getsource(R))
        if not delays or not loop or loop.group(1) != "SETTLE_PROBE_DELAYS":
            print("  FAIL the read-back does not back off through "
                  "SETTLE_PROBE_DELAYS")
            failures.append("settle probes do not use the backoff table")
        else:
            values = [float(v) for v in delays.group(1).split(",")]
            if values[0] > 0.1:
                print(f"  FAIL the first probe waits {values[0] * 1000:.0f} ms")
                failures.append("the first read-back probe is too slow")
            elif values != sorted(values):
                print(f"  FAIL the probe delays do not increase: {values}")
                failures.append("settle probes do not back off")
            else:
                print(f"  OK  the first probe waits {values[0] * 1000:.0f} ms, "
                      f"then backs off over {len(values)} probes")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"dropdown optimism check failed: {exc}")

    print("\n=== endpoint resilience (behavioural) ===")
    # A panel write must survive a halt, a scene change must not be sent as a
    # burst, and the per-report noise must actually be filtered. Each of these is
    # driven rather than read: the first versions of these checks were source
    # greps, and every one of them passed on code that had the bug.
    try:
        if str(plugin_dir / "py_modules") not in sys.path:
            sys.path.insert(0, str(plugin_dir / "py_modules"))
        R = importlib.import_module("rayneo")

        # A write that hits a halt must be cleared and retried.
        dev = R.RayNeoDevice()
        dev._handle = object()
        dev._info = R.DeviceInfo(device_type=65, connected=True, max_volume=16)
        dev.open = lambda: True
        dev.refresh_device_info = lambda: dev._info
        calls, halts = [], []
        dev.clear_halt = lambda: halts.append(1)

        def flaky_send(command, value=0, payload=b"", expect_response=True,
                       timeout_ms=400, trace=True):
            calls.append(command)
            if len(calls) == 1:
                raise R.RayNeoError("USB write failed (libusb error -7)")
            return None

        dev.send = flaky_send
        dev._write_panel(R.CMD_SET_HDR_MODE, 1)
        if len(calls) != 2 or not halts:
            print(f"  FAIL a halted write was not cleared and retried "
                  f"(sends={len(calls)}, halts={len(halts)})")
            failures.append("panel write does not recover from a halt")
        else:
            print("  OK  a halted write is cleared and retried")

        # Two frames differing only in the checksum and counter must read as
        # no change at all -- otherwise every offset looks like every
        # setting, which is what the first calibration run reported. Driven
        # through calibrate_status, because re-implementing the filter here
        # would test this file rather than the code.
        drift = [0]

        def noisy_frame():
            row = bytearray(64)
            row[0x08] = 0xE3
            row[0x04] = drift[0] & 0xFF
            row[0x05] = (drift[0] * 7) & 0xFF
            row[0x06] = (drift[0] * 3) & 0xFF
            return bytes(row)

        def noisy_read():
            drift[0] += 1
            row = noisy_frame()
            devn._apply_status_report(row)
            return row

        devn = R.RayNeoDevice()
        devn._handle = object()
        devn._info = R.DeviceInfo(device_type=65, connected=True, max_volume=16)
        devn.open = lambda: True
        devn.refresh_device_info = lambda: devn._info
        devn.read_status_report = noisy_read
        # A setter that changes nothing at all: whatever the report shows,
        # only per-report noise moved.
        for name in ("set_screen_size", "set_color_enhance", "set_audio_tube",
                     "set_hdr_mode", "set_high_dynamic"):
            setattr(devn, name, lambda *a, **k: None)
        devn.set_scene_mode = lambda m, save=True: None
        noise_report = "\n".join(devn.calibrate_status())
        leaked = [ln for ln in noise_report.splitlines()
                  if any(f"0x{b:02X}" in ln
                         for b in R.VOLATILE_STATUS_BYTES)]
        if leaked:
            print(f"  FAIL checksum/counter bytes leak into the diff: "
                  f"{leaked[:1]}")
            failures.append("volatile status bytes leak into the diff")
        else:
            print("  OK  a report differing only in checksum/count reads "
                  "as no change")

        # Scene mode sends three frames. They must go out back to back, like the
        # app's own save does -- a gap here is pure latency, and the halt-retry
        # is what covers the endpoint.
        scene_dev = R.RayNeoDevice()
        scene_dev._handle = object()
        scene_dev._info = R.DeviceInfo(device_type=65, connected=True,
                                        max_volume=16)
        scene_dev.open = lambda: True
        scene_dev.refresh_device_info = lambda: scene_dev._info
        scene_dev._calibrating = True     # skip the read-back
        stamps = []
        scene_dev.send = lambda *a, **k: stamps.append(time.monotonic())
        scene_dev.set_scene_mode("movie")
        if len(stamps) != 3:
            print(f"  FAIL scene mode sent {len(stamps)} frames, expected 3")
            failures.append("scene mode sends the wrong number of frames")
        else:
            gaps = [b - a for a, b in zip(stamps, stamps[1:])]
            if max(gaps) > 0.01:
                print(f"  FAIL scene mode pads its frames by "
                      f"{max(gaps) * 1000:.0f} ms")
                failures.append("scene mode frames are padded")
            else:
                print(f"  OK  scene mode sends its 3 frames back to back "
                      f"(max gap {max(gaps) * 1000:.0f} ms)")

        # Brightness is the one setting the official app splits in two, and the
        # half we were missing is the reason the panel caught up late while
        # volume and colour enhance felt immediate. AirApi has setBrightnessIndex
        # (0x09) and saveBrightness (0x0D) as separate calls, dispatched on
        # separate Flutter callbacks, so the app stages on every tick and
        # commits once at the end.
        #
        # They must stay *separate methods* now, not one call emitting both:
        # the whole point of the split is that the stage goes out immediately
        # per tick and the commit is debounced to the end of the gesture.
        #
        # Counted, not grepped: the send hook sees the frames, so the assertion
        # is about what actually goes on the wire.
        lum = R.RayNeoDevice()
        lum._handle = object()
        lum._info = R.DeviceInfo(device_type=65, connected=True, max_volume=16)
        lum.open = lambda: True
        lum.refresh_device_info = lambda: lum._info
        lum.send = lambda *a, **k: None
        sent = []
        lum._write_panel = lambda c, v=0, p=b"": sent.append(c)

        lum.set_brightness(5)
        staged_only = list(sent)
        sent.clear()
        # The commit confirms against the status report; answer with the step it
        # just staged so it can short-circuit after one read.
        agree = bytearray(64)
        agree[0], agree[8] = 0x99, 0xE3
        agree[R.STATUS_LUMINANCE_INDEX] = 5
        lum.read_status_report = lambda: (
            lum._apply_status_report(bytes(agree)) or bytes(agree))
        got = lum.save_brightness()
        committed_only = list(sent)

        if staged_only != [R.CMD_SET_BRIGHTNESS]:
            print(f"  FAIL staging sent {[hex(c) for c in staged_only]}, "
                  f"expected 0x09 alone -- the commit is a separate call")
            failures.append("the brightness stage still carries the commit")
        elif committed_only != [R.CMD_SAVE_BRIGHTNESS]:
            print(f"  FAIL the commit sent {[hex(c) for c in committed_only]}, "
                  f"expected 0x0D alone")
            failures.append("the brightness commit sends the wrong frames")
        elif got != 5:
            print(f"  FAIL the commit reported step {got}, expected the panel's 5")
            failures.append("the brightness commit does not read the panel back")
        else:
            print("  OK  staging sends 0x09 alone, the commit sends 0x0D alone "
                  "and reports the panel's step")

        # A commit the panel refused must report the panel's value, not the
        # requested one. This is the whole reason the reply carries a number:
        # the slider springs back to where the hardware is.
        ref = R.RayNeoDevice()
        ref._handle = object()
        ref._info = R.DeviceInfo(device_type=65, connected=True, max_volume=16)
        ref.open = lambda: True
        ref.send = lambda *a, **k: None
        ref._write_panel = lambda c, v=0, p=b"": None
        ref.set_brightness(5)
        other = bytearray(64)
        other[0], other[8] = 0x99, 0xE3
        other[R.STATUS_LUMINANCE_INDEX] = 3        # panel stayed at 3
        ref.read_status_report = lambda: (
            ref._apply_status_report(bytes(other)) or bytes(other))
        back = ref.save_brightness()
        if back != 3:
            print(f"  FAIL a refused commit reported {back}, expected the "
                  f"panel's 3 so the slider can go back")
            failures.append("a refused brightness commit is reported as accepted")
        else:
            print("  OK  a refused commit reports the panel's step, not the "
                  "requested one")

        # Brightness must stay the *only* two-frame setting. Every other one was
        # re-checked against AirApi and is a single JNI call: SetAudioVolume,
        # SetAudioMode, PanelSetHDRMode, PanelSetColorEnhance, AudioSetTubeMode,
        # SetScreenSize, SwitchTo2D/3DMode. If another one grows a second frame
        # it is either a missing commit half (a bug) or an invented command (also
        # a bug), and either way nobody should have to notice it by eye.
        single = {
            "set_volume": R.CMD_SET_AUDIO_VOLUME,
            "set_audio_mode": R.CMD_SET_AUDIO_MODE,
            "set_hdr_mode": R.CMD_SET_HDR_MODE,
            "set_color_enhance": R.CMD_SET_COLOR_ENHANCE,
            "set_audio_tube": R.CMD_SET_AUDIO_TUBE,
            "set_screen_size": R.CMD_SET_SCREEN_SIZE,
            "set_display_mode": R.CMD_SWITCH_TO_2D,
        }
        for meth, expect in single.items():
            d = R.RayNeoDevice()
            d._handle = object()
            d._info = R.DeviceInfo(device_type=65, connected=True, max_volume=16)
            d.open = lambda: True
            frames = []
            d._write_panel = lambda c, v=0, pl=b"": frames.append(c)
            d.send = lambda *a, **k: frames.append(a[0])
            arg = {"set_volume": 3, "set_audio_mode": "standard",
                   "set_hdr_mode": "sdr", "set_color_enhance": True,
                   "set_audio_tube": True, "set_screen_size": "medium",
                   "set_display_mode": "2d"}[meth]
            try:
                getattr(d, meth)(arg)
            except Exception as exc:
                print(f"  FAIL {meth} raised {exc}")
                failures.append(f"{meth} could not be exercised")
                continue
            # Reads do not count. Audio mode and picture quality confirm
            # themselves against the 0xE3 status report, so they put one write
            # plus several reads on the wire; what must stay at one is the write.
            writes = [f for f in frames
                      if f not in (R.CMD_STATUS_REPORT,
                                   R.CMD_ACQUIRE_DEVICE_INFO_2)]
            if len(writes) != 1:
                print(f"  FAIL {meth} sends {len(writes)} writes "
                      f"{[hex(f) for f in writes]}; the app sends one call")
                failures.append(f"{meth} is no longer a single-write change")
        else:
            print(f"  OK  the other {len(single)} settings stay single-frame, "
                  f"as the app has them")

        # Recovering on failure is too late. A silent command never gets a reply,
        # the endpoint halts after three of them, and the write that trips the halt
        # still succeeds -- so it is the *next* transfer that fails, after sitting
        # through _bulk_write's 200 ms timeout, and that cost lands on whatever the
        # user does next. Measured: all 32 -7s followed a 0x09 or 0x0D and none
        # followed a 0x50, which does answer and was written 85 times without
        # faulting once.
        wp = inspect.getsource(R.RayNeoDevice._write_panel)
        after = wp.rindex("self.send(command, value, payload, expect_response=False)")
        tail = wp[after:]
        if not re.search(r"^\s+self\.clear_halt\(\)\s*$", tail, re.M):
            print("  FAIL the halt is only cleared after a failure, so the next "
                  "transfer still pays the 200 ms timeout")
            failures.append("silent writes wedge the endpoint until a write fails")
        elif wp.count("self.clear_halt()") < 2:
            print("  FAIL the halt is not also cleared proactively, so three "
                  "unanswered writes can still accumulate")
            failures.append("silent writes can still wedge the endpoint")
        else:
            print("  OK  silent writes clear the halt before the third can "
                  "accumulate")

        # 0x0D wedges the interrupt endpoint: 16 of 18 commits were followed
        # within ~460 ms by a -7 on the next transfer, and every brightness write
        # after a commit cost 468-654 ms while _bulk_write sat through its 200 ms
        # timeout. The halt has to be cleared inside the commit, where the
        # debounce means nobody is waiting -- not left to leak onto the next drag.
        src = inspect.getsource(R.RayNeoDevice.save_brightness)
        clear = src.find("self.clear_halt()")
        commit_send = src.find("CMD_SAVE_BRIGHTNESS")
        read = src.find("read_status_report()")
        if clear < 0:
            print("  FAIL the commit does not clear the halt it causes")
            failures.append("the brightness commit leaves the endpoint wedged")
        elif clear < commit_send:
            print("  FAIL the halt is cleared before the commit is sent")
            failures.append("the commit clears the halt at the wrong point")
        elif read < 0:
            print("  FAIL the commit never reads the panel back")
            failures.append("the brightness commit does not verify")
        elif clear > read:
            print("  FAIL the halt is cleared after the read-back, so the "
                  "read is the thing that times out")
            failures.append("the halt is cleared too late")
        else:
            print("  OK  the commit clears the halt before reading back")

        # A commit frame that went out must never be reported as refused just
        # because the *check* failed. 16 "REFUSED the commit" lines, every one
        # right after a successful TX 0x0D, is what that mistake looks like.
        if "except RayNeoError" not in src:
            print("  FAIL a failed read-back aborts the whole commit")
            failures.append("a sent commit is reported as refused")
        elif "could not be read back" not in src:
            print("  FAIL a commit that sent but could not be read back is "
                  "silent")
            failures.append("an unverified commit says nothing")
        else:
            print("  OK  a sent-but-unverified commit is named, not refused")

        # And the frontend must not claim success after refusing: it logged both
        # "REFUSED the commit" and "committed in 721 ms" for the same call.
        hk2 = (plugin_dir / "src" / "hooks.ts").read_text()
        settled = re.search(r"if \(!settle\(r\)\) return;", hk2)
        if not settled:
            print("  FAIL the panel logs a commit as done even after refusing "
                  "it")
            failures.append("a refused commit is also logged as committed")
        else:
            print("  OK  a refused commit is not also logged as committed")

        # volumeLimit is an index; maxVolume is a count. Falling back to
        # maxVolume raw gave the slider a step the device may not have, silently,
        # on first paint and after a disconnect where volumeLimit is null -- so
        # the ceiling was right most of the time and wrong occasionally, which
        # reads as "the maximum is unreliable" rather than as an off-by-one.
        if re.search(r"volumeLimit\s*\?\?\s*state\.maxVolume", dev_src):
            print("  FAIL the volume ceiling falls back to maxVolume, which is "
                  "a count rather than an index")
            failures.append("the volume ceiling mixes a count with an index")
        elif not re.search(r"state\.maxVolume - 1", dev_src):
            print("  FAIL maxVolume is never converted from a count to an index")
            failures.append("maxVolume is not reduced before use as a bound")
        else:
            print("  OK  the volume ceiling is always an index")

        # A brightness read-back disagreement must NOT be a warning. The panel
        # stops reflecting brightness past the current display strategy mode's
        # band, so a request above the band is accepted and reported lower --
        # documented behaviour, and the reason the official app's slider appears
        # to fall back on its own. Warning about it made normal operation look
        # like a defect: six and twelve per session, all "asked 11, reports 8".
        commit_src = inspect.getsource(R.RayNeoDevice.save_brightness)
        if "WARN brightness read-back" in commit_src:
            print("  FAIL the brightness commit warns about a disagreement the "
                  "panel is documented to produce")
            failures.append("normal brightness band behaviour is reported as a fault")
        else:
            print("  OK  a brightness band disagreement is not reported as a fault")

        # ...while the same disagreement on the other silent settings *is* a
        # fault, and must still be called out.
        still = [n for n in ("_confirm_scene_mode", "_confirm_audio_mode",
                             "_confirm_hdr_mode")
                 if "WARN" not in inspect.getsource(getattr(R.RayNeoDevice, n))]
        if still:
            print(f"  FAIL {still} no longer report a refused change")
            failures.append("a refused change is silent")
        else:
            print("  OK  scene, audio and picture quality still warn when refused")

        # The audio tube decides the volume ceiling, and three code paths write
        # it: our own write, the echo of that write, and the 0xE3 status report.
        # A disagreement between them shows up as the maximum jumping between 12
        # and 15 for no visible reason -- reported exactly that way, with nothing
        # in the log to say which writer had done it. Every writer now goes
        # through one helper that names the source, so no path can change it
        # silently.
        direct = re.findall(r"\.audio_tube = ", inspect.getsource(R))
        helper = inspect.getsource(R.RayNeoDevice._observe_audio_tube)
        if len(direct) != 1:
            print(f"  FAIL {len(direct)} places assign audio_tube directly; "
                  f"each one is a way for the ceiling to move unlogged")
            failures.append("audio tube state can change without being recorded")
        elif not re.search(
            r"if previous != value:\s*\n\s*self\._note\(", helper
        ):
            # Structural, not a word search: checking only that `_note` appears
            # passed with the call sitting behind `if False:`, since the text was
            # still there. What has to hold is that the note is what a change does.
            print("  FAIL the audio-tube helper can change the state without "
                  "reporting it")
            failures.append("an audio tube change is not reported")
        elif inspect.getsource(R).count("_observe_audio_tube(") < 3:
            print("  FAIL a writer still bypasses the audio-tube helper")
            failures.append("a writer bypasses the audio-tube helper")
        elif "_observe_audio_tube" in re.search(
            r"elif cmd == CMD_SET_AUDIO_TUBE:(.*?)\n        elif ",
            inspect.getsource(R.RayNeoDevice._apply_event_frame), re.S
        ).group(1):
            # The echo is not trustworthy for this field: a write whose own reply
            # times out leaves it to be consumed by the next write, which then
            # reports the value before last. Measured, 26 toggles produced 34
            # transitions and every extra one moved the volume ceiling.
            print("  FAIL the audio tube is driven by the 0x48 echo again, which "
                  "a late reply can turn into the previous value")
            failures.append("a stale 0x48 reply can move the volume ceiling")
        else:
            print("  OK  every audio-tube writer goes through the logging helper")

        # The sliders must not be debounced. They were, for years of logs,
        # because each drag collapsed into one write -- which is exactly when the
        # panel felt like it lagged. The official app writes on every tick.
        dev_src = (plugin_dir / "src" / "useDevice.ts").read_text()
        hooks_src = (plugin_dir / "src" / "hooks.ts").read_text()
        stages = re.findall(r"const stage(\w+) = useCoalesced\(", dev_src)
        if len(stages) != 2:
            print(f"  FAIL {len(stages)} sliders go through useCoalesced, "
                  f"expected both")
            failures.append("a slider is not coalesced")
        else:
            slider = re.search(
                r"const setBrightnessNow = useCallback\((.*?)\n  \);",
                dev_src, re.S)
            body = slider.group(1) if slider else ""
            if "stageBrightness(" not in body:
                print("  FAIL brightness does not stage on every change")
                failures.append("brightness is not staged per tick")
            elif "commitBrightness(" not in body:
                print("  FAIL brightness stages but never commits")
                failures.append("brightness is staged without a commit")
            else:
                print("  OK  both sliders stage on every tick, coalesced")

        # One write in flight, not one per tick. Decky serialises every call, and
        # uncoalesced they queue: six writes went out together and every
        # confirmation came back 1.3-1.5 s later.
        hk = (plugin_dir / "src" / "hooks.ts").read_text()
        coal = re.search(r"export function useCoalesced<A>\(.*?\n\}", hk, re.S)
        if not coal:
            print("  FAIL there is no coalescing helper for the per-tick writes")
            failures.append("per-tick writes are not coalesced")
        elif not re.search(r"if \(!inFlight\.current\) void drain\(\)", coal.group(0)):
            print("  FAIL the coalescer sends regardless of what is in flight")
            failures.append("the coalescer does not bound the queue")
        elif not re.search(r"latest\.current = \{ v \}", coal.group(0)):
            print("  FAIL the coalescer does not remember the newest value")
            failures.append("the coalescer does not keep the newest value")
        elif not re.search(r"await send\(next\.v\)", coal.group(0)):
            print("  FAIL the coalescer does not await the write, so it can "
                  "never know one is in flight")
            failures.append("the coalescer does not await its write")
        else:
            print("  OK  the coalescer keeps one write in flight and the "
                  "newest value")

        # And it must re-send when the in-flight write finishes, or the value
        # under the user's finger at the end of a drag is never written.
        if not re.search(r"alive\.current\) void drain\(\);", coal.group(0)):
            print("  FAIL the coalescer drops the value queued behind an "
                  "in-flight write instead of sending it")
            failures.append("the last value of a drag is never written")
        else:
            print("  OK  a value queued behind an in-flight write is sent "
                  "afterwards")

        # The commit must not be the slow thing in the room. It runs while the
        # user may start dragging again, and every stage in that window queues
        # behind it: the full 2.2 s backoff measured 1874 ms and was most of
        # why the slider's median round trip went from 515 ms to 1556 ms.
        delays = re.search(r"BRIGHTNESS_CONFIRM_DELAYS = \(([^)]*)\)",
                           inspect.getsource(R))
        if not delays:
            print("  FAIL the commit's read-back schedule is missing")
            failures.append("the brightness commit has no bounded read-back")
        else:
            probes = [x for x in re.findall(r"[\d.]+", delays.group(1))]
            worst = sum(float(x) for x in probes)
            if len(probes) > 2:
                print(f"  FAIL the commit probes {len(probes)} times, "
                      f"worst case {worst:.2f} s")
                failures.append("the brightness commit blocks for too long")
            elif worst > 0.6:
                print(f"  FAIL the commit can block for {worst:.2f} s")
                failures.append("the brightness commit blocks for too long")
            else:
                print(f"  OK  the commit reads back {len(probes)} times, at "
                      f"most {worst:.2f} s")

        # ...and the commit must be debounced, because that is what stands in for
        # the app's "on release". The panel has no release event.
        commit_ms = re.search(r"BRIGHTNESS_COMMIT_MS = (\d+)", dev_src)
        settle = re.search(r"const SLIDER_SETTLE_MS = (\d+)", dev_src)
        if not commit_ms or not settle:
            print("  FAIL the commit delay or settle window is missing")
            failures.append("the brightness commit is not timed")
        elif not (150 <= int(commit_ms.group(1)) <= 1000):
            print(f"  FAIL the commit delay is {commit_ms.group(1)} ms")
            failures.append("the brightness commit delay is not a release stand-in")
        elif int(commit_ms.group(1)) >= int(settle.group(1)):
            print("  FAIL the commit can land after the settle window closes, "
                  "so its read-back would be treated as a stale poll report")
            failures.append("the brightness commit outlives the settle window")
        elif "useSettledCommit" not in dev_src:
            print("  FAIL the commit is not debounced")
            failures.append("the brightness commit fires on every tick")
        else:
            print(f"  OK  the commit waits {commit_ms.group(1)} ms after the "
                  f"last tick, inside the {settle.group(1)} ms settle window")

        # The read-back has to reach the slider even though incoming state is
        # ignored mid-drag, or a refused change leaves the thumb on a value the
        # panel never took.
        if "useSettledCommit" in dev_src and \
                "onConfirmed" not in hooks_src:
            print("  FAIL the commit's answer never reaches the slider")
            failures.append("a refused brightness change is not shown to the user")
        elif "useSettledCommit" in hooks_src and \
                "onConfirmed(r?.luminance)" not in hooks_src:
            print("  FAIL the commit does not hand the panel's step on")
            failures.append("the commit's read-back is discarded")
        else:
            print("  OK  a refused commit puts the slider back")

        # And the save must not be mistaken for a command we have measured.
        # UNACKNOWLEDGED_COMMANDS is guarded against the TX trace, so a frame
        # that has never been sent does not belong in it: that would be claiming
        # a reply rate nobody has observed.
        if R.CMD_SAVE_BRIGHTNESS in R.UNACKNOWLEDGED_COMMANDS:
            print("  FAIL 0x0D is listed as never acknowledging, but it has "
                  "never been sent, so nobody has measured it")
            failures.append("an unmeasured command is claimed to be silent")
        else:
            print("  OK  0x0D stays out of the measured silent-command table")

        # A second choice made while the first is still settling must not be
        # reported as the device refusing the first. The silent commands have no
        # reply, so the status report is the only evidence -- and it reports
        # whatever was written *last*. Without a token the panel invented a
        # device fault out of a quick user: picking 电影 then 护眼 produced two
        # WARNs claiming byte 0x12 was "probably not the scene-mode byte", when
        # it was precisely the value just written.
        CONFIRM = {"set_scene_mode": "_confirm_scene_mode",
                   "set_hdr_mode": "_confirm_hdr_mode",
                   "set_audio_mode": "_confirm_audio_mode"}
        for label, setter, first, second, offset in (
                ("scene mode", "set_scene_mode", "movie", "eyeProtection",
                 R.STATUS_PANEL_COLOR_PARAMS),
                ("picture quality", "set_hdr_mode", "sdr", "aihdr",
                 R.STATUS_HDR_MODE),
                ("audio mode", "set_audio_mode", "standard", "whisper",
                 R.STATUS_AUDIO_MODE)):
            ov = R.RayNeoDevice()
            ov._handle = object()
            ov._info = R.DeviceInfo(device_type=65, connected=True,
                                    max_volume=16)
            ov.open = lambda: True
            ov.send = lambda *a, **k: None
            notes = []
            ov._note = notes.append
            # The device always reports the *second* choice, and the second
            # choice is issued from inside the first one's read-back -- which is
            # exactly what a user changing their mind mid-settle looks like.
            landed = bytearray(64)
            landed[0], landed[8] = 0x99, 0xE3
            landed[offset] = R.SCENE_MODES[second] if setter == "set_scene_mode" \
                else (R.HDR_MODES[second] if setter == "set_hdr_mode"
                      else R.AUDIO_MODES[second])
            triggered = []

            def _read(_t=triggered, _d=ov, _s=setter, _s2=second):
                if not _t:
                    _t.append(1)
                    getattr(_d, _s)(_s2)
                _d._apply_status_report(bytes(landed))
                return bytes(landed)

            ov.read_status_report = _read
            getattr(ov, setter)(first)
            warned = [n for n in notes if n.startswith("WARN")]
            said = [n for n in notes if "not a fault" in n]
            if warned:
                print(f"  FAIL a {label} change made during the previous "
                      f"settle is reported as a device fault: {warned[0][:70]}")
                failures.append(f"{label} invents a fault from a fast user")
            elif not said:
                print(f"  FAIL a superseded {label} read-back is dropped "
                      f"silently, so it looks like nothing was checked")
                failures.append(f"{label} does not say a read-back was superseded")
            else:
                print(f"  OK  a {label} read-back superseded mid-settle is "
                      f"named as such, not called a fault")

            # ...and it must stay silent when the read-back simply agreed,
            # superseded or not. Checking supersession before the agreement test
            # logged "not a fault" for changes that had in fact landed --
            # noise where the log used to be silent.
            #
            # The confirmer is called directly with a stale token rather than
            # through the public setter: the setter takes its own token, so
            # going through it makes every request current by construction and
            # the check passes whatever the code does. Two bumps then token 1 is
            # the only way to actually be superseded.
            ok_dev = R.RayNeoDevice()
            ok_dev._handle = object()
            # audio_tube is set to match the frame below. Left at its None
            # default, the read logs a genuine None -> False transition, which is
            # correct behaviour and nothing to do with what this guard is about --
            # but it would make the "stays silent" assertion fail for the wrong
            # reason, so the device is set up realistically instead.
            ok_dev._info = R.DeviceInfo(device_type=65, connected=True,
                                        max_volume=16, audio_tube=False)
            ok_dev.open = lambda: True
            ok_dev.send = lambda *a, **k: None
            quiet = []
            ok_dev._note = quiet.append
            ok = bytearray(64)
            ok[0], ok[8] = 0x99, 0xE3
            ok[offset] = (R.SCENE_MODES[first] if setter == "set_scene_mode"
                          else (R.HDR_MODES[first] if setter == "set_hdr_mode"
                                else R.AUDIO_MODES[first]))
            ok_dev.read_status_report = lambda: (
                ok_dev._apply_status_report(bytes(ok)) or bytes(ok))
            key = {"set_scene_mode": "sceneMode", "set_hdr_mode": "hdrMode",
                   "set_audio_mode": "audioMode"}[setter]
            ok_dev._begin_confirm(key)
            ok_dev._begin_confirm(key)
            if not ok_dev._superseded(key, 1):
                print(f"  FAIL the {label} silence check is not actually "
                      f"superseded, so it cannot fail")
                failures.append(f"the {label} silence guard is hollow")
                continue
            arg = (first if setter == "set_scene_mode"
                   else (R.HDR_MODES[first] if setter == "set_hdr_mode"
                         else R.AUDIO_MODES[first]))
            getattr(ok_dev, CONFIRM[setter])(arg, 1)
            if quiet:
                print(f"  FAIL a {label} change that landed still logged "
                      f"{quiet[0][:60]!r}")
                failures.append(f"an agreed {label} read-back still speaks up")
            else:
                print(f"  OK  an agreed {label} read-back stays silent")



        # The calibration must ride out a halt rather than dropping probes.
        dev3 = R.RayNeoDevice()
        dev3._handle = object()
        dev3._info = R.DeviceInfo(device_type=65, connected=True, max_volume=16)
        dev3.open = lambda: True
        dev3.refresh_device_info = lambda: dev3._info
        dev3.clear_halt = lambda: None
        value = [0]
        base_frame = bytearray(64)
        base_frame[0x08] = 0xE3

        def encode():
            row = bytearray(base_frame)
            row[R.STATUS_AUDIO_TUBE] = 1 if value[0] else 0
            return bytes(row)

        def read_status():
            row = encode()
            dev3._apply_status_report(row)
            return row

        attempts = []
        def flaky_tube(on):
            attempts.append(1)
            if len(attempts) == 1:
                raise R.RayNeoError("USB write failed (libusb error -7)")
            value[0] = 1 if on else 0
            return encode()

        dev3.read_status_report = read_status
        dev3.set_audio_tube = flaky_tube
        dev3.set_screen_size = lambda s: None
        dev3.set_color_enhance = lambda v: None
        dev3.set_hdr_mode = lambda m: None
        dev3.set_high_dynamic = lambda v: None
        dev3.set_scene_mode = lambda m, save=True: None
        report = "\n".join(dev3.calibrate_status())
        tube_block = _calibration_block(report.splitlines(), "audio tube")
        if not any("0x18" in line and "failed" not in line
                   for line in tube_block):
            print("  FAIL a probe lost to a halt was not retried")
            failures.append("calibration drops a probe after a halt")
        else:
            print("  OK  a probe lost to a halt is retried, not dropped")

        # The official app sends its save frames back to back with nothing in
        # between, so padding ours only ever cost latency. The retry above is
        # what covers the endpoint instead.
        if R.PANEL_FRAME_GAP > 0.01:
            print(f"  FAIL frames are still padded by "
                  f"{R.PANEL_FRAME_GAP * 1000:.0f} ms each")
            failures.append("panel frames are padded for no reason")
        else:
            print(f"  OK  frames go out unspaced, like the app's "
                  f"(gap {R.PANEL_FRAME_GAP * 1000:.0f} ms)")

        # Removing that padding must not have removed the recovery pause too.
        if R.HALT_RECOVERY_SECONDS <= 0:
            print("  FAIL a cleared halt is retried with no pause at all")
            failures.append("halt recovery retries instantly")
        else:
            print(f"  OK  a cleared halt waits "
                  f"{R.HALT_RECOVERY_SECONDS * 1000:.0f} ms before retrying")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"endpoint resilience check failed: {exc}")

    # --- panel grouping ----------------------------------------------------
    # Two main groups, Display and Sound, with volume among the sound controls.
    # Easy to break by accident when a row moves, and the result is a control
    # filed under the wrong heading rather than anything that errors.
    print("\n=== panel grouping ===")
    try:
        content = (plugin_dir / "src" / "Content.tsx").read_text()
        sections = re.findall(r'<PanelSection title="([^"]+)"', content)
        want = ["连接 Connection", "显示 Display", "声音 Sound"]
        if sections != want:
            print(f"  FAIL sections are {sections}, expected {want}")
            failures.append("panel sections are not the expected groups")
        else:
            print("  OK  sections are 连接 / 显示 / 声音")

        # Which controls live in which group, by splitting on the headings.
        chunks = re.split(r'<PanelSection title="[^"]+">', content)
        display = chunks[sections.index("显示 Display") + 1]
        sound = chunks[sections.index("声音 Sound") + 1]
        if 'label="音量"' in display:
            print("  FAIL volume is still filed under Display")
            failures.append("volume is in the wrong group")
        elif 'label="音量"' not in sound:
            print("  FAIL volume is not in the Sound group")
            failures.append("volume is missing from the Sound group")
        else:
            print("  OK  volume lives under Sound, with the effect and the tube")

        wrong = [n for label, n in (("音效", "sound effect"),
                                    ("导音鳍", "sound tube"))
                 if f'label="{label}"' not in sound]
        if wrong:
            print(f"  FAIL {wrong} are not under Sound")
            failures.append("a sound control is in the wrong group")
        else:
            print("  OK  the sound effect and the sound tube are both under Sound")

        if 'description="Sound Effect"' not in sound:
            print("  FAIL the sound effect control lost its English name")
            failures.append("sound effect label is not 音效 / Sound Effect")
        else:
            print("  OK  the sound effect control reads 音效 / Sound Effect")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"panel grouping check failed: {exc}")

    # --- _mutating hygiene -------------------------------------------------
    # The background poller skips every cycle while _mutating is set, so a
    # setter that raises the flag without clearing it stops live updates for
    # the rest of the session. That presents as "the UI only refreshes when I
    # reopen the settings panel", which is very hard to trace back here.
    print("\n=== _mutating hygiene ===")
    try:
        if str(plugin_dir / "py_modules") not in sys.path:
            sys.path.insert(0, str(plugin_dir / "py_modules"))
        rayneo_mod = importlib.import_module("rayneo")
        src = inspect.getsource(rayneo_mod.RayNeoDevice)
        leaks = []
        setters = 0
        for m in re.finditer(
            r"    def ((?:set|save)_\w+)\(.*?\n(?=\n    def |\n    #|\Z)", src, re.S
        ):
            body, name = m.group(0), m.group(1)
            if "_mutating.set()" not in body:
                continue
            setters += 1
            if "_mutating.clear()" not in body and "_apply_probed(" not in body:
                leaks.append(name)
        print(f"  {setters} setters raise _mutating, "
              f"{setters - len(leaks)} clear it")
        if leaks:
            print(f"  FAIL never cleared: {', '.join(leaks)}")
            failures.append(f"_mutating never cleared by: {', '.join(leaks)}")
        else:
            print("  OK  every setter clears _mutating")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"_mutating check failed: {exc}")

    # --- brightness ceiling -----------------------------------------------
    # Fixed at 12 steps. The table has 29 entries, but only indices 0..11 map
    # onto wire values 0..12 -- the band the glasses' UI labels 1-12. Using the
    # table length would offer steps the panel does not render.
    print("\n=== brightness ceiling ===")
    try:
        for _dt in (65, 33, 36, 48):
            _dev = rayneo_mod.RayNeoDevice()
            _dev._info = rayneo_mod.DeviceInfo(device_type=_dt, connected=True)
            _tbl = rayneo_mod.brightness_table(_dt, None)
            _lv = _dev.brightness_levels()
            if _lv == 12:
                print(f"  OK  deviceType={_dt} -> {_lv} steps, wire {list(_tbl[:_lv])}")
            else:
                print(f"  FAIL deviceType={_dt} -> {_lv} steps, expected 12")
                failures.append(f"deviceType {_dt} brightness_levels != 12")
        # A short table must not be padded past its own length.
        _dev._info = rayneo_mod.DeviceInfo(device_type=32, connected=True)
        _lv32 = _dev.brightness_levels()
        if _lv32 == len(rayneo_mod.brightness_table(32, None)):
            print(f"  OK  NXTVIEW short table -> {_lv32} steps")
        else:
            print(f"  FAIL NXTVIEW -> {_lv32}, table has "
                  f"{len(rayneo_mod.brightness_table(32, None))}")
            failures.append("brightness_levels exceeds the short table")

        # Out-of-range requests must clamp rather than send 0xFF.
        sent = []
        _dev = rayneo_mod.RayNeoDevice()
        _dev._info = rayneo_mod.DeviceInfo(device_type=65, connected=True)
        _dev.open = lambda: None
        _dev.send = lambda cmd, value=0, payload=b'', **k: sent.append(value)
        _dev.set_probe_enabled(False)
        _tbl65 = rayneo_mod.brightness_table(65, None)
        for want, expect in ((0, _tbl65[0]), (5, _tbl65[5]), (99, _tbl65[11])):
            sent.clear()
            _dev.set_brightness(want)
            got = sent[0]
            if got == expect:
                print(f"  OK  brightness {want:>2} -> wire byte {got}")
            else:
                print(f"  FAIL brightness {want} -> {got}, expected {expect}")
                failures.append(f"set_brightness({want}) sent {got}")
        if rayneo_mod.BRIGHTNESS_OUT_OF_RANGE not in sent:
            print("  OK  never sends the 0xFF out-of-range sentinel")
        else:
            print("  FAIL sent 0xFF for an over-range request")
            failures.append("set_brightness sent 0xFF instead of clamping")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"brightness ceiling check failed: {exc}")

    # --- frontend log channel ---------------------------------------------
    # React render errors and rejected RPCs surface as red text in the panel
    # and nowhere else. The plugin routes them through log_frontend_error so
    # they reach the Decky log; verify the RPC exists and actually writes.
    print("\n=== frontend log channel ===")
    try:
        logged_lines = []

        class _Decky2:
            class logger:
                @staticmethod
                def info(m):
                    logged_lines.append(("info", m))

                @staticmethod
                def warning(m):
                    logged_lines.append(("warning", m))

                @staticmethod
                def error(m):
                    logged_lines.append(("error", m))

            @staticmethod
            async def emit(event, *args):
                pass

        sys.modules["decky"] = _Decky2
        _spec2 = importlib.util.spec_from_file_location(
            "_rn_main2", str(plugin_dir / "main.py")
        )
        _main2 = importlib.util.module_from_spec(_spec2)
        _spec2.loader.exec_module(_main2)

        _rpcs = {n for n, _f in inspect.getmembers(
            _main2.Plugin, inspect.isfunction) if not n.startswith("_")}
        for need in ("log_frontend_error",):
            if need in _rpcs:
                print(f"  OK  {need} RPC exists")
            else:
                print(f"  FAIL {need} RPC missing")
                failures.append(f"{need} RPC missing")

        asyncio.run(_main2.Plugin().log_frontend_error(
            "render", "TypeError: x is not a function",
            "at Foo (Content.tsx:123)\n    at Bar",
        ))
        if logged_lines and logged_lines[-1][0] == "error":
            _body = logged_lines[-1][1]
            if ("TypeError" in _body and "Content.tsx" in _body
                    and "frontend render" in _body):
                print(f"  OK  error reaches the log: {_body.splitlines()[0]}")
            else:
                print(f"  FAIL log body was {_body[:70]!r}")
                failures.append("log_frontend_error body missing detail")
        else:
            print("  FAIL nothing written to the log")
            failures.append("log_frontend_error wrote nothing")

        # Junk must not take the plugin down: these come from catch handlers.
        logged_lines.clear()
        for junk in (None, 123, {"a": 1}, ["x"], {"a": {"b": [1, 2]}}):
            try:
                asyncio.run(_main2.Plugin().log_frontend_error(
                    "junk", str(junk), None))
            except Exception as exc:
                print(f"  FAIL junk {junk!r} raised {exc}")
                failures.append("log_frontend_error raises on junk input")
                break
        else:
            print("  OK  tolerates junk input without raising")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"frontend log channel check failed: {exc}")

    # --- write clamping ---------------------------------------------------
    # The device can legitimately report a value above the current ceiling
    # (volume 15 while the audio tube caps at 12). Reporting it is correct --
    # that is the device's real state -- but a controlled slider handed an
    # out-of-range value clamps itself and fires onChange, and forwarding that
    # would write the clamp back as if the user had asked, on every poll.
    # The frontend clamps; the backend must therefore never *write* a value it
    # would refuse, or the two disagree.
    print("\n=== write clamping ===")
    try:
        for _tube, _want, _expect in (
            (False, 15, 15), (False, 20, 15),
            (True, 15, 12), (True, 12, 12), (True, 0, 0),
        ):
            _dev = rayneo_mod.RayNeoDevice()
            _dev._info = rayneo_mod.DeviceInfo(
                device_type=65, connected=True, audio_tube=_tube,
                max_volume=16,
            )
            _dev.open = lambda: None
            _sent = []
            _dev.send = lambda c, value=0, payload=b'', **k: _sent.append(value)
            _dev.set_probe_enabled(False)
            _dev.set_volume(_want)
            _got = _sent[0]
            if _got == _expect:
                print(f"  OK  tube={str(_tube):<5} volume {_want:>2} -> {_got}")
            else:
                print(f"  FAIL tube={str(_tube):<5} volume {_want} -> {_got}, "
                      f"expected {_expect}")
                failures.append(f"set_volume({_want}) wrote {_got} not {_expect}")
        # A brightness left over from a higher ceiling must clamp on write.
        _dev = rayneo_mod.RayNeoDevice()
        _dev._info = rayneo_mod.DeviceInfo(device_type=65, connected=True,
                                          luminance=28)
        _dev.open = lambda: None
        _sent = []
        _dev.send = lambda c, value=0, payload=b'', **k: _sent.append(value)
        _dev.set_probe_enabled(False)
        _dev.set_brightness(28)
        _tbl = rayneo_mod.brightness_table(65, None)
        if _sent[0] == _tbl[11]:
            print(f"  OK  brightness 28 -> wire {_sent[0]} (top of the band)")
        else:
            print(f"  FAIL brightness 28 -> wire {_sent[0]}, expected {_tbl[11]}")
            failures.append("set_brightness(28) did not clamp to the band top")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"write clamping check failed: {exc}")

    # --- read-back offsets are not double-booked --------------------------
    # hdr_mode used to read u8(0x3D), which the firmware's own reply parser
    # assigns to XrHidDeviceInfo::maxVolume. The symptom was innocuous but
    # misleading: "hdrMode: 16" in the log while the glasses sat in SDR.
    # One offset, one field -- assert no byte is claimed by two names.
    print("\n=== read-back offsets ===")
    try:
        _src = (plugin_dir / "py_modules" / "rayneo.py").read_text()
        _body = _src[_src.index("def _apply_device_info"):]
        _body = _body[:_body.index("    # -- display")]
        claims = {}
        for _m in re.finditer(
            r"(\w+)\s*=\s*(?:u8|bool)\(?\s*(0x[0-9A-Fa-f]+|[A-Z_][A-Z0-9_]*)",
            _body,
        ):
            _field, _tok = _m.group(1), _m.group(2)
            if _tok.startswith("0x"):
                _off = int(_tok, 16)
            else:
                _c = re.search(rf"{_tok}\s*=\s*0x([0-9A-Fa-f]+)", _src)
                if not _c:
                    continue
                _off = int(_c.group(1), 16)
            claims.setdefault(_off, []).append(_field)
        clashes = {off: names for off, names in claims.items() if len(names) > 1}
        if clashes:
            for off, names in sorted(clashes.items()):
                print(f"  FAIL reply[0x{off:02X}] read as {names}")
            failures.append(f"offset clashes: {clashes}")
        else:
            print(f"  OK  {len(claims)} reply offsets, each read once: "
                  + ", ".join(f"0x{o:02X}={n[0]}" for o, n in sorted(claims.items())))

        # 0x3D is maxVolume; nothing else may claim it.
        _max_vol = re.search(r"DEVICE_INFO_MAX_VOLUME\s*=\s*0x([0-9A-Fa-f]+)", _src)
        _off = int(_max_vol.group(1), 16) if _max_vol else None
        if _off is not None and claims.get(_off) == ["reported_max_volume"]:
            print(f"  OK  reply[0x{_off:02X}] is maxVolume, as the firmware says")
        else:
            print(f"  FAIL reply[0x{_off:02X}] claims {claims.get(_off)}")
            failures.append("0x3D is not read as maxVolume")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"read-back offset check failed: {exc}")

    # --- option keys agree across the language boundary -------------------
    # The frontend hard-codes option keys in src/optionKeys.ts so its types stay
    # `as const`, and the backend maps them in py_modules/rayneo.py. Two lists
    # means two sources of truth, so assert they match.
    print("\n=== option keys ===")
    try:
        _ts = (plugin_dir / "src" / "optionKeys.ts").read_text(encoding="utf-8")

        def _ts_keys(name):
            m = re.search(rf"{name} = \[([^\]]*)\]", _ts, re.S)
            if not m:
                return None
            return set(re.findall(r'"([^"]+)"', m.group(1)))

        # An unrecognised screen-size byte must be named, not dropped. Dropping
        # it leaves the panel showing a size the glasses are not set to, which is
        # the one way this field can lie with nothing else wrong. It matters
        # because the app's byte values are not fully accounted for: the app
        # sends `(i / 10) % 10` of a Flutter-chosen number and the firmware
        # passes it through, so 0/1/2 here rests on hardware calibration.
        #
        # Counted, and repeated: a persistent unknown value must be reported
        # once, not on every poll.
        seen = R.RayNeoDevice()
        seen._handle = object()
        seen._info = R.DeviceInfo(device_type=65, connected=True, max_volume=16,
                                  audio_tube=False)
        said = []
        seen._note = said.append
        probe = bytearray(64)
        probe[0], probe[8] = 0x99, 0xE3
        def _report(value):
            probe[R.STATUS_SCREEN_SIZE] = value
            seen._apply_status_report(bytes(probe))
            return len([x for x in said if "screen size" in x])
        if _report(8) != 1 or _report(8) != 1:
            print("  FAIL an unrecognised screen size is not reported exactly "
                  "once")
            failures.append("an unknown screen size is dropped or repeated")
        elif _report(9) != 2:
            print("  FAIL a second unrecognised screen size is not reported")
            failures.append("a changed unknown screen size is not reported")
        elif _report(1) != 2 or seen._info.screen_size != "medium":
            print("  FAIL a known screen size is misread")
            failures.append("a known screen size does not resolve")
        else:
            print("  OK  an unknown screen size is named once, a known one "
                  "resolves")

        # The label maps are keyed by the same option keys, and nothing checked
        # that. A key present in the backend and in optionKeys.ts but missing
        # here renders as `undefined` in the dropdown, which is the kind of thing
        # only a user notices.
        _labels = (plugin_dir / "src" / "labels.ts").read_text(encoding="utf-8")

        def _label_keys(map_name):
            m = re.search(rf"{map_name}[^=]*= \{{(.*?)\}};", _labels, re.S)
            return set(re.findall(r"^\s*(\w+):", m.group(1), re.M)) if m else None

        for label_name, opts_name in (("SCENE_LABELS", "SCENE_KEYS"),
                                      ("AUDIO_MODE_LABELS", "AUDIO_MODE_KEYS"),
                                      ("HDR_MODE_LABELS", "HDR_MODE_KEYS"),
                                      ("SCREEN_SIZE_LABELS",
                                       "SCREEN_SIZE_KEYS")):
            have, want = _label_keys(label_name), _ts_keys(opts_name)
            if have != want:
                print(f"  FAIL {label_name} {sorted(have or [])} != "
                      f"{opts_name} {sorted(want or [])}")
                failures.append(f"{label_name} has no label for every option key")
            else:
                print(f"  OK  every {label_name} key has a {opts_name} entry")

        pairs = [
            ("SCENE_KEYS", "SCENE_MODES"),
            ("AUDIO_MODE_KEYS", "AUDIO_MODES"),
            ("HDR_MODE_KEYS", "HDR_MODES"),
            ("SCREEN_SIZE_KEYS", "SCREEN_SIZES"),
        ]
        if str(plugin_dir / "py_modules") not in sys.path:
            sys.path.insert(0, str(plugin_dir / "py_modules"))
        _rayneo = importlib.import_module("rayneo")
        for ts_name, py_name in pairs:
            ts = _ts_keys(ts_name)
            py = set(getattr(_rayneo, py_name))
            if ts == py:
                print(f"  OK  {ts_name} == {py_name} {sorted(ts)}")
            else:
                print(f"  FAIL {ts_name} {sorted(ts or [])} != "
                      f"{py_name} {sorted(py)}")
                failures.append(f"{ts_name} and {py_name} disagree")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"option key check failed: {exc}")

    # --- event delivery ---------------------------------------------------
    # decky.emit is a coroutine. Calling it without awaiting builds a coroutine
    # that never runs, so the frontend receives nothing and the UI keeps its
    # last fetched state -- no error, no warning, just a plugin that looks
    # frozen. Assert against a stub whose emit is genuinely async.
    print("\n=== event delivery ===")
    try:
        emitted = []

        class _Decky:
            class logger:
                @staticmethod
                def warning(m):
                    pass

                @staticmethod
                def info(m):
                    pass

            @staticmethod
            async def emit(event, *args):
                emitted.append((event, args))

        sys.modules["decky"] = _Decky
        _spec = importlib.util.spec_from_file_location(
            "_rn_main", str(plugin_dir / "main.py")
        )
        _main = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_main)

        async def _go():
            emitted.clear()
            _main._safe_emit({"volume": 5, "volumeLimit": 12})
            await asyncio.sleep(0.05)
            if emitted:
                print("  OK  _safe_emit delivers a payload to the frontend")
            else:
                print("  FAIL _safe_emit delivered nothing "
                      "(decky.emit not awaited?)")
                failures.append("_safe_emit does not deliver events")

            emitted.clear()
            for i in range(50):
                _main._safe_emit({"i": i})
            await asyncio.sleep(0.1)
            if len(emitted) <= 2:
                print(f"  OK  rapid publishes coalesce ({len(emitted)} delivered)")
            else:
                print(f"  FAIL 50 publishes queued {len(emitted)} tasks")
                failures.append("_safe_emit queues unbounded tasks")

        asyncio.run(_go())
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"event delivery check failed: {exc}")

    # --- halted-endpoint recovery ---------------------------------------
    # A halted endpoint stays halted until libusb_clear_halt() is called, and
    # every later transfer then fails instantly -- which presents as "the
    # plugin is connected but nothing works". Both PIPE and TIMEOUT leave it
    # halted; only PIPE was handled originally.
    print("\n=== halted endpoint recovery ===")
    try:
        _lc = importlib.import_module("libusb_ctypes")
        if (_lc.LIBUSB_ERROR_PIPE, _lc.LIBUSB_ERROR_TIMEOUT) == (-9, -7):
            print("  OK  libusb error constants match the enum")
        else:
            print("  FAIL libusb error constants are wrong")
            failures.append("libusb error constants wrong")

        cleared = []

        class _FakeLib:
            class lib:
                rc = -7

                @classmethod
                def libusb_interrupt_transfer(cls, *a):
                    return cls.rc

                @staticmethod
                def libusb_clear_halt(h, ep):
                    cleared.append(ep.value)

        _orig_libusb = rayneo_mod.libusb
        rayneo_mod.libusb = lambda: _FakeLib
        try:
            dev = rayneo_mod.RayNeoDevice()
            dev.open = lambda: None
            dev._handle = 1
            dev._ep_out = 0x02
            dev._out_interrupt = True
            for rc, name in ((-7, "TIMEOUT"), (-9, "PIPE")):
                cleared.clear()
                _FakeLib.lib.rc = rc
                try:
                    dev._bulk_write(b"\x66\x09\x0a", timeout_ms=120)
                except rayneo_mod.RayNeoError:
                    pass
                if cleared:
                    print(f"  OK  {name} (rc={rc}) clears the halt")
                else:
                    print(f"  FAIL {name} (rc={rc}) leaves the endpoint halted")
                    failures.append(f"rc={rc} leaves endpoint halted")
            # A missing device must NOT be papered over with a halt clear.
            cleared.clear()
            _FakeLib.lib.rc = -4
            try:
                dev._bulk_write(b"\x66\x09\x0a", timeout_ms=120)
            except rayneo_mod.RayNeoError:
                pass
            if cleared:
                print("  FAIL NO_DEVICE clears halt (masking a real disconnect)")
                failures.append("NO_DEVICE wrongly clears halt")
            else:
                print("  OK  NO_DEVICE (rc=-4) does not mask a disconnect")
        finally:
            rayneo_mod.libusb = _orig_libusb
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"halt recovery check failed: {exc}")

    # --- USB traffic per change -----------------------------------------
    # Response diffing costs five extra transactions per setting change, which
    # is what pushed the endpoint into a timeout. It must be off by default.
    print("\n=== USB traffic per setting change ===")
    try:
        class _Counting(rayneo_mod.RayNeoDevice):
            def __init__(self):
                super().__init__()
                self.tx = 0
                self.rx = 0
                self.open = lambda: None

            def send(self, *a, **k):
                self.tx += 1
                return None

            def _read_probes(self):
                self.rx += 2
                return None, None

        counts = {}
        for probe in (False, True):
            dev = _Counting()
            dev._info = rayneo_mod.DeviceInfo(device_type=65, connected=True)
            dev.set_probe_enabled(probe)
            dev.set_brightness(10)
            counts[probe] = dev.tx + dev.rx
        print(f"  diffing off: {counts[False]} transactions")
        print(f"  diffing on : {counts[True]} transactions")
        # One, not two. Brightness was briefly one call emitting 0x09 and 0x0D
        # together; splitting them is the whole point now, so a brightness
        # *stage* is a single frame again and the commit is a second RPC.
        if counts[False] == 1:
            print("  OK  a setting change is a single transaction by default "
                  "(the brightness commit is its own call)")
        else:
            print(f"  FAIL default path costs {counts[False]} transactions, "
                  f"expected 1 -- the commit must not ride along with the stage")
            failures.append("the brightness commit is not a separate call")
        # What this section is really about: diffing is off unless asked for.
        # Tied to the measured delta rather than an absolute, so it cannot rot
        # when the frame count changes. Five, not four: the two probe reads are
        # four transactions and the confirming status report is a fifth.
        if counts[True] - counts[False] == 5:
            print("  OK  enabling diffing adds exactly its 5 transactions")
        else:
            print(f"  FAIL diffing changes the cost by "
                  f"{counts[True] - counts[False]}, expected the measured 5")
            failures.append("the diffing toggle has no effect")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"USB traffic check failed: {exc}")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"USB traffic check failed: {exc}")

    # --- project structure ----------------------------------------------
    # A hook in the definePlugin callback throws at runtime and blanks the whole
    # panel, with nothing in the log to explain it. Check it statically.
    print("\n=== project structure ===")
    _struct = subprocess.run(
        [sys.executable, str(plugin_dir / "tools" / "structcheck.py")],
        capture_output=True, text=True,
    )
    print((_struct.stdout or _struct.stderr).rstrip())
    if _struct.returncode != 0:
        failures.append("structcheck.py reported problems")

    # --- no shadowed methods --------------------------------------------
    # Two methods with one name in a class: the second silently wins, so the
    # first never runs. A duplicated _unload() hid the listener teardown that
    # way and no tool in the chain noticed, because the shadowed copy was also
    # valid Python that nothing called.
    print("\n=== duplicate methods ===")
    try:
        # Introspection cannot see this: a shadowed method is gone from the
        # class, so getmembers() only ever returns the survivor. The AST sees
        # both definitions.
        import ast

        for _label, _rel in (("main.py", "main.py"),
                             ("py_modules/rayneo.py", "py_modules/rayneo.py"),
                             ("py_modules/libusb_ctypes.py",
                              "py_modules/libusb_ctypes.py")):
            _tree = ast.parse((plugin_dir / _rel).read_text(encoding="utf-8"))
            _dupes: list = []
            for _node in _tree.body:
                if not isinstance(_node, ast.ClassDef):
                    continue
                _seen: set = set()
                for _item in _node.body:
                    if not isinstance(_item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if _item.name.startswith("__"):
                        continue
                    if _item.name in _seen:
                        _dupes.append(f"{_node.name}.{_item.name} "
                                      f"(line {_item.lineno})")
                    _seen.add(_item.name)
            if _dupes:
                print(f"  FAIL {_label}: shadowed {sorted(set(_dupes))}")
                failures.append(f"{_label} has shadowed methods: {_dupes}")
            else:
                print(f"  OK  {_label}: no shadowed methods")
    except Exception as exc:
        print(f"  FAIL {exc}")
        failures.append(f"duplicate method check failed: {exc}")

    print("\n=== result ===")
    if failures:
        for f in failures:
            print(f"  FAIL {f}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
