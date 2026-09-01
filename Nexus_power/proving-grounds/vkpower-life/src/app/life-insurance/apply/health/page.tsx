'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Header from '@/components/Header';
import StepIndicator from '@/components/StepIndicator';
import { useApp } from '@/lib/store';
import { HEALTH_CATEGORIES, getQuestionsByCategory } from '@/lib/health-questions';
import type { HealthQuestion } from '@/lib/types';

const APPLY_STEPS = [
  { label: 'Member' }, { label: 'Personal' }, { label: 'Replacement' },
  { label: 'Health (HLQ)' }, { label: 'Lifestyle' }, { label: 'Decision' },
  { label: 'Payment' }, { label: 'Beneficiary' }, { label: 'Signature' }, { label: 'Confirmation' },
];

function QuestionInput({ q, value, onChange }: { q: HealthQuestion; value: string; onChange: (v: string) => void }) {
  const id = `hlq_${q.id}`;

  if (q.type === 'yesno') {
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

  if (q.type === 'select' && q.options) {
    return (
      <select id={id} className="form-input text-sm" value={value} onChange={e => onChange(e.target.value)}>
        <option value="">Select...</option>
        {q.options.map(o => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  }

  if (q.type === 'textarea') {
    return (
      <textarea
        id={id} className="form-input text-sm min-h-[60px] resize-y"
        placeholder={q.placeholder || ''} value={value} onChange={e => onChange(e.target.value)}
      />
    );
  }

  if (q.type === 'date') {
    return <input id={id} type="date" className="form-input text-sm" value={value} onChange={e => onChange(e.target.value)} />;
  }

  if (q.type === 'number') {
    return <input id={id} type="number" className="form-input text-sm" placeholder={q.placeholder || ''} value={value} onChange={e => onChange(e.target.value)} />;
  }

  return <input id={id} className="form-input text-sm" placeholder={q.placeholder || ''} value={value} onChange={e => onChange(e.target.value)} />;
}

export default function HealthQuestionnairePage() {
  const router = useRouter();
  const { state, setHealthAnswer } = useApp();
  const answers = state.healthAnswers;
  const [expandedCategory, setExpandedCategory] = useState<string>(HEALTH_CATEGORIES[0]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    router.push('/life-insurance/apply/lifestyle/');
  };

  const answeredInCategory = (cat: string) => {
    const qs = getQuestionsByCategory(cat, answers);
    const primary = qs.filter(q => !q.dependsOn);
    const answered = primary.filter(q => answers[q.id]);
    return { total: primary.length, answered: answered.length };
  };

  return (
    <>
      <Header />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <StepIndicator steps={APPLY_STEPS} current={3} />
        <div className="card">
          <h1 className="text-2xl font-extrabold text-navy mb-2">Health Questionnaire (HLQ)</h1>
          <p className="text-sm text-gray-500 mb-2">
            Complete the following medical history questions. Answer each question honestly and completely.
            Selecting &ldquo;Yes&rdquo; will reveal follow-up questions for additional detail.
          </p>
          <p className="text-xs text-amber-600 mb-8">
            This questionnaire contains approximately 150 questions across 14 medical categories.
            All information is kept strictly confidential and is used solely for underwriting purposes.
          </p>

          <form onSubmit={handleSubmit}>
            <div className="space-y-2 mb-8">
              {HEALTH_CATEGORIES.map(cat => {
                const isOpen = expandedCategory === cat;
                const progress = answeredInCategory(cat);
                const questions = getQuestionsByCategory(cat, answers);

                return (
                  <div key={cat} className="border border-gray-200 rounded-xl overflow-hidden">
                    <button
                      type="button"
                      onClick={() => setExpandedCategory(isOpen ? '' : cat)}
                      className="w-full flex items-center justify-between px-5 py-4 bg-gray-50 hover:bg-gray-100 transition-colors text-left"
                    >
                      <div className="flex items-center gap-3">
                        <svg className={`w-4 h-4 text-gray-400 transition-transform ${isOpen ? 'rotate-90' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                        </svg>
                        <span className="font-bold text-navy text-sm">{cat}</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-gray-400">
                          {progress.answered}/{progress.total} answered
                        </span>
                        {progress.answered === progress.total && progress.total > 0 && (
                          <svg className="w-4 h-4 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                          </svg>
                        )}
                      </div>
                    </button>

                    {isOpen && (
                      <div className="px-5 py-4 space-y-4">
                        {questions.map(q => {
                          const isFollowUp = !!q.dependsOn;
                          return (
                            <div
                              key={q.id}
                              className={`${isFollowUp ? 'ml-6 pl-4 border-l-2 border-amber-200 bg-amber-50/50 rounded-r-lg p-3' : ''}`}
                            >
                              <div className="flex flex-col sm:flex-row sm:items-start gap-2 sm:gap-4">
                                <label htmlFor={`hlq_${q.id}`} className="flex-1 text-sm text-gray-800 leading-snug font-medium">
                                  {q.text}
                                </label>
                                <div className="sm:min-w-[200px] flex-shrink-0">
                                  <QuestionInput
                                    q={q}
                                    value={answers[q.id] || ''}
                                    onChange={v => setHealthAnswer(q.id, v)}
                                  />
                                </div>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 mb-6">
              <p className="text-sm text-blue-800">
                <strong>Note:</strong> You do not need to answer every question to proceed. Unanswered questions
                will be addressed during the underwriting review process. However, completing all questions
                may expedite your application.
              </p>
            </div>

            <div className="flex gap-3">
              <button type="submit" className="btn-primary">Continue to Lifestyle Questions</button>
              <button type="button" className="btn-outline" onClick={() => router.back()}>Back</button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
