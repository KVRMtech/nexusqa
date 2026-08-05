import { NextRequest } from 'next/server';
import { serviceResponse, requireAuth } from '@/lib/api-factory';

export async function POST(request: NextRequest) {
  const authErr = requireAuth(request, 'PaymentService');
  if (authErr) return authErr;

  return serviceResponse('PaymentService', {
    receiptId: 'rcp-' + crypto.randomUUID().slice(0, 8),
    receiptNumber: 'RCP-2026-' + Math.floor(Math.random() * 100000),
    pdfUrl: '/documents/receipts/rcp-2026.pdf',
    emailSent: true,
  });
}
