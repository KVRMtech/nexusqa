/**
 * TEST STUDIO (/apps/:id/studio) — the crawl portal's reuse of the video
 * portal's Test Studio. A crawl produces the SAME artifact-keyed factory data a
 * video does, so the very same panels serve it: every discovered flow becomes a
 * grounded Test Case (Test Cases tab), and any of them can be compiled + run +
 * healed on demand (Playwright tab) — even the ones a Run cycle has never
 * executed end-to-end.
 *
 * This host is thin: it resolves the app's latest crawl artifact and mounts the
 * ported panels (src/studio/*), which reach the factory through qe-central's
 * Phase-1 bridge with the operator's single portal login. No factory code and no
 * video-portal code is touched; this is additive reuse.
 */
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ArrowLeft, FlaskConical, KeyRound, PlayCircle, Route, ShieldCheck, Sparkles } from 'lucide-react';

import { api } from '../../lib/api';
import { useAsync } from '../../lib/useAsync';
import { EmptyState, ErrorState, Loading, Pill } from '../../components';
import DiscoveredFlows from './DiscoveredFlows';
import Journeys from './Journeys';
import TestCasesPanel from '../../studio/TestCasesPanel';
import PlaywrightExecutionPanel from '../../studio/PlaywrightExecutionPanel';
import EvidenceReportPanel from '../../studio/EvidenceReportPanel';
import PersonaMatrixPanel from '../../studio/PersonaMatrixPanel';

type StudioTab = 'flows' | 'journeys' | 'cases' | 'playwright' | 'evidence' | 'personas';

function TabButton({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        'inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold transition-colors ' +
        (active
          ? 'bg-teal/15 text-teal ring-1 ring-teal/40'
          : 'text-ink-low hover:text-ink hover:bg-inset')
      }
      aria-pressed={active}
    >
      {icon}
      {label}
    </button>
  );
}

export function TestStudio() {
  const { id } = useParams<{ id: string }>();
  const [tab, setTab] = useState<StudioTab>('flows');

  const state = useAsync((signal) => api.getApp(id!, { signal }), [id]);

  if (!id) return <ErrorState title="No app selected" error="Missing app id in the route." />;
  if (state.isLoading) return <Loading label="Loading Test Studio…" />;
  if (state.isError) return <ErrorState error={state.error} onRetry={state.reload} />;

  const app = state.data!;
  const artifactId = app.latest_artifact_id;

  return (
    <div className="space-y-5 max-w-[1600px]">
      {/* header */}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <Link
            to={`/apps/${id}`}
            className="inline-flex items-center gap-1.5 text-2xs text-ink-low hover:text-ink mb-2 transition-colors"
          >
            <ArrowLeft size={13} aria-hidden /> {app.name}
          </Link>
          <div className="flex items-center gap-2.5">
            <FlaskConical size={18} className="text-teal" aria-hidden />
            <h1 className="text-lg font-semibold text-ink tracking-tight truncate">Test Studio</h1>
            {app.tier && (
              <Pill tone={app.tier === 'behaves' ? 'teal' : 'warn'} size="sm" variant="outline">
                {app.tier === 'behaves' ? 'Behaves' : 'Renders'}
              </Pill>
            )}
          </div>
          <p className="text-2xs text-ink-low mt-1">
            Every flow the crawl discovered — browse as test cases, run any on demand.
          </p>
        </div>
      </div>

      {artifactId ? (
        <>
          {/* tabs */}
          <div className="flex items-center gap-2">
            <TabButton
              active={tab === 'flows'}
              onClick={() => setTab('flows')}
              icon={<Sparkles size={15} />}
              label="Discovered Flows"
            />
            <TabButton
              active={tab === 'journeys'}
              onClick={() => setTab('journeys')}
              icon={<Route size={15} />}
              label="Journeys"
            />
            <TabButton
              active={tab === 'cases'}
              onClick={() => setTab('cases')}
              icon={<FlaskConical size={15} />}
              label="Test Cases"
            />
            <TabButton
              active={tab === 'playwright'}
              onClick={() => setTab('playwright')}
              icon={<PlayCircle size={15} />}
              label="Playwright"
            />
            <TabButton
              active={tab === 'evidence'}
              onClick={() => setTab('evidence')}
              icon={<ShieldCheck size={15} />}
              label="Evidence Report"
            />
            <TabButton
              active={tab === 'personas'}
              onClick={() => setTab('personas')}
              icon={<KeyRound size={15} />}
              label="Members & Environments"
            />
          </div>

          {/* Discovered Flows is a native governance overlay (token surfaces). The
              ported panels carry their own light-navy identity and assume a light
              ground, so they mount on a token panel surface that matches the rest
              of the (now light) portal. */}
          {tab === 'flows' ? (
            <DiscoveredFlows artifactId={artifactId} onOpenPlaywright={() => setTab('playwright')} />
          ) : tab === 'journeys' ? (
            <Journeys appId={id} />
          ) : (
            <div className="rounded-2xl bg-panel text-ink ring-1 ring-line shadow-card p-5 overflow-x-auto">
              {tab === 'cases' ? (
                <TestCasesPanel artifactId={artifactId} onOpenPlaywright={() => setTab('playwright')} />
              ) : tab === 'evidence' ? (
                <EvidenceReportPanel artifactId={artifactId} />
              ) : tab === 'personas' ? (
                <PersonaMatrixPanel artifactId={artifactId} />
              ) : (
                <PlaywrightExecutionPanel artifactId={artifactId} />
              )}
            </div>
          )}
        </>
      ) : (
        <EmptyState
          title="No crawl artifact yet"
          hint="Crawl this app first (App → Crawl). Once the crawl completes, its discovered flows appear here as test cases you can run."
        />
      )}
    </div>
  );
}

export default TestStudio;
