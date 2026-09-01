'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';

const APPLY_STEPS = [
  { label: 'Member' }, { label: 'Personal' }, { label: 'Replacement' },
  { label: 'Health (HLQ)' }, { label: 'Lifestyle' }, { label: 'Decision' },
  { label: 'Payment' }, { label: 'Beneficiary' }, { label: 'Signature' }, { label: 'Confirmation' },
];

function genId(prefix: string) {
  const d = new Date();
  const base = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`;
  const rand = Math.random().toString(36).substring(2, 8).toUpperCase();
  return `${prefix}-${base}-${rand}`;
}

export default function ConfirmationPage() {
  const router = useRouter();
  const { state, setConfirmation, resetApplication } = useApp();
  const pi = state.personalInfo;
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!state.applicationNumber) {
      setConfirmation(genId('VKPL'), genId('CONF'));
    }
    setReady(true);
  }, []);

  if (!ready) return null;

  const applicantName = `${pi.ownerFirstName || ''} ${pi.ownerLastName || ''}`.trim();
  const signedDate = state.signedAt ? new Date(state.signedAt).toLocaleString() : '';

  const handleNewApplication = () => {
    resetApplication();
    router.push('/');
  };

  return (
    <>
      <Header />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={APPLY_STEPS} current={9} />
        <div className="card text-center">
          <div className="inline-flex items-center justify-center w-20 h-20 bg-green-500 rounded-full mb-6">
            <svg className="w-10 h-10 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
            </svg>
          </div>
          <h1 className="text-3xl font-extrabold text-navy mb-2">Application Submitted</h1>
          <p className="text-sm text-gray-500 mb-8 max-w-md mx-auto">
            Your life insurance application has been successfully submitted and is now being processed.
            Please retain your confirmation details below for your records.
          </p>

          <div className="bg-gradient-to-r from-navy to-navy-700 rounded-xl p-6 text-white text-left mb-8 max-w-lg mx-auto">
            <div className="grid grid-cols-1 gap-4">
              <div>
                <p className="text-xs text-gray-300 uppercase tracking-wider">Application Number</p>
                <p className="text-xl font-mono font-bold mt-1">{state.applicationNumber}</p>
              </div>
              <div>
                <p className="text-xs text-gray-300 uppercase tracking-wider">Confirmation Number</p>
                <p className="text-xl font-mono font-bold mt-1">{state.confirmationNumber}</p>
              </div>
              <div className="grid grid-cols-2 gap-4 pt-2 border-t border-white/20">
                <div>
                  <p className="text-xs text-gray-300 uppercase tracking-wider">Applicant</p>
                  <p className="text-sm font-medium mt-1">{applicantName}</p>
                </div>
                <div>
                  <p className="text-xs text-gray-300 uppercase tracking-wider">Submitted</p>
                  <p className="text-sm font-medium mt-1">{signedDate}</p>
                </div>
              </div>
            </div>
          </div>

          <div className="bg-gray-50 rounded-xl p-6 text-left mb-8 max-w-lg mx-auto">
            <h2 className="font-bold text-navy mb-4">Policy Details</h2>
            <div className="grid grid-cols-2 gap-y-3 gap-x-6 text-sm">
              <div className="text-gray-500">Product</div>
              <div className="font-medium">{state.quote.product || 'Term Life Insurance'}</div>
              <div className="text-gray-500">Coverage Amount</div>
              <div className="font-medium">${parseInt(state.quote.coverageAmount || '250000').toLocaleString()}</div>
              <div className="text-gray-500">Term Length</div>
              <div className="font-medium">{state.quote.termLength === 'whole' ? 'Whole Life' : `${state.quote.termLength || '20'} Years`}</div>
              <div className="text-gray-500">Decision</div>
              <div className={`font-medium ${state.decision === 'approved' ? 'text-green-700' : 'text-amber-700'}`}>
                {state.decision === 'approved' ? 'Approved' : 'Referred to Underwriter'}
              </div>
              <div className="text-gray-500">Estimated Premium</div>
              <div className="font-medium">${state.quote.estimatedPremium?.toFixed(2) || '—'}/mo</div>
              <div className="text-gray-500">Payment Method</div>
              <div className="font-medium">{state.payment.method === 'ach' ? 'ACH Bank Transfer' : 'Credit/Debit Card'}</div>
              <div className="text-gray-500">Billing Frequency</div>
              <div className="font-medium capitalize">{state.payment.billingFrequency || 'Monthly'}</div>
              <div className="text-gray-500">Primary Beneficiaries</div>
              <div className="font-medium">{state.beneficiaries.filter(b => b.type === 'primary').length} designated</div>
            </div>
          </div>

          <div className="bg-blue-50 border border-blue-200 rounded-xl p-5 text-left mb-8 max-w-lg mx-auto">
            <h3 className="font-bold text-blue-900 mb-3">What Happens Next</h3>
            <ol className="space-y-2 text-sm text-blue-800">
              <li className="flex gap-2">
                <span className="font-bold text-blue-600">1.</span>
                <span>A confirmation email has been sent to <strong>{pi.ownerEmail || 'your email address'}</strong>.</span>
              </li>
              <li className="flex gap-2">
                <span className="font-bold text-blue-600">2.</span>
                <span>Your application will be reviewed by our underwriting team within <strong>5-7 business days</strong>.</span>
              </li>
              <li className="flex gap-2">
                <span className="font-bold text-blue-600">3.</span>
                <span>You may be contacted for additional medical records or a paramedical exam.</span>
              </li>
              <li className="flex gap-2">
                <span className="font-bold text-blue-600">4.</span>
                <span>Once approved, your policy documents will be delivered electronically or by mail.</span>
              </li>
              <li className="flex gap-2">
                <span className="font-bold text-blue-600">5.</span>
                <span>You will have a <strong>30-day free-look period</strong> to review your policy after delivery.</span>
              </li>
            </ol>
          </div>

          <div className="flex flex-col sm:flex-row gap-3 justify-center max-w-lg mx-auto">
            <button onClick={() => window.print()} className="btn-outline flex-1">
              Print Confirmation
            </button>
            <button onClick={() => router.push('/portal/dashboard/')} className="btn-primary flex-1">
              Go to Dashboard
            </button>
            <button onClick={handleNewApplication} className="btn-outline flex-1">
              New Application
            </button>
          </div>
        </div>
      </main>
    </>
  );
}
