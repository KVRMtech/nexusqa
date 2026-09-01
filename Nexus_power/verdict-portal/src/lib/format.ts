/** VKPower Verdict — small pure formatting + class-name helpers. */
import { clsx } from 'clsx';
import type { ClassValue } from 'clsx';
import { formatDistanceToNowStrict } from 'date-fns';

/** Merge conditional class names (thin wrapper over clsx). */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}

/** Short form of a hash for display: first…last (monospace elsewhere). */
export function shortHash(hash: string | null | undefined, head = 8, tail = 6): string {
  if (!hash) return '—';
  if (hash.length <= head + tail + 1) return hash;
  return `${hash.slice(0, head)}…${hash.slice(-tail)}`;
}

/** Format a 0–100 percentage; null → the honest "n/a" (never a misleading 0). */
export function formatPct(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
  return `${value.toFixed(digits)}%`;
}

/** Human count with thousands separators. */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toLocaleString('en-US');
}

/** Format a USD string/number, or "—" when dollars were never priced. */
export function formatUsd(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—';
  const n = typeof value === 'string' ? Number(value) : value;
  if (Number.isNaN(n)) return '—';
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Absolute local timestamp (e.g. "Jul 9, 14:32"). */
export function formatDateTime(ts: string | null | undefined): string {
  if (!ts) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

/** Relative "time ago" (e.g. "8 min ago"), or "—". */
export function timeAgo(ts: string | null | undefined): string {
  if (!ts) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '—';
  // addSuffix is direction-aware: "30 minutes ago" for the past AND "in 30 days" for
  // the future — so a FUTURE timestamp (e.g. an attestation expiry) never renders as
  // "… ago" (which read as an expired gate even though it is valid for 30 more days).
  return formatDistanceToNowStrict(d, { addSuffix: true });
}

/** Title-case a snake/kebab token (e.g. "budget_stopped" → "Budget Stopped"). */
export function humanize(token: string | null | undefined): string {
  if (!token) return '';
  return token
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
