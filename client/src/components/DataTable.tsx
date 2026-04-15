// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Data Table Component
// ═══════════════════════════════════════════════════════════════
import { useState, useMemo, type ReactNode } from 'react';
import { ChevronUp, ChevronDown, ChevronsUpDown, ChevronLeft, ChevronRight } from 'lucide-react';
import clsx from 'clsx';
import { TableRowSkeleton } from './Skeleton';

export interface Column<T> {
  /** Unique key for the column */
  key: string;
  /** Header label */
  header: string;
  /** Accessor function to get the cell value */
  accessor: (row: T) => ReactNode;
  /** Raw value for sorting (defaults to accessor) */
  sortValue?: (row: T) => string | number;
  /** Whether this column is sortable */
  sortable?: boolean;
  /** Width class (e.g., "w-32", "min-w-[200px]") */
  width?: string;
  /** Alignment */
  align?: 'left' | 'center' | 'right';
}

interface DataTableProps<T> {
  columns: Column<T>[];
  data: T[];
  /** Unique key accessor for each row */
  rowKey: (row: T) => string;
  /** Loading state */
  loading?: boolean;
  /** Number of skeleton rows to show while loading */
  loadingRows?: number;
  /** Empty state message */
  emptyMessage?: string;
  /** Empty state icon */
  emptyIcon?: ReactNode;
  /** Enable pagination */
  pageSize?: number;
  /** Expandable row content */
  expandedContent?: (row: T) => ReactNode;
  /** Row click handler */
  onRowClick?: (row: T) => void;
  /** Currently expanded row keys */
  expandedKeys?: Set<string>;
  /** Toggle expanded row */
  onToggleExpand?: (key: string) => void;
  /** Additional table class */
  className?: string;
  /** Whether to stripe odd rows */
  striped?: boolean;
}

type SortDir = 'asc' | 'desc' | null;

export function DataTable<T>({
  columns,
  data,
  rowKey,
  loading = false,
  loadingRows = 5,
  emptyMessage = 'No data available',
  emptyIcon,
  pageSize,
  expandedContent,
  onRowClick,
  expandedKeys,
  onToggleExpand,
  className,
  striped = false,
}: DataTableProps<T>) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>(null);
  const [page, setPage] = useState(0);

  const handleSort = (key: string) => {
    if (sortKey === key) {
      setSortDir((prev) => (prev === 'asc' ? 'desc' : prev === 'desc' ? null : 'asc'));
      if (sortDir === 'desc') setSortKey(null);
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  };

  const sortedData = useMemo(() => {
    if (!sortKey || !sortDir) return data;
    const col = columns.find((c) => c.key === sortKey);
    if (!col) return data;
    const getValue = col.sortValue || ((row: T) => {
      const v = col.accessor(row);
      return typeof v === 'string' || typeof v === 'number' ? v : String(v);
    });
    return [...data].sort((a, b) => {
      const va = getValue(a);
      const vb = getValue(b);
      const cmp = va < vb ? -1 : va > vb ? 1 : 0;
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [data, sortKey, sortDir, columns]);

  const totalPages = pageSize ? Math.max(1, Math.ceil(sortedData.length / pageSize)) : 1;
  const displayData = pageSize ? sortedData.slice(page * pageSize, (page + 1) * pageSize) : sortedData;

  const alignClass = (a?: string) =>
    a === 'center' ? 'text-center' : a === 'right' ? 'text-right' : 'text-left';

  return (
    <div className={clsx('overflow-hidden rounded-xl ring-1 ring-white/[0.06]', className)}>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-white/[0.06]">
          <thead className="bg-white/[0.02]">
            <tr>
              {columns.map((col) => (
                <th
                  key={col.key}
                  className={clsx(
                    'px-4 py-3 table-header select-none',
                    alignClass(col.align),
                    col.width,
                    col.sortable && 'cursor-pointer hover:text-gray-300 transition-colors',
                  )}
                  onClick={col.sortable ? () => handleSort(col.key) : undefined}
                >
                  <span className="inline-flex items-center gap-1">
                    {col.header}
                    {col.sortable && (
                      <span className="text-gray-600">
                        {sortKey === col.key && sortDir === 'asc' ? (
                          <ChevronUp className="h-3.5 w-3.5" />
                        ) : sortKey === col.key && sortDir === 'desc' ? (
                          <ChevronDown className="h-3.5 w-3.5" />
                        ) : (
                          <ChevronsUpDown className="h-3.5 w-3.5" />
                        )}
                      </span>
                    )}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.04]">
            {loading ? (
              Array.from({ length: loadingRows }).map((_, i) => (
                <TableRowSkeleton key={i} cols={columns.length} />
              ))
            ) : displayData.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="px-4 py-12 text-center">
                  <div className="flex flex-col items-center gap-2 text-gray-500">
                    {emptyIcon}
                    <p className="text-sm">{emptyMessage}</p>
                  </div>
                </td>
              </tr>
            ) : (
              displayData.map((row) => {
                const key = rowKey(row);
                const isExpanded = expandedKeys?.has(key) ?? false;

                return (
                  <ExpandableRow
                    key={key}
                    row={row}
                    columns={columns}
                    isExpanded={isExpanded}
                    expandedContent={expandedContent}
                    onRowClick={onRowClick}
                    onToggleExpand={onToggleExpand ? () => onToggleExpand(key) : undefined}
                    striped={striped}
                    alignClass={alignClass}
                  />
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {pageSize && totalPages > 1 && (
        <div className="flex items-center justify-between border-t border-white/[0.06] px-4 py-3 bg-white/[0.02]">
          <p className="text-xs text-gray-500">
            Showing {page * pageSize + 1}–{Math.min((page + 1) * pageSize, sortedData.length)} of{' '}
            {sortedData.length}
          </p>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0}
              className="btn-ghost p-1.5"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <span className="text-xs text-gray-400 px-2">
              {page + 1} / {totalPages}
            </span>
            <button
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
              disabled={page >= totalPages - 1}
              className="btn-ghost p-1.5"
            >
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* --- Internal: Expandable Row --- */
function ExpandableRow<T>({
  row,
  columns,
  isExpanded,
  expandedContent,
  onRowClick,
  onToggleExpand,
  striped,
  alignClass,
}: {
  row: T;
  columns: Column<T>[];
  isExpanded: boolean;
  expandedContent?: (row: T) => ReactNode;
  onRowClick?: (row: T) => void;
  onToggleExpand?: () => void;
  striped: boolean;
  alignClass: (a?: string) => string;
}) {
  const clickable = !!(onRowClick || onToggleExpand);
  return (
    <>
      <tr
        className={clsx(
          'transition-colors',
          clickable && 'cursor-pointer hover:bg-white/[0.03]',
          striped && 'odd:bg-white/[0.015]',
          isExpanded && 'bg-white/[0.03]',
        )}
        onClick={() => {
          if (onToggleExpand) onToggleExpand();
          else if (onRowClick) onRowClick(row);
        }}
      >
        {columns.map((col) => (
          <td key={col.key} className={clsx('px-4 py-3 text-sm', alignClass(col.align))}>
            {col.accessor(row)}
          </td>
        ))}
      </tr>
      {isExpanded && expandedContent && (
        <tr>
          <td colSpan={columns.length} className="bg-white/[0.02] px-6 py-4">
            {expandedContent(row)}
          </td>
        </tr>
      )}
    </>
  );
}
