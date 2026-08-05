import { NextResponse } from 'next/server';

function txnId(): string {
  const ts = Date.now().toString(36);
  const rand = Math.random().toString(36).slice(2, 8);
  return `txn-${ts}-${rand}`;
}

export function serviceResponse(
  serviceName: string,
  result: Record<string, unknown>,
  options?: { status?: number; code?: string }
) {
  return NextResponse.json({
    meta: {
      service: serviceName,
      transactionId: txnId(),
      timestamp: new Date().toISOString(),
      version: 'v1',
    },
    status: options?.code ?? 'completed',
    data: result,
  }, { status: options?.status ?? 200 });
}

export function errorResponse(
  serviceName: string,
  message: string,
  status: number = 400,
) {
  return NextResponse.json({
    meta: {
      service: serviceName,
      transactionId: txnId(),
      timestamp: new Date().toISOString(),
    },
    status: 'error',
    error: { code: status, message },
  }, { status });
}

export function extractBearer(request: Request): string | null {
  const auth = request.headers.get('authorization');
  if (!auth?.startsWith('Bearer ')) return null;
  return auth.slice(7);
}

export function requireAuth(request: Request, serviceName: string) {
  if (!extractBearer(request)) {
    return errorResponse(serviceName, 'Bearer token required', 401);
  }
  return null;
}
