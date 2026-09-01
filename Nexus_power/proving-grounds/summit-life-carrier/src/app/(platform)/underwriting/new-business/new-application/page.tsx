'use client';

import { useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { motion, AnimatePresence } from 'framer-motion';
import { PageTransition } from '@/components/domain/page-transition';
import { ApiCallTracker } from '@/components/domain/api-call-tracker';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { applicationSubmissionSteps, executeFlow } from '@/lib/submission-orchestrator';
import { formatCurrency } from '@/lib/utils';
import type { ApiCallResult } from '@/lib/types';
import { FilePlus2, ArrowLeft, ArrowRight, Send, User, MapPin, Shield, HeartPulse, ClipboardCheck } from 'lucide-react';

const US_STATES = [
  'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD',
  'MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC',
  'SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
] as const;

const HEALTH_CONDITIONS = [
  { value: 'none', label: 'None' },
  { value: 'controlled_hypertension', label: 'Controlled Hypertension' },
  { value: 'type_2_diabetes', label: 'Type 2 Diabetes' },
  { value: 'elevated_bmi', label: 'Elevated BMI' },
  { value: 'sleep_apnea_treated', label: 'Sleep Apnea (Treated)' },
  { value: 'asthma', label: 'Asthma' },
  { value: 'anxiety_treated', label: 'Anxiety (Treated)' },
  { value: 'high_cholesterol', label: 'High Cholesterol' },
];

const applicationSchema = z.object({
  firstName: z.string().min(1, 'First name is required'),
  lastName: z.string().min(1, 'Last name is required'),
  dateOfBirth: z.string().min(1, 'Date of birth is required'),
  gender: z.enum(['male', 'female'], { required_error: 'Gender is required' }),
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
  product: z.string().min(1, 'Product is required'),
  faceAmount: z.coerce.number().min(10000, 'Minimum face amount is $10,000'),
  premiumMode: z.string().min(1, 'Premium mode is required'),
  tobaccoUse: z.enum(['yes', 'no'], { required_error: 'Tobacco use is required' }),
  healthConditions: z.array(z.string()).min(1, 'Select at least one option'),
  primaryPhysician: z.string().min(1, 'Physician name is required'),
  lastExamDate: z.string().min(1, 'Last exam date is required'),
});

type ApplicationFormData = z.infer<typeof applicationSchema>;

const stepIcons = [User, MapPin, Shield, HeartPulse, ClipboardCheck];
const stepLabels = ['Applicant', 'Address & Employment', 'Coverage', 'Health', 'Review & Submit'];

export default function NewApplicationPage() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [apiResults, setApiResults] = useState<ApiCallResult[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [totalSteps, setTotalSteps] = useState(0);

  const form = useForm<ApplicationFormData>({
    resolver: zodResolver(applicationSchema),
    defaultValues: {
      firstName: '', lastName: '', dateOfBirth: '', gender: undefined, ssn: '', email: '', phone: '',
      street: '', city: '', state: '', zip: '', occupation: '', employer: '', annualIncome: 0,
      product: '', faceAmount: 500000, premiumMode: '', tobaccoUse: undefined,
      healthConditions: [], primaryPhysician: '', lastExamDate: '',
    },
  });

  const values = form.watch();

  const canAdvance = useCallback(() => {
    const fields = form.getValues();
    switch (step) {
      case 0: return !!(fields.firstName && fields.lastName && fields.dateOfBirth && fields.gender && fields.ssn && fields.email && fields.phone);
      case 1: return !!(fields.street && fields.city && fields.state && fields.zip && fields.occupation && fields.employer && fields.annualIncome);
      case 2: return !!(fields.product && fields.faceAmount && fields.premiumMode && fields.tobaccoUse);
      case 3: return !!(fields.healthConditions.length > 0 && fields.primaryPhysician && fields.lastExamDate);
      default: return true;
    }
  }, [step, form]);

  const handleSubmit = async (data: ApplicationFormData) => {
    setSubmitting(true);
    const steps = applicationSubmissionSteps({
      ...data,
      tobaccoUse: data.tobaccoUse === 'yes',
      address: { street: data.street, city: data.city, state: data.state, zip: data.zip },
    });
    setTotalSteps(steps.length);
    const token = sessionStorage.getItem('slc_access_token') || '';
    await executeFlow(steps, token, setApiResults);
    setSubmitting(false);
  };

  const toggleCondition = (condition: string) => {
    const current = form.getValues('healthConditions');
    if (condition === 'none') {
      form.setValue('healthConditions', ['none']);
      return;
    }
    const filtered = current.filter(c => c !== 'none');
    if (filtered.includes(condition)) {
      form.setValue('healthConditions', filtered.filter(c => c !== condition));
    } else {
      form.setValue('healthConditions', [...filtered, condition]);
    }
  };

  const productLabels: Record<string, string> = {
    term_life_10: 'Term Life - 10 Year', term_life_20: 'Term Life - 20 Year', term_life_30: 'Term Life - 30 Year',
    whole_life: 'Whole Life', universal_life: 'Universal Life',
    variable_universal: 'Variable Universal Life', indexed_universal: 'Indexed Universal Life',
  };

  const modeLabels: Record<string, string> = {
    monthly: 'Monthly', quarterly: 'Quarterly', semi_annual: 'Semi-Annual', annual: 'Annual',
  };

  return (
    <PageTransition>
      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => router.push('/underwriting/new-business')}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div className="flex items-center gap-3">
            <FilePlus2 className="h-6 w-6 text-gold" />
            <div>
              <h1 className="text-2xl font-bold tracking-tight">Submit New Application</h1>
              <p className="text-muted-foreground">Complete the application form to begin the underwriting process</p>
            </div>
          </div>
        </div>

        {/* Step indicators */}
        <div className="flex items-center gap-1">
          {stepLabels.map((label, i) => {
            const Icon = stepIcons[i];
            return (
              <div key={label} className="flex items-center gap-1 flex-1">
                <button
                  type="button"
                  onClick={() => i < step && setStep(i)}
                  className={`flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium transition-colors w-full ${
                    i === step ? 'bg-navy text-white' : i < step ? 'bg-emerald-500/10 text-emerald-700 hover:bg-emerald-500/20' : 'bg-muted text-muted-foreground'
                  }`}
                >
                  <Icon className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate">{label}</span>
                </button>
                {i < stepLabels.length - 1 && <div className="h-px w-4 bg-border shrink-0" />}
              </div>
            );
          })}
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Form Panel */}
          <div className="lg:col-span-2">
            <Form {...form}>
              <form onSubmit={form.handleSubmit(handleSubmit)}>
                <Card>
                  <CardContent className="pt-6">
                    <AnimatePresence mode="wait">
                      {/* Step 1: Applicant Information */}
                      {step === 0 && (
                        <motion.div key="step-0" initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }} transition={{ duration: 0.2 }} className="space-y-4">
                          <div>
                            <h3 className="text-lg font-semibold">Applicant Information</h3>
                            <p className="text-sm text-muted-foreground">Personal details of the proposed insured</p>
                          </div>
                          <Separator />
                          <div className="grid grid-cols-2 gap-4">
                            <FormField control={form.control} name="firstName" render={({ field }) => (<FormItem><FormLabel>First Name</FormLabel><FormControl><Input placeholder="John" {...field} /></FormControl><FormMessage /></FormItem>)} />
                            <FormField control={form.control} name="lastName" render={({ field }) => (<FormItem><FormLabel>Last Name</FormLabel><FormControl><Input placeholder="Smith" {...field} /></FormControl><FormMessage /></FormItem>)} />
                          </div>
                          <div className="grid grid-cols-3 gap-4">
                            <FormField control={form.control} name="dateOfBirth" render={({ field }) => (<FormItem><FormLabel>Date of Birth</FormLabel><FormControl><Input type="date" {...field} /></FormControl><FormMessage /></FormItem>)} />
                            <FormField control={form.control} name="gender" render={({ field }) => (
                              <FormItem><FormLabel>Gender</FormLabel>
                                <Select onValueChange={field.onChange} defaultValue={field.value}><FormControl><SelectTrigger><SelectValue placeholder="Select" /></SelectTrigger></FormControl>
                                  <SelectContent><SelectItem value="male">Male</SelectItem><SelectItem value="female">Female</SelectItem></SelectContent>
                                </Select><FormMessage />
                              </FormItem>
                            )} />
                            <FormField control={form.control} name="ssn" render={({ field }) => (<FormItem><FormLabel>Social Security Number</FormLabel><FormControl><Input placeholder="XXX-XX-XXXX" {...field} /></FormControl><FormMessage /></FormItem>)} />
                          </div>
                          <div className="grid grid-cols-2 gap-4">
                            <FormField control={form.control} name="email" render={({ field }) => (<FormItem><FormLabel>Email Address</FormLabel><FormControl><Input type="email" placeholder="john@example.com" {...field} /></FormControl><FormMessage /></FormItem>)} />
                            <FormField control={form.control} name="phone" render={({ field }) => (<FormItem><FormLabel>Phone Number</FormLabel><FormControl><Input type="tel" placeholder="(555) 555-0100" {...field} /></FormControl><FormMessage /></FormItem>)} />
                          </div>
                        </motion.div>
                      )}

                      {/* Step 2: Address & Employment */}
                      {step === 1 && (
                        <motion.div key="step-1" initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }} transition={{ duration: 0.2 }} className="space-y-4">
                          <div>
                            <h3 className="text-lg font-semibold">Address & Employment</h3>
                            <p className="text-sm text-muted-foreground">Residential address and employment information</p>
                          </div>
                          <Separator />
                          <FormField control={form.control} name="street" render={({ field }) => (<FormItem><FormLabel>Street Address</FormLabel><FormControl><Input placeholder="123 Main Street, Apt 4B" {...field} /></FormControl><FormMessage /></FormItem>)} />
                          <div className="grid grid-cols-3 gap-4">
                            <FormField control={form.control} name="city" render={({ field }) => (<FormItem><FormLabel>City</FormLabel><FormControl><Input placeholder="Austin" {...field} /></FormControl><FormMessage /></FormItem>)} />
                            <FormField control={form.control} name="state" render={({ field }) => (
                              <FormItem><FormLabel>State</FormLabel>
                                <Select onValueChange={field.onChange} defaultValue={field.value}><FormControl><SelectTrigger><SelectValue placeholder="Select state" /></SelectTrigger></FormControl>
                                  <SelectContent>{US_STATES.map(s => <SelectItem key={s} value={s}>{s}</SelectItem>)}</SelectContent>
                                </Select><FormMessage />
                              </FormItem>
                            )} />
                            <FormField control={form.control} name="zip" render={({ field }) => (<FormItem><FormLabel>ZIP Code</FormLabel><FormControl><Input placeholder="78701" {...field} /></FormControl><FormMessage /></FormItem>)} />
                          </div>
                          <Separator />
                          <div className="grid grid-cols-2 gap-4">
                            <FormField control={form.control} name="occupation" render={({ field }) => (<FormItem><FormLabel>Occupation</FormLabel><FormControl><Input placeholder="Software Engineer" {...field} /></FormControl><FormMessage /></FormItem>)} />
                            <FormField control={form.control} name="employer" render={({ field }) => (<FormItem><FormLabel>Employer</FormLabel><FormControl><Input placeholder="Acme Corp" {...field} /></FormControl><FormMessage /></FormItem>)} />
                          </div>
                          <FormField control={form.control} name="annualIncome" render={({ field }) => (<FormItem><FormLabel>Annual Income</FormLabel><FormControl><Input type="number" placeholder="120000" {...field} /></FormControl><FormMessage /></FormItem>)} />
                        </motion.div>
                      )}

                      {/* Step 3: Coverage Details */}
                      {step === 2 && (
                        <motion.div key="step-2" initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }} transition={{ duration: 0.2 }} className="space-y-4">
                          <div>
                            <h3 className="text-lg font-semibold">Coverage Details</h3>
                            <p className="text-sm text-muted-foreground">Product selection and coverage parameters</p>
                          </div>
                          <Separator />
                          <FormField control={form.control} name="product" render={({ field }) => (
                            <FormItem><FormLabel>Product</FormLabel>
                              <Select onValueChange={field.onChange} defaultValue={field.value}><FormControl><SelectTrigger><SelectValue placeholder="Select product type" /></SelectTrigger></FormControl>
                                <SelectContent>
                                  <SelectItem value="term_life_10">Term Life - 10 Year</SelectItem>
                                  <SelectItem value="term_life_20">Term Life - 20 Year</SelectItem>
                                  <SelectItem value="term_life_30">Term Life - 30 Year</SelectItem>
                                  <SelectItem value="whole_life">Whole Life</SelectItem>
                                  <SelectItem value="universal_life">Universal Life</SelectItem>
                                  <SelectItem value="variable_universal">Variable Universal Life</SelectItem>
                                  <SelectItem value="indexed_universal">Indexed Universal Life</SelectItem>
                                </SelectContent>
                              </Select><FormMessage />
                            </FormItem>
                          )} />
                          <div className="grid grid-cols-2 gap-4">
                            <FormField control={form.control} name="faceAmount" render={({ field }) => (<FormItem><FormLabel>Face Amount ($)</FormLabel><FormControl><Input type="number" step="10000" placeholder="500000" {...field} /></FormControl><FormMessage /></FormItem>)} />
                            <FormField control={form.control} name="premiumMode" render={({ field }) => (
                              <FormItem><FormLabel>Premium Mode</FormLabel>
                                <Select onValueChange={field.onChange} defaultValue={field.value}><FormControl><SelectTrigger><SelectValue placeholder="Select frequency" /></SelectTrigger></FormControl>
                                  <SelectContent>
                                    <SelectItem value="monthly">Monthly</SelectItem>
                                    <SelectItem value="quarterly">Quarterly</SelectItem>
                                    <SelectItem value="semi_annual">Semi-Annual</SelectItem>
                                    <SelectItem value="annual">Annual</SelectItem>
                                  </SelectContent>
                                </Select><FormMessage />
                              </FormItem>
                            )} />
                          </div>
                          <FormField control={form.control} name="tobaccoUse" render={({ field }) => (
                            <FormItem><FormLabel>Tobacco Use (past 12 months)</FormLabel>
                              <div className="flex gap-4 pt-1">
                                <label className="flex items-center gap-2 cursor-pointer">
                                  <input type="radio" name="tobaccoUse" value="no" checked={field.value === 'no'} onChange={() => field.onChange('no')} className="accent-navy" />
                                  <span className="text-sm">No</span>
                                </label>
                                <label className="flex items-center gap-2 cursor-pointer">
                                  <input type="radio" name="tobaccoUse" value="yes" checked={field.value === 'yes'} onChange={() => field.onChange('yes')} className="accent-navy" />
                                  <span className="text-sm">Yes</span>
                                </label>
                              </div>
                              <FormMessage />
                            </FormItem>
                          )} />
                        </motion.div>
                      )}

                      {/* Step 4: Health Information */}
                      {step === 3 && (
                        <motion.div key="step-3" initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }} transition={{ duration: 0.2 }} className="space-y-4">
                          <div>
                            <h3 className="text-lg font-semibold">Health Information</h3>
                            <p className="text-sm text-muted-foreground">Medical history and current health conditions</p>
                          </div>
                          <Separator />
                          <FormField control={form.control} name="healthConditions" render={() => (
                            <FormItem>
                              <FormLabel>Health Conditions</FormLabel>
                              <div className="grid grid-cols-2 gap-2 pt-1">
                                {HEALTH_CONDITIONS.map(condition => (
                                  <label key={condition.value} className="flex items-center gap-2 rounded-md border px-3 py-2 cursor-pointer hover:bg-muted/50 transition-colors">
                                    <input
                                      type="checkbox"
                                      checked={values.healthConditions?.includes(condition.value) ?? false}
                                      onChange={() => toggleCondition(condition.value)}
                                      className="accent-navy"
                                    />
                                    <span className="text-sm">{condition.label}</span>
                                  </label>
                                ))}
                              </div>
                              <FormMessage />
                            </FormItem>
                          )} />
                          <div className="grid grid-cols-2 gap-4">
                            <FormField control={form.control} name="primaryPhysician" render={({ field }) => (<FormItem><FormLabel>Primary Physician</FormLabel><FormControl><Input placeholder="Dr. Katherine Reeves" {...field} /></FormControl><FormMessage /></FormItem>)} />
                            <FormField control={form.control} name="lastExamDate" render={({ field }) => (<FormItem><FormLabel>Last Physical Exam</FormLabel><FormControl><Input type="date" {...field} /></FormControl><FormMessage /></FormItem>)} />
                          </div>
                        </motion.div>
                      )}

                      {/* Step 5: Review & Submit */}
                      {step === 4 && (
                        <motion.div key="step-4" initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }} transition={{ duration: 0.2 }} className="space-y-4">
                          <div>
                            <h3 className="text-lg font-semibold">Review & Submit</h3>
                            <p className="text-sm text-muted-foreground">Review all information before submitting the application</p>
                          </div>
                          <Separator />
                          <div className="grid grid-cols-2 gap-4">
                            <div className="rounded-lg border p-4 space-y-2">
                              <h4 className="text-sm font-semibold flex items-center gap-2"><User className="h-3.5 w-3.5 text-gold" />Applicant</h4>
                              <div className="space-y-1 text-sm">
                                <p><span className="text-muted-foreground">Name:</span> {values.firstName} {values.lastName}</p>
                                <p><span className="text-muted-foreground">DOB:</span> {values.dateOfBirth}</p>
                                <p><span className="text-muted-foreground">Gender:</span> {values.gender === 'male' ? 'Male' : 'Female'}</p>
                                <p><span className="text-muted-foreground">SSN:</span> ***-**-{values.ssn?.slice(-4)}</p>
                                <p><span className="text-muted-foreground">Email:</span> {values.email}</p>
                                <p><span className="text-muted-foreground">Phone:</span> {values.phone}</p>
                              </div>
                            </div>
                            <div className="rounded-lg border p-4 space-y-2">
                              <h4 className="text-sm font-semibold flex items-center gap-2"><MapPin className="h-3.5 w-3.5 text-gold" />Address & Employment</h4>
                              <div className="space-y-1 text-sm">
                                <p><span className="text-muted-foreground">Address:</span> {values.street}</p>
                                <p>{values.city}, {values.state} {values.zip}</p>
                                <p><span className="text-muted-foreground">Occupation:</span> {values.occupation}</p>
                                <p><span className="text-muted-foreground">Employer:</span> {values.employer}</p>
                                <p><span className="text-muted-foreground">Income:</span> {formatCurrency(values.annualIncome || 0)}</p>
                              </div>
                            </div>
                            <div className="rounded-lg border p-4 space-y-2">
                              <h4 className="text-sm font-semibold flex items-center gap-2"><Shield className="h-3.5 w-3.5 text-gold" />Coverage</h4>
                              <div className="space-y-1 text-sm">
                                <p><span className="text-muted-foreground">Product:</span> {productLabels[values.product] || values.product}</p>
                                <p><span className="text-muted-foreground">Face Amount:</span> {formatCurrency(values.faceAmount || 0)}</p>
                                <p><span className="text-muted-foreground">Premium Mode:</span> {modeLabels[values.premiumMode] || values.premiumMode}</p>
                                <p><span className="text-muted-foreground">Tobacco:</span> {values.tobaccoUse === 'yes' ? 'Yes' : 'No'}</p>
                              </div>
                            </div>
                            <div className="rounded-lg border p-4 space-y-2">
                              <h4 className="text-sm font-semibold flex items-center gap-2"><HeartPulse className="h-3.5 w-3.5 text-gold" />Health</h4>
                              <div className="space-y-1 text-sm">
                                <p><span className="text-muted-foreground">Conditions:</span></p>
                                <div className="flex flex-wrap gap-1">
                                  {(values.healthConditions || []).map(c => (
                                    <Badge key={c} variant="secondary" className="text-xs">{HEALTH_CONDITIONS.find(h => h.value === c)?.label || c}</Badge>
                                  ))}
                                </div>
                                <p><span className="text-muted-foreground">Physician:</span> {values.primaryPhysician}</p>
                                <p><span className="text-muted-foreground">Last Exam:</span> {values.lastExamDate}</p>
                              </div>
                            </div>
                          </div>
                        </motion.div>
                      )}
                    </AnimatePresence>

                    {/* Navigation buttons */}
                    <div className="flex items-center justify-between pt-6 mt-6 border-t">
                      <Button type="button" variant="outline" disabled={step === 0 || submitting} onClick={() => setStep(s => s - 1)}>
                        <ArrowLeft className="mr-2 h-4 w-4" />Back
                      </Button>
                      <div className="flex gap-2">
                        {step < 4 && (
                          <Button type="button" onClick={() => setStep(s => s + 1)} disabled={!canAdvance()}>
                            Continue<ArrowRight className="ml-2 h-4 w-4" />
                          </Button>
                        )}
                        {step === 4 && (
                          <Button type="submit" variant="gold" disabled={submitting}>
                            <Send className="mr-2 h-4 w-4" />{submitting ? 'Submitting...' : 'Submit Application'}
                          </Button>
                        )}
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </form>
            </Form>
          </div>

          {/* API Call Tracker Panel */}
          <div className="lg:col-span-1">
            <Card className="sticky top-6">
              <CardHeader className="pb-3">
                <CardTitle className="text-sm">API Call Tracker</CardTitle>
                <CardDescription className="text-xs">
                  {totalSteps > 0 ? `${totalSteps} API calls across 7 phases` : 'Submit the application to begin processing'}
                </CardDescription>
              </CardHeader>
              <CardContent>
                {apiResults.length > 0 ? (
                  <ApiCallTracker results={apiResults} totalSteps={totalSteps} />
                ) : (
                  <div className="text-center py-8 text-muted-foreground">
                    <Send className="h-8 w-8 mx-auto mb-2 opacity-20" />
                    <p className="text-xs">Complete the form and click Submit to see API calls cascade in real-time</p>
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
