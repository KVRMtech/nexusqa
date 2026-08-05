'use client';

import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';

const APPLY_STEPS = [
  { label: 'Member' }, { label: 'Personal' }, { label: 'Replacement' },
  { label: 'Health (HLQ)' }, { label: 'Lifestyle' }, { label: 'Decision' },
  { label: 'Payment' }, { label: 'Beneficiary' }, { label: 'Signature' }, { label: 'Confirmation' },
];

function YesNo({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="flex gap-2">
      {['Yes', 'No'].map(v => (
        <button
          key={v} type="button" onClick={() => onChange(v)}
          className={`px-4 py-1.5 rounded-lg text-xs font-semibold border transition-colors ${
            value === v ? (v === 'Yes' ? 'bg-amber-500 text-white border-amber-500' : 'bg-navy text-white border-navy')
              : 'bg-white text-gray-500 border-gray-300 hover:border-gray-400'
          }`}
        >
          {v}
        </button>
      ))}
    </div>
  );
}

function LsQuestion({ id, text, children }: { id: string; text: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-start gap-2 sm:gap-4 py-3 border-b border-gray-100 last:border-0">
      <label htmlFor={id} className="flex-1 text-sm text-gray-800 font-medium leading-snug">{text}</label>
      <div className="sm:min-w-[200px] flex-shrink-0">{children}</div>
    </div>
  );
}

