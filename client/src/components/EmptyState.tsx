import { Inbox } from 'lucide-react';

interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description: string;
  action?: React.ReactNode;
}

export function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-white/[0.04] ring-1 ring-white/[0.06] mb-4">
        {icon || <Inbox className="h-7 w-7 text-gray-500" />}
      </div>
      <h3 className="text-sm font-semibold text-gray-300">{title}</h3>
      <p className="mt-1 max-w-sm text-xs text-gray-500">{description}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
