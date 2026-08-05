'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { motion } from 'framer-motion';
import { claimIntakeSchema, type ClaimIntakeFormData } from '@/lib/validations';
import { PageTransition } from '@/components/domain/page-transition';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ArrowLeft, CheckCircle2, AlertTriangle } from 'lucide-react';

export default function NewFnolPage() {
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const form = useForm<ClaimIntakeFormData>({
    resolver: zodResolver(claimIntakeSchema),
    defaultValues: { policyNumber: '', claimantFirstName: '', claimantLastName: '', claimantRelationship: '', claimantPhone: '', claimantEmail: '', dateOfLoss: '', description: '' },
  });

  async function onSubmit(data: ClaimIntakeFormData) {
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
            <h2 className="text-2xl font-bold">FNOL Recorded</h2>
            <p className="text-muted-foreground">First Notice of Loss has been recorded and routed to the claims team for investigation.</p>
            <Button variant="gold" onClick={() => router.push('/claims/reported')}>Back to Claims</Button>
          </motion.div>
        </div>
      </PageTransition>
    );
  }

  return (
    <PageTransition>
      <div className="space-y-6 max-w-3xl">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => router.back()}><ArrowLeft className="h-4 w-4" /></Button>
          <div>
            <h1 className="text-2xl font-bold flex items-center gap-2"><AlertTriangle className="h-6 w-6 text-gold" />First Notice of Loss</h1>
            <p className="text-muted-foreground">Record initial claim notification details</p>
          </div>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Claim Intake Form</CardTitle>
            <CardDescription>All fields marked with validation are required for claim registration</CardDescription>
          </CardHeader>
          <CardContent>
            <Form {...form}>
              <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
                <div className="grid grid-cols-2 gap-4">
                  <FormField control={form.control} name="policyNumber" render={({ field }) => (<FormItem><FormLabel>Policy Number</FormLabel><FormControl><Input placeholder="SL-TL-2026-50005" {...field} /></FormControl><FormMessage /></FormItem>)} />
                  <FormField control={form.control} name="claimType" render={({ field }) => (
                    <FormItem><FormLabel>Claim Type</FormLabel>
                      <Select onValueChange={field.onChange} defaultValue={field.value}><FormControl><SelectTrigger><SelectValue placeholder="Select claim type" /></SelectTrigger></FormControl>
                        <SelectContent>
                          <SelectItem value="death">Death Claim</SelectItem>
                          <SelectItem value="accelerated_death">Accelerated Death Benefit</SelectItem>
                          <SelectItem value="waiver_of_premium">Waiver of Premium</SelectItem>
                          <SelectItem value="accidental_death">Accidental Death</SelectItem>
                          <SelectItem value="dismemberment">Dismemberment</SelectItem>
                        </SelectContent>
                      </Select><FormMessage />
                    </FormItem>
                  )} />
                </div>

                <FormField control={form.control} name="dateOfLoss" render={({ field }) => (<FormItem><FormLabel>Date of Loss</FormLabel><FormControl><Input type="date" {...field} /></FormControl><FormMessage /></FormItem>)} />

                <div className="border-t pt-4">
                  <h3 className="text-sm font-semibold mb-3">Claimant Information</h3>
                  <div className="grid grid-cols-2 gap-4">
                    <FormField control={form.control} name="claimantFirstName" render={({ field }) => (<FormItem><FormLabel>First Name</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>)} />
                    <FormField control={form.control} name="claimantLastName" render={({ field }) => (<FormItem><FormLabel>Last Name</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>)} />
                    <FormField control={form.control} name="claimantRelationship" render={({ field }) => (<FormItem><FormLabel>Relationship to Insured</FormLabel><FormControl><Input placeholder="Spouse, Child, etc." {...field} /></FormControl><FormMessage /></FormItem>)} />
                    <FormField control={form.control} name="claimantPhone" render={({ field }) => (<FormItem><FormLabel>Phone Number</FormLabel><FormControl><Input type="tel" placeholder="(555) 555-0000" {...field} /></FormControl><FormMessage /></FormItem>)} />
                    <FormField control={form.control} name="claimantEmail" render={({ field }) => (<FormItem className="col-span-2"><FormLabel>Email Address</FormLabel><FormControl><Input type="email" {...field} /></FormControl><FormMessage /></FormItem>)} />
                  </div>
                </div>

                <FormField control={form.control} name="causeOfDeath" render={({ field }) => (<FormItem><FormLabel>Cause of Death (if applicable)</FormLabel><FormControl><Input placeholder="As stated on death certificate" {...field} /></FormControl><FormMessage /></FormItem>)} />

                <FormField control={form.control} name="description" render={({ field }) => (<FormItem><FormLabel>Circumstances Description</FormLabel><FormControl><Textarea className="min-h-[100px]" placeholder="Provide details about the circumstances of the loss..." {...field} /></FormControl><FormMessage /></FormItem>)} />

                <div className="flex justify-end gap-3">
                  <Button type="button" variant="outline" onClick={() => router.back()}>Cancel</Button>
                  <Button type="submit" variant="gold" disabled={submitting}>{submitting ? 'Recording FNOL...' : 'Record FNOL'}</Button>
                </div>
              </form>
            </Form>
          </CardContent>
        </Card>
      </div>
    </PageTransition>
  );
}
