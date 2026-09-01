import { NextRequest, NextResponse } from 'next/server';
import { applications, vendorOrders, suspenseItems } from '@/lib/data';

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const app = applications.find(a => a.id === id);
  if (!app) return NextResponse.json({ error: 'not_found', message: `Application ${id} not found` }, { status: 404 });

  return NextResponse.json({
    data: {
      ...app,
      vendorOrders: vendorOrders.filter(v => v.applicationId === id),
      suspenseItems: suspenseItems.filter(s => s.applicationId === id),
    },
    _links: {
      self: `/api/v1/applications/${id}`,
      collection: '/api/v1/applications',
      vendorOrders: `/api/v1/applications/${id}/vendor-orders`,
    },
  });
}

export async function PATCH(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const app = applications.find(a => a.id === id);
  if (!app) return NextResponse.json({ error: 'not_found', message: `Application ${id} not found` }, { status: 404 });

  const body = await request.json();
  const updated = { ...app, ...body, lastUpdated: new Date().toISOString() };

  return NextResponse.json({ data: updated, message: 'Application updated successfully' });
}
