'use client';

import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';
import { COVERAGE_AMOUNTS, TERM_LENGTHS, MILITARY_STATUSES, MILITARY_BRANCHES } from '@/lib/states';

const QUOTE_STEPS = [
  { label: 'Product' }, { label: 'Coverage' }, { label: 'Personal' },
  { label: 'Health' }, { label: 'Review' },
];

export default function QuoteCoveragePage() {
  const router = useRouter();
  const { state, updateQuote } = useApp();
  const q = state.quote;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    router.push('/life-insurance/quote/personal/');
  };

  const showBranch = q.militaryStatus && q.militaryStatus !== 'Civilian' && q.militaryStatus !== 'None';

  return (
    <>
      <Header />
      <main className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={QUOTE_STEPS} current={1} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Coverage Details</h1>
          <p className="text-sm text-gray-500 mb-8">
            Select your desired coverage amount, term length, and military affiliation.
          </p>

          <form onSubmit={handleSubmit}>
            <div className="space-y-6">
              <div>
                <label htmlFor="coverage_amount" className="form-label">Coverage Amount</label>
                <select
                  id="coverage_amount" className="form-input"
                  value={q.coverageAmount || ''} onChange={e => updateQuote({ coverageAmount: e.target.value })}
                  required
                >
                  <option value="">Select coverage amount...</option>
                  {COVERAGE_AMOUNTS.map(c => (
                    <option key={c.value} value={c.value}>{c.label}</option>
                  ))}
                </select>
              </div>

              <div>
                <label htmlFor="term_length" className="form-label">Term Length</label>
                <select
                  id="term_length" className="form-input"
                  value={q.termLength || ''} onChange={e => updateQuote({ termLength: e.target.value })}
                  required
                >
                  <option value="">Select term length...</option>
                  {TERM_LENGTHS.map(t => (
                    <option key={t.value} value={t.value}>{t.label}</option>
                  ))}
                </select>
                {q.product === 'whole-life' && (
                  <p className="text-xs text-amber-600 mt-1">Whole Life policies provide lifetime coverage. The term length applies to premium guarantee period.</p>
                )}
              </div>

              <div>
                <label htmlFor="military_status" className="form-label">Military Affiliation</label>
                <select
                  id="military_status" className="form-input"
                  value={q.militaryStatus || ''} onChange={e => updateQuote({ militaryStatus: e.target.value })}
                  required
                >
                  <option value="">Select military status...</option>
                  {MILITARY_STATUSES.map(m => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
              </div>

              {showBranch && (
                <div>
                  <label htmlFor="military_branch" className="form-label">Branch of Service</label>
                  <select
                    id="military_branch" className="form-input"
                    value={q.militaryBranch || ''} onChange={e => updateQuote({ militaryBranch: e.target.value })}
                    required
                  >
                    <option value="">Select branch...</option>
                    {MILITARY_BRANCHES.map(b => (
                      <option key={b} value={b}>{b}</option>
                    ))}
                  </select>
                </div>
              )}
            </div>

            <div className="mt-8 flex gap-3">
              <button type="submit" className="btn-primary">Continue</button>
              <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
