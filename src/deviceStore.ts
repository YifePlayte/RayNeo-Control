/**
 * External store for the device state pushed by the backend.
 *
 * The event listener is registered in the definePlugin callback and torn down in
 * onDismount, which is the pattern the official plugin template uses. That
 * means the subscription lives outside React, so components read it through
 * useSyncExternalStore rather than holding their own copy of the state.
 */

import type { DeviceState } from "./types";
import { EMPTY_STATE } from "./types";

type Listener = () => void;

let snapshot: DeviceState = EMPTY_STATE;
const listeners = new Set<Listener>();

function emit(): void {
  for (const fn of listeners) fn();
}

export const deviceStore = {
  /** Registered by index.tsx; returns the unsubscribe function. */
  subscribe(fn: Listener): () => void {
    listeners.add(fn);
    return () => {
      listeners.delete(fn);
    };
  },

  /** Must return a stable reference between changes, or React loops. */
  getSnapshot(): DeviceState {
    return snapshot;
  },

  setState(next: DeviceState): void {
    if (next === snapshot) return;
    snapshot = next;
    emit();
  },

  reset(): void {
    if (snapshot === EMPTY_STATE) return;
    snapshot = EMPTY_STATE;
    emit();
  },

  /** Test/diagnostic helper: how many components are currently subscribed. */
  get subscriberCount(): number {
    return listeners.size;
  },
};