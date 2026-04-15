// ═══════════════════════════════════════════════════════════════
//  MODULE 12 — ADMIN & SYSTEM HEALTH
//  "Engine grid, resources, integrations, users, audit log"
// ═══════════════════════════════════════════════════════════════

import { useState } from 'react';
import api from '../services/api';
import { useApiData } from '../hooks/useApiData';
import { PageHeader } from '../components/PageHeader';
import { Tabs } from '../components/Tabs';
import { StatusBadge } from '../components/StatusBadge';
import { ProgressBar } from '../components/ProgressBar';
import { SearchInput } from '../components/SearchInput';
import { EmptyState } from '../components/EmptyState';
import {
  ServerCog,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Cpu,
  HardDrive,
  MemoryStick,
  Activity,
  Users,
  Clock,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  ExternalLink,
  Search,
  Wifi,
  WifiOff,
  Settings,
  GitBranch,
  MessageSquare,
} from 'lucide-react';
import clsx from 'clsx';

// ── Types ─────────────────────────────────────────────────

interface EngineInfo {
  name: string;
  codeName: string;
  status: 'online' | 'degraded' | 'offline';
  mode: 'real' | 'stub';
  uptime: string;
  version: string;
  cpu: number;
  memory: number;
  requests24h: number;
  errors24h: number;
}

interface IntegrationInfo {
  name: string;
  type: string;
  status: 'connected' | 'disconnected' | 'error';
  lastSync: string;
}

interface AuditEntry {
  id: string;
  timestamp: string;
  user: string;
  action: string;
  resource: string;
  details: string;
}

// ── Empty fallback (production) ─────────────────────────────

const EMPTY_ENGINES: EngineInfo[] = [];

const EMPTY_RESOURCES: Record<string, { label: string; used: number; total: number; unit: string }> = {};

const EMPTY_INTEGRATIONS: IntegrationInfo[] = [];

const EMPTY_AUDIT: AuditEntry[] = [];

const EMPTY_USERS: { name: string; email: string; role: string; lastActive: string; status: string }[] = [];

// ── Component ─────────────────────────────────────────────

