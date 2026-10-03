#!/usr/bin/env python3
"""Cross-check a capability dump pasted from the plugin against Ghidra.

Usage:
    python3 tools/check_caps.py <hex response bytes>
    python3 tools/check_caps.py --from-file caps.txt

Prints the 24 capability flags with the response byte they come from, and
flags anything that looks inconsistent (e.g. a response too short to contain
the byte we expect, or a value that is neither 0 nor 1).
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "py_modules"))

from rayneo import (  # noqa: E402
    CAPABILITY_FIELD_NAMES,
    CAPABILITY_FLAG_OFFSETS,
    CAPABILITY_FLAGS,
)

#: Which plugin control each capability gates, for the summary at the end.
USED_BY_UI = {
    "lumChange": "屏幕亮度",
    "sideBySide": "显示模式 2D/3D",
    "panelScreenSize": "屏幕尺寸",
    "colorModeChange": "场景模式",
    "panelHDR": "画质动态 SDR/AI-HDR",
    "colorEnhance": "色彩增强",
    "audioVolume": "音量",
    "audioQuietMode": "音频模式",
    "audioSpatial": "环绕",
    "audioTubeMode": "导音鳍",
    "fps120": "(不暴露，仅诊断)",
}


def parse_hex(text: str) -> bytes:
    """Pull the hex dump rows out of the plugin's report.

    Only lines of the form "  [0a] 99 00 01 ..." count. Scanning the whole text
    for 2-digit hex tokens would also pick up the resp[] offsets from the flag
    table printed further down and corrupt the data.
    """
    hexes: list[str] = []
    for line in text.splitlines():
        if not re.match(r"^\s*\[\s*[0-9a-fA-F]{2,4}\s*\]", line):
            continue
        hexes += re.findall(r"\b[0-9a-fA-F]{2}\b", line)
    if not hexes:
        raise SystemExit(
            "no '[xx] hh hh ...' hex rows found. Paste the section that starts "
            "with '0xE0 response (N bytes)'."
        )
    return bytes(int(h, 16) for h in hexes)


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    src = sys.argv[1]
    if src == "--from-file":
        if len(sys.argv) < 3:
            raise SystemExit("need a file path")
        text = open(sys.argv[2], encoding="utf-8", errors="replace").read()
    else:
        text = src
    resp = parse_hex(text)

    print(f"parsed {len(resp)} bytes from the pasted dump\n")
    print(f"{'flag':>4} {'resp':>5} {'raw':>4}  {'SDK field':<34}{'plugin key':<18}note")
    print("-" * 88)

    problems = []
    rev = {}
    for name, flag in CAPABILITY_FLAGS.items():
        rev.setdefault(flag, []).append(name)

    for flag, off in enumerate(CAPABILITY_FLAG_OFFSETS):
        raw = resp[off] if off < len(resp) else None
        field = CAPABILITY_FIELD_NAMES[flag]
        keys = ",".join(sorted(rev.get(flag, ()))) or "-"
        note = ""
        if raw is None:
            note = "BEYOND RESPONSE"
            problems.append(f"flag {flag} expects resp[0x{off:02X}] but response is "
                            f"only {len(resp)} bytes")
        elif raw > 1:
            note = f"value {raw} is neither 0 nor 1"
            problems.append(f"flag {flag} ({field}) = {raw}, expected a boolean")
        print(f"{flag:>4} 0x{off:02X}   "
              f"{'-' if raw is None else raw:>3}  {field:<34}{keys:<18}{note}")

    print()
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("all 24 flags map to a readable boolean byte in the response")

    print()
    print("what the plugin would enable for the controls it shows:")
    for key, label in USED_BY_UI.items():
        flag = CAPABILITY_FLAGS.get(key)
        if flag is None:
            continue
        off = CAPABILITY_FLAG_OFFSETS[flag]
        raw = resp[off] if off < len(resp) else None
        state = "MISSING" if raw is None else ("ON " if raw else "off")
        print(f"  {state}  {label:<22} ({key}, flag {flag}, resp[0x{off:02X}]={raw})")
    return 0


if __name__ == "__main__":
    sys.exit(main())