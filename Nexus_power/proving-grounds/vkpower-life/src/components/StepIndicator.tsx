'use client';

interface Step {
  label: string;
  href?: string;
}

interface Props {
  steps: Step[];
  current: number;
}

export default function StepIndicator({ steps, current }: Props) {
  return (
    <nav aria-label="Progress" className="mb-8">
      <ol className="flex items-center gap-1 text-xs sm:text-sm overflow-x-auto pb-2">
        {steps.map((step, i) => {
          const isActive = i === current;
          const isDone = i < current;
          return (
            <li key={i} className="flex items-center whitespace-nowrap">
              {i > 0 && (
                <svg className="w-4 h-4 text-gray-300 mx-1 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                </svg>
              )}
              <span
                className={`flex items-center gap-1.5 px-2 py-1 rounded-full transition-colors ${
                  isActive
                    ? 'bg-navy text-white font-bold'
                    : isDone
                      ? 'text-green-700 font-medium'
                      : 'text-gray-400'
                }`}
              >
                {isDone && (
                  <svg className="w-3.5 h-3.5 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                  </svg>
                )}
                <span className="hidden sm:inline">{i + 1}.</span> {step.label}
              </span>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