export default function AdminPage() {
  const { data: engines, isLive } = useApiData(
    () => api.getAdminEngines(),
    EMPTY_ENGINES,
  );
  const { data: auditLog } = useApiData(
    () => api.getAuditLog('t-1'),
    EMPTY_AUDIT,
  );
  const { data: users } = useApiData(
    () => api.getAdminUsers('t-1'),
    EMPTY_USERS,
  );
  const { data: integrations } = useApiData(
    () => api.getAdminIntegrations('t-1'),
    EMPTY_INTEGRATIONS,
  );
  const [activeTab, setActiveTab] = useState<'engines' | 'integrations' | 'users' | 'audit'>('engines');

  const onlineEngines = engines.filter((e) => e.status === 'online').length;
  const degradedEngines = engines.filter((e) => e.status === 'degraded').length;
  const realEngines = engines.filter((e) => e.mode === 'real').length;

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <PageHeader
        zone="ZONE 4 · OPERATIONS"
        title="System Administration"
        subtitle="Engine health, system resources, integrations, and audit trail."
        isLive={isLive}
      />

      {/* System resource bars */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {Object.entries(EMPTY_RESOURCES).map(([key, res]) => {
          const pct = (res.used / res.total) * 100;
          const variant = pct > 85 ? 'red' as const : pct > 60 ? 'yellow' as const : 'green' as const;
          return (
            <div key={key} className="stat-card p-4">
              <div className="flex items-center justify-between mb-2">
                <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider">{res.label}</p>
                <span className={clsx('text-sm font-bold', pct > 85 ? 'text-red-400' : pct > 60 ? 'text-yellow-400' : 'text-green-400')}>
                  {pct.toFixed(0)}%
                </span>
              </div>
              <ProgressBar value={pct} variant={variant} size="md" />
              <p className="text-[10px] text-gray-600 mt-1">{res.used} / {res.total} {res.unit}</p>
            </div>
          );
        })}
      </div>

      {/* Tabs */}
      <Tabs
        tabs={[
          { id: 'engines', label: 'Engines' },
          { id: 'integrations', label: 'Integrations' },
          { id: 'users', label: 'Users' },
          { id: 'audit', label: 'Audit' },
        ]}
        activeTab={activeTab}
        onChange={(id) => setActiveTab(id as typeof activeTab)}
      />

      {/* Engines tab */}
      {activeTab === 'engines' && (
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
          {engines.length === 0 && (
            <div className="col-span-full">
              <EmptyState title="No Engines Registered" description="Engine status will appear once backend services are connected." />
            </div>
          )}
          {engines.map((eng) => {
            const statusColor =
              eng.status === 'online' ? 'bg-green-400' :
              eng.status === 'degraded' ? 'bg-yellow-400 animate-pulse' : 'bg-red-400';
            return (
              <div key={eng.name} className={clsx(
                'card p-4 transition-all',
                eng.status === 'degraded' && 'ring-yellow-500/20',
                eng.status === 'offline' && 'ring-red-500/20',
              )}>
                <div className="flex items-center gap-2 mb-3">
                  <span className={clsx('h-2 w-2 rounded-full', statusColor)} />
                  <span className="text-sm font-semibold text-white">{eng.name}</span>
                </div>
                <p className="text-[10px] text-gray-500 mb-2">{eng.codeName}</p>
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between text-[10px]">
                    <span className="text-gray-600">Mode</span>
                    <span className={eng.mode === 'real' ? 'badge-green' : 'badge-yellow'}>{eng.mode.toUpperCase()}</span>
                  </div>
                  <div className="flex items-center justify-between text-[10px]">
                    <span className="text-gray-600">CPU</span>
                    <span className={clsx('font-mono', eng.cpu > 80 ? 'text-red-400' : 'text-gray-300')}>{eng.cpu}%</span>
                  </div>
                  <div className="flex items-center justify-between text-[10px]">
                    <span className="text-gray-600">Memory</span>
                    <span className={clsx('font-mono', eng.memory > 80 ? 'text-red-400' : 'text-gray-300')}>{eng.memory}%</span>
                  </div>
                  <div className="flex items-center justify-between text-[10px]">
                    <span className="text-gray-600">24h Reqs</span>
                    <span className="text-gray-300 font-mono">{eng.requests24h.toLocaleString()}</span>
                  </div>
                  {eng.errors24h > 0 && (
                    <div className="flex items-center justify-between text-[10px]">
                      <span className="text-gray-600">Errors</span>
                      <span className="text-red-400 font-mono">{eng.errors24h}</span>
                    </div>
                  )}
                </div>
                <div className="mt-3 pt-2 border-t border-white/[0.04] flex items-center justify-between text-[9px] text-gray-600">
                  <span>v{eng.version}</span>
                  <span>Up {eng.uptime}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Integrations tab */}
      {activeTab === 'integrations' && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {integrations.length === 0 && (
            <div className="col-span-full">
              <EmptyState title="No Integrations Configured" description="Configure integrations with Jira, Confluence, and other tools." />
            </div>
          )}
          {integrations.map((intg) => {
            const statusIcon =
              intg.status === 'connected' ? <Wifi className="h-4 w-4 text-green-400" /> :
              intg.status === 'error' ? <AlertTriangle className="h-4 w-4 text-red-400" /> :
              <WifiOff className="h-4 w-4 text-gray-500" />;
            const statusBadge =
              intg.status === 'connected' ? 'badge-green' :
              intg.status === 'error' ? 'badge-red' : 'badge-gray';
            return (
              <div key={intg.name} className="card p-4 flex items-center gap-3">
                {statusIcon}
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-medium text-gray-200">{intg.name}</h3>
                    <span className={statusBadge}>{intg.status.toUpperCase()}</span>
                  </div>
                  <p className="text-[10px] text-gray-500">{intg.type} • Last sync: {intg.lastSync}</p>
                </div>
                <button className="btn-ghost text-[10px] py-1 px-2">
                  <Settings className="h-3 w-3" />
                </button>
              </div>
            );
          })}
        </div>
      )}

      {/* Users tab */}
      {activeTab === 'users' && (
        <div className="card overflow-hidden">
          {users.length === 0 ? (
            <EmptyState title="No Users Found" description="Users will appear here once accounts are created." />
          ) : (
          <table className="w-full text-left">
            <thead>
              <tr className="text-[10px] text-gray-500 uppercase tracking-wider border-b border-white/[0.06]">
                <th className="p-3">User</th>
                <th className="p-3">Role</th>
                <th className="p-3">Status</th>
                <th className="p-3">Last Active</th>
                <th className="p-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.email} className="border-b border-white/[0.03] last:border-0 hover:bg-white/[0.02]">
                  <td className="p-3">
                    <div className="flex items-center gap-2">
                      <div className="h-7 w-7 rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 flex items-center justify-center text-[10px] text-white font-bold">
                        {u.name.charAt(0)}
                      </div>
                      <div>
                        <p className="text-sm text-gray-200">{u.name}</p>
                        <p className="text-[10px] text-gray-600">{u.email}</p>
                      </div>
                    </div>
                  </td>
                  <td className="p-3 text-xs text-gray-400">{u.role}</td>
                  <td className="p-3">
                    <span className={u.status === 'active' ? 'badge-green' : 'badge-gray'}>
                      {u.status.toUpperCase()}
                    </span>
                  </td>
                  <td className="p-3 text-xs text-gray-500">{u.lastActive}</td>
                  <td className="p-3 text-right">
                    <button className="btn-ghost text-[10px] py-1 px-2">Edit</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          )}
        </div>
      )}

      {/* Audit tab */}
      {activeTab === 'audit' && (
        <div className="space-y-2">
          {auditLog.length === 0 && (
            <EmptyState title="No Audit Entries" description="System audit trail will populate as actions are performed." />
          )}
          {auditLog.map((entry) => (
            <div key={entry.id} className="card p-3 flex items-start gap-3">
              <Clock className="h-4 w-4 text-gray-600 shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[10px] text-gray-600 font-mono">
                    {new Date(entry.timestamp).toLocaleString()}
                  </span>
                  <span className="badge-nexus">{entry.action}</span>
                  <span className="text-xs font-medium text-gray-300">{entry.resource}</span>
                </div>
                <p className="text-xs text-gray-500 mt-0.5">{entry.details}</p>
                <p className="text-[10px] text-gray-600 mt-0.5">by {entry.user}</p>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
