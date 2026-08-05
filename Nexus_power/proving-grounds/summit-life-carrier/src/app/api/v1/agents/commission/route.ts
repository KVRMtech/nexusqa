import { NextRequest } from 'next/server';
import { serviceResponse, requireAuth } from '@/lib/api-factory';

export async function POST(request: NextRequest) {
  const authErr = requireAuth(request, 'AgentService');
  if (authErr) return authErr;

  return serviceResponse('AgentService', {
    agentId: 'AGT-7742',
    agentName: 'National Brokerage Group',
    commissionSchedule: 'Schedule A',
    firstYear: { rate: 0.55, amount: 2681.25 },
    renewal: { rate: 0.05, years: 9 },
    override: { rate: 0.10, managerId: 'AGT-1001' },
    bonusEligible: true,
  });
}
