"""Decky plugin entrypoint for RayNeo Control.

All hardware access lives in :mod:`rayneo`; this module is the thin bridge
between Decky's Python RPC layer and the TypeScript frontend.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

# The hardware layer lives in py_modules/, which the loader appends to sys.path
# itself (decky_loader/plugin/sandboxed_plugin.py):
#
#     sys.path.append(path.join(environ["DECKY_PLUGIN_DIR"], "py_modules"))
#
# so `import rayneo` below works without any path juggling here. That only holds
# under the loader; tools/ add the directory themselves when run standalone.

import decky  # type: ignore

from rayneo import (
    DeviceNotFound,
    RayNeoDevice,
    RayNeoError,
)

LOG_TAG = "RayNeoControl"

#: Emitted to the frontend whenever the glasses push a state change.
EVENT_STATE = "rayneo_state"

#: Consecutive unanswered polls before the glasses are declared gone.
#:
#: One is not enough. The 0x00 query is given 250 ms, and brightness and
#: picture-quality writes are silent -- the device never acknowledges them -- so
#: a poll landing straight after one can time out while the panel is still busy.
#: That single miss used to be reported as "glasses stopped answering", greying
#: out the panel for a moment before the next poll succeeded. The log showed six
#: of these in one session, each right after a brightness change that had itself
#: taken 261 ms instead of the usual 6.
#:
#: A real disconnect answers no poll at all, so requiring three costs 4.5 s to
#: notice a genuine unplug and buys immunity to the transient.
MISSES_BEFORE_DISCONNECT = 3

#: How often to re-read the device info block while nothing else is happening.
#: Read-only, so this is safe; 1.5 s keeps the UI live without being chatty.
POLL_INTERVAL = 1.5

_device = RayNeoDevice()

#: True when the user pressed Disconnect, so the poll must not undo it.
#:
#: The poll re-opens the glasses by itself when they go away, because a cable
#: knock should not need a button press to recover from. That is exactly wrong
#: for a deliberate disconnect: the panel closed the device and the poll put it
#: straight back, so the button appeared not to work -- within one poll interval.
#: Absence the user asked for and absence that happened to us are different
#: things, and only the second one is worth healing.
_user_closed = False

#: Last known good state, so the UI can render something before connecting.
_last_state: Dict[str, Any] = {"connected": False}

#: Set when a frame arrives, so the poll loop wakes immediately instead of
#: waiting out its remaining interval.
#: In-flight EVENT_STATE task, so repeated publishes do not pile up coroutines.
#: See _safe_emit -- decky.emit is async and has to be awaited.
_emit_task: Optional[asyncio.Task] = None


def _safe_emit(state: Dict[str, Any]) -> None:
    """Push ``state`` to the frontend as an EVENT_STATE notification.

    ``decky.emit`` is a coroutine and must be awaited -- calling it without
    awaiting just builds a coroutine object that never runs, so nothing reaches
    the frontend and the UI silently keeps whatever it last fetched. Every
    caller here is synchronous (a thread finishing a USB read), so the coroutine
    is scheduled on the plugin's own loop instead.

    The pending task is tracked so a failure is reported once rather than as an
    unretrieved-exception warning, and so the event loop does not fill up with
    tasks that outlive their loop.
    """
    global _last_state, _emit_task
    _last_state = state
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop (a worker thread): drop it. The poller re-publishes within
        # POLL_INTERVAL anyway, so state still converges.
        return
    if _emit_task is not None and not _emit_task.done():
        # An emit is already in flight; its payload is at most one poll old.
        return
    _emit_task = loop.create_task(_emit_now(state))


def _log_frontend(where: str, message: str, detail: Optional[str]) -> None:
    """Write a frontend-side error into the Decky log.

    The panel shows these as red text; without this they would exist only on
    screen. Kept deliberately tolerant of junk input since it is fed straight
    from a catch handler.
    """
    head = f"frontend {where}: {message}"
    try:
        if detail:
            body = str(detail)
            for line in body.splitlines():
                head += "\n    " + line
        # Guard against a pathological message from a render loop.
        decky.logger.error(head[:8000])
    except Exception as exc:  # pragma: no cover - defensive
        decky.logger.warning(f"{LOG_TAG}: could not log frontend message: {exc}")


async def _emit_now(state: Dict[str, Any]) -> None:
    try:
        await decky.emit(EVENT_STATE, state)
    except Exception as exc:  # pragma: no cover - defensive
        decky.logger.warning(f"{LOG_TAG}: could not emit state: {exc}")


def _publish(info) -> None:
    """Push a freshly read DeviceInfo to the frontend."""
    _safe_emit(info.to_dict())


def _state_dict(info) -> dict:
    """Serialise DeviceInfo, adding the derived volume ceiling.

    The slider must not offer a step the backend would clamp away, and the
    clamp depends on the audio tube, which is backend state.
    """
    data = dict(info.to_dict())
    data["volumeLimit"] = _device.volume_limit() if _device.connected else None
    data["brightnessLevels"] = (_device.brightness_levels()
                               if _device.connected else None)
    return data


def _on_frame(cmd: int, _data: bytes) -> None:
    """Publish the echo of a write we just sent.

    RayNeoDevice only reaches here while one of our commands is awaiting a
    reply, so this reflects our own writes and nothing else. It exists so a
    slider drag shows up at once instead of waiting out the poll.
    """
    if not _device.connected:
        return
    _safe_emit(_state_dict(_device.get_device_info()))


#: Whether the debug-only logging is on.
#:
#: Off, so a normal session leaves a short log of things that happened rather
#: than a transcript of everything that crossed the wire.
#:
#: What stays on regardless is anything a user might need to act on: errors,
#: WARN lines, connection changes, a disconnect and its recovery, and the rare
#: state transitions. What this gates is the volume -- the per-frame TX trace,
#: the three protocol tables dumped at startup, and the panel's own timing
#: notes. Those are all things you want once something is wrong, and none of
#: them are useful in a log nobody asked to be long.
_debug = False

#: Where the flag lives between reloads. Next to the plugin, which runs as root.
_DEBUG_FILE = Path(__file__).resolve().parent / ".debug.json"


def _load_debug() -> bool:
    """Read the saved flag. Anything unreadable means off."""
    try:
        return bool(json.loads(_DEBUG_FILE.read_text(encoding="utf-8"))["debug"])
    except Exception:
        return False


def _save_debug(on: bool) -> None:
    """Best-effort: a flag that cannot be saved is not worth failing a toggle."""
    try:
        _DEBUG_FILE.write_text(json.dumps({"debug": bool(on)}),
                               encoding="utf-8")
    except Exception as exc:
        decky.logger.warning(f"{LOG_TAG}: could not save the debug flag: {exc}")


def _on_tx(line: str) -> None:
    """Frame trace hook: log exactly what went out for a setting change."""
    if not _debug:
        return
    decky.logger.info(f"{LOG_TAG} {line}")


def _log_block(title: str, body: str) -> None:
    """Write a multi-line report to the Decky log.

    The UI panel is awkward to copy text out of, so the interesting protocol
    dumps go to ~/homebrew/logs/RayNeo-Control/ as well. Also mirrors to
    /tmp/rayneo-latest-<slug>.txt, which is easier to grep.
    """
    try:
        decky.logger.info(f"{LOG_TAG} ===== {title} =====")
        for line in body.splitlines():
            decky.logger.info(f"{LOG_TAG} | {line}")
    except Exception as exc:  # pragma: no cover - defensive
        decky.logger.warning(f"{LOG_TAG}: could not log {title}: {exc}")
    try:
        slug = "".join(ch if ch.isalnum() else "-" for ch in title.lower())[:40]
        with open(f"/tmp/rayneo-{slug}.txt", "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
    except OSError:
        pass


def _fmt(value) -> str:
    return "-" if value is None else str(value)


def _last_index(count) -> int:
    """maxVolume is a count, so the last valid index is one below it."""
    return (count - 1) if isinstance(count, int) and count > 0 else 0


def _try_reopen() -> bool:
    """Try to pick the glasses back up without the user pressing Connect.

    Never raises. A device that is genuinely absent is the expected case here,
    not something worth surfacing once every poll interval.

    Returns False, without touching USB, when the user asked for the disconnect.
    """
    if _user_closed:
        return False
    try:
        return bool(_device.open())
    except RayNeoError:
        return False


class Plugin:
    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _fail(exc: Exception) -> Dict[str, Any]:
        message = str(exc)
        if isinstance(exc, DeviceNotFound):
            message = "请用 USB-C 连接眼镜 Plug in the glasses over USB-C"
        decky.logger.error(f"{LOG_TAG}: {message}")
        return {"ok": False, "error": message}

    def _emit_state(self) -> None:
        try:
            _safe_emit(_state_dict(_device.get_device_info()))
        except Exception as exc:  # pragma: no cover - defensive
            decky.logger.warning(f"{LOG_TAG}: could not emit state: {exc}")

    # -- Decky RPC surface -----------------------------------------------------
    #
    # Grouped by what the panel uses them for. The grouping is one deep --
    # there is a sub-header per area rather than a class per area, because the
    # methods are thin wrappers over _device and splitting them out would add
    # indirection without removing any.

    # panel bootstrap

    async def get_metadata(self) -> Dict[str, Any]:
        """Current state plus the derived slider bounds, for building the UI.

        The option keys themselves are *not* sent: the frontend hard-codes them
        in src/optionKeys.ts so the TypeScript types stay ``as const``. Shipping
        a second copy here would be a drift risk, so
        tools/smoke_test.py asserts the two lists agree instead.
        """
        return {
            "state": _last_state,
            "brightnessLevels": _device.brightness_levels(),
        }

    # connection

    async def connect(self) -> Dict[str, Any]:
        """Open the device and read its full state.

        Retried once through a close. ``RayNeoDevice.open()`` is idempotent and
        returns whatever handle it already holds, so if a stale one survived an
        unplug the panel had not noticed yet, the first attempt is spent on a
        handle to a device that is no longer there and the user has to press the
        button again. One retry makes a single press enough whatever state the
        handle is in.
        """
        last: Optional[RayNeoError] = None
        info = None
        # Pressing Connect is the user asking for it, so the poll may look after
        # the connection again.
        global _user_closed
        _user_closed = False
        for attempt in (1, 2):
            try:
                info = await asyncio.to_thread(_device.refresh_device_info)
                break
            except RayNeoError as exc:
                last = exc
                try:
                    _device.close()
                except Exception:
                    pass
                if attempt == 1:
                    decky.logger.info(
                        f"{LOG_TAG}: first connect attempt failed ({exc}), "
                        f"reopening and retrying"
                    )
        if info is None:
            self._emit_state()
            return self._fail(last)

        _publish(info)
        # A fresh connection may have different capabilities.
        caps = await asyncio.to_thread(_device.capabilities)
        return {"ok": True, "state": _state_dict(info), "capabilities": caps}

    async def disconnect(self) -> Dict[str, Any]:
        # Remembered, so the poll does not immediately undo it. Without this the
        # device came straight back and the button looked broken.
        global _user_closed
        _user_closed = True
        _device.close()
        self._emit_state()
        return {"ok": True}

    async def _run(self, fn, *args) -> Dict[str, Any]:
        # Snapshot first so the publish below can tell "the glasses now report
        # something different" from "nothing moved".
        before = _state_dict(_device.get_device_info())
        try:
            await asyncio.to_thread(fn, *args)
        except (RayNeoError, ValueError) as exc:
            return self._fail(exc)
        # Publish whatever the glasses now report, rather than waiting for the
        # poll. Every control used to wait out the poll interval except the ones
        # whose reply happened to be echoed into the state, which left the
        # dropdowns feeling broken while the sliders felt fine.
        #
        # This is honest for both kinds of command: an echoed one has already
        # folded its reply in, and a silent one (scene mode, picture quality)
        # has already polled the status report to confirm. If the device refused
        # the change, the state is unchanged, nothing is published, and the UI
        # keeps showing what is really in effect.
        after = _state_dict(_device.get_device_info())
        if after != before:
            _safe_emit(after)
        return {"ok": True}

    # Display ------------------------------------------------------------

    # settings -- display

    async def set_brightness(self, index: int) -> Dict[str, Any]:
        return await self._run(_device.set_brightness, int(index))

    async def save_brightness(self) -> Dict[str, Any]:
        """Commit the staged brightness and hand back what the panel reports.

        Not a plain ``_run``: the panel needs the confirmed step in the reply,
        not only on the state event. The slider's settle window is still open
        when this lands, and incoming state is ignored for the duration of a
        drag -- so a refusal reported only through the event would never reach
        the thumb, and the slider would keep showing a value the panel refused.
        """
        before = _state_dict(_device.get_device_info())
        try:
            confirmed = await asyncio.to_thread(_device.save_brightness)
        except (RayNeoError, ValueError) as exc:
            return self._fail(exc)
        after = _state_dict(_device.get_device_info())
        if after != before:
            _safe_emit(after)
        # None means the panel never reported, so there is no answer to give and
        # the slider should stay where the user put it rather than jump.
        return {"ok": True, "luminance": confirmed}

    async def set_volume(self, level: int) -> Dict[str, Any]:
        return await self._run(_device.set_volume, int(level))

    async def set_display_mode(self, mode: str) -> Dict[str, Any]:
        return await self._run(_device.set_display_mode, str(mode))

    async def set_screen_size(self, size: str) -> Dict[str, Any]:
        return await self._run(_device.set_screen_size, str(size))

    # Picture -------------------------------------------------------------

    async def set_scene_mode(self, mode: str) -> Dict[str, Any]:
        return await self._run(_device.set_scene_mode, str(mode))

    async def set_hdr_mode(self, mode: str) -> Dict[str, Any]:
        return await self._run(_device.set_hdr_mode, str(mode))

    async def set_color_enhance(self, enabled: bool) -> Dict[str, Any]:
        return await self._run(_device.set_color_enhance, bool(enabled))

    # Audio ---------------------------------------------------------------

    # settings -- audio

    async def set_audio_mode(self, mode: str) -> Dict[str, Any]:
        return await self._run(_device.set_audio_mode, str(mode))

    async def set_audio_tube(self, enabled: bool) -> Dict[str, Any]:
        return await self._run(_device.set_audio_tube, bool(enabled))

    # Diagnostics ---------------------------------------------------------

    # panel preferences

    async def get_debug(self) -> Dict[str, Any]:
        """Whether the debug-only logging and panel tools are on."""
        return {"ok": True, "debug": _debug}

    async def set_debug(self, enabled: bool) -> Dict[str, Any]:
        """Turn the debug surface on or off, and remember it.

        One flag for both halves: the panel hides its debug buttons and the
        backend stops writing the volume, which are the same request.
        """
        global _debug
        _debug = bool(enabled)
        _save_debug(_debug)
        decky.logger.info(
            f"{LOG_TAG}: debug {'on' if _debug else 'off'}"
        )
        return {"ok": True, "debug": _debug}

    # device reports

    async def get_capabilities(self) -> Dict[str, Any]:
        try:
            caps = await asyncio.to_thread(_device.capabilities)
        except RayNeoError as exc:
            return self._fail(exc)
        return {"ok": True, "capabilities": caps}

    async def get_capability_dump(self) -> Dict[str, Any]:
        """Formatted capability table plus the raw 0xE0 response."""
        try:
            text = await asyncio.to_thread(_device.capability_dump)
        except RayNeoError as exc:
            return self._fail(exc)
        _log_block("0xE0 capabilities", text)
        return {"ok": True, "dump": text}

    async def get_status_report(self) -> Dict[str, Any]:
        """Raw command 0xE3 status report, decoded field by field.

        The panel settings are only in this response, so it is the way to read
        back what was sent and confirm it landed.
        """
        try:
            text = await asyncio.to_thread(_device.status_dump)
        except RayNeoError as exc:
            return self._fail(exc)
        _log_block("0xE3 status report", text)
        return {"ok": True, "dump": text}

    # frontend reporting

    async def log_frontend_error(self, where: str, message: str,
                                detail: Optional[str] = None) -> Dict[str, Any]:
        """Record a frontend error in the Decky log.

        A React render error or a rejected RPC shows up as red text in the
        panel and nowhere else, which makes it invisible to the log and to
        tools/smoke_test.py. Routing them through here is the only way to see
        what actually broke on the frontend.
        """
        _log_frontend(where, message, detail)
        return {}

    async def reopen_device(self) -> Dict[str, Any]:
        """Drop the USB handle and reconnect.

        Recovery for the failure mode where the interrupt endpoint stops
        accepting transfers: every later write fails within 1 ms and the plugin
        looks alive but does nothing. A scene change was observed to trigger it
        (the preview frame went 300 ms unanswered), which suggests the panel
        restarts its USB session.
        """
        try:
            _device.close()
        except Exception as exc:
            decky.logger.warning(f"{LOG_TAG}: close before reopen failed: {exc}")
        global _user_closed
        _user_closed = False
        try:
            info = await asyncio.to_thread(_device.refresh_device_info)
        except RayNeoError as exc:
            self._emit_state()
            return self._fail(exc)
        # A fresh handle may expose different capabilities.
        _publish(info)
        decky.logger.info(f"{LOG_TAG}: reopened device")
        return {"ok": True, "state": _state_dict(info)}

    async def log_frontend_message(self, where: str, message: str
                                   ) -> Dict[str, Any]:
        """Record a frontend timing note in the Decky log.

        Not for errors -- log_frontend_error is. This exists because "the
        dropdown does not feel responsive" has no visible cause from the backend
        side: the write goes out in a millisecond, so the only place the delay
        can be is between the click and the render, and nothing was reporting
        from there.
        """
        if _debug:
            decky.logger.info(f"{LOG_TAG} [ui] {where}: {message}")
        return {"ok": True}

    # poll control

    async def pause_polling(self, seconds: float = 1.0) -> Dict[str, Any]:
        """Stand the background poll down briefly, while a slider is dragged.

        The poll shares the device lock with every write, so one that lands
        during a drag can delay the debounced write at the end of it. It also
        keeps reporting the pre-drag value, which puts the thumb back under the
        user's finger.

        Always succeeds. A lost pause only costs a little extra polling, which is
        why this is a deadline on the backend rather than a flag the frontend has
        to remember to clear.
        """
        try:
            _device.pause_polling(float(seconds))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad duration"}
        return {"ok": True}

    # debug tools -- hidden unless debug mode is on

    async def set_probe(self, enabled: bool) -> Dict[str, Any]:
        """Enable/disable before/after response diffing on every change.

        Off by default. Each probe adds four USB transactions per setting
        change, which is enough to time out the interrupt endpoint on this
        hardware -- a scene change that went 300 ms unanswered left every later
        transfer failing instantly. Only turn it on when a new field offset
        needs discovering.
        """
        _device.set_probe_enabled(bool(enabled))
        decky.logger.info(
            f"{LOG_TAG}: response diffing {'enabled' if enabled else 'disabled'}"
        )
        return {"ok": True, "probeEnabled": bool(enabled)}

    async def take_snapshot(self) -> Dict[str, Any]:
        """Dump both read-only responses, for before/after diffing.

        The device-info block (0x00) holds volume, mute, 2D/3D and the
        brightness wire value; the status report (0xE3) holds the panel
        settings -- brightness index, audio mode, colour enhance, audio tube,
        screen size, scene mode and picture quality. Take a snapshot, change one
        setting, take another, diff: the byte that moved is that setting's
        offset.
        """
        try:
            text = await asyncio.to_thread(_device.snapshot)
        except RayNeoError as exc:
            return self._fail(exc)
        _log_block("snapshot", text)
        return {"ok": True, "dump": text}

    async def calibrate_status(self) -> Dict[str, Any]:
        """Work out which 0xE3 byte belongs to which panel setting.

        The offsets for scene mode, picture quality, colour enhance, audio tube
        and screen size currently come from reading the firmware's own parser,
        and that reading has already been wrong once: 0x16..0x17 turn out to be a
        single uint16, so the colour byte and the tube byte cannot be told apart
        by value. Read-back alone does not settle it -- two settings at
        coinciding values move the same byte.

        So this changes one setting at a time from a known baseline, diffs the
        status report, and puts it back. A diff names a byte rather than
        inferring which struct field it feeds.

        Changes several settings briefly and costs a few dozen USB transactions,
        so it is a one-off investigation, not something to run while playing.
        Every step is guarded and every setting is restored.
        """
        try:
            lines = await asyncio.to_thread(_device.calibrate_status)
        except RayNeoError as exc:
            return self._fail(exc)
        _log_block("0xE3 calibration", "\n".join(lines))
        return {"ok": True, "report": "\n".join(lines)}

    async def probe_maxima(self) -> Dict[str, Any]:
        """Walk the volume ceiling and report what the device echoes back.

        ``maxVolume`` is reported by the glasses (reply[0x3D], confirmed in the
        firmware's reply parser) and the audio tube lowers the usable ceiling.
        Neither is enforced by the firmware: it accepts any index we send, so
        this walks up to see where the reported state stops following.

        Read-only apart from setting volume, which the user sees. The original
        volume is restored at the end.
        """
        def _work() -> str:
            info0 = _device.get_device_info()
            start = info0.volume if info0.volume is not None else 0
            limit = _device.volume_limit()
            lines = [
                f"device: type={info0.device_type} fw={info0.firmware_version} "
                f"tube={info0.audio_tube}",
                f"maxVolume (reply[0x3D]) = {_fmt(info0.max_volume)} "
                f"-> indices 0..{_last_index(info0.max_volume)}",
                f"effective ceiling (tube on caps at 12) = {limit}",
                "",
                f"{'level':>5} {'0x00[0x2A]':>11} {'0x3D':>5}  verdict",
                "-" * 52,
            ]
            for level in range(max(0, start - 1), (info0.max_volume or 16) + 4):
                try:
                    _device.set_volume(level)
                except RayNeoError as exc:
                    lines.append(f"{level:>5} {'-':>11} {'-':>5}  rejected: {exc}")
                    break
                now = _device.get_device_info()
                if now.volume != level:
                    lines.append(f"{level:>5} {_fmt(now.volume):>11} "
                                 f"{_fmt(now.max_volume):>5}  "
                                 f"clamped to {now.volume}")
                    break
                lines.append(f"{level:>5} {_fmt(now.volume):>11} "
                             f"{_fmt(now.max_volume):>5}  accepted")

            lines += ["", "restoring original volume...",
                      f"  was {start}"]
            try:
                _device.set_volume(start)
            except RayNeoError as exc:
                lines.append(f"  restore failed: {exc}")
            now = _device.get_device_info()
            lines.append(f"  now volume={_fmt(now.volume)} "
                         f"maxVolume={_fmt(now.max_volume)} "
                         f"limit={_device.volume_limit()}")
            lines.append("")
            lines.append("If 0x3D never changes, the ceiling is a client-side")
            lines.append("concept: the device reports one maxVolume and both the")
            lines.append("official app and this plugin derive the tube limit from it.")
            return "\n".join(lines)

        try:
            text = await asyncio.to_thread(_work)
        except RayNeoError as exc:
            return self._fail(exc)
        _log_block("volume probe", text)
        return {"ok": True, "dump": text}

    async def send_raw(
        self, command: int, value: int = 0, payload: Optional[List[int]] = None
    ) -> Dict[str, Any]:
        """Escape hatch for debugging: send an arbitrary command frame."""
        data = bytes(bytearray(payload or ()))

        def _send() -> Optional[str]:
            resp = _device.send(int(command), int(value), data, timeout_ms=500)
            return None if resp is None else resp.hex()

        try:
            hex_resp = await asyncio.to_thread(_send)
        except RayNeoError as exc:
            return self._fail(exc)
        return {"ok": True, "response": hex_resp}

    # -- lifecycle -------------------------------------------------------------

    async def _main(self) -> None:
        global _debug
        _debug = _load_debug()
        decky.logger.info(
            f"{LOG_TAG} backend starting (debug {'on' if _debug else 'off'})"
        )
        _device.add_frame_listener(_on_frame)
        _device.set_tx_listener(_on_tx)
        # Try once at startup; if the glasses are already plugged in this makes
        # the UI show live values immediately.
        try:
            info = await asyncio.to_thread(_device.refresh_device_info)
            decky.logger.info(
                f"{LOG_TAG} connected: type={info.device_type} fw={info.firmware_version}"
            )
            _publish(info)
            await self._log_protocol_snapshot(info)
        except RayNeoError as exc:
            decky.logger.info(f"{LOG_TAG} not connected yet: {exc}")

        await self._poll_loop()

    async def _log_protocol_snapshot(self, info) -> None:
        """Dump every read-only response, in one consistent shape.

        Logged automatically so protocol verification needs no UI interaction --
        unless debug is off, in which case it is three tables nobody asked for on
        every reload.
        """
        if not _debug:
            return
        try:
            text = await asyncio.to_thread(_device.protocol_dump)
        except Exception as exc:
            decky.logger.warning(f"{LOG_TAG}: protocol dump failed: {exc}")
            return
        for block in text.split("\n\n"):
            title = block.splitlines()[0].strip("= ")
            _log_block(title, "\n".join(block.splitlines()[1:]).strip())

    async def _poll_loop(self) -> None:
        """Keep the UI in sync with the glasses, by polling.

        There is no push channel. The USB IN endpoint is only read while a
        reply is being awaited (see RayNeoDevice._await_response), so a change
        made on the glasses' own side -- phone app, hardware button -- is seen
        here, at the next poll, not immediately. POLL_INTERVAL is that delay.

        A fixed sleep is therefore correct here; there is nothing to wake us
        early. _state_dirty used to be awaited for that purpose, but only
        _on_frame sets it and _on_frame only runs inside a command's reply
        window, so it could never fire on its own and the wait always timed
        out.

        _on_frame still matters, but for a different reason: it applies the
        echo of a write immediately, so a slider drag does not wait out the
        poll before moving.
        """
        # Consecutive unanswered polls. Reset by any success, so a single late
        # reply costs nothing.
        _missed = 0
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            if _device.is_mutating():
                continue

            if not _device.connected:
                # Keep looking for the glasses instead of giving up on them.
                #
                # This used to `continue` straight past a disconnected device, so
                # once the poll declared them gone nothing ever polled again: the
                # panel sat on a frozen state and every control stopped reflecting
                # reality until the user noticed and pressed Connect. That turned
                # one wrong "gone" into a plugin that looked dead with the glasses
                # sitting right there -- a busy panel is enough to cause a wrong
                # "gone", and one did exactly that.
                #
                # The cost is one USB enumeration per interval, and only while
                # disconnected.
                if not await asyncio.to_thread(_try_reopen):
                    continue
                decky.logger.info(
                    f"{LOG_TAG} glasses answered again, resuming live updates"
                )
                _missed = 0

            info = await asyncio.to_thread(_device.poll_state)
            if info is None:
                # One unanswered query is not a disconnect -- see
                # MISSES_BEFORE_DISCONNECT. Count them, and only give up once the
                # device has had several chances in a row.
                _missed += 1
                if _missed < MISSES_BEFORE_DISCONNECT:
                    continue
                if _last_state.get("connected"):
                    decky.logger.info(
                        f"{LOG_TAG} glasses stopped answering "
                        f"({_missed} consecutive polls)"
                    )
                    gone = _state_dict(_device.get_device_info())
                    gone["connected"] = False
                    gone["volumeLimit"] = None
                    gone["brightnessLevels"] = None
                    # Release the dead handle. `open()` is idempotent and hands
                    # back whatever handle it already holds, so leaving a stale
                    # one in place made the next Connect spend its only attempt
                    # on a handle to a device that is no longer there: the first
                    # press failed and only the second worked. The state above is
                    # captured first, because get_device_info() returns an empty
                    # DeviceInfo once the handle is gone.
                    try:
                        await asyncio.to_thread(_device.close)
                    except Exception as exc:
                        decky.logger.warning(
                            f"{LOG_TAG}: close after disconnect failed: {exc}"
                        )
                    _safe_emit(gone)
                continue
            _missed = 0
            _publish(info)

    async def _unload(self) -> None:
        _device.remove_frame_listener(_on_frame)
        _device.clear_tx_listener()
        try:
            _device.close()
        except Exception:
            pass
        decky.logger.info(f"{LOG_TAG} backend unloading")

    async def _uninstall(self) -> None:
        await self._unload()