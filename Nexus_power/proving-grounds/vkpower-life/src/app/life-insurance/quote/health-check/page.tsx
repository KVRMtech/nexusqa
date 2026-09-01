'use client';

import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';

const QUOTE_STEPS = [
  { label: 'Product' }, { label: 'Coverage' }, { label: 'Personal' },
  { label: 'Health' }, { label: 'Review' },
];

export default function QuoteHealthCheckPage() {
  const router = useRouter();
  const { state, updateQuote } = useApp();
  const q = state.quote;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const heightFt = parseInt(q.heightFeet || '0');
    const heightIn = parseInt(q.heightInches || '0');
    const weight = parseInt(q.weight || '0');
    const totalInches = heightFt * 12 + heightIn;
    const bmi = totalInches > 0 ? (weight / (totalInches * totalInches)) * 703 : 0;

    const age = q.dateOfBirth ? Math.floor((Date.now() - new Date(q.dateOfBirth).getTime()) / 31557600000) : 30;
    const coverageAmount = parseInt(q.coverageAmount || '250000');
    const basePer1000 = 0.08;
    const ageFactor = 1 + Math.pow(Math.max(0, age - 25) / 40, 1.8) * 2.5;
    const termFactor = q.termLength === 'whole' ? 6 : 1 + (parseInt(q.termLength || '20') - 10) * 0.04;
    const genderFactor = q.gender === 'Female' ? 0.88 : 1.0;
    const tobaccoFactor = q.tobaccoUse === 'Yes' ? 1.8 : 1.0;
    const bmiFactor = bmi > 35 ? 1.5 : bmi > 30 ? 1.2 : 1.0;
    const premium = (coverageAmount / 1000) * basePer1000 * ageFactor * termFactor * genderFactor * tobaccoFactor * bmiFactor;

    updateQuote({ estimatedPremium: Math.round(premium * 100) / 100 });
    router.push('/life-insurance/quote/review/');
  };

  return (
    <>
      <Header />
      <main className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={QUOTE_STEPS} current={3} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Health Eligibility</h1>
          <p className="text-sm text-gray-500 mb-8">
            A few quick health questions to determine your eligibility and estimate your premium.
            Detailed health information will be collected during the application process.
          </p>

          <form onSubmit={handleSubmit}>
            <div className="space-y-6">
              <div className="field-grid-3">
                <div>
                  <label htmlFor="q_height_ft" className="form-label">Height (feet)</label>
                  <select
                    id="q_height_ft" className="form-input"
                    value={q.heightFeet || ''} onChange={e => updateQuote({ heightFeet: e.target.value })}
                    required
                  >
                    <option value="">Feet...</option>
                    {['4', '5', '6', '7'].map(f => <option key={f} value={f}>{f} ft</option>)}
                  </select>
                </div>
                <div>
                  <label htmlFor="q_height_in" className="form-label">Height (inches)</label>
                  <select
                    id="q_height_in" className="form-input"
                    value={q.heightInches || ''} onChange={e => updateQuote({ heightInches: e.target.value })}
                    required
                  >
                    <option value="">Inches...</option>
                    {Array.from({ length: 12 }, (_, i) => String(i)).map(n =>
                      <option key={n} value={n}>{n} in</option>
                    )}
                  </select>
                </div>
                <div>
                  <label htmlFor="q_weight" className="form-label">Weight (lbs)</label>
                  <input
                    id="q_weight" type="number" className="form-input"
                    min="50" max="600" placeholder="e.g. 170"
                    value={q.weight || ''} onChange={e => updateQuote({ weight: e.target.value })}
                    required
                  />
                </div>
              </div>

              <div>
                <label className="form-label">Have you used tobacco or nicotine products in the past 12 months?</label>
                <div className="flex gap-3 mt-1">
                  {['No', 'Yes'].map(v => (
                    <button
                      key={v} type="button"
                      onClick={() => updateQuote({ tobaccoUse: v })}
                      className={`px-6 py-2 rounded-lg text-sm font-semibold border transition-colors ${
                        q.tobaccoUse === v
                          ? 'bg-navy text-white border-navy'
                          : 'bg-white text-gray-600 border-gray-300 hover:border-gray-400'
                      }`}
                    >
                      {v}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className="form-label">
                  Have you been diagnosed with or treated for any of the following in the past 10 years?
                </label>
                <p className="text-xs text-gray-500 mb-3">
                  Heart disease, cancer, stroke, diabetes, chronic kidney disease, liver disease, HIV/AIDS,
                  lung disease, or any condition requiring ongoing treatment.
                </p>
                <div className="flex gap-3">
                  {['No', 'Yes'].map(v => (
                    <button
                      key={v} type="button"
                      onClick={() => updateQuote({ healthEligible: v === 'No' })}
                      className={`px-6 py-2 rounded-lg text-sm font-semibold border transition-colors ${
                        (q.healthEligible === true && v === 'No') || (q.healthEligible === false && v === 'Yes')
                          ? 'bg-navy text-white border-navy'
                          : 'bg-white text-gray-600 border-gray-300 hover:border-gray-400'
                      }`}
                    >
                      {v}
                    </button>
                  ))}
                </div>
                {q.healthEligible === false && (
                  <p className="text-xs text-amber-600 mt-2">
                    Pre-existing conditions may affect your premium or require additional underwriting review.
                    You can still proceed with your quote.
                  </p>
                )}
              </div>
            </div>

            <div className="mt-8 flex gap-3">
              <button type="submit" className="btn-gold">See My Quote</button>
              <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
