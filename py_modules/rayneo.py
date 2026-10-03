"""FFalcon / RayNeo glasses USB transport.

Implements the protocol reverse-engineered from the official Android app
(``libFFalconXRServer.so``); see ``docs/PROTOCOL.md`` for the full write-up.

Summary of the wire format:

* 64-byte command frames sent as a single bulk OUT transfer.
  ``frame[0] = 0x66`` (host->device magic), ``frame[1] = command``,
  ``frame[2] = value``, ``frame[3:] = optional payload``, rest zero-padded.
* Responses come back on the bulk IN endpoint: ``>= 64`` bytes,
  ``resp[0] == 0x99`` (device->host magic), ``resp[8]`` echoes the command.

The glasses are a DisplayPort/USB-C external display and are driven purely over
USB; no Bluetooth is involved.
"""

from __future__ import annotations

import ctypes
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from libusb_ctypes import (
    LIBUSB_ENDPOINT_IN,
    LIBUSB_ERROR_ACCESS,
    LIBUSB_ERROR_PIPE,
    LIBUSB_ERROR_TIMEOUT,
    LIBUSB_SUCCESS,
    TRANSFER_TYPES_OK,
    libusb,
    transfer_type_name,
)

# --- USB identifiers -----------------------------------------------------------

#: Known (vendor, product) pairs for a glasses in normal (non-DFU) mode.
#:
#: Taken verbatim from the app's own ``UsbReceiver.isNormalDevice``:
#:
#:     (vendorId == 7099  && productId == 44880)   // 0x1BCB:0xAF50
#:  || (vendorId == 14657 && productId == 44880)   // 0x3941:0xAF50
#:
#: Note the firmware-update (DFU) identities, which this plugin must never open:
#:
#:     (1155  == 0x0483, 57105 == 0xDF11)
#:     (14657 == 0x3941, 44881 == 0xAF51)   <-- DFU PID is AF50 + 1
#:
#: Confirmed on hardware: a GT Max enumerates as 3941:af50 "RayNeo AR Glasses".
KNOWN_IDS = (
    (0x1BCB, 0xAF50),  # older glasses
    (0x3941, 0xAF50),  # current glasses (GT Max)
)

#: Firmware-update identities. Listed only so that we can *refuse* them; opening
#: a DFU interface would put the glasses into an unexpected state.
DFU_IDS = (
    (0x0483, 0xDF11),
    (0x3941, 0xAF51),
)

FRAME_SIZE = 64

CMD_MAGIC = 0x66
RESP_MAGIC = 0x99

# Response dispatch reads resp[8] for the echoed command.
RESP_CMD_OFFSET = 8

# --- Commands (frame[1]) -------------------------------------------------------
#
# This is the recovered command table, not just the opcodes the plugin happens
# to send. Roughly half of these are never called -- panel power, reset,
# bootloader, gyro bias, IMU, FOV -- because the plugin deliberately does not
# expose them (see the notes inline and docs/PROTOCOL.md for the full table).
#
# They are kept so the protocol stays reproducible in one place: a reader
# comparing this against the app's libFFalconXRServer.so sees the whole surface
# and can tell "we chose not to" from "we never found it". Deleting the unused
# half would lose that distinction.
#
# CMD_STATUS_REPORT is a second name for CMD_ACQUIRE_DEVICE_INFO_2 (same 0xE3),
# kept because "status report" is what the reply actually is.

# The full opcode table, recovered from the app and from the SendHidCommand call
# sites in libFFalconXRServer.so (see docs/PROTOCOL.md).
#
# Most of these are never sent by this plugin and never will be -- depth
# adjustment, frame rate, IMU, factory reset, SBS and the rest are the app's
# features, not this panel's. They are here as the reference table, and they are
# deliberately whole: a half-table invites someone to "clean up the unused ones"
# and lose the context that made an implemented command's numbering legible.
CMD_ACQUIRE_DEVICE_INFO = 0x00
CMD_OPEN_IMU = 0x01
CMD_CLOSE_IMU = 0x02
CMD_SWITCH_TO_3D = 0x06
CMD_SWITCH_TO_2D = 0x07
CMD_SET_BRIGHTNESS = 0x09
CMD_SAVE_BRIGHTNESS = 0x0D
CMD_PANEL_POWER_ON = 0x0E
CMD_PANEL_POWER_OFF = 0x0F
CMD_PANEL_POWER_SWAP = 0x12
CMD_SET_PANEL_DISTANCE = 0x17
CMD_SET_HIGH_DYNAMIC = 0x18
CMD_SET_HDR_MODE = 0x1A
CMD_SET_COLOR_ENHANCE = 0x1B
CMD_RESET_SETTINGS = 0x1D
CMD_SAVE_SETTINGS = 0x1F
# 0x20 / 0x21 are 60 Hz / 120 Hz panel frame rate (XRService::PanelFrameRateSet).
#
# Not exposed, matching the official app, which shows no refresh-rate setting for
# this model. The device itself says it could: the GT Max reports
# isSupportFps120 = yes, and FxrApi.Support120fps() is `deviceType == 36 || that
# bit`, which is true here. So this is the app declining to offer it on this
# model, not the hardware refusing.
#
# The app's i18n does carry `refreshRate` and two "120 Hz is locked in 3D" tips,
# but that file ships to every RayNeo model, so it is no evidence about this one.
# Reading it as evidence was a mistake made here once already. Its own strings
# are for the models that do have the control.
CMD_FRAME_RATE_60 = 0x20
CMD_FRAME_RATE_120 = 0x21
CMD_GET_FOV = 0x23
CMD_ENABLE_AUDIO_PERSISTENCE = 0x25
CMD_SWITCH_SIDE_BY_SIDE = 0x30
CMD_GET_TRACE = 0x33
CMD_ENABLE_AUDIO = 0x34
CMD_ENABLE_PSENSOR = 0x38
CMD_GET_IMU_CALIBRATION = 0x3C
CMD_SET_GYRO_BIAS = 0x3F
CMD_SET_AUDIO_TUBE = 0x48
CMD_SET_AUDIO_MODE = 0x49
CMD_SET_AUDIO_VOLUME = 0x50
CMD_DISABLE_WHEEL_KEY_SBS = 0x58
#: Same value as CMD_MAGIC, and both are correct: 0x66 is the first byte of
#: every frame *and* the id of the reboot command, which sits in frame[1]. The
#: app's ``RebootAndBootloader`` is the only user of the command form and this
#: plugin never calls it, so nothing here can confuse the two -- but do not
#: "fix" the duplicate on the assumption that one of them is a typo.
CMD_REBOOT_BOOTLOADER = 0x66
CMD_PANEL_COLOR_ADJUST = 0x73
CMD_SET_SCREEN_SIZE = 0x76
CMD_GET_FUNC_SUPPORT = 0xE0
CMD_ACQUIRE_DEVICE_INFO_2 = 0xE3
#: Alias for the same 0xE3 opcode, named for what it is: a status report.
CMD_STATUS_REPORT = 0xE3

#: Commands the glasses never acknowledge, so waiting for a reply is pure cost.
#:
#: Measured from the TX trace across every session: tallying each send against
#: whether a frame came back.
#:
#:     cmd    replied   silent
#:     0x09       0        23     brightness
#:     0x1A       0        12     picture quality
#:     0x73       0        36     panel colour params (scene mode)
#:     0x18       0         3     high dynamic
#:     0x49       0         4     audio mode
#:     0x48       2         0     audio tube
#:     0x1B      14         0     colour enhance
#:     0x50      11         0     volume
#:     0x76      14         0     screen size
#:     0xE0      14         0     capabilities
#:
#: This is not a detail. Waiting the full timeout on a silent command costs
#: hundreds of milliseconds and -- far worse -- three unanswered writes in a row
#: are enough to leave the interrupt endpoint wedged, after which every later
#: transfer fails at once. Scene mode was the worst case: the code treated the
#: preview's missing reply as a failure and skipped the two save frames, so the
#: change was previewed and never persisted.
#:
#: Audio mode was listed as acknowledging until the tally was redone over every
#: session: 0 replies in 4. One earlier send appeared to draw a frame, but a
#: single stray match is not a reply rate, and the split is otherwise clean --
#: everything that goes through the panel's own path is silent.
UNACKNOWLEDGED_COMMANDS = frozenset({
    CMD_SET_BRIGHTNESS,
    CMD_SET_HDR_MODE,
    CMD_PANEL_COLOR_ADJUST,
    CMD_SET_HIGH_DYNAMIC,
    CMD_SET_AUDIO_MODE,
})

#: Gap between the frames of a single setting change, in seconds. Zero.
#:
#: The official app does not space them, and never combines them either.
#: ``AirApi.saveColorMode`` is two back-to-back JNI calls with nothing in
#: between:
#:
#:     public void saveColorMode(int i) {
#:         this.mFXRServer.PanelColorParmsAdjust(255, (byte) 1, b);
#:         this.mFXRServer.PanelColorParmsAdjust(15, (byte) 1, b);
#:     }
#:
#: and there is no sleep anywhere in the colour path -- the only ones in the SDK
#: are in ``Dfu.java``, for firmware updates. More to the point, the app treats
#: preview and save as two separate user actions: ``GlassDeviceManager``
#: dispatches ``previewColorMode`` on one Flutter callback (case 32) and
#: ``saveColorMode`` on another (case 33). It never sends a preview and a save
#: in the same breath, so it never exercises the burst this plugin used to.
#:
#: A 150 ms gap was added here when three unspaced writes were found to leave the
#: interrupt endpoint halted. That cost 300 ms on every scene change and was
#: papering over a problem ``_write_panel`` now fixes properly: a failed write
#: clears the halt and retries. If the panel ever does need the frames separated,
#: the read-back will say so -- ``_confirm_scene_mode`` logs a WARN when the
#: device reports a different mode than the one asked for.
PANEL_FRAME_GAP = 0.0

#: Pause after clearing a halted endpoint, before retrying the write.
#:
#: The endpoint needs a moment to come back. The retry used to reuse
#: PANEL_FRAME_GAP, which is now zero -- so removing the frame spacing must not
#: silently remove the recovery pause as well. Kept separate for that reason.
HALT_RECOVERY_SECONDS = 0.05

#: Delays between read-back probes while a panel setting settles, in seconds.
#:
#: Backoff rather than one fixed wait. The first probe is what decides whether
#: the user sees any delay at all, and the write itself leaves the device in
#: under a millisecond -- so a fixed 200 ms before the first look was dead time.
#: The tail stays generous, because a scene change genuinely can take a moment.
SETTLE_PROBE_DELAYS = (0.05, 0.10, 0.20, 0.35, 0.5, 0.5, 0.5)

#: How long each of the background poll's two attempts waits for the 0x00 reply.
#:
#: Deliberately generous, and this is the opposite of what it used to be. The
#: first attempt was briefly shortened to 60 ms on the theory that the poll was
#: holding the device lock and delaying the user's next write. That theory was
#: wrong: the TX trace and the frontend's own timestamp show the write arriving
#: and its confirmation leaving 6 ms apart, with the ~256 ms in between spent
#: getting the call to the backend at all. The poll was never the bottleneck, so
#: the short timeout bought nothing and cost a false disconnect -- a -7 wedge
#: produced a miss, the shortened query turned the next one into a miss too, and
#: three of them declared the glasses gone five seconds before a write answered
#: in 2 ms.
#:
#: A busy panel is the case a short timeout was meant to help, and the retry
#: covers it: waiting costs nothing now that the lock is not what the user is
#: waiting on.
POLL_QUERY_TIMEOUT_MS = 250
POLL_QUERY_RETRY_MS = 250

#: How long to wait before each of the brightness commit's two read-backs.
#:
#: Bounded on purpose. The commit runs while the user may start dragging again,
#: so it must not be the slow thing in the room -- see save_brightness. If both
#: probes disagree with what was staged, the background poll is the authority
#: and will correct the published state on its next cycle.
BRIGHTNESS_CONFIRM_DELAYS = (0.08, 0.25)

#: How long to let the panel settle before reading a setting back.
#:
#: These commands are fire-and-forget, so there is no reply to synchronise on
#: and the only evidence that a change landed is the status report. 250 ms was
#: not enough: a scene change committed later than that, and was read back as
#: unchanged -- which is what SETTLE_PROBE_DELAYS backs off from.

# PanelColorAdjust sub-operations (frame[2]).
PANEL_COLOR_OP_MODE_PREVIEW = 12
PANEL_COLOR_OP_SAVE = 255
PANEL_COLOR_OP_SAVE2 = 15

#: User-facing scene modes -> the byte the firmware actually expects.
#
# CAREFUL: the firmware's scene order does NOT match the SDK enum names.
#
# The Java SDK (FxrConstant.PanelColorModeOp) says:
#     colorModeStandard = 0, colorModeSplendid = 1,
#     colorModeSoft = 2, colorModeEyeProtection = 3
# and the app's own i18n labels those four as
#     标准 / 电影(imageModeMovie) / 柔和(imageModeSoft) / 护眼(imageModeEyeProtection)
# with 阅读 being a *separate* mode keyed `scene_office_mode`.
#
# But on real hardware (GT Max, Gemini 2.0) sending 3 lands on 阅读, so the
# firmware's four scene slots are ordered:
#     0 标准 / 1 电影 / 2 护眼 / 3 阅读
# Verified empirically: selecting 护眼 while sending 3 produced 阅读.
#
# The display strings live in src/labels.ts; only the wire values belong here.
SCENE_MODES: Dict[str, int] = {
    "standard": 0,
    "movie": 1,
    "eyeProtection": 2,
    "reading": 3,
}

SCENE_MODE_BY_VALUE: Dict[int, str] = {v: k for k, v in SCENE_MODES.items()}

#: Display modes.
DISPLAY_MODE_2D = "2d"
DISPLAY_MODE_3D = "3d"

#: Screen-size steps, measured on a GT Max.
#:
#: The device echoes the byte back verbatim in the 0xE3 status report at offset
#: 0x18, and the app's i18n labels the three slots:
#:     large  = 115% (放大画面，减少黑边)
#:     medium = 100% (原始比例)
#:     small  = 85%  (缩小画面，避免裁切)
#:
#: Empirically the firmware numbers them 0/1/2 in that order -- sending 1 gave
#: 100% and sending 0 gave 115%, so the SDK-derived guess (large=1) was
#: inverted. Verified: the status-report byte became 1, 0 and 2 for the three
#: settings. (It was originally read at 0x1A; see the STATUS_* block.)
SCREEN_SIZES: Dict[str, int] = {
    "large": 0,   # 115 %
    "medium": 1,  # 100 %
    "small": 2,   # 85 %
}

SCREEN_SIZE_BY_VALUE: Dict[int, str] = {v: k for k, v in SCREEN_SIZES.items()}

