'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import { useApp } from '@/lib/store';
import { lookupMember, getDefaultProfile } from '@/lib/members';

type LoginStep = 'credentials' | 'pin';

export default function LoginPage() {
  const router = useRouter();
  const { login, setProfile } = useApp();
  const [step, setStep] = useState<LoginStep>('credentials');
  const [memberNumber, setMemberNumber] = useState('');
  const [password, setPassword] = useState('');
  const [pin, setPin] = useState('');
  const [error, setError] = useState('');
  const [useMfa, setUseMfa] = useState(true);

  const handleCredentials = (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    const mn = memberNumber.trim() || '25000001';
    if (!mn) { setError('Please enter your member number.'); return; }
    if (useMfa) {
      setMemberNumber(mn);
      setStep('pin');
    } else {
      const profile = lookupMember(mn) || getDefaultProfile(mn);
      login(mn);
      setProfile(profile);
      router.push('/portal/dashboard/');
    }
  };

  const handlePin = (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    if (pin.length < 4) { setError('Enter your 6-digit security PIN.'); return; }
    const mn = memberNumber.trim() || '25000001';
    const profile = lookupMember(mn) || getDefaultProfile(mn);
    login(mn);
    setProfile(profile);
    router.push('/portal/dashboard/');
  };

  return (
    <>
      <Header />
      <main className="flex-1 flex items-start justify-center pt-16 pb-20 px-4">
        <div className="card w-full max-w-md">
          {step === 'credentials' ? (
            <>
              <h1 className="text-2xl font-extrabold text-navy mb-1">
                {useMfa ? 'Secure Sign In' : 'Sign In'}
              </h1>
              <p className="text-sm text-gray-500 mb-6">
                {useMfa
                  ? 'Member number and password, then a one-time security PIN.'
                  : 'Access your VKPower Life account.'}
              </p>
              <form onSubmit={handleCredentials} noValidate>
                <div className="space-y-4">
                  <div>
                    <label htmlFor="member_number" className="form-label">Member number</label>
                    <input
                      id="member_number" name="member_number"
                      className="form-input" inputMode="numeric" autoComplete="username"
                      placeholder="e.g. 25000001"
                      value={memberNumber} onChange={e => setMemberNumber(e.target.value)}
                    />
                  </div>
                  <div>
                    <label htmlFor="password" className="form-label">Password</label>
                    <input
                      id="password" name="password" type="password"
                      className="form-input" autoComplete="current-password"
                      value={password} onChange={e => setPassword(e.target.value)}
                    />
                  </div>
                </div>
                {error && <p className="text-sm text-red-600 mt-3">{error}</p>}
                <div className="mt-6 flex flex-col gap-3">
                  <button type="submit" className="btn-primary w-full py-3">
                    {useMfa ? 'Continue' : 'Sign in'}
                  </button>
                  <button
                    type="button"
                    onClick={() => setUseMfa(!useMfa)}
                    className="text-sm text-navy-500 hover:text-navy underline text-center"
                  >
                    {useMfa ? 'Sign in without PIN' : 'Sign in with security PIN'}
                  </button>
                </div>
              </form>
              <div className="mt-6 pt-4 border-t border-gray-100">
                <p className="text-xs text-gray-400 leading-relaxed">
                  Demo members: <strong>25000001</strong> (young officer),{' '}
                  <strong>25000002</strong> (enlisted),{' '}
                  <strong>50000001</strong> (veteran),{' '}
                  <strong>50000005</strong> (civilian),{' '}
                  <strong>75000001</strong> (retired). Any password accepted.
                </p>
              </div>
            </>
          ) : (
            <>
              <h1 className="text-2xl font-extrabold text-navy mb-1">Security Check</h1>
              <p className="text-sm text-gray-500 mb-6">
                Enter the 6-digit PIN for member {memberNumber}.
              </p>
              <form onSubmit={handlePin} noValidate>
                <div>
                  <label htmlFor="pin" className="form-label">Security PIN</label>
                  <input
                    id="pin" name="pin"
                    className="form-input text-center text-xl tracking-[0.5em] font-mono"
                    inputMode="numeric" maxLength={6} autoComplete="one-time-code"
                    placeholder="••••••"
                    value={pin} onChange={e => setPin(e.target.value.replace(/\D/g, '').slice(0, 6))}
                    autoFocus
                  />
                </div>
                {error && <p className="text-sm text-red-600 mt-3">{error}</p>}
                <div className="mt-6 flex gap-3">
                  <button type="submit" className="btn-primary flex-1 py-3">
                    Verify &amp; Sign In
                  </button>
                  <button
                    type="button"
                    onClick={() => { setStep('credentials'); setPin(''); setError(''); }}
                    className="btn-outline"
                  >
                    Back
                  </button>
                </div>
              </form>
              <p className="text-xs text-gray-400 mt-4">Any 6-digit PIN is accepted in this demonstration.</p>
            </>
          )}
        </div>
      </main>
    </>
  );
}
