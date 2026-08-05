'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';
import { RELATIONSHIPS } from '@/lib/states';
import type { BeneficiaryRecord } from '@/lib/types';

const APPLY_STEPS = [
  { label: 'Member' }, { label: 'Personal' }, { label: 'Replacement' },
  { label: 'Health (HLQ)' }, { label: 'Lifestyle' }, { label: 'Decision' },
  { label: 'Payment' }, { label: 'Beneficiary' }, { label: 'Signature' }, { label: 'Confirmation' },
];

const EMPTY_BEN: BeneficiaryRecord = {
  firstName: '', lastName: '', relationship: '', percentage: 0,
  dateOfBirth: '', ssn: '', type: 'primary',
};

export default function BeneficiaryDesignationPage() {
  const router = useRouter();
  const { state, addBeneficiary, removeBeneficiary } = useApp();
  const [form, setForm] = useState<BeneficiaryRecord>({ ...EMPTY_BEN });
  const [error, setError] = useState('');

  const primary = state.beneficiaries.filter(b => b.type === 'primary');
  const contingent = state.beneficiaries.filter(b => b.type === 'contingent');
  const primaryTotal = primary.reduce((s, b) => s + b.percentage, 0);
  const contingentTotal = contingent.reduce((s, b) => s + b.percentage, 0);

  const handleAdd = (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.firstName || !form.lastName || !form.relationship || !form.percentage) {
      setError('Please complete all required fields.');
      return;
    }
    const existing = form.type === 'primary' ? primaryTotal : contingentTotal;
    if (existing + form.percentage > 100) {
      setError(`Total ${form.type} beneficiary allocation cannot exceed 100%. Currently at ${existing}%.`);
      return;
    }
    setError('');
    addBeneficiary(form);
    setForm({ ...EMPTY_BEN, type: form.type });
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (primary.length === 0) {
      setError('At least one primary beneficiary is required.');
      return;
    }
    if (primaryTotal !== 100) {
      setError(`Primary beneficiary allocations must total 100%. Currently at ${primaryTotal}%.`);
      return;
    }
    if (contingent.length > 0 && contingentTotal !== 100) {
      setError(`Contingent beneficiary allocations must total 100%. Currently at ${contingentTotal}%.`);
      return;
    }
    router.push('/life-insurance/apply/signature/');
  };

  const set = (key: keyof BeneficiaryRecord) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const val = key === 'percentage' ? parseInt(e.target.value) || 0 : e.target.value;
    setForm(f => ({ ...f, [key]: val }));
  };

  function BenTable({ title, list, total, benType }: { title: string; list: BeneficiaryRecord[]; total: number; benType: string }) {
    return (
      <div className="mb-6">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-bold text-navy text-sm">{title}</h3>
          <span className={`text-xs font-semibold px-2 py-1 rounded ${total === 100 ? 'bg-green-100 text-green-700' : total > 0 ? 'bg-amber-100 text-amber-700' : 'bg-gray-100 text-gray-500'}`}>
            {total}% of 100%
          </span>
        </div>
        {list.length === 0 ? (
          <p className="text-sm text-gray-400 py-4 text-center bg-gray-50 rounded-lg">No {benType} beneficiaries added</p>
        ) : (
          <div className="space-y-2">
            {list.map((b, i) => {
              const globalIdx = state.beneficiaries.indexOf(b);
              return (
                <div key={i} className="flex items-center justify-between bg-gray-50 rounded-lg p-4 text-sm">
                  <div className="flex-1">
                    <span className="font-medium">{b.firstName} {b.lastName}</span>
                    <span className="text-gray-400 mx-2">|</span>
                    <span className="text-gray-600">{b.relationship}</span>
                    <span className="text-gray-400 mx-2">|</span>
                    <span className="font-bold text-navy">{b.percentage}%</span>
                    {b.dateOfBirth && <span className="text-gray-400 ml-2 text-xs">DOB: {b.dateOfBirth}</span>}
                  </div>
                  <button type="button" onClick={() => removeBeneficiary(globalIdx)} className="text-red-500 hover:text-red-700 text-xs font-semibold ml-4">
                    Remove
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  return (
    <>
      <Header />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={APPLY_STEPS} current={7} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Beneficiary Designation</h1>
          <p className="text-sm text-gray-500 mb-8">
            Designate who will receive the death benefit proceeds. Primary beneficiary allocations must total 100%.
            Contingent beneficiaries receive the benefit if no primary beneficiary survives the insured.
          </p>

          <form onSubmit={handleSubmit}>
            <BenTable title="Primary Beneficiaries" list={primary} total={primaryTotal} benType="primary" />
            <BenTable title="Contingent Beneficiaries" list={contingent} total={contingentTotal} benType="contingent" />

            <div className="border border-gray-200 rounded-xl p-5 mb-6">
              <h2 className="section-title">Add Beneficiary</h2>
              <div className="field-grid">
                <div>
                  <label htmlFor="ben_type" className="form-label">Beneficiary Type</label>
                  <select id="ben_type" className="form-input" value={form.type} onChange={e => setForm(f => ({ ...f, type: e.target.value as 'primary' | 'contingent' }))}>
                    <option value="primary">Primary</option>
                    <option value="contingent">Contingent</option>
                  </select>
                </div>
                <div>
                  <label htmlFor="ben_first" className="form-label">First Name</label>
                  <input id="ben_first" className="form-input" value={form.firstName} onChange={set('firstName')} />
                </div>
                <div>
                  <label htmlFor="ben_last" className="form-label">Last Name</label>
                  <input id="ben_last" className="form-input" value={form.lastName} onChange={set('lastName')} />
                </div>
                <div>
                  <label htmlFor="ben_rel" className="form-label">Relationship</label>
                  <select id="ben_rel" className="form-input" value={form.relationship} onChange={set('relationship')}>
                    <option value="">Select...</option>
                    {RELATIONSHIPS.map(r => <option key={r} value={r}>{r}</option>)}
                  </select>
                </div>
                <div>
                  <label htmlFor="ben_pct" className="form-label">Percentage (%)</label>
                  <input id="ben_pct" type="number" className="form-input" min={1} max={100} value={form.percentage || ''} onChange={set('percentage')} />
                </div>
                <div>
                  <label htmlFor="ben_dob" className="form-label">Date of Birth</label>
                  <input id="ben_dob" type="date" className="form-input" value={form.dateOfBirth} onChange={set('dateOfBirth')} />
                </div>
                <div>
                  <label htmlFor="ben_ssn" className="form-label">SSN</label>
                  <input id="ben_ssn" className="form-input" placeholder="XXX-XX-XXXX" value={form.ssn} onChange={set('ssn')} />
                </div>
              </div>
              <div className="mt-4">
                <button type="button" onClick={handleAdd} className="btn-outline">+ Add Beneficiary</button>
              </div>
            </div>

            {error && (
              <div className="bg-red-50 border border-red-200 rounded-lg p-3 mb-6">
                <p className="text-sm text-red-700 font-medium">{error}</p>
              </div>
            )}

            <div className="flex gap-3">
              <button type="submit" className="btn-primary">Continue to Signature</button>
              <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
