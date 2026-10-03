/** Shared types mirroring the Python backend contract. */

export interface DeviceState {
  connected: boolean;
  deviceType: number | null;
  firmwareVersion: number | null;
  /**
   * Firmware build date as YYYYMMDD, e.g. "20260922".
   *
   * This is what the official app displays as the firmware version, and it comes
   * from a different field than `firmwareVersion` -- a NUL-terminated date string
   * rather than the numeric revision. Both are shown.
   */
  firmwareBuild: string | null;
  glassesId: string | null;
  volume: number | null;
  maxVolume: number | null;
  /** Highest selectable volume index; lower than maxVolume-1 with the tube on. */
  volumeLimit: number | null;
  /**
   * Number of selectable brightness steps (12). The panel never reports its
   * own ceiling, so this is fixed from the usable band of the brightness table.
   */
  brightnessLevels: number | null;
  displayMode3d: boolean | null;
  audioMode: number | null;
  whisper: boolean | null;
  mute: boolean | null;
  panelDistance: number | null;
  /** Picture quality: 0 = SDR, 1 = AI-HDR, from the 0xE3 status report. */
  hdrMode: number | null;
  /** Whether HDR is active right now, as distinct from the mode that was set. */
  hdrEnabled: boolean | null;
  /** Scene mode key, read from the 0xE3 status report. */
  sceneMode: string | null;
  colorEnhance: boolean | null;
  audioTube: boolean | null;
  /** "large" | "medium" | "small" | null, read from the 0xE3 status report. */
  screenSize: string | null;
  luminance: number | null;
  /** The brightness byte actually transmitted, i.e. table[luminance]. */
  luminanceValue: number | null;
  maxLuminance: number | null;
  wakeup: boolean | null;
  raw: Record<string, number>;
}

/**
 * Feature flags reported by the glasses in the command-0xE0 response.
 * Names mirror the SDK's DEVICEFUNCSUPPORT fields, lower-camelCased.
 * All 24 are read from the device. `fps120` is informational only: the GT Max
 * reports true, but the official app offers no refresh-rate setting on it, so
 * neither does this plugin.
 */
export interface Capabilities {
  colorAdjust: boolean;
  colorModeChange: boolean;
  colorModeEye: boolean;
  audioDisable: boolean;
  audioQuietMode: boolean;
  audioSpatial: boolean;
  audioTubeMode: boolean;
  audioVolume: boolean;
  fovGet: boolean;
  /** Read for the capability dump; no control is gated on it. */
  fps120: boolean;
  getOrbit: boolean;
  gyroTempBias: boolean;
  gsensorCorrect: boolean;
  gsensorRaw: boolean;
  lumChange: boolean;
  colorEnhance: boolean;
  panelDistance: boolean;
  panelDolby: boolean;
  panelHDR: boolean;
  panelHighDynamic: boolean;
  panelScreenSize: boolean;
  sideBySide: boolean;
  timeSync: boolean;
  userOrbit: boolean;
}

export interface RpcResult {
  ok: boolean;
  error?: string;
  state?: DeviceState;
  capabilities?: Capabilities;
  response?: string | null;
  /**
   * Step the panel reports after a brightness commit, or null when it never
   * reported. Returned in the reply rather than left to the state event because
   * the slider's settle window is still open when it lands, and incoming state
   * is ignored for the duration of a drag.
   */
  luminance?: number | null;
}

export interface Metadata {
  sceneModes: string[];
  audioModes: string[];
  hdrModes: string[];
  screenSizes: string[];
  /** Number of selectable brightness steps (from the device's lookup table). */
  brightnessLevels: number;
  state: DeviceState;
}

export const EMPTY_STATE: DeviceState = {
  connected: false,
  deviceType: null,
  firmwareVersion: null,
  firmwareBuild: null,
  glassesId: null,
  volume: null,
  maxVolume: null,
  volumeLimit: null,
  brightnessLevels: null,
  displayMode3d: null,
  audioMode: null,
  whisper: null,
  mute: null,
  panelDistance: null,
  hdrMode: null,
  hdrEnabled: null,
  sceneMode: null,
  colorEnhance: null,
  audioTube: null,
  screenSize: null,
  luminance: null,
  luminanceValue: null,
  maxLuminance: null,
  wakeup: null,
  raw: {},
};