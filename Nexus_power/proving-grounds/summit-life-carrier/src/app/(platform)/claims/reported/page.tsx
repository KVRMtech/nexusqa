'use client';

import { useRouter } from 'next/navigation';
import { ColumnDef } from '@tanstack/react-table';
import { DataTable } from '@/components/domain/data-table';
import { PageTransition } from '@/components/domain/page-transition';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { claims } from '@/lib/data';
import { formatCurrency, formatDate } from '@/lib/utils';
import type { Claim } from '@/lib/types';
import { ClipboardList, Plus } from 'lucide-react';
import Link from 'next/link';

const statusVariant: Record<string, 'default' | 'success' | 'warning' | 'destructive' | 'info' | 'secondary'> = {
  reported: 'warning', under_investigation: 'info', pending_documents: 'destructive', in_review: 'warning', approved: 'success', denied: 'destructive', closed: 'secondary',
};

const columns: ColumnDef<Claim>[] = [
  { accessorKey: 'claimNumber', header: 'Claim Number', cell: ({ row }) => <span className="font-mono text-sm font-medium">{row.original.claimNumber}</span> },
  { id: 'insured', header: 'Insured', accessorFn: row => `${row.insured.lastName}, ${row.insured.firstName}`, cell: ({ row }) => (
    <div><div className="font-medium">{row.original.insured.lastName}, {row.original.insured.firstName}</div><div className="text-xs text-muted-foreground">Policy: {row.original.policyNumber}</div></div>
  )},
  { accessorKey: 'type', header: 'Type', cell: ({ row }) => <Badge variant="outline">{row.original.type.replace(/_/g, ' ')}</Badge> },
  { accessorKey: 'faceAmount', header: 'Face Amount', cell: ({ row }) => <span className="font-mono">{formatCurrency(row.original.faceAmount)}</span> },
  { accessorKey: 'status', header: 'Status', cell: ({ row }) => <Badge variant={statusVariant[row.original.status] || 'default'}>{row.original.status.replace(/_/g, ' ')}</Badge> },
  { accessorKey: 'isContestable', header: 'Contestable', cell: ({ row }) => row.original.isContestable ? <Badge variant="destructive">Yes</Badge> : <Badge variant="success">No</Badge> },
  { accessorKey: 'reportedAt', header: 'Reported', cell: ({ row }) => <span className="text-sm text-muted-foreground">{formatDate(row.original.reportedAt)}</span> },
];

export default function ReportedClaimsPage() {
  const router = useRouter();
  return (
    <PageTransition>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <ClipboardList className="h-6 w-6 text-gold" />
            <div>
              <h1 className="text-2xl font-bold tracking-tight">Reported Claims</h1>
              <p className="text-muted-foreground">Claims under investigation and pending adjudication</p>
            </div>
          </div>
          <Link href="/claims/reported/new-fnol"><Button variant="gold"><Plus className="mr-2 h-4 w-4" />New FNOL</Button></Link>
        </div>
        <DataTable columns={columns} data={claims} searchKey="insured" searchPlaceholder="Search claims..." onRowClick={row => router.push(`/claims/reported/${row.id}/investigation`)} />
      </div>
    </PageTransition>
  );
}
