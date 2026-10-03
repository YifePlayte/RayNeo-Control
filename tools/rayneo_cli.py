#!/usr/bin/env python3
"""Command-line tool to talk to RayNeo/FFalcon glasses over USB.

Useful for verifying the protocol against real hardware without going through
Decky:

    python3 tools/rayneo_cli.py devices       # list matching USB devices
    python3 tools/rayneo_cli.py interfaces    # show the bulk interface details
    python3 tools/rayneo_cli.py info          # dump the device-info block
    python3 tools/rayneo_cli.py caps          # dump the capability bitmap
    python3 tools/rayneo_cli.py set brightness 7
    python3 tools/rayneo_cli.py set volume 5
    python3 tools/rayneo_cli.py set display-mode 3d
    python3 tools/rayneo_cli.py set scene-mode movie
    python3 tools/rayneo_cli.py set hdr-mode aihdr
    python3 tools/rayneo_cli.py set audio-mode surround
    python3 tools/rayneo_cli.py set audio-tube on
    python3 tools/rayneo_cli.py set color-enhance on
    python3 tools/rayneo_cli.py raw 00 00      # send a raw frame
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "py_modules"))

from rayneo import (  # noqa: E402
    KNOWN_IDS,
    DeviceNotFound,
    RayNeoDevice,
    RayNeoError,
)


def _devices(_args: argparse.Namespace | None = None) -> None:
    """Enumerate without opening: read-only, touches no interface state."""
    import ctypes
    import struct

    from rayneo import DFU_IDS, libusb

    usb = libusb()
    usb.init()
    lst = ctypes.POINTER(ctypes.c_void_p)()
    n = usb.lib.libusb_get_device_list(usb.ctx, ctypes.byref(lst))
    if n < 0:
        raise SystemExit(f"libusb_get_device_list failed: {n}")
    try:
        desc = ctypes.create_string_buffer(18)
        found = False
        print(f"{'bus':>4} {'dev':>4}  {'vid:pid':<10} product")
        for i in range(n):
            if usb.lib.libusb_get_device_descriptor(lst[i], desc) != 0:
                continue
            vid, pid = struct.unpack_from("<HH", desc.raw, 8)
            bus, addr = desc.raw[16], desc.raw[17]
            is_rayneo = (vid, pid) in KNOWN_IDS
            is_dfu = (vid, pid) in DFU_IDS
            if not (is_rayneo or is_dfu):
                continue
            found = True
            tag = "MATCH" if is_rayneo else "DFU (refused)"
            print(f"{bus:>4} {addr:>4}  {vid:04X}:{pid:04X}   {tag}")
        if not found:
            print("  (no RayNeo device)")
        print()
        print(f"searched for (vid,pid) in: "
              f"{[f'{v:04X}/{p:04X}' for v, p in KNOWN_IDS]}")
    finally:
        usb.lib.libusb_free_device_list(lst, 1)


def _interfaces(_args: argparse.Namespace | None = None) -> None:
    """Open the device and report the control interface that would be claimed."""
    dev = RayNeoDevice()
    try:
        dev.open()
    except RayNeoError as exc:
        raise SystemExit(f"error: {exc}")
    iface_no, ep_out, ep_in = dev._interface, dev._ep_out, dev._ep_in
    print(f"opened, interface {iface_no}")
    print(f"  OUT = 0x{ep_out:02X}   IN = 0x{ep_in:02X}")
    dev.close()
    print("closed cleanly")


def _info(_args: argparse.Namespace | None = None) -> None:
    dev = RayNeoDevice()
    dev.refresh_device_info()
    for k, v in dev.get_device_info().to_dict().items():
        print(f"{k:>18}: {v}")


#: SDK field name for each capability flag, indexed the same way as
#: rayneo.CAPABILITY_FLAG_OFFSETS. Ghidra-verified; see docs/PROTOCOL.md.
FLAG_FIELDS = (
    "isSupportFovGet", "isSupportSideBySide", "isSupportFps120",
    "isSupportLumChange", "isSupportAccumaster",
    "isSupportAccumasterModeChange", "isSupportAccumasterModeEye",
    "isSupportAudioVolumeChange", "isSupportAudioQuietMode",
    "isSupportAudioDisable", "isSupportGsensorRAW",
    "isSupportGsensorAngelCorrect", "isSupportGryoTempBias",
    "isSupportTimeSyncMsg", "isSupportGetOrbit", "isSupportUserOrbit",
    "isSupportPanelDistanceAdjust", "isSupportPanelHighDynamic",
    "isSupportAudioSpatialMode", "isSupportAudioTubeMode",
    "isSupportPanelHDR", "isSupportPanelColorEnhance",
    "isSupportPanelDolbyLLDV", "isSupportPanelScreenSize",
)


def _caps(_args: argparse.Namespace | None = None) -> None:
    """Print the capability bitmap in a form that can be diffed against the app."""
    from rayneo import (
        CAPABILITY_FLAG_OFFSETS,
        CAPABILITY_FLAGS,
        CMD_GET_FUNC_SUPPORT,
    )

    dev = RayNeoDevice()
    raw = dev.send(CMD_GET_FUNC_SUPPORT, timeout_ms=800)

    if raw is None:
        print("!! command 0xE0 got no response; falling back to defaults.\n")
        for k, v in dev.capabilities().items():
            print(f"{k:>18}: {v}")
        return

    print(f"response ({len(raw)} bytes):")
    for i in range(0, len(raw), 16):
        chunk = raw[i:i + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  [{i:02x}] {hexs:<47} {ascii_}")
    print()

    print("capability flags:")
    print(f"{'flag':>4} {'resp':>5} {'val':>4}  {'SDK field':<34} {'plugin key':<18}")
    print("-" * 72)
    rev = {}
    for name, flag in CAPABILITY_FLAGS.items():
        rev.setdefault(flag, []).append(name)
    for flag, off in enumerate(CAPABILITY_FLAG_OFFSETS):
        val = raw[off] if off < len(raw) else None
        keys = ",".join(rev.get(flag, ())) or "-"
        field = FLAG_FIELDS[flag] if flag < len(FLAG_FIELDS) else "?"
        mark = "" if val is not None else "  (beyond response!)"
        print(f"{flag:>4} 0x{off:02X}   "
              f"{'-' if val is None else val:>3}  {field:<34} {keys:<18}{mark}")

    print()
    print("parsed capabilities:")
    for k, v in sorted(dev.capabilities().items()):
        print(f"{k:>18}: {v}")


def _set(args) -> None:
    dev = RayNeoDevice()
    what, value = args.what, args.value
    if what == "brightness":
        dev.set_brightness(int(value))
    elif what == "volume":
        dev.set_volume(int(value))
    elif what == "display-mode":
        dev.set_display_mode(value)
    elif what == "screen-size":
        dev.set_screen_size(value)
    elif what == "scene-mode":
        dev.set_scene_mode(value)
    elif what == "hdr-mode":
        dev.set_hdr_mode(value)
    elif what == "color-enhance":
        dev.set_color_enhance(value.lower() in ("on", "1", "true", "yes"))
    elif what == "audio-mode":
        dev.set_audio_mode(value)
    elif what == "audio-tube":
        dev.set_audio_tube(value.lower() in ("on", "1", "true", "yes"))
    else:
        raise SystemExit(f"unknown setting {what}")
    print("OK")


def _raw(args) -> None:
    dev = RayNeoDevice()
    resp = dev.send(int(args.cmd, 16), int(args.val, 16), timeout_ms=args.timeout)
    if resp is None:
        print("(no response)")
    else:
        print("resp:", resp.hex())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices").set_defaults(fn=_devices)
    sub.add_parser("interfaces").set_defaults(fn=_interfaces)
    sub.add_parser("info").set_defaults(fn=_info)
    sub.add_parser("caps").set_defaults(fn=_caps)

    sp = sub.add_parser("set")
    sp.add_argument("what", choices=[
        "brightness", "volume", "display-mode", "screen-size", "scene-mode",
        "hdr-mode", "color-enhance", "audio-mode", "audio-tube",
    ])
    sp.add_argument("value")
    sp.set_defaults(fn=_set)

    rp = sub.add_parser("raw")
    rp.add_argument("cmd")
    rp.add_argument("val")
    rp.add_argument("--timeout", type=int, default=500)
    rp.set_defaults(fn=_raw)

    args = p.parse_args()
    try:
        args.fn(args)
    except (RayNeoError, DeviceNotFound) as exc:
        print("error:", exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()