# --- 0xE3 status report (FUN_0019ca50) -----------------------------------------
#
# The status report is decoded by FUN_0019ca50, reached from
# XRService::UpdateDeviceState(XrHidDeviceStatusReport) @ 0x192e30. It is a
# DIFFERENT parser from the 0x00 one (FUN_0019ba50), which is why the panel
# fields this one owns -- including scene mode and picture quality -- are absent
# from the device-info response entirely. That is what those two settings were
# wrongly believed to have no read-back for.
#
# Each offset is the `lVar9 + N` read that feeds one XrHidDeviceInfo field.
# Two of them (luminance index at 0x0A, audio mode at 0x10) were already
# measured on hardware before this table was recovered and still match, which
# is the evidence that the rest is right rather than merely plausible. Volume
# (0x0C), mute (0x0E) and 2D/3D (0x0F) also appear here; they are left to the
# device-info block, which already reads them from confirmed offsets.
#
# Status of each entry. "calibrated" means calibrate_status() changed that one
# setting on the device and watched which byte moved; the calibration writes
# every option of every setting in turn, so a byte shared by two settings
# cannot masquerade as either.
#
#   0x0A  luminance index   calibrated
#   0x10  audio mode        calibrated
#   0x12  scene mode        calibrated, both directions (0<->3, 3<->1, 1<->0)
#   0x15  picture quality   calibrated, one direction (SDR -> AI-HDR: 0->1)
#   0x17  colour enhance    calibrated, both directions (0<->1)
#   0x18  audio tube        calibrated
#   0x1A  screen size       calibrated
#   0x13  high dynamic      unknown -- every probe hit an endpoint halt
#   0x14  --                always 0 on this device
#   0x16  --                always 0 on this device
#
# The firmware's field names are off by one across this group, twice over.
# FUN_0019ca50 copies a uint16 from 0x16 into struct[0xD5] and the next byte
# into struct[0xD6], and pairs 0x13/0x14/0x15 with panelHighDynamic /
# panelHDRMode / panelHDREnabled. The measurements put picture quality at 0x15
# rather than 0x14, and colour enhance at 0x17 rather than 0x16. Trusting the
# pairing is what broke colour enhance and, through it, the volume ceiling,
# which is derived from the audio tube.
STATUS_LUMINANCE_INDEX = 0x0A
STATUS_AUDIO_MODE = 0x10
STATUS_PANEL_COLOR_PARAMS = 0x12
STATUS_HIGH_DYNAMIC = 0x13
STATUS_HDR_MODE = 0x15
STATUS_HDR_ENABLED = 0x14
STATUS_COLOR_ENHANCE = 0x17
STATUS_AUDIO_TUBE = 0x18
STATUS_SCREEN_SIZE = 0x1A

#: The frame rate is *not* in the status report: struct 0xD0 is written by the
#: 0x00 parser from reply[0x28]. A 120 here was a coincidence.
DEVICE_INFO_FRAME_RATE = 0x28

#: Brightness byte on the wire, in the device-info response. Confirmed by
#: diffing: index 28 -> 0x29 became 28, index 0 -> 1, index 8 -> 9.
DEVICE_INFO_LUMINANCE_VALUE = 0x29

#: The build-date string in the device-info response, NUL-terminated.
#:
#: A GT Max reports "Sep 22 2026". The SDK parses this into
#: ``FXRDeviceInfo.TIME { year, month, day }`` and the official app displays it
#: as 20260922 -- that is the string users recognise as the firmware version.
DEVICE_INFO_BUILD_DATE = 0x18

#: The numeric firmware revision, uint16 little-endian, in the same response.
#:
#: A GT Max reports 26. This is a *different* field from the build date: it is
#: what ``FXRDeviceInfo::firmwareVersion`` holds and what
#: ``AirApi.getBrightnessIndex``-adjacent getters return, and the app passes it
#: through to ``GlassInfo.firmwareVersion``. The app's own UI just does not show
#: it -- it shows the build date instead.
DEVICE_INFO_FIRMWARE_VERSION = 0x24

#: Month abbreviations, as the firmware spells them.
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def firmware_build(resp: bytes) -> Optional[str]:
    """The firmware build date as YYYYMMDD, or None if it is not readable.

    Returns None rather than a guess when the string does not parse. A
    mis-parsed date is worse than a missing one: it would sit next to a
    legitimate version number looking equally authoritative.
    """
    end = resp.find(b"\x00", DEVICE_INFO_BUILD_DATE)
    if end < 0:
        end = len(resp)
    raw = resp[DEVICE_INFO_BUILD_DATE:end].decode("ascii", "replace").strip()
    parts = raw.split()
    if len(parts) != 3:
        return None
    month_name, day_text, year_text = parts
    month = _MONTHS.get(month_name[:3].lower())
    if month is None or not day_text.isdigit() or not year_text.isdigit():
        return None
    day = int(day_text)
    year = int(year_text)
    if not 1 <= day <= 31 or not 2000 <= year <= 2099:
        return None
    return f"{year:04d}{month:02d}{day:02d}"

#: Reverse maps for the diff log, so a moved byte reports which setting it is
#: rather than a bare offset.
STATUS_FIELD_NAMES: Dict[int, str] = {
    STATUS_LUMINANCE_INDEX: "luminance index",
    STATUS_AUDIO_MODE: "audio mode",
    STATUS_PANEL_COLOR_PARAMS: "scene mode / panel colour params",
    #: Read but never identified, and never published. Constant 2 across nine
    #: snapshots spanning all four scene modes, plus a run with AI-HDR on and
    #: off. The app's PanelSetHighDynamicMode (0x18) is not exposed here and
    #: calibrate_status is the only thing that writes it, so the label stays
    #: descriptive rather than claiming to be that switch: the byte has never
    #: been observed to move with anything.
    STATUS_HIGH_DYNAMIC: "high dynamic (unidentified, constant 2)",
    STATUS_HDR_MODE: "picture quality (hdr mode)",
    STATUS_HDR_ENABLED: "hdr enabled",
    STATUS_COLOR_ENHANCE: "colour enhance",
    STATUS_AUDIO_TUBE: "audio tube",
    STATUS_SCREEN_SIZE: "screen size",
}

DEVICE_INFO_FIELD_NAMES: Dict[int, str] = {
    0x24: "firmware version (numeric revision)",
    DEVICE_INFO_BUILD_DATE: "firmware build date",
    0x28: "frame rate",
    DEVICE_INFO_LUMINANCE_VALUE: "luminance (wire value)",
    0x2A: "volume",
    0x2B: "2D/3D",
    0x2C: "wakeup",
    0x2D: "audio mode",
    0x2E: "mute",
    0x2F: "panel distance",
    0x30: "light sensor valid",
}

#: maxVolume, in the device-info response. From the reply parser
#: (FUN_0019ba50 in libFFalconXRServer.so):
#:
#:     struct[0xCD] = reply[0x3D]   // 0xCD is maxVolume in XrHidDeviceInfo
#:
#: The GT Max reports 16, i.e. indices 0..15, where 0..12 is normal volume
#: and 13..15 is the overdrive range (matches the app's own slider, confirmed
#: on hardware).
DEVICE_INFO_MAX_VOLUME = 0x3D
DEVICE_INFO_FIELD_NAMES[DEVICE_INFO_MAX_VOLUME] = "max volume"

#: Where each key of :meth:`DeviceInfo.to_dict` actually comes from, as
#: ``(command, offset)``. ``offset`` -1 means the value is not on the wire at
#: all: computed locally, read from the SDK's own configuration, or derived from
#: another field.
#:
#: Written out rather than inferred. The table in the log is only worth reading
#: if its source column is right, and a name-matching heuristic quietly puts the
#: wrong offset next to a field -- which is the exact failure this whole file is
#: about.
DEVICE_INFO_FIELD_ORIGIN: Dict[str, Tuple[int, int]] = {
    "connected": (0x00, -1),          # our own flag, not a reading
    "deviceType": (0x00, 0x15),
    "firmwareVersion": (0x00, DEVICE_INFO_FIRMWARE_VERSION),
    "firmwareBuild": (0x00, DEVICE_INFO_BUILD_DATE),
    "glassesId": (0x00, 0x26),
    "manufacturer": (0x00, -1),       # SDK config, never the wire
    "frameRate": (0x00, DEVICE_INFO_FRAME_RATE),
    "volume": (0x00, 0x2A),
    "maxVolume": (0x00, DEVICE_INFO_MAX_VOLUME),
    "volumeLimit": (0x00, -1),        # derived from the audio tube
    "displayMode3d": (0x00, 0x2B),
    "audioMode": (0x00, 0x2D),
    "whisper": (0x00, -1),            # derived from audioMode
    "mute": (0x00, 0x2E),
    "panelDistance": (0x00, 0x2F),
    "hdrMode": (0xE3, STATUS_HDR_MODE),
    "hdrEnabled": (0xE3, STATUS_HDR_ENABLED),
    "sceneMode": (0xE3, STATUS_PANEL_COLOR_PARAMS),
    "colorEnhance": (0xE3, STATUS_COLOR_ENHANCE),
    "audioTube": (0xE3, STATUS_AUDIO_TUBE),
    "screenSize": (0xE3, STATUS_SCREEN_SIZE),
    "luminance": (0xE3, STATUS_LUMINANCE_INDEX),
    "luminanceValue": (0x00, DEVICE_INFO_LUMINANCE_VALUE),
    "maxLuminance": (0x00, -1),       # local table length, not a reading
    "wakeup": (0x00, 0x2C),
}

#: Audio modes (标准 / 轻语 / 环绕). ``setWhisperMode(true)`` pins whisper to 1 and
#: the native layer forwards the byte unclamped.
AUDIO_MODES: Dict[str, int] = {
    "standard": 0,
    "whisper": 1,
    "surround": 2,
}

#: Picture-quality mode (SDR / AI-HDR). The JNI wrapper clamps to 0..2; the UI
#: only surfaces the first two.
HDR_MODES: Dict[str, int] = {
    "sdr": 0,
    "aihdr": 1,
}




# --- Errors --------------------------------------------------------------------


class RayNeoError(RuntimeError):
    """Raised for any failure talking to the glasses."""


class DeviceNotFound(RayNeoError):
    pass


# --- Device handle -------------------------------------------------------------


@dataclass
class DeviceInfo:
    connected: bool = False
    device_type: Optional[int] = None
    firmware_version: Optional[int] = None
    #: Firmware build date as YYYYMMDD, e.g. "20260922". This is what the
    #: official app shows as the firmware version; ``firmware_version`` is the
    #: separate numeric revision (26 on a GT Max). Both are shown.
    firmware_build: Optional[str] = None
    glasses_id: Optional[str] = None
    #: Panel vendor string from the device-info block; selects the brightness table.
    manufacturer: Optional[str] = None
    #: Device-reported panel refresh rate (0x00 reply[0x28]). The GT Max
    #: reports 120.
    #:
    #: It stays in to_dict even though the panel never renders it, because
    #: device_info_table() builds the protocol dump from to_dict -- so removing
    #: it here would take it out of the dump too, which is the one place it is
    #: worth having. Unlike high_dynamic, whose dump comes from
    #: STATUS_FIELD_NAMES and is therefore unaffected.
    frame_rate: Optional[int] = None
    volume: Optional[int] = None
    max_volume: Optional[int] = None
    display_mode_3d: Optional[bool] = None
    audio_mode: Optional[int] = None
    whisper: Optional[bool] = None
    mute: Optional[bool] = None
    panel_distance: Optional[int] = None
    #: Picture-quality mode (0 = SDR, 1 = AI-HDR), from reply[0x14].
    hdr_mode: Optional[int] = None
    #: Whether HDR is active right now (reply[0x15]). Distinct from hdr_mode:
    #: the mode is what was asked for, this is what the panel is doing.
    hdr_enabled: Optional[bool] = None
    #: Scene mode as a key of SCENE_MODES, from the panel colour parameters
    #: byte (reply[0x12]).
    scene_mode: Optional[str] = None
    color_enhance: Optional[bool] = None
    #: 0x13, read but unidentified. Kept for calibrate_status, which is
    #: the only writer (via 0x18). Deliberately absent from to_dict:
    #: a value that has never moved under anything is not a setting the
    #: panel has any business showing.
    high_dynamic: Optional[bool] = None
    audio_tube: Optional[bool] = None
    #: Screen size key ("large" / "medium" / "small"), read back from the 0xE3
    #: status report. There is no dedicated getter for it in the info block.
    screen_size: Optional[str] = None
    luminance: Optional[int] = None
    #: The brightness byte actually transmitted, i.e. ``table[index]``.
    #: Measured at offset 0x29 of the device-info response.
    luminance_value: Optional[int] = None
    max_luminance: Optional[int] = None
    #: Volume index cap. Also device-reported and also mode-dependent: with the
    #: audio tube on, the gain above 100 % is unavailable. Unmeasured offset, so
    #: this stays at DEFAULT_MAX_VOLUME until the probe locates it.
    max_volume: Optional[int] = None
    #: Highest volume index currently selectable. Equals ``max_volume - 1``
    #: normally, but drops to AUDIO_TUBE_VOLUME_LIMIT while the audio tube is
    #: on. Published so the slider matches what the backend will accept.
    volume_limit: Optional[int] = None
    wakeup: Optional[bool] = None
    raw: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "connected": self.connected,
            "deviceType": self.device_type,
            "firmwareVersion": self.firmware_version,
            "firmwareBuild": self.firmware_build,
            "glassesId": self.glasses_id,
            "manufacturer": self.manufacturer,
            "frameRate": self.frame_rate,
            "volume": self.volume,
            "maxVolume": self.max_volume,
            "volumeLimit": self.volume_limit,
            "displayMode3d": self.display_mode_3d,
            "audioMode": self.audio_mode,
            "whisper": self.whisper,
            "mute": self.mute,
            "panelDistance": self.panel_distance,
            "hdrMode": self.hdr_mode,
            "hdrEnabled": self.hdr_enabled,
            "sceneMode": self.scene_mode,
            "colorEnhance": self.color_enhance,
            "audioTube": self.audio_tube,
            "screenSize": self.screen_size,
            "luminance": self.luminance,
            "luminanceValue": self.luminance_value,
            "maxLuminance": self.max_luminance,
            "wakeup": self.wakeup,
            "raw": self.raw,
        }


# --- Brightness ----------------------------------------------------------------
# ``XRService::PanelLunaSet(index)`` does NOT put the raw index on the wire; it
# sends ``table[index]`` (and 0xFF when the index is out of range). The table is
# selected by ``LuminanceUtils::Init`` from four arrays compiled into
# ``libFFalconXRServer.so``, keyed on the device's manufacturer string and
# deviceType, then truncated by ``cutBrightnessArray`` to the firmware's
# ``maxBrightness``.
#
# Selection logic (recovered from LuminanceUtils::Init @0x9c410):
#   manufacturer == "samsung":
#       deviceType in [33, 36] -> _SAMSUNG_ARIES
#       deviceType == 32       -> _NXTVIEW
#       otherwise              -> _SAMSUNG_OTHER
#   otherwise:
#       deviceType == 32       -> _NXTVIEW
#       otherwise              -> _GENERAL
#
# RayNeo GT Max reports deviceType 64/65 (Gemini) with a non-Samsung panel, so
# it selects _GENERAL -- the 29-step table.

