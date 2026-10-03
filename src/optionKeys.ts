/** Canonical option keys, in display order. Mirrors the backend maps. */

export const SCENE_KEYS = [
  "standard",
  "movie",
  "eyeProtection",
  "reading",
] as const;

export const AUDIO_MODE_KEYS = ["standard", "whisper", "surround"] as const;

export const HDR_MODE_KEYS = ["sdr", "aihdr"] as const;

export const SCREEN_SIZE_KEYS = ["large", "medium", "small"] as const;

export type SceneKey = (typeof SCENE_KEYS)[number];
export type AudioModeKey = (typeof AUDIO_MODE_KEYS)[number];
export type HdrModeKey = (typeof HDR_MODE_KEYS)[number];
export type ScreenSizeKey = (typeof SCREEN_SIZE_KEYS)[number];