/**
 * Display labels for the native option keys.
 *
 * Scene modes are ordered the way the *firmware* numbers them, not the way the
 * Java SDK enum is named. The SDK calls slot 2 "Soft" (柔和) and slot 3
 * "EyeProtection" (护眼), but the app's i18n lists 阅读 as a separate mode keyed
 * `scene_office_mode`, and on real hardware sending 3 produces 阅读. So the
 * user-visible order is 通用 / 电影 / 护眼 / 阅读 -> 0 / 1 / 2 / 3.
 *
 * Reading (阅读) also suppresses colour enhancement, and AI-HDR does too, so
 * the toggle is greyed out in either mode rather than letting the panel write a
 * combination the app would not.
 */
export const SCENE_LABELS: Record<string, string> = {
  standard: "通用",
  movie: "电影",
  eyeProtection: "护眼",
  reading: "阅读",
};

/**
 * Scene modes the official app locks a control out in, plus one the hardware
 * enforces on its own.
 *
 * Colour enhancement has two restrictions, and they come from different places:
 *
 *   - AI-HDR, per the app's i18n:
 *       colorEnhanceDisableTips = AI-HDR模式下不支持启用色彩增强
 *   - 阅读, per the hardware. **Not** per the i18n, and not per the status
 *     report either -- in 阅读 mode `0x17` reads back as 1, but the panel does
 *     not apply it. Confirmed by hand: the toggle cannot be turned on there.
 *     That is the read-back lying, and hardware wins over it.
 *
 * 护眼 is deliberately absent. The device clears `0x17` by itself when you
 * switch into it, but it accepts the value back happily -- so it is a one-off
 * reset, not a lock, and gating it would take away a control that works.
 *
 * Screen size uses the app's i18n, which is explicit about both:
 *
 *   screen_size_not_supported_in_scene_office_mode = 阅读场景模式下无法调整屏幕尺寸
 *   screen_size_not_supported_in_3D_mode = 3D 模式下不支持调整屏幕尺寸
 */
export const SCENE_BLOCKS_COLOR_ENHANCE = ["reading"] as const;

export const SCENE_BLOCKS_SCREEN_SIZE = ["reading"] as const;

export const MODE_BLOCKS_SCREEN_SIZE = ["3d"] as const;

export const AUDIO_MODE_LABELS: Record<string, string> = {
  standard: "标准",
  whisper: "轻语",
  surround: "空间环绕",
};

// Names follow the official app's i18n keys: `dynamicQuality` (Dynamic Quality)
// and `colorEnhance`. Note the app itself is inconsistent about the English --
// the toggle reads "Color Enhancement" while its tooltip says "Color boost" --
// so the shorter form is used here.
export const HDR_MODE_LABELS: Record<string, string> = {
  sdr: "SDR",
  aihdr: "AI-HDR",
};

export const SCREEN_SIZE_LABELS: Record<string, string> = {
  large: "115%",
  medium: "100%",
  small: "85%",
};

export const DEVICE_TYPE_NAMES: Record<number, string> = {
  32: "NextView Pro",
  33: "Aries",
  34: "Aries 1.5 (SeeYa)",
  35: "Aries 1.5 (Sony)",
  36: "Aries 1.8",
  48: "Taurus",
  49: "Taurus 1.5",
  53: "Taurus 2.0",
  54: "Taurus 3.0",
  55: "Taurus 3.0 Pro",
  56: "Taurus 2.0 Pro",
  57: "Taurus 4.0",
  58: "Taurus 4.0 Pro",
  // 64/65 are internal SDK codenames ("Gemini 1.0"/"Gemini 2.0") that the
  // official app never shows either. 65 is confirmed by hardware to be the
  // RayNeo GT Max; 64 is the generation before it and the retail name is not
  // documented anywhere in the APK, so it stays generic.
  64: "RayNeo GT",
  65: "RayNeo GT Max",
};

export function modelName(type: number | null): string {
  if (type == null) return "未知型号";
  return DEVICE_TYPE_NAMES[type] ?? `型号 ${type}`;
}