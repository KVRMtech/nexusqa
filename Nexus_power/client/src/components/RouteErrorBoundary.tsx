// ═══════════════════════════════════════════════════════════════
//  VKPOWER AI ENGINE FACTORY — Route-Level Error Boundary
// ═══════════════════════════════════════════════════════════════
import { Component, type ReactNode, type ErrorInfo } from 'react';
import { AlertOctagon, RefreshCw, ArrowLeft } from 'lucide-react';

interface Props {
  children: ReactNode;
  /** Page name shown in the error card */
  pageName?: string;
  /** Custom fallback for inline errors */
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * Route-level error boundary that catches render errors within a single page.
 * Shows an inline recovery card without taking down the entire app/sidebar.
 */
export class RouteErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error(`[RouteErrorBoundary:${this.props.pageName || 'unknown'}]`, error, errorInfo);
  }

  handleRetry = () => this.setState({ hasError: false, error: null });
  handleGoBack = () => window.history.back();

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;

      return (
        <div className="flex flex-col items-center justify-center py-20 px-4">
          <div className="max-w-md w-full space-y-5 text-center">
            <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-xl bg-red-500/15 ring-1 ring-red-500/25">
              <AlertOctagon className="h-7 w-7 text-red-400" />
            </div>

            <div>
              <h2 className="text-xl font-bold text-[#0a2540]">
                {this.props.pageName ? `${this.props.pageName} crashed` : 'Page error'}
              </h2>
              <p className="mt-1.5 text-sm text-slate-500">
                This module encountered an error. Other modules are unaffected.
              </p>
            </div>

            {import.meta.env.DEV && this.state.error && (
              <div className="rounded-lg bg-white ring-1 ring-white/[0.06] p-3 text-left">
                <pre className="text-xs text-red-300 overflow-auto max-h-32 whitespace-pre-wrap">
                  {this.state.error.toString()}
                </pre>
              </div>
            )}

            <div className="flex items-center justify-center gap-3">
              <button onClick={this.handleRetry} className="btn-primary">
                <RefreshCw className="h-4 w-4" />
                Retry
              </button>
              <button onClick={this.handleGoBack} className="btn-secondary">
                <ArrowLeft className="h-4 w-4" />
                Go Back
              </button>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
