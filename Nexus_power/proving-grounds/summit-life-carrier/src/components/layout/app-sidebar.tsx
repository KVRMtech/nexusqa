'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { cn } from '@/lib/utils';
import { Separator } from '@/components/ui/separator';
import { LayoutDashboard, FileSearch, Shield, ClipboardList, Calculator, Settings, ChevronLeft, ChevronRight, FilePlus2, UserPlus, CreditCard } from 'lucide-react';
import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';

const navGroups = [
  {
    label: 'Overview',
    items: [
      { href: '/dashboard/overview', label: 'Dashboard', icon: LayoutDashboard },
    ],
  },
  {
    label: 'Underwriting',
    items: [
      { href: '/underwriting/new-business', label: 'New Business Queue', icon: FileSearch },
      { href: '/underwriting/new-business/new-application', label: 'Submit Application', icon: FilePlus2 },
    ],
  },
  {
    label: 'Customer Management',
    items: [
      { href: '/customers/profile', label: 'New Customer', icon: UserPlus },
    ],
  },
  {
    label: 'Payments',
    items: [
      { href: '/payments/process', label: 'Process Payment', icon: CreditCard },
    ],
  },
  {
    label: 'Policy Administration',
    items: [
      { href: '/policy-admin/in-force', label: 'In-Force Policies', icon: Shield },
    ],
  },
  {
    label: 'Claims Operations',
    items: [
      { href: '/claims/reported', label: 'Reported Claims', icon: ClipboardList },
    ],
  },
  {
    label: 'Actuarial',
    items: [
      { href: '/actuarial/product-pricing', label: 'Product Pricing', icon: Calculator },
    ],
  },
];

export function AppSidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);

  return (
    <motion.aside
      initial={false}
      animate={{ width: collapsed ? 64 : 260 }}
      transition={{ duration: 0.2, ease: 'easeInOut' }}
      className="relative flex h-screen flex-col border-r bg-navy text-white"
    >
      <div className="flex h-16 items-center px-4 gap-3">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-gold text-navy-dark font-bold text-sm shrink-0">SL</div>
        <AnimatePresence>
          {!collapsed && (
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="overflow-hidden whitespace-nowrap">
              <div className="text-sm font-semibold text-gold">Summit Life</div>
              <div className="text-[10px] text-white/50 tracking-wider uppercase">Carrier Admin</div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <Separator className="bg-white/10" />

      <nav className="flex-1 overflow-y-auto py-4 space-y-6">
        {navGroups.map(group => (
          <div key={group.label}>
            {!collapsed && <div className="px-4 mb-2 text-[10px] font-semibold uppercase tracking-wider text-white/40">{group.label}</div>}
            {group.items.map(item => {
              const isActive = pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={cn(
                    'flex items-center gap-3 px-4 py-2.5 mx-2 rounded-md text-sm transition-colors',
                    isActive ? 'bg-gold/20 text-gold font-medium' : 'text-white/70 hover:bg-white/5 hover:text-white'
                  )}
                >
                  <item.icon className="h-4 w-4 shrink-0" />
                  <AnimatePresence>
                    {!collapsed && <motion.span initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="truncate">{item.label}</motion.span>}
                  </AnimatePresence>
                </Link>
              );
            })}
          </div>
        ))}
      </nav>

      <Separator className="bg-white/10" />

      <div className="p-2">
        {!collapsed && (
          <Link href="/settings/preferences" className="flex items-center gap-3 px-4 py-2.5 mx-0 rounded-md text-sm text-white/70 hover:bg-white/5 hover:text-white transition-colors">
            <Settings className="h-4 w-4 shrink-0" />
            <span>Settings</span>
          </Link>
        )}
      </div>

      <button
        onClick={() => setCollapsed(c => !c)}
        className="absolute -right-3 top-20 z-10 flex h-6 w-6 items-center justify-center rounded-full border bg-background text-foreground shadow-sm hover:bg-muted"
      >
        {collapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronLeft className="h-3 w-3" />}
      </button>
    </motion.aside>
  );
}
