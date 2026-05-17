// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Tabs Component
// ═══════════════════════════════════════════════════════════════
import type { ReactNode } from 'react';
import clsx from 'clsx';

export interface Tab {
  id: string;
  label: string;
  icon?: ReactNode;
  count?: number;
  disabled?: boolean;
}

interface TabsProps {
  tabs: Tab[];
  activeTab: string;
  onChange: (id: string) => void;
  /** Visual style */
  variant?: 'underline' | 'pills';
  className?: string;
}

export function Tabs({ tabs, activeTab, onChange, variant = 'underline', className }: TabsProps) {
  if (variant === 'pills') {
    return (
      <div className={clsx('flex flex-wrap gap-1.5', className)} role="tablist">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            onClick={() => !tab.disabled && onChange(tab.id)}
            disabled={tab.disabled}
            className={clsx(
              'inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium transition-all duration-150',
              activeTab === tab.id
                ? 'bg-nexus-500/15 text-nexus-400 ring-1 ring-nexus-500/25'
                : 'text-slate-500 hover:bg-white/[0.04] hover:text-slate-700',
              tab.disabled && 'opacity-40 cursor-not-allowed',
            )}
          >
            {tab.icon}
            {tab.label}
            {typeof tab.count === 'number' && (
              <span className="badge-gray text-[10px] px-1.5 py-0">{tab.count}</span>
            )}
          </button>
        ))}
      </div>
    );
  }

  // Underline variant
  return (
    <div className={clsx('border-b border-gray-200', className)} role="tablist">
      <div className="flex gap-0 -mb-px">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            onClick={() => !tab.disabled && onChange(tab.id)}
            disabled={tab.disabled}
            className={clsx(
              'inline-flex items-center gap-2 border-b-2 px-4 py-2.5 text-sm font-medium transition-all duration-150',
              activeTab === tab.id
                ? 'border-nexus-500 text-nexus-400'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-gray-200',
              tab.disabled && 'opacity-40 cursor-not-allowed',
            )}
          >
            {tab.icon}
            {tab.label}
            {typeof tab.count === 'number' && (
              <span className="badge-gray text-[10px] px-1.5 py-0">{tab.count}</span>
            )}
          </button>
        ))}
      </div>
    </div>
  );
}
