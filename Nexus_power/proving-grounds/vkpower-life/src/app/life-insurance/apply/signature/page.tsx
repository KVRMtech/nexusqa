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

const CONSENTS = [
  {
    id: 'accuracy',
    text: 'I certify that all statements and answers provided in this application are complete and true to the best of my knowledge and belief. I understand that any misrepresentation or omission may result in denial of a claim or rescission of the policy.',
  },
  {
    id: 'authorization',
    text: 'I authorize VKPower Life Insurance Company to obtain information from medical professionals, hospitals, pharmacies, the MIB Group, Inc., and other sources to evaluate this application. This authorization is valid for 24 months from the date signed.',
  },
  {
    id: 'hipaa',
    text: 'I acknowledge receipt of the HIPAA Privacy Notice and authorize the release of protected health information (PHI) to VKPower Life Insurance Company and its authorized representatives for the purpose of underwriting this application.',
  },
  {
    id: 'replacement',
    text: 'I understand that if this policy replaces existing insurance, a comparison of the existing and proposed policies has been or will be provided, and I have reviewed or will review the information before the free-look period expires.',
  },
  {
    id: 'electronic',
    text: 'I consent to conducting this transaction electronically, including the use of electronic signatures and electronic delivery of policy documents. I understand that I may request paper copies at any time by contacting VKPower Life.',
  },
  {
    id: 'fraud',
    text: 'I understand that any person who knowingly presents a false or fraudulent claim for payment of a loss or benefit, or knowingly presents false information in an application for insurance, is guilty of a crime and may be subject to fines and confinement.',
  },
];

export default function SignaturePage() {
  const router = useRouter();
  const { state, sign } = useApp();
  const pi = state.personalInfo;
  const [consents, setConsents] = useState<Record<string, boolean>>({});
  const [typedName, setTypedName] = useState('');
  const [error, setError] = useState('');

  const allConsented = CONSENTS.every(c => consents[c.id]);
  const expectedName = `${pi.ownerFirstName || ''} ${pi.ownerLastName || ''}`.trim();
  const nameMatch = typedName.trim().toLowerCase() === expectedName.toLowerCase();

  const toggle = (id: string) => setConsents(prev => ({ ...prev, [id]: !prev[id] }));

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!allConsented) {
      setError('You must acknowledge all statements before signing.');
      return;
    }
    if (!nameMatch) {
      setError(`Your typed signature must match your legal name: "${expectedName}".`);
      return;
    }
    sign(new Date().toISOString());
    router.push('/life-insurance/apply/confirmation/');
  };

  return (
    <>
      <Header />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={APPLY_STEPS} current={8} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Electronic Signature</h1>
          <p className="text-sm text-gray-500 mb-8">
            Review the statements below and provide your electronic signature to complete the application.
            By signing, you acknowledge all statements and authorize VKPower Life to process your application.
          </p>

          <form onSubmit={handleSubmit}>
            <h2 className="section-title">Application Summary</h2>
            <div className="bg-gray-50 rounded-xl p-5 mb-8">
              <div className="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm">
                <div><span className="text-gray-500">Applicant:</span> <span className="font-medium">{expectedName}</span></div>
                <div><span className="text-gray-500">Product:</span> <span className="font-medium">{state.quote.product || 'Term Life'}</span></div>
                <div><span className="text-gray-500">Coverage:</span> <span className="font-medium">${parseInt(state.quote.coverageAmount || '250000').toLocaleString()}</span></div>
                <div><span className="text-gray-500">Term:</span> <span className="font-medium">{state.quote.termLength === 'whole' ? 'Whole Life' : `${state.quote.termLength || '20'} Years`}</span></div>
                <div><span className="text-gray-500">Decision:</span> <span className={`font-medium ${state.decision === 'approved' ? 'text-green-700' : state.decision === 'referred' ? 'text-amber-700' : 'text-red-700'}`}>{state.decision === 'approved' ? 'Approved' : state.decision === 'referred' ? 'Referred to Underwriter' : 'Declined'}</span></div>
                <div><span className="text-gray-500">Beneficiaries:</span> <span className="font-medium">{state.beneficiaries.length} designated</span></div>
                <div><span className="text-gray-500">Payment:</span> <span className="font-medium">{state.payment.method === 'ach' ? 'ACH Bank Transfer' : 'Credit/Debit Card'} — {state.payment.billingFrequency || 'Monthly'}</span></div>
              </div>
            </div>

            <h2 className="section-title">Acknowledgements &amp; Consents</h2>
            <div className="space-y-4 mb-8">
              {CONSENTS.map(c => (
                <label key={c.id} className="flex gap-3 items-start cursor-pointer group">
                  <input
                    type="checkbox"
                    checked={!!consents[c.id]}
                    onChange={() => toggle(c.id)}
                    className="mt-1 h-4 w-4 rounded border-gray-300 text-navy focus:ring-gold accent-navy"
                  />
                  <span className="text-sm text-gray-700 leading-relaxed group-hover:text-gray-900">{c.text}</span>
                </label>
              ))}
            </div>

            <h2 className="section-title">Electronic Signature</h2>
            <div className="bg-gray-50 rounded-xl p-5 mb-6">
              <p className="text-sm text-gray-600 mb-4">
                Type your full legal name below to sign this application electronically. Your typed name
                must exactly match: <strong>{expectedName}</strong>.
              </p>
              <div className="max-w-md">
                <label htmlFor="sig_name" className="form-label">Signature</label>
                <input
                  id="sig_name" className="form-input text-lg font-serif italic"
                  placeholder="Type your full legal name"
                  value={typedName} onChange={e => setTypedName(e.target.value)}
                  required autoComplete="off"
                />
              </div>
              {typedName && nameMatch && (
                <div className="mt-3 flex items-center gap-2">
                  <svg className="w-4 h-4 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                  </svg>
                  <span className="text-xs text-green-700 font-medium">Signature verified</span>
                </div>
              )}
              {typedName && !nameMatch && (
                <div className="mt-3 flex items-center gap-2">
                  <svg className="w-4 h-4 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                  <span className="text-xs text-red-600 font-medium">Name does not match. Please type: &ldquo;{expectedName}&rdquo;</span>
                </div>
              )}
            </div>

            <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 mb-6">
              <p className="text-xs text-blue-800">
                By submitting this application with your electronic signature, you agree that your typed name
                has the same legal effect as a handwritten signature under the Electronic Signatures in Global
                and National Commerce (E-SIGN) Act and the Uniform Electronic Transactions Act (UETA).
              </p>
            </div>

            {error && (
              <div className="bg-red-50 border border-red-200 rounded-lg p-3 mb-6">
                <p className="text-sm text-red-700 font-medium">{error}</p>
              </div>
            )}

            <div className="flex gap-3">
              <button type="submit" className="btn-gold" disabled={!allConsented || !nameMatch}>
                Sign &amp; Submit Application
              </button>
              <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
