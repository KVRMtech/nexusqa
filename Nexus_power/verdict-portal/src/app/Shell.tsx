/**
 * Shell — the app frame: a fixed left rail (brand + nav + status) and a slim top
 * bar (tenant switcher + principal). The routed screens render into <Outlet/>.
 */
import { NavLink, Outlet } from 'react-router-dom';
import { Gauge as GaugeIcon, LayoutGrid, LogOut, PlusCircle } from 'lucide-react';

import { api } from '../lib/api';
import { useAuth } from '../lib/auth';
import { QEC_AUDIENCE } from '../lib/config';
import { cn } from '../lib/format';
import { useAsync } from '../lib/useAsync';
import { Seal, StatusDot } from '../components';

const NAV = [
  { to: '/', label: 'Command Center', icon: LayoutGrid, end: true },
  { to: '/onboard', label: 'Onboard app', icon: PlusCircle, end: false },
];

function BrandMark() {
  return (
    <div className="flex items-center gap-2.5 px-4 pt-5 pb-4">
      <Seal size={30} tone="certified" title="VKPower Verdict" />
      <div className="leading-none">
        <div className="text-sm font-semibold text-ink tracking-tight">
          VKPower <span className="text-teal">Verdict</span>
        </div>
        <div className="text-[10px] text-ink-low font-mono mt-1">the verdict on every release</div>
      </div>
    </div>
  );
}

function HealthChip() {
  const { data, isError } = useAsync((signal) => api.getHealth({ signal }), []);
  const healthy = data?.status === 'healthy';
  const tone = isError ? 'crit' : healthy ? 'good' : data ? 'warn' : 'neutral';
  const label = isError ? 'unreachable' : (data?.status ?? 'checking…');
  return (
    <div className="flex items-center gap-2 text-2xs text-ink-low">
      <StatusDot tone={tone} pulse={healthy} label={`API ${label}`} />
      <span className="tabular">API · {label}</span>
    </div>
  );
}

function TenantSwitcher() {
  const { session, switchTenant } = useAuth();
  if (!session) return null;
  const tenants = session.availableTenants.length ? session.availableTenants : [session.tenantId];
  return (
    <label className="flex items-center gap-2 text-2xs text-ink-low">
      <span className="uppercase tracking-wide">Tenant</span>
      <select
        value={session.tenantId}
        onChange={(e) => switchTenant(e.target.value)}
        aria-label="Active tenant"
        className="bg-panel-2 text-ink text-xs rounded-md ring-1 ring-line-strong px-2 py-1 font-mono focus-visible:ring-teal/60"
      >
        {tenants.map((t) => (
          <option key={t} value={t}>
            {t}
          </option>
        ))}
      </select>
    </label>
  );
}

export function Shell() {
  const { session, logout } = useAuth();

  return (
    <div className="min-h-full flex bg-bg text-ink">
      {/* ── Left rail ── */}
      <aside className="w-60 shrink-0 border-r border-line bg-bg-elev flex flex-col fixed inset-y-0 left-0 z-20">
        <BrandMark />
        <nav className="flex-1 px-2.5 py-2 space-y-1" aria-label="Primary">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors',
                  isActive
                    ? 'bg-teal/[0.10] text-ink ring-1 ring-teal/25'
                    : 'text-ink-mid hover:text-ink hover:bg-ink/[0.04]',
                )
              }
            >
              <Icon size={16} aria-hidden />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="px-4 py-3 border-t border-line space-y-2">
          <HealthChip />
          <div className="flex items-center gap-2 text-2xs text-ink-faint">
            <GaugeIcon size={12} aria-hidden />
            <span className="font-mono">aud · {QEC_AUDIENCE}</span>
          </div>
          {api.mock && (
            <div className="text-2xs font-semibold text-gold bg-gold/10 ring-1 ring-gold/25 rounded px-2 py-1 text-center">
              MOCK DATA
            </div>
          )}
        </div>
      </aside>

      {/* ── Main column ── */}
      <div className="flex-1 min-w-0 ml-60 flex flex-col">
        <header className="h-14 shrink-0 border-b border-line bg-bg-elev/80 backdrop-blur flex items-center justify-between px-6 sticky top-0 z-10">
          <TenantSwitcher />
          <div className="flex items-center gap-4">
            {session && (
              <div className="text-right leading-tight">
                <div className="text-xs text-ink truncate max-w-[16rem]">{session.email || session.sub}</div>
                <div className="text-2xs text-ink-low uppercase tracking-wide">{session.role}</div>
              </div>
            )}
            <button
              type="button"
              onClick={logout}
              className="inline-flex items-center gap-1.5 text-xs text-ink-mid hover:text-ink rounded-md px-2 py-1.5 ring-1 ring-line hover:ring-line-strong transition-colors"
            >
              <LogOut size={14} aria-hidden />
              Sign out
            </button>
          </div>
        </header>

        <main className="flex-1 min-w-0 p-6 animate-fade-in">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

export default Shell;
