// ═══════════════════════════════════════════════════════════════
//  VKPOWER AI ENGINE FACTORY — Global Error Boundary
// ═══════════════════════════════════════════════════════════════
import { Component, type ReactNode, type ErrorInfo } from 'react';
import { AlertTriangle, RefreshCw, Home } from 'lucide-react';

interface Props {
  children: ReactNode;
  /** Optional fallback UI. If not provided, uses the default full-page error UI. */
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

/**
 * Top-level error boundary that catches render errors in the entire application.
 * Shows a full-page recovery UI with reload / go-home actions.
 */
export class GlobalErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    this.setState({ errorInfo });
    // In production, send to telemetry/Sentry here
    console.error('[GlobalErrorBoundary]', error, errorInfo);
  }

  handleReload = () => window.location.reload();
  handleGoHome = () => { window.location.href = '/'; };

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;

      return (
        <div className="flex h-screen items-center justify-center bg-[#f5f7fa] p-6">
          <div className="max-w-lg w-full text-center space-y-6">
            {/* Icon */}
            <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-2xl bg-red-500/15 ring-1 ring-red-500/25">
              <AlertTriangle className="h-8 w-8 text-red-400" />
            </div>

            {/* Title */}
            <div>
              <h1 className="text-2xl font-bold text-[#0a2540]">Something went wrong</h1>
              <p className="mt-2 text-sm text-slate-500">
                An unexpected error occurred in the application. This has been logged automatically.
              </p>
            </div>

            {/* Error details (dev only) */}
            {import.meta.env.DEV && this.state.error && (
              <details className="text-left rounded-lg bg-white ring-1 ring-white/[0.06] p-4">
                <summary className="text-xs font-medium text-slate-500 cursor-pointer">
                  Error Details
                </summary>
                <pre className="mt-2 text-xs text-red-300 overflow-auto max-h-48 whitespace-pre-wrap">
                  {this.state.error.toString()}
                  {this.state.errorInfo?.componentStack}
                </pre>
              </details>
            )}

            {/* Actions */}
            <div className="flex items-center justify-center gap-3">
              <button onClick={this.handleReload} className="btn-primary">
                <RefreshCw className="h-4 w-4" />
                Reload Page
              </button>
              <button onClick={this.handleGoHome} className="btn-secondary">
                <Home className="h-4 w-4" />
                Go Home
              </button>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
