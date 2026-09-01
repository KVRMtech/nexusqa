'use client';

import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';
import { PRODUCTS } from '@/lib/states';

const QUOTE_STEPS = [
  { label: 'Product' }, { label: 'Coverage' }, { label: 'Personal' },
  { label: 'Health' }, { label: 'Review' },
];

export default function QuoteStartPage() {
  const router = useRouter();
  const { state, updateQuote } = useApp();
  const selected = state.quote.product || '';

  const handleSelect = (product: string) => {
    updateQuote({ product });
  };

  const handleContinue = (e: React.FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    router.push('/life-insurance/quote/coverage/');
  };

  return (
    <>
      <Header />
      <main className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={QUOTE_STEPS} current={0} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Choose Your Coverage Type</h1>
          <p className="text-sm text-gray-500 mb-8">
            Select the type of life insurance that best fits your needs and financial goals.
          </p>

          <form onSubmit={handleContinue}>
            <div className="space-y-3">
              {PRODUCTS.map(p => (
                <label
                  key={p.value}
                  className={`flex items-start gap-4 p-4 rounded-xl border-2 cursor-pointer transition-all ${
                    selected === p.value
                      ? 'border-navy bg-navy-50 shadow-sm'
                      : 'border-gray-200 hover:border-gray-300 bg-white'
                  }`}
                >
                  <input
                    type="radio" name="product" value={p.value}
                    checked={selected === p.value}
                    onChange={() => handleSelect(p.value)}
                    className="mt-1 w-4 h-4 text-navy accent-navy"
                  />
                  <div>
                    <div className="font-bold text-navy">{p.label}</div>
                    <div className="text-sm text-gray-500 mt-0.5">{p.description}</div>
                  </div>
                </label>
              ))}
            </div>

            <div className="mt-8 flex gap-3">
              <button type="submit" className="btn-primary" disabled={!selected}>
                Continue
              </button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
