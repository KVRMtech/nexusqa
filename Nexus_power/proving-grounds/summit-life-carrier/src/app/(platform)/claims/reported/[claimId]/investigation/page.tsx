'use client';

import { useParams, useRouter } from 'next/navigation';
import { claims } from '@/lib/data';
import { formatCurrency, formatDate } from '@/lib/utils';
import { PageTransition, StaggerContainer, StaggerItem } from '@/components/domain/page-transition';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Separator } from '@/components/ui/separator';
import { ArrowLeft, FileText, CheckCircle2, XCircle, Clock, AlertTriangle, DollarSign, Shield, User } from 'lucide-react';

export default function ClaimInvestigationPage() {
  const params = useParams();
  const router = useRouter();
  const claim = claims.find(c => c.id === params.claimId);
  if (!claim) return <div className="p-8 text-center text-muted-foreground">Claim not found</div>;

  const docsReceived = claim.documents.filter(d => d.received).length;
  const totalDocs = claim.documents.length;

  return (
    <PageTransition>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Button variant="ghost" size="icon" onClick={() => router.back()}><ArrowLeft className="h-4 w-4" /></Button>
            <div>
              <div className="flex items-center gap-3">
                <h1 className="text-2xl font-bold font-mono">{claim.claimNumber}</h1>
                <Badge variant="info">{claim.status.replace(/_/g, ' ')}</Badge>
                {claim.isContestable && <Badge variant="destructive">Contestable</Badge>}
              </div>
              <p className="text-muted-foreground">{claim.insured.firstName} {claim.insured.lastName} | {claim.type.replace(/_/g, ' ')} | {formatCurrency(claim.faceAmount)}</p>
            </div>
          </div>
          <div className="flex gap-2">
            <Button variant="outline">Request Documents</Button>
            <Button variant="gold">Approve Claim</Button>
          </div>
        </div>

        <StaggerContainer className="grid gap-4 md:grid-cols-4">
          {[
            { label: 'Face Amount', value: formatCurrency(claim.faceAmount), icon: DollarSign },
            { label: 'Benefit Amount', value: formatCurrency(claim.benefitAmount), icon: DollarSign },
            { label: 'Documents', value: `${docsReceived}/${totalDocs}`, icon: FileText },
            { label: 'Contestability', value: claim.isContestable ? 'Active' : 'Expired', icon: Shield },
          ].map(stat => (
            <StaggerItem key={stat.label}>
              <Card><CardContent className="pt-6 flex items-center gap-4"><stat.icon className="h-8 w-8 text-gold/60" /><div><div className="text-xs text-muted-foreground">{stat.label}</div><div className="text-xl font-bold">{stat.value}</div></div></CardContent></Card>
            </StaggerItem>
          ))}
        </StaggerContainer>

        <Tabs defaultValue="overview" className="space-y-4">
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="documents">Documents ({totalDocs})</TabsTrigger>
            <TabsTrigger value="investigation">Investigation ({claim.investigationNotes.length})</TabsTrigger>
            <TabsTrigger value="contestability">Contestability</TabsTrigger>
            <TabsTrigger value="payment">Payment</TabsTrigger>
          </TabsList>

          <TabsContent value="overview">
            <div className="grid gap-4 md:grid-cols-2">
              <Card>
                <CardHeader><CardTitle className="text-base flex items-center gap-2"><User className="h-4 w-4" />Claimant</CardTitle></CardHeader>
                <CardContent className="grid grid-cols-2 gap-3 text-sm">
                  <div><span className="text-muted-foreground">Name</span><div className="font-medium">{claim.claimant.firstName} {claim.claimant.lastName}</div></div>
                  <div><span className="text-muted-foreground">Relationship</span><div className="font-medium">{claim.claimant.relationship}</div></div>
                  <div><span className="text-muted-foreground">Phone</span><div className="font-medium">{claim.claimant.phone}</div></div>
                  <div><span className="text-muted-foreground">Email</span><div className="font-medium">{claim.claimant.email}</div></div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader><CardTitle className="text-base">Loss Details</CardTitle></CardHeader>
                <CardContent className="grid grid-cols-2 gap-3 text-sm">
                  <div><span className="text-muted-foreground">Date of Loss</span><div className="font-medium">{formatDate(claim.dateOfLoss)}</div></div>
                  <div><span className="text-muted-foreground">Date Reported</span><div className="font-medium">{formatDate(claim.reportedAt)}</div></div>
                  <div><span className="text-muted-foreground">Cause of Death</span><div className="font-medium">{claim.causeOfDeath || 'Not specified'}</div></div>
                  <div><span className="text-muted-foreground">Analyst</span><div className="font-medium">{claim.assignedAnalyst}</div></div>
                </CardContent>
              </Card>
            </div>
          </TabsContent>

          <TabsContent value="documents">
            <Card>
              <CardContent className="pt-6 space-y-3">
                {claim.documents.map((doc, i) => (
                  <div key={i} className="flex items-center justify-between p-3 rounded-lg border">
                    <div className="flex items-center gap-3">
                      {doc.received ? <CheckCircle2 className="h-4 w-4 text-emerald-500" /> : <XCircle className="h-4 w-4 text-red-400" />}
                      <div><div className="font-medium text-sm">{doc.name}</div><div className="text-xs text-muted-foreground">Type: {doc.type}</div></div>
                    </div>
                    {doc.received ? (
                      <div className="text-xs text-muted-foreground">Received {doc.receivedAt ? formatDate(doc.receivedAt) : ''}</div>
                    ) : (
                      <Badge variant="destructive">Outstanding</Badge>
                    )}
                  </div>
                ))}
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="investigation">
            <Card>
              <CardContent className="pt-6 space-y-4">
                {claim.investigationNotes.map((note, i) => (
                  <div key={i} className="border-l-2 border-gold pl-4 space-y-1">
                    <div className="flex items-center justify-between"><span className="font-medium text-sm">{note.author}</span><span className="text-xs text-muted-foreground">{formatDate(note.createdAt)}</span></div>
                    <p className="text-sm text-muted-foreground">{note.text}</p>
                  </div>
                ))}
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="contestability">
            <Card>
              <CardContent className="pt-6 space-y-4">
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div><span className="text-muted-foreground">Contestability Expiry</span><div className="font-medium">{formatDate(claim.contestabilityExpiry)}</div></div>
                  <div><span className="text-muted-foreground">Status</span><div className="mt-1">{claim.isContestable ? <Badge variant="destructive">Within Contestability Period</Badge> : <Badge variant="success">Outside Contestability Period</Badge>}</div></div>
                </div>
                <Separator />
                <div className="p-4 bg-muted/50 rounded-lg text-sm">
                  {claim.isContestable ? (
                    <div className="flex items-start gap-2"><AlertTriangle className="h-4 w-4 text-amber-500 mt-0.5 shrink-0" /><span>This policy is within the 2-year contestability period. Enhanced investigation and documentation requirements apply per state insurance regulations.</span></div>
                  ) : (
                    <div className="flex items-start gap-2"><CheckCircle2 className="h-4 w-4 text-emerald-500 mt-0.5 shrink-0" /><span>This policy is outside the 2-year contestability period. Standard claims processing applies unless fraud is suspected.</span></div>
                  )}
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="payment">
            <Card>
              <CardContent className="pt-6">
                {claim.payments.length > 0 ? claim.payments.map((p, i) => (
                  <div key={i} className="flex justify-between p-3 border-b last:border-0">
                    <div><div className="font-medium text-sm">{p.payee}</div><div className="text-xs text-muted-foreground">{p.type} | {formatDate(p.date)}</div></div>
                    <span className="font-mono font-medium">{formatCurrency(p.amount)}</span>
                  </div>
                )) : (
                  <div className="text-center py-8 text-muted-foreground">
                    <DollarSign className="h-8 w-8 mx-auto mb-2 text-muted-foreground/40" />
                    <p>No payments processed. Claim must be approved before payment disbursement.</p>
                  </div>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
    </PageTransition>
  );
}
