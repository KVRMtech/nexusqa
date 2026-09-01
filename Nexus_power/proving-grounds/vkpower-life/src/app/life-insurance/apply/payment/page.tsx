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

export default function PaymentPage() {
  const router = useRouter();
  const { state, updatePayment } = useApp();
  const pay = state.payment;
  const set = (key: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    updatePayment({ [key]: e.target.value });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    router.push('/life-insurance/apply/beneficiary/');
  };

  const method = pay.method || '';
  const isACH = method === 'ach';
  const isCard = method === 'card';

  return (
    <>
      <Header />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={APPLY_STEPS} current={6} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Payment Information</h1>
          <p className="text-sm text-gray-500 mb-8">
            Set up your premium payment method. Your first payment will be processed upon policy issuance.
          </p>

          <form onSubmit={handleSubmit}>
            <h2 className="section-title">Billing Frequency</h2>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-8">
              {[
                { value: 'monthly', label: 'Monthly' },
                { value: 'quarterly', label: 'Quarterly' },
                { value: 'semiannual', label: 'Semi-Annual' },
                { value: 'annual', label: 'Annual' },
              ].map(opt => (
                <button
                  key={opt.value} type="button"
                  onClick={() => updatePayment({ billingFrequency: opt.value })}
                  className={`p-3 rounded-xl border text-sm font-semibold transition-colors ${
                    pay.billingFrequency === opt.value ? 'bg-navy text-white border-navy' : 'bg-white text-gray-600 border-gray-300 hover:border-gray-400'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>

            <h2 className="section-title">Payment Method</h2>
            <div className="flex gap-4 mb-6">
              <button
                type="button"
                onClick={() => updatePayment({ method: 'ach' })}
                className={`flex-1 p-4 rounded-xl border-2 transition-colors ${
                  isACH ? 'border-navy bg-navy/5' : 'border-gray-200 hover:border-gray-300'
                }`}
              >
                <div className="flex items-center gap-3">
                  <div className={`w-5 h-5 rounded-full border-2 flex items-center justify-center ${isACH ? 'border-navy' : 'border-gray-300'}`}>
                    {isACH && <div className="w-2.5 h-2.5 rounded-full bg-navy" />}
                  </div>
                  <div className="text-left">
                    <p className="font-bold text-sm text-navy">ACH Bank Transfer</p>
                    <p className="text-xs text-gray-500">Direct debit from checking or savings</p>
                  </div>
                </div>
              </button>
              <button
                type="button"
                onClick={() => updatePayment({ method: 'card' })}
                className={`flex-1 p-4 rounded-xl border-2 transition-colors ${
                  isCard ? 'border-navy bg-navy/5' : 'border-gray-200 hover:border-gray-300'
                }`}
              >
                <div className="flex items-center gap-3">
                  <div className={`w-5 h-5 rounded-full border-2 flex items-center justify-center ${isCard ? 'border-navy' : 'border-gray-300'}`}>
                    {isCard && <div className="w-2.5 h-2.5 rounded-full bg-navy" />}
                  </div>
                  <div className="text-left">
                    <p className="font-bold text-sm text-navy">Credit / Debit Card</p>
                    <p className="text-xs text-gray-500">Visa, Mastercard, Discover, Amex</p>
                  </div>
                </div>
              </button>
            </div>

            {isACH && (
              <div className="bg-gray-50 rounded-xl p-5 mb-6">
                <h3 className="font-bold text-navy text-sm mb-4">Bank Account Information</h3>
                <div className="field-grid">
                  <div>
                    <label htmlFor="pay_bank" className="form-label">Bank Name</label>
                    <input id="pay_bank" className="form-input" placeholder="e.g. Chase Bank" value={pay.bankName || ''} onChange={set('bankName')} required />
                  </div>
                  <div>
                    <label htmlFor="pay_acct_type" className="form-label">Account Type</label>
                    <select id="pay_acct_type" className="form-input" value={pay.accountType || ''} onChange={set('accountType')} required>
                      <option value="">Select...</option>
                      <option value="Checking">Checking</option>
                      <option value="Savings">Savings</option>
                    </select>
                  </div>
                  <div>
                    <label htmlFor="pay_routing" className="form-label">Routing Number</label>
                    <input id="pay_routing" className="form-input" maxLength={9} inputMode="numeric" placeholder="9-digit routing number" value={pay.routingNumber || ''} onChange={set('routingNumber')} required />
                  </div>
                  <div>
                    <label htmlFor="pay_account" className="form-label">Account Number</label>
                    <input id="pay_account" className="form-input" inputMode="numeric" placeholder="Account number" value={pay.accountNumber || ''} onChange={set('accountNumber')} required />
                  </div>
                </div>
                <p className="text-xs text-gray-400 mt-4">
                  Your routing and account numbers can be found at the bottom of your checks.
                  VKPower Life uses 256-bit encryption to protect your banking information.
                </p>
              </div>
            )}

            {isCard && (
              <div className="bg-gray-50 rounded-xl p-5 mb-6">
                <h3 className="font-bold text-navy text-sm mb-4">Card Information</h3>
                <div className="field-grid">
                  <div className="md:col-span-2">
                    <label htmlFor="pay_cardholder" className="form-label">Cardholder Name</label>
                    <input id="pay_cardholder" className="form-input" placeholder="Name as it appears on card" value={pay.cardholderName || ''} onChange={set('cardholderName')} required autoComplete="cc-name" />
                  </div>
                  <div className="md:col-span-2">
                    <label htmlFor="pay_cardnum" className="form-label">Card Number</label>
                    <input id="pay_cardnum" className="form-input" maxLength={19} inputMode="numeric" placeholder="XXXX XXXX XXXX XXXX" value={pay.cardNumber || ''} onChange={set('cardNumber')} required autoComplete="cc-number" />
                  </div>
                  <div>
                    <label htmlFor="pay_expiry" className="form-label">Expiration Date</label>
                    <input id="pay_expiry" className="form-input" placeholder="MM/YY" maxLength={5} value={pay.cardExpiry || ''} onChange={set('cardExpiry')} required autoComplete="cc-exp" />
                  </div>
                  <div>
                    <label htmlFor="pay_cvv" className="form-label">CVV</label>
                    <input id="pay_cvv" className="form-input" maxLength={4} inputMode="numeric" placeholder="3 or 4 digits" value={pay.cardCvv || ''} onChange={set('cardCvv')} required autoComplete="cc-csc" />
                  </div>
                </div>
                <p className="text-xs text-gray-400 mt-4">
                  Your card information is encrypted and processed through PCI DSS Level 1 certified systems.
                  We do not store your full card number.
                </p>
              </div>
            )}

            {!method && (
              <div className="bg-gray-50 rounded-xl p-8 text-center text-gray-400 text-sm mb-6">
                Select a payment method above to continue.
              </div>
            )}

            <div className="flex gap-3">
              <button type="submit" className="btn-primary" disabled={!method}>Continue to Beneficiary Designation</button>
              <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
