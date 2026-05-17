// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Modal Component
// ═══════════════════════════════════════════════════════════════
import { useEffect, useRef, type ReactNode } from 'react';
import { X } from 'lucide-react';
import clsx from 'clsx';

interface ModalProps {
  /** Whether the modal is open */
  open: boolean;
  /** Called when the modal requests to close */
  onClose: () => void;
  /** Modal title in the header */
  title?: string;
  /** Modal description below title */
  description?: string;
  /** Modal width: sm (480px), md (640px), lg (768px), xl (1024px), full */
  size?: 'sm' | 'md' | 'lg' | 'xl' | 'full';
  /** Whether clicking the backdrop closes the modal */
  closeOnBackdrop?: boolean;
  /** Footer content (action buttons) */
  footer?: ReactNode;
  /** Body content */
  children: ReactNode;
}

const sizeClasses: Record<string, string> = {
  sm: 'max-w-[480px]',
  md: 'max-w-[640px]',
  lg: 'max-w-[768px]',
  xl: 'max-w-[1024px]',
  full: 'max-w-[calc(100vw-3rem)]',
};

export function Modal({
  open,
  onClose,
  title,
  description,
  size = 'md',
  closeOnBackdrop = true,
  footer,
  children,
}: ModalProps) {
  const overlayRef = useRef<HTMLDivElement>(null);

  // Close on Escape key
  useEffect(() => {
    if (!open) return;
    const handle = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handle);
    return () => document.removeEventListener('keydown', handle);
  }, [open, onClose]);

  // Trap focus & prevent body scroll
  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => { document.body.style.overflow = ''; };
  }, [open]);

  if (!open) return null;

  return (
    <div
      ref={overlayRef}
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      onClick={(e) => {
        if (closeOnBackdrop && e.target === overlayRef.current) onClose();
      }}
    >
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black/70 backdrop-blur-sm animate-[fade-in_0.15s_ease-out]" />

      {/* Panel */}
      <div
        className={clsx(
          'relative w-full rounded-xl bg-white shadow-2xl ring-1 ring-white/[0.08] animate-[slide-in_0.2s_ease-out]',
          sizeClasses[size],
        )}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        {/* Header */}
        {(title || description) && (
          <div className="flex items-start justify-between gap-4 border-b border-gray-200 px-6 py-4">
            <div>
              {title && <h2 className="text-lg font-semibold text-[#0a2540]">{title}</h2>}
              {description && (
                <p className="mt-0.5 text-sm text-slate-500">{description}</p>
              )}
            </div>
            <button
              onClick={onClose}
              className="rounded-md p-1.5 text-slate-500 hover:text-[#0a2540] hover:bg-white/[0.06] transition-colors"
              aria-label="Close"
            >
              <X className="h-5 w-5" />
            </button>
          </div>
        )}

        {/* Body */}
        <div className="max-h-[calc(80vh-8rem)] overflow-y-auto px-6 py-4">{children}</div>

        {/* Footer */}
        {footer && (
          <div className="flex items-center justify-end gap-3 border-t border-gray-200 px-6 py-4">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