_LUM_NXTVIEW = (1, 2, 0, 3, 4)
_LUM_GENERAL = (
    1, 2, 0, 5, 6, 7, 3, 8, 9, 10, 11, 12, 4,
    13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28,
)
_LUM_SAMSUNG_ARIES = (1, 2, 0, 5, 6, 7, 3)
_LUM_SAMSUNG_OTHER = (1, 2, 0, 3)

DEVICE_TYPE_NXTVIEW = 32
SAMSUNG_MANUFACTURER = "samsung"


def brightness_table(
    device_type: Optional[int], manufacturer: Optional[str]
) -> Tuple[int, ...]:
    """Return the UI-index -> wire-value brightness table for a device."""
    if (manufacturer or "").strip().lower() == SAMSUNG_MANUFACTURER:
        if device_type is not None and 33 <= device_type <= 36:
            return _LUM_SAMSUNG_ARIES
        if device_type == DEVICE_TYPE_NXTVIEW:
            return _LUM_NXTVIEW
        return _LUM_SAMSUNG_OTHER
    if device_type == DEVICE_TYPE_NXTVIEW:
        return _LUM_NXTVIEW
    return _LUM_GENERAL


#: Fallback used until the device identifies itself.
DEFAULT_BRIGHTNESS_TABLE = _LUM_GENERAL

#: Value the firmware expects when the brightness index is out of range.
BRIGHTNESS_OUT_OF_RANGE = 0xFF

#: Number of brightness steps the panel accepts.
#:
#: The GENERAL table has 29 entries, but only the first 12 map onto the range the
#: glasses actually render: 1 2 0 5 6 7 3 8 9 10 11 12 -- that is wire values
#: 0..12, a bijection (index 2 gives 0, index 12 gives 4, and so on). Everything
#: past index 11 (4, 13..28) is a second, much brighter band that this panel
#: does not drive.
#:
#: The device never states a ceiling -- ``maxLuminance`` is just the table
#: length -- and the usable ceiling depends on the display strategy mode
#: (防抖 / 固定 / 随行), which is not reported either. Rather than guess per
#: mode, the slider stops at the top of the band the glasses' own UI labels
#: 1-12.
BRIGHTNESS_STEPS = 12

# --- Capability flags ----------------------------------------------------------
# Two independent Ghidra passes over libFFalconXRServer.so pin down both halves
# of the mapping, and they are only consistent with each other one way round:
#
#   1. flag index -> response byte offset, from the SIMD unpack in
#      XRService::UpdateDeviceState(XrHidDeviceFuncSupport) @ 0x9cc54:
#        ldur s0,[x9,#0x9]  -> flags 0..3    resp[0x09..0x0c]
#        ld1 v0.b[4],#0xd   -> flag  4        resp[0x0d]
#        ld1 v0.b[5],#0x16  -> flag  5        resp[0x16]
#        ld1 v0.b[6],#0x19  -> flag  6        resp[0x19]
#        ld1 v1.b[4],#0x14  -> flags 12,13    resp[0x13],resp[0x14]
#        ld1 v0.b[7],#0xe   -> flag  7        resp[0x0e]
#        ld1 v0.s[2],#0xf   -> flags 8..11    resp[0x0f..0x12]
#        ld1 v0.b[14],#0x15 -> flag  14       resp[0x15]
#        ld1 v0.b[15],#0x18 -> flag  15       resp[0x18]
#        ldur d0,[x9,#0x1a] -> flags 16..23   resp[0x1a..0x21]
#
#   2. flag index -> field name, from JNIFormatHelper::C2Java::ConvertXRDeviceInfo
#      @ 0x155ac4. Each field does `ldrb w3,[x21,#struct_off]` immediately before
#      SetBooleanField, and the struct offsets of the 24 capability bools run
#      0x04..0x1b with no gaps, so ascending offset == ascending flag index.
#
# Java cross-check: FxrApi.java's Support*() methods name 22 of the 24 fields,
# and all 22 agree.
#
# NOTE: this unpack only runs for deviceType >= 0x30, which covers every
# Gemini/Taurus unit (GT Max reports 64/65).
CAPABILITY_FLAG_OFFSETS = (
    0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x16, 0x19, 0x0E,
    0x0F, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x18,
    0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x21,
)

#: feature name -> capability flag index
CAPABILITY_FLAGS = {
    "fovGet":          0,   # isSupportFovGet
    "sideBySide":      1,   # isSupportSideBySide      (2D/3D)
    "fps120":          2,   # isSupportFps120          (60/120 Hz)
    "lumChange":       3,   # isSupportLumChange       (屏幕亮度)
    "colorAdjust":     4,   # isSupportAccumaster      (色彩调节)
    "colorModeChange": 5,   # isSupportAccumasterModeChange  (场景模式)
    "colorModeEye":    6,   # isSupportAccumasterModeEye     (护眼场景)
    "audioVolume":     7,   # isSupportAudioVolumeChange
    "audioQuietMode":  8,   # isSupportAudioQuietMode  (轻语)
    "audioDisable":    9,   # isSupportAudioDisable    (静音)
    "gsensorRaw":     10,   # isSupportGsensorRAW
    "gsensorCorrect": 11,   # isSupportGsensorAngelCorrect
    "gyroTempBias":   12,   # isSupportGryoTempBias
    "timeSync":       13,   # isSupportTimeSyncMsg
    "getOrbit":       14,   # isSupportGetOrbit
    "userOrbit":      15,   # isSupportUserOrbit
    "panelDistance":  16,   # isSupportPanelDistanceAdjust
    "panelHighDynamic": 17, # isSupportPanelHighDynamic
    "audioSpatial":   18,   # isSupportAudioSpatialMode (环绕)
    "audioTubeMode":  19,   # isSupportAudioTubeMode   (导音鳍)
    "panelHDR":       20,   # isSupportPanelHDR        (SDR/AI-HDR)
    "colorEnhance":   21,   # isSupportPanelColorEnhance
    "panelDolby":     22,   # isSupportPanelDolbyLLDV
    "panelScreenSize": 23,  # isSupportPanelScreenSize (115/100/85 %)
}

