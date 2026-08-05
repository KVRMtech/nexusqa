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
import { customerProfileSteps, executeFlow } from '@/lib/submission-orchestrator';
import type { ApiCallResult } from '@/lib/types';
import { UserPlus, Send } from 'lucide-react';

const US_STATES = [
  'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD',
  'MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC',
  'SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
] as const;

const profileSchema = z.object({
  firstName: z.string().min(1, 'First name is required'),
  lastName: z.string().min(1, 'Last name is required'),
  dateOfBirth: z.string().min(1, 'Date of birth is required'),
  ssn: z.string().regex(/^\d{3}-?\d{2}-?\d{4}$/, 'Enter a valid SSN (XXX-XX-XXXX)'),
  email: z.string().email('Enter a valid email address'),
  phone: z.string().min(10, 'Enter a valid phone number'),
  street: z.string().min(1, 'Street address is required'),
  city: z.string().min(1, 'City is required'),
  state: z.string().min(1, 'State is required'),
  zip: z.string().regex(/^\d{5}(-\d{4})?$/, 'Enter a valid ZIP code'),
  occupation: z.string().min(1, 'Occupation is required'),
  employer: z.string().min(1, 'Employer is required'),
  annualIncome: z.coerce.number().min(1, 'Annual income is required'),
});

type ProfileFormData = z.infer<typeof profileSchema>;

export default function CustomerProfilePage() {
  const [apiResults, setApiResults] = useState<ApiCallResult[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [totalSteps, setTotalSteps] = useState(0);

  const form = useForm<ProfileFormData>({
    resolver: zodResolver(profileSchema),
    defaultValues: {
      firstName: '', lastName: '', dateOfBirth: '', ssn: '', email: '', phone: '',
      street: '', city: '', state: '', zip: '', occupation: '', employer: '', annualIncome: 0,
    },
  });

  const handleSubmit = async (data: ProfileFormData) => {
    setSubmitting(true);
    const steps = customerProfileSteps({
      ...data,
      address: { street: data.street, city: data.city, state: data.state, zip: data.zip },
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
          <UserPlus className="h-6 w-6 text-gold" />
          <div>
            <h1 className="text-2xl font-bold tracking-tight">New Customer Profile</h1>
            <p className="text-muted-foreground">Register a new customer with identity and compliance verification</p>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Form Panel */}
          <div className="lg:col-span-2">
            <Card>
              <CardContent className="pt-6">
                <Form {...form}>
                  <form onSubmit={form.handleSubmit(handleSubmit)} className="space-y-6">
                    {/* Personal Information */}
                    <div>
                      <h3 className="text-sm font-semibold mb-3">Personal Information</h3>
                      <div className="grid grid-cols-2 gap-4">
                        <FormField control={form.control} name="firstName" render={({ field }) => (<FormItem><FormLabel>First Name</FormLabel><FormControl><Input placeholder="John" {...field} /></FormControl><FormMessage /></FormItem>)} />
                        <FormField control={form.control} name="lastName" render={({ field }) => (<FormItem><FormLabel>Last Name</FormLabel><FormControl><Input placeholder="Smith" {...field} /></FormControl><FormMessage /></FormItem>)} />
                      </div>
                      <div className="grid grid-cols-3 gap-4 mt-4">
                        <FormField control={form.control} name="dateOfBirth" render={({ field }) => (<FormItem><FormLabel>Date of Birth</FormLabel><FormControl><Input type="date" {...field} /></FormControl><FormMessage /></FormItem>)} />
                        <FormField control={form.control} name="ssn" render={({ field }) => (<FormItem><FormLabel>SSN</FormLabel><FormControl><Input placeholder="XXX-XX-XXXX" {...field} /></FormControl><FormMessage /></FormItem>)} />
                        <FormField control={form.control} name="phone" render={({ field }) => (<FormItem><FormLabel>Phone</FormLabel><FormControl><Input type="tel" placeholder="(555) 555-0100" {...field} /></FormControl><FormMessage /></FormItem>)} />
                      </div>
                      <div className="mt-4">
                        <FormField control={form.control} name="email" render={({ field }) => (<FormItem><FormLabel>Email Address</FormLabel><FormControl><Input type="email" placeholder="john@example.com" {...field} /></FormControl><FormMessage /></FormItem>)} />
                      </div>
                    </div>

                    <Separator />

                    {/* Address */}
                    <div>
                      <h3 className="text-sm font-semibold mb-3">Address</h3>
                      <FormField control={form.control} name="street" render={({ field }) => (<FormItem><FormLabel>Street Address</FormLabel><FormControl><Input placeholder="123 Main Street" {...field} /></FormControl><FormMessage /></FormItem>)} />
                      <div className="grid grid-cols-3 gap-4 mt-4">
                        <FormField control={form.control} name="city" render={({ field }) => (<FormItem><FormLabel>City</FormLabel><FormControl><Input placeholder="Austin" {...field} /></FormControl><FormMessage /></FormItem>)} />
                        <FormField control={form.control} name="state" render={({ field }) => (
                          <FormItem><FormLabel>State</FormLabel>
                            <Select onValueChange={field.onChange} defaultValue={field.value}><FormControl><SelectTrigger><SelectValue placeholder="Select" /></SelectTrigger></FormControl>
                              <SelectContent>{US_STATES.map(s => <SelectItem key={s} value={s}>{s}</SelectItem>)}</SelectContent>
                            </Select><FormMessage />
                          </FormItem>
                        )} />
                        <FormField control={form.control} name="zip" render={({ field }) => (<FormItem><FormLabel>ZIP Code</FormLabel><FormControl><Input placeholder="78701" {...field} /></FormControl><FormMessage /></FormItem>)} />
                      </div>
                    </div>

                    <Separator />

                    {/* Employment */}
                    <div>
                      <h3 className="text-sm font-semibold mb-3">Employment</h3>
                      <div className="grid grid-cols-2 gap-4">
                        <FormField control={form.control} name="occupation" render={({ field }) => (<FormItem><FormLabel>Occupation</FormLabel><FormControl><Input placeholder="Software Engineer" {...field} /></FormControl><FormMessage /></FormItem>)} />
                        <FormField control={form.control} name="employer" render={({ field }) => (<FormItem><FormLabel>Employer</FormLabel><FormControl><Input placeholder="Acme Corp" {...field} /></FormControl><FormMessage /></FormItem>)} />
                      </div>
                      <div className="mt-4">
                        <FormField control={form.control} name="annualIncome" render={({ field }) => (<FormItem><FormLabel>Annual Income</FormLabel><FormControl><Input type="number" placeholder="120000" {...field} /></FormControl><FormMessage /></FormItem>)} />
                      </div>
                    </div>

                    <div className="flex justify-end pt-2">
                      <Button type="submit" variant="gold" disabled={submitting}>
                        <Send className="mr-2 h-4 w-4" />{submitting ? 'Creating Profile...' : 'Create Customer Profile'}
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
                <CardTitle className="text-sm">Verification Pipeline</CardTitle>
                <CardDescription className="text-xs">
                  {totalSteps > 0 ? `${totalSteps} verification and compliance calls` : 'Submit the form to begin verification'}
                </CardDescription>
              </CardHeader>
              <CardContent>
                {apiResults.length > 0 ? (
                  <ApiCallTracker results={apiResults} totalSteps={totalSteps} />
                ) : (
                  <div className="text-center py-8 text-muted-foreground">
                    <UserPlus className="h-8 w-8 mx-auto mb-2 opacity-20" />
                    <p className="text-xs">Identity verification, KYC/AML screening, and compliance checks will appear here</p>
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
