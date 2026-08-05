'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';
import { evaluateUnderwriting, calculatePremium, type UnderwritingResult } from '@/lib/underwriting';

const APPLY_STEPS = [
  { label: 'Member' }, { label: 'Personal' }, { label: 'Replacement' },
  { label: 'Health (HLQ)' }, { label: 'Lifestyle' }, { label: 'Decision' },
  { label: 'Payment' }, { label: 'Beneficiary' }, { label: 'Signature' }, { label: 'Confirmation' },
];

export default function DecisionPage() {
  const router = useRouter();
  const { state, setDecision } = useApp();
  const [result, setResult] = useState<UnderwritingResult | null>(null);
  const [premium, setPremium] = useState(0);
  const [processing, setProcessing] = useState(true);

  useEffect(() => {
    const timer = setTimeout(() => {
      const q = state.quote;
      const pi = state.personalInfo;
      const dob = q.dateOfBirth || pi.ownerDob || '1990-01-01';
      const age = Math.floor((Date.now() - new Date(dob).getTime()) / 31557600000);
      const gender = q.gender || pi.ownerGender || 'Male';
      const hFeet = parseInt(q.heightFeet || '5');
      const hIn = parseInt(q.heightInches || '10');
      const weight = parseInt(q.weight || '170');
      const heightM = (hFeet * 12 + hIn) * 0.0254;
      const bmi = weight * 0.453592 / (heightM * heightM);
      const tobacco = q.tobaccoUse === 'Yes' || state.lifestyleAnswers.ls_tobacco === 'Yes';
      const coverage = parseInt(q.coverageAmount || '250000');
      const milStatus = q.militaryStatus || pi.employmentStatus || '';

      const uw = evaluateUnderwriting(age, gender, bmi, tobacco, state.healthAnswers, state.lifestyleAnswers, milStatus, coverage);
      setResult(uw);
      setDecision(uw.decision, uw.reasons);

      if (uw.decision === 'approved' || uw.decision === 'referred') {
        const prem = calculatePremium(coverage, q.termLength || '20', age, gender, uw.premiumMultiplier);
        setPremium(prem);
      }
      setProcessing(false);
    }, 2500);
    return () => clearTimeout(timer);
  }, []);

  return (
    <>
      <Header />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={APPLY_STEPS} current={5} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Underwriting Decision</h1>

          {processing && (
            <div className="flex flex-col items-center py-16">
              <div className="w-16 h-16 border-4 border-gray-200 border-t-gold rounded-full animate-spin mb-6" />
              <p className="text-lg font-bold text-navy">Processing Your Application</p>
              <p className="text-sm text-gray-500 mt-2">Evaluating health history, lifestyle, and risk factors...</p>
              <div className="mt-8 max-w-md w-full space-y-3">
                {['Verifying health questionnaire responses', 'Analyzing lifestyle risk factors', 'Calculating risk classification', 'Determining premium rates'].map((step, i) => (
                  <div key={i} className="flex items-center gap-3 text-sm text-gray-600">
                    <svg className="w-4 h-4 text-green-500 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                    </svg>
                    {step}
                  </div>
                ))}
              </div>
            </div>
          )}

          {!processing && result && result.decision === 'approved' && (
            <div>
              <div className="bg-green-50 border border-green-200 rounded-xl p-6 mb-8">
                <div className="flex items-center gap-3 mb-4">
                  <div className="w-12 h-12 bg-green-500 rounded-full flex items-center justify-center">
                    <svg className="w-7 h-7 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                    </svg>
                  </div>
                  <div>
                    <h2 className="text-xl font-extrabold text-green-800">Application Approved</h2>
                    <p className="text-sm text-green-600">Risk Classification: {result.riskClass}</p>
                  </div>
                </div>
                <p className="text-sm text-green-700">
                  Congratulations! Based on the information you provided, your application has been approved.
                  You may proceed to set up your payment and complete the application.
                </p>
              </div>

              <div className="bg-gradient-to-r from-navy to-navy-700 rounded-xl p-6 text-white mb-8">
                <div className="grid grid-cols-2 md:grid-cols-4 gap-6 text-center">
                  <div>
                    <p className="text-xs text-gray-300 uppercase tracking-wider">Monthly Premium</p>
                    <p className="text-3xl font-extrabold mt-1">${premium.toFixed(2)}</p>
                  </div>
                  <div>
                    <p className="text-xs text-gray-300 uppercase tracking-wider">Risk Class</p>
                    <p className="text-lg font-bold mt-1">{result.riskClass}</p>
                  </div>
                  <div>
                    <p className="text-xs text-gray-300 uppercase tracking-wider">Coverage</p>
                    <p className="text-lg font-bold mt-1">${parseInt(state.quote.coverageAmount || '250000').toLocaleString()}</p>
                  </div>
                  <div>
                    <p className="text-xs text-gray-300 uppercase tracking-wider">Term</p>
                    <p className="text-lg font-bold mt-1">{state.quote.termLength === 'whole' ? 'Whole Life' : `${state.quote.termLength} Years`}</p>
                  </div>
                </div>
              </div>

              {result.reasons.length > 0 && (
                <div className="mb-8">
                  <h3 className="font-bold text-navy text-sm mb-3">Underwriting Notes</h3>
                  <ul className="space-y-1">
                    {result.reasons.map((r, i) => (
                      <li key={i} className="text-sm text-gray-600 flex items-start gap-2">
                        <span className="text-amber-500 mt-0.5">&#x2022;</span>
                        {r}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="flex gap-3">
                <button onClick={() => router.push('/life-insurance/apply/payment/')} className="btn-primary">Continue to Payment</button>
                <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
              </div>
            </div>
          )}

          {!processing && result && result.decision === 'referred' && (
            <div>
              <div className="bg-amber-50 border border-amber-200 rounded-xl p-6 mb-8">
                <div className="flex items-center gap-3 mb-4">
                  <div className="w-12 h-12 bg-amber-500 rounded-full flex items-center justify-center">
                    <svg className="w-7 h-7 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01" />
                    </svg>
                  </div>
                  <div>
                    <h2 className="text-xl font-extrabold text-amber-800">Refer to Underwriter (RTU)</h2>
                    <p className="text-sm text-amber-600">Additional Review Required</p>
                  </div>
                </div>
                <p className="text-sm text-amber-700">
                  Based on the information you provided, your application requires additional review by a licensed underwriter.
                  This is not a denial — many RTU applications are approved after manual review. You may still continue the application.
                </p>
              </div>

              <div className="bg-white border border-gray-200 rounded-xl p-5 mb-8">
                <h3 className="font-bold text-navy mb-3">Factors Requiring Review</h3>
                <ul className="space-y-2">
                  {result.reasons.map((r, i) => (
                    <li key={i} className="text-sm text-gray-700 flex items-start gap-2">
                      <svg className="w-4 h-4 text-amber-500 flex-shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01" />
                      </svg>
                      {r}
                    </li>
                  ))}
                </ul>
              </div>

              <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 mb-8">
                <p className="text-sm text-blue-800">
                  <strong>What happens next:</strong> A VKPower underwriter will review your application within 5-7 business days.
                  You may be contacted for additional medical information or records. An estimated premium of{' '}
                  <strong>${premium.toFixed(2)}/month</strong> has been calculated pending final review.
                </p>
              </div>

              <div className="flex gap-3">
                <button onClick={() => router.push('/life-insurance/apply/payment/')} className="btn-primary">Continue Application</button>
                <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
              </div>
            </div>
          )}

          {!processing && result && result.decision === 'declined' && (
            <div>
              <div className="bg-red-50 border border-red-200 rounded-xl p-6 mb-8">
                <div className="flex items-center gap-3 mb-4">
                  <div className="w-12 h-12 bg-red-500 rounded-full flex items-center justify-center">
                    <svg className="w-7 h-7 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </div>
                  <div>
                    <h2 className="text-xl font-extrabold text-red-800">Application Declined</h2>
                    <p className="text-sm text-red-600">Unable to Offer Coverage at This Time</p>
                  </div>
                </div>
                <p className="text-sm text-red-700">
                  We regret that based on the information provided, we are unable to offer life insurance coverage at this time.
                  This decision is based on the combination of risk factors identified during the underwriting process.
                </p>
              </div>

              <div className="bg-white border border-gray-200 rounded-xl p-5 mb-8">
                <h3 className="font-bold text-navy mb-3">Reasons for Decline</h3>
                <ul className="space-y-2">
                  {result.reasons.map((r, i) => (
                    <li key={i} className="text-sm text-gray-700 flex items-start gap-2">
                      <svg className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                      {r}
                    </li>
                  ))}
                </ul>
              </div>

              <div className="bg-gray-50 rounded-xl p-5 mb-8">
                <h3 className="font-bold text-navy mb-2">Your Options</h3>
                <ul className="space-y-2 text-sm text-gray-700">
                  <li>&#x2022; Contact a VKPower representative at <strong>1-800-555-LIFE</strong> to discuss your options</li>
                  <li>&#x2022; Apply for a Guaranteed Issue policy (no medical underwriting, higher premiums)</li>
                  <li>&#x2022; Reapply after addressing identified risk factors (e.g., tobacco cessation, BMI improvement)</li>
                  <li>&#x2022; Request a formal adverse action notice explaining this decision</li>
                </ul>
              </div>

              <div className="flex gap-3">
                <button onClick={() => router.push('/')} className="btn-primary">Return to Home</button>
                <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
              </div>
            </div>
          )}
        </div>
      </main>
    </>
  );
}
