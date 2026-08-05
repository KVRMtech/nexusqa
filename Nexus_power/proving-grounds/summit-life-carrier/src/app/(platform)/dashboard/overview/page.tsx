'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { PageTransition, StaggerContainer, StaggerItem } from '@/components/domain/page-transition';
import { applications, policies, claims } from '@/lib/data';
import { formatCurrency, formatDate } from '@/lib/utils';
import { useAuth } from '@/lib/providers';
import { FileSearch, Shield, ClipboardList, TrendingUp, AlertTriangle, Clock } from 'lucide-react';
import Link from 'next/link';

const statusColor: Record<string, 'default' | 'success' | 'warning' | 'destructive' | 'info'> = {
  submitted: 'info', in_review: 'warning', requirements_pending: 'destructive', underwriting: 'warning', approved: 'success', declined: 'destructive',
  active: 'success', lapsed: 'destructive', reported: 'warning', under_investigation: 'info',
};

export default function DashboardOverviewPage() {
  const { user } = useAuth();
  const pendingApps = applications.filter(a => !['approved', 'declined', 'withdrawn'].includes(a.status));
  const openClaims = claims.filter(c => !['approved', 'denied', 'closed'].includes(c.status));

  return (
    <PageTransition>
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Welcome back, {user?.name?.split(' ')[0]}</h1>
          <p className="text-muted-foreground">Here is your operational overview for today.</p>
        </div>

        <StaggerContainer className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          {[
            { title: 'Pending Applications', value: String(pendingApps.length), icon: FileSearch, description: `${applications.filter(a => a.status === 'submitted').length} new submissions`, color: 'text-blue-600' },
            { title: 'Active Policies', value: String(policies.filter(p => p.status === 'active').length), icon: Shield, description: formatCurrency(policies.reduce((s, p) => s + p.faceAmount, 0)) + ' total face', color: 'text-emerald-600' },
            { title: 'Open Claims', value: String(openClaims.length), icon: ClipboardList, description: formatCurrency(openClaims.reduce((s, c) => s + c.benefitAmount, 0)) + ' exposure', color: 'text-amber-600' },
            { title: 'Portfolio Performance', value: '98.2%', icon: TrendingUp, description: 'Persistency rate (13-month)', color: 'text-violet-600' },
          ].map(stat => (
            <StaggerItem key={stat.title}>
              <Card>
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                  <CardTitle className="text-sm font-medium">{stat.title}</CardTitle>
                  <stat.icon className={`h-4 w-4 ${stat.color}`} />
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-bold">{stat.value}</div>
                  <p className="text-xs text-muted-foreground">{stat.description}</p>
                </CardContent>
              </Card>
            </StaggerItem>
          ))}
        </StaggerContainer>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between">
              <CardTitle className="text-lg">Underwriting Queue</CardTitle>
              <Link href="/underwriting/new-business" className="text-sm text-gold hover:underline">View all</Link>
            </CardHeader>
            <CardContent className="space-y-3">
              {applications.slice(0, 4).map(app => (
                <Link key={app.id} href={`/underwriting/new-business/${app.id}/review`} className="flex items-center justify-between p-3 rounded-lg border hover:bg-muted/50 transition-colors">
                  <div className="space-y-1">
                    <div className="font-medium text-sm">{app.applicant.firstName} {app.applicant.lastName}</div>
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <span>{app.caseNumber}</span>
                      <span>{formatCurrency(app.faceAmount)}</span>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant={statusColor[app.status] || 'default'}>{app.status.replace(/_/g, ' ')}</Badge>
                    {app.status === 'requirements_pending' && <AlertTriangle className="h-3 w-3 text-amber-500" />}
                  </div>
                </Link>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between">
              <CardTitle className="text-lg">Recent Activity</CardTitle>
              <Badge variant="outline" className="gap-1"><Clock className="h-3 w-3" />Live</Badge>
            </CardHeader>
            <CardContent className="space-y-4">
              {[
                { action: 'Application submitted', detail: `${applications[3].applicant.firstName} ${applications[3].applicant.lastName} — ${formatCurrency(applications[3].faceAmount)} ${applications[3].product.replace(/_/g, ' ')}`, time: '2 hours ago', type: 'info' as const },
                { action: 'Vendor report received', detail: 'Paramedical exam results for UW-2026-00142', time: '4 hours ago', type: 'success' as const },
                { action: 'Claim reported', detail: `${claims[0].claimNumber} — ${claims[0].insured.firstName} ${claims[0].insured.lastName}`, time: '1 day ago', type: 'warning' as const },
                { action: 'Policy issued', detail: `${policies[0].policyNumber} — William Okonkwo`, time: '3 days ago', type: 'success' as const },
                { action: 'Suspense created', detail: 'Awaiting APS for UW-2026-00143', time: '5 days ago', type: 'destructive' as const },
              ].map((event, i) => (
                <div key={i} className="flex gap-3">
                  <div className={`mt-1 h-2 w-2 rounded-full shrink-0 ${event.type === 'success' ? 'bg-emerald-500' : event.type === 'warning' ? 'bg-amber-500' : event.type === 'destructive' ? 'bg-red-500' : 'bg-blue-500'}`} />
                  <div className="space-y-0.5 min-w-0">
                    <div className="text-sm font-medium">{event.action}</div>
                    <div className="text-xs text-muted-foreground truncate">{event.detail}</div>
                    <div className="text-[10px] text-muted-foreground/60">{event.time}</div>
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>
      </div>
    </PageTransition>
  );
}
