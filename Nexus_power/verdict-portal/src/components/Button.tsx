/** Button — the shared action primitive (typed, accessible, loading-aware). */
import { forwardRef } from 'react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';

import { cn } from '../lib/format';
import { Spinner } from './states';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  icon?: ReactNode;
  children?: ReactNode;
}

const VARIANT: Record<ButtonVariant, string> = {
  primary: 'bg-teal text-bg hover:bg-teal/90 ring-1 ring-teal/40 font-semibold',
  secondary: 'bg-panel-2 text-ink hover:bg-line-strong ring-1 ring-line-strong',
  ghost: 'bg-transparent text-ink-mid hover:text-ink hover:bg-ink/[0.05] ring-1 ring-transparent',
  danger: 'bg-transparent text-crit hover:bg-crit/10 ring-1 ring-crit/40 font-semibold',
};

const SIZE: Record<ButtonSize, string> = {
  sm: 'text-xs px-2.5 py-1.5 gap-1.5',
  md: 'text-sm px-3.5 py-2 gap-2',
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', loading = false, icon, disabled, className, children, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(
        'inline-flex items-center justify-center rounded-lg leading-none whitespace-nowrap transition-colors',
        'disabled:opacity-50 disabled:cursor-not-allowed',
        VARIANT[variant],
        SIZE[size],
        className,
      )}
      {...rest}
    >
      {loading ? <Spinner size={14} className="text-current" /> : icon != null ? <span className="shrink-0 inline-flex">{icon}</span> : null}
      {children}
    </button>
  );
});

export default Button;
