/**
 * Shell — the app frame: a fixed NAVY left rail (brand + nav + status) and a slim
 * NAVY top bar (tenant switcher + principal), framing the light content area —
 * the VKPower (Video) USAA signature. The routed screens render into <Outlet/>.
 */
import { NavLink, Outlet } from 'react-router-dom';
import { Gauge as GaugeIcon, LayoutGrid, LogOut, PlusCircle } from 'lucide-react';

import { api } from '../lib/api';
import { useAuth } from '../lib/auth';
import { QEC_AUDIENCE } from '../lib/config';
import { cn } from '../lib/format';
import { useAsync } from '../lib/useAsync';
import { StatusDot, VkMark } from '../components';

const NAV = [
  { to: '/', label: 'Command Center', icon: LayoutGrid, end: true },
  { to: '/onboard', label: 'Onboard app', icon: PlusCircle, end: false },
];

function BrandMark() {
  return (
    <div className="flex items-center gap-2.5 px-4 pt-5 pb-4">
      <VkMark size={30} title="VKPower Verdict" />
      <div className="leading-none">
        <div className="text-sm font-semibold text-white tracking-tight">
          VKPower <span className="text-gold">Verdict</span>
        </div>
        <div className="text-[10px] text-white/45 font-mono mt-1">the verdict on every release</div>
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
    <div className="flex items-center gap-2 text-2xs text-white/55">
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
    <label className="flex items-center gap-2 text-2xs text-white/60">
      <span className="uppercase tracking-wide">Tenant</span>
      <select
        value={session.tenantId}
        onChange={(e) => switchTenant(e.target.value)}
        aria-label="Active tenant"
        className="bg-white/[0.08] text-white text-xs rounded-md ring-1 ring-white/15 px-2 py-1 font-mono focus-visible:ring-gold/60 [&>option]:text-ink"
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
    <div className="min-h-full flex text-ink">
      {/* Transparent root so the fixed body canvas (the navy/gold radial glow)
          shows through the content column; the navy aside paints its own area. */}
      {/* ── Left rail (navy) ── */}
      <aside className="w-60 shrink-0 border-r border-white/[0.06] bg-nexus-900 flex flex-col fixed inset-y-0 left-0 z-20">
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
                    ? 'bg-white/[0.10] text-white ring-1 ring-white/15'
                    : 'text-white/65 hover:text-white hover:bg-white/[0.06]',
                )
              }
            >
              <Icon size={16} aria-hidden />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="px-4 py-3 border-t border-white/[0.06] space-y-2">
          <HealthChip />
          <div className="flex items-center gap-2 text-2xs text-white/35">
            <GaugeIcon size={12} aria-hidden />
            <span className="font-mono">aud · {QEC_AUDIENCE}</span>
          </div>
          {api.mock && (
            <div className="text-2xs font-semibold text-gold bg-gold/15 ring-1 ring-gold/30 rounded px-2 py-1 text-center">
              MOCK DATA
            </div>
          )}
        </div>
      </aside>

      {/* ── Main column ── */}
      <div className="flex-1 min-w-0 ml-60 flex flex-col">
        <header className="h-14 shrink-0 border-b border-white/[0.06] bg-nexus-900/95 backdrop-blur flex items-center justify-between px-6 sticky top-0 z-10">
          <TenantSwitcher />
          <div className="flex items-center gap-4">
            {session && (
              <div className="text-right leading-tight">
                <div className="text-xs text-white truncate max-w-[16rem]">{session.email || session.sub}</div>
                <div className="text-2xs text-white/55 uppercase tracking-wide">{session.role}</div>
              </div>
            )}
            <button
              type="button"
              onClick={logout}
              className="inline-flex items-center gap-1.5 text-xs text-white/70 hover:text-white rounded-md px-2 py-1.5 ring-1 ring-white/15 hover:ring-white/30 transition-colors"
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
