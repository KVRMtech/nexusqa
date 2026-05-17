// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Page Header Component
// ═══════════════════════════════════════════════════════════════
import type { ReactNode } from 'react';
import { LiveBadge } from './LiveBadge';

interface PageHeaderProps {
  /** Zone label shown in uppercase above the title (e.g., "ZONE 1 · KNOWLEDGE CAPTURE") */
  zone?: string;
  /** Page title */
  title: string;
  /** Subtitle / description below the title */
  subtitle?: string;
  /** Show the Live/Demo badge */
  isLive?: boolean;
  /** Action buttons shown on the right */
  actions?: ReactNode;
  /** Additional content below the title row (e.g., filter bar, tabs) */
  children?: ReactNode;
}

export function PageHeader({ zone, title, subtitle, isLive, actions, children }: PageHeaderProps) {
  return (
    <div data-page-header className="mb-6 space-y-4 animate-[fade-in_0.3s_ease-out]">
      {isLive === false && (
        <div
          role="alert"
          className="flex items-center gap-2 rounded-lg border border-yellow-500/30 bg-yellow-500/10 px-4 py-2 text-sm text-yellow-300"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            className="h-4 w-4 shrink-0"
            viewBox="0 0 20 20"
            fill="currentColor"
          >
            <path
              fillRule="evenodd"
              d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z"
              clipRule="evenodd"
            />
          </svg>
          <span>
            <strong>Backend Unavailable</strong> — This page could not load live
            data from the API. Fix the failing backend dependency or request
            path before trusting the UI state.
          </span>
        </div>
      )}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          {zone && <p className="zone-header mb-1">{zone}</p>}
          <div className="flex items-center gap-3">
            <h1 className="page-title">{title}</h1>
            {typeof isLive === 'boolean' && <LiveBadge isLive={isLive} />}
          </div>
          {subtitle && <p className="page-subtitle">{subtitle}</p>}
        </div>
        {actions && (
          <div className="flex flex-wrap items-center gap-2 shrink-0">{actions}</div>
        )}
      </div>
      {children}
    </div>
  );
}
