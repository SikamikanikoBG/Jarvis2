import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  /** Shown in the fallback so the user knows which screen broke. */
  label: string;
}

interface State {
  error: Error | null;
}

/** Keeps one broken screen from blanking the whole app; the shell and navigation stay usable. */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(`[${this.props.label}] render error`, error, info.componentStack);
  }

  override render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div className="screen">
        <div className="screen-inner">
          <div className="card ep bad">
            <div className="ep-head">
              <span className="dot dot-danger" />
              <h3 style={{ textTransform: 'none' }}>{this.props.label} could not render</h3>
            </div>
            <div className="ep-detail mono small">{this.state.error.message}</div>
            <div className="small muted">Usually a response that does not match the protocol. Check the core version, then reload.</div>
            <button type="button" className="btn btn-secondary btn-sm" style={{ alignSelf: 'flex-start' }} onClick={() => this.setState({ error: null })}>
              Try again
            </button>
          </div>
        </div>
      </div>
    );
  }
}
