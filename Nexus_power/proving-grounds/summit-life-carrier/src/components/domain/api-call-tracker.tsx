'use client';

import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Check, X, Loader2, Circle, ChevronDown, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import { PHASE_LABELS } from '@/lib/submission-orchestrator';
import type { ApiCallResult, ApiCallPhase } from '@/lib/types';

interface ApiCallTrackerProps {
  results: ApiCallResult[];
  totalSteps: number;
  /** Optional label to show in the success banner (e.g. case ID) */
  successLabel?: string;
}

const PHASE_DOT_COLORS: Record<ApiCallPhase, string> = {
  customer_verification: 'bg-blue-500',
  vendor_checks: 'bg-purple-500',
  underwriting: 'bg-amber-500',
  compliance: 'bg-red-500',
  rating: 'bg-emerald-500',
  documentation: 'bg-cyan-500',
  finalization: 'bg-gray-500',
};

function StatusIcon({ status }: { status: ApiCallResult['status'] }) {
  switch (status) {
    case 'completed':
      return (
        <motion.div initial={{ scale: 0 }} animate={{ scale: 1 }} transition={{ type: 'spring', stiffness: 500, damping: 25 }}>
          <Check className="h-3.5 w-3.5 text-emerald-500" />
        </motion.div>
      );
    case 'failed':
      return (
        <motion.div initial={{ scale: 0 }} animate={{ scale: 1 }}>
          <X className="h-3.5 w-3.5 text-red-500" />
        </motion.div>
      );
    case 'running':
      return <Loader2 className="h-3.5 w-3.5 text-blue-500 animate-spin" />;
    default:
      return <Circle className="h-3 w-3 text-muted-foreground/40" />;
  }
}

export function ApiCallTracker({ results, totalSteps, successLabel }: ApiCallTrackerProps) {
  const [expandedCalls, setExpandedCalls] = useState<Set<string>>(new Set());

  const completed = results.filter(r => r.status === 'completed').length;
  const failed = results.filter(r => r.status === 'failed').length;
  const running = results.filter(r => r.status === 'running').length;
  const progress = totalSteps > 0 ? ((completed + failed) / totalSteps) * 100 : 0;

  const phaseOrder: ApiCallPhase[] = [
    'customer_verification', 'vendor_checks', 'underwriting',
    'compliance', 'rating', 'documentation', 'finalization',
  ];
  const grouped = phaseOrder.reduce<Record<string, ApiCallResult[]>>((acc, phase) => {
    const items = results.filter(r => r.step.phase === phase);
    if (items.length > 0) acc[phase] = items;
    return acc;
  }, {});

  const toggleExpand = (callId: string) => {
    setExpandedCalls(prev => {
      const next = new Set(prev);
      if (next.has(callId)) next.delete(callId);
      else next.add(callId);
      return next;
    });
  };

  const allDone = results.length > 0 && results.every(r => r.status === 'completed' || r.status === 'failed');
  const allSuccess = allDone && failed === 0;

  return (
    <div className="space-y-4">
      {/* Progress header */}
      <div className="space-y-2">
        <div className="flex items-center justify-between text-sm">
          <span className="font-medium text-foreground">
            {running > 0 ? 'Processing...' : allDone ? 'Complete' : 'Waiting to start'}
          </span>
          <span className="text-muted-foreground font-mono text-xs">
            {completed + failed}/{totalSteps} calls
          </span>
        </div>
        <div className="h-2 w-full rounded-full bg-muted overflow-hidden">
          <motion.div
            className={cn(
              'h-full rounded-full transition-colors',
              allSuccess ? 'bg-emerald-500' : failed > 0 ? 'bg-amber-500' : 'bg-blue-500'
            )}
            initial={{ width: 0 }}
            animate={{ width: `${progress}%` }}
            transition={{ duration: 0.3, ease: 'easeOut' }}
          />
        </div>
      </div>

      {/* Success banner */}
      <AnimatePresence>
        {allSuccess && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="rounded-lg bg-emerald-500/10 border border-emerald-500/20 p-3"
          >
            <div className="flex items-center gap-2">
              <Check className="h-4 w-4 text-emerald-500" />
              <span className="text-sm font-medium text-emerald-700 dark:text-emerald-400">
                All {totalSteps} API calls completed successfully
              </span>
            </div>
            {successLabel && (
              <p className="text-xs text-emerald-600 dark:text-emerald-500 mt-1 ml-6">
                {successLabel}
              </p>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Phase groups */}
      <div className="space-y-3">
        {Object.entries(grouped).map(([phase, calls]) => {
          const phaseLabel = PHASE_LABELS[phase as ApiCallPhase];
          const dotColor = PHASE_DOT_COLORS[phase as ApiCallPhase];
          const phaseCompleted = calls.filter(c => c.status === 'completed').length;
          const phaseTotal = calls.length;

          return (
            <div key={phase} className="space-y-1">
              <div className="flex items-center gap-2 px-1">
                <div className={cn('h-1.5 w-1.5 rounded-full', dotColor)} />
                <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  {phaseLabel.label}
                </span>
                <span className="text-[10px] text-muted-foreground/60 ml-auto font-mono">
                  {phaseCompleted}/{phaseTotal}
                </span>
              </div>

              <div className="space-y-px">
                {calls.map(result => {
                  const elapsed = result.startedAt && result.completedAt
                    ? result.completedAt - result.startedAt
                    : null;
                  const isExpanded = expandedCalls.has(result.step.id);
                  const canExpand = (result.status === 'completed' && result.response) || (result.status === 'failed' && result.error);

                  return (
                    <div key={result.step.id}>
                      <button
                        type="button"
                        onClick={() => canExpand && toggleExpand(result.step.id)}
                        disabled={!canExpand}
                        className={cn(
                          'flex items-center gap-2 w-full rounded px-2 py-1.5 text-left transition-colors',
                          canExpand && 'hover:bg-muted/50 cursor-pointer',
                          !canExpand && 'cursor-default'
                        )}
                      >
                        <StatusIcon status={result.status} />
                        <span
                          className={cn(
                            'text-xs flex-1 truncate',
                            result.status === 'completed' && 'text-foreground',
                            result.status === 'failed' && 'text-red-500',
                            result.status === 'running' && 'text-blue-500 font-medium',
                            result.status === 'pending' && 'text-muted-foreground/60'
                          )}
                        >
                          {result.step.name}
                        </span>
                        {elapsed !== null && (
                          <span className="text-[10px] text-muted-foreground/60 font-mono tabular-nums shrink-0">
                            {elapsed}ms
                          </span>
                        )}
                        {result.status === 'failed' && (
                          <span className="text-[10px] text-red-400 shrink-0">failed</span>
                        )}
                        {canExpand && (
                          isExpanded
                            ? <ChevronDown className="h-3 w-3 text-muted-foreground shrink-0" />
                            : <ChevronRight className="h-3 w-3 text-muted-foreground shrink-0" />
                        )}
                      </button>

                      <AnimatePresence>
                        {isExpanded && (
                          <motion.div
                            initial={{ opacity: 0, height: 0 }}
                            animate={{ opacity: 1, height: 'auto' }}
                            exit={{ opacity: 0, height: 0 }}
                            transition={{ duration: 0.15 }}
                            className="overflow-hidden"
                          >
                            <pre className="text-[10px] leading-relaxed text-muted-foreground bg-muted/30 rounded p-2 ml-6 mr-1 mb-1 overflow-x-auto max-h-32">
                              {result.response
                                ? JSON.stringify(result.response, null, 2)
                                : result.error}
                            </pre>
                          </motion.div>
                        )}
                      </AnimatePresence>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
