// Per-widget error boundary. Before this, a widget
// whose render threw (e.g. NoteView reading `.length` off a nullable field a
// live row actually had null) had nothing between it and the app root —
// React unmounts up to the nearest boundary, and there wasn't one, so one
// bad note blanked the whole dashboard. One boundary per panel means a
// single broken widget shows its own inline error and every other panel
// keeps working; reused as a defensive backstop alongside the normalisation
// fix in NoteEditor.tsx (`normalizeNote`), not instead of it.
import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
  widgetTitle: string;
}

interface State {
  error: Error | null;
}

export class WidgetErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error(`Widget "${this.props.widgetTitle}" crashed:`, error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="panel-state error" role="alert">
          {this.props.widgetTitle} hit an error and couldn't render: {this.state.error.message || "unknown error"}
        </div>
      );
    }
    return this.props.children;
  }
}
