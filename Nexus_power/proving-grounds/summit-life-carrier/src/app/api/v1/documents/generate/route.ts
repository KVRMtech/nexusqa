import { NextRequest } from 'next/server';
import { serviceResponse, errorResponse, requireAuth } from '@/lib/api-factory';

const VALID_TYPES = ['application_packet', 'illustration', 'disclosure', 'receipt'];

export async function POST(request: NextRequest) {
  const authErr = requireAuth(request, 'DocumentService');
  if (authErr) return authErr;

  const body = await request.json();
  if (!body.type || !VALID_TYPES.includes(body.type)) {
    return errorResponse('DocumentService', `Invalid type. Must be one of: ${VALID_TYPES.join(', ')}`);
  }

  return serviceResponse('DocumentService', {
    documentId: 'doc-' + crypto.randomUUID().slice(0, 8),
    type: body.type,
    status: 'generated',
    format: 'pdf',
    pages: 12,
    sizeBytes: 245000,
    downloadUrl: '/documents/' + body.type + '.pdf',
    generatedAt: new Date().toISOString(),
  });
}
