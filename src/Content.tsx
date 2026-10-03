import {
  ButtonItem,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
} from "@decky/ui";
import { useEffect } from "react";

import {
  setAudioMode,
  setAudioTube,
  setColorEnhance,
  setDisplayMode,
  setHdrMode,
  setSceneMode,
  setScreenSize,
} from "./api";
import { DebugSection } from "./DebugSection";
import { ErrorBoundary } from "./ErrorBoundary";
import {
  AUDIO_MODE_LABELS,
  HDR_MODE_LABELS,
  MODE_BLOCKS_SCREEN_SIZE,
  SCENE_BLOCKS_COLOR_ENHANCE,
  SCENE_BLOCKS_SCREEN_SIZE,
  SCENE_LABELS,
  SCREEN_SIZE_LABELS,
  modelName,
} from "./labels";
import { installErrorReporting } from "./diagnostics";
import {
  AUDIO_MODE_KEYS,
  HDR_MODE_KEYS,
  SCENE_KEYS,
  SCREEN_SIZE_KEYS,
  type AudioModeKey,
  type HdrModeKey,
  type SceneKey,
  type ScreenSizeKey,
} from "./optionKeys";
import { useChoice } from "./hooks";
import { useDevice } from "./useDevice";
import type { DeviceState } from "./types";

/** Native numeric value -> option key (inverse of the backend maps). */
const AUDIO_MODE_BY_VALUE: Record<number, AudioModeKey> = {
  0: "standard",
  1: "whisper",
  2: "surround",
};

const HDR_BY_VALUE: Record<number, HdrModeKey> = {
  0: "sdr",
  1: "aihdr",
};

function toOptions<T extends string>(
  keys: readonly T[],
  labels: Record<string, string>,
): { data: T; label: string }[] {
  return keys.map((k) => ({ data: k, label: labels[k] ?? k }));
}

/**
 * The plugin body.
 *
 * The definePlugin callback in index.tsx is a plain factory, not a component, so
 * no hook may be called from there. Everything stateful lives under this
 * boundary so a render failure still leaves the panel showing something.
 */
export function Content() {
  useEffect(() => {
    // Uncaught exceptions and rejected promises that escape React.
    installErrorReporting();
  }, []);
  return (
    <ErrorBoundary>
      <Panel />
    </ErrorBoundary>
  );
}

