'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import { useApp } from '@/lib/store';
import { RELATIONSHIPS } from '@/lib/states';

export default function BeneficiariesPage() {
  const router = useRouter();
  const { state, addBeneficiary, removeBeneficiary } = useApp();
  const [firstName, setFirstName] = useState('');
  const [lastName, setLastName] = useState('');
  const [relationship, setRelationship] = useState('Spouse');
  const [percentage, setPercentage] = useState('10');
  const [dob, setDob] = useState('');
  const [ssn, setSsn] = useState('');
  const [bType, setBType] = useState<'primary' | 'contingent'>('primary');

  useEffect(() => {
    if (!state.authenticated) router.replace('/login/');
  }, [state.authenticated, router]);

  if (!state.authenticated || !state.profile) return null;
  const p = state.profile;
  const all = [...p.beneficiaries, ...state.beneficiaries];
  const primary = all.filter(b => b.type === 'primary');
  const contingent = all.filter(b => b.type === 'contingent');

  const handleAdd = (e: React.FormEvent) => {
    e.preventDefault();
    addBeneficiary({
      firstName: firstName || 'New',
      lastName: lastName || 'Beneficiary',
      relationship,
      percentage: parseInt(percentage) || 10,
      dateOfBirth: dob,
      ssn: ssn || '***-**-0000',
      type: bType,
    });
    setFirstName(''); setLastName(''); setDob(''); setSsn(''); setPercentage('10');
  };

  const renderTable = (title: string, list: typeof all) => (
    <div className="mb-8">
      <h2 className="section-title">{title} ({list.length})</h2>
      {list.length === 0 ? (
        <p className="text-sm text-gray-400 italic">No {title.toLowerCase()} designated</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200">
                <th className="text-left py-2 px-3 text-[11px] uppercase tracking-wider text-gray-400 font-semibold">#</th>
                <th className="text-left py-2 px-3 text-[11px] uppercase tracking-wider text-gray-400 font-semibold">Name</th>
                <th className="text-left py-2 px-3 text-[11px] uppercase tracking-wider text-gray-400 font-semibold">Relationship</th>
                <th className="text-left py-2 px-3 text-[11px] uppercase tracking-wider text-gray-400 font-semibold">Date of Birth</th>
                <th className="text-right py-2 px-3 text-[11px] uppercase tracking-wider text-gray-400 font-semibold">Share</th>
              </tr>
            </thead>
            <tbody>
              {list.map((b, i) => (
                <tr key={i} className="border-b border-gray-100 hover:bg-gray-50">
                  <td className="py-2.5 px-3 text-gray-400">{i + 1}</td>
                  <td className="py-2.5 px-3 font-medium">{b.firstName} {b.lastName}</td>
                  <td className="py-2.5 px-3 text-gray-600">{b.relationship}</td>
                  <td className="py-2.5 px-3 text-gray-600">{b.dateOfBirth || '—'}</td>
                  <td className="py-2.5 px-3 text-right font-bold text-navy">{b.percentage}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );

  return (
    <>
      <Header />
      <main className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <h1 className="text-2xl font-extrabold text-navy mb-1">Beneficiaries</h1>
        <p className="text-sm text-gray-500 mb-8">{p.firstName} {p.lastName} — {all.length} beneficiary(ies) on file</p>

        <div className="card">
          {renderTable('Primary Beneficiaries', primary)}
          {renderTable('Contingent Beneficiaries', contingent)}

          <h2 className="section-title">Add a Beneficiary</h2>
          <form onSubmit={handleAdd}>
            <div className="field-grid">
              <div>
                <label htmlFor="b_first" className="form-label">First name</label>
                <input id="b_first" className="form-input" value={firstName} onChange={e => setFirstName(e.target.value)} required />
              </div>
              <div>
                <label htmlFor="b_last" className="form-label">Last name</label>
                <input id="b_last" className="form-input" value={lastName} onChange={e => setLastName(e.target.value)} required />
              </div>
              <div>
                <label htmlFor="b_rel" className="form-label">Relationship</label>
                <select id="b_rel" className="form-input" value={relationship} onChange={e => setRelationship(e.target.value)}>
                  {RELATIONSHIPS.map(r => <option key={r} value={r}>{r}</option>)}
                </select>
              </div>
              <div>
                <label htmlFor="b_type" className="form-label">Type</label>
                <select id="b_type" className="form-input" value={bType} onChange={e => setBType(e.target.value as 'primary' | 'contingent')}>
                  <option value="primary">Primary</option>
                  <option value="contingent">Contingent</option>
                </select>
              </div>
              <div>
                <label htmlFor="b_pct" className="form-label">Share %</label>
                <input id="b_pct" className="form-input" type="number" min="1" max="100" value={percentage} onChange={e => setPercentage(e.target.value)} />
              </div>
              <div>
                <label htmlFor="b_dob" className="form-label">Date of birth</label>
                <input id="b_dob" className="form-input" type="date" value={dob} onChange={e => setDob(e.target.value)} />
              </div>
              <div>
                <label htmlFor="b_ssn" className="form-label">SSN (last 4)</label>
                <input id="b_ssn" className="form-input" maxLength={4} inputMode="numeric" placeholder="0000" value={ssn} onChange={e => setSsn(e.target.value)} />
              </div>
            </div>
            <div className="mt-6">
              <button type="submit" className="btn-primary">Add Beneficiary</button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
