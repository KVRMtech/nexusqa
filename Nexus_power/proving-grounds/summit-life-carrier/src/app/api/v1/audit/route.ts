import { NextRequest } from 'next/server';
import { serviceResponse, requireAuth } from '@/lib/api-factory';

export async function POST(request: NextRequest) {
  const authErr = requireAuth(request, 'AuditService');
  if (authErr) return authErr;

  const body = await request.json();

  return serviceResponse('AuditService', {
    auditId: 'aud-' + crypto.randomUUID().slice(0, 8),
    action: body.action,
    entity: body.entity,
    entityId: body.entityId,
    performedBy: body.performedBy,
    ipAddress: '10.0.1.42',
    timestamp: new Date().toISOString(),
  }, { status: 201 });
}
