// ═══════════════════════════════════════════════════════════════
//  MODULE 4 — KNOWLEDGE GRAPH EXPLORER
//  "Interactive knowledge graph with natural language queries"
// ═══════════════════════════════════════════════════════════════

import { useState, useEffect, useMemo, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import api from '../services/api';
import { useApiData } from '../hooks/useApiData';
import { PageHeader } from '../components';
import { StatusBadge } from '../components/StatusBadge';
import type { GraphNode, GraphStats } from '../types';
import type { CanonicalArtifact } from '../types/canonical';
import { EmptyState } from '../components/EmptyState';
import {
  Network,
  Search,
  MessageSquare,
  Brain,
  Users,
  FlaskConical,
  AlertTriangle,
  FileText,
  Link2,
  Filter,
  ZoomIn,
  ZoomOut,
  Maximize2,
  Send,
  Loader2,
  ChevronRight,
  Tag,
} from 'lucide-react';
import clsx from 'clsx';

// ── Demo graph data ────────────────────────────────────────

interface GraphVisNode {
  id: string;
  label: string;
  type: string;
  color: string;
  size: number;
  x: number;
  y: number;
}

interface GraphVisEdge {
  source: string;
  target: string;
  label: string;
  type: 'validates' | 'contradicts' | 'stated_by' | 'traces_to' | 'depends_on';
}

const NODE_COLORS: Record<string, string> = {
  Person: '#8b5cf6',
  BusinessRule: '#6366f1',
  TestCase: '#22c55e',
  Contradiction: '#ef4444',
  Regulation: '#f59e0b',
  Jira: '#3b82f6',
  Session: '#ec4899',
  Product: '#06b6d4',
};

const EMPTY_NODES: GraphVisNode[] = [];

const EMPTY_EDGES: GraphVisEdge[] = [];

const NL_SUGGESTIONS = [
  'Show me every rule that has no test coverage',
  'What did John say about nonforfeiture in the last month?',
  'Find all contradictions between actuarial and dev team',
  'Which rules are linked to TX regulations?',
  'Show the full lineage for test case TC-4521',
];

const EDGE_COLORS: Record<string, string> = {
  validates: '#22c55e',
  contradicts: '#ef4444',
  stated_by: '#8b5cf6',
  traces_to: '#3b82f6',
  depends_on: '#f59e0b',
};

export default function KnowledgeGraphPage() {
  const { user } = useAuth();
  const [searchParams] = useSearchParams();
  const artifactId = searchParams.get('artifact_id');
  const sessionId = searchParams.get('session_id');
  const tenantId = user?.tenant_id || '';

  // ── Backbone graph data (single API call) ──────────────
  const { data: graphData, isLive } = useApiData(
    () => api.searchKnowledge('*', tenantId).then((r: any) => ({
      nodes: (r.nodes || EMPTY_NODES) as GraphVisNode[],
      edges: (r.edges || EMPTY_EDGES) as GraphVisEdge[],
    })),
    { nodes: EMPTY_NODES, edges: EMPTY_EDGES },
    !!tenantId,
  );
  const graphNodes = graphData.nodes;
  const graphEdges = graphData.edges;

  // ── Canonical artifact visual graph (when artifact_id is provided) ──
  const [artifactNodes, setArtifactNodes] = useState<GraphVisNode[]>([]);
  const [artifactEdges, setArtifactEdges] = useState<GraphVisEdge[]>([]);
  const [artifactSource, setArtifactSource] = useState<string | null>(null);

  useEffect(() => {
    if (!artifactId || !tenantId) return;
    let cancelled = false;

    api.getArtifact(artifactId).then((artifact: CanonicalArtifact) => {
      if (cancelled) return;
      const blob = (artifact.full_artifact_json ?? {}) as Record<string, unknown>;
      const vg = blob.visual_graph as { nodes?: unknown[]; edges?: unknown[] } | undefined;
      if (!vg) return;

      setArtifactSource(artifact.source_filename ?? artifact.artifact_id);

      // Convert visual graph nodes to GraphVisNode format
      const nodes: GraphVisNode[] = (vg.nodes ?? []).map((n: any, i: number) => ({
        id: `vg-node-${n.id ?? i}`,
        label: n.label ?? n.name ?? `Node ${i}`,
        type: n.type ?? 'VisualEntity',
        color: NODE_COLORS[n.type ?? 'Product'] ?? '#06b6d4',
        size: n.size ?? 18,
        x: n.x ?? 100 + (i % 8) * 90,
        y: n.y ?? 80 + Math.floor(i / 8) * 80,
      }));

      const edges: GraphVisEdge[] = (vg.edges ?? []).map((e: any, i: number) => ({
        source: `vg-node-${e.source ?? 0}`,
        target: `vg-node-${e.target ?? 0}`,
        label: e.label ?? e.relationship ?? 'related',
        type: (e.type ?? 'traces_to') as GraphVisEdge['type'],
      }));

      if (!cancelled) {
        setArtifactNodes(nodes);
        setArtifactEdges(edges);
      }
    }).catch(() => {});

    return () => { cancelled = true; };
  }, [artifactId, tenantId]);

  // Merge backbone + artifact nodes/edges
  const mergedNodes = useMemo(() => [...graphNodes, ...artifactNodes], [graphNodes, artifactNodes]);
  const mergedEdges = useMemo(() => [...graphEdges, ...artifactEdges], [graphEdges, artifactEdges]);

  const [searchQuery, setSearchQuery] = useState('');
  const [nlQuery, setNlQuery] = useState('');
  const [nlLoading, setNlLoading] = useState(false);
  const [nlResult, setNlResult] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphVisNode | null>(null);
  const [filters, setFilters] = useState<Set<string>>(new Set());
  const [zoom, setZoom] = useState(1);

  const nodeTypes = useMemo(() => {
    const types = new Map<string, number>();
    mergedNodes.forEach((n) => types.set(n.type, (types.get(n.type) || 0) + 1));
    return types;
  }, [mergedNodes]);

  const visibleNodes = useMemo(() => {
    let nodes = mergedNodes;
    if (filters.size > 0) {
      nodes = nodes.filter((n) => filters.has(n.type));
    }
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      nodes = nodes.filter((n) => n.label.toLowerCase().includes(q) || n.type.toLowerCase().includes(q));
    }
    return nodes;
  }, [mergedNodes, filters, searchQuery]);

  const visibleEdges = useMemo(() => {
    const ids = new Set(visibleNodes.map((n) => n.id));
    return mergedEdges.filter((e) => ids.has(e.source) && ids.has(e.target));
  }, [visibleNodes, mergedEdges]);

  const toggleFilter = (type: string) => {
    setFilters((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  const handleNlSearch = async () => {
    if (!nlQuery.trim() || !tenantId) return;
    setNlLoading(true);
    setNlResult(null);
    try {
      const result = await api.searchKnowledge(nlQuery.trim(), tenantId);
      const nodeCount = result.nodes?.length ?? 0;
      const edgeCount = result.edges?.length ?? 0;
      setNlResult(`Found ${nodeCount} matching nodes and ${edgeCount} relationships for "${nlQuery}".`);
    } catch {
      setNlResult('Search failed — knowledge graph endpoint may be unavailable.');
    }
    setNlLoading(false);
  };

  return (
    <div className="space-y-4 animate-fade-in">
      {/* Header */}
      <PageHeader
        title="Knowledge Graph Explorer"
        subtitle="Navigate relationships between rules, tests, people, and evidence."
        isLive={isLive}
      />

      {/* Canonical artifact context banner */}
      {artifactSource && (
        <div className="flex items-center gap-3 px-4 py-2 bg-nexus-500/10 border border-nexus-500/30 rounded-lg">
          <StatusBadge label="Canonical Context" variant="nexus" />
          <span className="text-xs text-gray-300">Showing visual graph entities from <span className="text-nexus-400 font-medium">{artifactSource}</span></span>
          {artifactNodes.length > 0 && (
            <span className="text-[10px] text-gray-500 ml-auto">{artifactNodes.length} nodes, {artifactEdges.length} edges from artifact</span>
          )}
        </div>
      )}

      {/* Search bar */}
      <div className="flex gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-500" />
          <input
            type="text"
            className="input-field pl-10"
            placeholder="Search nodes by name, type, or keyword..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
      </div>

      {/* Graph stats + filters */}
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-xs text-gray-500">
          {mergedNodes.length} nodes • {mergedEdges.length} edges • {mergedNodes.filter((n) => n.type === 'Contradiction').length} contradictions
        </span>
        <span className="text-gray-700">|</span>
        <span className="text-xs text-gray-500">Filters:</span>
        {Array.from(nodeTypes.entries()).map(([type, count]) => (
          <button
            key={type}
            onClick={() => toggleFilter(type)}
            className={clsx(
              'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium transition-all ring-1 ring-inset',
              filters.size === 0 || filters.has(type)
                ? 'ring-white/20 text-gray-200'
                : 'ring-white/5 text-gray-600 opacity-50',
            )}
          >
            <span className="h-2 w-2 rounded-full" style={{ backgroundColor: NODE_COLORS[type] || '#666' }} />
            {type} ({count})
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        {/* Graph Canvas (3 cols) */}
        <div className="lg:col-span-3 card overflow-hidden">
          {/* Toolbar */}
          <div className="flex items-center justify-between px-4 py-2 bg-white/[0.02] border-b border-white/[0.06]">
            <div className="flex gap-1">
              <button onClick={() => setZoom((z) => Math.min(z + 0.2, 2))} className="btn-ghost p-1.5">
                <ZoomIn className="h-4 w-4" />
              </button>
              <button onClick={() => setZoom((z) => Math.max(z - 0.2, 0.4))} className="btn-ghost p-1.5">
                <ZoomOut className="h-4 w-4" />
              </button>
              <button onClick={() => setZoom(1)} className="btn-ghost p-1.5">
                <Maximize2 className="h-4 w-4" />
              </button>
            </div>
            <div className="flex gap-4 text-[10px] text-gray-500">
              <span className="flex items-center gap-1"><span className="h-1.5 w-4 rounded bg-green-500" /> Validates</span>
              <span className="flex items-center gap-1"><span className="h-1.5 w-4 rounded bg-red-500" /> Contradicts</span>
              <span className="flex items-center gap-1"><span className="h-1.5 w-4 rounded bg-purple-500" /> Stated By</span>
              <span className="flex items-center gap-1"><span className="h-1.5 w-4 rounded bg-blue-500" /> Traces To</span>
              <span className="flex items-center gap-1"><span className="h-1.5 w-4 rounded bg-yellow-500" /> Depends On</span>
            </div>
          </div>

          {/* SVG Graph */}
          <div className="relative bg-gray-950/50" style={{ height: 500 }}>
            {mergedNodes.length === 0 && (
              <div className="absolute inset-0 flex items-center justify-center z-10">
                <EmptyState title="No Knowledge Graph Data" description="The graph populates as sessions are processed and rules extracted." />
              </div>
            )}
            <svg
              width="100%"
              height="100%"
              viewBox={`0 0 ${900 / zoom} ${500 / zoom}`}
              className="cursor-grab active:cursor-grabbing"
            >
              <defs>
                <marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
                  <polygon points="0 0, 8 3, 0 6" fill="#555" />
                </marker>
                {/* Glow filter */}
                <filter id="glow">
                  <feGaussianBlur stdDeviation="3" result="coloredBlur" />
                  <feMerge>
                    <feMergeNode in="coloredBlur" />
                    <feMergeNode in="SourceGraphic" />
                  </feMerge>
                </filter>
              </defs>

              {/* Edges */}
              {visibleEdges.map((edge, idx) => {
                const src = visibleNodes.find((n) => n.id === edge.source);
                const tgt = visibleNodes.find((n) => n.id === edge.target);
                if (!src || !tgt) return null;
                return (
                  <g key={idx}>
                    <line
                      x1={src.x}
                      y1={src.y}
                      x2={tgt.x}
                      y2={tgt.y}
                      stroke={EDGE_COLORS[edge.type] || '#444'}
                      strokeWidth={edge.type === 'contradicts' ? 2 : 1.2}
                      strokeOpacity={0.5}
                      strokeDasharray={edge.type === 'contradicts' ? '5,3' : 'none'}
                      markerEnd="url(#arrowhead)"
                    />
                    <text
                      x={(src.x + tgt.x) / 2}
                      y={(src.y + tgt.y) / 2 - 6}
                      fill="#666"
                      fontSize="8"
                      textAnchor="middle"
                    >
                      {edge.label}
                    </text>
                  </g>
                );
              })}

              {/* Nodes */}
              {visibleNodes.map((node) => (
                <g
                  key={node.id}
                  className="cursor-pointer"
                  onClick={() => setSelectedNode(selectedNode?.id === node.id ? null : node)}
                >
                  <circle
                    cx={node.x}
                    cy={node.y}
                    r={node.size}
                    fill={node.color}
                    fillOpacity={0.15}
                    stroke={node.color}
                    strokeWidth={selectedNode?.id === node.id ? 2.5 : 1.5}
                    filter={selectedNode?.id === node.id ? 'url(#glow)' : undefined}
                  />
                  <circle cx={node.x} cy={node.y} r={4} fill={node.color} />
                  <text
                    x={node.x}
                    y={node.y + node.size + 12}
                    fill="#ccc"
                    fontSize="10"
                    fontWeight="500"
                    textAnchor="middle"
                  >
                    {node.label}
                  </text>
                  <text
                    x={node.x}
                    y={node.y + node.size + 22}
                    fill="#666"
                    fontSize="8"
                    textAnchor="middle"
                  >
                    {node.type}
                  </text>
                </g>
              ))}
            </svg>
          </div>
        </div>

        {/* Side panel */}
        <div className="space-y-4">
          {/* Natural Language Query */}
          <div className="card p-4">
            <h3 className="text-xs font-bold uppercase tracking-wider text-gray-500 mb-3 flex items-center gap-2">
              <MessageSquare className="h-3.5 w-3.5 text-nexus-400" />
              Ask in Natural Language
            </h3>
            <div className="space-y-2">
              <div className="relative">
                <input
                  type="text"
                  className="input-field pr-10 text-xs"
                  placeholder="Ask anything about the knowledge graph..."
                  value={nlQuery}
                  onChange={(e) => setNlQuery(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleNlSearch()}
                />
                <button
                  onClick={handleNlSearch}
                  disabled={nlLoading}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-nexus-400 hover:text-nexus-300 disabled:opacity-50"
                >
                  {nlLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                </button>
              </div>

              {nlResult && (
                <div className="rounded-lg bg-nexus-500/10 p-3 text-xs text-gray-300 animate-fade-in">
                  {nlResult}
                </div>
              )}

              <div className="space-y-1.5 mt-3">
                <p className="text-[10px] text-gray-600">Try asking:</p>
                {NL_SUGGESTIONS.map((s, i) => (
                  <button
                    key={i}
                    onClick={() => { setNlQuery(s); }}
                    className="block w-full text-left text-[11px] text-gray-500 hover:text-nexus-400 transition-colors truncate"
                  >
                    "{s}"
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Selected Node Detail */}
          {selectedNode ? (
            <div className="card p-4 animate-slide-in">
              <div className="flex items-center gap-2 mb-3">
                <span className="h-3 w-3 rounded-full" style={{ backgroundColor: selectedNode.color }} />
                <h3 className="text-sm font-semibold text-white">{selectedNode.label}</h3>
              </div>
              <div className="space-y-2 text-xs">
                <div className="flex justify-between">
                  <span className="text-gray-500">Type</span>
                  <span className="text-gray-300">{selectedNode.type}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-500">Connections</span>
                  <span className="text-gray-300">
                    {mergedEdges.filter((e) => e.source === selectedNode.id || e.target === selectedNode.id).length}
                  </span>
                </div>
                <div>
                  <p className="text-gray-500 mb-1">Related:</p>
                  {mergedEdges
                    .filter((e) => e.source === selectedNode.id || e.target === selectedNode.id)
                    .slice(0, 5)
                    .map((e, i) => {
                      const otherId = e.source === selectedNode.id ? e.target : e.source;
                      const other = mergedNodes.find((n) => n.id === otherId);
                      return other ? (
                        <button
                          key={i}
                          onClick={() => setSelectedNode(other)}
                          className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-gray-400 hover:bg-white/[0.04] transition-colors"
                        >
                          <span className="h-2 w-2 rounded-full shrink-0" style={{ backgroundColor: other.color }} />
                          <span className="truncate">{other.label}</span>
                          <span className="text-[10px] text-gray-600 ml-auto">{e.label}</span>
                        </button>
                      ) : null;
                    })}
                </div>
              </div>
            </div>
          ) : (
            <div className="card p-4 text-center">
              <Network className="h-8 w-8 text-gray-600 mx-auto mb-2" />
              <p className="text-xs text-gray-500">Click a node to see details</p>
            </div>
          )}

          {/* Graph Stats */}
          <div className="card p-4">
            <h3 className="text-xs font-bold uppercase tracking-wider text-gray-500 mb-3">Graph Statistics</h3>
            <div className="space-y-2">
              {Array.from(nodeTypes.entries()).map(([type, count]) => (
                <div key={type} className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full" style={{ backgroundColor: NODE_COLORS[type] || '#666' }} />
                    <span className="text-xs text-gray-400">{type}</span>
                  </div>
                  <span className="text-xs font-semibold text-gray-300">{count}</span>
                </div>
              ))}
              <div className="border-t border-white/[0.06] pt-2 mt-2 flex justify-between">
                <span className="text-xs text-gray-500 font-medium">Total</span>
                <span className="text-xs font-bold text-white">{mergedNodes.length} nodes / {mergedEdges.length} edges</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
