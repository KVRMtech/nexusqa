'use client';

import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';
import { US_STATES } from '@/lib/states';

const APPLY_STEPS = [
  { label: 'Member' }, { label: 'Personal' }, { label: 'Replacement' },
  { label: 'Health (HLQ)' }, { label: 'Lifestyle' }, { label: 'Decision' },
  { label: 'Payment' }, { label: 'Beneficiary' }, { label: 'Signature' }, { label: 'Confirmation' },
];

export default function PersonalInfoPage() {
  const router = useRouter();
  const { state, updatePersonalInfo } = useApp();
  const pi = state.personalInfo;
  const set = (key: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    updatePersonalInfo(key, e.target.value);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    router.push('/life-insurance/apply/replacement/');
  };

  const sameAsOwner = pi.insuredSameAsOwner === 'Yes';

  return (
    <>
      <Header />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={APPLY_STEPS} current={1} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Personal Information</h1>
          <p className="text-sm text-gray-500 mb-8">
            Review and verify the information below. Fields have been pre-populated from your member profile.
          </p>

          <form onSubmit={handleSubmit}>
            <h2 className="section-title">Policy Owner Information</h2>
            <div className="field-grid mb-8">
              <div>
                <label htmlFor="pi_first" className="form-label">First Name</label>
                <input id="pi_first" className="form-input" value={pi.ownerFirstName || ''} onChange={set('ownerFirstName')} required autoComplete="given-name" />
              </div>
              <div>
                <label htmlFor="pi_last" className="form-label">Last Name</label>
                <input id="pi_last" className="form-input" value={pi.ownerLastName || ''} onChange={set('ownerLastName')} required autoComplete="family-name" />
              </div>
              <div>
                <label htmlFor="pi_dob" className="form-label">Date of Birth</label>
                <input id="pi_dob" type="date" className="form-input" value={pi.ownerDob || ''} onChange={set('ownerDob')} required autoComplete="bday" />
              </div>
              <div>
                <label htmlFor="pi_gender" className="form-label">Gender</label>
                <select id="pi_gender" className="form-input" value={pi.ownerGender || ''} onChange={set('ownerGender')} required>
                  <option value="">Select...</option>
                  <option value="Male">Male</option>
                  <option value="Female">Female</option>
                  <option value="Non-binary">Non-binary</option>
                </select>
              </div>
              <div>
                <label htmlFor="pi_ssn" className="form-label">Social Security Number</label>
                <input id="pi_ssn" className="form-input" placeholder="XXX-XX-XXXX" value={pi.ownerSsn || ''} onChange={set('ownerSsn')} required autoComplete="off" />
              </div>
              <div>
                <label htmlFor="pi_citizenship" className="form-label">Citizenship</label>
                <select id="pi_citizenship" className="form-input" value={pi.ownerCitizenship || ''} onChange={set('ownerCitizenship')} required>
                  <option value="">Select...</option>
                  <option value="United States">United States Citizen</option>
                  <option value="Permanent Resident">Permanent Resident</option>
                  <option value="Visa Holder">Visa Holder</option>
                  <option value="Other">Other</option>
                </select>
              </div>
            </div>

            <h2 className="section-title">Contact Information</h2>
            <div className="field-grid mb-8">
              <div>
                <label htmlFor="pi_email" className="form-label">Email Address</label>
                <input id="pi_email" type="email" className="form-input" value={pi.ownerEmail || ''} onChange={set('ownerEmail')} required autoComplete="email" />
              </div>
              <div>
                <label htmlFor="pi_phone" className="form-label">Phone Number</label>
                <input id="pi_phone" type="tel" className="form-input" value={pi.ownerPhone || ''} onChange={set('ownerPhone')} required autoComplete="tel" />
              </div>
            </div>

            <h2 className="section-title">Residential Address</h2>
            <div className="field-grid mb-8">
              <div className="md:col-span-2">
                <label htmlFor="pi_street" className="form-label">Street Address</label>
                <input id="pi_street" className="form-input" value={pi.ownerStreet || ''} onChange={set('ownerStreet')} required autoComplete="address-line1" />
              </div>
              <div>
                <label htmlFor="pi_unit" className="form-label">Apt / Suite / Unit</label>
                <input id="pi_unit" className="form-input" value={pi.ownerUnit || ''} onChange={set('ownerUnit')} autoComplete="address-line2" />
              </div>
              <div>
                <label htmlFor="pi_city" className="form-label">City</label>
                <input id="pi_city" className="form-input" value={pi.ownerCity || ''} onChange={set('ownerCity')} required autoComplete="address-level2" />
              </div>
              <div>
                <label htmlFor="pi_state" className="form-label">State</label>
                <select id="pi_state" className="form-input" value={pi.ownerState || ''} onChange={set('ownerState')} required autoComplete="address-level1">
                  <option value="">Select state...</option>
                  {US_STATES.map(s => <option key={s.code} value={s.code}>{s.name}</option>)}
                </select>
              </div>
              <div>
                <label htmlFor="pi_zip" className="form-label">ZIP Code</label>
                <input id="pi_zip" className="form-input" maxLength={10} value={pi.ownerZip || ''} onChange={set('ownerZip')} required autoComplete="postal-code" />
              </div>
            </div>

            <h2 className="section-title">Employment Information</h2>
            <div className="field-grid mb-8">
              <div>
                <label htmlFor="pi_emp_status" className="form-label">Employment Status</label>
                <select id="pi_emp_status" className="form-input" value={pi.employmentStatus || ''} onChange={set('employmentStatus')} required>
                  <option value="">Select...</option>
                  <option value="Employed">Employed</option>
                  <option value="Self-Employed">Self-Employed</option>
                  <option value="Active Duty">Active Duty Military</option>
                  <option value="Retired">Retired</option>
                  <option value="Unemployed">Unemployed</option>
                  <option value="Student">Student</option>
                  <option value="Homemaker">Homemaker</option>
                </select>
              </div>
              <div>
                <label htmlFor="pi_occupation" className="form-label">Occupation</label>
                <input id="pi_occupation" className="form-input" value={pi.occupation || ''} onChange={set('occupation')} required />
              </div>
              <div>
                <label htmlFor="pi_employer" className="form-label">Employer Name</label>
                <input id="pi_employer" className="form-input" value={pi.employer || ''} onChange={set('employer')} />
              </div>
              <div>
                <label htmlFor="pi_income" className="form-label">Annual Income</label>
                <input id="pi_income" type="number" className="form-input" placeholder="e.g. 75000" value={pi.annualIncome || ''} onChange={set('annualIncome')} required />
              </div>
            </div>

            <h2 className="section-title">Insured Person</h2>
            <div className="mb-6">
              <label className="form-label">Is the insured the same as the policy owner?</label>
              <div className="flex gap-3 mt-1">
                {['Yes', 'No'].map(v => (
                  <button
                    key={v} type="button"
                    onClick={() => updatePersonalInfo('insuredSameAsOwner', v)}
                    className={`px-6 py-2 rounded-lg text-sm font-semibold border transition-colors ${
                      pi.insuredSameAsOwner === v ? 'bg-navy text-white border-navy' : 'bg-white text-gray-600 border-gray-300 hover:border-gray-400'
                    }`}
                  >
                    {v}
                  </button>
                ))}
              </div>
            </div>

            {!sameAsOwner && (
              <div className="field-grid mb-8 p-4 bg-gray-50 rounded-xl">
                <div>
                  <label htmlFor="pi_ins_first" className="form-label">Insured First Name</label>
                  <input id="pi_ins_first" className="form-input" value={pi.insuredFirstName || ''} onChange={set('insuredFirstName')} required />
                </div>
                <div>
                  <label htmlFor="pi_ins_last" className="form-label">Insured Last Name</label>
                  <input id="pi_ins_last" className="form-input" value={pi.insuredLastName || ''} onChange={set('insuredLastName')} required />
                </div>
                <div>
                  <label htmlFor="pi_ins_dob" className="form-label">Insured Date of Birth</label>
                  <input id="pi_ins_dob" type="date" className="form-input" value={pi.insuredDob || ''} onChange={set('insuredDob')} required />
                </div>
                <div>
                  <label htmlFor="pi_ins_gender" className="form-label">Insured Gender</label>
                  <select id="pi_ins_gender" className="form-input" value={pi.insuredGender || ''} onChange={set('insuredGender')} required>
                    <option value="">Select...</option>
                    <option value="Male">Male</option>
                    <option value="Female">Female</option>
                    <option value="Non-binary">Non-binary</option>
                  </select>
                </div>
                <div>
                  <label htmlFor="pi_ins_ssn" className="form-label">Insured SSN</label>
                  <input id="pi_ins_ssn" className="form-input" placeholder="XXX-XX-XXXX" value={pi.insuredSsn || ''} onChange={set('insuredSsn')} required />
                </div>
                <div>
                  <label htmlFor="pi_ins_rel" className="form-label">Relationship to Owner</label>
                  <select id="pi_ins_rel" className="form-input" value={pi.insuredRelationship || ''} onChange={set('insuredRelationship')} required>
                    <option value="">Select...</option>
                    <option value="Spouse">Spouse</option>
                    <option value="Child">Child</option>
                    <option value="Parent">Parent</option>
                    <option value="Business Partner">Business Partner</option>
                    <option value="Other">Other</option>
                  </select>
                </div>
              </div>
            )}

            <div className="mt-8 flex gap-3">
              <button type="submit" className="btn-primary">Continue to Replacement Information</button>
              <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
