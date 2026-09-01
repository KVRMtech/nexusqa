import { NextRequest } from 'next/server';
import { serviceResponse, requireAuth } from '@/lib/api-factory';

const SERVICE = 'bank-account-verification';

export async function POST(request: NextRequest) {
  const denied = requireAuth(request, SERVICE);
  if (denied) return denied;

  return serviceResponse(SERVICE, {
    verificationId: `vrf-${crypto.randomUUID().slice(0, 8)}`,
    status: 'micro_deposits_initiated',
    depositsExpectedBy: '2026-08-05',
    method: 'micro_deposit',
  });
}
