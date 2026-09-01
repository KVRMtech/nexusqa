import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Monitor, Eye, ArrowRight, Loader2, AlertCircle, CheckCircle2, Play } from 'lucide-react';
import api from '../services/api';

interface ArtifactRow {
  artifact_id: string;
  session_id: string;
  source_filename: string | null;
  status: string;
  has_visual_semantics: boolean;
  /** True only when visual_scenes are actually persisted in the DB. */
  visual_e2e_ready: boolean;
  scene_count: number;
  frame_count: number;
  visual_summary: string;
  created_at: string;
  processing_time_seconds: number;
}

export default function VisualE2ETestsPage() {
  const [artifacts, setArtifacts] = useState<ArtifactRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const tenantId = JSON.parse(sessionStorage.getItem('nexus_user') || '{}').tenant_id || 'nexus-platform';
        const sessions = await api.listSessions(tenantId);
        const sessionList = Array.isArray(sessions) ? sessions : sessions?.sessions ?? [];

        // For each session, fetch artifacts and find ones with visual semantics
        const visualArtifacts: ArtifactRow[] = [];
        for (const sess of sessionList) {
          const sid = sess.session_id || sess.id;
          if (!sid) continue;
          try {
            const arts = await api.listSessionArtifacts(sid, tenantId);
            if (Array.isArray(arts)) {
              for (const a of arts) {
                if (a.status === 'completed') {
                  visualArtifacts.push({
                    artifact_id: a.artifact_id,
                    session_id: sid,
                    source_filename: a.source_filename,
                    status: a.status,
                    has_visual_semantics: a.has_visual_semantics ?? false,
                    visual_e2e_ready: a.visual_e2e_ready ?? false,
                    scene_count: a.scene_count ?? 0,
                    frame_count: a.frame_count ?? 0,
                    visual_summary: a.visual_summary ?? '',
                    created_at: a.created_at,
                    processing_time_seconds: a.processing_time_seconds ?? 0,
                  });
                }
              }
            }
          } catch {
            // skip sessions without artifacts
          }
        }

        // Sort by created_at descending
        visualArtifacts.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
        if (!cancelled) setArtifacts(visualArtifacts);
      } catch (err: unknown) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load sessions');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="h-8 w-8 text-[#2670a3] animate-spin" />
          <p className="text-sm text-slate-500">Loading visual analysis sessions&hellip;</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="flex flex-col items-center gap-4 text-red-400">
          <AlertCircle className="h-8 w-8" />
          <p className="text-sm">{error}</p>
        </div>
      </div>
    );
  }

  // visual_e2e_ready = graph substrate is persisted; use it as the definitive gate.
  // Artifacts where has_visual_semantics=true but visual_e2e_ready=false had their
  // persist_visual_evidence stage skipped (pipeline ordering fault — degraded).
  const visualReady = artifacts.filter(a => a.visual_e2e_ready);
  const degraded = artifacts.filter(a => a.has_visual_semantics && !a.visual_e2e_ready);
  const noVisual = artifacts.filter(a => !a.has_visual_semantics && !a.visual_e2e_ready);

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      {/* Header */}
      <div className="flex items-center gap-4">
        <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-br from-[#2670a3]/15 to-[#2670a3]/5 border border-[#2670a3]/20">
          <Monitor className="h-6 w-6 text-[#2670a3]" />
        </div>
        <div>
          <h1 className="text-2xl font-bold text-[#0a2540]">Visual E2E Tests</h1>
          <p className="text-sm text-slate-500">
            Inspect scene-by-scene visual evidence, approve transitions, and generate Playwright tests
          </p>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-4">
        <div className="rounded-lg bg-white border border-gray-200 shadow-sm p-4">
          <p className="text-xs text-slate-500 uppercase tracking-wide">Total Artifacts</p>
          <p className="text-2xl font-bold text-[#0a2540] mt-1">{artifacts.length}</p>
        </div>
        <div className="rounded-lg bg-white border border-green-200 shadow-sm p-4">
          <p className="text-xs text-green-600 uppercase tracking-wide">Visual Ready</p>
          <p className="text-2xl font-bold text-green-600 mt-1">{visualReady.length}</p>
        </div>
        {degraded.length > 0 && (
          <div className="rounded-lg bg-white border border-amber-300 shadow-sm p-4">
            <p className="text-xs text-amber-600 uppercase tracking-wide">Graph Degraded</p>
            <p className="text-2xl font-bold text-amber-500 mt-1">{degraded.length}</p>
          </div>
        )}
        <div className="rounded-lg bg-white border border-gray-200 shadow-sm p-4">
          <p className="text-xs text-slate-500 uppercase tracking-wide">Pending Visual</p>
          <p className="text-2xl font-bold text-slate-400 mt-1">{noVisual.length}</p>
        </div>
      </div>

      {/* Visual-ready artifacts */}
      {visualReady.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-lg font-semibold text-[#0a2540] flex items-center gap-2">
            <Eye className="h-5 w-5 text-green-600" />
            Ready for Visual E2E Testing
          </h2>
          <div className="space-y-3">
            {visualReady.map(a => (
              <div
                key={a.artifact_id}
                className="rounded-lg bg-white border border-gray-200 hover:border-[#2670a3]/40 shadow-sm hover:shadow-md transition-all p-5"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1 min-w-0 space-y-2">
                    <div className="flex items-center gap-3">
                      <CheckCircle2 className="h-4 w-4 text-green-600 flex-shrink-0" />
                      <h3 className="text-sm font-medium text-[#0a2540] truncate">
                        {a.source_filename || `Artifact ${a.artifact_id.slice(0, 8)}`}
                      </h3>
                      <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-green-50 text-green-700 border border-green-200">
                        VISUAL READY
                      </span>
                    </div>
                    <p className="text-xs text-slate-500 line-clamp-2">
                      {a.visual_summary || 'No visual summary available'}
                    </p>
                    <div className="flex items-center gap-4 text-xs text-slate-400">
                      <span>{a.scene_count} scenes</span>
                      <span>{a.frame_count} frames</span>
                      <span>{new Date(a.created_at).toLocaleDateString()}</span>
                    </div>
                  </div>
                  <div className="flex flex-col gap-2">
                    <Link
                      to={`/sessions/${a.session_id}/visual-flow?artifact_id=${a.artifact_id}`}
                      className="flex items-center gap-2 px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors"
                    >
                      <Play className="h-3.5 w-3.5" />
                      Open Visual Flow
                    </Link>
                    <Link
                      to={`/sessions/${a.session_id}/canonical`}
                      className="flex items-center gap-2 px-4 py-2 rounded-lg bg-gray-100 hover:bg-gray-200 text-slate-600 text-xs transition-colors"
                    >
                      View Canonical Result
                      <ArrowRight className="h-3 w-3" />
                    </Link>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Degraded: analysis succeeded but visual graph was not persisted */}
      {degraded.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-lg font-semibold text-amber-600 flex items-center gap-2">
            <AlertCircle className="h-5 w-5" />
            Visual Graph Degraded
          </h2>
          <p className="text-xs text-slate-500">
            Visual analysis completed but the scene graph was not persisted
            (pipeline ordering fault). Re-upload the video to regenerate, or
            contact an admin to replay the persistence stage.
          </p>
          <div className="space-y-2">
            {degraded.map(a => (
              <div
                key={a.artifact_id}
                className="rounded-lg bg-amber-50 border border-amber-200 p-4"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <AlertCircle className="h-4 w-4 text-amber-500" />
                    <span className="text-sm text-slate-700">
                      {a.source_filename || `Artifact ${a.artifact_id.slice(0, 8)}`}
                    </span>
                    <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-amber-100 text-amber-700 border border-amber-300">
                      GRAPH DEGRADED
                    </span>
                  </div>
                  <Link
                    to={`/sessions/${a.session_id}/canonical`}
                    className="text-xs text-slate-400 hover:text-[#2670a3]"
                  >
                    View Result →
                  </Link>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Non-visual artifacts */}
      {noVisual.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-lg font-semibold text-slate-500">
            Artifacts Without Visual Analysis
          </h2>
          <div className="space-y-2">
            {noVisual.map(a => (
              <div
                key={a.artifact_id}
                className="rounded-lg bg-gray-50 border border-gray-200 p-4 opacity-70"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <AlertCircle className="h-4 w-4 text-slate-400" />
                    <span className="text-sm text-slate-500">
                      {a.source_filename || `Artifact ${a.artifact_id.slice(0, 8)}`}
                    </span>
                  </div>
                  <Link
                    to={`/sessions/${a.session_id}/canonical`}
                    className="text-xs text-slate-400 hover:text-[#2670a3]"
                  >
                    View Result →
                  </Link>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {artifacts.length === 0 && (
        <div className="text-center py-16">
          <Monitor className="h-12 w-12 text-slate-300 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-slate-500 mb-2">No completed artifacts yet</h3>
          <p className="text-sm text-slate-400">
            Upload a screen recording via Sessions to start visual E2E analysis
          </p>
          <Link to="/sessions" className="inline-flex items-center gap-2 mt-4 text-[#2670a3] hover:text-[#1d5784] text-sm">
            Go to Sessions <ArrowRight className="h-4 w-4" />
          </Link>
        </div>
      )}
    </div>
  );
}
