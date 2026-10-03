/** Typed wrappers around the Python backend methods. */

import { callable as rawCallable } from "@decky/api";

import { logged } from "./diagnostics";
import type { Capabilities, DeviceState, Metadata, RpcResult } from "./types";

/**
 * Wrap every RPC so failures are logged before they surface as red text.
 *
 * The panel shows a rejection as an error line and nothing else, so without
 * this the plugin log only ever contains the backend half of a failure.
 */
function callable<Args extends unknown[], Res>(
  name: string,
): (...args: Args) => Promise<Res> {
  return logged(name, rawCallable<Args, Res>(name));
}

export const getMetadata = callable<[], Metadata>("get_metadata");
export const getDebug = callable<[], RpcResult & { debug?: boolean }>("get_debug");
export const setDebug = callable<[enabled: boolean], RpcResult>("set_debug");
export const connectDevice = callable<[], RpcResult>("connect");
export const disconnectDevice = callable<[], RpcResult>("disconnect");

export const setBrightness = callable<[index: number], RpcResult>("set_brightness");
export const commitBrightness = callable<[], RpcResult>("save_brightness");
export const setVolume = callable<[level: number], RpcResult>("set_volume");
export const setDisplayMode = callable<[mode: string], RpcResult>("set_display_mode");
export const setScreenSize = callable<[size: string], RpcResult>("set_screen_size");


export const setSceneMode = callable<[mode: string], RpcResult>("set_scene_mode");
export const setHdrMode = callable<[mode: string], RpcResult>("set_hdr_mode");
export const setColorEnhance = callable<
  [enabled: boolean],
  RpcResult
>("set_color_enhance");

export const setAudioMode = callable<[mode: string], RpcResult>("set_audio_mode");
export const setAudioTube = callable<[enabled: boolean], RpcResult>("set_audio_tube");

export const getCapabilities = callable<
  [],
  RpcResult & { capabilities?: Capabilities }
>("get_capabilities");
export const getCapabilityDump = callable<[], RpcResult & { dump?: string }>(
  "get_capability_dump",
);
export const getStatusReport = callable<[], RpcResult & { dump?: string }>(
  "get_status_report",
);
export const takeSnapshot = callable<[], RpcResult & { dump?: string }>(
  "take_snapshot",
);
export const probeMaxima = callable<[], RpcResult & { dump?: string }>(
  "probe_maxima",
);
/**
 * Change one setting at a time, diff the 0xE3 report, restore.
 *
 * Pins the status-report offsets that are currently read out of the firmware's
 * own parser rather than measured. Brief, and it restores what it changed.
 */
export const calibrateStatus = callable<[], RpcResult & { report?: string }>(
  "calibrate_status",
);
/**
 * Enable before/after response diffing on every setting change.
 *
 * Off by default: it costs four extra USB transactions per change, which
 * times out the interrupt endpoint on this hardware. Only useful when a new
 * field offset needs discovering.
 */
export const setProbe = callable<[enabled: boolean], RpcResult>(
  "set_probe",
);
/**
 * Stand the background poll down for a while, while a slider is dragged.
 *
 * Takes effect on the backend as a deadline, not as a flag this side has to
 * clear, so a dropped call costs a little extra polling and nothing else.
 */
export const pausePolling = callable<[seconds: number], RpcResult>(
  "pause_polling",
);
/**
 * Put a timing note in the plugin log.
 *
 * Not for errors. A control that "does not feel responsive" leaves no trace on
 * the backend side -- the write leaves the device in a millisecond -- so the
 * delay can only be between the click and the render, and nothing was reporting
 * from there.
 */
/**
 * Whether the panel should emit its timing notes at all.
 *
 * Module-level rather than React state because logUiTiming is called from
 * everywhere and must be able to drop a note without a hook, a render, or an
 * RPC. Kept in step with the backend flag by the toggle in the settings panel.
 */
let debugNotes = false;

/** Called by the debug toggle; the backend does the same for its own logs. */
export function setDebugNotes(on: boolean): void {
  debugNotes = on;
}

export function debugNotesEnabled(): boolean {
  return debugNotes;
}

const logUiTimingRemote = callable<
  [where: string, message: string],
  RpcResult
>("log_frontend_message");

/**
 * A timing note, dropped entirely when debug is off.
 *
 * Wrapped rather than gated at each call site: there are a dozen callers and
 * one of them forgetting would put lines back in a log the user has asked to be
 * quiet.
 */
export function logUiTiming(where: string, message: string): Promise<RpcResult> {
  if (!debugNotes) return Promise.resolve({ ok: true });
  return logUiTimingRemote(where, message);
}
/**
 * Drop the USB handle and reconnect.
 *
 * Recovery for a wedged interrupt endpoint, where every write fails within
 * 1 ms and the panel appears frozen.
 */
export const reopenDevice = callable<[], RpcResult & { state?: DeviceState }>(
  "reopen_device",
);
export const sendRaw = callable<
  [command: number, value: number, payload: number[]],
  RpcResult
>("send_raw");

/** Event name used by the backend to push state changes. */
export const STATE_EVENT = "rayneo_state";