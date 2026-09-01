import { NextRequest, NextResponse } from 'next/server';
import { policies } from '@/lib/data';

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const policy = policies.find(p => p.id === id);
  if (!policy) return NextResponse.json({ error: 'not_found', message: `Policy ${id} not found` }, { status: 404 });

  return NextResponse.json({
    data: policy,
    _links: {
      self: `/api/v1/policies/${id}`,
      collection: '/api/v1/policies',
      transactions: `/api/v1/policies/${id}/transactions`,
    },
  });
}
