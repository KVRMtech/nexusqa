/**
 * Phase 16 — Tenant administration page.
 *
 * Admin-only. Lists every tenant + their lifecycle state. Operators
 * can provision, suspend, resume, offboard, or trigger a GDPR export
 * from this single screen. Each destructive action requires a typed
 * confirmation matching the tenant id — slows down "oops" mistakes.
 *
 * Wires to platform-api endpoints at /v1/admin/tenants/* (see
 * platform/api/app/routers/tenants.py). The endpoints reject any
 * non-admin JWT with 403, so the UI doesn't try to hide the controls
 * for non-admin users — auth is the source of truth.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import api from '../services/api';

// ─── Types (mirror platform/api/app/routers/tenants.py wire schema) ──

type TenantState =
  | 'pending'
  | 'active'
  | 'suspended'
  | 'offboarding'
  | 'deleted';

interface Tenant {
  tenant_id: string;
  display_name: string;
  state: TenantState;
  tier: 'pilot' | 'enterprise' | 'internal';
  provisioned_at?: string | null;
  suspended_at?: string | null;
  offboarding_started_at?: string | null;
  retention_until?: string | null;
  deleted_at?: string | null;
}

interface TenantListResponse {
  tenants: Tenant[];
  total: number;
}

// ─── Lifecycle transitions allowed from each state ─────────────

const TRANSITIONS: Record<TenantState, Array<{ action: string; label: string; danger?: boolean }>> = {
  pending:     [{ action: 'provision', label: 'Provision' }],
  active:      [
    { action: 'suspend', label: 'Suspend' },
    { action: 'offboard', label: 'Offboard', danger: true },
  ],
  suspended:   [
    { action: 'resume', label: 'Resume' },
    { action: 'offboard', label: 'Offboard', danger: true },
  ],
  offboarding: [{ action: 'finalize-deletion', label: 'Finalize deletion', danger: true }],
  deleted:     [],
};

const STATE_COLOR: Record<TenantState, string> = {
  pending:     '#888',
  active:      '#2a9d2a',
  suspended:   '#d18b1a',
  offboarding: '#cc3a3a',
  deleted:     '#444',
};

// ─── Page component ─────────────────────────────────────────────

export default function AdminTenantsPage(): JSX.Element {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [filterState, setFilterState] = useState<TenantState | 'all'>('all');
  const [pendingAction, setPendingAction] = useState<{
    tenant: Tenant;
    action: string;
    reason?: string;
    typedConfirm?: string;
  } | null>(null);

  const fetchTenants = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = (await api.listTenants(
        filterState === 'all' ? undefined : filterState,
      )) as TenantListResponse;
      setTenants(data.tenants);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'failed to load');
    } finally {
      setLoading(false);
    }
  }, [filterState]);

  useEffect(() => {
    fetchTenants();
  }, [fetchTenants]);

  const visibleTenants = useMemo(
    () => tenants.sort((a, b) => a.tenant_id.localeCompare(b.tenant_id)),
    [tenants],
  );

  const applyTransition = async () => {
    if (!pendingAction) return;
    const { tenant, action, reason } = pendingAction;
    try {
      const body: { reason?: string; retention_days?: number } = {};
      if (action === 'suspend') body.reason = reason || 'no reason supplied';
      if (action === 'offboard') {
        body.reason = reason || 'no reason supplied';
        body.retention_days = 30;
      }
      await api.transitionTenant(
        tenant.tenant_id,
        action as 'provision' | 'suspend' | 'resume' | 'offboard' | 'finalize-deletion',
        body,
      );
      setPendingAction(null);
      await fetchTenants();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'transition failed');
    }
  };

  const triggerExport = async (tenant: Tenant) => {
    try {
      const data = (await api.exportTenantData(tenant.tenant_id)) as {
        job_id: string;
        status: string;
        note?: string;
      };
      alert(
        `Export job queued.\n\nJob id: ${data.job_id}\nStatus: ${data.status}\n\n` +
        (data.note ? `Note: ${data.note}` : ''),
      );
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'export failed');
    }
  };

  // Render

  return (
    <div style={{ padding: 24, fontFamily: 'system-ui, sans-serif', maxWidth: 1200 }}>
      <h1>Tenant Administration</h1>
      <p style={{ color: '#555' }}>
        Admin-only. All actions emit an audit_log entry attributed to your
        JWT subject. Destructive actions require typed confirmation.
      </p>

      <div style={{ marginBottom: 16 }}>
        <label>
          Filter by state:{' '}
          <select
            value={filterState}
            onChange={(e) => setFilterState(e.target.value as TenantState | 'all')}
          >
            <option value="all">all</option>
            <option value="pending">pending</option>
            <option value="active">active</option>
            <option value="suspended">suspended</option>
            <option value="offboarding">offboarding</option>
            <option value="deleted">deleted</option>
          </select>
        </label>{' '}
        <button onClick={fetchTenants} disabled={loading}>
          {loading ? 'refreshing…' : 'refresh'}
        </button>
      </div>

      {error && (
        <div style={{ background: '#fee', padding: 10, marginBottom: 16, color: '#c00' }}>
          {error}
        </div>
      )}

      <table
        style={{
          width: '100%',
          borderCollapse: 'collapse',
          fontSize: 13,
        }}
      >
        <thead>
          <tr style={{ background: '#f5f5f5' }}>
            <th style={cell}>tenant_id</th>
            <th style={cell}>display_name</th>
            <th style={cell}>state</th>
            <th style={cell}>tier</th>
            <th style={cell}>provisioned</th>
            <th style={cell}>retention until</th>
            <th style={cell}>actions</th>
          </tr>
        </thead>
        <tbody>
          {visibleTenants.length === 0 && (
            <tr><td colSpan={7} style={{ ...cell, textAlign: 'center', color: '#888' }}>
              no tenants
            </td></tr>
          )}
          {visibleTenants.map((t) => (
            <tr key={t.tenant_id}>
              <td style={cell}><code>{t.tenant_id}</code></td>
              <td style={cell}>{t.display_name}</td>
              <td style={{
                ...cell,
                color: STATE_COLOR[t.state],
                fontWeight: 600,
              }}>
                {t.state}
              </td>
              <td style={cell}>{t.tier}</td>
              <td style={cell}>{t.provisioned_at ? new Date(t.provisioned_at).toLocaleDateString() : '—'}</td>
              <td style={cell}>{t.retention_until ? new Date(t.retention_until).toLocaleString() : '—'}</td>
              <td style={cell}>
                {TRANSITIONS[t.state].map((tr) => (
                  <button
                    key={tr.action}
                    onClick={() => setPendingAction({ tenant: t, action: tr.action })}
                    style={{
                      marginRight: 6,
                      padding: '3px 8px',
                      background: tr.danger ? '#fcc' : '#e9e9e9',
                      border: '1px solid #ccc',
                      cursor: 'pointer',
                    }}
                  >
                    {tr.label}
                  </button>
                ))}
                <button
                  onClick={() => triggerExport(t)}
                  style={{
                    marginRight: 6,
                    padding: '3px 8px',
                    background: '#dde',
                    border: '1px solid #99c',
                    cursor: 'pointer',
                  }}
                  title="GDPR data export"
                >
                  Export
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {pendingAction && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 999,
        }}>
          <div style={{
            background: '#fff', padding: 24, minWidth: 480, maxWidth: 600,
            borderRadius: 6, boxShadow: '0 8px 24px rgba(0,0,0,0.2)',
          }}>
            <h3>
              {pendingAction.action} tenant <code>{pendingAction.tenant.tenant_id}</code>
            </h3>
            <p>
              Current state: <strong>{pendingAction.tenant.state}</strong>.
              {pendingAction.action === 'offboard' &&
                ' Default retention: 30 days. Personal Data deleted after retention expires.'}
              {pendingAction.action === 'finalize-deletion' &&
                ' This will HARD-DELETE all tenant Personal Data. The audit-log shell remains.'}
            </p>

            {(pendingAction.action === 'suspend' || pendingAction.action === 'offboard') && (
              <textarea
                placeholder="Reason (required, free text — appears in audit log)"
                value={pendingAction.reason || ''}
                onChange={(e) => setPendingAction({ ...pendingAction, reason: e.target.value })}
                style={{ width: '100%', minHeight: 60, marginBottom: 12 }}
              />
            )}

            {(pendingAction.action === 'offboard' || pendingAction.action === 'finalize-deletion') && (
              <>
                <p style={{ color: '#c00', fontSize: 13 }}>
                  Type the tenant_id to confirm:
                  <br />
                  <code>{pendingAction.tenant.tenant_id}</code>
                </p>
                <input
                  value={pendingAction.typedConfirm || ''}
                  onChange={(e) => setPendingAction({ ...pendingAction, typedConfirm: e.target.value })}
                  style={{ width: '100%', marginBottom: 12, fontFamily: 'monospace' }}
                  autoFocus
                />
              </>
            )}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button onClick={() => setPendingAction(null)}>Cancel</button>
              <button
                onClick={applyTransition}
                disabled={
                  (pendingAction.action === 'offboard' || pendingAction.action === 'finalize-deletion') &&
                  pendingAction.typedConfirm !== pendingAction.tenant.tenant_id
                }
                style={{
                  padding: '6px 16px',
                  background: TRANSITIONS[pendingAction.tenant.state]
                    .find((t) => t.action === pendingAction.action)?.danger
                    ? '#c00'
                    : '#2a9d2a',
                  color: '#fff',
                  border: 'none',
                  borderRadius: 3,
                  cursor: 'pointer',
                }}
              >
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const cell: React.CSSProperties = {
  padding: '8px 12px',
  borderBottom: '1px solid #eee',
  textAlign: 'left',
  verticalAlign: 'middle',
};