#: SDK field names, indexed by capability flag. Same source as
#: CAPABILITY_FLAG_OFFSETS; kept separately so the diagnostic dump can show the
#: real field names next to the plugin's own short keys.
CAPABILITY_FIELD_NAMES = (
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

#: 120 Hz refresh is not exposed, matching the official app, which offers no such
#: setting on this model. Technically it would work -- isSupportFps120 reads yes
#: and FxrApi.Support120fps() returns true for the GT Max -- so this is a product
#: decision by RayNeo, not a hardware wall, and the plugin follows it rather than
#: inventing a control the app does not have.
#:
#: The bit is read for diagnostics only. A future control would gate on it, plus
#: 3D mode, which pins the rate to 60 Hz.

#: Conservative fallback when command 0xE0 gets no reply. Only the features the
#: SDK gates on deviceType >= 48 are assumed present; everything the SDK treats
#: as "always" is assumed present too, so a failed probe never hides a control
#: the user expects.
DEFAULT_CAPABILITIES = {
    "fovGet": False,
    "sideBySide": True,
    "fps120": False,
    "lumChange": True,
    "colorAdjust": True,
    "colorModeChange": True,
    "colorModeEye": True,
    "audioVolume": True,
    "audioQuietMode": True,
    "audioDisable": True,
    "gsensorRaw": False,
    "gsensorCorrect": False,
    "gyroTempBias": False,
    "timeSync": False,
    "getOrbit": False,
    "userOrbit": False,
    "panelDistance": True,
    "panelHighDynamic": True,
    "audioSpatial": False,
    "audioTubeMode": True,
    "panelHDR": True,
    "colorEnhance": True,
    "panelDolby": False,
    "panelScreenSize": True,
}

#: Fallback for maxVolume when the device has not reported one (offset
#: DEVICE_INFO_MAX_VOLUME). The GT Max reports 16, meaning indices 0..15: the
#: firmware treats 0..12 as normal volume and 13..15 as the overdrive range.
#: See AUDIO_TUBE_VOLUME_LIMIT.
DEFAULT_MAX_VOLUME = 16

#: Highest volume index that is still 100 %. Above this the firmware applies
#: gain, which the audio tube cannot take -- the official app reports
#: "导音鳍开启后，无法继续增益音量" and clamps here.
#:
#: Verified on hardware: with the tube on, the device accepts indices far past
#: this without complaint, so the clamp has to be applied on our side.
AUDIO_TUBE_VOLUME_LIMIT = 12


#: Frame bytes that change on every status report whatever the settings are:
#: 0x04 is a checksum over the report and 0x05..0x06 a counter that climbs
#: between reads. Observed directly -- 0x05 went 197 -> 207 -> 216 -> 226 -> 238
#: -> 249 -> 5 -> 28 across one calibration with no setting change in between.
#:
#: A diff that includes them reports them as moved for every option of every
#: setting, which is how the first calibration run was mostly noise.
VOLATILE_STATUS_BYTES = (0x04, 0x05, 0x06)


def _dump_table(headers: List[str], rows: List[List[str]]) -> str:
    """``parsed`` then a column table with a rule under the header.

    Every decoded response is rendered this way. A table beats key = value for
    all three: each row relates the field to something else -- for the device
    info and the status report, the byte it came from; for the bitmap, the SDK
    name and the plugin key -- and a flat pair throws exactly that away.
    """
    if not rows:
        return "parsed  nothing"
    widths = [max(len(headers[i]), max(len(r[i]) for r in rows))
              for i in range(len(headers))]
    def line(cells):
        return "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))
    out = ["parsed",
           "  " + line(headers),
           "  " + "-" * (sum(widths) + 2 * (len(widths) - 1))]
    out += ["  " + line(r) for r in rows]
    return "\n".join(out)


def _dump_block(title: str, raw: Optional[bytes], parsed: str) -> str:
    """One command's response: the raw frame, then what it decodes to.

    Every read-only response in the log goes through this, so 0x00, 0xE0 and
    0xE3 read the same way: a ``raw`` section and a ``parsed`` section, one
    block each, in command-id order. They did not used to -- the device-info
    decode sat in its own block *before* 0x00, and 0xE3 was logged as a bare
    ``response`` naming no command at all.
    """
    out = [f"===== {title} ====="]
    if raw is None:
        out.append("  raw     no response")
    else:
        frame = format_frame(raw, title).splitlines()
        out.append("  raw     " + frame[0])
        out += ["    " + line for line in frame[1:]]
    out.append("  " + parsed.replace("\n", "\n  "))
    return "\n".join(out)


def _diff_offsets(entry: str) -> tuple:
    """Offsets named in one ``_diff_bytes`` entry such as ``[0x12] 0->3``."""
    out = []
    rest = entry
    while True:
        start = rest.find("[0x")
        if start < 0:
            return tuple(out)
        end = rest.find("]", start)
        if end < 0:
            return tuple(out)
        out.append(int(rest[start + 3:end], 16))
        rest = rest[end:]


def _meaningful(raw: bytes) -> bytes:
    """``raw`` with the per-report checksum and counter zeroed out."""
    return bytes(0 if i in VOLATILE_STATUS_BYTES else b
                 for i, b in enumerate(raw))


def _default_max_luminance() -> int:
    """Fallback for maxLuminance.

    NOTE: this is NOT a device-reported value, despite sitting next to
    maxVolume in the info block and despite the SDK exposing GetMaxLuminance().
    The reply parser shows where it really comes from::

        // FUN_0019ba50, after filling struct[0xCD] from reply[0x3D]
        struct[0xCE] = index of reply[0x29] in the brightness table;
        struct[0xCF] = (table_end - table_begin) >> 2;   // 0xCF = maxLuminance

    So maxLuminance is simply the length of the local brightness table -- 29
    entries for a GT Max. The device never states its own ceiling.

    What the glasses *do* limit is the value they report back at
    DEVICE_INFO_LUMINANCE_VALUE. Measured on hardware, that value stops rising
    once brightness passes the low band (wire values 1..9) or the low band plus
    one more step (1..12), depending on the display strategy mode -- a mode we
    cannot read. Pushing a higher step is accepted but not reflected, which is
    what makes the official app's slider "fall back" on its own.
    """
    return len(DEFAULT_BRIGHTNESS_TABLE)


#: Where the kernel exposes raw USB descriptors. Each device directory holds a
#: world-readable `descriptors` file containing the standard descriptor chain
#: (device, configuration, interface, endpoint...) exactly as sent on the wire.
SYSFS_USB_DEVICES = "/sys/bus/usb/devices"


def _sysfs_config_blobs(vid: int, pid: int) -> List[bytes]:
    """Configuration descriptor blobs read straight from sysfs.

    This is the primary descriptor source, deliberately preferred over
    ``libusb_get_config_descriptor``. libusb malloc's a block whose layout is
    ``struct libusb_config_descriptor`` -- which contains flexible array members
    that ctypes cannot express, so every attempt to walk it either read garbage
    (bNumInterfaces came back as 49) or hit a NULL ``interface`` pointer. sysfs
    hands us the untouched wire format instead, which needs no layout
    assumptions and is readable by any user.

    Returns one blob per configuration descriptor found, in file order.
    """
    out: List[bytes] = []
    root = SYSFS_USB_DEVICES
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return out

    for name in entries:
        base = os.path.join(root, name)
        # Interface entries look like "1-5:1.0"; we want the device itself.
        if ":" in name:
            continue
        try:
            with open(os.path.join(base, "idVendor"), "r") as fh:
                have_vid = int(fh.read().strip(), 16)
            with open(os.path.join(base, "idProduct"), "r") as fh:
                have_pid = int(fh.read().strip(), 16)
        except (OSError, ValueError):
            continue
        if (have_vid, have_pid) != (vid, pid):
            continue

        try:
            with open(os.path.join(base, "descriptors"), "rb") as fh:
                blob = fh.read()
        except OSError:
            continue

        off = 0
        while off + 2 <= len(blob):
            dlen, dtype = blob[off], blob[off + 1]
            if dlen < 2 or off + dlen > len(blob):
                break
            if dtype == 0x02:  # configuration descriptor
                total = struct.unpack_from("<H", blob, off + 2)[0]
                total = max(9, min(total, len(blob) - off))
                out.append(blob[off:off + total])
            off += dlen
    return out


def _parse_config_blob(blob: bytes) -> Optional[tuple]:
    """Walk a configuration descriptor blob.

    Returns ``(iface_no, ep_out, ep_in, out_mps, in_mps, out_tt, in_tt)`` for
    the first interface exposing a usable IN and OUT endpoint, else ``None``.

    The chain is walked sequentially rather than assuming endpoints sit at a
    fixed offset after the interface descriptor: class-specific descriptors may
    come between them. On the GT Max a 9-byte HID descriptor (0x21) sits
    exactly there, which an offset-based parser reads as garbage.
    """
    if len(blob) < 9 or blob[1] != 0x02:
        return None
    total = min(struct.unpack_from("<H", blob, 2)[0], len(blob))

    cur_iface: Optional[int] = None
    ep_in = ep_out = None
    in_mps = out_mps = 0
    in_tt = out_tt = None

    off = 9
    while off + 2 <= total:
        dlen, dtype = blob[off], blob[off + 1]
        if dlen < 2 or off + dlen > total:
            break

        if dtype == 0x04 and dlen >= 9:            # interface descriptor
            (_bl, _dt, iface_no, _alt, num_eps, iface_class,
             _sub, _proto, _iif) = struct.unpack_from("<BBBBBBBBB", blob, off)
            # 0xFF = vendor specific, 0x03 = HID.
            cur_iface = iface_no if (iface_class in (0xFF, 0x03) and num_eps) else None
            ep_in = ep_out = None
            in_mps = out_mps = 0
            in_tt = out_tt = None

        elif dtype == 0x05 and dlen >= 7 and cur_iface is not None:
            (_bl, _dt, addr, attrs, mps, _iv) = struct.unpack_from(
                "<BBBBHB", blob, off)
            # bmAttributes & 0x03: 0 control, 1 iso, 2 bulk, 3 interrupt
            tt = attrs & 0x03
            if tt in TRANSFER_TYPES_OK:
                if addr & LIBUSB_ENDPOINT_IN:
                    if ep_in is None:
                        ep_in, in_mps, in_tt = addr, mps, tt
                elif ep_out is None:
                    ep_out, out_mps, out_tt = addr, mps, tt
            if ep_in is not None and ep_out is not None:
                return (cur_iface, ep_out, ep_in, out_mps, in_mps, out_tt, in_tt)

        off += dlen
    return None


def _describe_device(vid: int, pid: int) -> str:
    """Interface/endpoint listing for every configuration, for diagnosis.

    Never raises: it runs *after* a lookup has already failed, so a malformed
    blob must produce a readable report instead of a second exception that
    hides the real problem.
    """
    out: List[str] = [f"looking for {vid:04X}:{pid:04X} in {SYSFS_USB_DEVICES}"]
    blobs = _sysfs_config_blobs(vid, pid)
    if not blobs:
        out.append("no sysfs descriptors matched; the device may have just been "
                   "replugged, or /sys/bus/usb/devices is not mounted.")
        return "\n".join(out)
    for i, blob in enumerate(blobs):
        out.append(f"config {i}: {_summarise_blob(blob)}")
        out.append(f"  usable interface: {_parse_config_blob(blob)}")
        out.extend(_dump_raw_config_blob(blob))
    out.append(
        "note: only BULK or INTERRUPT endpoints are accepted; "
        "CONTROL/ISOCHRONOUS are skipped."
    )
    return "\n".join(out)


def _summarise_blob(blob: bytes) -> str:
    if len(blob) < 9 or blob[1] != 0x02:
        return f"not a configuration descriptor ({len(blob)} bytes)"
    total = struct.unpack_from("<H", blob, 2)[0]
    return (f"bNumInterfaces={blob[4]} bConfigurationValue={blob[5]} "
            f"bmAttributes=0x{blob[7]:02x} MaxPower={blob[8]} wTotalLength={total}")


def format_frame(raw: bytes, label: str) -> str:
    """Hex + ASCII of a raw frame, plus a list of its non-zero bytes.

    ``label`` is required and names the frame, e.g. ``"0xE3 status report"``.
    It used to default to a bare ``"response"``, which is why the status report
    logged without saying which command produced it while the other two named
    themselves. Every dump in the log now goes through this one function, so a
    frame is rendered the same way wherever it appears.

    Callers that print this inside a ``===== title =====`` block should pass the
    same wording as the title, minus the block decoration.
    """
    lines = [f"{label} ({len(raw)} bytes)"]
    for i in range(0, len(raw), 16):
        chunk = raw[i:i + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  [{i:02x}] {hexs:<47} {text}")
    nz = "  ".join(f"[{i:02x}]={raw[i]}" for i in range(len(raw)) if raw[i])
    lines.append("")
    lines.append(f"non-zero bytes: {nz or '(none)'}")
    return "\n".join(lines)


def _dump_raw_config_blob(blob: bytes) -> List[str]:
    """Human-readable hex + descriptor walk, for the failure report."""
    lines = [f"  raw ({len(blob)} bytes):"]
    for o in range(0, len(blob), 16):
        chunk = blob[o:o + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"    +{o:03x}  {hexs:<47}  {text}")
    if len(blob) >= 9 and blob[1] == 0x02:
        total = min(struct.unpack_from("<H", blob, 2)[0], len(blob))
        lines.append(
            f"  header: bNumInterfaces={blob[4]} bConfigurationValue={blob[5]} "
            f"bmAttributes=0x{blob[7]:02x} MaxPower={blob[8]} wTotalLength={total}"
        )
        off = 9
        while off + 2 <= total:
            dlen, dtype = blob[off], blob[off + 1]
            if dlen < 2 or off + dlen > total:
                lines.append(f"    +{off:03x} truncated/invalid (dlen={dlen})")
                break
            if dtype == 0x04 and dlen >= 9:
                (_b, _d, ino, alt, ne, cls, sub, proto, _ii) = struct.unpack_from(
                    "<BBBBBBBBB", blob, off)
                lines.append(
                    f"    +{off:03x} interface {ino} alt {alt}: class=0x{cls:02x} "
                    f"sub=0x{sub:02x} proto=0x{proto:02x} endpoints={ne} "
                    f"{'accepted' if cls in (0xFF, 0x03) and ne else 'REJECTED'}"
                )
            elif dtype == 0x05 and dlen >= 7:
                (_b, _d, addr, attrs, mps, ival) = struct.unpack_from(
                    "<BBBBHB", blob, off)
                tt = attrs & 0x03
                lines.append(
                    f"    +{off:03x}   endpoint 0x{addr:02X} "
                    f"{'IN ' if addr & LIBUSB_ENDPOINT_IN else 'OUT'} "
                    f"{transfer_type_name(tt):<12} mps={mps} interval={ival} "
                    f"{'usable' if tt in TRANSFER_TYPES_OK else 'ignored'}"
                )
            off += dlen
    return lines


class RayNeoDevice:
    """A connected pair of RayNeo glasses.

    Instances are cheap; all I/O is serialised with an internal lock because the
    glasses only tolerate one outstanding control transfer at a time.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handle: Optional[ctypes.c_void_p] = None
        self._device: Optional[ctypes.c_void_p] = None
        self._interface: int = -1
        self._ep_in: Optional[int] = None
        self._ep_out: Optional[int] = None
        self._in_len: int = FRAME_SIZE
        self._out_len: int = FRAME_SIZE
        #: True when the descriptor said the endpoint is interrupt (type 3)
        #: rather than bulk (type 2). The GT Max is interrupt-only.
        self._in_interrupt: bool = True
        self._out_interrupt: bool = True
        #: VID/PID of the device found by _find_device(), used to locate its
        #: descriptors under sysfs.
        self._vid: int = 0
        self._pid: int = 0
        #: Callbacks invoked as ``fn(cmd, response)`` for every valid frame
        #: read, which is only while a reply is being awaited (see
        #: _await_response).
        self._frame_listeners: List = []
        #: Set while a user-initiated mutation is in flight. The background
        #: poller skips while it is set: both share ``_lock``, so a poll holding
        #: it for its timeout window delayed the next setting change by up to
        #: that window, which is very visible on a dropdown.
        self._mutating = threading.Event()
        #: Trace hook, called as ``fn(text)`` for every frame sent. Used to log
        #: the exact wire bytes a setting produces.
        self._tx_listener = None
        #: Before/after response diffing. A discovery aid; see _apply_probed.
        self._probe_enabled = False
        #: True only inside calibrate_status(), which suppresses the post-write
        #: confirmations so they cannot add a read in the middle of a diff.
        self._calibrating = False
        #: Monotonic time until which the background poll stands down, or 0.
        #:
        #: A deadline rather than a flag on purpose. A boolean that is never
        #: cleared stops live updates for the rest of the session with no
        #: visible cause -- the _mutating leak did exactly that -- so a lost
        #: pause can only ever cost a little extra polling.
        self._poll_paused_until = 0.0
        #: Per-setting counter of requests that are waiting to be read back.
        #:
        #: The silent commands have no reply, so the only evidence that one
        #: landed is the status report -- which is also the only thing that can
        #: be checked. But the user can pick a second value while the first is
        #: still settling, and then the first one's read-back reports the
        #: *second* one's value. Treating that as a disagreement invents a
        #: device fault out of a fast user: picking 电影 and then 护眼 within a
        #: second produced two WARNs claiming the byte at 0x12 was probably not
        #: the scene-mode byte, when it was exactly what had just been written.
        #:
        #: Keyed per setting, because only a newer request for the *same*
        #: setting makes an older read-back meaningless.
        self._confirm_seq: Dict[str, int] = {}
        #: Last unrecognised screen-size byte seen in a status report, so a
        #: persistent one is reported once rather than every poll.
        self._bad_screen_size: Optional[int] = None
        self._info = DeviceInfo(
            luminance=0,
            max_luminance=_default_max_luminance(),
            volume=0,
            max_volume=DEFAULT_MAX_VOLUME,
        )

    # -- discovery --------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._handle is not None

    def _find_device(self) -> ctypes.c_void_p:
        """Locate the first device matching a known VID/PID."""
        usb = libusb()
        ctx = usb.ensure()
        device_list = ctypes.POINTER(ctypes.c_void_p)()
        count = usb.lib.libusb_get_device_list(ctx, ctypes.byref(device_list))
        if count < 0:
            raise RayNeoError(f"libusb_get_device_list failed: {count}")
        desc = ctypes.create_string_buffer(18)
        try:
            for i in range(count):
                dev = device_list[i]
                if usb.lib.libusb_get_device_descriptor(dev, desc) != 0:
                    continue
                vid, pid = struct.unpack_from("<HH", desc.raw, 8)
                if (vid, pid) in KNOWN_IDS:
                    usb.lib.libusb_ref_device(dev)
                    self._vid, self._pid = vid, pid
                    return ctypes.c_void_p(dev if isinstance(dev, int)
                                           else dev.value)
                if (vid, pid) in DFU_IDS:
                    # Never claim a firmware-update interface. Refuse loudly so a
                    # half-completed official DFU is not mistaken for "glasses
                    # not connected".
                    raise RayNeoError(
                        f"Glasses are in DFU/firmware-update mode "
                        f"(vid=0x{vid:04X} pid=0x{pid:04X}). Refusing to touch "
                        f"them. Power-cycle the glasses to leave DFU mode."
                    )
        finally:
            # The second argument is libusb's `unref_devices` *boolean*, not a
            # count. Passing `count` here unrefs every device in the list that
            # many times, driving the refcounts to zero and leaving `dev` as a
            # dangling pointer -> segfault on the next call. We took our own
            # reference above with libusb_ref_device, so drop only the one the
            # list itself holds.
            usb.lib.libusb_free_device_list(device_list, 1)
        raise DeviceNotFound(
            "No RayNeo glasses found. Plug them in over USB-C. (expected VID "
            f"{'/'.join(f'{v:04X}' for v, _ in KNOWN_IDS)}, PID AF50)"
        )

    def _find_control_interface(self, vid: int, pid: int) -> Optional[tuple]:
        """Find a vendor/HID interface exposing one usable IN and one OUT.

        Mirrors ``XRUsbConnection::QueryInterface``: accept ``bInterfaceClass``
        of 0xFF (vendor specific) or 0x03 (HID) with at least one endpoint, then
        require a bulk *or interrupt* IN and OUT on that same interface. The
        GT Max is a single HID interface with two interrupt endpoints
        (0x81 in / 0x02 out, 64 bytes, bInterval 4 ms).

        Descriptors come from sysfs (see ``_sysfs_config_blobs``), not from
        libusb, whose struct layout cannot be walked reliably through ctypes.
        """
        for blob in _sysfs_config_blobs(vid, pid):
            found = _parse_config_blob(blob)
            if found:
                return found
        return None

    def open(self) -> bool:
        """Open the glasses and claim the control interface. Idempotent."""
        with self._lock:
            if self._handle is not None:
                return True
            usb = libusb()
            dev = self._find_device()  # takes a reference we must release
            owned = True
            handle = ctypes.c_void_p()
            opened = False
            try:
                # NOTE: libusb_open() must come first. libusb_get_configuration()
                # takes a libusb_device_handle* and dereferences handle+0x40 to
                # reach the inner device; handing it the libusb_device* from
                # enumeration segfaults.
                rc = usb.lib.libusb_open(dev, ctypes.byref(handle))
                if rc != LIBUSB_SUCCESS:
                    # Only -3 is a permissions problem. Saying "check udev" for
                    # every failure sent the reader after udev rules when the
                    # glasses had simply been unplugged: -4 is NO_DEVICE, which
                    # is what pulling the cable mid-command produces.
                    hint = ("Check udev permissions."
                            if rc == LIBUSB_ERROR_ACCESS
                            else "Reconnect the glasses and press Connect.")
                    raise RayNeoError(
                        f"Could not open the glasses (libusb error {rc}). "
                        f"{hint}"
                    )
                opened = True

                picked = self._find_control_interface(self._vid, self._pid)
                if picked is None:
                    raise RayNeoError(
                        "Glasses found but no interface with a usable IN and OUT "
                        "endpoint was found. Descriptor dump:\n"
                        f"{_describe_device(self._vid, self._pid)}"
                    )
                iface_no, ep_out, ep_in, out_len, in_len, out_tt, in_tt = picked

                # The official app detaches the kernel driver before claiming.
                usb.lib.libusb_set_auto_detach_kernel_driver(handle, 1)
                rc = usb.lib.libusb_claim_interface(handle, iface_no)
                if rc != LIBUSB_SUCCESS:
                    raise RayNeoError(
                        f"Could not claim interface {iface_no} (libusb error {rc}). "
                        "Another program (e.g. the official app) may hold it."
                    )

                self._handle = handle
                self._device = dev
                self._interface = iface_no
                self._ep_out = ep_out
                self._ep_in = ep_in
                self._out_len = max(out_len, FRAME_SIZE)
                self._in_len = max(in_len, FRAME_SIZE)
                self._out_interrupt = (out_tt == 3)
                self._in_interrupt = (in_tt == 3)
                self._info.connected = True
                owned = False  # ownership handed to self._device
                return True
            finally:
                if opened and self._handle is None:
                    try:
                        usb.lib.libusb_close(handle)
                    except Exception:
                        pass
                if owned:
                    usb.lib.libusb_unref_device(dev)

    def close(self) -> None:
        with self._lock:
            usb = libusb()
            if self._handle is not None:
                try:
                    usb.lib.libusb_release_interface(self._handle, self._interface)
                except Exception:
                    pass
                usb.lib.libusb_close(self._handle)
                self._handle = None
            if self._device is not None:
                usb.lib.libusb_unref_device(self._device)
                self._device = None
            self._interface = -1
            self._ep_in = self._ep_out = None
            self._info.connected = False

    # -- low-level I/O ----------------------------------------------------------
    #
    # The GT Max exposes a single HID interface whose two endpoints are
    # *interrupt*, not bulk (bmAttributes == 3), 64 bytes each, bInterval 4 ms.
    # libusb_bulk_transfer() hardcodes USB_ENDPOINT_XFER_BULK internally and
    # returns EINVAL on an interrupt endpoint, so pick the helper from the
    # transfer type we found in the descriptor.

    def _write_fn(self):
        usb = libusb()
        return (usb.lib.libusb_interrupt_transfer if self._out_interrupt
                else usb.lib.libusb_bulk_transfer)

    def _read_fn(self):
        usb = libusb()
        return (usb.lib.libusb_interrupt_transfer if self._in_interrupt
                else usb.lib.libusb_bulk_transfer)

    def _bulk_write(self, data: bytes, timeout_ms: int = 200) -> None:
        if self._handle is None or self._ep_out is None:
            raise RayNeoError("Glasses are not connected")
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        transferred = ctypes.c_int(0)
        rc = self._write_fn()(
            self._handle,
            ctypes.c_ubyte(self._ep_out),
            ctypes.cast(buf, ctypes.c_void_p),
            len(data),
            ctypes.byref(transferred),
            timeout_ms,
        )
        if rc != LIBUSB_SUCCESS:
            if rc in (LIBUSB_ERROR_PIPE, LIBUSB_ERROR_TIMEOUT):
                self.clear_halt()
            raise RayNeoError(
                f"USB write failed (libusb error {rc}"
                f"{', interrupt endpoint' if self._out_interrupt else ''})"
            )

    def clear_halt(self) -> None:
        """Clear the halt condition on both endpoints.

        A halted endpoint stays halted until this is called, and every later
        transfer then fails instantly -- the panel looks alive but nothing
        happens. PIPE (-9) is the usual reason, but TIMEOUT (-7) leaves the
        endpoint halted too, and three unanswered writes are enough to trigger
        it (observed on hardware).
        """
        usb = libusb()
        if self._handle is None:
            return
        for ep in (self._ep_out, self._ep_in):
            if ep is None:
                continue
            try:
                usb.lib.libusb_clear_halt(self._handle, ctypes.c_ubyte(ep))
            except Exception:
                pass

    def _bulk_read(self, timeout_ms: int = 300) -> Optional[bytes]:
        if self._handle is None or self._ep_in is None:
            return None
        size = max(self._in_len, FRAME_SIZE)
        buf = (ctypes.c_ubyte * size)()
        transferred = ctypes.c_int(0)
        rc = self._read_fn()(
            self._handle,
            ctypes.c_ubyte(self._ep_in),
            ctypes.cast(buf, ctypes.c_void_p),
            size,
            ctypes.byref(transferred),
            timeout_ms,
        )
        if rc != LIBUSB_SUCCESS or transferred.value <= 0:
            return None
        return bytes(bytearray(buf)[: transferred.value])

    def send(
        self,
        command: int,
        value: int = 0,
        payload: bytes = b"",
        expect_response: bool = True,
        timeout_ms: int = 400,
        trace: bool = True,
    ) -> Optional[bytes]:
        """Build and send one command frame.

        Returns the matching response frame, or ``None`` when the device did not
        answer within ``timeout_ms``.

        ``trace=False`` suppresses the TX trace hook. The background poller
        uses it so periodic reads do not flood the log with identical frames.
        """
        if not 0 <= command <= 0xFF:
            raise ValueError(f"command out of range: {command}")
        if not 0 <= value <= 0xFF:
            raise ValueError(f"value out of range: {value}")

        frame = bytearray(FRAME_SIZE)
        frame[0] = CMD_MAGIC
        frame[1] = command & 0xFF
        frame[2] = value & 0xFF
        if payload:
            if 3 + len(payload) > FRAME_SIZE:
                raise ValueError("payload does not fit in a 64-byte frame")
            frame[3 : 3 + len(payload)] = payload

        started = time.monotonic()
        with self._lock:
            self._bulk_write(bytes(frame))
            if not expect_response:
                elapsed = (time.monotonic() - started) * 1000
                if trace:
                    self._trace(command, value, payload, None, elapsed)
                return None
            resp = self._await_response(command, timeout_ms)
            elapsed = (time.monotonic() - started) * 1000
            if trace:
                self._trace(command, value, payload, resp, elapsed)
            return resp

    def set_tx_listener(self, fn) -> None:
        """Install ``fn(text)`` to receive a line per transmitted frame."""
        self._tx_listener = fn

    def clear_tx_listener(self) -> None:
        self._tx_listener = None

    def is_mutating(self) -> bool:
        """True while a user-initiated change is in flight."""
        return self._mutating.is_set()

    # -- offset discovery -------------------------------------------------------
    #
    # Several device-info field offsets are still guesses, and hardcoding them
    # is what made colour-enhance, brightness and the maxima show the wrong
    # state. Rather than keep inferring the layout, we measure it: read the two
    # read-only responses, apply one change, read them again, and report the
    # bytes that moved. That byte *is* the field's offset.

    def _read_probes(self) -> Tuple[Optional[bytes], Optional[bytes]]:
        """Both read-only responses, without raising."""
        info = None
        report = None
        try:
            with self._lock:
                if self._handle is None:
                    return None, None
                info = self.send(CMD_ACQUIRE_DEVICE_INFO, timeout_ms=250,
                                 trace=False)
                report = self.send(CMD_STATUS_REPORT, timeout_ms=250,
                                   trace=False)
        except RayNeoError:
            pass
        return info, report

    @staticmethod
    def _diff_bytes(before: Optional[bytes], after: Optional[bytes],
                    named: bool = True) -> List[str]:
        if not before or not after:
            return []
        out = []
        for i in range(min(len(before), len(after))):
            if before[i] != after[i]:
                # Naming the field is the point during normal diffing: a bare
                # offset is what left three status-report fields mislabelled.
                # calibrate_status passes named=False, because there the whole
                # job is to find out which name belongs to which offset --
                # printing the assumed one would just restate the assumption.
                tag = (STATUS_FIELD_NAMES.get(i, DEVICE_INFO_FIELD_NAMES.get(i))
                       if named else None)
                out.append(f"[0x{i:02X}] {before[i]}->{after[i]}"
                           + (f"  {tag}" if tag else ""))
        return out

    def status_dump(self, raw: Optional[bytes] = None) -> str:
        """One 0xE3 response: the raw frame, then what each field means.

        Costs no extra transaction when ``raw`` is a frame the poll already
        fetched. Rendered by the same _dump_block as the other two, so all three
        read alike.
        """
        if raw is None:
            raw = self.read_status_report()
        return _dump_block("0xE3 status report", raw,
                           self.status_table(raw) if raw else "parsed  nothing")

    def protocol_dump(self) -> str:
        """All three read-only responses, one uniform block each.

        Order is by command id, so the log reads the same way every time no
        matter what asked for what. Every block is a ``raw`` hex section and a
        ``parsed`` table, so all three read alike.
        """
        info = self.refresh_device_info()
        blocks = [
            _dump_block("0x00 device info",
                        getattr(self, "_raw_device_info", None),
                        self.device_info_table(info)),
        ]
        with self._lock:
            self.open()
            raw_e0 = self.send(CMD_GET_FUNC_SUPPORT, timeout_ms=800)
        blocks.append(_dump_block("0xE0 capabilities", raw_e0,
                                  self.capability_table(raw_e0)))
        raw_e3 = self.read_status_report()
        blocks.append(_dump_block("0xE3 status report", raw_e3,
                                  self.status_table(raw_e3) if raw_e3
                                  else "parsed  nothing"))
        return "\n\n".join(blocks)

    def capability_table(self, raw: Optional[bytes]) -> str:
        """The capability bitmap as a table: flag, offset, value, SDK name, key.

        Rendered this way rather than as key = value because each row relates
        four separate things, and a flat pair loses the columns that make the
        mapping checkable by eye against the response bytes.
        """
        rev: Dict[int, List[str]] = {}
        for name, flag in CAPABILITY_FLAGS.items():
            rev.setdefault(flag, []).append(name)
        caps = self._capabilities_from(raw)
        if raw is None:
            return "parsed  0xE0 got no response; showing defaults."
        rows = []
        for flag, off in enumerate(CAPABILITY_FLAG_OFFSETS):
            if off >= len(raw):
                continue
            field = (CAPABILITY_FIELD_NAMES[flag]
                     if flag < len(CAPABILITY_FIELD_NAMES) else "?")
            keys = ", ".join(sorted(rev.get(flag, ()))) or "-"
            on = "yes" if any(caps.get(k, False) for k in rev.get(flag, ())) else "-"
            rows.append([f"{flag:>4}", f"0x{off:02X}", f"{raw[off]:>3}",
                         field, keys, on])
        return _dump_table(["flag", "resp", "val", "SDK field", "plugin key", "on"],
                           rows)

    def device_info_table(self, info) -> str:
        """The parsed state as a table, with the byte each value came from.

        The source column is the point: it is what makes the map checkable
        against the raw frames above, which is the whole reason this dump exists.
        A dash means the value is not on the wire at all -- computed locally,
        read from the SDK's own configuration, or derived from another field.
        """
        rows = []
        for key, value in info.to_dict().items():
            if key == "raw":
                continue
            command, off = DEVICE_INFO_FIELD_ORIGIN.get(key, (0x00, -1))
            src = "-" if off < 0 else f"{command:02X}:{off:02X}"
            rows.append([key, str(value), src])
        return _dump_table(["field", "value", "cmd:off"], rows)

    def status_table(self, raw: bytes) -> str:
        """The decoded status report as a table, one row per known field."""
        rows = [[f"{off:02X}", name, str(raw[off]) if off < len(raw) else ""]
                for off, name in sorted(STATUS_FIELD_NAMES.items())]
        return _dump_table(["off", "field", "val"], rows)

    def status_fields(self, raw: Optional[bytes] = None) -> List[str]:
        """Named decode of the 0xE3 report, for the log and the debug panel.

        Costs nothing extra if ``raw`` is a frame already fetched this poll;
        otherwise it sends the query itself.
        """
        if raw is None:
            raw = self.read_status_report()
        if not raw:
            return []
        return [f"0x{off:02X}={raw[off]:<3d} {name}"
                for off, name in sorted(STATUS_FIELD_NAMES.items())
                if off < len(raw)]

    def probe_change(self, label: str, apply_fn) -> None:
        """Apply ``apply_fn`` and log which response bytes it moved.

        Read-only before and after, so it is safe to leave enabled; the extra
        traffic is five transactions -- two probe reads either side of the
        change, plus a confirming status report. Failures are swallowed -- this
        is diagnostics and must never break a setting change.
        """
        before_info, before_rep = self._read_probes()
        apply_fn()
        # The firmware may need a moment to reflect the change.
        time.sleep(0.12)
        after_info, after_rep = self._read_probes()

        lines = [f"--- {label} ---"]
        di = self._diff_bytes(before_info, after_info)
        dr = self._diff_bytes(before_rep, after_rep)
        lines.append("0x00 device info: " + (", ".join(di) if di else "(no change)"))
        lines.append("0xE3 status    : " + (", ".join(dr) if dr else "(no change)"))
        named = self.status_fields(after_rep)
        if named:
            lines.append("0xE3 after     : " + "  ".join(named))
        hook = self._tx_listener
        if hook is not None:
            for line in lines:
                hook(line)

    def _trace(self, command: int, value: int, payload: bytes,
               resp: Optional[bytes], elapsed_ms: float) -> None:
        """Report one transmitted frame to the trace hook, if one is installed."""
        hook = self._tx_listener
        if hook is None:
            return
        head = f"{CMD_MAGIC:02X} {command:02X} {value:02X}"
        if payload:
            head += " " + " ".join(f"{b:02X}" for b in payload)
        got = "no reply" if resp is None else resp[:12].hex()
        try:
            hook(f"TX 0x{command:02X}  [{head}]  -> {got}  ({elapsed_ms:.0f} ms)")
        except Exception:
            pass

    def _await_response(self, command: int, timeout_ms: int) -> Optional[bytes]:
        """Read frames until one echoes ``command``, or the deadline passes.

        This is the ONLY place the USB IN endpoint is read. There is no
        background reader, so nothing is read while the plugin is idle: frames
        the glasses push on their own sit in the buffer until the next command
        happens to read them. All authoritative state therefore comes from the
        poll in main.py, not from anything arriving here.

        While a reply *is* being awaited, other frames can turn up (IMU
        samples, key events). They go through :meth:`_on_frame` before the
        filter drops them, so they are not silently discarded.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return None
            data = self._bulk_read(remaining_ms)
            if not data:
                continue
            if len(data) < FRAME_SIZE or data[0] != RESP_MAGIC:
                continue
            echoed = data[RESP_CMD_OFFSET]
            self._on_frame(echoed, data)
            if echoed != command:
                continue
            return data

    # -- frame echo -------------------------------------------------------------

    def add_frame_listener(self, fn) -> None:
        """Register ``fn(cmd, response)``, called for every valid frame seen."""
        with self._lock:
            self._frame_listeners.append(fn)

    def remove_frame_listener(self, fn) -> None:
        with self._lock:
            if fn in self._frame_listeners:
                self._frame_listeners.remove(fn)

    def _on_frame(self, cmd: int, data: bytes) -> None:
        """Apply a state change carried by a frame, then notify listeners."""
        with self._lock:
            if self._apply_event_frame(cmd, data):
                listeners = list(self._frame_listeners)
            else:
                listeners = []
        for fn in listeners:
            try:
                fn(cmd, data)
            except Exception:
                # A listener must never break the I/O path.
                pass

    def _apply_event_frame(self, cmd: int, data: bytes) -> bool:
        """Fold a frame that echoes one of our own writes into :attr:`_info`.

        Returns True when something actually changed.

        These are echoes of commands *this plugin* sent, so they only ever
        reflect what we just did -- never a change made on the glasses' own
        side. Confirmed state always comes from the read-only poll; this just
        makes our own writes show up without waiting for it.
        """
        payload = data[9] if len(data) > 9 else 0
        info = self._info
        changed = False

        if cmd == CMD_SET_BRIGHTNESS and 9 < len(data):
            applied = payload
            if applied != info.luminance:
                info.luminance = applied
                changed = True
        elif cmd == CMD_SET_AUDIO_VOLUME and 9 < len(data):
            if payload != info.volume:
                info.volume = payload
                changed = True
        elif cmd == CMD_SET_AUDIO_MODE and 9 < len(data):
            if payload != info.audio_mode:
                info.audio_mode = payload
                info.whisper = payload == 1
                changed = True
        elif cmd == CMD_SET_COLOR_ENHANCE:
            val = bool(payload)
            if val != info.color_enhance:
                info.color_enhance = val
                changed = True
        elif cmd == CMD_SET_HIGH_DYNAMIC:
            val = bool(payload)
            if val != info.high_dynamic:
                info.high_dynamic = val
                changed = True
        elif cmd == CMD_SET_AUDIO_TUBE:
            # Deliberately ignored. The reply to 0x48 is matched by command id
            # and this protocol carries no sequence number, so a write whose own
            # reply timed out leaves that reply to be consumed by the *next*
            # write -- which then reports the value before last. Measured over
            # one session of rapid toggling: 26 writes, 5 of them timing out, and
            # 34 state transitions instead of 26, with replies like
            #
            #     TX 0x48 [66 48 00] -> ...48010000     wrote 00, reply says 01
            #
            # Every one of those stale values moved the volume ceiling with it,
            # which is the "maximum changes irregularly when I toggle a few
            # times" report. The 0xE3 status report carries the device's state as
            # of the moment it answers rather than as of some earlier request, so
            # it is the only trustworthy source here; set_audio_tube already sets
            # the value optimistically, so the panel moves at once either way.
            pass
        elif cmd == CMD_SET_HDR_MODE:
            if payload != info.hdr_mode:
                info.hdr_mode = payload
                changed = True
        elif cmd in (CMD_SWITCH_TO_3D, CMD_SWITCH_TO_2D):
            val = cmd == CMD_SWITCH_TO_3D
            if val != info.display_mode_3d:
                info.display_mode_3d = val
                changed = True
        return changed

    # -- reading and decoding state ---------------------------------------------

    def refresh_device_info(self) -> DeviceInfo:
        """Read the full device-info block from the glasses.

        Also folds in the 0xE3 status report, which is where screen size,
        colour enhance and the audio tube actually live. Both queries are
        read-only.
        """
        with self._lock:
            self.open()
            resp = self.send(CMD_ACQUIRE_DEVICE_INFO, timeout_ms=600, trace=False)
            if resp is None:
                resp = self.send(CMD_ACQUIRE_DEVICE_INFO_2, timeout_ms=600,
                                 trace=False)
            if resp is None:
                raise RayNeoError("Timed out waiting for the device info reply")
            self._raw_device_info = resp
            self._apply_device_info(resp)
        try:
            self.read_status_report()
        except RayNeoError:
            # The info block alone is still useful.
            pass
        return self._info

    def snapshot(self) -> str:
        """Every read-only response, for before/after diffing.

        Changing one setting and diffing two snapshots pins an offset
        immediately, which is more reliable than inferring a layout. Worth doing
        for any status-report field not yet confirmed on hardware.

        The same rendering as the connect-time dump, so the two are directly
        comparable -- which is the whole point of diffing them.
        """
        return self.protocol_dump()

    def pause_polling(self, seconds: float = 1.0) -> None:
        """Ask the background poll to stand down for ``seconds``.

        Called while a slider is being dragged. Two things make that worth doing:

        - The poll shares ``_lock`` with every write, and a poll that lands on a
          wedged endpoint holds the lock across its full timeout window. That is
          enough to delay the debounced write at the end of a drag, which is the
          one write the user is actually waiting on.
        - The device still reports the pre-drag value throughout, because nothing
          has been transmitted yet. Applying it mid-drag puts the thumb back
          under the user's finger.

        A deadline, not a flag: if the caller never comes back, the poll resumes
        by itself.
        """
        self._poll_paused_until = time.monotonic() + max(0.0, float(seconds))

    def poll_state(self) -> Optional[DeviceInfo]:
        """Read-only refresh used by the background poller.

        Never raises and never writes to the glasses: it issues the same
        ``AcquireDeviceInfo`` query the official app uses to prime its UI, and
        returns ``None`` when the device is not answering so the caller can
        decide whether to keep waiting or report a disconnect.

        Bails out immediately while :attr:`_mutating` is set. Both paths share
        ``_lock``, so a poll that starts just before a user change would hold
        the lock for its whole timeout window -- that is what made a dropdown
        selection take about a second to appear.
        """
        if self._mutating.is_set():
            return self._info if self.connected else None
        if time.monotonic() < self._poll_paused_until:
            # Report the current state, not None: a pause is not a disconnect,
            # and the poller reads None as "the glasses stopped answering".
            return self._info if self.connected else None
        try:
            with self._lock:
                if self._handle is None:
                    return None
                # Both attempts wait generously. See POLL_QUERY_TIMEOUT_MS: the
                # short first attempt this used to make was chasing a bottleneck
                # that turned out to be the plugin bridge, and it cost a false
                # disconnect when a -7 wedge made three polls miss in a row.
                resp = self.send(CMD_ACQUIRE_DEVICE_INFO,
                                 timeout_ms=POLL_QUERY_TIMEOUT_MS,
                                 trace=False)
                if resp is None:
                    self.clear_halt()
                    resp = self.send(CMD_ACQUIRE_DEVICE_INFO,
                                     timeout_ms=POLL_QUERY_RETRY_MS,
                                     trace=False)
                if resp is None:
                    # Both attempts went unanswered. Say so, and say how long
                    # the lock was held, because that window is exactly what a
                    # concurrent write has to queue behind. Without this line
                    # the only symptom was brightness taking 261 ms with nothing
                    # in the log to explain it, which is a question this file
                    # cannot answer after the fact.
                    self._note(
                        f"poll: device info unanswered after both attempts "
                        f"({POLL_QUERY_TIMEOUT_MS} + {POLL_QUERY_RETRY_MS} ms), "
                        f"device lock held for that long"
                    )
                    return None
                self._apply_device_info(resp)
            self.read_status_report()
            return self._info
        except RayNeoError:
            return None

    def read_status_report(self) -> Optional[bytes]:
        """Raw command 0xE3 status report.

        This is the response that carries the panel settings -- brightness
        index, volume, audio mode, colour enhancement, audio tube, screen size,
        scene mode and picture quality -- because they live in a different
        struct region from the ones the 0x00 device-info block fills. See the
        STATUS_* table for the firmware derivation and for which entries are
        hardware-confirmed.
        """
        with self._lock:
            self.open()
            raw = self.send(CMD_STATUS_REPORT, timeout_ms=400, trace=False)
        if raw is not None:
            self._apply_status_report(raw)
        return raw

    def _apply_status_report(self, raw: bytes) -> None:
        """Fold the 0xE3 report into :attr:`_info`.

        Offsets come from the firmware's own parser for this frame; see the
        STATUS_* block above. Picture quality and scene mode are here too --
        they were believed to have no read-back until that parser was found,
        which is why they used to be frontend-only state.
        """
        def u8(off: int) -> Optional[int]:
            return raw[off] if len(raw) > off else None

        info = self._info
        idx = u8(STATUS_LUMINANCE_INDEX)
        if idx is not None:
            # Only trust it as an index if it is a plausible step for the
            # brightness table we would send from.
            limit = len(brightness_table(info.device_type, info.manufacturer))
            if idx <= limit:
                info.luminance = idx
        mode = u8(STATUS_AUDIO_MODE)
        if mode is not None and mode <= 2:
            info.audio_mode = mode
            info.whisper = mode == 1
        enhance = u8(STATUS_COLOR_ENHANCE)
        if enhance is not None:
            info.color_enhance = bool(enhance)
        tube = u8(STATUS_AUDIO_TUBE)
        if tube is not None:
            tube_on = bool(tube)
            if tube_on != info.audio_tube:
                self._observe_audio_tube(tube_on, "status report 0xE3")
        size = u8(STATUS_SCREEN_SIZE)
        if size is not None:
            if size in SCREEN_SIZE_BY_VALUE:
                info.screen_size = SCREEN_SIZE_BY_VALUE[size]
                self._bad_screen_size = None
            elif size != self._bad_screen_size:
                # An unrecognised value used to be dropped in silence, leaving
                # whatever was there before. That is the one way this field can
                # lie without anything being wrong elsewhere: the panel would go
                # on showing a size the glasses are not set to.
                #
                # Worth naming because the app's byte values are not fully
                # accounted for. GlassDeviceManager sends `(i / 10) % 10` of
                # something the Flutter layer chose, and XRService::SetScreenSize
                # passes the byte through untouched, so 0/1/2 here rests on the
                # hardware calibration rather than on reading the app's
                # arithmetic -- which only yields 0/1/2 if Flutter sends
                # 100/110/120 rather than 100/115/85.
                #
                # Remembered, so a persistent unknown value is reported once
                # rather than on every poll.
                self._bad_screen_size = size
                self._note(
                    f"screen size: status report carries unrecognised value "
                    f"{size}; panel is showing {info.screen_size!r}"
                )

        # Picture quality. STATUS_HDR_MODE is the SDR / AI-HDR selector -- the
        # one the user toggles in the app. The two bytes either side of it are
        # related but different: one is the separate high-dynamic panel
        # feature, the other is whether HDR is active right now. A byte that
        # only ever reads 0 or 1 is therefore not evidence of being the mode.
        panel = u8(STATUS_HDR_MODE)
        if panel is not None and panel in HDR_MODES.values():
            info.hdr_mode = panel
        high = u8(STATUS_HIGH_DYNAMIC)
        if high is not None:
            info.high_dynamic = bool(high)
        active = u8(STATUS_HDR_ENABLED)
        if active is not None:
            info.hdr_enabled = bool(active)

        # Scene mode, read from the panel colour parameters byte.
        #
        # Read leniently on purpose. A strict `value in SCENE_MODES` guard
        # discards anything wider than an index, and since nothing else in this
        # frame moves when the scene changes, one discard is enough to leave the
        # panel showing the old mode forever. The write is confirmed separately,
        # so a wrong read is reported rather than silently believed.
        colour = u8(STATUS_PANEL_COLOR_PARAMS)
        if colour is not None:
            info.scene_mode = SCENE_MODE_BY_VALUE.get(colour)

        # The audio tube read above decides the volume ceiling, so this has to
        # come after it.
        info.volume_limit = self.volume_limit()

    def _apply_device_info(self, resp: bytes) -> None:
        """Decode an XrHidDeviceInfo response into a :class:`DeviceInfo`.

        Offsets marked "measured" were confirmed on real hardware by diffing
        the response across a single setting change. The rest are still
        unverified and are listed in docs/PROTOCOL.md as such.
        """
        def u8(off: int) -> Optional[int]:
            return resp[off] if len(resp) > off else None

        def u16(off: int) -> Optional[int]:
            if len(resp) <= off + 1:
                return None
            return struct.unpack_from("<H", resp, off)[0]

        info = self._info
        info.connected = True
        info.device_type = u8(0x15)
        info.firmware_version = u16(DEVICE_INFO_FIRMWARE_VERSION)
        info.firmware_build = firmware_build(resp)

        id_chars = [u8(0x26), u8(0x27)]
        if all(c is not None for c in id_chars):
            info.glasses_id = "".join(f"{c:02X}" for c in id_chars)

        info.frame_rate = u8(DEVICE_INFO_FRAME_RATE)
        info.luminance_value = u8(DEVICE_INFO_LUMINANCE_VALUE)
        info.volume = u8(0x2A)
        reported_max_volume = u8(DEVICE_INFO_MAX_VOLUME)
        if reported_max_volume:
            info.max_volume = reported_max_volume
        sbs = u8(0x2B)
        info.display_mode_3d = None if sbs is None else bool(sbs)
        wake = u8(0x2C)
        info.wakeup = None if wake is None else (wake == 0)
        info.audio_mode = u8(0x2D)
        mute = u8(0x2E)
        info.mute = None if mute is None else bool(mute)
        info.whisper = None if info.audio_mode is None else (info.audio_mode == 1)
        info.panel_distance = u8(0x2F)
        # NOTE: reply[0x30] is NOT the high-dynamic flag. The 0x00 reply parser
        # writes it into XrHidDeviceInfo::lsensorValid (0xB5), alongside 0x31 ->
        # gsensorValid and 0x32 -> msensorValid. High dynamic arrives on the
        # status report instead, at reply[0x13].
        #
        # NOTE: hdr_mode is deliberately NOT read here. This slot used to carry
        # u8(0x3D), which is maxVolume -- the firmware's reply parser writes
        # reply[0x3D] into XrHidDeviceInfo::maxVolume, not into anything HDR.
        # That is why the log used to show "hdrMode: 16" while the glasses were
        # in SDR. Picture quality is a status-report field (reply[0x14]).
        #
        # The panel vendor is likewise not carried in the response (the SDK
        # reads it from its own config), so it stays None unless something set
        # it. The brightness table selection copes with that: anything that is
        # neither a Samsung panel nor deviceType 32 uses the 29-step table.
        info.raw = {
            "b30": u8(0x30) or 0,
            "b31": u8(0x31) or 0,
            "b32": u8(0x32) or 0,
            "b33": u8(0x33) or 0,
            "b3d": u8(DEVICE_INFO_MAX_VOLUME) or 0,   # maxVolume, not HDR
            "b3e": u8(0x3E) or 0,
        }
        return None

    # -- brightness and volume --------------------------------------------------

    def set_brightness(self, index: int) -> None:
        """Stage a panel brightness step -- sent immediately, on every change.

        ``index`` is the UI step (0-based). The value put on the wire is
        ``table[index]``, matching ``XRService::PanelLunaSet``; an out-of-range
        index is transmitted as 0xFF.

        Command 0x09 produces no reply on this firmware -- 23 sends in the TX
        trace, zero replies -- so we do not wait for one.

        This only *stages*. The commit is :meth:`save_brightness`, which is a
        separate frame the official app also sends separately, so the two are
        separate methods here too.
        """
        self._mutating.set()
        table = brightness_table(self._info.device_type, self._info.manufacturer)
        levels = self.brightness_levels()
        idx = max(0, min(int(index), levels - 1))
        value = table[idx] if 0 <= idx < len(table) else BRIGHTNESS_OUT_OF_RANGE

        def _do() -> None:
            with self._lock:
                self.open()
            # PanelLunaSet -> SendHidCommand(handle, 9, table[i], 0, 0).
            #
            # This is the applying half, not a preview. The app's own
            # PanelLunaSet calls UpdateDeviceState itself on success, so the app
            # treats the Set as the point at which brightness has landed; the
            # separate 0x0D is a persistence nudge. Measured, a brightness change
            # round-trips in 6-11 ms and feels immediate with or without the
            # commit on the critical path.
            self._write_panel(CMD_SET_BRIGHTNESS, value)
            self._info.luminance = idx

        label = f"brightness index={idx} value={value}"
        if idx != int(index):
            label += f" (asked {index})"
        self._apply_probed(label, _do)

    def save_brightness(self) -> Optional[int]:
        """Commit the staged brightness, then read the panel back.

        The second half of the official app's brightness change. AirApi.java
        exposes the two separately::

            public void setBrightnessIndex(int i) { ... PanelLuminanceSet(i); }
            public void saveBrightness()          { ... PanelLuminanceSave(); }

        and GlassDeviceManager dispatches them on two different Flutter
        callbacks -- case 4 for the index, case 5 for the save -- so the app
        stages a value on every tick of a drag and commits once at the end.

        ``PanelLunaSave`` (@ 0x15a7a4) is
        ``SendHidCommand(handle, 0x0D, 0, 0, 0)``: no payload, so it commits
        whatever was staged. The two stubs after it are the same shape sending
        0x0E and 0x0F, PanelPowerOn and PanelPowerOff.

        Returns the step the panel actually reports, so the caller can put the
        slider back where the hardware is rather than where it was asked to go.
        That read-back is the point of the whole exercise: 0x09 is silent, so
        without it the panel would be showing a value nobody had confirmed.

        This replaces an earlier version of this method that sat unused behind
        ``send_raw`` and was written off as doing nothing. The evidence given
        then was that brightness read back correctly straight after a plain
        brightness write -- but a status report reports what was *staged*, so
        that observation could not tell staged from committed and proved
        nothing about the panel's behaviour. The user reporting that the panel
        caught up late is what settled it.
        """
        self._mutating.set()
        staged = self._info.luminance
        confirmed: Optional[int] = None

        def _do() -> None:
            nonlocal confirmed
            with self._lock:
                self.open()
            self._write_panel(CMD_SAVE_BRIGHTNESS, 0)
            # Two probes, not _settle_then_read's seven.
            #
            # The full backoff runs up to 2.2 s and takes the device lock for a
            # status read on every attempt. Measured, it took 1874 ms -- and every
            # brightness stage in that window queued behind it, which is most of
            # why the slider went from a 515 ms median round trip to 1556 ms once
            # stages went out per tick. The commit cannot afford to be the slow
            # thing in the room.
            #
            # If the first probe already agrees, the panel took the commit and
            # there is nothing to wait for. If it disagrees, one more probe after
            # a short pause covers a panel that needed a moment; beyond that the
            # background poll is the authority and will correct the state.
            #
            # 0x0D wedges the interrupt endpoint. Measured: 16 of 18 commits were
            # followed within ~460 ms by `USB write failed (libusb error -7)` on
            # the *next* transfer, and every brightness write after a commit cost
            # 468-654 ms because _bulk_write had to sit through its 200 ms timeout
            # before failing. Clearing the halt straight away is the same treatment
            # _write_panel already gives the other silent commands, and it moves the
            # cost inside the commit -- where the debounce means nobody is waiting --
            # instead of leaking it onto the user's next drag.
            self.clear_halt()
            for pause in BRIGHTNESS_CONFIRM_DELAYS:
                time.sleep(pause)
                try:
                    raw = self.read_status_report()
                except RayNeoError as exc:
                    # The commit frame itself went out; only the *check* failed.
                    # Raising here would report a change as refused when it was in
                    # fact sent, which is what happened: 16 "REFUSED the commit"
                    # lines, every one of them following a successful TX 0x0D.
                    self._note(
                        f"brightness commit sent but could not be read back "
                        f"({exc}); leaving the state alone"
                    )
                    return
                if raw is None:
                    # Nothing came back, so there is no answer to give. Leave the
                    # state alone rather than inventing one.
                    return
                got = raw[STATUS_LUMINANCE_INDEX]
                # _apply_status_report already folded this into _info, so
                # publishing it is enough for the panel to correct the slider.
                confirmed = got
                if got == staged:
                    return
            # Reported once, after the last probe -- not inside the loop. Logging
            # per probe put two identical WARNs in the log for every disagreement,
            # which is the same noise the audio-tube path was fixed for: it makes
            # one refusal look like two.
            #
            # Not a warning at all, as it turns out. A disagreement here is the
            # documented behaviour of the panel, not a fault: the glasses stop
            # *reflecting* brightness once it passes the current display
            # strategy mode's band (wire 1..9, or 1..12 in the next mode up), so
            # a request above the band is accepted and reported back lower. That
            # is exactly why the official app's slider appears to fall back on
            # its own.
            #
            # WARNING here made normal operation look like a defect -- six and
            # twelve of them per session, all "asked 11, panel reports 8". The
            # read-back still does its job: the published state carries the
            # panel's value, so the thumb moves to where the hardware is. It just
            # does not shout about it.

        self._apply_probed(f"brightness save (staged {staged})", _do)
        return confirmed

    def brightness_levels(self) -> int:
        """Number of selectable brightness steps: :data:`BRIGHTNESS_STEPS`.

        Fixed rather than discovered. The device never reports a ceiling (see
        ``_default_max_luminance``) and the real one depends on the display
        strategy mode, which is not on the wire either. Watching for a step that
        comes back lower was tried and dropped: the mode can change at any time,
        so a ceiling learned once would be wrong the moment it did.
        """
        table = brightness_table(self._info.device_type, self._info.manufacturer)
        return max(1, min(BRIGHTNESS_STEPS, len(table)))

    def volume_levels(self) -> int:
        """Highest selectable volume index for the connected device.

        ``maxVolume`` is treated as a count, so the GT Max's 16 means indices
        0..15. **That is an inference, not a measurement.** The official app does
        nothing that settles it: ``getVolumeMax()`` returns ``GetAudioMaxVolume()``
        raw and ``setVolumeIndex()`` sends the index unchecked, and
        ``getVolumeMax()`` has no caller anywhere in the decompiled app -- the
        slider's ceiling lives in the Flutter layer.

        What *is* measured: the device reports its current volume as 15, so index
        15 exists. Index 16 has never been sent, so it is untested rather than
        known-bad.
        """
        # maxVolume is a COUNT, not an inclusive bound: 16 means indices 0..15.
        max_volume = self._info.max_volume or DEFAULT_MAX_VOLUME
        return max(0, max_volume - 1)

    def volume_limit(self) -> int:
        """Highest volume index currently allowed.

        The audio tube cannot take the overdrive range above 100 %, so with the
        tube on the ceiling drops to :data:`AUDIO_TUBE_VOLUME_LIMIT`. The
        firmware does not enforce this -- it accepts any index we send -- so the
        clamp is ours.
        """
        levels = self.volume_levels()
        if self._info.audio_tube:
            return min(levels, AUDIO_TUBE_VOLUME_LIMIT)
        return levels

    def set_volume(self, level: int) -> None:
        """Set the audio volume.

        Clamped to :meth:`volume_limit`. The device accepts out-of-range values
        without complaint, so without this the slider could park above the
        ceiling and simply look unresponsive.
        """
        self._mutating.set()
        target = max(0, min(int(level), self.volume_limit()))

        def _do() -> None:
            with self._lock:
                self.open()
                self.send(CMD_SET_AUDIO_VOLUME, target & 0xFF, timeout_ms=120)
                self._info.volume = target

        note = "" if target == int(level) else f" (asked {level})"
        self._apply_probed("volume " + str(target) + note, _do)

    def calibrate_status(self) -> List[str]:
        """Pin the 0xE3 offsets that are still firmware-derived only.

        Each step changes one setting, diffs the status report, and puts the
        setting back where it was. A diff names a *byte*; the firmware table
        only says which struct field a byte was inferred to feed, and that
        inference is exactly what went wrong last time -- 0x16..0x17 turn out to
        be one uint16, so "the colour byte" and "the tube byte" were never
        distinguishable from static analysis alone.

        Read-back is not enough on its own either: two settings at coinciding
        values move the same byte, so a single diff cannot tell them apart. This
        changes one setting at a time from a known baseline, which does.

        Costs a few dozen USB transactions and briefly changes what the user
        sees. Every step is individually guarded and every setting is restored,
        so a failure part-way leaves the glasses as they were found.
        """
        # Refresh first: the baseline is what gets restored at the end, and the
        # panel's own last-known values are the only thing that knows it.
        try:
            self.refresh_device_info()
            self.read_status_report()
        except RayNeoError as exc:
            return [f"calibration aborted: {exc}"]
        info = self.get_device_info()
        # Ordered hardest-first: the panel commands that never answer are also
        # the ones that wedge the endpoint, so they get the run of clear air
        # while it still works.
        baseline = {
            "scene mode": (info.scene_mode, self.set_scene_mode,
                           ["reading", "movie", "eyeProtection", "standard"]),
            "picture quality": (info.hdr_mode, self.set_hdr_mode, [1, 0]),
            "high dynamic": (info.high_dynamic, self.set_high_dynamic,
                            [True, False]),
            "colour enhance": (info.color_enhance, self.set_color_enhance,
                               [True, False]),
            "audio tube": (info.audio_tube, self.set_audio_tube, [True, False]),
            "screen size": (info.screen_size, self.set_screen_size,
                            ["small", "medium", "large"]),
        }

        out = ["0xE3 offset calibration -- every option of one setting at a "
               "time", ""]
        self._calibrating = True
        try:
            return self._calibrate_run(baseline, out)
        finally:
            self._calibrating = False

    def _calibrate_run(self, baseline, out: List[str]) -> List[str]:
        unrestored = []
        for label, (current, setter, options) in baseline.items():
            out.append(f"{label}   (was {current!r})")
            out += self._calibrate_step(label, setter, options)
            warning = self._calibrate_restore(label, setter, current)
            if warning:
                unrestored.append(f"  ! {warning}")

        out.append("")
        if unrestored:
            out += ["NOT fully restored -- the glasses are left as below:"]
            out += unrestored
            out.append("")
        out.append("An offset that moves for every option of a setting is that")
        out.append("setting's byte. One that moves for only some options is")
        out.append("something coupled to it. An offset shared by two settings")
        out.append("cannot be told apart by value -- the firmware reads")
        out.append("0x16..0x17 as one uint16, so that pair is a single word.")
        out.append("Anything reading '(nothing)' was not applied by the device.")
        return out

    def _calibrate_step(self, label: str, setter, options: list) -> List[str]:
        """Try every option of one setting, reporting which bytes each moves.

        Every option, not just one that differs from the current value: the
        current value is itself read from the offset being tested, so trusting it
        to pick a different target assumes the answer. A byte that moves for only
        some values is itself informative -- that is how a coupled field shows up
        rather than masquerading as the setting.

        The baseline is re-read before each option rather than shared across the
        setting, and the per-report checksum and counter are masked out. Doing
        neither made the first run mostly noise: 0x04, 0x05 and 0x06 moved for
        every option of every setting, purely because time had passed.
        """
        lines = []
        for target in options:
            lines.append(f"    {target!r:<16} "
                         + self._calibrate_apply(setter, target))
        return lines

    def _calibrate_apply(self, setter, target) -> str:
        """Write one value, settle, and report which bytes moved.

        Retried once through a recovered endpoint. The first run lost five of
        eighteen probes to a halt that recovery then cleared, so the answer was
        known for some settings and not others purely because of where the halt
        happened to land.
        """
        for attempt in (1, 2):
            result = self._calibrate_attempt(setter, target)
            if not result.startswith("(failed") and not result.startswith("(no base"):
                return result
            if attempt == 1:
                self._recover_endpoint()
        return result

    def _calibrate_attempt(self, setter, target) -> str:
        """One pass of _calibrate_apply: baseline, write, settle, diff."""
        # _mutating is held across the whole step: the background poller reads
        # this same device, and a transaction landing mid-diff shows up as a
        # byte that "moved".
        self._mutating.set()
        try:
            origin = self.read_status_report()
            if origin is None:
                return "(no baseline: status report did not answer)"
            setter(target)
            after = self._settle_then_read()
        except RayNeoError as exc:
            return f"(failed: {exc})"
        finally:
            self._mutating.clear()
        if after is None:
            return "(status report went quiet)"
        # Unnamed on purpose, and with the volatile bytes masked -- see _diff_bytes.
        moved = [m for m in self._diff_bytes(origin, after, named=False)
                 # Drop an entry only if every offset in it is per-report noise.
                 if not all(o in VOLATILE_STATUS_BYTES for o in _diff_offsets(m))]
        return ("moved " + ", ".join(moved)) if moved else "(nothing)"

    # -- offset calibration (writes every option, then diffs the report) --------

    def _calibrate_restore(self, label: str, setter, current) -> Optional[str]:
        """Put one setting back. Returns a warning if it could not be.

        An unknown baseline is the one case that leaves the glasses changed,
        which happens when the offset being calibrated is the very thing that
        would have told us the current value. Worth saying out loud.
        """
        if current is None:
            return f"{label}: current value was unknown, so it was NOT restored"
        try:
            self._mutating.set()
            setter(current)
        except Exception as exc:
            return f"{label}: restore to {current!r} failed ({exc})"
        finally:
            self._mutating.clear()
        return None

    # -- panel writes, and confirming the silent ones ---------------------------

    def _write_panel(self, command: int, value: int = 0,
                 payload: bytes = b"") -> None:
        """Send one fire-and-forget frame, seeing it through a halt.

        Five commands are silent, and they are the ones that wedge the endpoint,
        so a failed write here is nearly always a halt rather than a real
        refusal. Clearing it and sending again is safe in a way it would not be
        for a command with a reply: there is no reply to have already consumed,
        and every one of these sets an absolute value rather than toggling, so a
        retry cannot double-apply.

        This retry is why the frames need no spacing between them: a wedge costs
        a few tens of milliseconds here instead of 300 ms of padding on every
        single change.
        """
        with self._lock:
            try:
                self.send(command, value, payload, expect_response=False)
            except RayNeoError:
                self.clear_halt()
                time.sleep(HALT_RECOVERY_SECONDS)
                self.send(command, value, payload, expect_response=False)
            # Clear the halt *before* the third unanswered write, not after the
            # first failure.
            #
            # The endpoint halts after three unanswered writes, and a silent
            # command is by definition never answered -- so a run of them wedges
            # it. Measured over one session: every single -7 followed a 0x09 or a
            # 0x0D (18 and 14 of them) and none followed a 0x50, which does answer
            # and was written 85 times at 20 a second without faulting once.
            #
            # Recovering on failure is too late: the write that trips the halt
            # still succeeds, and it is the *next* transfer that fails -- after
            # sitting through _bulk_write's 200 ms timeout. That cost landed on
            # whichever user action came next, which is why brightness drags felt
            # like they stuttered. Clearing here is two control transfers, about a
            # millisecond, and it keeps the count of unanswered writes at one.
            self.clear_halt()

    def _write_panel_sequence(self, frames) -> None:
        """Send several frames for one setting, spaced so the endpoint keeps up."""
        for index, (command, value, payload) in enumerate(frames):
            if index:
                time.sleep(PANEL_FRAME_GAP)
            self._write_panel(command, value, payload)

    def _recover_endpoint(self) -> None:
        """Un-wedge the interrupt endpoint after a run of unanswered writes.

        Three silent commands in a row leave it halted, and every later transfer
        then fails immediately. Clearing the halt is enough when the handle is
        still good; a full reopen is the fallback. Never raises: this runs while
        already handling a failure.
        """
        try:
            if self._handle is None:
                return
            self.clear_halt()
            if self.read_status_report() is not None:
                self._note("endpoint recovered by clearing the halt")
                return
        except Exception:
            pass
        try:
            self.close()
            self.open()
            self._note("endpoint recovered by reopening the device")
        except Exception as exc:
            self._note(f"endpoint recovery failed: {exc}")

    def _note(self, line: str) -> None:
        """Report a diagnostic line through the trace hook, if one is installed.

        Same channel as the frame trace: the backend owns the USB conversation
        and this module owns no logger of its own, so it cannot log directly.
        """
        hook = self._tx_listener
        if hook is None:
            return
        try:
            hook(line)
        except Exception:
            pass

    def _begin_confirm(self, key: str) -> int:
        """Register a request whose effect will be read back from the device.

        Returns a token to hand to the matching ``_confirm_*``. Passing it back
        is what lets a superseded read-back be recognised as such instead of
        being reported as the device refusing the change.
        """
        self._confirm_seq[key] = self._confirm_seq.get(key, 0) + 1
        return self._confirm_seq[key]

    def _superseded(self, key: str, token: int) -> bool:
        """True when a newer request for the same setting has been issued."""
        return self._confirm_seq.get(key, 0) != token

    def _settle_then_read(self, satisfied=None) -> Optional[bytes]:
        """Wait for the panel to commit, then read the status report back.

        The fire-and-forget commands have no reply to synchronise on, so the
        status report is the only evidence that a change landed. How long the
        panel takes is not published anywhere, and 250 ms was not enough: a scene
        change committed later than that and read back as unchanged.

        Two ways out, checked in this order:

        - ``satisfied(frame)`` -- the device already reports what we asked for.
          Returns after a single read, which is the common case and saves a round
          trip the user would otherwise sit through.
        - two identical frames in a row, meaning the panel has stopped changing
          its mind. That is what catches a change the device refused.

        The comparison is on the bytes that mean something; 0x04..0x06 carry a
        checksum and a counter that move on every report regardless.
        """
        deadline = time.monotonic() + sum(SETTLE_PROBE_DELAYS)
        previous: Optional[bytes] = None
        for delay in SETTLE_PROBE_DELAYS:
            time.sleep(delay)
            if time.monotonic() >= deadline:
                break
            current = self.read_status_report()
            if current is None:
                continue
            if satisfied is not None and satisfied(current):
                return current
            if previous is not None and _meaningful(current) == _meaningful(previous):
                return current
            previous = current
        return previous

    def _confirm_scene_mode(self, expected: str, token: int) -> None:
        """Check scene mode landed, giving the panel time to commit.

        Skipped while calibrate_status runs -- there the diff itself is the
        measurement, and an extra read would only widen the window in which the
        background poller can interleave.
        """
        if self._calibrating:
            return
        wanted = SCENE_MODES[expected]
        raw = self._settle_then_read(
            lambda frame: frame[STATUS_PANEL_COLOR_PARAMS] == wanted)
        got = self._info.scene_mode
        if raw is None or got == expected:
            return
        # Only now, with a genuine disagreement in hand, is it worth asking
        # whether it was ours to blame. Checking this first produced a note
        # saying "not a disagreement" for read-backs that had agreed, which is
        # noise where there was previously none.
        if self._superseded("sceneMode", token):
            self._note(
                f"scene mode: asked {expected!r} but the device reports "
                f"{got!r}, which is what the newer choice wrote -- not a fault"
            )
            return
        self._note(
            f"WARN scene mode read-back disagrees: asked {expected}, device says "
            f"{got!r} -- reply[0x12]={raw[STATUS_PANEL_COLOR_PARAMS]} "
            "is probably not the scene-mode byte"
        )

    def _confirm_audio_mode(self, expected: int, token: int) -> None:
        """Check the audio mode landed, giving the panel time to commit.

        Command 0x49 draws no reply, so the status report is the only evidence.
        Without this the panel published whatever was asked for rather than what
        is in effect.
        """
        if self._calibrating:
            return
        raw = self._settle_then_read(
            lambda frame: frame[STATUS_AUDIO_MODE] == expected)
        got = self._info.audio_mode
        if raw is None or got == expected:
            return
        if self._superseded("audioMode", token):
            self._note(
                f"audio mode: asked {expected} but the device reports {got}, "
                "which is what the newer choice wrote -- not a fault"
            )
            return
        self._note(
            f"WARN audio mode read-back disagrees: asked {expected}, device "
            f"says {got} -- reply[0x{STATUS_AUDIO_MODE:#04x}]="
            f"{raw[STATUS_AUDIO_MODE]}"
        )

    def _confirm_hdr_mode(self, expected: int, token: int) -> None:
        """Check picture quality landed, giving the panel time to commit."""
        if self._calibrating:
            return
        raw = self._settle_then_read(
            lambda frame: frame[STATUS_HDR_MODE] == expected)
        got = self._info.hdr_mode
        if raw is None or got == expected:
            return
        if self._superseded("hdrMode", token):
            self._note(
                f"picture quality: asked {expected} but the device reports "
                f"{got}, which is what the newer choice wrote -- not a fault"
            )
            return
        self._note(
            f"WARN picture quality read-back disagrees: asked {expected}, device "
            f"says {got} -- reply[0x14]={raw[STATUS_HDR_MODE]} and "
            f"reply[0x13]={raw[STATUS_HIGH_DYNAMIC]} are candidates"
        )

    # -- display ----------------------------------------------------------------

    def set_display_mode(self, mode: str) -> None:
        if mode not in (DISPLAY_MODE_2D, DISPLAY_MODE_3D):
            raise ValueError(f"unknown display mode {mode!r}")
        self._mutating.set()
        cmd = CMD_SWITCH_TO_3D if mode == DISPLAY_MODE_3D else CMD_SWITCH_TO_2D

        def _do() -> None:
            with self._lock:
                self.open()
                self.send(cmd, timeout_ms=300)

        self._apply_probed("display-mode " + mode, _do)

    def set_screen_size(self, size: str) -> None:
        if size not in SCREEN_SIZES:
            raise ValueError(f"unknown screen size {size!r}")
        self._mutating.set()

        def _do() -> None:
            with self._lock:
                self.open()
                self.send(CMD_SET_SCREEN_SIZE, SCREEN_SIZES[size], timeout_ms=300)
                self._info.screen_size = size

        self._apply_probed("screen-size " + size, _do)

    def set_scene_mode(self, mode: str, save: bool = True) -> None:
        """Set the picture/scene mode (标准 / 电影 / 护眼 / 阅读)."""
        if mode not in SCENE_MODES:
            raise ValueError(f"unknown scene mode {mode!r}")
        self._mutating.set()
        value = SCENE_MODES[mode]
        # Taken per request, before any writing: a second choice made while this
        # one is still settling has to be able to say so when the read lands.
        token = self._begin_confirm("sceneMode")

        def _do() -> None:
            with self._lock:
                self.open()
            # PanelColorParmsAdjust(subOp, arg2, arg3) is laid out as
            #   frame[1]=0x73  frame[2]=subOp
            #   frame[3]=0x00 (hard-coded by the firmware wrapper)
            #   frame[4]=arg2  frame[5]=arg3
            # i.e. the payload is always the 3 bytes [0x00, arg2, arg3].
            #
            # Verified in XRService::PanelColorAdjust @ 0x15b128:
            #   strb w2,[sp,#0x1] / strb w3,[sp,#0x2] / strb wzr,[sp]
            #   mov w1,#0x73 / mov w2,w22 / mov w4,#0x3 / mov x3,sp
            #
            # Official app (AirApi.java):
            #   previewColorMode(i) -> PanelColorParmsAdjust(12, i, i)
            #   saveColorMode(i)    -> PanelColorParmsAdjust(255, 1, i)
            #                           + PanelColorParmsAdjust(15, 1, i)
            #
            # NOTE: dropping that leading 0x00 shifts the colour mode into
            # frame[4] from the firmware's point of view, which it reads as
            # "standard" -- the change would flash and then revert.
            #
            # None of the three frames is ever acknowledged (see
            # UNACKNOWLEDGED_COMMANDS), so none of them waits for a reply. The
            # code used to require the preview's reply and skip the saves
            # without it, which meant the change was previewed and then never
            # persisted.
            #
            # And they go out unspaced, like the app's own save does. The 150 ms
            # gap that used to separate them was there because three unspaced
            # writes used to leave the endpoint halted; _write_panel now clears
            # the halt and retries, so the gap was paying 300 ms to avoid a
            # problem that costs nothing to recover from. See PANEL_FRAME_GAP.
            frame = bytes((0x00, 0x01, value & 0xFF))
            frames = [(CMD_PANEL_COLOR_ADJUST, PANEL_COLOR_OP_MODE_PREVIEW,
                       bytes((0x00, value & 0xFF, value & 0xFF)))]
            if save:
                frames.append((CMD_PANEL_COLOR_ADJUST, PANEL_COLOR_OP_SAVE, frame))
                frames.append((CMD_PANEL_COLOR_ADJUST, PANEL_COLOR_OP_SAVE2, frame))
            self._write_panel_sequence(frames)
            self._confirm_scene_mode(mode, token)

        self._apply_probed("scene-mode " + mode, _do)

    def set_hdr_mode(self, mode: str | int) -> None:
        """Set the picture-quality mode (SDR / AI-HDR).

        Takes the key or the raw number, because calibrate_status drives it from
        a read-back value.
        """
        if isinstance(mode, int):
            if mode not in HDR_MODES.values():
                raise ValueError(f"unknown hdr mode {mode!r}")
            value = mode
        else:
            if mode not in HDR_MODES:
                raise ValueError(f"unknown hdr mode {mode!r}")
            value = HDR_MODES[mode]
        self._mutating.set()
        token = self._begin_confirm("hdrMode")

        def _do() -> None:
            with self._lock:
                self.open()
            self._write_panel(CMD_SET_HDR_MODE, value)
            self._confirm_hdr_mode(value, token)

        self._apply_probed("hdr-mode " + str(value), _do)

    def set_high_dynamic(self, enabled: bool) -> None:
        self._mutating.set()

        def _do() -> None:
            with self._lock:
                self.open()
            self._write_panel(CMD_SET_HIGH_DYNAMIC, 1 if enabled else 0)
            self._info.high_dynamic = bool(enabled)

        self._apply_probed("high-dynamic " + str(enabled), _do)

    def set_color_enhance(self, enabled: bool) -> None:
        self._mutating.set()

        def _do() -> None:
            with self._lock:
                self.open()
                self.send(CMD_SET_COLOR_ENHANCE, 1 if enabled else 0, timeout_ms=300)
                self._info.color_enhance = bool(enabled)

        self._apply_probed("color-enhance " + str(enabled), _do)

    # -- audio ------------------------------------------------------------------

    def _observe_audio_tube(self, value: bool, source: str) -> bool:
        """Set the audio tube state, reporting every transition and its source.

        Three different code paths write this field -- our own write, the echo of
        that write, and the 0xE3 status report the poll reads -- and the volume
        ceiling is derived from it, so a disagreement between them shows up as
        the maximum volume jumping between 12 and 15 for no visible reason. It
        was reported exactly that way: toggling the tube a few times in a row made
        the ceiling move irregularly, and the log could not say which of the three
        writers had done it because nothing recorded the transitions at all.
        """
        value = bool(value)
        previous = self._info.audio_tube
        self._info.audio_tube = value
        if previous != value:
            self._note(
                f"audio tube {previous} -> {value} ({source}); volume ceiling "
                f"now {self.volume_limit()}"
            )
            return True
        return False

    def set_audio_tube(self, enabled: bool) -> None:
        """Toggle the 导音鳍 (audio tube / directional-fin) guide."""
        self._mutating.set()

        def _do() -> None:
            with self._lock:
                self.open()
                self.send(CMD_SET_AUDIO_TUBE, 1 if enabled else 0, timeout_ms=300)
                self._observe_audio_tube(enabled, "our own write")

        self._apply_probed("audio-tube " + str(enabled), _do)

    def set_audio_mode(self, mode: str) -> None:
        """Set the audio mode (标准 / 轻语 / 空间环绕).

        Command 0x49 does not reply on this firmware either -- 0 in 4 sends, and
        0 in 12 across every session -- so the mode is confirmed against the
        status report instead. It used to be listed as acknowledging on the
        strength of one stray frame match, which is not a reply rate.
        """
        if mode not in AUDIO_MODES:
            raise ValueError(f"unknown audio mode {mode!r}")
        self._mutating.set()
        wanted = AUDIO_MODES[mode]
        token = self._begin_confirm("audioMode")

        def _do() -> None:
            self._write_panel(CMD_SET_AUDIO_MODE, wanted)
            self._confirm_audio_mode(wanted, token)

        self._apply_probed("audio-mode " + mode, _do)

    # -- probing and capabilities -----------------------------------------------

    def _apply_probed(self, label: str, fn) -> None:
        """Run ``fn``, optionally with before/after response diffing.

        The diffing exists to *discover* field offsets, and every offset is now
        measured, so it is off by default. It costs four USB transactions per
        setting change, and on this hardware that is enough to push the
        interrupt endpoint into a timeout: a scene change went 300 ms with no
        answer and every later transfer then failed instantly.

        Enable it from :meth:`set_probe_enabled` when investigating a new
        offset. ``_mutating`` is always cleared, because the background poller
        skips every cycle while it is set -- a leak here silently stops live
        updates for the rest of the session.
        """
        if not self._probe_enabled:
            # Normal path: a failure is the caller's problem to see, so the
            # frontend can report it instead of pretending the change landed.
            try:
                fn()
            finally:
                self._mutating.clear()
            return

        try:
            self.probe_change(label, fn)
        except RayNeoError:
            # The probe itself may be what failed, or the change may not have
            # been applied yet -- try once more without instrumentation.
            try:
                fn()
            except RayNeoError:
                pass
        finally:
            self._mutating.clear()

    def set_probe_enabled(self, enabled: bool) -> None:
        """Turn before/after response diffing on or off.

        Off by default: it is a discovery tool, and it multiplies the USB
        traffic per setting change.
        """
        self._probe_enabled = bool(enabled)

    def get_device_info(self) -> DeviceInfo:
        with self._lock:
            if not self.connected:
                return DeviceInfo()
            return self._info

    def capabilities(self) -> Dict[str, bool]:
        """Feature flags reported by the glasses (command 0xE0).

        See the module-level ``CAPABILITY_FLAG_OFFSETS`` /
        ``CAPABILITY_FLAGS`` comments for the Ghidra derivation of both the
        byte offsets and the flag->name mapping.
        """
        with self._lock:
            self.open()
            resp = self.send(CMD_GET_FUNC_SUPPORT, timeout_ms=500)
        return self._capabilities_from(resp)

    def _capabilities_from(self, resp: Optional[bytes]) -> Dict[str, bool]:
        """Decode a 0xE0 response into the flag dict. None means assume nothing."""
        if resp is None:
            return dict(DEFAULT_CAPABILITIES)

        def bit(flag: int) -> bool:
            off = CAPABILITY_FLAG_OFFSETS[flag]
            # A short response means the firmware predates some capability
            # bits; report those as unsupported rather than raising.
            return len(resp) > off and bool(resp[off])

        return {name: bit(flag) for name, flag in CAPABILITY_FLAGS.items()}

    def capability_dump(self) -> str:
        """Capability report for one 0xE0 response: raw frame plus flag table.

        The flag -> name mapping and the response byte offsets are both
        Ghidra-verified (see the module-level tables and docs/PROTOCOL.md).
        This is what proves on real hardware which features the unit has.

        Decodes the response it already has. It used to call capabilities(),
        which sent 0xE0 a second time and threw the reply away -- so every
        capability dump cost two transactions and the log showed two.
        """
        with self._lock:
            self.open()
            raw = self.send(CMD_GET_FUNC_SUPPORT, timeout_ms=800)
        return _dump_block("0xE0 capabilities", raw, self.capability_table(raw))
