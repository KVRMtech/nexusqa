'use client';

import { useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { motion } from 'framer-motion';
import { policies } from '@/lib/data';
import { addressChangeSchema, type AddressChangeFormData } from '@/lib/validations';
import { formatCurrency } from '@/lib/utils';
import { PageTransition } from '@/components/domain/page-transition';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ArrowLeft, CheckCircle2, MapPin, Users, CreditCard, RefreshCw, ArrowUpDown, FileText, TrendingDown, TrendingUp, Repeat, Wallet, Percent, Shield } from 'lucide-react';

const SERVICE_TYPES = [
  { type: 'address_change', label: 'Address Change', icon: MapPin, category: 'Administrative' },
  { type: 'beneficiary_change', label: 'Beneficiary Change', icon: Users, category: 'Administrative' },
  { type: 'name_change', label: 'Name Change', icon: FileText, category: 'Administrative' },
  { type: 'premium_mode_change', label: 'Premium Mode Change', icon: RefreshCw, category: 'Financial' },
  { type: 'policy_loan', label: 'Policy Loan', icon: CreditCard, category: 'Financial' },
  { type: 'loan_repayment', label: 'Loan Repayment', icon: Wallet, category: 'Financial' },
  { type: 'partial_surrender', label: 'Partial Surrender', icon: TrendingDown, category: 'Financial' },
  { type: 'full_surrender', label: 'Full Surrender', icon: TrendingDown, category: 'Financial' },
  { type: 'reinstatement', label: 'Reinstatement', icon: RefreshCw, category: 'Policy Change' },
  { type: 'conversion', label: 'Conversion', icon: Repeat, category: 'Policy Change' },
  { type: 'face_increase', label: 'Face Amount Increase', icon: TrendingUp, category: 'Policy Change' },
  { type: 'face_decrease', label: 'Face Amount Decrease', icon: TrendingDown, category: 'Policy Change' },
  { type: 'dividend_option', label: 'Dividend Option', icon: Percent, category: 'Policy Change' },
  { type: 'automatic_premium_loan', label: 'Auto Premium Loan', icon: ArrowUpDown, category: 'Policy Change' },
  { type: 'paid_up_additions', label: 'Paid-Up Additions', icon: Shield, category: 'Policy Change' },
];

export default function NewTransactionPage() {
  const params = useParams();
  const router = useRouter();
  const [selectedType, setSelectedType] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const policy = policies.find(p => p.id === params.policyId);
  if (!policy) return <div className="p-8 text-center text-muted-foreground">Policy not found</div>;

  const form = useForm<AddressChangeFormData>({
    resolver: zodResolver(addressChangeSchema),
    defaultValues: { street: '', city: '', state: '', zip: '' },
  });

  async function onSubmit() {
    setSubmitting(true);
    await new Promise(r => setTimeout(r, 1500));
    setSubmitting(false);
    setSubmitted(true);
  }

  if (submitted) {
    return (
      <PageTransition>
        <div className="flex items-center justify-center min-h-[60vh]">
          <motion.div initial={{ scale: 0.8, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} className="text-center space-y-4">
            <CheckCircle2 className="h-16 w-16 text-emerald-500 mx-auto" />
            <h2 className="text-2xl font-bold">Transaction Submitted</h2>
            <p className="text-muted-foreground">Service request for {policy.policyNumber} has been submitted for processing.</p>
            <Button variant="gold" onClick={() => router.push(`/policy-admin/in-force/${policy.id}`)}>Back to Policy</Button>
          </motion.div>
        </div>
      </PageTransition>
    );
  }

  const grouped = SERVICE_TYPES.reduce<Record<string, typeof SERVICE_TYPES>>((acc, s) => {
    (acc[s.category] ??= []).push(s);
    return acc;
  }, {});

  return (
    <PageTransition>
      <div className="space-y-6 max-w-4xl">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => router.back()}><ArrowLeft className="h-4 w-4" /></Button>
          <div>
            <h1 className="text-2xl font-bold">New Service Transaction</h1>
            <p className="text-muted-foreground">{policy.policyNumber} | {policy.insured.firstName} {policy.insured.lastName} | {formatCurrency(policy.faceAmount)}</p>
          </div>
        </div>

        {!selectedType ? (
          <div className="space-y-6">
            {Object.entries(grouped).map(([category, items]) => (
              <div key={category}>
                <h3 className="text-sm font-semibold text-muted-foreground mb-3 uppercase tracking-wider">{category}</h3>
                <div className="grid gap-3 md:grid-cols-3">
                  {items.map(svc => (
                    <button key={svc.type} onClick={() => setSelectedType(svc.type)} className="flex items-center gap-3 p-4 rounded-lg border hover:border-gold/50 hover:bg-muted/50 transition-all text-left">
                      <svc.icon className="h-5 w-5 text-gold shrink-0" />
                      <span className="text-sm font-medium">{svc.label}</span>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle>{SERVICE_TYPES.find(s => s.type === selectedType)?.label}</CardTitle>
                  <CardDescription>Complete the transaction details below</CardDescription>
                </div>
                <Button variant="ghost" size="sm" onClick={() => setSelectedType(null)}>Change Type</Button>
              </div>
            </CardHeader>
            <CardContent>
              {selectedType === 'address_change' ? (
                <Form {...form}>
                  <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
                    <FormField control={form.control} name="street" render={({ field }) => (<FormItem><FormLabel>Street Address</FormLabel><FormControl><Input placeholder="123 Main St" {...field} /></FormControl><FormMessage /></FormItem>)} />
                    <div className="grid grid-cols-3 gap-4">
                      <FormField control={form.control} name="city" render={({ field }) => (<FormItem><FormLabel>City</FormLabel><FormControl><Input placeholder="City" {...field} /></FormControl><FormMessage /></FormItem>)} />
                      <FormField control={form.control} name="state" render={({ field }) => (<FormItem><FormLabel>State</FormLabel><FormControl><Input placeholder="TX" maxLength={2} {...field} /></FormControl><FormMessage /></FormItem>)} />
                      <FormField control={form.control} name="zip" render={({ field }) => (<FormItem><FormLabel>ZIP Code</FormLabel><FormControl><Input placeholder="78701" {...field} /></FormControl><FormMessage /></FormItem>)} />
                    </div>
                    <div className="flex justify-end gap-3 pt-4">
                      <Button type="button" variant="outline" onClick={() => setSelectedType(null)}>Cancel</Button>
                      <Button type="submit" variant="gold" disabled={submitting}>{submitting ? 'Processing...' : 'Submit Transaction'}</Button>
                    </div>
                  </form>
                </Form>
              ) : (
                <div className="space-y-4">
                  <p className="text-sm text-muted-foreground">Transaction form for {selectedType?.replace(/_/g, ' ')} will capture all required details including effective date, authorization, and supporting documentation.</p>
                  <div className="flex justify-end gap-3">
                    <Button type="button" variant="outline" onClick={() => setSelectedType(null)}>Cancel</Button>
                    <Button variant="gold" onClick={onSubmit} disabled={submitting}>{submitting ? 'Processing...' : 'Submit Transaction'}</Button>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        )}
      </div>
    </PageTransition>
  );
}
