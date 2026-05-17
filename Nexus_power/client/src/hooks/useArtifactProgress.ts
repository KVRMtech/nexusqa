// ═══════════════════════════════════════════════════════════════
//  useArtifactProgress — live eyes-engine progress for one artifact
// ═══════════════════════════════════════════════════════════════
//
// Wraps the generic ``useSSE`` hook with the artifact-progress
// endpoint contract introduced in Phase 2:
//
//   GET /api/v1/artifacts/{artifact_id}/progress  (text/event-stream)
//
// Events are JSON objects shaped like:
//
//   { artifact_id, eyes_job_id, status, current_stage, progress_percent }
//
// or terminal markers:
//
//   { timeout: true, eyes_job_id }
//   { error: '<message>' }
//
// The hook surfaces the latest snapshot and a derived ``isComplete``
// flag, and stays connected with exponential-backoff retry until the
// server emits a terminal status — at which point we close the
// stream so the browser doesn't keep an idle EventSource around.

import { useEffect, useMemo, useState } from 'react';
import { useSSE } from './useSSE';

export interface ArtifactProgressEvent {
  artifact_id?: string;
  eyes_job_id?: string | null;
  status?: string | null;
  current_stage?: string | null;
  progress_percent?: number | null;
  /** Optional artifact-level fields surfaced when no eyes job is linked. */
  semantic_completeness_score?: number | null;
  note?: string | null;
  /** Terminal markers. */
  timeout?: boolean;
  error?: string;
}

interface UseArtifactProgressOptions {
  artifactId: string;
  /** Optional explicit eyes job id — forwarded as ?eyes_job_id=. */
  eyesJobId?: string | null;
  /** Disable the connection (e.g. when the inspector is closed). */
  enabled?: boolean;
  /** Polling interval seconds passed to the server (0.5 – 10.0). */
  pollInterval?: number;
}

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);

export function useArtifactProgress({
  artifactId,
  eyesJobId,
  enabled = true,
  pollInterval = 1.5,
}: UseArtifactProgressOptions) {
  const [latest, setLatest] = useState<ArtifactProgressEvent | null>(null);
  const [history, setHistory] = useState<ArtifactProgressEvent[]>([]);

  // Build the SSE path with the eyes_job_id + poll_interval query
  // params.  Memoised so the URL only changes when its inputs change
  // (avoids the useSSE hook reconnecting on every render).
  const path = useMemo(() => {
    const qs = new URLSearchParams();
    if (eyesJobId) qs.set('eyes_job_id', eyesJobId);
    if (pollInterval !== 1.5) qs.set('poll_interval', String(pollInterval));
    const suffix = qs.toString();
    return `/v1/artifacts/${encodeURIComponent(artifactId)}/progress${suffix ? `?${suffix}` : ''}`;
  }, [artifactId, eyesJobId, pollInterval]);

  const isTerminal = useMemo(() => {
    if (!latest) return false;
    if (latest.timeout) return true;
    if (latest.error) return true;
    if (typeof latest.status === 'string' && TERMINAL_STATUSES.has(latest.status.toLowerCase())) {
      return true;
    }
    return false;
  }, [latest]);

  // Disable the inner SSE connection once we've reached a terminal
  // state so the browser doesn't keep an idle EventSource open.
  const { status, disconnect, reconnect } = useSSE({
    path,
    enabled: enabled && !!artifactId && !isTerminal,
    autoReconnect: true,
    onMessage: (data) => {
      if (data && typeof data === 'object') {
        const evt = data as ArtifactProgressEvent;
        setLatest(evt);
        setHistory(prev => (prev.length >= 100 ? [...prev.slice(-99), evt] : [...prev, evt]));
      }
    },
  });

  // When the artifact id changes (user navigates to another artifact),
  // forget the previous progress so the UI doesn't briefly show stale
  // numbers from the old stream.
  useEffect(() => {
    setLatest(null);
    setHistory([]);
  }, [artifactId]);

  return {
    /** Latest progress snapshot from the stream. */
    latest,
    /** Append-only history (most recent last; capped at 100 events). */
    history,
    /** SSE connection status — useful for surfacing a "reconnecting" hint. */
    sseStatus: status,
    /** True once the upstream job finished, errored, or timed out. */
    isTerminal,
    /** Manually close the SSE connection (e.g. when the inspector closes). */
    disconnect,
    /** Force-reconnect after a terminal close (e.g. user re-triggered processing). */
    reconnect,
  };
}
