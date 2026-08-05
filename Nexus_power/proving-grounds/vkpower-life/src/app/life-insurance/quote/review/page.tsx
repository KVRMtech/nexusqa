'use client';

import { useRouter } from 'next/navigation';
import Link from 'next/link';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';
import { COVERAGE_AMOUNTS, TERM_LENGTHS, PRODUCTS } from '@/lib/states';

const QUOTE_STEPS = [
  { label: 'Product' }, { label: 'Coverage' }, { label: 'Personal' },
  { label: 'Health' }, { label: 'Review' },
];

function money(n: number) {
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function QuoteReviewPage() {
  const router = useRouter();
  const { state } = useApp();
  const q = state.quote;

  const productLabel = PRODUCTS.find(p => p.value === q.product)?.label || q.product || '—';
  const coverageLabel = COVERAGE_AMOUNTS.find(c => c.value === q.coverageAmount)?.label || q.coverageAmount || '—';
  const termLabel = TERM_LENGTHS.find(t => t.value === q.termLength)?.label || q.termLength || '—';
  const premium = q.estimatedPremium || 0;

  return (
    <>
      <Header />
      <main className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={QUOTE_STEPS} current={4} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Your Quote Summary</h1>
          <p className="text-sm text-gray-500 mb-8">
            Based on the information you provided, here is your estimated premium.
          </p>

          <div className="bg-gradient-to-br from-navy to-navy-600 text-white rounded-xl p-6 mb-8">
            <div className="text-sm text-gray-300 mb-1">Estimated Monthly Premium</div>
            <div className="text-4xl font-black" id="q_premium">{money(premium)}</div>
            <div className="text-xs text-gray-400 mt-2">
              Final premium determined after full application and underwriting review
            </div>
          </div>

          <div className="space-y-6">
            <div>
              <h2 className="section-title">Coverage Details</h2>
              <div className="field-grid text-sm">
                <div className="flex justify-between py-2 border-b border-gray-100">
                  <span className="text-gray-500">Product</span>
                  <span className="font-medium">{productLabel}</span>
                </div>
                <div className="flex justify-between py-2 border-b border-gray-100">
                  <span className="text-gray-500">Coverage Amount</span>
                  <span className="font-medium">{coverageLabel}</span>
                </div>
                <div className="flex justify-between py-2 border-b border-gray-100">
                  <span className="text-gray-500">Term Length</span>
                  <span className="font-medium">{termLabel}</span>
                </div>
                <div className="flex justify-between py-2 border-b border-gray-100">
                  <span className="text-gray-500">Military Status</span>
                  <span className="font-medium">{q.militaryStatus || '—'}</span>
                </div>
              </div>
            </div>

            <div>
              <h2 className="section-title">Applicant Information</h2>
              <div className="field-grid text-sm">
                <div className="flex justify-between py-2 border-b border-gray-100">
                  <span className="text-gray-500">Name</span>
                  <span className="font-medium">{q.firstName} {q.lastName}</span>
                </div>
                <div className="flex justify-between py-2 border-b border-gray-100">
                  <span className="text-gray-500">Date of Birth</span>
                  <span className="font-medium">{q.dateOfBirth || '—'}</span>
                </div>
                <div className="flex justify-between py-2 border-b border-gray-100">
                  <span className="text-gray-500">Gender</span>
                  <span className="font-medium">{q.gender || '—'}</span>
                </div>
                <div className="flex justify-between py-2 border-b border-gray-100">
                  <span className="text-gray-500">State</span>
                  <span className="font-medium">{q.state || '—'}</span>
                </div>
                <div className="flex justify-between py-2 border-b border-gray-100">
                  <span className="text-gray-500">Tobacco Use</span>
                  <span className="font-medium">{q.tobaccoUse || '—'}</span>
                </div>
                <div className="flex justify-between py-2 border-b border-gray-100">
                  <span className="text-gray-500">Health Pre-Screen</span>
                  <span className={`font-medium ${q.healthEligible ? 'text-green-700' : 'text-amber-600'}`}>
                    {q.healthEligible ? 'No conditions reported' : 'Conditions reported — review required'}
                  </span>
                </div>
              </div>
            </div>
          </div>

          <div className="mt-8 bg-green-50 border border-green-200 rounded-xl p-5">
            <h3 className="font-bold text-green-800 mb-1">Ready to apply?</h3>
            <p className="text-sm text-green-700">
              Lock in your rate by completing a full application. The process takes approximately 15-20 minutes
              and includes detailed health history, beneficiary designation, and payment setup.
            </p>
          </div>

          <div className="mt-6 flex flex-wrap gap-3">
            <Link href="/life-insurance/apply/member-lookup" className="btn-gold text-base px-8 py-3">
              Apply Now
            </Link>
            <button type="button" className="btn-outline" onClick={() => router.push('/life-insurance/quote/start/')}>
              Start Over
            </button>
            {state.authenticated && (
              <Link href="/portal/dashboard" className="btn-outline">
                Back to Dashboard
              </Link>
            )}
          </div>
        </div>
      </main>
    </>
  );
}
