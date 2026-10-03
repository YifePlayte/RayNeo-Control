/**
 * All device state, in one hook.
 *
 * Kept out of the components so they stay declarative: this file owns the event
 * subscription, the poll, the slider clamps and the apply/error plumbing, and
 * the components only render.
 */

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import {
  commitBrightness as commitBrightnessRemote,
  connectDevice,
  disconnectDevice,
  getCapabilities,
  getMetadata,
  logUiTiming,
  pausePolling,
  reopenDevice,
  setBrightness,
  setVolume,
} from "./api";
import { deviceStore } from "./deviceStore";
import { reportError } from "./diagnostics";
import {
  useClampSlider,
  useCoalesced,
  useDebugFlag,
  usePending,
  useSettledCommit,
} from "./hooks";
import type { Capabilities, DeviceState, RpcResult } from "./types";

/**
 * The definePlugin callback is a plain factory (DefinePluginFn = () => Plugin),
 * not a component: React has no active dispatcher there, so a hook call in that
 * scope throws and the whole panel fails to render with no visible cause. Every
 * lifecycle-dependent hook therefore lives under a real component, and
 * tools/structcheck.py fails the build if one appears outside one.
 */
const DEFAULT_BRIGHTNESS_LEVELS = 12;
const DEFAULT_VOLUME_LIMIT = 15;

/**
 * How long a slider keeps the poll out of the way after the last change.
 *
 * Comfortably longer than the brightness commit delay and the poll interval
 * (1.5 s), so one drag never straddles a poll.
 */
const SLIDER_SETTLE_MS = 1800;

/**
 * How long after the last change the brightness commit is sent.
 *
 * Stands in for the app's "on release": the panel has no release event, so the
 * debounce is what turns a stream of stages into one commit.
 *
 * Must stay clear of {@link SLIDER_SETTLE_MS}. The commit's read-back is
 * allowed to move the thumb while the settle window is open -- it is the
 * device's answer, not a poll event -- but the poll pause also expires on the
 * same clock, and the two overlapping would let a stale report undo the
 * correction a moment later.
 */
const BRIGHTNESS_COMMIT_MS = 500;

/**
 * Minimum gap between two pausePolling RPCs.
 *
 * Comfortably longer than a poll interval, so the pause is still refreshed
 * before it can lapse, and short enough that it never queues behind much.
 */
const PAUSE_THROTTLE_MS = 800;

export interface DeviceController {
  state: DeviceState;
  caps: Capabilities | null;
  connected: boolean;
  /** True while a connect/disconnect/reopen is in flight. */
  busy: boolean;
  error: string | null;
  toast: string | null;
  isPending: (key: string) => boolean;
  flash: (msg: string) => void;

  /** Apply a change, marking only that control as pending. */
  apply: (
    key: string,
    optimistic: () => void,
    fn: () => Promise<RpcResult>,
  ) => void;

  onConnect: () => void;
  onDisconnect: () => void;
  onReopen: () => void;

  /** Debug surface: gates the panel's debug tools and the backend's volume. */
  debug: boolean;
  onSetDebug: (on: boolean) => void;

  /** Slider position and ceiling, kept in sync with the device. */
  brightness: number;
  brightnessMax: number;
  volume: number;
  volumeMax: number;
  setBrightnessNow: (v: number) => void;
  setVolumeNow: (v: number) => void;
}

