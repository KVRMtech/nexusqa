'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { motion } from 'framer-motion';
import { useAuth } from '@/lib/providers';
import { loginSchema, type LoginFormData } from '@/lib/validations';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { Separator } from '@/components/ui/separator';
import { Shield, Lock, KeyRound } from 'lucide-react';

export default function SignInPage() {
  const router = useRouter();
  const { login } = useAuth();
  const [showMfa, setShowMfa] = useState(false);
  const [isLoading, setIsLoading] = useState(false);

  const form = useForm<LoginFormData>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: '', password: '' },
  });

  async function onSubmit(data: LoginFormData) {
    setIsLoading(true);
    await new Promise(r => setTimeout(r, 1200));

    if (!showMfa) {
      setShowMfa(true);
      setIsLoading(false);
      return;
    }

    login(data.email);
    router.push('/dashboard/overview');
  }

  return (
    <div className="min-h-screen flex">
      <div className="hidden lg:flex lg:w-1/2 bg-navy flex-col justify-between p-12">
        <div>
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-gold text-navy-dark font-bold">SL</div>
            <div>
              <div className="text-lg font-semibold text-gold">Summit Life Insurance</div>
              <div className="text-xs text-white/50 tracking-wider uppercase">Carrier Administration Platform</div>
            </div>
          </div>
        </div>
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.3 }}>
          <h1 className="text-3xl font-bold text-white mb-4">Enterprise Policy Administration</h1>
          <p className="text-white/60 text-lg leading-relaxed">Underwriting workbench, policy issuance, service transactions, and claims adjudication in a single unified platform.</p>
          <div className="mt-8 grid grid-cols-2 gap-4">
            {[
              { label: 'Active Policies', value: '47,832' },
              { label: 'YTD Issued', value: '$2.4B' },
              { label: 'Claims Paid', value: '$185M' },
              { label: 'Avg Cycle Time', value: '4.2 days' },
            ].map(stat => (
              <div key={stat.label} className="bg-white/5 rounded-lg p-4">
                <div className="text-2xl font-bold text-gold">{stat.value}</div>
                <div className="text-xs text-white/40 mt-1">{stat.label}</div>
              </div>
            ))}
          </div>
        </motion.div>
        <div className="text-xs text-white/30">SOC 2 Type II Certified | HIPAA Compliant | State Insurance Department Approved</div>
      </div>

      <div className="flex-1 flex items-center justify-center p-8 bg-background">
        <motion.div initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: 0.3 }} className="w-full max-w-md">
          <Card className="border-0 shadow-xl">
            <CardHeader className="space-y-1 text-center">
              <div className="flex justify-center mb-2"><Shield className="h-8 w-8 text-gold" /></div>
              <CardTitle className="text-2xl">Secure Sign In</CardTitle>
              <CardDescription>OAuth 2.0 / OpenID Connect Authentication</CardDescription>
            </CardHeader>
            <CardContent>
              <Form {...form}>
                <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
                  <FormField control={form.control} name="email" render={({ field }) => (
                    <FormItem>
                      <FormLabel>Corporate Email</FormLabel>
                      <FormControl><Input placeholder="user@summitlife.com" type="email" autoComplete="email" {...field} /></FormControl>
                      <FormMessage />
                    </FormItem>
                  )} />
                  <FormField control={form.control} name="password" render={({ field }) => (
                    <FormItem>
                      <FormLabel>Password</FormLabel>
                      <FormControl><Input placeholder="Enter your password" type="password" autoComplete="current-password" {...field} /></FormControl>
                      <FormMessage />
                    </FormItem>
                  )} />

                  {showMfa && (
                    <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }}>
                      <FormField control={form.control} name="mfaCode" render={({ field }) => (
                        <FormItem>
                          <FormLabel className="flex items-center gap-2"><KeyRound className="h-3 w-3" />MFA Verification Code</FormLabel>
                          <FormControl><Input placeholder="000000" maxLength={6} inputMode="numeric" {...field} /></FormControl>
                          <FormMessage />
                        </FormItem>
                      )} />
                    </motion.div>
                  )}

                  <Button type="submit" className="w-full" variant="gold" disabled={isLoading}>
                    {isLoading ? (
                      <span className="flex items-center gap-2"><span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />{showMfa ? 'Verifying...' : 'Authenticating...'}</span>
                    ) : showMfa ? (
                      <span className="flex items-center gap-2"><Lock className="h-4 w-4" />Verify & Sign In</span>
                    ) : 'Continue'}
                  </Button>
                </form>
              </Form>

              <div className="relative my-6">
                <Separator />
                <span className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 bg-card px-2 text-xs text-muted-foreground">or</span>
              </div>

              <div className="space-y-2">
                <Button variant="outline" className="w-full" type="button">
                  <svg className="mr-2 h-4 w-4" viewBox="0 0 24 24"><path fill="currentColor" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"/><path fill="currentColor" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="currentColor" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/><path fill="currentColor" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>
                  Sign in with Google SSO
                </Button>
                <Button variant="outline" className="w-full" type="button">
                  <svg className="mr-2 h-4 w-4" viewBox="0 0 24 24"><path fill="currentColor" d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>
                  Sign in with Enterprise SSO
                </Button>
              </div>

              <p className="mt-6 text-center text-xs text-muted-foreground">Protected by enterprise-grade OAuth 2.0 with PKCE flow. All sessions are encrypted and audited.</p>
            </CardContent>
          </Card>
        </motion.div>
      </div>
    </div>
  );
}
