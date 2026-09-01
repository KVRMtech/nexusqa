'use client';

import { useRouter } from 'next/navigation';
import { ColumnDef } from '@tanstack/react-table';
import { DataTable } from '@/components/domain/data-table';
import { PageTransition } from '@/components/domain/page-transition';
import { Badge } from '@/components/ui/badge';
import { policies } from '@/lib/data';
import { formatCurrency, formatDate } from '@/lib/utils';
import type { Policy } from '@/lib/types';
import { Shield } from 'lucide-react';

const statusVariant: Record<string, 'success' | 'destructive' | 'warning' | 'secondary'> = {
  active: 'success', lapsed: 'destructive', surrendered: 'destructive', paid_up: 'warning', death_claim: 'secondary',
};

const columns: ColumnDef<Policy>[] = [
  { accessorKey: 'policyNumber', header: 'Policy Number', cell: ({ row }) => <span className="font-mono text-sm font-medium">{row.original.policyNumber}</span> },
  { id: 'insured', header: 'Insured', accessorFn: row => `${row.insured.lastName}, ${row.insured.firstName}`, cell: ({ row }) => (
    <div><div className="font-medium">{row.original.insured.lastName}, {row.original.insured.firstName}</div><div className="text-xs text-muted-foreground">DOB: {formatDate(row.original.insured.dateOfBirth)}</div></div>
  )},
  { accessorKey: 'product', header: 'Product', cell: ({ row }) => <span className="text-sm">{row.original.product.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}</span> },
  { accessorKey: 'faceAmount', header: 'Face Amount', cell: ({ row }) => <span className="font-mono">{formatCurrency(row.original.faceAmount)}</span> },
  { accessorKey: 'premiumAmount', header: 'Premium', cell: ({ row }) => <span className="font-mono text-sm">{formatCurrency(row.original.premiumAmount)}/{row.original.premiumMode.replace('_', '-')}</span> },
  { accessorKey: 'cashValue', header: 'Cash Value', cell: ({ row }) => <span className="font-mono text-sm">{formatCurrency(row.original.cashValue)}</span> },
  { accessorKey: 'status', header: 'Status', cell: ({ row }) => <Badge variant={statusVariant[row.original.status] || 'default'}>{row.original.status.replace(/_/g, ' ')}</Badge> },
  { accessorKey: 'effectiveDate', header: 'Effective', cell: ({ row }) => <span className="text-sm text-muted-foreground">{formatDate(row.original.effectiveDate)}</span> },
];

export default function InForcePoliciesPage() {
  const router = useRouter();
  return (
    <PageTransition>
      <div className="space-y-6">
        <div className="flex items-center gap-3">
          <Shield className="h-6 w-6 text-gold" />
          <div>
            <h1 className="text-2xl font-bold tracking-tight">In-Force Policies</h1>
            <p className="text-muted-foreground">Active policy contracts and administration</p>
          </div>
        </div>
        <DataTable columns={columns} data={policies} searchKey="insured" searchPlaceholder="Search policies..." onRowClick={row => router.push(`/policy-admin/in-force/${row.id}`)} />
      </div>
    </PageTransition>
  );
}
