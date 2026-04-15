import { useMemo, useState } from 'react';
import { Outlet, NavLink, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { useEngineStatus } from '../hooks/useEngineStatus';
import { NotificationCenter } from '../components/NotificationCenter';
import { isCanonicalOnly } from '../productMode';
import {
  LayoutDashboard,
  Radio,
  PlayCircle,
  UserCircle,
  Network,
  AlertTriangle,
  Brain,
  Layers,
  Shield,
  CheckCircle2,
  Link2,
  Zap,
  Hammer,
  Scale,
  BarChart3,
  ServerCog,
  LogOut,
  Menu,
  X,
  ChevronDown,
  Hexagon,
  Target,
  Users,
} from 'lucide-react';
import clsx from 'clsx';

interface NavItem {
  name: string;
  href: string;
  icon: React.ElementType;
}

interface NavGroup {
  zone: string;
  label: string;
  items: NavItem[];
}

const allNavGroups: NavGroup[] = [
  {
    zone: '0',
    label: '',
    items: [{ name: 'Executive Insights', href: '/', icon: BarChart3 }],
  },
  {
    zone: '1',
    label: 'KNOWLEDGE CAPTURE',
    items: [
      { name: 'Session Command', href: '/sessions', icon: Radio },
      { name: 'SME Profiles', href: '/sme-profiles', icon: UserCircle },
    ],
  },
  {
    zone: '2',
    label: 'INTELLIGENCE HUB',
    items: [
      { name: 'Knowledge Graph', href: '/knowledge-graph', icon: Network },
      { name: 'Contradiction Radar', href: '/contradictions', icon: AlertTriangle },
      { name: 'AI Confidence', href: '/ai-confidence', icon: Shield },
    ],
  },
  {
    zone: '3',
    label: 'TESTING POWERHOUSE',
    items: [
      { name: 'Traceability Matrix', href: '/traceability', icon: Link2 },
      { name: 'Test Center', href: '/test-center', icon: Zap },
      { name: 'Data Forge', href: '/data-forge', icon: Hammer },
    ],
  },
  {
    zone: '4',
    label: 'OPERATIONS',
    items: [
      { name: 'Compliance Cockpit', href: '/compliance', icon: Scale },
      { name: 'Brain & Tiers', href: '/brain', icon: Brain },
      { name: 'System Admin', href: '/admin', icon: ServerCog },
    ],
  },
  {
    zone: '5',
    label: 'QI ENGINEER PORTAL',
    items: [
      { name: 'Mission Control', href: '/qi/missions', icon: Target },
      { name: 'Persona Gallery', href: '/qi/personas', icon: Users },
    ],
  },
];

/** Canonical mode: single nav entry for Sessions */
const canonicalNavGroups: NavGroup[] = [
  {
    zone: 'canonical',
    label: 'CANONICAL PROCESSING',
    items: [
      { name: 'Sessions', href: '/sessions', icon: Radio },
    ],
  },
];

export default function AppLayout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const { healthyCount, totalCount, allHealthy } = useEngineStatus();
  const navGroups = useMemo(() => isCanonicalOnly ? canonicalNavGroups : allNavGroups, []);

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  const SidebarContent = () => (
    <div className="flex h-full flex-col">
      {/* Logo */}
      <div className="flex h-16 items-center gap-3 px-5 border-b border-white/[0.06]">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-nexus-500 to-purple-600 shadow-lg shadow-nexus-500/25">
          <Hexagon className="h-5 w-5 text-white" />
        </div>
        <div>
          <h1 className="text-[15px] font-bold text-white tracking-tight">NEXUS</h1>
          <p className="text-[9px] font-semibold text-nexus-400 uppercase tracking-[0.2em]">{isCanonicalOnly ? 'Canonical Processing' : 'AI Engine Factory'}</p>
        </div>
      </div>

      {/* Navigation grouped by Zone */}
      <nav className="flex-1 overflow-y-auto px-3 py-3 space-y-1">
        {navGroups.map((group) => (
          <div key={group.zone}>
            {group.label && (
              <p className="mt-4 mb-1.5 px-3 text-[10px] font-bold text-gray-500 uppercase tracking-widest">
                {group.label}
              </p>
            )}
            {group.items.map((item) => (
              <NavLink
                key={item.href}
                to={item.href}
                end={item.href === '/'}
                onClick={() => setSidebarOpen(false)}
                className={({ isActive }) =>
                  clsx(
                    'group flex items-center gap-3 rounded-lg px-3 py-2 text-[13px] font-medium transition-all duration-150',
                    isActive
                      ? 'bg-nexus-500/15 text-nexus-400 shadow-sm shadow-nexus-500/5'
                      : 'text-gray-400 hover:bg-white/[0.04] hover:text-gray-200',
                  )
                }
              >
                <item.icon className="h-[18px] w-[18px] shrink-0" />
                {item.name}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      {/* Live indicator — real engine health */}
      <div className="px-4 py-2">
        <div className={clsx(
          'flex items-center gap-2 rounded-lg px-3 py-2',
          allHealthy ? 'bg-green-500/10' : 'bg-yellow-500/10',
        )}>
          <span className="relative flex h-2 w-2">
            <span className={clsx(
              'absolute inline-flex h-full w-full animate-ping rounded-full opacity-75',
              allHealthy ? 'bg-green-400' : 'bg-yellow-400',
            )} />
            <span className={clsx(
              'relative inline-flex h-2 w-2 rounded-full',
              allHealthy ? 'bg-green-500' : 'bg-yellow-500',
            )} />
          </span>
          <span className={clsx(
            'text-[11px] font-medium',
            allHealthy ? 'text-green-400' : 'text-yellow-400',
          )}>
            {allHealthy ? 'All Engines Online' : `${healthyCount}/${totalCount} Engines Online`}
          </span>
        </div>
      </div>

      {/* User section */}
      <div className="border-t border-white/[0.06] p-3">
        <div className="relative">
          <button
            onClick={() => setUserMenuOpen(!userMenuOpen)}
            className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm hover:bg-white/[0.04] transition-colors"
          >
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-nexus-500 to-purple-600 text-xs font-bold text-white">
              {user?.name?.charAt(0).toUpperCase() || 'U'}
            </div>
            <div className="flex-1 text-left min-w-0">
              <p className="text-sm font-medium text-gray-200 truncate">{user?.name || 'User'}</p>
              <p className="text-[11px] text-gray-500 truncate">{user?.role || 'Admin'}</p>
            </div>
            <ChevronDown className="h-4 w-4 text-gray-500" />
          </button>

          {userMenuOpen && (
            <div className="absolute bottom-full left-0 mb-2 w-full rounded-lg bg-gray-800/95 backdrop-blur shadow-xl ring-1 ring-white/10 animate-fade-in">
              <div className="p-1">
                <button
                  onClick={handleLogout}
                  className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm text-red-400 hover:bg-white/[0.06]"
                >
                  <LogOut className="h-4 w-4" />
                  Sign out
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );

  return (
    <div className="flex h-full bg-gray-950">
      {/* Mobile sidebar overlay */}
      {sidebarOpen && (
        <div className="fixed inset-0 z-40 lg:hidden" onClick={() => setSidebarOpen(false)}>
          <div className="fixed inset-0 bg-black/70 backdrop-blur-sm" />
        </div>
      )}

      {/* Sidebar — mobile */}
      <div
        className={clsx(
          'fixed inset-y-0 left-0 z-50 w-64 bg-gray-900/95 backdrop-blur transition-transform duration-300 lg:hidden',
          sidebarOpen ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        <button
          onClick={() => setSidebarOpen(false)}
          className="absolute right-3 top-4 rounded-md p-1 text-gray-400 hover:text-white"
        >
          <X className="h-5 w-5" />
        </button>
        <SidebarContent />
      </div>

      {/* Sidebar — desktop */}
      <div className="hidden lg:fixed lg:inset-y-0 lg:flex lg:w-64 lg:flex-col">
        <div className="flex flex-1 flex-col bg-gray-900 border-r border-white/[0.06]">
          <SidebarContent />
        </div>
      </div>

      {/* Main content */}
      <div className="flex flex-1 flex-col lg:pl-64">
        {/* Top bar */}
        <div className="sticky top-0 z-30 flex h-14 items-center justify-between gap-4 border-b border-white/[0.06] bg-gray-950/90 px-4 backdrop-blur">
          {/* Left: mobile hamburger + logo */}
          <div className="flex items-center gap-4">
            <button onClick={() => setSidebarOpen(true)} className="text-gray-400 hover:text-white lg:hidden">
              <Menu className="h-6 w-6" />
            </button>
            <div className="flex items-center gap-2 lg:hidden">
              <Hexagon className="h-5 w-5 text-nexus-500" />
              <h1 className="text-base font-bold text-white">NEXUS</h1>
            </div>
          </div>
          {/* Right: notification center (always visible) */}
          <div className="flex items-center gap-2">
            <NotificationCenter />
          </div>
        </div>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto bg-gray-950">
          <div className="mx-auto max-w-[1440px] px-4 py-6 sm:px-6 lg:px-8">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
