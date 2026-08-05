'use client';

import { useParams, useRouter } from 'next/navigation';
import { policies } from '@/lib/data';
import { formatCurrency, formatDate } from '@/lib/utils';
import { PageTransition } from '@/components/domain/page-transition';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Separator } from '@/components/ui/separator';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ArrowLeft, Shield, Users, DollarSign, FileText, Plus } from 'lucide-react';
import Link from 'next/link';

export default function PolicyDetailPage() {
  const params = useParams();
  const router = useRouter();
  const policy = policies.find(p => p.id === params.policyId);
  if (!policy) return <div className="p-8 text-center text-muted-foreground">Policy not found</div>;

  return (
    <PageTransition>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Button variant="ghost" size="icon" onClick={() => router.back()}><ArrowLeft className="h-4 w-4" /></Button>
            <div>
              <div className="flex items-center gap-3">
                <h1 className="text-2xl font-bold font-mono">{policy.policyNumber}</h1>
                <Badge variant="success">{policy.status}</Badge>
              </div>
              <p className="text-muted-foreground">{policy.insured.firstName} {policy.insured.lastName} | {policy.product.replace(/_/g, ' ')}</p>
            </div>
          </div>
          <Link href={`/policy-admin/in-force/${policy.id}/transactions/new`}><Button variant="gold"><Plus className="mr-2 h-4 w-4" />New Transaction</Button></Link>
        </div>

        <div className="grid gap-4 md:grid-cols-4">
          <Card><CardContent className="pt-6"><div className="text-xs text-muted-foreground">Face Amount</div><div className="text-2xl font-bold">{formatCurrency(policy.faceAmount)}</div></CardContent></Card>
          <Card><CardContent className="pt-6"><div className="text-xs text-muted-foreground">Annual Premium</div><div className="text-2xl font-bold">{formatCurrency(policy.premiumAmount)}</div></CardContent></Card>
          <Card><CardContent className="pt-6"><div className="text-xs text-muted-foreground">Cash Value</div><div className="text-2xl font-bold">{formatCurrency(policy.cashValue)}</div></CardContent></Card>
          <Card><CardContent className="pt-6"><div className="text-xs text-muted-foreground">Loan Balance</div><div className="text-2xl font-bold">{formatCurrency(policy.loanBalance)}</div></CardContent></Card>
        </div>

        <Tabs defaultValue="details" className="space-y-4">
          <TabsList>
            <TabsTrigger value="details">Policy Details</TabsTrigger>
            <TabsTrigger value="beneficiaries">Beneficiaries ({policy.beneficiaries.length})</TabsTrigger>
            <TabsTrigger value="riders">Riders ({policy.riders.length})</TabsTrigger>
            <TabsTrigger value="payments">Payment History ({policy.paymentHistory.length})</TabsTrigger>
            <TabsTrigger value="transactions">Transactions ({policy.transactions.length})</TabsTrigger>
          </TabsList>

          <TabsContent value="details">
            <div className="grid gap-4 md:grid-cols-2">
              <Card>
                <CardHeader><CardTitle className="text-base flex items-center gap-2"><Shield className="h-4 w-4" />Policy Information</CardTitle></CardHeader>
                <CardContent className="grid grid-cols-2 gap-4 text-sm">
                  <div><span className="text-muted-foreground">Policy Number</span><div className="font-medium font-mono">{policy.policyNumber}</div></div>
                  <div><span className="text-muted-foreground">Product Type</span><div className="font-medium">{policy.product.replace(/_/g, ' ')}</div></div>
                  <div><span className="text-muted-foreground">Effective Date</span><div className="font-medium">{formatDate(policy.effectiveDate)}</div></div>
                  <div><span className="text-muted-foreground">Issue Date</span><div className="font-medium">{formatDate(policy.issueDate)}</div></div>
                  <div><span className="text-muted-foreground">Premium Mode</span><div className="font-medium capitalize">{policy.premiumMode.replace(/_/g, ' ')}</div></div>
                  <div><span className="text-muted-foreground">Status</span><Badge variant="success" className="mt-0.5">{policy.status}</Badge></div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader><CardTitle className="text-base flex items-center gap-2"><Users className="h-4 w-4" />Insured & Owner</CardTitle></CardHeader>
                <CardContent className="space-y-4 text-sm">
                  <div>
                    <div className="text-muted-foreground mb-1">Insured</div>
                    <div className="font-medium">{policy.insured.firstName} {policy.insured.lastName}</div>
                    <div className="text-xs text-muted-foreground">DOB: {formatDate(policy.insured.dateOfBirth)} | {policy.insured.gender}</div>
                  </div>
                  <Separator />
                  <div>
                    <div className="text-muted-foreground mb-1">Owner</div>
                    <div className="font-medium">{policy.owner.firstName} {policy.owner.lastName}</div>
                  </div>
                </CardContent>
              </Card>
            </div>
          </TabsContent>

          <TabsContent value="beneficiaries">
            <Card>
              <CardContent className="pt-6">
                <Table>
                  <TableHeader>
                    <TableRow><TableHead>Name</TableHead><TableHead>Relationship</TableHead><TableHead>Type</TableHead><TableHead>Percentage</TableHead><TableHead>Irrevocable</TableHead></TableRow>
                  </TableHeader>
                  <TableBody>
                    {policy.beneficiaries.map((b, i) => (
                      <TableRow key={i}>
                        <TableCell className="font-medium">{b.name}</TableCell>
                        <TableCell>{b.relationship}</TableCell>
                        <TableCell><Badge variant={b.type === 'primary' ? 'default' : 'secondary'}>{b.type}</Badge></TableCell>
                        <TableCell className="font-mono">{b.percentage}%</TableCell>
                        <TableCell>{b.irrevocable ? <Badge variant="warning">Yes</Badge> : 'No'}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="riders">
            <Card><CardContent className="pt-6">
              {policy.riders.map((r, i) => (
                <div key={i} className="flex items-center justify-between p-3 border-b last:border-0">
                  <div><div className="font-medium text-sm">{r.name}</div>{r.faceAmount && <div className="text-xs text-muted-foreground">Benefit: {formatCurrency(r.faceAmount)}</div>}</div>
                  <span className="font-mono text-sm">{formatCurrency(r.premium)}/yr</span>
                </div>
              ))}
            </CardContent></Card>
          </TabsContent>

          <TabsContent value="payments">
            <Card><CardContent className="pt-6">
              <Table>
                <TableHeader><TableRow><TableHead>Date</TableHead><TableHead>Type</TableHead><TableHead>Amount</TableHead></TableRow></TableHeader>
                <TableBody>
                  {policy.paymentHistory.map((p, i) => (
                    <TableRow key={i}><TableCell>{formatDate(p.date)}</TableCell><TableCell>{p.type}</TableCell><TableCell className="font-mono">{formatCurrency(p.amount)}</TableCell></TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent></Card>
          </TabsContent>

          <TabsContent value="transactions">
            <Card><CardContent className="pt-6">
              {policy.transactions.length > 0 ? policy.transactions.map(t => (
                <div key={t.id} className="flex items-center justify-between p-3 border-b last:border-0">
                  <div><div className="font-medium text-sm">{t.type.replace(/_/g, ' ')}</div><div className="text-xs text-muted-foreground">{formatDate(t.requestedAt)}</div></div>
                  <Badge variant={t.status === 'completed' ? 'success' : t.status === 'rejected' ? 'destructive' : 'warning'}>{t.status}</Badge>
                </div>
              )) : <p className="text-center py-4 text-muted-foreground">No transactions recorded</p>}
            </CardContent></Card>
          </TabsContent>
        </Tabs>
      </div>
    </PageTransition>
  );
}
