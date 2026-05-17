// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Status Badge Component
// ═══════════════════════════════════════════════════════════════
import clsx from 'clsx';

type Variant =
  | 'success' | 'warning' | 'error' | 'info' | 'nexus' | 'gray'
  | 'green' | 'yellow' | 'red' | 'blue' | 'purple' | 'orange';

const variantMap: Record<Variant, string> = {
  success: 'badge-green',
  green: 'badge-green',
  warning: 'badge-yellow',
  yellow: 'badge-yellow',
  error: 'badge-red',
  red: 'badge-red',
  info: 'badge-blue',
  blue: 'badge-blue',
  nexus: 'badge-nexus',
  purple: 'badge-purple',
  gray: 'badge-gray',
  orange: 'badge-orange',
};

interface StatusBadgeProps {
  /** Display text */
  label: string;
  /** Color variant */
  variant?: Variant;
  /** Show a pulsing dot indicator */
  pulse?: boolean;
  /** Optional icon on the left */
  icon?: React.ReactNode;
  /** Additional CSS classes */
  className?: string;
}

/**
 * Reusable badge component that replaces scattered inline badge styling.
 * Maps semantic names (success, warning, error, etc.) to the Tailwind @layer badge classes.
 */
export function StatusBadge({ label, variant = 'gray', pulse, icon, className }: StatusBadgeProps) {
  return (
    <span className={clsx(variantMap[variant], className)}>
      {pulse && (
        <span className="relative mr-1.5 flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-75" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-current" />
        </span>
      )}
      {icon && <span className="mr-1 flex items-center">{icon}</span>}
      {label}
    </span>
  );
}
