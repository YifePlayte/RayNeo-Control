import { useCallback, useEffect, useRef, useState } from "react";

import {
  getDebug,
  logUiTiming,
  setDebug as setDebugRemote,
  setDebugNotes,
} from "./api";
import { reportError } from "./diagnostics";

/**
 * How long a dropdown shows the user's choice before falling back to whatever
 * the glasses report.
 *
 * Comfortably longer than the slowest silent command's round trip -- scene mode
 * is three frames plus a settle read, about 0.1 s -- and short enough that a
 * change the device throws away does not sit on screen looking accepted.
 */
const OPTIMISTIC_TTL_MS = 2000;

/**
 * The debug surface switch, mirrored from the backend.
 *
 * One flag covers both halves: the panel hides its debug tools and the backend
 * stops writing the per-frame trace, the startup protocol tables and the
 * `[ui]` timing notes. It lives backend-side so it survives a reload, and the
 * frontend only mirrors it and primes its own note-suppression.
 *
 * The frontend's half is applied to the module-level switch in `api.ts` rather
 * than to React state, because the notes are emitted from callers that have no
 * access to a hook and must not pay an RPC to be dropped.
 */
export function useDebugFlag(): [boolean, (on: boolean) => void] {
  const [debug, setDebug] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await getDebug();
        if (cancelled || typeof r?.debug !== "boolean") return;
        setDebug(r.debug);
        setDebugNotes(r.debug);
      } catch {
        /* off is the default and a fine fallback */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const set = useCallback((on: boolean) => {
    // Moved first, and the notes switched with it: this is a preference, not a
    // device write, so it must not wait on a round trip to feel right.
    setDebug(on);
    setDebugNotes(on);
    void setDebugRemote(on).catch(() => {});
  }, []);

  return [debug, set];
}

/**
 * A dropdown that moves the instant it is chosen.
 *
 * Five of the commands the panel sends draw no reply at all, so the device
 * cannot confirm them faster than a status-report round trip. Showing that
 * delay makes the control feel dead even though the write went out at once --
 * and it is exactly why the scene, picture-quality and sound-mode dropdowns
 * lagged while screen size did not.
 *
 * So the choice is shown first and then replaced by whatever the glasses
 * report, and expires on its own if nothing ever arrives.
 *
 * Note this is only the *display*. Whether a change actually landed is decided
 * by the backend, which logs a WARN when the device reports something other
 * than what was asked for -- that is the authoritative answer, and it does not
 * depend on a React effect firing.
 */
export function useChoice<T extends string>(
  reported: T | null,
  fallback: T,
  name = "choice",
): readonly [T, (v: T) => void] {
  const [choice, setChoice] = useState<T | null>(null);
  const timer = useRef<number | undefined>(undefined);

  const pick = useCallback(
    (v: T) => {
      logUiTiming(`${name} pick`, `chose "${v}" (reported was "${reported}")`)
        .catch(() => {});
      setChoice(v);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(
        () => setChoice(null),
        OPTIMISTIC_TTL_MS,
      );
    },
    [name, reported],
  );

  // Drop the override once the device reports the change. It is dropped by the
  // *expiry timer* above rather than by an effect on `reported`, because the
  // optimistic value already falls back to `reported` below -- so an effect
  // would only add a second path to the same place, and in practice never ran.
  //
  // Whether a change actually landed is not decided here. The backend logs a WARN
  // when the device reports something other than what was asked for, and that
  // is the authoritative answer; this side only reports how long it took.
  useEffect(() => () => window.clearTimeout(timer.current), []);

  return [choice ?? reported ?? fallback, pick] as const;
}

/**
 * Keep a slider inside its bounds.
 *
 * The device can report a value above the current ceiling -- volume 15 while the
 * audio tube caps it at 12 -- and a controlled slider handed an out-of-range
 * value clamps itself and fires `onChange`, which would write the clamp back to
 * the device. Every poll.
 */
export function useClampSlider(
  set: (fn: (v: number | null) => number | null) => void,
  max: number,
  highestValid: number,
): void {
  useEffect(() => {
    set((v) => (v == null || v <= highestValid ? v : highestValid));
  }, [max, highestValid, set]);
}

/**
 * Send on every call, but keep only one request in flight.
 *
 * A drag fires `onChange` faster than a request can cross the bridge, and
 * Decky serialises those. Measured with one request per tick: six writes went
 * out together, the backend ran each in 1 ms, and every confirmation came back
 * 1.3–1.5 s later — a six-deep queue. The slider moved immediately but the
 * hardware followed a second and a half behind, which reads as stutter.
 *
 * So: send at once when idle, and while one is in flight remember only the
 * newest value. When the current one finishes, send that. The queue is then
 * never deeper than one, intermediate values that the user has already dragged
 * past are dropped instead of queued, and the value under their finger is
 * always the last thing written.
 *
 * This is not a debounce. Nothing waits to be sent: the first movement of a
 * gesture goes out immediately, which is what a trailing debounce got wrong.
 */
