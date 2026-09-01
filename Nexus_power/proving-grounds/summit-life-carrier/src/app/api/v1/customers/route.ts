import { NextRequest } from 'next/server';
import { serviceResponse, requireAuth } from '@/lib/api-factory';
import { customers } from '@/lib/data';

export async function GET(request: NextRequest) {
  const denied = requireAuth(request, 'CustomerService');
  if (denied) return denied;

  const url = request.nextUrl;
  const page = Math.max(1, Number(url.searchParams.get('page') ?? 1));
  const limit = Math.max(1, Number(url.searchParams.get('limit') ?? 25));
  const start = (page - 1) * limit;

  return serviceResponse('CustomerService', {
    customers: customers.slice(start, start + limit),
    meta: { total: customers.length, page, limit },
  });
}

export async function POST(request: NextRequest) {
  const denied = requireAuth(request, 'CustomerService');
  if (denied) return denied;

  const body = await request.json();
  const customer = {
    id: `cust-${crypto.randomUUID().slice(0, 8)}`,
    ...body,
    status: body.status ?? 'prospect',
    policyCount: 0,
    createdAt: new Date().toISOString(),
  };

  return serviceResponse('CustomerService', { customer }, { status: 201 });
}
