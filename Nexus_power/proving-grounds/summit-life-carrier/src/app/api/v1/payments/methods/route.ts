import { NextRequest } from 'next/server';
import { serviceResponse, errorResponse, requireAuth } from '@/lib/api-factory';

const SERVICE = 'payment-methods';

const SEED_METHODS = [
  { id: 'pm-001', customerId: 'cust-7f3a9b2e', type: 'bank_account', last4: '4821', bankName: 'Chase', accountType: 'checking', isDefault: true, status: 'verified' },
  { id: 'pm-002', customerId: 'cust-7f3a9b2e', type: 'bank_account', last4: '9033', bankName: 'Bank of America', accountType: 'savings', isDefault: false, status: 'verified' },
];

export async function GET(request: NextRequest) {
  const denied = requireAuth(request, SERVICE);
  if (denied) return denied;

  const customerId = request.nextUrl.searchParams.get('customerId');
  if (!customerId) return errorResponse(SERVICE, 'customerId query parameter required');

  const methods = SEED_METHODS.filter(m => m.customerId === customerId);
  return serviceResponse(SERVICE, { paymentMethods: methods });
}

export async function POST(request: NextRequest) {
  const denied = requireAuth(request, SERVICE);
  if (denied) return denied;

  const body = await request.json();
  const method = {
    id: `pm-${crypto.randomUUID().slice(0, 8)}`,
    ...body,
    status: 'pending_verification',
    createdAt: new Date().toISOString(),
  };
  return serviceResponse(SERVICE, { paymentMethod: method }, { status: 201, code: 'created' });
}
