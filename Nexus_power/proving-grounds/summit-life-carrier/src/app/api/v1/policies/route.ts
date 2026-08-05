import { NextRequest, NextResponse } from 'next/server';
import { policies } from '@/lib/data';

export async function GET(request: NextRequest) {
  const { searchParams } = new URL(request.url);
  const status = searchParams.get('status');
  const page = parseInt(searchParams.get('page') || '1');
  const limit = parseInt(searchParams.get('limit') || '25');

  let filtered = [...policies];
  if (status) filtered = filtered.filter(p => p.status === status);

  const start = (page - 1) * limit;
  const paged = filtered.slice(start, start + limit);

  return NextResponse.json({
    data: paged.map(p => ({
      id: p.id,
      policyNumber: p.policyNumber,
      product: p.product,
      insured: p.insured,
      owner: p.owner,
      faceAmount: p.faceAmount,
      premiumAmount: p.premiumAmount,
      premiumMode: p.premiumMode,
      status: p.status,
      effectiveDate: p.effectiveDate,
      cashValue: p.cashValue,
      loanBalance: p.loanBalance,
    })),
    meta: { total: filtered.length, page, limit, totalPages: Math.ceil(filtered.length / limit) },
  });
}
