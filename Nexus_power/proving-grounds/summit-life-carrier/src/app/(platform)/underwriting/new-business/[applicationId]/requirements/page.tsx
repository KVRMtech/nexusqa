'use client';

import { useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { motion, AnimatePresence } from 'framer-motion';
import { applications, vendorOrders } from '@/lib/data';
import { vendorOrderSchema, type VendorOrderFormData } from '@/lib/validations';
import { formatCurrency, formatDate } from '@/lib/utils';
import { PageTransition } from '@/components/domain/page-transition';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { ArrowLeft, Plus, CheckCircle2, Clock, AlertTriangle, FileText, Stethoscope, Car, Database, Pill, Search, ClipboardCheck } from 'lucide-react';

const VENDOR_CATALOG = [
  { name: 'MVR' as const, label: 'Motor Vehicle Report', icon: Car, cost: 12.50, turnaround: '1-2 business days', description: 'Driving history from state DMV' },
  { name: 'MIB' as const, label: 'MIB Check', icon: Database, cost: 8.00, turnaround: '4-8 hours', description: 'Medical Information Bureau code check' },
  { name: 'ESI' as const, label: 'Electronic Short Interview', icon: ClipboardCheck, cost: 22.00, turnaround: '1-3 business days', description: 'Telephone health interview' },
  { name: 'Rx_PBM' as const, label: 'Prescription History', icon: Pill, cost: 15.00, turnaround: '12-24 hours', description: 'Pharmacy benefit manager prescription history' },
  { name: 'APS' as const, label: 'Attending Physician Statement', icon: FileText, cost: 35.00, turnaround: '10-21 business days', description: 'Medical records from treating physician' },
  { name: 'Paramedical' as const, label: 'Paramedical Examination', icon: Stethoscope, cost: 145.00, turnaround: '3-7 business days', description: 'Physical exam including blood and urine samples' },
  { name: 'Inspection' as const, label: 'Inspection Report', icon: Search, cost: 85.00, turnaround: '5-10 business days', description: 'Financial and personal background verification' },
];

export default function RequirementsPage() {
  const params = useParams();
  const router = useRouter();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [orderingVendor, setOrderingVendor] = useState<string | null>(null);

  const app = applications.find(a => a.id === params.applicationId)!;
  if (!app) return <div className="p-8 text-center text-muted-foreground">Application not found</div>;

  const orders = vendorOrders.filter(v => v.applicationId === app.id);
  const orderedVendors = new Set(orders.map(o => o.vendor));

  const form = useForm<VendorOrderFormData>({
    resolver: zodResolver(vendorOrderSchema),
    defaultValues: { applicationId: app.id, priority: 'routine' },
  });

  function onSubmit(data: VendorOrderFormData) {
    setOrderingVendor(data.vendor);
    setTimeout(() => {
      setOrderingVendor(null);
      setDialogOpen(false);
      form.reset({ applicationId: app.id, priority: 'routine' });
    }, 1500);
  }

  const statusIcon = (status: string) => {
    if (status === 'received' || status === 'reviewed') return <CheckCircle2 className="h-4 w-4 text-emerald-500" />;
    if (status === 'pending') return <AlertTriangle className="h-4 w-4 text-red-500" />;
    return <Clock className="h-4 w-4 text-amber-500" />;
  };

  return (
    <PageTransition>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Button variant="ghost" size="icon" onClick={() => router.back()}><ArrowLeft className="h-4 w-4" /></Button>
            <div>
              <h1 className="text-2xl font-bold">Requirements & Vendor Orders</h1>
              <p className="text-muted-foreground">{app.caseNumber} | {app.applicant.firstName} {app.applicant.lastName}</p>
            </div>
          </div>
          <Button variant="gold" onClick={() => setDialogOpen(true)}><Plus className="mr-2 h-4 w-4" />Order Report</Button>
        </div>

        <div className="grid gap-4 md:grid-cols-3">
          <Card><CardContent className="pt-6 text-center"><div className="text-3xl font-bold">{orders.length}</div><div className="text-sm text-muted-foreground">Total Orders</div></CardContent></Card>
          <Card><CardContent className="pt-6 text-center"><div className="text-3xl font-bold text-emerald-600">{orders.filter(o => o.status === 'received').length}</div><div className="text-sm text-muted-foreground">Reports Received</div></CardContent></Card>
          <Card><CardContent className="pt-6 text-center"><div className="text-3xl font-bold">{formatCurrency(orders.reduce((s, o) => s + o.cost, 0))}</div><div className="text-sm text-muted-foreground">Total Cost</div></CardContent></Card>
        </div>

        <Card>
          <CardHeader><CardTitle>Vendor Reports</CardTitle></CardHeader>
          <CardContent>
            <div className="space-y-3">
              <AnimatePresence>
                {orders.map(order => {
                  const vendor = VENDOR_CATALOG.find(v => v.name === order.vendor);
                  return (
                    <motion.div key={order.id} initial={{ opacity: 0, x: -20 }} animate={{ opacity: 1, x: 0 }} className="flex items-center justify-between p-4 rounded-lg border">
                      <div className="flex items-center gap-4">
                        {statusIcon(order.status)}
                        <div>
                          <div className="font-medium">{vendor?.label || order.vendor}</div>
                          <div className="text-xs text-muted-foreground">Ordered {formatDate(order.orderedAt)}{order.receivedAt && ` | Received ${formatDate(order.receivedAt)}`}</div>
                        </div>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className="text-sm font-mono text-muted-foreground">{formatCurrency(order.cost)}</span>
                        <Badge variant={order.status === 'received' ? 'success' : order.status === 'pending' ? 'destructive' : 'warning'}>{order.status}</Badge>
                      </div>
                    </motion.div>
                  );
                })}
              </AnimatePresence>
              {orders.length === 0 && <p className="text-center py-8 text-muted-foreground">No vendor reports ordered yet. Click &quot;Order Report&quot; to begin.</p>}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>Available Reports</CardTitle></CardHeader>
          <CardContent>
            <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
              {VENDOR_CATALOG.map(vendor => {
                const ordered = orderedVendors.has(vendor.name);
                return (
                  <div key={vendor.name} className={`p-4 rounded-lg border ${ordered ? 'bg-muted/50 opacity-60' : 'hover:border-gold/50'} transition-colors`}>
                    <div className="flex items-start justify-between mb-2">
                      <vendor.icon className="h-5 w-5 text-gold" />
                      {ordered && <Badge variant="success" className="text-[10px]">Ordered</Badge>}
                    </div>
                    <div className="font-medium text-sm">{vendor.label}</div>
                    <div className="text-xs text-muted-foreground mt-1">{vendor.description}</div>
                    <div className="flex justify-between mt-3 text-xs">
                      <span className="text-muted-foreground">{vendor.turnaround}</span>
                      <span className="font-medium">{formatCurrency(vendor.cost)}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>

        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Order Vendor Report</DialogTitle>
              <DialogDescription>Select the report type and priority for {app.applicant.firstName} {app.applicant.lastName}</DialogDescription>
            </DialogHeader>
            <Form {...form}>
              <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
                <FormField control={form.control} name="vendor" render={({ field }) => (
                  <FormItem>
                    <FormLabel>Report Type</FormLabel>
                    <Select onValueChange={field.onChange} defaultValue={field.value}><FormControl><SelectTrigger><SelectValue placeholder="Select vendor report" /></SelectTrigger></FormControl>
                      <SelectContent>{VENDOR_CATALOG.filter(v => !orderedVendors.has(v.name)).map(v => <SelectItem key={v.name} value={v.name}>{v.label} ({formatCurrency(v.cost)})</SelectItem>)}</SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )} />
                <FormField control={form.control} name="priority" render={({ field }) => (
                  <FormItem>
                    <FormLabel>Priority</FormLabel>
                    <Select onValueChange={field.onChange} defaultValue={field.value}><FormControl><SelectTrigger><SelectValue /></SelectTrigger></FormControl>
                      <SelectContent><SelectItem value="routine">Routine</SelectItem><SelectItem value="rush">Rush (+50%)</SelectItem><SelectItem value="stat">STAT (+100%)</SelectItem></SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )} />
                <FormField control={form.control} name="notes" render={({ field }) => (
                  <FormItem><FormLabel>Notes (Optional)</FormLabel><FormControl><Textarea placeholder="Special instructions for this order..." {...field} /></FormControl><FormMessage /></FormItem>
                )} />
                <DialogFooter>
                  <Button type="button" variant="outline" onClick={() => setDialogOpen(false)}>Cancel</Button>
                  <Button type="submit" variant="gold" disabled={!!orderingVendor}>{orderingVendor ? 'Placing Order...' : 'Place Order'}</Button>
                </DialogFooter>
              </form>
            </Form>
          </DialogContent>
        </Dialog>
      </div>
    </PageTransition>
  );
}
