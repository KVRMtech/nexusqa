'use client';

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { motion } from 'framer-motion';
import { rateTable } from '@/lib/data';
import { rateQuoteSchema, type RateQuoteFormData } from '@/lib/validations';
import { formatCurrency } from '@/lib/utils';
import { PageTransition } from '@/components/domain/page-transition';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Separator } from '@/components/ui/separator';
import { Calculator, TrendingUp, DollarSign, BarChart3 } from 'lucide-react';

export default function ProductPricingPage() {
  const [quote, setQuote] = useState<{ annual: number; monthly: number; rate: number; mortality: number; expense: number; profit: number } | null>(null);

  const form = useForm<RateQuoteFormData>({
    resolver: zodResolver(rateQuoteSchema),
    defaultValues: { age: 35, gender: 'male', tobaccoUse: false, faceAmount: 500000, product: 'term_life_20', riskClass: 'preferred' },
  });

  function onSubmit(data: RateQuoteFormData) {
    const match = rateTable.find(r => r.age <= data.age && r.gender === data.gender && r.tobaccoUse === data.tobaccoUse && r.riskClass === data.riskClass && r.product === data.product);
    const rate = match?.ratePerThousand || 1.50;
    const annual = (data.faceAmount / 1000) * rate;
    setQuote({
      annual,
      monthly: annual / 12,
      rate,
      mortality: match?.mortalityFactor || rate * 0.72,
      expense: match?.expenseFactor || rate * 0.15,
      profit: match?.profitMargin || rate * 0.13,
    });
  }

  return (
    <PageTransition>
      <div className="space-y-6">
        <div className="flex items-center gap-3">
          <Calculator className="h-6 w-6 text-gold" />
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Product Pricing Engine</h1>
            <p className="text-muted-foreground">Generate premium quotes and view rate tables</p>
          </div>
        </div>

        <div className="grid gap-6 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Rate Quote Calculator</CardTitle>
              <CardDescription>Enter applicant parameters to calculate premium</CardDescription>
            </CardHeader>
            <CardContent>
              <Form {...form}>
                <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
                  <div className="grid grid-cols-2 gap-4">
                    <FormField control={form.control} name="age" render={({ field }) => (<FormItem><FormLabel>Age</FormLabel><FormControl><Input type="number" min={18} max={85} {...field} onChange={e => field.onChange(parseInt(e.target.value))} /></FormControl><FormMessage /></FormItem>)} />
                    <FormField control={form.control} name="gender" render={({ field }) => (
                      <FormItem><FormLabel>Gender</FormLabel><Select onValueChange={field.onChange} defaultValue={field.value}><FormControl><SelectTrigger><SelectValue /></SelectTrigger></FormControl><SelectContent><SelectItem value="male">Male</SelectItem><SelectItem value="female">Female</SelectItem></SelectContent></Select><FormMessage /></FormItem>
                    )} />
                  </div>
                  <FormField control={form.control} name="product" render={({ field }) => (
                    <FormItem><FormLabel>Product</FormLabel><Select onValueChange={field.onChange} defaultValue={field.value}><FormControl><SelectTrigger><SelectValue /></SelectTrigger></FormControl>
                      <SelectContent>
                        <SelectItem value="term_life_10">Term Life 10</SelectItem><SelectItem value="term_life_20">Term Life 20</SelectItem><SelectItem value="term_life_30">Term Life 30</SelectItem>
                        <SelectItem value="whole_life">Whole Life</SelectItem><SelectItem value="universal_life">Universal Life</SelectItem>
                        <SelectItem value="indexed_universal">Indexed Universal Life</SelectItem><SelectItem value="variable_universal">Variable Universal Life</SelectItem>
                      </SelectContent></Select><FormMessage /></FormItem>
                  )} />
                  <FormField control={form.control} name="faceAmount" render={({ field }) => (<FormItem><FormLabel>Face Amount</FormLabel><FormControl><Input type="number" min={25000} step={25000} {...field} onChange={e => field.onChange(parseInt(e.target.value))} /></FormControl><FormMessage /></FormItem>)} />
                  <FormField control={form.control} name="riskClass" render={({ field }) => (
                    <FormItem><FormLabel>Risk Classification</FormLabel><Select onValueChange={field.onChange} defaultValue={field.value}><FormControl><SelectTrigger><SelectValue /></SelectTrigger></FormControl>
                      <SelectContent>
                        <SelectItem value="preferred_plus">Preferred Plus</SelectItem><SelectItem value="preferred">Preferred</SelectItem><SelectItem value="standard_plus">Standard Plus</SelectItem>
                        <SelectItem value="standard">Standard</SelectItem><SelectItem value="substandard_table_a">Substandard Table A</SelectItem><SelectItem value="substandard_table_b">Substandard Table B</SelectItem>
                      </SelectContent></Select><FormMessage /></FormItem>
                  )} />
                  <div className="flex items-center gap-3">
                    <FormField control={form.control} name="tobaccoUse" render={({ field }) => (
                      <FormItem className="flex items-center gap-2">
                        <FormControl><input type="checkbox" className="h-4 w-4 rounded border-input" checked={field.value} onChange={field.onChange} /></FormControl>
                        <FormLabel className="!mt-0">Tobacco Use</FormLabel>
                      </FormItem>
                    )} />
                  </div>
                  <Button type="submit" variant="gold" className="w-full"><Calculator className="mr-2 h-4 w-4" />Calculate Premium</Button>
                </form>
              </Form>
            </CardContent>
          </Card>

          {quote ? (
            <motion.div initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }}>
              <Card>
                <CardHeader><CardTitle className="flex items-center gap-2"><DollarSign className="h-5 w-5 text-gold" />Premium Quote</CardTitle></CardHeader>
                <CardContent className="space-y-6">
                  <div className="grid grid-cols-2 gap-4">
                    <div className="p-4 bg-gold/10 rounded-lg text-center"><div className="text-3xl font-bold text-gold">{formatCurrency(quote.annual)}</div><div className="text-sm text-muted-foreground">Annual Premium</div></div>
                    <div className="p-4 bg-muted rounded-lg text-center"><div className="text-3xl font-bold">{formatCurrency(quote.monthly)}</div><div className="text-sm text-muted-foreground">Monthly Equivalent</div></div>
                  </div>
                  <Separator />
                  <div>
                    <h4 className="text-sm font-semibold mb-3 flex items-center gap-2"><BarChart3 className="h-4 w-4" />Rate Decomposition</h4>
                    <div className="space-y-2">
                      <div className="flex justify-between text-sm"><span>Rate per $1,000</span><span className="font-mono">${quote.rate.toFixed(2)}</span></div>
                      <div className="flex justify-between text-sm text-muted-foreground"><span>Mortality Cost</span><span className="font-mono">${quote.mortality.toFixed(2)}</span></div>
                      <div className="flex justify-between text-sm text-muted-foreground"><span>Expense Loading</span><span className="font-mono">${quote.expense.toFixed(2)}</span></div>
                      <div className="flex justify-between text-sm text-muted-foreground"><span>Profit Margin</span><span className="font-mono">${quote.profit.toFixed(2)}</span></div>
                    </div>
                    <div className="mt-4 w-full h-3 rounded-full bg-muted overflow-hidden flex">
                      <div className="bg-blue-500 h-full" style={{ width: `${(quote.mortality / quote.rate) * 100}%` }} />
                      <div className="bg-amber-500 h-full" style={{ width: `${(quote.expense / quote.rate) * 100}%` }} />
                      <div className="bg-emerald-500 h-full" style={{ width: `${(quote.profit / quote.rate) * 100}%` }} />
                    </div>
                    <div className="flex justify-between text-[10px] text-muted-foreground mt-1">
                      <span>Mortality</span><span>Expense</span><span>Profit</span>
                    </div>
                  </div>
                </CardContent>
              </Card>
            </motion.div>
          ) : (
            <Card className="flex items-center justify-center min-h-[400px]">
              <div className="text-center text-muted-foreground">
                <TrendingUp className="h-12 w-12 mx-auto mb-3 opacity-30" />
                <p className="font-medium">Enter parameters to generate quote</p>
                <p className="text-sm">Rate calculation with mortality, expense, and profit breakdown</p>
              </div>
            </Card>
          )}
        </div>

        <Card>
          <CardHeader><CardTitle>Rate Table (Sample)</CardTitle></CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Age</TableHead><TableHead>Gender</TableHead><TableHead>Tobacco</TableHead><TableHead>Risk Class</TableHead>
                  <TableHead>Product</TableHead><TableHead>Rate/$1K</TableHead><TableHead>Mortality</TableHead><TableHead>Expense</TableHead><TableHead>Profit</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rateTable.slice(0, 15).map((r, i) => (
                  <TableRow key={i}>
                    <TableCell>{r.age}</TableCell>
                    <TableCell className="capitalize">{r.gender}</TableCell>
                    <TableCell>{r.tobaccoUse ? <Badge variant="destructive" className="text-[10px]">Yes</Badge> : 'No'}</TableCell>
                    <TableCell className="text-xs">{r.riskClass.replace(/_/g, ' ')}</TableCell>
                    <TableCell className="text-xs">{r.product.replace(/_/g, ' ')}</TableCell>
                    <TableCell className="font-mono">${r.ratePerThousand.toFixed(2)}</TableCell>
                    <TableCell className="font-mono text-muted-foreground">${r.mortalityFactor.toFixed(2)}</TableCell>
                    <TableCell className="font-mono text-muted-foreground">${r.expenseFactor.toFixed(2)}</TableCell>
                    <TableCell className="font-mono text-muted-foreground">${r.profitMargin.toFixed(2)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </PageTransition>
  );
}
