import { NextRequest } from 'next/server';
import { serviceResponse, requireAuth } from '@/lib/api-factory';

const SERVICE = 'commission-calculation';

export async function POST(request: NextRequest) {
  const denied = requireAuth(request, SERVICE);
  if (denied) return denied;

  return serviceResponse(SERVICE, {
    commissionId: `com-${crypto.randomUUID().slice(0, 8)}`,
    agentId: 'AGT-001',
    rate: 0.55,
    firstYearCommission: 2681.25,
    renewalRate: 0.05,
    overrideRate: 0.10,
  });
}
