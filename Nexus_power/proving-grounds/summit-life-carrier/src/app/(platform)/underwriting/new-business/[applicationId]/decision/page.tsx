'use client';

import { useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { motion } from 'framer-motion';
import { applications } from '@/lib/data';
import { underwritingDecisionSchema, type UnderwritingDecisionFormData } from '@/lib/validations';
import { formatCurrency } from '@/lib/utils';
import { PageTransition } from '@/components/domain/page-transition';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage, FormDescription } from '@/components/ui/form';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import { Separator } from '@/components/ui/separator';
import { ArrowLeft, ShieldCheck, AlertTriangle, CheckCircle2, XCircle, Scale } from 'lucide-react';

const RISK_CLASSES = [
  { value: 'preferred_plus', label: 'Preferred Plus', multiplier: 0.75 },
  { value: 'preferred', label: 'Preferred', multiplier: 0.85 },
  { value: 'standard_plus', label: 'Standard Plus', multiplier: 0.92 },
  { value: 'standard', label: 'Standard', multiplier: 1.00 },
  { value: 'substandard_table_a', label: 'Substandard — Table A (+25%)', multiplier: 1.25 },
  { value: 'substandard_table_b', label: 'Substandard — Table B (+50%)', multiplier: 1.50 },
  { value: 'substandard_table_c', label: 'Substandard — Table C (+75%)', multiplier: 1.75 },
  { value: 'substandard_table_d', label: 'Substandard — Table D (+100%)', multiplier: 2.00 },
];