export function useCoalesced<A>(send: (v: A) => Promise<unknown>): (v: A) => void {
  const inFlight = useRef(false);
  const latest = useRef<{ v: A } | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const drain = useCallback(async () => {
    const next = latest.current;
    if (!next) return;
    latest.current = null;
    inFlight.current = true;
    try {
      await send(next.v);
    } catch {
      /* apply() has already reported the failure */
    } finally {
      inFlight.current = false;
      if (latest.current && alive.current) void drain();
    }
  }, [send]);

  return useCallback(
    (v: A) => {
      latest.current = { v };
      if (!inFlight.current) void drain();
    },
    [drain],
  );
}

/**
 * Commit a two-stage setting once the gesture settles.
 *
 * Brightness is the official app's odd one out: `GlassDeviceManager` dispatches
 * case 4 (`setBrightnessIndex` -> 0x09) on every tick and case 5
 * (`saveBrightness` -> 0x0D) once at the end, so a change is *staged* live and
 * *committed* after the finger stops. The panel has no release event, so this
 * debounce is what stands in for one.
 *
 * `onConfirmed` receives the step the device reports, which may not be the one
 * that was asked for. That is the point: the caller springs the slider back to
 * where the hardware is, rather than leaving it on a value the panel refused.
 *
 * Lives in a hook because it is state and an async round trip, not rendering.
 */
export function useSettledCommit(
  commit: () => Promise<{
    ok?: boolean;
    error?: string;
    luminance?: number | null;
  }>,
  onConfirmed: (luminance?: number | null) => void,
  delayMs: number,
): (v: number) => void {
  // The read-back is the one thing allowed to move the thumb while the settle
  // window is still open. Incoming state is ignored for the duration of a drag
  // so a stale report cannot fight the user's finger, but this is the device's
  // answer to the change just asked for, so it wins: if the panel refused the
  // step, the slider goes back to where the hardware actually is.
  // Returns false when the commit was refused, so the caller does not also log
  // a success: the log used to carry both "REFUSED the commit" and "committed in
  // 721 ms" for the same call.
  const settle = useCallback(
    (r: { ok?: boolean; error?: string; luminance?: number | null } | null) => {
      if (r != null && r.ok === false) {
        logUiTiming(
          "apply:brightnessSave",
          `backend REFUSED the commit: ${r.error ?? "no reason given"}`,
        ).catch(() => {});
        return false;
      }
      onConfirmed(r?.luminance);
      return true;
    },
    [onConfirmed],
  );

  return useDebounced((_v: number) => {
    const started = performance.now();
    void (async () => {
      try {
        const r = await commit();
        if (!settle(r)) return;
        logUiTiming(
          "apply:brightnessSave",
          `committed in ${Math.round(performance.now() - started)} ms, `
            + `panel reads ${r?.luminance ?? "nothing"}`,
        ).catch(() => {});
      } catch (e) {
        reportError("apply:brightnessSave", e);
      }
    })();
  }, delayMs);
}

/**
 * Trailing-edge debounce.
 *
 * Only used for the brightness *commit* now. The stages themselves are sent on
 * every tick, as the app does; debouncing them was what made the panel feel
 * like it lagged, because each drag collapsed into one write that landed after
 * the thumb had already stopped.
 */
export function useDebounced<A extends unknown[]>(
  fn: (...args: A) => void,
  delayMs: number,
): ((...args: A) => void) & { flush: () => void; cancel: () => void } {
  const timer = useRef<number | undefined>(undefined);
  const pendingArgs = useRef<A | null>(null);
  const latest = useRef(fn);
  latest.current = fn;

  const cancel = useCallback(() => {
    window.clearTimeout(timer.current);
    timer.current = undefined;
    pendingArgs.current = null;
  }, []);

  const flush = useCallback(() => {
    if (timer.current === undefined) return;
    window.clearTimeout(timer.current);
    timer.current = undefined;
    const args = pendingArgs.current;
    pendingArgs.current = null;
    if (args) latest.current(...args);
  }, []);

  useEffect(() => cancel, [cancel]);

  const debounced = useCallback(
    (...args: A) => {
      pendingArgs.current = args;
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(flush, delayMs);
    },
    [delayMs, flush],
  );

  return Object.assign(debounced, { flush, cancel });
}

/**
 * Per-control pending tracking.
 *
 * Only the control that is actually changing shows a busy state; the rest of
 * the panel stays interactive. Keys are short strings like "brightness".
 */
export function usePending() {
  const [pending, setPending] = useState<Record<string, number>>({});

  const track = useCallback(
    async <T,>(key: string, fn: () => Promise<T>): Promise<T> => {
      // A counter rather than a boolean so overlapping calls for the same key
      // do not clear each other's state early.
      setPending((p) => ({ ...p, [key]: (p[key] ?? 0) + 1 }));
      try {
        return await fn();
      } finally {
        setPending((p) => {
          const next = (p[key] ?? 1) - 1;
          if (next <= 0) {
            const { [key]: _drop, ...rest } = p;
            return rest;
          }
          return { ...p, [key]: next };
        });
      }
    },
    [],
  );

  const isPending = useCallback((key: string) => pending[key] > 0, [pending]);
  return { isPending, track };
}
