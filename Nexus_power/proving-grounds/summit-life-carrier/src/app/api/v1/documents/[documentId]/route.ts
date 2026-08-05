import { NextRequest } from 'next/server';
import { serviceResponse, requireAuth } from '@/lib/api-factory';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ documentId: string }> }
) {
  const authErr = requireAuth(request, 'DocumentService');
  if (authErr) return authErr;

  const { documentId } = await params;

  return serviceResponse('DocumentService', {
    documentId,
    type: 'application_packet',
    status: 'generated',
    format: 'pdf',
    pages: 14,
    sizeBytes: 312000,
    createdAt: '2026-07-25T11:00:00Z',
    downloadUrl: '/documents/' + documentId + '.pdf',
    createdBy: 'usr-002',
  });
}
