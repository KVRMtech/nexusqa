import { NextRequest, NextResponse } from 'next/server';
import { claims } from '@/lib/data';

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const claim = claims.find(c => c.id === id);
  if (!claim) return NextResponse.json({ error: 'not_found', message: `Claim ${id} not found` }, { status: 404 });

  return NextResponse.json({
    data: claim,
    _links: {
      self: `/api/v1/claims/${id}`,
      collection: '/api/v1/claims',
      documents: `/api/v1/claims/${id}/documents`,
      payments: `/api/v1/claims/${id}/payments`,
    },
  });
}

export async function PUT(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const claim = claims.find(c => c.id === id);
  if (!claim) return NextResponse.json({ error: 'not_found', message: `Claim ${id} not found` }, { status: 404 });

  const body = await request.json();
  const updated = { ...claim, ...body };

  return NextResponse.json({ data: updated, message: 'Claim updated successfully' });
}
