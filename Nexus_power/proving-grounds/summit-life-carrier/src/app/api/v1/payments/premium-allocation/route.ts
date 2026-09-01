import { NextRequest } from 'next/server';
import { serviceResponse, errorResponse, requireAuth } from '@/lib/api-factory';

const SERVICE = 'premium-allocation';

export async function POST(request: NextRequest) {
  const denied = requireAuth(request, SERVICE);
  if (denied) return denied;

  const body = await request.json();
  if (!body.policyId) return errorResponse(SERVICE, 'policyId is required');

  return serviceResponse(SERVICE, {
    allocationId: `alloc-${crypto.randomUUID().slice(0, 8)}`,
    policyId: body.policyId,
    breakdown: [
      { component: 'mortality', amount: 1950 },
      { component: 'expense', amount: 975 },
      { component: 'cash_value', amount: 1200 },
      { component: 'profit', amount: 750 },
    ],
    totalAllocated: 4875,
  });
}
