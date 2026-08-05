import { NextRequest } from 'next/server';
import { serviceResponse, requireAuth } from '@/lib/api-factory';

const SERVICE = 'payment-method-validation';

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ methodId: string }> },
) {
  const denied = requireAuth(request, SERVICE);
  if (denied) return denied;

  const { methodId } = await params;
  return serviceResponse(SERVICE, {
    methodId,
    valid: true,
    bankVerified: true,
    nameMatch: true,
    accountStatus: 'active',
  });
}
