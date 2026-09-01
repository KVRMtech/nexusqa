import { NextRequest } from 'next/server';
import { serviceResponse, requireAuth } from '@/lib/api-factory';

export async function POST(request: NextRequest) {
  const authErr = requireAuth(request, 'NotificationService');
  if (authErr) return authErr;

  const body = await request.json();

  return serviceResponse('NotificationService', {
    notificationId: 'ntf-' + crypto.randomUUID().slice(0, 8),
    type: body.type,
    channel: body.channel,
    recipient: body.recipient,
    status: 'dispatched',
    dispatchedAt: new Date().toISOString(),
  });
}
