// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Confirm Dialog Component
// ═══════════════════════════════════════════════════════════════
import { AlertTriangle, CheckCircle, Info, XCircle } from 'lucide-react';
import { Modal } from './Modal';
import clsx from 'clsx';

interface ConfirmDialogProps {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: string;
  /** Visual style of the dialog */
  variant?: 'danger' | 'warning' | 'info' | 'success';
  confirmLabel?: string;
  cancelLabel?: string;
  isLoading?: boolean;
}

const iconMap = {
  danger: <XCircle className="h-6 w-6 text-red-400" />,
  warning: <AlertTriangle className="h-6 w-6 text-yellow-400" />,
  info: <Info className="h-6 w-6 text-blue-400" />,
  success: <CheckCircle className="h-6 w-6 text-green-400" />,
};

const btnStyleMap = {
  danger: 'btn-danger',
  warning: 'btn-primary',
  info: 'btn-primary',
  success: 'btn-primary',
};

export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  message,
  variant = 'danger',
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  isLoading = false,
}: ConfirmDialogProps) {
  return (
    <Modal open={open} onClose={onClose} size="sm">
      <div className="flex flex-col items-center text-center py-2">
        <div className="mb-4">{iconMap[variant]}</div>
        <h3 className="text-lg font-semibold text-[#0a2540]">{title}</h3>
        <p className="mt-2 text-sm text-slate-500">{message}</p>
        <div className="mt-6 flex items-center gap-3">
          <button onClick={onClose} className="btn-secondary" disabled={isLoading}>
            {cancelLabel}
          </button>
          <button
            onClick={onConfirm}
            className={clsx(btnStyleMap[variant], isLoading && 'opacity-50 cursor-not-allowed')}
            disabled={isLoading}
          >
            {isLoading ? 'Processing…' : confirmLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}
