'use client';

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { PageTransition } from '@/components/domain/page-transition';
import { ApiCallTracker } from '@/components/domain/api-call-tracker';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Separator } from '@/components/ui/separator';
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { paymentProcessingSteps, executeFlow } from '@/lib/submission-orchestrator';
import { formatCurrency } from '@/lib/utils';
import type { ApiCallResult } from '@/lib/types';
import { CreditCard, Send, Building2 } from 'lucide-react';

const POLICIES = [
  { id: 'SL-TL-2026-50005', label: 'SL-TL-2026-50005 — Term Life 20yr, $500,000', premium: 487.50 },
  { id: 'SL-WL-2024-99001', label: 'SL-WL-2024-99001 — Whole Life, $250,000', premium: 312.75 },
];

const paymentSchema = z.object({
  policyId: z.string().min(1, 'Select a policy'),
  premiumAmount: z.coerce.number().min(1, 'Amount must be greater than zero'),
  bankName: z.string().min(1, 'Bank name is required'),
  routingNumber: z.string().regex(/^\d{9}$/, 'Routing number must be 9 digits'),
  accountNumber: z.string().min(4, 'Account number is required').max(17, 'Account number too long'),
});

type PaymentFormData = z.infer<typeof paymentSchema>;

export default function ProcessPaymentPage() {
  const [apiResults, setApiResults] = useState<ApiCallResult[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [totalSteps, setTotalSteps] = useState(0);

  const form = useForm<PaymentFormData>({
    resolver: zodResolver(paymentSchema),
    defaultValues: {
      policyId: '', premiumAmount: 0, bankName: '', routingNumber: '', accountNumber: '',
    },
  });

  const selectedPolicy = POLICIES.find(p => p.id === form.watch('policyId'));

  const handlePolicyChange = (policyId: string, fieldOnChange: (val: string) => void) => {
    fieldOnChange(policyId);
    const policy = POLICIES.find(p => p.id === policyId);
    if (policy) {
      form.setValue('premiumAmount', policy.premium);
    }
  };

  const handleSubmit = async (data: PaymentFormData) => {
    setSubmitting(true);
    const steps = paymentProcessingSteps({
      ...data,
      amount: data.premiumAmount,
      customerId: 'cust-existing',
      email: 'policyholder@example.com',
    });
    setTotalSteps(steps.length);
    const token = sessionStorage.getItem('slc_access_token') || '';
    await executeFlow(steps, token, setApiResults);
    setSubmitting(false);
  };

  return (
    <PageTransition>
      <div className="space-y-6">
        <div className="flex items-center gap-3">
          <CreditCard className="h-6 w-6 text-gold" />
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Process Payment</h1>
            <p className="text-muted-foreground">Process a premium payment with bank verification and fraud screening</p>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Form Panel */}
          <div className="lg:col-span-2">
            <Card>
              <CardContent className="pt-6">
                <Form {...form}>
                  <form onSubmit={form.handleSubmit(handleSubmit)} className="space-y-6">
                    {/* Policy Selection */}
                    <div>
                      <h3 className="text-sm font-semibold mb-3">Policy</h3>
                      <FormField control={form.control} name="policyId" render={({ field }) => (
                        <FormItem><FormLabel>Select Policy</FormLabel>
                          <Select onValueChange={(val) => handlePolicyChange(val, field.onChange)} defaultValue={field.value}>
                            <FormControl><SelectTrigger><SelectValue placeholder="Choose a policy" /></SelectTrigger></FormControl>
                            <SelectContent>
                              {POLICIES.map(p => <SelectItem key={p.id} value={p.id}>{p.label}</SelectItem>)}
                            </SelectContent>
                          </Select><FormMessage />
                        </FormItem>
                      )} />
                    </div>

                    <Separator />

                    {/* Amount */}
                    <div>
                      <h3 className="text-sm font-semibold mb-3">Payment Amount</h3>
                      <FormField control={form.control} name="premiumAmount" render={({ field }) => (
                        <FormItem>
                          <FormLabel>Premium Amount</FormLabel>
                          <FormControl><Input type="number" step="0.01" {...field} /></FormControl>
                          {selectedPolicy && (
                            <p className="text-xs text-muted-foreground">Scheduled premium: {formatCurrency(selectedPolicy.premium)}</p>
                          )}
                          <FormMessage />
                        </FormItem>
                      )} />
                    </div>

                    <Separator />

                    {/* Bank Details */}
                    <div>
                      <h3 className="text-sm font-semibold flex items-center gap-2 mb-3">
                        <Building2 className="h-4 w-4 text-muted-foreground" />
                        Bank Account (ACH)
                      </h3>
                      <FormField control={form.control} name="bankName" render={({ field }) => (<FormItem><FormLabel>Bank Name</FormLabel><FormControl><Input placeholder="First National Bank" {...field} /></FormControl><FormMessage /></FormItem>)} />
                      <div className="grid grid-cols-2 gap-4 mt-4">
                        <FormField control={form.control} name="routingNumber" render={({ field }) => (<FormItem><FormLabel>Routing Number</FormLabel><FormControl><Input placeholder="021000021" maxLength={9} {...field} /></FormControl><FormMessage /></FormItem>)} />
                        <FormField control={form.control} name="accountNumber" render={({ field }) => (<FormItem><FormLabel>Account Number</FormLabel><FormControl><Input placeholder="1234567890" {...field} /></FormControl><FormMessage /></FormItem>)} />
                      </div>
                    </div>

                    <div className="flex justify-end pt-2">
                      <Button type="submit" variant="gold" disabled={submitting}>
                        <Send className="mr-2 h-4 w-4" />{submitting ? 'Processing...' : 'Process Payment'}
                      </Button>
                    </div>
                  </form>
                </Form>
              </CardContent>
            </Card>
          </div>

          {/* API Call Tracker */}
          <div className="lg:col-span-1">
            <Card className="sticky top-6">
              <CardHeader className="pb-3">
                <CardTitle className="text-sm">Payment Pipeline</CardTitle>
                <CardDescription className="text-xs">
                  {totalSteps > 0 ? `${totalSteps} payment processing calls` : 'Submit to begin payment processing'}
                </CardDescription>
              </CardHeader>
              <CardContent>
                {apiResults.length > 0 ? (
                  <ApiCallTracker results={apiResults} totalSteps={totalSteps} />
                ) : (
                  <div className="text-center py-8 text-muted-foreground">
                    <CreditCard className="h-8 w-8 mx-auto mb-2 opacity-20" />
                    <p className="text-xs">Bank verification, fraud screening, and payment processing calls will appear here</p>
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      </div>
    </PageTransition>
  );
}