export default function RiskDecisionPage() {
  const params = useParams();
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const app = applications.find(a => a.id === params.applicationId);
  if (!app) return <div className="p-8 text-center text-muted-foreground">Application not found</div>;

  const basePremium = (app.faceAmount / 1000) * 1.05;
  const form = useForm<UnderwritingDecisionFormData>({
    resolver: zodResolver(underwritingDecisionSchema),
    defaultValues: { applicationId: app.id, rationale: '', requiresManagerApproval: false },
  });

  const decision = form.watch('decision');
  const riskClass = form.watch('riskClass');
  const selectedRisk = RISK_CLASSES.find(r => r.value === riskClass);
  const calculatedPremium = selectedRisk ? basePremium * selectedRisk.multiplier : basePremium;

  async function onSubmit(data: UnderwritingDecisionFormData) {
    setSubmitting(true);
    await new Promise(r => setTimeout(r, 2000));
    setSubmitting(false);
    setSubmitted(true);
  }

  if (submitted) {
    return (
      <PageTransition>
        <div className="flex items-center justify-center min-h-[60vh]">
          <motion.div initial={{ scale: 0.8, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} className="text-center space-y-4">
            <CheckCircle2 className="h-16 w-16 text-emerald-500 mx-auto" />
            <h2 className="text-2xl font-bold">Decision Recorded</h2>
            <p className="text-muted-foreground">The underwriting decision for {app.caseNumber} has been recorded and routed for processing.</p>
            <div className="flex gap-2 justify-center">
              <Button variant="outline" onClick={() => router.push('/underwriting/new-business')}>Back to Queue</Button>
              <Button variant="gold" onClick={() => router.push(`/underwriting/new-business/${app.id}/review`)}>View Case</Button>
            </div>
          </motion.div>
        </div>
      </PageTransition>
    );
  }

  return (
    <PageTransition>
      <div className="space-y-6 max-w-4xl">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => router.back()}><ArrowLeft className="h-4 w-4" /></Button>
          <div>
            <h1 className="text-2xl font-bold flex items-center gap-2"><ShieldCheck className="h-6 w-6 text-gold" />Risk Assessment & Decision</h1>
            <p className="text-muted-foreground">{app.caseNumber} | {app.applicant.firstName} {app.applicant.lastName} | {formatCurrency(app.faceAmount)} {app.product.replace(/_/g, ' ')}</p>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-3">
          <Card><CardContent className="pt-6"><div className="text-xs text-muted-foreground">Applicant Age</div><div className="text-2xl font-bold">{new Date().getFullYear() - new Date(app.applicant.dateOfBirth).getFullYear()}</div></CardContent></Card>
          <Card><CardContent className="pt-6"><div className="text-xs text-muted-foreground">Tobacco Status</div><div className="flex items-center gap-2 mt-1">{app.applicant.tobaccoUse ? <Badge variant="destructive">Tobacco User</Badge> : <Badge variant="success">Non-Tobacco</Badge>}</div></CardContent></Card>
          <Card><CardContent className="pt-6"><div className="text-xs text-muted-foreground">Health Conditions</div><div className="mt-1 flex flex-wrap gap-1">{app.applicant.healthConditions.length > 0 ? app.applicant.healthConditions.map(c => <Badge key={c} variant="warning">{c.replace(/_/g, ' ')}</Badge>) : <Badge variant="success">None</Badge>}</div></CardContent></Card>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Scale className="h-5 w-5" />Underwriting Decision</CardTitle>
            <CardDescription>Record the risk classification and decision for this application</CardDescription>
          </CardHeader>
          <CardContent>
            <Form {...form}>
              <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
                <FormField control={form.control} name="decision" render={({ field }) => (
                  <FormItem>
                    <FormLabel>Decision</FormLabel>
                    <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
                      {[
                        { value: 'approve', label: 'Approve', icon: CheckCircle2, color: 'border-emerald-500 bg-emerald-50 text-emerald-700' },
                        { value: 'approve_rated', label: 'Approve Rated', icon: AlertTriangle, color: 'border-amber-500 bg-amber-50 text-amber-700' },
                        { value: 'decline', label: 'Decline', icon: XCircle, color: 'border-red-500 bg-red-50 text-red-700' },
                        { value: 'counter_offer', label: 'Counter Offer', icon: Scale, color: 'border-blue-500 bg-blue-50 text-blue-700' },
                        { value: 'postpone', label: 'Postpone', icon: AlertTriangle, color: 'border-gray-500 bg-gray-50 text-gray-700' },
                      ].map(opt => (
                        <button type="button" key={opt.value} onClick={() => field.onChange(opt.value)}
                          className={`flex flex-col items-center gap-1 p-3 rounded-lg border-2 transition-all text-sm ${field.value === opt.value ? opt.color : 'border-transparent bg-muted/50 hover:bg-muted'}`}>
                          <opt.icon className="h-5 w-5" />
                          <span className="font-medium">{opt.label}</span>
                        </button>
                      ))}
                    </div>
                    <FormMessage />
                  </FormItem>
                )} />

                {(decision === 'approve' || decision === 'approve_rated') && (
                  <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} className="space-y-4">
                    <FormField control={form.control} name="riskClass" render={({ field }) => (
                      <FormItem>
                        <FormLabel>Risk Classification</FormLabel>
                        <Select onValueChange={field.onChange} defaultValue={field.value}>
                          <FormControl><SelectTrigger><SelectValue placeholder="Select risk class" /></SelectTrigger></FormControl>
                          <SelectContent>{RISK_CLASSES.map(rc => <SelectItem key={rc.value} value={rc.value}>{rc.label}</SelectItem>)}</SelectContent>
                        </Select>
                        <FormMessage />
                      </FormItem>
                    )} />

                    {selectedRisk && (
                      <Card className="bg-muted/50">
                        <CardContent className="pt-4">
                          <div className="grid grid-cols-3 gap-4 text-sm">
                            <div><span className="text-muted-foreground">Base Premium</span><div className="text-lg font-bold">{formatCurrency(basePremium)}</div></div>
                            <div><span className="text-muted-foreground">Rate Multiplier</span><div className="text-lg font-bold">{selectedRisk.multiplier}x</div></div>
                            <div><span className="text-muted-foreground">Annual Premium</span><div className="text-lg font-bold text-gold">{formatCurrency(calculatedPremium)}</div></div>
                          </div>
                        </CardContent>
                      </Card>
                    )}

                    <FormField control={form.control} name="premiumAmount" render={({ field }) => (
                      <FormItem>
                        <FormLabel>Final Premium Amount (Annual)</FormLabel>
                        <FormControl><Input type="number" step="0.01" placeholder={calculatedPremium.toFixed(2)} {...field} onChange={e => field.onChange(parseFloat(e.target.value))} /></FormControl>
                        <FormDescription>Override the calculated premium if adjustments are needed</FormDescription>
                        <FormMessage />
                      </FormItem>
                    )} />
                  </motion.div>
                )}

                <Separator />

                <FormField control={form.control} name="rationale" render={({ field }) => (
                  <FormItem>
                    <FormLabel>Decision Rationale</FormLabel>
                    <FormControl><Textarea placeholder="Provide detailed rationale for this underwriting decision..." className="min-h-[120px]" {...field} /></FormControl>
                    <FormMessage />
                  </FormItem>
                )} />

                <FormField control={form.control} name="conditions" render={({ field }) => (
                  <FormItem>
                    <FormLabel>Conditions / Amendments (Optional)</FormLabel>
                    <FormControl><Textarea placeholder="Any conditions attached to this decision..." {...field} /></FormControl>
                    <FormMessage />
                  </FormItem>
                )} />

                <div className="flex justify-end gap-3">
                  <Button type="button" variant="outline" onClick={() => router.back()}>Cancel</Button>
                  <Button type="submit" variant="gold" disabled={submitting}>{submitting ? 'Recording Decision...' : 'Record Decision'}</Button>
                </div>
              </form>
            </Form>
          </CardContent>
        </Card>
      </div>
    </PageTransition>
  );
}
