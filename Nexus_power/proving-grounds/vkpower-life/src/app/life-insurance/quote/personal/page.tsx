'use client';

import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';
import { US_STATES } from '@/lib/states';

const QUOTE_STEPS = [
  { label: 'Product' }, { label: 'Coverage' }, { label: 'Personal' },
  { label: 'Health' }, { label: 'Review' },
];

export default function QuotePersonalPage() {
  const router = useRouter();
  const { state, updateQuote } = useApp();
  const q = state.quote;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    router.push('/life-insurance/quote/health-check/');
  };

  return (
    <>
      <Header />
      <main className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={QUOTE_STEPS} current={2} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Personal Information</h1>
          <p className="text-sm text-gray-500 mb-8">
            Basic information to calculate your personalized quote.
          </p>

          <form onSubmit={handleSubmit}>
            <div className="field-grid">
              <div>
                <label htmlFor="q_first" className="form-label">First name</label>
                <input
                  id="q_first" className="form-input" autoComplete="given-name"
                  value={q.firstName || ''} onChange={e => updateQuote({ firstName: e.target.value })}
                  required
                />
              </div>
              <div>
                <label htmlFor="q_last" className="form-label">Last name</label>
                <input
                  id="q_last" className="form-input" autoComplete="family-name"
                  value={q.lastName || ''} onChange={e => updateQuote({ lastName: e.target.value })}
                  required
                />
              </div>
              <div>
                <label htmlFor="q_dob" className="form-label">Date of birth</label>
                <input
                  id="q_dob" type="date" className="form-input" autoComplete="bday"
                  value={q.dateOfBirth || ''} onChange={e => updateQuote({ dateOfBirth: e.target.value })}
                  required
                />
              </div>
              <div>
                <label htmlFor="q_gender" className="form-label">Gender</label>
                <select
                  id="q_gender" className="form-input"
                  value={q.gender || ''} onChange={e => updateQuote({ gender: e.target.value })}
                  required
                >
                  <option value="">Select...</option>
                  <option value="Male">Male</option>
                  <option value="Female">Female</option>
                  <option value="Non-binary">Non-binary</option>
                </select>
              </div>
              <div>
                <label htmlFor="q_state" className="form-label">State of residence</label>
                <select
                  id="q_state" className="form-input" autoComplete="address-level1"
                  value={q.state || ''} onChange={e => updateQuote({ state: e.target.value })}
                  required
                >
                  <option value="">Select state...</option>
                  {US_STATES.map(s => (
                    <option key={s.code} value={s.code}>{s.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="q_email" className="form-label">Email address</label>
                <input
                  id="q_email" type="email" className="form-input" autoComplete="email"
                  placeholder="you@example.com"
                  value={q.email || ''} onChange={e => updateQuote({ email: e.target.value })}
                  required
                />
              </div>
              <div>
                <label htmlFor="q_phone" className="form-label">Phone number</label>
                <input
                  id="q_phone" type="tel" className="form-input" autoComplete="tel"
                  placeholder="(555) 555-0100"
                  value={q.phone || ''} onChange={e => updateQuote({ phone: e.target.value })}
                  required
                />
              </div>
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
