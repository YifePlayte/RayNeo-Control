import { PanelSection, PanelSectionRow } from "@decky/ui";
import { Component, type ErrorInfo, type ReactNode } from "react";

import { reportError } from "./diagnostics";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * Catch React render errors and log them.
 *
 * React recovers from a failed render internally, so these never reach
 * window.onerror -- without a boundary they exist only as red text in the
 * panel. Catching them here is the only way to get them into the log.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    reportError(
      "render",
      new Error(
        [error.name + ": " + error.message, error.stack, info.componentStack]
          .filter(Boolean)
          .join("\n"),
      ),
    );
  }

  render(): ReactNode {
    const { error } = this.state;
    if (error) {
      return (
        <PanelSection title="面板出错了 Panel crashed">
          <PanelSectionRow>
            <div style={{ color: "var(--decky-ui-text-danger, #f88)" }}>
              {error.message}
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <div
              style={{
                color: "var(--decky-ui-text-secondary)",
                fontSize: "0.9em",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              详情已写入插件日志
            </div>
          </PanelSectionRow>
        </PanelSection>
      );
    }
    return this.props.children;
  }
}