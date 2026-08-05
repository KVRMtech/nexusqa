'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';
import { lookupMember, getDefaultProfile } from '@/lib/members';

const APPLY_STEPS = [
  { label: 'Member' }, { label: 'Personal' }, { label: 'Replacement' },
  { label: 'Health (HLQ)' }, { label: 'Lifestyle' }, { label: 'Decision' },
  { label: 'Payment' }, { label: 'Beneficiary' }, { label: 'Signature' }, { label: 'Confirmation' },
];

export default function MemberLookupPage() {
  const router = useRouter();
  const { state, login, setProfile, setPersonalInfo } = useApp();
  const [memberNumber, setMemberNumber] = useState(state.memberNumber || '');
  const [found, setFound] = useState<ReturnType<typeof lookupMember>>(null);
  const [searched, setSearched] = useState(false);

  const handleLookup = () => {
    const mn = memberNumber.trim();
    if (!mn) return;
    const profile = lookupMember(mn);
    setFound(profile);
    setSearched(true);
  };

  const handleContinue = (e: React.FormEvent) => {
    e.preventDefault();
    const mn = memberNumber.trim() || '25000001';
    const profile = found || getDefaultProfile(mn);
    login(mn);
    setProfile(profile);

    setPersonalInfo({
      ownerFirstName: profile.firstName,
      ownerLastName: profile.lastName,
      ownerDob: profile.dateOfBirth,
      ownerGender: profile.gender,
      ownerSsn: profile.ssn,
      ownerEmail: profile.email,
      ownerPhone: profile.phone,
      ownerStreet: profile.address.street,
      ownerUnit: profile.address.unit,
      ownerCity: profile.address.city,
      ownerState: profile.address.state,
      ownerZip: profile.address.zip,
      ownerCitizenship: profile.citizenship,
      ownerResidency: profile.residencyState,
      employmentStatus: profile.employment.status,
      occupation: profile.employment.occupation,
      employer: profile.employment.employer,
      annualIncome: profile.employment.annualIncome,
      insuredSameAsOwner: 'Yes',
    });

    router.push('/life-insurance/apply/personal-info/');
  };

  return (
    <>
      <Header />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={APPLY_STEPS} current={0} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Member Lookup</h1>
          <p className="text-sm text-gray-500 mb-8">
            Enter your VKPower member number to automatically retrieve your profile and
            pre-populate the application with your information on file.
          </p>

          <form onSubmit={handleContinue}>
            <div className="max-w-sm">
              <label htmlFor="app_member" className="form-label">Member Number</label>
              <div className="flex gap-3">
                <input
                  id="app_member" className="form-input" inputMode="numeric"
                  placeholder="e.g. 25000001"
                  value={memberNumber} onChange={e => setMemberNumber(e.target.value)}
                />
                <button type="button" onClick={handleLookup} className="btn-outline whitespace-nowrap">
                  Look Up
                </button>
              </div>
            </div>

            {searched && found && (
              <div className="mt-6 bg-green-50 border border-green-200 rounded-xl p-5">
                <div className="flex items-center gap-2 mb-3">
                  <svg className="w-5 h-5 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span className="font-bold text-green-800">Member Found</span>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-sm">
                  <div><span className="text-gray-500">Name:</span> <span className="font-medium">{found.firstName} {found.lastName}</span></div>
                  <div><span className="text-gray-500">Age:</span> <span className="font-medium">{found.age}</span></div>
                  <div><span className="text-gray-500">Gender:</span> <span className="font-medium">{found.gender}</span></div>
                  <div><span className="text-gray-500">Status:</span> <span className="font-medium">{found.militaryStatus}</span></div>
                  <div><span className="text-gray-500">Location:</span> <span className="font-medium">{found.address.city}, {found.address.state}</span></div>
                  <div><span className="text-gray-500">Policy:</span> <span className="font-medium">{found.policyNumber}</span></div>
                </div>
                <p className="text-xs text-green-700 mt-3">
                  Your profile will be used to pre-populate the application. You can review and edit all information on the next page.
                </p>
              </div>
            )}

            {searched && !found && (
              <div className="mt-6 bg-amber-50 border border-amber-200 rounded-xl p-5">
                <div className="flex items-center gap-2 mb-2">
                  <svg className="w-5 h-5 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.072 16.5c-.77.833.192 2.5 1.732 2.5z" />
                  </svg>
                  <span className="font-bold text-amber-800">Member Not Found</span>
                </div>
                <p className="text-sm text-amber-700">
                  No existing profile found for member {memberNumber}. You can still proceed — all information will need to be entered manually.
                </p>
              </div>
            )}

            <div className="mt-8 flex gap-3">
              <button type="submit" className="btn-primary">Continue to Personal Information</button>
              <button type="button" className="btn-outline" onClick={() => router.back()}>Back to Quote</button>
            </div>
          </form>

          <div className="mt-6 pt-4 border-t border-gray-100">
            <p className="text-xs text-gray-400">
              Available members: <strong>25000001</strong> (Maria Young, Officer),{' '}
              <strong>25000002</strong> (James Torres, Enlisted),{' '}
              <strong>50000001</strong> (Robert Mitchell, Veteran),{' '}
              <strong>50000005</strong> (John Senior, Civilian),{' '}
              <strong>75000001</strong> (Patricia Warren, Retired).
            </p>
          </div>
        </div>
      </main>
    </>
  );
}
