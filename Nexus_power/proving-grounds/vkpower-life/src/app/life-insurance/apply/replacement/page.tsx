'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';

const APPLY_STEPS = [
  { label: 'Member' }, { label: 'Personal' }, { label: 'Replacement' },
  { label: 'Health (HLQ)' }, { label: 'Lifestyle' }, { label: 'Decision' },
  { label: 'Payment' }, { label: 'Beneficiary' }, { label: 'Signature' }, { label: 'Confirmation' },
];

export default function ReplacementPage() {
  const router = useRouter();
  const { state, addReplacement, removeReplacement } = useApp();
  const [hasInternal, setHasInternal] = useState('No');
  const [hasExternal, setHasExternal] = useState('No');
  const [company, setCompany] = useState('');
  const [policyNumber, setPolicyNumber] = useState('');
  const [faceAmount, setFaceAmount] = useState('');
  const [reason, setReason] = useState('');
  const [addType, setAddType] = useState<'internal' | 'external'>('internal');

  const handleAdd = (e: React.FormEvent) => {
    e.preventDefault();
    addReplacement({
      type: addType,
      company: company || (addType === 'internal' ? 'VKPower Life Insurance' : ''),
      policyNumber: policyNumber || '',
      faceAmount: faceAmount || '0',
      reason: reason || '',
    });
    setCompany(''); setPolicyNumber(''); setFaceAmount(''); setReason('');
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    router.push('/life-insurance/apply/health/');
  };

  const internals = state.replacements.filter(r => r.type === 'internal');
  const externals = state.replacements.filter(r => r.type === 'external');

  return (
    <>
      <Header />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={APPLY_STEPS} current={2} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Replacement Information</h1>
          <p className="text-sm text-gray-500 mb-8">
            Federal and state regulations require disclosure of any existing life insurance policies
            that will be replaced or changed as a result of this new application.
          </p>

          <form onSubmit={handleSubmit}>
            <div className="space-y-8">
              <div className="bg-gray-50 rounded-xl p-5">
                <label className="form-label text-base">
                  Will this policy replace or change any existing VKPower Life insurance or annuity?
                </label>
                <div className="flex gap-3 mt-2">
                  {['No', 'Yes'].map(v => (
                    <button
                      key={v} type="button"
                      onClick={() => setHasInternal(v)}
                      className={`px-6 py-2 rounded-lg text-sm font-semibold border transition-colors ${
                        hasInternal === v ? 'bg-navy text-white border-navy' : 'bg-white text-gray-600 border-gray-300 hover:border-gray-400'
                      }`}
                    >
                      {v}
                    </button>
                  ))}
                </div>
              </div>

              <div className="bg-gray-50 rounded-xl p-5">
                <label className="form-label text-base">
                  Will this policy replace or change any life insurance or annuity from another company?
                </label>
                <div className="flex gap-3 mt-2">
                  {['No', 'Yes'].map(v => (
                    <button
                      key={v} type="button"
                      onClick={() => setHasExternal(v)}
                      className={`px-6 py-2 rounded-lg text-sm font-semibold border transition-colors ${
                        hasExternal === v ? 'bg-navy text-white border-navy' : 'bg-white text-gray-600 border-gray-300 hover:border-gray-400'
                      }`}
                    >
                      {v}
                    </button>
                  ))}
                </div>
              </div>

              {(hasInternal === 'Yes' || hasExternal === 'Yes') && (
                <div className="border border-gray-200 rounded-xl p-5">
                  <h2 className="section-title">Add Replacement Policy</h2>
                  <div className="field-grid">
                    <div>
                      <label htmlFor="rp_type" className="form-label">Replacement Type</label>
                      <select id="rp_type" className="form-input" value={addType} onChange={e => setAddType(e.target.value as 'internal' | 'external')}>
                        <option value="internal">Internal (VKPower)</option>
                        <option value="external">External (Other Company)</option>
                      </select>
                    </div>
                    <div>
                      <label htmlFor="rp_company" className="form-label">Insurance Company</label>
                      <input id="rp_company" className="form-input" placeholder={addType === 'internal' ? 'VKPower Life Insurance' : 'Company name'} value={company} onChange={e => setCompany(e.target.value)} />
                    </div>
                    <div>
                      <label htmlFor="rp_policy" className="form-label">Policy Number</label>
                      <input id="rp_policy" className="form-input" placeholder="e.g. VKPL-12345" value={policyNumber} onChange={e => setPolicyNumber(e.target.value)} />
                    </div>
                    <div>
                      <label htmlFor="rp_face" className="form-label">Face Amount ($)</label>
                      <input id="rp_face" type="number" className="form-input" placeholder="e.g. 250000" value={faceAmount} onChange={e => setFaceAmount(e.target.value)} />
                    </div>
                    <div className="md:col-span-2">
                      <label htmlFor="rp_reason" className="form-label">Reason for Replacement</label>
                      <select id="rp_reason" className="form-input" value={reason} onChange={e => setReason(e.target.value)}>
                        <option value="">Select reason...</option>
                        <option value="Lower Premium">Lower Premium</option>
                        <option value="Higher Coverage">Higher Coverage Amount</option>
                        <option value="Better Benefits">Better Policy Benefits</option>
                        <option value="Coverage Consolidation">Coverage Consolidation</option>
                        <option value="Term Conversion">Term to Permanent Conversion</option>
                        <option value="Financial Planning">Financial Planning Change</option>
                        <option value="Other">Other</option>
                      </select>
                    </div>
                  </div>
                  <div className="mt-4">
                    <button type="button" onClick={handleAdd} className="btn-outline">
                      + Add Replacement Policy
                    </button>
                  </div>
                </div>
              )}

              {state.replacements.length > 0 && (
                <div>
                  <h2 className="section-title">Policies to be Replaced ({state.replacements.length})</h2>
                  <div className="space-y-3">
                    {state.replacements.map((r, i) => (
                      <div key={i} className="flex items-center justify-between bg-gray-50 rounded-lg p-4 text-sm">
                        <div>
                          <span className="font-medium">{r.company || 'Unknown'}</span>
                          <span className="text-gray-400 mx-2">|</span>
                          <span className="text-gray-600">{r.policyNumber || 'No number'}</span>
                          <span className="text-gray-400 mx-2">|</span>
                          <span className="text-gray-600">${parseInt(r.faceAmount || '0').toLocaleString()}</span>
                          <span className="inline-block ml-2 text-xs bg-gray-200 text-gray-600 rounded px-2 py-0.5">{r.type}</span>
                        </div>
                        <button type="button" onClick={() => removeReplacement(i)} className="text-red-500 hover:text-red-700 text-xs font-semibold">
                          Remove
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <div className="mt-8 flex gap-3">
              <button type="submit" className="btn-primary">Continue to Health Questionnaire</button>
              <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
