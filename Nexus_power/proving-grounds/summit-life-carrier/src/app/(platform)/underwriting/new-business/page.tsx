'use client';

import { useRouter } from 'next/navigation';
import { ColumnDef } from '@tanstack/react-table';
import { DataTable } from '@/components/domain/data-table';
import { PageTransition } from '@/components/domain/page-transition';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { applications } from '@/lib/data';
import { formatCurrency, formatDate } from '@/lib/utils';
import type { Application } from '@/lib/types';
import { FileSearch, Plus } from 'lucide-react';

const statusVariant: Record<string, 'default' | 'success' | 'warning' | 'destructive' | 'info' | 'secondary'> = {
  submitted: 'info', in_review: 'warning', requirements_pending: 'destructive', underwriting: 'warning', approved: 'success', approved_rated: 'success', declined: 'destructive', counter_offer: 'secondary', postponed: 'secondary', withdrawn: 'secondary',
};

const columns: ColumnDef<Application>[] = [
  { accessorKey: 'caseNumber', header: 'Case Number', cell: ({ row }) => <span className="font-mono text-sm font-medium">{row.original.caseNumber}</span> },
  { id: 'applicant', header: 'Applicant', accessorFn: row => `${row.applicant.lastName}, ${row.applicant.firstName}`, cell: ({ row }) => (
    <div>
      <div className="font-medium">{row.original.applicant.lastName}, {row.original.applicant.firstName}</div>
      <div className="text-xs text-muted-foreground">{row.original.applicant.occupation}</div>
    </div>
  )},
  { accessorKey: 'product', header: 'Product', cell: ({ row }) => <span className="text-sm">{row.original.product.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}</span> },
  { accessorKey: 'faceAmount', header: 'Face Amount', cell: ({ row }) => <span className="font-mono">{formatCurrency(row.original.faceAmount)}</span> },
  { accessorKey: 'status', header: 'Status', cell: ({ row }) => <Badge variant={statusVariant[row.original.status] || 'default'}>{row.original.status.replace(/_/g, ' ')}</Badge> },
  { accessorKey: 'assignedUnderwriter', header: 'Underwriter', cell: ({ row }) => {
    const names: Record<string, string> = { 'usr-001': 'M. Chen', 'usr-002': 'D. Kowalski', 'usr-003': 'P. Sharma' };
    return <span className="text-sm">{names[row.original.assignedUnderwriter] || row.original.assignedUnderwriter}</span>;
  }},
  { accessorKey: 'submittedAt', header: 'Submitted', cell: ({ row }) => <span className="text-sm text-muted-foreground">{formatDate(row.original.submittedAt)}</span> },
];

export default function NewBusinessQueuePage() {
  const router = useRouter();

  return (
    <PageTransition>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <FileSearch className="h-6 w-6 text-gold" />
            <div>
              <h1 className="text-2xl font-bold tracking-tight">New Business Queue</h1>
              <p className="text-muted-foreground">Underwriting applications awaiting review and decision</p>
            </div>
          </div>
          <Button variant="gold"><Plus className="mr-2 h-4 w-4" />New Application</Button>
        </div>

        <DataTable
          columns={columns}
          data={applications}
          searchKey="applicant"
          searchPlaceholder="Search applicants..."
          onRowClick={row => router.push(`/underwriting/new-business/${row.id}/review`)}
        />
      </div>
    </PageTransition>
  );
}
