/**
 * Frontend error reporting.
 *
 * A React error is shown as red text in the panel and nowhere else -- it never
 * reaches the plugin log, which makes a crash on the frontend invisible to
 * `tools/smoke_test.py` and painful to diagnose. Everything here funnels into
 * one RPC that writes to the Decky log, so what the user sees on screen is also
 * in ~/homebrew/logs/RayNeo-Control/.
 */

import { callable } from "@decky/api";

export const logFrontendError = callable<
  [where: string, message: string, detail?: string],
  Record<string, never>
>("log_frontend_error");


/** Flatten anything thrown into a loggable string. */
function describe(err: unknown): { message: string; detail?: string } {
  if (err instanceof Error) {
    const out: { message: string; detail?: string } = {
      message: `${err.name}: ${err.message}`,
    };
    // A React render failure often carries no useful stack, so include the
    // component stack when there is one.
    const props = err as Error & { componentStack?: string };
    const parts = [err.stack, props.componentStack].filter(Boolean);
    if (parts.length > 0) out.detail = parts.join("\n");
    return out;
  }
  if (typeof err === "string") return { message: err };
  try {
    return { message: JSON.stringify(err) };
  } catch {
    return { message: String(err) };
  }
}

let installed = false;

/**
 * Install global handlers for errors that escape React: uncaught exceptions and
 * rejected promises. React render errors are caught separately by the
 * ErrorBoundary below, since React swallows them before they reach window.
 */
export function installErrorReporting(): void {
  if (installed) return;
  installed = true;

  window.addEventListener("error", (ev: ErrorEvent) => {
    const { message, detail } = describe(ev.error ?? ev.message);
    void logFrontendError("window.onerror", message, detail).catch(() => {
      /* the log channel itself is down; nothing more to do */
    });
  });

  window.addEventListener("unhandledrejection", (ev: PromiseRejectionEvent) => {
    const { message, detail } = describe(ev.reason);
    void logFrontendError("unhandledrejection", message, detail).catch(() => {
      /* ignore */
    });
  });
}

/** Report an error that was caught somewhere we can name. */
export function reportError(where: string, err: unknown): void {
  const { message, detail } = describe(err);
  void logFrontendError(where, message, detail).catch(() => {
    /* ignore */
  });
}


/**
 * Wrap a callable so every failure is logged before it propagates.
 *
 * The red text in the panel comes from these rejections, and without this the
 * log only ever shows the backend half of a failure.
 */
export function logged<TArgs extends unknown[], TResult>(
  name: string,
  fn: (...args: TArgs) => Promise<TResult>,
): (...args: TArgs) => Promise<TResult> {
  return async (...args: TArgs) => {
    try {
      return await fn(...args);
    } catch (err) {
      const { message, detail } = describe(err);
      void logFrontendError(
        `rpc:${name}(${args.map((a) => JSON.stringify(a)).join(", ")})`,
        message,
        detail,
      ).catch(() => {
        /* ignore */
      });
      throw err;
    }
  };
}