export default function LifestylePage() {
  const router = useRouter();
  const { state, setLifestyleAnswer } = useApp();
  const ls = state.lifestyleAnswers;
  const set = (id: string) => (v: string) => setLifestyleAnswer(id, v);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    router.push('/life-insurance/apply/decision/');
  };

  return (
    <>
      <Header />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={APPLY_STEPS} current={4} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Lifestyle Questions</h1>
          <p className="text-sm text-gray-500 mb-8">
            The following questions help us assess lifestyle-related risk factors for your application.
          </p>

          <form onSubmit={handleSubmit}>
            {/* TOBACCO */}
            <h2 className="section-title">Tobacco &amp; Nicotine</h2>
            <div className="mb-6">
              <LsQuestion id="ls_tobacco" text="Have you used cigarettes, cigars, pipe tobacco, chewing tobacco, or nicotine products (including e-cigarettes/vaping) in the past 12 months?">
                <YesNo id="ls_tobacco" value={ls.ls_tobacco || ''} onChange={set('ls_tobacco')} />
              </LsQuestion>
              {ls.ls_tobacco === 'Yes' && (
                <div className="ml-6 pl-4 border-l-2 border-amber-200 space-y-3 mt-2 bg-amber-50/50 rounded-r-lg p-3">
                  <div>
                    <label htmlFor="ls_tobacco_type" className="form-label text-sm">Type of tobacco/nicotine product</label>
                    <select id="ls_tobacco_type" className="form-input text-sm" value={ls.ls_tobacco_type || ''} onChange={e => setLifestyleAnswer('ls_tobacco_type', e.target.value)}>
                      <option value="">Select...</option>
                      <option value="Cigarettes">Cigarettes</option>
                      <option value="Cigars">Cigars</option>
                      <option value="Pipe">Pipe Tobacco</option>
                      <option value="Chewing">Chewing Tobacco/Snuff</option>
                      <option value="Vaping">E-Cigarettes/Vaping</option>
                      <option value="Nicotine Patches">Nicotine Patches/Gum</option>
                      <option value="Multiple">Multiple Products</option>
                    </select>
                  </div>
                  <div>
                    <label htmlFor="ls_tobacco_freq" className="form-label text-sm">Frequency of use</label>
                    <select id="ls_tobacco_freq" className="form-input text-sm" value={ls.ls_tobacco_freq || ''} onChange={e => setLifestyleAnswer('ls_tobacco_freq', e.target.value)}>
                      <option value="">Select...</option>
                      <option value="Daily">Daily</option>
                      <option value="Several times per week">Several times per week</option>
                      <option value="Occasionally">Occasionally (less than weekly)</option>
                      <option value="Quit within 12 months">Recently quit (within 12 months)</option>
                    </select>
                  </div>
                </div>
              )}
            </div>

            {/* ALCOHOL */}
            <h2 className="section-title">Alcohol</h2>
            <div className="mb-6">
              <LsQuestion id="ls_alcohol" text="Do you consume alcoholic beverages?">
                <YesNo id="ls_alcohol" value={ls.ls_alcohol || ''} onChange={set('ls_alcohol')} />
              </LsQuestion>
              {ls.ls_alcohol === 'Yes' && (
                <div className="ml-6 pl-4 border-l-2 border-amber-200 space-y-3 mt-2 bg-amber-50/50 rounded-r-lg p-3">
                  <div>
                    <label htmlFor="ls_alcohol_freq" className="form-label text-sm">Average drinks per week</label>
                    <select id="ls_alcohol_freq" className="form-input text-sm" value={ls.ls_alcohol_freq || ''} onChange={e => setLifestyleAnswer('ls_alcohol_freq', e.target.value)}>
                      <option value="">Select...</option>
                      <option value="1-3">1-3 drinks per week</option>
                      <option value="4-7">4-7 drinks per week</option>
                      <option value="8-14">8-14 drinks per week</option>
                      <option value="15+">15 or more drinks per week</option>
                    </select>
                  </div>
                  <LsQuestion id="ls_alcohol_treatment" text="Have you ever been treated for alcohol abuse or attended AA or similar programs?">
                    <YesNo id="ls_alcohol_treatment" value={ls.ls_alcohol_treatment || ''} onChange={set('ls_alcohol_treatment')} />
                  </LsQuestion>
                </div>
              )}
            </div>

            {/* DRUGS */}
            <h2 className="section-title">Substance Use</h2>
            <div className="mb-6">
              <LsQuestion id="ls_marijuana" text="Have you used marijuana or CBD products in the past 12 months?">
                <YesNo id="ls_marijuana" value={ls.ls_marijuana || ''} onChange={set('ls_marijuana')} />
              </LsQuestion>
              <LsQuestion id="ls_drugs" text="Have you used any recreational or illicit drugs (cocaine, heroin, methamphetamine, unprescribed opioids, etc.) in the past 5 years?">
                <YesNo id="ls_drugs" value={ls.ls_drugs || ''} onChange={set('ls_drugs')} />
              </LsQuestion>
              {ls.ls_drugs === 'Yes' && (
                <div className="ml-6 pl-4 border-l-2 border-amber-200 mt-2 bg-amber-50/50 rounded-r-lg p-3">
                  <label htmlFor="ls_drugs_detail" className="form-label text-sm">Describe substance and last use date</label>
                  <textarea id="ls_drugs_detail" className="form-input text-sm min-h-[60px]" value={ls.ls_drugs_detail || ''} onChange={e => setLifestyleAnswer('ls_drugs_detail', e.target.value)} />
                </div>
              )}
              <LsQuestion id="ls_drug_treatment" text="Have you ever been treated in a rehabilitation facility or program for substance abuse?">
                <YesNo id="ls_drug_treatment" value={ls.ls_drug_treatment || ''} onChange={set('ls_drug_treatment')} />
              </LsQuestion>
            </div>

            {/* DRIVING */}
            <h2 className="section-title">Driving History</h2>
            <div className="mb-6">
              <LsQuestion id="ls_dui" text="Have you had any DUI, DWI, or reckless driving convictions in the past 10 years?">
                <YesNo id="ls_dui" value={ls.ls_dui || ''} onChange={set('ls_dui')} />
              </LsQuestion>
              {ls.ls_dui === 'Yes' && (
                <div className="ml-6 pl-4 border-l-2 border-amber-200 space-y-3 mt-2 bg-amber-50/50 rounded-r-lg p-3">
                  <div>
                    <label htmlFor="ls_dui_count" className="form-label text-sm">Number of DUI/DWI convictions</label>
                    <select id="ls_dui_count" className="form-input text-sm" value={ls.ls_dui_count || ''} onChange={e => setLifestyleAnswer('ls_dui_count', e.target.value)}>
                      <option value="">Select...</option>
                      <option value="1">1</option>
                      <option value="2">2</option>
                      <option value="3+">3 or more</option>
                    </select>
                  </div>
                  <div>
                    <label htmlFor="ls_dui_date" className="form-label text-sm">Date of most recent conviction</label>
                    <input id="ls_dui_date" type="date" className="form-input text-sm" value={ls.ls_dui_date || ''} onChange={e => setLifestyleAnswer('ls_dui_date', e.target.value)} />
                  </div>
                </div>
              )}
              <LsQuestion id="ls_license_suspended" text="Has your driver's license been suspended or revoked in the past 5 years?">
                <YesNo id="ls_license_suspended" value={ls.ls_license_suspended || ''} onChange={set('ls_license_suspended')} />
              </LsQuestion>
              <LsQuestion id="ls_moving_violations" text="Have you received more than 3 moving violations in the past 3 years?">
                <YesNo id="ls_moving_violations" value={ls.ls_moving_violations || ''} onChange={set('ls_moving_violations')} />
              </LsQuestion>
            </div>

            {/* HAZARDOUS ACTIVITIES */}
            <h2 className="section-title">Hazardous Activities &amp; Hobbies</h2>
            <div className="mb-6">
              <LsQuestion id="ls_aviation" text="Do you pilot or intend to pilot any aircraft (other than as a fare-paying passenger)?">
                <YesNo id="ls_aviation" value={ls.ls_aviation || ''} onChange={set('ls_aviation')} />
              </LsQuestion>
              {ls.ls_aviation === 'Yes' && (
                <div className="ml-6 pl-4 border-l-2 border-amber-200 space-y-3 mt-2 bg-amber-50/50 rounded-r-lg p-3">
                  <div>
                    <label htmlFor="ls_aviation_type" className="form-label text-sm">Pilot type</label>
                    <select id="ls_aviation_type" className="form-input text-sm" value={ls.ls_aviation_type || ''} onChange={e => setLifestyleAnswer('ls_aviation_type', e.target.value)}>
                      <option value="">Select...</option>
                      <option value="Private Pilot">Private Pilot</option>
                      <option value="Commercial Pilot">Commercial Pilot (occupation)</option>
                      <option value="Military Pilot">Military Pilot</option>
                      <option value="Student Pilot">Student Pilot</option>
                    </select>
                  </div>
                  <div>
                    <label htmlFor="ls_aviation_hours" className="form-label text-sm">Annual flight hours</label>
                    <input id="ls_aviation_hours" type="number" className="form-input text-sm" placeholder="e.g. 100" value={ls.ls_aviation_hours || ''} onChange={e => setLifestyleAnswer('ls_aviation_hours', e.target.value)} />
                  </div>
                </div>
              )}
              <LsQuestion id="ls_skydiving" text="Do you participate in skydiving, base jumping, or paragliding?">
                <YesNo id="ls_skydiving" value={ls.ls_skydiving || ''} onChange={set('ls_skydiving')} />
              </LsQuestion>
              <LsQuestion id="ls_scuba" text="Do you participate in scuba diving?">
                <YesNo id="ls_scuba" value={ls.ls_scuba || ''} onChange={set('ls_scuba')} />
              </LsQuestion>
              {ls.ls_scuba === 'Yes' && (
                <div className="ml-6 pl-4 border-l-2 border-amber-200 mt-2 bg-amber-50/50 rounded-r-lg p-3">
                  <LsQuestion id="ls_scuba_depth" text="Do you dive deeper than 100 feet or in caves/wrecks?">
                    <YesNo id="ls_scuba_depth" value={ls.ls_scuba_depth || ''} onChange={set('ls_scuba_depth')} />
                  </LsQuestion>
                </div>
              )}
              <LsQuestion id="ls_racing" text="Do you participate in motor vehicle, motorcycle, or boat racing?">
                <YesNo id="ls_racing" value={ls.ls_racing || ''} onChange={set('ls_racing')} />
              </LsQuestion>
              <LsQuestion id="ls_climbing" text="Do you participate in rock climbing, mountaineering, or similar activities at heights above 14,000 feet?">
                <YesNo id="ls_climbing" value={ls.ls_climbing || ''} onChange={set('ls_climbing')} />
              </LsQuestion>
            </div>

            {/* TRAVEL */}
            <h2 className="section-title">International Travel</h2>
            <div className="mb-6">
              <LsQuestion id="ls_travel" text="Do you plan to travel to or reside in any foreign country for more than 3 months in the next 2 years?">
                <YesNo id="ls_travel" value={ls.ls_travel || ''} onChange={set('ls_travel')} />
              </LsQuestion>
              {ls.ls_travel === 'Yes' && (
                <div className="ml-6 pl-4 border-l-2 border-amber-200 mt-2 bg-amber-50/50 rounded-r-lg p-3">
                  <label htmlFor="ls_travel_countries" className="form-label text-sm">Countries and duration of planned travel</label>
                  <textarea id="ls_travel_countries" className="form-input text-sm min-h-[60px]" placeholder="e.g. Germany - 6 months, business assignment" value={ls.ls_travel_countries || ''} onChange={e => setLifestyleAnswer('ls_travel_countries', e.target.value)} />
                </div>
              )}
              <LsQuestion id="ls_combat" text="Are you currently deployed to or expecting deployment to a combat zone or hazardous duty location?">
                <YesNo id="ls_combat" value={ls.ls_combat || ''} onChange={set('ls_combat')} />
              </LsQuestion>
            </div>

            {/* LEGAL */}
            <h2 className="section-title">Legal History</h2>
            <div className="mb-6">
              <LsQuestion id="ls_felony" text="Have you ever been convicted of a felony?">
                <YesNo id="ls_felony" value={ls.ls_felony || ''} onChange={set('ls_felony')} />
              </LsQuestion>
              {ls.ls_felony === 'Yes' && (
                <div className="ml-6 pl-4 border-l-2 border-amber-200 mt-2 bg-amber-50/50 rounded-r-lg p-3">
                  <label htmlFor="ls_felony_detail" className="form-label text-sm">Describe the conviction and date</label>
                  <textarea id="ls_felony_detail" className="form-input text-sm min-h-[60px]" value={ls.ls_felony_detail || ''} onChange={e => setLifestyleAnswer('ls_felony_detail', e.target.value)} />
                </div>
              )}
              <LsQuestion id="ls_bankruptcy" text="Have you filed for bankruptcy in the past 10 years?">
                <YesNo id="ls_bankruptcy" value={ls.ls_bankruptcy || ''} onChange={set('ls_bankruptcy')} />
              </LsQuestion>
            </div>

            <div className="flex gap-3">
              <button type="submit" className="btn-primary">Continue to Underwriting Decision</button>
              <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
