/**
 * Loading / Empty / Error — the reusable state primitives EVERY data view uses,
 * so the portal's "loading, empty, error on every data view" contract is one
 * import, not re-invented per screen.
 */
import type { ReactNode } from 'react';
import { AlertTriangle, Inbox, Loader2, RotateCw } from 'lucide-react';

import type { QecApiError } from '../lib/api';
import { cn } from '../lib/format';

// ── Spinner ──────────────────────────────────────────────────────────────────

export function Spinner({ size = 18, className }: { size?: number; className?: string }) {
  return <Loader2 size={size} className={cn('animate-spin text-teal', className)} aria-hidden />;
}

// ── Loading ──────────────────────────────────────────────────────────────────

export interface LoadingProps {
  label?: string;
  className?: string;
  /** 'panel' = padded block; 'inline' = compact row. */
  variant?: 'panel' | 'inline';
}

export function Loading({ label = 'Loading…', className, variant = 'panel' }: LoadingProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'flex items-center gap-2 text-ink-low',
        variant === 'panel' ? 'justify-center py-10' : 'py-1',
        className,
      )}
    >
      <Spinner />
      <span className="text-xs">{label}</span>
    </div>
  );
}

// ── Skeleton ─────────────────────────────────────────────────────────────────

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        'rounded-md bg-line-strong/40 relative overflow-hidden',
        'before:absolute before:inset-0 before:animate-sheen',
        "before:bg-[linear-gradient(90deg,transparent,rgb(var(--ink)/0.06),transparent)] before:bg-[length:200%_100%]",
        className,
      )}
      aria-hidden
    />
  );
}

export function SkeletonRows({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('space-y-2', className)} aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  );
}

// ── Empty ────────────────────────────────────────────────────────────────────

export interface EmptyStateProps {
  title?: string;
  hint?: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ title = 'Nothing here yet', hint, icon, action, className }: EmptyStateProps) {
  return (
    <div className={cn('flex flex-col items-center justify-center text-center py-10 px-4', className)}>
      <span className="text-ink-faint mb-2">{icon ?? <Inbox size={26} aria-hidden />}</span>
      <p className="text-sm text-ink-mid font-medium">{title}</p>
      {hint != null && <p className="text-xs text-ink-low mt-1 max-w-sm">{hint}</p>}
      {action != null && <div className="mt-3">{action}</div>}
    </div>
  );
}

// ── Error ────────────────────────────────────────────────────────────────────

export interface ErrorStateProps {
  error?: QecApiError | Error | string | null;
  title?: string;
  onRetry?: () => void;
  className?: string;
}

function messageOf(error: ErrorStateProps['error']): string {
  if (!error) return 'Something went wrong.';
  if (typeof error === 'string') return error;
  return error.message || 'Something went wrong.';
}

export function ErrorState({ error, title = 'Could not load this', onRetry, className }: ErrorStateProps) {
  const status = error && typeof error !== 'string' && 'status' in error ? (error as QecApiError).status : undefined;
  return (
    <div
      role="alert"
      className={cn(
        'flex flex-col items-center justify-center text-center py-9 px-4 rounded-lg ring-1 ring-crit/25 bg-crit/[0.06]',
        className,
      )}
    >
      <AlertTriangle size={24} className="text-crit mb-2" aria-hidden />
      <p className="text-sm text-ink font-medium">{title}</p>
      <p className="text-xs text-ink-low mt-1 max-w-md break-words">
        {status ? <span className="tabular text-crit mr-1">[{status}]</span> : null}
        {messageOf(error)}
      </p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 inline-flex items-center gap-1.5 text-xs font-semibold text-teal hover:text-ink rounded-md px-2.5 py-1.5 ring-1 ring-teal/30 hover:ring-teal/60 transition-colors"
        >
          <RotateCw size={13} aria-hidden />
          Retry
        </button>
      )}
    </div>
  );
}
