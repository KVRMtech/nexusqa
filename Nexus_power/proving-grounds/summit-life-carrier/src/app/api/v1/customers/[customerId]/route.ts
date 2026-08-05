import { NextRequest } from 'next/server';
import { serviceResponse, errorResponse, requireAuth } from '@/lib/api-factory';
import { customers } from '@/lib/data';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ customerId: string }> },
) {
  const denied = requireAuth(request, 'CustomerService');
  if (denied) return denied;

  const { customerId } = await params;
  const customer = customers.find((c) => c.id === customerId);
  if (!customer) return errorResponse('CustomerService', 'Customer not found', 404);

  return serviceResponse('CustomerService', { customer });
}

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ customerId: string }> },
) {
  const denied = requireAuth(request, 'CustomerService');
  if (denied) return denied;

  const { customerId } = await params;
  const customer = customers.find((c) => c.id === customerId);
  if (!customer) return errorResponse('CustomerService', 'Customer not found', 404);

  const body = await request.json();
  const updated = { ...customer, ...body, id: customer.id };

  return serviceResponse('CustomerService', { customer: updated });
}
