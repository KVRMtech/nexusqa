import { NextRequest } from 'next/server';
import { serviceResponse, requireAuth } from '@/lib/api-factory';

const SERVICE = 'tax-withholding';

export async function POST(request: NextRequest) {
  const denied = requireAuth(request, SERVICE);
  if (denied) return denied;

  return serviceResponse(SERVICE, {
    applicableWithholding: false,
    federalRate: 0,
    stateRate: 0,
    reason: 'life_insurance_premium_not_taxable',
    irs1099Required: false,
  });
}
