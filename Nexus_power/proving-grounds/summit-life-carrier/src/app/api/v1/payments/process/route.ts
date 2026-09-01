import { NextRequest } from 'next/server';
import { serviceResponse, errorResponse, requireAuth } from '@/lib/api-factory';

const SERVICE = 'payment-processing';

export async function POST(request: NextRequest) {
  const denied = requireAuth(request, SERVICE);
  if (denied) return denied;

  const body = await request.json();
  if (!body.policyId || !body.amount || !body.paymentMethodId) {
    return errorResponse(SERVICE, 'policyId, amount, and paymentMethodId are required');
  }

  return serviceResponse(SERVICE, {
    paymentId: `pay-${crypto.randomUUID().slice(0, 8)}`,
    status: 'processed',
    amount: body.amount,
    referenceNumber: `REF-2026-${Math.floor(Math.random() * 100000)}`,
    processedAt: new Date().toISOString(),
  });
}