export function useDevice(): DeviceController {
  // The backend emits an event on every change; index.tsx owns the
  // subscription and feeds this external store, so components just read it.
  const state = useSyncExternalStore(
    deviceStore.subscribe,
    deviceStore.getSnapshot,
    deviceStore.getSnapshot,
  );
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const { isPending, track } = usePending();

  const toastTimer = useRef<number | undefined>(undefined);
  const flash = useCallback((msg: string) => {
    setToast(msg);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 1600);
  }, []);
  useEffect(() => () => window.clearTimeout(toastTimer.current), []);

  const apply = useCallback(
    (
      key: string,
      optimistic: () => void,
      fn: () => Promise<RpcResult>,
    ) => {
      // Move the control immediately; never block the UI on the round trip.
      optimistic();
      setError(null);
      const started = performance.now();
      // Returned rather than discarded, so a caller that sends on every tick can
      // keep exactly one write in flight. Every call here crosses the Decky
      // bridge, which serialises: firing one per tick without awaiting built a
      // six-deep queue whose writes all landed about 1.5 s after the drag ended.
      return (async () => {
        try {
          const r = await track(key, fn);
          const ms = Math.round(performance.now() - started);
          // A refusal is not a confirmation. The backend answers a failed write
          // with {"ok": false} rather than by raising, so awaiting it resolved
          // normally and this used to log "confirmed" for writes that never
          // happened: five of them, up to 17.6 s each, all of which had in fact
          // failed because the cable was pulled mid-command.
          if (r != null && r.ok === false) {
            const why = r.error ?? "no reason given";
            logUiTiming(
              `apply:${key}`,
              `backend REFUSED the change after ${ms} ms: ${why}`,
            ).catch(() => {});
            setError(why);
          } else {
            logUiTiming(
              `apply:${key}`,
              `backend confirmed in ${ms} ms`,
            ).catch(() => {});
          }
        } catch (e) {
          setError(String(e));
          reportError(`apply:${key}`, e);
        }
      })();
    },
    [track],
  );

  const [debug, onSetDebug] = useDebugFlag();

  const loadCaps = useCallback(async () => {
    try {
      const r = await getCapabilities();
      if (r?.ok && r.capabilities) setCaps(r.capabilities);
    } catch {
      /* capability bits are optional; controls just stay enabled */
    }
  }, []);

  // --- initial state -----------------------------------------------------
  // The subscription is set up in index.tsx; this only primes the store so the
  // panel is populated before the first event arrives.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const m = await getMetadata();
        if (cancelled || !m) return;
        if (m.state) deviceStore.setState(m.state);
      } catch (e) {
        if (!cancelled) setError(String(e));
        reportError("getMetadata", e);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // --- slider bounds -----------------------------------------------------
  // brightnessLevels is a count and volumeLimit is already an index; maxVolume is
  // also a count, so reading it as a bound gives the slider a step the device
  // may not have -- silently, on first paint and after a disconnect.
  const brightnessMax = Math.max(
    1,
    state.brightnessLevels ?? DEFAULT_BRIGHTNESS_LEVELS,
  );
  const volumeMax = Math.max(
    1,
    state.volumeLimit ??
      (typeof state.maxVolume === "number"
        ? Math.max(0, state.maxVolume - 1)
        : DEFAULT_VOLUME_LIMIT),
  );

  // Local slider position, so dragging stays smooth while the device round trip
  // is still in flight.
  const [brightness, setBrightnessLocal] = useState<number | null>(null);
  const [volume, setVolumeLocal] = useState<number | null>(null);

  // While a slider is being dragged, incoming device values are dropped for that
  // slider. The device still reports the pre-drag value -- nothing has been
  // transmitted yet -- so applying it would put the thumb back under the user's
  // finger. A deadline again, so nothing has to remember to clear it.
  const adjustingUntil = useRef(0);
  const isAdjusting = useCallback(
    () => Date.now() < adjustingUntil.current,
    [],
  );

  const pausedAt = useRef(0);
  const beginAdjust = useCallback(() => {
    adjustingUntil.current = Date.now() + SLIDER_SETTLE_MS;
    // Throttled, and this matters more than it looks. A drag fires onChange far
    // more often than the write debounce, and every call here used to send an
    // RPC -- hundreds of them per drag. Decky serialises RPCs, so those queued
    // up behind each other and the one write the user was waiting on sat in the
    // queue: brightness took 262 ms on 4 of 29 changes while the write itself
    // took 1 ms. Once per throttle window keeps the pause working without the
    // storm. The local deadline above needs no RPC, so dragging still feels
    // immediate either way.
    const now = Date.now();
    if (now - pausedAt.current < PAUSE_THROTTLE_MS) return;
    pausedAt.current = now;
    // Fire and forget: a pause that does not take hold costs a little extra
    // polling, so it must never surface as an error in the panel.
    pausePolling(SLIDER_SETTLE_MS / 1000).catch(() => {});
  }, []);

  useEffect(() => {
    if (state.luminance != null && !isAdjusting()) {
      setBrightnessLocal(state.luminance);
    }
  }, [state.luminance, isAdjusting]);
  useEffect(() => {
    if (state.volume != null && !isAdjusting()) {
      setVolumeLocal(state.volume);
    }
  }, [state.volume, isAdjusting]);

  useClampSlider(setBrightnessLocal, brightnessMax, brightnessMax - 1);
  useClampSlider(setVolumeLocal, volumeMax, volumeMax);

  // Both sliders write on every tick, which is the official app's shape: it
  // stages brightness continuously (case 4) and commits once at the end (case
  // 5), and volume has no commit stage at all. One write per tick is affordable
  // there because JNI is in-process; here every call crosses a serialising
  // bridge, so useCoalesced keeps one in flight and the newest value only.
  const stageBrightness = useCoalesced(
    useCallback((v: number) => apply("brightness", () => {}, () => setBrightness(v)), [apply]),
  );
  const stageVolume = useCoalesced(
    useCallback((v: number) => apply("volume", () => {}, () => setVolume(v)), [apply]),
  );
  const onConfirmed = useCallback((index?: number | null) => {
    if (typeof index === "number") setBrightnessLocal(index);
  }, []);
  const commitBrightness = useSettledCommit(
    commitBrightnessRemote,
    onConfirmed,
    BRIGHTNESS_COMMIT_MS,
  );
  const setBrightnessNow = useCallback(
    (v: number) => {
      const next = Math.max(0, Math.min(v, brightnessMax - 1));
      setBrightnessLocal(next);
      beginAdjust();
      stageBrightness(next);
      commitBrightness(next);
    },
    [beginAdjust, brightnessMax, commitBrightness, stageBrightness],
  );
  const setVolumeNow = useCallback(
    (v: number) => {
      const next = Math.max(0, Math.min(v, volumeMax));
      setVolumeLocal(next);
      beginAdjust();
      stageVolume(next);
    },
    [beginAdjust, stageVolume, volumeMax],
  );

  // --- connection --------------------------------------------------------
  const onConnect = useCallback(() => {
    setBusy(true);
    setError(null);
    void (async () => {
      try {
        const res = await connectDevice();
        if (res?.state) deviceStore.setState(res.state);
        if (res?.ok) {
          if (res.capabilities) setCaps(res.capabilities as Capabilities);
          else void loadCaps();
          flash("已连接");
        } else {
          setError(res?.error ?? "连接失败");
        }
      } catch (e) {
        setError(String(e));
        reportError("connect", e);
      } finally {
        setBusy(false);
      }
    })();
  }, [flash, loadCaps]);

  /**
   * Drop the USB handle and reconnect.
   *
   * Recovery for a wedged interrupt endpoint: once it stops accepting transfers
   * every write fails within 1 ms, so the panel looks alive but nothing happens.
   */
  const onReopen = useCallback(() => {
    setBusy(true);
    setError(null);
    void (async () => {
      try {
        const res = await reopenDevice();
        if (res?.state) deviceStore.setState(res.state);
        if (res?.ok) {
          flash("已重连 USB");
          void loadCaps();
        } else {
          setError(res?.error ?? "重连失败");
        }
      } catch (e) {
        setError(String(e));
        reportError("reopen", e);
      } finally {
        setBusy(false);
      }
    })();
  }, [flash, loadCaps]);

  const onDisconnect = useCallback(() => {
    setBusy(true);
    void (async () => {
      try {
        await disconnectDevice();
        setCaps(null);
        deviceStore.reset();
        flash("已断开");
      } catch (e) {
        reportError("disconnect", e);
      } finally {
        setBusy(false);
      }
    })();
  }, [flash]);

  return {
    state,
    caps,
    debug,
    onSetDebug,
    connected: state.connected,
    busy,
    error,
    toast,
    isPending,
    flash,
    apply,
    onConnect,
    onDisconnect,
    onReopen,
    brightness: brightness ?? 0,
    brightnessMax,
    volume: volume ?? 0,
    volumeMax,
    setBrightnessNow,
    setVolumeNow,
  };
}