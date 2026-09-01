/**
 * Coverage Ledger panel — the honesty spine, made visible.
 *
 * Turns "did we miss anything on this app?" into a MEASURED number plus a named list of
 * exactly what wasn't fully captured and why. A control is FULLY only when its facts were
 * actually read; everything else is PARTIAL / UNHANDLED / OPAQUE with a plain reason — never
 * a single rolled-up green number, never a silent gap. This is what lets the product tell a
 * customer precisely what it reached and what it did not.
 */
import { useMemo, useState } from 'react';
import { CheckCircle2, ChevronDown, ChevronRight, Gauge } from 'lucide-react';

import { Panel, Pill, SectionHead, SkeletonRows } from '../../components';
import { api } from '../../lib/api';
import { cn } from '../../lib/format';
import { useAsync } from '../../lib/useAsync';
import type { CoverageControl, CoverageDisposition } from '../../types/qec';

const DISP: Record<CoverageDisposition, { label: string; tone: 'good' | 'warn' | 'crit' | 'neutral'; bar: string }> = {
  FULLY: { label: 'Fully captured', tone: 'good', bar: 'var(--good, #12855a)' },
  PARTIAL: { label: 'Partial', tone: 'warn', bar: 'var(--warn, #b3730a)' },
  UNHANDLED: { label: 'Unhandled', tone: 'crit', bar: 'var(--crit, #c33636)' },
  OPAQUE: { label: 'Opaque', tone: 'neutral', bar: 'var(--muted, #6b7488)' },
};

function pctTone(pct: number): 'good' | 'warn' | 'crit' {
  if (pct >= 85) return 'good';
  if (pct >= 60) return 'warn';
  return 'crit';
}

export default function CoveragePanel({ appId }: { appId: string }) {
  const manifest = useAsync((signal) => api.getSeedManifest(appId, 'full', { signal }), [appId]);
  const [showAll, setShowAll] = useState(false);
  const cov = manifest.data?.coverage;

  const segments = useMemo(() => {
    if (!cov || !cov.total) return [];
    return ([
      ['FULLY', cov.fully],
      ['PARTIAL', cov.partial],
      ['UNHANDLED', cov.unhandled],
      ['OPAQUE', cov.opaque],
    ] as [CoverageDisposition, number][])
      .filter(([, n]) => n > 0)
      .map(([d, n]) => ({ d, n, w: (100 * n) / cov.total }));
  }, [cov]);

  if (manifest.isLoading) {
    return (
      <Panel tone="elevated">
        <SectionHead title="Capture coverage" subtitle="how much of this app's UI we fully captured" />
        <div className="mt-3"><SkeletonRows rows={2} /></div>
      </Panel>
    );
  }
  if (!cov || cov.total === 0) return null;

  const pt = pctTone(cov.coverage_pct);
  const gaps = cov.gaps ?? [];
  const count: Record<CoverageDisposition, number> = {
    FULLY: cov.fully, PARTIAL: cov.partial, UNHANDLED: cov.unhandled, OPAQUE: cov.opaque,
  };

  return (
    <Panel tone="elevated">
      <SectionHead
        title="Capture coverage"
        subtitle="every control we did or did not fully capture — measured, never a silent gap"
        icon={<Gauge size={16} className="text-teal" />}
        right={
          <span className="inline-flex items-baseline gap-1">
            <span className={cn('text-2xl font-bold tabular-nums', `text-${pt}`)}>{cov.coverage_pct}%</span>
            <span className="text-2xs text-ink-low">fully captured</span>
          </span>
        }
      />

      {/* breakdown bar */}
      <div className="mt-3 flex h-2.5 w-full overflow-hidden rounded-full bg-line" role="img"
           aria-label={`${cov.fully} fully, ${cov.partial} partial, ${cov.unhandled} unhandled, ${cov.opaque} opaque`}>
        {segments.map((s) => (
          <div key={s.d} style={{ width: `${s.w}%`, background: DISP[s.d].bar }} />
        ))}
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-2xs">
        {(['FULLY', 'PARTIAL', 'UNHANDLED', 'OPAQUE'] as CoverageDisposition[])
          .filter((d) => count[d] > 0)
          .map((d) => (
            <span key={d} className="inline-flex items-center gap-1.5 text-ink-low">
              <span className="h-2 w-2 rounded-full" style={{ background: DISP[d].bar }} aria-hidden />
              {count[d]} {DISP[d].label.toLowerCase()}
            </span>
          ))}
        <span className="text-ink-low">· {cov.total} controls</span>
      </div>

      {/* named gaps */}
      {gaps.length === 0 ? (
        <div className="mt-4 flex items-center gap-2.5 rounded-lg ring-1 ring-good/30 bg-good/[0.06] px-3 py-2">
          <CheckCircle2 size={14} className="text-good shrink-0" aria-hidden />
          <p className="text-2xs text-ink">Every captured control was fully read — nothing partial or unhandled.</p>
        </div>
      ) : (
        <div className="mt-4">
          <button
            type="button"
            onClick={() => setShowAll((s) => !s)}
            className="inline-flex items-center gap-1 text-xs font-medium text-ink hover:text-teal transition"
          >
            {showAll ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            {gaps.length} {gaps.length === 1 ? 'control' : 'controls'} we did not fully capture — and why
          </button>
          {showAll && (
            <div className="mt-2 space-y-1.5">
              {gaps.map((g: CoverageControl) => (
                <div key={g.label} className="flex items-start gap-2.5 rounded-lg bg-inset ring-1 ring-line px-3 py-2">
                  <Pill tone={DISP[g.disposition].tone} size="sm" variant="soft">{DISP[g.disposition].label}</Pill>
                  <div className="min-w-0">
                    <div className="text-xs font-medium text-ink truncate">{g.label}</div>
                    <div className="text-2xs text-ink-low leading-relaxed">{g.reason}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {cov.measures_captured_only && (
        <p className="mt-3 text-2xs text-ink-low leading-relaxed">
          Coverage is measured over what the crawl reached. Surfaces the DOM can't read — a canvas
          app, a closed shadow root, a cross-origin embed — are detected and counted as <b>opaque</b>
          {cov.opaque === 0 ? ' (none found here)' : ''}, never silently assumed covered.
        </p>
      )}
    </Panel>
  );
}
