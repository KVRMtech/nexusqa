'use client';

import { useParams, useRouter } from 'next/navigation';
import { applications, vendorOrders, suspenseItems } from '@/lib/data';
import { formatCurrency, formatDate } from '@/lib/utils';
import { PageTransition } from '@/components/domain/page-transition';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Separator } from '@/components/ui/separator';
import { ArrowLeft, FileText, Stethoscope, ShieldCheck, Clock, AlertCircle, User, MapPin, Briefcase, DollarSign, Heart } from 'lucide-react';
import Link from 'next/link';

export default function ApplicationReviewPage() {
  const params = useParams();
  const router = useRouter();
  const app = applications.find(a => a.id === params.applicationId);
  if (!app) return <div className="p-8 text-center text-muted-foreground">Application not found</div>;

  const orders = vendorOrders.filter(v => v.applicationId === app.id);
  const suspense = suspenseItems.filter(s => s.applicationId === app.id);
  const a = app.applicant;

  const statusColor: Record<string, string> = {
    submitted: 'info', in_review: 'warning', requirements_pending: 'destructive', underwriting: 'warning', approved: 'success', declined: 'destructive',
  };

  return (
    <PageTransition>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Button variant="ghost" size="icon" onClick={() => router.back()}><ArrowLeft className="h-4 w-4" /></Button>
            <div>
              <div className="flex items-center gap-3">
                <h1 className="text-2xl font-bold">{a.firstName} {a.lastName}</h1>
                <Badge variant={statusColor[app.status] as 'info' | 'warning' | 'destructive' | 'success' || 'default'}>{app.status.replace(/_/g, ' ')}</Badge>
              </div>
              <p className="text-muted-foreground">{app.caseNumber} | {app.product.replace(/_/g, ' ')} | {formatCurrency(app.faceAmount)}</p>
            </div>
          </div>
          <div className="flex gap-2">
            <Link href={`/underwriting/new-business/${app.id}/requirements`}><Button variant="outline"><Stethoscope className="mr-2 h-4 w-4" />Requirements</Button></Link>
            <Link href={`/underwriting/new-business/${app.id}/decision`}><Button variant="gold"><ShieldCheck className="mr-2 h-4 w-4" />Risk Decision</Button></Link>
          </div>
        </div>

        <Tabs defaultValue="overview" className="space-y-4">
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="health">Health Profile</TabsTrigger>
            <TabsTrigger value="financial">Financial</TabsTrigger>
            <TabsTrigger value="vendors">Vendor Reports ({orders.length})</TabsTrigger>
            <TabsTrigger value="suspense">Suspense ({suspense.length})</TabsTrigger>
            <TabsTrigger value="notes">Notes ({app.notes.length})</TabsTrigger>
          </TabsList>

          <TabsContent value="overview">
            <div className="grid gap-4 md:grid-cols-2">
              <Card>
                <CardHeader><CardTitle className="text-base flex items-center gap-2"><User className="h-4 w-4" />Personal Information</CardTitle></CardHeader>
                <CardContent className="grid grid-cols-2 gap-4 text-sm">
                  <div><span className="text-muted-foreground">Full Name</span><div className="font-medium">{a.firstName} {a.lastName}</div></div>
                  <div><span className="text-muted-foreground">Date of Birth</span><div className="font-medium">{formatDate(a.dateOfBirth)}</div></div>
                  <div><span className="text-muted-foreground">Gender</span><div className="font-medium capitalize">{a.gender}</div></div>
                  <div><span className="text-muted-foreground">SSN</span><div className="font-medium font-mono">{a.ssn}</div></div>
                  <div><span className="text-muted-foreground">Email</span><div className="font-medium">{a.email}</div></div>
                  <div><span className="text-muted-foreground">Phone</span><div className="font-medium">{a.phone}</div></div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader><CardTitle className="text-base flex items-center gap-2"><MapPin className="h-4 w-4" />Address & Employment</CardTitle></CardHeader>
                <CardContent className="grid grid-cols-2 gap-4 text-sm">
                  <div className="col-span-2"><span className="text-muted-foreground">Address</span><div className="font-medium">{a.address.street}, {a.address.city}, {a.address.state} {a.address.zip}</div></div>
                  <div><span className="text-muted-foreground">Occupation</span><div className="font-medium">{a.occupation}</div></div>
                  <div><span className="text-muted-foreground">Annual Income</span><div className="font-medium">{formatCurrency(a.annualIncome)}</div></div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader><CardTitle className="text-base flex items-center gap-2"><Briefcase className="h-4 w-4" />Policy Details</CardTitle></CardHeader>
                <CardContent className="grid grid-cols-2 gap-4 text-sm">
                  <div><span className="text-muted-foreground">Product</span><div className="font-medium">{app.product.replace(/_/g, ' ')}</div></div>
                  <div><span className="text-muted-foreground">Face Amount</span><div className="font-medium text-lg">{formatCurrency(app.faceAmount)}</div></div>
                  <div><span className="text-muted-foreground">Premium Mode</span><div className="font-medium capitalize">{app.premiumMode.replace(/_/g, ' ')}</div></div>
                  <div><span className="text-muted-foreground">Submitted</span><div className="font-medium">{formatDate(app.submittedAt)}</div></div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader><CardTitle className="text-base flex items-center gap-2"><Heart className="h-4 w-4" />Health Summary</CardTitle></CardHeader>
                <CardContent className="text-sm space-y-3">
                  <div className="flex justify-between"><span className="text-muted-foreground">Tobacco Use</span><Badge variant={a.tobaccoUse ? 'destructive' : 'success'}>{a.tobaccoUse ? 'Yes' : 'No'}</Badge></div>
                  <Separator />
                  <div><span className="text-muted-foreground">Health Conditions</span><div className="mt-1 flex flex-wrap gap-1">{a.healthConditions.length > 0 ? a.healthConditions.map(c => <Badge key={c} variant="warning">{c.replace(/_/g, ' ')}</Badge>) : <Badge variant="success">None reported</Badge>}</div></div>
                </CardContent>
              </Card>
            </div>
          </TabsContent>

          <TabsContent value="health">
            <Card><CardContent className="pt-6"><p className="text-muted-foreground">Detailed health questionnaire responses, attending physician statements, and medical underwriting worksheet available upon vendor report completion.</p></CardContent></Card>
          </TabsContent>

          <TabsContent value="financial">
            <Card>
              <CardHeader><CardTitle className="text-base flex items-center gap-2"><DollarSign className="h-4 w-4" />Financial Justification</CardTitle></CardHeader>
              <CardContent className="text-sm space-y-4">
                <div className="grid grid-cols-3 gap-4">
                  <div><span className="text-muted-foreground">Annual Income</span><div className="text-lg font-bold">{formatCurrency(a.annualIncome)}</div></div>
                  <div><span className="text-muted-foreground">Face Amount</span><div className="text-lg font-bold">{formatCurrency(app.faceAmount)}</div></div>
                  <div><span className="text-muted-foreground">Income Multiple</span><div className="text-lg font-bold">{(app.faceAmount / a.annualIncome).toFixed(1)}x</div></div>
                </div>
                <Separator />
                <div className="p-3 bg-muted/50 rounded-md">
                  <div className="flex items-center gap-2 text-sm">
                    {app.faceAmount / a.annualIncome <= 25 ? (
                      <><Badge variant="success">Within guidelines</Badge><span>Coverage does not exceed 25x annual income threshold</span></>
                    ) : (
                      <><Badge variant="warning">Exceeds threshold</Badge><span>Financial justification documentation required</span></>
                    )}
                  </div>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="vendors">
            <Card>
              <CardContent className="pt-6">
                <div className="space-y-3">
                  {orders.length > 0 ? orders.map(o => (
                    <div key={o.id} className="flex items-center justify-between p-3 rounded-lg border">
                      <div className="flex items-center gap-3">
                        <FileText className="h-4 w-4 text-muted-foreground" />
                        <div><div className="font-medium text-sm">{o.vendor}</div><div className="text-xs text-muted-foreground">Ordered {formatDate(o.orderedAt)}</div></div>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className="text-sm font-mono">{formatCurrency(o.cost)}</span>
                        <Badge variant={o.status === 'received' || o.status === 'reviewed' ? 'success' : o.status === 'pending' ? 'destructive' : 'warning'}>{o.status}</Badge>
                      </div>
                    </div>
                  )) : <p className="text-center text-muted-foreground py-4">No vendor orders placed yet</p>}
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="suspense">
            <Card>
              <CardContent className="pt-6">
                {suspense.length > 0 ? suspense.map(s => (
                  <div key={s.id} className="flex items-start gap-3 p-3 rounded-lg border mb-3">
                    <AlertCircle className="h-4 w-4 text-amber-500 mt-0.5 shrink-0" />
                    <div className="space-y-1">
                      <div className="font-medium text-sm">{s.reason.replace(/_/g, ' ')}</div>
                      <div className="text-xs text-muted-foreground">{s.description}</div>
                      <div className="flex items-center gap-2 text-xs text-muted-foreground"><Clock className="h-3 w-3" />Due {formatDate(s.dueDate)}</div>
                    </div>
                  </div>
                )) : <p className="text-center text-muted-foreground py-4">No active suspense items</p>}
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="notes">
            <Card>
              <CardContent className="pt-6 space-y-4">
                {app.notes.length > 0 ? app.notes.map((n, i) => (
                  <div key={i} className="border-l-2 border-gold pl-4 space-y-1">
                    <div className="flex items-center justify-between"><span className="font-medium text-sm">{n.author}</span><span className="text-xs text-muted-foreground">{formatDate(n.createdAt)}</span></div>
                    <p className="text-sm text-muted-foreground">{n.text}</p>
                  </div>
                )) : <p className="text-center text-muted-foreground py-4">No notes yet</p>}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
    </PageTransition>
  );
}
