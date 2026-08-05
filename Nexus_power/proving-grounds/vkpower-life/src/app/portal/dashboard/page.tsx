'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import Header from '@/components/Header';
import { useApp } from '@/lib/store';

function money(n: number) {
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function DashboardPage() {
  const router = useRouter();
  const { state } = useApp();

  useEffect(() => {
    if (!state.authenticated) router.replace('/login/');
  }, [state.authenticated, router]);

  if (!state.authenticated || !state.profile) return null;

  const p = state.profile;

  return (
    <>
      <Header />
      <main className="flex-1">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          <div className="flex items-center gap-3 mb-2">
            <span className="inline-flex items-center rounded-full bg-navy-50 px-3 py-1 text-xs font-bold text-navy tracking-wide">
              Member {p.memberNumber}
            </span>
            {p.militaryStatus !== 'None' && p.militaryStatus !== 'Civilian' && (
              <span className="inline-flex items-center rounded-full bg-green-50 px-3 py-1 text-xs font-bold text-green-700">
                {p.militaryStatus}
              </span>
            )}
          </div>
          <h1 className="text-2xl font-extrabold text-navy" id="welcome">
            Welcome, {p.firstName} {p.lastName}
          </h1>
          <p className="text-sm text-gray-500 mt-1">Your VKPower Life policy summary</p>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-8">
            {[
              { label: 'Policy', value: p.policyNumber, id: 'pol' },
              { label: 'Coverage', value: money(p.coverageAmount), id: 'cov' },
              { label: 'Monthly Premium', value: money(p.monthlyPremium), id: 'prem' },
              { label: 'Beneficiaries', value: String(p.beneficiaries.length), id: 'bencount' },
            ].map(kv => (
              <div key={kv.id} className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
                <div className="text-[11px] uppercase tracking-wider text-gray-400 font-semibold">{kv.label}</div>
                <div className="text-xl font-extrabold text-navy mt-1" id={kv.id}>{kv.value}</div>
              </div>
            ))}
          </div>

          <div className="mt-8 card">
            <h2 className="section-title">Personal Information</h2>
            <div className="field-grid text-sm">
              <div><span className="text-gray-500">Date of Birth:</span> <span className="font-medium">{p.dateOfBirth}</span></div>
              <div><span className="text-gray-500">Gender:</span> <span className="font-medium">{p.gender}</span></div>
              <div><span className="text-gray-500">Email:</span> <span className="font-medium">{p.email}</span></div>
              <div><span className="text-gray-500">Phone:</span> <span className="font-medium">{p.phone}</span></div>
              <div><span className="text-gray-500">Address:</span> <span className="font-medium">{p.address.street}{p.address.unit ? `, ${p.address.unit}` : ''}, {p.address.city}, {p.address.state} {p.address.zip}</span></div>
              <div><span className="text-gray-500">Employment:</span> <span className="font-medium">{p.employment.occupation} at {p.employment.employer}</span></div>
            </div>
          </div>

          <div className="flex flex-wrap gap-4 mt-8">
            <Link href="/portal/beneficiaries" className="btn-gold">Manage Beneficiaries</Link>
            <Link href="/life-insurance/quote/start" className="btn-primary">Get a New Quote</Link>
          </div>
        </div>
      </main>
    </>
  );
}
