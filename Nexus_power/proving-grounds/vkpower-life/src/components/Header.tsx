'use client';

import Link from 'next/link';
import { useApp } from '@/lib/store';

export default function Header() {
  const { state, logout } = useApp();

  return (
    <header className="bg-navy text-white border-b-[3px] border-gold">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">
          <Link href="/" className="flex items-center gap-3 group">
            <span className="w-8 h-8 rounded-lg bg-gradient-to-br from-gold to-gold-200 flex items-center justify-center text-navy font-black text-sm">
              V
            </span>
            <span className="font-extrabold tracking-wide text-sm sm:text-base">
              VKPower Life Insurance
            </span>
          </Link>

          {state.authenticated && (
            <nav className="flex items-center gap-1 sm:gap-4 text-sm" aria-label="Primary">
              <Link href="/portal/dashboard" className="text-gray-300 hover:text-white px-2 py-1 rounded transition-colors">
                Dashboard
              </Link>
              <Link href="/portal/beneficiaries" className="text-gray-300 hover:text-white px-2 py-1 rounded transition-colors hidden sm:block">
                Beneficiaries
              </Link>
              <Link href="/life-insurance/quote/start" className="text-gray-300 hover:text-white px-2 py-1 rounded transition-colors">
                Get a Quote
              </Link>
              <button
                onClick={() => { logout(); window.location.href = '/login/'; }}
                className="text-gray-400 hover:text-white px-2 py-1 rounded transition-colors ml-2"
              >
                Sign out
              </button>
            </nav>
          )}
        </div>
      </div>
    </header>
  );
}