function Panel() {
  const dev = useDevice();
  const {
    state,
    caps,
    connected,
    busy,
    error,
    toast,
    apply,
    onConnect,
    onDisconnect,
    onReopen,
  } = dev;

  // Every setting below is read back from the glasses rather than remembered
  // here, so the panel shows what the hardware is actually doing -- including
  // after the phone app changes something. Each dropdown still shows the choice
  // the moment it is made, then hands over to the reported value.
  const [screenSize, pickScreenSize] = useChoice<ScreenSizeKey>(
    state.screenSize as ScreenSizeKey | null,
    "medium",
    "screenSize",
  );
  const [sceneMode, pickSceneMode] = useChoice<SceneKey>(
    state.sceneMode as SceneKey | null,
    "standard",
    "sceneMode",
  );
  const [hdrOpt, pickHdr] = useChoice<HdrModeKey>(
    state.hdrMode != null ? HDR_BY_VALUE[state.hdrMode] ?? "sdr" : null,
    "sdr",
    "hdrMode",
  );
  // NOTE: the app's i18n carries strings like "开启导音鳍后不支持调整音频模式"
  // and "阅读场景模式下无法调整屏幕尺寸", but the GT Max does not honour the
  // first one (the audio mode still changes with the tube on). Nothing is gated
  // on those strings: greying out a control that actually works is worse than
  // letting the device ignore a change it does not accept.
  const [audioMode, pickAudioMode] = useChoice<AudioModeKey>(
    state.audioMode != null
      ? AUDIO_MODE_BY_VALUE[state.audioMode] ?? "standard"
      : null,
    "standard",
    "audioMode",
  );
  const [displayMode, pickDisplayMode] = useChoice<"2d" | "3d">(
    state.displayMode3d == null ? null : state.displayMode3d ? "3d" : "2d",
    "2d",
    "displayMode",
  );

  // Colour boost is locked out in two places, for two different reasons: AI-HDR
  // owns the colour pipeline (the app says so in
  // "AI-HDR模式下不支持启用色彩增强"), and 阅读 refuses it in hardware -- the status
  // byte reads back 1 there but the panel does not apply it, which is a
  // read-back that lies rather than a setting that took. 护眼 is not gated: the
  // device clears the value on entry but accepts it back.
  const colorEnhanceBlocked =
    hdrOpt === "aihdr" ||
    (SCENE_BLOCKS_COLOR_ENHANCE as readonly string[]).includes(sceneMode);

  // Screen size is the one the app's i18n names a scene mode for:
  // "阅读场景模式下无法调整屏幕尺寸", and separately "3D模式下不支持调整屏幕尺寸".
  // Neither was gated before, so the panel let the user write a combination the
  // app does not offer.
  const screenSizeBlocked =
    (SCENE_BLOCKS_SCREEN_SIZE as readonly string[]).includes(sceneMode) ||
    (MODE_BLOCKS_SCREEN_SIZE as readonly string[]).includes(displayMode);

  // Only the connection controls are gated on `busy`; settings stay live.
  const locked = !connected;

  return (
    <>
      <PanelSection title="连接 Connection">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={connected ? onDisconnect : onConnect}
          >
            {busy ? "处理中…" : connected ? "断开 Disconnect" : "连接 Connect"}
          </ButtonItem>
        </PanelSectionRow>
        {connected && (
          <PanelSectionRow>
            <InfoLine label="型号 Model" value={modelName(state.deviceType)} />
          </PanelSectionRow>
        )}
        {connected && firmwareLabel(state) && (
          <PanelSectionRow>
            <InfoLine label="固件 Firmware" value={firmwareLabel(state)} />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="显示 Display">
        <PanelSectionRow>
          <SliderField
            label="屏幕亮度"
            description="Brightness"
            value={dev.brightness}
            min={0}
            max={dev.brightnessMax - 1}
            step={1}
            disabled={locked || caps?.lumChange === false}
            showValue
            onChange={dev.setBrightnessNow}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="显示模式"
            description="Display Mode"
            layout="inline"
            disabled={locked}
            rgOptions={[
              { data: "2d", label: "2D" },
              { data: "3d", label: "3D" },
            ]}
            selectedOption={displayMode}
            onChange={(o) =>
              apply("displayMode", () => pickDisplayMode(o.data),
                    () => setDisplayMode(o.data))
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="屏幕尺寸"
            description="Screen Size"
            layout="inline"
            disabled={
              locked || caps?.panelScreenSize === false || screenSizeBlocked
            }
            rgOptions={toOptions(SCREEN_SIZE_KEYS, SCREEN_SIZE_LABELS)}
            selectedOption={screenSize}
            onChange={(o) =>
              apply("screenSize", () => pickScreenSize(o.data),
                    () => setScreenSize(o.data))
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="场景模式"
            description="Scene Mode"
            layout="inline"
            disabled={locked || caps?.colorAdjust === false}
            rgOptions={toOptions(SCENE_KEYS, SCENE_LABELS)}
            selectedOption={sceneMode}
            onChange={(o) =>
              apply("sceneMode", () => pickSceneMode(o.data),
                    () => setSceneMode(o.data))
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="动态画质"
            description="Dynamic Quality"
            layout="inline"
            disabled={locked || caps?.panelHDR === false}
            rgOptions={toOptions(HDR_MODE_KEYS, HDR_MODE_LABELS)}
            selectedOption={hdrOpt}
            onChange={(o) =>
              apply("hdrMode", () => pickHdr(o.data),
                    () => setHdrMode(o.data))
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="色彩增强"
            description="Color Boost"
            checked={state.colorEnhance ?? false}
            disabled={locked || caps?.colorEnhance === false || colorEnhanceBlocked}
            onChange={(v: boolean) =>
              apply("colorEnhance", () => {}, () => setColorEnhance(v))
            }
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="声音 Sound">
        <PanelSectionRow>
          <SliderField
            label="音量"
            description="Volume"
            value={dev.volume}
            min={0}
            max={dev.volumeMax}
            step={1}
            disabled={locked || caps?.audioVolume === false}
            showValue
            onChange={dev.setVolumeNow}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="音效"
            description="Sound Effect"
            layout="inline"
            disabled={locked || caps?.audioQuietMode === false}
            rgOptions={toOptions(AUDIO_MODE_KEYS, AUDIO_MODE_LABELS)}
            selectedOption={audioMode}
            onChange={(o) =>
              apply("audioMode", () => pickAudioMode(o.data),
                    () => setAudioMode(o.data))
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="导音鳍"
            description="Sound Tube"
            checked={state.audioTube ?? false}
            disabled={locked || caps?.audioTubeMode === false}
            onChange={(v: boolean) =>
              apply("audioTube", () => {}, () => setAudioTube(v))
            }
          />
        </PanelSectionRow>
      </PanelSection>

      {error && (
        <PanelSection>
          <PanelSectionRow>
            <div style={{ color: "var(--decky-ui-text-danger, #f88)" }}>
              {error}
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      <DebugSection
        connected={connected}
        busy={busy}
        onReopen={onReopen}
        debug={dev.debug}
        onSetDebug={dev.onSetDebug}
      />

      {toast && (
        <div
          style={{
            position: "fixed",
            bottom: 32,
            left: "50%",
            transform: "translateX(-50%)",
            background: "rgba(0,0,0,0.8)",
            padding: "6px 14px",
            borderRadius: 4,
            zIndex: 1000,
          }}
        >
          {toast}
        </div>
      )}
    </>
  );
}

/**
 * Firmware, the way the official app writes it.
 *
 * The build date leads because that is what the app shows and what people
 * recognise -- "20260922" rather than "26". The numeric revision is a separate
 * field (reply[0x24] vs the date string at 0x18) and is kept alongside it in
 * brackets, because it is real and measured: it is the SDK's own
 * firmwareVersion, just not the one the app puts on screen.
 *
 * Either half can be missing on its own, so neither is assumed present.
 */
function firmwareLabel(state: DeviceState): string {
  const build = state.firmwareBuild;
  const rev = state.firmwareVersion;
  if (build && rev != null) return `${build} (${rev})`;
  return build ?? (rev != null ? String(rev) : "");
}

function InfoLine({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        width: "100%",
      }}
    >
      <span>{label}</span>
      <span style={{ color: "var(--decky-ui-text-secondary)" }}>{value}</span>
    </div>
  );
}