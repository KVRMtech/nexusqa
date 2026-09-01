import { NextRequest } from 'next/server';
import { serviceResponse, requireAuth } from '@/lib/api-factory';

const SERVICE = 'payment-details';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ paymentId: string }> },
) {
  const denied = requireAuth(request, SERVICE);
  if (denied) return denied;

  const { paymentId } = await params;
  return serviceResponse(SERVICE, {
    paymentId,
    policyId: 'pol-3a8c21',
    amount: 4875.0,
    status: 'processed',
    paymentMethod: 'bank_account',
    last4: '4821',
    referenceNumber: 'REF-2026-74821',
    processedAt: '2026-07-28T10:15:00Z',
    allocations: [
      { component: 'mortality', amount: 1950 },
      { component: 'expense', amount: 975 },
      { component: 'cash_value', amount: 1200 },
      { component: 'profit', amount: 750 },
    ],
  });
}
