// ═══════════════════════════════════════════════════════════════
//  VKPOWER AI ENGINE FACTORY — Components Barrel Export
// ═══════════════════════════════════════════════════════════════

// Error Boundaries
export { GlobalErrorBoundary } from './GlobalErrorBoundary';
export { RouteErrorBoundary } from './RouteErrorBoundary';

// Layout & Navigation
export { PageHeader } from './PageHeader';

// Data Display
export { StatCard } from './StatCard';
export { StatusBadge } from './StatusBadge';
export { DataTable } from './DataTable';
export { ProgressBar } from './ProgressBar';
export { Skeleton, StatCardSkeleton, TableRowSkeleton, CardSkeleton, PageSkeleton } from './Skeleton';

// Phase 3 — Visual Evidence Inspector
export { ConfidenceChip, bandForScore } from './ConfidenceChip';
export type { ConfidenceLevel } from './ConfidenceChip';
export { SceneFrameWithOverlays, normaliseBox } from './SceneFrameWithOverlays';
export type { NormalisedBox } from './SceneFrameWithOverlays';
export { AppTimeline } from './AppTimeline';

// Inputs & Controls
export { SearchInput } from './SearchInput';
export { FilterBar } from './FilterBar';
export { Tabs } from './Tabs';

// Feedback & Overlay
export { Modal } from './Modal';
export { ConfirmDialog } from './ConfirmDialog';
export { NotificationCenter } from './NotificationCenter';

// Legacy (kept for backward compatibility)
export { EmptyState } from './EmptyState';
export { LiveBadge } from './LiveBadge';

// Type re-exports
export type { Column } from './DataTable';
export type { FilterOption } from './FilterBar';
export type { Tab } from './Tabs';
